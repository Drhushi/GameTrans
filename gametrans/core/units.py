"""单位组装 —— 把槽位清单按**边界策略**装成翻译单位，并守住不变量。

## 这个模块解决什么问题

V1 文档 把提取层的职责分成两层：

* **槽位身份**（引擎的活）—— 见 :mod:`gametrans.core.slots`；
* **单位边界**（我们的活）—— 就是这里。

引擎只给"一条条槽位"，不给"哪几条算一个工作单位"。边界是我们定的产品决定，
它决定**补译范围多小、上下文多准、成本多少**。而边界会有多种（按文件 / 按场景 /
按 agent 指定），所以这里不规定"哪种边界对"，只保证**组装机制可靠**：

1. **不重不漏**（I1）—— 每条槽位恰属一个单位；
2. **可复现**（I3）—— 同一输入两次组装结果一样；
3. **身份与位置无关** —— 源文件头部插入几行，单位身份不该变（否则全项目译文失效）；
4. **身份分层**（I2）—— 改边界不动槽位身份，引擎侧与已填译文都不受影响；
5. **策略写错当场报错** —— 漏掉槽位、抢同一条、引用幽灵，一律拒绝，不产出悄悄错掉的结果。

## 与适配层的分工

适配层给槽位（含引擎的 id 与位置），我们给边界策略。
"哪些结构事实可用来划边界"（场景、事件、控制流、玩家体验顺序）由适配层提供——
本模块只接受已经划好的 :class:`Boundary`，不自己去猜结构。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from gametrans.core.models import Context, Locator, Scanner, TranslationUnit
from gametrans.core.slots import Slot, SlotKeying, SlotSet

__all__ = [
    "Boundary",
    "BoundaryPolicy",
    "GroupByFile",
    "GroupByStructureKey",
    "OneSlotPerUnit",
    "assemble_units",
]


@dataclass
class Boundary:
    """一个单位的成员范围：它包含哪几条槽位。

    ``type`` / ``label`` 由策略声明（能声明就声明，声明不了留空，
    组装时会从槽位推断 —— 但**不编造结构**）。
    """

    #: 属于这个单位的槽位身份键（``Slot.key``）
    slot_keys: list[str]
    #: 单位类型（协议 §5.2 的 ``type``）；留空则从槽位推断
    type: str = ""
    #: 结构归属（label / scene 之类），供人读与调试；拿不到就留空
    label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 去重但保序：同一条槽位被列两次是策略笔误，不接受
        deduped: list[str] = []
        for key in self.slot_keys:
            if key not in deduped:
                deduped.append(key)
        self.slot_keys = deduped

        if not self.slot_keys:
            raise ValueError(
                "边界里一条槽位都没有：空单位会以『翻译 0 条』的样子混进产物，"
                "必须由策略明确划出成员。"
            )


class BoundaryPolicy:
    """边界策略：把槽位清单划成一个或多个边界。

    子类实现 :meth:`group`。**策略不做校验** —— 漏槽位、抢槽位、引用幽灵
    由 :func:`assemble_units` 统一把关，这样任何策略写错都不会静默产出坏结果。
    """

    #: 策略名，写进单位元数据，便于回答"这批单位是按什么划的"
    name: str = "unnamed"

    def group(self, slots: list[Slot]) -> list[Boundary]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"name": self.name}


class GroupByFile(BoundaryPolicy):
    """最保守的边界：一个文件里所有可译内容算一个单位。

    为什么把它当默认：**结构归属目前拿不到**（实测引擎的 label 字段为空，
    见契约 §5.4）。与其编一个看起来精细、实际靠猜的边界，不如先给一个
    粗但**诚实**的边界 —— 它的坏处（补译范围偏大）是可见的，
    而不是"边界看起来对、实际切错了"这种看不见的坏处。

    更细的边界（按场景 / 按事件 / 按 agent 指定）属于后续策略，
    它们需要适配层提供结构事实。
    """

    name = "by-file"

    def group(self, slots: list[Slot]) -> list[Boundary]:
        buckets: dict[str, list[Slot]] = {}
        for slot in slots:
            buckets.setdefault(_file_identity(slot.file), []).append(slot)
        return [
            Boundary(slot_keys=[slot.key for slot in members])
            for _file, members in sorted(buckets.items())
        ]


class OneSlotPerUnit(BoundaryPolicy):
    """最细的边界：一条槽位一个单位。

    翻译层按**单位**产出译文，而引擎的槽位粒度本来就是"一条文本" —— 于是
    "译文落在哪条槽位"不需要猜：单位恰含一条槽位，逐条对应。

    更粗的边界（按 label / 按场景）是后续策略：它们需要适配层提供结构事实，
    而且单位含多条槽位时"按单位落译文"会被明确拒绝（见
    :mod:`gametrans.core.workflow`），必须逐条指出落在哪条槽位上。
    """

    name = "by-slot"

    def group(self, slots: list[Slot]) -> list[Boundary]:
        return [Boundary(slot_keys=[slot.key]) for slot in slots]


class GroupByStructureKey(BoundaryPolicy):
    """按**引擎给出的结构坐标**划边界：同一坐标下的槽位合成一个翻译单位。

    坐标由适配层在槽位元数据里申报（``metadata["boundary_key"]``）—— 内核不认识
    任何引擎的结构，只按申报的键归拢。一个翻译单位因此可以是"一场戏里的多句话"，
    **一个单位就是图上的一条文本，也就是一次翻译请求**。

    与 :class:`OneSlotPerUnit` 的关系：后者是"一条槽位一个单位"（行业默认），
    前者才是"一个该一起理解的结构窗口装多句"。两者都是策略插槽，换策略不动槽位身份。

    槽位缺这个键时**当场报错**：静默把缺键的槽位各自成组，会让"没结构归属"
    悄悄变成一个独立单位 —— 这类事故本项目吃过（一次请求只翻一句话）。
    """

    name = "by-structure"

    def __init__(self, key_name: str = "boundary_key") -> None:
        self.key_name = key_name

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "key": self.key_name}

    def group(self, slots: list[Slot]) -> list[Boundary]:
        missing = [
            slot.key
            for slot in slots
            if not str(slot.metadata.get(self.key_name) or "")
            # 锚在**骨架块**上的文本（引号说话人的名字）本来就没有源码结构坐标，
            # 不该因此报错，也不该塞进某个场景的单元里。判据用适配层申报的
            # `metadata["anchor"]`，内核不认识任何引擎的形态类名。
            and str(slot.metadata.get("anchor") or "") != "block"
        ]
        if missing:
            preview = "、".join(sorted(missing)[:3])
            raise ValueError(
                f"有 {len(missing)} 条槽位没有结构坐标（metadata[{self.key_name!r}]，"
                f"例如 {preview}）：边界由适配层申报，缺键不许各自成组。"
            )
        buckets: dict[str, list[Slot]] = {}
        for slot in slots:
            key = str(slot.metadata.get(self.key_name) or "")
            if not key:
                # 没有结构坐标的独立文本（说话人名字）：**按原文自成一类**。
                # 一个名字翻一次，而不是每出现一次翻一次。
                key = f"{self.name}:{slot.source}"
            buckets.setdefault(key, []).append(slot)
        return [
            Boundary(
                slot_keys=[slot.key for slot in members],
                label=key,
                metadata={"structure_key": key},
            )
            for key, members in sorted(buckets.items())
        ]


def _file_identity(file: str) -> str:
    """把同一文件的不同写法归成一个身份。

    **真实数据上撞出来的**：引擎对同一批内容会给两种路径写法 ——
    对话通道给项目相对路径（``game/script.rpy``），字符串表通道给绝对路径。
    不归一会让同一个文件被劈成两个单位，补译范围与上下文全被切碎。

    只统一"分隔符与冗余成分"，**不解析路径语义、区分大小写** ——
    那属于上层的事（核心不认识任何引擎的目录约定）。
    """
    normalized = file.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _unit_type(boundary: Boundary, slots: list[Slot]) -> str:
    """单位类型：策略声明优先；否则从槽位推断；都没有就按定键方式给个中性值。

    刻意**不编造引擎结构**（不把"文件里有 say 就说是对话场景"这种话写进来）。
    """
    if boundary.type:
        return boundary.type

    classes = {slot.node_class for slot in slots if slot.node_class}
    if len(classes) == 1:
        return sorted(classes)[0].lower()

    keyings = {slot.keying for slot in slots}
    if len(keyings) == 1:
        only = keyings.pop()
        return "identified" if only is SlotKeying.IDENTIFIED else "keyed"
    return "mixed"


def _unit_id(slot_keys: list[str]) -> str:
    """单位身份：**内容寻址**，且与槽位位置无关。

    只吃槽位身份键（那些键本身已经与位置无关），再排序后哈希 ——
    于是"整体挪行号"不改变任何单位身份，"改动前后差异比较"也才有意义。
    """
    payload = "\x00".join(sorted(slot_keys))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"unit_{digest}"


def assemble_units(
    slots: SlotSet,
    policy: BoundaryPolicy,
    *,
    scanner: "Scanner | None" = None,
) -> list[TranslationUnit]:
    """按 ``policy`` 把 ``slots`` 组装成单位，并守住全部不变量。

    任何一种策略写错都在这里被拦下，而不是产出"看起来正常、实际少了几句"的产物。

    ``scanner`` 是适配层申报的受保护结构切分器。不给就如实标记"结构未校验" ——
    槽位来自引擎的串清单，它的 Segment 结构只能由适配层按引擎语法切分，不许假装扫过
    （见 :meth:`~gametrans.core.models.TranslationUnit.from_text`）。
    """
    ordered = slots.all()
    if not ordered:
        return []

    boundaries = policy.group(list(ordered))

    known = {slot.key for slot in ordered}
    by_key = {slot.key: slot for slot in ordered}

    claimed: dict[str, str] = {}  # slot_key -> 它已归属的边界的可读标识
    for index, boundary in enumerate(boundaries):
        label = boundary.label or boundary.type or f"#{index}"
        for key in boundary.slot_keys:
            if key not in known:
                raise ValueError(
                    f"边界申明了一条不存在的槽位 {key!r}（来自 {label}）："
                    f"数据不一致，拒绝组装。"
                )
            if key in claimed:
                raise ValueError(
                    f"槽位 {key!r} 被多个单位抢：已在 {claimed[key]!r}，又被 {label!r} 认领。"
                    f"一条槽位恰属一个单位，否则会翻译两遍、写回两处冲突。"
                )
            claimed[key] = label

    missing = [key for key in known if key not in claimed]
    if missing:
        preview = "、".join(sorted(missing)[:5])
        raise ValueError(
            f"有 {len(missing)} 条槽位没被任何边界覆盖（例如 {preview}）："
            f"漏掉就等于漏译，且产物看起来正常。请修边界策略。"
        )

    units: list[TranslationUnit] = []
    for boundary in boundaries:
        members = [by_key[key] for key in boundary.slot_keys]
        members.sort(key=lambda slot: (slot.file, slot.line))

        # 单位原文 = 各槽位原文顺次拼接；槽位身份留给元数据，回填时按槽位找位置。
        source = "".join(slot.source for slot in members)
        first = members[0]

        metadata: dict[str, Any] = {
            "slot_keys": [slot.key for slot in members],
            "boundary_policy": policy.name,
        }
        if boundary.label:
            metadata["structure_label"] = boundary.label
        metadata.update(boundary.metadata)
        # **内容类别**是适配层在槽位上申报的事实（叙事内容 / 引擎自带的界面与开发工具）。
        # 只在一个单位里的槽位**说法一致**时才往单位上写：混了两种就不写 ——
        # 内核把"没申报"当"照旧产资产"，宁可多产一条候选，也不因为归属说不清而悄悄停产。
        classes = {
            str(slot.metadata.get("content_class") or "").strip() for slot in members
        }
        classes.discard("")
        if len(classes) == 1:
            metadata["content_class"] = classes.pop()

        # **节点内部的结构**：按槽位自己的结构坐标（`label/menu[n]`，适配层申报）
        # 切成有序的连续块。用途是"一个节点可以按结构切开、切开的部分拿去并行"——
        # 大节点（真靶 act25 = 1,972 条槽位）不该只能整段一次问，也不该按条数随便剁。
        # 只记下标区间（`start` / `count`），槽位键在 `slot_keys` 里，不存两份。
        blocks: list[dict[str, Any]] = []
        for index, slot in enumerate(members):
            key = str(slot.metadata.get("structure_label") or "")
            if blocks and blocks[-1]["structure_label"] == key:
                blocks[-1]["count"] += 1
            else:
                blocks.append({"structure_label": key, "start": index, "count": 1})
        if blocks:
            metadata["blocks"] = blocks

        # scanner：由适配层申报（引擎语法只有它认识）；不给就如实标记"结构未校验"。
        unit = TranslationUnit.from_text(
            id=_unit_id(boundary.slot_keys),
            type=_unit_type(boundary, members),
            source=source,
            scanner=scanner,
            locator=Locator(file=first.file, line=first.line, kind="line",
                            payload={"first_slot": first.key}),
            context=Context(),
            metadata=metadata,
        )
        units.append(unit)

    return units
