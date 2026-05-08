#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LGTBot 消息日志 —— Web UI 拓展页面

功能：
  - 在内存中维护本插件收到 / 发出的消息日志（环形队列，默认上限 500 条）
  - 在 ElainaBot Web 面板侧边栏注册「LGTBot 日志」页面
  - 页面内嵌 JS 自动每 3 秒拉取最新日志（本身就是同一个 page url）

设计要点：
  - 不依赖任何插件自定义 HTTP 路由（框架未提供该机制）
  - 通过把 `_registry[key]` 替换为一个 dict 子类，让访问 'html' 键时
    懒生成最新 HTML —— 既兼容 `core.plugin.web_pages.get_page_html`，
    又能保证每次请求都拿到最新数据
"""

from __future__ import annotations

import json
import time
from collections import deque
from threading import Lock
from typing import Optional

from core.plugin import web_pages
from .. import boot

# ──────── 日志缓冲 ────────────────────────────────────────────────────────
# 跨插件热重载共享：日志 deque 与锁挂在 C++ 扩展上常驻进程，旧 callback
# 写入的日志在 Web 面板（新模块注册的页面）也能被读到。

_MAX_LOGS = 500
_p = boot._get_persistent()
if 'logs_deque' not in _p:
    _p['logs_deque'] = deque(maxlen=_MAX_LOGS)
    _p['logs_lock'] = Lock()
_logs: deque = _p['logs_deque']
_lock: Lock = _p['logs_lock']

PAGE_KEY = 'lgtbot-logs'


def log_incoming(uid: str, gid: str, content: str):
    """记录收到的消息（来自 QQ 玩家，准备转发给 LGTBot 引擎）"""
    _append({
        'time': time.time(),
        'direction': 'in',
        'kind': 'group' if gid else 'private',
        'uid': uid or '',
        'gid': gid or '',
        'content': content or '',
        'image': False,
    })


def log_outgoing(target_id: str, is_uid: bool, content: str, *, image: bool = False):
    """记录发出的消息（LGTBot 引擎 → QQ）"""
    _append({
        'time': time.time(),
        'direction': 'out',
        'kind': 'private' if is_uid else 'group',
        'uid': target_id if is_uid else '',
        'gid': '' if is_uid else target_id,
        'content': content or '',
        'image': image,
    })


def _append(entry: dict):
    with _lock:
        _logs.append(entry)


def get_logs() -> list:
    """快照当前所有日志"""
    with _lock:
        return list(_logs)


def clear_logs():
    with _lock:
        _logs.clear()


# ──────── HTML 模板 ───────────────────────────────────────────────────────

_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh" data-theme="light">
<head>
  <meta charset="utf-8">
  <title>LGTBot 机器人</title>
  <style>
    /* ───── 主题变量（白天默认 / 黑夜可切换）───── */
    :root[data-theme="light"] {
      --bg: #f6f7fb;
      --panel: #ffffff;
      --panel-2: #fafbff;
      --border: #e6e8f0;
      --border-2: #d9dce8;
      --text: #1f2433;
      --text-muted: #6b7280;
      --text-faint: #9aa1ad;
      --accent: #5b6ee8;
      --accent-text: #ffffff;
      --in: #1f9d55;
      --out: #1d6fdc;
      --img: #e08600;
      --tag-bg: #eef0f7;
      --tag-text: #4a5160;
      --hover: #eef1f9;
      --shadow: 0 1px 3px rgba(20, 30, 60, .06);
      --scroll-track: #f0f1f6;
      --scroll-thumb: #cdd2e0;
    }
    :root[data-theme="dark"] {
      --bg: #0f0f23;
      --panel: #1a1a2e;
      --panel-2: #15152a;
      --border: #2a2a3e;
      --border-2: #3a3a5e;
      --text: #e0e0e0;
      --text-muted: #a0a3ae;
      --text-faint: #6b6f7e;
      --accent: #7c8aff;
      --accent-text: #ffffff;
      --in: #4caf50;
      --out: #2196f3;
      --img: #ff9800;
      --tag-bg: #2a2a3e;
      --tag-text: #aab0c4;
      --hover: #22223e;
      --shadow: 0 1px 3px rgba(0, 0, 0, .3);
      --scroll-track: #0f0f23;
      --scroll-thumb: #3a3a5e;
    }

    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { background: var(--bg); color: var(--text); }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      padding: 24px; min-height: 100vh;
      transition: background .25s, color .25s;
    }
    .header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 16px; gap: 16px; flex-wrap: wrap;
    }
    .title-group { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
    h1 { font-size: 18px; color: var(--accent); }
    .subtitle { font-size: 12px; color: var(--text-faint); }
    .stats { font-size: 12px; color: var(--text-muted); font-family: monospace; }
    .badge {
      display: inline-block; padding: 1px 7px; border-radius: 3px; font-size: 10px;
      background: color-mix(in srgb, var(--accent) 18%, transparent);
      color: var(--accent); margin-left: 6px;
    }
    .filters { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
    .filters button {
      background: var(--panel); border: 1px solid var(--border); color: var(--text);
      padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
      transition: background .15s, border-color .15s, color .15s;
    }
    .filters button:hover { background: var(--hover); border-color: var(--border-2); }
    .filters button.active {
      background: var(--accent); color: var(--accent-text); border-color: var(--accent);
    }
    .filters .spacer { flex: 1; }
    .icon-btn {
      background: var(--panel) !important; border: 1px solid var(--border);
      width: 32px; height: 30px; padding: 0 !important; display: inline-flex;
      align-items: center; justify-content: center; font-size: 14px;
    }
    .log-list {
      background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
      max-height: 75vh; overflow-y: auto; box-shadow: var(--shadow);
    }
    .log-row {
      display: grid; grid-template-columns: 76px 56px 200px 1fr;
      padding: 8px 12px; border-bottom: 1px solid var(--border); gap: 10px;
      align-items: start; font-size: 12px;
    }
    .log-row:last-child { border-bottom: none; }
    .log-row:hover { background: var(--panel-2); }
    .log-row .time { color: var(--text-faint); font-family: 'Consolas', monospace; }
    .log-row .dir { font-weight: 600; font-size: 11px; }
    .log-row.in .dir { color: var(--in); }
    .log-row.out .dir { color: var(--out); }
    .log-row .who {
      color: var(--text-muted); font-family: 'Consolas', monospace;
      word-break: break-all; font-size: 11px;
    }
    .log-row .content {
      color: var(--text); word-break: break-word;
      white-space: pre-wrap; line-height: 1.5;
    }
    .log-row .img-mark { color: var(--img); font-weight: 600; }
    .log-row .kind-tag {
      display: inline-block; padding: 0 5px; border-radius: 2px; font-size: 9px;
      background: var(--tag-bg); color: var(--tag-text); margin-right: 4px;
    }
    .empty {
      padding: 48px; text-align: center; color: var(--text-faint); font-size: 13px;
    }
    .log-list::-webkit-scrollbar { width: 8px; }
    .log-list::-webkit-scrollbar-track { background: var(--scroll-track); }
    .log-list::-webkit-scrollbar-thumb { background: var(--scroll-thumb); border-radius: 4px; }
    .log-list::-webkit-scrollbar-thumb:hover { background: var(--border-2); }
  </style>
</head>
<body>
  <div class="header">
    <div class="title-group">
      <h1>LGTBot 机器人 <span class="badge" id="live-badge">实时</span></h1>
      <span class="subtitle">· 消息日志</span>
    </div>
    <div class="stats" id="stats">—</div>
  </div>

  <div class="filters">
    <button class="active" data-f="all">全部</button>
    <button data-f="in">⬇ 收到</button>
    <button data-f="out">⬆ 发出</button>
    <button data-f="group">群聊</button>
    <button data-f="private">私聊</button>
    <span class="spacer"></span>
    <button id="auto-toggle" title="暂停/恢复自动刷新">⏸ 自动</button>
    <button id="manual-refresh" title="立即刷新">🔄</button>
    <button id="theme-toggle" class="icon-btn" title="切换主题">🌙</button>
  </div>

  <div class="log-list" id="log-list"></div>

  <script id="log-data" type="application/json">__LOG_DATA_PLACEHOLDER__</script>
  <script>
    const PAGE_KEY = '__PAGE_KEY__';
    const REFRESH_MS = 3000;
    const STORAGE_THEME = 'lgtbot-page-theme';
    let currentFilter = 'all';
    let autoRefresh = true;
    let timer = null;
    let currentLogs = [];      // 缓存最近一次拿到的日志，过滤按钮直接用本地数据切换

    /* ───── 主题（白天默认） ───── */
    function applyTheme(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      document.getElementById('theme-toggle').textContent = (theme === 'dark') ? '☀' : '🌙';
      try { localStorage.setItem(STORAGE_THEME, theme); } catch (e) {}
    }
    function initTheme() {
      let saved = 'light';
      try { saved = localStorage.getItem(STORAGE_THEME) || 'light'; } catch (e) {}
      applyTheme(saved);
    }

    /* ───── 工具 ───── */
    function fmtTime(ts) {
      const d = new Date(ts * 1000);
      const h = String(d.getHours()).padStart(2, '0');
      const m = String(d.getMinutes()).padStart(2, '0');
      const s = String(d.getSeconds()).padStart(2, '0');
      return `${h}:${m}:${s}`;
    }
    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c =>
        ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    /* ───── 渲染（纯本地，不发起任何网络请求）───── */
    function render() {
      const logs = currentLogs;
      const filtered = logs.filter(l => {
        if (currentFilter === 'all') return true;
        if (currentFilter === 'in') return l.direction === 'in';
        if (currentFilter === 'out') return l.direction === 'out';
        if (currentFilter === 'group') return l.kind === 'group';
        if (currentFilter === 'private') return l.kind === 'private';
        return true;
      });
      const list = document.getElementById('log-list');
      const stats = document.getElementById('stats');
      stats.textContent = `共 ${logs.length} / 显示 ${filtered.length}  ·  ${autoRefresh ? '自动刷新中' : '已暂停'}`;
      if (!filtered.length) {
        list.innerHTML = '<div class="empty">暂无日志</div>';
        return;
      }
      const rows = filtered.slice().reverse().map(l => {
        const cls = 'log-row ' + (l.direction === 'in' ? 'in' : 'out');
        const dir = l.direction === 'in' ? '⬇ 收' : '⬆ 发';
        const kindTag = `<span class="kind-tag">${l.kind === 'group' ? '群' : '私'}</span>`;
        const who = l.gid
          ? `${kindTag}g:${escapeHtml(l.gid.slice(0, 16))}`
          : `${kindTag}u:${escapeHtml((l.uid || '').slice(0, 16))}`;
        const imgMark = l.image ? '<span class="img-mark">[图]</span> ' : '';
        const content = imgMark + escapeHtml(l.content || '');
        return `<div class="${cls}">
          <div class="time">${fmtTime(l.time)}</div>
          <div class="dir">${dir}</div>
          <div class="who">${who}</div>
          <div class="content">${content}</div>
        </div>`;
      }).join('');
      list.innerHTML = rows;
    }

    /* ───── 数据加载 ───── */
    function loadInline() {
      try {
        currentLogs = JSON.parse(document.getElementById('log-data').textContent);
        render();
      } catch (e) {
        document.getElementById('log-list').innerHTML =
          '<div class="empty">日志解析失败: ' + escapeHtml(e.message) + '</div>';
      }
    }

    async function refresh() {
      try {
        const text = await fetch('/api/web-pages/' + PAGE_KEY, { cache: 'no-store' }).then(r => r.text());
        const m = text.match(/<script id="log-data"[^>]*>([\\s\\S]*?)<\\/script>/);
        if (m) {
          currentLogs = JSON.parse(m[1]);
          render();
        }
      } catch (e) { /* 静默 */ }
    }

    /* ───── 事件绑定 ───── */
    document.querySelectorAll('.filters button[data-f]').forEach(btn => {
      btn.addEventListener('click', () => {
        currentFilter = btn.dataset.f;
        document.querySelectorAll('.filters button[data-f]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        render();   // 直接用本地缓存重新渲染（之前 bug：调用 refresh 受网络/iframe 影响）
      });
    });

    document.getElementById('auto-toggle').addEventListener('click', () => {
      autoRefresh = !autoRefresh;
      document.getElementById('auto-toggle').textContent = autoRefresh ? '⏸ 自动' : '▶ 自动';
      document.getElementById('live-badge').style.opacity = autoRefresh ? '1' : '.4';
      if (autoRefresh) timer = setInterval(refresh, REFRESH_MS);
      else { clearInterval(timer); timer = null; }
      render();   // 立即更新 stats 中的"已暂停/自动刷新中"
    });
    document.getElementById('manual-refresh').addEventListener('click', refresh);
    document.getElementById('theme-toggle').addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme');
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    });

    /* ───── 启动 ───── */
    initTheme();
    loadInline();
    timer = setInterval(refresh, REFRESH_MS);
  </script>
</body>
</html>
'''


def _render_html() -> str:
    """每次访问页面时调用，把当前 _logs 序列化为 JSON 嵌入 HTML"""
    logs = get_logs()
    data_json = json.dumps(logs, ensure_ascii=False, default=str)
    # JSON 中可能含 </script>，需转义避免破坏外层 <script> 标签
    data_json = data_json.replace('</script>', '<\\/script>')
    return (_HTML_TEMPLATE
            .replace('__LOG_DATA_PLACEHOLDER__', data_json)
            .replace('__PAGE_KEY__', PAGE_KEY))


# ──────── 注册到 Web 面板 ────────────────────────────────────────────────

class _LazyHtmlDict(dict):
    """字典子类：访问 'html' key 时调用 provider 动态生成；其他键正常字典行为。

    `core.plugin.web_pages.get_page_html` 内部对 'html' 字段先做 truthy 检查
    再取值，这两次访问都会经过 .get / .__getitem__，因此用此子类替代 _registry
    中的普通 dict 即可在每次请求时生成最新 HTML。
    """

    def __init__(self, base: dict, html_provider):
        super().__init__(base)
        self._provider = html_provider

    def get(self, key, default=None):
        if key == 'html':
            return self._provider()
        return super().get(key, default)

    def __getitem__(self, key):
        if key == 'html':
            return self._provider()
        return super().__getitem__(key)


def register():
    """在 web_pages._registry 中注册「LGTBot 机器人」拓展页面（懒渲染）

    页面 key 仍保留 'lgtbot-logs'（侧边栏 URL 依赖），仅展示名称为
    「LGTBot 机器人」。当前页面内容只有「消息日志」一个功能模块，
    后续可在此页面内追加更多功能（统计 / 排行 / 房间状态等）。
    """
    base = {
        'key': PAGE_KEY,
        'label': 'LGTBot 机器人',
        'source': 'plugin',
        'source_name': 'LGTBot_ElainaBot',
        'html': '',          # 占位，会被 _LazyHtmlDict 覆盖
        'html_file': '',
        'icon': '',
    }
    web_pages._registry[PAGE_KEY] = _LazyHtmlDict(base, _render_html)


def unregister():
    web_pages.unregister_page(PAGE_KEY)
