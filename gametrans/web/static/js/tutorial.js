/* 新手教程 —— 高亮逐步引导。
 *
 * 首次打开自动播放一次（localStorage.gt-tutorial-done，存不了就不自动播，
 * 免得每次都弹），设置页「界面」卡可随时重看。每一步先切到对应视图，再贴着
 * 目标控件锚一张步骤卡；遮罩用一个贴住目标的 spot 元素配超大 box-shadow 挖亮，
 * 一层元素顶掉四块遮罩拼接。几何计算都在 placeCard()（纯函数），错了也只是
 * 卡片摆歪，不碰数据。
 */

import { $ } from './util.js';

const DONE_KEY = 'gt-tutorial-done';
const VIEW_IDS = ['overview', 'graph', 'translations', 'resources', 'workbench', 'settings'];

/* 步骤表：view = 先切到哪个视图（null = 留在原地）；target = 要挖亮的控件
 * （null = 卡片居中）。目标全是 index.html 里的静态骨架，不依赖数据到没到。
 * 文案一句话说清「这页是干嘛的」，细节留给页面自己。 */
const STEPS = [
  {
    view: null,
    target: null,
    title: '欢迎使用 gametrans',
    body: '这是给 Ren\'Py / RPG Maker 游戏做 AI 辅助翻译的本地控制台：打开游戏工程 → 配好模型 → 整理术语书 → 翻译 → 校对写回。花一分钟认认各个页面。',
  },
  {
    view: 'overview',
    target: '#hero',
    title: '01 总览',
    body: '进度、流水线各段状态、花销与运行记录都从这看；点流水线任意一段能跳到对应页面。',
  },
  {
    view: 'graph',
    target: '#graph-canvas-box',
    title: '02 路径图',
    body: '翻译顺序由依赖图决定 —— 谁等谁、先翻哪场，图上一目了然。节点可拖拽缩放，点开看单元现场。',
  },
  {
    view: 'translations',
    target: '#unit-list',
    title: '03 译文',
    body: '翻译工作单：一行一场戏，点开逐句核对原文与译文，就地可改。',
  },
  {
    view: 'resources',
    target: '#card-termbook',
    title: '04 资源',
    body: '术语书是翻译质量的根：人名与设定先定下来，模型才不会各叫各的。改动的译名先进待审，确认才生效。',
  },
  {
    view: 'workbench',
    target: '#wb-ledger',
    title: '05 请求',
    body: '每次发给模型的请求都记在这：长什么样、花了多少、有没有被结构闸拦下。跑批顺不顺，来这查账。',
  },
  {
    view: 'settings',
    target: '#card-access',
    title: '06 设置',
    body: '在「模型接入」填接口地址与 API Key；翻译配置、引擎路径也在这一页。想重看本教程，就在上面的「界面」卡里。',
  },
  {
    view: 'settings',
    target: '#card-agent',
    title: '把 agent 接上',
    body: '扫描、翻译、写回由你的 AI agent 跑，面板看现场——没接 agent，翻译跑不起来。在「AI agent」卡复制 MCP 配置接给它；还没有 agent，照卡里的链接装一个（Codex、Claude Code 都行）。',
  },
  {
    view: null,
    target: null,
    title: '就这些',
    body: '回「总览」打开一个游戏工程就能开工。教程随时可以在 设置 → 界面 → 重看新手教程 找回来。',
  },
];

/* 纯几何：给目标矩形与卡片尺寸，算卡片落点。按 下→上→右→左 找第一个完全
 * 装得下的方位，都装不下就把首选方位 clamp 进视口。测试直接打这个函数。 */
export function placeCard(rect, cardW, cardH, vw, vh, pad = 14) {
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  const fits = (left, top) =>
    left >= 8 && top >= 8 && left + cardW <= vw - 8 && top + cardH <= vh - 8;
  const candidates = [
    ['bottom', cx - cardW / 2, rect.bottom + pad],
    ['top', cx - cardW / 2, rect.top - cardH - pad],
    ['right', rect.right + pad, cy - cardH / 2],
    ['left', rect.left - cardW - pad, cy - cardH / 2],
  ];
  for (const [place, left, top] of candidates) {
    if (fits(left, top)) return { place, left, top };
  }
  const [place, left, top] = candidates[0];
  return {
    place,
    left: Math.min(Math.max(8, left), Math.max(8, vw - cardW - 8)),
    top: Math.min(Math.max(8, top), Math.max(8, vh - cardH - 8)),
  };
}

function currentViewId() {
  const match = /^#\/([a-z]+)/.exec(location.hash || '');
  return match && VIEW_IDS.includes(match[1]) ? match[1] : 'overview';
}

let active = false;
let index = 0;
let root = null;
let spot = null;
let shield = null;
let card = null;
let els = {};

