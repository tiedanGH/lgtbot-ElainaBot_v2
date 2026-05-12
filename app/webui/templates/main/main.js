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

/* ──── 主题(顶部标题栏右侧的 #theme-toggle,整页通用)────
   图标显示「当前主题」: 浅色 ☀,深色 🌙(展示当前态而非目标态)。
   默认 light;localStorage 持久化用户选择。 */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = (theme === 'dark') ? '🌙' : '☀';
  try { localStorage.setItem(STORAGE_THEME, theme); } catch (e) {}
}
function initTheme() {
  let saved = 'light';
  try { saved = localStorage.getItem(STORAGE_THEME) || 'light'; } catch (e) {}
  applyTheme(saved);
}
document.getElementById('theme-toggle').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'dark' ? 'light' : 'dark');
});

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
  if (!confirm('确认重启 LGTBot？\n会以全新进程重新加载 C++ 引擎、bridge 与全部游戏插件,\n等同于命令 /重启。若存在进行中的对局会被拒绝。')) {
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

/* ──── 启动 ──── */
window.addEventListener('DOMContentLoaded', () => {
  initTheme();
  logsLoadInline();
  usersLoadInline();
  setInterval(logsRefresh, REFRESH_MS);
});
