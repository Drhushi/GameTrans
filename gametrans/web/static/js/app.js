/* gametrans 控制台 —— 装配与调度。
 *
 * 面板是观察站：它渲染交互层的视图与工作区产物，可写的只有配置 / 凭证 /
 * 引擎 SDK 路径 / 内容资产 / 单条译文，调度权在 agent 与 CLI。
 *
 * 刷新策略（用户可感知的承诺）：
 *   - 有动静（状态签名变化）约 2 秒一轮，空闲约 8 秒，页面切到后台就停；
 *   - 重数据（路径图 / 译文样本 / 资源清单）只在对应视图激活时拉，且隔几轮才刷；
 *   - 渲染走增量 patch，输入中的表单永不被刷新冲掉。
 */

import { $, num, sig } from './util.js';
import { api, probeCapabilities, errBanner } from './api.js';
import * as store from './store.js';
import { applyTheme } from './views/settings.js';
import * as overviewView from './views/overview.js';
import * as graphView from './views/graph.js';
import * as translationsView from './views/translations.js';
import * as resourcesView from './views/resources.js';
import * as settingsView from './views/settings.js';
import * as workbenchView from './views/workbench.js';
import './titlebar.js';
import { mountTutorial, maybeAutoStart } from './tutorial.js';
import { armScrollbar } from './scrollbar.js';

const VIEWS = [
  overviewView.view,
  graphView.view,
  translationsView.view,
  resourcesView.view,
  workbenchView.view,
  settingsView.view,
];

const TITLES = {
  overview: '总览',
  graph: '路径图',
  translations: '译文',
  resources: '资源',
  workbench: '请求',
  settings: '设置',
};

/* ---------- 路由 ---------- */

function currentViewId() {
  const match = /^#\/([a-z]+)/.exec(location.hash || '');
  const name = match ? match[1] : '';
  return name in TITLES ? name : 'overview';
}

function activeView() {
  return VIEWS.find((v) => v.id === currentViewId());
}

function applyRoute() {
  const id = currentViewId();
  document.querySelectorAll('.view').forEach((el) => {
    el.classList.toggle('active', el.dataset.view === id);
  });
  document.querySelectorAll('.nav-item').forEach((el) => {
    el.classList.toggle('active', el.dataset.nav === id);
  });
  $('bar-title').textContent = TITLES[id];
  document.title = `gametrans · ${TITLES[id]}`;
  const view = activeView();
  view?.activate?.();
  view?.render?.();
  fetchForView(id, true); // 切视图立刻要新鲜数据
}

window.addEventListener('hashchange', applyRoute);

/* 数字键 1-6 切视图；正在表单里打字时不抢按键；新手教程进行中让给它 */
window.addEventListener('keydown', (event) => {
  if (event.ctrlKey || event.altKey || event.metaKey) return;
  if (document.documentElement.dataset.tour === '1') return;
  const tag = event.target && event.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  const order = VIEWS.map((v) => v.id);
  const idx = Number(event.key) - 1;
  if (idx >= 0 && idx < order.length) location.hash = `#/${order[idx]}`;
});

/* ---------- 全局状态呈现（顶栏 / 页脚 / 连接点） ---------- */

store.on(['status'], (status) => {
  if (!status) return;
  $('project-path').textContent = status.project_root || '—';
  // 桌面壳：标题栏与操作系统窗口标题跟着项目走（就地切项目后窗口名不变旧）
  {
    const name = (status.project_root || '').split(/[\\/]/).filter(Boolean).pop();
    if (name) {
      const t = $('titlebar-title');
      if (t) t.textContent = name;
      window.pywebview?.api?.set_title?.(name);
    }
  }
  // 顶栏放"接下来该看什么"：翻译进度与待办。口径与路径图一致 —— 按**单元**
  // 算（by_status），不是按译文记录算：一个单元多条槽位会翻好几倍。
  // 引擎 / 目标语言 / 版本这类一次配好就不变的，在设置页与侧栏脚注里都有。
  const g = status.graph || {};
  const bs = g.by_status || {};
  const done = bs.usable || 0;
  const review = bs.needs_review || 0;
  const translatable = done + review + (bs.untranslated || 0);
  const pct = translatable ? Math.round((done / translatable) * 100) : null;
  const r = status.resources || {};
  $('badges').innerHTML = [
    translatable ? `<span class="badge">翻译进度 <b>${num(done)}/${num(translatable)}（${pct}%）</b></span>` : '',
    review ? `<span class="badge">待复核 <b>${num(review)}</b></span>` : '',
    r.candidates ? `<span class="badge">待审候选 <b>${num(r.candidates)}</b></span>` : '',
    status.patches?.length ? `<span class="badge">补丁包 <b>${num(status.patches.length)}</b></span>` : '',
  ].join('');
  $('side-version').textContent = `gametrans v${status.tool_version ?? ''}`;
  const t = status.translations || {};
  $('footer-status').textContent =
    `${status.engine || '—'} · ${status.target_language || '—'} · ` +
    `${g.present ? '已提取' : '未提取'} · ${t.present ? '已翻译' : '未翻译'} · 更新于 ${new Date().toLocaleTimeString('zh-CN')}`;
});

