#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LGTBot WebUI 入口 —— 注册「LGTBot 机器人」侧边栏页面并组装多标签布局。

骨架与拼装:
  · ``PAGE_KEY = 'lgtbot'``  唯一对用户可见的侧边栏入口
  · ``RESTART_KEY = '__lgtbot_restart'``  内部 action 端点,被 ``get_pages``
    wrap 过滤,只在「重启 LGTBot」按钮 fetch 时使用
  · 顶部标题栏右侧放「🔁 重启 LGTBot」按钮(整页通用,不属于任一标签)
  · 两个标签:消息日志(``page_logs``)/ 用户数据(``page_users``);各自的
    HTML / JS / 数据生成都委托给对应模块,本文件只做组装

每次 HTTP 请求 ``_render_html()`` 跑一次,把两个标签的 HTML/JS 片段和数据
JSON 都拼进同一份 HTML —— 这样无论用户当前在哪个标签上,刷新都能就地更新。

设计注意点:
  · ``_LazyHtmlDict.get('html')`` 返回 truthy 占位而非真调 provider,避免框架
    ``core.plugin.web_pages.get_page_html`` 的「先 truthy 后取值」双次访问
    把 ``_render_restart`` 副作用跑两遍 → tcache double-free
  · ``_ensure_get_pages_filters_restart`` 一次性 wrap ``web_pages.get_pages``
    把 RESTART_KEY 从侧边栏列表里隐去,链式 wrap 不与其它插件冲突
  · ``_render_restart`` 内部延迟 import ``dispatcher``,断开循环依赖(本模块
    被 dispatcher 间接 import)
