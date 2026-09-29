"""Localization IR —— 引擎适配器与翻译核心之间**唯一**的交换格式。

对应 docx《游戏 AI 翻译系统架构与 Engine Adapter Protocol v0.1》第 5、6、11 节
规定的最小字段集：

===============  ==================================================
协议对象          本模块里的类
===============  ==================================================
Project          :class:`~gametrans.core.ir.ProjectIR`
Unit             :class:`TranslationUnit`
Segment          :class:`Segment`
Locator          :class:`Locator`
Graph Edge       :class:`GraphEdge`
Constraint       :class:`Constraint`
Artifact         :class:`TranslationArtifact`
Export Target    :class:`gametrans.engines.base.ExportTarget`
===============  ==================================================

三条不能破的边界：

* **核心不认识任何具体引擎。** 引擎专属信息一律塞进
  :attr:`Locator.payload` 这个不透明字典，核心只搬运、从不解释；
* **Unit 的边界由适配器决定。** 核心不按句子或文件重新切分，只消费适配器给的
  ``segments``；
* **不可译结构是结构化数据，不是口头约定。** ``Segment.kind`` 标记出变量、标签、
  控制码、转义与必需换行，机器校验器据此判断译文有没有破坏结构
  （见 :mod:`gametrans.core.constraints`）。

字段名与协议的唯一偏差：协议里的边字段叫 ``from`` / ``to``，而 ``from`` 是 Python
关键字，因此 Python 侧字段名是 ``source`` / ``target``，序列化时仍写回
``"from"`` / ``"to"``。
"""

from __future__ import annotations

import enum
import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# 结构图的节点类型（不是 IR 的一部分，是软件内部的导航结构）
# --------------------------------------------------------------------------- #


class NodeKind(str, enum.Enum):
    """路径图节点的逻辑类型。"""

    ROOT = "root"
    FILE = "file"
    LABEL = "label"
    MENU = "menu"
    CHOICE = "choice"
    SAY = "say"
    STRING = "string"
    #: 一个结构段里混了多种内容（真靶 30 个剧情场景是这一类）。
    #: 它**不是** `string` —— 把剧情场景记成"字符串条目"会让报告与面板一起撒谎（R73）。
    MIXED = "mixed"
    DEFINITION = "definition"
    UNSUPPORTED = "unsupported"

    @classmethod
    def from_token(cls, token: str) -> "NodeKind":
        """把引擎给的自由文本 token 归一成节点类型，未知则归入 UNSUPPORTED。"""
        try:
            return cls(token)
        except ValueError:
            return cls.UNSUPPORTED


# --------------------------------------------------------------------------- #
# Segment —— 文本与结构的统一表示
# --------------------------------------------------------------------------- #


class SegmentKind(str, enum.Enum):
    """协议规定的最小结构类型；引擎可以扩展自己的值（当作不透明字符串处理）。"""

    TEXT = "text"
    #: 引擎明确标为不可译的整块内容
    PROTECTED = "protected"
    #: 引擎控制码，例如暂停、清屏
    CONTROL = "control"
    #: 变量/占位符/格式化参数
    VARIABLE = "variable"
    #: 标签与富文本标记
    TAG = "tag"
    #: 转义序列与必须保留的换行
    ESCAPE = "escape"

    @classmethod
    def from_token(cls, token: str) -> "SegmentKind | None":
        try:
            return cls(token)
        except ValueError:
            return None


@dataclass
class Segment:
    """一段文本或一个受保护结构。

    ``translatable`` 不给时按 ``kind`` 推导 —— 只有 ``text`` 默认可译。这样适配器
    只需要老实标注类型，不必同时维护"可译性"这个冗余字段。
    """

    kind: str
    value: str
    translatable: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.translatable is None:
            self.translatable = self.kind == SegmentKind.TEXT.value
        else:
            self.translatable = bool(self.translatable)

    @property
    def is_protected(self) -> bool:
        return not self.translatable

    @property
    def is_text(self) -> bool:
        return self.kind == SegmentKind.TEXT.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "translatable": bool(self.translatable),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            kind=str(data.get("kind", SegmentKind.TEXT.value)),
            value=str(data.get("value", "")),
            translatable=data.get("translatable"),
            metadata=dict(data.get("metadata") or {}),
        )


