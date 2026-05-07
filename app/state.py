#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""共享运行时状态 —— 多个子模块共享的可变全局变量。

设计：Python 模块本身就是单例，把所有跨模块共享的状态集中在这里，
其他子模块通过 `from . import state; state.xxx = ...` 读写，避免到处传参。
"""

from __future__ import annotations
import asyncio
from typing import Optional

# 由 main.py 在 module top-level 捕获（PluginManager 仅在加载窗口期 set 此值）
plugin_ctx = None

# 由 @on_load 设置，C++ 工作线程通过 run_coroutine_threadsafe 调度到此循环
event_loop: Optional[asyncio.AbstractEventLoop] = None

# LGTBot 引擎是否已成功 start
started: bool = False

# 用户信息缓存：uid → {'name': str, 'avatar': str}
user_cache: dict[str, dict] = {}

# 命令触发后给 LGTBot 下一条文本回复附"加入/退出"按钮（一次性消费）
# key = 'g:<gid>' / 'u:<uid>'
pending_buttons: dict[str, list] = {}
