/* 01 总览：流水线动脉 + 指标砖 + 动态流 + 成本与运行记录。
 * 原独立「账本」页并入：覆盖/质量与指标砖是同一组数字，只留一份；
 * 花销与运行记录（每轮 token、资产用/中、产出、还没做）搬到这里。 */

import { esc, num, $ } from '../util.js';
import { patch, table } from '../render.js';
import { get } from '../store.js';

function renderPipeline(status, running) {
  const g = status.graph || {};
  const t = status.translations || {};
  const patches = (status.patches || []).length;
  const target = status.export_target || {};

  const extractDone = !!g.present;
  const translateState = !t.present
    ? 'idle'
    : t.failed || t.needs_review
      ? 'warn'
      : 'done';
  const deliverDone = patches > 0;

  const translateLabel = running
    ? '进行中'
    : { done: '已完成', warn: '有待处理', idle: '未开始' }[translateState];
  const translateMetric = t.present
    ? `${num(t.ok)}/${num(t.total)} 可用` +
      (t.needs_review ? ` · 复核 ${num(t.needs_review)}` : '') +
      (t.failed ? ` · 失败 ${num(t.failed)}` : '')
    : '运行 <code>gametrans translate</code>';

  const stages = [
    {
      n: '1', name: '提取', view: 'graph',
      state: extractDone ? 'done' : 'idle',
      label: extractDone ? '已完成' : '未开始',
      metric: extractDone
        ? `${num(g.translatable)} 可译 · ${num(g.nodes)} 节点 · ${num(g.char_count)} 字`
        : '运行 <code>gametrans scan</code>',
    },
    {
      n: '2', name: '翻译', view: 'translations',
      state: running ? 'run' : translateState,
      label: translateLabel,
      metric: translateMetric,
    },
    {
      n: '3', name: '交付', view: 'ops',
      state: deliverDone ? 'done' : 'idle',
      label: deliverDone ? '有产物' : '未开始',
      metric: `${num(patches)} 个补丁` +
        (target.file_structure ? ` · ${esc(target.file_structure)}` : ''),
    },
  ];

  const stageHtml = (s) => `<li><a class="stage ${s.state}" href="#/${s.view}" data-n="${s.n}">
    <span class="stage-head">
      <span class="stage-name">${esc(s.name)}</span>
      <span class="stage-state">${esc(s.label)}</span>
    </span>
    <span class="stage-metric">${s.metric}</span>
  </a></li>`;
  const arrow = (lit) => `<li class="pipe-arrow ${lit ? 'lit' : ''}" aria-hidden="true"></li>`;

  patch($('pipe'),
    stageHtml(stages[0]) + arrow(extractDone) + stageHtml(stages[1]) + arrow(t.present) + stageHtml(stages[2]));
}

function renderTiles(status) {
  const g = status.graph || {};
  const t = status.translations || {};
  const failed = t.failed || 0;
  const review = t.needs_review || 0;
  const done = t.ok ?? t.usable ?? 0;
  const untranslated = g.present && t.present
    ? Math.max(0, (g.translatable || 0) - (t.total || 0))
    : null;
  patch($('tiles'), `
    <div class="tile"><div class="n">${num(g.present ? g.translatable : 0)}</div><div class="k">可译条目</div></div>
    <div class="tile ${t.present ? 'ok' : ''}"><div class="n">${num(done)}</div><div class="k">已翻译</div></div>
    <div class="tile ${review ? 'warn' : ''}"><div class="n">${num(review)}</div><div class="k">待复核</div></div>
    <div class="tile ${failed ? 'err' : ''}"><div class="n">${num(failed)}</div><div class="k">翻译失败</div></div>
    <div class="tile"><div class="n">${untranslated == null ? '—' : num(untranslated)}</div><div class="k">尚未翻译</div></div>
    <div class="tile"><div class="n">${num((status.patches || []).length)}</div><div class="k">补丁</div></div>`);
}

function renderViews(body) {
  const scopeEl = $('views-scope');
  const scopeText = body.scope === 'all' ? '（含对 agent 透明的信息）' : '（用户可见）';
  if (scopeEl.textContent !== scopeText) scopeEl.textContent = scopeText;

  if (!body.views.length) {
    patch($('views'), '<p class="empty">当前没有对用户可见的信息。</p>');
    return;
  }
  patch($('views'), body.views
    .map((v) => {
      const sections = (v.sections || [])
        .map((s) => {
          const title = s.title ? `<div class="section-title">${esc(s.title)}</div>` : '';
          const lines = (s.lines || []).map((l) => `<div class="line">${esc(l)}</div>`).join('');
          return title + lines;
        })
        .join('');
      const agent = v.overridden_by
        ? `<span class="view-agent">已被 ${esc(v.overridden_by)} 改写</span>`
        : '';
      return `<article class="view-msg ${esc(v.severity)}">
        <div class="view-head">
          <span class="view-title">${esc(v.title)}</span>
          <span class="view-meta">${esc(v.topic)} · ${esc(v.visibility)}</span>
          ${agent}
        </div>${sections}</article>`;
    })
    .join(''));
}

/* ---------- 成本与运行记录（原账本页） ---------- */

