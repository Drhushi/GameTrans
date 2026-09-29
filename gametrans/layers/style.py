"""风格指南：一等翻译资源（Translation Layer Architecture Guide §13、§21）。

Style 从"提示词里临时写的一句话"变成**正式资源**，因为它对成品文字质量的影响
比堆更多背景知识更明显，而且它必须能被复用、被版本化、被实验对照。

两条边界在这里落地：

* **Style 与 Engine Constraint 分开** —— 占位符、标签、控制码属于适配器的技术约束，
  古典/克制/口语化属于 Style；两者分别注入、分别统计。
* **有作用域** —— 全局、人物、场景、区域、单元五级；按任务解析后再注入，而不是
  把整份风格指南无差别塞进每次调用。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.models import Issue

__all__ = [
    "ASPECT_LABELS",
    "STYLE_ASPECTS",
    "STYLE_FILE",
    "StyleEntry",
    "StyleGuide",
    "scope_specificity",
]

STYLE_FILE = "style.jsonl"

#: 文档 §13 列出的风格维度。取值不限于这些 —— 未登记的 aspect 原样保留、原样渲染。
STYLE_ASPECTS: tuple[str, ...] = (
    "target_language",
    "tone",
    "narrative",
    "dialogue",
    "character_voice",
    "formality",
    "lexicon",
    "punctuation",
    "forbidden",
    "naming",
)

ASPECT_LABELS: dict[str, str] = {
    "target_language": "目标语言与地区",
    "tone": "整体文风",
    "narrative": "叙事文本风格",
    "dialogue": "对白风格",
    "character_voice": "人物语言风格",
    "formality": "正式/口语程度",
    "lexicon": "词汇偏好",
    "punctuation": "标点与排版",
    "forbidden": "禁用表达",
    "naming": "译名与文化处理",
    #: 配置里的"自定义要求"就是这一条 —— 自由文本，没结构，和风格同一条通道
    "custom": "自定义要求",
}

#: 作用域前缀 → 匹配哪一类标识。``global`` 对所有任务成立。
_SCOPE_KINDS: dict[str, str] = {
    "character": "speaker",
    "scene": "scene",
    "unit": "unit_id",
    "region": "region_id",
}

#: 越具体的作用域越靠前（渲染时先看到单元级要求，再看到全局基调）。
_SPECIFICITY: dict[str, int] = {
    "unit": 4,
    "character": 3,
    "scene": 3,
    "region": 2,
    "global": 0,
}


def scope_specificity(scope: str) -> int:
    kind = str(scope).partition(":")[0].strip().lower()
    return _SPECIFICITY.get(kind, 1)


@dataclass
class StyleEntry:
    """一条风格要求：**在什么作用域下、哪一维度、要求是什么**。"""

    aspect: str
    value: str
    scope: str = "global"
    note: str = ""
    priority: int = 50
    source: str = "human"

    def key(self) -> tuple[str, str]:
        return (self.aspect, self.scope)

    def matches(
        self,
        *,
        unit_id: str | None = None,
        speaker: str | None = None,
        scene: str | None = None,
        region_id: str | None = None,
    ) -> bool:
        kind, _, payload = self.scope.partition(":")
        kind = kind.strip().lower()
        if kind == "global" or not self.scope:
            return True
        field_name = _SCOPE_KINDS.get(kind)
        if field_name is None:
            # 不认识的作用域前缀：不猜它能匹配谁，也就不注入。
            return False
        target = payload.strip()
        if not target:
            return False
        actual = {
            "unit_id": unit_id,
            "speaker": speaker,
            "scene": scene,
            "region_id": region_id,
        }[field_name]
        return actual is not None and str(actual) == target

    def to_dict(self) -> dict[str, Any]:
        return {
            "aspect": self.aspect,
            "value": self.value,
            "scope": self.scope,
            "note": self.note,
            "priority": self.priority,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StyleEntry":
        return cls(
            aspect=str(data.get("aspect", "")),
            value=str(data.get("value", "")),
            scope=str(data.get("scope", "global") or "global"),
            note=str(data.get("note", "")),
            priority=int(data.get("priority") or 0),
            source=str(data.get("source", "human")),
        )

    def render(self) -> str:
        label = ASPECT_LABELS.get(self.aspect, self.aspect)
        line = f"- {label}：{self.value}"
        if self.note:
            line += f"（{self.note}）"
        if self.scope and self.scope != "global":
            line += f" [作用域 {self.scope}]"
        return line


class StyleGuide:
    """风格指南文件：读写 ``style.jsonl``，按作用域解析出本次任务适用的条目。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._cache: tuple[tuple[int, int] | None, list[StyleEntry], list[Issue]] | None = None
        self._lock = threading.RLock()

    # ---- 读写 ---------------------------------------------------------------

    def _stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _raw_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def _parse(self, lines: list[str]) -> tuple[list[StyleEntry], list[Issue]]:
        entries: list[StyleEntry] = []
        problems: list[Issue] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(
                    Issue(
                        code="style_unparsable",
                        message=f"第 {index} 行不是合法 JSON：{exc.msg}",
                        ref=f"{self.path}:{index}",
                        detail={"line": line},
                    )
                )
                continue
            if not isinstance(payload, dict) or "aspect" not in payload:
                problems.append(
                    Issue(
                        code="style_missing_field",
                        message=f"第 {index} 行缺少 aspect 字段",
                        ref=f"{self.path}:{index}",
                        detail={"line": line},
                    )
                )
                continue
            entry = StyleEntry.from_dict(payload)
            if not entry.value.strip():
                problems.append(
                    Issue(
                        code="style_empty_value",
                        message=f"第 {index} 行的风格要求是空的",
                        ref=f"{self.path}:{index}",
                        detail=entry.to_dict(),
                    )
                )
            entries.append(entry)
        return entries, problems

    def _parsed(self) -> tuple[list[StyleEntry], list[Issue]]:
        stamp = self._stamp()
        cached = self._cache
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2]
        with self._lock:
            cached = self._cache
            if cached is not None and cached[0] == stamp:
                return cached[1], cached[2]
            entries, problems = self._parse(self._raw_lines())
            self._cache = (stamp, entries, problems)
            return entries, problems

    def ensure(self) -> "StyleGuide":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        return self

    def entries(self) -> list[StyleEntry]:
        """读出全部风格要求；非法行被跳过，不抛异常。"""
        return self._parsed()[0]

    def validate(self) -> list[Issue]:
        return self._parsed()[1]

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(lines)
        self.path.write_text(body + "\n" if body else "", encoding="utf-8")
        with self._lock:
            self._cache = None

    # ---- 变更 ---------------------------------------------------------------

    def add(self, entry: StyleEntry) -> StyleEntry:
        """新增或按 ``(aspect, scope)`` 覆盖。解析不了的行原样保留。"""
        self.ensure()
        lines = self._raw_lines()
        payload = json.dumps(entry.to_dict(), ensure_ascii=False)
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(existing, dict):
                continue
            other = StyleEntry.from_dict(existing)
            if other.key() == entry.key():
                lines[index] = payload
                self._write(lines)
                return entry
        lines.append(payload)
        self._write(lines)
        return entry

    def remove(self, aspect: str, scope: str = "global") -> bool:
        lines = self._raw_lines()
        kept: list[str] = []
        removed = False
        for line in lines:
            if not line.strip():
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if isinstance(existing, dict) and StyleEntry.from_dict(existing).key() == (
                aspect,
                scope,
            ):
                removed = True
                continue
            kept.append(line)
        if removed:
            self._write(kept)
        return removed

    # ---- 查询 ---------------------------------------------------------------

    def for_task(
        self,
        *,
        unit_id: str | None = None,
        speaker: str | None = None,
        scene: str | None = None,
        region_id: str | None = None,
    ) -> list[StyleEntry]:
        """这次任务适用的风格要求，具体作用域在前、全局在后。"""
        picked = [
            entry
            for entry in self.entries()
            if entry.value.strip()
            and entry.matches(
                unit_id=unit_id, speaker=speaker, scene=scene, region_id=region_id
            )
        ]
        picked.sort(
            key=lambda e: (-scope_specificity(e.scope), -e.priority, e.aspect, e.scope)
        )
        return picked

    @staticmethod
    def render(entries: Iterable[StyleEntry]) -> str:
        items = [entry for entry in entries if entry.value.strip()]
        if not items:
            return ""
        return "【风格要求】\n" + "\n".join(entry.render() for entry in items)

    def summary(self) -> dict[str, Any]:
        return {"path": str(self.path), "entries": len(self.entries())}
