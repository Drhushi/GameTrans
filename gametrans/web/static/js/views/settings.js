/* 06 设置：模型接入（provider + 凭证）、翻译配置（微调项收进折叠）、引擎 SDK、
 * 指令台（原独立「指令」页并入：一张只读的命令参考清单，收在折叠里）。
 *
 * 交互上的三条硬规矩：
 *   1. 字段的含义（标签 / 说明 / 可选值 / 下限）由后端 /api/config 的 fields 给，
 *      前端不维护第二份 —— 校验在后端，含义也该由后端说；说明走悬停提示，不占版面；
 *   2. 有未保存改动就亮出操作条（保存 / 放弃），暂停自动刷新，离开页面前提醒；
 *   3. 正在输入的那一格永不被自动刷新覆盖（行为由 tests/test_panel_dom.py 钉住）。
 */

import { esc, $, copyText } from '../util.js';
import { toast } from '../render.js';
import { get, on } from '../store.js';
import { apiPost, api } from '../api.js';

/* ---------- 主题 ---------- */

export function applyTheme(pref) {
  const dark =
    pref === 'dark' ||
    (pref === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.pref = pref;
  try {
    localStorage.setItem('gt-theme', pref);
  } catch (_) { /* 隐私模式存不进去就只在会话内生效 */ }
  // 设置页「界面」卡里的档位按钮跟着亮
  document.querySelectorAll('#ui-theme [data-theme-pref]').forEach((btn) => {
    btn.classList.toggle('on', btn.dataset.themePref === pref);
  });
}

/* ---------- 配置表单：字段含义来自后端 fields ---------- */

let configDirty = false;
let configSnapshot = null; // 最近一次 /api/config 的完整响应；保存前的本地校验要读 min

function fieldHtml(key, value, meta) {
  const id = `config-field-${key}`;
  const label = meta?.label || key;
  // 说明进悬停提示：字段含义查得到，但不占版面
  const help = meta?.help ? ` title="${esc(meta.help)}"` : '';
  const type = meta?.type || (typeof value === 'boolean' ? 'bool' : typeof value);
  if (type === 'bool') {
    return `<label class="field check"${help}>
      <input type="checkbox" id="${id}" data-key="${esc(key)}" data-type="bool" ${value ? 'checked' : ''}>
      <span>${esc(label)}</span>
    </label>`;
  }
  if (meta?.choices?.length) {
    const options = meta.choices.map(String);
    // 当前值不在可选集里时保留显示：用户配过的值不能因为渲染就悄悄丢
    if (value != null && String(value) !== '' && !options.includes(String(value))) {
      options.unshift(String(value));
    }
    return `<label class="field"${help}><span>${esc(label)}</span>
      <select id="${id}" data-key="${esc(key)}" data-type="choice">
        ${options.map((c) => `<option value="${esc(c)}" ${c === String(value) ? 'selected' : ''}>${esc(c)}</option>`).join('')}
      </select></label>`;
  }
  if (type === 'text') {
    return `<label class="field wide-field"${help}><span>${esc(label)}</span>
      <textarea id="${id}" data-key="${esc(key)}" data-type="text" rows="2">${esc(value ?? '')}</textarea></label>`;
  }
  const min = meta?.min != null ? ` min="${esc(meta.min)}"` : '';
  return `<label class="field"${help}><span>${esc(label)}</span>
    <input type="${type === 'int' || type === 'number' ? 'number' : 'text'}" id="${id}" data-key="${esc(key)}" data-type="${esc(type)}" value="${esc(value ?? '')}"${min} spellcheck="false"></label>`;
}

function buildConfigGroupsHtml(body) {
  const config = body.config || {};
  const fields = body.fields || {};
  // 默认值对所有人都够用的微调项标了 advanced：整组收进「更多设置」折叠
  const groups = { basic: '', advanced: '' };
  for (const group of body.groups || []) {
    const rendered = group.keys
      .filter((key) => key in config)
      .map((key) => fieldHtml(key, config[key], fields[key]))
      .join('');
    if (!rendered) continue;
    const advanced = group.keys.every((key) => fields[key]?.advanced);
    const html = `<div class="config-group"><h3>${esc(group.title)}</h3><div class="fields">${rendered}</div></div>`;
    groups[advanced ? 'advanced' : 'basic'] += html;
  }
  return groups;
}

function syncConfigValues(config) {
  document.querySelectorAll('#card-config [data-key]').forEach((el) => {
    if (el === document.activeElement) return; // 正在输入的那一格不碰
    const value = config[el.dataset.key];
    if (el.dataset.type === 'bool') el.checked = !!value;
    else if (String(el.value) !== String(value ?? '')) el.value = value ?? '';
  });
}

function renderConfigGroups(body) {
  configSnapshot = body;
  const container = $('config-groups');
  if (!configDirty) {
    const signature = JSON.stringify((body.groups || []).map((g) => [g.title, g.keys]));
    if (container.dataset.signature !== signature) {
      const groups = buildConfigGroupsHtml(body);
      container.innerHTML = groups.basic;
      $('config-advanced-groups').innerHTML = groups.advanced;
      $('config-advanced').hidden = !groups.advanced;
      container.dataset.signature = signature;
    } else {
      syncConfigValues(body.config || {});
    }
  }
  // 每次数据到达都复核一次：误报的脏状态（重载恢复、改回原值）在这里自愈
  if (configDirty && !configDiffersFromSaved()) {
    configDirty = false;
    $('config-result').textContent = '';
  }
  $('config-dirtybar').hidden = !configDirty;
  const result = $('config-result');
  if (!configDirty && body.warnings && body.warnings.length && !result.textContent.startsWith('已保存')) {
    result.textContent = body.warnings.join('；');
  }
}

/** 表单当前值和最近一次服务端值是否真的不同。
 * 脏不脏由"值 diff"说了算，不由"听到过输入"说了算 —— 浏览器重载后会恢复
 * 表单值并补发 input 事件，用户把值改回原样也不算未保存。 */
function configDiffersFromSaved() {
  const saved = configSnapshot?.config || {};
  let differs = false;
  document.querySelectorAll('#card-config [data-key]').forEach((el) => {
    const current = el.dataset.type === 'bool' ? String(el.checked) : String(el.value ?? '');
    const base = el.dataset.type === 'bool'
      ? String(!!saved[el.dataset.key])
      : String(saved[el.dataset.key] ?? '');
    if (current !== base) differs = true;
  });
  return differs;
}

function onConfigEdit() {
  configDirty = configDiffersFromSaved();
  $('config-dirtybar').hidden = !configDirty;
  $('config-result').textContent = '';
}

/** 数字字段的本地校验：返回出错原因，合法返回 null。 */
function numberProblem(el) {
  const raw = String(el.value).trim();
  if (raw === '') return '要填个数';
  const n = Number(raw);
  if (!Number.isFinite(n)) return '要是个数';
  const min = configSnapshot?.fields?.[el.dataset.key]?.min;
  if (min != null && n < min) return `不能小于 ${min}`;
  return null;
}

async function saveConfig() {
  const result = $('config-result');
  // 先本地把数字拦一遍：留空 / 非数 / 低于下限的就地标红，不发请求
  const problems = [];
  document.querySelectorAll('#card-config [data-key]').forEach((el) => {
    el.closest('.field')?.classList.remove('invalid');
    if (el.dataset.type === 'int' || el.type === 'number') {
      const why = numberProblem(el);
      if (why) {
        el.closest('.field')?.classList.add('invalid');
        const label = configSnapshot?.fields?.[el.dataset.key]?.label || el.dataset.key;
        problems.push(`「${label}」${why}`);
      }
    }
  });
  if (problems.length) {
    result.textContent = `还没改对：${problems.join('；')}`;
    document
      .querySelector('#config-groups .field.invalid input, #config-groups .field.invalid select')
      ?.focus();
    return;
  }
  result.textContent = '保存中…';
  try {
    const payload = {};
    document.querySelectorAll('#config-groups [data-key]').forEach((el) => {
      const key = el.dataset.key;
      if (el.dataset.type === 'bool') payload[key] = el.checked;
      else if (el.dataset.type === 'text') payload[key] = el.value;
      else if (el.dataset.type === 'int' || el.type === 'number') payload[key] = Number(el.value);
      else payload[key] = el.value;
    });
    await apiPost('/api/config', payload);
    configDirty = false;
    renderConfigGroups(await api('/api/config'));
    result.textContent = '已保存。';
    toast('配置已保存');
  } catch (error) {
    result.textContent = `保存失败：${error.message}`;
  }
}

function discardConfig() {
  configDirty = false;
  const body = get('config');
  if (body) renderConfigGroups(body);
  $('config-result').textContent = '已放弃未保存的更改。';
}

/* ---------- 模型接入：provider + 凭证，一次存完 ---------- */

let accessDirty = false;

function renderAccessProvider(body) {
  const select = $('access-provider');
  const meta = body.fields?.provider || { choices: ['openai', 'mock'] };
  const current = body.config?.provider ?? '';
  const options = meta.choices.map(String);
  if (current && !options.includes(String(current))) options.unshift(String(current));
  const html = options
    .map((c) => `<option value="${esc(c)}" ${c === String(current) ? 'selected' : ''}>${esc(c)}</option>`)
    .join('');
  if (select.dataset.sig !== html) {
    select.innerHTML = html;
    select.dataset.sig = html;
  }
  if (document.activeElement !== select) select.value = String(current ?? '');
}

function renderCredentialView(body) {
  const cred = body.credentials || {};
  const detail = [];
  detail.push(cred.configured ? `已配置（${cred.api_key}）` : '未配置');
  if (cred.base_url) detail.push(cred.base_url);
  if (cred.model) detail.push(cred.model);
  if (cred.env_override) detail.push('环境变量已设置，优先级高于这里');
  const el = $('credential-detail');
  el.textContent = detail.join(' ｜ ');
  el.classList.toggle('ok', !!cred.configured);

  const baseUrl = $('credential-base-url');
  const model = $('credential-model');
  if (document.activeElement !== baseUrl) baseUrl.value = cred.base_url || '';
  if (document.activeElement !== model) model.value = cred.model || '';
}

function renderAccessIdentity(cred) {
  const keyInput = $('credential-api-key');
  if (document.activeElement !== keyInput) {
    keyInput.placeholder = cred.configured ? '留空 = 不修改；换新钥匙就填新的' : 'sk-…';
  }
  $('credential-clear').hidden = !(cred.configured && !cred.env_override);
}

function renderAccess(body) {
  renderAccessProvider(body);
  renderCredentialView(body);
  renderAccessIdentity(body.credentials || {});
}

/** 接入卡同理：密钥打了字就算改；其余三项与服务端值比对。 */
function accessDiffersFromSaved() {
  if ($('credential-api-key').value.trim() !== '') return true;
  const saved = configSnapshot || {};
  const cred = saved.credentials || {};
  const config = saved.config || {};
  return $('access-provider').value !== String(config.provider ?? '')
    || $('credential-base-url').value.trim() !== String(cred.base_url ?? '')
    || $('credential-model').value.trim() !== String(cred.model ?? '');
}

function onAccessEdit() {
  accessDirty = accessDiffersFromSaved();
  $('credential-result').textContent = accessDirty ? '有未保存的更改。' : '';
}

async function saveCredentials() {
  const keyInput = $('credential-api-key');
  const result = $('credential-result');
  const payload = {
    provider: $('access-provider').value,
    base_url: $('credential-base-url').value.trim(),
    model: $('credential-model').value.trim(),
  };
  const typedKey = keyInput.value.trim();
  if (typedKey) payload.api_key = typedKey; // 留空 = 不动已存的；清除走专门的按钮
  result.textContent = '保存中…';
  try {
    await apiPost('/api/config', payload);
    accessDirty = false;
    keyInput.value = '';
    renderAccess(await api('/api/config'));
    result.textContent = '已保存。';
    toast('接入设置已保存');
  } catch (error) {
    result.textContent = `保存失败：${error.message}`;
  }
}

async function clearCredentialKey() {
  if (!confirm('确定清除已保存的 API Key？环境变量若已设置，仍会继续生效。')) return;
  const result = $('credential-result');
  result.textContent = '正在清除…';
  try {
    await apiPost('/api/config', { api_key: '' });
    accessDirty = false;
    renderAccess(await api('/api/config'));
    result.textContent = '已清除。';
    toast('已清除密钥');
  } catch (error) {
    result.textContent = `清除失败：${error.message}`;
  }
}

/* ---------- 引擎 SDK ---------- */

function renderToolchain(status) {
  const chain = status?.toolchain;
  if (!chain) return;
  const detail = $('toolchain-detail');
  const parts = [chain.detail || ''];
  if (chain.version) parts.push(`版本 ${chain.version}`);
  detail.textContent = parts.filter(Boolean).join(' ｜ ');
  detail.classList.toggle('ok', !!chain.usable);
  $('engine-badge').textContent = `引擎：${status.engine || '—'}`;
  // 不需要外部工具链的引擎（如 RPGM）不摆 SDK 输入框；
  // 老后端没报 required 时保守显示，别把能填的入口藏掉
  $('engine-sdk-row').hidden = chain.required === false;
  const input = $('engine-sdk-path');
  if (chain.path && input.value === '') input.value = chain.path;
}

async function saveEngineSdk() {
  const input = $('engine-sdk-path');
  const result = $('engine-option-result');
  result.textContent = '保存中…';
  try {
    const payload = await apiPost('/api/engine-option', {
      key: 'sdk_path',
      value: input.value.trim(),
    });
    renderToolchain({ toolchain: payload.toolchain, engine: payload.engine });
    result.textContent = payload.toolchain?.usable
      ? '已保存，官方工具可用。'
      : '已保存，但官方工具还不可用 —— 检查路径是否指向含 renpy/ 与 lib/ 的那一层。';
  } catch (error) {
    result.textContent = `保存失败：${error.message}`;
  }
}

/* ---------- 检查更新（面板唯一一次主动出网，点按钮才发） ---------- */

async function checkUpdate() {
  const box = $('update-result');
  const button = $('update-check');
  button.disabled = true;
  box.textContent = '正在问 GitHub…';
  try {
    // 后端约定：信封永远 ok:true，检查结果看 checked —— 查不到给 reason 一句人话
    const body = await api('/api/update-check');
    if (!body.checked) {
      box.textContent = body.reason || '检查不了更新。';
    } else if (body.update_available) {
      box.innerHTML = `有新版 ${esc(body.latest)}（当前 v${esc(body.current)}）—— `
        + `<a href="${esc(body.url)}" target="_blank" rel="noreferrer">打开下载页</a>，`
        + '下载后覆盖安装，数据不丢。';
    } else {
      box.textContent = `已是最新（v${esc(body.current)}）。`;
    }
  } catch (error) {
    box.textContent = `检查不了更新：${error.message || error}`;
  } finally {
    button.disabled = false;
  }
}

/* ---------- AI agent 接入（面板唯一该劝人装东西的地方） ---------- */

/** MCP 配置形状随运行形态变：冻结版直接指 exe（GameTrans.exe mcp），
 * 源码版用 python -m gametrans mcp + cwd=检出根。字段来自 /api/ping。 */
function mcpConfig(caps) {
  if (caps?.frozen && caps?.executable) {
    return { mcpServers: { gametrans: { command: caps.executable, args: ['mcp'] } } };
  }
  return {
    mcpServers: {
      gametrans: {
        command: 'python',
        args: ['-m', 'gametrans', 'mcp'],
        cwd: caps?.source_root || '/path/to/GameTrans',
      },
    },
  };
}

function renderAgentCard() {
  const caps = get('caps');
  const box = $('agent-mcp-json');
  if (!box) return;
  const config = mcpConfig(caps);
  box.textContent = JSON.stringify(config, null, 2);
  // 两家 agent 的现成接法（语法对过官方文档；Windows 路径 Claude Code 吃反斜杠，用正斜杠）
  const { command } = config.mcpServers.gametrans;
  const args = config.mcpServers.gametrans.args.join(' ');
  $('agent-mcp-how').innerHTML =
    `已有 agent：Codex → <code>codex mcp add gametrans -- ${esc(command)} ${esc(args)}</code> ｜ `
    + `Claude Code → <code>claude mcp add --scope user gametrans -- ${esc(command)} ${esc(args)}</code>。`
    + '或把上面的 JSON 贴进它的配置文件（Codex 在 ~/.codex/config.toml，形状同上）。';
}

async function copyAgentConfig() {
  const ok = await copyText($('agent-mcp-json').textContent || '');
  toast(ok ? '已复制 MCP 配置' : '复制失败，手动选中那段 JSON 复制');
}

/* ---------- 项目：就地切到另一个游戏目录 ---------- */

/** 桌面壳（pywebview）里才有原生目录选择框；注入不赶首帧，事件与轮询都会等到。 */
function revealProjectPick() {
  if (window.pywebview?.api) $('project-pick').hidden = false;
}

async function pickProjectFolder() {
  try {
    const picked = await window.pywebview.api.pick_folder();
    if (picked) $('project-path-input').value = String(picked);
  } catch (_) { /* 弹框被取消：留着用户手填 */ }
}

async function switchProject(path) {
  const result = $('project-result');
  const target = (path ?? $('project-path-input').value).trim();
  if (!target) {
    result.textContent = '先填游戏目录、点「浏览…」选一个，或从上面「最近打开」里点一个。';
    return;
  }
  result.textContent = '切换中…';
  try {
    await apiPost('/api/project', { path: target });
    location.reload(); // 整页重载：所有 slice 都是旧项目的了
  } catch (error) {
    result.textContent = `切换失败：${error.message}`;
  }
}

/** 「最近打开」一键切换：列表来自 /api/projects（launcher 与切换共用的记忆）。
 * 拉不到就悄悄留空 —— 老后端没有这个接口，不该在设置页挂出一块死面板。 */
async function loadRecentProjects() {
  const el = $('project-recent');
  if (!el) return;
  try {
    const body = await api('/api/projects');
    const projects = body.projects || [];
    const current = get('status')?.project_root || '';
    if (!projects.length) {
      el.innerHTML = '<span class="hint">还没有记录；打开过项目后，这里会列出最近用过的。</span>';
      return;
    }
    el.innerHTML = projects
      .map((p) => {
        const on = p.path === current;
        return `<button type="button" class="gbtn${on ? ' on' : ''}" data-project-path="${esc(p.path)}" title="${esc(p.path)}"${on ? ' disabled' : ''}>${esc(p.name)}</button>`;
      })
      .join('');
  } catch (_) { /* 没有这个接口就不显示 */ }
}

/* ---------- 指令台（原「指令」页）：全部 CLI / MCP 操作，点击复制命令 ---------- */

function renderOperations(operations) {
  const prefix = 'gametrans ';
  const el = $('operations');
  if (!el) return;
  el.innerHTML = operations
    .map((op) => {
      const tags = [
        op.mutates
          ? '<span class="op-tag op-rw">写</span>'
          : '<span class="op-tag op-ro">只读</span>',
        op.mutates && op.panel_callable
          ? '<span class="op-tag op-panel">面板</span>'
          : '',
      ].join('');
      const cmd = prefix + op.name.replace(/\./g, ' ');
      return `<div class="op" data-cmd="${esc(cmd)}" title="点击复制 CLI 命令">
        <div class="op-name">${esc(op.name)}${tags}</div>
        <div class="op-sum">${esc(op.summary)}</div>
        <div class="op-cmd">$ ${esc(cmd)}</div>
      </div>`;
    })
    .join('');
}

export const view = {
  id: 'settings',
  slices: ['config', 'status', 'operations'],

  mount() {
    // 项目卡：最近打开一键切换；浏览按钮只有桌面壳里才有；切换失败就地把原因写出来
    $('project-recent').addEventListener('click', (ev) => {
      const chip = ev.target.closest('[data-project-path]');
      if (chip && !chip.disabled) switchProject(chip.dataset.projectPath);
    });
    revealProjectPick();
    window.addEventListener('pywebviewready', revealProjectPick);
    $('project-pick').addEventListener('click', pickProjectFolder);
    $('project-switch').addEventListener('click', () => switchProject());
    $('project-path-input').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') switchProject();
    });
    // 引擎
    $('engine-option-save').addEventListener('click', saveEngineSdk);
    // 检查更新（面板唯一一次主动出网，点按钮才发）
    $('update-check').addEventListener('click', checkUpdate);
    // AI agent 接入：配置随 caps 到手渲染（caps 只探一遍）；mount 时先渲染一次兜底
    $('agent-mcp-copy').addEventListener('click', copyAgentConfig);
    on(['caps'], renderAgentCard);
    renderAgentCard();
    // 指令台：点卡片复制 CLI 命令
    $('operations')?.addEventListener('click', async (ev) => {
      const card = ev.target.closest('.op');
      if (!card || !card.dataset.cmd) return;
      const ok = await copyText(card.dataset.cmd);
      toast(ok ? `已复制：${card.dataset.cmd}` : card.dataset.cmd);
    });
    // 翻译配置：改动攒着，操作条上一次保存 / 放弃（监听整卡，折叠区也算）
    $('config-save').addEventListener('click', saveConfig);
    $('config-discard').addEventListener('click', discardConfig);
    const configCard = $('card-config');
    configCard.addEventListener('input', onConfigEdit);
    configCard.addEventListener('change', onConfigEdit);
    // 模型接入：provider + 凭证一起存
    $('credential-save').addEventListener('click', saveCredentials);
    $('credential-clear').addEventListener('click', clearCredentialKey);
    const accessCard = $('card-access');
    accessCard.addEventListener('input', onAccessEdit);
    accessCard.addEventListener('change', onAccessEdit);
    // 有未保存的改动时，别让用户一关页签就把改动丢了
    window.addEventListener('beforeunload', (ev) => {
      if (configDirty || accessDirty) {
        ev.preventDefault();
        ev.returnValue = '';
      }
    });
  },

  render() {
    const config = get('config');
    if (config) {
      renderConfigGroups(config);
      if (!accessDirty) renderAccess(config);
    }
    const status = get('status');
    if (status) {
      renderToolchain(status);
      $('project-current').textContent = status.project_root || '—';
      // 「最近打开」只在 status 到手后拉一次：高亮"当前开着的项目"要用它
      const recent = $('project-recent');
      if (status.project_root && !recent.dataset.done) {
        recent.dataset.done = '1';
        loadRecentProjects();
      }
    }
    const operations = get('operations');
    if (operations) renderOperations(operations.operations || []);
  },
};
