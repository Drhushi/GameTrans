"""带权路径图：提取层交给翻译层的逻辑结构。

提取层不返回"一堆字符串"，而是返回一张**带权路径图**。图上同时存在两种关系：

* **结构边**（``PathNode.parent / children``）—— 文件 → label → menu → 语句的包含关系，
  用来定位和写回。
* **依赖边**（:class:`~gametrans.core.models.GraphEdge`）—— A → B 表示"翻译 B 时，
  需要 A 已经交代过的知识"。**这才是这张图真正的价值**：它把传统机器翻译"不识上下文"
  这个漏洞变成了一个可以求解的排序问题。

节点权重的主维度是**成本**（翻译开支），不是重要性。因为每个节点都得翻一次，总成本
基本固定 —— 能优化的不是成本，而是**上下文可得性**：保证翻到某个节点时，它依赖的
知识已经被生产出来了。

调度单位是**区域**（:class:`Region`）：一个 label，或者一个文件里不属任何 label 的
模块文本（``define`` / ``strings``）。区域之间互不依赖的部分天然可以并行。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Iterable, Iterator

from gametrans.core.models import (
    ORDERING_DIRECTIONS,
    EdgeType,
    GraphEdge,
    NodeKind,
    NodeWeight,
    PathNode,
    TranslationUnit,
    estimate_tokens,
)


@dataclass
class Region:
    """调度单位：一个 label，或一个文件级模块区。

    区域是**派生**的（由 :meth:`PathGraph.regions` 从结构图算出来），不是持久化实体 ——
    这样结构一旦变化，区域自动跟着变，不会出现两份需要同步的真相。
    """

    region_id: str
    path: str
    #: "label" | "module"
    kind: str
    node_ids: list[str] = field(default_factory=list)
    cost: int = 0

    @property
    def size(self) -> int:
        return len(self.node_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "cost": self.cost,
            "node_ids": list(self.node_ids),
        }


@dataclass
class PathGraph:
    """一张带权路径图。引擎无关：``engine`` 只是个名字标签。"""

    engine: str
    roots: list[str] = field(default_factory=list)
    nodes: dict[str, PathNode] = field(default_factory=dict)
    #: 区域之间的信息依赖。结构边在节点里，语义边在这里，刻意分开。
    dependencies: list[GraphEdge] = field(default_factory=list)
    #: 图级的事实（引擎申报、不属于任何单个节点的东西）。当前一项：
    #: ``characters`` —— **角色变量 → 显示名**。落盘的理由：读原始骨架的消费者
    #: （摘要脚本）拿到的是变量名，只有这张表能换成显示名；不落盘的话每个消费者
    #: 都得自己再解析一遍源码。
    metadata: dict[str, Any] = field(default_factory=dict)

    #: :meth:`regions` / :meth:`region_ids` / :meth:`region_of` 的进程内缓存：
    #: ``(节点集合版本, 区域表, 区域 id 集合, 节点 → 区域 id)``。
    #:
    #: 为什么需要：:meth:`as_region` 每条边都要问一次"这个端点算不算区域名"，
    #: :meth:`region_of` 更是被逐边、逐节点地调 —— 而它们原先每次都重算整张区域表
    #: （``walk()`` + 逐节点推身份）。这些逐边循环（分层、取知识点、投影依赖）于是
    #: 变成 O(边 × 节点)：实测 2,048 个单元的线性工程里 ``region_layers()`` 要
    #: 23.7 秒，补上这一层缓存后 41 毫秒。
    #:
    #: 失效只认"节点集合变没变"（区域身份由节点结构决定，边不影响它）。节点在注册
    #: 之后不再原地改写 —— 这是 :class:`PathGraphBuilder` 的用法约定。
    _regions_cache: tuple[tuple[int, int], list[Region], set[str], dict[str, str]] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    # ---- 查询 ---------------------------------------------------------------

    def get(self, node_id: str) -> PathNode:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise KeyError(f"路径图中不存在节点：{node_id}") from None

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)

    def walk(self) -> list[str]:
        """确定性深度优先遍历，返回 node_id 序列。"""
        order: list[str] = []
        seen: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in seen:
                return
            seen.add(node_id)
            order.append(node_id)
            for child in self.nodes[node_id].children:
                visit(child)

        for root in self.roots:
            visit(root)
        # 兜底：父节点缺失的孤儿节点也要被走到，否则 agent 会看到"图里有但遍历不到"
        for node_id in self.nodes:
            visit(node_id)
        return order

    def translatable_nodes(self) -> list[PathNode]:
        """全部可译节点，按权重降序（优先级 → 体积 → 深度），路径兜底稳定排序。"""
        nodes = [n for n in self.nodes.values() if n.is_translatable]
        nodes.sort(key=lambda n: (n.weight.sort_key, n.path))
        return nodes

    def translatable_units(self) -> list[TranslationUnit]:
        return [n.unit for n in self.translatable_nodes() if n.unit is not None]

    def batches(self, size: int) -> list[list[TranslationUnit]]:
        """按权重顺序切批，供翻译层串行或并行消费。"""
        if size <= 0:
            raise ValueError("批次大小必须为正整数")
        units = self.translatable_units()
        return [units[i : i + size] for i in range(0, len(units), size)]

    def iter_nodes(self) -> Iterator[PathNode]:
        for node_id in self.walk():
            yield self.nodes[node_id]

    def stats(self) -> dict[str, Any]:
        """给 agent 与交互层看的形状摘要。

        除形状之外还报四个**可核对的读数**（论文 §4.0 那张表的数字必须能从产品
        报告里取到，不能只有一次性探针算得出来）：

        * ``ordering_edges`` / ``ordering_edges_on_nodes``：有多少条边能定先后、
          其中有多少真的落到了本图的节点/分组上（两个数不相等 = 命名空间对不上，
          分层会"一条边都看不见"）；
        * ``region_layers``：分了几层（恒为 1 就说明先后信息全丢了）；
        * ``dangling_edges``：端点找不到落点的边。
        """
        by_kind: dict[str, int] = {}
        char_count = 0
        max_depth = 0
        for node in self.nodes.values():
            max_depth = max(max_depth, node.weight.depth)
            if node.is_translatable:
                by_kind[node.kind.value] = by_kind.get(node.kind.value, 0) + 1
                char_count += node.weight.char_count
        translatable = sum(by_kind.values())
        ordering_total, ordering_on_nodes = self.ordering_edges_land_on_regions()
        branches, joins = self.flow_degrees()
        return {
            "engine": self.engine,
            "nodes": len(self.nodes),
            "translatable": translatable,
            "containers": len(self.nodes) - translatable,
            "by_kind": dict(sorted(by_kind.items())),
            "char_count": char_count,
            "max_depth": max_depth,
            "roots": len(self.roots),
            "regions": len(self.regions()),
            "dependencies": len(self.dependencies),
            "ordering_edges": ordering_total,
            "ordering_edges_on_nodes": ordering_on_nodes,
            "region_layers": len(self.region_layers()),
            "dangling_edges": len(self.dangling_dependencies()),
            # 分支 / 汇合：C2（汇合冲突）的材料就在这两个读数里
            "branch_points": branches,
            "join_points": joins,
            "cost": self.total_cost(),
        }

    # ---- 成本 ---------------------------------------------------------------

    def total_cost(self) -> int:
        """整张图的 token 估算：各单元的（输入 + 输出）之和，**一次直出**的口径。

        不含每次调用固定要付的上下文，也不含分轮重复 —— 那些取决于运行参数，
        见 `plan` 的 ``token_estimate``。
        """
        return sum(n.weight.cost for n in self.translatable_nodes())

    def cost_of(self, node_ids: Iterable[str]) -> int:
        total = 0
        for node_id in node_ids:
            node = self.nodes.get(node_id)
            if node is not None and node.is_translatable:
                total += node.weight.cost
        return total

    # ---- 区域 ---------------------------------------------------------------

    def _owning_ancestor(self, node: PathNode, kind: NodeKind) -> PathNode | None:
        seen: set[str] = set()
        current = node
        while current.parent is not None and current.parent not in seen:
            seen.add(current.parent)
            parent = self.nodes.get(current.parent)
            if parent is None:
                return None
            if parent.kind is kind:
                return parent
            current = parent
        return None

    @staticmethod
    def _module_region_id(file_node: PathNode) -> str:
        rel = str(file_node.metadata.get("relpath") or file_node.path)
        return PurePosixPath(rel).stem or "module"

    @staticmethod
    def _label_region_id(label: PathNode) -> str:
        """label 节点的区域 id。

        优先用引擎填的 ``metadata["label"]``；没有就从路径尾部推导 —— 区域 id 不该
        依赖一个可选字段，否则同一张图在不同引擎包里会长出不同的区域名。
        """
        name = label.metadata.get("label")
        if name:
            return str(name)
        tail = label.path.rsplit("#", 1)[-1]
        return tail or label.node_id

    def _region_identity(self, node: PathNode) -> tuple[str, str, str]:
        """返回 ``(region_id, path, kind)``。"""
        label = self._owning_ancestor(node, NodeKind.LABEL)
        if label is not None:
            return self._label_region_id(label), label.path, "label"
        file_node = self._owning_ancestor(node, NodeKind.FILE)
        if file_node is None:
            return "unknown", node.path, "module"
        return self._module_region_id(file_node), file_node.path, "module"

    def regions(self) -> list[Region]:
        """把可译节点按区域归拢，顺序即阅读顺序（首次出现的位置）。

        区域顺序有意义：依赖边的方向是按阅读顺序推出来的（先出现的交代后出现的）。

        返回的是缓存里的那一份，**当只读用**；区域表只在节点集合变化时重算
        （见 :attr:`_regions_cache`）。
        """
        key = (id(self.nodes), len(self.nodes))
        cached = self._regions_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        found: "OrderedDict[str, Region]" = OrderedDict()
        region_of: dict[str, str] = {}
        for node_id in self.walk():
            node = self.nodes[node_id]
            if not node.is_translatable:
                continue
            region_id, path, kind = self._region_identity(node)
            #: 节点 → 区域（**不带**同名错开后缀）：与 ``region_of()`` 的语义一致
            region_of[node_id] = region_id
            region = found.get(region_id)
            if region is not None and region.kind != kind:
                # label 与模块区同名时错开，避免两个不同东西被并成一个区域
                region_id = f"{region_id}:{kind}"
                region = found.get(region_id)
            if region is None:
                region = Region(region_id=region_id, path=path, kind=kind)
                found[region_id] = region
            region.node_ids.append(node_id)
            region.cost += node.weight.cost
        table = list(found.values())
        self._regions_cache = (key, table, {r.region_id for r in table}, region_of)
        return table

    def region_of(self, node_id: str) -> str | None:
        """某个**可译节点**属于哪个区域。容器节点不属于任何区域。

        走 :meth:`regions` 的缓存：这个方法在逐边、逐节点的循环里被调用，
        每次重算区域表会让那些循环变成 O(节点²)。
        """
        node = self.nodes.get(node_id)
        if node is None or not node.is_translatable:
            return None
        self.regions()
        cache = self._regions_cache
        return cache[3].get(node_id) if cache is not None else None

    def level_paths(self, levels: Iterable[str]) -> dict[str, dict[str, str]]:
        """每个可译节点在各级结构容器下的**层级标识**，如 ``{"label": "safe1"}``。

        这是给适配层算分组键用的事实出口：**容器结构是内核知道的事实**
        （节点树与节点类别），"哪一级算一个翻译单元"仍然由适配层申报
        （``EngineSupportPack.grouping_levels``）。

        为什么必须由内核给：组键曾经取自提取层写进 ``unit.context.scene`` 的 label，
        而那段 context 只在槽位缝上源码时才有 —— 缝到 menu 行或完全缝不上的单元
        （真靶上 1,263 条）拿到 ``None``，组键退化成文件级、一条一组，
        于是出现"一次请求只翻一句话"。容器树不会因为缝不上而消失，所以它是可用的
        权威来源。级别名与容器类别的对应按**名字**取（适配层申报的层级名就是容器
        类别的名字，如 Ren'Py 的 ``label``／``file``、RPGM 的 ``file``）。
        """
        kinds: list[tuple[str, NodeKind]] = []
        for level in levels:
            try:
                kinds.append((str(level), NodeKind(str(level))))
            except ValueError:
                # 引擎自定义的层级（没有对应的容器类别）：内核如实不给这一级，
                # 由适配层回落到它自己能在别处找到的层级
                continue
        if not kinds:
            return {}
        found: dict[str, dict[str, str]] = {}
        for node_id in self.walk():
            node = self.nodes[node_id]
            if not node.is_translatable:
                continue
            chain = self._container_chain(node)
            paths: dict[str, str] = {}
            for name, kind in kinds:
                holder = chain.get(kind)
                if holder is not None:
                    paths[name] = self._level_identity(holder, kind)
            found[node_id] = paths
        return found

    def _container_chain(self, node: PathNode) -> dict[NodeKind, PathNode]:
        """从近到远的祖先容器（同一类别取最近的一个）。"""
        chain: dict[NodeKind, PathNode] = {}
        seen: set[str] = set()
        current = node
        while current.parent is not None and current.parent not in seen:
            seen.add(current.parent)
            parent = self.nodes.get(current.parent)
            if parent is None:
                break
            chain.setdefault(parent.kind, parent)
            current = parent
        return chain

    def _level_identity(self, container: PathNode, kind: NodeKind) -> str:
        """一级容器在分组键里的标识（内核只给稳定标识，不认识引擎语法）。"""
        if kind is NodeKind.LABEL:
            # 与区域 id 同源：`_label_region_id` 优先用引擎填的 label 名
            return self._label_region_id(container)
        rel = str(container.metadata.get("relpath") or "")
        if rel:
            return rel
        # 没有申报 relpath 的容器：把"直到它自己"的相对路径原样交出去
        # （根节点名不算在相对路径里 —— 组键不该带项目名）
        return self._relative_path(container)

    def _relative_path(self, node: PathNode) -> str:
        parts = [part for part in str(node.path).replace("\\", "/").split("/") if part]
        if self.roots:
            root = self.nodes.get(self.roots[0])
            if root is not None:
                name = str(root.path).replace("\\", "/").rstrip("/").split("/")[-1]
                if parts and parts[0] == name:
                    parts = parts[1:]
        return "/".join(parts)

    # ---- 依赖 ---------------------------------------------------------------

    def add_dependency(self, dependency: GraphEdge) -> GraphEdge:
        self.dependencies.append(dependency)
        return dependency

    def remove_dependency(self, provider: str, consumer: str) -> bool:
        before = len(self.dependencies)
        self.dependencies = [
            d for d in self.dependencies if not (d.source == provider and d.target == consumer)
        ]
        return len(self.dependencies) != before

    def region_ids(self) -> set[str]:
        """全部区域 id。走 :meth:`regions` 的缓存，**当只读用**。

        这个方法原先每条边调一次、每次都重算整张区域表 —— 那是分层与取知识点
        变成 O(边 × 节点) 的根源（见 :attr:`_regions_cache`）。
        """
        self.regions()
        cache = self._regions_cache
        return cache[2] if cache is not None else set()

    def prune_empty_containers(self) -> int:
        """删掉**没有内容的容器**（0 个子节点、自身也不承载单元），返回删掉的个数。

        为什么要有这一步：单元＝一个结构段之后，源码侧的 menu 容器不再挂任何东西
        —— 真靶 99 个 menu 容器里 **98 个是空壳**（另有 5 个空 label、12 个空 file）。
        留着它们会让"节点总数"骗人：用户看到的 209 个节点里有 115 个什么都不装。

        从叶子往上删：父容器可能因为子节点被删而变成空壳，下一轮再删它。
        **根不删**（一个空工程保留它的根）；承载单元的节点永不删。
        """
        removed = 0
        while True:
            doomed = [
                node_id
                for node_id, node in self.nodes.items()
                if node.unit is None and not node.children and node_id not in self.roots
            ]
            if not doomed:
                break
            for node_id in doomed:
                node = self.nodes.pop(node_id)
                removed += 1
                parent = self.nodes.get(node.parent) if node.parent else None
                if parent is not None and node_id in parent.children:
                    parent.children.remove(node_id)
        return removed

    def providers_of(self, region_id: str) -> list[GraphEdge]:
        """翻译这个分组之前，应该先翻哪些分组。

        边的端点有两种写法：**单元结点 id**（引擎控制流投影后就是这种）与
        **分组名**（人手工 `graph depend` 写的是这种）。这里两边都归一 ——
        只比字面时，单元端点的边一条都匹配不上，知识点因此永远取不到（R73）。
        """
        wanted = self.as_region(region_id)
        return [
            d
            for d in self.dependencies
            if d.target == wanted or self.region_of(d.target) == wanted
        ]

    def consumers_of(self, region_id: str) -> list[GraphEdge]:
        """这个分组交代的知识，会被哪些分组消费（端点归一规则同 :meth:`providers_of`）。"""
        wanted = self.as_region(region_id)
        return [
            d
            for d in self.dependencies
            if d.source == wanted or self.region_of(d.source) == wanted
        ]

    def topics_for(self, region_id: str) -> set[str]:
        """翻译这个分组时需要具备的知识点。"""
        topics: set[str] = set()
        for dependency in self.providers_of(region_id):
            topics.update(dependency.topics)
        return topics

    def topics_for_node(self, node_id: str) -> set[str]:
        """翻译某条文本时需要具备的知识点（看它所属分组）。"""
        region_id = self.region_of(node_id)
        return self.topics_for(region_id) if region_id else set()

    def dangling_dependencies(self) -> list[GraphEdge]:
        """端点在本图里**找不到任何落点**的边 —— 多半是 agent 或用户写错了名字。

        判据要认**两个命名空间**（单元结点 id 与分组名），因为两者都是契约允许的
        写法。只认分组名时，真靶 60 条引擎边会**全部**被报成悬空（R73）。
        """
        known = set(self.nodes) | self.region_ids()
        return [
            d for d in self.dependencies if d.source not in known or d.target not in known
        ]

    def ordering_dependencies(self) -> list[GraphEdge]:
        """方向已经确认、可以用来决定先后顺序的边。

        未确认的边（``direction="reading_order"``）只表示"两处有关系"，不参与排序 ——
        这正是并列支线不会被错排成串行链的原因。
        """
        return [d for d in self.dependencies if d.direction in ORDERING_DIRECTIONS]

    # ---- 协议视图 ----------------------------------------------

    def protocol_edges(self) -> list[GraphEdge]:
        """IR 视图下的全部边。

        就是适配器声明的那张边表 —— 内核不推导引擎结构语义（菜单分支、控制流
        先后都由适配器声明，见 ``engines/renpy/extractor.py``）。
        """
        return list(self.dependencies)

    def project_dependencies_onto_units(self) -> tuple[int, int]:
        """把依赖边落到**单元结点**上，返回 ``(投影的边数, 丢弃的自环数)``。

        图上的一条文本就是一个翻译单元。边的端点有两种写法：

        * 已经落在单元结点上 —— 保留；
        * 落在**区域**（引擎的场景/模块）上 —— 投影成该区域里**每个**单元的边。

        区域里有多个单元时**扇出**（每个单元都要知道"前一场戏交代过什么"），
        不是随手挑一个。投影后自己指自己的边丢掉：单元内部的关系已经在单元里了。

        没有可译结点的图原样返回 —— 一条边都没有可投影的对象。
        """
        unit_ids: set[str] = set()
        units_by_region: dict[str, list[str]] = {}
        for node_id in self.walk():
            node = self.nodes[node_id]
            if node.unit is None:
                continue
            unit_ids.add(node_id)
            region = self.region_of(node_id)
            if region:
                units_by_region.setdefault(region, []).append(node_id)
        if not unit_ids:
            return 0, 0

        def landing(endpoint: str) -> list[str]:
            if endpoint in unit_ids:
                return [endpoint]
            return units_by_region.get(endpoint, [])

        projected = 0
        self_loops = 0
        rebuilt: list[GraphEdge] = []
        seen: set[tuple[str, str, str]] = set()
        for edge in self.dependencies:
            sources = landing(edge.source)
            targets = landing(edge.target)
            if not sources or not targets:
                rebuilt.append(edge)  # 落不到单元上的如实留着，不假装
                continue
            for source in sources:
                for target in targets:
                    if source == target:
                        self_loops += 1
                        continue
                    key = (source, target, edge.type)
                    if key in seen:
                        continue
                    seen.add(key)
                    rewritten = replace(edge, source=source, target=target)
                    if source != edge.source or target != edge.target:
                        projected += 1
                    rebuilt.append(rewritten)
        self.dependencies = rebuilt
        return projected, self_loops

    def as_region(self, endpoint: str) -> str:
        """把一条边的端点归一到**区域**：是区域就原样返回，是单元结点就换成它所属的区域。

        为什么非要这一步：适配器投影依赖边时，端点可能落在**单元结点**上
        （真靶上就是这样：34 条控制流边的端点全是 `unit_xxx`），而区域身份是
        label / 模块名 —— 两套命名空间对不上时，区域分层会**把它们全部当成不存在**，
        于是"46 个区域、34 条边"的图算出来只有 1 个阶段，而报告上看不出任何异常。
        控制流是**区域级**的事实，单元级的 agent/启发式依赖同样该参与排序，
        所以统一在这里归一，而不是让每处调用方各自记得转换。
        """
        if endpoint in self.region_ids():
            return endpoint
        return self.region_of(endpoint) or endpoint

    def ordering_edges_land_on_regions(self) -> tuple[int, int]:
        """``(参与排序的边数, 真的落到区域上的边数)``。

        两个数不相等 = **边的端点命名空间与区域对不上** —— 那种情况下区域分层会
        把边全部当成不存在，算出"1 个阶段"，而报告上一切正常。真靶上就是这么发生的
        （34 条控制流边的端点是 `unit_xxx`，区域身份却是 label 名）。
        这个读数就是给报告点名用的。
        """
        regions = {r.region_id for r in self.regions()}
        total = 0
        landed = 0
        for dependency in self.ordering_dependencies():
            total += 1
            provider = self.as_region(dependency.source)
            consumer = self.as_region(dependency.target)
            if provider in regions and consumer in regions and provider != consumer:
                landed += 1
        return total, landed

    def flow_degrees(self) -> tuple[dict[str, int], dict[str, int]]:
        """``(分支点, 汇合点)``：出度 ≥2 与入度 ≥2 的节点 → 度数。

        **分支与汇合是"图依赖翻译机制"里必须能点名的东西**：汇合点就是
        ``IN(v) = JOIN({OUT(u)})`` 里那个 JOIN 发生的地方 —— 分支叙事里两条互斥路线
        可以各自给同一个实体定名，冲突只有在汇合处才看得见。它们以前只是"属性"、
        没有任何地方读，于是"汇合冲突"在图上没有材料（登记册 R81）。

        只数**能定先后的边**（``orders``），端点先归一到调度分组，自环不算。
        """
        out: dict[str, int] = {}
        into: dict[str, int] = {}
        for dependency in self.ordering_dependencies():
            provider = self.as_region(dependency.source)
            consumer = self.as_region(dependency.target)
            if provider == consumer:
                continue
            out[provider] = out.get(provider, 0) + 1
            into[consumer] = into.get(consumer, 0) + 1
        branches = {node: degree for node, degree in out.items() if degree >= 2}
        joins = {node: degree for node, degree in into.items() if degree >= 2}
        return (
            dict(sorted(branches.items(), key=lambda kv: (-kv[1], kv[0]))),
            dict(sorted(joins.items(), key=lambda kv: (-kv[1], kv[0]))),
        )

    def region_predecessors(self, *, include_weak: bool = False) -> dict[str, set[str]]:
        """区域 → 它的**直接前驱**区域（只认能定先后的边；自环与落到区域外的边丢掉）。

        这是"翻译顺序"里唯一的事实：**谁该等谁**。层、阶段都是从它派生出来的视图 ——
        可以随时从它再算出来，反过来算不回去。
        """
        known = {r.region_id for r in self.regions()}
        found: dict[str, set[str]] = {region: set() for region in known}
        source = self.dependencies if include_weak else self.ordering_dependencies()
        for dependency in source:
            provider = self.as_region(dependency.source)
            consumer = self.as_region(dependency.target)
            if provider in known and consumer in known and provider != consumer:
                found[consumer].add(provider)
        return found

    def predecessor_closure(self, region: str, *, include_weak: bool = False) -> set[str]:
        """该区域的**传递前驱**（不含自身）。上下文"该看得见谁"的作用域就是它。"""
        predecessors = self.region_predecessors(include_weak=include_weak)
        seen: set[str] = set()
        pending = list(predecessors.get(region, ()))
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(predecessors.get(current, ()))
        return seen

    def critical_path(self, *, include_weak: bool = False) -> list[str]:
        """最长依赖链（区域级，一条即可）。**它的长度就是最少要跑几轮。**

        按层序做一遍 DP（不递归 —— 长链上递归会撞 Python 的递归上限）。
        同层里互为上下文的区域（强连通分量缩过点）不参与跨层延展。
        """
        predecessors = self.region_predecessors(include_weak=include_weak)
        best: dict[str, list[str]] = {}
        for layer in self.region_layers(include_weak=include_weak):
            for region in layer:
                chain = [region]
                for parent in sorted(predecessors.get(region, ())):
                    candidate = best.get(parent, []) + [region]
                    if len(candidate) > len(chain):
                        chain = candidate
                best[region] = chain
        return max(best.values(), key=len) if best else []

    def region_layers(self, *, include_weak: bool = False) -> list[list[str]]:
        """按依赖深度给区域分层 —— **这是派生视图，不是机制**。

        机制是 :meth:`region_predecessors`（谁等谁）。分层只是它的一次拓扑分解：
        第 k 层是"最长依赖链长度为 k"的区域，**同一层内互不依赖**。所以它有两个正当用途：
        给人读"最少要跑几轮"（= 层数），以及给暂时还在用 barrier 的执行器分阶段。

        ``include_weak=True`` 会把方向未经确认的边也算进去，用来看"如果什么都信会长
        什么样"；默认只信确认过的方向。

        成环是常态（真实游戏里 label 互相 jump），所以先做**强连通分量缩点**再分层：
        互为上下文的区域落在同一层，它们本来就该一起翻。
        """
        regions = [r.region_id for r in self.regions()]
        known = set(regions)
        edges: dict[str, set[str]] = {r: set() for r in regions}
        source = self.dependencies if include_weak else self.ordering_dependencies()
        for dependency in source:
            provider = self.as_region(dependency.source)
            consumer = self.as_region(dependency.target)
            if provider in known and consumer in known and provider != consumer:
                edges[provider].add(consumer)
        if not any(edges.values()) and source:
            # 有边、却一条都落不到区域上 —— 命名空间又对不上了。
            # 静默产出"1 个阶段"正是本项目最怕的形态，所以这里留一个可查的读数
            # （`ordering_edges_land_on_regions()`），由报告点名。
            pass

        components = _strongly_connected(regions, edges)
        component_of = {node: i for i, comp in enumerate(components) for node in comp}

        dag: dict[int, set[int]] = {i: set() for i in range(len(components))}
        indegree = {i: 0 for i in range(len(components))}
        for provider, consumers in edges.items():
            for consumer in consumers:
                a, b = component_of[provider], component_of[consumer]
                if a != b and b not in dag[a]:
                    dag[a].add(b)
                    indegree[b] += 1

        # Kahn 分层：每轮取当前入度为 0 的全体，构成一层
        layers: list[list[str]] = []
        remaining = dict(indegree)
        while True:
            ready = sorted(i for i, degree in remaining.items() if degree == 0)
            if not ready:
                break
            layer: list[str] = []
            for index in ready:
                layer.extend(components[index])
                del remaining[index]
            for index in ready:
                for nxt in dag[index]:
                    if nxt in remaining:
                        remaining[nxt] -= 1
            layers.append(sorted(layer))

        # 极端情况（理论上不该发生）：还有没排掉的，兜底收进最后一层
        if remaining:
            leftovers = sorted(n for i in remaining for n in components[i])
            layers.append(leftovers)
        return layers

    # ---- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "roots": list(self.roots),
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
            "dependencies": [d.to_dict() for d in self.dependencies],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PathGraph":
        nodes = {
            str(nid): PathNode.from_dict(payload)
            for nid, payload in (data.get("nodes") or {}).items()
        }
        return cls(
            engine=str(data.get("engine", "unknown")),
            roots=[str(r) for r in (data.get("roots") or [])],
            nodes=nodes,
            dependencies=[
                GraphEdge.from_dict(d) for d in (data.get("dependencies") or [])
            ],
            metadata=dict(data.get("metadata") or {}),
        )


def _strongly_connected(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Kosaraju 强连通分量，迭代实现。

    用迭代而不是递归，是因为真实工程的依赖链可能很长，而 Python 默认递归深度
    只有 1000 —— 一个几百层深的依赖链就会炸掉整个调度计算。
    """
    order: list[str] = []
    seen: set[str] = set()
    for start in nodes:
        if start in seen:
            continue
        seen.add(start)
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(edges.get(start, ())))]
        while stack:
            node, iterator = stack[-1]
            advanced = False
            for nxt in iterator:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, iter(edges.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()

    reverse: dict[str, list[str]] = {n: [] for n in nodes}
    for src, targets in edges.items():
        for dst in targets:
            reverse.setdefault(dst, []).append(src)

    components: list[list[str]] = []
    assigned: set[str] = set()
    for start in reversed(order):
        if start in assigned:
            continue
        assigned.add(start)
        component: list[str] = []
        stack = [start]
        while stack:
            node = stack.pop()
            component.append(node)
            for nxt in reverse.get(node, ()):
                if nxt not in assigned:
                    assigned.add(nxt)
                    stack.append(nxt)
        components.append(sorted(component))
    return components


class PathGraphBuilder:
    """构造带权路径图的小工具，引擎支持包用它产出统一形状的图。"""

    def __init__(self, engine: str) -> None:
        self.engine = engine
        self._nodes: dict[str, PathNode] = {}
        self._roots: list[str] = []
        #: 引擎申报的图级事实（见 :attr:`PathGraph.metadata`）。
        self.metadata: dict[str, Any] = {}

    # ---- 内部 ---------------------------------------------------------------

    def _register(self, node: PathNode) -> str:
        if node.node_id in self._nodes:
            existing = self._nodes[node.node_id]
            raise ValueError(
                f"节点 id 重复：{node.node_id!r}"
                f"（已有 {existing.kind.value} @ {existing.path}，"
                f"新来 {node.kind.value} @ {node.path}）"
            )
        self._nodes[node.node_id] = node
        if node.parent is None:
            self._roots.append(node.node_id)
        else:
            self._nodes[node.parent].children.append(node.node_id)
        return node.node_id

    def _child_index(self, parent: str | None, token: str) -> int:
        if parent is None:
            return sum(1 for n in self._nodes.values() if n.parent is None and n.kind.value == token)
        return sum(
            1
            for cid in self._nodes[parent].children
            if self._nodes[cid].kind.value == token
        )

    def _resolve_parent(self, parent: str | None) -> tuple[str | None, str, int]:
        if parent is None:
            return None, "", 1
        pnode = self._nodes[parent]
        return parent, pnode.path, pnode.weight.depth + 1

    # ---- 公开 API -----------------------------------------------------------

    def add_container(
        self,
        path: str,
        kind: NodeKind,
        weight: int = 0,
        *,
        parent: str | None = None,
        node_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        separator: str = "/",
    ) -> str:
        """加一个容器节点（文件/标签/菜单等），本身不承载待译文本。

        ``separator`` 让引擎决定子路径怎么拼接 —— Ren'Py 支持包用 ``""`` 拼出
        ``script.rpy#start`` 这种人类与 agent 都一眼看懂的路径。
        """
        parent_id, parent_path, depth = self._resolve_parent(parent)
        full_path = f"{parent_path}{separator}{path}" if parent_path else path
        node = PathNode(
            node_id=node_id or full_path,
            path=full_path,
            kind=kind,
            parent=parent_id,
            weight=NodeWeight(
                char_count=weight,
                occurrences=1,
                depth=depth,
            ),
            metadata=dict(metadata or {}),
        )
        return self._register(node)

    def add_unit(
        self,
        node_id: str,
        unit: TranslationUnit,
        *,
        parent: str | None = None,
        kind: NodeKind | None = None,
        occurrences: int = 1,
        cost: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """加一个承载待译文本的叶节点。

        ``cost`` 是 token 估算；不给就按字符数估（见 :func:`gametrans.core.models.estimate_tokens`）。
        引擎日后可以塞真实计量进来。
        """
        node_kind = kind or NodeKind.from_token(unit.type)
        parent_id, parent_path, depth = self._resolve_parent(parent)
        index = self._child_index(parent_id, node_kind.value)
        leaf = f"{node_kind.value}[{index}]"
        full_path = f"{parent_path}/{leaf}" if parent_path else leaf
        node = PathNode(
            node_id=node_id,
            path=full_path,
            kind=node_kind,
            parent=parent_id,
            weight=NodeWeight(
                char_count=unit.char_count,
                cost=cost if cost is not None else estimate_tokens(unit.char_count),
                occurrences=occurrences,
                depth=depth,
            ),
            unit=unit,
            metadata=dict(metadata or {}),
        )
        return self._register(node)

    def add_unsupported(
        self,
        node_id: str,
        *,
        path: str,
        parent: str | None = None,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """记录一处"看起来该翻但本期拿不准"的文本。

        按 spec 的失败模式，这类内容进 ``unsupported`` 清单而不是抛错，
        让用户和 agent 都能看到缺口在哪。
        """
        parent_id, parent_path, depth = self._resolve_parent(parent)
        full_path = f"{parent_path}/{path}" if parent_path else path
        meta = dict(metadata or {})
        if reason:
            meta["reason"] = reason
        node = PathNode(
            node_id=node_id,
            path=full_path,
            kind=NodeKind.UNSUPPORTED,
            parent=parent_id,
            weight=NodeWeight(char_count=0, occurrences=1, depth=depth),
            metadata=meta,
        )
        return self._register(node)

    def adopt(self, node: PathNode) -> str:
        """登记一个已经建好的节点，**原样保留它的 id 与路径**。

        用来把一张图里的容器（文件 / label / menu）搬到另一张图上 —— 从别处搬来的节点
        路径已经拼好了，重新按父子关系拼一遍只会拼错。``children`` 由本图重新连，
        所以传进来的旧 children 会被丢掉。
        """
        return self._register(replace(node, children=[]))

    def build(self, *, prune_empty: bool = False) -> PathGraph:
        """收口成一张图。

        ``prune_empty=True`` **只给"成品的图"用**（内容已经挂上去了）：那时没有内容的
        容器就是真的空壳（真靶 99 个 menu 里 98 个）。**结构图不要开** —— 结构图是中间
        产物，引擎清单里缝不上源码的内容还要按它找家；删掉文件容器，那些内容就会落到
        "unknown" 上（探针实测：5 个文件桶并成一个 unknown，R76）。
        """
        graph = PathGraph(
            engine=self.engine,
            roots=list(self._roots),
            nodes=dict(self._nodes),
            metadata=dict(self.metadata),
        )
        if prune_empty:
            graph.prune_empty_containers()
        return graph
