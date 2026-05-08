#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图床上传调度 + 图片尺寸解析

通过 ElainaBot 的 image_hosting 模块（如已启用），按优先级尝试把本地
图片上传成可内嵌 markdown 的 URL：COS → B站。任一成功即返回 URL；
全部失败或 image_hosting 未启用时返回 None，由上层回退到 msg_type=7。

注意：QQ 官方机器人 markdown 中的 `![alt](url)` 要求 URL 域名已在
QQ Bot 开放平台「消息 URL 配置」报备，否则消息会被丢弃 / 不显示。
COS bucket 的 CDN 域名是最常见的报备目标。
"""

from __future__ import annotations
import struct
from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 图片尺寸解析（不依赖 PIL）─────────────────────────────────────
# 直接读 PNG / JPEG / GIF / WebP 文件头，解析失败时返回 (300, 300) 作为占位

def get_image_size(data: bytes) -> tuple[int, int]:
    try:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return struct.unpack('>II', data[16:24])
        if data[:3] == b'GIF':
            return struct.unpack('<HH', data[6:10])
        if data[:2] == b'\xff\xd8':  # JPEG
            i = 2
            while i < len(data):
                while i < len(data) and data[i] != 0xFF:
                    i += 1
                while i < len(data) and data[i] == 0xFF:
                    i += 1
                marker = data[i]; i += 1
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack('>HH', data[i + 3:i + 7])
                    return (w, h)
                i += struct.unpack('>H', data[i:i + 2])[0]
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            ck = data[12:16]
            if ck == b'VP8 ':
                w, h = struct.unpack('<HH', data[26:30])
                return (w & 0x3fff, h & 0x3fff)
            if ck == b'VP8L':
                b0, b1, b2, b3 = data[21:25]
                return (1 + (((b1 & 0x3f) << 8) | b0),
                        1 + (((b3 & 0x0f) << 10) | (b2 << 2) | ((b1 & 0xc0) >> 6)))
            if ck == b'VP8X':
                return (1 + (data[24] | (data[25] << 8) | (data[26] << 16)),
                        1 + (data[27] | (data[28] << 8) | (data[29] << 16)))
    except Exception as e:
        log.debug(f'图片尺寸解析失败: {e}')
    return (300, 300)


# ──────── 单个图床的上传适配（统一返回 URL 或 None）──────────────────────
# image_hosting 模块返回类型不一致：COS 返回 dict、B站返回 str、失败返
# (False, reason) 元组。这里把语义统一为「成功 → URL 字符串，失败 → None」。

async def _try_cos(hosting, data, filename, user_id):
    try:
        r = await hosting.upload_cos(data, filename, user_id=user_id or None)
    except Exception as e:
        log.warning(f'COS 上传异常: {e}')
        return None
    if isinstance(r, dict) and r.get('file_url'):
        return r['file_url']
    log.warning(f'COS 上传失败: {r}')
    return None


async def _try_bilibili(hosting, data, filename, user_id):
    try:
        r = await hosting.upload_bilibili(data)
    except Exception as e:
        log.warning(f'B站上传异常: {e}')
        return None
    if isinstance(r, str) and r.startswith('http'):
        return r
    log.warning(f'B站上传失败: {r}')
    return None


# 优先级：COS → B站
# 不接 QQ 频道：lgtbot 是群机器人场景，频道 channel_id 一般缺失
_UPLOADERS = (('cos', _try_cos), ('bilibili', _try_bilibili))


# ──────── 对外接口 ───────────────────────────────────────────────────────

def _get_hosting():
    """从 BotManager 取 image_hosting 模块，未启用则返回 None"""
    try:
        from core.bot.manager import _bot_manager_ref
        bm = _bot_manager_ref
        if bm is None or bm.module_manager is None:
            return None
        return bm.module_manager.get('image_hosting')
    except Exception:
        return None


async def upload_image(data: bytes, filename: str, user_id: str = '') -> str | None:
    """按优先级尝试图床上传。任一成功立即返回 URL；全部失败返回 None。"""
    hosting = _get_hosting()
    if hosting is None:
        return None

    try:
        status = hosting.status() if hasattr(hosting, 'status') else {}
    except Exception:
        status = {}

    enabled = [(n, fn) for n, fn in _UPLOADERS if status.get(n)]
    if not enabled:
        return None

    for name, fn in enabled:
        url = await fn(hosting, data, filename, user_id)
        if url:
            log.info(f'图床 {name} 上传成功: {url}')
            return url
    return None