# --------------------------------------------------------------------------- #
# Locator —— 稳定定位
# --------------------------------------------------------------------------- #


@dataclass
class Locator:
    """译文该写回原文件的哪里。

    核心**只保存并继承**它，从不解释其语义；只有对应的适配器/导出器知道
    ``payload`` 里那些字段是什么意思。
    """

    file: str
    line: int = 0
    #: 定位方式自述（``line`` / ``path`` / ``resource_id`` / ...），给人和 agent 看
    kind: str = "line"
    #: 引擎专属、对核心不透明的定位数据（Ren'Py 的缩进、translate 块 id、label 范围……）
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Locator":
        return cls(
            file=str(data.get("file", "")),
            line=int(data.get("line", 0) or 0),
            kind=str(data.get("kind", "line")),
            payload=dict(data.get("payload") or {}),
        )


# --------------------------------------------------------------------------- #
# Context —— 上下文声明
# --------------------------------------------------------------------------- #


@dataclass
class Context:
    """适配器向核心暴露的**结构化**上下文关系。

    适配器不需要把整部游戏复制进每个 Unit，只需要说明"它属于哪个场景、谁在说话、
    前后有谁"，核心按任务需要自己构建 Context Package。
    """

    parent: str | None = None
    chapter: str | None = None
    scene: str | None = None
    speaker: str | None = None
    characters: list[str] = field(default_factory=list)
    neighboring_units: list[str] = field(default_factory=list)
    world: list[str] = field(default_factory=list)
    location: str | None = None
    references: list[str] = field(default_factory=list)
    #: 适配器给的一句话说明（例如 "label start"），核心不解释
    note: str = ""

    def render(self) -> str:
        """压成一行，供提示词与人类阅读。"""
        bits: list[str] = []
        for label, value in (
            ("章节", self.chapter),
            ("场景", self.scene),
            ("位置", self.location),
            ("说话人", self.speaker),
        ):
            if value:
                bits.append(f"{label}：{value}")
        if self.characters:
            bits.append("相关角色：" + "、".join(self.characters))
        if self.world:
            bits.append("相关设定：" + "、".join(self.world))
        if self.note:
            bits.append(self.note)
        return "；".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent": self.parent,
            "chapter": self.chapter,
            "scene": self.scene,
            "speaker": self.speaker,
            "characters": list(self.characters),
            "neighboring_units": list(self.neighboring_units),
            "world": list(self.world),
            "location": self.location,
            "references": list(self.references),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Context":
        return cls(
            parent=data.get("parent"),
            chapter=data.get("chapter"),
            scene=data.get("scene"),
            speaker=data.get("speaker"),
            characters=[str(c) for c in (data.get("characters") or [])],
            neighboring_units=[str(c) for c in (data.get("neighboring_units") or [])],
            world=[str(c) for c in (data.get("world") or [])],
            location=data.get("location"),
            references=[str(c) for c in (data.get("references") or [])],
            note=str(data.get("note", "")),
        )


# --------------------------------------------------------------------------- #
# Translation Unit
# --------------------------------------------------------------------------- #

#: 把一段文本切成 segment 的函数。由适配器提供 —— 核心不认识任何语法。
Scanner = Callable[[str], list[Segment]]


