#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""消息派发 + INTERACTION 处理（@handler 注册入口）

模块 import 时通过装饰器把 handler 注册进框架的 _pending_handlers 列表，
随后 PluginManager 收集到本插件名下。
"""

from __future__ import annotations
import os
import sys
import time
import asyncio
import threading

from core.plugin.decorators import handler
from core.base.logger import get_logger, PLUGIN
from core.message.event import (
    GROUP_AT_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
    INTERACTION_CREATE,
)

from . import state, quota, helpers, boot, buttons, uploader, userdb
from .webui import message_log

log = get_logger(PLUGIN, 'LGTBot')

# 菜单 logo 文件路径（仓库内置）
_MENU_LOGO_PATH = os.path.join(boot.PLUGIN_DIR, 'images', 'logo_transparent_colorful.png')


async def _resolve_menu_logo() -> dict | None:
    """读取 images/logo_transparent_colorful.png 并通过图床上传 + 23h 缓存。

    任何异常都吞掉返回 None：菜单 logo 仅是装饰，不应阻断欢迎菜单回复。
    返回的字典含 ``url`` / ``width`` / ``height``，可直接拼 markdown。
    """
    try:
        if not os.path.isfile(_MENU_LOGO_PATH):
            return None
        with open(_MENU_LOGO_PATH, 'rb') as f:
            data = f.read()
        return await uploader.upload_image_cached(
            data, 'menu_logo.png', cache_key='menu:logo')
    except Exception as e:
        log.debug(f'菜单 logo 解析失败: {e}')
        return None

# 本插件监听的消息事件类型
_LGT_MSG_EVENTS = frozenset({
    GROUP_AT_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
})


# ──────── 消息派发 ────────────────────────────────────────────────────────

@handler(r'.*', name='LGTBot 消息派发', priority=-100, event_types=_LGT_MSG_EVENTS)
async def lgtbot_dispatch(event, match):
    """将所有群 @ / 私聊消息派发给 LGTBot 引擎（不消费事件，其他插件仍可处理）"""
    if not state.started:
        return

    content = (event.content or '').strip()
    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''

    # 用户缓存：昵称 + 头像 URL（事件携带 username + 用 appid 推导头像）
    # 走 userdb 落盘，5 分钟批量 flush；name / avatar 任一为空时不会覆盖 DB 旧值
    if uid:
        appid = event.appid or ''
        avatar = helpers.QQ_AVATAR_URL.format(appid=appid, openid=uid) if appid else ''
        userdb.mark_dirty(uid, name=getattr(event, 'username', '') or '', avatar=avatar)

    # 用户消息 → 用 msg_id 刷新被动引用配额（5 条新额度）
    appid_str = event.appid or ''
    if event.message_id:
        if event.is_group and gid:
            quota.refresh_ref(helpers.target_key(gid, False), 'msg_id', event.message_id, appid_str)
        if uid:
            quota.refresh_ref(helpers.target_key(uid, True), 'msg_id', event.message_id, appid_str)

    # 空消息（仅 @bot）→ 回欢迎菜单，不进 LGTBot 引擎
    if not content:
        message_log.log_incoming(uid, gid, '(空消息：触发欢迎菜单)')
        try:
            logo = await _resolve_menu_logo()
            if logo and logo.get('url'):
                md = (f'![logo #{logo["width"]}px #{logo["height"]}px]'
                      f'({logo["url"]})\n\n'
                      + buttons.MENU_TEXT_BODY)
            else:
                md = buttons.MENU_TEXT_HEADER + buttons.MENU_TEXT_BODY
            await event.reply(md, buttons=buttons.MENU_BUTTONS)
            message_log.log_outgoing(gid or uid, not (event.is_group and gid),
                                     '[欢迎菜单]')
        except Exception as e:
            log.warning(f'菜单回复失败: {e}')
        return

    # 按钮附加完全交给 C++ 桥接层根据消息内容判断（见
    # LGTBot_ElainaBot.cc::ClassifyMatchEvent）—— 此处不再做命令模式匹配,
    # 这样 /新游戏 触发的「先解散后新建」两条消息也不会把按钮挂错位置。

    message_log.log_incoming(uid, gid if event.is_group else '', content)

    # 派发给 C++ 引擎（独立线程，避免 C++ match-lock 与 asyncio loop 互锁）
    try:
        if event.is_group and gid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_public_message,
                args=(content, uid, gid),
                daemon=True,
            ).start()
        elif event.is_direct and uid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_private_message,
                args=(content, uid),
                daemon=True,
            ).start()
    except Exception as e:
        log.warning(f'派发消息失败: {e}')


# ──────── INTERACTION 收割机：刷新引用配额 ─────────────────────────────────
# 「🔄 刷新」按钮（type=1 callback，data='__lgt_relay__'）被点击时触发：
#   1. 立即 ack_interaction(code=0) —— 让客户端不再显示"请求超时"
#   2. 用本次 INTERACTION 的 event_id 刷新对应 target 的引用配额
#   3. 唤醒可能正在 _wait_and_consume 中等待的发送协程（refresh_ref 内做）
#
# 用户体验：点击按钮无明显反应（仅短暂 toast），但被动消息能力立即恢复 5 条

@handler(r'.*', name='LGTBot 刷新按钮回调', priority=-200, event_types={INTERACTION_CREATE})
async def lgtbot_interaction_relay(event, match):
    try:
        await event.ack_interaction(code=0)
    except Exception:
        pass

    if not event.event_id:
        return
    appid_str = event.appid or ''
    if event.is_group and event.group_id:
        quota.refresh_ref(helpers.target_key(event.group_id, False),
                          'event_id', event.event_id, appid_str)
    if event.user_id:
        quota.refresh_ref(helpers.target_key(event.user_id, True),
                          'event_id', event.event_id, appid_str)


# ──────── 重启逻辑(命令 / WebUI 共用) ─────────────────────────────────────
# 拆成两步,让命令 handler 和 WebUI 重启按钮共用同一原子语义:
#   1. check_and_prepare_restart()  同步检查 + (若可)干净释放 C++ 引擎
#   2. schedule_exec_after(delay)    异步任务:延迟后 os.execv 整个 Python 进程
# 调用方在两者之间插入「响应已发出」步骤(event.reply 或 HTTP response),保证
# 用户看到的提示先送达再换进程。
#
# 为什么必须 exec 而不是 plugin_manager.reload:CPython 扩展模块一经 import
# 就常驻 sys.modules,plugin 热重载只重跑 Python 装饰器、并不 dlclose
# LGTBot_ElainaBot.so;同样,libbot_core.so 与各 libgame.so 一经引擎 dlopen
# 也驻留进程。要让 build.sh 重编的 C++ 二进制真正生效,只能换一个全新 Python
# 进程 —— 这正是本插件 /重启 与 WebUI 重启按钮的诉求(主框架的 /框架重启 也
# 是这个套路,只是没有 LGTBot 活跃对局的预检)。

def check_and_prepare_restart() -> tuple[bool, str]:
    """同步检查并准备重启。返回 (是否可重启, 给用户的提示文案)。

    · 引擎未加载   → (False, 无需重启提示)
    · 活跃 match   → (False, 拒绝原因);引擎保持运行
    · 否则         → (True, 正在重启提示),并已干净释放 C++ 引擎、把
                     state.started / boot.is_engine_running 置 False;
                     调用方接下来必须立刻调度 exec,否则插件会处于
                     「Python 还在 / C++ 引擎已无」的半残状态。
    """
    if not boot.LGTBOT_AVAILABLE:
        return (False, 'ℹ️ LGTBot 引擎未加载，无需重启。')
    if not boot.LGTBot_ElainaBot.release_bot_if_not_processing_games():
        return (False, '⚠️ 当前存在进行中的游戏，请等待对局结束。')
    state.started = False
    boot.mark_engine_running(False)
    return (True, '🔁 LGTBot 正在重启（重新加载全部 C++ 引擎与游戏插件）…')


def schedule_exec_after(delay: float = 0.5, on_failure=None) -> None:
    """延迟 delay 秒后 ``os.execv`` 自身。延迟用于让此前的响应送达。

    on_failure 可选,在 execv 失败(罕见,通常仅 sys.executable 丢失时)被调用,
    支持 sync 或 async。任务挂在当前 event loop 上,不依赖任何 Python 模块
    全局状态(本插件被销毁也不影响已经入队的 coroutine)。
    """
    async def _do():
        await asyncio.sleep(delay)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log.error(f'os.execv 重启失败,引擎已释放但进程未替换,需手动重启: {e}')
            if on_failure is not None:
                try:
                    if asyncio.iscoroutinefunction(on_failure):
                        await on_failure()
                    else:
                        on_failure()
                except Exception:
                    pass
    try:
        asyncio.get_running_loop().create_task(_do())
    except RuntimeError:
        log.error('无运行中 asyncio loop,无法调度 exec')


# ──────── 主人专属:本插件全套重启指令 ────────────────────────────────────
# 触发文本 "/重启"(框架自动剥前导 /，regex 不带 / 同样匹配 "重启")。
# `owner_only=True` 框架内置:非主人触发时直接回 owner_only 模板,不进函数体。
# WebUI 重启按钮也走同一对 helper —— 见 webui/message_log.py::_render_restart。

@handler(r'^重启$',
         name='LGTBot 插件重启',
         owner_only=True,
         event_types=_LGT_MSG_EVENTS,
         priority=100)
async def lgtbot_restart(event, match):
    """主人发起的本插件「全套」重启 —— exec 整个 Python 进程,把 bridge .so /
    libbot_core.so / 全部 libgame.so 重新 dlopen,等价于让 build.sh 重编后的
    C++ 二进制即刻生效。
    """
    ok, msg = check_and_prepare_restart()
    await event.reply(msg)
    if not ok:
        return

    async def _on_fail():
        try:
            await event.reply('❌ 重启时发生错误，引擎已释放但进程未替换，需手动重启，详情请查看控制台日志。')
        except Exception:
            pass

    schedule_exec_after(0.5, on_failure=_on_fail)
