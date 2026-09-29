/* 02 路径图：画布（拖拽 / 缩放 / 点选）为主，列表兜底。
 *
 * 有依赖边（后端 R4）就按边分层画 DAG；没有边就用节点自带的
 * parent/children 铺成树 —— 反正提取结果本来就带结构，画布今天就可用。
 *
 * **节点上写什么**：写这一场能读的剧情文字（第一句／场景摘要），代码侧的
 * 逻辑路径只进 tooltip —— 用户看图是要确认"内容被理清了"，不是看 label 名。
 * 布局把长链折成网格、章画成带子，见 `graphcanvas.js`。
 */

import { esc, num, $ } from '../util.js';
import { patch, table, tokenPill, statusTag } from '../render.js';
import { get, set } from '../store.js';
import { createGraphCanvas } from '../graphcanvas.js';
import { openUnitSite } from '../drawer.js';
import { api } from '../api.js';

let canvas = null;
let mode = 'canvas';
let filterText = '';
// 筛选三维度（全部点选，见 renderLegend / 章带标题 / 下拉）：
let statusSet = new Set();     // 点图例 chip 切换；空 = 全部状态
let chapterFilter = null;      // 点章带标题只看该章；再点恢复
let termbookWritings = null;   // 术语书写法缓存（筛选候选用，一次拉取）
let filterOptions = [];        // 组合框候选：说话人 + 术语写法
let legendCtx = { hasEdges: false, dropped: 0 };

function statusByIdFrom(translations) {
  const map = new Map();
  for (const r of translations?.sample || []) {
    if (r.unit_id) map.set(r.unit_id, r.status);
    if (r.path) map.set(r.path, r.status);
  }
  return map;
}

function renderLegend() {
  // 状态 chip 即筛选：点亮/熄灭对应状态的场卡（无选中 = 全部状态）。
  // 少画的边要说出来：不然"算法丢了边"和"这张图本来就没边"长得一模一样
  const hint = legendCtx.hasEdges
    ? (legendCtx.dropped ? ` · ${num(legendCtx.dropped)} 条边没画出来` : '')
    : ' · 本次提取没有依赖边';
  const chips = [
    ['usable', '可用', 'var(--accent)'],
    ['needs_review', '待复核', 'var(--amber)'],
    ['untranslated', '未翻译', 'var(--cyan)'],
  ]
    .map(
      ([key, label, color]) =>
        `<span class="legend-chip${statusSet.has(key) ? ' on' : ''}" data-status="${key}"><i style="background:${color}"></i>${label}</span>`
    )
    .join('');
  patch($('graph-legend'), `${chips}<span class="hint">${hint}</span>`);
}

function renderCanvas() {
  const graph = get('graph');
  const statusMap = statusByIdFrom(get('translations'));
  if (!graph?.present || !graph.nodes?.length) {
    patch($('graph-canvas'), '<p class="empty" style="margin:40px auto;max-width:520px;">尚未提取。运行 <code>gametrans scan</code> 后这里会出现带权路径图。</p>');
    legendCtx = { hasEdges: false, dropped: 0 };
    renderLegend();
    return;
  }
  if (!canvas) {
    canvas = createGraphCanvas($('graph-canvas'), {
      onSelect: openNode,
      onChapterPick: (ch) => {
        // 点章带标题 = 只看该章；再点恢复全部
        chapterFilter = chapterFilter === ch ? null : ch;
        applyFilter();
      },
    });
  }
  canvas.setData(graph.nodes, graph.edges || null, statusMap);
  applyFilter();
  buildFilterOptions(graph);
  ensureTermbook().then(() => buildFilterOptions(graph)); // 术语书晚到再刷一次候选
  // 从译文页跳回来：聚焦到那个单元所在的场（进场档、居中、高亮）
  const focusUnit = get('focusUnit');
  if (canvas && focusUnit) {
    set('focusUnit', null);
    const node = (graph.nodes || []).find((n) => n.unit_id === focusUnit || n.path === focusUnit);
    if (node?.region) canvas.focusRegion(node.region);
  }
  const shown = graph.nodes.length;
  const total = graph.total ?? shown;
  $('graph-meta').textContent =
    `共 ${num(total)} 节点 · 可译 ${num(graph.translatable ?? '—')}` +
    (shown < total ? ` · 画布显示前 ${num(shown)}（可搜索缩小范围）` : '');
  legendCtx = {
    hasEdges: !!(graph.edges && graph.edges.length),
    dropped: canvas.stats?.().droppedEdges || 0,
  };
  renderLegend();
}

/** 筛选组合框的候选：说话人（按出现次数）+ 术语书里的写法，去重合并。
 * 术语书一次拉取后缓存；拉不到（还没建 / 接口异常）就只有说话人。 */
