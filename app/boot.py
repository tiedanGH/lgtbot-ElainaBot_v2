#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C++ 扩展导入 + 路径常量

import 副作用顺序敏感，需要在所有依赖 lgtbot_qq C++ 扩展的子模块之前加载：

  1. 把插件目录加入 sys.path，让 `import lgtbot_qq` 能找到 .so
  2. 临时 chdir 到 build/，让 libbot_core.so 加载时静态初始化的
     `k_markdown2image_path = current_path() / "markdown2image"` 捕获到正确路径
  3. 设置 RTLD_GLOBAL 标志，使 libbot_core.so 静态依赖的 glog/gflags 等符号
     对后续 dlopen 的 libgame.so 可见（否则报 undefined symbol: ...LogMessage...）
  4. import 完成后立即恢复 CWD 和 dlopen flags，避免影响主框架其他相对路径
"""

from __future__ import annotations
import os
import sys

# ──────── 路径常量 ────────────────────────────────────────────────────────
# __file__ → plugins/lgtbot_qq/app/boot.py  → 插件根目录是其上一级的上一级
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR  = os.path.join(PLUGIN_DIR, 'build')
DATA_DIR   = os.path.join(PLUGIN_DIR, 'data')
GAME_PATH  = os.path.join(BUILD_DIR, 'plugins')   # 各 libgame.so 所在目录
DB_PATH    = os.path.join(DATA_DIR, 'lgtbot.db')
IMG_PATH   = os.path.join(DATA_DIR, 'images')
CONF_PATH  = os.path.join(DATA_DIR, 'lgtbot.json')   # LGTBot 引擎自身的配置

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMG_PATH, exist_ok=True)


# ──────── LGTBot 引擎配置文件预生成 ───────────────────────────────────────
# 启动时若 data/lgtbot.json 不存在则写入空 JSON。引擎自身在 LoadConfig 阶段
# 也会兜底创建，这里前置一次让 Web UI「插件 → 配置」入口立刻可见可编辑。
def _ensure_lgtbot_conf():
    if os.path.isfile(CONF_PATH):
        return
    try:
        with open(CONF_PATH, 'w', encoding='utf-8') as f:
            f.write('{}\n')
    except OSError:
        pass


_ensure_lgtbot_conf()

# 让 `import lgtbot_qq` 能找到同目录下的 .so / .pyd
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


# ──────── C++ 扩展加载 ────────────────────────────────────────────────────
LGTBOT_AVAILABLE = False
IMPORT_ERROR = ''
lgtbot_qq = None  # 模块对象，导入成功后赋值

_old_cwd = os.getcwd()
_chdir_ok = os.path.isdir(BUILD_DIR)
if _chdir_ok:
    os.chdir(BUILD_DIR)

if hasattr(sys, 'setdlopenflags') and hasattr(os, 'RTLD_GLOBAL'):
    # 仅 POSIX；Windows 上 sys.setdlopenflags 不存在，对应平台也不需要此操作
    _old_flags = sys.getdlopenflags()
    sys.setdlopenflags(os.RTLD_NOW | os.RTLD_GLOBAL)
    try:
        import lgtbot_qq as _lib  # noqa: F401
        lgtbot_qq = _lib
        LGTBOT_AVAILABLE = True
    except ImportError as e:
        IMPORT_ERROR = str(e)
    finally:
        sys.setdlopenflags(_old_flags)
else:
    try:
        import lgtbot_qq as _lib
        lgtbot_qq = _lib
        LGTBOT_AVAILABLE = True
    except ImportError as e:
        IMPORT_ERROR = str(e)

# 立即恢复主框架的 CWD（避免全局 CWD 漂移导致 ElainaBot 自身路径错乱）
if _chdir_ok:
    os.chdir(_old_cwd)
