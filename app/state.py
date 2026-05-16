#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""共享运行时状态 —— 多个子模块共享的可变全局变量。

设计：Python 模块本身就是单例，把所有跨模块共享的状态集中在这里，
其他子模块通过 `from . import state; state.xxx = ...` 读写，避免到处传参。

跨插件热重载：`pending_buttons` 等可变容器从 `boot._get_persistent()` 取得，
挂在 C++ 扩展模块对象上常驻进程，新旧模块实例引用同一份字典 —— 这样
热重载时即便旧 callback 还在用旧 state 对象，读到的还是同一份数据。

用户昵称 / 头像缓存改走 SQLite 持久化（见 ``userdb.py``），不在此模块。
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
pending_buttons: dict[str, list] = _p['pending_buttons']  # 'g:gid'/'u:uid' → [[btn]]
# /新游戏 X 时记录;/加入 时回查给「📜 规则」按钮用 —— 跨热重载持久,
# 进程重启即丢(失忆群按 /加入 时该按钮会缺规则,无大碍)。
current_game: dict[str, str] = _p.setdefault('current_game', {})  # target_key → 游戏名
# 运行时观测到 GROUP_MESSAGE_CREATE 事件的群 openid 集合 —— QQ 在 bot 管理后台
# 给某群开了「全量推送」时,任何事件(包括 is_at_self=True 的 @ 消息)都会以
# GROUP_MESSAGE_CREATE 投递。框架的 ``non_at_message.{enabled,group_whitelist}``
# 是「是否把 non-AT 派给插件」的二级开关,QQ 没开的话框架 enabled=true 也没用;
# QQ 开了但框架没开,我们 ignore_at_check=True 还是收得到。所以**真·全量群**
# 标记应该来自实际投递的事件 —— dispatcher 看到 GROUP_MESSAGE_CREATE 就把 gid
# 加进来,helpers.is_full_volume_group 优先看这个集合,再退回框架配置兜底。
full_volume_groups: set[str] = _p.setdefault('full_volume_groups', set())
