"""语言包事实 —— 游戏认哪些语言、文本靠什么字体显示、语言目录里现有什么。

## 为什么在适配层

`config.language`、`Language("...")`、`gui.*_font`、`style ...: font ...`、`{font=...}`、
`tl/<语言>/` 目录约定 —— 全是引擎私有知识。内核不认识其中任何一个（登记簿 §1.1.2），
所以"把事实读出来"这件事只能长在适配层。

## 分工：说事实，不拿主意

这个模块**只报"引擎里写着什么、文件在不在"**，不做判据、不改任何东西：

* 用哪个字体、字体从哪来、要不要随包分发 —— 交给 agent 决定；
* 语言入口怎么接（改配置 / 往游戏自带的语言字典补一条 / 别的方式）—— 交给 agent 决定；
* 我们只保证**该说的说到位**：每个事实都带 `文件:行号`，找不到的文件单独点名。

真事故就是这两类看不见：

1. `text_font "tl/chinese/fonts/X.ttf"` 指向不存在的文件 —— 渲染到那一步直接
   `Exception: Could not find font`，而报告里当时什么都没有；
2. 游戏配置写的是 `chinese`、我们产出的是 `zh_CN` —— 译文进得去、玩家点不到。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from gametrans.engines.renpy.extractor import discover_rpy_files
from gametrans.engines.renpy.skeleton_ops import find_translation_dirs
from gametrans.engines.renpy.skeleton_pipeline import translation_dir

__all__ = ["language_facts"]

#: Ren'Py 的内容范围与排除项。适配器自己调时会传它的申报值，这里是给
#: 直接调用的场合（测试、一次性脚本）兜底 —— 与 `RenPyPack` 的声明一致。
RENPY_FILE_GLOBS = ("game/**/*.rpy",)
RENPY_EXCLUDED_PARTS = ("tl", ".gametrans", "renpy")

#: 引擎自带的字体：运行时由 Ren'Py 提供，不需要随语言包分发。
BUILTIN_FONTS = frozenset(
    {
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
        "DejaVuSans-Oblique.ttf",
        "DejaVuSans-BoldOblique.ttf",
    }
)

#: ``define config.language = "chinese"`` / ``config.language = "chinese"``
_CONFIG_LANGUAGE = re.compile(r"""config\.language\s*=\s*(?P<q>['"])(?P<code>[^'"]*)(?P=q)""")
#: ``Language("chinese")`` / ``Language(None)`` —— None 就是引擎的"默认语言"
_LANGUAGE_CALL = re.compile(r"""\bLanguage\(\s*(?P<arg>None|(?P<q>['"])(?P<code>[^'"]*)(?P=q))\s*\)""")
#: ``language_titles["chinese"] = ...`` —— 游戏自己那套语言表
_HOOK_SUBSCRIPT = re.compile(
    r"""\b(?P<name>\w*[Ll]anguage\w*)\s*\[\s*(?P<q>['"])(?P<code>[^'"]*)(?P=q)\s*\]\s*="""
)
#: ``language_titles = {"chinese": "中文"}``
_HOOK_DICT = re.compile(r"""\b(?P<name>\w*[Ll]anguage\w*)\s*=\s*\{(?P<body>[^}]*)\}""")
_QUOTED = re.compile(r"""['"](?P<value>[^'"]*)['"]""")
#: ``gui.text_font = "DejaVuSans.ttf"``（define 与 init python 里都是这个形状）
_GUI_FONT_VAR = re.compile(r"""\b(?P<name>gui\.\w*font\w*)\s*=\s*(?P<q>['"])(?P<value>[^'"]+)(?P=q)""")
#: 样式块头：``style mytext:`` / ``style mytext is text:`` / ``translate chinese style mytext is text:``
_STYLE_HEAD = re.compile(r"^(?P<indent>\s*)(?:translate\s+\S+\s+)?style\s+(?P<name>[\w.]+)(?:\s+is\s+[\w.]+)?\s*:\s*$")
#: ``font "..."`` / ``text_font "..."`` —— 样式体、screen 属性都用这个形状
_FONT_ATTR = re.compile(r"""\b(?:text_)?font\s+(?P<q>['"])(?P<value>[^'"]+)(?P=q)""")
#: 正文里的行内字体：``{font=adeb.ttf}...{/font}``
_FONT_TAG = re.compile(r"\{font=(?P<value>[^{}]+)\}")
#: ``translate chinese style mytext is text:``
_TRANSLATE_STYLE = re.compile(r"^\s*translate\s+(?P<lang>\S+)\s+style\s+(?P<name>[\w.]+)")


def _relpath(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:  # pragma: no cover - 扫描范围都在工程内
        return path.as_posix()


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _language_section(root: Path, sources: Iterable[Path], target: str) -> dict[str, Any]:
    """游戏认哪些语言、谁在切、目标语言是不是其中之一。"""
    on_disk = find_translation_dirs(root)
    configured: list[dict[str, Any]] = []
    switches: list[dict[str, Any]] = []
    hooks: list[dict[str, Any]] = []

    for path in sources:
        rel = _relpath(root, path)
        for number, line in enumerate(_lines(path), start=1):
            for match in _CONFIG_LANGUAGE.finditer(line):
                configured.append({"language": match.group("code"), "file": rel, "line": number})
            for match in _LANGUAGE_CALL.finditer(line):
                code = None if match.group("arg") == "None" else match.group("code")
                switches.append({"language": code, "file": rel, "line": number})
            for match in _HOOK_SUBSCRIPT.finditer(line):
                hooks.append(
                    {"name": match.group("name"), "language": match.group("code"), "file": rel, "line": number}
                )
            for match in _HOOK_DICT.finditer(line):
                for value in _QUOTED.findall(match.group("body")):
                    hooks.append(
                        {"name": match.group("name"), "language": value, "file": rel, "line": number}
                    )

    known = sorted(
        {
            item["language"]
            for item in [*configured, *switches, *hooks]
            if item["language"]
        }
    )
    return {
        "target": target,
        "known": known,
        "on_disk": on_disk,
        #: 目标语言在不在"游戏认的代码"里 —— 不在就意味着玩家可能点不到我们产出的目录。
        #: 这是**事实**，不是判据：报出来让 agent 决定怎么接。
        "target_known": bool(target) and target in known,
        "target_on_disk": bool(target) and target in on_disk,
        "configured_default": configured,
        "switches": switches,
        "hooks": hooks,
    }


def _resolve_font(reference: str, root: Path, language: str, sdk_path: str) -> tuple[str, str]:
    """这个字体引用指向哪、在不在。

    返回 ``(状态, 解析到的路径)``，状态取值：

    * ``found`` —— 游戏目录或语言目录里真的存在；
    * ``builtin_found`` / ``builtin_unverified`` —— 引擎自带的字体（没配 SDK 就只能说"没核实"）；
    * ``missing`` —— 哪儿都没有。**运行时渲染到它就会抛异常**，所以要单独点名。
    """
    name = str(reference or "").strip()
    if not name:
        return "missing", ""

    game = root / "game"
    candidates: list[Path] = []
    if "/" in name or "\\" in name:
        candidates.extend([game / name, root / name])
    else:
        candidates.append(game / name)
        if language:
            candidates.append(game / "tl" / language / name)
    for candidate in candidates:
        if candidate.is_file():
            return "found", str(candidate)

    if name in BUILTIN_FONTS:
        if sdk_path:
            builtin = Path(sdk_path) / "renpy" / "common" / name
            if builtin.is_file():
                return "builtin_found", str(builtin)
        return "builtin_unverified", ""
    return "missing", ""


def _scan_fonts(
    root: Path,
    path: Path,
    *,
    origin: str,
    variables: list[dict[str, Any]] | None,
    styles: list[dict[str, Any]] | None,
    references: dict[tuple[str, str, int], dict[str, Any]],
    style_overrides: list[str] | None = None,
) -> None:
    rel = _relpath(root, path)
    style_name: str | None = None
    style_indent = -1

    for number, line in enumerate(_lines(path), start=1):
        head = _STYLE_HEAD.match(line)
        if head:
            style_name = head.group("name")
            style_indent = len(head.group("indent"))
            if style_overrides is not None and _TRANSLATE_STYLE.match(line):
                style_overrides.append(style_name)
            continue
        if style_name is not None and line.strip():
            if len(line) - len(line.lstrip()) <= style_indent:
                style_name = None

        if variables is not None:
            for match in _GUI_FONT_VAR.finditer(line):
                variables.append(
                    {
                        "name": match.group("name"),
                        "value": match.group("value"),
                        "file": rel,
                        "line": number,
                    }
                )
                # 变量指着的那个字体同样是"引用"：它找不到，渲染照样崩
                references.setdefault(
                    (match.group("value"), rel, number),
                    {
                        "reference": match.group("value"),
                        "file": rel,
                        "line": number,
                        "origin": origin,
                        "form": "gui_variable",
                        "name": match.group("name"),
                    },
                )
        for match in _FONT_ATTR.finditer(line):
            references.setdefault(
                (match.group("value"), rel, number),
                {
                    "reference": match.group("value"),
                    "file": rel,
                    "line": number,
                    "origin": origin,
                },
            )
            if styles is not None and style_name is not None:
                styles.append(
                    {"name": style_name, "font": match.group("value"), "file": rel, "line": number}
                )
        for match in _FONT_TAG.finditer(line):
            references.setdefault(
                (match.group("value"), rel, number),
                {
                    "reference": match.group("value"),
                    "file": rel,
                    "line": number,
                    "origin": origin,
                    "form": "inline_tag",
                },
            )


def _font_section(
    root: Path,
    language: str,
    sdk_path: str,
    sources: Iterable[Path],
    pack_files: Iterable[Path],
) -> dict[str, Any]:
    """字体从哪来：变量指谁、样式设了谁、都找不找得到。"""
    variables: list[dict[str, Any]] = []
    styles: list[dict[str, Any]] = []
    references: dict[tuple[str, str, int], dict[str, Any]] = {}

    for path in sources:
        _scan_fonts(
            root, path, origin="game_source", variables=variables, styles=styles, references=references
        )
    for path in pack_files:
        _scan_fonts(
            root, path, origin="language_pack", variables=variables, styles=None, references=references
        )

    resolved: list[dict[str, Any]] = []
    for entry in references.values():
        status, path = _resolve_font(entry["reference"], root, language, sdk_path)
        resolved.append({**entry, "status": status, "resolved": path})
    resolved.sort(key=lambda item: (item["status"] != "missing", item["reference"]))

    return {
        "variables": variables,
        "styles": styles,
        "all": resolved,
        "missing": [item for item in resolved if item["status"] == "missing"],
    }


def _pack_section(root: Path, pack_dir: Path, pack_files: Iterable[Path], language: str) -> dict[str, Any]:
    """语言目录里现在有什么（事实）——agent 据此判断还差什么。"""
    by_suffix: Counter[str] = Counter()
    total_bytes = 0
    files = 0
    if pack_dir.is_dir():
        for path in sorted(pack_dir.rglob("*")):
            if not path.is_file():
                continue
            files += 1
            total_bytes += path.stat().st_size
            by_suffix[path.suffix.lower() or "(无扩展名)"] += 1

    style_overrides: list[str] = []
    font_variables_set: list[str] = []
    for path in pack_files:
        _scan_fonts(
            root,
            path,
            origin="language_pack",
            variables=None,
            styles=None,
            references={},
            style_overrides=style_overrides,
        )
        for line in _lines(path):
            for match in _GUI_FONT_VAR.finditer(line):
                if match.group("name") not in font_variables_set:
                    font_variables_set.append(match.group("name"))

    return {
        "language": language,
        "directory": str(pack_dir),
        "exists": pack_dir.is_dir(),
        "files": files,
        "total_bytes": total_bytes,
        "by_suffix": dict(sorted(by_suffix.items())),
        "style_overrides": sorted(set(style_overrides)),
        "font_variables_set": font_variables_set,
    }


def language_facts(
    *,
    project_root: Path,
    language: str = "",
    sdk_path: str = "",
    file_globs: Iterable[str] | None = None,
    excluded_parts: Iterable[str] | None = None,
) -> dict[str, Any]:
    """把语言包相关的事实读出来。**纯只读**，不写盘、不做判断。

    ``language`` 是目标语言（决定去哪找语言目录）；``sdk_path`` 只用来核实引擎自带的
    字体到底在不在（没配就如实说"没核实"）。
    """
    root = Path(project_root)
    language = str(language or "").strip()
    sources = discover_rpy_files(
        root,
        tuple(file_globs or RENPY_FILE_GLOBS),
        tuple(excluded_parts or RENPY_EXCLUDED_PARTS),
    )
    pack_dir = translation_dir(root, language) if language else root / "game" / "tl"
    pack_files = sorted(pack_dir.rglob("*.rpy")) if pack_dir.is_dir() else []

    return {
        "language": language,
        "languages": _language_section(root, sources, language),
        "fonts": _font_section(root, language, str(sdk_path or ""), sources, pack_files),
        "language_pack": _pack_section(root, pack_dir, pack_files, language),
    }
