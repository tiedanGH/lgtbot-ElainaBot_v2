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
"""

from __future__ import annotations
import sqlite3
import asyncio
import time
from typing import Optional

from core.base.logger import get_logger, PLUGIN
from . import boot

log = get_logger(PLUGIN, 'LGTBot')

_FLUSH_INTERVAL_S = 300.0      # 5 分钟批量落盘
_conn: Optional[sqlite3.Connection] = None
# uid → {'name','avatar','last_seen'}（下次 flush 写入）
# last_seen 在 mark_dirty 调用时刻就记下,不是 flush 时刻 —— 否则 WebUI 看到
# 的「上次活跃」最多会比真实事件晚 5 分钟（一个完整 flush 周期）。
_pending: dict[str, dict] = {}
_flusher_task: Optional[asyncio.Task] = None


# ──────── 连接 / Schema 初始化（模块 import 即执行）─────────────────────────

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


_conn = _init_conn()


# ──────── 读路径 ──────────────────────────────────────────────────────────
# 查找顺序：内存 _pending → SQLite。
#   理由：mark_dirty 写入 _pending 后,flusher 默认 5 分钟才落盘,在此之前
#   纯走 SELECT 会查不到（新用户首次发消息后整整 5 分钟内昵称仍显示截断
#   openid）。先看 _pending 既消除这个窗口,也比 SQLite 主键查询快一个
#   数量级（dict.get ~100ns vs SELECT ~10µs）。
#
# 线程安全：_pending 的写都在 asyncio loop 线程串行执行,读发生在 C++
# 工作线程。GIL 保证 dict.get 原子,`_pending = {}` 重绑定也原子,读到
# 的总是某个一致的 dict 对象。

def get_name(openid: str) -> str:
    """命中返回 name,未命中返回 ''。先查 _pending 再查 DB,异常吞掉返回 ''。"""
    if not openid:
        return ''
    pend = _pending.get(openid)
    if pend and pend.get('name'):
        return pend['name']
    if _conn is None:
        return ''
    try:
        cur = _conn.execute(
            'SELECT name FROM user_cache WHERE openid = ?', (openid,))
        row = cur.fetchone()
        return row[0] if row else ''
    except Exception as e:
        log.debug(f'userdb.get_name 异常 ({openid}): {e}')
        return ''


def get_avatar(openid: str) -> str:
    """命中返回 avatar,未命中返回 ''。先查 _pending 再查 DB。"""
    if not openid:
        return ''
    pend = _pending.get(openid)
    if pend and pend.get('avatar'):
        return pend['avatar']
    if _conn is None:
        return ''
    try:
        cur = _conn.execute(
            'SELECT avatar FROM user_cache WHERE openid = ?', (openid,))
        row = cur.fetchone()
        return row[0] if row else ''
    except Exception as e:
        log.debug(f'userdb.get_avatar 异常 ({openid}): {e}')
        return ''


def count_users() -> int:
    """返回当前缓存用户总数(SQLite + ``_pending`` 合并去重)。

    与 ``list_users`` 不同:本函数不取行数据、不受 limit 截断,直接给出
    完整 cardinality,适合 WebUI 顶部「总用户: N」这种汇总数字。
    SQL 异常时降级为 ``len(_pending)``,保证调用方拿到的总是合理数字。
    """
    if _conn is None:
        return len(_pending)
    try:
        cur = _conn.execute('SELECT COUNT(*) FROM user_cache')
        db_count = cur.fetchone()[0] or 0
        if not _pending:
            return db_count
        # _pending 里可能有还没落盘的新 openid,跟 DB 不重叠的部分要加上
        placeholders = ','.join('?' * len(_pending))
        cur = _conn.execute(
            f'SELECT COUNT(*) FROM user_cache WHERE openid IN ({placeholders})',
            tuple(_pending.keys()))
        overlap = cur.fetchone()[0] or 0
        return db_count + len(_pending) - overlap
    except Exception as e:
        log.debug(f'userdb.count_users 异常: {e}')
        return len(_pending)


def list_users(limit: int = 1000) -> list[dict]:
    """列出所有缓存用户(按 last_seen 倒序)。合并 ``_pending`` 中尚未落盘的条目。

    返回列表元素: ``{'openid', 'name', 'avatar', 'last_seen'}``。
    无 DB 连接时降级到仅返回 ``_pending``;DB 异常时也仅返回 pending。
    供 WebUI「用户数据」面板使用,limit 默认 1000 防止极端场景下网页过大。
    """
    rows: dict[str, dict] = {}
    if _conn is not None:
        try:
            cur = _conn.execute(
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
    # 合并 _pending —— pending 比 DB 新,非空字段覆盖;last_seen 取 pending 中
    # mark_dirty 时刻记下的真实事件时间,不再 bump 到「现在」(那样每次 WebUI
    # 刷新都会把活跃时间拉到查询时刻,完全失真)。
    for uid, e in _pending.items():
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
    entry = _pending.setdefault(
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
    global _pending
    if _conn is None or not _pending:
        return
    snapshot = _pending
    _pending = {}
    rows = [(uid, e['name'], e['avatar'], e.get('last_seen') or 0)
            for uid, e in snapshot.items()]
    try:
        _conn.execute('BEGIN')
        _conn.executemany(_UPSERT_SQL, rows)
        _conn.execute('COMMIT')
    except Exception as e:
        try:
            _conn.execute('ROLLBACK')
        except Exception:
            pass
        for uid, ent in snapshot.items():
            slot = _pending.setdefault(
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
    """关闭长连接(WAL checkpoint 在连接 close 时自动触发)。"""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
