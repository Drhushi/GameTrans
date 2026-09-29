/* 04 资源：术语书 + 待审更正 + 风格指南。
 *
 * 术语书是**一份文件、一行一个实体**，照 SillyTavern 的世界书条目裁成五栏：
 *   - `key`：一组写法，**每个写法带自己的译名**（`Eve Herschel` 命中时用它自己的
 *     `伊芙·赫歇尔` 渲染，不是 `伊芙`）。任一写法命中即触发这一行；`key[0]` 只是身份。
 *   - `profile`：追加式事实列表（一个元素一条事实，注入时用「；」连成一行）。
 *   - `constant`：蓝灯 = 没有写法命中也永远注入（世界观 / 风格类）。
 *   - `order`：注入顺序，数值大的更靠后（更靠近提示词末尾）；蓝灯行先注入。
 *   - `position`：terms（进【术语书】段）/ tail（排在该段最后）。
 *
 * **追加免审、更正要审**：新写法、空译名、新事实直接追加；改已有的译名或事实要先进
 * 「待审更正」那一小段，采用才替换。面板上的铅笔 = 人拍的板，直接生效（不进待审）。
 *
 * 交互约定（左右分栏：左清单、右详情）：
 *   - 左栏清单一行一个实体（摘要：身份 → 译名 + 徽标），点行换右栏详情；
 *   - chip 即筛选（与工作单同一套交互）：待定译 / 待复核点到只剩那一类；
 *     搜索框按写法 / 译名 / 设定收窄清单；
 *   - 右栏详情给全量：写法 → 译名、逐条设定、注入栏位、这一行排着的待审更正；
 *   - 「✎ 修改」= 详情里就地编辑，改到一半点了别处会提醒未保存；
 *   - 复选框多选（在清单上），选中出现批量删除条；全选只选筛出来的行；
 *   - "＋ 添加"才弹出添加表单，不常驻占版面；
 *   - "从摘要抽实体"从场摘要抽实体（幂等，连点不会重复产行）。
 *
 * 添加表单仍由操作注册表驱动（字段名 / 必填 / 默认值不写死，tests/test_panel_dom.py
 * 钉住防冲掉行为）。有未保存输入时整块冻结。
 */

import { esc, num, $ } from '../util.js';
import { patch, table, toast } from '../render.js';
import { get, set } from '../store.js';
import { apiPost, api } from '../api.js';

let contentDirty = false;

/* ---------- 添加表单（操作注册表驱动；收在「＋ 添加」后面） ---------- */

const ADD_FORMS = [
  {
    container: 'term-add', op: 'resource.term.add',
    label: '添加一条', done: '已添加。',
    core: ['key', 'profile', 'constant', 'order', 'position'],
  },
  {
    container: 'style-add', op: 'resource.style.add',
    label: '添加风格要求', done: '风格要求已添加。',
    core: ['aspect', 'value', 'scope'],
  },
];

function contentSignature(operations) {
  return ADD_FORMS.map((slot) => {
    const op = (operations || []).find((o) => o.name === slot.op);
    return op ? `${op.name}(${op.params.map((p) => p.name).join(',')})` : '-';
  }).join('|');
}

function formField(p) {
  const label = `${p.help || p.name}${p.required ? ' *' : ''}`;
  const required = p.required ? ' data-required="1"' : '';
  if (p.type === 'bool') {
    return `<label class="field check"><input type="checkbox" data-param="${esc(p.name)}"><span>${esc(label)}</span></label>`;
  }
  if (p.choices) {
    const options = p.choices
      .map((c) => `<option value="${esc(c)}" ${c === p.default ? 'selected' : ''}>${esc(c)}</option>`)
      .join('');
    return `<label class="field"><span>${esc(label)}</span>
      <select data-param="${esc(p.name)}"${required}>${options}</select></label>`;
  }
  const value = p.default == null ? '' : p.default;
  if (p.multiline || p.name === 'body' || p.name === 'key') {
    return `<label class="field wide-field"><span>${esc(label)}</span>
      <textarea data-param="${esc(p.name)}" rows="3" data-default="${esc(value)}"${required}>${esc(value)}</textarea></label>`;
  }
  return `<label class="field"><span>${esc(label)}</span>
    <input type="${p.type === 'int' ? 'number' : 'text'}" data-param="${esc(p.name)}" value="${esc(value)}" data-default="${esc(value)}"${required} spellcheck="false"></label>`;
}

