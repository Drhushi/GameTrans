/* 桌面壳标题栏 —— 无边框窗口的自绘替代品。
 *
 * window.pywebview 只在 pywebview 壳里注入（scripts/open_panel.py）：探到就显示
 * 标题栏并给 body 打 has-titlebar，浏览器里永远不出现 —— 同一份前端两种形态。
 * 窗口操作走 window.pywebview.api（壳里的 _ShellApi）；拖动区是 pywebview 认的
 * pywebview-drag-region 类。无边框窗口没有系统缩放边缘，右 / 下 / 角三条
 * 缩放热区在这里补上（屏幕坐标取增量，鼠标跑出窗口事件也不断流）。
 */

import { $ } from './util.js';

const EDGE = 6; // 缩放热区厚度（px），贴着窗口右缘 / 下缘

function shellApi() {
  return window.pywebview?.api ?? null;
}

function bindButtons() {
  $('titlebar-min').addEventListener('click', () => shellApi().minimize());
  $('titlebar-max').addEventListener('click', () => shellApi().toggle_maximize());
  $('titlebar-close').addEventListener('click', () => shellApi().close());
  // 双击空白处 = 最大化 / 还原，跟系统标题栏一个手感
  $('titlebar').addEventListener('dblclick', (ev) => {
    if (ev.target.closest('.tb-btn')) return;
    shellApi().toggle_maximize();
  });
}

function bindEdgeResize() {
  const edges = [
    { cls: 'gt-resize-e', cursor: 'ew-resize', dx: 1, dy: 0 },
    { cls: 'gt-resize-s', cursor: 'ns-resize', dx: 0, dy: 1 },
    { cls: 'gt-resize-se', cursor: 'nwse-resize', dx: 1, dy: 1 },
  ];
  for (const { cls, cursor, dx, dy } of edges) {
    const el = document.createElement('div');
    el.className = cls;
    el.style.cursor = cursor;
    el.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      const sx = ev.screenX;
      const sy = ev.screenY;
      const move = (m) =>
        shellApi().resize_by(dx ? m.screenX - sx : 0, dy ? m.screenY - sy : 0);
      const up = () => {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', up);
      };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    });
    document.body.appendChild(el);
  }
}

function show() {
  // macOS 保留系统标题栏（左上角原生红绿灯），页内这条自绘的不出现
  if (window.pywebview.platform === 'cocoa') return;
  if (!shellApi()) return;
  const bar = $('titlebar');
  if (!bar) return;
  bar.hidden = false;
  document.body.classList.add('has-titlebar');
  bindButtons();
  bindEdgeResize();
}

// pywebview 的注入不赶首帧：轮询到 api 出现为止（约 5 秒），探到就停。
let tries = 0;
const timer = setInterval(() => {
  if (shellApi()) {
    clearInterval(timer);
    show();
  } else if (++tries > 50) {
    clearInterval(timer); // 浏览器模式：window.pywebview 永远不来
  }
}, 100);
