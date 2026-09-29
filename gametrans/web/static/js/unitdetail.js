/* 单元现场：从一个结点看进去，**这个单元**的每一句 + 碰过它的那些请求。
 *
 * 句子表带状态筛选（chip 即筛选，与工作单同一套交互）：改完一句状态变了，
 * 计数与列表当场跟上 —— 不再属于当前筛选的句子从表里撤掉。
 *
 * 排版与「工作台」一致：左边信息、正文一律进等宽块（`pre.calltext`），
 * 请求与回复点开才拉全文（列表不背正文）。
 *
 * 改译文：不再用底部的"改这一句"大编辑器（那是"一个单位一句话"年代的产物），
 * 每句译文旁一支铅笔（✎）就地修改 —— 原位变输入框，保存走同一条生产路径，
 * 改到一半点了别处会提醒未保存。
 *
 * 跳转：从路径图进来给「去译文页逐句改」；从译文页进来给「在路径图中查看」。
 * 两边都把焦点单元塞进 store，对面页面 activate 时接住并展开/聚焦。
 */

import { esc, copyText, num } from './util.js';
import { api } from './api.js';
import { statusTag, toast, saveVerdict } from './render.js';

function slotKey(slot) {
  const status = slot.artifact?.status || 'untranslated';
  return status === 'ok' || status === 'usable' ? 'ok'
    : status === 'needs_review' || status === 'untranslated' ? status
    : 'failed';
}

function counts(slots) {
  const out = { ok: 0, needs_review: 0, failed: 0, untranslated: 0 };
  for (const slot of slots) out[slotKey(slot)] += 1;
  return out;
}

function slotStatus(slot) {
  const status = slot.artifact?.status || 'untranslated';
  return statusTag(status === 'ok' ? 'ok' : status);
}

function callLines(call) {
  return [
    `${call.id}`,
    call.items + ' 条',
    call.latency_ms ? (call.latency_ms / 1000).toFixed(1) + 's' : '—',
    call.seconds_per_item ? call.seconds_per_item.toFixed(2) + 's/条' : '—',
    (call.missing || []).length ? `没拿到 ${(call.missing || []).length}` : '全拿到',
    call.ok ? '成功' : '失败',
  ];
}

function block(title, body, note) {
  return `<div class="dsec"><h4>${esc(title)}${
    note ? ` <span class="hint">${esc(note)}</span>` : ''
  }</h4><pre class="calltext">${esc(body || '（空）')}</pre></div>`;
}

async function toggleCall(box, call) {
  if (box.dataset.open === '1') {
    box.dataset.open = '0';
    box.innerHTML = '';
    return;
  }
  box.dataset.open = '1';
  box.innerHTML = '<p class="hint">读取中…</p>';
  try {
    const body = await api(`/api/exchanges/${encodeURIComponent(call.id)}`);
    const row = body.exchange;
    const answers = (row.parsed || []).length
      ? row.parsed.map((item) => `- [${item.unit_id ?? '?'}] ${item.target ?? ''}`).join('\n')
      : '（没有解析出逐条译文）';
    box.innerHTML = [
      block('system（提示词）', row.prompt_system),
      block('user（真发出去的待译内容）', row.prompt_user),
      block('模型原始回复', row.response_raw, row.response_raw ? '' : '这一次没有回复正文'),
      block('解析出来的逐条译文', answers),
      row.error ? block('错误', row.error) : '',
    ].join('');
  } catch (err) {
    box.innerHTML = `<p class="hint">读不到这次请求：${esc(err.message)}</p>`;
  }
}

/**
 * 渲染一个单元现场。
 * payload: {unit, slots:[{slot_key,order,position,speaker,source,artifact}], calls:[...]}
 * opts: {editable, onSave(unitId,target), onJumpToWorklist(unitId), onJumpToGraph(unitId)}
 */
