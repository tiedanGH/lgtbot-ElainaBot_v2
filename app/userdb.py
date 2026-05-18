#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用户昵称 / 头像持久化（SQLite）

为什么需要：QQ 官方机器人 API 不提供「按 openid 查昵称」的接口，但每次入站
事件 payload 自带 username。把它落盘后，离线用户在排行榜 / 战绩 / 历史对局
回放等场景里才能正确显示昵称（否则只能用 raw openid，C++ 端会包成
``<openid_first4…last4>``）。

设计要点：
  · 一条 SQLite 长连接，``check_same_thread=False`` + ``isolation_level=None``
    （autocommit）—— C++ 工作线程同步 SELECT 与 asyncio flusher 写入分别
    在不同线程，WAL 模式下读写不互锁
  · 写路径走 ``mark_dirty(uid, name=, avatar=)`` → pending dict → 后台 asyncio
    任务每 5 分钟批量 UPSERT；dispatcher 不直接 INSERT，避免每条消息一次
    磁盘往返
  · ``last_seen`` 在 ``mark_dirty`` 调用时刻记下并存入 pending；``flush_now``
    只搬运不重写时间戳。这样 WebUI 看到的「上次活跃」就是用户真实发消息
    的那一刻,而不是「上次 flush 周期跑完的整点」（旧实现的偏差最大 5 分钟）。
  · UPSERT 的 CASE 表达式保护：name / avatar 任一为空时不覆盖 DB 旧值
    （QQ 偶尔漏传 username 时不要把好不容易拿到的昵称擦掉）
  · 读路径：``get_name`` 直接 SELECT，主键查询 ~10–100µs，排行榜 50 次调用
    约 5ms，可接受
  · 任何 SQLite 异常 ``log.warning`` 不传播，主流程继续；连接打不开时所有
    API 走 no-op 兜底

跨热重载共享(关键):``_conn`` 和 ``_pending`` 不在模块顶级,而是放在
``boot._get_persistent()`` 字典里 —— 旧 userdb 模块实例 (被 C++ 引擎旧
callbacks 函数的 ``__globals__`` 引用住,gc 不掉) 和新 userdb 模块实例都通过
``_get_state()`` 拿到**同一份**连接和 pending,热重载有活跃对局时:

  · 旧 C++ → 旧 callbacks.cb_get_user_name → 旧 userdb.get_name → 同一份
    SQLite 连接和 pending —— 不会因为新 dispatcher 写入新 _pending 旧侧读不到
    导致昵称查询全部返空。
  · 新 dispatcher → 新 userdb.mark_dirty → 同一份 pending —— 新玩家立即可见。

整个进程全生命周期只一条 SQLite 连接,直到进程 ``os.execv`` 才真正关闭。
"""

from __future__ import annotations
import sqlite3
import asyncio
import threading
import time
from typing import Optional

from core.base.logger import get_logger, PLUGIN
from . import boot

log = get_logger(PLUGIN, 'LGTBot')

_FLUSH_INTERVAL_S = 300.0      # 5 分钟批量落盘

# flusher task 是 per-load 的(asyncio.Task 绑定当前 loop),不进持久化字典。
# @on_load 起新 task,@on_unload 取消旧 task,各自管自己,因为它们操作的真实
# 数据 (pending dict) 是同一份共享对象,谁起的 task flush 进去都一样。
_flusher_task: Optional[asyncio.Task] = None


# ──────── 连接 / Schema 初始化 ────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_cache (
    openid    TEXT PRIMARY KEY NOT NULL,
    name      TEXT NOT NULL DEFAULT '',
    avatar    TEXT NOT NULL DEFAULT '',
    last_seen INTEGER NOT NULL DEFAULT 0
)
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_user_cache_last_seen ON user_cache(last_seen)
"""

