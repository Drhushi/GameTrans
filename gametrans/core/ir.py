"""Project 级 Localization IR 与提取层一致性校验。

``ProjectIR`` 是"提取层交给翻译层的完整产物"：Unit 集合 + 关系声明 + 资源引用。
它与 :class:`~gametrans.core.graph.PathGraph` 的分工是：

* ``PathGraph`` 是**导航结构**（文件 → label → menu → 语句），软件内部用来定位、
  展示与写回；
* ``ProjectIR`` 是**协议视图**（Unit + Edge），引擎与核心之间唯一需要认识的形状。

当前的依赖边仍然挂在**区域**（label / 文件模块）上，而不是 Unit 上；``branch`` 边已经是
Unit 级的。因此一致性校验接受"端点指向已知 Unit 或已知区域"（契约 R11）。

:meth:`ProjectIR.conformance` 把「提取层必须保证的性质」变成可执行的检查 ——
文档里写了却没检查的承诺，等于没写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from gametrans.core.models import (
    GraphEdge,
    Locator,
    TranslationUnit,
)

__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "ProjectIR",
    "UnitDiff",
]


@dataclass
class ConformanceCheck:
    """一条提取层性质检查的结果。``detail`` 必须能指向具体对象，否则等于没说。"""

    name: str
    ok: bool
    detail: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "count": self.count}


@dataclass
class ConformanceReport:
    ok: bool
    checks: list[ConformanceCheck] = field(default_factory=list)

    @property
    def failures(self) -> list[ConformanceCheck]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "failed": [c.name for c in self.failures],
        }


@dataclass
class UnitDiff:
    """两次提取之间的差异。"""

    added: list[TranslationUnit] = field(default_factory=list)
    removed: list[TranslationUnit] = field(default_factory=list)
    changed: list[TranslationUnit] = field(default_factory=list)
    unchanged: int = 0

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed) + self.unchanged

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": [u.id for u in self.added],
            "removed": [u.id for u in self.removed],
            "changed": [u.id for u in self.changed],
            "unchanged": self.unchanged,
        }


@dataclass
class ProjectIR:
    """一次本地化项目的协议视图。"""

    project_id: str
    engine_id: str
    source_language: str
    target_languages: list[str]
    units: list[TranslationUnit] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    #: 引擎可提供的资源引用与能力自述，核心"按能力使用"
    resources: dict[str, Any] = field(default_factory=dict)
    #: 边端点还允许指向哪些**区域** id（当前依赖边的粒度，见模块 docstring）
    regions: list[str] = field(default_factory=list)

    # ---- 构造 ---------------------------------------------------------------

    @classmethod
    def from_graph(
        cls,
        graph: Any,
        *,
        project_id: str,
        engine_id: str,
        source_language: str,
        target_languages: Iterable[str],
        resources: dict[str, Any] | None = None,
    ) -> "ProjectIR":
        units = list(graph.translatable_units())
        edges = list(graph.protocol_edges()) if hasattr(graph, "protocol_edges") else []
        regions = sorted({r.region_id for r in graph.regions()}) if hasattr(graph, "regions") else []
        return cls(
            project_id=project_id,
            engine_id=engine_id,
            source_language=source_language,
            target_languages=[str(t) for t in target_languages],
            units=units,
            edges=edges,
            resources=dict(resources or {}),
            regions=regions,
        )

    # ---- 查询 ---------------------------------------------------------------

    @property
    def unit_ids(self) -> list[str]:
        return [u.id for u in self.units]

    def unit(self, unit_id: str) -> TranslationUnit:
        for unit in self.units:
            if unit.id == unit_id:
                return unit
        raise KeyError(f"IR 里没有这条 Unit：{unit_id!r}")

    def known_refs(self) -> set[str]:
        return set(self.unit_ids) | set(self.regions)

    def diff(self, other: "ProjectIR") -> UnitDiff:
        """按 ``id`` + 内容指纹比较两次提取的结果。"""
        before = {u.id: u for u in self.units}
        after = {u.id: u for u in other.units}
        result = UnitDiff()
        for unit_id, unit in before.items():
            counterpart = after.get(unit_id)
            if counterpart is None:
                result.removed.append(unit)
            elif counterpart.fingerprint == unit.fingerprint:
                result.unchanged += 1
            else:
                result.changed.append(counterpart)
        for unit_id, unit in after.items():
            if unit_id not in before:
                result.added.append(unit)
        return result

    # ---- 一致性 -------------------------------------------------------------

    def conformance(self) -> ConformanceReport:
        """提取层必须保证的五条性质，逐条给出可核对的结论。"""
        checks = [
            self._check_units_present(),
            self._check_ids(),
            self._check_boundaries(),
            self._check_locators(),
            self._check_protected_structure(),
            self._check_edges(),
        ]
        return ConformanceReport(ok=all(c.ok for c in checks), checks=checks)

    def _check_units_present(self) -> ConformanceCheck:
        """0 条 Unit 时其它检查都会"空过" —— 那种通过等于什么都没证明。"""
        return ConformanceCheck(
            name="units_present",
            ok=bool(self.units),
            detail=(
                f"{len(self.units)} 条 Unit 可供校验"
                if self.units
                else "IR 里一条 Unit 也没有：这份结果什么也没证明"
            ),
            count=len(self.units),
        )

    def _check_ids(self) -> ConformanceCheck:
        seen: set[str] = set()
        duplicates: list[str] = []
        blank = 0
        for unit in self.units:
            if not unit.id:
                blank += 1
                continue
            if unit.id in seen:
                duplicates.append(unit.id)
            seen.add(unit.id)
        problems = []
        if blank:
            problems.append(f"{blank} 条 Unit 没有 id")
        if duplicates:
            problems.append("重复 id：" + "、".join(sorted(set(duplicates))[:5]))
        return ConformanceCheck(
            name="unit_id_unique",
            ok=not problems,
            # 这条只验**唯一性**（一次提取之内）。稳定性靠内容寻址 + 回归测试锁定
            # （tests/test_renpy_extract.py 的跨目录/插行用例），不在这里冒充。
            detail="；".join(problems) or f"{len(self.units)} 条 Unit 的 id 唯一",
            count=len(self.units),
        )

    def _check_boundaries(self) -> ConformanceCheck:
        problems: list[str] = []
        for unit in self.units:
            if not unit.segments:
                problems.append(f"{unit.id}: 没有 segment")
            elif not unit.type:
                problems.append(f"{unit.id}: 没有 type（Unit 边界不可解释）")
        return ConformanceCheck(
            name="unit_boundary_explainable",
            ok=not problems,
            detail="；".join(problems[:5]) or "每条 Unit 都有类型与 segment",
            count=len(self.units),
        )

    def _check_locators(self) -> ConformanceCheck:
        problems: list[str] = []
        for unit in self.units:
            locator: Locator | None = unit.locator
            if locator is None or not locator.file:
                problems.append(f"{unit.id}: 没有可用的 Locator")
            elif locator.kind == "line" and locator.line <= 0:
                problems.append(f"{unit.id}: Locator 是行定位但行号为 {locator.line}")
        return ConformanceCheck(
            name="locator_sufficient",
            ok=not problems,
            detail="；".join(problems[:5]) or "每条 Unit 都能被 Locator 定位",
            count=len(self.units),
        )

    def _check_protected_structure(self) -> ConformanceCheck:
        """受保护结构必须被**识别**出来，而不是被当成普通文本吞掉。

        "切分有没有吞掉内容"要靠原始文本才对得上（拿 segment 自己证明自己不算数）。
        适配器没留原始文本时，这条检查**明确说明覆盖性未校验** —— 宁可说"没查"，
        也不假装通过。
        """
        problems: list[str] = []
        unstructured = 0
        unverified = 0
        for unit in self.units:
            raw = unit.metadata.get("raw_source")
            if raw is None:
                unverified += 1
            elif "".join(s.value for s in unit.segments) != raw:
                problems.append(f"{unit.id}: segment 没有完整覆盖原文")
            for segment in unit.segments:
                if not segment.kind:
                    problems.append(f"{unit.id}: 有 segment 没有 kind")
                    break
            else:
                if not unit.protected_segments:
                    unstructured += 1
        detail = "；".join(problems[:5])
        if not detail:
            detail = f"{len(self.units)} 条 Unit 的结构标注自洽"
            extras: list[str] = []
            if unstructured:
                extras.append(f"其中 {unstructured} 条不含受保护结构")
            if unverified:
                extras.append(f"另有 {unverified} 条未记录原始文本，覆盖性未校验")
            if extras:
                detail += "（" + "；".join(extras) + "）"
        return ConformanceCheck(
            name="protected_structure_recognized",
            ok=not problems,
            detail=detail,
            count=len(self.units),
        )

    def _check_edges(self) -> ConformanceCheck:
        known = self.known_refs()
        unknown: list[str] = []
        for edge in self.edges:
            for ref in (edge.source, edge.target):
                if ref not in known and ref not in unknown:
                    unknown.append(ref)
        return ConformanceCheck(
            name="graph_references_exist",
            ok=not unknown,
            detail=(
                "指向不存在的 Unit/区域：" + "、".join(unknown[:5])
                if unknown
                else f"{len(self.edges)} 条边全部指向已知对象"
            ),
            count=len(self.edges),
        )

    # ---- 序列化 -------------------------------------------------------------

    def to_dict(self, *, include_units: bool = True) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "engine_id": self.engine_id,
            "source_language": self.source_language,
            "target_languages": list(self.target_languages),
            "unit_count": len(self.units),
            "units": [u.to_dict() for u in self.units] if include_units else [],
            "graph": {
                "edges": [e.to_dict() for e in self.edges],
                "edge_count": len(self.edges),
                "regions": list(self.regions),
            },
            "resources": dict(self.resources),
        }
