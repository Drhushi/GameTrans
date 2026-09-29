/* 请求（原「工作台」）：**请求台账 + 请求模板** 摆在同一页。
 *
 * 为什么合成一页：这两样是同一件事的两半 ——
 *   模板 决定"请求长什么样"，台账 给出"真发出去/只拦下来的那些请求与回复"。
 * 原第三栏「图结构」与路径图的表格重复，已砍掉；单元与场次去路径图看。
 *
 * 台账那张表把**拦截下来的请求**与**真实调用流水**合并了：两边的正文大半重复，
 * 按请求正文的指纹对上号 —— 真跑过的那行多出回复/用量/错误与"哪几条没拿到"，
 * 只拦没发的单独一行标「未发出」。长列表服务端分页。
 *
 * 模板那一栏是"阴阳代码"的解药：预览按钮调的是**生产那条装配路径**
 * （`/api/prompt-preview` → TranslateLayer.build_request），所以这里看到的就是会发出去的。
 */

import { esc, num, $ } from '../util.js';
import { patch, toast } from '../render.js';
import { api, apiPost } from '../api.js';
import { get, set } from '../store.js';

const PAGE = 50;

let tab = 'ledger'; // ledger | template
let offset = 0;
let ledgerFilter = '';
let selected = null; // 台账里点开的那一条
let detail = null;
let detailNote = '';
let draft = null; // 正在编辑的模板草稿
let draftName = '';
let preview = null;
let previewNote = '';
let previewCompare = ''; // 与哪个模板对比（空＝不对比）
let previewUnit = ''; // 预览哪个单元（空＝槽位最多的那个）
let previewUnits = []; // 可选单元清单（后端给）
let onlyDiff = false; // 只显示两边不一样的行（各留一行上下文）

/* ---------- 顶部标签 ---------- */

const TABS = [
  ['ledger', '请求台账'],
  ['template', '请求模板'],
];

function renderTabs() {
  patch($('workbench-tabs'), TABS.map(([id, label]) =>
    `<button class="gbtn${tab === id ? ' on' : ''}" data-tab="${id}" type="button">${esc(label)}</button>`
  ).join(''));
}

function renderActiveTab() {
  for (const [id] of TABS) {
    const box = $(`wb-${id}`);
    if (box) box.hidden = id !== tab;
  }
  renderTabs();
}

/* ---------- 一、请求台账 ---------- */

function ledgerRows() {
  const all = (get('exchanges') || {}).exchanges || [];
  const q = ledgerFilter.trim().toLowerCase();
  if (!q) return all;
  return all.filter((row) => [row.run_id, row.phase, row.template, row.model, row.error]
    .some((value) => String(value || '').toLowerCase().includes(q)));
}

function ledgerSummary() {
  const body = get('exchanges');
  const s = (body && body.summary) || {};
  if (!s.rows) return '';
  const seconds = (ms) => (ms == null ? '—' : (ms / 1000).toFixed(1) + ' 秒');
  const minutes = (ms) => (ms == null ? '—' : (ms / 60000).toFixed(1) + ' 分钟');
  return `共 ${s.rows} 条请求：真发出 ${s.sent} 次（失败 ${s.failed}）、只拦没发 ${s.intercepted_only} 条 · ` +
    `过程里有 ${s.missed_count} 条槽位曾在某次调用里没拿到 · ` +
    `耗时合计 ${minutes(s.total_ms)}，每次调用 p50 ${seconds(s.p50_ms)} / p95 ${seconds(s.p95_ms)}` +
    (s.seconds_per_item != null ? `，平均 ${s.seconds_per_item} 秒/条` : '');
}

/** 错误这一列只给**一个编号**：HTTP 状态码（404 / 429 / 402…），其余一律「其他」。
 *  完整报文留在 title 与详情里 —— 列表里铺一长串服务端 JSON，只会把该看的东西淹掉。 */
