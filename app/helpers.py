#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用辅助：sender 查找 / 跨线程协程执行 / target_key / mention 美化"""

from __future__ import annotations
import re
import asyncio

from core.base.logger import get_logger, PLUGIN
from . import state, userdb

log = get_logger(PLUGIN, 'LGTBot')

# QQ 官方机器人头像直链（未在 SDK 文档，但实测可用）
# 尺寸：40 / 100 / 140 / 640；LGTBot 渲染头像约 100x100
QQ_AVATAR_URL = 'https://q.qlogo.cn/qqapp/{appid}/{openid}/100'

# 媒体消息（msg_type=7）的 content 字段是 QQ 协议层不解析 <@openid> 的纯文本，
# 图文同条场景下把 mention 退化为 "@昵称"（损失：无 ping 通知）
_MENTION_RE = re.compile(r'<@([^>\s]+)>')


def target_key(target_id: str, is_uid: bool) -> str:
    """统一 target 标识：群消息 'g:<gid>'，私聊 'u:<uid>'"""
    return ('u:' if is_uid else 'g:') + target_id


def humanize_mentions(text: str) -> str:
    """把 <@openid> 转成 @昵称（用于图文消息 content）

    QQ msg_type=7 的 content 不解析 <@openid> 提及语法，会原样显示为字面字符串。
    本函数从 ``userdb`` (SQLite) 取对应昵称替换，保持图文单条消息的同时让
    文字可读。DB 未命中时退化为截短 uid 占位。
    """
    if not text or '<@' not in text:
        return text

    def _repl(m):
        uid = m.group(1)
        name = userdb.get_name(uid)
        if name:
            return f'@{name}'
        # DB 未命中：截短 openid 占位
        return f'@{uid[:6]}…' if len(uid) > 6 else f'@{uid}'

    return _MENTION_RE.sub(_repl, text)


def get_sender(appid: str = ''):
    """从 BotManager 全局引用获取 MessageSender。

    appid 为空时返回任一可用 sender（单 Bot 场景足够）。
    """
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is None or not _bot_manager_ref._bots:
            return None
        if appid and appid in _bot_manager_ref._bots:
            return _bot_manager_ref._bots[appid].sender
        return next(iter(_bot_manager_ref._bots.values())).sender
    except Exception as e:
        log.warning(f'获取 sender 失败: {e}')
        return None


def run_coro_blocking(coro, timeout: float = 15.0):
    """C++ 工作线程 → asyncio 事件循环 的安全桥接（阻塞等待结果）"""
    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.warning('事件循环不可用，丢弃协程')
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)
    except Exception as e:
        log.warning(f'协程执行异常: {e}')
        return None


def is_full_volume_group(gid: str, appid: str = '') -> bool:
    """判断 ``gid`` 在某个 bot 配置里是否开了「全量消息」(non-AT 接收)。

    主框架配置位 ``non_at_message.enabled`` (全局开关) + ``non_at_message
    .group_whitelist`` (白名单)。任一为 True 即视为全量群。

    Args:
        gid: 群 openid
        appid: bot 的 appid。空字符串时遍历所有已加载 bot —— 任一 bot 视该
               群为全量即返回 True。callbacks 路径上若已从 quota ref 拿到 appid
               应优先传入,语义更精确。

    ``cfg.get_bot_setting`` 自带 5s mtime 检查的缓存(``core/base/config.py``),
    Web UI 改配置后约 5 秒生效,无需手动 invalidate。
    """
    if not gid:
        return False
    try:
        from core.base.config import cfg
    except Exception:
        return False

    def _check(aid: str) -> bool:
        if cfg.get_bot_setting(aid, 'non_at_message.enabled', False):
            return True
        wl = cfg.get_bot_setting(aid, 'non_at_message.group_whitelist', []) or []
        return gid in wl

    if appid:
        return _check(appid)

    # 无 appid 上下文(配额耗尽 fallback 路径):扫所有已加载 bot
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref and _bot_manager_ref._bots:
            return any(_check(aid) for aid in _bot_manager_ref._bots)
    except Exception:
        pass
    return False
