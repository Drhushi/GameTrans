/* 路径图画布：章带 + 场卡 + 依赖边，一种视图，纯 SVG 手写，零依赖。
 *
 * 布局是最朴素的工程流程图画法：全书一条从左到右的长带，章按申报/依赖
 * 顺序横排成段（章 = 带里的一段列，章间空一列）；章内深度决定列序，
 * 同深度的场并排同列、沿前驱居中；跨章知识边与章内边一样首尾相连。
 *
 * 默认视图是看得清字的 1:1、锚在图首；"缩到装下全图"只留给 fit 按钮。
 *
 * 缩放就是几何放大缩小，不换内容：场卡（一场一张、左侧状态色条）在任何倍率下
 * 都是同一批东西，不会因为缩放换档长出别的东西来。点场卡直接开单元现场。
 *
 * 交互约定：拖背景 = 平移；单击场卡 = 选中（再点一次 = 打开抽屉看信息）；
 * 拖场卡 = 移动这一场，拖章标题 = 整章一起挪，边从节点的实时坐标画，不会断；
 * 选中的场卡里可以划选文字（拖它的色条才能移动它）。手动挪过的位置在
 * 数据刷新后由布局重排。
 */

const NODE_W = 196;
const NODE_H = 46;
const GAP_X = 64;
const GAP_Y = 18;
const MAX_NODES = 500;
//: 自然排序："第 2 章" < "第 10 章"、act4 < act11。载荷顺序是**权重序**
//: （translatable_nodes 按优先级排），与故事顺序无关，不能拿来当章序。
const NATURAL = new Intl.Collator('zh', { numeric: true });
//: 行与行之间留出的空隙 —— 章带子的标题写在这里。
const ROW_GAP = 84;
//: 同列两个场块之间的缝 —— 塔式连排会让人认不出"一块是一场"。
const REGION_GAP = 12;

/** 取一句人能读的话当节点标题：去掉 Ren'Py 文本标签与转义，压平空白再截断。 */
function readable(text, limit = 24) {
  const flat = String(text || '')
    .replace(/\{[^}]*\}/g, '')
    .replace(/\\n/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!flat) return '';
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

const STATUS_COLORS = {
  // 后端算好的节点状态（全图口径）。未翻译给了 --cyan：它是未开工项目里
  // 最常见的状态，用 --line-2 会让色条贴在卡片上根本看不见。
  usable: 'var(--accent)',
  needs_review: 'var(--amber)',
  untranslated: 'var(--cyan)',
  not_translatable: 'var(--faint)',
  // 采样回退时会拿到 Artifact 自带的状态词
  ok: 'var(--accent)',
  failed: 'var(--red)',
  skipped: 'var(--faint)',
  unknown: 'var(--line-2)',
};

/** 边的两端。
 *
 * **后端发的是协议名** ``from`` / ``to`` —— ``GraphEdge.to_dict`` 写的就是它们
 * （``from`` 在 Python 里是关键字，所以字段本身叫 ``source`` / ``target``，只有
 * 序列化时才换名）。画布内部一律用 ``source`` / ``target``，转换只在这一个地方做：
 * 散在别处的话，谁按哪个名字读就全凭运气，而读错的后果是**边被静默丢光、
 * 图变成一排孤立的点**。
 */
export function edgeEnds(edge) {
  return {
    source: edge?.from ?? edge?.source ?? null,
    target: edge?.to ?? edge?.target ?? null,
  };
}

/** 节点该显示成什么状态。
 *
 * 后端算好的那个优先：它按**全图**统计，而 ``/api/translations`` 的样本有条数上限，
 * 超出上限的节点在那边查不到，会被一并褪成"未知"。样本只在后端没给时才兜底。
 */
export function nodeStatus(node, statusById) {
  return (
    node?.status ??
    statusById.get(node?.unit_id) ??
    statusById.get(node?.path) ??
    (node?.unit_id ? 'unknown' : 'not_translatable')
  );
}

function escXml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

/** 有依赖边时：按依赖深度分层（真·最长链：前驱**全部**落定才落定，
 * 深度 = 各前驱深度的最大值 + 1）。旧实现"被任何前驱碰到就算落定"，
 * 多条入边的节点会被最早的前驱拉到浅层 —— 消费者因此排到某些前驱前面。
 * 环里的节点永远凑不齐前驱，连同它们的下游一起贴到最后一层 —— 与调度
 * "环里的区域同属一波"同语义。返回 Map：依赖深度 → 该深度的节点 id。 */