# UPSERT：CASE 保证空字符串不覆盖已有非空值
_UPSERT_SQL = """
INSERT INTO user_cache(openid, name, avatar, last_seen) VALUES(?,?,?,?)
ON CONFLICT(openid) DO UPDATE SET
    name      = CASE WHEN excluded.name   != '' THEN excluded.name   ELSE user_cache.name   END,
    avatar    = CASE WHEN excluded.avatar != '' THEN excluded.avatar ELSE user_cache.avatar END,
    last_seen = excluded.last_seen
"""


def _init_conn() -> Optional[sqlite3.Connection]:
    """打开长连接 + 建表 + PRAGMA。失败返回 None,各 API 自行 no-op 兜底。"""
    try:
        conn = sqlite3.connect(
            boot.USER_CACHE_DB,
            check_same_thread=False,   # C++ 线程读 + asyncio 线程写
            isolation_level=None,      # autocommit:flush 内显式 BEGIN/COMMIT
        )
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous  = NORMAL')
        conn.execute(_SCHEMA_SQL)
        conn.execute(_INDEX_SQL)
        return conn
    except Exception as e:
        log.warning(f'userdb 连接 / 建表失败: {e}（昵称缓存将走 no-op 兜底）')
        return None


def _get_state() -> dict:
    """跨热重载共享的 (conn, pending) 状态 —— 通过 ``boot._get_persistent()`` 字典。

    新旧 userdb 模块实例(各自的 ``__dict__`` 不同)调本函数,都拿到**同一个**
    dict 对象 (``boot._get_persistent()['userdb']``)。返回 dict 结构:
      ``{'conn': sqlite3.Connection | None, 'pending': dict[str, dict]}``

    懒初始化:首次访问时建 SQLite 连接和空 pending。 ``close()`` 主动把 conn 置
    None 之后,再次调用本函数会重开连接(``pending`` 内容保留)。

    线程安全:初始化路径用 ``boot._get_persistent()`` 里的同一把 Lock 串行 ——
    Lock 本身放进字典里,新老模块也能共享同一把。
    """
    p = boot._get_persistent()
    state = p.get('userdb')
    if state is not None and state.get('conn') is not None:
        return state
    # 慢路径:首次创建或 conn 被 close 过,需要(重新)建连接。setdefault 保证
    # 多线程并发首次进入时只创建一把 Lock,后续都拿同一把。
    p.setdefault('userdb_lock', threading.Lock())
    with p['userdb_lock']:
        state = p.get('userdb')
        if state is None:
            state = {'conn': _init_conn(), 'pending': {}}
            p['userdb'] = state
        elif state.get('conn') is None:
            state['conn'] = _init_conn()
        return state


# ──────── 读路径 ──────────────────────────────────────────────────────────
# 查找顺序：内存 pending → SQLite。
#   理由：mark_dirty 写入 pending 后,flusher 默认 5 分钟才落盘,在此之前
#   纯走 SELECT 会查不到（新用户首次发消息后整整 5 分钟内昵称仍显示截断
#   openid）。先看 pending 既消除这个窗口,也比 SQLite 主键查询快一个
#   数量级（dict.get ~100ns vs SELECT ~10µs）。
#
# 线程安全:pending 的写都在 asyncio loop 线程串行执行,读发生在 C++
# 工作线程。GIL 保证 dict.get 原子;flush_now 用 ``state['pending'] = {}``
# 重绑定 dict 入口,读到的总是某个一致的 dict 对象。

def get_name(openid: str) -> str:
    """命中返回 name,未命中返回 ''。先查 pending 再查 DB,异常吞掉返回 ''。"""
    if not openid:
        return ''
    state = _get_state()
    pend = state['pending'].get(openid)
    if pend and pend.get('name'):
        return pend['name']
    conn = state['conn']
    if conn is None:
        return ''
    try:
        cur = conn.execute(
            'SELECT name FROM user_cache WHERE openid = ?', (openid,))
        row = cur.fetchone()
        return row[0] if row else ''
    except Exception as e:
        log.debug(f'userdb.get_name 异常 ({openid}): {e}')
        return ''


