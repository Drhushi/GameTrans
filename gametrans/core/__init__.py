"""引擎无关的内核：Localization IR、路径图、结构约束、一致性校验、会话编排。

**隔离约束**：本包及其子模块不得 import 任何具体引擎实现
（``gametrans.engines.<name>``），只能依赖 ``gametrans.engines.base`` 与
``gametrans.engines.registry``。由 ``tests/test_engine_boundary.py`` 用 AST 强制。
"""

from __future__ import annotations

from gametrans.core.constraints import default_constraints, profile_of, validate
from gametrans.core.dependencies import candidate_dependencies, policy_for
from gametrans.core.graph import PathGraph, PathGraphBuilder, Region
from gametrans.core.ir import ConformanceCheck, ConformanceReport, ProjectIR, UnitDiff
from gametrans.core.models import (
    Constraint,
    ConstraintType,
    ConstraintViolation,
    Context,
    EdgeType,
    GraphEdge,
    Issue,
    Locator,
    NodeKind,
    NodeWeight,
    PathNode,
    Provenance,
    Segment,
    SegmentKind,
    TranslationArtifact,
    TranslationStatus,
    TranslationUnit,
    ValidationResult,
)
from gametrans.core.bindings import SlotBindings, UnitBinding, bind_units
from gametrans.core.report import RunReport, StageReport
from gametrans.core.slots import Slot, SlotKeying, SlotLocation, SlotSet
from gametrans.core.units import (
    Boundary,
    BoundaryPolicy,
    GroupByFile,
    OneSlotPerUnit,
    assemble_units,
)
from gametrans.core.workflow import SlotWorkflow

__all__ = [
    "Boundary",
    "BoundaryPolicy",
    "ConformanceCheck",
    "ConformanceReport",
    "Constraint",
    "ConstraintType",
    "ConstraintViolation",
    "Context",
    "EdgeType",
    "GraphEdge",
    "GroupByFile",
    "Issue",
    "Locator",
    "NodeKind",
    "NodeWeight",
    "OneSlotPerUnit",
    "PathGraph",
    "PathGraphBuilder",
    "PathNode",
    "ProjectIR",
    "Provenance",
    "Region",
    "RunReport",
    "Segment",
    "SegmentKind",
    "Slot",
    "SlotBindings",
    "SlotKeying",
    "SlotLocation",
    "SlotSet",
    "SlotWorkflow",
    "StageReport",
    "TranslationArtifact",
    "TranslationStatus",
    "TranslationUnit",
    "UnitBinding",
    "UnitDiff",
    "ValidationResult",
    "assemble_units",
    "bind_units",
    "candidate_dependencies",
    "default_constraints",
    "policy_for",
    "profile_of",
    "validate",
]
