"""骨架状态：让 agent 与用户能看见"引擎给的产物里有什么"。

## 为什么它是只读、且不依赖 SDK 配置

它**不写任何东西**（包括不写译文存储），所以：

* 不需要先配好引擎就能看 —— 骨架目录在就有东西可报；
* 因此它**不预设**"新旧哪条链路作数"（见登记簿 R7）。

只读操作的门槛应当低：想看就能看，不该因为"还没配 SDK"而看不了。

## 报什么

* 骨架在不在、有几个文件、每个文件多少条槽位；
* **identified / keyed 各多少**（两种定键方式，回填方式不同）；
* 认不出来的行（解析告警）—— 不藏；
* 引擎工具链状态（配没配 SDK），因为"能不能刷新骨架"取决于它。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.slots import SlotKeying
from gametrans.engines.renpy.skeleton import parse_skeleton_dir

__all__ = ["skeleton_status", "find_translation_dirs"]

#: 引擎自带的界面文本清单，不是游戏内容（见登记簿 R2）
DEFAULT_SKIP = ("common.rpy",)

#: 骨架目录的一个文件名长这样：script.rpy。用它来**发现**已有的语言目录 ——
#: 我们不猜语言代码有哪些，而是看磁盘上实际存在什么。
_RPY_SUFFIX = ".rpy"


def find_translation_dirs(project_root: Path) -> list[str]:
    """列出工程里已经存在的骨架语言目录名（按字母序）。

    只认"里面有 .rpy 的目录"——空的或不存在的语言目录不算已生成。
    """
    root = Path(project_root) / "game" / "tl"
    if not root.is_dir():
        return []
    found: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if any(child.suffix == _RPY_SUFFIX for child in entry.iterdir() if child.is_file()):
            found.append(entry.name)
    return found


def skeleton_status(
    *,
    project_root: Path,
    language: str = "",
    skip: Iterable[str] = DEFAULT_SKIP,
    include_files: bool = True,
) -> dict[str, Any]:
    """报告骨架状态。**纯只读**，不写盘、不改任何状态。

    ``language`` 留空时自动取"磁盘上已存在的语言目录"（只有一个就用它；
    有多个则报出来让调用方明确指定，而不是替它猜）。
    """
    project_root = Path(project_root)
    available = find_translation_dirs(project_root)

    chosen = language
    ambiguous = False
    if not chosen:
        if len(available) == 1:
            chosen = available[0]
        elif len(available) > 1:
            ambiguous = True

    payload: dict[str, Any] = {
        "project": str(project_root),
        "available_languages": available,
        "language": chosen,
        "skeleton_dir": None,
        "exists": False,
        "files": [],
        "slots": 0,
        "identified": 0,
        "keyed": 0,
        "occurrences_total": 0,
        "warnings": [],
    }

    if ambiguous and not language:
        payload["needs_language"] = True
        payload["note"] = (
            f"工程里有多个已有骨架：{'、'.join(available)}；"
            f"请明确指定目标语言。"
        )
        return payload

    if not chosen:
        payload["note"] = (
            "还没有任何骨架。需要用官方引擎生成一次"
            "（`translate <语言>`），或指定 --language 查看尚未生成的目录。"
        )
        return payload

    tl_dir = project_root / "game" / "tl" / chosen
    payload["skeleton_dir"] = str(tl_dir)

    if not tl_dir.is_dir():
        payload["note"] = f"骨架目录不存在：{tl_dir}"
        return payload

    parsed = parse_skeleton_dir(tl_dir, skip=tuple(skip))
    identified = [s for s in parsed.slots if s.keying is SlotKeying.IDENTIFIED]
    keyed = [s for s in parsed.slots if s.keying is SlotKeying.KEYED]

    payload.update(
        {
            "exists": True,
            "slots": len(parsed.slots),
            "identified": len(identified),
            "keyed": len(keyed),
            "occurrences_total": sum(s.occurrences for s in parsed.slots),
            "warnings": list(parsed.warnings),
        }
    )

    if include_files:
        files: list[dict[str, Any]] = []
        for path in sorted(tl_dir.glob("*" + _RPY_SUFFIX)):
            if path.name in set(skip):
                continue
            files.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "slots": sum(1 for s in parsed.slots if _same_file(s.file, path.name)),
                }
            )
        payload["files"] = files

    return payload


def _same_file(locator_file: str, name: str) -> bool:
    """槽位记录的文件路径可能有多种写法（相对/绝对）。

    这里只做**等价的字面比对**：取路径最后一段。内核不解析路径语义，
    适配层也不该在这里发明一套 —— 能用就行。
    """
    tail = re.split(r"[\\/]", locator_file or "")[-1]
    return tail == name