function build() {
  root = document.createElement('div');
  root.id = 'tour-root';
  root.hidden = true;
  root.innerHTML = `
    <div class="tour-shield"></div>
    <div class="tour-spot"></div>
    <div class="tour-card" role="dialog" aria-modal="true" aria-label="新手教程">
      <div class="tour-head">
        <span class="mono tour-no"></span>
        <b class="tour-title"></b>
      </div>
      <p class="tour-body"></p>
      <div class="tour-foot">
        <button class="ghost" type="button" data-tour="skip">跳过</button>
        <span class="tour-foot-right">
          <button class="ghost" type="button" data-tour="prev" hidden>上一步</button>
          <button class="gbtn" type="button" data-tour="next">下一步</button>
        </span>
      </div>
    </div>`;
  document.body.appendChild(root);
  spot = root.querySelector('.tour-spot');
  shield = root.querySelector('.tour-shield');
  card = root.querySelector('.tour-card');
  els = {
    no: root.querySelector('.tour-no'),
    title: root.querySelector('.tour-title'),
    body: root.querySelector('.tour-body'),
    prev: root.querySelector('[data-tour="prev"]'),
    next: root.querySelector('[data-tour="next"]'),
  };
  root.querySelector('[data-tour="skip"]').addEventListener('click', () => quit(false));
  els.prev.addEventListener('click', () => go(index - 1));
  els.next.addEventListener('click', () =>
    index === STEPS.length - 1 ? quit(true) : go(index + 1));
  // 遮罩底下滚页面会让目标跑出聚光圈：滚轮拦掉，万一还是滚了（滚动条/键盘）就重摆
  shield.addEventListener('wheel', (e) => e.preventDefault(), { passive: false });
  window.addEventListener('keydown', onKey);
  window.addEventListener('resize', reposition);
  window.addEventListener('scroll', reposition, true);
}

function onKey(event) {
  if (!active) return;
  const tag = event.target && event.target.tagName;
  if (event.key === 'Escape') {
    quit(false);
  } else if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
    return; // 正在打字不抢方向键
  } else if (event.key === 'ArrowRight') {
    go(index + 1);
  } else if (event.key === 'ArrowLeft') {
    go(index - 1);
  }
}

function reposition() {
  if (active) render();
}

function render() {
  const step = STEPS[index];
  els.no.textContent = `${index + 1} / ${STEPS.length}`;
  els.title.textContent = step.title;
  els.body.textContent = step.body;
  els.prev.hidden = index === 0;
  els.next.textContent = index === STEPS.length - 1 ? '完成' : '下一步';
  position(step);
}

function position(step) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const cw = card.offsetWidth;
  const ch = card.offsetHeight;
  const target = step.target ? document.querySelector(step.target) : null;
  if (!target) {
    // 无目标步骤：0 尺寸的 spot 摆在正中，超大 box-shadow 均匀压暗全屏
    spot.style.left = `${vw / 2}px`;
    spot.style.top = `${vh / 2}px`;
    spot.style.width = '0px';
    spot.style.height = '0px';
    card.style.left = `${Math.max(8, (vw - cw) / 2)}px`;
    card.style.top = `${Math.max(8, (vh - ch) / 2 - 24)}px`;
    return;
  }
  const pad = 6;
  // 目标可能在折叠线下（设置页就很长）：先滚到视口中央再量，聚光才指着看得见的东西
  const before = target.getBoundingClientRect();
  if (before.top < 0 || before.bottom > vh || before.top > vh) {
    target.scrollIntoView({ block: 'center', behavior: 'instant' });
  }
  const r = target.getBoundingClientRect();
  spot.style.left = `${r.left - pad}px`;
  spot.style.top = `${r.top - pad}px`;
  spot.style.width = `${r.width + pad * 2}px`;
  spot.style.height = `${r.height + pad * 2}px`;
  const pos = placeCard(r, cw, ch, vw, vh);
  card.style.left = `${pos.left}px`;
  card.style.top = `${pos.top}px`;
}

function go(i) {
  index = Math.max(0, Math.min(STEPS.length - 1, i));
  const step = STEPS[index];
  if (step.view && step.view !== currentViewId()) {
    // 切视图要等 hashchange 把视图点亮、布局走完一帧才能量矩形；连等两帧保险
    const afterRoute = () => {
      window.removeEventListener('hashchange', afterRoute);
      requestAnimationFrame(() => requestAnimationFrame(() => { if (active) render(); }));
    };
    window.addEventListener('hashchange', afterRoute);
    location.hash = `#/${step.view}`;
    return;
  }
  render();
}

function quit(finished) {
  active = false;
  root.hidden = true;
  delete document.documentElement.dataset.tour;
  try { localStorage.setItem(DONE_KEY, 'done'); } catch (_) { /* 存不了就算了 */ }
  if (finished && currentViewId() !== 'overview') location.hash = '#/overview';
}

/** 重看入口（设置页按钮）与首次自动播放共用。已在放就直接忽略。 */
export function startTour() {
  if (active) return;
  if (!root) build();
  index = 0;
  active = true;
  document.documentElement.dataset.tour = '1';
  root.hidden = false;
  go(0);
  els.next.focus();
}

/** 设置页「界面」卡里的「重看新手教程」按钮。 */
export function mountTutorial() {
  $('tutorial-replay')?.addEventListener('click', startTour);
}

/** 首访自动播放：能读 localStorage 且没放过才播 —— 存不了就永不自动播，
 * 否则用户每次打开都要再跳一次。 */
export function maybeAutoStart() {
  let seen;
  try { seen = localStorage.getItem(DONE_KEY); } catch (_) { return; }
  if (seen) return;
  setTimeout(startTour, 600); // 等首帧 settle，别跟首屏渲染抢场
}
