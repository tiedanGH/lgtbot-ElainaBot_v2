#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LGTBot × ElainaBot 集成插件 (QQ Official Bot)

工作原理：
  1. 编译产物 lgtbot_qq.so / lgtbot_qq.pyd 与本文件同目录，import 即可使用
  2. @on_load   阶段：捕获事件循环 + 启动 LGTBot C++ 引擎，注入 4 个回调
  3. @handler   阶段：将所有群 @ / 私聊消息派发给 LGTBot 引擎（独立线程，避免 C++ 锁阻塞）
  4. C++ 引擎从工作线程回调 Python 时，通过 run_coroutine_threadsafe 桥接到 ElainaBot sender

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
import re
import sys
import time
import threading
import asyncio

from core.plugin.decorators import handler, on_load, on_unload
from core.plugin import context as _ctx_mod
from core.base.logger import get_logger, PLUGIN
from core.message.event import (
    GROUP_AT_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
)

# 关键：必须在模块顶层 import 阶段捕获 PluginContext。
# PluginManager 加载流程：
#   1. _ctx_mod.ctx = plugin_ctx   ← set
#   2. 执行本文件顶层代码（此处可读到 ctx）
#   3. _ctx_mod.ctx = None         ← reset
#   4. 调用 @on_load 函数（此时 ctx 已是 None）
# 所以不能在 @on_load 里 from core.plugin.context import ctx，必须现在抓住引用。
_PLUGIN_CTX = _ctx_mod.ctx

log = get_logger(PLUGIN, 'LGTBot')

# ──────── 路径准备 ────────────────────────────────────────────────────────
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILD_DIR  = os.path.join(_PLUGIN_DIR, 'build')          # CMake 构建目录
_DATA_DIR   = os.path.join(_PLUGIN_DIR, 'data')
# C++ 游戏插件目录：CMake 把每个游戏编译为 build/plugins/<game>/libgame.so
# LGTBot 的 LoadGameModules 扫描该目录下的所有子目录寻找 libgame.so
_GAME_PATH  = os.path.join(_BUILD_DIR, 'plugins')
_DB_PATH    = os.path.join(_DATA_DIR, 'lgtbot.db')
_IMG_PATH   = os.path.join(_DATA_DIR, 'images')
os.makedirs(_DATA_DIR, exist_ok=True)
os.makedirs(_IMG_PATH, exist_ok=True)

# 让 import lgtbot_qq 能找到同目录的 .so / .pyd
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# ──────── C++ 扩展导入 ────────────────────────────────────────────────────
# Linux 关键步骤：必须以 RTLD_GLOBAL 加载 lgtbot_qq.so
#   原因：libbot_core.so 静态依赖 glog/gflags 等符号，游戏 libgame.so 在运行
#         时 dlopen 但未直接链接 glog，需要宿主进程的全局符号表能找到
#         google::LogMessage::stream 等符号。Python 默认 RTLD_LOCAL 会导致
#         "undefined symbol: _ZN6google10LogMessage6streamEv" 错误。
_LGTBOT_AVAILABLE = False
_IMPORT_ERROR = ''
lgtbot_qq = None

# 关键步骤 2：临时把 CWD 切到 build/，让 libbot_core.so 加载时静态初始化的
#   k_markdown2image_path = current_path() / "markdown2image"
# 捕获到正确路径（markdown2image 二进制位于 build/markdown2image）。
# 该常量定义在 lgtbot/bot_core/image.h，是 inline const 静态变量，仅初始化一次。
_old_cwd = os.getcwd()
_chdir_ok = os.path.isdir(_BUILD_DIR)
if _chdir_ok:
    os.chdir(_BUILD_DIR)

if hasattr(sys, 'setdlopenflags') and hasattr(os, 'RTLD_GLOBAL'):
    # 仅 POSIX；Windows 上 sys.setdlopenflags 不存在，对应平台也不需要此操作
    _old_flags = sys.getdlopenflags()
    sys.setdlopenflags(os.RTLD_NOW | os.RTLD_GLOBAL)
    try:
        import lgtbot_qq  # noqa: F401
        _LGTBOT_AVAILABLE = True
    except ImportError as e:
        _IMPORT_ERROR = str(e)
    finally:
        sys.setdlopenflags(_old_flags)
