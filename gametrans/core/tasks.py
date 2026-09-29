"""翻译任务协议与状态机（Translation Layer Architecture Guide §5、§14、§17、§22）。

三件事在这里定死：

* **Task 是数据，不是一句字符串。** 一个 Task 带着它的上下文请求、已检索到的上下文、
  术语/风格/引擎约束、执行策略、校验策略与来源；Executor 收到整个 Task，于是
  "为什么这样翻、依据什么、用了哪些资源、受了什么约束"事后可追。
* **状态只能按文档给的图走。** 非法迁移抛错并**留在原状态**；``attempts`` 只在真正
  进入翻译时递增。
* **状态必须持久化。** :class:`TaskStore` 把 Task 落到工作区的 JSONL：可中断、可恢复、
  可解释；坏行如实进诊断，不让整份文件作废。

``task_id`` 由 ``(项目, 单元, 原文版本, 目标语言)`` 决定 —— 同样的输入重放得到同样的 id，
持久化往返不改变它。
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.models import Segment, TranslationUnit
from gametrans.errors import GameTransError

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "ContextRequest",
    "ExecutionPolicy",
    "RetrievedContext",
    "RetrievedItem",
    "TaskPlanner",
    "TaskState",
    "TaskStore",
    "TaskTransitionError",
    "TranslationTask",
    "ValidationPolicy",
    "task_id_for",
]


class TaskTransitionError(GameTransError):
    """一次非法的状态迁移。抛出后原 Task 保持不变。"""


# --------------------------------------------------------------------------- #
# 状态机（Guide §17）
# --------------------------------------------------------------------------- #


class TaskState(str, enum.Enum):
    """翻译任务的状态。取值即协议词汇，落盘时按值写。"""

    DISCOVERED = "discovered"
    READY = "ready"
    TRANSLATING = "translating"
    VALIDATING = "validating"
    COMPLETED = "completed"
    RETRYING = "retrying"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


#: 允许的迁移。文档给的图是 DISCOVERED → READY → TRANSLATING → VALIDATING →
#: PASS/COMPLETED、FAIL/RETRYING → TRANSLATING；BLOCKED / NEEDS_REVIEW / CANCELLED
#: 是旁路。``NEEDS_REVIEW`` 留一条回 ``TRANSLATING`` 的口子，那是"人/agent 要求再试一次"。
TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DISCOVERED: frozenset(
        {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.READY: frozenset(
        {TaskState.TRANSLATING, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.TRANSLATING: frozenset(
        {
            TaskState.VALIDATING,
            TaskState.RETRYING,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.VALIDATING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.RETRYING,
            TaskState.NEEDS_REVIEW,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.RETRYING: frozenset(
        {
            TaskState.TRANSLATING,
            TaskState.NEEDS_REVIEW,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.NEEDS_REVIEW: frozenset(
        {TaskState.TRANSLATING, TaskState.CANCELLED}
    ),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

#: 走到这里就结束了，不再有出口。
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.CANCELLED}
)

#: 进入这个状态意味着"真的又问了一次模型"，``attempts`` 据此递增。
_ATTEMPT_STATES = frozenset({TaskState.TRANSLATING})


def _state(value: Any) -> TaskState:
    if isinstance(value, TaskState):
        return value
    try:
        return TaskState(str(value))
    except ValueError:
        raise TaskTransitionError(
            f"未知的任务状态：{value!r}",
            hint=f"可用状态：{', '.join(s.value for s in TaskState)}",
        ) from None


# --------------------------------------------------------------------------- #
# Task 的组成部分
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContextRequest:
    """这个 Task 需要哪几层信息（Guide §8 的检索阶梯，按顺序尝试）。"""

    #: 取哪些检索层：direct / structural / knowledge / memory / targeted
    kinds: tuple[str, ...] = ("direct", "structural", "knowledge", "memory")
    #: 依赖图给出的知识点，例如 ``character:Eileen``
    topics: tuple[str, ...] = ()
    #: 每层最多取多少条（0 = 由检索策略决定）
    max_items: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kinds": list(self.kinds),
            "topics": list(self.topics),
            "max_items": self.max_items,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextRequest":
        return cls(
            kinds=tuple(str(k) for k in (data.get("kinds") or ())),
            topics=tuple(str(t) for t in (data.get("topics") or ())),
            max_items=int(data.get("max_items") or 0),
        )


@dataclass
class RetrievedItem:
    """一条检索结果：内容之外，必须带着**它是从哪来的、有多可信**（Guide §11）。

    ``assertion`` 区分事实与推断：模型推出来的东西只能是 ``hypothesis``，
    永远不进术语约束（Guide §7、§12）。
    """

    layer: str
    type: str
    content: str
    source: str = ""
    version: str = ""
    scope: str = ""
    priority: int = 50
    confidence: float = 1.0
    #: fact / hypothesis
    assertion: str = "fact"
    provenance: str = ""

    @property
    def is_fact(self) -> bool:
        return self.assertion == "fact"

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "type": self.type,
            "content": self.content,
            "source": self.source,
            "version": self.version,
            "scope": self.scope,
            "priority": self.priority,
            "confidence": self.confidence,
            "assertion": self.assertion,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetrievedItem":
        return cls(
            layer=str(data.get("layer", "")),
            type=str(data.get("type", "")),
            content=str(data.get("content", "")),
            source=str(data.get("source", "")),
            version=str(data.get("version", "")),
            scope=str(data.get("scope", "")),
            priority=int(data.get("priority") or 0),
            confidence=float(data.get("confidence") or 0.0),
            assertion=str(data.get("assertion", "fact")),
            provenance=str(data.get("provenance", "")),
        )


@dataclass
class RetrievedContext:
    """Context Resolver 的输出：结构化候选 + 每层取到多少条（Guide §9）。"""

    items: list[RetrievedItem] = field(default_factory=list)
    #: 检索层 → 条数。用来统计"这次到底喂进了哪些层"
    layers: dict[str, int] = field(default_factory=dict)
    #: 检索策略的名字，进 provenance
    strategy: str = ""
    #: **一条上下文都没取到** —— 如实记下来，而不是假装上下文很丰富
    degraded: bool = False
    #: 任务点名要、结果取空了的那几层。空资源（项目里就没有术语表）不算质量问题，
    #: 但"要过而没取到"必须能看见，否则没法解释译文为什么差。
    starved: list[str] = field(default_factory=list)
    #: 取到了、但因为**某类自己的配额**没进去的条数（按类记：glossary/worldbook/style…）。
    #: 以前三类共用一个总额，术语多的段落把风格挤掉，而报告上一切正常 —— 记下来才看得见。
    dropped: dict[str, int] = field(default_factory=dict)

    def facts(self) -> list[RetrievedItem]:
        return [item for item in self.items if item.is_fact]

    def hypotheses(self) -> list[RetrievedItem]:
        return [item for item in self.items if not item.is_fact]

    def by_layer(self, layer: str) -> list[RetrievedItem]:
        return [item for item in self.items if item.layer == layer]

    def of_type(self, *types: str) -> list[RetrievedItem]:
        wanted = set(types)
        return [item for item in self.items if item.type in wanted]

    def facts_of_type(self, *types: str) -> list[RetrievedItem]:
        return [item for item in self.facts() if item.type in types]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "layers": dict(self.layers),
            "strategy": self.strategy,
            "degraded": self.degraded,
            "starved": list(self.starved),
            "dropped": dict(self.dropped),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetrievedContext":
        return cls(
            items=[RetrievedItem.from_dict(i) for i in (data.get("items") or ())],
            layers={str(k): int(v) for k, v in (data.get("layers") or {}).items()},
            strategy=str(data.get("strategy", "")),
            degraded=bool(data.get("degraded", False)),
            starved=[str(name) for name in (data.get("starved") or ())],
            dropped={str(k): int(v) for k, v in (data.get("dropped") or {}).items()},
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    """这一次 Task 怎么执行。**不含**任何模型私有参数（那是 preset 的事）。"""

    provider: str = ""
    model: str = ""
    mode: str = "auto"
    batch_size: int = 8
    concurrency: int = 1
    retry_on_violation: int = 1
    #: 重试上限，超过就进 NEEDS_REVIEW，不无限重试
    max_attempts: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "batch_size": self.batch_size,
            "concurrency": self.concurrency,
            "retry_on_violation": self.retry_on_violation,
            "max_attempts": self.max_attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionPolicy":
        return cls(
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            mode=str(data.get("mode", "auto")),
            batch_size=int(data.get("batch_size") or 8),
            concurrency=int(data.get("concurrency") or 1),
            retry_on_violation=int(data.get("retry_on_violation") or 0),
            max_attempts=int(data.get("max_attempts") or 3),
        )


@dataclass(frozen=True)
class ValidationPolicy:
    """这一次 Task 要求校验器检查什么。

    ``require_structure=False`` 时只做输出形状检查；内核不认识的约束名可以放进
    ``extra_constraints``，它们会如实出现在"检查不了"的诊断里，不会假装通过。
    """

    require_structure: bool = True
    extra_constraints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_structure": self.require_structure,
            "extra_constraints": list(self.extra_constraints),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationPolicy":
        return cls(
            require_structure=bool(data.get("require_structure", True)),
            extra_constraints=tuple(str(c) for c in (data.get("extra_constraints") or ())),
        )


# --------------------------------------------------------------------------- #
# Task
# --------------------------------------------------------------------------- #

#: 术语约束来自这几类知识条目；风格约束来自这几类。两者**不许混**（Guide §13）。
TERMINOLOGY_TYPES: frozenset[str] = frozenset({"approved_glossary", "glossary"})
STYLE_TYPES: frozenset[str] = frozenset({"style_guide", "style"})

#: 同一行里**没在请求里命中**的那个写法：它跟着整行一起进，是参考不是硬约束。
GLOSSARY_HINT_TYPE = "approved_glossary_hint"


def task_id_for(
    *,
    project_id: str,
    unit_id: str,
    source_version: str,
    target_language: str,
) -> str:
    """Task 的身份：同样的输入 → 同样的 id，持久化往返不改变它。"""
    payload = "\x00".join((project_id, unit_id, source_version, target_language))
    return "task:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class TranslationTask:
    """翻译层实际执行的工作（Guide §14）。

    **Unit 是数据，Task 是工作计划**：同一个 Unit 可以产生上下文分析、正式翻译、
    校验、修复等不同 Task；这里这一条是"正式翻译"那一个。
    """

    task_id: str
    project_id: str
    unit_id: str
    source_version: str
    target_language: str
    source_segments: list[Segment] = field(default_factory=list)
    context_request: ContextRequest = field(default_factory=ContextRequest)
    retrieved_context: RetrievedContext = field(default_factory=RetrievedContext)
    terminology_constraints: list[str] = field(default_factory=list)
    style_constraints: list[str] = field(default_factory=list)
    engine_constraints: list[str] = field(default_factory=list)
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    validation_policy: ValidationPolicy = field(default_factory=ValidationPolicy)
    status: TaskState = TaskState.DISCOVERED
    attempts: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)

    # ---- 派生 ---------------------------------------------------------------

    @property
    def source(self) -> str:
        return "".join(segment.value for segment in self.source_segments)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def resources(self) -> dict[str, Any]:
        """协议里的 ``resources``：这一次用到的资源版本与各类约束的来源。"""
        versions: dict[str, str] = {}
        for item in self.retrieved_context.items:
            if item.version and item.source:
                versions.setdefault(item.source, item.version)
        return {
            "versions": versions,
            "layers": dict(self.retrieved_context.layers),
            "terminology": list(self.terminology_constraints),
            "style": list(self.style_constraints),
            "engine": list(self.engine_constraints),
        }

    # ---- 状态迁移 -----------------------------------------------------------

    def advance(
        self,
        target: TaskState | str,
        *,
        context: RetrievedContext | None = None,
        note: str = "",
    ) -> "TranslationTask":
        """走一步状态机。非法迁移抛 :class:`TaskTransitionError`，原对象不变。"""
        wanted = _state(target)
        allowed = TRANSITIONS[self.status]
        if wanted not in allowed:
            raise TaskTransitionError(
                f"任务 {self.task_id} 不能从 {self.status.value} 走到 {wanted.value}",
                hint=(
                    f"合法去向：{', '.join(sorted(s.value for s in allowed)) or '（终态，无出口）'}"
                ),
            )
        attempts = self.attempts + 1 if wanted in _ATTEMPT_STATES else self.attempts
        provenance = dict(self.provenance)
        history = list(provenance.get("history") or [])
        history.append(wanted.value)
        provenance["history"] = history
        if note:
            provenance["note"] = note
        if context is None:
            # 上下文没变，约束也不必重算（每条 Task 要迁移三到四次，这是实打实的开销）
            return replace(
                self, status=wanted, attempts=attempts, provenance=provenance
            )
        return replace(
            self,
            status=wanted,
            attempts=attempts,
            provenance=provenance,
            retrieved_context=context,
            terminology_constraints=_constraints_from(context, TERMINOLOGY_TYPES),
            style_constraints=_constraints_from(context, STYLE_TYPES),
        )

    # ---- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "unit_id": self.unit_id,
            "source_version": self.source_version,
            "target_language": self.target_language,
            "source_segments": [s.to_dict() for s in self.source_segments],
            "context_request": self.context_request.to_dict(),
            "retrieved_context": self.retrieved_context.to_dict(),
            "resources": self.resources(),
            "terminology_constraints": list(self.terminology_constraints),
            "style_constraints": list(self.style_constraints),
            "engine_constraints": list(self.engine_constraints),
            "execution_policy": self.execution_policy.to_dict(),
            "validation_policy": self.validation_policy.to_dict(),
            "status": self.status.value,
            "attempts": self.attempts,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationTask":
        return cls(
            task_id=str(data.get("task_id", "")),
            project_id=str(data.get("project_id", "")),
            unit_id=str(data.get("unit_id", "")),
            source_version=str(data.get("source_version", "")),
            target_language=str(data.get("target_language", "")),
            source_segments=[Segment.from_dict(s) for s in (data.get("source_segments") or ())],
            context_request=ContextRequest.from_dict(data.get("context_request") or {}),
            retrieved_context=RetrievedContext.from_dict(data.get("retrieved_context") or {}),
            terminology_constraints=[
                str(c) for c in (data.get("terminology_constraints") or ())
            ],
            style_constraints=[str(c) for c in (data.get("style_constraints") or ())],
            engine_constraints=[str(c) for c in (data.get("engine_constraints") or ())],
            execution_policy=ExecutionPolicy.from_dict(data.get("execution_policy") or {}),
            validation_policy=ValidationPolicy.from_dict(data.get("validation_policy") or {}),
            status=_state(data.get("status", TaskState.DISCOVERED.value)),
            attempts=int(data.get("attempts") or 0),
            provenance=dict(data.get("provenance") or {}),
        )


def _constraints_from(context: RetrievedContext, types: frozenset[str]) -> list[str]:
    """从检索结果里挑出指定类型的**事实**约束。

    推断（hypothesis）一律不进约束 —— 这是"防止世界书污染"在代码里的落点
    （Guide §7、§25）。
    """
    return [item.content for item in context.facts_of_type(*sorted(types))]


# --------------------------------------------------------------------------- #
# Task 的构造
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskPlanner:
    """把一个 Unit 变成一条待执行的翻译 Task（Guide §4 的 Task Planner）。"""

    project_id: str
    target_language: str
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    validation_policy: ValidationPolicy = field(default_factory=ValidationPolicy)

    def plan(
        self,
        unit: TranslationUnit,
        *,
        context_request: ContextRequest | None = None,
        engine_constraints: Iterable[str] | None = None,
    ) -> TranslationTask:
        if engine_constraints is None:
            engine_constraints = [
                segment.value for segment in unit.protected_segments
            ]
        request = context_request or ContextRequest()
        return TranslationTask(
            task_id=task_id_for(
                project_id=self.project_id,
                unit_id=unit.id,
                source_version=unit.fingerprint,
                target_language=self.target_language,
            ),
            project_id=self.project_id,
            unit_id=unit.id,
            source_version=unit.fingerprint,
            target_language=self.target_language,
            source_segments=list(unit.segments),
            context_request=request,
            engine_constraints=[str(value) for value in engine_constraints],
            execution_policy=self.execution_policy,
            validation_policy=self.validation_policy,
            provenance={"planner": type(self).__name__, "history": [TaskState.DISCOVERED.value]},
        )


# --------------------------------------------------------------------------- #
# 持久化（Guide §17：状态必须持久化）
# --------------------------------------------------------------------------- #


class TaskStore:
    """任务状态的落盘处：``tasks.jsonl``，一行一个 Task，后写的覆盖先写的。

    读取**逐行宽容**：坏行进 :meth:`problems` 并跳过，不让一行烂数据把整份历史作废。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._problems: list[str] = []
        self._cache: tuple[tuple[int, int] | None, list[TranslationTask]] | None = None
        self._lock = threading.RLock()

    # ---- 读 -----------------------------------------------------------------

    def _stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def all(self) -> list[TranslationTask]:
        stamp = self._stamp()
        cached = self._cache
        if cached is not None and cached[0] == stamp:
            return list(cached[1])
        with self._lock:
            cached = self._cache
            if cached is not None and cached[0] == stamp:
                return list(cached[1])
            records, problems = self._read()
            self._problems = problems
            self._cache = (stamp, records)
            return list(records)

    def _read(self) -> tuple[list[TranslationTask], list[str]]:
        if not self.path.exists():
            return [], []
        ordered: dict[str, TranslationTask] = {}
        problems: list[str] = []
        for number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                task = TranslationTask.from_dict(payload)
            except Exception as exc:  # noqa: BLE001 - 一行烂数据不该拖垮整份历史
                problems.append(f"{self.path.name}:{number}: 无法解析：{exc}")
                continue
            if not task.task_id:
                problems.append(f"{self.path.name}:{number}: 缺少 task_id，已跳过")
                continue
            ordered[task.task_id] = task
        return list(ordered.values()), problems

    def get(self, task_id: str) -> TranslationTask | None:
        for task in self.all():
            if task.task_id == task_id:
                return task
        return None

    def for_unit(self, unit_id: str) -> list[TranslationTask]:
        return [task for task in self.all() if task.unit_id == unit_id]

    def counts(self) -> dict[str, int]:
        """各状态各有多少条 —— 只读查询的主要答案。"""
        counts = {state.value: 0 for state in TaskState}
        for task in self.all():
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        return counts

    def problems(self) -> list[str]:
        self.all()
        return list(self._problems)

    # ---- 写 -----------------------------------------------------------------

    def save_many(self, tasks: Iterable[TranslationTask]) -> int:
        """按 ``task_id`` 覆盖式写入。返回写完以后文件里有多少条。"""
        incoming = {task.task_id: task for task in tasks}
        if not incoming:
            return len(self.all())
        with self._lock:
            existing, _ = self._read()
            merged = {task.task_id: task for task in existing}
            merged.update(incoming)
            payload = "".join(
                json.dumps(task.to_dict(), ensure_ascii=False) + "\n"
                for task in merged.values()
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
            self._cache = None
            self._problems = []
        return len(merged)

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()
            self._cache = None
            self._problems = []