function formHtml(slot, op) {
  const params = op.params.filter((p) => p.name !== 'project' && p.name !== 'workdir');
  const isFront = (p) => p.required || slot.core?.includes(p.name);
  const front = params.filter(isFront).map(formField).join('');
  const rest = params.filter((p) => !isFront(p));
  const extras = rest.length
    ? `<details class="more"><summary>更多选项</summary><div class="fields">${rest.map(formField).join('')}</div></details>`
    : '';
  return `<form class="content-form" data-tool="${esc(op.tool_name)}" data-name="${esc(op.name)}" data-label="${esc(slot.label)}" data-done="${esc(slot.done)}">
    <div class="fields">${front}</div>
    ${extras}
    <div class="form-actions">
      <button type="submit">${esc(slot.label)}</button>
    </div>
  </form>`;
}

function renderContentForms(operations) {
  const signature = contentSignature(operations);
  for (const slot of ADD_FORMS) {
    const container = $(slot.container);
    if (!container || contentDirty || container.dataset.signature === signature) continue;
    const op = (operations || []).find((o) => o.name === slot.op);
    patch(container, op ? formHtml(slot, op) : '');
    container.dataset.signature = signature;
  }
}

function panelResult(form) {
  const panel = form.closest('.panel');
  return panel?.querySelector('.op-result') || null;
}

function clearForm(form) {
  form.querySelectorAll('[data-param]').forEach((el) => {
    if (el.type === 'checkbox') el.checked = false;
    else el.value = el.dataset.default ?? '';
  });
}

async function submitContentForm(form) {
  const result = panelResult(form);
  const payload = {};
  const missing = [];
  form.querySelectorAll('[data-param]').forEach((el) => {
    el.closest('.field')?.classList.remove('invalid');
    const value = el.type === 'checkbox' ? (el.checked ? 'true' : '') : el.value.trim();
    if (value !== '') payload[el.dataset.param] = value;
    else if (el.dataset.required === '1') {
      el.closest('.field')?.classList.add('invalid');
      const label = el.closest('.field')?.querySelector(':scope > span')?.textContent || el.dataset.param;
      missing.push(label.replace(/\s*\*\s*$/, ''));
    }
  });
  if (missing.length) {
    if (result) result.textContent = `还有没填的必填项：${missing.join('、')}`;
    form.querySelector('.field.invalid input, .field.invalid textarea')?.focus();
    return;
  }
  const done = form.dataset.done || `${form.dataset.label}完成。`;
  if (result) result.textContent = '正在保存…';
  try {
    await apiPost(`/api/operations/${form.dataset.tool}`, payload);
    if (result) result.textContent = done;
    toast(done);
    contentDirty = false;
    clearForm(form);
    await refreshResources();
  } catch (error) {
    if (result) result.textContent = `失败：${error.message}`;
  }
}

async function refreshResources() {
  const results = await Promise.allSettled([
    api('/api/termbook'),
    api('/api/style'),
  ]);
  const keys = ['termbook', 'style'];
  results.forEach((r, i) => {
    if (r.status === 'fulfilled') set(keys[i], r.value);
  });
}

/* ---------- 术语书：一行一个实体（五栏） ---------- */

let addOpen = false;
let selection = new Set();   // 行身份（key[0].writing）
let editing = null;          // { key } 正在就地编辑的那一行
let termFilter = 'all';      // all | naming（有待定译写法）| pending（有待审更正）
let filterText = '';         // 清单搜索：写法 / 译名 / 设定
let detailKey = null;        // 右侧详情显示的那一行（空书时为 null）
let listScrollSig = '';      // 清单滚动手势的指纹：同筛同选才保滚动位置

function setResult(text) {
  const el = $('termbook-result');
  if (el) el.textContent = text;
}

function setPendingResult(text) {
  const el = $('term-pending-result');
  if (el) el.textContent = text;
}

/* 行身份：key[0].writing。它只是显示名 —— 命中与渲染都按整个 key 逐条来。 */
function identity(entry) {
  const key = entry?.key || [];
  return String(key[0]?.writing || '');
}

