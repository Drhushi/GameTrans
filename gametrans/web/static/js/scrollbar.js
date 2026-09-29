/* 覆盖式滚动条：原生滚动条归零后内容会顶到窗口边（不留空槽），这根自绘
 * 滑块浮在内容上，只在滚动时显形 —— 滚轮滚到哪它就在哪，停下即隐。
 * 只做指示不做拖拽（pointer-events: none），滚动条本身滚轮就够用。
 */

const IDLE_MS = 900; // 停滚多久后隐去

let timer = 0;

function bar() {
  return document.getElementById('gt-scrollbar');
}

function update() {
  const el = bar();
  if (!el) return;
  const de = document.documentElement;
  const scrollable = de.scrollHeight - de.clientHeight;
  if (scrollable <= 1) {
    el.classList.remove('on');
    return;
  }
  // 标题栏是 fixed 的，滑块从它下面开始，别压到标题栏上
  const top = de.clientHeight ? (de.scrollTop / de.scrollHeight) * de.clientHeight : 0;
  const height = Math.max(40, (de.clientHeight / de.scrollHeight) * de.clientHeight);
  el.style.top = `${top}px`;
  el.style.height = `${Math.min(height, de.clientHeight - top - 4)}px`;
  el.classList.add('on');
  clearTimeout(timer);
  timer = setTimeout(() => el.classList.remove('on'), IDLE_MS);
}

export function armScrollbar() {
  if (bar()) return;
  const el = document.createElement('div');
  el.id = 'gt-scrollbar';
  document.body.appendChild(el);
  window.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update);
  // 视图切换 / 数据刷新会改内容高度：尺寸一变就重算
  new ResizeObserver(update).observe(document.body);
  update();
}