function errorTag(text) {
  const raw = String(text || '').trim();
  if (!raw) return '—';
  const http = raw.match(/\bHTTP\s+(\d{3})\b/);
  return http ? http[1] : '其他';
}

function renderLedger() {
  const body = get('exchanges');
  const all = ledgerRows();
  patch($('wb-ledger-meta'), ledgerSummary());
  if (body && body.warnings && body.warnings.length) {
    patch($('wb-ledger-warn'),
      `<span class="c warn">! ${esc(body.warnings.slice(0, 3).join('；'))}</span>`);
  } else {
    patch($('wb-ledger-warn'), '');
  }

  if (!all.length) {
    patch($('wb-ledger-table'),
      '<tbody><tr><td class="empty">还没有请求台账 —— 跑一次 translate，或先用拦截脚本冻一份请求</td></tr></tbody>');
    patch($('wb-ledger-pager'), '');
    return;
  }

  const head = '<thead><tr><th>来源</th><th class="agent-only">运行</th><th class="agent-only">#</th>' +
    '<th>阶段</th><th>模板</th><th>要问</th>' +
    '<th>没拿到</th><th>结果</th><th>耗时</th><th>秒/条</th>' +
    '<th class="agent-only">请求字符</th><th class="agent-only">回复字符</th>' +
    '<th>token</th><th>错误</th></tr></thead>';
  const rows = all.map((row) => {
    const on = selected === row.id;
    const lost = (row.missing || []).length;
    const usage = row.usage || {};
    const origin = row.sent ? '已发出' : '未发出';
    const seconds = row.latency_ms != null ? (row.latency_ms / 1000).toFixed(1) : '—';
    return `<tr class="rowlink${on ? ' on' : ''}" data-id="${esc(row.id)}">
      <td class="mono">${esc(origin)}${row.intercepted_too ? ' <span class="hint">· 也拦过</span>' : ''}</td>
      <td class="mono truncate agent-only">${esc(row.run_id || row.name || '—')}</td>
      <td class="mono agent-only">${row.index || '—'}</td>
      <td class="mono">${esc(row.phase || '—')}</td>
      <td class="mono truncate" title="${esc(row.template_digest || '')}">${
        esc(row.template || '—')}</td>
      <td class="num">${row.items}</td>
      <td class="num ${lost ? 'st-err' : ''}">${lost || '—'}</td>
      <td class="mono ${row.ok === null ? 'st-dim' : row.ok ? 'st-ok' : 'st-err'}">${
        row.ok === null ? '—' : row.ok ? '成功' : '失败'}</td>
      <td class="num">${seconds}</td>
      <td class="num">${row.seconds_per_item != null ? row.seconds_per_item.toFixed(2) : '—'}</td>
      <td class="num agent-only">${num(row.prompt_chars)}</td>
      <td class="num agent-only">${num(row.response_chars)}</td>
      <td class="num">${usage.total_tokens != null ? num(usage.total_tokens) : '—'}</td>
      <td class="mono" title="${esc(row.error || '')}">${errorTag(row.error)}</td>
    </tr>`;
  }).join('');
  patch($('wb-ledger-table'), `${head}<tbody>${rows}</tbody>`);

  const total = body.total || all.length;
  const here = offset + all.length;
  patch($('wb-ledger-pager'), `
    <button class="gbtn" data-go="${Math.max(0, offset - PAGE)}" type="button" ${offset ? '' : 'disabled'}>上一页</button>
    <span class="hint">第 ${offset + 1}–${here} 条 / 共 ${total} 条</span>
    <button class="gbtn" data-go="${here}" type="button" ${here < total ? '' : 'disabled'}>下一页</button>`);
}

function block(title, body, note) {
  return `<div class="dsec"><h4>${esc(title)}${note ? ` <span class="hint">${esc(note)}</span>` : ''}</h4>
    <pre class="calltext">${esc(body || '（空）')}</pre></div>`;
}

