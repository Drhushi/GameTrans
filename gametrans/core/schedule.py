"""翻译计划：把"先后依赖"变成可执行的阶段结构。

延续块 1 的分工 —— **框架负责表达与消费，不负责判断，也不负责算最优。**

* :class:`TranslationPlan` 是容器：一次翻译被表达成若干个**有序阶段**，每个阶段是一组
  可以自由并行的区域。
* :class:`Scheduler` 是插槽：怎么把区域排成阶段，和"怎么判断依赖"一样是可替换的。

默认的 :class:`LayeredScheduler` **不额外引入任何策略** —— 它只是把已经确认的先后
关系照搬成阶段边界。没确认方向的边一概不管，所以并列支线不会被错排成串行链。

:class:`SeedBulkScheduler` 形状上对应设计里的「先翻知识产出者、再并行铺开」，
但它选谁当 seed 是**拍脑袋的策略**，所以它自称 ``validated=False``。

至于"哪种排法效率最高" —— 那是这个插槽要接受的目标函数，不在这层解决。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from gametrans.core.graph import PathGraph


@dataclass
class PlanPhase:
    """一个阶段：这些区域之间没有先后约束，可以自由并行。"""

    name: str
    regions: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "regions": list(self.regions), "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanPhase":
        return cls(
            name=str(data.get("name", "")),
            regions=[str(r) for r in (data.get("regions") or [])],
            reason=str(data.get("reason", "")),
        )


@dataclass
class TranslationPlan:
    """一次翻译的执行计划。阶段按顺序执行，阶段内部可并行。"""

    phases: list[PlanPhase] = field(default_factory=list)
    strategy: str = ""
    notes: list[str] = field(default_factory=list)

    def region_order(self) -> list[str]:
        """把阶段摊平成一维的区域执行顺序。"""
        order: list[str] = []
        for phase in self.phases:
            order.extend(phase.regions)
        return order

    def phase_of(self, region_id: str) -> str | None:
        for phase in self.phases:
            if region_id in phase.regions:
                return phase.name
        return None

    def phase_index(self, region_id: str) -> int | None:
        for index, phase in enumerate(self.phases):
            if region_id in phase.regions:
                return index
        return None

    def validate(self) -> list[str]:
        """计划是 agent / 用户都可能手改的数据，所以要能校验而不是想当然。"""
        problems: list[str] = []
        seen: set[str] = set()
        for phase in self.phases:
            for region in phase.regions:
                if region in seen:
                    problems.append(f"区域 {region!r} 出现在多个阶段里")
                seen.add(region)
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "notes": list(self.notes),
            "phases": [p.to_dict() for p in self.phases],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationPlan":
        return cls(
            phases=[PlanPhase.from_dict(p) for p in (data.get("phases") or [])],
            strategy=str(data.get("strategy", "")),
            notes=[str(n) for n in (data.get("notes") or [])],
        )


class Scheduler(Protocol):
    """调度策略 —— 又一个留给"后人智慧"的插槽。

    框架提供的是"阶段"这个机制；**怎么排**（包括"什么算效率最高"）是策略的事。

    ``flow`` 是可选输入：一次**实体流图**读数（见 :mod:`gametrans.layers.entityflow`），
    它给出"按知识边算出来的前驱集"。给了就用它排（并如实报读数），不给就照旧只看
    控制边 —— 同一套策略、两种输入，不新增第二种前驱集。
    """

    name: str
    #: 是否引入未经校准的判断。照搬既有约束的实现应当自称 True。
    validated: bool

    def describe(self) -> dict[str, Any]:
        ...

    def plan(self, graph: PathGraph, *, flow: Any = None) -> TranslationPlan:
        ...


def _closure_within(start: str, step: dict[str, set[str]], pending: set[str]) -> set[str]:
    """从 ``start`` 出发沿 ``step`` 走，只走 ``pending`` 里的节点。"""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(name for name in step.get(node, ()) if name in pending)
    return seen


def _source_group(pending: dict[str, set[str]], successors: dict[str, set[str]]) -> list[str]:
    """互相等的那一组（一个"源"强连通分量）：取"能到达自己的人数"最少的那个节点，
    和它能互相到达的那些。

    为什么需要它：知识边是**读出来的**，两个区域互相引用完全可能（A 引用了 B 首见的
    实体，B 也引用了 A 首见的）。那时谁也等不到谁，正确的处置是**一起翻**（与
    :meth:`PathGraph.region_layers` 对环的处置同一口径）。
    """
    back: dict[str, set[str]] = {}
    for name in sorted(pending):
        back[name] = _closure_within(name, pending, set(pending))
    start = min(back, key=lambda name: (len(back[name]), name))
    forward = _closure_within(start, successors, set(pending))
    return sorted(back[start] & forward) or [start]


def waves_from_predecessors(
    predecessors: dict[str, set[str]], regions: list[str]
) -> list[list[str]]:
    """把"谁等谁"摊成**波次**：没有未完成前驱的区域进当前波，互相等的进同一波。

    ``predecessors`` 就是那张前驱集（区域 → 前驱区域）。返回的波次就是阶段：
    **同一波内可并行，波与波之间等待** —— 与执行器今天消费的"阶段"是同一个东西。
    """
    known = set(regions)
    pending = {
        name: {p for p in predecessors.get(name, ()) if p in known} for name in regions
    }
    successors: dict[str, set[str]] = {name: set() for name in regions}
    for name, deps in pending.items():
        for provider in deps:
            successors.setdefault(provider, set()).add(name)
    done: set[str] = set()
    waves: list[list[str]] = []
    while pending:
        ready = sorted(name for name, deps in pending.items() if not (deps - done))
        if not ready:  # 环：互相等的那一组一起翻
            ready = _source_group(pending, successors)
        for name in ready:
            pending.pop(name, None)
        done.update(ready)
        waves.append(ready)
    return waves


@dataclass
class LayeredScheduler:
    """照搬依赖分层 —— 不额外引入任何策略。

    确认过的先后关系直接变成阶段边界；没确认的一律不管，于是它们落进同一阶段、
    最大程度并行。这个实现不假装知道"怎么排更快"，它只保证"不违反已知的先后"。
    """

    name: str = "layered"
    validated: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "validated": True,
            "note": "只照搬依赖分层，不额外引入任何策略：确认过的先后即阶段边界，其余并行。",
        }

    def plan(self, graph: PathGraph, *, flow: Any = None) -> TranslationPlan:
        layers = graph.region_layers()
        phases = [
            PlanPhase(name=f"layer-{index}", regions=list(layer), reason=f"依赖深度 {index}")
            for index, layer in enumerate(layers)
        ]
        return TranslationPlan(
            phases=phases,
            strategy=self.name,
            notes=["阶段内可并行；阶段之间只由已确认的先后关系分隔。"],
        )


@dataclass
class SinglePhaseScheduler:
    """全部塞进一个阶段 —— 最大并行度，完全不理会先后。

    存在的意义是当基准：想比较"尊重先后"到底牺牲了多少并行度时，拿它对一下。
    """

    name: str = "single-phase"
    validated: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "validated": True,
            "note": "全部区域放进一个阶段，最大并行度，不理会先后依赖。用作对照基准。",
        }

    def plan(self, graph: PathGraph, *, flow: Any = None) -> TranslationPlan:
        regions = [r.region_id for r in graph.regions()]
        return TranslationPlan(
            phases=[PlanPhase(name="all", regions=regions, reason="全部并行")],
            strategy=self.name,
            notes=["忽略先后约束，仅用于对照。"],
        )


@dataclass
class SeedBulkScheduler:
    """先翻知识产出者，再并行铺开。

    形状对应设计里的 seed / bulk 两段，但**"谁算知识产出者"和"seed 该多大"都是拍的**，
    没有真实数据校准，所以自称 ``validated=False``。

    它至少不会违反已知先后：provider 永远排在 consumer 之前或同一阶段。
    """

    name: str = "seed-bulk"
    validated: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "validated": False,
            "note": (
                "最早一层里的知识产出者作为 seed 先翻，其余并行铺开。"
                "选谁当 seed 的策略未经数据校准，占位实现。"
            ),
        }

    def plan(self, graph: PathGraph, *, flow: Any = None) -> TranslationPlan:
        layers = graph.region_layers()
        #: 边的端点可能落在**单元结点**上（真靶上就是这样：控制流边挂在 `unit_xxx` 上），
        #: 而区域身份是 label 名（`scene000` / `prologue`）。不归一的话 `region in providers`
        #: 永远为假 —— seed 阶段整个消失、策略静默退化成"单批全并行"，而阶段名与说明
        #: 还写着"先行批"，报告上看不出异常。同一处归一 `LayeredScheduler`（走
        #: `region_layers`）与 `flow_degrees` 都在用，这里补齐。
        providers = {graph.as_region(d.source) for d in graph.ordering_dependencies()}
        seed = [region for region in (layers[0] if layers else []) if region in providers]
        seeded = set(seed)
        bulk = [r.region_id for r in graph.regions() if r.region_id not in seeded]

        phases: list[PlanPhase] = []
        if seed:
            phases.append(
                PlanPhase(name="seed", regions=seed, reason="最早一层里的知识产出者")
            )
        phases.append(PlanPhase(name="bulk", regions=bulk, reason="其余区域，并行铺开"))
        return TranslationPlan(
            phases=phases,
            strategy=self.name,
            notes=["seed 阶段只为了让后续批次一开始就有上下文，代价是一小段串行前缀。"],
        )


@dataclass
class ChapterParallelScheduler:
    """章内照搬分层，章间并排开工 —— 把"书的长度"从轮数里去掉。

    **为什么需要它。** 视觉小说的图是一条长链：严格分层等于把叙事长度 1:1 变成轮数。
    真靶工程 45 个区域算出 **32 轮，其中 26 轮只含 1 个区域** —— 而章与章之间
    真正要传递的东西很薄：全局硬约束（术语 / 专名）+ 上一章的摘要。章摘要住在工作区的
    ``summaries.json``，**从原文生成**（不是从译文），所以开跑前就躺在盘上。

    **保留什么、放松什么。** 章内每一条有序约束原样保留 —— 阶段内的先后不是我重排的，
    ``global_rank`` 直接取自 :meth:`PathGraph.region_layers`，所以环的强连通缩点、
    汇合点的屏障语义一并沿用现成实现。被放松的只有**跨章的 sequencing 边**：不再逐场
    串行，改由"章摘要 + 全局硬约束"接力。放松了几条边**记在 notes 里**，不静默。

    **没有申报过章的项目**（工作区里没有 ``chapters.json``）会退回严格分层，并说明原因 ——
    章是项目申报的事实，内核不替它猜。

    ``validated=False``：放松有依据，但在拿到质量读数（术语一致率 / chrF / 中断损失）
    之前，不冒充"校准过"。

    ``lag``：后面几章最多提前 ``lag`` 轮开工。``0`` = 完全并行（默认，轮数最少）；
    调大 = 越接近严格分层，用来做对照。
    """

    name: str = "chapter-parallel"
    validated: bool = False
    lag: int = 0

    #: 没有章的单元归到哪一组（面板也用这个名字）。
    UNCHAPTERED: str = "系统文本"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "validated": False,
            "note": (
                "章内照搬依赖分层；章间并排开工（跨章 sequencing 边被放松，改由章摘要 + 硬约束接力）。"
                f"lag={self.lag}（0=完全并行）。未拿到质量读数前自称未校准。"
            ),
            "lag": self.lag,
        }

    # ---- 内部 ---------------------------------------------------------------

    @staticmethod
    def _chapter_of_region(graph: PathGraph) -> tuple[dict[str, str], list[str]]:
        """区域 → 章。返回 (映射, 一个区域里混了多章的名单)。"""
        mapping: dict[str, str] = {}
        mixed: list[str] = []
        for node_id in graph.walk():
            node = graph.nodes.get(node_id)
            unit = getattr(node, "unit", None) if node is not None else None
            if unit is None:
                continue
            region = graph.region_of(node_id)
            if not region:
                continue
            chapter = str(getattr(unit.context, "chapter", "") or "").strip()
            if not chapter:
                continue
            seen = mapping.get(region)
            if seen is None:
                mapping[region] = chapter
            elif seen != chapter and region not in mixed:
                mixed.append(region)
        return mapping, sorted(mixed)

    def plan(self, graph: PathGraph, *, flow: Any = None) -> TranslationPlan:
        layers = graph.region_layers()
        rank = {region: index for index, layer in enumerate(layers) for region in layer}
        chapter_of, mixed = self._chapter_of_region(graph)
        regions = [r.region_id for r in graph.regions()]

        if not chapter_of:
            fallback = LayeredScheduler().plan(graph, flow=flow)
            fallback.strategy = self.name
            fallback.notes = [
                "这个工作区没有申报过章（找不到 chapters.json 的章戳），退回严格依赖分层。",
                "章由项目申报：见 gametrans.core.chapters / 工作区的 chapters.json。",
            ]
            return fallback

        # 有章的区域按章分组，其余进"系统文本"一组
        groups: dict[str, list[str]] = {}
        for region in regions:
            groups.setdefault(chapter_of.get(region, self.UNCHAPTERED), []).append(region)

        # 章的顺序 = 它在图上的最早出现顺序（阅读顺序的事实，不是长度猜的）
        order = sorted(groups, key=lambda name: min(rank.get(r, 0) for r in groups[name]))
        # "系统文本"与剧情无关：它只受界面术语一致性约束，放进第一批
        order = [self.UNCHAPTERED] * (self.UNCHAPTERED in groups) + [
            name for name in order if name != self.UNCHAPTERED
        ]

        # ---- 章内：按**知识边**分波（给了 flow 就用它，没给就照旧的依赖分层）--------
        # 一个区域的前驱 = 它里面出现的那些实体的引入场 —— 见 layers/entityflow.py。
        # 章与章之间照旧按章序（一章翻完再下一章），所以波次是"章内深度 + 章序偏移"。
        knowledge = {name: set() for name in regions}
        used_flow = bool(flow is not None and getattr(flow, "entities", None))
        if used_flow:
            for region, providers in (flow.predecessors or {}).items():
                if region in knowledge:
                    knowledge[region] = {p for p in providers if p in knowledge}

        waves: dict[int, list[str]] = {}
        offset = 0
        span_of: dict[str, int] = {}
        for ordinal, name in enumerate(order):
            members = groups[name]
            base = min(rank.get(r, 0) for r in members)
            if used_flow:
                # 章内：知识边分波（一个区域等它引用的实体的引入场）
                depth = {
                    region: index
                    for index, wave in enumerate(
                        waves_from_predecessors(knowledge, sorted(members))
                    )
                    for region in wave
                }
                span = max(depth.values(), default=-1) + 1
                shift = offset  # 章与章**串行**：一章的下一波排在上一章之后
                offset += span
            else:
                # 老行为：章内照搬依赖分层，章间距 ordinal*lag
                depth = {r: rank.get(r, base) - base + 1 for r in members}
                span = max(depth.values(), default=0)
                shift = 0 if name == self.UNCHAPTERED else ordinal * self.lag
            for region in members:
                waves.setdefault(depth.get(region, 0) + shift, []).append(region)
            span_of[name] = span

        reason = (
            "章内按知识边分波、章间按章序" if used_flow else f"各章内部的第 N 层并行开工"
        )
        phases = [
            PlanPhase(
                name=f"wave-{index}",
                regions=sorted(waves[index], key=lambda r: (rank.get(r, 0), r)),
                reason=reason,
            )
            for index in sorted(waves)
        ]

        # 放松了哪些跨章边 —— 记账，不静默
        chapter_index = {name: i for i, name in enumerate(order)}
        cross = 0
        for dependency in graph.ordering_dependencies():
            provider = graph.as_region(dependency.source)
            consumer = graph.as_region(dependency.target)
            if provider == consumer or provider not in chapter_of or consumer not in chapter_of:
                continue
            if chapter_index[chapter_of[provider]] < chapter_index[chapter_of[consumer]]:
                cross += 1

        notes = [
            f"章（按图上出现顺序）：{'、'.join(name for name in order if name != self.UNCHAPTERED)}。",
        ]
        if used_flow:
            readings = dict(getattr(flow, "readings", dict)() or {})
            notes += [
                "章内顺序不再取自控制边，而是**按知识边**：一个区域的前驱 = 它引用的那些"
                "实体的引入场（见 layers/entityflow.py）。章与章之间照旧按章序，一章翻完再下一章"
                "（章间是**波次偏移**，不是前驱边；所以轮数 = 各章波数之和，而 plan 里的"
                "`critical_path` 是同一张前驱表里最长的那条链，不含这个偏移）。",
                f"这一轮算进去 {readings.get('entities', 0)} 个实体 / "
                f"{readings.get('regions_without_predecessors', 0)} 个区域没有前驱；"
                f"控制边里那些纯过场（载荷 0）自然不在前驱里，不需要逐条放松。",
            ]
            if cross:
                notes.append(
                    f"（跨章的 control 边 {cross} 条不再逐场串行 —— 章间由**章序**兜住。）"
                )
            if readings.get("ambiguous"):
                notes.append(
                    "这些实体的\"最早出现\"落在同一层里，谁当引入场是按区域名定的"
                    f"（约定含糊处，如实报出）：{'、'.join(readings['ambiguous'][:8])}。"
                )
            if readings.get("never_seen"):
                notes.append(
                    f"术语书里有 {len(readings['never_seen'])} 行在剧情场原文里一次都没出现，"
                    "它们不产生前驱。"
                )
        else:
            notes += [
                f"跨章的 sequencing 边 {cross} 条被放松 —— 章与章之间改由章摘要 + 全局硬约束接力，"
                "不再逐场串行（这是本策略唯一放松的东西）。",
                "章内顺序原样保留（取自 region_layers，环的缩点与汇合点屏障一并沿用）。",
            ]
        if self.UNCHAPTERED in groups:
            notes.append(
                f"{len(groups[self.UNCHAPTERED])} 个区域没有章归属，作为「{self.UNCHAPTERED}」放在第一批。"
            )
        if mixed:
            notes.append(f"这些区域里的单元跨了多个章，按首次出现的章算：{'、'.join(mixed)}。")
        notes.append(
            f"代价与对照：lag={self.lag}；轮数 {len(phases)}。"
        )

        return TranslationPlan(phases=phases, strategy=self.name, notes=notes)


#: 默认调度策略：章并行。**为什么是它**：真靶工程上严格分层 32 轮里 26 轮
#: 只有一个区域在干等，而章并行放松 2 条跨章边就砍到 15 轮。它对没申报章的项目
#: 自动退回严格分层（见 :meth:`ChapterParallelScheduler.plan`），所以当默认是
#: 安全的：行为只在"项目申报过章"这一点上与旧默认不同。
DEFAULT_SCHEDULER: Scheduler = ChapterParallelScheduler()

#: 全部可选的调度策略。**只有这一处**列"有哪些策略" —— 命令行、`plan` 命令与
#: `translate` 的可选参数都从这里取，免得"列出来的能选、选得到的没列"。
SCHEDULERS: tuple[Scheduler, ...] = (
    LayeredScheduler(),
    SeedBulkScheduler(),
    SinglePhaseScheduler(),
    ChapterParallelScheduler(),
)


def default_scheduler() -> Scheduler:
    return DEFAULT_SCHEDULER


def scheduler_named(name: str) -> Scheduler:
    """按名字取调度策略。**不替调用方挑一个"更好的"** —— 没点名就用默认（照搬依赖分层）。

    不认识的名字**报错**，不静默退回默认：静默退回等于"我要的调度没生效，
    而报告上的策略名还是我点的那一个"，这类事故查起来最费劲。
    """
    wanted = str(name or "").strip()
    if not wanted:
        return DEFAULT_SCHEDULER
    for scheduler in SCHEDULERS:
        if scheduler.name == wanted:
            return scheduler
    raise ValueError(
        f"没有这个调度策略：{wanted!r}（可用：{', '.join(s.name for s in SCHEDULERS)}）"
    )
