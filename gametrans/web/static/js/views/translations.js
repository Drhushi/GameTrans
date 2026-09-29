/* 03 译文：**单元工作单** —— 一行一场戏（一个单元），点一行展开单元现场。
 *
 * 为什么重做：旧的收件箱是"一个单元一句话"年代的槽位级清单，一场几十句的
 * 现在既看不过来，也和路径图抽屉里的「单元现场」重复。这一页现在是单元级：
 * 按状态过滤、搜索，点开就是逐句原文/译文 + 碰过它的请求 —— 与路径图抽屉
 * 同一个部件（unitdetail.js），改哪句、存哪里都是同一条生产路径。
 */

import { esc, num, $ } from '../util.js';
import { patch, statusTag } from '../render.js';
import { get, set } from '../store.js';
import { renderUnitSite } from '../unitdetail.js';
import { regionRank } from '../graphcanvas.js';
import { apiPost, api } from '../api.js';

let statusFilter = 'all';
let filterText = '';
let openId = null;
let pendingScroll = false;

/** 单元按**剧情顺序**排：区域次序用路径图的拓扑序（与画布同一套排序），
 * 同区域内按路径排 —— 工作单读起来是"这本书从头干到尾"。 */
function nodes() {
  const graph = get('graph');
  const ns = (graph?.nodes || []).filter((n) => n.unit_id || n.path);
  const rank = regionRank(ns, graph?.edges || []);
  const key = (n) => rank.get(n.region || '') ?? Number.MAX_SAFE_INTEGER;
  const byRank = (a, b) => key(a) - key(b)
    || String(a.path || '').localeCompare(String(b.path || ''));
  // 系统文本（无章）排最后，与路径图的章带一致：工作单读起来是"剧情从头到尾，附录在最后"
  const story = ns.filter((n) => n.chapter).sort(byRank);
  const system = ns.filter((n) => !n.chapter).sort(byRank);
  return [...story, ...system];
}

function unitStatus(n) {
  return n.status === 'ok' ? 'usable' : n.status || 'untranslated';
}

function visibleNodes() {
  const q = filterText.toLowerCase();
  return nodes().filter((n) => {
    if (statusFilter !== 'all' && unitStatus(n) !== statusFilter) return false;
    if (!q) return true;
    return `${n.title || ''} ${n.source || ''} ${n.path || ''} ${n.speaker || ''}`
      .toLowerCase()
      .includes(q);
  });
}

function truncate(text, limit = 42) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  if (!flat) return '';
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

function renderChips() {
  const body = get('graph');
  const by = body?.by_status || {};
  const defs = [
    ['all', '全部', nodes().length],
    ['usable', '可用', by.usable ?? 0],
    ['needs_review', '待复核', by.needs_review ?? 0],
    ['untranslated', '未翻译', by.untranslated ?? 0],
  ];
  patch($('translation-chips'), defs
    .filter(([, label, count]) => label === '全部' || count > 0)
    .map(([value, label, count]) =>
      `<span class="chip click ${statusFilter === value ? 'on' : ''}" data-status="${value}">${label} <b>${num(count)}</b></span>`)
    .join(''));
  $('translations-ratio').textContent = body?.present
    ? `${num(nodes().length)} 场 · 可用 ${num(by.usable ?? 0)}`
    : '';
}

