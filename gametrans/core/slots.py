"""槽位（Slot）—— 可译内容的**最小身份单位**，以及它的两种定键方式。

## 这个模块解决什么问题

引擎交给我们的不是"一堆字符串"，而是"一堆**槽位**"：每条可译内容带着自己的身份与位置。
但身份有**两种定法**，基数完全不同（实测见 V1 文档 §5）：

* :attr:`SlotKeying.IDENTIFIED` —— 引擎给每条出现一个独立 id。
  同一原文出现两次就是**两条**独立槽位，1:1。
  对话、旁白属于这类，回填时按 id 找位置。
* :attr:`SlotKeying.KEYED` —— **键就是原文自己**。同一原文出现两次只算**一条**，N:1。
  引擎侧这是硬约束（重复原文会被引擎直接拒绝），不是我们的选择。
  菜单选项、界面文本属于这类，回填时按原文找位置。

实测依据（哪个引擎的哪条通道对应哪一类）见 V1 文档 §5 ——
**引擎专属细节不进内核**，所以这里只定义抽象，不点名任何引擎。

## 为什么这件事必须在协议里说清、而不能靠猜

因为**槽位自己算不出来**：`"Same line here."` 在对话里是 identified、在菜单里是 keyed，
**同原文、不同类**。猜错哪一类，基数就错了（该两条变一条，或该一条变两条），
下游的补译范围、成本、去重全都跟着错。所以定键方式**必须由适配层如实告知**，
不给就报错，绝不默认成某一种。

## 与"单位"的分工

* **槽位身份** —— 引擎说了算（回填靠它，改了就填不回去）；
* **单位边界** —— 我们说了算（决定补译范围、上下文、成本）。

两者分开，才能做到"将来改单位边界，槽位身份不变、引擎侧不动、已填译文不失效"。
:class:`SlotSet` 负责把槽位归到单位上，并保证 I1（每条槽位恰属一个单位）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = ["SlotKeying", "SlotLocation", "Slot", "SlotSet"]


class SlotKeying(str, enum.Enum):
    """槽位身份是怎么定出来的。

    **没有默认值** —— 不填就报错。猜错这一类，基数就错。
    """

    #: 引擎给每条出现一个独立 id（对话 / 旁白）
    IDENTIFIED = "identified"
    #: 键就是原文（菜单选项 / 界面文本）
    KEYED = "keyed"

    @classmethod
    def parse(cls, value: Any) -> "SlotKeying":
        """严格解析：认不出来就报错，不悄悄回退成某一种。"""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            raise ValueError(
                f"未知的槽位定键方式：{value!r}；"
                f"只认 {[k.value for k in cls]}。定键方式猜错会让基数出错，"
                f"所以这里拒绝默认值。"
            ) from None


@dataclass(frozen=True)
class SlotLocation:
    """槽位在原文件里的一处出现位置。"""

    file: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line}


@dataclass
class Slot:
    """一条可译内容（槽位）。

    ``engine_id`` 只有 identified 槽位才有；keyed 槽位的键是 ``source`` 本身。
    keyed 槽位不给 id 就是不给 —— 不要替引擎编一个。
    """

    keying: "SlotKeying"
    source: str
    file: str
    line: int = 0
    #: 引擎给的 id（仅 identified）
    engine_id: str | None = None
    #: 引擎自己的形态分类（引擎给的可译节点类别名）。某些定键方式下常常没有。
    node_class: str | None = None
    #: 归属于哪个单位（单位边界由我们定，可以为空 = 尚未划分）
    unit_id: str | None = None
    #: 这一条在源里出现了几次（keyed 合并后 > 1）
    occurrences: int = 1
    #: 全部出现位置：keyed 合并时保留每一处，用来回答"这条译文影响几处"
    locations: list[SlotLocation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.keying = SlotKeying.parse(self.keying)

        if not self.source and self.source != "":
            raise ValueError("槽位缺少原文")

        if self.keying is SlotKeying.IDENTIFIED and not self.engine_id:
            raise ValueError(
                "identified 槽位必须带 engine_id：它的身份就是引擎给的 id，"
                "没有 id 就无从回填。"
            )

        if self.keying is SlotKeying.KEYED and self.engine_id:
            raise ValueError(
                f"keyed 槽位的键是原文，不该带 engine_id（收到 {self.engine_id!r}）；"
                f"混用两种身份会让它和 identified 槽位错误地互相影响。"
            )

        # 没显式给 locations 时，用自己这一处兜底
        if not self.locations and self.file:
            self.locations = [SlotLocation(self.file, self.line)]
        if self.occurrences < 1:
            self.occurrences = max(len(self.locations), 1)

    @property
    def key(self) -> str:
        """槽位的身份键，**与位置无关**，且带类别前缀。

        同一句挪个行号仍然是同一个槽位；这是"改动传播"能算准的前提。

        前缀（``id:`` / ``keyed:``）是必需的，不是装饰：两类槽位曾经可能在同一个键空间里
        撞车。极端但真实的情形是引擎算出的 id 恰好等于某句 keyed 原文 ——
        没有前缀的话，keyed 的去重合并会**把 identified 槽位吞掉**，
        表现为"某句对话莫名消失"。有前缀，两类就永远各自独立。
        """
        if self.keying is SlotKeying.IDENTIFIED:
            return f"id:{self.engine_id}"
        return f"keyed:{self.source}"

    @property
    def is_identified(self) -> bool:
        return self.keying is SlotKeying.IDENTIFIED

    @property
    def is_keyed(self) -> bool:
        return self.keying is SlotKeying.KEYED

    def merged_with(self, other: "Slot") -> "Slot":
        """把同一键的另一处出现并进来（keyed 去重）。"""
        locations = list(self.locations)
        for spot in other.locations or [SlotLocation(other.file, other.line)]:
            if not any(s.file == spot.file and s.line == spot.line for s in locations):
                locations.append(spot)

        return Slot(
            keying=self.keying,
            source=self.source,
            file=self.file,
            line=self.line,
            engine_id=self.engine_id,
            node_class=self.node_class or other.node_class,
            unit_id=self.unit_id or other.unit_id,
            occurrences=len(locations),
            locations=locations,
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "keying": self.keying.value,
            "engine_id": self.engine_id,
            "source": self.source,
            "file": self.file,
            "line": self.line,
            "node_class": self.node_class,
            "unit_id": self.unit_id,
            "occurrences": self.occurrences,
            "locations": [spot.to_dict() for spot in self.locations],
            "metadata": dict(self.metadata),
        }


class SlotSet:
    """一次提取里的全部槽位，按身份键索引。

    两类槽位**分开索引**，绝不允许互相合并：同原文在 identified 与 keyed 里
    是两条不同的东西，合并了就会出现"改一处影响另一处"的隐形错误。
    """

    def __init__(self, slots: Iterable[Slot] | None = None) -> None:
        self._slots: dict[str, Slot] = {}
        #: 记录每条槽位归属的单位，用来保证"恰属一个"
        self._owner: dict[str, str] = {}
        if slots:
            for slot in slots:
                self.add(slot)

    # ---- 写入 ---------------------------------------------------------------

    def add(self, slot: Slot) -> Slot:
        """加入一条槽位。

        * identified：同 id 再来一次 = 数据自相矛盾，报错；
        * keyed：同原文再来一次 = **合并**（引擎的硬约束），但要记下每一处位置。
        """
        key = slot.key
        existing = self._slots.get(key)

        if existing is None:
            self._slots[key] = slot
            if slot.unit_id:
                self._owner[key] = slot.unit_id
            return slot

        if slot.is_identified:
            raise ValueError(
                f"identified 槽位 id 重复：{slot.engine_id!r}"
                f"（已有 {existing.source!r}@{existing.file}:{existing.line}，"
                f"又来 {slot.source!r}@{slot.file}:{slot.line}）。"
                f"引擎保证 id 唯一，重复说明数据自相矛盾。"
            )

        merged = existing.merged_with(slot)
        self._slots[key] = merged
        if merged.unit_id:
            self._owner[key] = merged.unit_id
        return merged

    def assign(self, key: str, unit_id: str) -> None:
        """把一条槽位划进某个单位。

        一个槽位**恰属一个单位**（V1 文档 I1）：
        已归属的再改到别的单位就直接报错，而不是默默搬走 ——
        默默搬走会让上一轮算出的补译范围失效。
        """
        if key not in self._slots:
            raise ValueError(f"没有这个槽位：{key!r}")

        current = self._owner.get(key)
        if current and current != unit_id:
            raise ValueError(
                f"槽位 {key!r} 已归属于单位 {current!r}，不能再改到 {unit_id!r}；"
                f"一条槽位恰属一个单位。"
            )

        self._owner[key] = unit_id
        self._slots[key].unit_id = unit_id

    # ---- 读取 ---------------------------------------------------------------

    def get(self, key: str) -> Slot | None:
        """按身份键取。也接受直接给原文（keyed 的便利写法）。"""
        if key in self._slots:
            return self._slots[key]
        return self._slots.get(f"keyed:{key}")

    def __len__(self) -> int:
        return len(self._slots)

    def __iter__(self):
        return iter(self._slots.values())

    def all(self) -> list[Slot]:
        return list(self._slots.values())

    def by_keying(self, keying: SlotKeying) -> list[Slot]:
        wanted = SlotKeying.parse(keying)
        return [slot for slot in self._slots.values() if slot.keying is wanted]

    def by_source(self, source: str) -> list[Slot]:
        """按原文找出全部槽位（可能跨两类，各自独立）。"""
        return [slot for slot in self._slots.values() if slot.source == source]

    def for_unit(self, unit_id: str) -> list[Slot]:
        return [slot for slot in self._slots.values() if slot.unit_id == unit_id]

    def unassigned(self) -> list[Slot]:
        """还没划进任何单位的槽位。

        必须能查出来：漏划等于漏译，而且没人会发现。
        """
        return [slot for slot in self._slots.values() if not slot.unit_id]

    def occurrences_total(self) -> int:
        """槽位覆盖的源出现次数总和 —— 和"槽位数"不是一回事。"""
        return sum(slot.occurrences for slot in self._slots.values())

    def summary(self) -> dict[str, Any]:
        identified = self.by_keying(SlotKeying.IDENTIFIED)
        keyed = self.by_keying(SlotKeying.KEYED)
        return {
            "slots": len(self._slots),
            "identified": len(identified),
            "keyed": len(keyed),
            "occurrences_total": self.occurrences_total(),
            "unassigned": len(self.unassigned()),
        }
