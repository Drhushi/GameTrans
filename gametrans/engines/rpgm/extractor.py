"""RPGM 提取器：把玩家可见文本整理成带权路径图。

与 Ren'Py 侧最大的结构差异在这里 —— 而且是对我们有利的那一侧：

* Ren'Py 里"哪些文本要翻"要靠**官方工具产出的骨架**回答，源码只读结构；
* RPG Maker MV **没有官方翻译工具**，所以内容范围由适配层按**引擎自己的数据结构**
  申报（`content.py` 的字段与指令清单）。id 也不是凭空造的：它就是引擎数据里的
  那条路径（``Map003.json#events[20].pages[0].list[9]``），因此仍然"不是我们发明的"。

结构归属也不用另读源码：地图 → 事件 → 页 → 指令的层级本来就在数据里。

**身份按位置定**（同一句原文在不同的位置是两条，可以有不同译法），并同时记**原文
指纹**兜底 —— 游戏更新导致位置漂移时，靠指纹能在同一份数据文件里重新缝上。

权重是本层的"意见"：

* 选项   90 —— 直接决定玩家操作，翻错了体验最差
* 名称   70 —— 角色 / 物品 / 技能 / 状态名，贯穿全篇，一致性价值最高
* 对话   60 —— 主体内容
* 界面   50 —— 引擎内建词条与说明文本，量小且独立
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path

from gametrans.core.graph import PathGraph, PathGraphBuilder
from gametrans.core.models import (
    Context,
    EdgeType,
    GraphEdge,
    Locator,
    NodeKind,
    TranslationUnit,
)
from gametrans.engines.base import ExtractContext
from gametrans.engines.rpgm.content import (
    UNCERTAIN_CONTROL_CODE,
    UNCERTAIN_PLUGIN_COMMAND,
    UNCERTAIN_SCRIPT_COMMAND,
    Candidate,
    ContentScan,
    collect_content,
)
from gametrans.engines.rpgm.datafiles import DataSource, load_data, select_source
from gametrans.engines.rpgm.segments import segmentize

__all__ = ["RpgmExtractor"]

#: 未知控制码的说明 —— 报出来是为了让"遇到再补"可执行。
_CONTROL_CODE_WHY = (
    "实测清单里没有这些控制码。它们仍被当作受保护结构（不会被当成正文翻坏），"
    "但没被单独验证过；补进清单时请连同回归用例一起加"
)


def _sha(text: str, length: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


class RpgmExtractor:
    """把一个 RPG Maker MV 工程抽成带权路径图。"""

    def __init__(self, source: DataSource | None = None, scan: ContentScan | None = None):
        #: 允许注入（测试与后续写回要复用同一份数据源），不给就自己选
        self._source = source
        self._scan = scan

    def extract(self, ctx: ExtractContext) -> PathGraph:
        data, scan = self.content(ctx)
        source = self._resolve_source(ctx)

        builder = PathGraphBuilder("rpgm")
        root_id = builder.add_container(ctx.project_root.name, NodeKind.ROOT)

        file_ids: dict[str, str] = {}
        containers: dict[str, str] = {}

        def file_node(file_name: str) -> str:
            if file_name in file_ids:
                return file_ids[file_name]
            rel = self._relpath(source, file_name, ctx.project_root)
            node_id = builder.add_container(
                rel,
                NodeKind.FILE,
                parent=root_id,
                metadata={"relpath": rel, "file": rel},
            )
            file_ids[file_name] = node_id
            return node_id

        def container_node(candidate: Candidate) -> str:
            """一条文本挂在哪个容器下 —— 决定它的"区域"（调度的粒度）。"""
            file_name = candidate.file_name
            parent = file_node(file_name)
            if not candidate.scene or candidate.scene == file_name[: -len(".json")]:
                return parent
            if candidate.scene in containers:
                return containers[candidate.scene]
            # 一个事件就是一个"场景"：用 LABEL 容器，于是区域按事件归拢，
            # 而路径里也带上了地图与事件号（agent 与用户都要指着它说话）。
            # 路径里只放本段（``events[1]``）—— 文件名已经在父节点的路径里了。
            local = candidate.scene.split("#", 1)[1] if "#" in candidate.scene else candidate.scene
            node_id = builder.add_container(
                local,
                NodeKind.LABEL,
                parent=parent,
                separator="#",
                metadata={"label": candidate.scene},
            )
            containers[candidate.scene] = node_id
            return node_id

        for candidate in scan.candidates:
            unit = self._build_unit(
                candidate,
                self._relpath(source, candidate.file_name, ctx.project_root),
                source.kind,
            )
            builder.add_unit(
                candidate.structural_path,
                unit,
                parent=container_node(candidate),
                kind=candidate.node_kind,
            )

        graph = builder.build(prune_empty=True)
        self.apply_occurrences(graph)
        graph.dependencies.extend(self._branch_edges(scan, graph))
        # 同一个事件里的命令顺序（`list[i]`）—— 这是 RPGM 唯一能定先后的边
        graph.dependencies.extend(self._sequence_edges(scan, graph))
        self._record(ctx, source, scan, graph)
        return graph

    # ---- 数据 ---------------------------------------------------------------

    def _resolve_source(self, ctx: ExtractContext) -> DataSource:
        if self._source is None:
            self._source = select_source(ctx.project_root)
        return self._source

    def content(self, ctx: ExtractContext) -> tuple[dict, ContentScan]:
        """读数据并清点内容 —— 与 :meth:`extract` 分开，供只读问答复用。"""
        source = self._resolve_source(ctx)
        data = load_data(source)
        scan = self._scan if self._scan is not None else collect_content(data)
        return data, scan

    @staticmethod
    def _relpath(source: DataSource, file_name: str, project_root: Path) -> str:
        """数据文件的**项目根相对**路径（``www/data/compressed/Map001.json``）。

        必须是项目根相对，而不是 web 根相对：内核在导出前会拿
        ``project_root / locator.file`` 去核对文件在不在（它不认识 ``www/`` 这种
        引擎目录约定）。给成 web 根相对的话，内核对每一条都会判"定位不到"，
        于是写回一个字都出不去 —— 而报告只会说"没有可用译文"，很难查。
        """
        root = Path(project_root).resolve()
        for path in source.files.values():
            if path.name != file_name:
                continue
            try:
                return path.resolve().relative_to(root).as_posix()
            except ValueError:
                return path.as_posix()
        return f"{source.directory.name}/{file_name}"

    # ---- 单位 ---------------------------------------------------------------

    @staticmethod
    def _build_unit(candidate: Candidate, relpath: str, datasource: str) -> TranslationUnit:
        payload = dict(candidate.payload)
        # 引擎私有定位数据：内核对它一无所知，写回时原样交还
        payload.update(
            {
                "file": relpath,
                "datasource": datasource,
                "structural_path": candidate.structural_path,
                "json_pointer": candidate.structural_path.split("#", 1)[-1],
                "category": candidate.category,
                # 位置漂了以后的兜底：按原文指纹在同一份文件里重新缝上
                "source_fingerprint": candidate.fingerprint,
            }
        )
        return TranslationUnit.from_text(
            id=f"{candidate.unit_type}_{_sha(candidate.structural_path)}",
            type=candidate.unit_type,
            source=candidate.text,
            scanner=segmentize,
            locator=Locator(
                # 数据文件不是按行读的 —— 假报行号会骗过一致性检查，
                # 所以这里如实申报"按引擎数据结构里的路径定位"
                file=relpath,
                line=0,
                kind="path",
                payload=payload,
            ),
            context=Context(
                scene=candidate.scene,
                speaker=candidate.speaker,
                characters=[candidate.speaker] if candidate.speaker else [],
                location=candidate.location,
                note=candidate.note,
            ),
        )

    # ---- 关系 ---------------------------------------------------------------

    @staticmethod
    def _branch_edges(scan: ContentScan, graph: PathGraph) -> list[GraphEdge]:
        """同一个 ``102`` 下相邻选项之间的 ``branch`` 边（Unit 级）。

        ``provenance="engine"`` —— 选项是引擎结构，不是我们猜的。方向刻意留
        ``reading_order``：并列选项之间没有先后，参与排序会把并行度压成串行链。

        边端点是候选自己的 ``structural_path``（就是 unit 的 node_id），所以不依赖
        建图的中间状态。
        """
        grouped: dict[str, list[tuple[int, str]]] = {}
        for candidate in scan.candidates:
            if candidate.unit_type != "choice":
                continue
            anchor = candidate.payload.get("choice_command_index")
            choice_index = candidate.payload.get("choice_index")
            if anchor is None or choice_index is None:
                continue
            prefix = candidate.structural_path.split(".choice[", 1)[0]
            grouped.setdefault(prefix, []).append((int(choice_index), candidate.structural_path))

        edges: list[GraphEdge] = []
        seen: set[tuple[str, str]] = set()
        for prefix in sorted(grouped):
            options = sorted(grouped[prefix])
            for (_left_index, left), (_right_index, right) in zip(options, options[1:]):
                # 端点是**图上的结点 id**（RPGM 就是结构路径）。这里原先取 `unit.id`，
                # 而 RPGM 的结点键是结构路径 —— 两套命名空间对不上，真靶 Subject-Jane 上
                # 431 条分支边因此**全部悬空**（`dangling_dependencies()` 全都点得到）。
                if left not in graph.nodes or right not in graph.nodes:
                    continue
                pair = (left, right)
                if pair in seen:
                    continue
                seen.add(pair)
                edges.append(
                    GraphEdge(
                        source=left,
                        target=right,
                        type=EdgeType.BRANCH.value,
                        provenance="engine",
                        direction="reading_order",
                        note="同一个选项菜单下的并列选项",
                    )
                )
        return edges

    @staticmethod
    def _sequence_edges(scan: ContentScan, graph: PathGraph) -> list[GraphEdge]:
        """同一个事件里，命令按 ``list[i]`` 顺序执行 —— 引擎事实，不是猜的。

        为什么必须有：RPGM 原先只发"并列选项"的 branch 边（方向刻意留 ``reading_order``，
        不参与排序），于是真靶 Subject-Jane 上 431 条边**一条都不能定先后**，分层退化成
        1 层（等于全并行），274 个单元还缝不上结构段。命令顺序就写在引擎自己的数据结构
        里（``list[i]``），读得出来就该读 —— 跨事件没有全局顺序（事件靠条件触发），
        所以这里只连**同一事件内**相邻的两条。
        """
        grouped: dict[str, list[tuple[tuple[int, int], str]]] = {}
        for candidate in scan.candidates:
            path = str(candidate.structural_path or "")
            match = ORDER_KEY_RE.match(path)
            if match is None:
                continue
            key = (int(match.group("index")), int(match.group("choice") or 0))
            grouped.setdefault(match.group("prefix"), []).append((key, path))

        edges: list[GraphEdge] = []
        seen: set[tuple[str, str]] = set()
        for prefix in sorted(grouped):
            items = sorted(grouped[prefix])
            for (_left_key, left), (_right_key, right) in zip(items, items[1:]):
                if left not in graph.nodes or right not in graph.nodes:
                    continue
                pair = (left, right)
                if pair in seen:
                    continue
                seen.add(pair)
                edges.append(
                    GraphEdge(
                        source=left,
                        target=right,
                        type=EdgeType.SEQUENCE.value,
                        provenance="engine",
                        direction="control_flow",
                        note="同一个事件里命令按 list 顺序执行",
                    )
                )
        return edges

    # ---- 记账 ---------------------------------------------------------------

    @staticmethod
    def _record(
        ctx: ExtractContext, source: DataSource, scan: ContentScan, graph: PathGraph
    ) -> None:
        report = ctx.report
        report.metrics["rpgm"] = {
            "data_source": source.kind,
            "data_directory": str(source.directory),
            "data_files": len(source.files),
            "evidence": list(source.evidence),
            "translatable": len(scan.candidates),
            "by_category": scan.by_category(),
            "excluded_total": sum(scan.excluded.values()),
            "empty_total": sum(scan.empty.values()),
        }

        if scan.excluded:
            report.add_issue(
                "skipped",
                code="editor_only_text",
                message=(
                    f"{sum(scan.excluded.values())} 处文本只出现在编辑器里"
                    "（事件名 / 地图树名 / 公共事件名 / 开关变量名 / 开发者备注），"
                    "不是玩家可见内容，已跳过"
                ),
                detail={
                    "count": sum(scan.excluded.values()),
                    "by_category": dict(sorted(scan.excluded.items())),
                    "samples": {
                        reason: values
                        for reason, values in sorted(scan.excluded_samples.items())
                    },
                },
            )

        if scan.empty:
            report.add_issue(
                "skipped",
                code="empty_source_text",
                message=(
                    f"{sum(scan.empty.values())} 处原文是空的（引擎照样给它留了位置）。"
                    "它们不是'翻译失败'，也不该被当成缺口去查"
                ),
                detail={
                    "count": sum(scan.empty.values()),
                    "by_category": dict(sorted(scan.empty.items())),
                },
            )

        for code in (UNCERTAIN_PLUGIN_COMMAND, UNCERTAIN_SCRIPT_COMMAND):
            entry = scan.uncertain.get(code)
            if not entry:
                continue
            report.add_issue(
                "unsupported",
                code=code,
                message=f"{entry['count']} 处{_UNCERTAIN_LABEL[code]}还没纳入内容范围：{entry['why']}",
                detail={
                    "count": entry["count"],
                    "samples": list(entry["samples"]),
                    "why": entry["why"],
                },
            )

        if scan.unclassified_codes:
            report.add_issue(
                "unsupported",
                code=UNCERTAIN_CONTROL_CODE,
                message=(
                    f"发现 {len(scan.unclassified_codes)} 个实测清单之外的控制码："
                    f"{'、'.join(sorted(scan.unclassified_codes))}。{_CONTROL_CODE_WHY}"
                ),
                detail={
                    "codes": sorted(scan.unclassified_codes),
                    "count": sum(scan.unclassified_codes.values()),
                    "samples": list(scan.unclassified_samples),
                    "why": _CONTROL_CODE_WHY,
                },
            )

    @staticmethod
    def apply_occurrences(graph: PathGraph) -> None:
        """同一句话在多处出现 → 标上 ``occurrences``（翻一次到处受益）。

        注意这不影响身份：位置不同的两条仍是两个 unit，只是权重知道它们同文。
        """
        counts: Counter[str] = Counter(
            node.unit.source for node in graph.nodes.values() if node.unit is not None
        )
        for node in graph.nodes.values():
            if node.unit is None:
                continue
            total = counts[node.unit.source]
            if total > 1:
                node.weight = replace(node.weight, occurrences=total)


ORDER_KEY_RE = re.compile(r"^(?P<prefix>.+?)\.list\[(?P<index>\d+)\](?:\.choice\[(?P<choice>\d+)\])?$")

_UNCERTAIN_LABEL = {
    UNCERTAIN_PLUGIN_COMMAND: "插件指令参数",
    UNCERTAIN_SCRIPT_COMMAND: "脚本指令",
}