store.on(['running'], (running) => {
  if (!$('conn-dot').classList.contains('bad')) {
    $('conn-dot').classList.toggle('busy', !!running);
    $('conn-text').textContent = running ? '有动静' : '在线';
  }
});

/* ---------- 错误横幅 ---------- */

let hadError = false;

function showError(err) {
  hadError = true;
  const banner = $('banner');
  banner.hidden = false;
  banner.innerHTML = errBanner(err);
  $('main').style.opacity = '0.35';
  $('footer-status').textContent = `无法读取项目状态（HTTP ${err.status ?? '—'}）`;
  $('conn-dot').classList.add('bad');
  $('conn-dot').classList.remove('busy');
  $('conn-text').textContent = '离线';
}

function clearError() {
  if (!hadError) return;
  hadError = false;
  $('banner').hidden = true;
  $('main').style.opacity = '1';
  $('conn-dot').classList.remove('bad');
  $('conn-text').textContent = '在线';
}

/* ---------- 数据拉取 ---------- */

const showAll = $('show-all');

async function safe(promise, slice) {
  try {
    store.set(slice, await promise);
  } catch (err) {
    if (err.status !== 404) console.warn(`[${slice}]`, err.message);
  }
}

const fetchers = {
  views: () => safe(api('/api/views' + (showAll.checked ? '?all=1' : '')), 'views'),
  graph: async () => {
    try {
      // 用户视图只发生效边（谁等谁）；agent 视图（all=1）给原始底账边
      const body = await api('/api/graph?limit=500' + (showAll.checked ? '&all=1' : ''));
      store.set('graph', body);
      if (body?.edges?.length) {
        const caps = store.get('caps') || {};
        if (!caps.graph_edges) store.set('caps', { ...caps, graph_edges: true });
      }
    } catch (err) {
      console.warn('[graph]', err.message);
    }
  },
  translations: () => safe(api('/api/translations?limit=500'), 'translations'),
  reports: () => safe(api('/api/reports?limit=50'), 'reports'),
  resources: () => {
    // 术语书不在这里拉：它由资源页自己刷新（/api/termbook，含待审更正），
    // 那是这一页的主数据，跟着轮询走会白拉好几遍
    safe(api('/api/style'), 'style');
  },
  settings: () => safe(api('/api/config'), 'config'),
  operations: () => safe(api('/api/operations'), 'operations'),
  // 工作台：台账列表只装摘要（正文在详情接口），模板与图结构各自一份
  exchanges: () => safe(api('/api/exchanges?limit=50'), 'exchanges'),
  templates: () => safe(api('/api/prompt-templates'), 'templates'),
};

const VIEW_FETCHES = {
  overview: ['views', 'reports'],
  graph: ['graph', 'translations'],
  translations: ['graph'],
  resources: ['resources', 'operations'],
  workbench: ['exchanges', 'templates'],
  settings: ['settings', 'operations'],
};

/* 隔几轮才刷的重数据；不在表里的意味着 force 才拉 */
const VIEW_PERIODS = {
  views: 2, graph: 2, translations: 3, reports: 5, resources: 4, settings: 8,
  exchanges: 5, templates: 8,
};

