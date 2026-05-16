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
import asyncio
import os
import time

from core.base.logger import get_logger, PLUGIN
from . import state, quota, helpers, boot, uploader, userdb, buttons, log_attribution
from .webui import message_log

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 「刷新按钮使用说明」教学提示(game_started 触发,紧跟开局公告发出) ────
# 触发流:LGTBot_ElainaBot.cc::ClassifyMatchEvent 识别引擎「游戏开始,您可以使用」
# 广播 → 调 cb_match_event(kind='game_started') → 此处把 key 记入
# _pending_tip_keys。**真正的发送时机被推迟到本帧的 cb_send_text_message /
# cb_send_image_message 把引擎那条开局公告同步落地之后**:
#   1. C++ 调 cb_match_event(只标记,不立刻发) → 立即返回
#   2. C++ 调 cb_send_text_message → 内部 run_coro_blocking 同步等开局公告送达
#   3. 等待返回后我们才 `_schedule_refresh_tip` —— 这就保证 QQ 端先看到「游戏
#      开始」,再看到「刷新按钮使用说明」,不会因为 asyncio FIFO 调度导致教学
#      提示抢在开局公告前面送达。
_pending_tip_keys: set[str] = set()

_REFRESH_TIP_BASE = (
    '## ⚠️ 消息回复限制\n'
    '机器人每条消息**最多回复5次**，且**5分钟**后失效。\n'
    '🔄 看到刷新按钮请**及时点击**，否则将影响机器人发消息和游戏进程。'
)

# 全量申请段 —— 只在群聊里拼到末尾,私信里没有「群号」概念,这段会显得突兀
_REFRESH_TIP_GROUP_TAIL = (
    '\n'
    '\n'
    '> 💡 群主授权群聊消息权限后可规避此限制，@bot 发送：\n'
    '```\n'
    '全量申请 <本群群号>\n'
    '```\n'
    '> 然后按照图片提示进行操作'
)


async def _send_refresh_tip(target_id: str, is_uid: bool) -> None:
    """走标准 `_send_text_quota_managed` 通道发出教学提示(纯文案,无 demo 按钮)。

    第 4 / 5 条配额上的真正刷新按钮由 ``_send_text_quota_managed`` 按 count 自动
    挂载,这条教学消息本身不再追加示例按钮 —— 避免视觉重复 + 让文案干净。

    私信场景(``is_uid=True``) 不拼「全量申请」段 —— 私聊没有「群号」概念,
    那段会显得突兀。群聊才完整呈现。
    """
    msg = _REFRESH_TIP_BASE if is_uid else (_REFRESH_TIP_BASE + _REFRESH_TIP_GROUP_TAIL)
    try:
        message_log.log_outgoing(target_id, is_uid, msg)
        await _send_text_quota_managed(target_id, is_uid, msg, None)
    except Exception as e:
        log.debug(f'消息回复限制说明发送失败 ({target_id}): {e}')


def _schedule_refresh_tip(target_id: str, is_uid: bool) -> None:
    """C++ 工作线程安全地把 `_send_refresh_tip` 投到 asyncio loop,fire-and-forget。

    `asyncio.run_coroutine_threadsafe` 返回的 Future 故意不 await —— C++ 线程
    立即返回继续处理引擎下一帧。
    """
    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.debug('事件循环不可用,跳过刷新按钮使用说明')
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _send_refresh_tip(target_id, is_uid), loop)
    except Exception as e:
        log.debug(f'调度刷新按钮使用说明失败: {e}')


def _consume_pending_tip(key: str, target_id: str, is_uid: bool) -> None:
    """若本 key 之前在 cb_match_event 里被打了 game_started 标记,这里弹掉并发出。

    由两个发送回调(text / image)在 `run_coro_blocking` 同步返回后调用 ——
    那一刻引擎的开局公告已经送达 QQ,我们才能把教学提示安全地排在它后面。

    全量群里 bot 不被 5 条/msg_id 限制,refresh 按钮永远不会出现 —— 这条
    教学的整段文案(在讲怎么点刷新按钮)会变成误导。所以只清掉标记,不发送。
    """
    if key not in _pending_tip_keys:
        return
    _pending_tip_keys.discard(key)
    if (not is_uid) and helpers.is_full_volume_group(target_id):
        log.debug(f'全量群 {target_id} 跳过刷新按钮使用说明')
        return
    _schedule_refresh_tip(target_id, is_uid)


# ──────── 用户信息回调（被 LGTBot 引擎调用，需返回字符串） ─────────────────