function layersFromEdges(nodes, childrenOf) {
  const remaining = new Map(); // 未落定的前驱数
  const predsOf = new Map();
  const kidsOf = new Map();
  for (const n of nodes) {
    remaining.set(n.node_id, 0);
    predsOf.set(n.node_id, new Set());
    kidsOf.set(n.node_id, new Set());
  }
  for (const n of nodes) {
    for (const c of childrenOf(n) || []) {
      if (!remaining.has(c) || c === n.node_id || predsOf.get(c).has(n.node_id)) continue;
      remaining.set(c, remaining.get(c) + 1);
      predsOf.get(c).add(n.node_id);
      kidsOf.get(n.node_id).add(c);
    }
  }
  const depth = new Map();
  let frontier = nodes.filter((n) => remaining.get(n.node_id) === 0).map((n) => n.node_id);
  while (frontier.length) {
    const next = [];
    for (const id of frontier) {
      let d = 0;
      for (const p of predsOf.get(id)) d = Math.max(d, depth.get(p) + 1);
      depth.set(id, d);
      for (const c of kidsOf.get(id)) {
        remaining.set(c, remaining.get(c) - 1);
        if (remaining.get(c) === 0) next.push(c);
      }
    }
    frontier = [...new Set(next)];
  }
  const layers = new Map();
  for (const n of nodes) {
    const d = depth.get(n.node_id);
    if (d === undefined) continue;
    if (!layers.has(d)) layers.set(d, []);
    layers.get(d).push(n.node_id);
  }
  const rest = nodes.map((n) => n.node_id).filter((id) => !depth.has(id));
  if (rest.length) {
    let maxD = -1;
    for (const d of depth.values()) maxD = Math.max(maxD, d);
    layers.set(maxD + 1, rest);
  }
  return layers;
}

/** 区域的**剧情顺序**：有依赖边时按块拓扑分层（谁先交代谁靠前，环安全），
 * 无边时按首现顺序。返回 `region -> 序号`。路径图的列序与译文页的工作单排序
 * 共用这一份 —— 两处看到的"从头到尾"必须一致。 */
export function regionRank(nodes, dataEdges) {
  const firstSeen = [];
  const seen = new Set();
  for (const n of nodes) {
    const key = n.region || regionKey(n.path);
    if (!seen.has(key)) {
      seen.add(key);
      firstSeen.push(key);
    }
  }
  const rank = new Map(firstSeen.map((k, i) => [k, i]));
  if (dataEdges && dataEdges.length) {
    const grouped = layoutGraph(nodes, dataEdges);
    const keyLayers = layersFromEdges(
      firstSeen.map((k) => ({ node_id: k })),
      (fake) => [...(grouped.children.get(fake.node_id) || [])]
    );
    let next = 0;
    for (const [, layerKeys] of [...keyLayers.entries()].sort((a, b) => a[0] - b[0])) {
      for (const key of layerKeys) if (rank.has(key)) rank.set(key, firstSeen.length + next++);
    }
  }
  return rank;
}

/** 把一张图折成"布局块"上的有向图，供分层用。
 *
 * 后端的边是**两种粒度混着**的：控制流与依赖边连的是**区域**（label、文件这种
 * "一块"），并列选项边连的是**单元**。画布画的是单元，所以先把端点归一化成
 * "这个单元属于哪一块"，再按块分层 —— 否则绝大多数边会因为"端点在画布上找不到"
 * 被静默丢掉，图看着就是一堆孤立的点（这正是 R4 之前的样子）。
 *
 * 返回 ``{members, children, keys, dropped}``。``dropped`` 是两端都归不到块上的边数：
 * 丢了一半的边不该看起来像"这张图本来就没有边"。
 */
export function layoutGraph(nodes, edges) {
  const regionOf = new Map();
  const known = new Set();
  for (const n of nodes) {
    const region = n.region || n.path || n.node_id;
    regionOf.set(n.node_id, region);
    known.add(region);
  }
  const keyOf = (endpoint) => {
    if (regionOf.has(endpoint)) return regionOf.get(endpoint);
    return known.has(endpoint) ? endpoint : null;
  };

  const members = new Map();
  for (const n of nodes) {
    const key = regionOf.get(n.node_id);
    if (!members.has(key)) members.set(key, []);
    members.get(key).push(n.node_id);
  }

  const children = new Map([...members.keys()].map((k) => [k, new Set()]));
  let dropped = 0;
  for (const edge of edges || []) {
    const { source, target } = edgeEnds(edge);
    const from = keyOf(source);
    const to = keyOf(target);
    if (from === null || to === null) dropped += 1;
    // 同一块内部的先后由路径顺序表达，不是分层信息。
    // ``orders=false``（reading_order 等"有关系但方向未确认"的边）不参与分层：
    // 分层必须只认**真正生效**的排序边（control_flow / agent），否则图画出来的
    // 是"如果什么都信"的形状，不是调度实际用的那张。
    else if (from !== to && edge.orders !== false) children.get(from).add(to);
  }
  return { members, children, keys: [...members.keys()], dropped };
}

/** 按依赖边给块分层（细节档已改阅读顺序，不再走这里；函数保留 ——
 * 它被 tests/test_panel_graph_canvas.py 钉住，也是将来"执行顺序视图"的现成零件。） */
