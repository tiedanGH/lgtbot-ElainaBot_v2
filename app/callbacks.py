#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C++ 引擎回调实现（由 LGTBot_ElainaBot.so 调用，运行在 C++ 工作线程）

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
from . import state, quota, helpers, boot, uploader, userdb, buttons
from .webui import message_log

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 用户信息回调（被 LGTBot 引擎调用，需返回字符串） ─────────────────

def cb_match_announce(target_id: str, is_uid: bool, game_name: str):
    """C++ → Python：bridge 在 BriefInfo 里解析到「游戏名称：X」时调用本回调。

    LGTBot 的 BriefInfo 在新建房间 / 玩家加入 / 玩家退出 / 改设置 后都会广播,
    bridge 层每次都会拉到游戏名透传过来,本侧把它写入 state.current_game,
    供 cb_send_text_message 现场构造「📜 规则」按钮用。
    """
    if not target_id or not game_name:
        return
    state.current_game[helpers.target_key(target_id, is_uid)] = game_name


def cb_get_user_name(uid: str) -> str:
    """C++ → Python：返回用户昵称（DB 未命中时返回 uid 兜底）"""
    return userdb.get_name(uid) or uid


def cb_get_user_avatar_url(uid: str) -> str:
    """C++ → Python：返回头像 URL

    优先从 userdb 取（写入时 appid 正确，多 bot 部署下用 DB 缓存的 URL
    比临时 fallback 更准确）；DB 未命中（如历史排行榜里的离线用户）则
    用任一活跃 Bot 的 appid 临时推导，不写回 DB —— 等用户下次发消息时
    dispatcher 会用真正的 event.appid 落盘。
    C++ 端 DownloadUserAvatar 会用 libcurl 下载，失败则跳过。
    """
    cached = userdb.get_avatar(uid)
    if cached:
        return cached
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref and _bot_manager_ref._bots:
            appid = next(iter(_bot_manager_ref._bots.keys()))
            return helpers.QQ_AVATAR_URL.format(appid=appid, openid=uid)
    except Exception:
        pass
    return ''


# ──────── 文本发送 ────────────────────────────────────────────────────────

def cb_send_text_message(target_id: str, is_uid: bool, msg: str):
    """C++ → Python：发送文本消息（带配额管理 + 按钮接力续命）"""
    key = helpers.target_key(target_id, is_uid)
    extra_buttons = state.pending_buttons.pop(key, None)
    # PENDING_GAME_ACTION sentinel:dispatcher 留下的「按发送时游戏名构造」标记。
    # 此处刚好是 cb_match_announce 之后(同一次 HandleMessages 内 bridge 先调
    # match_announce 再调 send_text_message),current_game[key] 已是最新值。
    if extra_buttons == buttons.PENDING_GAME_ACTION:
        extra_buttons = buttons.build_game_action_buttons(state.current_game.get(key))
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
                log.warning(f'❌ [无引用] {key} 经 {elapsed:.1f}s 等待后丢失旧引用，'
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
        tag = '⚠️' if is_last else '🔄'
        log.info(f'📊 [配额追踪] {key} 已用 {count}/{quota.REF_QUOTA} → {tag}')
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

    raw_content = content or ''
    # 日志展示用 humanize 版（更可读），实际发送时再按通道决定是否 humanize
    message_log.log_outgoing(
        target_id, is_uid,
        helpers.humanize_mentions(raw_content), image=True,
    )

    filename = os.path.basename(image_path) or 'lgtbot.png'

    async def _do():
        await _send_image_quota_managed(target_id, is_uid, data, raw_content, filename)

    helpers.run_coro_blocking(_do())


async def _send_image_quota_managed(target_id, is_uid, data, raw_content, filename):
    """图片发送核心：配额管理 + 优先图床+markdown，失败回退 media

    发送通道二选一：
      A. 图床 markdown：通过 image_hosting 上传图片得到 URL，用 markdown
         `![](url)` 内嵌；保留 `<@openid>` 原生 mention，可挂刷新按钮
      B. 媒体兜底：图床未启用 / 上传失败时走原有 msg_type=7 路径（content
         字段需 humanize mentions，无法挂按钮）
    """
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
                log.warning(f'❌ [无引用] {key} 经 {elapsed:.1f}s 等待后丢失旧引用，丢弃此条图片')
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

    # ── 通道 A：尝试图床 → markdown 内嵌 ─────────────────────────────────
    user_id_for_cos = target_id if is_uid else ''
    image_url = await uploader.upload_image(data, filename, user_id=user_id_for_cos)
    if image_url:
        if await _send_markdown_image(sender, target_id, is_uid, ref_type, ref_value,
                                      raw_content, image_url, data, count):
            return
        # markdown 发送失败（极少见，比如域名未报备被 QQ 拒）→ 落回 media

    # ── 通道 B：媒体兜底（msg_type=7）────────────────────────────────────
    await _send_media_fallback(sender, target_id, is_uid, ref_type, ref_value, raw_content, data)


async def _send_markdown_image(sender, target_id, is_uid, ref_type, ref_value,
                               raw_content, image_url, data, count) -> bool:
    """构造 markdown 文本 + 图片 + 按钮，调 send_to_*。成功返回 True"""
    width, height = uploader.get_image_size(data)
    parts = []
    if raw_content:
        parts.append(raw_content)
    parts.append(f'![image #{width}px #{height}px]({image_url})')
    md = '\n\n'.join(parts)

    # markdown 通道支持挂按钮（不像 msg_type=7）
    btns: list = []
    is_last = (count >= quota.REF_QUOTA)
    if count >= quota.REFRESH_BUTTON_THRESHOLD:
        btns.append(quota.build_refresh_button(is_last=is_last))
    btns_arg = btns if btns else None

    kwargs = {ref_type: ref_value}
    try:
        if is_uid:
            await sender.send_to_user(target_id, md, buttons=btns_arg, **kwargs)
        else:
            await sender.send_to_group(target_id, md, buttons=btns_arg, **kwargs)
        return True
    except Exception as e:
        log.warning(f'markdown 图片发送失败 ({target_id}): {e}, 回退到媒体消息')
        return False


async def _send_media_fallback(sender, target_id, is_uid, ref_type, ref_value,
                               raw_content, data):
    """msg_type=7 媒体消息兜底：上传 file_info → send_to_* with media。
    media 不解析 <@openid>，content 这里要先 humanize 成可读 @昵称。"""
    from core.message.media import upload_media_bytes  # 延迟导入

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

    rendered_content = helpers.humanize_mentions(raw_content)
    media_dict = {'file_info': file_info}
    kwargs = {ref_type: ref_value}
    try:
        if is_uid:
            await sender.send_to_user(target_id, rendered_content, media=media_dict, **kwargs)
        else:
            await sender.send_to_group(target_id, rendered_content, media=media_dict, **kwargs)
    except Exception as e:
        log.warning(f'发送图片失败 ({target_id}): {e}')