def cb_match_event(target_id: str, is_uid: bool, kind: str, game_name: str):
    """C++ → Python：bridge 按消息内容分类后调用,把按钮 / 当前游戏名一次性敲定。

    bridge 端的分类逻辑见 ``LGTBot_ElainaBot.cc::ClassifyMatchEvent``;本侧只
    根据 ``kind`` 走 switch:

      ``announce``       仅刷新 ``state.current_game[key]``(brief 出现但非
                         新建/加入/退出场景,如 /设置 成功后的回执),不动按钮。
      ``new_game``       刷新游戏名;在下一条文本回复挂「加入 / 退出 + 规则」。
      ``join_leave``     刷新游戏名;同上挂「加入 / 退出 + 规则」(玩家加入/
                         退出时也补一个规则按钮,方便随时查阅)。
      ``all_left``       清空当前游戏名;挂「游戏列表 / 创建房间」引导。
      ``terminate``      清空当前游戏名,不挂按钮(/新游戏 前置解散 / 管理员
                         主动结束等场景,紧接着会有真正的新建消息覆盖,或就该
                         安静收尾)。
      ``game_started``   引擎 Match::GameStart 成功后的 BoardcastAtAll —— 不动
                         按钮,只把 key 记入 ``_pending_tip_keys``;真正发出
                         「刷新按钮使用说明」由本帧 cb_send_text/image 同步
                         送完开局公告后立刻触发(_consume_pending_tip)。比
                         Python 侧匹配 /开始 用户输入更可靠:用户瞎敲 /开始
                         不在房间里时引擎不会广播这条,自然不会误教学。
      ``unknown_meta``       未参与游戏 / 不在本群的游戏 —— 挂「元指令帮助」。
      ``unknown_config``     等待房间里输错配置 —— 挂「配置帮助 + 元指令帮助」。
      ``unknown_game``       游戏进行中输错游戏指令 —— 挂「游戏帮助 + 元指令帮助」。
      ``unknown_game_name``  /新游戏 / /规则 等误输游戏名 —— 挂「🎲 游戏列表」。
      ``about``              /关于 命令回执 —— 挂「适配层仓库 + LGT-Bot 仓库」链接按钮。

    所有按钮通过 ``state.pending_buttons[key]`` 暂存,被随后的
    ``cb_send_text_message`` pop 出来一次性附上(bridge 调本回调 → 再调
    send_text_message,同步顺序,GIL 下读写安全)。
    """
    if not target_id:
        return
    key = helpers.target_key(target_id, is_uid)

    # 状态更新
    if kind in ('all_left', 'terminate'):
        state.current_game.pop(key, None)
    elif game_name:
        state.current_game[key] = game_name

    # 按钮挂载 —— new_game / join_leave 都挂同样一组:
    #   · 群聊:  「加入 / 退出」+ 「📖《X》规则」 两行
    #   · 私聊:  仅「📖《X》规则」一行(DM 里 /加入 /退出 无意义,见 is_uid 分支)
    # 私聊场景下 build_game_action_buttons 返回的若是空列表(game_name 未知的
    # 极端情形),`if btns:` 跳过 pending_buttons 写入,避免给框架塞空按钮组。
    if kind in ('new_game', 'join_leave'):
        btns = buttons.build_game_action_buttons(
            state.current_game.get(key),
            include_rule=True,
            include_join_leave=not is_uid,
        )
        if btns:
            state.pending_buttons[key] = btns
    elif kind == 'all_left':
        state.pending_buttons[key] = buttons.build_dissolve_buttons()
    elif kind == 'unknown_meta':
        state.pending_buttons[key] = buttons.build_unknown_meta_buttons()
    elif kind == 'unknown_config':
        state.pending_buttons[key] = buttons.build_unknown_config_buttons()
    elif kind == 'unknown_game':
        state.pending_buttons[key] = buttons.build_unknown_game_buttons()
    elif kind == 'unknown_game_name':
        state.pending_buttons[key] = buttons.build_game_list_buttons()
    elif kind == 'about':
        state.pending_buttons[key] = buttons.build_about_buttons()
    elif kind == 'game_started':
        # 仅标记 —— 本帧 cb_send_text_message / cb_send_image_message 在引擎
        # 开局公告同步落地后调 _consume_pending_tip 真正发出教学提示,保证 QQ
        # 端的顺序是「游戏开始」→「刷新按钮使用说明」,而不是反过来。
        _pending_tip_keys.add(key)
    # 'announce' / 'terminate' 不挂按钮


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
    """C++ → Python：发送文本消息（带配额管理 + 按钮接力续命）

    本条回复要附的按钮（若有）已由 bridge 先调用 cb_match_event 写进
    state.pending_buttons[key]——同一次 HandleMessages 内顺序调用,
    GIL 保护下读写安全。
    """
    key = helpers.target_key(target_id, is_uid)
    extra_buttons = state.pending_buttons.pop(key, None)
    message_log.log_outgoing(target_id, is_uid, msg)

    async def _do():
        await _send_text_quota_managed(target_id, is_uid, msg, extra_buttons)

    helpers.run_coro_blocking(_do())

    # 引擎这条已落地(run_coro_blocking 同步等了它),如果本帧 cb_match_event
    # 之前标了 game_started,这里把「刷新按钮使用说明」紧跟着排出去。
    _consume_pending_tip(key, target_id, is_uid)