else:
    try:
        import lgtbot_qq  # noqa: F401
        _LGTBOT_AVAILABLE = True
    except ImportError as e:
        _IMPORT_ERROR = str(e)

# 恢复主框架的 CWD（绝不能让全局 CWD 漂移，否则 ElainaBot 自身路径会错乱）
if _chdir_ok:
    os.chdir(_old_cwd)

# ──────── 全局状态 ────────────────────────────────────────────────────────
_event_loop: asyncio.AbstractEventLoop | None = None
_started = False

# 用户信息缓存：uid → {'name': str, 'avatar': str}
_user_cache: dict[str, dict] = {}

# 最近事件上下文：用于主动推送时获取 msg_id / event_id（QQ 官方机器人要求）
# 缓存超过 5 分钟会被忽略（QQ msg_id 时效约 5 分钟）
_MSG_TTL = 300
_last_group_event: dict[str, dict] = {}   # group_id → {'msg_id', 'event_id', 'ts', 'appid'}
_last_user_event:  dict[str, dict] = {}   # user_id  → {'msg_id', 'event_id', 'ts', 'appid'}

# QQ 官方机器人头像直链（未在 SDK 文档中，但实测可用）
#   尺寸可选: 40 / 100 / 140 / 640；LGTBot 渲染头像约 100x100，取 100 即可
_QQ_AVATAR_URL = 'https://q.qlogo.cn/qqapp/{appid}/{openid}/100'

# ──────── 按钮模板 ─────────────────────────────────────────────────────────
# 设计要点：本插件的按钮**不使用 `enter` 字段**。
#   原因：当 bot.yaml 配置 `message.button_enter_to_send: true` 时，框架
#         keyboard.py 会把 `type=2 + enter=True` 强制转成 `type=1`（纯
#         callback 按钮）。type=1 在 QQ 协议层永远不会回填输入框，仅触发
#         INTERACTION_CREATE → bot ACK → 客户端弹"操作成功"，与"点按钮 →
#         文字进输入框"的本意冲突。
#   去掉 enter 后保持 type=2 不被转换，行为：点击 → 文字回填到输入框 →
#         用户手动点发送。如果用户希望"自动发送"，可在 bot.yaml 把
#         button_enter_to_send 设为 false，并在按钮里加回 enter=True。
_GAME_ACTION_BUTTONS = [[
    {'text': '🟢 加入', 'data': '/加入', 'type': 2, 'style': 1},
    {'text': '🔴 退出', 'data': '/退出', 'type': 2, 'style': 3},
]]

_MENU_BUTTONS = [
    [
        {'text': '📖 查看帮助',  'data': '/帮助',     'type': 2, 'style': 4},
        {'text': '🎲 游戏列表',  'data': '/游戏列表', 'type': 2, 'style': 1},
    ],
    [
        {'text': '🏆 排行大图',  'data': '/排行大图', 'type': 2, 'style': 1},
        {'text': '📊 我的战绩',  'data': '/战绩',     'type': 2, 'style': 1},
    ],
    # 第 3 行：链接按钮（type=0）—— 不受 button_enter_to_send 影响
    [
        {'text': '仓库',
         'link': 'https://github.com/tiedanGH/lgtbot-ElainaBot_v2'},
        {'text': '网站',
         'link': 'https://tiedan.site'},
        {'text': '官群',
         'link': ('https://qun.qq.com/universal-share/share?ac=1'
                  '&authKey=GLoA6W7KujPW%2B%2B%2FeirVZVVEn61q%2FAmLFyd9mkJ8u%2Bv0E%2B2IooquHavHi9iaJSxKK'
                  '&busi_data=eyJncm91cENvZGUiOiIxMDU5ODM0MDI0IiwidG9rZW4iOiJsTUFlUHZsdVJpSUhTc2dLSTBoeDI2M0IxS09kTGg3NzFsd1dvaVVLajVqTTIvRm9zaGlMTHBrekRIOGdVZHlaIiwidWluIjoiMjI5NTgyNDkyNyJ9'
                  '&data=IMqVKIvDehyMv2ooaqlgzql0-Q9XENN4pK6qGR1mqYoZH5AFDBMmrflWNEFN-EOLeKuJTxLABAwgaaUnUp-iyw'
                  '&svctype=4&tempid=h5_group_info')},
    ],
]

