#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C++ 引擎回调实现（由 lgtbot_qq.so 调用，运行在 C++ 工作线程）

入口函数（同步）：
  · cb_get_user_name(uid)            返回昵称
  · cb_get_user_avatar_url(uid)      返回头像 URL
  · cb_send_text_message(...)        发文本（带配额管理）
  · cb_send_image_message(...)       发图（带配额管理 + 图文同条）

异步发送核心：
  · _send_text_quota_managed         配额管理 + 自动追加刷新按钮
  · _send_image_quota_managed        配额管理 + 上传 + media 字段（支持 event_id）
"""

from __future__ import annotations
import os
import time

from core.base.logger import get_logger, PLUGIN
from . import state, quota, helpers, boot
from .webui import message_log

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 用户信息回调（被 LGTBot 引擎调用，需返回字符串） ─────────────────

def cb_get_user_name(uid: str) -> str:
    """C++ → Python：返回用户昵称（找不到时返回 uid）"""
    info = state.user_cache.get(uid)
    return info['name'] if info and info.get('name') else uid


def cb_get_user_avatar_url(uid: str) -> str:
    """C++ → Python：返回头像 URL

    优先从缓存取（消息事件中已用 event.appid 拼好）；
    若缓存未命中（如历史排行榜里的离线用户），用任一活跃 Bot 的 appid 推导。
    C++ 端 DownloadUserAvatar 会用 libcurl 下载，失败则跳过。
    """
    info = state.user_cache.get(uid)
    if info and info.get('avatar'):
        return info['avatar']
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref and _bot_manager_ref._bots:
            appid = next(iter(_bot_manager_ref._bots.keys()))
            url = helpers.QQ_AVATAR_URL.format(appid=appid, openid=uid)
            state.user_cache.setdefault(uid, {})['avatar'] = url
            return url
    except Exception:
        pass
    return ''


# ──────── 文本发送 ────────────────────────────────────────────────────────

def cb_send_text_message(target_id: str, is_uid: bool, msg: str):
    """C++ → Python：发送文本消息（带配额管理 + 按钮接力续命）"""
    extra_buttons = state.pending_buttons.pop(helpers.target_key(target_id, is_uid), None)
    message_log.log_outgoing(target_id, is_uid, msg)

    async def _do():
        await _send_text_quota_managed(target_id, is_uid, msg, extra_buttons)

    helpers.run_coro_blocking(_do())


async def _send_text_quota_managed(target_id, is_uid, msg, extra_buttons):
    """文本发送核心：配额管理 + 自动追加刷新按钮 + 配额满时等待续命"""
    key = helpers.target_key(target_id, is_uid)
    msg_preview = (msg or '')[:30].replace('\n', ' ')

    consumed = quota.try_consume_ref(key)
    if consumed is None:
        # 配额满 → 阻塞等待，不预先尝试发送（直接发也会被 QQ 拒）
        import time as _t
        wait_start = _t.monotonic()
        log.info(f'⏳ [配额已满] {key} 已用 {quota.REF_QUOTA}/{quota.REF_QUOTA}，'
                 f'阻塞等待刷新按钮 ≤{quota.REFRESH_WAIT_TIMEOUT:.0f}s | 待发: {msg_preview!r}')
        consumed = await quota.wait_and_consume(key, quota.REFRESH_WAIT_TIMEOUT)
        elapsed = _t.monotonic() - wait_start
        if consumed is None:
            # 等待超时 → 不丢弃，强制使用现有引用尝试发送（QQ 多半会拒，但试一下）
            consumed = quota.try_consume_ref(key, ignore_quota=True)
            if consumed is None:
                log.warning(f'❌ [无引用] {key} 经 {elapsed:.1f}s 等待后连旧引用都没有了，'
                            f'丢弃此条文本: {msg_preview!r}')
                return
            log.warning(f'⚠️ [超时强发] {key} 经 {elapsed:.1f}s 无刷新，'
                        f'使用现有引用强制尝试发送: {msg_preview!r}')
        else:
            log.info(f'✅ [配额已刷新] {key} 等 {elapsed:.1f}s 后续命成功，重发文本')

    ref_type, ref_value, count, ref_appid = consumed
    sender = helpers.get_sender(ref_appid)
    if sender is None:
        log.warning(f'无可用 sender，丢弃文本消息 → {target_id}')
        return

    # 第 4 条起追加刷新按钮；第 5 条（达到上限）用「⚠️ 最终刷新」加强提示
    btns = list(extra_buttons) if extra_buttons else []
    is_last = (count >= quota.REF_QUOTA)
    if count >= quota.REFRESH_BUTTON_THRESHOLD:
        btns.append(quota.build_refresh_button(is_last=is_last))
        tag = '⚠️ 最终' if is_last else '🔄'
        log.info(f'📊 [配额追踪] {key} 已用 {count}/{quota.REF_QUOTA} → 附 {tag} 刷新按钮')
    btns_arg = btns if btns else None

    kwargs = {ref_type: ref_value}
    try:
        if is_uid:
            await sender.send_to_user(target_id, msg, buttons=btns_arg, **kwargs)
        else:
            await sender.send_to_group(target_id, msg, buttons=btns_arg, **kwargs)
    except Exception as e:
        log.warning(f'发送文本失败 ({target_id}): {e}')


# ──────── 图片发送 ────────────────────────────────────────────────────────

def cb_send_image_message(target_id: str, is_uid: bool, image_path: str, content: str = ''):
    """C++ → Python：发送图片（可附带 content，合并为单条媒体消息）

    LGTBot 通过 popen 异步调用 markdown2image 生成图片，存在小概率回调到达
    时文件还未落盘，这里短暂轮询等待最多 2s。
    """
    if not os.path.isfile(image_path):
        deadline = time.time() + 2.0
        while time.time() < deadline and not os.path.isfile(image_path):
            time.sleep(0.05)
    if not os.path.isfile(image_path):
        mk_bin = os.path.join(boot.BUILD_DIR, 'markdown2image')
        if not os.path.isfile(mk_bin):
            log.warning(f'markdown2image 二进制缺失: {mk_bin} —— 请重新执行 build.sh')
        else:
            log.warning(f'图片渲染失败 (markdown2image 调用未生成文件): {image_path}')
        return

    try:
        with open(image_path, 'rb') as f:
            data = f.read()
    except Exception as e:
        log.warning(f'读取图片失败: {e}')
        return

    rendered_content = helpers.humanize_mentions(content or '')
    message_log.log_outgoing(target_id, is_uid, rendered_content, image=True)

    async def _do():
        await _send_image_quota_managed(target_id, is_uid, data, rendered_content)

    helpers.run_coro_blocking(_do())


async def _send_image_quota_managed(target_id, is_uid, data, content):
    """图片发送核心：配额管理 + 上传 + 用 send_to_* 配合 media 字段

    用 send_to_group/user + media 而非 sender.send_image：前者同时支持
    msg_id 和 event_id 引用，后者只能 msg_id。媒体消息无法挂按钮（QQ 协议），
    所以图片消息不附刷新按钮。
    """
    from core.message.media import upload_media_bytes  # 延迟导入

    key = helpers.target_key(target_id, is_uid)
    consumed = quota.try_consume_ref(key)
    if consumed is None:
        import time as _t
        wait_start = _t.monotonic()
        log.info(f'⏳ [配额已满] {key} 已用 {quota.REF_QUOTA}/{quota.REF_QUOTA}，'
                 f'阻塞等待刷新按钮 ≤{quota.REFRESH_WAIT_TIMEOUT:.0f}s | 待发: [图片]')
        consumed = await quota.wait_and_consume(key, quota.REFRESH_WAIT_TIMEOUT)
        elapsed = _t.monotonic() - wait_start
        if consumed is None:
            consumed = quota.try_consume_ref(key, ignore_quota=True)
            if consumed is None:
                log.warning(f'❌ [无引用] {key} 经 {elapsed:.1f}s 等待后连旧引用都没有了，丢弃此条图片')
                return
            log.warning(f'⚠️ [超时强发] {key} 经 {elapsed:.1f}s 无刷新，'
                        f'强制使用现有引用尝试发送图片')
        else:
            log.info(f'✅ [配额已刷新] {key} 等 {elapsed:.1f}s 后续命成功，重发图片')

    ref_type, ref_value, count, ref_appid = consumed
    sender = helpers.get_sender(ref_appid)
    if sender is None:
        log.warning(f'无可用 sender，丢弃图片 → {target_id}')
        return

    prefix = 'users' if is_uid else 'groups'
    upload_ep = f"/v2/{prefix}/{target_id}/files"
    try:
        file_info = await upload_media_bytes(sender, data, 1, upload_ep)
    except Exception as e:
        log.warning(f'图片上传异常: {e}')
        return
    if not file_info:
        log.warning(f'图片上传失败 → {target_id}')
        return

    media_dict = {'file_info': file_info}
    kwargs = {ref_type: ref_value}
    try:
        if is_uid:
            await sender.send_to_user(target_id, content, media=media_dict, **kwargs)
        else:
            await sender.send_to_group(target_id, content, media=media_dict, **kwargs)
    except Exception as e:
        log.warning(f'发送图片失败 ({target_id}): {e}')