function checkbox(key) {
  return `<input type="checkbox" data-sel="${esc(key)}" ${selection.has(key) ? 'checked' : ''} title="选中以便批量删除">`;
}

function actionButton(act, label, data, cls = '') {
  const attrs = Object.entries(data || {})
    .map(([k, v]) => ` data-${k}="${esc(v)}"`)
    .join('');
  return `<button type="button" class="rowbtn ${cls}" data-act="${act}"${attrs} title="${esc(label)}">${esc(label)}</button>`;
}

/* 写法列表：**一个写法一行**，`写法 → 译名` 三列格子（.tb-writings）——
 * 同一实体的箭头与译名左右对齐，一行几个写法挤在一起时扫读很费劲。
 * 还没有译名的写法在翻译时是 `⟦写法⟧`（模型只保留标签、不起名字），
 * 所以这里要标出来：不标就和"填漏了"分不开。 */
function writingsHtml(entry) {
  return (entry.key || []).map((item) => (item.target
    ? `<span class="tb-src">${esc(item.writing)}</span><span class="tb-arrow">→</span><span class="tb-dst">${esc(item.target)}</span>`
    : `<span class="tb-src">${esc(item.writing)}</span><span class="tb-arrow">→</span><span class="tb-pending" title="原文里是 ⟦${esc(item.writing)}⟧：等人或 agent 定译名">待定译</span>`))
    .join('');
}

function factsHtml(entry) {
  const facts = entry.profile || [];
  if (!facts.length) return '';
  return `<div class="tb-body">${facts.map((fact) => `<div>${esc(fact)}</div>`).join('')}</div>`;
}

/* 栏位说明收进 agent 视图：绿灯 / order / position 是默认值时不逐行念一遍。
 * 蓝灯是行为差异（没命中也注入），用户视图也要看得见。 */
function flagsHtml(entry) {
  const bits = [entry.constant
    ? '<span class="tb-const" title="蓝灯：没有写法命中也每次注入（世界观 / 风格类）">蓝灯</span>'
    : '<span class="hint agent-only">绿灯 写法命中才注入</span>'];
  if (entry.position === 'tail') bits.push('<span class="hint">tail · 排本段最后</span>');
  if ((Number(entry.order) || 0) !== 100) {
    bits.push(`<span class="hint mono">order ${esc(entry.order)}</span>`);
  }
  return bits.join(' ');
}

/* 就地编辑：五栏都能改。key 与 profile 是**列表**，所以各占一个多行输入框
 * （key 一行一个写法，`写法 → 译名`；profile 一行一条事实）。
 * 保存走 `resource.term.add` —— 那是**人拍的板**，直接生效、不进待审队列。 */
function editRow(entry) {
  const key = identity(entry);
  const writings = (entry.key || [])
    .map((item) => (item.target ? `${item.writing} → ${item.target}` : item.writing))
    .join('\n');
  const facts = (entry.profile || []).join('\n');
  return `<div class="tb-row editing" data-editing="1">
    <label class="field wide-field"><span>写法（一行一个：<code>写法 → 译名</code>；只写写法就是还没定译名）</span>
      <textarea data-edit="key" rows="3" spellcheck="false">${esc(writings)}</textarea></label>
    <label class="field wide-field"><span>事实（一行一条）</span>
      <textarea data-edit="profile" rows="3" spellcheck="false">${esc(facts)}</textarea></label>
    <label class="field check"><input type="checkbox" data-edit="constant" ${entry.constant ? 'checked' : ''}><span>蓝灯（每次注入）</span></label>
    <label class="field"><span>order</span>
      <input type="number" data-edit="order" value="${esc(entry.order)}" spellcheck="false"></label>
    <label class="field"><span>position</span>
      <select data-edit="position">
        <option value="terms" ${entry.position === 'terms' ? 'selected' : ''}>terms</option>
        <option value="tail" ${entry.position === 'tail' ? 'selected' : ''}>tail</option>
      </select></label>
    ${actionButton('save', '保存', { key }, 'go')}
    ${actionButton('cancel', '取消', {})}
  </div>`;
}

