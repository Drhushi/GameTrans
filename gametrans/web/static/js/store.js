/* 全局状态：各接口的最新数据 + 订阅。
 *
 * 视图只读 store、按 slice 订阅；轮询调度器往里写。
 * slice 变化时只通知订阅了该 slice 的视图，没变的 slice 不触发重渲染 ——
 * 这就是"数据没变就不 patch"的源头。
 */

const listeners = new Map(); // slice -> Set<fn>
const state = new Map();     // slice -> value

export function get(slice) {
  return state.get(slice);
}

export function set(slice, value) {
  const prev = state.get(slice);
  const changed = JSON.stringify(prev) !== JSON.stringify(value);
  state.set(slice, value);
  if (changed) {
    for (const fn of listeners.get(slice) || []) fn(value, prev);
    for (const fn of listeners.get('*') || []) fn(slice, value, prev);
  }
  return changed;
}

export function on(slices, fn) {
  for (const s of slices) {
    if (!listeners.has(s)) listeners.set(s, new Set());
    listeners.get(s).add(fn);
  }
  return () => {
    for (const s of slices) listeners.get(s)?.delete(fn);
  };
}