"""

from __future__ import annotations

import html

from core.plugin import web_pages
from . import page_logs, page_users


PAGE_KEY = 'lgtbot'
RESTART_KEY = '__lgtbot_restart'


# ──────── 主 HTML 模板 ─────────────────────────────────────────────────────

_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh" data-theme="light">
<head>
  <meta charset="utf-8">
  <title>LGTBot 机器人</title>
  <style>
    /* ───── 主题变量 ───── */
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

    /* ───── 顶部标题栏 ───── */
    .topbar {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 14px; gap: 16px; flex-wrap: wrap;
    }
    .topbar h1 { font-size: 18px; color: var(--accent); }
    .topbar .badge {
      display: inline-block; padding: 1px 7px; border-radius: 3px; font-size: 10px;
      background: color-mix(in srgb, var(--accent) 18%, transparent);
      color: var(--accent); margin-left: 6px;
    }
    .restart-btn {
      background: var(--panel); color: var(--text);
      border: 1px solid var(--border); padding: 6px 14px; border-radius: 6px;
      cursor: pointer; font-size: 13px;
      transition: background .15s, border-color .15s, color .15s;
    }
    .restart-btn:hover { background: var(--hover); border-color: var(--border-2); }

    /* ───── 标签导航 ───── */
    .tabs {
      display: flex; gap: 4px; margin-bottom: 14px;
      border-bottom: 1px solid var(--border);
    }
    .tabs .tab {
      background: transparent; border: none; padding: 9px 18px;
      cursor: pointer; font-size: 13px; color: var(--text-muted);
      border-bottom: 2px solid transparent; margin-bottom: -1px;
      transition: color .15s, border-color .15s;
    }
    .tabs .tab:hover { color: var(--text); }
    .tabs .tab.active {
      color: var(--accent); border-bottom-color: var(--accent); font-weight: 600;
    }

    /* ───── 标签内容容器 ───── */
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }

    /* ───── 消息日志:filters & list ───── */
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
    .filters .stats { font-size: 12px; color: var(--text-muted); font-family: monospace; }

    .log-list {
      background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
      max-height: 65vh; overflow-y: auto; box-shadow: var(--shadow);
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
    .log-list .empty {
      padding: 48px; text-align: center; color: var(--text-faint); font-size: 13px;
    }
    .log-list::-webkit-scrollbar { width: 8px; }
    .log-list::-webkit-scrollbar-track { background: var(--scroll-track); }
    .log-list::-webkit-scrollbar-thumb { background: var(--scroll-thumb); border-radius: 4px; }
    .log-list::-webkit-scrollbar-thumb:hover { background: var(--border-2); }

    /* ───── 用户数据:toolbar / 分页 / 表格 ───── */
    .users-toolbar {
      display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
      flex-wrap: wrap;
    }
    .users-toolbar .query-time-wrap {
      font-size: 12px; color: var(--text-muted);
    }
    .users-toolbar #users-query-time { font-family: monospace; color: var(--text); }
    .users-toolbar .spacer { flex: 1; }

    /* 分页控件(在「刷新数据」按钮左侧)*/
    .pagination {
      display: inline-flex; align-items: center; gap: 4px; font-size: 12px;
    }
    .pagination button {
      background: var(--panel); border: 1px solid var(--border); color: var(--text);
      width: 28px; height: 28px; border-radius: 6px; cursor: pointer;
      font-size: 14px; line-height: 1;
      display: inline-flex; align-items: center; justify-content: center;
      transition: background .15s, border-color .15s;
    }
    .pagination button:hover:not(:disabled) {
      background: var(--hover); border-color: var(--border-2);
    }
    .pagination button:disabled { opacity: .4; cursor: not-allowed; }
    .pagination input[type="number"] {
      width: 48px; height: 28px; text-align: center;
      background: var(--panel); border: 1px solid var(--border); color: var(--text);
      border-radius: 6px; font-size: 12px;
      -moz-appearance: textfield;
    }
    .pagination input[type="number"]::-webkit-outer-spin-button,
    .pagination input[type="number"]::-webkit-inner-spin-button {
      -webkit-appearance: none; margin: 0;
    }
    .pagination .page-total {
      color: var(--text-muted); font-family: monospace; padding: 0 4px;
    }

    .users-toolbar #users-refresh {
      background: var(--panel); border: 1px solid var(--border); color: var(--text);
      padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
      transition: background .15s, border-color .15s;
    }
    .users-toolbar #users-refresh:hover {
      background: var(--hover); border-color: var(--border-2);
    }
    .users-toolbar #users-refresh:disabled { opacity: .5; cursor: wait; }

    /* 用户列表网格(默认 1 列,屏幕够宽时变 2 列)
       关键:.users-section-right 的 base "display: none" 必须放在 @media 之前;
       两条规则同特异性 (0,1,0),后写者赢 —— 若 base 放在 @media 后,即便
       屏幕够宽,@media 里的 display:block 也会被 base 覆盖回 none,直接
       表现为「2 列模式右侧不显示」。 */
    .users-grid {
      display: grid; grid-template-columns: 1fr; gap: 16px;
    }
    .users-section-right { display: none; }
    @media (min-width: 1200px) {
      .users-grid { grid-template-columns: 1fr 1fr; }
      .users-section-right { display: block; }
    }

    .users-section {
      background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
      overflow: hidden; box-shadow: var(--shadow);
    }

    /* 表头:单行四列「序号 / 用户 / OpenID / 上次活跃」,列宽与 user-row 对齐 */
    .users-header {
      background: var(--panel-2); color: var(--text-muted);
      font-weight: 600; font-size: 11px;
      border-bottom: 1px solid var(--border);
    }
    .users-header .header-row {
      display: grid; grid-template-columns: 6% 30% 40% 24%;
    }
    .users-header .header-row > div {
      padding: 8px 12px; text-align: center;
    }
    .users-header .header-row .col-user { text-align: left; }

    /* 用户行 */
    .user-row {
      display: grid; grid-template-columns: 6% 30% 40% 24%;
      align-items: center; padding: 6px 0;
      border-bottom: 1px solid var(--border); font-size: 12px;
      transition: background .15s;
    }
    .user-row:last-child { border-bottom: none; }
    .user-row:hover { background: var(--panel-2); }
    .user-row > div { padding: 4px 12px; }
    .user-row .col-idx {
      text-align: center; color: var(--text-muted); font-family: monospace;
    }
    .user-row .col-user {
      display: flex; align-items: center; gap: 10px;
      word-break: break-word; min-width: 0;
    }
    .user-row .col-user .user-name { min-width: 0; word-break: break-word; }
    .user-row .col-openid {
      text-align: center; font-family: monospace; word-break: break-all;
    }
    .user-row .col-seen {
      text-align: center; font-family: monospace; color: var(--text-muted);
      word-break: keep-all;
    }
    .user-row .avatar-img {
      width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0;
      background: var(--panel-2); border: 1px solid var(--border); object-fit: cover;
    }
    .user-row.empty {
      grid-template-columns: 1fr;
      text-align: center; color: var(--text-faint); padding: 36px !important;
    }
  </style>
</head>
<body>
  <div class="topbar">
    <h1>LGTBot 机器人 <span class="badge" id="live-badge">实时</span></h1>
    <button id="restart-btn" class="restart-btn" title="重启 LGTBot 引擎 (整进程重载 C++)">🔁 重启 LGTBot</button>
  </div>

  <div class="tabs">
    <button class="tab active" data-tab="logs">📜 消息日志</button>
    <button class="tab" data-tab="users">👥 用户数据</button>
  </div>

  <div id="tab-logs" class="tab-pane active">
__LOGS_HTML__
  </div>

  <div id="tab-users" class="tab-pane">
__USERS_HTML__
  </div>

  <script id="log-data" type="application/json">__LOG_DATA__</script>
  <script id="user-data" type="application/json">__USER_DATA__</script>
  <script>
    /* ──── 主模板提供的全局 ──── */
    const PAGE_KEY = '__PAGE_KEY__';
    const RESTART_KEY = '__RESTART_KEY__';
    const REFRESH_MS = 3000;
    const STORAGE_THEME = 'lgtbot-page-theme';

    /* iframe 的 src 里带 ?token=... (auth.require_auth 只认 Bearer / ?token);
     * 内部 fetch 默认不带,要从 location.search 抠出来再拼回去。 */
    const TOKEN_QS = (function () {
      const m = location.search.match(/[?&]token=([^&]+)/);
      return m ? ('?token=' + m[1]) : '';
    })();
    const apiUrl = (key) => '/api/web-pages/' + encodeURIComponent(key) + TOKEN_QS;

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c =>
        ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    /* ──── 主题(各标签内的 #theme-toggle 共用)──── */
    function applyTheme(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      const btn = document.getElementById('theme-toggle');
      if (btn) btn.textContent = (theme === 'dark') ? '☀' : '🌙';
      try { localStorage.setItem(STORAGE_THEME, theme); } catch (e) {}
    }
    function initTheme() {
      let saved = 'light';
      try { saved = localStorage.getItem(STORAGE_THEME) || 'light'; } catch (e) {}
      applyTheme(saved);
    }

    /* ──── 标签切换 ──── */
    document.querySelectorAll('.tabs .tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const t = btn.dataset.tab;
        document.querySelectorAll('.tabs .tab').forEach(b => b.classList.toggle('active', b === btn));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + t));
      });
    });

    /* ──── 顶部全宽横幅(重启按钮反馈用)──── */
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

    /* ──── 重启按钮(整页通用,标题栏右侧)──── */
    document.getElementById('restart-btn').addEventListener('click', async () => {
      if (!confirm('确认重启 LGTBot？\\n会以全新进程重新加载 C++ 引擎、bridge 与全部游戏插件,\\n等同于命令 /重启。若存在进行中的对局会被拒绝。')) {
        return;
      }
      try {
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

    /* ──── 各标签 JS(由 page_*.py 注入)──── */
__LOGS_JS__

__USERS_JS__

    /* ──── 启动 ──── */
    initTheme();
    logsLoadInline();
    usersLoadInline();
    setInterval(logsRefresh, REFRESH_MS);
  </script>
</body>
</html>
'''


def _render_html() -> str:
    """每次访问页面调用,生成最新 HTML(含两个标签的内容和数据)。"""
    return (_HTML_TEMPLATE
            .replace('__LOGS_HTML__', page_logs.TAB_HTML)
            .replace('__USERS_HTML__', page_users.TAB_HTML)
            .replace('__LOG_DATA__', page_logs.get_data())
            .replace('__USER_DATA__', page_users.get_data())
            .replace('__LOGS_JS__', page_logs.TAB_JS)
            .replace('__USERS_JS__', page_users.TAB_JS)
            .replace('__PAGE_KEY__', PAGE_KEY)
            .replace('__RESTART_KEY__', RESTART_KEY))


# ──────── 重启 action 端点(隐藏,仅按钮 GET) ──────────────────────────────
# 复用 dispatcher 里命令 /重启 路径的 check_and_prepare_restart +
# schedule_exec_after,两条入口语义完全一致 —— 包括「有活跃对局则拒绝」原子
# 预检和「0.5s 后 os.execv 整进程」的换进程动作,确保 C++ 二进制真正被新进程
# 重新 dlopen。

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
    # 延迟 import 断开循环依赖(dispatcher 间接 import 本模块)
    from .. import dispatcher
    ok, msg = dispatcher.check_and_prepare_restart()
    if ok:
        dispatcher.schedule_exec_after(0.5)
    return _RESTART_RESPONSE_HTML.format(msg=html.escape(msg))


# ──────── LazyHtmlDict ──────────────────────────────────────────────────

class _LazyHtmlDict(dict):
    """字典子类:访问 'html' key 时调用 provider 动态生成;其他键正常字典行为。

    框架 ``get_page_html`` 内部对 'html' 字段先做 truthy 检查再取值。两次访问
    若都直传 provider,有副作用的 provider(此处 ``_render_restart`` 释放 C++ 引擎)
    会跑两遍 → 第二次 deref 已 freed 的 ``g_bot_core`` 触发 tcache double-free。
    本类 ``.get('html')`` 只返回 truthy 占位,真正生成留给 ``__getitem__``。
    """

    def __init__(self, base: dict, html_provider):
        super().__init__(base)
        self._provider = html_provider

    def get(self, key, default=None):
        if key == 'html':
            return True
        return super().get(key, default)

    def __getitem__(self, key):
        if key == 'html':
            return self._provider()
        return super().__getitem__(key)


# ──────── 侧边栏过滤 wrap ────────────────────────────────────────────────

def _ensure_get_pages_filters_restart():
    """把 ``web_pages.get_pages`` 包一层,从侧边栏列表里过滤掉 RESTART_KEY。

    幂等(``_lgtbot_wrapped`` 标记防重复包);链式(``_lgtbot_inner`` 保留对原
    函数的引用,与其它插件后续的 wrap 兼容)。
    """
    cur = web_pages.get_pages
    if getattr(cur, '_lgtbot_wrapped', False):
        return
    inner = cur

    def filtered():
        return [p for p in inner() if p.get('key') != RESTART_KEY]

    filtered._lgtbot_wrapped = True
    filtered._lgtbot_inner = inner
    web_pages.get_pages = filtered


# ──────── 注册 / 注销 ────────────────────────────────────────────────────

def register():
    """在 ``web_pages._registry`` 中注册两个页面(懒渲染):

    · ``lgtbot``           —— 「LGTBot 机器人」侧边栏入口(展示双标签内容)
    · ``__lgtbot_restart`` —— 重启 action 端点;不出现在侧边栏(由 wrap 过滤)
    """
    log_base = {
        'key': PAGE_KEY,
        'label': 'LGTBot 机器人',
        'source': 'plugin',
        'source_name': 'LGTBot_ElainaBot',
        'html': '',          # 占位,会被 _LazyHtmlDict 覆盖
        'html_file': '',
        'icon': '',
    }
    web_pages._registry[PAGE_KEY] = _LazyHtmlDict(log_base, _render_html)

    restart_base = {
        'key': RESTART_KEY,
        'label': '',         # 即便过滤失效也尽量空白显示,二重保险
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
