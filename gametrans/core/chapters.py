"""章：**由项目申报**（``<工作区>/chapters.json``），提取后给单元盖戳。

为什么不自动推：有的游戏按文件分章、有的只在剧情文本里写一句 "End of Chapter 1"、
还有没任何痕迹的黑天鹅（真靶工程是单文件 29,841 行，章界只在文本里）。
自动规则只能当**提议**，终裁权在人和 agent —— 所以这里是"读申报、盖戳"，不是"猜"。

申报文件长这样（``name`` 是给人看的，``labels`` 是该章包含的结构名；Ren'Py 是 label，
RPGM 是地图/类别名，两边都按单元自己的 ``context.scene`` 匹配）::

    {
      "declared_by": "agent 提议 + 用户确认",
      "chapters": [
        {"name": "第 1 章", "labels": ["start", "act1", "..."], "evidence": "文本 'End of Chapter 1' @8961"}
      ]
    }

落不到章的单元 ``chapter`` 留空（面板把它归到"系统文本"一组，不硬塞）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from gametrans.core.graph import PathGraph

__all__ = ["CHAPTERS_FILE", "ChapterTable", "apply_chapters", "load_chapters"]

#: 申报文件的名字（住在工作区里，跟着项目走）。
CHAPTERS_FILE = "chapters.json"


def load_chapters(workdir: Path) -> list[dict]:
    """读申报；文件不在、读不动、格式不对都当"没申报"（不猜，也不报错）。"""
    path = Path(workdir) / CHAPTERS_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    chapters = payload.get("chapters") if isinstance(payload, dict) else None
    return [item for item in (chapters or []) if isinstance(item, dict)]


def chapter_of_label(workdir: Path) -> dict[str, str]:
    """``结构名 → 章名`` 的对照表（同一结构名出现两次时，先申报的优先）。"""
    mapping: dict[str, str] = {}
    for chapter in load_chapters(workdir):
        name = str(chapter.get("name") or "").strip()
        if not name:
            continue
        for label in chapter.get("labels") or []:
            mapping.setdefault(str(label), name)
    return mapping


def chapter_ranges(workdir: Path) -> list[tuple[str, str, int, int]]:
    """申报里的**行号区间**：``[(章名, 文件, 起, 止)]``。

    为什么还要行号：有些场在图上是一个单元，但它的场名**不是源码里的 ``label`` 行**
    （真靶工程有 4 个这样的剧情场），标签法看不见它们。这时按单元自己的定位
    行号归章 —— 但要**限定文件**：行号是每个文件各自的，不限定的话界面文件里第 42 行
    会被错算进第一章。
    """
    ranges: list[tuple[str, str, int, int]] = []
    for chapter in load_chapters(workdir):
        name = str(chapter.get("name") or "").strip()
        spec = chapter.get("lines")
        if not name or not isinstance(spec, dict):
            continue
        try:
            start = int(spec.get("from"))
            stop = int(spec.get("to"))
        except (TypeError, ValueError):
            continue
        ranges.append((name, str(spec.get("file") or ""), start, stop))
    return ranges


@dataclass
class ChapterTable:
    """章申报的**一次加载 + 两路判定** —— 产品与图外脚本共用同一口径。

    为什么要收成一处：给图上的单元盖章（:func:`apply_chapters`）与"图外的脚本按章过滤"
    （如 `lab/readouts/scene_summaries.py`）本来是两段各写一遍的判定。只按**场名**的那
    一侧看不见"场名不是源码 label 的场"（真靶工程有 4 个：`evelewd1` / `lexilewd1`
    / `sakilewd1` / `menu1` —— 它们在源码里是缩进在块里的 label，行首匹配枚举不到），
    于是同一张图上"产品认为它在第 2 章、脚本认为它没有章"。
    """

    labels: dict[str, str] = field(default_factory=dict)
    ranges: tuple[tuple[str, str, int, int], ...] = ()

    @classmethod
    def load(cls, workdir: Path) -> "ChapterTable":
        return cls(labels=chapter_of_label(workdir), ranges=tuple(chapter_ranges(workdir)))

    @property
    def empty(self) -> bool:
        return not self.labels and not self.ranges

    def of(self, scene: str = "", *, line: int = 0, where: str = "") -> str:
        """这个单元属于哪一章：先按**场名**，没命中的再按**行号 + 文件**兜底。

        行号必须限定文件：行号是每个文件各自的，不限定的话界面文件里第 42 行会被错算进
        第一章。判不出来就返回空串（没章的单元不硬塞）。
        """
        chapter = self.labels.get(str(scene or ""))
        if chapter or not line:
            return chapter or ""
        for name, want_file, start, stop in self.ranges:
            if start <= line < stop and (not want_file or want_file in str(where or "")):
                return name
        return ""


def apply_chapters(graph: PathGraph, workdir: Path) -> int:
    """按申报给单元盖章戳，返回盖上的单元数（没申报就返回 0，什么都不改）。

    先按**场名**（label / 地图）命中；没命中的再按**定位行号 + 文件**兜底 ——
    两路的判定在 :class:`ChapterTable`，与图外脚本共用。
    """
    table = ChapterTable.load(workdir)
    if table.empty:
        return 0
    stamped = 0
    for node in graph.nodes.values():
        unit = node.unit
        if unit is None or unit.context is None:
            continue
        locator = unit.locator
        chapter = table.of(
            str(unit.context.scene or ""),
            line=int(getattr(locator, "line", 0) or 0),
            where=str(getattr(locator, "file", "") or "") if locator else "",
        )
        if chapter and unit.context.chapter != chapter:
            unit.context.chapter = chapter
            stamped += 1
    return stamped