# 触发"加入/退出"按钮的命令模式：/新游戏、/加入、/退出、/随机游戏
_GAME_ACTION_RE = re.compile(r'^\s*/(新游戏|加入|随机游戏)(\s|$)')

# 命令触发后，LGTBot 的 [第一条] 文本回复将附带按钮（一次性）
# key 形如 'g:<group_id>' 或 'u:<user_id>'
_pending_buttons: dict[str, list] = {}

# 媒体消息（msg_type=7）的 content 字段是 QQ 协议层面不解析 <@openid> 的纯文本，
# 图文同条场景下把 mention 退化为可读的 "@昵称"（损失：无 ping 通知）
_MENTION_RE = re.compile(r'<@([^>\s]+)>')


def _humanize_mentions(text: str) -> str:
    """把 <@openid> 转成 @昵称（用于图文消息 content）

    QQ 协议中 msg_type=7 的 content 不解析 <@openid> 提及语法，
    会原样显示为字面字符串。本函数从 _user_cache 取对应昵称替换，
    保持图文单条消息的同时让文字可读。
    """
    if not text or '<@' not in text:
        return text

    def _repl(m):
        uid = m.group(1)
        info = _user_cache.get(uid, {})
        name = info.get('name', '')
        if name:
            return f'@{name}'
        # 缓存未命中：截短 uid 占位（避免泄露完整 openid 的同时仍可识别）
        return f'@{uid[:6]}…' if len(uid) > 6 else f'@{uid}'

    return _MENTION_RE.sub(_repl, text)


def _target_key(target_id: str, is_uid: bool) -> str:
    return ('u:' if is_uid else 'g:') + target_id


# ──────── Sender / 协程辅助 ──────────────────────────────────────────────

