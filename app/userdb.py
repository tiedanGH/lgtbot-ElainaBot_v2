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
_pending: dict[str, dict] = {}  # uid → {'name','avatar'}（下次 flush 写入）
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


# ──────── 写路径 ──────────────────────────────────────────────────────────

def mark_dirty(openid: str, *, name: str = '', avatar: str = '') -> None:
    """把一次更新加入 pending,5 分钟内会被 flush_now 批量落盘。

    name / avatar 为空表示「这次没有新值」—— 不覆盖 pending 已有值,
    SQL UPSERT 阶段也会再做一次空值保护(CASE 表达式)。
    """
    if not openid or (not name and not avatar):
        return
    entry = _pending.setdefault(openid, {'name': '', 'avatar': ''})
    if name:
        entry['name'] = name
    if avatar:
        entry['avatar'] = avatar


def flush_now() -> None:
    """把 pending 一次性 UPSERT 到 DB;失败则把 pending 合并回去等下次重试。

    设计：先把 pending 整个换成空 dict,失败时再把 snapshot 合并回新 pending
    （而非整体覆盖），避免与失败期间新到的 mark_dirty 互相覆盖。
    """
    global _pending
    if _conn is None or not _pending:
        return
    snapshot = _pending
    _pending = {}
    now = int(time.time())
    rows = [(uid, e['name'], e['avatar'], now) for uid, e in snapshot.items()]
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
            slot = _pending.setdefault(uid, {'name': '', 'avatar': ''})
            if ent['name']:
                slot['name'] = ent['name']
            if ent['avatar']:
                slot['avatar'] = ent['avatar']
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