async def _send_text_quota_managed(target_id, is_uid, msg, extra_buttons):
    """文本发送核心：配额管理 + 自动追加刷新按钮 + 配额满时等待续命

    全量群分支:配额耗尽时不再阻塞等刷新按钮,直接走主动消息(``kwargs={}``);
    且整个生命周期不追加 ``build_refresh_button``,因为全量群里 bot 不被
    5 条/msg_id 限制,这个教学按钮没有意义。
    """
    key = helpers.target_key(target_id, is_uid)
    msg_preview = (msg or '')[:30].replace('\n', ' ')

    consumed = quota.try_consume_ref(key)
    # 全量群判定:只看运行时观测到的事实(state.full_volume_groups),不再退回
    # 框架 non_at_message.* 配置 —— 配置可能与 QQ 后台权限不同步,误判会让
    # 非全量群也走主动消息(QQ 必拒,把 bot 的配额烧掉)。
    is_full = (not is_uid) and helpers.is_full_volume_group(target_id)

    if consumed is None:
        if is_full:
            # 全量群配额满 → 直接主动消息,不等刷新按钮
            log.info(f'⚡ [全量直推] {key} 配额已满，走主动消息: {msg_preview!r}')
        else:
            # 非全量群:配额满 → 阻塞等待，不预先尝试发送（直接发也会被 QQ 拒）
            import time as _t
            wait_start = _t.monotonic()
            log.info(f'⏳ [配额已满] {key} 已用 {quota.REF_QUOTA}/{quota.REF_QUOTA}，'
                     f'阻塞等待刷新按钮 ≤{quota.REFRESH_WAIT_TIMEOUT:.0f}s | 待发: {msg_preview!r}')
            consumed = await quota.wait_and_consume(key, quota.REFRESH_WAIT_TIMEOUT)
            elapsed = _t.monotonic() - wait_start
            if consumed is None:
                # 等待超时 → 改走主动消息(无 msg_id/event_id)。
                # bot 若在该群/用户上有主动 quota 还能落地,语义更干净。
                # consumed 仍为 None,继续往下走全量群分支共享的主动消息路径。
                log.warning(f'⏰ [超时强发] {key} 经 {elapsed:.1f}s 无刷新，尝试发送主动消息')
            else:
                log.info(f'✅ [配额已刷新] {key} 等 {elapsed:.1f}s 后续命成功，重发文本')

    # 准备 sender / kwargs。consumed 仍为 None 即主动路径(全量直推 / 刷新超时兜底)。
    if consumed is not None:
        ref_type, ref_value, count, ref_appid = consumed
        sender = helpers.get_sender(ref_appid)
        kwargs = {ref_type: ref_value}
    else:
        # 全量群主动路径:无 ref / 无 appid;用任一可用 sender,kwargs 空
        sender = helpers.get_sender('')
        count = 0
        kwargs = {}
    if sender is None:
        log.warning(f'无可用 sender，丢弃文本消息 → {target_id}')
        return

    # 第 4 条起追加刷新按钮;第 5 条（达到上限）用「⚠️ 最终刷新」加强提示。
    # 全量群从不追加(bot 不被 5 条/msg_id 限制,这个教学按钮没意义)。
    btns = list(extra_buttons) if extra_buttons else []
    if not is_full and count >= quota.REFRESH_BUTTON_THRESHOLD:
        is_last = (count >= quota.REF_QUOTA)
        btns.append(quota.build_refresh_button(is_last=is_last))
        tag = '⚠️' if is_last else '🔄'
        log.info(f'📊 [配额追踪] {key} 已用 {count}/{quota.REF_QUOTA} → {tag}')
    btns_arg = btns if btns else None

    try:
        with log_attribution.mark_outbound():
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

    # 多图情况下,game_started 的「游戏开始」字符串只会出现在合并后的 caption
    # (第 1 张图带 content),所以只有 raw_content 非空时才有可能命中标记;
    # 第 2 张以后 raw_content 为空,_consume_pending_tip 看不到 key 也是 no-op。
    if raw_content:
        _consume_pending_tip(
            helpers.target_key(target_id, is_uid), target_id, is_uid)


