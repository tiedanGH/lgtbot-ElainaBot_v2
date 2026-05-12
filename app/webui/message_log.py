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

import html
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

PAGE_KEY = 'lgtbot'
# 「重启」按钮的 action 端点 —— 下划线前缀表内部用途;通过 _ensure_get_pages_filters
# 从侧边栏列表里过滤掉,用户视角下只看到一个 PAGE_KEY 对应的「LGTBot 机器人」入口。
# 必须借助一个独立 web_pages 注册来跑「检查 + 释放 + 调度 exec」逻辑 —— 框架未给
# 插件提供自定义 HTTP 路由钩子,/api/plugins/reload 只做 plugin 热重载不动 C++ .so,
# /api/bot/restart 又没有 LGTBot 活跃对局的预检,都不合适。
RESTART_KEY = '__lgtbot_restart'


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
    <button id="restart-btn" title="重启 LGTBot 引擎(重新加载全部 C++)">🔁 重启 LGTBot</button>
    <button id="auto-toggle" title="暂停/恢复自动刷新">⏸ 自动</button>
    <button id="manual-refresh" title="立即刷新">🔄</button>
    <button id="theme-toggle" class="icon-btn" title="切换主题">🌙</button>
  </div>

  <div class="log-list" id="log-list"></div>

  <script id="log-data" type="application/json">__LOG_DATA_PLACEHOLDER__</script>
  <script>
    const PAGE_KEY = '__PAGE_KEY__';
    const RESTART_KEY = '__RESTART_KEY__';
    const REFRESH_MS = 3000;
    const STORAGE_THEME = 'lgtbot-page-theme';
    let currentFilter = 'all';
    let autoRefresh = true;
    let timer = null;
    let currentLogs = [];      // 缓存最近一次拿到的日志，过滤按钮直接用本地数据切换

    /* ───── 认证 token 透传 ─────
     * core/web/auth.py::validate_token 仅检 Authorization: Bearer 头或
     * ?token= 查询参数,不查 cookie。SPA 加载 iframe 时把 token 放在 src 的
     * ?token= 里只够首次加载用,iframe 内 JS 再发 fetch 默认不带,导致 401。
     * 这里从自身 location.search 把 token 抠出来,每次 fetch 都拼回去。
     */
    const TOKEN_QS = (function () {
      const m = location.search.match(/[?&]token=([^&]+)/);
      return m ? ('?token=' + m[1]) : '';
    })();
    const apiUrl = (key) => '/api/web-pages/' + encodeURIComponent(key) + TOKEN_QS;

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
        const r = await fetch(apiUrl(PAGE_KEY), { cache: 'no-store' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const text = await r.text();
        const m = text.match(/<script id="log-data"[^>]*>([\\s\\S]*?)<\\/script>/);
        if (m) {
          currentLogs = JSON.parse(m[1]);
          render();
        }
      } catch (e) {
        // 不再静默：把错误丢到 console 便于排查,但不打扰用户(下次 tick 自动重试)
        console.warn('[lgtbot-logs] refresh failed:', e);
      }
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

    /* ───── 重启按钮 + 顶部横幅 ───── */
    function showBanner(msg, isWarning) {
      const old = document.getElementById('lgtbot-banner');
      if (old) old.remove();
      const b = document.createElement('div');
      b.id = 'lgtbot-banner';
      b.style.cssText =
        'position:fixed;top:0;left:0;right:0;padding:14px 24px;text-align:center;' +
        'background:' + (isWarning ? '#dc3545' : '#1d6fdc') + ';' +
        'color:#fff;z-index:9999;box-shadow:0 2px 8px rgba(0,0,0,.18);' +
        'font-size:14px;font-weight:500;white-space:pre-wrap;line-height:1.6;';
      b.textContent = msg;
      document.body.appendChild(b);
      setTimeout(() => b.remove(), 8000);
    }
    document.getElementById('restart-btn').addEventListener('click', async () => {
      if (!confirm('确认重启 LGTBot？\\n会以全新进程重新加载 C++ 引擎、bridge 与全部游戏插件,\\n等同于命令 /重启。若存在进行中的对局会被拒绝。')) {
        return;
      }
      try {
        // RESTART_KEY 是个隐藏的 web_pages 注册项,GET 它即触发服务端的「检查
        // 活跃对局 → 释放引擎 → 调度 os.execv」流程(与命令 /重启 共用同一对
        // dispatcher helper),最后返回带 <div id="msg"> 的小确认页。
        // 我们从那个 div 把文案抠出来横幅化;0.5s 后服务端就 exec 了,届时本
        // 页面会短暂 502,等用户手动刷新即可看到新进程的页面。
        const r = await fetch(apiUrl(RESTART_KEY), { cache: 'no-store' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const text = await r.text();
        const doc = new DOMParser().parseFromString(text, 'text/html');
        const msgEl = doc.getElementById('msg');
        const msg = msgEl ? msgEl.textContent.trim() : '已请求重启';
        const isWarn = msg.includes('⚠️') || msg.includes('❌') || msg.includes('ℹ️');
        showBanner(msg, isWarn);
      } catch (e) {
        showBanner('重启请求失败: ' + e.message, true);
      }
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
            .replace('__PAGE_KEY__', PAGE_KEY)
            .replace('__RESTART_KEY__', RESTART_KEY))


# ──────── 重启 action 端点(隐藏,仅按钮 GET) ──────────────────────────────
# 复用 dispatcher 里命令路径的 check_and_prepare_restart + schedule_exec_after,
# 两条入口(IM 命令 /重启 与 WebUI 按钮)语义完全一致 —— 包括「有活跃对局则
# 拒绝」的原子预检和「0.5s 后 os.execv 整进程」的换进程动作,确保 C++ 二进制
# 真正被新进程重新 dlopen。
# 注:LazyHtmlDict.get('html') 已改成返回 truthy 占位(见类 docstring),provider
# 在一次 HTTP 请求中只会被调用一次 —— 否则这里的 release_bot 副作用会跑两遍,
# 第二遍 deref freed 的 g_bot_core 引发 tcache double-free。

_RESTART_RESPONSE_HTML = '''<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>LGTBot 重启</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         padding: 40px; background: #f6f7fb; color: #1f2433; }}
  .card {{ background: #fff; border: 1px solid #e6e8f0; border-radius: 8px;
          padding: 32px; max-width: 480px; margin: 60px auto; text-align: center;
          box-shadow: 0 1px 3px rgba(20,30,60,.06); }}
  h1 {{ font-size: 18px; color: #5b6ee8; margin-bottom: 16px; }}
  #msg {{ font-size: 14px; line-height: 1.7; white-space: pre-wrap; }}
  .hint {{ margin-top: 20px; font-size: 12px; color: #9aa1ad; }}
</style></head>
<body><div class="card"><h1>LGTBot 重启</h1>
<div id="msg">{msg}</div>
<div class="hint">几秒后进程会被替换,本页面将短暂不可访问 —— 稍候手动刷新。</div>
</div></body></html>'''


def _render_restart() -> str:
    """触发重启 + 返回确认页 HTML。"""
    # 延迟 import,断开与 dispatcher 的循环依赖(dispatcher 顶层 import 本模块)
    from .. import dispatcher
    ok, msg = dispatcher.check_and_prepare_restart()
    if ok:
        dispatcher.schedule_exec_after(0.5)
    return _RESTART_RESPONSE_HTML.format(msg=html.escape(msg))


# ──────── 注册到 Web 面板 ────────────────────────────────────────────────

class _LazyHtmlDict(dict):
    """字典子类：访问 'html' key 时调用 provider 动态生成；其他键正常字典行为。

    `core.plugin.web_pages.get_page_html` 内部对 'html' 字段先做 truthy 检查
    (info.get('html')) 再取值 (info['html'])。**单次 HTTP 请求会触发两次访问。**
    若 provider 有副作用,会被执行两次 —— 这对重启 action 就是灾难:第一次
    调 release_bot_if_not_processing_games 释放 C++ 的 BotCtx,第二次再调时
    g_bot_core 已是悬空指针,导致 `free(): double free detected in tcache 2`。
    因此 .get('html') 只返回 truthy 占位,真正的 provider 调用只走 __getitem__。
    """

    def __init__(self, base: dict, html_provider):
        super().__init__(base)
        self._provider = html_provider

    def get(self, key, default=None):
        if key == 'html':
            return True   # 占位 truthy,不真正生成 HTML(避免 provider 副作用被双触发)
        return super().get(key, default)

    def __getitem__(self, key):
        if key == 'html':
            return self._provider()
        return super().__getitem__(key)


def _ensure_get_pages_filters_restart():
    """把 web_pages.get_pages 包一层,从侧边栏列表里过滤掉 RESTART_KEY。

    RESTART_KEY 只是个 HTTP action 端点(主页内的「🔁 重启 LGTBot」按钮 GET
    它就触发重启),不该再以独立页面身份出现在用户侧边栏。框架的 get_pages
    直接 dump 整个 _registry,无 plugin 级过滤钩子,只能就地包一层。

    幂等:多次 register()(热重载)只包一次。链式:其它 plugin 后续若也包了
    一层,通过 _lgtbot_inner 保留对原函数的引用,链不被冲断。
    """
    cur = web_pages.get_pages
    if getattr(cur, '_lgtbot_wrapped', False):
        return  # 本插件已经包过,跳过
    inner = cur

    def filtered():
        return [p for p in inner() if p.get('key') != RESTART_KEY]

    filtered._lgtbot_wrapped = True
    filtered._lgtbot_inner = inner
    web_pages.get_pages = filtered


def register():
    """在 web_pages._registry 中注册两个页面(懒渲染):

    · ``lgtbot``          —— 「LGTBot 机器人」消息日志页(显示于侧边栏)
    · ``__lgtbot_restart`` —— 重启 action 端点;不显示在侧边栏(被
                              _ensure_get_pages_filters_restart 过滤),
                              仅作为主页内「🔁 重启 LGTBot」按钮 GET 的目标。

    两个 key 都走 _LazyHtmlDict,确保每次访问触发最新的 HTML 生成。
    """
    log_base = {
        'key': PAGE_KEY,
        'label': 'LGTBot 机器人',
        'source': 'plugin',
        'source_name': 'LGTBot_ElainaBot',
        'html': '',          # 占位，会被 _LazyHtmlDict 覆盖
        'html_file': '',
        'icon': '',
    }
    web_pages._registry[PAGE_KEY] = _LazyHtmlDict(log_base, _render_html)

    restart_base = {
        'key': RESTART_KEY,
        'label': '',         # 即使过滤失效也尽量空白显示,二重保险
        'source': 'plugin',
        'source_name': 'LGTBot_ElainaBot',
        'html': '',
        'html_file': '',
        'icon': '',
    }
    web_pages._registry[RESTART_KEY] = _LazyHtmlDict(restart_base, _render_restart)

    _ensure_get_pages_filters_restart()


def unregister():
    web_pages.unregister_page(PAGE_KEY)
    web_pages.unregister_page(RESTART_KEY)
    # get_pages 的 wrap 不主动 unwrap:其它插件可能后续也加了包装,贸然恢复会断链。
    # 留着的副作用仅是过滤一个已不存在的 key,无害。