def get_avatar(openid: str) -> str:
    """命中返回 avatar,未命中返回 ''。先查 pending 再查 DB。"""
    if not openid:
        return ''
    state = _get_state()
    pend = state['pending'].get(openid)
    if pend and pend.get('avatar'):
        return pend['avatar']
    conn = state['conn']
    if conn is None:
        return ''
    try:
        cur = conn.execute(
            'SELECT avatar FROM user_cache WHERE openid = ?', (openid,))
        row = cur.fetchone()
        return row[0] if row else ''
    except Exception as e:
        log.debug(f'userdb.get_avatar 异常 ({openid}): {e}')
        return ''


def count_users() -> int:
    """返回当前缓存用户总数(SQLite + ``pending`` 合并去重)。

    与 ``list_users`` 不同:本函数不取行数据、不受 limit 截断,直接给出
    完整 cardinality,适合 WebUI 顶部「总用户: N」这种汇总数字。
    SQL 异常时降级为 ``len(pending)``,保证调用方拿到的总是合理数字。
    """
    state = _get_state()
    conn = state['conn']
    pending = state['pending']
    if conn is None:
        return len(pending)
    try:
        cur = conn.execute('SELECT COUNT(*) FROM user_cache')
        db_count = cur.fetchone()[0] or 0
        if not pending:
            return db_count
        # pending 里可能有还没落盘的新 openid,跟 DB 不重叠的部分要加上
        placeholders = ','.join('?' * len(pending))
        cur = conn.execute(
            f'SELECT COUNT(*) FROM user_cache WHERE openid IN ({placeholders})',
            tuple(pending.keys()))
        overlap = cur.fetchone()[0] or 0
        return db_count + len(pending) - overlap
    except Exception as e:
        log.debug(f'userdb.count_users 异常: {e}')
        return len(pending)


def list_users(limit: int = 1000) -> list[dict]:
    """列出所有缓存用户(按 last_seen 倒序)。合并 ``pending`` 中尚未落盘的条目。

    返回列表元素: ``{'openid', 'name', 'avatar', 'last_seen'}``。
    无 DB 连接时降级到仅返回 ``pending``;DB 异常时也仅返回 pending。
    供 WebUI「用户数据」面板使用,limit 默认 1000 防止极端场景下网页过大。
    """
    state = _get_state()
    conn = state['conn']
    pending = state['pending']
    rows: dict[str, dict] = {}
    if conn is not None:
        try:
            cur = conn.execute(
                'SELECT openid, name, avatar, last_seen FROM user_cache '
                'ORDER BY last_seen DESC LIMIT ?',
                (limit,))
            for r in cur.fetchall():
                rows[r[0]] = {
                    'openid': r[0],
                    'name': r[1] or '',
                    'avatar': r[2] or '',
                    'last_seen': r[3] or 0,
                }
        except Exception as e:
            log.debug(f'userdb.list_users 异常: {e}')
    # 合并 pending —— pending 比 DB 新,非空字段覆盖;last_seen 取 pending 中
    # mark_dirty 时刻记下的真实事件时间,不再 bump 到「现在」(那样每次 WebUI
    # 刷新都会把活跃时间拉到查询时刻,完全失真)。
    for uid, e in pending.items():
        slot = rows.setdefault(uid, {
            'openid': uid, 'name': '', 'avatar': '', 'last_seen': 0,
        })
        if e['name']:
            slot['name'] = e['name']
        if e['avatar']:
            slot['avatar'] = e['avatar']
        pend_ts = e.get('last_seen', 0)
        if pend_ts > slot['last_seen']:
            slot['last_seen'] = pend_ts
    return sorted(rows.values(), key=lambda r: -r['last_seen'])[:limit]


# ──────── 写路径 ──────────────────────────────────────────────────────────