function buildFilterOptions(graph) {
  const speakers = new Map();
  for (const n of graph.nodes || []) {
    if (n.speaker) speakers.set(n.speaker, (speakers.get(n.speaker) || 0) + 1);
  }
  const seen = new Set(speakers.keys());
  filterOptions = [
    ...[...speakers.entries()].sort((a, b) => b[1] - a[1]).map(([s]) => s),
    ...(termbookWritings || []).filter((w) => !seen.has(w)),
  ];
  renderSuggestions();
}

async function ensureTermbook() {
  if (termbookWritings) return termbookWritings;
  try {
    const body = await api('/api/termbook');
    termbookWritings = [
      ...new Set(
        (body.entries || [])
          .flatMap((e) => (e.key || []).map((k) => k.writing))
          .filter(Boolean)
      ),
    ];
  } catch (_) {
    termbookWritings = [];
  }
  return termbookWritings;
}

/** 联想面板：随输入收窄（子串匹配），点选即填入。 */
function renderSuggestions() {
  const box = $('graph-suggest');
  if (!box || box.hidden) return;
  const q = filterText.toLowerCase();
  const items = (q ? filterOptions.filter((o) => o.toLowerCase().includes(q)) : filterOptions).slice(0, 30);
  box.innerHTML = items.length
    ? items.map((o) => `<div data-value="${esc(o)}">${esc(o)}</div>`).join('')
    : '<div class="hint" style="padding:6px 10px;">没有匹配的人物 / 术语 —— 直接回车按关键词过滤</div>';
}

function showSuggestions() {
  const box = $('graph-suggest');
  if (!box) return;
  box.hidden = false;
  renderSuggestions();
}

function hideSuggestions() {
  const box = $('graph-suggest');
  if (box) box.hidden = true;
}

function applyFilter() {
  if (!canvas) return;
  canvas.setFilter({ q: filterText, statuses: statusSet, chapter: chapterFilter });
}

/** 事件卡 → 抽屉的 kv 行。摘要是带【字段】标记的整段文本（场次 / 地点时间 /
 * 出场人物 / 发生了什么…），按标记拆开摆，与抽屉里"这一次的账"那块同款排版；
 * 没带标记的旧格式整段原样放，不硬拆。 */