function renderCost(status) {
  const cred = status?.credentials || {};
  const cost = status?.graph?.total_cost;
  patch($('ledger-cost'), `
    <dl class="kv">
      <dt>模型</dt><dd class="${cred.configured ? 'ok' : 'bad'}">${esc(`${status?.provider || '—'} · ${cred.model || '未设置'}`)}</dd>
      <dt>凭证</dt><dd class="${cred.configured ? 'ok' : 'bad'}">${esc(cred.configured ? `已配置（${cred.api_key}）` : '未配置')}</dd>
      <dt>成本估算</dt><dd>${cost != null ? esc(num(cost)) : '—'}</dd>
      <dt>token 明细</dt><dd>${get('caps')?.reports ? '见下方运行记录' : '待后端报告接口（需求 R5）'}</dd>
    </dl>`);
}

/* 运行记录那栏：**用了几条资产 / 造了几条**分开写。
 *
 * 为什么非要在面板上看它：真靶上出过"181 条候选一条没批、46 个单元照跑"——
 * 那一轮报告上一切正常，代价要到成品才看得见（同一个人名两种写法）。
 * 这几个读数本来就在 `/api/reports` 的 `metrics` 里，之前只是没人渲染。
 */
function assetCell(m) {
  const usable = (m.assets_usable_glossary || 0) + (m.assets_usable_worldbook || 0);
  const hit = (m.assets_hit_glossary || 0) + (m.assets_hit_worldbook || 0);
  const pending = (m.assets_pending_glossary || 0) + (m.assets_pending_worldbook || 0);
  const verdict = m.asset_preflight;
  if (usable == null && hit == null && !verdict) return '—';
  const label = {
    ok: '有命中',
    no_assets: '零资产',
    pending_only: '只有待批',
    assets_never_fire: '打不响',
  }[verdict] || (verdict || '—');
  const cls = verdict === 'ok' ? 'st-ok' : verdict ? 'st-err' : '';
  return `<span class="${cls}">${esc(label)}</span>`
    + ` <span class="hint">用 ${num(usable)} / 中 ${num(hit)}${pending ? ` / 待批 ${num(pending)}` : ''}</span>`;
}

/* 产出那一栏：这一轮自己造出来多少条，以及会不会自己生效。 */
function produceCell(m) {
  // 「译文与原文对不上」是**这一轮翻出来的坏东西**：一条译文结构全过、状态 ok，
  // 却不是这句原文的译文（真靶 2026-09-29 抓到 5 条）。它已经被置留（不写回、
  // 不进记忆），但翻过一眼才算数 —— 所以在这里点出来，别让它埋在报告 JSON 里。
  const off = num(m.correspondence_failed_records || 0);
  const offCell = m.correspondence_failed_records
    ? `<span class="bad" title="译文与原文对不上（同一单元里两条原文不同的句子译文一字不差／一条译文整段埋在另一条里）—— 已置留，逐条看一眼">对不上 ${off}</span>`
    : '';
  if (m.declared_terms == null && m.terms_conflicts == null) return offCell || '—';
  const parts = [];
  if (offCell) parts.push(offCell);
  if (m.declared_terms) parts.push(`申报 ${num(m.declared_terms)}`);
  if (m.terms_auto_approved) parts.push(`自动批准 ${num(m.terms_auto_approved)}`);
  if (m.terms_waiting) parts.push(`证据不足 ${num(m.terms_waiting)}`);
  if (m.terms_conflicts) parts.push(`<span class="bad">撞车 ${num(m.terms_conflicts)}</span>`);
  if (m.worldbook_candidates_created) parts.push(`世界书 ${num(m.worldbook_candidates_created)}`);
  return parts.length ? parts.join(' · ') : '—';
}

/* 还有哪一步没做：原样印出 `workflow_detail` 的文案（只陈述，不劝）。 */
function stepsCell(m) {
  const codes = m.workflow_notes;
  if (!codes || !codes.length) return '—';
  const texts = (m.workflow_detail || []).map((item) => item.text).filter(Boolean);
  const label = texts.length ? texts.join('；') : codes.join('、');
  return `<span class="bad" title="${esc(label)}">${num(codes.length)} 项</span>`;
}

function renderRuns(reports) {
  const caps = get('caps');
  if (!caps?.reports) {
    $('runs-hint').textContent = '· 运行报告读取接口未接入（后端需求 R5），上线后这里显示每次运行的 token、费用与质量明细';
    table($('runs-table'), [], []);
    return;
  }
  $('runs-hint').textContent = '';
  const rows = (reports?.reports || []).slice(0, 50).map((r) => {
    const m = r.metrics || {};
    return `<tr>
    <td class="mono truncate" title="${esc(r.run_id || '')}">${esc(r.run_id || '—')}</td>
    <td class="mono">${esc(r.command || '—')}</td>
    <td class="mono ${r.ok ? 'st-ok' : 'st-err'}">${r.ok === undefined ? '—' : r.ok ? '成功' : '失败'}</td>
    <td class="mono">${esc(r.finished_at || r.started_at || '—')}</td>
    <td class="num">${m.llm?.total_tokens != null ? num(m.llm.total_tokens) : '—'}</td>
    <td>${assetCell(m)}</td>
    <td>${produceCell(m)}</td>
    <td>${stepsCell(m)}</td>
  </tr>`;
  });
  table($('runs-table'), ['run', '命令', '结果', '时间', 'token', '资产（用/中）', '产出', '还没做'], rows);
}

export const view = {
  id: 'overview',
  slices: ['status', 'views', 'running', 'reports', 'caps'],

  render() {
    const status = get('status');
    if (!status) return;
    renderPipeline(status, !!get('running'));
    renderTiles(status);
    renderCost(status);
    renderRuns(get('reports'));
    const views = get('views');
    if (views) renderViews(views);
  },
};
