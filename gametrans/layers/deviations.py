"""声明的偏离 —— 给**已有译文**的两类"我知道它会偏离规则，但它是对的"。

判据是两层（见 :mod:`gametrans.core.constraints`）：硬性拦的是"身份丢了、会让游戏显示错"，
写法差异放行。但有两类**合法的硬性偏离**，判据自己认不出来，必须有人拍板：

* ``empty`` —— **有意留空**：这条我就是要它空着（抹掉不该出现的英文、或界面留白）。
  真机语料里前辈这么干了 101 处（引擎自带字符串 + 默认模板的死界面）。
* ``expression`` —— **表达式改写**：把 ``[scorelogs]`` 换成 ``[number_to_chinese(scorelogs)]``
  这类"接一个 helper"的改法（前辈包里有 17 处，用来把运行时数字转成中文）。

## 为什么必须显式

这两类都会让"占位符身份/非空"这些硬判据报警，而它们又是对的。**默认不许**（否则
"手滑丢了占位符"就能混过去），只有声明过的条目才放行 —— 声明里带理由与拍板身份，
报告里照常留痕。模型身份不许拍板（只能提候选），与知识层、补充条目同一条规矩。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gametrans.core.models import Issue
from gametrans.errors import ResourceError
from gametrans.layers.supplements import APPROVER_IDENTITIES

__all__ = ["Deviation", "DeviationStore", "DEVIATION_KINDS"]

DEVIATIONS_FILE = "deviations.jsonl"

#: 允许声明的偏离种类。加新种类要同时想清楚"判据怎么消费它"。
DEVIATION_KINDS = frozenset({"empty", "expression"})


@dataclass
class Deviation:
    """一条声明：这条译文**有意**偏离判据的哪一条。"""

    unit_id: str
    kind: str
    by: str = "agent"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "by": self.by,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Deviation":
        return cls(
            unit_id=str(data.get("unit_id", "")),
            kind=str(data.get("kind", "")),
            by=str(data.get("by", "agent")),
            reason=str(data.get("reason", "")),
        )


class DeviationStore:
    """声明的偏离集合。文件即资源：一行一个 JSON 对象。"""

    def __init__(self, path: Path, legacy_path: Path | None = None) -> None:
        self.path = Path(path)
        #: 早先的位置（`resources/` 里那份）。只在 :meth:`ensure` 上搬一次，读路径不动。
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self._entries: list[Deviation] = []
        self._stamp: tuple[int, int] | None = None
        self.load_warnings: list[str] = []

    # ---- 生命周期 -----------------------------------------------------------

    def ensure(self) -> "DeviationStore":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.legacy_path is not None and not self.path.exists() and self.legacy_path.exists():
            # 早先它住在 `resources/` 里（和"给模型的材料"混住）。搬一次，不留两份。
            # 只在**写路径**（ensure）上做：打开工程、只读查询一个字都不该写。
            self.legacy_path.replace(self.path)
        if not self.path.exists():
            self._save()
        return self

    def _stamp_now(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh(self) -> None:
        stamp = self._stamp_now()
        if stamp == self._stamp:
            return
        self._stamp = stamp
        self.load()

    def load(self) -> None:
        self._entries = []
        if not self.path.is_file():
            return
        for index, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                self._entries.append(Deviation.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                self.load_warnings.append(f"{self.path.name} 第 {index} 行无法解析（{exc}），已跳过")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in self._entries),
            encoding="utf-8",
        )
        self._stamp = self._stamp_now()

    # ---- 读写 ---------------------------------------------------------------

    def entries(self) -> list[Deviation]:
        self._refresh()
        return list(self._entries)

    def by_unit(self) -> dict[str, frozenset[str]]:
        """``unit_id`` → 这条译文被批准了哪些偏离。给判据与写回消费。"""
        self._refresh()
        grouped: dict[str, set[str]] = {}
        for entry in self._entries:
            grouped.setdefault(entry.unit_id, set()).add(entry.kind)
        return {unit_id: frozenset(kinds) for unit_id, kinds in grouped.items()}

    def empty_units(self) -> set[str]:
        """被批准"有意留空"的单位 —— 写回时要把空字符串真的写进去。"""
        self._refresh()
        return {entry.unit_id for entry in self._entries if entry.kind == "empty"}

    def add(self, entry: Deviation) -> Deviation:
        if not entry.unit_id:
            raise ResourceError("偏离声明必须指明是哪条译文", hint="给 unit_id。")
        if entry.kind not in DEVIATION_KINDS:
            raise ResourceError(
                f"不认识的偏离种类：{entry.kind!r}",
                hint=f"可用：{'、'.join(sorted(DEVIATION_KINDS))}。",
            )
        identity = str(entry.by or "").strip().lower()
        if identity not in APPROVER_IDENTITIES:
            raise ResourceError(
                f"这个身份不能批准偏离：{entry.by!r}",
                hint=f"能拍板的是：{'、'.join(sorted(APPROVER_IDENTITIES))}。模型只能提候选。",
            )
        entry.by = identity
        self._refresh()
        self._entries = [
            item
            for item in self._entries
            if not (item.unit_id == entry.unit_id and item.kind == entry.kind)
        ]
        self._entries.append(entry)
        self._save()
        return entry

    def remove(self, unit_id: str, kind: str = "") -> int:
        self._refresh()
        before = len(self._entries)
        self._entries = [
            item
            for item in self._entries
            if not (item.unit_id == unit_id and (not kind or item.kind == kind))
        ]
        removed = before - len(self._entries)
        if removed:
            self._save()
        return removed

    # ---- 报告 ---------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        self._refresh()
        kinds: dict[str, int] = {}
        for entry in self._entries:
            kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
        return {"path": str(self.path), "entries": len(self._entries), "by_kind": kinds}

    def validate(self) -> list[Issue]:
        self._refresh()
        problems: list[Issue] = []
        for entry in self._entries:
            if not entry.unit_id:
                problems.append(
                    Issue(code="deviation_without_unit", message=f"{self.path.name}: 有一条偏离没写 unit_id")
                )
            elif entry.kind not in DEVIATION_KINDS:
                problems.append(
                    Issue(
                        code="deviation_unknown_kind",
                        message=f"{self.path.name}: {entry.unit_id} 的偏离种类不认识（{entry.kind}）",
                        ref=entry.unit_id,
                    )
                )
        return problems