function layersFromLayoutGraph(nodes, grouped) {
  const { members, children, keys } = grouped;
  const byPath = new Map(nodes.map((n) => [n.path, n]));
  const keyLayers = layersFromEdges(
    keys.map((key) => ({ node_id: key })),
    (fake) => [...(children.get(fake.node_id) || [])]
  );
  const layers = new Map();
  for (const [index, layerKeys] of keyLayers) {
    const expanded = [];
    for (const key of layerKeys) {
      const group = (members.get(key) || [])
        .slice()
        .sort((a, b) => (byPath.get(a)?.path || '').localeCompare(byPath.get(b)?.path || ''));
      expanded.push(...group);
    }
    layers.set(index, expanded);
  }
  return layers;
}

/** 区域身份的兜底：路径 `file#label/...` 的 file#label 前缀。后端给了
 * `n.region` 就不用它；老载荷（或工作台自己的节点表）才走这条。 */
/** 节点标签：# 后最后两段，比孤零零的 say[0] 有辨识度。 */
function nodeLabel(path) {
  const s = String(path || '');
  const hashAt = s.indexOf('#');
  const parts = (hashAt < 0 ? s : s.slice(hashAt + 1)).split('/').filter(Boolean);
  return parts.slice(-2).join('/') || s;
}

function regionKey(path) {
  const s = String(path || '');
  const hashAt = s.indexOf('#');
  return hashAt < 0 ? s : s.slice(0, hashAt + 1 + s.slice(hashAt + 1).split('/')[0].length);
}