def _get_sender(appid: str = ''):
    """从 BotManager 全局引用获取 MessageSender。

    appid 为空时返回第一个可用 sender（单 Bot 场景下足够）。
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


def _run_coro_blocking(coro, timeout: float = 15.0):
    """在 C++ 工作线程中安全执行协程（阻塞等待结果）"""
    if _event_loop is None or _event_loop.is_closed():
        log.warning('事件循环不可用，丢弃协程')
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, _event_loop)
        return fut.result(timeout=timeout)
    except Exception as e:
        log.warning(f'协程执行异常: {e}')
        return None


def _pop_event_ctx(target_id: str, is_uid: bool):
    """取出最近事件上下文（用于 send_to_group/send_to_user 的 msg_id/event_id）"""
    table = _last_user_event if is_uid else _last_group_event
    ctx = table.get(target_id)
    if not ctx:
        return None, None, ''
    if time.time() - ctx['ts'] > _MSG_TTL:
        # 过期上下文 —— 删除避免污染
        table.pop(target_id, None)
        return None, None, ctx.get('appid', '')
    return ctx.get('msg_id'), ctx.get('event_id'), ctx.get('appid', '')


# ──────── C++ 回调实现（由 lgtbot_qq.so 调用，通常在工作线程）────────────

def cb_get_user_name(uid: str) -> str:
    """C++ → Python：返回用户昵称（找不到时返回 uid）"""
    info = _user_cache.get(uid)
    return info['name'] if info and info.get('name') else uid


def cb_get_user_avatar_url(uid: str) -> str:
    """C++ → Python：返回头像 URL

    优先从缓存取（消息事件中已用 event.appid 拼好）；
    若缓存未命中（如历史排行榜里的离线用户），用任一活跃 Bot 的 appid 推导。
    C++ 端 DownloadUserAvatar 会用 libcurl 下载，失败则跳过。
    """
    info = _user_cache.get(uid)
    if info and info.get('avatar'):
        return info['avatar']

    # 缓存未命中 → 用任一可用 Bot 的 appid 推导
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref and _bot_manager_ref._bots:
            appid = next(iter(_bot_manager_ref._bots.keys()))
            url = _QQ_AVATAR_URL.format(appid=appid, openid=uid)
            # 顺带写回缓存避免重复推导
            _user_cache.setdefault(uid, {})['avatar'] = url
            return url
    except Exception:
        pass
    return ''


def cb_send_text_message(target_id: str, is_uid: bool, msg: str):
    """C++ → Python：发送文本消息

    根据最近事件上下文取 msg_id / event_id；若无上下文则尝试主动推送。
    若该 target 在 _pending_buttons 中有待附按钮（用户刚执行了 /新游戏 / /加入 / /退出），
    则把按钮附在本条文本消息上（一次性，发完即清）。
    """
    msg_id, event_id, appid = _pop_event_ctx(target_id, is_uid)
    sender = _get_sender(appid)
    if sender is None:
        log.warning(f'无可用 sender，丢弃文本消息 → {target_id}')
        return

    # 取出本目标的待附按钮（一次性消费）
    buttons = _pending_buttons.pop(_target_key(target_id, is_uid), None)

    async def _do():
        try:
            if is_uid:
                await sender.send_to_user(target_id, msg, msg_id=msg_id,
                                          event_id=event_id, buttons=buttons)
            else:
                await sender.send_to_group(target_id, msg, msg_id=msg_id,
                                           event_id=event_id, buttons=buttons)
        except Exception as e:
            log.warning(f'发送文本失败 ({target_id}): {e}')

    _run_coro_blocking(_do())


def cb_send_image_message(target_id: str, is_uid: bool, image_path: str, content: str = ''):
    """C++ → Python：发送图片（可附带 content，合并为单条媒体消息）

    LGTBot 通过 popen 异步调用 markdown2image 生成图片，存在小概率
    回调到达时文件还未落盘，这里短暂轮询等待最多 2s。

    QQ 官方机器人 msg_type=7 (MEDIA) 消息可携带 content 文字字段，
    用于把 "@xxx 文本 + 图片" 合并为同一条消息（避免变成两条）。
    """
    if not os.path.isfile(image_path):
        deadline = time.time() + 2.0
        while time.time() < deadline and not os.path.isfile(image_path):
            time.sleep(0.05)
    if not os.path.isfile(image_path):
        mk_bin = os.path.join(_BUILD_DIR, 'markdown2image')
        if not os.path.isfile(mk_bin):
            log.warning(f'markdown2image 二进制缺失: {mk_bin} —— 请重新执行 build.sh')
        else:
            log.warning(f'图片渲染失败 (markdown2image 调用未生成文件): {image_path}')
        return

    msg_id, event_id, appid = _pop_event_ctx(target_id, is_uid)
    sender = _get_sender(appid)
    if sender is None:
        log.warning(f'无可用 sender，丢弃图片 → {target_id}')
        return

    try:
        with open(image_path, 'rb') as f:
            data = f.read()
    except Exception as e:
        log.warning(f'读取图片失败: {e}')
        return

    # QQ 媒体消息 content 不解析 <@openid> → 转 @昵称 保持可读
    rendered_content = _humanize_mentions(content or '')

    async def _do():
        try:
            target_type = 'user' if is_uid else 'group'
            await sender.send_image(target_type, target_id, data,
                                    content=rendered_content, msg_id=msg_id)
        except Exception as e:
            log.warning(f'发送图片失败 ({target_id}): {e}')

    _run_coro_blocking(_do())


# ──────── 插件配置 ────────────────────────────────────────────────────────

# 默认配置（首次启动会写入 data/config.yaml）
_DEFAULT_CONFIG = {
    'admin_uids': [],
}

# 配置项注释（写入 YAML 文件头部，方便用户编辑）
_CONFIG_COMMENTS = {
    'admin_uids': (
        'LGTBot 内部管理员 openid 列表（不同于 ElainaBot 的 owner_ids）\n'
        '#   这些用户可执行 LGTBot 管理命令（如 /管理 重置赛季 等）\n'
        '#   留空则该机器人无 LGTBot 管理员；可在 Web 面板「日志」查 user_id'
    ),
}


def _get_ctx():
    """获取插件 PluginContext。

    优先用模块顶层 import 阶段捕获的 _PLUGIN_CTX（最可靠）；
    若不可用，尝试从全局 BotManager 的 plugin_manager 反查；
    最后兜底手动构造一个（保证配置文件总能落地）。
    """
    if _PLUGIN_CTX is not None:
        return _PLUGIN_CTX

    # 反查：从 BotManager → PluginManager 取
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is not None:
            pm = getattr(_bot_manager_ref, 'plugin_manager', None)
            if pm is not None:
                # 单文件插件名为文件去掉 .py，多文件插件名为目录名
                # 本插件是多文件形式，目录名 = lgtbot_qq
                info = (pm.get_plugin('lgtbot_qq')
                        if hasattr(pm, 'get_plugin') else None)
                if info and getattr(info, 'ctx', None):
                    return info.ctx
    except Exception:
        pass

    # 兜底：直接构造（PluginContext 只是 data/ 目录的 YAML 读写器，无副作用）
    try:
        from core.plugin.context import PluginContext
        return PluginContext('lgtbot_qq', _PLUGIN_DIR)
    except Exception as e:
        log.warning(f'构造 PluginContext 失败: {e}')
        return None


def _load_plugin_config() -> str:
    """读取 data/config.yaml，返回 LGTBot 引擎需要的逗号分隔 admin 字符串

    - 不存在则创建带注释的默认模板（此时 Web UI 才能看到）
    - 存在但缺字段则自动补齐
    - admin_uids 字段非法时降级为空（不阻断启动）
    """
    ctx = _get_ctx()

    # ── 标准加载（ensure_config 保证字段齐全 + 写入注释）──
    try:
        if ctx is not None:
            cfg = ctx.ensure_config(_DEFAULT_CONFIG,
                                     filename='config.yaml',
                                     comments=_CONFIG_COMMENTS)
        else:
            log.warning('PluginContext 完全不可用，使用默认配置（Web UI 将看不到配置文件）')
            cfg = dict(_DEFAULT_CONFIG)
    except Exception as e:
        log.warning(f'加载配置异常，使用默认值: {e}')
        cfg = dict(_DEFAULT_CONFIG)

    uids = cfg.get('admin_uids', [])
    if not isinstance(uids, list):
        log.warning('config.yaml 中 admin_uids 应为列表，已忽略')
        uids = []
    admins_str = ','.join(str(u).strip() for u in uids if str(u).strip())
    if admins_str:
        log.info(f'LGTBot 管理员配置：{len(uids)} 人')
    return admins_str


# ──────── 插件生命周期 ────────────────────────────────────────────────────

@on_load
async def _setup():
    global _event_loop, _started

    # ── 第一步：无条件确保配置文件存在 ─────────────────────────────────────
    # 即使后续 LGTBot 不可用 / 未编译，也要让 data/config.yaml 被创建出来，
    # 这样 Web UI 「插件配置」入口才能看到并允许用户编辑（解决"暂无配置文件"）
    admins = _load_plugin_config()

    if not _LGTBOT_AVAILABLE:
        log.error('=' * 60)
        log.error(f'lgtbot_qq C++ 扩展未编译或导入失败：{_IMPORT_ERROR}')
        log.error('请先按 plugins/lgtbot_qq/DEPLOY.md 编译后再启动')
        log.error('=' * 60)
        return

    # 捕获主事件循环 —— C++ 工作线程将通过 run_coroutine_threadsafe 调度到此循环
    _event_loop = asyncio.get_running_loop()

    # 检查游戏目录是否存在已编译的游戏 .so
    if not os.path.isdir(_GAME_PATH):
        log.error('=' * 60)
        log.error(f'游戏插件目录不存在: {_GAME_PATH}')
        log.error('请先在 plugins/lgtbot_qq/ 下执行 bash build.sh 完成编译')
        log.error('=' * 60)
        return
    game_count = sum(
        1 for d in os.listdir(_GAME_PATH)
        if os.path.isfile(os.path.join(_GAME_PATH, d, 'libgame.so'))
    )
    if game_count == 0:
        log.error('=' * 60)
        log.error(f'未在 {_GAME_PATH} 下发现任何 libgame.so')
        log.error('请检查 build.sh 是否带 --no-games 关闭了游戏编译')
        log.error('=' * 60)
        return

    log.info(f'初始化 LGTBot 引擎: 游戏数={game_count}, db={_DB_PATH}')
    ok = lgtbot_qq.start(
        _GAME_PATH, _DB_PATH, '', _IMG_PATH, admins,
        cb_get_user_name, cb_get_user_avatar_url,
        cb_send_text_message, cb_send_image_message,
    )
    if not ok:
        log.error('LGTBot 引擎启动失败 (查看上方 stderr 输出)')
        return

    _started = True
    log.info('✅ LGTBot 引擎已就绪')


@on_unload
async def _teardown():
    global _started
    if not _started or not _LGTBOT_AVAILABLE:
        return
    if lgtbot_qq.release_bot_if_not_processing_games():
        _started = False
        log.info('LGTBot 引擎已安全关闭')
    else:
        log.warning('存在进行中的游戏 —— 引擎未释放，强制退出可能丢失对局状态')


# ──────── 消息派发 ────────────────────────────────────────────────────────

# 监听所有消息事件，优先级低（让其他插件可拦截系统命令）
_LGT_EVENTS = frozenset({
    GROUP_AT_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
})


_MENU_TEXT = (
    '## 🎮 LGT-Bot 机器人\n'
    '\n'
    '---\n'
    '\n'
    '集成超过 50 种游戏！\n'
    '▸ 群内 @我 + `/新游戏 <名称>` 创建房间\n'
    '▸ 私聊我体验单机游戏\n'
    '▸ 进入房间后输入 `帮助` 查看玩法\n\n'
    '👇 点下方按钮快速开始'
)


@handler(r'.*', priority=-100, event_types=_LGT_EVENTS)
async def lgtbot_dispatch(event, match):
    """将消息派发给 LGTBot 引擎（不消费事件，其他插件仍可处理）"""
    if not _started:
        return

    content = (event.content or '').strip()
    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''

    # 更新用户缓存（QQ 事件携带 username + 可推导头像 URL）
    if uid:
        appid = event.appid or ''
        avatar = _QQ_AVATAR_URL.format(appid=appid, openid=uid) if appid else ''
        old = _user_cache.get(uid, {})
        _user_cache[uid] = {
            'name': getattr(event, 'username', '') or old.get('name', ''),
            'avatar': avatar or old.get('avatar', ''),
        }

    # 缓存事件上下文 —— 供后续 C++ 主动发送时取 msg_id / event_id
    ctx = {
        'msg_id': event.message_id,
        'event_id': event.event_id,
        'ts': time.time(),
        'appid': event.appid or '',
    }
    if event.is_group and gid:
        _last_group_event[gid] = ctx
    if uid:
        _last_user_event[uid] = ctx

    # 空消息（仅 @bot）→ 回欢迎菜单，不进 LGTBot 引擎
    if not content:
        try:
            await event.reply(_MENU_TEXT, buttons=_MENU_BUTTONS)
        except Exception as e:
            log.warning(f'菜单回复失败: {e}')
        return

    # 命令检测：执行 /新游戏 /加入 /退出 时，给 LGTBot 的下一条文本回复附"加入/退出"按钮
    if _GAME_ACTION_RE.match(content):
        target = gid if (event.is_group and gid) else uid
        if target:
            _pending_buttons[_target_key(target, not (event.is_group and gid))] = _GAME_ACTION_BUTTONS

    # 派发给 C++ 引擎（独立线程，避免 C++ match-lock 与 asyncio loop 互锁）
    try:
        if event.is_group and gid:
            threading.Thread(
                target=lgtbot_qq.on_public_message,
                args=(content, uid, gid),
                daemon=True,
            ).start()
        elif event.is_direct and uid:
            threading.Thread(
                target=lgtbot_qq.on_private_message,
                args=(content, uid),
                daemon=True,
            ).start()
    except Exception as e:
        log.warning(f'派发消息失败: {e}')


# INTERACTION_CREATE 处理：本插件按钮全部使用 type=2（无 enter），不会触发
# callback 事件，无需 ACK handler。如果未来某个按钮真要走 type=1 callback 流程
# （例如需要按钮根据上下文动态执行不同操作），再单独添加 INTERACTION_CREATE
# handler 处理 —— 避免在这里全局兜底 ACK 干扰其他插件的交互逻辑。