/* 注入顺序就是列表顺序：position（terms 在前、tail 在最后）→ 蓝灯在前 → order 升序。
 * 与后端 `injection_sort_key` 同一条规则，所以屏幕上看到的顺序就是提示词里的顺序。 */
function injectionCompare(a, b) {
  const rank = (entry) => [
    entry.position === 'tail' ? 1 : 0,
    entry.constant ? 0 : 1,
    Number(entry.order) || 0,
    identity(entry),
  ];
  const left = rank(a);
  const right = rank(b);
  for (let i = 0; i < left.length; i += 1) {
    if (left[i] < right[i]) return -1;
    if (left[i] > right[i]) return 1;
  }
  return 0;
}

function sortedEntries() {
  return (get('termbook')?.entries || []).slice().sort(injectionCompare);
}

/* ---------- 筛选与搜索：chip 即筛选（与工作单同一套交互） ----------
 * 待定译 = 行里有还没定译名的写法（原文里是 ⟦写法⟧，等人定名）；
 * 待复核 = 行的写法排着待审更正（采用之前书里还是旧值）。
 * 搜索框只收窄**清单**，不影响 chip 的口径 —— 两层各管各的。 */
function entryHasNaming(entry) {
  return (entry.key || []).some((item) => !item.target);
}

function pendingWritingSet() {
  const out = new Set();
  for (const record of get('termbook')?.pending || []) {
    const writing = String(record.writing || '').trim();
    if (writing) out.add(writing);
  }
  return out;
}

function entryHasPending(entry, pendingSet) {
  return (entry.key || []).some((item) => pendingSet.has(String(item.writing || '').trim()));
}

function entryText(entry) {
  return (entry.key || []).map((item) => `${item.writing} ${item.target || ''}`)
    .concat(entry.profile || []).join(' ');
}

function searchedEntries() {
  const q = filterText.trim().toLowerCase();
  const all = sortedEntries();
  if (!q) return all;
  return all.filter((entry) => entryText(entry).toLowerCase().includes(q));
}

function visibleEntries() {
  const searched = searchedEntries();
  if (termFilter === 'all') return searched;
  const pendingSet = pendingWritingSet();
  return searched.filter((entry) => (termFilter === 'naming'
    ? entryHasNaming(entry)
    : entryHasPending(entry, pendingSet)));
}

function renderTermChips(searched) {
  const naming = searched.filter(entryHasNaming).length;
  const pendingSet = pendingWritingSet();
  const pending = searched.filter((entry) => entryHasPending(entry, pendingSet)).length;
  // 筛的那一类清零了就退回全部，别停在一个空筛选上
  if (termFilter === 'naming' && !naming) termFilter = 'all';
  if (termFilter === 'pending' && !pending) termFilter = 'all';
  const defs = [['all', '全部', searched.length]];
  if (naming) defs.push(['naming', '待定译', naming]);
  if (pending) defs.push(['pending', '待复核', pending]);
  patch($('termbook-chips'), defs.map(([value, label, n]) =>
    `<span class="chip click ${termFilter === value ? 'on' : ''}" data-term-filter="${value}">${label} <b>${num(n)}</b></span>`)
    .join(''));
}

/* ---------- 左栏：清单。一行一个实体，可扫读、可勾选批量删 ---------- */

function listRow(entry, pendingSet) {
  const key = identity(entry);
  const ps = pendingSet || pendingWritingSet();
  const pendingCount = (entry.key || [])
    .filter((item) => ps.has(String(item.writing || '').trim())).length;
  // 摘要行用**第一个有译名的写法**；全行都没定译名才亮「待定译」
  const named = (entry.key || []).find((item) => item.target);
  const badges = [
    pendingCount ? `<span class="tb-mini warn" title="这一行排着 ${pendingCount} 条待审更正">⚠${pendingCount}</span>` : '',
    entry.constant ? '<span class="tb-mini const" title="蓝灯：没有写法命中也每次注入">蓝灯</span>' : '',
  ].filter(Boolean).join('');
  return `<div class="tb-row tb-item ${detailKey === key ? 'on' : ''}" data-detail="${esc(key)}">
    ${checkbox(key)}
    <span class="tb-src" title="${esc(key)}">${esc(key)}</span>
    <span class="tb-arrow">→</span>
    ${named
      ? `<span class="tb-dst" title="${esc(named.target)}">${esc(named.target)}</span>`
      : '<span class="tb-pending" title="这一行的写法都还没定译名，原文里是 ⟦…⟧">待定译</span>'}
    ${badges ? `<span class="tb-badges">${badges}</span>` : ''}
  </div>`;
}

