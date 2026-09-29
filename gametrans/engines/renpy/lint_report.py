"""引擎自己的孤儿译文报告（``lint``）—— "游戏更新后哪些旧译文对不上了"。

为什么这件事非引擎不可：光看 ``tl/<语言>/`` 目录分不出孤儿 —— 一条孤儿块与一条正常块
长得一模一样。只有引擎知道"这个 id 在当前的主语言内容里还存不存在"，判据写在它自己的
``renpy/lint.py::check_orphan_translations`` 里（``id 不在主语言 id 集合里`` 即孤儿）。

输出形状（``problem_listing``，同一份源码）：

```
Orphan Translations:

game/tl/chinese/script.rpy:
    * line  1234 (id start_abc123)
```

三条来自源码的注意点：

* 它**一次报所有语言**，靠路径里的 ``tl/<语言>/`` 归属；
* 不带 ``--all-problems`` 时每个文件的条目**全列**（带了才截成 4 条 + ``and N more.``）；
* 引擎的开关叫 ``--no-orphan-tl``（**关掉**报告），所以我们要的是默认行为。

没有官方 SDK 时这件事做不到 —— 那就如实说做不到（带 hint），绝不假装"没有孤儿"。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from gametrans.engines.renpy.skeleton_pipeline import DEFAULT_TIMEOUT, resolve_launcher

__all__ = ["orphan_report", "parse_orphan_report"]

#: 引擎在 lint 输出里用的段头
ORPHAN_HEADER = "Orphan Translations:"

#: ``game/tl/chinese/script.rpy:``（列 0，以冒号收尾）
_FILE_HEADER = re.compile(r"^(?P<file>.+):\s*$")
#: ``    * line  1234 (id start_abc123)``
_ENTRY = re.compile(r"^[ \t]+\*[ \t]+line[ \t]+(?P<line>\d+)[ \t]+\(id[ \t]+(?P<id>[^)]+)\)\s*$")
#: ``    * and 7 more.``（`--all-problems` 才会出现）
_MORE = re.compile(r"^[ \t]+\*[ \t]+and[ \t]+(?P<count>\d+)[ \t]+more\.\s*$")

#: 官方 SDK 里的启动器候选（与骨架生成同一套）
SDK_LAUNCHERS = ("renpy.exe", "renpy.sh", "renpy.py")


def parse_orphan_report(text: str, *, language: str = "") -> dict[str, Any]:
    """把 lint 输出里的孤儿段解析成结构化结果（只认那一段，别的段一概不看）。"""
    entries: list[dict[str, Any]] = []
    other_languages: dict[str, int] = {}
    truncated = 0
    current_file = ""
    current_language = ""
    in_section = False

    for raw in text.splitlines():
        if not in_section:
            if raw.strip() == ORPHAN_HEADER:
                in_section = True
            continue

        if not raw.strip():
            continue

        if raw[:1] not in (" ", "\t"):
            header = _FILE_HEADER.match(raw.strip())
            if header and _looks_like_a_path(header.group("file")):
                current_file = header.group("file")
                current_language = _language_of(current_file)
                continue
            # 段头（`Python Warnings:` 这类）—— 孤儿段到此结束
            break

        entry = _ENTRY.match(raw)
        if entry:
            item = {
                "file": current_file,
                "line": int(entry.group("line")),
                "engine_id": entry.group("id"),
                "language": current_language,
            }
            if language and current_language and current_language != language:
                other_languages[current_language] = other_languages.get(current_language, 0) + 1
            elif language and not current_language:
                other_languages["(路径里看不出语言)"] = (
                    other_languages.get("(路径里看不出语言)", 0) + 1
                )
            else:
                entries.append(item)
            continue

        more = _MORE.match(raw)
        if more:
            truncated += int(more.group("count"))

    count = len(entries) + truncated
    files = len({entry["file"] for entry in entries})
    note = ""
    if truncated:
        note = (
            f"还有 {truncated} 条被 lint 截成 `and N more.` —— "
            "不带 `--all-problems` 跑一次就能拿全"
        )
    return {
        "supported": True,
        "language": language,
        "count": count,
        "files": files,
        "truncated": truncated,
        "entries": entries,
        "other_languages": other_languages,
        "note": note,
    }


def orphan_report(
    project_root: Path,
    *,
    language: str = "",
    options: dict[str, Any] | None = None,
    launchers: tuple[str, ...] = SDK_LAUNCHERS,
    timeout: int = DEFAULT_TIMEOUT,
    run: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """跑一次官方 ``lint``，读出孤儿译文。做不到就如实说做不到。"""
    options = dict(options or {})
    raw = str(options.get("sdk_path") or "").strip()
    if not raw:
        return {
            "supported": False,
            "language": language,
            "reason": "孤儿译文只有引擎自己算得出来，而它需要官方 SDK",
            "hint": (
                "把官方 Ren'Py SDK 的根目录填进来（`gametrans engine option set sdk_path <SDK 目录>`），"
                "或在面板的引擎设置里填；之后这一栏会自动出现。"
            ),
        }

    sdk = Path(raw)
    launcher = resolve_launcher(sdk, launchers) if sdk.is_dir() else None
    if launcher is None:
        return {
            "supported": False,
            "language": language,
            "reason": f"SDK 路径不可用：{sdk}",
            "hint": "填 SDK 的**根目录**（里面有 renpy/ 与启动器的那一层）。",
        }

    command = [str(launcher), str(project_root), "lint"]
    runner = run or _default_runner
    try:
        completed = runner(command, timeout=timeout)
    except OSError as exc:
        return {
            "supported": False,
            "language": language,
            "reason": f"官方 lint 跑不起来：{exc}",
            "hint": "确认启动器可执行、以及当前用户有权限运行它。",
        }
    except subprocess.TimeoutExpired:
        return {
            "supported": False,
            "language": language,
            "reason": f"官方 lint 超过 {timeout} 秒没结束",
            "hint": "工程过大时可以调大超时。",
        }

    stdout = completed.stdout or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if getattr(completed, "returncode", 0) != 0:
        return {
            "supported": False,
            "language": language,
            "reason": f"官方 lint 退出码 {completed.returncode}",
            "hint": "先单独跑一次 `renpy <游戏目录> lint` 看看它报什么。",
            "output_tail": stdout[-2000:],
        }

    parsed = parse_orphan_report(stdout, language=language)
    if not parsed["entries"] and ORPHAN_HEADER not in stdout:
        parsed["note"] = (
            parsed["note"] or "引擎这次的输出里没有孤儿段（可能它没报这一类）"
        )
    return parsed


def _default_runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _looks_like_a_path(text: str) -> bool:
    """文件行 vs 段头：段头（``Python Warnings:``）没有路径特征。"""
    return ("/" in text) or ("\\" in text) or ("." in text)


def _language_of(path: str) -> str:
    parts = [part for part in re.split(r"[\\/]+", path) if part]
    if "tl" in parts:
        index = parts.index("tl")
        if index + 1 < len(parts):
            return parts[index + 1]
    return ""
