#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LGTBot × ElainaBot 集成插件 (QQ Official Bot) —— 入口文件

各功能拆分到 app/ 子模块（详见 app/__init__.py），本文件只负责：

  1. 声明插件元数据
  2. 在 module top-level 捕获 PluginContext（PluginManager 仅在加载窗口期暴露）
  3. 顺序触发各子模块加载（boot 第一个，处理 C++ 扩展副作用）
  4. 实现 @on_load / @on_unload 生命周期

部署：见同目录 DEPLOY.md
"""

__plugin_meta__ = {
    'name': 'LGTBot 机器人',
    'author': '铁蛋',
    'description': '基于 LGTBot C++ 引擎的游戏机器人',
    'version': '1.0.0',
    'github': 'https://github.com/slontia/lgtbot',
}

import os
import asyncio

from core.plugin.decorators import on_load, on_unload
from core.plugin import context as _ctx_mod
from core.base.logger import get_logger, PLUGIN

# ──────── 关键步骤：捕获 PluginContext ────────────────────────────────────
# PluginManager 加载流程：
#   1. _ctx_mod.ctx = plugin_ctx   ← set
#   2. 执行本文件顶层代码（这里读到 ctx）
#   3. _ctx_mod.ctx = None         ← reset
#   4. 调用 @on_load 函数（此时 ctx 已是 None）
# 所以必须在模块顶层捕获，不能延迟到 @on_load 内
from plugins.lgtbot_qq.app import state as _state
_state.plugin_ctx = _ctx_mod.ctx

# ──────── 触发各子模块加载 ────────────────────────────────────────────────
# 顺序敏感：boot 必须最先（处理 C++ 扩展导入 + chdir + RTLD_GLOBAL 副作用），
# 其他模块依赖 boot.lgtbot_qq / boot.BUILD_DIR / boot.LGTBOT_AVAILABLE 等
from plugins.lgtbot_qq.app import boot              # noqa: F401  C++ 引擎与路径
from plugins.lgtbot_qq.app.webui import message_log # noqa: F401  Web 面板侧边栏页面
from plugins.lgtbot_qq.app import dispatcher        # noqa: F401  @handler 注册（消息派发 + INTERACTION）
from plugins.lgtbot_qq.app import callbacks         # C++ 回调（被 lgtbot_qq.start 注入）
from plugins.lgtbot_qq.app import config as _config

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 生命周期 ────────────────────────────────────────────────────────

@on_load
async def _setup():
    # 注册 Web 面板拓展页（无论 LGTBot 是否可用，让用户先能看到日志页）
    message_log.register()

    # 加载 / 创建配置（让 Web UI「插件 → 配置」入口立刻可见 config.yaml）
    admins = _config.load_plugin_config()

    if not boot.LGTBOT_AVAILABLE:
        log.error('=' * 60)
        log.error(f'lgtbot_qq C++ 扩展未编译或导入失败：{boot.IMPORT_ERROR}')
        log.error('请先按 plugins/lgtbot_qq/DEPLOY.md 编译后再启动')
        log.error('=' * 60)
        return

    # 捕获主事件循环 —— C++ 工作线程通过 run_coroutine_threadsafe 调度到此循环
    _state.event_loop = asyncio.get_running_loop()

    # 检查游戏目录是否存在已编译的游戏 .so
    if not os.path.isdir(boot.GAME_PATH):
        log.error('=' * 60)
        log.error(f'游戏插件目录不存在: {boot.GAME_PATH}')
        log.error('请先在 plugins/lgtbot_qq/ 下执行 bash build.sh 完成编译')
        log.error('=' * 60)
        return
    game_count = sum(
        1 for d in os.listdir(boot.GAME_PATH)
        if os.path.isfile(os.path.join(boot.GAME_PATH, d, 'libgame.so'))
    )
    if game_count == 0:
        log.error('=' * 60)
        log.error(f'未在 {boot.GAME_PATH} 下发现任何 libgame.so')
        log.error('请检查 build.sh 是否带 --no-games 关闭了游戏编译')
        log.error('=' * 60)
        return

    log.info(f'初始化 LGTBot 引擎: 游戏数={game_count}, db={boot.DB_PATH}, conf={boot.CONF_PATH}')
    ok = boot.lgtbot_qq.start(
        boot.GAME_PATH, boot.DB_PATH, boot.CONF_PATH, boot.IMG_PATH, admins,
        callbacks.cb_get_user_name, callbacks.cb_get_user_avatar_url,
        callbacks.cb_send_text_message, callbacks.cb_send_image_message,
    )
    if not ok:
        log.error('LGTBot 引擎启动失败 (查看上方 stderr 输出)')
        return

    _state.started = True
    log.info('✅ LGTBot 引擎已就绪')


@on_unload
async def _teardown():
    # 注销 Web 面板页面（无论引擎状态如何）
    try:
        message_log.unregister()
    except Exception:
        pass

    if not _state.started or not boot.LGTBOT_AVAILABLE:
        return
    if boot.lgtbot_qq.release_bot_if_not_processing_games():
        _state.started = False
        log.info('LGTBot 引擎已安全关闭')
    else:
        log.warning('存在进行中的游戏 —— 引擎未释放，强制退出可能丢失对局状态')