/* ---------- 右栏：详情。选中实体的全部内容 + 动作 ---------- */

function renderTermDetail(entry) {
  const key = identity(entry);
  if (editing?.key === key) {
    return `<div class="tb-detail-head"><h3>${esc(key)}</h3></div>
      <div class="tb-detail-body">${editRow(entry)}</div>`;
  }
  const actions = actionButton('edit', '✎ 修改', { key })
    + actionButton('del', '🗑 删除', { key }, 'danger');
  // 只把**挂在这一行**上的待审更正搬进详情（整份队列仍在下方「待审更正」面板）
  const ps = pendingWritingSet();
  const pendings = (get('termbook')?.pending || [])
    .filter((record) => (entry.key || [])
      .some((item) => String(item.writing || '').trim() === String(record.writing || '').trim()));
  return `
    <div class="tb-detail-head">
      <h3 title="${esc(key)}">${esc(key)}</h3>
      <span class="tb-actions">${actions}</span>
    </div>
    <div class="tb-detail-body">
      <div class="dsec"><h4>写法 → 译名</h4><div class="tb-writings">${writingsHtml(entry)}</div></div>
      <div class="dsec"><h4>设定</h4>${factsHtml(entry) || '<p class="hint">还没有设定 —— 没有译名也没有设定的行不会注入。</p>'}</div>
      ${flagsHtml(entry)
        ? `<div class="dsec dsec-agent-if-empty"><h4>注入</h4><div class="tb-flags">${flagsHtml(entry)}</div></div>`
        : ''}
      ${pendings.length ? `<div class="dsec"><h4>这一行的待审更正（${pendings.length}）</h4>${pendings.map(pendingRow).join('')}</div>` : ''}
    </div>
  `;
}

function renderTermbook() {
  renderBulkBar();
  const searched = searchedEntries();
  renderTermChips(searched);
  const entries = visibleEntries();
  // 详情落在第一行：筛出来的结果里没有它（行被删/被筛掉）就换人，右栏永远有内容
  if (!entries.some((entry) => identity(entry) === detailKey)) {
    detailKey = entries.length ? identity(entries[0]) : null;
  }
  const current = entries.find((entry) => identity(entry) === detailKey) || null;
  // 全选框自己的勾选态也要跟住：列表重画后它是新建的节点，不绑就会"弹回"。
  // 全选只选**筛出来**的行 —— 看不见的行不该被批量删掉。
  const allChecked = entries.length > 0 && entries.every((e) => selection.has(identity(e)));
  const list = `<div class="tb-list">
    <div class="tb-row tb-head">
      <input type="checkbox" data-sel="__all" ${allChecked ? 'checked' : ''} title="全选 / 全不选">
      <span class="tb-head-label">${entries.length ? `${entries.length} 个实体` : '清单'}</span>
      <span class="hint agent-only">显示的先后就是注入的先后</span>
    </div>
    <div class="tb-rows">
      ${entries.map((entry) => listRow(entry)).join('')}
      ${entries.length ? '' : '<p class="empty">没有符合筛选的行。</p>'}
    </div>
  </div>`;
  const detail = `<div class="tb-detail" data-key="${esc(String(detailKey))}">${current
    ? renderTermDetail(current)
    : '<p class="empty">术语书还是空的。点右上角「＋ 添加」建第一条。</p>'}</div>`;
  // 两栏都各自滚动（.tb-rows / .tb-detail-body），重绘不许把滚动位置甩回顶部：
  // 同筛同选的重绘（勾选、刷新、点 ✎）原位保住；换筛选/搜索/换详情才归零。
  const box = $('termbook');
  const prevRows = box?.querySelector('.tb-rows');
  const prevBody = box?.querySelector('.tb-detail-body');
  const listSig = `${termFilter}|${filterText.trim().toLowerCase()}|${entries.length}`;
  const listTop = prevRows && listScrollSig === listSig ? prevRows.scrollTop : 0;
  const detailTop = prevBody && box?.querySelector('.tb-detail')?.dataset.key === String(detailKey)
    ? prevBody.scrollTop
    : 0;
  listScrollSig = listSig;
  patch(box, `<div class="tb-split">${list}${detail}</div>`);
  const newRows = box?.querySelector('.tb-rows');
  const newBody = box?.querySelector('.tb-detail-body');
  if (newRows) newRows.scrollTop = listTop;
  if (newBody) newBody.scrollTop = detailTop;
}

