#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""插件配置（data/config.yaml）—— 通过 ElainaBot 标准配置体系存取。

字段：
  · admin_uids: list[str]            LGTBot 内部管理员 openid 列表
  · refresh_wait_timeout: float      被动消息配额耗尽后等待刷新按钮的秒数
  · image_hosting: str               markdown 图片内嵌使用的单个图床名（留空 = 禁用）
"""

from __future__ import annotations

from core.base.logger import get_logger, PLUGIN
from . import state, boot

log = get_logger(PLUGIN, 'LGTBot')

DEFAULT_CONFIG = {
    'admin_uids': [],
    'refresh_wait_timeout': 15.0,
    'image_hosting': '',
}
CONFIG_COMMENTS = {
    'admin_uids': (
        'LGTBot 内部管理员 openid 列表（不同于 ElainaBot 的 owner_ids）\n'
        '#   这些用户可执行 LGTBot 管理命令（如 %帮助 等）\n'
        '#   留空则该机器人无 LGTBot 管理员；可在 Web 面板「日志」查 user_id'
    ),
    'refresh_wait_timeout': (
        '被动消息配额（5 条）耗尽时，等待用户点击「刷新」按钮的最长秒数\n'
        '#   超时后会用旧引用强制尝试发送（多半会被拒绝）\n'
        '#   推荐 5–30 秒：过短玩家来不及点，过长命令响应延迟明显'
    ),
    'image_hosting': (
        '游戏图片走 markdown 内嵌时使用的图床（依赖主框架 image_hosting 模块）\n'
        '#   留空 = 不启用图床，所有图片直接以 msg_type=7 媒体消息发送\n'
        '#   可选值：cos / nature / bilibili / chatglm / ukaka / xingye\n'
        '#   只尝试指定的这一个图床，上传失败立即回退 msg_type=7\n'
        '#   （遍历所有启用图床耗时过长，故仅支持单选）\n'
        '#   注意：图床域名需先在 QQ 开放平台「消息 URL 配置」报备'
    ),
}


def _get_ctx():
    """三层降级取 PluginContext，保证 config.yaml 总能落地

      ① main.py 在 import 阶段捕获到的 state.plugin_ctx（最可靠）
      ② 通过 BotManager → PluginManager 反查（应对热重载等情形）
      ③ 直接用 plugin 目录构造一个 PluginContext（兜底）
    """
    if state.plugin_ctx is not None:
        return state.plugin_ctx

    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is not None:
            pm = getattr(_bot_manager_ref, 'plugin_manager', None)
            if pm is not None:
                info = pm.get_plugin('LGTBot_ElainaBot') if hasattr(pm, 'get_plugin') else None
                if info and getattr(info, 'ctx', None):
                    return info.ctx
    except Exception:
        pass

    try:
        from core.plugin.context import PluginContext
        return PluginContext('LGTBot_ElainaBot', boot.PLUGIN_DIR)
    except Exception as e:
        log.warning(f'构造 PluginContext 失败: {e}')
        return None


def load_plugin_config() -> str:
    """加载 / 创建 data/config.yaml，返回 LGTBot 引擎需要的逗号分隔 admin 字符串

    - 不存在则创建带注释的默认模板（此时 Web UI 才能看到该配置文件）
    - 存在但缺字段则自动补齐
    - admin_uids 字段非法时降级为空（不阻断启动）
    """
    ctx = _get_ctx()
    try:
        if ctx is not None:
            cfg = ctx.ensure_config(DEFAULT_CONFIG, filename='config.yaml',
                                     comments=CONFIG_COMMENTS)
        else:
            log.warning('PluginContext 完全不可用，使用默认配置（Web UI 将看不到配置文件）')
            cfg = dict(DEFAULT_CONFIG)
    except Exception as e:
        log.warning(f'加载配置异常，使用默认值: {e}')
        cfg = dict(DEFAULT_CONFIG)

    uids = cfg.get('admin_uids', [])
    if not isinstance(uids, list):
        log.warning('config.yaml 中 admin_uids 应为列表，已忽略')
        uids = []
    admins_str = ','.join(str(u).strip() for u in uids if str(u).strip())
    if admins_str:
        log.info(f'LGTBot 管理员配置：{len(uids)} 人')

    # 把运行时可调字段套用到 quota 模块（每次 @on_load 都重新读取，
    # 改完 config.yaml 在 Web UI reload 插件即生效，无需重启进程）
    _apply_runtime_tunables(cfg)

    return admins_str


def _apply_runtime_tunables(cfg: dict):
    """把 config.yaml 中的可调字段下发到对应运行时模块"""
    from . import quota, uploader

    timeout = cfg.get('refresh_wait_timeout', 15.0)
    try:
        timeout_f = float(timeout)
    except (TypeError, ValueError):
        log.warning(f'refresh_wait_timeout 应为数值，已忽略 (got {timeout!r})')
    else:
        if timeout_f <= 0:
            log.warning(f'refresh_wait_timeout 应为正数，已忽略 (got {timeout_f})')
        elif quota.REFRESH_WAIT_TIMEOUT != timeout_f:
            log.info(f'refresh_wait_timeout: {quota.REFRESH_WAIT_TIMEOUT}s → {timeout_f}s')
            quota.REFRESH_WAIT_TIMEOUT = timeout_f

    backend = cfg.get('image_hosting', '')
    if not isinstance(backend, str):
        log.warning(f'image_hosting 应为字符串，已忽略 (got {backend!r})')
        backend = ''
    backend = backend.strip().lower()
    valid = {name for name, _ in uploader._UPLOADERS}
    if backend and backend not in valid:
        log.warning(f'image_hosting 未知图床 {backend!r}，可选值：{sorted(valid)}；已禁用')
        backend = ''
    if uploader.SELECTED_BACKEND != backend:
        old = uploader.SELECTED_BACKEND or '(未启用)'
        new = backend or '(未启用)'
        log.info(f'image_hosting: {old} → {new}')
        uploader.SELECTED_BACKEND = backend