async def _send_image_quota_managed(target_id, is_uid, data, raw_content, filename):
    """图片发送核心：配额管理 + 优先图床+markdown，失败回退 media

    发送通道二选一：
      A. 图床 markdown：通过 image_hosting 上传图片得到 URL，用 markdown
         `![](url)` 内嵌；保留 `<@openid>` 原生 mention，可挂刷新按钮
      B. 媒体兜底：图床未启用 / 上传失败时走原有 msg_type=7 路径（content
         字段需 humanize mentions，无法挂按钮）

    全量群配额耗尽时不等刷新按钮,直接主动消息(``ref_type=''`` 透传到下游)。
    """
    key = helpers.target_key(target_id, is_uid)
    consumed = quota.try_consume_ref(key)
    # 同 _send_text_quota_managed: 只看运行时观测,不退回框架配置
    is_full = (not is_uid) and helpers.is_full_volume_group(target_id)

    if consumed is None:
        if is_full:
            log.info(f'⚡ [全量直推] {key} 配额已满，图片走主动消息')
        else:
            import time as _t
            wait_start = _t.monotonic()
            log.info(f'⏳ [配额已满] {key} 已用 {quota.REF_QUOTA}/{quota.REF_QUOTA}，'
                     f'阻塞等待刷新按钮 ≤{quota.REFRESH_WAIT_TIMEOUT:.0f}s | 待发: [图片]')
            consumed = await quota.wait_and_consume(key, quota.REFRESH_WAIT_TIMEOUT)
            elapsed = _t.monotonic() - wait_start
            if consumed is None:
                # 等待超时 → 改走主动消息(理由同 _send_text_quota_managed:
                # 过期 msg_id 强发必拒,主动消息至少留一条出路)。
                log.warning(f'⏰ [超时强发] {key} 经 {elapsed:.1f}s 无刷新，尝试发送图片主动消息')
            else:
                log.info(f'✅ [配额已刷新] {key} 等 {elapsed:.1f}s 后续命成功，重发图片')

    # 准备 sender / ref tuple。consumed 仍为 None 即全量群主动路径,
    # 用空 ref_type/ref_value 透传到下游,下游靠 ref_type 为空切换 kwargs={}。
    if consumed is not None:
        ref_type, ref_value, count, ref_appid = consumed
        sender = helpers.get_sender(ref_appid)
    else:
        ref_type, ref_value, count = '', '', 0
        sender = helpers.get_sender('')
    if sender is None:
        log.warning(f'无可用 sender，丢弃图片 → {target_id}')
        return

    # ── 通道 A：尝试图床 → markdown 内嵌 ─────────────────────────────────
    user_id_for_cos = target_id if is_uid else ''
    image_url = await uploader.upload_image(data, filename, user_id=user_id_for_cos)
    if image_url:
        if await _send_markdown_image(sender, target_id, is_uid, ref_type, ref_value,
                                      raw_content, image_url, data, count,
                                      is_full=is_full):
            return
        # markdown 发送失败（极少见，比如域名未报备被 QQ 拒）→ 落回 media

    # ── 通道 B：媒体兜底（msg_type=7）────────────────────────────────────
    await _send_media_fallback(sender, target_id, is_uid, ref_type, ref_value, raw_content, data)


async def _send_markdown_image(sender, target_id, is_uid, ref_type, ref_value,
                               raw_content, image_url, data, count,
                               *, is_full: bool = False) -> bool:
    """构造 markdown 文本 + 图片 + 按钮，调 send_to_*。成功返回 True。

    ``ref_type=''`` 表示主动消息(全量群配额耗尽路径):kwargs 留空,
    不带 msg_id/event_id。``is_full=True`` 时同样跳过刷新按钮追加。
    """
    width, height = uploader.get_image_size(data)
    parts = []
    if raw_content:
        parts.append(raw_content)
    parts.append(f'![image #{width}px #{height}px]({image_url})')
    md = '\n\n'.join(parts)

    # markdown 通道支持挂按钮（不像 msg_type=7);全量群从不挂刷新按钮
    btns: list = []
    if not is_full and count >= quota.REFRESH_BUTTON_THRESHOLD:
        is_last = (count >= quota.REF_QUOTA)
        btns.append(quota.build_refresh_button(is_last=is_last))
    btns_arg = btns if btns else None

    kwargs = {ref_type: ref_value} if ref_type else {}
    try:
        with log_attribution.mark_outbound():
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
    media 不解析 <@openid>，content 这里要先 humanize 成可读 @昵称。

    ``ref_type=''`` 表示主动消息(全量群配额耗尽路径):kwargs 留空。
    """
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
    kwargs = {ref_type: ref_value} if ref_type else {}
    try:
        with log_attribution.mark_outbound():
            if is_uid:
                await sender.send_to_user(target_id, rendered_content, media=media_dict, **kwargs)
            else:
                await sender.send_to_group(target_id, rendered_content, media=media_dict, **kwargs)
    except Exception as e:
        log.warning(f'发送图片失败 ({target_id}): {e}')