/* 待审更正：改已有的译名 / 事实排在那一份文件里，采用才替换。 */
function pendingRow(record) {
  const what = record.what === 'profile' ? '事实' : '译名';
  const index = record.index == null ? '—' : record.index;
  return `<div class="tb-row tb-pending-item" data-pending="${esc(record.id)}">
    <div class="tb-main">
      <div class="tb-line">
        <span class="tb-src">${esc(record.writing)}</span>
        <span class="tb-arrow">${esc(what)} #${esc(index)}</span>
        <span class="tb-dst">${esc(record.old || '（空）')} → ${esc(record.new || '（清空）')}</span>
      </div>
      ${record.why ? `<div class="tb-note hint">${esc(record.why)}</div>` : ''}
    </div>
    <span class="tb-actions">
      ${actionButton('adopt', '采用', { id: record.id }, 'go')}
      ${actionButton('discard', '丢弃', { id: record.id })}
    </span>
  </div>`;
}

function renderPending() {
  const pending = get('termbook')?.pending || [];
  patch($('term-pending-counts'), pending.length ? `${pending.length} 条 — ` : '');
  patch($('term-pending'), pending.length
    ? pending.map(pendingRow).join('')
    : '<p class="empty">没有待审更正。改已有的译名或事实会排在这里。</p>');
}

function renderBulkBar() {
  const bar = $('termbook-bulk');
  if (!bar) return;
  bar.hidden = selection.size === 0;
  $('termbook-bulk-note').textContent = `已选 ${selection.size} 条`;
}

/* ---------- 术语书的动作 ---------- */

/* 有未保存的修改时，先问一句再放行（取消则吞掉这次点击）。 */
function ensureNotEditing() {
  if (!editing) return true;
  if (confirm('有未保存的修改，放弃并继续？')) {
    editing = null;
    return true;
  }
  return false;
}

function onTermbookClick(ev) {
  // 点清单行 = 换详情（勾选框除外）
  const item = ev.target.closest('.tb-item');
  if (item && !ev.target.closest('[data-sel]')) {
    if (item.dataset.detail === detailKey) return;
    if (!ensureNotEditing()) return;
    detailKey = item.dataset.detail;
    renderTermbook();
    return;
  }
  const btn = ev.target.closest('[data-act]');
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === 'save') { saveEdit(btn.dataset.key); return; }
  if (act === 'cancel') { editing = null; renderTermbook(); return; }
  if (act === 'adopt') { adoptPending(btn.dataset.id); return; }
  if (act === 'discard') { discardPending(btn.dataset.id); return; }
  if (!ensureNotEditing()) return;
  const key = btn.dataset.key;
  if (act === 'edit') { editing = { key }; renderTermbook(); }
  else if (act === 'del') deleteEntry(key);
}

/* 改到一半点了页面别处：提醒未保存（确认前不丢内容）。 */
function onDocumentClick(ev) {
  if (!editing) return;
  if (ev.target.closest('#termbook') || ev.target.closest('.content-form')) return;
  if (!confirm('有未保存的修改，确定放弃吗？')) return;
  editing = null;
  renderTermbook();
}

function onTermbookChange(ev) {
  const box = ev.target.closest('[data-sel]');
  if (!box) return;
  if (box.dataset.sel === '__all') {
    // 全选只选**筛出来**的行：看不见的行不进批量删除
    const all = visibleEntries().map(identity);
    selection = box.checked ? new Set(all) : new Set();
  } else if (box.checked) {
    selection.add(box.dataset.sel);
  } else {
    selection.delete(box.dataset.sel);
  }
  renderTermbook();
}