export function createGraphCanvas(container, { onSelect, onChapterPick } = {}) {
  let nodes = [];
  let posById = new Map();      // node_id -> {x, y}
  let dimById = new Map();      // node_id -> bool
  let edges = [];
  let chapterByNode = new Map(); // node_id -> 章（后端填了 chapter 才画带子）
  let columnsMeta = [];         // 网格里每一列的几何与归属（含成员 ids），章带共用
  let regionOfNode = new Map(); // node_id -> region（区域级边锚定 / 高亮用）
  let regionSpan = new Map();   // region -> 首末节点 id（区域级边画成末格→首格）
  let focusedRegion = null;     // 从译文页跳回来时高亮的场
  let lastData = [[], []];      // 最近一次 setData 的入参：归位按钮按它重排
  let view = { x: 0, y: 0, k: 1 };
  let filterState = { q: '', statuses: null, chapter: null };
  let lastShapeSig = '';
  //: 两端都归不到画布上的边数。留着是为了让图例如实说一句"有几条没画出来"，
  //: 而不是让少画的边看起来像"这张图本来就没有边"。
  let droppedEdges = 0;

  const size = () => ({
    w: container.clientWidth || 900,
    h: container.clientHeight || 600,
  });

  function layout(dataNodes, dataEdges) {
    nodes = dataNodes.slice(0, MAX_NODES);
    const byPath = new Map(nodes.map((n) => [n.path, n]));
    // 空数组也要走"没有边"那条路：否则一张没有边的图会被排成一整排，
    // 而不是退回按场景分列的树
    const hasEdges = !!(dataEdges && dataEdges.length);
    const grouped = hasEdges ? layoutGraph(nodes, dataEdges) : null;
    droppedEdges = grouped ? grouped.dropped : 0;

    // 画线的候选：生效边（orders=true）实线画；"有关系但方向未确认"的边虚线弱画
    let drawable;
    if (hasEdges) {
      drawable = dataEdges
        .map((edge) => ({
          ...edgeEnds(edge),
          orders: edge.orders !== false,
        }))
        .filter((e) => e.source !== e.target);
    } else {
      drawable = nodes
        .filter((n) => n.parent && byPath.has(n.parent))
        .map((n) => ({ source: byPath.get(n.parent).node_id, target: n.node_id }));
    }

    // ---- 剧情地图排版：全书一条从左到右的长带，章按申报/依赖顺序横排成段。
    // 章内的知识边决定列内的深度（真正生效的并行结构）；跨章的边与章内边
    // 同待遇，首尾相连直接画。章内排版是标准分层 DAG：列 = 章内依赖深度，
    // 节点 = 场块（块内单元竖排），列沿前驱居中，不再按路径字典序。
    chapterByNode = new Map(nodes.map((n) => [n.node_id, n.chapter || '']));
    const membersOf = new Map(); // region -> [node_id...]（保持路径序）
    for (const n of nodes) {
      const r = n.region || regionKey(n.path);
      if (!membersOf.has(r)) membersOf.set(r, []);
      membersOf.get(r).push(n.node_id);
    }
    const regions = [...membersOf.keys()];
    const chapterOfRegion = (r) => chapterByNode.get(membersOf.get(r)[0]) || '';
    // 场的**故事序**：按区域路径排（引擎按文件顺序吐节点，路径序≈故事序）。
    // 载荷顺序是**权重序**（translatable_nodes 按优先级排），拿它当故事序章带会反着堆。
    const pathOf = new Map(nodes.map((n) => [n.node_id, n.path]));
    const regionsByPath = [...regions].sort((a, b) =>
      NATURAL.compare(pathOf.get(membersOf.get(a)[0]) || '', pathOf.get(membersOf.get(b)[0]) || ''));
    const storyIndexOf = new Map(regionsByPath.map((r, i) => [r, i]));

    // 深度只看**章内**的生效边；没有边就退化为剧情序号
    const depthOf = new Map(regions.map((r, i) => [r, i]));
    if (grouped) {
      const intraChildren = new Map(
        [...grouped.children.entries()].map(([k, set]) => [
          k,
          new Set([...set].filter((c) => chapterOfRegion(k) === chapterOfRegion(c))),
        ])
      );
      const depthLayers = layersFromEdges(
        regions.map((r) => ({ node_id: r })),
        (fake) => [...(intraChildren.get(fake.node_id) || [])]
      );
      for (const [d, rs] of depthLayers) for (const r of rs) depthOf.set(r, d);
    }

    const seq = [...regions].sort(
      (a, b) => depthOf.get(a) - depthOf.get(b) || storyIndexOf.get(a) - storyIndexOf.get(b)
    );
    // 章序跟**故事**走，不跟深度 —— 知识边把各章的深度拉平后，深度序会
    // 退化成节点 id 序，章就倒了。故事序按区域路径排（见上），章按首场排。
    const chapterSeen = [];
    for (const r of regionsByPath) {
      const ch = chapterOfRegion(r);
      if (!chapterSeen.includes(ch)) chapterSeen.push(ch);
    }
    // 章带先后还要**不违反依赖方向**：跨章生效边指到哪章，哪章就得靠后，
    // 否则消费者章被排在提供者章前面，节点看起来"跑到前驱前面"。
    // 章级拓扑（环安全），同一层里按路径序；没有跨章边时就是纯路径序。
    const chapterPreds = new Map(chapterSeen.map((ch) => [ch, new Set()]));
    if (grouped) {
      for (const [from, set] of grouped.children) {
        const cf = chapterOfRegion(from);
        for (const to of set) {
          const ct = chapterOfRegion(to);
          if (cf !== ct && chapterPreds.has(cf) && chapterPreds.has(ct)) {
            chapterPreds.get(ct).add(cf);
          }
        }
      }
    }
    const chapterTopo = [];
    const placedChapter = new Set();
    while (chapterTopo.length < chapterSeen.length) {
      const ready = chapterSeen.filter(
        (ch) =>
          !placedChapter.has(ch) &&
          [...chapterPreds.get(ch)].every((p) => placedChapter.has(p))
      );
      if (!ready.length) {
        // 环：剩下的章按路径序收尾，不再假装有先后
        for (const ch of chapterSeen) if (!placedChapter.has(ch)) chapterTopo.push(ch);
        break;
      }
      for (const ch of ready) {
        chapterTopo.push(ch);
        placedChapter.add(ch);
      }
    }
    const chapterOrder = [...chapterTopo.filter(Boolean), ...chapterTopo.filter((c) => !c)];

    // ---- 全书一条从左到右的长带：章按序横排成段（章 = 带里的一段列），
    // 章内深度决定列序，章与章之间空一列作分隔。跨章知识边与章内边一样
    // **首尾相连**（源场末格 → 目标场首格），没有专门的章间端口线。
    // 垂直方向全书共用一套居中逻辑：每列沿"各块前驱中心"的平均对齐，
    // 没有已摆前驱的列（如全书开头的章首列）在全书高度内居中。
    posById = new Map();
    columnsMeta = [];
    let rowTop = 0;
    const blockHOf = (r) => membersOf.get(r).length * (NODE_H + GAP_Y) - GAP_Y;

    // 全局前驱表：跨章边也算 —— 它们现在就是普通的边
    const parentsOf = new Map(regions.map((r) => [r, []]));
    if (grouped) {
      for (const [from, set] of grouped.children) {
        for (const to of set) {
          if (parentsOf.has(to)) parentsOf.get(to).push(from);
        }
      }
    }

    // 每章的列：章内深度定列序；章与章之间空一列
    const columns = []; // 全书从左到右的列：[region...]
    const bands = [];   // 每章一段：{chapter, first, numCols}
    let offset = 0;
    for (const ch of chapterOrder) {
      const regs = seq.filter((r) => chapterOfRegion(r) === ch);
      const depths = [...new Set(regs.map((r) => depthOf.get(r)))].sort((a, b) => a - b);
      const colRank = new Map(depths.map((d, i) => [d, i]));
      const first = offset;
      for (const r of regs) {
        const c = first + colRank.get(depthOf.get(r));
        if (!columns[c]) columns[c] = [];
        columns[c].push(r);
      }
      bands.push({ chapter: ch, first, numCols: depths.length });
      offset += depths.length + 1; // 章与章之间空一列
    }
    for (let i = 0; i < offset; i++) if (!columns[i]) columns[i] = [];

    // 先量每列高度（与摆放无关），全书带高 = 最高的那一列
    const colH = columns.map((list) =>
      list.length
        ? list.reduce((acc, r) => acc + blockHOf(r) + REGION_GAP, 0) - REGION_GAP
        : 0
    );
    const rowH = Math.max(0, ...colH);

    const placedCenter = new Map(); // region -> 块中心 y（重心法的输入）
    const top = rowTop;
    for (let c = 0; c < columns.length; c++) {
      const list = columns[c];
      if (!list.length) continue;
      const x = c * (NODE_W + GAP_X);
      const avgOf = (r) => {
        const ps = parentsOf.get(r).filter((p) => placedCenter.has(p));
        if (!ps.length) return null;
        return ps.reduce((acc, p) => acc + placedCenter.get(p), 0) / ps.length;
      };
      list.sort((a, b) => {
        const aa = avgOf(a);
        const bb = avgOf(b);
        if (aa === null && bb === null) {
          return membersOf.get(a)[0].localeCompare(membersOf.get(b)[0]);
        }
        if (aa === null) return 1;
        if (bb === null) return -1;
        return aa - bb || membersOf.get(a)[0].localeCompare(membersOf.get(b)[0]);
      });
      // 列沿前驱居中：列的中心对准"各块前驱中心"的平均；没有已摆前驱的
      // 列（章首列）在全书高度内居中 —— 对称扇形，线不全挤在一头。
      const h = colH[c];
      const centers = [];
      for (const r of list) {
        for (const pp of parentsOf.get(r)) {
          if (placedCenter.has(pp)) centers.push(placedCenter.get(pp));
        }
      }
      const desired = centers.length
        ? centers.reduce((acc, v) => acc + v, 0) / centers.length
        : top + rowH / 2;
      let y = Math.min(Math.max(desired - h / 2, top), top + Math.max(rowH - h, 0));
      for (const r of list) {
        const bh = blockHOf(r);
        membersOf.get(r).forEach((id, i) => {
          posById.set(id, { x, y: y + i * (NODE_H + GAP_Y) });
        });
        placedCenter.set(r, y + bh / 2);
        y += bh + REGION_GAP;
      }
    }

    // 章带：框住自己那一段列，标题在带顶
    columnsMeta = bands.map(({ chapter, first, numCols }) => ({
      row: '0',
      chapter,
      x: first * (NODE_W + GAP_X) - 14,
      y: top - 30,
      w: (numCols - 1) * (NODE_W + GAP_X) + NODE_W + 28,
      h: 30 + rowH + 14,
    }));
    rowTop += rowH + ROW_GAP;

    // 区域锚点：区域级边画成"源区域末格 → 目标区域首格"——区域成列，
    // 这两格正好一个对着出边方向、一个对着入边方向。
    regionOfNode = new Map();
    regionSpan = new Map();
    for (const n of nodes) {
      const region = n.region || regionKey(n.path);
      regionOfNode.set(n.node_id, region);
      const p = posById.get(n.node_id);
      if (!p) continue;
      const span = regionSpan.get(region);
      if (!span) regionSpan.set(region, { top: n.node_id, bot: n.node_id, topY: p.y, botY: p.y });
      else {
        if (p.y < span.topY) { span.topY = p.y; span.top = n.node_id; }
        if (p.y > span.botY) { span.botY = p.y; span.bot = n.node_id; }
      }
    }

    // ---- 边整理：按"区域对（+是否生效）"聚合成一根，锚到首末格、悬停报
    // 条数。跨章知识边与章内边**同待遇** —— 源场末格 → 目标场首格，各自连
    // 各自的卡：目标不同就进不同的卡，没有共同出入口，也就没有重叠问题。
    // 端点有两种粒度：单元 id（查 regionOfNode）与**区域 id**（知识边就是
    // 区域级的，直接认 —— regionSpan 里有它才画得出来，否则进 dropped 计数）。
    const regionOfEndpoint = (ep) =>
      regionOfNode.get(ep) ?? (regionSpan.has(ep) ? ep : null);
    const pairCount = new Map();
    for (const e of drawable) {
      const ra = regionOfEndpoint(e.source);
      const rb = regionOfEndpoint(e.target);
      if (!ra || !rb || ra === rb) continue;
      const key = `${ra}\u0000${rb}\u0000${e.orders ? 1 : 0}`;
      pairCount.set(key, (pairCount.get(key) || 0) + 1);
    }
    edges = [...pairCount.entries()]
      .map(([key, count]) => {
        const [source, target, flag] = key.split('\u0000');
        return { source, target, orders: flag === '1', count };
      })
      .filter((e) => !!edgeAnchors(e));
    dimById = new Map();
  }

  /** 一条边的两端落在画布上的坐标：端点是单元就直接用它的位置，
   * 是区域就锚在该区域的末格（源）/ 首格（目标）。同一段内部的先后
   * 由列内顺序表达，不画（画了全是短竖线）。 */
  function edgeAnchors(e) {
    const a = anchorPos(e.source, 'bot');
    const b = anchorPos(e.target, 'top');
    if (!a || !b || (a.x === b.x && a.y === b.y)) return null;
    const ra = regionOfNode.get(e.source);
    if (ra && ra === regionOfNode.get(e.target)) return null;
    return { a, b };
  }

  function anchorPos(key, which) {
    if (posById.has(key)) return posById.get(key);
    const span = regionSpan.get(key);
    if (!span) return null;
    return posById.get(which === 'top' ? span.top : span.bot);
  }

  /** 章带子：一章一条，标题写在带子左上角（一章一行，通常不会续；
   * 「（续）」是防御 —— 万一未来一章拆成多行，第二条带不再顶着同样的
   * 标题装"框全了"）。
   *
   * 无章的系统文本也画带（标题「系统文本」）：它们自成一组但不画框的话，
   * 会像浮在相邻剧情章的带子里、跟那一章"连上"。
   */
  function chapterBands() {
    const bands = [];
    const seen = new Set();
    for (const meta of columnsMeta) {
      const last = bands[bands.length - 1];
      if (last && last.key === meta.chapter && last.row === meta.row) {
        const x0 = Math.min(last.x, meta.x);
        const x1 = Math.max(last.x + last.w, meta.x + meta.w);
        last.x = x0;
        last.w = x1 - x0;
        last.h = Math.max(last.h, meta.h);
        continue;
      }
      const name = meta.chapter || '系统文本';
      const cont = seen.has(name) ? '（续）' : '';
      seen.add(name);
      bands.push({
        ...meta,
        key: meta.chapter,
        chapter: name + cont,
      });
    }
    return bands;
  }

  /** 单元级边 + 区域级边：端点是单元就直连，是区域就锚到首末格（edgeAnchors）。
   * 生效边实线；方向未确认的边（orders=false）虚线弱画；聚合边悬停报条数。 */
  function edgeSvg() {
    return edges
      .map((e) => {
        const ends = edgeAnchors(e);
        if (!ends) return '';
        const { a, b } = ends;
        const x1 = a.x + NODE_W;
        const y1 = a.y + NODE_H / 2;
        const x2 = b.x;
        const y2 = b.y + NODE_H / 2;
        const mx = Math.max(30, (x2 - x1) / 2);
        const dim = dimById.get(e.source) || dimById.get(e.target) ? ' dim' : '';
        const weak = e.orders ? '' : ' weak';
        const tip = e.count > 1 ? `<title>${e.count} 条依赖</title>` : '';
        return `<path class="gedge${dim}${weak}" d="M ${x1} ${y1} C ${x1 + mx} ${y1}, ${x2 - mx} ${y2}, ${x2} ${y2}">${tip}</path>`;
      })
      .join('');
  }

  /** 单元节点：一张小卡 = 一句（一个翻译单元），左侧色条是它的状态。
   * 卡面就是素面板色，不另染色 —— 状态只用那根色条说。
   * 卡上写人读的：概要标题优先，没摘要就退原文首句；代码路径只进 tooltip。 */
  function nodeSvg() {
    return nodes.map((n) => {
      const p = posById.get(n.node_id);
      if (!p) return '';
      const bar = STATUS_COLORS[n.status || 'unknown'] || 'var(--line-2)';
      const dim = dimById.get(n.node_id) ? ' dim' : '';
      const sel = focusedRegion && regionOfNode.get(n.node_id) === focusedRegion ? ' sel' : '';
      const picked = n.node_id === selectedNode ? ' gsel' : '';
      const sub = [n.kind, n.speaker].filter(Boolean).join(' · ');
      const tip = [n.chapter ? `章：${n.chapter}` : '', n.path].filter(Boolean).join('\n');
      const label = n.title || readable(n.source, 22) || nodeLabel(n.path);
      return `<g class="gnode${dim}${sel}${picked}" data-node="${escXml(n.node_id)}" transform="translate(${p.x},${p.y})">
        <title>${escXml(tip)}</title>
        <rect class="box" width="${NODE_W}" height="${NODE_H}"></rect>
        <rect class="bar" width="4" height="${NODE_H}" fill="${bar}"></rect>
        <text x="12" y="19" font-size="11.5">${escXml(label)}</text>
        <text class="sub" x="12" y="35" font-size="10">${escXml(sub)}</text>
      </g>`;
    }).join('');
  }

  function renderSvg() {
    const { w, h } = size();
    const bandSvg = chapterBands()
      .map((b) => `<g class="gband" data-chapter="${escXml(b.key)}">
        <rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="12"></rect>
        <text class="gband-title" x="${b.x + 12}" y="${b.y + 21}">${escXml(b.chapter)}</text>
      </g>`)
      .join('');
    container.innerHTML = `<svg width="${w}" height="${h}" viewBox="${view.x} ${view.y} ${w / view.k} ${h / view.k}">
      <defs><marker id="gt-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse"><path class="garrow" d="M 1 1 L 8 5 L 1 9"></path></marker></defs>
      ${bandSvg}${edgeSvg()}${nodeSvg()}
    </svg>`;
  }

  function applyView() {
    const svg = container.querySelector('svg');
    if (!svg) return;
    const { w, h } = size();
    svg.setAttribute('viewBox', `${view.x} ${view.y} ${w / view.k} ${h / view.k}`);
  }

  /** 默认视图：1:1（字看得清的倍率），锚在图的左上角。几百场的书
   * "装下全图"就得把字缩到看不见，所以那个档只留给 fit 按钮手动用。 */
  function startView() {
    if (!posById.size) return;
    let minX = Infinity, minY = Infinity;
    for (const p of posById.values()) {
      minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
    }
    view.k = 1;
    view.x = minX - 40;
    view.y = minY - 40;
    applyView();
  }

  function fit() {
    if (!posById.size) return;
    const { w, h } = size();
    // 三档共用同一套坐标：fit 永远适配整个图，落在哪一档由倍率决定
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const p of posById.values()) {
      minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
      maxX = Math.max(maxX, p.x + NODE_W); maxY = Math.max(maxY, p.y + NODE_H);
    }
    const pad = 40;
    const k = Math.min((w - pad * 2) / (maxX - minX), (h - pad * 2) / (maxY - minY), 1.4);
    // 适配后**居中**：内容盒中心对准视口中心，多出来的那一边平分，
    // 不再钉在左上角（长条图右侧/下侧会留一大块空白）。
    zoomTo(Math.max(k, 0.08), (minX + maxX) / 2, (minY + maxY) / 2);
  }

  function zoomAt(factor, mx, my) {
    const { w, h } = size();
    const k2 = Math.min(Math.max(view.k * factor, 0.08), 4);
    const gx = view.x + mx / view.k;
    const gy = view.y + my / view.k;
    view.x = gx - mx / k2;
    view.y = gy - my / k2;
    view.k = k2;
    applyView();
  }

  // ---- 指针交互：平移 / 选中 / 拖节点 / 拖整章 ----
  let drag = null;
  let selectedNode = null; // 单击选中的节点（再点一次才开抽屉；选中后卡里可划字）
  let rafPending = false;

  function scheduleRender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      renderSvg();
    });
  }

  container.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const rect = container.getBoundingClientRect();
    zoomAt(ev.deltaY < 0 ? 1.15 : 1 / 1.15, ev.clientX - rect.left, ev.clientY - rect.top);
  }, { passive: false });

  container.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return;
    // 目标在 **down 时**记下：setPointerCapture 会把 up 重定向到容器，
    // up 里的 ev.target 不再是原目标 —— 用 down 时的目标判断，真鼠标才点得动。
    const nodeEl = ev.target.closest?.('.gnode');
    const bandEl = ev.target.closest?.('.gband');
    let mode = 'pan';
    let node = null;
    let chapter = null;
    if (nodeEl) {
      node = nodeEl.dataset.node || null;
      // 选中的卡：拖字是划选（浏览器原生，不 capture）；拖色条仍是移动。
      // 未选中的卡：拖 = 移动，单击 = 选中。
      mode =
        nodeEl.classList.contains('gsel') && !ev.target.classList.contains('bar')
          ? 'text'
          : 'node';
    } else if (bandEl && ev.target.classList.contains('gband-title')) {
      mode = 'chapter';
      chapter = bandEl.dataset.chapter ?? '';
    }
    drag = {
      mode, node, chapter,
      startX: ev.clientX, startY: ev.clientY,
      lastX: ev.clientX, lastY: ev.clientY,
      moved: false,
      starts: null, // 要挪的东西的起始坐标（世界坐标）
      bandMetas: [], // 拖整章时连带子的几何一起挪
    };
    if (mode === 'node' && node && posById.has(node)) {
      drag.starts = new Map([[node, { ...posById.get(node) }]]);
    } else if (mode === 'chapter') {
      drag.starts = new Map(
        nodes
          .filter((n) => (chapterByNode.get(n.node_id) || '') === chapter && posById.has(n.node_id))
          .map((n) => [n.node_id, { ...posById.get(n.node_id) }])
      );
      drag.bandMetas = columnsMeta.filter((m) => m.chapter === chapter);
    }
    if (mode !== 'text') container.setPointerCapture(ev.pointerId);
  });

  container.addEventListener('pointermove', (ev) => {
    if (!drag || drag.mode === 'text') return;
    const dx = ev.clientX - drag.lastX;
    const dy = ev.clientY - drag.lastY;
    drag.lastX = ev.clientX;
    drag.lastY = ev.clientY;
    if (Math.abs(ev.clientX - drag.startX) + Math.abs(ev.clientY - drag.startY) > 4) {
      drag.moved = true;
      if (drag.mode === 'pan') container.classList.add('panning');
    }
    if (!drag.moved) return;
    if (drag.mode === 'pan') {
      view.x -= dx / view.k;
      view.y -= dy / view.k;
      applyView();
      return;
    }
    // 节点 / 整章拖动：按世界坐标平移起点。边每次从 posById 现画，不会断。
    const wx = (ev.clientX - drag.startX) / view.k;
    const wy = (ev.clientY - drag.startY) / view.k;
    for (const [id, s] of drag.starts) {
      posById.set(id, { x: s.x + wx, y: s.y + wy });
    }
    for (const m of drag.bandMetas) {
      m.x += dx / view.k;
      m.y += dy / view.k;
    }
    scheduleRender();
  });

  container.addEventListener('pointerup', (ev) => {
    container.classList.remove('panning');
    const wasClick = drag && !drag.moved;
    const mode = drag?.mode;
    const nodeId = drag?.node ?? null; // down 时记下的节点（up 的 target 已被 capture 重定向）
    const chapter = drag?.chapter ?? null;
    drag = null;
    if (!wasClick) return;
    // ``text`` 模式 = 点在已选中场卡的字上：按一下（没拖动）就是"再点一次
    // 打开看信息"；拖动才是在卡里划选文字（down 时没 capture，原生选区照常）。
    if ((mode === 'node' || mode === 'text') && nodeId) {
      if (selectedNode === nodeId) {
        // 已选中再点一次 = 打开这个单元的现场抽屉
        const node = nodes.find((n) => n.node_id === nodeId);
        if (node && onSelect) onSelect(node);
      } else {
        selectedNode = nodeId;
        renderSvg();
      }
    } else if (mode === 'chapter') {
      // 点章带标题（没拖动）= 筛选只看这一章（开/关交给外层切换）
      if (onChapterPick) onChapterPick(chapter);
    } else if (mode === 'pan' && selectedNode !== null) {
      selectedNode = null; // 点背景取消选中
      renderSvg();
    }
  });

  /** 以世界坐标 (wx, wy) 为中心把缩放调到 k。 */
  function zoomTo(k, wx, wy) {
    const { w, h } = size();
    view.k = Math.min(Math.max(k, 0.08), 4);
    view.x = wx - w / (2 * view.k);
    view.y = wy - h / (2 * view.k);
    applyView();
  }

  window.addEventListener('resize', renderSvg);

  return {
    setData(dataNodes, dataEdges, statusById) {
      lastData = [dataNodes, dataEdges];
      for (const n of dataNodes) {
        n.status = nodeStatus(n, statusById);
      }
      // 节点集合没变就保留用户的平移缩放，只更新状态色
      const shapeSig = dataNodes.map((n) => n.node_id).join('|');
      layout(dataNodes, dataEdges);
      renderSvg();
      if (shapeSig !== lastShapeSig) {
        lastShapeSig = shapeSig;
        startView();
      } else {
        applyView();
      }
      this.setFilter(filterState);
    },
    setFilter({ q, statuses, chapter }) {
      filterState = { q, statuses, chapter };
      const query = (q || '').trim().toLowerCase();
      dimById = new Map();
      for (const n of nodes) {
        const statusOk = !statuses || !statuses.size || statuses.has(n.status);
        const chapterOk = !chapter || (n.chapter || '') === chapter;
        const textOk =
          !query ||
          `${n.kind} ${n.path} ${n.source} ${n.speaker || ''}`.toLowerCase().includes(query);
        dimById.set(n.node_id, !(statusOk && chapterOk && textOk));
      }
      renderSvg();
    },
    zoomIn: () => {
      const { w, h } = size();
      zoomAt(1.25, w / 2, h / 2);
    },
    zoomOut: () => {
      const { w, h } = size();
      zoomAt(1 / 1.25, w / 2, h / 2);
    },
    fit,
    /** 全部节点归位：按当前数据重新排版，手动拖过的位置作废；视图不动。 */
    reset: () => {
      layout(lastData[0], lastData[1]);
      renderSvg();
    },
    /** 这一轮排完之后，有几条边没能落到画布上。图例用它如实报一句。 */
    stats: () => ({ droppedEdges }),
    /** 从译文页跳回来：居中到这一场，场内的节点高亮。 */
    focusRegion(region) {
      const pts = nodes
        .filter((n) => (n.region || regionKey(n.path)) === region && posById.has(n.node_id))
        .map((n) => posById.get(n.node_id));
      if (!pts.length) return;
      focusedRegion = region;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const p of pts) {
        minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
        maxX = Math.max(maxX, p.x + NODE_W); maxY = Math.max(maxY, p.y + NODE_H);
      }
      zoomTo(Math.max(view.k, 1), (minX + maxX) / 2, (minY + maxY) / 2);
      renderSvg();
    },
  };
}
