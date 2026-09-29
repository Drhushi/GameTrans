/* 小工具：转义、格式化、签名、复制。 */

export const esc = (v) =>
  String(v ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );

export const num = (v) => (typeof v === 'number' ? v.toLocaleString('zh-CN') : '—');

export const $ = (id) => document.getElementById(id);

/** 稳定的字符串签名：增量渲染用它判断"数据到底变没变"。 */
export function sig(value) {
  return JSON.stringify(value);
}

export function debounce(fn, ms) {
  let timer = 0;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    return false;
  }
}

/** 相对时间：刚刚 / n 秒前 / n 分钟前。 */
export function ago(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 5) return '刚刚';
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  return `${Math.round(s / 3600)} 小时前`;
}

/**
 * 提取"结构 token"（占位符 / 标签 / 方括号），供编辑器的即时校验对比原文与译文。
 * 这只是提示性检查，权威校验永远在后端保存闸门。
 */
export function structureTokens(text) {
  const s = String(text ?? '');
  const count = (re) => (s.match(re) || []).length;
  return {
    placeholders: count(/%(\([^)]*\))?[sdfr%]|\{[^{}]*\}/g),
    brackets: count(/\[[^\]]*\]/g),
    tags: count(/<\/?[a-zA-Z][^>]*>/g),
    newlines: count(/\n/g),
  };
}

export function tokenReport(source, target) {
  const a = structureTokens(source);
  const b = structureTokens(target);
  const rows = [
    ['占位符 / 花括号', a.placeholders, b.placeholders],
    ['方括号', a.brackets, b.brackets],
    ['标签', a.tags, b.tags],
    ['换行', a.newlines, b.newlines],
  ];
  return rows.map(([label, x, y]) => ({
    label,
    ok: x === y,
    text: `${label}：原文 ${x} · 译文 ${y}`,
  }));
}
