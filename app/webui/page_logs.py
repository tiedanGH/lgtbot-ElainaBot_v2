#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「消息日志」标签 —— HTML 片段 + JS 片段 + 数据生成。

被 ``webui/main.py`` 拼装到主页面。功能与重构前完全一致(过滤、自动 3 秒刷新),
仅:
  · 去掉了「立即刷新」按钮(自动刷新已经够用)
  · 切换主题 / 自动刷新切换 从顶栏挪到本标签内部底部,切到「用户数据」标签
    会随 .tab-pane 整体隐藏(.active 切换)
"""

from __future__ import annotations

import json

from . import message_log


# ──────── HTML 片段(嵌入主模板 #tab-logs 内)─────────────────────────────

TAB_HTML = '''
<div class="filters">
  <button class="active" data-f="all">全部</button>
  <button data-f="in">⬇ 收到</button>
  <button data-f="out">⬆ 发出</button>
  <button data-f="group">群聊</button>
  <button data-f="private">私聊</button>
  <span class="spacer"></span>
  <span class="stats" id="logs-stats">—</span>
  <button id="auto-toggle" title="暂停/恢复自动刷新">⏸ 自动</button>
  <button id="theme-toggle" class="icon-btn" title="切换主题">🌙</button>
</div>
<div class="log-list" id="log-list"></div>
'''


# ──────── JS 片段(注入主模板 <script> 内)──────────────────────────────
# 依赖主模板里早就定义好的: PAGE_KEY / apiUrl(key) / escapeHtml / applyTheme

TAB_JS = '''
let logsFilter = 'all';
let logsAutoRefresh = true;
let logsTimer = null;
let logsCache = [];

function logsFmtTime(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

function logsLoadInline() {
  try {
    logsCache = JSON.parse(document.getElementById('log-data').textContent);
    logsRender();
  } catch (e) {
    document.getElementById('log-list').innerHTML =
      '<div class="empty">日志解析失败: ' + escapeHtml(e.message) + '</div>';
  }
}

function logsRender() {
  const filtered = logsCache.filter(l => {
    if (logsFilter === 'all') return true;
    if (logsFilter === 'in') return l.direction === 'in';
    if (logsFilter === 'out') return l.direction === 'out';
    if (logsFilter === 'group') return l.kind === 'group';
    if (logsFilter === 'private') return l.kind === 'private';
    return true;
  });
  const list = document.getElementById('log-list');
  const stats = document.getElementById('logs-stats');
  stats.textContent = '共 ' + logsCache.length + ' / 显示 ' + filtered.length +
    '  ·  ' + (logsAutoRefresh ? '自动刷新中' : '已暂停');
  if (!filtered.length) {
    list.innerHTML = '<div class="empty">暂无日志</div>';
    return;
  }
  const rows = filtered.slice().reverse().map(l => {
    const cls = 'log-row ' + (l.direction === 'in' ? 'in' : 'out');
    const dir = l.direction === 'in' ? '⬇ 收' : '⬆ 发';
    const kindTag = '<span class="kind-tag">' + (l.kind === 'group' ? '群' : '私') + '</span>';
    const who = l.gid
      ? kindTag + 'g:' + escapeHtml(l.gid.slice(0, 16))
      : kindTag + 'u:' + escapeHtml((l.uid || '').slice(0, 16));
    const imgMark = l.image ? '<span class="img-mark">[图]</span> ' : '';
    const content = imgMark + escapeHtml(l.content || '');
    return '<div class="' + cls + '">' +
      '<div class="time">' + logsFmtTime(l.time) + '</div>' +
      '<div class="dir">' + dir + '</div>' +
      '<div class="who">' + who + '</div>' +
      '<div class="content">' + content + '</div>' +
    '</div>';
  }).join('');
  list.innerHTML = rows;
}

async function logsRefresh() {
  if (!logsAutoRefresh) return;
  try {
    const r = await fetch(apiUrl(PAGE_KEY), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const m = text.match(/<script id="log-data"[^>]*>([\\s\\S]*?)<\\/script>/);
    if (m) {
      logsCache = JSON.parse(m[1]);
      logsRender();
    }
  } catch (e) {
    console.warn('[logs] refresh failed:', e);
  }
}

document.querySelectorAll('.filters button[data-f]').forEach(btn => {
  btn.addEventListener('click', () => {
    logsFilter = btn.dataset.f;
    document.querySelectorAll('.filters button[data-f]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    logsRender();
  });
});

document.getElementById('auto-toggle').addEventListener('click', () => {
  logsAutoRefresh = !logsAutoRefresh;
  document.getElementById('auto-toggle').textContent = logsAutoRefresh ? '⏸ 自动' : '▶ 自动';
  const badge = document.getElementById('live-badge');
  if (badge) badge.style.opacity = logsAutoRefresh ? '1' : '.4';
  logsRender();
});

document.getElementById('theme-toggle').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'dark' ? 'light' : 'dark');
});
'''


def get_data() -> str:
    """返回 logs JSON,可直接嵌入 ``<script id="log-data">``。

    JSON 中可能含 ``</script>``,先转义避免破坏外层 ``<script>`` 标签。"""
    data_json = json.dumps(message_log.get_logs(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')
