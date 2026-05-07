#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""消息派发 + INTERACTION 处理（@handler 注册入口）

模块 import 时通过装饰器把 handler 注册进框架的 _pending_handlers 列表，
随后 PluginManager 收集到本插件名下。
"""

from __future__ import annotations
import time
import threading

from core.plugin.decorators import handler
from core.base.logger import get_logger, PLUGIN
from core.message.event import (
    GROUP_AT_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
    INTERACTION_CREATE,
)

from . import state, quota, helpers, boot, buttons
from .webui import message_log

log = get_logger(PLUGIN, 'LGTBot')

# 本插件监听的消息事件类型
_LGT_MSG_EVENTS = frozenset({
    GROUP_AT_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
})


# ──────── 消息派发 ────────────────────────────────────────────────────────

@handler(r'.*', priority=-100, event_types=_LGT_MSG_EVENTS)
async def lgtbot_dispatch(event, match):
    """将所有群 @ / 私聊消息派发给 LGTBot 引擎（不消费事件，其他插件仍可处理）"""
    if not state.started:
        return

    content = (event.content or '').strip()
    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''

    # 用户缓存：昵称 + 头像 URL（事件携带 username + 用 appid 推导头像）
    if uid:
        appid = event.appid or ''
        avatar = helpers.QQ_AVATAR_URL.format(appid=appid, openid=uid) if appid else ''
        old = state.user_cache.get(uid, {})
        state.user_cache[uid] = {
            'name': getattr(event, 'username', '') or old.get('name', ''),
            'avatar': avatar or old.get('avatar', ''),
        }

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
            await event.reply(buttons.MENU_TEXT, buttons=buttons.MENU_BUTTONS)
            message_log.log_outgoing(gid or uid, not (event.is_group and gid),
                                     '[欢迎菜单]')
        except Exception as e:
            log.warning(f'菜单回复失败: {e}')
        return

    # 命令检测：执行 /新游戏 /加入 /退出 时，给 LGTBot 下一条文本回复附按钮
    if buttons.GAME_ACTION_RE.match(content):
        target = gid if (event.is_group and gid) else uid
        if target:
            state.pending_buttons[helpers.target_key(target, not (event.is_group and gid))] = \
                buttons.GAME_ACTION_BUTTONS

    message_log.log_incoming(uid, gid if event.is_group else '', content)

    # 派发给 C++ 引擎（独立线程，避免 C++ match-lock 与 asyncio loop 互锁）
    try:
        if event.is_group and gid:
            threading.Thread(
                target=boot.lgtbot_qq.on_public_message,
                args=(content, uid, gid),
                daemon=True,
            ).start()
        elif event.is_direct and uid:
            threading.Thread(
                target=boot.lgtbot_qq.on_private_message,
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

@handler(r'.*', priority=-200, event_types={INTERACTION_CREATE})
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
