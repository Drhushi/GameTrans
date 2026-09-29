"""实体流图 → 前驱集：由**读出来的事实**算出"谁该等谁"。

**它是什么。** 术语书告诉我们这本书里有哪些**实体**（一行一个实体，若干写法）；
图上的原文告诉我们每个写法**出现在哪些区域**。这两样合起来就是**知识边**：
「这个实体的引入场 → 之后用到它的区域」，方向标记 ``agent``（读出来的，不是猜的，
与启发式猜的 ``dependency`` 边分开 —— 见 AGENTS 的术语表）。

**它算出的东西只有一张表**：每个区域的**前驱集**。规则两句话：

    一个区域要等的，是它引用的**每个实体的引入场**（那个实体第一次出现的那一场）；
    后面那些"又提到同一个词、但不是最早"的场，一条都不算前驱。

所以一个区域可以等好几个：`Eve` 最早在第 1 场交代、`Saki` 最早在第 2 场，那引用这两个
实体的那一场就等 `{1, 2}`。载荷为 0 的控制流边（纯过场）**自然就不在前驱里**了 ——
没有阈值、没有"放松"这个动作，是同一套规则的自然结果。

**声明会再化简一次**（谁等谁不变）：如果某个引入场本身就要等另一个引入场（1 → 2），
那"等 2"已经蕴含"等 1"，声明里不重复写它。传递闭包逐条相同，只是边少了 ——
不然一个后场会挂上七八条边，图看上去"全连上了"。

**口径（三处约定，都在这里写清）**：

* **命中口径**照抄摘要侧候选那套（:func:`gametrans.layers.entities._term_pattern`）：
  下划线当空格、大小写不敏感、两侧不许贴着字母 —— 原文里写 ``Saki_Natsume`` 也算
  ``Saki Natsume`` 这个写法出现过。它比注入用的整词口径宽一格，因为这里要的是
  "这个词在全靶里出现过几次"，不是"这句话要不要注入它"。
* **只算剧情场**（``unit.metadata["content_class"] == "story"``）：界面 / 工具场既不
  当引入场，也不产生前驱 —— 那些杂项文本走另一条线。
* **"最早"按区域层序**（:meth:`PathGraph.region_layers`，那是控制边算出来的，所以
  不循环）。同层内的先后只是排序，不是事实 —— 见 ``readings()["ambiguous"]``：
  有多少实体的"最早"落在同层并列里，那是这条约定真正含糊的地方，要看得见。

**不落盘**：它是图的派生视图，随算随出。``scan`` 会重建整张图，写进去的边下次就没了；
而这张表零模型成本、可复现，算一次是秒级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gametrans.core.graph import PathGraph
from gametrans.layers.entities import _term_pattern

__all__ = [
    "KnowledgeFlow",
    "apply_payloads",
    "build",
    "chapter_hints",
    "longest_chain",
    "region_order",
    "story_regions",
]


def story_regions(graph: PathGraph) -> set[str]:
    """哪些区域是**剧情场**（界面 / 工具场不参与知识边）。"""
    found: set[str] = set()
    for node in graph.nodes.values():
        unit = node.unit
        if unit is None:
            continue
        region = graph.region_of(node.node_id)
        if not region:
            continue
        if str((unit.metadata or {}).get("content_class") or "") == "story":
            found.add(region)
    return found


def region_order(graph: PathGraph) -> dict[str, int]:
    """区域的**层序**（层号摊平成一维）。"最早出现"就是按它比大小。

    用控制边算出来的分层（:meth:`PathGraph.region_layers`）—— 它是事实，不依赖知识边，
    所以这里不会绕回去。同层里按区域名排序，只是为了让结果可复现。
    """
    order: dict[str, int] = {}
    for index, layer in enumerate(graph.region_layers()):
        for region in sorted(layer):
            order.setdefault(region, index * 1000 + len(order))
    return order


@dataclass
class KnowledgeFlow:
    """实体流图的一次读数（不落盘）。

    * ``entities``：行身份（术语书那一行的第一个写法）→ 出现过的剧情区域；
    * ``home``：同上 → **引入场**（层序最早的那个）；
    * ``predecessors``：区域 → 前驱区域（**这就是新前驱集**）；
    * ``by_region``：区域 → 这一场里出现过的实体（给人看"为什么等它"）；
    * ``payloads``：``(提供者, 消费者) → 载荷``（**边权重取的就是它**）。
    """

    entities: dict[str, list[str]] = field(default_factory=dict)
    home: dict[str, str] = field(default_factory=dict)
    predecessors: dict[str, set[str]] = field(default_factory=dict)
    by_region: dict[str, list[str]] = field(default_factory=dict)
    #: 区域 → 这一场原文里出现过的实体。**不化简**的反查索引，载荷从它算。
    mentions: dict[str, set[str]] = field(default_factory=dict)
    #: ``(提供者区域, 消费区域) → 载荷``：提供者**首次提供**、消费区域原文里真用到的实体
    #: 条目数。与 ``predecessors``（化简过的前驱集）**不是一回事** —— 这里只回答"这条边
    #: 传了几条知识"。化简掉的前驱照样有载荷：它确实传了东西，只是被更晚的引入场蕴含、
    #: 不必单独等。所以载荷按**未化简**的原始关系算，与边存在与否解耦。
    payloads: dict[tuple[str, str], int] = field(default_factory=dict)
    #: 实体 → 那些"正文里没写名字、靠**说话人**认出来"的区域。给人看"它为什么有前驱"：
    #: 真靶 `evesip2` 的前驱就是靠这一路才接上的（Eve 说了话，正文里没有人名）。
    by_speaker: dict[str, list[str]] = field(default_factory=dict)
    #: "最早"落在同层并列里的实体（约定含糊的地方，要看得见）
    ambiguous: list[str] = field(default_factory=list)
    #: 被"每个区域只留最早一条"省掉的前驱条数（放弃的"必须等"，如实报出）
    skipped_predecessors: int = 0
    #: 术语书里没有写法的行、以及原文里一次都没出现过的行（如实报出，不静默丢）
    no_writing: int = 0
    never_seen: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.entities

    def why(self, region: str) -> list[tuple[str, str]]:
        """这个区域"为什么等它"：``(前驱区域, 这个实体是它第一次交代的)`` 逐条列。"""
        found: list[tuple[str, str]] = []
        for entity in self.by_region.get(region, ()):
            home = self.home.get(entity)
            if home and home != region and home in self.predecessors.get(region, set()):
                found.append((home, entity))
        return found

    def payload_of(self, source: str, target: str) -> int:
        """一条边传了几条知识：``(提供者, 消费者)`` → 条目数。没有关系就是 0。

        取不到 = 这两个区域之间没有"提供者首见 → 消费者原文里用到"的关系。**不区分
        "传 0 条"与"没有边"**：值只加不减，进表就 ≥ 1，所以 0 只有一个含义。
        """
        return int(self.payloads.get((source, target), 0))

    def readings(self) -> dict[str, Any]:
        """给人看的读数：算进去多少、还剩多少含糊、前驱集有多大、载荷有多大。"""
        sizes = sorted((len(v) for v in self.predecessors.values()), reverse=True)
        return {
            "entities": len(self.entities),
            "homes": len(set(self.home.values())),
            "regions_with_predecessors": sum(1 for v in self.predecessors.values() if v),
            "regions_without_predecessors": sum(
                1 for v in self.predecessors.values() if not v
            ),
            "predecessors_max": sizes[0] if sizes else 0,
            #: 载荷（边权重）：有几对区域之间有知识流、一共几条、单边最大几条
            "payload_edges": len(self.payloads),
            "payload_total": sum(self.payloads.values()),
            "payload_max": max(self.payloads.values(), default=0),
            #: 靠**说话人**（正文里没人名）认出来的引用：几个实体、几处
            "speaker_entities": len(self.by_speaker),
            "speaker_regions": sum(len(v) for v in self.by_speaker.values()),
            "ambiguous": list(self.ambiguous),
            "skipped_predecessors": int(self.skipped_predecessors),
            "never_seen": list(self.never_seen),
            "no_writing": int(self.no_writing),
        }


def _transitive_predecessors(
    predecessors: dict[str, set[str]]
) -> dict[str, set[str]]:
    """每个区域的**传递前驱**（含间接的）。"谁等谁"的闭包，化简时用它判蕴含。"""
    closed: dict[str, set[str]] = {}
    for start in predecessors:
        seen: set[str] = set()
        stack = list(predecessors.get(start, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(predecessors.get(node, ()))
        closed[start] = seen
    return closed


def longest_chain(predecessors: dict[str, set[str]], regions: list[str]) -> list[str]:
    """这张前驱集里**最长的那条链**（区域级，一条即可）。

    波次是它的拓扑分解，所以链长 ≤ 波数；两者一起看才知道"最少几轮"与"哪几场连成一条"。
    环里的区域同属一波（见 :func:`gametrans.core.schedule.waves_from_predecessors`），
    链不会在环里绕圈。
    """
    from gametrans.core.schedule import waves_from_predecessors

    best: dict[str, list[str]] = {}
    for wave in waves_from_predecessors(predecessors, regions):
        for region in wave:
            chain = [region]
            for parent in sorted(predecessors.get(region, ())):
                if parent in best and len(best[parent]) + 1 > len(chain):
                    chain = best[parent] + [region]
            best[region] = chain
    return max(best.values(), key=len) if best else []


def speaker_names(unit: Any) -> set[str]:
    """这一场里**说过话的人**（显示名）。

    为什么它算"引用"：Ren'Py 的对白把说话人与正文分开（`e "..."` 里正文只有台词），
    所以一场戏里的人名常常**只在说话人栏**。真靶工程的 `evesip2` 就是这样：
    7 个槽位里 5 条是 `Eve` 说的、2 条是主角说的，正文一个名字都没有 ——
    只认原文命中时它成了孤岛（谁也不等、被排到最前面），而它明明接着前面的剧情。

    只取**显示名**（`display_speaker` / `context.characters` / `context.speaker`）：
    槽位里的 `speaker` 是引擎变量名（`e` / `mc`），拿它去比术语书是错的。
    """
    names: set[str] = set()
    context = getattr(unit, "context", None)
    if context is not None:
        speaker = str(getattr(context, "speaker", "") or "").strip()
        if speaker:
            names.add(speaker)
        for name in getattr(context, "characters", None) or ():
            if str(name or "").strip():
                names.add(str(name).strip())
    payload = getattr(getattr(unit, "locator", None), "payload", None) or {}
    for entry in payload.get("slots") or []:
        if not isinstance(entry, dict):
            continue
        display = str(entry.get("display_speaker") or "").strip()
        if display:
            names.add(display)
    return names


def build(graph: PathGraph, termbook: Any, *, story_only: bool = True) -> KnowledgeFlow:
    """算一次实体流图与它给出的前驱集。**零模型成本**。

    ``termbook`` 只用到两件事：每一行有哪些写法（``entry.writings``）。行的身份取第一个
    写法（与面板、变更日志同一个口径）。

    **"引用"有两路**（缺一路就会出现上一段说的孤岛）：

    * **原文命中** —— 这一场的原文里真的写着这个写法（与注入同一口径）；
    * **说话人命中** —— 这一场里这个人物**说了话**（显示名与某个写法同名）。
      只在没被原文命中时才记进 ``by_speaker``，好让"它为什么有前驱"说得清。
    """
    flow = KnowledgeFlow()
    if graph is None or termbook is None:
        return flow
    allowed = story_regions(graph) if story_only else {r.region_id for r in graph.regions()}
    if not allowed:
        return flow
    order = region_order(graph)

    units: list[tuple[str, str]] = []
    speakers: dict[str, set[str]] = {}
    for node in graph.nodes.values():
        unit = node.unit
        if unit is None:
            continue
        region = graph.region_of(node.node_id)
        if region in allowed:
            units.append((region, str(unit.source or "")))
            names = speaker_names(unit)
            if names:
                speakers.setdefault(region, set()).update(names)

    for entry in getattr(termbook, "entries", lambda: [])():
        writings = [str(writing) for writing in getattr(entry, "writings", []) or [] if writing]
        if not writings:
            flow.no_writing += 1
            continue
        patterns = [_term_pattern(writing) for writing in writings]
        lowered = {writing.strip().lower() for writing in writings}
        seen: list[str] = []
        from_speaker: list[str] = []
        for region, source in units:
            if region in seen:
                continue
            if any(pattern.search(source) for pattern in patterns):
                seen.append(region)
                continue
            # 原文里没写这个名字 —— 但这一场里他/她说了话，那也算用到了这个实体
            if lowered & {
                name.strip().lower() for name in speakers.get(region, ())
            }:
                seen.append(region)
                from_speaker.append(region)
        if not seen:
            flow.never_seen.append(str(getattr(entry, "writing", "") or writings[0]))
            continue
        seen.sort(key=lambda region: order.get(region, 10 ** 9))
        flow.entities[entry.writing] = seen
        if from_speaker:
            flow.by_speaker[entry.writing] = sorted(
                from_speaker, key=lambda region: order.get(region, 10 ** 9)
            )

    for entity, regions in flow.entities.items():
        flow.home[entity] = regions[0]
        for region in regions:
            flow.mentions.setdefault(region, set()).add(entity)
    # 载荷（边权重）：提供者首见 → 消费者原文里用到。**不化简** —— 化简那一刀砍的是
    # "要不要单独等它"（前驱集），不是"它传了几条知识"。
    for entity, regions in flow.entities.items():
        home = flow.home[entity]
        for region in regions:
            if region == home:
                continue
            key = (home, region)
            flow.payloads[key] = flow.payloads.get(key, 0) + 1
    for region in allowed:
        flow.predecessors.setdefault(region, set())
    # **每个实体只连它的引入场**：后面那些"又提到同一个词、但不是最早"的场（第 3、4 场）
    # 一条都不连 —— 它们不是谁的前驱。于是一个区域要等的是"它引用的每个实体的引入场"，
    # 可以是好几个（`Eve` 最早在第 1 场、`Saki` 最早在第 2 场 → 它等 1 和 2）。
    fan_in: dict[str, set[str]] = {region: set() for region in allowed}
    for entity, regions in flow.entities.items():
        for region in regions:
            if region == flow.home[entity]:
                continue
            fan_in.setdefault(region, set()).add(flow.home[entity])
            flow.by_region.setdefault(region, [])
            if entity not in flow.by_region[region]:
                flow.by_region[region].append(entity)
    # 再化一次简：某个引入场如果**本身就要等**另一个引入场（1 → 2），那"等 2"已经
    # 蕴含"等 1" —— 声明里不必重复。**谁等谁没变**（传递闭包逐条相同），只是不再重复写。
    # 真靶上这一刀把 122 条声明压到下面这个数（读数里报出来）。
    reached = _transitive_predecessors(fan_in)
    for region, providers in fan_in.items():
        kept: list[str] = []
        # 从**最晚**的引入场往早里走：一个引入场只要被已留下的某个引入场传递依赖着，
        # 那"等那个晚的"就已经把它等掉了，声明里不再重复（这就是化简那一刀）。
        for candidate in sorted(providers, key=lambda name: -order.get(name, 10 ** 9)):
            if any(candidate in reached.get(name, set()) for name in kept):
                flow.skipped_predecessors += 1
                continue
            kept.append(candidate)
        if kept:
            flow.predecessors[region] = set(kept)
    #: `by_region` 只留"这条边真的交代了"的实体（与最终前驱集对齐，免得读数自相矛盾）
    for region in list(flow.by_region):
        kept = flow.predecessors.get(region) or set()
        writings = [
            entity
            for entity in flow.by_region[region]
            if flow.home.get(entity) in kept
        ]
        if writings:
            flow.by_region[region] = writings
        else:
            flow.by_region.pop(region, None)

    # 同层并列：某实体的最早出现，落在同一层里有不止一个候选区域 —— 那种情况下
    # "谁当引入场"是按区域名定的，不是读出来的事实。逐条报出来。
    layer_of: dict[str, int] = {}
    for index, layer in enumerate(graph.region_layers()):
        for region in layer:
            layer_of.setdefault(region, index)
    for entity, regions in flow.entities.items():
        best = layer_of.get(regions[0], 10**9)
        if sum(1 for region in regions if layer_of.get(region, 10**9) == best) > 1:
            flow.ambiguous.append(entity)
    # 章戳核对（拿知识边投票定章）**刻意不做**：实测两个方向都有系统性偏差 ——
    # 拿"引入者"投票会把第 2、3 章全判成第 1 章（主角团都在第 1 章交代），
    # 拿"消费者"投票会把第 1 章判成第 2、3 章（后面的章反复引用前面的东西），
    # 而未入章的分支恰好一个消费者都没有。章界继续当**项目申报的事实**。
    return flow


def apply_payloads(
    graph: PathGraph, flow: KnowledgeFlow, *, source: str = ""
) -> dict[str, int]:
    """把**载荷**写进每条边的 ``weight`` —— 这是唯一一处写载荷的地方。

    为什么必须单独一步、不能在建图时写：引擎提取阶段**不认识术语书**（它只扫源码），
    而载荷是"术语书 × 原文命中"算出来的。所以扫出来的边一律 ``weight=0``，等这一步
    按当前术语书填。含义是"这条边一条知识都没传"，不是"关系不强"。

    写的是 ``weight``，**不动** ``topics`` / ``direction`` / ``provenance``：载荷按
    **未化简**的"首见 → 用到"算（被化简掉的前驱照样有载荷），``topics`` 只列这条边
    自己声明的那几条 —— 两者不是同一个数。

    同一区域内部的边跳过（自环；并列分支常常也在同一区域里）：自己不向自己交代知识。

    ``source`` 是**这批载荷按哪一版术语书算的**（术语书的版本戳），写进
    ``graph.metadata["payload_source"]``。载荷是派生量，术语书一动它就过期 —— 有了戳，
    读取方（plan / 面板）能自己比对出来，不用猜"盘上这个数还算不算数"。

    返回读数：扫过几条边、其中几条**非零**、一共几条知识、按哪一版算的。
    """
    total = 0
    loaded = 0
    entries = 0
    for dependency in graph.dependencies:
        provider = graph.as_region(dependency.source)
        consumer = graph.as_region(dependency.target)
        if provider == consumer:
            continue
        load = flow.payload_of(provider, consumer)
        dependency.weight = load
        total += 1
        entries += load
        if load:
            loaded += 1
    graph.metadata["payload_source"] = str(source)
    return {
        "edges": total,
        "loaded_edges": loaded,
        "entries": entries,
        "source": str(source),
    }
