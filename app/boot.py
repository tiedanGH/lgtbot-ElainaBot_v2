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
import ctypes
import glob

# ──────── 路径常量 ────────────────────────────────────────────────────────
# __file__ → plugins/lgtbot_qq/app/boot.py  → 插件根目录是其上一级的上一级
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR  = os.path.join(PLUGIN_DIR, 'build')
DATA_DIR   = os.path.join(PLUGIN_DIR, 'data')
ENGINE_DIR = os.path.join(DATA_DIR, 'engine')        # LGTBot 引擎内部文件目录
GAME_PATH  = os.path.join(BUILD_DIR, 'plugins')      # 各 libgame.so 所在目录
DB_PATH    = os.path.join(DATA_DIR, 'lgtbot.db')
IMG_PATH   = os.path.join(DATA_DIR, 'images')
# 引擎自身的配置文件 —— 放在 data/engine/ 子目录避免污染 Web UI 的「插件 → 配置」
# 入口（该入口非递归扫描 data/，子文件夹自动不可见，与 config.yaml 区分清楚）
CONF_PATH  = os.path.join(ENGINE_DIR, 'lgtbot.json')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ENGINE_DIR, exist_ok=True)
os.makedirs(IMG_PATH, exist_ok=True)


# ──────── LGTBot 引擎配置文件预生成 ───────────────────────────────────────
# 启动时若 data/engine/lgtbot.json 不存在则写入空 JSON。引擎自身在 LoadConfig
# 阶段也会兜底创建，这里前置一次确保 Python 一侧可以直接传 CONF_PATH 给 Start。
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


# ──────── 预加载本地共享库 ────────────────────────────────────────────────
# lgtbot_qq.so 链接 libbot_core.so（位于 build/），但 ld.so 默认不搜
# build/，rpath 缺失时会报 "cannot open shared object file"。
# 用 ctypes.CDLL 显式按绝对路径预加载所有 build/lib*.so，配合 RTLD_GLOBAL
# 让符号进全局符号表，后续 lgtbot_qq.so 通过 dlopen 加载时直接命中。
if _chdir_ok:
    _libs = sorted(glob.glob(os.path.join(BUILD_DIR, 'lib*.so')))
    # 两趟：A 依赖 B 时第一趟 A 失败、第二趟 B 已就位则 A 成功
    for _ in range(2):
        for _lib in _libs:
            try:
                ctypes.CDLL(_lib, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


# ──────── 跨插件热重载持久化容器 ──────────────────────────────────────────
# 插件热重载时，PluginManager 会把本插件的 Python 模块从 sys.modules 移除并
# 重新 import；但 C++ 扩展 `lgtbot_qq` 一旦被 dlopen 就常驻进程内，sys.modules
# 也保留缓存。利用这一点，把所有需要跨重载共享的可变容器挂到扩展模块对象上：
#
#   user_cache       - 用户昵称/头像缓存
#   pending_buttons  - 命令触发的待附按钮（一次性消费）
#   active_ref       - 被动消息配额状态（msg_id/event_id + count）
#   ref_waiters      - 配额满时等待的 asyncio.Event 列表
#
# 这样旧 callback（持有旧模块引用）和新 dispatcher（新模块引用）操作的都是
# 同一份字典，热重载后玩家命令仍能正确路由到旧引擎里仍在进行的游戏。
_PERSIST_ATTR = '_elaina_persistent'
_ENGINE_RUNNING_ATTR = '_elaina_engine_running'


def _get_persistent() -> dict:
    """返回挂在 C++ 扩展上的持久化容器，缺失则创建。

    第一次插件加载：扩展模块上没有 _elaina_persistent → 创建新 dict
    后续热重载：直接复用已有的 dict（旧字典里的所有 key/value 仍可访问）
    """
    if lgtbot_qq is None:
        # 扩展未编译：返回一次性的 fallback dict（不会跨重载共享，但避免 None）
        return {
            'user_cache': {},
            'pending_buttons': {},
            'active_ref': {},
            'ref_waiters': {},
        }
    p = getattr(lgtbot_qq, _PERSIST_ATTR, None)
    if p is None:
        p = {
            'user_cache': {},
            'pending_buttons': {},
            'active_ref': {},
            'ref_waiters': {},
        }
        try:
            setattr(lgtbot_qq, _PERSIST_ATTR, p)
        except Exception:
            pass
    return p


def is_engine_running() -> bool:
    """LGTBot C++ 引擎在上次 / 本次 plugin load 中已成功 start 且未释放？"""
    if lgtbot_qq is None:
        return False
    return bool(getattr(lgtbot_qq, _ENGINE_RUNNING_ATTR, False))


def mark_engine_running(running: bool):
    """记录引擎运行状态到扩展模块属性（跨重载持久）"""
    if lgtbot_qq is not None:
        try:
            setattr(lgtbot_qq, _ENGINE_RUNNING_ATTR, bool(running))
        except Exception:
            pass

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