/* `写法 → 译名` 一行一个；只有写法就是"还没定译名"（这一行靠别的写法或事实注入）。 */
function parseWritings(text) {
  const key = [];
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.lastIndexOf('→');
    if (at < 0) key.push({ writing: line, target: '' });
    else key.push({
      writing: line.slice(0, at).trim(),
      target: line.slice(at + 1).trim(),
    });
  }
  return key.filter((item) => item.writing);
}

async function saveEdit(key) {
  const row = document.querySelector('.tb-row.editing');
  const val = (name) => row?.querySelector(`[data-edit="${name}"]`)?.value.trim() ?? '';
  const writings = parseWritings(val('key'));
  if (!writings.length) { setResult('写法不能空：一行一个写法。'); return; }
  const profile = String(row?.querySelector('[data-edit="profile"]')?.value ?? '')
    .split('\n').map((line) => line.trim()).filter(Boolean);
  if (!writings.some((item) => item.target) && !profile.length) {
    setResult('译名和事实至少填一栏 —— 都空的行不会注入。'); return;
  }
  const constant = !!row?.querySelector('[data-edit="constant"]')?.checked;
  const order = Number(row?.querySelector('[data-edit="order"]')?.value ?? 100);
  const position = row?.querySelector('[data-edit="position"]')?.value || 'terms';
  try {
    await apiPost('/api/operations/resource_term_add', {
      key: writings, profile, constant, order, position, by: 'human',
      // 编辑表单把五栏**全部**读出来又全部送回来，所以 `exact` ——
      // "清掉一条事实 / 删掉一个写法"必须表达得出来（缺省是逐栏合并）。
      exact: true,
    });
    editing = null;
    selection.delete(key);
    setResult('已保存。');
    toast('已保存');
    await refreshResources();
  } catch (err) {
    setResult(`保存失败：${err.message}`);
  }
}

async function deleteEntry(key) {
  if (!confirm(`删除「${key}」这一行？（写法与事实会一起删，随时可以再加回来）`)) return;
  try {
    await apiPost('/api/operations/resource_term_remove', { source: key });
    selection.delete(key);
    setResult(`已删除「${key}」。`);
    toast('已删除');
    await refreshResources();
  } catch (err) {
    setResult(`删除失败：${err.message}`);
  }
}

async function adoptPending(id) {
  setPendingResult('正在采用…');
  try {
    await apiPost('/api/operations/resource_term_pending_adopt', { id, by: 'human' });
    setPendingResult('已采用，替换生效。');
    toast('已采用');
    await refreshResources();
  } catch (err) {
    setPendingResult(`采用失败：${err.message}`);
  }
}

async function discardPending(id) {
  setPendingResult('正在丢弃…');
  try {
    await apiPost('/api/operations/resource_term_pending_drop', { id });
    setPendingResult('已丢弃，书里一个字节都没动。');
    toast('已丢弃');
    await refreshResources();
  } catch (err) {
    setPendingResult(`丢弃失败：${err.message}`);
  }
}

/* 从场摘要抽实体：零模型成本、幂等 —— 已登记过的写法不会再来一遍，
 * 所以可以放心连点。新写法 / 空译名 / 新事实免审追加，改已有的译名进待审队列。 */
