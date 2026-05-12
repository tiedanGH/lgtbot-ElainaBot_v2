#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LGTBot 消息日志缓冲 —— 纯数据层

只负责维护本插件收到 / 发出的消息日志(环形队列,默认上限 500 条)以及对外
暴露 log_incoming / log_outgoing / get_logs / clear_logs。页面渲染与
WebUI 注册的逻辑已挪到 ``webui/main.py``,由它再委托给 ``page_logs.py`` /
``page_users.py`` 生成各标签的内容。

跨插件热重载:日志 deque 与锁挂在 C++ 扩展上常驻进程(``boot._get_persistent``),
旧 callback 写入的日志在新 dispatcher 注册的页面里也能被读到。
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from .. import boot

# ──────── 日志缓冲(跨插件热重载共享)──────────────────────────────────────

_MAX_LOGS = 500
_p = boot._get_persistent()
if 'logs_deque' not in _p:
    _p['logs_deque'] = deque(maxlen=_MAX_LOGS)
    _p['logs_lock'] = Lock()
_logs: deque = _p['logs_deque']
_lock: Lock = _p['logs_lock']


def log_incoming(uid: str, gid: str, content: str):
    """记录收到的消息(来自 QQ 玩家,即将转发给 LGTBot 引擎)。"""
    _append({
        'time': time.time(),
        'direction': 'in',
        'kind': 'group' if gid else 'private',
        'uid': uid or '',
        'gid': gid or '',
        'content': content or '',
        'image': False,
    })


def log_outgoing(target_id: str, is_uid: bool, content: str, *, image: bool = False):
    """记录发出的消息(LGTBot 引擎 → QQ)。"""
    _append({
        'time': time.time(),
        'direction': 'out',
        'kind': 'private' if is_uid else 'group',
        'uid': target_id if is_uid else '',
        'gid': '' if is_uid else target_id,
        'content': content or '',
        'image': image,
    })


def _append(entry: dict):
    with _lock:
        _logs.append(entry)


def get_logs() -> list:
    """快照当前所有日志。"""
    with _lock:
        return list(_logs)


def clear_logs():
    with _lock:
        _logs.clear()
