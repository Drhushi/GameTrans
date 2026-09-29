"""声明的补充条目 —— 引擎不枚举、但玩家看得见的文本。

## 它补的是什么

内容清单以引擎为准（§1.1.2），但有些**玩家一眼就看见**的文本引擎不枚举：

* 字面量说话人名：``"Girl" "台词"`` —— 引擎当它是结构，只把台词算内容；
* 代码里的界面提示：``renpy.input("What is your name?")`` —— 不在引擎的枚举范围；
* 没被 ``_()`` 包起来、却直接显示的界面文案。

**真机探针实测（Ren'Py 8.5.3，语言激活后）**：把 ``old``/``new`` 条目放进该语言的
字符串表，这些文本在运行时会被翻译 ——

===============================  ==========================================
``translate_string('Girl')``     ``女孩``（手动加的条目生效）
``substitute('Girl')``           ``女孩``（渲染路径走的就是它，角色名也走它）
``translate_string('Eileen')``   ``Eileen``（没有条目的原样返回，不误伤）
===============================  ==========================================

也就是说：**补这些内容不用动游戏源码**，把条目落进语言包即可。产物落盘在
:mod:`gametrans.engines.renpy.skeleton_writer`（适配层），这里只管"谁声明了什么"。

## 为什么是"声明"不是"猜"

内容范围不因为这里而放宽：条目必须**显式声明**，每条带出处与理由，并且要有人或 agent
拍板 —— 模型身份不许批准（与知识层同一条规矩），避免"模型觉得自己看见了什么就补什么"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.models import Issue
from gametrans.errors import ResourceError

__all__ = ["Supplement", "SupplementSet", "APPROVER_IDENTITIES"]

SUPPLEMENTS_FILE = "supplements.jsonl"

#: 谁能拍板"这条也算内容"。模型身份不在其列 —— 它只能提候选，不能定稿。
APPROVER_IDENTITIES = frozenset({"human", "user", "agent"})


@dataclass
class Supplement:
    """一条声明：这句原文也要翻（引擎不枚举它）。"""

    #: 字符串表的键：运行时真正被查找的那段文本
    source: str
    target: str
    #: 谁拍的板（human / user / agent）
    by: str = "agent"
    #: 为什么补它（人看得懂的一句话）
    reason: str = ""
    #: 出处（源码位置，便于人核对）；拿不到就留空，不编
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "by": self.by,
            "reason": self.reason,
            "file": self.file,
            "line": self.line,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Supplement":
        return cls(
            source=str(data.get("source", "")),
            target=str(data.get("target", "")),
            by=str(data.get("by", "agent")),
            reason=str(data.get("reason", "")),
            file=str(data.get("file", "")),
            line=int(data.get("line", 0) or 0),
        )


class SupplementSet:
    """声明的补充条目集合。文件即资源：一行一个 JSON 对象，人和 agent 都能直接读写。"""

    def __init__(self, path: Path, legacy_path: Path | None = None) -> None:
        self.path = Path(path)
        #: 早先的位置（`resources/` 里那份）。只在 :meth:`ensure` 上搬一次，读路径不动。
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self._entries: list[Supplement] = []
        #: 文件戳（mtime_ns, size）：文件没变就不重解析，别的进程改了也能跟上
        self._stamp: tuple[int, int] | None = None
        self.load_warnings: list[str] = []

    # ---- 生命周期 -----------------------------------------------------------

    def ensure(self) -> "SupplementSet":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.legacy_path is not None and not self.path.exists() and self.legacy_path.exists():
            # 早先它住在 `resources/` 里（和"给模型的材料"混住）。搬一次，不留两份。
            # 只在**写路径**（ensure）上做：打开工程、只读查询一个字都不该写。
            self.legacy_path.replace(self.path)
        if not self.path.exists():
            self._save()
        return self

    def load(self) -> None:
        self._entries = []
        if not self.path.is_file():
            return
        for index, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                self._entries.append(Supplement.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                self.load_warnings.append(f"{self.path.name} 第 {index} 行无法解析（{exc}），已跳过")

    def _stamp_now(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh(self) -> None:
        """文件没变就不重解析；别的进程改了声明也能跟上（与路径图同一套做法）。"""
        stamp = self._stamp_now()
        if stamp == self._stamp:
            return
        self._stamp = stamp
        self.load()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n" for entry in self._entries),
            encoding="utf-8",
        )
        self._stamp = self._stamp_now()

    # ---- 读写 ---------------------------------------------------------------

    def entries(self) -> list[Supplement]:
        self._refresh()
        return list(self._entries)

    def lookup(self, source: str) -> Supplement | None:
        self._refresh()
        for entry in self._entries:
            if entry.source == source:
                return entry
        return None

    @staticmethod
    def _require_approver(by: str) -> str:
        identity = str(by or "").strip().lower()
        if identity not in APPROVER_IDENTITIES:
            raise ResourceError(
                f"这个身份不能声明补充条目：{by!r}",
                hint=f"能拍板的是：{'、'.join(sorted(APPROVER_IDENTITIES))}。模型只能提候选。",
            )
        return identity

    def add(self, entry: Supplement) -> Supplement:
        """新增/覆盖一条声明（同原文只留一条）。"""
        self._refresh()
        if not entry.source:
            raise ResourceError("补充条目必须有原文", hint="原文是它在字符串表里的键。")
        entry.by = self._require_approver(entry.by)
        self._entries = [item for item in self._entries if item.source != entry.source]
        self._entries.append(entry)
        self._save()
        return entry

    def remove(self, source: str) -> bool:
        self._refresh()
        before = len(self._entries)
        self._entries = [item for item in self._entries if item.source != source]
        if len(self._entries) != before:
            self._save()
            return True
        return False

    # ---- 报告 ---------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        self._refresh()
        return {
            "path": str(self.path),
            "entries": len(self._entries),
            "sources": [entry.source for entry in self._entries[:20]],
        }

    def validate(self) -> list[Issue]:
        """把不合法的条目报出来（不猜、不修）。"""
        self._refresh()
        problems: list[Issue] = []
        for entry in self._entries:
            if not entry.source:
                problems.append(
                    Issue(
                        code="supplement_without_source",
                        message=f"{self.path.name}: 有一条补充条目没有原文（拿不到字符串表的键）",
                    )
                )
            elif not entry.target:
                problems.append(
                    Issue(
                        code="supplement_without_target",
                        message=f"{self.path.name}: {entry.source!r} 没有译文，落盘只会重复原文",
                        ref=entry.source,
                    )
                )
        return problems