@dataclass
class TranslationUnit:
    """核心处理的最小"有意义翻译工作单元"。

    协议不规定一个 Unit 必须是一句话、一个段落还是一个文件 —— 那是适配器依据引擎
    结构、场景边界与玩家体验顺序做的判断。
    """

    id: str
    #: dialogue / ui / scene / item / quest / ...，取值由适配器决定
    type: str = ""
    segments: list[Segment] = field(default_factory=list)
    locator: Locator | None = None
    context: Context = field(default_factory=Context)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: 这条 Unit 需要哪些知识/资源（例如 ``character:Eileen``），核心据此取资源
    resource_refs: list[str] = field(default_factory=list)

    # ---- 派生 ---------------------------------------------------------------

    @property
    def source(self) -> str:
        """原始文本（受保护结构原样在内）。这是交给模型的"待译内容"。"""
        return "".join(segment.value for segment in self.segments)

    @property
    def protected_segments(self) -> list[Segment]:
        return [s for s in self.segments if s.is_protected]

    @property
    def char_count(self) -> int:
        return len(self.source)

    @property
    def speaker(self) -> str | None:
        return self.context.speaker

    @property
    def fingerprint(self) -> str:
        """内容指纹：``id`` + 原文。

        "提取结果能够在游戏更新后进行差异比较" —— 差异比较要有可比
        的键，这就是那个键（见 :meth:`gametrans.core.ir.ProjectIR.diff`）。
        """
        payload = f"{self.id}\x00{self.source}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # ---- 构造与序列化 -------------------------------------------------------

    @classmethod
    def from_text(
        cls,
        *,
        id: str,
        type: str,
        source: str,
        scanner: Scanner | None = None,
        locator: Locator | None = None,
        context: Context | None = None,
        metadata: dict[str, Any] | None = None,
        resource_refs: list[str] | None = None,
    ) -> "TranslationUnit":
        """便捷构造：给了 ``scanner`` 就结构化切分，否则整条当作可译文本。

        没有结构语法的引擎（或测试夹具）可以只用这一个入口，不必自己拼 Segment。
        原文同时记进 ``metadata["raw_source"]``：这样一致性校验能验证"切分没有吞掉
        内容"，而不是只能相信自己的输出（见 :meth:`ProjectIR._check_protected_structure`）。
        """
        segments = scanner(source) if scanner is not None else []
        if not segments:
            segments = [Segment(SegmentKind.TEXT.value, source)]
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("raw_source", source)
        if scanner is None:
            # 没给 scanner = 没人扫过这段文本的结构。如实记下来：一致性检查会把它报成
            # "覆盖性未校验"，导出边界会拒绝认证它 —— 而不是当它是段没有结构的纯文本。
            merged_metadata.setdefault("segments_unverified", True)
        return cls(
            id=id,
            type=type,
            segments=segments,
            locator=locator,
            context=context or Context(),
            metadata=merged_metadata,
            resource_refs=list(resource_refs or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "segments": [s.to_dict() for s in self.segments],
            "locator": self.locator.to_dict() if self.locator else None,
            "context": self.context.to_dict(),
            "metadata": dict(self.metadata),
            "resource_refs": list(self.resource_refs),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationUnit":
        """读一条 Unit。

        对旧工作区数据**宽容但不猜测**：认得出的字段（``unit_id`` / ``kind`` /
        ``location`` / ``engine_hints`` / 裸 ``source``）照读，认不出的原样留在
        ``metadata`` 里。旧格式不是承诺，但也没必要让项目打不开。
        """
        segments_payload = data.get("segments")
        unverified = False
        if segments_payload:
            segments = [Segment.from_dict(s) for s in segments_payload]
        else:
            # 旧数据/外部数据只有裸 source：我们**没有**扫过它的结构，因此无法校验
            # 受保护元素。这里如实标记出来 —— 下游（导出边界）据此拒绝"认证"它，
            # 而不是把它当成一段没有结构的普通文本放行。
            segments = [Segment(SegmentKind.TEXT.value, str(data.get("source", "")))]
            unverified = True

        locator_payload = data.get("locator") or data.get("location")
        locator = Locator.from_dict(locator_payload) if locator_payload else None
        legacy_hints = data.get("engine_hints")
        if legacy_hints:
            if locator is None:
                locator = Locator(file="", payload=dict(legacy_hints))
            elif not locator.payload:
                locator.payload = dict(legacy_hints)

        context_payload = data.get("context")
        if isinstance(context_payload, dict):
            context = Context.from_dict(context_payload)
        else:
            context = Context(
                speaker=data.get("speaker"),
                note=str(context_payload or ""),
            )

        metadata = dict(data.get("metadata") or {})
        if unverified:
            metadata["segments_unverified"] = True

        return cls(
            id=str(data.get("id", data.get("unit_id", ""))),
            type=str(data.get("type", data.get("kind", ""))),
            segments=segments,
            locator=locator,
            context=context,
            metadata=metadata,
            resource_refs=[str(r) for r in (data.get("resource_refs") or [])],
        )


# --------------------------------------------------------------------------- #
# Graph Edge
# --------------------------------------------------------------------------- #


class EdgeType(str, enum.Enum):
    """第一版协议建议的边类型。"""

    #: 翻译 target 时需要 source 已经交代过的信息
    DEPENDENCY = "dependency"
    #: 上下文相关，但不构成调度先后
    CONTEXT = "context"
    #: 玩家体验/控制流上的先后
    SEQUENCE = "sequence"
    #: 并列分支（同一菜单下的不同选项）
    BRANCH = "branch"


#: 依赖方向的全部合法取值
DIRECTIONS: tuple[str, ...] = ("control_flow", "reading_order", "agent")

#: **可以用来决定先后顺序**的方向。
#:
#: `reading_order` 刻意不在其中：它只说明"这两处按文档顺序排列"，是软件猜的。
#: 共享角色只能证明"有关系"，证明不了"谁在谁之前" —— 让这种边参与排序，会把
#: 并列支线错排成串行链。方向未经确认时，宁可当作"无约束"，也不要假排序。
ORDERING_DIRECTIONS: frozenset[str] = frozenset({"control_flow", "agent"})


@dataclass
class GraphEdge:
    """图上的一条关系声明（协议里的 ``{from, to, type, weight?}``）。

    Python 侧字段叫 ``source`` / ``target``，因为 ``from`` 是关键字；序列化时写回
    协议名 ``"from"`` / ``"to"``。

    四个字段各管一件事，**不要混**：

    * ``weight`` —— **载荷**：提供者**首次提供**、消费者**原文里真的出现**的知识
      **条目数**（与术语书同一套写法命中口径，见 :mod:`gametrans.layers.entityflow`）。
      它是"这条边传了几条知识"，不是"关系有多强"：**0 = 没传东西**（纯过场、并列分支），
      值只加不减。扫描刚出来时一律是 0（引擎不认识术语书），由
      :func:`gametrans.layers.entityflow.apply_payloads` 按当前术语书填。它是**派生量**：
      术语书一变就该重算，所以盘上的值会过期 —— 读取方要看
      ``graph.json`` 里的 ``payload_source`` 戳（见 `core/session.py` 的 save_graph）。
    * ``direction`` —— **谁在谁之前**？共享实体回答不了，只有控制流或人/agent 能。
    * ``provenance`` —— 这条边是**谁**提的？软件猜的、agent 定的、还是用户指定的。
    * ``topics`` —— 这条边自己**声明**了哪些知识点（注入要用）。它与载荷不是同一件事：
      载荷按**未化简**的"首见 → 用到"算（被化简掉的前驱照样有载荷），``topics`` 只列
      这条边确实声明的那几条。

    ``type`` 不给就按 ``direction`` 与 ``topics`` 推导。判断依赖的方法本身是可以
    替换的（见 :mod:`gametrans.core.dependencies`）；这里只定义"表达关系需要哪些信息"。
    """

    source: str
    target: str
    type: str = ""
    weight: int = 0
    #: 依赖的具体知识点，例如 ``character:Eileen`` / ``world:Kingsway`` / ``tone:melancholic``
    topics: list[str] = field(default_factory=list)
    #: heuristic | agent | user
    provenance: str = "heuristic"
    #: control_flow | reading_order | agent。默认"未确认"，因为这是不花钱就能得到的下界。
    direction: str = "reading_order"
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 载荷是条目数：只保下界（负数没有意义），**不设上界** —— 一条边可以传很多条知识。
        self.weight = max(0, int(self.weight))
        if self.direction not in DIRECTIONS:
            self.direction = "reading_order"
        if not self.type:
            self.type = self._derived_type()

    def _derived_type(self) -> str:
        if self.direction == "control_flow":
            return EdgeType.SEQUENCE.value
        if self.topics:
            return EdgeType.DEPENDENCY.value
        return EdgeType.CONTEXT.value

    @property
    def orders(self) -> bool:
        """这条边能不能拿去决定先后顺序。"""
        return self.direction in ORDERING_DIRECTIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "type": self.type,
            "weight": self.weight,
            "topics": list(self.topics),
            "provenance": self.provenance,
            "direction": self.direction,
            "note": self.note,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphEdge":
        source = data.get("from", data.get("source", data.get("provider", "")))
        target = data.get("to", data.get("target", data.get("consumer", "")))
        return cls(
            source=str(source),
            target=str(target),
            type=str(data.get("type", "")),
            weight=int(float(data.get("weight", data.get("strength", 0)) or 0)),
            topics=[str(t) for t in (data.get("topics") or [])],
            provenance=str(data.get("provenance", "heuristic")),
            direction=str(data.get("direction", "reading_order")),
            note=str(data.get("note", "")),
            metadata=dict(data.get("metadata") or {}),
        )


# --------------------------------------------------------------------------- #
# Constraint 与校验结果
# --------------------------------------------------------------------------- #


class ConstraintType(str, enum.Enum):
    """机器可验证的结构约束。违反时任务进重试/修复/人工审核，**不写回**。"""

    PLACEHOLDER_COUNT_PRESERVED = "placeholder_count_preserved"
    TAG_BALANCE_PRESERVED = "tag_balance_preserved"
    CONTROL_CODE_PRESERVED = "control_code_preserved"
    REQUIRED_NEWLINES_PRESERVED = "required_newlines_preserved"
    #: 转义序列：``[[`` 丢了会把字面量方括号变成变量插值
    ESCAPE_SEQUENCE_PRESERVED = "escape_sequence_preserved"
    #: 术语标签：译文里的 ``⟦写法⟧`` 必须与"这一条原文该出现的标签"逐条相等（见
    #: :mod:`gametrans.layers.tags`）—— 丢了标签 = 模型自己起了个名字，那是要人裁的
    TERM_TAG_PRESERVED = "term_tag_preserved"
    #: **原文 → 译文对应**：这条译文看着像不像这条原文的译文（长度严重不称、空原文有译文、
    #: 同一批里两条例子的译文一字不差、一条译文整段埋在另一条里）。上面几条守的都是
    #: "结构没被改坏"，这一条守的是"内容没被挂错"—— 判据只做**形状**上的合理性检查，
    #: 判不了语义，见 :func:`gametrans.core.constraints.source_target_correspondence`。
    SOURCE_TARGET_CORRESPONDS = "source_target_corresponds"
    OUTPUT_SHAPE_VALID = "output_shape_valid"


@dataclass
class Constraint:
    """``constraint_type`` 作用在 ``target``（这里是 unit id）上。"""

    constraint_type: str
    target: str = ""
    #: error | warning —— 只有 error 会挡住写回
    severity: str = "error"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_type": self.constraint_type,
            "target": self.target,
            "severity": self.severity,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Constraint":
        return cls(
            constraint_type=str(data.get("constraint_type", "")),
            target=str(data.get("target", "")),
            severity=str(data.get("severity", "error")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ConstraintViolation:
    """一条被机器抓到的违约。``detail`` 里带上实际值，方便 agent 判分支。"""

    constraint_type: str
    target: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_type": self.constraint_type,
            "target": self.target,
            "severity": self.severity,
            "message": self.message,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConstraintViolation":
        return cls(
            constraint_type=str(data.get("constraint_type", "")),
            target=str(data.get("target", "")),
            severity=str(data.get("severity", "error")),
            message=str(data.get("message", "")),
            detail=dict(data.get("detail") or {}),
        )


@dataclass
class ValidationResult:
    """一次校验的完整结果。``checked`` 记录**实际执行过**哪些约束 —— 没检查过的
    不许假装通过。"""

    ok: bool = True
    violations: list[ConstraintViolation] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[ConstraintViolation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[ConstraintViolation]:
        return [v for v in self.violations if v.severity != "error"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": [v.to_dict() for v in self.violations],
            "checked": list(self.checked),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationResult":
        return cls(
            ok=bool(data.get("ok", True)),
            violations=[ConstraintViolation.from_dict(v) for v in (data.get("violations") or [])],
            checked=[str(c) for c in (data.get("checked") or [])],
        )


# --------------------------------------------------------------------------- #
# Artifact
# --------------------------------------------------------------------------- #


class TranslationStatus(str, enum.Enum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"
    #: 译文存在但没通过结构校验（或其它需要人/agent 拍板的情况）—— 一样不写回
    NEEDS_REVIEW = "needs_review"


#: **占位记录**的固定说明：槽位在这一轮**没排到**，台账照旧覆盖全图，于是给它一条空记录。
#:
#: ⚠️ 它长得跟"翻过但没成"一模一样（都是 `status=failed`），可这两件事要分别对待：
#: 一个是"还没轮到"，一个是"试过、坏了"。读数层（`layers/staleness.py`）按这个字面值
#: 把前者归到"还没有译文"，否则一次"跑到第 3 阶段就停"的跑批会让全工程看起来像翻坏了
#: （真靶实测：15,755 条里 12,358 条被读成"不可用（状态 failed）"，其实它们从没被问过）。
PLACEHOLDER_ERROR = "未产出译文"


def is_placeholder(record: "TranslationArtifact") -> bool:
    """这条记录是「这一轮没排到它」的占位吗（见 :data:`PLACEHOLDER_ERROR`）。

    它**不是结果**，只是"台账覆盖全图"的副产品。两条纪律由它决定：

    * 读数把它算"还没有译文"（`layers/staleness.py`），不算"翻坏了"；
    * 落盘时它**不覆盖**盘上已有的记录（`ProjectSession._save_translations`）——
      阶段切窄的跑批会给全图写占位，按槽位合并时把前几轮真翻好的那几条盖成
      "未产出译文"。真靶 2026-09-28 实测：`--start-phase 4` 那次（阶段 4–6）
      把第一轮翻好的 `start` / `act1` 盖没了，用户第二天问"最早两个节点为什么翻译无了"。
    """
    return (
        record.status is TranslationStatus.FAILED
        and str(record.error or "") == PLACEHOLDER_ERROR
        and not record.knowledge_fingerprint
    )


@dataclass
class Provenance:
    """译文是从哪来的：模型、Agent、预设、prompt 摘要……。

    这些不是 Engine Adapter 的责任，但对可重现、增量翻译与问题追踪很重要。
    """

    provider: str = ""
    model: str = ""
    preset: str = ""
    agent: str = ""
    prompt_digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "preset": self.preset,
            "agent": self.agent,
            "prompt_digest": self.prompt_digest,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provenance":
        return cls(
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            preset=str(data.get("preset", "")),
            agent=str(data.get("agent", "")),
            prompt_digest=str(data.get("prompt_digest", "")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class TranslationArtifact:
    """译文不是"原文 → 译文"两列，而是可追踪、可导出的中间产物。

    写回层只认 ``is_usable`` 为真的 Artifact：状态是 OK、有内容、且校验通过。
    """

    unit_id: str
    translated_segments: list[Segment] = field(default_factory=list)
    locator: Locator | None = None
    status: TranslationStatus = TranslationStatus.OK
    provenance: Provenance = field(default_factory=Provenance)
    validation: ValidationResult | None = None
    #: 这条译文依赖的资源版本（术语表/世界书文件的摘要）
    resource_versions: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: 原文留档：补译定位、报告与人工比对都要用
    source: str = ""
    path: str = ""
    error: str | None = None
    #: 这条译文是在什么知识状态下产生的（见 :mod:`gametrans.layers.staleness`）
    knowledge_fingerprint: str = ""
    #: 产出它的翻译任务（Guide §22：Artifact 要能回答"这句话为什么这样翻"）
    task_id: str = ""

    # ---- 派生 ---------------------------------------------------------------

    @property
    def target(self) -> str:
        return "".join(segment.value for segment in self.translated_segments)

    @property
    def translated_text(self) -> str:
        """只取可译部分，供统计使用。"""
        return "".join(s.value for s in self.translated_segments if s.translatable)

    @property
    def is_usable(self) -> bool:
        if self.status is not TranslationStatus.OK:
            return False
        if not self.target.strip():
            return False
        return self.validation is None or self.validation.ok

    @property
    def is_tracked(self) -> bool:
        return bool(self.knowledge_fingerprint)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "translated_segments": [s.to_dict() for s in self.translated_segments],
            "locator": self.locator.to_dict() if self.locator else None,
            "status": self.status.value,
            "provenance": self.provenance.to_dict(),
            "validation": self.validation.to_dict() if self.validation else None,
            "resource_versions": dict(self.resource_versions),
            "metadata": dict(self.metadata),
            "source": self.source,
            "path": self.path,
            "error": self.error,
            "knowledge_fingerprint": self.knowledge_fingerprint,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationArtifact":
        segments_payload = data.get("translated_segments")
        if segments_payload:
            segments = [Segment.from_dict(s) for s in segments_payload]
        elif data.get("target"):
            segments = [Segment(SegmentKind.TEXT.value, str(data["target"]))]
        else:
            segments = []

        provenance_payload = data.get("provenance")
        if isinstance(provenance_payload, dict):
            provenance = Provenance.from_dict(provenance_payload)
        else:
            provenance = Provenance(provider=str(data.get("provider", "")))

        validation_payload = data.get("validation")
        locator_payload = data.get("locator")
        return cls(
            unit_id=str(data.get("unit_id", "")),
            translated_segments=segments,
            locator=Locator.from_dict(locator_payload) if locator_payload else None,
            status=TranslationStatus(str(data.get("status", "ok"))),
            provenance=provenance,
            validation=(
                ValidationResult.from_dict(validation_payload)
                if isinstance(validation_payload, dict)
                else None
            ),
            resource_versions=dict(data.get("resource_versions") or {}),
            metadata=dict(data.get("metadata") or {}),
            source=str(data.get("source", "")),
            path=str(data.get("path", "")),
            error=data.get("error"),
            knowledge_fingerprint=str(data.get("knowledge_fingerprint", "")),
            task_id=str(data.get("task_id", "")),
        )

    @classmethod
    def from_target(
        cls,
        *,
        unit_id: str,
        target: str = "",
        source: str = "",
        status: TranslationStatus = TranslationStatus.OK,
        provider: str = "",
        model: str = "",
        path: str = "",
        locator: Locator | None = None,
        error: str | None = None,
        knowledge_fingerprint: str = "",
        validation: ValidationResult | None = None,
        resource_versions: dict[str, str] | None = None,
        metadata: dict[str, Any] | None = None,
        task_id: str = "",
    ) -> "TranslationArtifact":
        """便捷构造：把一段纯译文包成 Artifact（适配器与测试都用得上）。"""
        segments = (
            [Segment(SegmentKind.TEXT.value, target)] if target else []
        )
        return cls(
            unit_id=unit_id,
            translated_segments=segments,
            locator=locator,
            status=status,
            provenance=Provenance(provider=provider, model=model),
            validation=validation,
            resource_versions=dict(resource_versions or {}),
            metadata=dict(metadata or {}),
            source=source,
            path=path,
            error=error,
            knowledge_fingerprint=knowledge_fingerprint,
            task_id=task_id,
        )


# --------------------------------------------------------------------------- #
# 图谱导航结构（软件内部，不是协议的一部分）
# --------------------------------------------------------------------------- #


#: 估算 token 用的"字符 / token"比。**未经账本校准** —— 先用通用经验值，
#: 等有真跑账本（`lab/harness/ledger`）再换成按模型/语言回归出来的系数。
#: 原文侧按英文、译文侧按中文。它们只影响**估算**，不影响任何判定。
SOURCE_CHARS_PER_TOKEN = 4.0
TARGET_CHARS_PER_TOKEN = 1.6


def estimate_tokens(
    char_count: int, *, target_chars_per_token: float = TARGET_CHARS_PER_TOKEN
) -> int:
    """一段原文译成译文大约要多少 token（**输入 + 输出**，一次直出的口径）。

    只算这一条文本自己：**不含**系统提示 / 术语注入 / 摘要那些每次调用都要重复付的
    固定上下文，也不含分轮造成的重复。那两样取决于**运行参数**（`unit_budget`、
    本轮注入了什么），所以放在报告层按真实参数加（见 `plan` 的 `token_estimate`）——
    节点上留一个与运行参数无关的体量，换参数时不必重算整张图。
    """
    chars = max(0, int(char_count))
    source_tokens = math.ceil(chars / float(SOURCE_CHARS_PER_TOKEN))
    target_tokens = math.ceil(chars / float(target_chars_per_token))
    return int(source_tokens + target_tokens)


@dataclass(frozen=True)
class NodeWeight:
    """带权路径图里一个节点的权重 —— **这个节点自己的开销**。

    主维度是**成本**：完成这段文本要花多少 token（输入 + 输出，一次直出的口径），
    由 :func:`estimate_tokens` 从字符数估出来。``char_count`` 是原始度量，留着供核对；
    引擎或 agent 可以覆盖 ``cost``，把真实计量塞进来。

    它**不决定顺序**：决定翻译顺序的是依赖关系（谁等谁）。这里只回答"做这个节点要花
    多少"；"这个节点有多重要"由**出边的载荷**回答（见 :mod:`gametrans.layers.entityflow`）。

    ``occurrences`` 是成本摊薄因子 —— 同一句话在多处出现，翻一次到处受益。
    """

    char_count: int = 0
    cost: int = 0
    occurrences: int = 1
    depth: int = 1

    def __post_init__(self) -> None:
        # cost 缺省时按字符数估 token；写成 0 也视为"没给"，因为零成本的文本没有意义
        if not self.cost:
            object.__setattr__(self, "cost", estimate_tokens(self.char_count))

    @property
    def sort_key(self) -> tuple[int, int]:
        """列表顺序：成本降序 → 深度升序。

        **它不是调度机制**（谁等谁由前驱集定）。只决定"列给人和 agent 看的顺序"。
        """
        return (-self.cost, self.depth)

    def to_dict(self) -> dict[str, Any]:
        return {
            "char_count": self.char_count,
            "cost": self.cost,
            "occurrences": self.occurrences,
            "depth": self.depth,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeWeight":
        return cls(
            char_count=int(data.get("char_count", 0)),
            cost=int(data.get("cost", 0)),
            occurrences=int(data.get("occurrences", 1)),
            depth=int(data.get("depth", 1)),
        )


@dataclass
class PathNode:
    """导航结构上的一个节点。

    ``path`` 是**逻辑路径**（例如 ``game/script.rpy#start/say[2]``），供 agent 与
    用户指认；``node_id`` 是稳定标识，供结构内引用。承载待译文本的节点带一个
    :class:`TranslationUnit`。
    """

    node_id: str
    path: str
    kind: NodeKind
    weight: NodeWeight
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    unit: TranslationUnit | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_translatable(self) -> bool:
        return self.unit is not None

    @property
    def is_container(self) -> bool:
        return self.unit is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "kind": self.kind.value,
            "weight": self.weight.to_dict(),
            "parent": self.parent,
            "children": list(self.children),
            "unit": self.unit.to_dict() if self.unit else None,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PathNode":
        unit = data.get("unit")
        return cls(
            node_id=str(data["node_id"]),
            path=str(data["path"]),
            kind=NodeKind(str(data["kind"])),
            weight=NodeWeight.from_dict(data.get("weight") or {}),
            parent=data.get("parent"),
            children=list(data.get("children") or []),
            unit=TranslationUnit.from_dict(unit) if unit else None,
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class Issue:
    """报告里的一条问题记录。``code`` 是给 agent 判分支用的稳定枚举。"""

    code: str
    message: str
    ref: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "ref": self.ref,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Issue":
        return cls(
            code=str(data["code"]),
            message=str(data["message"]),
            ref=data.get("ref"),
            detail=dict(data.get("detail") or {}),
        )
