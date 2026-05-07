#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""共享运行时状态 —— 多个子模块共享的可变全局变量。

设计：Python 模块本身就是单例，把所有跨模块共享的状态集中在这里，
其他子模块通过 `from . import state; state.xxx = ...` 读写，避免到处传参。

跨插件热重载：`user_cache` / `pending_buttons` 等可变容器从 `boot._get_persistent()`
取得，挂在 C++ 扩展模块对象上常驻进程，新旧模块实例引用同一份字典 ——
这样热重载时即便旧 callback 还在用旧 state 对象，读到的还是同一份数据。
"""

from __future__ import annotations
import asyncio
from typing import Optional

from . import boot

# 由 main.py 在 module top-level 捕获（PluginManager 仅在加载窗口期 set 此值）
plugin_ctx = None

# 由 @on_load 设置，C++ 工作线程通过 run_coroutine_threadsafe 调度到此循环
# （asyncio loop 本身跨重载不变，每次 @on_load 重新捕获是 OK 的）
event_loop: Optional[asyncio.AbstractEventLoop] = None

# LGTBot 引擎是否已成功 start（per-load，与 boot.is_engine_running() 配合使用）
started: bool = False

# ── 跨重载共享的可变容器（取自 boot 持久化字典）──
_p = boot._get_persistent()
user_cache: dict[str, dict] = _p['user_cache']           # uid → {'name', 'avatar'}
pending_buttons: dict[str, list] = _p['pending_buttons']  # 'g:gid'/'u:uid' → [[btn]]
