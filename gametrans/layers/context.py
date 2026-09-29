"""上下文检索与装配（Translation Layer Architecture Guide §8、§9、§10）。

这一层把两件本来容易糊在一起的事拆开：

* **Context Resolver 决定"取什么"** —— 按 ``direct → structural → knowledge → memory →
  targeted`` 的阶梯逐层取候选，每层带来源、版本、范围、优先级、置信度与断言类型；
  某一层取空了才往下多走一步（§8 的"不足"才升级）。**不默认全文预读**。
* **Context Builder 决定"怎么组织"** —— 把已选中的信息按层排成模型输入，每层可以
  单独启用、单独统计，于是"哪种 Context 策略更好"是能实验的，而不是一个黑盒。

三条边界：

* 检索结果必须能追溯来源 —— 于是"这句话为什么这样翻"有答案（§22）。
* 推断（hypothesis）默认不注入；显式打开时也单独成段并标注未验证（§7）。
* 没实现的检索层（全文检索）不写进统计 —— 检查不了的事不假装做过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from gametrans.core.tasks import (
    GLOSSARY_HINT_TYPE,
    STYLE_TYPES,
    TERMINOLOGY_TYPES,
    RetrievedContext,
    RetrievedItem,
    TranslationTask,
)
from gametrans.layers.preflight import request_texts
from gametrans.layers.resource import (
    TERMBOOK_LEGEND,
    ResourceLayer,
    row_key,
    setting_content,
    term_content,
)

__all__ = [
    "CONTEXT_SECTIONS",
    "DEFAULT_LAYERS",
    "DEFAULT_LAYERS",
    "EXPERIMENT_ONLY_LAYERS",
    "IMPLEMENTED_LAYERS",
    "MUTUALLY_EXCLUSIVE_ARMS",
    "MAX_CONTAINER_ITEMS",
    "RETRIEVAL_LADDER",
    "ContextBuilder",
    "ContextPackage",
    "ContextResolver",
    "RetrievalPolicy",
]

#: 检索阶梯，顺序即优先级（Guide §8）。最后那一层是**最后手段**，本阶段不实现。
#:
#: ``neighbors`` 是 ``structural`` 的**等预算对照臂**（RQ3）：同样的条数、同样的字符
#: 预算，装的是邻居原文而不是图导出的关系。它必须占**与 structural 同一格** ——
#: 检索预算先到先得，排在阶梯末尾的话，等它取数时池子已经被 memory 与 targeted
#: 吃掉，只能拿到更少的条数（实测真夹具上 8 条 vs 7 条、126 vs 110 字符）。
RETRIEVAL_LADDER: tuple[str, ...] = (
    "direct",
    "structural",
    "neighbors",
    "knowledge",
    "memory",
    "targeted",
    "full_text",
)

#: 真正会走的层。``full_text`` 不在其中 —— 它不进统计，也不假装跑过。
IMPLEMENTED_LAYERS: frozenset[str] = frozenset(
    {"direct", "structural", "neighbors", "knowledge", "memory", "targeted"}
)

#: 两条臂**互斥**：同时请求就是条件定义错误（"结构 vs 平铺"没有第三种含义）。
#: 静默二选一会让消融实验量到的两组其实是同一组，而报告上看不出来。
MUTUALLY_EXCLUSIVE_ARMS: tuple[frozenset[str], ...] = (frozenset({"structural", "neighbors"}),)

#: 对照臂**不进默认档位**：它们只在实验里被显式点名时才上。
#: 让 `neighbors` 进默认会让默认策略同时含两条互斥臂 —— 那不是"默认"，是条件冲突。
EXPERIMENT_ONLY_LAYERS: frozenset[str] = frozenset({"neighbors"})

DEFAULT_LAYERS: tuple[str, ...] = tuple(
    layer
    for layer in RETRIEVAL_LADDER
    if layer in IMPLEMENTED_LAYERS and layer not in EXPERIMENT_ONLY_LAYERS
)

#: 一条 Task 最多记几层容器归属（见 ContextResolver._container_chain）。
MAX_CONTAINER_ITEMS = 3

#: 原型：这几层装的是"必须遵守"的约束，不是可选上下文 —— 它们**自己一个池子**。
#: 混在一起抢容量时，可选上下文一满就会把知识整层挡在外面。
CONSTRAINT_LAYERS: frozenset[str] = frozenset({"knowledge"})

#: 约束类里**还要过一道条数闸**的条目类型 —— 剩下那些在产出它们的层里自己收口。
#:
#: ⚠️ 2026-09-28 改：以前这里还把术语（`glossary` 8 条）与设定（`worldbook` 4 条）分成两池。
#: 那是**三个文件那会儿**的分法（`glossary.jsonl` / `worldbook.jsonl` / `worldbook.md`）；
#: 现在术语与设定是**同一行的两栏**（`termbook.jsonl` 一行一个实体），再把同一行劈成两类去
#: 抢额度就说不通了。真靶实测的代价：`Saki` 那一行在 **20 次调用里有 12 次根本没进请求**
#: （每次恰好 12 条、按排序截尾，与"这一段用不用得到"无关），模型看到正文里的 `Saki`
#: 却没人告诉它译名，就自己起名叫了「沙希」—— 盘上于是 `沙希 ×10 / 咲 ×3`，
#: 看起来像模型前后不一致，其实是**它没收到那条**。
#: 现在术语/设定按**行**在 `_knowledge` 里收口（见 :data:`DEFAULT_ITEM_CAPS`）。
CONSTRAINT_ITEM_GROUPS: dict[str, str] = {
    "style_guide": "style",
}

#: 约束类里每一类的条数上限。**两档**：术语书、风格 —— 术语与设定是同一行的两栏、同属
#: 术语书，不再各占一笔（"就只有风格和术语是需要分开的了"）。
#:
#: ⚠️ 别按**条**把一行拆开算：术语与设定是同一行的两栏，一行有几个写法就吃几个格子 ——
#: 正是它把真靶的 `Saki → 咲` 在 20 次调用里挤掉了 12 次，模型于是自己起名「沙希」。
#: 现在**按行整行进**。
DEFAULT_ITEM_CAPS: dict[str, int] = {"termbook": 64, "style": 4}

#: 记账用：条目类型 → 它在报告里归哪一档。正常配置下池子总额 = 各档之和，这里挡不到多少。
ACCOUNT_GROUPS: dict[str, str] = {
    "approved_glossary": "termbook_hit",
    GLOSSARY_HINT_TYPE: "termbook_hit",
    "worldbook_entry": "termbook_hit",
    "style_guide": "style",
}

#: 【术语书】那一段收哪几类知识条目：译名与设定。
TERMBOOK_ITEM_TYPES: frozenset[str] = TERMINOLOGY_TYPES | {
    GLOSSARY_HINT_TYPE,
    "worldbook_entry",
}


@dataclass
class RetrievalPolicy:
    """取什么、取多少、什么时候停。检索策略是插槽，不是常量（§21）。"""

    layers: tuple[str, ...] = DEFAULT_LAYERS
    #: **可选上下文**（direct / structural / neighbors / targeted / memory）的条数上限。
    #: 这是**输入成本**上限（§8），不是"上下文越长越好"。约束类有自己的池子，不从这里划。
    budget: int = 16
    #: 术语 / 设定 / 风格各自的条数上限（`None` = 用 :data:`DEFAULT_ITEM_CAPS`）。
    #: 术语与设定**按行**算，分 `literal`（原文里真出现）/ `background`（靠知识点沾上的）两档。
    item_caps: dict[str, int] | None = None
    max_neighbours: int = 2
    max_targeted: int = 3
    max_memory: int = 4
    #: 翻译记忆的**相近译法**要不要查。默认不查：它是"全表 + difflib"，实测
    #: 12,999 条记忆时一次全量跑批要 42 分钟、换来 0 条参考（见 register）。
    #: 精确命中（字典查找）不走这里，照旧零成本复用。
    fuzzy_memory: bool = False
    use_glossary: bool = True
    use_worldbook: bool = True
    use_style: bool = True
    use_knowledge: bool = True
    use_memory: bool = True

    @property
    def caps(self) -> dict[str, int]:
        return dict(self.item_caps or DEFAULT_ITEM_CAPS)

    @property
    def constraint_ceiling(self) -> int:
        """约束类一共收多少条 = 各类配额之和。

        刻意**不**从 :attr:`budget` 里划：两件事是两种成本 —— 约束是"必须遵守"，
        可选上下文是"可能有用"，让它们互相抢额度正是以前那个毛病的来源。
        """
        return sum(self.caps.values())

    def __post_init__(self) -> None:
        for arm in MUTUALLY_EXCLUSIVE_ARMS:
            if arm <= set(self.layers):
                names = "、".join(sorted(arm))
                raise ValueError(
                    f"这些检索层互斥，不能同时请求：{names}。"
                    "它们是「结构关系」与「等预算平铺」两条对照臂，同时上就等于条件定义错误。"
                )


class ContextResolver:
    """按阶梯取候选（Guide §9）。

    它**不**组织文本，也**不**决定怎么问模型 —— 那两件事分别属于 Context Builder 与
    Executor。它只回答一个问题：这个任务需要的信息，都在哪、有多可信。
    """

    def __init__(
        self,
        resources: ResourceLayer,
        *,
        graph: Any = None,
        policy: RetrievalPolicy | None = None,
    ) -> None:
        self.resources = resources
        self.graph = graph
        self.policy = policy or RetrievalPolicy()
        #: speaker → 该说话者的其它 Unit（targeted 层用；按图建一次，别每个任务扫全图）
        self._by_speaker: dict[str, list[Any]] | None = None

    # ---- 入口 ---------------------------------------------------------------

    def resolve(
        self,
        task: TranslationTask,
        *,
        unit: Any = None,
        node_id: str | None = None,
        skip_layers: Iterable[str] = (),
    ) -> RetrievedContext:
        """按阶梯取候选。

        ``skip_layers`` 是这次检索**明确不需要**的层。典型用法：这条内容已经精确命中
        翻译记忆，就不必再去找"相近译法"——那是为一个不存在的模型调用付检索成本。
        """
        policy = self.policy
        skipped = set(skip_layers)
        requested = [
            kind
            for kind in task.context_request.kinds
            if kind in policy.layers and kind not in skipped
        ]
        items: list[RetrievedItem] = []
        counts: dict[str, int] = {}
        starved: list[str] = []
        dropped: dict[str, int] = {}
        escalating = False
        #: 两个**互不抢额度**的池子：约束类（术语/设定/风格）有自己的上限，
        #: 可选上下文有自己的上限。以前是"从总额里划一块给约束"，于是约束一多就把
        #: 上下文饿死，而报告上看不见。
        used = {"constraint": 0, "context": 0}
        ceilings = {
            "constraint": policy.constraint_ceiling,
            "context": policy.budget,
        }
        #: 约束类里**还要过闸**的那几类自己的配额（术语/设定已在产出层按行收口，见
        #: :data:`CONSTRAINT_ITEM_GROUPS`）
        per_kind: dict[str, int] = {}
        caps = policy.caps

        for layer in RETRIEVAL_LADDER:
            if layer not in policy.layers or layer not in IMPLEMENTED_LAYERS:
                continue
            if layer in skipped:
                continue
            pool = "constraint" if layer in CONSTRAINT_LAYERS else "context"
            if used[pool] >= ceilings[pool]:
                # 这个池子满了：跳过这一层，但**不 break** —— 别的池子还可能有位置。
                # 以前这里一 break，结构层就能把术语表永久挡在外面。
                continue
            if layer not in requested and not escalating:
                continue
            produced = self._collect(layer, task, unit, node_id, dropped)
            counts[layer] = len(produced)
            if layer in CONSTRAINT_LAYERS:
                # 这一类自己超了配额的不进去，但要**记下来**（谁被挤掉看得见）
                kept: list[RetrievedItem] = []
                for item in produced:
                    group = CONSTRAINT_ITEM_GROUPS.get(item.type)
                    cap = caps.get(group or "", 0)
                    if group is not None and per_kind.get(group, 0) >= cap:
                        dropped[group] = dropped.get(group, 0) + 1
                        continue
                    if group is not None:
                        per_kind[group] = per_kind.get(group, 0) + 1
                    kept.append(item)
                produced = kept
            room = ceilings[pool] - used[pool]
            keep = produced[:room] if room > 0 else []
            if len(keep) < len(produced):
                # 池子总额也把它挡了一部分：按类归到"被挤掉"
                for item in produced[len(keep):]:
                    group = ACCOUNT_GROUPS.get(item.type, item.type)
                    dropped[group] = dropped.get(group, 0) + 1
            items.extend(keep)
            used[pool] += len(keep)
            if layer in requested and not produced:
                # 这一层取空了 → 按 §8 往下多走一步
                starved.append(layer)
                escalating = True
            elif produced:
                escalating = False

        return RetrievedContext(
            items=items,
            layers=counts,
            strategy="ladder:" + "+".join(counts) if counts else "ladder:empty",
            # "取不到"与"项目里本来就没有"是两件事：前者进 starved，后者不算降级
            degraded=not items,
            starved=starved,
            dropped=dropped,
        )

    # ---- 各层 ---------------------------------------------------------------

    def _collect(
        self,
        layer: str,
        task: TranslationTask,
        unit: Any,
        node_id: str | None,
        dropped: dict[str, int] | None = None,
    ) -> list[RetrievedItem]:
        """取某一层的候选。

        ``dropped`` 是"被挤掉的条数"的账本：配额挤掉的在这里记，
        **资源层自己预算挤掉的**（世界书的字符预算）也由这里并进来 —— 两处都要看得见。
        """
        if unit is None:
            return []
        if layer == "direct":
            return self._direct(unit)
        if layer == "structural":
            return self._structural(unit, node_id)
        if layer == "neighbors":
            # 对照臂按**同样的阶梯位置与同样的预算**产出：先拿到结构臂那一份，
            # 再逐条换成等字符数的邻居原文（见 _neighbors 的说明）。
            return self._neighbors(self._structural(unit, node_id), unit, node_id)
        if layer == "knowledge":
            return self._knowledge(unit, task, node_id, dropped)
        if layer == "memory":
            return self._memory(unit, task.target_language)
        if layer == "targeted":
            return self._targeted(unit)
        return []  # pragma: no cover - IMPLEMENTED_LAYERS 已经穷举

    def _direct(self, unit: Any) -> list[RetrievedItem]:
        """Level 1：当前 Segment、说话者、场景、前后邻接。

        这些机器已经知道，**不让 LLM 重建**（Guide §6 Level 1）。
        """
        items: list[RetrievedItem] = []
        context = getattr(unit, "context", None)
        if context is None:
            return items
        scope = f"unit:{unit.id}"
        if context.speaker:
            items.append(
                RetrievedItem(
                    layer="direct",
                    type="speaker",
                    content=context.speaker,
                    source="unit.context",
                    scope=scope,
                    priority=90,
                    provenance="adapter",
                )
            )
        if context.scene:
            items.append(
                RetrievedItem(
                    layer="direct",
                    type="scene",
                    content=context.scene,
                    source="unit.context",
                    scope=scope,
                    priority=80,
                    provenance="adapter",
                )
            )
        for neighbour_id in list(context.neighboring_units)[: self.policy.max_neighbours]:
            neighbour = self._node(neighbour_id)
            if neighbour is None or getattr(neighbour, "unit", None) is None:
                # 图里找不到就不写进去 —— 不编造一个"前后文"
                continue
            items.append(
                RetrievedItem(
                    layer="direct",
                    type="neighbour",
                    content=neighbour.unit.source,
                    source="graph",
                    scope=f"unit:{neighbour_id}",
                    priority=70,
                    provenance="adapter",
                )
            )
        return items

    def _structural(self, unit: Any, node_id: str | None) -> list[RetrievedItem]:
        """Level 2：容器层级、控制流、分支、跨文件关系（Guide §6 Level 2）。"""
        items: list[RetrievedItem] = []
        node = self._node(node_id or getattr(unit, "id", ""))
        if node is not None:
            items.extend(self._container_chain(node))
        if self.graph is not None and node is not None:
            region = self.graph.region_of(node.node_id)
            if region:
                items.append(
                    RetrievedItem(
                        layer="structural",
                        type="region",
                        content=str(region),
                        source="graph",
                        scope=f"region:{region}",
                        priority=60,
                        provenance="adapter",
                    )
                )
        return items

    def _neighbours(self, node: Any) -> list[str]:
        """邻居原文池：同区域优先，按"离本单元的距离"排序，**排除本单元自己**。

        确定性来自固定的遍历与排序：同输入必须逐字同输出，否则"三次重复"里混进了随机性。
        """
        if self.graph is None:
            return []
        region = self.graph.region_of(node.node_id)
        ordered: list[tuple[str, str]] = []
        for other_id in sorted(self.graph.nodes):
            other = self.graph.nodes[other_id]
            unit = getattr(other, "unit", None)
            text = getattr(unit, "source", "") if unit is not None else ""
            if not text or other_id == node.node_id:
                continue
            if region and self.graph.region_of(other_id) != region:
                continue
            ordered.append((other_id, text))
        try:
            index = [item[0] for item in ordered].index(node.node_id)
        except ValueError:
            index = 0
        window = ordered[max(0, index - 3) : index + 4]
        return [text for other_id, text in window if other_id != node.node_id] or [
            text for _id, text in ordered
        ]

    def _neighbors(
        self, structural: list[RetrievedItem], unit: Any, node_id: str | None
    ) -> list[RetrievedItem]:
        """Level 2 的**等预算平铺臂**：条数与字符预算逐条对齐 ``structural``。

        为什么必须逐条对齐字符数：条数或字符数差一点，两边的输入成本就不同，
        "结构关系本身有没有额外作用"这个问题就问不出来了 —— 差的那些字本身就能帮忙。

        邻居池用尽时按环形复用（真实工程 15k+ 单元用不尽），这样"条数恒等于结构臂"
        这个不变量在极小的图上依然成立。
        """
        node = self._node(node_id or getattr(unit, "id", ""))
        if node is None or not structural:
            return []
        pool = self._neighbours(node)
        if not pool:
            return []

        items: list[RetrievedItem] = []
        cursor = 0
        for slot, reference in enumerate(structural):
            budget = len(str(reference.content))
            parts: list[str] = []
            used = 0
            guard = 0
            limit = 4 * len(pool) + budget + 8
            while used < budget:
                remaining = budget - used
                candidate = pool[(cursor + slot + guard) % len(pool)]
                if len(candidate) <= remaining:
                    parts.append(candidate)
                    used += len(candidate)
                    cursor += 1
                elif remaining:
                    # 余量装不下整个候选：用它的前缀**填满**（少一个字也会让两臂的
                    # 输入成本不同，而那正是这个对照要排除的东西）
                    parts.append(candidate[:remaining])
                    used = budget
                guard += 1
                if guard > limit:  # pragma: no cover - 防御：池里全是空串时才会到
                    break
            text = "".join(parts)[:budget]
            if not text:
                continue
            items.append(
                RetrievedItem(
                    layer="neighbors",
                    type="neighbour_text",
                    content=text,
                    source="graph",
                    # scope 与 priority 都对齐结构臂那条：阶梯位置相同，只换内容
                    scope=reference.scope,
                    priority=reference.priority,
                    provenance="neighbour window",
                )
            )
        return items

    def _container_chain(self, node: Any) -> list[RetrievedItem]:
        """最近的几层容器归属（label / 文件 / 工程根）。

        只取最近的 :data:`MAX_CONTAINER_ITEMS` 层：结构归属是用来判断"这段文本处于什么
        位置"的，把整条祖先链（真实工程上能到 19 层）搬进每条 Task 只是给审计文件灌水。
        """
        items: list[RetrievedItem] = []
        seen: set[str] = set()
        current = getattr(node, "parent", None)
        while current is not None and current not in seen:
            if len(items) >= MAX_CONTAINER_ITEMS:
                break
            seen.add(current)
            parent = self._node(current)
            if parent is None:
                break
            label = str(getattr(parent, "kind", "") or "")
            path = str(getattr(parent, "path", "") or parent.node_id)
            items.append(
                RetrievedItem(
                    layer="structural",
                    type="structure",
                    content=path,
                    source="graph",
                    scope=f"{label}:{parent.node_id}",
                    priority=55,
                    provenance="adapter",
                )
            )
            if label == "root" or getattr(parent, "parent", None) is None:
                break
            current = getattr(parent, "parent", None)
        return items

    def _knowledge(
        self,
        unit: Any,
        task: TranslationTask,
        node_id: str | None = None,
        dropped: dict[str, int] | None = None,
    ) -> list[RetrievedItem]:
        """Level 3：术语书（译名与设定）、人物与实体资料、Style Guide（Guide §6）。

        术语书进请求只有**两条路**：**绿灯** = 行的任一写法在
        **请求里那份文本**上命中（正文 + 说话人标注）；**蓝灯** = `constant=true` 的行
        无条件注入。扫描文本走 :func:`gametrans.layers.preflight.request_texts`——
        与预检**同一份口径**（说话人在请求里是真的存在的一行；只扫正文会让
        "只说名字不在正文"的行永远命中不了，真靶上模型于是自己起名）。
        """
        policy = self.policy
        speaker = getattr(unit.context, "speaker", None) if unit.context else None
        scene = getattr(unit.context, "scene", None) if unit.context else None
        unit_id = getattr(unit, "id", None)
        # 区域要一起传：风格是**按作用域解析**的，`scope="region:某区域"` 的条目
        # 少了这个参数就永远匹配不上（"写了但从不生效"是最难查的一类）。
        region = None
        node = None
        if self.graph is not None and node_id:
            region = self.graph.region_of(node_id)
            node = self.graph.nodes.get(node_id)
        # **请求里那份文本**（含说话人标注）：命中判据与预检读数共用这一份，
        # 不再各写一遍（真靶 2026-09-29：`Cliff`/`Saki` 只在标注里出现，只扫正文时
        # 那两个单元一条术语都没进 —— 模型于是自己起名「克里夫」「沙希」）。
        scan_slots = request_texts([node]) if node is not None else []
        scan_text = "\n".join(scan_slots) if scan_slots else str(unit.source or "")
        context = self.resources.context_for(
            unit.source,
            slots=scan_slots or None,
            use_glossary=policy.use_glossary,
            use_worldbook=policy.use_worldbook,
            use_style=policy.use_style,
            use_knowledge=policy.use_knowledge,
            unit_id=unit_id,
            speaker=speaker,
            scene=scene,
            region_id=region,
        )
        #: 原文**字面命中**的术语：**按写法**记（不是按行）—— 一行有一组写法，
        #: `Eve Herschel` 命中时它用的是自己那个译名，只有命中的那个写法算硬约束。
        literal_terms = {
            writing for entry in context.terms for writing in entry.hit_writings(scan_text)
        }
        versions = self.resources.versions()
        if dropped is not None:
            # 资源层自己挤掉的（设定的字符预算）：并进同一本账，别让它消失在两层之间。
            for group, count in context.dropped.items():
                dropped[group] = dropped.get(group, 0) + count
        items: list[RetrievedItem] = []
        # **按行走，不按条走。** `context.entries` 本来就是一行一个实体（`terms` / `profiles`
        # 只是它按两栏切出来的视图）。以前这里把它拍平成一条条 item、再按 item 计额度，
        # 于是"一行有几个写法就吃几个格"—— 真靶的后果是 `Saki` 那行被挤掉（见
        # `DEFAULT_ITEM_CAPS` 的说明）。现在：**一行的两栏一起进或一起不进**，计一行。
        caps = self.policy.caps
        hit_rows: list[list[RetrievedItem]] = []
        for entry in context.entries:
            row: list[RetrievedItem] = []
            # 一行可能有好几个带译名的写法（`Eve` / `Eve Herschel`）：**逐条出行**，
            # 每个写法用它自己的译名 —— 只出一行"身份 → 身份译名"会把短名的译名
            # 铺到全名上，正是这次重构要修的东西。
            for writing, target in entry.targets:
                row.append(
                    RetrievedItem(
                        layer="knowledge",
                        type=(
                            "approved_glossary"
                            if writing in literal_terms
                            else GLOSSARY_HINT_TYPE
                        ),
                        content=term_content(writing, target),
                        source="termbook.jsonl",
                        version=versions.get("termbook", ""),
                        scope="global",
                        priority=85,
                        provenance="human",
                    )
                )
            if entry.is_profile:
                row.append(
                    RetrievedItem(
                        layer="knowledge",
                        type="worldbook_entry",
                        content=setting_content(entry),
                        source="termbook.jsonl",
                        version=versions.get("termbook", ""),
                        scope=f"worldbook:{entry.writing}",
                        priority=60,
                        provenance="human",
                    )
                )
            if not row:
                continue
            hit_rows.append(row)
        budget = caps.get("termbook", 0)
        for row in hit_rows:
            if len(row) > budget:
                if dropped is not None:
                    dropped["termbook_hit"] = dropped.get("termbook_hit", 0) + 1
                continue
            budget -= len(row)
            items.extend(row)
        for entry in context.style:
            items.append(
                RetrievedItem(
                    layer="knowledge",
                    type="style_guide",
                    content=entry.value,
                    source="style.jsonl",
                    version=versions.get("style", ""),
                    scope=entry.scope,
                    priority=entry.priority,
                    provenance=entry.source,
                )
            )
        return items

    def _memory(self, unit: Any, target_language: str) -> list[RetrievedItem]:
        if not self.policy.use_memory or not self.policy.fuzzy_memory:
            # 相近译法默认不查：它是"全表 + difflib"，实测在 12,999 条记忆上一次
            # 全量跑批要 42 分钟、换 0 条参考。精确命中走 lookup（字典查找），
            # 那条路零成本，不在这里。
            return []
        versions = self.resources.versions()
        items: list[RetrievedItem] = []
        for hit in self.resources.memory.suggest(
            unit.source, target_language, limit=self.policy.max_memory
        ):
            items.append(
                RetrievedItem(
                    layer="memory",
                    type="translation_memory",
                    content=f"{hit.entry.source} → {hit.entry.target}",
                    source="memory.jsonl",
                    version=versions.get("memory", ""),
                    scope="global",
                    priority=50,
                    confidence=hit.score,
                    # 记忆里的译文是我们自己产出的，**只作参考**（§8 的"不足"才用）
                    assertion="hypothesis",
                    provenance="translation memory（仅供参考，不要照抄）",
                )
            )
        return items

    def _targeted(self, unit: Any) -> list[RetrievedItem]:
        """定向回源文本取证：同一说话者/同一场景的其它 Unit。

        这一层是"定点查"，与全文预读是两回事（Guide §8、§20）。
        """
        if self.graph is None or self.policy.max_targeted <= 0:
            return []
        speaker = getattr(unit.context, "speaker", None) if unit.context else None
        if not speaker:
            return []
        items: list[RetrievedItem] = []
        for node in self._index_by_speaker().get(str(speaker), []):
            if node.unit is None or node.unit.id == getattr(unit, "id", None):
                continue
            items.append(
                RetrievedItem(
                    layer="targeted",
                    type="targeted_source",
                    content=node.unit.source,
                    source="graph",
                    scope=f"speaker:{speaker}",
                    priority=45,
                    provenance=f"same speaker: {speaker}",
                )
            )
            if len(items) >= self.policy.max_targeted:
                break
        return items

    # ---- 辅助 ---------------------------------------------------------------

    def _node(self, node_id: str | None) -> Any:
        if self.graph is None or not node_id:
            return None
        if node_id not in self.graph:
            return None
        return self.graph.get(node_id)

    def _index_by_speaker(self) -> dict[str, list[Any]]:
        if self._by_speaker is None:
            index: dict[str, list[Any]] = {}
            for node in self.graph.translatable_nodes():
                speaker = getattr(node.unit.context, "speaker", None) if node.unit else None
                if speaker:
                    index.setdefault(str(speaker), []).append(node)
            self._by_speaker = index
        return self._by_speaker


# --------------------------------------------------------------------------- #
# Context Builder（Guide §10）
# --------------------------------------------------------------------------- #

#: Builder 组织的段。每段可单独启用、单独统计 —— 于是不同 Context 策略可比。
CONTEXT_SECTIONS: tuple[str, ...] = (
    "task_objective",
    "current_source",
    "immediate_context",
    "structural_context",
    "relevant_knowledge",
    "terminology_constraints",
    "style_constraints",
    "engine_constraints",
    "provenance",
)


@dataclass
class ContextPackage:
    """一次 Task 的模型输入：分段 + 每段条目数 + 用到的原始条目。"""

    sections: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    items: list[RetrievedItem] = field(default_factory=list)

    def render(self) -> str:
        return "\n\n".join(text for text in self.sections.values() if text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "sections": dict(self.sections),
            "counts": dict(self.counts),
            "items": [item.to_dict() for item in self.items],
        }


class ContextBuilder:
    """把已选中的信息组织成模型输入。**它不负责寻找信息**（Guide §10）。"""

    def __init__(self, sections: Iterable[str] = CONTEXT_SECTIONS) -> None:
        self.sections = tuple(sections)

    def build(self, task: TranslationTask, context: RetrievedContext) -> ContextPackage:
        builders = {
            "task_objective": self._objective,
            "current_source": self._current_source,
            "immediate_context": self._immediate,
            "structural_context": self._structural,
            "relevant_knowledge": self._knowledge,
            "terminology_constraints": self._terminology,
            "style_constraints": self._style,
            "engine_constraints": self._engine,
            "provenance": self._provenance,
        }
        sections: dict[str, str] = {}
        counts: dict[str, int] = {}
        for name in self.sections:
            builder = builders.get(name)
            if builder is None:  # 未知段名：不静默通过，明说它没内容
                sections[name] = ""
                counts[name] = 0
                continue
            text, count = builder(task, context)
            sections[name] = text
            counts[name] = count
        return ContextPackage(sections=sections, counts=counts, items=list(context.items))

    # ---- 各段 ---------------------------------------------------------------

    @staticmethod
    def _objective(task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        return f"【任务】把这段文本翻译成 {task.target_language}", 1

    @staticmethod
    def _current_source(task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        if not task.source.strip():
            return "", 0
        return f"【待译原文】\n{task.source}", 1

    def _immediate(self, task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        items = context.by_layer("direct")
        if not items:
            return "", 0
        labels = {"speaker": "说话人", "scene": "场景", "neighbour": "相邻文本"}
        lines = [f"- {labels.get(item.type, item.type)}：{item.content}" for item in items]
        return "【直接上下文】\n" + "\n".join(lines), len(items)

    def _structural(self, task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        items = context.by_layer("structural")
        if not items:
            return "", 0
        lines = [f"- {item.type}：{item.content}" for item in items]
        return "【结构上下文】\n" + "\n".join(lines), len(items)

    def _knowledge(self, task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        """参考译法（相近句子的写法）。

        术语书的译名行与设定行合成一段（【术语书】，见 :meth:`_terminology`）：
        这里口径是"标的这两段没必要区分，东西越多越乱"。
        """
        memory = [item for item in context.by_layer("memory")]
        if not memory:
            return "", 0
        return (
            "【相近译法（仅供参考，不要照抄）】\n"
            + "\n".join(
                f"- {item.content}（相似度 {item.confidence:.2f}）" for item in memory
            ),
            len(memory),
        )

    @staticmethod
    def _terminology(task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        """【术语书】：译名与设定本来就是同一个实体的两栏，一段里连着出。

        以前按内容分两段渲染（术语约束 / 已确认知识），这里口径是
        "没必要区分，东西越多越乱"；数据一直只有一份（`termbook.jsonl` 一行一个实体），
        所以合成一段不丢任何东西。**同一个实体的两栏连着放**（按 :func:`row_key` 归位，
        行内保持检索给的出现顺序）。

        段头那句口径来自 :data:`~gametrans.layers.resource.TERMBOOK_LEGEND`：注入形状
        只有一处说了算，**跟着请求走** —— 用户换掉提示词模板也不至于读不懂这一段。
        """
        items = context.facts_of_type(*sorted(TERMBOOK_ITEM_TYPES))
        if not items:
            return "", 0
        rank: dict[str, int] = {}
        for item in items:
            rank.setdefault(row_key(item.content), len(rank))
        ordered = sorted(items, key=lambda item: rank[row_key(item.content)])
        return (
            "【术语书】\n" + TERMBOOK_LEGEND + "\n"
            + "\n".join(f"- {item.content}" for item in ordered),
            len(ordered),
        )

    @staticmethod
    def _style(task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        items = context.facts_of_type(*sorted(STYLE_TYPES))
        if not items:
            return "", 0
        return (
            "【风格要求】\n" + "\n".join(f"- {item.content}" for item in items),
            len(items),
        )

    @staticmethod
    def _engine(task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        if not task.engine_constraints:
            return "", 0
        return (
            "【必须原样保留】\n" + " ".join(task.engine_constraints),
            len(task.engine_constraints),
        )

    @staticmethod
    def _provenance(task: TranslationTask, context: RetrievedContext) -> tuple[str, int]:
        seen: dict[str, str] = {}
        for item in context.items:
            if item.source:
                seen[item.source] = item.version
        if not seen:
            return "", 0
        lines = [
            f"- {source}@{version}" if version else f"- {source}"
            for source, version in sorted(seen.items())
        ]
        return "【本次上下文来源】\n" + "\n".join(lines), len(lines)
