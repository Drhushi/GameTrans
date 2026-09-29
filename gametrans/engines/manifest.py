"""适配包的**身份声明** —— 一个适配包"是谁、什么版本、要哪个内核"。

声明单独成文件（``engine.json``）而不是只写在 Python 类里，理由只有一条：
**它必须能在不执行适配包代码的前提下被读到**。装载器要先看它认不认这个协议、
能不能装，然后才决定要不要 import 那份代码 —— "先执行再看兼容性"顺序反了，
一个为旧协议写的包就可能在新内核上跑出莫名其妙的行为。

声明与实现两处都有名字和版本，所以两处不一致必须**报出来**（见 loader）：
* 名字不一致 → 拒装（注册名必须唯一权威，不能一边说 A 一边说 B）；
* 版本不一致 → 装载，但以声明为准并把差异报出来（用户看的是声明）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gametrans.errors import EngineError

__all__ = ["PACK_MANIFEST_NAME", "REQUIRED_FIELDS", "PackManifest", "read_manifest"]

#: 适配包目录里的身份声明文件名。
PACK_MANIFEST_NAME = "engine.json"

#: 必填字段。``entry`` 是"实例在模块里的属性名"，装载器按它取。
REQUIRED_FIELDS = ("name", "version", "protocol", "entry")


@dataclass(frozen=True)
class PackManifest:
    """一份读得通的身份声明。"""

    name: str
    version: str
    protocol: str
    entry: str
    directory: Path
    authors: tuple[str, ...] = field(default_factory=tuple)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "protocol": self.protocol,
            "entry": self.entry,
            "authors": list(self.authors),
            "summary": self.summary,
            "directory": str(self.directory),
        }


def _as_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip()


def read_manifest(directory: Path) -> PackManifest:
    """读一个适配包目录的身份声明。

    读不通就抛 :class:`~gametrans.errors.EngineError`，**消息本身就是给用户看的
    拒绝理由**（装载器把它原样记进"被拒清单"）—— 所以每条都说清"哪不对、怎么办"。
    """
    directory = Path(directory)
    path = directory / PACK_MANIFEST_NAME
    if not path.is_file():
        raise EngineError(
            f"{directory} 里没有 {PACK_MANIFEST_NAME}，因此不知道这是个什么适配包",
            hint=(
                f"适配包目录里要放一份 {PACK_MANIFEST_NAME}，至少写清 "
                f"{'、'.join(REQUIRED_FIELDS)} 四样。"
            ),
        )
    try:
        # ``utf-8-sig``：Windows 上记事本存 JSON 会带 BOM，那不是"坏声明"。
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "msg", str(exc))
        raise EngineError(
            f"{path} 读不出来（{detail}）",
            hint=f"确认它是一份完整的 JSON，例如 {{\"name\": \"...\", \"version\": \"...\"}}。",
        ) from None
    if not isinstance(payload, dict):
        raise EngineError(
            f"{path} 的顶层不是一个对象",
            hint=f"{PACK_MANIFEST_NAME} 必须是一个 JSON 对象（花括号包起来）。",
        )

    missing = [key for key in REQUIRED_FIELDS if not _as_text(payload, key)]
    if missing:
        raise EngineError(
            f"{path} 缺少必填字段：{'、'.join(missing)}",
            hint=f"必填：{'、'.join(REQUIRED_FIELDS)}。",
        )

    authors_payload = payload.get("authors") or ()
    if isinstance(authors_payload, str):
        authors: tuple[str, ...] = (authors_payload,)
    elif isinstance(authors_payload, (list, tuple)):
        authors = tuple(str(item) for item in authors_payload)
    else:
        raise EngineError(
            f"{path} 的 authors 不是一个列表",
            hint="authors 可以省掉；要写就写成字符串数组。",
        )

    return PackManifest(
        name=_as_text(payload, "name"),
        version=_as_text(payload, "version"),
        protocol=_as_text(payload, "protocol"),
        entry=_as_text(payload, "entry"),
        directory=directory,
        authors=authors,
        summary=_as_text(payload, "summary"),
    )
