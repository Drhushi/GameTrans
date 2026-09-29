"""槽位工作流 —— 把「槽位 → 单位 → 译文绑定 → 覆盖报告」串成一条内核链路。

## 为什么需要它

:mod:`gametrans.core.slots`、:mod:`gametrans.core.units`、:mod:`gametrans.core.bindings`
各自都成立，但**内核里没有地方把它们连起来** —— 三块孤立地基。真实使用是一条顺序：

```
外部给的槽位  →  按边界策略组装单位  →  译文记到槽位上  →  覆盖报告 / 缺口点名
```

这条链路**引擎无关**：槽位从哪来不关它的事（那是适配层的活），它只保证
"给一批槽位，就能组装、记账、报缺口"，并且换边界策略**不动槽位身份**。

## 两条不肯让步的规矩

**① 记译文必须指出是哪条槽位。** 一个单位含多条槽位时，"给这个单位一句译文"是
**含糊**的 —— 含糊地接受就会把译文落到猜出来的某一条上，那是静默错位。
:meth:`SlotWorkflow.record` 因此**只在单位恰好含一条槽位时**可用，否则要求
:meth:`SlotWorkflow.record_slots` 逐条指明。

**② 缺口按槽位点名，不按单位。** 一个单位只差一条时，不能把整个单位报成缺 ——
那会让补译范围虚胖，也会掩盖"其实只差一句"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from gametrans.core.bindings import SlotBindings, bind_units
from gametrans.core.slots import Slot, SlotSet
from gametrans.core.units import BoundaryPolicy, assemble_units

__all__ = ["SlotWorkflow"]


@dataclass
class SlotWorkflow:
    """一次提取/翻译往返的内核编排。

    ``units`` 与 ``bindings`` 在构造时一次算好；之后只做"记译文"与"报覆盖"。
    """

    slots: SlotSet
    policy: BoundaryPolicy
    units: list[Any] = field(init=False, default_factory=list)
    bindings: SlotBindings = field(init=False, default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.units = assemble_units(self.slots, self.policy)
        self.bindings = bind_units(self.slots, self.units)

    # ---- 查询 ---------------------------------------------------------------

    def unit(self, unit_id: str) -> Any | None:
        for unit in self.units:
            if unit.id == unit_id:
                return unit
        return None

    def coverage(self) -> dict[str, Any]:
        """整体覆盖情况（三种缺口分开报，见 :meth:`SlotBindings.coverage`）。"""
        return self.bindings.coverage()

    def unfilled(self) -> list[Slot]:
        """还欠哪些槽位 —— 补译的输入，也是控制面回答"还差什么"的唯一入口。"""
        return self.bindings.unfilled()

    def unit_coverage(self) -> dict[str, dict[str, Any]]:
        """按单位汇总覆盖情况。

        补译是按单位排活的，所以"哪个单位还没弄完"必须能一眼看出来。
        但注意缺口的**真相**仍在槽位粒度（:meth:`unfilled`）——
        这里只是把槽位事实按单位聚合，不改变它。
        """
        report: dict[str, dict[str, Any]] = {}
        for unit in self.units:
            binding = self.bindings
            keys = list(unit.metadata.get("slot_keys") or [])
            usable = 0
            needs_review = 0
            untranslated = 0
            for key in keys:
                record = binding.record_for(key)
                if record is None:
                    untranslated += 1
                elif record["status"] == "ok" and record["target"]:
                    usable += 1
                else:
                    needs_review += 1
            total = len(keys)
            report[unit.id] = {
                "slots": total,
                "usable": usable,
                "needs_review": needs_review,
                "untranslated": untranslated,
                "missing": needs_review + untranslated,
                "coverage": 1.0 if total == 0 else usable / total,
            }
        return report

    # ---- 记译文 -------------------------------------------------------------

    def record(
        self,
        unit_id: str,
        target: str,
        *,
        status: str = "ok",
        provider: str = "",
        model: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """给**恰好含一条槽位**的单位记译文。

        含多条槽位时拒绝：那种情形下"这个单位的译文"是含糊的，必须用
        :meth:`record_slots` 逐条指明，否则译文会落到猜出来的某一条上。
        """
        unit = self._require_unit(unit_id)
        keys = list(unit.metadata.get("slot_keys") or [])
        if len(keys) != 1:
            raise ValueError(
                f"单位 {unit_id!r} 含 {len(keys)} 条槽位，"
                f"只说一句译文是含糊的（会落到猜出来的某一条上）。"
                f"请用 record_slots 逐条指明哪条槽位对应哪句译文。"
            )
        self.bindings.record(
            keys[0], target, status=status, provider=provider, model=model, detail=detail
        )

    def record_slots(
        self,
        unit_id: str,
        targets: dict[str, str],
        *,
        status: str = "ok",
        provider: str = "",
        model: str = "",
    ) -> None:
        """逐条槽位记译文。键必须是这个单位自己的槽位。"""
        unit = self._require_unit(unit_id)
        own = set(unit.metadata.get("slot_keys") or [])
        for key in targets:
            if key not in own:
                raise ValueError(
                    f"槽位 {key!r} 不属于单位 {unit_id!r}："
                    f"把别的单位的槽位记到这个单位名下，等于悄悄改掉边界。"
                )
        for key, text in targets.items():
            self.bindings.record(key, text, status=status, provider=provider, model=model)

    def record_many(
        self,
        rows: Iterable[tuple[str, str]],
        *,
        status: str = "ok",
        provider: str = "",
        model: str = "",
    ) -> None:
        """按 ``(槽位键, 译文)`` 批量记 —— 翻译层拿到的是槽位粒度的结果。"""
        for key, text in rows:
            if self.slots.get(key) is None:
                raise ValueError(f"没有这个槽位：{key!r}")
            self.bindings.record(key, text, status=status, provider=provider, model=model)

    def apply_results(self, results: Iterable[dict[str, Any]]) -> int:
        """把**按单位**产出的译文落到槽位上 —— 翻译层与填回之间的桥。

        翻译层按 ``unit_id`` 产出译文（它不认识槽位），而填回骨架要**按槽位**的译文。
        这里负责换算，规矩只有一条：

        * 单位**恰含一条**槽位时，单位键与槽位键就是同一个，直接落；
        * 单位含**多条**槽位时，**拒绝** —— "这个单位的一句译文"是含糊的，
          猜一条填上去就是静默错位。

        返回实际落下去的条数；不在当前单位集合里的结果**不落**（由调用方对照
        :meth:`SlotBindings.orphan_keys` 之类自行核对）。
        """
        applied = 0
        for row in results:
            unit_id = row.get("unit_id") or row.get("unit")
            if not unit_id:
                continue
            unit = self.unit(str(unit_id))
            if unit is None:
                continue

            keys = list(unit.metadata.get("slot_keys") or [])
            if len(keys) != 1:
                raise ValueError(
                    f"单位 {unit_id!r} 含 {len(keys)} 条槽位，"
                    f"按单位落译文是含糊的（会落到猜出来的某一条上）。"
                    f"请先用更细的边界策略，或改用 record_slots 逐条指明。"
                )

            self.bindings.record(
                keys[0],
                str(row.get("target") or ""),
                status=str(row.get("status") or "ok"),
                provider=str(row.get("provider") or ""),
                model=str(row.get("model") or ""),
            )
            applied += 1
        return applied

    # ---- 内部 ---------------------------------------------------------------

    def _require_unit(self, unit_id: str) -> Any:
        unit = self.unit(unit_id)
        if unit is None:
            raise ValueError(f"没有这个单位：{unit_id!r}")
        return unit
