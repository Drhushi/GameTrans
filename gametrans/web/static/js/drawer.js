/* 右侧抽屉：单元详情的壳。路径图点节点、译文行展开都往这里放。 */

import { esc } from './util.js';
import { renderUnitDetail } from './detail.js';
import { renderUnitSite } from './unitdetail.js';

let closeHandler = null;

export function closeDrawer() {
  const root = document.getElementById('drawer-root');
  root.innerHTML = '';
  if (closeHandler) {
    window.removeEventListener('keydown', closeHandler);
    closeHandler = null;
  }
}

/**
 * 打开抽屉并渲染单元详情。
 * item: 路径图节点载荷或译文条目；artifact: 译文样本条目（可空）。
 * opts: {editable, onSave(unitId, target)}
 */
export function openUnitDrawer(item, artifact, opts = {}) {
  const root = document.getElementById('drawer-root');
  closeDrawer();
  root.innerHTML = `
    <div class="drawer-mask" id="drawer-mask"></div>
    <aside class="drawer" role="dialog" aria-label="单元详情">
      <div class="drawer-head">
        <h3>${esc(item.path || item.unit_id || '—')}</h3>
        <button class="x" id="drawer-close" type="button">ESC</button>
      </div>
      <div class="drawer-body" id="drawer-body"></div>
    </aside>`;
  renderUnitDetail(root.querySelector('#drawer-body'), item, artifact, opts);

  closeHandler = (ev) => {
    if (ev.key === 'Escape') closeDrawer();
  };
  window.addEventListener('keydown', closeHandler);
  root.querySelector('#drawer-close').addEventListener('click', closeDrawer);
  root.querySelector('#drawer-mask').addEventListener('click', closeDrawer);
}

/**
 * 打开抽屉并渲染**单元现场**（图里点节点的入口）。
 *
 * payload 来自 ``/api/units/<结点>``：这个单元的每一句（原文/译文/状态）
 * 与碰过它的每一次请求。与 :func:`openUnitDrawer` 分开：那个是"单条译文"的旧样子，
 * 仍被译文收件箱用着。
 */
export function openUnitSite(payload, opts = {}) {
  const root = document.getElementById('drawer-root');
  closeDrawer();
  const unit = payload?.unit || {};
  root.innerHTML = `
    <div class="drawer-mask" id="drawer-mask"></div>
    <aside class="drawer" role="dialog" aria-label="单元现场">
      <div class="drawer-head">
        <h3>${esc(unit.label || unit.path || '单元')} <span class="hint">${(payload?.slots || []).length} 句</span></h3>
        <button class="x" id="drawer-close" type="button">ESC</button>
      </div>
      <div class="drawer-body" id="drawer-body"></div>
    </aside>`;
  renderUnitSite(root.querySelector('#drawer-body'), payload, opts);

  closeHandler = (ev) => {
    if (ev.key === 'Escape') closeDrawer();
  };
  window.addEventListener('keydown', closeHandler);
  root.querySelector('#drawer-close').addEventListener('click', closeDrawer);
  root.querySelector('#drawer-mask').addEventListener('click', closeDrawer);
}