function renderList() {
  const body = get('graph');
  if (!body?.present || !nodes().length) {
    patch($('unit-list'), '<p class="empty">尚未提取。运行 <code>gametrans scan</code> 后这里会出现工作单。</p>');
    return;
  }
  const rows = visibleNodes().map((n) => {
    const id = n.unit_id || n.path;
    const open = openId === id;
    const title = n.title || truncate(n.source) || n.path;
    const sub = [n.speaker, n.weight?.char_count ? `${num(n.weight.char_count)} 字` : '']
      .filter(Boolean).join(' · ');
    return `<div class="inbox-row ${open ? 'open loading' : ''}" data-uid="${esc(id)}">
      <div class="inbox-main">
        <span class="mono">${statusTag(unitStatus(n))}</span>
        ${n.term_tags_pending
          ? '<span class="tb-pending" title="这条译文里还有没定译的名字（原文里是 ⟦写法⟧）：文本已翻好，名字等你在术语书里定，写回时按术语书渲染">待定译</span>'
          : ''}
        <span class="inbox-src">${esc(title)}</span>
        <span class="inbox-path">${esc([sub, n.path].filter(Boolean).join(' · '))}</span>
      </div>
      ${open ? '<div class="inbox-detail" data-detail="' + esc(id) + '" hidden></div>' : ''}
    </div>`;
  });
  const more = (body.matched ?? nodes().length) > nodes().length
    ? '<p class="hint">图上还有更多单元，用过滤或去路径图缩小范围。</p>'
    : '';
  patch($('unit-list'), rows.join('') + more);

  if (pendingScroll) {
    pendingScroll = false;
    const open = document.querySelector('#unit-list .inbox-row.open');
    open?.scrollIntoView({ block: 'center' });
  }

  // 展开的行拉单元现场（与路径图抽屉同一个部件、同一条保存路径）。
  // 展开只发生一次：块到手前保持 hidden（行头有就地"读取中"提示），渲染完才展开，
  // 不再是"先弹一个占位块、内容到了又弹一次"。
  document.querySelectorAll('#unit-list .inbox-detail').forEach(async (el) => {
    if (el.dataset.wired) return;
    el.dataset.wired = '1';
    const node = nodes().find((n) => (n.unit_id || n.path) === el.dataset.detail);
    try {
      if (!node?.node_id) throw new Error('找不到这个单元');
      const payload = await api(`/api/units/${encodeURIComponent(node.node_id)}`);
      if (openId !== el.dataset.detail || !el.isConnected) return; // 等待期间已被收起/换行
      renderUnitSite(el, payload, {
        editable: !!get('caps')?.translation_edit,
        onSave: saveTranslation,
        onJumpToGraph: (unitId) => {
          set('focusUnit', unitId || node.unit_id);
          location.hash = '#/graph';
        },
      });
      el.hidden = false;
      el.closest('.inbox-row')?.classList.remove('loading');
    } catch (err) {
      if (openId !== el.dataset.detail || !el.isConnected) return;
      el.hidden = false;
      el.closest('.inbox-row')?.classList.remove('loading');
      el.innerHTML = `<p class="hint">打不开单元现场：${esc(err.message)}</p>`;
    }
  });
}

async function saveTranslation(unitId, target) {
  const body = await apiPost(`/api/translations/${encodeURIComponent(unitId)}`, { target });
  try {
    const fresh = await api('/api/graph?limit=500');
    set('graph', fresh);
  } catch (_) { /* 下一轮轮询补上 */ }
  return body.artifact;
}

export const view = {
  id: 'translations',
  slices: ['graph', 'caps'],

  activate() {
    // 从路径图「去译文页逐句改」跳过来：直接展开那个单元并滚到可见处
    const focus = get('focusUnit');
    if (focus) {
      openId = focus;
      set('focusUnit', null);
      pendingScroll = true;
    }
  },

  mount() {
    $('translation-chips').addEventListener('click', (ev) => {
      const chip = ev.target.closest('[data-status]');
      if (!chip) return;
      statusFilter = chip.dataset.status;
      openId = null;
      renderChips();
      renderList();
    });
    $('translation-filter').addEventListener('input', (ev) => {
      filterText = ev.target.value.trim();
      renderList();
    });
    $('unit-list').addEventListener('click', (ev) => {
      if (ev.target.closest('.inbox-detail')) return; // 单元现场里点按钮不折行
      const row = ev.target.closest('.inbox-row');
      if (!row) return;
      const uid = row.dataset.uid;
      openId = openId === uid ? null : uid;
      renderList();
    });
  },

  render() {
    renderChips();
    renderList();
  },
};
