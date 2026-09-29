/* 渲染层：增量 patch + 脏表单保护 + 共享小部件。
 *
 * patch() 是整个面板不闪、不冲输入的关键：
 *   1. 签名没变 → 什么都不做（列表不闪跳）；
 *   2. 区域里有正在聚焦的输入控件 → 本轮跳过（用户输入优先于刷新），
 *      失焦后下一轮自动补上；
 *   3. 否则整体替换 innerHTML。
 */

import { esc } from './util.js';

export function patch(el, html) {
  if (!el) return false;
  if (el.dataset.sig === html) return false;
  const active = document.activeElement;
  if (
    active &&
    el.contains(active) &&
    (active.tagName === 'INPUT' ||
      active.tagName === 'TEXTAREA' ||
      active.tagName === 'SELECT')
  ) {
    return false; // 正在输入的区域不碰
  }
  el.innerHTML = html;
  el.dataset.sig = html;
  return true;
}

export function table(el, headers, rows, { rowAttr = '' } = {}) {
  if (!rows.length) {
    patch(el, `<tbody><tr><td class="empty">没有匹配的条目。</td></tr></tbody>`);
    return;
  }
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join('');
  patch(el, `<thead><tr>${head}</tr></thead><tbody>${rows.join('')}</tbody>`);
}

export function statusTag(status) {
  const labels = {
    ok: ['可用', 'st-ok'],
    usable: ['可用', 'st-ok'],
    needs_review: ['待复核', 'st-warn'],
    failed: ['失败', 'st-err'],
    skipped: ['跳过', 'st-dim'],
    unbound: ['未挂单位', 'st-dim'],
    untranslated: ['未翻译', 'st-dim'],
    unknown: ['未知', 'st-dim'],
    // 知识条目的状态（写在术语表 / 世界书的条目自己身上）
    approved: ['生效', 'st-ok'],
    pending_validation: ['待审', 'st-warn'],
    rejected: ['已否决', 'st-err'],
  };
  const [label, cls] = labels[status] || [status, 'st-dim'];
  return `<span class="mono ${cls}">${esc(label)}</span>`;
}

/** 开销（token 估算）的数值格：数字本身是读数，颜色只帮一眼分出量级。 */
export function tokenPill(tokens) {
  const n = Number(tokens);
  if (!Number.isFinite(n)) return '<span class="pill cost-low">—</span>';
  const cls = n >= 400 ? 'cost-high' : n >= 120 ? 'cost-mid' : 'cost-low';
  return `<span class="pill ${cls}">${Math.round(n)}</span>`;
}

let toastTimer = 0;
export function toast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 2600);
}

/** 单条译文 / 单元共用的"原文 → 结构检查"提示块。 */
export function checksHtml(rows) {
  if (!rows || !rows.length) return '';
  return `<div class="checks">${rows
    .map(
      (r) =>
        `<span class="c ${r.ok ? 'ok' : 'bad'}">${r.ok ? '✓' : '✗'} ${esc(r.text)}</span>`
    )
    .join('')}</div>`;
}

/** 后端校验结果 → 提示块（Artifact.validation 的形状后端说了算，这里防御式读取）。 */
export function validationRows(validation) {
  if (!validation) return [];
  const violations = validation.violations || validation.constraints || [];
  if (Array.isArray(violations) && violations.length) {
    return violations.map((v) => ({
      ok: false,
      text: [v.type, v.message].filter(Boolean).join('：') || JSON.stringify(v),
    }));
  }
  if (validation.ok === false) {
    return [{ ok: false, text: validation.message || '校验未通过' }];
  }
  if (validation.ok === true) return [{ ok: true, text: '结构校验通过' }];
  return [];
}

/** 保存译文之后的结论。
 *
 * 后端把"存下来了"与"过闸门了"分成两件事：没过结构校验的条目**仍然返回 200 并落盘**
 * （用户写的字不该因为没过闸门就被吞掉），但带着 status: "needs_review" 与逐条
 * violations 回来。所以面板不能只看请求成功就说"校验通过" —— 那等于替后端签一张
 * 它没签的合格证。
 */
export function saveVerdict(artifact) {
  const validation = artifact?.validation;
  if (artifact?.status === 'ok' || validation?.ok === true) {
    return { ok: true, text: '已保存，校验通过。' };
  }
  const reasons = (validation?.violations || []).map((v) => v.message).filter(Boolean);
  const why = reasons.length ? reasons.join('；') : artifact?.error || '结构校验未通过';
  return { ok: false, text: `已保存，但未通过校验（不会写回）：${why}` };
}
