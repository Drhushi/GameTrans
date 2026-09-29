/* API 客户端与能力探测。
 *
 * 后端按横切契约应在 /api/ping 里自报 capabilities（events / unit_detail /
 * translation_edit / reports / staleness / graph_edges）。老后端没有这个字段，
 * 就退到探测：能安全探测的（报告列表、事件流、图里的边）探一下，探测不了
 * 的一律按"没有"处理 —— 面板宁可少显示一个按钮，不显示一个点了报错的按钮。
 */

import { esc } from './util.js';

async function request(path, options) {
  const resp = await fetch(path, { cache: 'no-store', ...options });
  let body = null;
  try {
    body = await resp.json();
  } catch (_) {
    /* 响应不是 JSON，走下面的兜底错误 */
  }
  if (!resp.ok || (body && body.ok === false)) {
    const err = (body && body.error) || {};
    const e = new Error(err.message || `HTTP ${resp.status}`);
    e.hint = err.hint || null;
    e.status = resp.status;
    e.body = body;
    throw e;
  }
  return body;
}

export const api = (path) => request(path);

export const apiPost = (path, payload) =>
  request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  });

/** 全部已知能力位；探测只增不删（一次会话内后端不会退化）。 */
export const CAP_DEFS = [
  ['events', '实时事件流', 'R1'],
  ['graph_edges', '依赖边数据', 'R4'],
  ['unit_detail', '单元详情查询', 'R2'],
  ['translation_edit', '译文编辑保存', 'R3'],
  ['reports', '运行报告读取', 'R5'],
  ['staleness', '过期译文检测', 'R6'],
];

export async function probeCapabilities() {
  const caps = {
    events: false,
    graph_edges: false,
    unit_detail: false,
    translation_edit: false,
    reports: false,
    staleness: false,
  };
  try {
    const ping = await api('/api/ping');
    const declared = ping.capabilities;
    if (declared && typeof declared === 'object') {
      for (const key of Object.keys(caps)) {
        if (key in declared) caps[key] = !!declared[key];
      }
      // 「AI agent 接入」卡要按运行形态渲染 MCP 配置：冻结版给 executable，
      // 源码版给 source_root；老后端没有这两个字段，卡里退到通用写法。
      caps.frozen = !!ping.frozen;
      caps.executable = typeof ping.executable === 'string' ? ping.executable : '';
      caps.source_root = typeof ping.source_root === 'string' ? ping.source_root : '';
      return caps;
    }
  } catch (_) {
    return caps; // ping 都不通，后面轮询会再报错
  }

  // 老后端：能探的探一下
  try {
    const reports = await api('/api/reports');
    caps.reports = !!reports;
  } catch (_) { /* 没有就没有 */ }
  caps.events = await new Promise((resolve) => {
    let settled = false;
    const done = (v) => {
      if (!settled) { settled = true; resolve(v); }
    };
    try {
      const es = new EventSource('/api/events');
      const timer = setTimeout(() => { es.close(); done(false); }, 2500);
      es.onopen = () => { clearTimeout(timer); es.close(); done(true); };
      es.onerror = () => { clearTimeout(timer); es.close(); done(false); };
    } catch (_) {
      done(false);
    }
  });
  return caps;
}

/** 错误横幅内容：message + hint。 */
export function errBanner(err) {
  return `<b>${esc(err.message)}</b>${err.hint ? `<span class="hint">${esc(err.hint)}</span>` : ''}`;
}
