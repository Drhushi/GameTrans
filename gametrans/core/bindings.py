"""槽位绑定 —— 译文到槽位的唯一映射，以及"还欠哪些槽位"的点名报告。

## 为什么需要这一层

架构上（V1 文档）译文是**落在槽位上**的：单位决定补译范围，
槽位决定译文落到哪个位置。但在此之前：

* :mod:`gametrans.core.slots` 只说槽位**是什么**；
* :mod:`gametrans.core.units` 把槽位装成单位；
* **没有东西把译文挂上去** —— 于是"这条槽位该填哪句译文"回答不出来。

这一层补上它，并且承担第二件更要紧的事：**把覆盖缺口按槽位点名**。

## 为什么"点名"这件事值钱

项目此前的缺口报告给的是一个混合总数（README 里那个"417 条拿不准"），
真缺口和噪音混在一起，看的人只能判断错。按槽位点名之后才能回答：

* 到底哪几条**没翻**；
* 哪几条**翻了但没通过结构校验**（不会写回，报成"已覆盖"就是骗人）；
* 哪几条**压根没挂到单位上**（漏挂等于漏译，而且没人会发现）。

这三种缺口必须分开报，不能加成一个数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from gametrans.core.slots import Slot, SlotSet

__all__ = ["UnitBinding", "SlotBindings", "USABLE_STATUS", "bind_units"]

#: 算作"真的填上了"的状态。别的状态都不写回，因此都算缺口。
USABLE_STATUS = "ok"


@dataclass
class UnitBinding:
    """一个单位声明它管哪些槽位。"""

    unit_id: str
    slot_keys: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise ValueError("单位绑定必须带 unit_id")

        deduped: list[str] = []
        for key in self.slot_keys:
            if key not in deduped:
                deduped.append(key)
        self.slot_keys = deduped

        if not self.slot_keys:
            raise ValueError(
                f"单位 {self.unit_id!r} 没声明任何槽位：空单位会以"
                f"『翻译 0 条』的样子混进产物，必须由边界策略明确划出成员。"
            )


@dataclass
class _Recorded:
    """一条槽位上记的东西。"""

    target: str
    status: str
    provider: str = ""
    model: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: 这条是**声明过的"有意留空"**：空译文也算可用（照样要写进产物）
    allow_empty: bool = False

    @property
    def usable(self) -> bool:
        return self.status == USABLE_STATUS and (bool(self.target) or self.allow_empty)


class SlotBindings:
    """一次翻译任务里，**译文 → 槽位**的全部绑定关系。

    写入侧的规矩：一条槽位恰属一个单位、恰有一个译文位置。
    读取侧的重点是 :meth:`unfilled` 与 :meth:`coverage` —— 缺口必须点名。
    """

    def __init__(self, slots: SlotSet, bindings: Iterable[UnitBinding] | None = None) -> None:
        self._slots = slots
        self._units: dict[str, UnitBinding] = {}
        self._owner: dict[str, str] = {}  # slot_key -> unit_id
        self._records: dict[str, _Recorded] = {}
        if bindings:
            for binding in bindings:
                self.bind(binding)

    # ---- 写入 ---------------------------------------------------------------

    def bind(self, binding: UnitBinding) -> None:
        """把一个单位的成员范围登记下来。

        一条槽位恰属一个单位：被两个单位抢会让同一句翻译两遍、写回两处冲突。
        """
        if binding.unit_id in self._units:
            raise ValueError(f"单位 {binding.unit_id!r} 已经绑定过了")

        for key in binding.slot_keys:
            if self._slots.get(key) is None:
                raise ValueError(
                    f"单位 {binding.unit_id!r} 声明了一条不存在的槽位 {key!r}："
                    f"数据不一致，拒绝绑定。"
                )
            owner = self._owner.get(key)
            if owner is not None:
                raise ValueError(
                    f"槽位 {key!r} 被多个单位抢：已在 {owner!r}，又被 {binding.unit_id!r} 认领。"
                    f"一条槽位恰属一个单位。"
                )
            self._owner[key] = binding.unit_id

        self._units[binding.unit_id] = binding

    def record(
        self,
        slot_key: str,
        target: str,
        *,
        status: str = USABLE_STATUS,
        provider: str = "",
        model: str = "",
        detail: dict[str, Any] | None = None,
        allow_unknown: bool = False,
        allow_empty: bool = False,
    ) -> None:
        """给一条槽位记译文。

        同一条槽位再记一次是**覆盖**（例如重试成功），不是并存两份。

        ``allow_unknown=True`` 用来记"当前槽位集合里没有"的译文 ——
        它出现在真实场景里：绑定来自上一次提取，而游戏更新后槽位变了。
        这类条目**必须能被写回层看见并报成"没落地"**，所以允许存在；
        默认仍然是拒绝（凭空记译文多半是调用方搞错了）。
        """
        if self._slots.get(slot_key) is None and not allow_unknown:
            raise ValueError(f"没有这个槽位：{slot_key!r}，不能凭空记译文")

        self._records[slot_key] = _Recorded(
            target=target,
            status=status,
            provider=provider,
            model=model,
            detail=dict(detail or {}),
            allow_empty=allow_empty,
        )

    # ---- 读取 ---------------------------------------------------------------

    def translation_for(self, slot_key: str) -> str | None:
        """这条槽位该填哪句译文。没记过就返回 ``None``（不是空串 —— 两者含义不同）。"""
        recorded = self._records.get(slot_key)
        return recorded.target if recorded else None

    def record_for(self, slot_key: str) -> dict[str, Any] | None:
        recorded = self._records.get(slot_key)
        if recorded is None:
            return None
        return {
            "target": recorded.target,
            "status": recorded.status,
            "provider": recorded.provider,
            "model": recorded.model,
            "detail": dict(recorded.detail),
            "allow_empty": recorded.allow_empty,
        }

    def unit_of(self, slot_key: str) -> str | None:
        return self._owner.get(slot_key)

    @property
    def slots(self) -> SlotSet:
        """这一批绑定的槽位集合（只读入口）。"""
        return self._slots

    def orphan_keys(self) -> list[str]:
        """记了译文、但**不在当前槽位集合里**的槽位键。

        真实场景：绑定来自上一次提取，而游戏更新后槽位变了 —— 这些译文已经无处可落。
        写回层必须能把它们报成"没落地"，否则用户会以为全翻好了。
        """
        known = {slot.key for slot in self._slots}
        return sorted(key for key in self._records if key not in known)

    def slots_for_unit(self, unit_id: str) -> list[Slot]:
        binding = self._units.get(unit_id)
        if binding is None:
            return []
        found: list[Slot] = []
        for key in binding.slot_keys:
            slot = self._slots.get(key)
            if slot is not None:
                found.append(slot)
        return found

    def unit_ids(self) -> list[str]:
        return sorted(self._units)

    def unbound_slots(self) -> list[Slot]:
        """还没挂到任何单位的槽位。漏挂等于漏译，且产物看起来正常。"""
        return [slot for slot in self._slots if slot.key not in self._owner]

    def unfilled(self) -> list[Slot]:
        """**还欠哪些槽位**：没译文的，或译了但不可用的（如结构校验没过）。

        这是补译的输入，也是缺口报告唯一的可信来源。
        """
        pending: list[Slot] = []
        for slot in self._slots:
            recorded = self._records.get(slot.key)
            if recorded is None or not recorded.usable:
                pending.append(slot)
        return pending

    def coverage(self) -> dict[str, Any]:
        """覆盖情况，**三种缺口分开报**。

        * ``untranslated`` —— 压根没记译文；
        * ``needs_review`` —— 记了但状态不是可用（不会写回）；
        * ``unbound`` —— 没挂到任何单位（漏挂）。

        ``missing`` 是三者之和；刻意不合并成单一数字，免得重演
        "真缺口被噪音淹掉"的老毛病。
        """
        total = len(self._slots)
        usable = 0
        needs_review = 0
        untranslated = 0
        for slot in self._slots:
            recorded = self._records.get(slot.key)
            if recorded is None:
                untranslated += 1
            elif recorded.usable:
                usable += 1
            else:
                needs_review += 1

        unbound = len(self.unbound_slots())
        return {
            "slots": total,
            "usable": usable,
            "needs_review": needs_review,
            "untranslated": untranslated,
            "unbound": unbound,
            "missing": needs_review + untranslated + unbound,
            "coverage": 1.0 if total == 0 else usable / total,
        }


def bind_units(slots: SlotSet, units: Iterable[Any]) -> SlotBindings:
    """把 :func:`~gametrans.core.units.assemble_units` 组装出的单位登记成绑定。

    组装器已经把"哪个单位含哪几条槽位"写进了 ``unit.metadata["slot_keys"]``；
    这里只是把它读出来交给 :class:`SlotBindings`，不做任何重新划分 ——
    边界永远由策略决定，绑定只负责如实登记。
    """
    bindings = SlotBindings(slots)
    for unit in units:
        keys = list((getattr(unit, "metadata", {}) or {}).get("slot_keys") or [])
        if not keys:
            raise ValueError(
                f"单位 {getattr(unit, 'id', '?')!r} 没有 slot_keys："
                f"它不是由 assemble_units 组装的，或元数据被改坏了。"
            )
        bindings.bind(UnitBinding(unit_id=unit.id, slot_keys=keys))
    return bindings