function fetchForView(viewId, force) {
  for (const name of VIEW_FETCHES[viewId] || []) {
    if (force || !VIEW_PERIODS[name] || tickNo % VIEW_PERIODS[name] === 0) {
      fetchers[name]();
    }
  }
}

/* ---------- 自适应轮询 ---------- */

let timer = 0;
let tickNo = 0;
let inFlight = false;
let lastChange = 0;
let lastStatusSig = '';

async function tick() {
  if (inFlight || document.hidden) {
    schedule();
    return;
  }
  inFlight = true;
  try {
    const status = await api('/api/status');
    const s = sig(status);
    if (lastStatusSig === '') {
      // 首轮只记基线：打开面板那一刻不算"有动静"，
      // 否则每次打开都会先谎报 12 秒的"进行中"
      lastStatusSig = s;
    } else if (s !== lastStatusSig) {
      lastStatusSig = s;
      lastChange = Date.now();
    }
    store.set('status', status);
    clearError();
    fetchForView(currentViewId(), false);
  } catch (err) {
    showError(err);
  } finally {
    inFlight = false;
    tickNo += 1;
  }
  schedule();
}

function schedule() {
  const running = Date.now() - lastChange < 12000;
  store.set('running', running);
  clearTimeout(timer);
  timer = setTimeout(tick, running ? 2000 : 8000);
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    clearTimeout(timer);
    tick(); // 回到前台立刻补一轮
  }
});

/* ---------- 装配 ---------- */

function mount() {
  armScrollbar();
  // 视图渲染：订阅各自的 slice，数据变化且视图处于激活态才重渲染
  for (const view of VIEWS) {
    view.mount?.();
    store.on(view.slices, () => {
      if (currentViewId() === view.id) view.render();
    });
  }

  applyTheme(document.documentElement.dataset.pref || 'system');

  // 桌面壳（?shell=1）里主题跟壳走：壳在每次页面加载后注入，设置页的档位组收起
  if (new URLSearchParams(location.search).has('shell')) {
    document.getElementById('ui-theme')?.remove();
  }

  // 主题：设置页「界面」卡里的三个档位按钮（跟随系统 / 日间 / 夜间）
  document.querySelectorAll('#ui-theme [data-theme-pref]').forEach((btn) => {
    btn.addEventListener('click', () => applyTheme(btn.dataset.themePref));
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if ((document.documentElement.dataset.pref || 'system') === 'system') applyTheme('system');
  });

  // agent 视图：除了动态页的数据口径（views?all=1），也管各页里给 agent 看
  // 的辅助信息 —— 挂 .agent-only 的块只在打开时显示。
  // 首次打开弹页内探窗解释；勾了「下次不再提醒」就不再弹（localStorage 记住）。
  const syncAgentView = () => {
    document.documentElement.dataset.agentView = showAll.checked ? '1' : '0';
  };
  let agentTipSkip = false;
  try {
    agentTipSkip = localStorage.getItem('gt-agent-view-tip') === 'skip';
  } catch (_) { /* 存不了就每次都提醒 */ }
  const closeAgentTip = (accepted) => {
    if ($('agent-view-tip-skip').checked) {
      try { localStorage.setItem('gt-agent-view-tip', 'skip'); } catch (_) {}
    }
    $('agent-view-tip').hidden = true;
    if (!accepted) {
      showAll.checked = false;
      syncAgentView();
      fetchers.views();
    }
  };
  syncAgentView();
  showAll.addEventListener('change', () => {
    syncAgentView();
    fetchers.views();
    fetchers.graph(); // 边的口径跟着开关换：生效边 / 底账
    if (showAll.checked && !agentTipSkip) $('agent-view-tip').hidden = false;
  });
  $('agent-view-tip-ok').addEventListener('click', () => closeAgentTip(true));
  $('agent-view-tip-cancel').addEventListener('click', () => closeAgentTip(false));

  applyRoute();

  // 新手教程：首访自动放一遍（localStorage.gt-tutorial-done 记账），
  // 设置页「界面」卡的「重看新手教程」按钮也在 tutorial.js 里接
  mountTutorial();
  maybeAutoStart();

  // 能力探测：决定按能力显隐的按钮（编辑 / 报告 / 事件流…）
  probeCapabilities().then((caps) => store.set('caps', caps));

  tick();
}

mount();