/** 这一次回复里**模型申报的实体**（术语书产出）：parsed 里每条译文都带
 * declared_terms（原文写法 → 译名 / 设定），按出现顺序去重。全是候选 ——
 * 批准与否决在「资源」页，这里只负责让人看见"这一次产出了什么"。 */
function declaredTerms(row) {
  const out = [];
  const seen = new Set();
  for (const item of row.parsed || []) {
    for (const t of item.declared_terms || []) {
      const source = String(t.source || '').trim();
      if (!source) continue;
      const key = `${source}\u0000${t.target || ''}\u0000${t.profile || ''}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(t);
    }
  }
  return out;
}

function renderDetail() {
  const box = $('wb-ledger-detail');
  if (!box) return;
  if (!selected) {
    patch(box, '<p class="hint">点上面任意一行，这里给出那一次请求逐字的 system / user 与回复。</p>');
    return;
  }
  if (!detail) {
    patch(box, `<p class="hint">${esc(detailNote || '读取中…')}</p>`);
    return;
  }
  const row = detail;
  const lost = row.missing || [];
  const parsed = row.parsed || [];
  const answers = parsed.length
    ? parsed.map((item) => `- [${item.unit_id ?? '?'}] ${item.target ?? ''}`).join('\n')
    : '（没有解析出逐条译文）';
  const terms = declaredTerms(row);
  const termsBlock = terms.length
    ? `<div class="dsec"><h4>术语书产出 <span class="hint">这次回复里模型申报的实体 · 全是候选，批准与否决在「资源」页</span></h4>
      <table class="termtable"><thead><tr><th>原文写法</th><th>译名</th><th>设定</th></tr></thead><tbody>
        ${terms.map((t) => `<tr>
          <td class="mono">${esc(t.source)}</td>
          <td>${esc(t.target || '') || '<span class="hint">—</span>'}</td>
          <td class="src">${esc(t.profile || '') || '<span class="hint">—</span>'}</td>
        </tr>`).join('')}
      </tbody></table></div>`
    : '';
  // 问了哪几条、回了几条：复盘对齐用的机器信息，收进 agent 视图
  patch(box, [
    `<div class="dsec"><h4>这一次的账</h4><dl class="kv">
      <dt>请求模板</dt><dd class="mono">${esc(row.template || '—')}${
        row.template_label ? ` <span class="hint">${esc(row.template_label)}</span>` : ''} ${
        row.template_digest ? `<span class="hint">${esc(row.template_digest)}</span>` : ''}</dd>
      <dt>要问 / 回了几条</dt><dd>${num(row.items)} / ${num(parsed.length)}${
        lost.length ? ` <span class="c bad">没拿到 ${num(lost.length)}</span>` : ''}</dd>
      <dt>耗时</dt><dd>${row.latency_ms != null ? (row.latency_ms / 1000).toFixed(1) + ' 秒' : '—'}${
        row.seconds_per_item != null ? `（${row.seconds_per_item.toFixed(2)} 秒/条）` : ''}</dd>
      <dt>收尾原因</dt><dd class="mono">${esc(row.finish_reason || '—')}</dd>
      <dt>推理 token</dt><dd>${row.reasoning_tokens != null ? num(row.reasoning_tokens) : '—'}</dd>
    </dl></div>`,
    termsBlock,
    `<div class="dsec agent-only"><h4>要问哪几条（${num((row.unit_ids || []).length)} 条，其中没拿到 ${num(lost.length)}）</h4>
      <pre class="calltext">${esc((row.unit_ids || []).join('\n') || '（没记下槽位清单）')}</pre>${
      lost.length ? `<pre class="calltext">没拿到的：\n${esc(lost.join('\n'))}</pre>` : ''}</div>`,
    `<div class="dsec agent-only"><h4>回了几条（${num(parsed.length)} 条逐字对账）</h4>
      <pre class="calltext">${esc(answers)}</pre></div>`,
    block('system（提示词）', row.prompt_system),
    block('user（真发出去的待译内容）', row.prompt_user),
    block('模型原始回复', row.response_raw,
      row.sent ? (row.response_raw ? '' : '这一次没有回复正文') : '（这条只拦下来过，没发出去）'),
    row.error ? block('错误', row.error) : '',
  ].join(''));
}

async function openRow(id) {
  selected = id;
  detail = null;
  detailNote = '';
  renderLedger();
  renderDetail();
  try {
    const body = await api(`/api/exchanges/${encodeURIComponent(id)}`);
    if (selected === id) detail = body.exchange;
  } catch (err) {
    if (selected === id) detailNote = `读不到这一条：${err.message}`;
  }
  renderDetail();
}

/* ---------- 二、请求模板 ---------- */

function templates() {
  const body = get('templates');
  return (body && body.templates) || {};
}

function fields() {
  const body = get('templates');
  return (body && body.fields) || [];
}

function activeName() {
  const body = get('templates');
  return (body && body.active) || 'full';
}

function templateNames() {
  const saved = (get('templates') || {}).saved || [];
  return Object.keys(templates()).map((name) => `${name}${saved.includes(name) ? '（自定义）' : ''}`);
}

function fieldInput(field, value) {
  const key = esc(field.key);
  if (field.kind === 'bool') {
    return `<label class="field"><span>${esc(field.label)}</span>
      <input type="checkbox" data-tpl="${key}" ${value ? 'checked' : ''}></label>`;
  }
  if (field.kind === 'multiline') {
    return `<label class="field wide-field" title="${esc(field.help || '')}">
      <span>${esc(field.label)}</span>
      <textarea data-tpl="${key}" rows="4" spellcheck="false">${esc(value ?? '')}</textarea></label>`;
  }
  return `<label class="field wide-field" title="${esc(field.help || '')}">
    <span>${esc(field.label)}</span>
    <input type="text" data-tpl="${key}" value="${esc(value ?? '')}" spellcheck="false"></label>`;
}

/* 两份请求正文的**行差**：只在 A 出现的行、只在 B 出现的行。
 * 不做对齐（LCS）—— 比的是同一批条目，值得看的是"这一行这边有那边没有"，
 * 逐行对齐在没有共同锚点的两段提示词上只会给出更难读的结果。 */
function bodyDiff(a, b) {
  const la = String(a || '').split('\n');
  const lb = String(b || '').split('\n');
  const seenB = new Map();
  for (const line of lb) seenB.set(line, (seenB.get(line) || 0) + 1);
  const seenA = new Map();
  for (const line of la) seenA.set(line, (seenA.get(line) || 0) + 1);
  const onlyA = new Set();
  const usedB = new Map();
  la.forEach((line, i) => {
    const budget = (seenB.get(line) || 0) - (usedB.get(line) || 0);
    if (line.trim() && budget > 0) usedB.set(line, (usedB.get(line) || 0) + 1);
    else if (line.trim()) onlyA.add(i);
  });
  const usedA = new Map();
  const onlyB = new Set();
  lb.forEach((line, i) => {
    const budget = (seenA.get(line) || 0) - (usedA.get(line) || 0);
    if (line.trim() && budget > 0) usedA.set(line, (usedA.get(line) || 0) + 1);
    else if (line.trim()) onlyB.add(i);
  });
  return { la, lb, onlyA, onlyB };
}

function sideBySide(diff, side, limit) {
  const lines = side === 'a' ? diff.la : diff.lb;
  const marks = side === 'a' ? diff.onlyA : diff.onlyB;
  let keep = lines.map((_, i) => i);
  if (onlyDiff) {
    const around = new Set();
    marks.forEach((i) => { for (let k = i - 1; k <= i + 1; k += 1) around.add(k); });
    keep = keep.filter((i) => around.has(i));
  }
  const shown = keep.slice(0, limit);
  const body = shown.map((i) =>
    `<span class="dline${marks.has(i) ? ' only' : ''}">${esc(lines[i]) || ' '}</span>`).join('');
  return { body, hidden: keep.length - shown.length };
}

function renderPreview() {
  if (!preview) return '';
  const { list, name, compare } = preview;
  const others = templateNames().map((raw) => raw.replace(/（自定义）$/, ''))
    .filter((value) => value !== name);
  const picker = `<label class="field"><span>单元</span>
      <select id="wb-unit">
        ${previewUnits.map((unit) => `<option value="${esc(unit.node_id)}" ${
          unit.node_id === previewUnit ? 'selected' : ''}>${
          esc(unit.label)} · ${unit.slots} 槽</option>`).join('')}
      </select></label>
    ${others.length ? `<label class="field"><span>对比</span>
      <select id="wb-compare">
        <option value="">不对比</option>
        ${others.map((value) => `<option value="${esc(value)}" ${
          value === compare ? 'selected' : ''}>${esc(value)}</option>`).join('')}
      </select></label>` : ''}
    <label class="field"><span>只看不同的行</span>
      <input type="checkbox" id="wb-only-diff" ${onlyDiff ? 'checked' : ''}></label>`;

  const blocks = list.map((item) => {
    const other = item.compare;
    const head = `<h4>${esc(item.unit_label || item.unit_id)}
      <span class="hint">${item.items} 条（要回填 ${item.asked ?? item.items} 条）</span></h4>`;
    if (!other) {
      return `<div class="dsec">${head}
        <pre class="calltext">${esc(item.system)}</pre>
        <pre class="calltext">${esc(item.user)}</pre></div>`;
    }
    const sys = bodyDiff(item.system, other.system);
    const user = bodyDiff(item.user, other.user);
    const stat = (diff, index) => {
      const lines = index === 0 ? diff.la : diff.lb;
      const marks = index === 0 ? diff.onlyA : diff.onlyB;
      return `${lines.length} 行 / ${(index === 0 ? item : other).user.length.toLocaleString()} 字符` +
        (marks.size ? ` · 这边独有 ${marks.size} 行` : '');
    };
    const left = sideBySide(user, 'a', onlyDiff ? 2000 : 400);
    const right = sideBySide(user, 'b', onlyDiff ? 2000 : 400);
    return `<div class="dsec">${head}
      <p class="hint">同一批条目、同一条装配路径，只换模板。system 提示词：${name} ${
        sys.onlyA.size} 行独有 / ${other.name} ${sys.onlyB.size} 行独有。</p>
      <div class="diff-pair">
        <div class="dsec"><h4>${esc(name)} <span class="hint">${stat(user, 0)}</span></h4>
          <pre class="calltext">${left.body}</pre>
          ${left.hidden ? `<p class="hint">还有 ${num(left.hidden)} 行没显示</p>` : ''}</div>
        <div class="dsec"><h4>${esc(other.name)} <span class="hint">${stat(user, 1)}</span></h4>
          <pre class="calltext">${right.body}</pre>
          ${right.hidden ? `<p class="hint">还有 ${num(right.hidden)} 行没显示</p>` : ''}</div>
      </div>
      <details><summary class="hint">看两份 system 提示词</summary>
        <div class="diff-pair">
          <pre class="calltext">${esc(item.system)}</pre>
          <pre class="calltext">${esc(other.system)}</pre>
        </div>
      </details>
    </div>`;
  }).join('');

  return `<div class="dsec"><h4>预览 <span class="hint">用生产那条装配路径装出来的请求，与真跑逐字同源</span>
      <span class="row" style="margin-left:auto;">${picker}</span></h4>
    ${blocks}</div>`;
}

function renderTemplate() {
  const body = get('templates');
  if (!body) {
    patch($('wb-template-body'), '<p class="hint">读取中…</p>');
    return;
  }
  if (!draft) {
    draft = { ...(templates()[activeName()] || {}) };
    draftName = activeName();
  }
  const saved = body.saved || [];
  // 左栏清单：点一下就换草稿；生效中的那份常亮，不再需要"选中 → 点切到它"两步
  const list = Object.keys(templates()).map((name) => {
    const live = name === activeName();
    return `<button class="tpl-item${name === draftName ? ' on' : ''}" data-tpl-pick="${esc(name)}" type="button">
      ${esc(name)}${live ? ' <span class="live">● 生效中</span>' : ''}
      <span class="hint">${saved.includes(name) ? '自定义' : '出厂模板'}</span>
    </button>`;
  }).join('');
  const isCustom = saved.includes(draftName);

  const html = `
    <div class="tpl-grid">
      <aside class="tpl-list">
        ${list}
        <button class="gbtn" id="wb-template-reset" type="button" title="出厂模板恢复原样，自定义的保留">恢复出厂模板</button>
      </aside>
      <div class="dsec">
        <div class="row spread" style="align-items:flex-end;">
          <div>
            <div class="hint" style="margin-bottom:4px;">正在编辑</div>
            <b style="font-size:14px;">${esc(draftName)}</b>
            ${draftName === activeName() ? '<span class="live" style="color:var(--accent);"> ● 生效中</span>' : ''}
          </div>
          <div class="row">
            <button class="gbtn" id="wb-template-use" type="button"
              title="让「${esc(draftName)}」成为真跑用的模板（未保存的名字要先另存为）">启用这份</button>
            <button class="gbtn" id="wb-template-preview" type="button">预览请求</button>
          </div>
        </div>
        <div class="row" style="margin:10px 0;">
          <label class="field"><span>另存为名字</span>
            <input type="text" id="wb-template-name" value="${esc(draftName)}" spellcheck="false" style="width:190px;"></label>
          <button class="gbtn" id="wb-template-save" type="button" style="align-self:flex-end;">另存为（同名即覆盖）</button>
          ${isCustom ? `<button class="gbtn" id="wb-template-delete" type="button" style="align-self:flex-end;" title="删掉「${esc(draftName)}」">🗑 删除</button>` : ''}
        </div>
        <div class="config-groups" id="wb-template-fields">
          ${fields().map((field) => fieldInput(field, draft[field.key])).join('')}
        </div>
      </div>
    </div>
    ${preview ? renderPreview() : ''}
    ${previewNote ? `<p class="hint">${esc(previewNote)}</p>` : ''}`;
  patch($('wb-template-body'), html);
}

function collectDraft() {
  const next = { ...draft };
  document.querySelectorAll('#wb-template-fields [data-tpl]').forEach((el) => {
    const key = el.dataset.tpl;
    next[key] = el.type === 'checkbox' ? el.checked : el.value;
  });
  const nameBox = $('wb-template-name');
  if (nameBox) draftName = nameBox.value.trim();
  return next;
}

async function saveTemplate() {
  const template = collectDraft();
  try {
    const body = await apiPost('/api/prompt-templates',
      { action: 'save', name: draftName, template });
    set('templates', body);
    draft = { ...(body.templates[draftName] || template) };
    toast(`已保存并切到「${draftName}」`);
  } catch (err) {
    toast(err.message);
  }
  renderTemplate();
}

async function templateAction(payload, note) {
  try {
    const body = await apiPost('/api/prompt-templates', payload);
    set('templates', body);
    draft = null;
    preview = null;
    toast(note);
  } catch (err) {
    toast(err.message);
  }
  renderTemplate();
}

function knownTemplate(name) {
  return Object.keys(templates()).includes(name);
}

async function runPreview() {
  draft = collectDraft();
  previewNote = '';
  // 预览走的是生产装配路径，后端只认**已保存**的模板名 —— 草稿传不过去。
  // 所以这里得说实话：预览的是哪一份，草稿的改动看不看得到。
  const target = knownTemplate(draftName) ? draftName : activeName();
  if (knownTemplate(draftName)) {
    const saved = templates()[draftName] || {};
    if (Object.keys(draft).some((key) => saved[key] !== draft[key])) {
      previewNote = `预览的是已保存的「${draftName}」；正在编辑的草稿还没保存，预览里看不到 —— 先「另存为」。`;
    }
  } else if (draftName) {
    previewNote = `「${draftName}」还没保存；预览用的是当前生效模板。`;
  }
  // 不挑对比对象时，默认对着**另一个出厂模板**预览 —— 看差异是这一栏的主要用途
  const builtins = (get('templates') || {}).builtin || Object.keys(templates());
  if (!previewCompare || previewCompare === target) {
    previewCompare = builtins.find((name) => name !== target) || '';
  }
  try {
    const body = await apiPost('/api/prompt-preview',
      { units: 1, name: target, compare: previewCompare, unit: previewUnit });
    preview = { name: body.template, compare: body.compare, list: body.previews || [] };
    previewUnits = body.units || [];
    if (!previewUnit && previewUnits.length) previewUnit = previewUnits[0].node_id;
  } catch (err) {
    preview = null;
    previewNote = `预览失败：${err.message}`;
  }
  renderTemplate();
}

/* ---------- 装配 ---------- */

export const view = {
  id: 'workbench',
  slices: ['exchanges', 'templates'],

  mount() {
    $('workbench-tabs')?.addEventListener('click', (ev) => {
      const button = ev.target.closest('button[data-tab]');
      if (!button) return;
      tab = button.dataset.tab;
      renderActiveTab();
      if (tab === 'template') renderTemplate();
    });

    $('wb-ledger-table')?.addEventListener('click', (ev) => {
      const row = ev.target.closest('tr[data-id]');
      if (row) openRow(row.dataset.id);
    });

    $('wb-ledger-filter')?.addEventListener('input', (ev) => {
      ledgerFilter = ev.target.value;
      renderLedger();
    });

    $('wb-ledger-pager')?.addEventListener('click', (ev) => {
      const button = ev.target.closest('button[data-go]');
      if (!button || button.disabled) return;
      offset = Number(button.dataset.go) || 0;
      api(`/api/exchanges?limit=${PAGE}&offset=${offset}`)
        .then((body) => set('exchanges', body))
        .catch(() => {});
    });

    $('wb-template-body')?.addEventListener('click', (ev) => {
      const pick = ev.target.closest('[data-tpl-pick]');
      if (pick) {
        // 清单里点一份就把它装进编辑区（未保存的草稿会被覆盖 —— 与原来换下拉同语义）
        draft = { ...(templates()[pick.dataset.tplPick] || {}) };
        draftName = pick.dataset.tplPick;
        preview = null;
        renderTemplate();
        return;
      }
      const id = ev.target.id;
      if (id === 'wb-template-use') {
        templateAction({ action: 'activate', name: draftName }, `已切换生效模板：${draftName}`);
      } else if (id === 'wb-template-save') {
        saveTemplate();
      } else if (id === 'wb-template-delete') {
        templateAction({ action: 'delete', name: draftName }, '已删除');
      } else if (id === 'wb-template-reset') {
        templateAction({ action: 'reset' }, '已恢复出厂模板');
      } else if (id === 'wb-template-preview') {
        runPreview();
      }
    });

    // 对比对象与"只看不同的行"：改一下就用同一份数据重画，不必重新装配请求
    $('wb-template-body')?.addEventListener('change', (ev) => {
      if (ev.target.id === 'wb-compare') {
        previewCompare = ev.target.value;
        runPreview();
      } else if (ev.target.id === 'wb-unit') {
        previewUnit = ev.target.value;
        runPreview();
      } else if (ev.target.id === 'wb-only-diff') {
        onlyDiff = ev.target.checked;
        renderTemplate();
      }
    });
  },

  render() {
    renderActiveTab();
    if (tab === 'ledger') {
      renderLedger();
      renderDetail();
    } else {
      renderTemplate();
    }
  },
};
