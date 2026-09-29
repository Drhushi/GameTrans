/* 单元详情：路径图抽屉与译文收件箱展开共用的内容块。
 *
 * 编辑器带即时结构检查（占位符 / 括号 / 标签 / 换行 与原文对比），这只是
 * 打字时的提示；权威校验在后端保存闸门（后端需求 R3）。保存能力没有接上时
 * 退级为只读 + 「复制给 agent」。
 */

import { esc, num, tokenReport, copyText } from './util.js';
import { statusTag, checksHtml, validationRows, toast, saveVerdict } from './render.js';

function kv(pairs) {
  return `<dl class="kv">${pairs
    .map(([k, v, cls = '']) => `<dt>${esc(k)}</dt><dd class="${cls}">${v}</dd>`)
    .join('')}</dl>`;
}

function provenanceLine(provenance) {
  if (!provenance) return '';
  const parts = [];
  if (provenance.provider) parts.push(`provider ${provenance.provider}`);
  if (provenance.model) parts.push(`model ${provenance.model}`);
  if (provenance.agent) parts.push(`agent ${provenance.agent}`);
  if (provenance.prompt_digest) parts.push(`prompt ${String(provenance.prompt_digest).slice(0, 12)}…`);
  return parts.join(' ｜ ');
}

export function agentBrief(item) {
  const lines = [
    `# gametrans 翻译单元`,
    `路径: ${item.path || '—'}`,
    `类型: ${item.kind || '—'}`,
  ];
  if (item.speaker) lines.push(`说话人: ${item.speaker}`);
  if (item.locator && item.locator.file) {
    lines.push(`位置: ${item.locator.file}${item.locator.line ? ':' + item.locator.line : ''}`);
  }
  lines.push(`原文: ${item.source || '—'}`);
  lines.push(`译文: ${item.target || '（未翻译）'}`);
  lines.push(`状态: ${item.status || 'unknown'}`);
  if (item.error) lines.push(`错误: ${item.error}`);
  return lines.join('\n');
}

/**
 * 往 container 里渲染一个单元详情。
 * item: {path, kind, source, speaker, context, locator, unit_id, weight, resource_refs}
 * artifact: 译文样本里的对应条目（可空 = 未翻译）
 * opts: {editable, onSave(unitId, target)}
 */
export function renderUnitDetail(container, item, artifact, opts = {}) {
  const target = artifact?.target ?? '';
  const status = artifact?.status ?? 'untranslated';
  const editable = !!opts.editable && !!artifact?.unit_id;

  const head = kv([
    ['状态', statusTag(status)],
    ['类型', esc(item.kind || '—')],
    ['位置', esc(item.locator?.file ? `${item.locator.file}${item.locator.line ? ':' + item.locator.line : ''}` : '—')],
    ['开销', esc(item.weight ? `${num(item.weight.cost)} token · ${num(item.weight.char_count)} 字 · 复现 ${num(item.weight.occurrences)}` : '—')],
    ...(item.speaker ? [['说话人', esc(item.speaker)]] : []),
  ]);

  const prov = provenanceLine(artifact?.provenance);
  const errorBlock = artifact?.error
    ? `<div class="checks"><span class="c bad">✗ ${esc(artifact.error)}</span></div>`
    : '';

  container.innerHTML = `
    ${head}
    <div class="dsec">
      <h4>原文</h4>
      <div class="src-text">${esc(item.source ?? '—')}</div>
    </div>
    ${item.context ? `<div class="dsec"><h4>结构上下文 <span class="hint">（提取层给出；检索细节待后端 R2）</span></h4><div class="ctx-text">${esc(item.context)}</div></div>` : ''}
    <div class="dsec">
      <h4>译文 ${artifact ? '' : '<span class="hint">（未翻译）</span>'}</h4>
      ${errorBlock}
      ${checksHtml(validationRows(artifact?.validation))}
      ${prov ? `<p class="hint mono" style="margin:4px 0 8px;">${esc(prov)}</p>` : ''}
      <div class="editor">
        <textarea id="detail-editor" spellcheck="false" ${editable ? '' : 'readonly'}
          placeholder="${editable ? '直接改译文，保存时后端会重算结构校验' : '编辑能力未接入（后端需求 R3），当前只读'}">${esc(target)}</textarea>
        <div id="detail-checks"></div>
        <div class="row">
          ${editable ? '<button id="detail-save" type="button">保存译文</button>' : ''}
          <button class="ghost" id="detail-copy" type="button">复制给 agent</button>
          <span class="hint" id="detail-result"></span>
        </div>
      </div>
    </div>
    ${artifact?.resource_versions && Object.keys(artifact.resource_versions).length
      ? `<div class="dsec"><h4>产出时的资源版本</h4>${kv(Object.entries(artifact.resource_versions).map(([k, v]) => [esc(k), esc(String(v).slice(0, 24))]))}</div>`
      : ''}
  `;

  const editor = container.querySelector('#detail-editor');
  const checks = container.querySelector('#detail-checks');
  const result = container.querySelector('#detail-result');

  const refreshChecks = () => {
    const rows = tokenReport(item.source ?? '', editor.value);
    checks.innerHTML = checksHtml(rows);
  };
  editor.addEventListener('input', refreshChecks);
  refreshChecks();

  container.querySelector('#detail-copy')?.addEventListener('click', async () => {
    const ok = await copyText(agentBrief({ ...item, target: editor.value, status }));
    toast(ok ? '单元详情已复制，可以粘给 agent' : '复制失败（非安全上下文）');
  });

  const save = container.querySelector('#detail-save');
  if (save) {
    save.addEventListener('click', async () => {
      if (!opts.onSave) return;
      save.disabled = true;
      result.textContent = '保存中…';
      try {
        const saved = await opts.onSave(artifact.unit_id, editor.value);
        // 保存成功 ≠ 校验通过：没过闸门的条目照样落盘，但状态是 needs_review。
        // 结论以后端重算的结果为准，面板不自作主张说"通过了"。
        const verdict = saveVerdict(saved);
        result.textContent = verdict.text;
        toast(verdict.ok ? '译文已保存' : '已保存，但未通过校验');
      } catch (err) {
        result.textContent = `保存失败：${err.message}`;
        if (err.hint) result.title = err.hint;
      } finally {
        save.disabled = false;
      }
    });
  }
}