function eventCardHtml(node) {
  const title = node.title || '';
  const raw = String(node.summary || '');
  if (!title && !raw) return '';
  const parts = raw.split(/【([^】]+)】/);
  let body;
  if (parts.length >= 3) {
    const rows = [];
    if (parts[0].trim()) rows.push(['概要', parts[0].trim()]);
    for (let i = 1; i + 1 < parts.length; i += 2) {
      rows.push([parts[i].trim(), parts[i + 1].trim()]);
    }
    body = `<dl class="kv">${rows
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v) || '<span class="hint">—</span>'}</dd>`)
      .join('')}</dl>`;
  } else {
    body = `<pre class="calltext">${esc(raw || '（这一场还没有概要）')}</pre>`;
  }
  return `<div class="dsec scene-summary">
    <h4>${esc(title || '本场梗概')}<span class="hint">模型生成 · 供参考</span></h4>
    ${body}
  </div>`;
}

async function openNode(node) {
  // 单元现场由后端拼（逐句原文/译文/状态 + 碰过它的请求）。
  // 抽屉里**不再改句子**：改译文去译文页的工作单（那边按场排队、铅笔就地改），
  // 这里给跳转按钮把焦点单元带过去。
  const root = document.getElementById('drawer-root');
  // 占位抽屉不带入场动画（drawer-ghost）：openUnitSite 拿到数据后会整个重建
  // 一遍，两次都播 drawer-in 就是"抽屉跳出来两下"——只让最终那次入场。
  root.innerHTML = `<div class="drawer-mask"></div><aside class="drawer drawer-ghost">
    <div class="drawer-head"><h3>${esc(node.path || '单元')}</h3>
    <button class="x" type="button" onclick="this.closest('.drawer').parentElement.innerHTML=''">ESC</button></div>
    <div class="drawer-body"><p class="hint">读取这个单元的每一句与碰过它的请求…</p></div></aside>`;
  let payload = null;
  try {
    payload = await api(`/api/units/${encodeURIComponent(node.node_id)}`);
  } catch (err) {
    document.getElementById('drawer-root').innerHTML = '';
    return;
  }
  openUnitSite(payload, {
    editable: false,
    onJumpToWorklist: (unitId) => {
      set('focusUnit', unitId || node.unit_id);
      location.hash = '#/translations';
    },
  });
  // 场摘要（模型生成的标题 + 事件卡）挂在抽屉最上面：它是"这场戏讲了什么"的
  // 第一眼答案，比逐句清单先读。事件卡按字段摊成 kv 行，与下面"逐句""请求"
  // 是同一套排版惯例，只是排在最前。
  const body = root.querySelector('.drawer-body');
  if (body && !body.querySelector('.scene-summary')) {
    const card = eventCardHtml(node);
    if (card) body.insertAdjacentHTML('afterbegin', card);
  }
}

function renderTable() {
  const graph = get('graph');
  if (!graph?.present || !graph.nodes?.length) {
    table($('graph-table'), [], []);
    return;
  }

  const q = filterText.toLowerCase();
  const nodes = graph.nodes.filter((n) => {
    const textOk =
      !q || `${n.kind} ${n.path} ${n.source}`.toLowerCase().includes(q);
    const statusOk = !statusSet.size || statusSet.has(n.status);
    const chapterOk = !chapterFilter || (n.chapter || '') === chapterFilter;
    return textOk && statusOk && chapterOk;
  });
  const statusMap = statusByIdFrom(get('translations'));
  const rows = nodes.map((n) => {
    const status = statusMap.get(n.unit_id || n.path);
    return `<tr class="rowlink" data-node="${esc(n.node_id)}">
      <td>${tokenPill(n.weight?.cost)}</td>
      <td>${status ? statusTag(status) : '<span class="hint">—</span>'}</td>
      <td class="mono">${esc(n.kind)}</td>
      <td class="mono truncate" title="${esc(n.path)}">${esc(n.path)}</td>
      <td class="src truncate" title="${esc(n.source || '')}">${esc(n.source || '—')}</td>
      <td class="num">${num(n.weight?.char_count)}</td>
      <td class="num">${num(n.weight?.occurrences)}</td>
    </tr>`;
  });
  table($('graph-table'), ['开销', '状态', '类型', '逻辑路径', '原文', '字数', '复现'], rows);
}

async function apiGetTranslations() {
  try {
    const body = await api('/api/translations?limit=500');
    set('translations', body);
  } catch (_) { /* 轮询会重试 */ }
}

export const view = {
  id: 'graph',
  slices: ['graph', 'translations'],

  mount() {
    $('graph-filter').addEventListener('input', (ev) => {
      filterText = ev.target.value.trim();
      if (mode === 'canvas') applyFilter();
      else renderTable();
      showSuggestions(); // 打字时联想同步收窄
    });
    $('graph-filter').addEventListener('focus', showSuggestions);
    $('graph-filter').addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') hideSuggestions();
    });
    $('graph-suggest').addEventListener('pointerdown', (ev) => {
      const item = ev.target.closest('[data-value]');
      if (!item) return;
      ev.preventDefault(); // 别让输入框失焦
      filterText = item.dataset.value;
      $('graph-filter').value = filterText;
      if (mode === 'canvas') applyFilter();
      else renderTable();
      hideSuggestions();
    });
    document.addEventListener('pointerdown', (ev) => {
      if (!ev.target.closest('#graph-suggest') && ev.target.id !== 'graph-filter') {
        hideSuggestions();
      }
    });
    $('graph-legend').addEventListener('click', (ev) => {
      const chip = ev.target.closest('[data-status]');
      if (!chip) return;
      const key = chip.dataset.status;
      if (statusSet.has(key)) statusSet.delete(key);
      else statusSet.add(key);
      renderLegend();
      if (mode === 'canvas') applyFilter();
      else renderTable();
    });
    $('graph-mode-canvas').addEventListener('click', () => setMode('canvas'));
    $('graph-mode-table').addEventListener('click', () => setMode('table'));
    $('graph-zoom-in').addEventListener('click', () => canvas?.zoomIn());
    $('graph-zoom-out').addEventListener('click', () => canvas?.zoomOut());
    $('graph-zoom-fit').addEventListener('click', () => canvas?.fit());
    $('graph-reset').addEventListener('click', () => canvas?.reset());
    $('graph-table-box').addEventListener('click', (ev) => {
      const row = ev.target.closest('tr[data-node]');
      if (!row) return;
      const graph = get('graph');
      const node = (graph?.nodes || []).find((n) => n.node_id === row.dataset.node);
      if (node) openNode(node);
    });
  },

  activate() {
    setMode(mode);
  },

  render() {
    if (mode === 'canvas') renderCanvas();
    else renderTable();
  },
};

function setMode(next) {
  mode = next;
  $('graph-mode-canvas').classList.toggle('on', mode === 'canvas');
  $('graph-mode-table').classList.toggle('on', mode === 'table');
  $('graph-canvas-box').hidden = mode !== 'canvas';
  $('graph-table-box').hidden = mode !== 'table';
  if (mode === 'canvas') renderCanvas();
  else renderTable();
}