export function renderUnitSite(container, payload, opts = {}) {
  const unit = payload.unit || {};
  const slots = payload.slots || [];
  const calls = payload.calls || [];
  const tally = counts(slots);

  const jump = [];
  if (opts.onJumpToWorklist) {
    jump.push('<button id="site-jump" class="gbtn" type="button">去译文页逐句改 →</button>');
  }
  if (opts.onJumpToGraph) {
    jump.push('<button id="site-back" class="gbtn" type="button">← 在路径图中查看</button>');
  }
  jump.push('<button id="site-copy" class="gbtn ghost" type="button">复制整个单元给 agent</button>');

  container.innerHTML = `
    <dl class="kv">
      <dt>状态</dt><dd>${statusTag(tally.ok ? (tally.ok === slots.length ? 'ok' : 'needs_review') : 'untranslated')}
        <span class="hint">共 ${slots.length} 句 · 分状态筛在下面那句表上</span></dd>
      <dt>路径</dt><dd class="mono">${esc(unit.path || '—')}</dd>
      <dt>类型</dt><dd class="mono">${esc(unit.kind || '—')}${unit.region ? ` · 区域 ${esc(unit.region)}` : ''}</dd>
      <dt>单元身份</dt><dd class="mono">${esc(unit.unit_id || '—')}</dd>
    </dl>
    <div class="row" style="margin-bottom:10px;">${jump.join('')}</div>

    <div class="dsec">
      <h4>这个单元的每一句 <span class="hint">${opts.editable ? '点译文旁的 ✎ 就地修改' : '原文/译文同一张表里对齐看'}</span></h4>
      <div class="chips" id="site-slot-chips" style="margin-bottom:8px;"></div>
      <div class="table-wrap tall" id="site-slot-box">
        <table class="slottable">
          <thead><tr><th>#</th><th>说话人</th><th>原文</th><th>译文</th><th>状态</th><th></th></tr></thead>
          <tbody id="site-slots"></tbody>
        </table>
      </div>
    </div>

    <div class="dsec">
      <h4>碰过这个单元的请求 <span class="hint">${calls.length} 次 · 点一行看那次逐字的 system / user / 回复</span></h4>
      ${calls.length ? calls.map((call) => `
        <div class="callrowline">
          <button class="gbtn calltoggle" type="button" data-call="${esc(call.id)}">
            ${callLines(call).map((piece) => esc(piece)).join(' · ')}
          </button>
          <div class="calldetail" data-box="${esc(call.id)}"></div>
        </div>`).join('')
        : '<p class="hint">这个单元没有被任何一次调用碰到过（还没轮到它，或它本来就不需要翻）。</p>'}
    </div>
  `;

  /* ---- 句级筛选 + 大单元分块渲染：chip 即筛选（与工作单同一套交互），
   * 几千句一口气建 DOM 要好几秒，先出一屏，滚到哪补到哪。
   * 行内编辑 / 复制走事件委托和数据本身，不受分块影响。 ---- */
  const CHUNK = 100;
  const FILTER_LABELS = { ok: '可用', needs_review: '待复核', failed: '失败', untranslated: '未翻' };
  let slotFilter = 'all';
  let shown = 0;
  const tbody = container.querySelector('#site-slots');
  const chipsBox = container.querySelector('#site-slot-chips');

  // 只数得上的状态才出 chip；「全部」永远在（也是退出筛选的出口）
  function renderSlotChips() {
    const tally = counts(slots);
    const defs = [['all', '全部', slots.length],
      ...Object.entries(FILTER_LABELS)
        .map(([key, label]) => [key, label, tally[key]])
        .filter(([, , n]) => n > 0)];
    chipsBox.innerHTML = defs.map(([value, label, n]) =>
      `<span class="chip click ${slotFilter === value ? 'on' : ''}" data-slot-filter="${value}">${esc(label)} <b>${num(n)}</b></span>`)
      .join('');
  }

  const visibleSlots = () => (slotFilter === 'all'
    ? slots
    : slots.filter((slot) => slotKey(slot) === slotFilter));

  const slotRowHtml = (slot) => {
    const editable = opts.editable && slot.artifact?.unit_id;
    return `<tr class="slotrow" data-slot="${esc(slot.slot_key)}">
      <td class="mono">${esc(slot.position)}</td>
      <td class="mono">${esc(slot.speaker || '—')}</td>
      <td class="src">${esc(slot.source || '')}</td>
      <td class="dst"><span class="dst-text">${esc(slot.artifact?.target || '')}</span></td>
      <td class="dst-cell-status">${slotStatus(slot)}</td>
      <td class="actions">${editable
        ? `<button type="button" class="rowbtn icon" data-edit-slot="${esc(slot.slot_key)}" title="就地修改这句译文">✎</button>`
        : ''}</td>
    </tr>`;
  };

  function syncMoreRow() {
    const row = container.querySelector('#site-more-row');
    if (!row) return;
    const rest = visibleSlots().length - shown;
    if (rest <= 0) {
      row.remove();
    } else {
      row.querySelector('button').textContent =
        `还有 ${num(rest)} 句没展开 · 点击继续（或滚到表格底部自动补）`;
    }
  }

  function ensureMoreRow() {
    if (visibleSlots().length <= CHUNK || container.querySelector('#site-more-row')) return;
    tbody.insertAdjacentHTML('beforeend',
      `<tr id="site-more-row"><td colspan="6" style="text-align:center;padding:10px 0;">
        <button type="button" class="gbtn" id="site-more"></button>
      </td></tr>`);
    syncMoreRow();
  }

  function appendSlots() {
    const list = visibleSlots();
    const rest = list.length - shown;
    if (rest <= 0) return;
    const next = list.slice(shown, shown + CHUNK);
    const moreRow = container.querySelector('#site-more-row');
    const html = next.map(slotRowHtml).join('');
    if (moreRow) moreRow.insertAdjacentHTML('beforebegin', html);
    else tbody.insertAdjacentHTML('beforeend', html);
    shown += next.length;
    syncMoreRow();
  }

  function applySlotFilter(next) {
    if (next === slotFilter) return;
    if (editing) {
      if (!confirm('有未保存的修改，放弃并继续？')) return;
      const prev = slotByKey(editing);
      if (prev) closeEdit(prev);
    }
    slotFilter = next;
    shown = 0;
    tbody.innerHTML = '';
    ensureMoreRow();
    appendSlots();
    renderSlotChips();
  }

  renderSlotChips();
  ensureMoreRow();
  appendSlots();

  const slotBox = container.querySelector('#site-slot-box');
  slotBox.addEventListener('scroll', () => {
    if (slotBox.scrollTop + slotBox.clientHeight >= slotBox.scrollHeight - 240) appendSlots();
  }, { passive: true });

  /* ---- 行内编辑：✎ → 原位变输入框；点了别处提醒未保存 ---- */
  let editing = null; // slot_key

  const slotByKey = (key) => slots.find((s) => s.slot_key === key);

  function rerenderRow(slot) {
    const row = container.querySelector(`.slotrow[data-slot="${CSS.escape(slot.slot_key)}"]`);
    if (!row) return;
    row.querySelector('.dst').innerHTML = `<span class="dst-text">${esc(slot.artifact?.target || '')}</span>`;
    row.querySelector('.dst-cell-status').innerHTML = slotStatus(slot);
    row.classList.remove('editing');
  }

  async function commitEdit(slot, box) {
    const target = box.querySelector('textarea').value;
    try {
      const saved = await opts.onSave(slot.artifact.unit_id, target);
      const verdict = saveVerdict(saved);
      slot.artifact.target = target;
      if (saved?.status) slot.artifact.status = saved.status;
      toast(verdict.text);
    } catch (err) {
      toast(`保存失败：${err.message}`);
    }
    editing = null;
    rerenderRow(slot);
    // 状态变了要跟上：计数重算；这句不再属于当前筛选（待复核改成可用）就从列表里撤掉
    renderSlotChips();
    if (slotFilter !== 'all' && slotKey(slot) !== slotFilter) {
      container.querySelector(`.slotrow[data-slot="${CSS.escape(slot.slot_key)}"]`)?.remove();
      shown -= 1;
      syncMoreRow();
    }
  }

  function closeEdit(slot) {
    editing = null;
    rerenderRow(slot);
  }

  container.querySelectorAll('[data-edit-slot]').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const key = btn.dataset.editSlot;
      if (editing === key) return;
      if (editing) {
        if (!confirm('有未保存的修改，放弃并继续？')) return;
        const prev = slotByKey(editing);
        if (prev) closeEdit(prev);
      }
      const slot = slotByKey(key);
      if (!slot) return;
      editing = key;
      const row = container.querySelector(`.slotrow[data-slot="${CSS.escape(key)}"]`);
      const dst = row.querySelector('.dst');
      dst.innerHTML = `<textarea rows="2" spellcheck="false">${esc(slot.artifact?.target || '')}</textarea>`
        + `<div class="row"><button type="button" class="rowbtn" data-save="1">保存</button>`
        + `<button type="button" class="rowbtn ghost" data-cancel="1">取消</button></div>`;
      row.classList.add('editing');
      dst.querySelector('textarea').focus();
    });
  });

  container.addEventListener('click', async (ev) => {
    const filterChip = ev.target.closest('[data-slot-filter]');
    if (filterChip) { applySlotFilter(filterChip.dataset.slotFilter); return; }
    if (ev.target.closest('[data-edit-slot]')) return;
    if (editing) {
      const slot = slotByKey(editing);
      const box = slot && container.querySelector(`.slotrow[data-slot="${CSS.escape(slot.slot_key)}"] .dst`);
      if (ev.target.closest('[data-save]') && slot && box) { await commitEdit(slot, box); return; }
      if (ev.target.closest('[data-cancel]') && slot) { closeEdit(slot); return; }
      if (!ev.target.closest('.slotrow.editing')) {
        if (!confirm('有未保存的修改，确定放弃吗？')) return;
        closeEdit(slot);
      }
    }
    if (ev.target.closest('#site-more')) {
      appendSlots();
      return;
    }
    if (ev.target.closest('#site-jump')) {
      opts.onJumpToWorklist?.(unit.unit_id || slots[0]?.artifact?.unit_id);
      return;
    }
    if (ev.target.closest('#site-back')) {
      opts.onJumpToGraph?.(unit.unit_id || slots[0]?.artifact?.unit_id);
      return;
    }
    if (ev.target.closest('#site-copy')) {
      const lines = [
        `# gametrans 单元：${unit.label || unit.path || '—'}`,
        `路径: ${unit.path || '—'}`,
        `类型: ${unit.kind || '—'}${unit.region ? ` · 区域 ${unit.region}` : ''}`,
        `身份: ${unit.unit_id || '—'}`,
        `句数: ${slots.length}`,
        '',
      ];
      for (const slot of slots) {
        lines.push(`[${slot.position}] ${slot.speaker || ''} ${slot.source}`);
        lines.push(`  → ${slot.artifact?.target || '（未翻译）'}`);
      }
      const ok = await copyText(lines.join('\n'));
      toast(ok ? '整个单元已复制，可以粘给 agent' : '复制失败（非安全上下文）');
      return;
    }
    const toggle = ev.target.closest('.calltoggle');
    if (toggle) {
      const call = calls.find((item) => item.id === toggle.dataset.call);
      const box = container.querySelector(`[data-box="${CSS.escape(toggle.dataset.call)}"]`);
      if (call && box) await toggleCall(box, call);
    }
  });
}
