"""知识模型：申报 → 合并 → 沉淀（Guide §7、§11、§12、§18）。

这一层的存在理由只有一个：**防止 AI 的推断污染长期知识**（Guide §2.2 记的就是那笔账）。

三条不变量：

* ``assertion`` 由**来源**决定，不由谁写进来决定 —— 模型与观察出来的东西只能是
  ``hypothesis``，脚本 / 官方 / 人写的才是 ``fact``（Guide §7）。
* **长期知识只有一个落盘处** —— 就是术语书本身（五栏：``key`` / ``profile`` /
  ``constant`` / ``order`` / ``position``）。**术语书里没有"待审"这个状态**：审核只
  发生在**更正**上 —— 改已有的译名或事实要进 ``termbook.pending.jsonl``，新增（新写法、
  空译名、新事实）直接追加。所以这里不再有"候选台账"这个视图。
* **模型的判定不自动落盘**：这条通道写进去的东西要过两道自己的闸 —— 跨批判据
  （:func:`plan_consolidation`：同一个原文在**不同的批**里各自被申报过一次且译名一致）
  与语料判据（:mod:`gametrans.layers.naming`）。闸门在产品里叫"写入门槛"，不是审核状态。

闭环（Guide §18）：翻译 → 申报 → 跨批合并 → 过闸写入 → 更正进待审队列。
:class:`KnowledgeUpdate` 负责"申报 → 合并"这一段。
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from gametrans.core.constraints import TERM_TAG_RE
from gametrans.errors import GameTransError
from gametrans.layers.trigger import usable_keys

__all__ = [
    "APPROVERS",
    "CANDIDATE_TERM",
    "DEFAULT_MIN_BATCHES",
    "FACT_SOURCES",
    "ConsolidationPlan",
    "DeclarationEvidence",
    "KnowledgeError",
    "KnowledgeItem",
    "KnowledgeStatus",
    "KnowledgeUpdate",
    "SourceConflict",
    "assertion_of",
    "candidate_id",
    "plan_consolidation",
]

#: 谁的判断算"事实来源"。模型与统计观察都不在其中 —— 它们只能产出候选。
FACT_SOURCES: frozenset[str] = frozenset(
    {"script", "official", "human", "user", "agent", "worldbook", "glossary", "import"}
)

#: 谁有权采用待审更正 / 直接编辑术语书。``model`` 刻意不在列：AI 推断默认不是事实。
APPROVERS: frozenset[str] = frozenset({"human", "user", "agent"})


class KnowledgeError(GameTransError):
    """知识库操作被拒绝（例如模型试图批准自己的推断）。"""


def assertion_of(source: str) -> str:
    """来源 → 断言类型。认不出的来源一律当推断，宁可保守。"""
    return "fact" if str(source).strip().lower() in FACT_SOURCES else "hypothesis"


class KnowledgeStatus(str, enum.Enum):
    """一条**申报**在合并流水线里的处置。

    它**不是术语书的一栏**（术语书只有五栏，没有状态）：这一层只回答"本次申报是被
    过闸写进去了，还是还在等第二份证据 / 被语料判据挡下了"。
    """

    PENDING = "pending_validation"
    APPROVED = "approved"
    REJECTED = "rejected"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class KnowledgeItem:
    """一条长期知识：内容之外必须带**来源、范围、版本、优先级、置信度、状态**。"""

    id: str
    type: str
    content: str
    #: 这一行带着的**译名**（没有就是空的）
    target: str = ""
    #: 这一行带着的**设定**（没有就是空的）
    profile: str = ""
    source: str = ""
    version: str = "1"
    scope: str = "global"
    priority: int = 50
    confidence: float = 1.0
    status: str = KnowledgeStatus.PENDING.value
    created_at: str = ""
    updated_at: str = ""
    provenance: str = ""
    #: 支撑它的证据（unit id / 出处）
    evidence: list[str] = field(default_factory=list)
    #: 机器可读的标记。当前只有 :data:`~gametrans.layers.naming.DROP_TAG`
    #: —— "语料判据说这条不像专名"（判成这样的申报**不写进术语书**）。
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now()
        if not self.updated_at:
            self.updated_at = self.created_at

    # ---- 派生 ---------------------------------------------------------------

    @property
    def assertion(self) -> str:
        return assertion_of(self.source)

    @property
    def is_fact(self) -> bool:
        return self.assertion == "fact"

    @property
    def is_approved(self) -> bool:
        return self.status == KnowledgeStatus.APPROVED.value

    @property
    def is_pending(self) -> bool:
        return self.status == KnowledgeStatus.PENDING.value

    @property
    def usable_as_constraint(self) -> bool:
        """能不能当强制约束（例如"这个词必须译成这个"）。"""
        return self.is_fact and self.is_approved

    # ---- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "target": self.target,
            "profile": self.profile,
            "source": self.source,
            "version": self.version,
            "scope": self.scope,
            "priority": self.priority,
            "confidence": self.confidence,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provenance": self.provenance,
            "evidence": list(self.evidence),
            "tags": list(self.tags),
            "assertion": self.assertion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeItem":
        return cls(
            id=str(data.get("id", "")),
            type=str(data.get("type", "")),
            content=str(data.get("content", "")),
            target=str(data.get("target", "")),
            profile=str(data.get("profile", "")),
            source=str(data.get("source", "")),
            version=str(data.get("version", "1")),
            scope=str(data.get("scope", "global") or "global"),
            priority=int(data.get("priority") or 0),
            confidence=float(data.get("confidence") if data.get("confidence") is not None else 0.0),
            status=str(data.get("status", KnowledgeStatus.PENDING.value)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            provenance=str(data.get("provenance", "")),
            evidence=[str(e) for e in (data.get("evidence") or ())],
            tags=[str(t) for t in (data.get("tags") or ())],
        )


# --------------------------------------------------------------------------- #
# 知识更新流水线：翻译 → 观察 → 候选知识（Guide §18）
# --------------------------------------------------------------------------- #

#: 观察用到的申报类型：术语书里的一行（译名与事实都在一行上）。
CANDIDATE_TERM = "term_candidate"


@dataclass
class KnowledgeUpdate:
    """把**模型在响应里申报的实体**并进术语书。

    刻意只做"重复且一致的原申报"这一条判据：它可复现、可解释、有证据，而且产出的
    东西一定只是**它自己的判据允许的那一批** —— 所以它只管"写不写"，管不了"对不对"。
    """

    #: 落盘对象：**术语书自己**（``ResourceLayer.termbook``）。写入走 ``TermBook.apply``：
    #: 新增（新写法 / 空译名 / 新事实）直接追加，**改已有的译名或事实进待审队列**。
    store: Any
    min_occurrences: int = 2
    min_length: int = 4
    base_confidence: float = 0.6
    #: 上一次 :meth:`absorb_declared` 里"同一个实体又说了一遍事实"的条数
    merged_profiles: int = 0
    #: 上一次 :meth:`absorb_declared` 里**申报的事实书上已经写过**的条数（措辞近乎逐字）。
    #: 这些被 `same_fact` 当重复吞掉 —— 它们是"模型没看见已知信息"的直接读数。
    repeated_facts: int = 0
    #: 上一次里**换个说法说的同一件事**的条数（`same_fact` 拦不住、会进待审队列）。
    #: 两类加起来就是"模型把已知事实又报了一遍"。当时问的正是这件事有没有报警。
    near_duplicate_facts: int = 0
    #: 上一次 :meth:`absorb_declared` 里**译名那一栏抄回来的其实是标签本身**的写法。
    #: 这些写法**没有落笔**（名字仍然没定），名字就是它们自己，按出现顺序去重。
    tagged_targets: list[str] = field(default_factory=list)

    def absorb_declared(
        self,
        triples: Iterable[tuple[str, str, str]],
        *,
        verdicts: dict[str, tuple[bool, str]] | None = None,
        approved: Iterable[str] | None = None,
    ) -> list[KnowledgeItem]:
        """模型在响应里申报的**实体**（原文写法 → 译名 / 设定）→ 术语书。

        一条申报 = 术语书里的一行：``source`` 是**原文侧**的写法（这一行的身份，也是
        设定那一侧的写法），``target`` 是它在这批译文里用到的译名，``profile`` 是它背后
        的设定。后两栏**都可以空**，空的那一栏就当没申报。

        与 :meth:`observe` 的分工，一句话说清：

        * ``observe`` 靠"**整份译文里重复且一致**"—— 那是离线全量抽术语的判据，
          一次翻译请求（一个段）里几乎必然全落空：专名句通常只出现一次；
        * ``absorb_declared`` 靠**模型当场申报** —— 覆盖每一句、不多花一次调用，
          而且申报的正是"这一段实际用过的写法"。

        ``approved`` 是**允许新开行**的原文写法集合（跨批合并的结论，见
        :func:`plan_consolidation`）。给 ``None`` = 全部允许（不走跨批合并的调用方）。
        **已经在书里的写法不受它约束**：新写法、空译名、新事实一律免审追加。

        ``verdicts`` 是**语料判据**的意见（``{原文: (是否保留, 理由)}``，见
        :mod:`gametrans.layers.naming`）。判"不保留"的一律**不写进书**（真靶上 `play`
        全小写出现 213 次，写进去就是"每句都注入的硬约束"）。旧版把这种申报写成"候选"
        留痕；新形状没有"待审的条目"，留痕的职责归跨批合并的报告与语料判据自己的读数。

        译名那一栏**是标签本身**（``⟦Wolves⟧``）的申报当"没申报"：模型被要求"原样保留
        标签、不必起名"，抄回来的是记号不是名字 —— 落笔会把记号变成定译，再经写回原样
        进游戏文件。这一批的写法收在 :attr:`tagged_targets` 里，报告会点名。
        """
        # 与 `resource.py` 是**互相引用**的（那边要 `APPROVERS` / `KnowledgeError`），
        # 所以这两个判据在函数里取 —— 只在"书里已经有这一行"那一支用得上。
        from gametrans.layers.resource import near_fact, same_fact

        by_target = {
            str(target).strip(): str(source).strip()
            for source, target, *_rest in triples
            if str(source).strip() and str(target).strip()
        }
        allowed = None if approved is None else {str(item) for item in approved}
        created: list[KnowledgeItem] = []
        seen: set[str] = set()
        for triple in triples:
            source, target, profile = (list(triple) + ["", "", ""])[:3]
            text = str(source).strip()
            translated = str(target).strip()
            setting = str(profile).strip()
            if TERM_TAG_RE.search(translated):
                # 译名那一栏抄回来的其实是**标签本身**（`⟦Wolves⟧`）。标签是"这个名字还
                # 没定译"的临时记号（`layers/tags.py`），不是名字 —— 落笔就等于把记号
                # 当成定译写进书，下一轮它会以"有译名的写法"进硬约束，写回那一刻原样
                # 进游戏文件（实测：真靶 3 个单元就漏出 33 处 `⟦…⟧`）。
                # 这里当**没申报**处理：名字仍然没定，写法照旧包标签，等人或 agent 定。
                if text not in self.tagged_targets:
                    self.tagged_targets.append(text)
                translated = ""
            if len(text) < 2 or not (translated or setting):
                continue
            note = ""
            if setting:
                # 设定那一侧的写法必须是**原文侧**的：写成译文的话这条设定永远
                # 打不响（真靶实测 6/6 条中文写法在 3,269 个槽位上命中 0 句，R62）。
                # 模型把译名当写法写时，用同一批申报里那条术语的原文侧补出来。
                fallback = by_target.get(text)
                if fallback and fallback != text:
                    note = f"写法由同批申报的原文侧补出：{fallback!r}"
                    text = fallback
                elif not usable_keys([text]):
                    note = (
                        f"写法 {text!r} 不像名字（占位符或过短）："
                        "命中面会很宽，要人换一个原文里的写法"
                    )
            if str(self.store.status_of(text)) == "rejected":
                # 已否决的写法不许借这条通道复活（`TermBook.apply` 自己也会挡，
                # 这里早退是为了**不把它算进这一轮的产出**）。
                continue
            if text in seen or self.store.status_of(text) is not None:
                # 这个词书里已经有行了：**免审追加**（新写法、空译名、新事实），
                # 要改已有的译名就进待审队列（`TermBook.apply` 自己分流）。
                #
                # 空译名那一栏照旧由模型申报填上（设计决定：留着它省一次
                # 定名调用）。那些写法在请求里是 `⟦写法⟧`，模型申报了名字就等于把它定了 ——
                # 人要改就走 `resource.term.pending.*` 或直接改那一行，已经产出的译文
                # 不用重翻（写回时按最新术语书渲染，见 `layers/tags.py`）。
                before = self._fact_count(text)
                row = self.store.find(text)
                known = list(row.facts) if row is not None else []
                # **这条申报说的是不是书上已经写过的事**：
                # 逐字/子串的被 `apply` 当重复吞掉，换语序/插词的会排进待审 —— 两类都记下来。
                # 只报数，不参与决策（决策仍只有 `apply` 那一处）。
                if setting and known:
                    if any(same_fact(setting, item) for item in known):
                        self.repeated_facts += 1
                    elif any(near_fact(setting, item) for item in known):
                        self.near_duplicate_facts += 1
                self.store.absorb(
                    text,
                    target=translated,
                    fact=setting,
                    by="model",
                    why=note or "模型在响应里申报的实体",
                )
                if self._fact_count(text) > before:
                    self.merged_profiles += 1
                continue
            seen.add(text)
            if allowed is not None and text not in allowed:
                # 跨批判据还没给够（或语料判据说它不像专名）：**不新开行**。
                continue
            keep, reason = (verdicts or {}).get(text, (True, ""))
            if not keep:
                continue
            if reason:
                note = f"{note}；【语料判据】{reason}" if note else f"【语料判据】{reason}"
            self.store.absorb(
                text,
                target=translated,
                fact=setting,
                by="model",
                why=note or "模型在响应里申报的实体",
            )
            created.append(
                KnowledgeItem(
                    id=candidate_id(text),
                    type=CANDIDATE_TERM,
                    content=text,
                    target=translated,
                    profile=setting,
                    source="model",
                    scope="global",
                    priority=30,
                    confidence=0.5,
                    # 写进去了就是生效的：新形状没有"待审的条目"这个状态。
                    status=KnowledgeStatus.APPROVED.value,
                    provenance=note,
                    evidence=[],
                    tags=[],
                )
            )
        return created

    def _fact_count(self, writing: str) -> int:
        entry = self.store.find(writing)
        return len(entry.facts) if entry is not None else 0

    def observe(
        self,
        records: Iterable[tuple[str, str, str]],
        *,
        approved_sources: Iterable[str] = (),
    ) -> list[KnowledgeItem]:
        """数一遍"已有译文里重复且一致的对应"，**不往术语书里产条目**。

        ``records`` 是 ``(unit_id, 原文, 译文)``。这些对应照旧进翻译记忆（那是调用方
        自己写进去的），但**不再登记成术语候选** —— 设计判断：
        "这个机制出来的术语书条目没啥用"。真靶实测：跑完 act2 + act10 之后，这条通道
        往术语书里塞了 26 行，逐条都不是术语 —— 整句（`I know what I'm doing. {w}…`）、
        语气词（`Yeah.` / `Huh?`）、标点串（`.........`）、拟声词（`*Exhale*` / `EEEEK!`）。
        原因是判据只有"同一原文 ≥2 次且译法一致"，没有任何术语性闸门；而术语要回答的是
        "这个词指谁、后面还会遇到吗"，不是"这句话出现了两次"。

        闸门留在下面（那些 ``continue``）：它们现在只决定**计数**，用来在报告里说明
        "有多少条重复一致的对应被挡在术语书外面"。要让它们重新成为候选，得先有一条能
        区分"术语"与"整句"的判据。
        """
        known = {str(source) for source in approved_sources}
        grouped: dict[str, dict[str, Any]] = {}
        for unit_id, source, target in records:
            text = str(source).strip()
            translated = str(target).strip()
            if len(text) < self.min_length or not translated:
                continue
            if text in known:
                continue
            bucket = grouped.setdefault(
                text, {"targets": set(), "units": [], "complete": True}
            )
            bucket["targets"].add(translated)
            if unit_id not in bucket["units"]:
                bucket["units"].append(str(unit_id))

        settled = 0
        for _text, bucket in grouped.items():
            units = sorted(bucket["units"])
            if len(units) < self.min_occurrences:
                continue
            if len(bucket["targets"]) != 1:
                # 同一原文出现了多种译法 —— 那不是术语候选，是待处理的矛盾。
                continue
            settled += 1
        #: 这一次观察里"重复且一致"的条数（只报数，不登记）
        self.observed_settled = settled
        return []


# --------------------------------------------------------------------------- #
# 跨批合并：把"边跑边积累"的申报整理成可以拍板的结论
# --------------------------------------------------------------------------- #

#: 判"重复且一致"的**批数**门槛。一批 = 一轮调用（批内的调用互相看不见）。
DEFAULT_MIN_BATCHES = 2


@dataclass(frozen=True)
class DeclarationEvidence:
    """一条申报的出处：哪个批、什么原文、模型用了什么译名。

    "哪一批"必须带上：批内的调用是并行发出的、互相看不见，同一批里说两遍**不是**
    两份独立证据。把同批的两次算成两份，就等于让模型给自己作证。
    """

    group: str
    source: str
    target: str


@dataclass
class SourceConflict:
    """同一个原文被申报成了不止一个译名 —— 这是**待裁定的矛盾**，不是术语。

    谁都不批（交给人或 agent 选），但每种写法各出现在哪些批里要留全：只报"有冲突"
    而看不到"哪个更常用"，裁决的人就只能拍脑袋。
    """

    source: str
    targets: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "targets": {key: list(value) for key, value in self.targets.items()},
        }


@dataclass
class ConsolidationPlan:
    """跨批合并的结论。字段名就是处置动作，不另起一套说法。"""

    #: ``(原文, 译名, 在哪些批里这么说过)`` —— 重复且一致，可以写进术语书
    approve: list[tuple[str, str, list[str]]] = field(default_factory=list)
    #: 同一原文多种译名 —— 待裁决：先落第一个，其余进待审更正
    conflict: list[SourceConflict] = field(default_factory=list)
    #: 只在一批里露过面 —— 还是申报，等后面的批给第二份证据
    waiting: list[tuple[str, str, list[str]]] = field(default_factory=list)
    #: 批数够了，但**语料判据**说它不像专名（``termhood:drop``）—— 不写进书
    dropped: list[tuple[str, str, list[str]]] = field(default_factory=list)
    #: 书里已经有这一行了（人写的 / 模型写的 / 已否决）—— 不重判，也不复活
    settled: list[tuple[str, str]] = field(default_factory=list)


def plan_consolidation(
    evidence: Iterable[DeclarationEvidence],
    *,
    min_batches: int = DEFAULT_MIN_BATCHES,
    verdicts: Mapping[str, tuple[bool, str]] | None = None,
    state: Mapping[str, str] | None = None,
) -> ConsolidationPlan:
    """把跨批的申报整理成结论：谁能写进书、谁撞了、谁还差一份证据。

    ``verdicts`` 是**语料判据**的意见（``{原文: (是否保留, 理由)}``，见
    :mod:`gametrans.layers.naming`）；判"不保留"的一律进
    :attr:`ConsolidationPlan.dropped` —— 判据只给建议，但**自动写入不许 force**：
    真靶上 `play` 全小写出现 213 次，写进去就是"每句都注入的硬约束"。

    ``state`` 是这些原文**现在**在术语书里的状态（``""`` = 有行；``"rejected"`` =
    被否决过；表里没有的不要给键）。有状态的一律进 :attr:`ConsolidationPlan.settled`：
    自动写入不许推翻人的否决，也不许在人已经写好之后又插一次手。
    """
    if min_batches < DEFAULT_MIN_BATCHES:
        raise ValueError(
            f"批数门槛至少是 {DEFAULT_MIN_BATCHES}：一批里申报过一次就批准，"
            "等于让模型批准自己"
        )
    verdicts = verdicts or {}
    state = state or {}
    seen: dict[str, dict[str, list[str]]] = {}
    order: list[str] = []
    for item in evidence:
        source = str(item.source).strip()
        target = str(item.target).strip()
        if not source or not target:
            continue
        if source not in seen:
            seen[source] = {}
            order.append(source)
        groups = seen[source].setdefault(target, [])
        if str(item.group) not in groups:
            groups.append(str(item.group))
    plan = ConsolidationPlan()
    for source in order:
        targets = seen[source]
        now = state.get(source)
        if now is not None:
            # 书里已经有这一行了（``""`` = 生效）或被否决过（``"rejected"``）：不重判，
            # 也不复活。"待审的条目"这个状态已经没有住址了 —— 待审的是**更正**。
            plan.settled.append((source, str(now)))
            continue
        if len(targets) > 1:
            plan.conflict.append(SourceConflict(source=source, targets=targets))
            continue
        target, groups = next(iter(targets.items()))
        if len(groups) < min_batches:
            plan.waiting.append((source, target, groups))
            continue
        keep, _reason = verdicts.get(source, (True, ""))
        if not keep:
            plan.dropped.append((source, target, groups))
            continue
        plan.approve.append((source, target, groups))
    return plan


def candidate_id(text: str) -> str:
    """候选的 id：按 ``source``（这个词在原文里的写法）内容寻址 —— 一个词只有一条。"""
    digest = hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()[:12]
    return f"candidate:term:{digest}"