def mark_dirty(openid: str, *, name: str = '', avatar: str = '') -> None:
    """把一次更新加入 pending,5 分钟内会被 flush_now 批量落盘。

    name / avatar 为空表示「这次没有新值」—— 不覆盖 pending 已有值,
    SQL UPSERT 阶段也会再做一次空值保护(CASE 表达式)。

    ``last_seen`` 每次调用都刷成「当前时刻」,即便 name/avatar 都为空也算
    一次活跃事件(dispatcher 每条入站消息都会调一次本函数,这正是「上次
    活跃时间」的语义)。
    """
    if not openid:
        return
    state = _get_state()
    pending = state['pending']
    entry = pending.setdefault(
        openid, {'name': '', 'avatar': '', 'last_seen': 0})
    if name:
        entry['name'] = name
    if avatar:
        entry['avatar'] = avatar
    entry['last_seen'] = int(time.time())


def flush_now() -> None:
    """把 pending 一次性 UPSERT 到 DB;失败则把 pending 合并回去等下次重试。

    设计：先把 pending 整个换成空 dict,失败时再把 snapshot 合并回新 pending
    （而非整体覆盖），避免与失败期间新到的 mark_dirty 互相覆盖。

    ``last_seen`` 用 pending 里 mark_dirty 时刻记下的值,不是 flush 时刻 ——
    这样真实事件时间不会被批量落盘的整点对齐抹掉。
    """
    state = _get_state()
    conn = state['conn']
    pending = state['pending']
    if conn is None or not pending:
        return
    # 整体替换:此后新 mark_dirty 写到新 dict,snapshot 是要落盘的旧 dict
    snapshot = pending
    state['pending'] = {}
    rows = [(uid, e['name'], e['avatar'], e.get('last_seen') or 0)
            for uid, e in snapshot.items()]
    try:
        conn.execute('BEGIN')
        conn.executemany(_UPSERT_SQL, rows)
        conn.execute('COMMIT')
    except Exception as e:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        # 失败:把 snapshot 合并回(可能已有新内容的)state['pending'],等下次重试
        cur_pending = state['pending']
        for uid, ent in snapshot.items():
            slot = cur_pending.setdefault(
                uid, {'name': '', 'avatar': '', 'last_seen': 0})
            if ent['name']:
                slot['name'] = ent['name']
            if ent['avatar']:
                slot['avatar'] = ent['avatar']
            # last_seen 取较大值:失败期间可能又有新 mark_dirty 进来
            ent_ts = ent.get('last_seen') or 0
            if ent_ts > slot['last_seen']:
                slot['last_seen'] = ent_ts
        log.warning(f'userdb flush 失败 ({len(rows)} 条),保留 pending 重试: {e}')


# ──────── 后台 flusher ────────────────────────────────────────────────────

async def _flusher_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        try:
            flush_now()
        except Exception as e:
            log.warning(f'userdb flusher 异常: {e}')


def start_flusher(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """在 @on_load 中调用,把 5 分钟周期 flusher 挂到主 loop。"""
    global _flusher_task
    if loop is None:
        return
    if _flusher_task is not None and not _flusher_task.done():
        return
    try:
        _flusher_task = loop.create_task(_flusher_loop())
    except Exception as e:
        log.warning(f'启动 userdb flusher 失败: {e}')
        _flusher_task = None


def stop_flusher() -> None:
    """在 @on_unload 开头调用,取消周期任务（之后调 flush_now 强制落盘）。"""
    global _flusher_task
    if _flusher_task is not None:
        try:
            _flusher_task.cancel()
        except Exception:
            pass
        _flusher_task = None


def close() -> None:
    """关闭跨重载共享的 SQLite 连接（WAL checkpoint 在 close 时自动触发）。

    ⚠️ 注意:**默认不应从 @on_unload 调用** —— 热重载时旧 callbacks 仍要透过
    旧 userdb 模块查这条连接。只有进程真要退出 / 引擎彻底释放后才该调用。
    实务上 ``os.execv`` 重启时 OS 关 fd,这里几乎不会被自动调用了。

    再次访问 ``_get_state()`` 会自动重开连接 —— pending 保留。
    """
    p = boot._get_persistent()
    state = p.get('userdb')
    if state is None:
        return
    conn = state.get('conn')
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        state['conn'] = None