async function scanSummaryCandidates() {
  const btn = $('term-candidates-scan');
  if (btn) btn.disabled = true;
  setResult('正在从场摘要抽实体…');
  try {
    const body = await apiPost('/api/operations/resource_term_candidates', { min_scenes: 2 });
    setResult(`摘要侧实体：新增 ${body.created_count} 行，跳过 ${body.skipped_count} 条`
      + (body.proposed_count ? `，要改已有译名的进待审 ${body.proposed_count} 条` : ''));
    toast(body.created_count ? `新增 ${body.created_count} 行` : '没有新实体');
    await refreshResources();
  } catch (err) {
    setResult(`抽取失败：${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteSelected() {
  const count = selection.size;
  if (!count) return;
  if (!confirm(`删除所选 ${count} 行？删了可以随时再加回来。`)) return;
  setResult(`正在删除 ${count} 行…`);
  const failures = [];
  for (const key of selection) {
    try {
      await apiPost('/api/operations/resource_term_remove', { source: key });
    } catch (err) {
      failures.push(`${key}：${err.message}`);
    }
  }
  selection.clear();
  setResult(failures.length
    ? `删完，但有 ${failures.length} 条失败：${failures.join('；')}`
    : `已删除 ${count} 行。`);
  toast(failures.length ? '部分删除失败' : '已删除');
  await refreshResources();
}

/* ---------- 风格指南（保持表格式） ---------- */

function renderStyle(body) {
  const rows = (body?.entries || []).map(
    (e) => `<tr>
      <td class="mono">${esc(e.aspect)}</td>
      <td>${esc(e.value)}</td>
      <td class="mono">${esc(e.scope)}</td>
      <td class="src">${esc(e.note) || '<span class="hint">—</span>'}</td>
      <td class="num">${esc(e.priority)}</td>
      <td class="actions"><button type="button" class="rowbtn" data-style-del="1" data-aspect="${esc(e.aspect)}" data-scope="${esc(e.scope || '')}" title="删除">🗑</button></td>
    </tr>`
  );
  table($('style-table'), ['维度', '要求', '作用域', '备注', '优先级', ''], rows);
}

function renderStyleProblems(body) {
  const problems = body?.problems || [];
  patch($('style-problems'), problems.length
    ? `<div class="checks">${problems
      .map((p) => `<span class="c bad">✗ ${esc(p.message || p.code || JSON.stringify(p))}</span>`)
      .join('')}</div>`
    : '');
}

async function removeStyle(aspect, scope) {
  if (!confirm(`删除风格要求「${aspect}」？`)) return;
  const result = $('style-result');
  result.textContent = '正在删除…';
  const payload = { aspect };
  if (scope) payload.scope = scope;
  try {
    await apiPost('/api/operations/resource_style_remove', payload);
    result.textContent = '已删除。';
    toast('已删除');
    await refreshResources();
  } catch (error) {
    result.textContent = `删除失败：${error.message}`;
  }
}

/* ---------- 装配 ---------- */

export const view = {
  id: 'resources',
  slices: ['termbook', 'style', 'operations'],

  mount() {
    for (const slot of ADD_FORMS) {
      $(slot.container)?.addEventListener('submit', (ev) => {
        const form = ev.target.closest('.content-form');
        if (!form) return;
        ev.preventDefault();
        submitContentForm(form);
      });
      $(slot.container)?.addEventListener('input', () => {
        contentDirty = true;
      });
    }
    // "＋ 添加"才弹出表单
    $('termbook-add-toggle').addEventListener('click', () => {
      addOpen = !addOpen;
      $('termbook-add').hidden = !addOpen;
      $('termbook-add-toggle').textContent = addOpen ? '收起' : '＋ 添加';
    });
    $('term-candidates-scan').addEventListener('click', scanSummaryCandidates);
    $('termbook-chips').addEventListener('click', (ev) => {
      const chip = ev.target.closest('[data-term-filter]');
      if (!chip) return;
      if (!ensureNotEditing()) return;
      termFilter = chip.dataset.termFilter;
      renderTermbook();
    });
    $('termbook-search').addEventListener('input', (ev) => {
      if (!ensureNotEditing()) {
        ev.target.value = filterText; // 放弃了换页就把框里的字退回去
        return;
      }
      filterText = ev.target.value;
      renderTermbook();
    });
    $('termbook').addEventListener('click', onTermbookClick);
    $('termbook').addEventListener('change', onTermbookChange);
    $('term-pending').addEventListener('click', onTermbookClick);
    document.addEventListener('click', onDocumentClick);
    $('termbook-bulk-delete').addEventListener('click', deleteSelected);
    $('termbook-bulk-cancel').addEventListener('click', () => {
      selection.clear();
      renderTermbook();
    });
    $('style-table').addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-style-del]');
      if (!btn) return;
      removeStyle(btn.dataset.aspect, btn.dataset.scope);
    });
  },

  activate() {
    // 术语书是这一页的主数据：轮询不拉它（app.js 的注释里有原因），
    // 所以每次进页面自己刷新一次 —— 不然打开就是"空的"
    refreshResources();
  },

  render() {
    const ops = get('operations');
    if (ops) renderContentForms(ops.operations || []);
    renderTermbook();
    renderPending();
    renderStyle(get('style'));
    renderStyleProblems(get('style'));
  },
};
