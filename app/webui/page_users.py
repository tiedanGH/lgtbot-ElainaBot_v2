#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「用户数据」标签 —— 查询 ``data/user_cache.db`` 并以网格方式展示。

布局
  · 顶部工具栏:查询时间 / 分页控件 / 「🔄 刷新数据」按钮
  · 主体:用「网格 + 双行表头」实现的伪表格(div + CSS grid),不用 <table>
    是因为 2 列模式下要保证按时间从左到右、从上到下排列,table 的行布局
    满足不了
  · 表头列宽:用户 40%(头像 10% + 名称 30%)/ OpenID 40% / 上次活跃 20%
  · OpenID 与 上次活跃 居中显示
  · 头像直接用 ``<img>`` 渲染 ``q.qlogo.cn`` 直链

响应式:屏幕宽 ≥ 1200px 时变成两列(每页 100 条);否则单列(每页 50 条)。
列内排序由 ``userdb.list_users()`` 按 last_seen 降序给出,前端 ``grid-auto-flow:
row`` 把元素从左到右铺,刚好对应「时间越近越靠前(左+上)」。

分页:前端纯 JS,数据仍由后端一次性 list_users(limit=1000) 返回;翻页是切片。
1000 条上限对插件级用户量(社群尺度)足够,如有更大需求再补后端分页。
"""

from __future__ import annotations

import json
import time

from .. import userdb


# ──────── HTML 片段(嵌入主模板 #tab-users 内)─────────────────────────────

TAB_HTML = '''
<div class="users-toolbar">
  <span class="query-time-wrap">查询时间: <span id="users-query-time">—</span></span>
  <span class="spacer"></span>
  <div class="pagination">
    <button id="users-prev" title="上一页">‹</button>
    <input type="number" id="users-page-input" min="1" value="1" title="输入页码跳转">
    <span class="page-total">/ <span id="users-page-total">1</span></span>
    <button id="users-next" title="下一页">›</button>
  </div>
  <button id="users-refresh" title="重新查询用户数据库">🔄 刷新数据</button>
</div>

<div class="users-grid">
  <div class="users-section">
    <div class="users-header">
      <div class="header-row">
        <div class="col-idx">序号</div>
        <div class="col-user">用户</div>
        <div class="col-openid">OpenID</div>
        <div class="col-seen">上次活跃</div>
      </div>
    </div>
    <div id="users-list-1"></div>
  </div>
  <div class="users-section users-section-right">
    <div class="users-header">
      <div class="header-row">
        <div class="col-idx">序号</div>
        <div class="col-user">用户</div>
        <div class="col-openid">OpenID</div>
        <div class="col-seen">上次活跃</div>
      </div>
    </div>
    <div id="users-list-2"></div>
  </div>
</div>
'''


# ──────── JS 片段(注入主模板 <script> 内)──────────────────────────────
# 依赖主模板里已定义的: PAGE_KEY / apiUrl(key) / escapeHtml

TAB_JS = '''
let usersCache = [];
let usersQueryTs = 0;
let usersPage = 1;
const usersWideMQ = window.matchMedia('(min-width: 1200px)');

function usersPageSize() { return usersWideMQ.matches ? 100 : 50; }
function usersTotalPages() {
  return Math.max(1, Math.ceil(usersCache.length / usersPageSize()));
}

function usersFmtDateTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' +
         String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' +
         String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

function usersRowHtml(u, serial) {
  /* serial 是该用户在按 last_seen 降序的完整列表中的全局名次(1-indexed,
     跨页累加),始终唯一,即便切换 1/2 列模式或翻页也对得上。 */
  const avatar = u.avatar
    ? '<img class="avatar-img" src="' + escapeHtml(u.avatar) + '" alt="" loading="lazy" referrerpolicy="no-referrer">'
    : '<div class="avatar-img"></div>';
  const name = escapeHtml(u.name || '—');
  return '<div class="user-row">' +
    '<div class="col-idx">' + serial + '</div>' +
    '<div class="col-user">' + avatar + '<span class="user-name">' + name + '</span></div>' +
    '<div class="col-openid">' + escapeHtml(u.openid || '') + '</div>' +
    '<div class="col-seen">' + usersFmtDateTime(u.last_seen) + '</div>' +
  '</div>';
}

function usersLoadInline() {
  try {
    const data = JSON.parse(document.getElementById('user-data').textContent);
    usersCache = data.users || [];
    usersQueryTs = data.query_time || 0;
    usersPage = 1;
    usersRender();
  } catch (e) {
    document.getElementById('users-list-1').innerHTML =
      '<div class="user-row empty">用户数据解析失败: ' + escapeHtml(e.message) + '</div>';
  }
}

function usersRender() {
  /* 查询时间始终更新 */
  document.getElementById('users-query-time').textContent = usersFmtDateTime(usersQueryTs);

  /* 分页:钳位、刷 UI */
  const total = usersTotalPages();
  if (usersPage > total) usersPage = total;
  if (usersPage < 1) usersPage = 1;
  document.getElementById('users-page-input').value = usersPage;
  document.getElementById('users-page-total').textContent = total;
  document.getElementById('users-prev').disabled = usersPage <= 1;
  document.getElementById('users-next').disabled = usersPage >= total;

  const list1 = document.getElementById('users-list-1');
  const list2 = document.getElementById('users-list-2');

  if (!usersCache.length) {
    list1.innerHTML = '<div class="user-row empty">暂无用户数据</div>';
    list2.innerHTML = '';
    return;
  }

  const pageSize = usersPageSize();
  const start = (usersPage - 1) * pageSize;
  const slice = usersCache.slice(start, start + pageSize);

  if (usersWideMQ.matches) {
    /* 2 列模式:按 index 奇偶拆,即第 0/2/4 …→ 左列,1/3/5 …→ 右列。
       因为 list_users 已按 last_seen 降序,所以视觉上左上最新,然后右上,然后
       左二行,然后右二行 …… 满足「从左到右从上到下排列活跃时间」。
       序号 = 在完整列表里的全局名次 = start + 本页内 index + 1。 */
    const leftHtml = [];
    const rightHtml = [];
    slice.forEach((u, i) => {
      const serial = start + i + 1;
      const row = usersRowHtml(u, serial);
      if (i % 2 === 0) leftHtml.push(row);
      else rightHtml.push(row);
    });
    list1.innerHTML = leftHtml.join('');
    list2.innerHTML = rightHtml.join('');
  } else {
    list1.innerHTML = slice.map((u, i) => usersRowHtml(u, start + i + 1)).join('');
    list2.innerHTML = '';
  }
}

async function usersRefresh() {
  const btn = document.getElementById('users-refresh');
  btn.disabled = true;
  try {
    const r = await fetch(apiUrl(PAGE_KEY), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const m = text.match(/<script id="user-data"[^>]*>([\\s\\S]*?)<\\/script>/);
    if (m) {
      const data = JSON.parse(m[1]);
      usersCache = data.users || [];
      usersQueryTs = data.query_time || 0;
      /* 刷新数据后回到第 1 页,语义上「重新查询」就应该看到最新顶部 */
      usersPage = 1;
      usersRender();
    }
  } catch (e) {
    console.warn('[users] refresh failed:', e);
  } finally {
    btn.disabled = false;
  }
}

/* 分页事件 */
document.getElementById('users-prev').addEventListener('click', () => {
  if (usersPage > 1) { usersPage--; usersRender(); }
});
document.getElementById('users-next').addEventListener('click', () => {
  if (usersPage < usersTotalPages()) { usersPage++; usersRender(); }
});
document.getElementById('users-page-input').addEventListener('change', (e) => {
  const v = parseInt(e.target.value, 10);
  if (!isNaN(v)) { usersPage = v; usersRender(); }
});
document.getElementById('users-refresh').addEventListener('click', usersRefresh);

/* 屏幕跨过 1200px 阈值时重排(2 列 ⇄ 1 列,每页容量也会变)。
   matchMedia 比 resize 监听更节流,只在阈值翻转时触发。 */
usersWideMQ.addEventListener('change', usersRender);
'''


def get_data() -> str:
    """返回 ``{query_time, users}`` JSON,可嵌入 ``<script id="user-data">``。"""
    payload = {
        'query_time': int(time.time()),
        'users': userdb.list_users(),
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')
