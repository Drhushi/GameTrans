"""RPGM 语言包事实面（只读）。

**这是事实面，不是判据**（契约 §0.3）：只报"引擎里写着什么、文件在不在"，
不替调用方拿主意 —— 用哪个字体、字体从哪来、语言入口怎么接，都由 agent 决定。

RPG Maker MV 与 Ren'Py 在这件事上**形状完全不同**，这几点必须说清：

1. **MV 没有内建的语言切换入口**。没有 ``Language(...)``，偏好设置里也没有语言项 ——
   ``System.json`` 的 ``locale`` 是唯一一个"当前语言"字段。所以"游戏认哪些语言代码"
   在这里不是引擎说了算，而是**插件**说了算；
2. **认不认某种语言由插件的正则决定**。真靶（与夹具）里 ``YEP_MessageCore`` 写着
   ``$dataSystem.locale.match(/^zh/)`` —— 匹配上了才把消息窗字体换成 ``Font Name CH``。
   所以事实面要报出"谁在认、认的是哪个前缀、在哪一行"；
3. **字体来自两个地方**：``fonts/*.css`` 的 ``@font-face``（引擎按文件加载），
   以及插件参数里的字体名（可能是系统字体栈，例如 ``SimHei, Heiti TC, sans-serif``）。
   后者不依赖随包分发字体 —— 与 Ren'Py 那边"必须塞字体进语言目录"是两回事。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from gametrans.engines.rpgm.datafiles import (
    FONT_DIR,
    PLUGIN_SOURCE_DIR,
    PLUGIN_TABLE,
    read_plugins,
    resolve_www_root,
)
from gametrans.engines.rpgm.lzstring import LZStringError, decompress_from_base64

__all__ = ["language_facts"]

#: ``$dataSystem.locale.match(/^zh/)`` —— 插件认语言的实际写法。
_LOCALE_MATCH = re.compile(r"locale\s*\.\s*match\s*\(\s*/\^([A-Za-z_\-]+)/")

#: 插件参数名 → 它对应的语言桶。这是 YEP_MessageCore 的私有约定（引擎知识）。
_FONT_PARAM_BUCKETS: dict[str, str] = {
    "Font Name": "default",
    "Font Name CH": "zh",
    "Font Name KR": "ko",
}

#: ``@font-face { font-family: X; src: url("Y"); }``
_FONT_FACE = re.compile(
    r"@font-face\s*\{(?P<body>[^}]*)\}", re.IGNORECASE | re.DOTALL
)
_FAMILY = re.compile(r"font-family\s*:\s*[\"']?(?P<value>[^;\"']+)", re.IGNORECASE)
_SRC_URL = re.compile(r"url\(\s*[\"']?(?P<value>[^\"')]+)", re.IGNORECASE)

#: 语言代码前缀 → 它是不是目标语言。``zh_CN`` 认下 ``zh``。
def _matches_target(prefix: str, language: str) -> bool:
    wanted = (language or "").strip().lower()
    if not wanted:
        return False
    return wanted.startswith(prefix.lower())


def language_facts(
    project_root: Path,
    *,
    language: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project_root = Path(project_root)
    www = resolve_www_root(project_root)
    if www is None:
        return {
            "language": language,
            "supported": False,
            "detail": (
                f"{project_root} 里没有 RPG Maker MV 的工程结构（找不到 www/js/）"
            ),
        }

    facts: dict[str, Any] = {
        "language": language,
        "supported": True,
        "www_root": str(www),
        "target_known": False,
        "locale": _locale(www),
        "language_entry": {
            "kind": "none",
            "switches": [],
            "detail": (
                "RPG Maker MV 没有内建的语言切换入口：没有 Language(...) 注册表，"
                "偏好设置里也没有语言项。System.json 的 locale 是唯一的'当前语言'字段，"
                "而'认不认某种语言'由插件的正则决定（见 recognized_prefixes）。"
            ),
        },
        "recognized_prefixes": _prefixes(www, language),
        "fonts": _fonts(www),
        "notes": [
            "本引擎不需要随包分发字体：字体名可以写成系统字体栈"
            "（例如 `SimHei, Heiti TC, sans-serif`），缺字形时浏览器/运行时还会逐字回退。",
            "这些是**事实**，不是判据：用哪个字体、语言入口怎么接，由 agent 拿主意。",
        ],
    }
    facts["target_known"] = any(
        entry["matches_target"] for entry in facts["recognized_prefixes"]
    )
    return facts


# --------------------------------------------------------------------------- #
# locale
# --------------------------------------------------------------------------- #


def _system_payload(www: Path) -> tuple[Any, str]:
    """读 ``System.json``（压缩优先，与数据源选择的口径一致）。"""
    compressed = www / "data" / "compressed" / "System.json"
    plain = www / "data" / "System.json"
    for path, is_compressed in ((compressed, True), (plain, False)):
        if not path.is_file():
            continue
        rel = path.relative_to(www).as_posix()
        try:
            raw = path.read_text(encoding="utf-8")
            if is_compressed:
                raw = decompress_from_base64(raw)
            return json.loads(raw), rel
        except (OSError, UnicodeDecodeError, LZStringError, json.JSONDecodeError):
            continue
    return None, ""


def _locale(www: Path) -> dict[str, Any]:
    payload, rel = _system_payload(www)
    if not isinstance(payload, dict) or "locale" not in payload:
        return {
            "value": "",
            "source": rel or "",
            "detail": "读不出 System.json 的 locale（文件缺失或读不了）",
        }
    return {"value": str(payload.get("locale") or ""), "source": f"{rel}#locale"}


# --------------------------------------------------------------------------- #
# 认哪些语言前缀
# --------------------------------------------------------------------------- #


def _prefixes(www: Path, language: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    source_dir = www / PLUGIN_SOURCE_DIR
    if source_dir.is_dir():
        for path in sorted(source_dir.glob("*.js")):
            rel = path.relative_to(www).as_posix()
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, start=1):
                for match in _LOCALE_MATCH.finditer(line):
                    found.append(
                        {
                            "prefix": match.group(1),
                            "via": f"{rel}:{number}",
                            "matches_target": _matches_target(match.group(1), language),
                        }
                    )
    return found


# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #


def _fonts(www: Path) -> dict[str, Any]:
    return {
        "css_faces": _css_faces(www),
        "message_window": _message_window_fonts(www),
    }


def _css_faces(www: Path) -> list[dict[str, Any]]:
    font_dir = www / FONT_DIR
    faces: list[dict[str, Any]] = []
    if not font_dir.is_dir():
        return faces
    for path in sorted(font_dir.glob("*.css")):
        rel = path.relative_to(www).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _FONT_FACE.finditer(text):
            body = match.group("body")
            family = _FAMILY.search(body)
            url = _SRC_URL.search(body)
            if not family or not url:
                continue
            line = text[: match.start()].count("\n") + 1
            target = (font_dir / url.group("value").strip()).resolve()
            faces.append(
                {
                    "family": family.group("value").strip(),
                    "url": url.group("value").strip(),
                    # 引擎按文件加载字体：文件不在，渲染到它就炸 —— 这是事实，不是判据
                    "exists": target.is_file(),
                    "declared_at": f"{rel}:{line}",
                }
            )
    return faces


def _message_window_fonts(www: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for plugin in read_plugins(www):
        parameters = plugin.get("parameters")
        if not isinstance(parameters, dict):
            continue
        for key, bucket in _FONT_PARAM_BUCKETS.items():
            if key not in parameters:
                continue
            value = parameters.get(key)
            if value is None or not str(value).strip():
                continue
            entries.append(
                {
                    "bucket": bucket,
                    "value": str(value),
                    "via": f"{plugin.get('name')}.{key}",
                    "declared_at": f"{PLUGIN_TABLE}#{plugin.get('name')}.{key}",
                }
            )
    return entries
