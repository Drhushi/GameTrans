"""Ren'Py 文本的 Segment 切分（Engine Adapter 侧）。

**为什么这件工作在适配器里而不是内核里**：``[name]`` 是变量、``{w}`` 是控制码、
``[[`` 是转义 —— 这些全是引擎语法。核心只认识 ``Segment.kind`` 这个抽象枚举
（引擎标签、变量、控制码如何识别属于 Adapter 的职责）。

切出来的段满足一条硬性质：**拼回去必须逐字节等于原文**。校验器与一致性检查都
依赖这条性质（见 :mod:`gametrans.core.constraints`、:meth:`ProjectIR.conformance`）。
"""

from __future__ import annotations

import re

from gametrans.core.models import Segment, SegmentKind

__all__ = ["CONTROL_CODES", "is_control_code", "segmentize"]

#: Ren'Py 的流程/显示控制码 —— 它们不是标签，不需要配对，但必须原样保留。
CONTROL_CODES = frozenset(
    {
        "w",
        "nw",
        "p",
        "fast",
        "clear",
        "done",
        "nvl_clear",
        "space",
        "vspace",
        "image",
    }
)

_CONTROL_WITH_ARG = re.compile(r"^(w|p|space|vspace|image)(=.*)?$")

#: ``[[`` 是"字面量左方括号"的转义写法，必须排在 ``[...]`` 之前匹配。
_ESCAPE_BRACKETS = re.compile(r"\[\[|\{\{")
#: ``[name]`` / ``[name!t]`` / ``[name!q]`` —— 变量插值
_VARIABLE = re.compile(r"\[[^\[\]\n]*\]")
#: ``%(name)s`` / ``%s`` / ``%d`` —— 老式格式化参数
_FORMAT = re.compile(r"%\((\w+)\)[sdifr]|%[sdifr]")
#: ``{...}`` —— 文本标签或控制码，由内容再区分
_BRACES = re.compile(r"\{[^{}\n]*\}")
#: ``\`` + 任意字符 —— 没被解码器吃掉的转义残留
_BACKSLASH = re.compile(r"\\.", re.DOTALL)
#: 换行 —— 字符串里的真换行是"必须保留的结构标记"
_NEWLINE = re.compile(r"\n")

_TOKEN = re.compile(
    "|".join(
        pattern.pattern
        for pattern in (_ESCAPE_BRACKETS, _VARIABLE, _FORMAT, _BRACES, _BACKSLASH, _NEWLINE)
    )
)


def is_control_code(body: str) -> bool:
    """``{w}`` / ``{p=1.0}`` / ``{space=10}`` 是控制码，其余 ``{...}`` 是文本标签。"""
    name = body.strip("/").split("=", 1)[0].strip()
    if body.startswith("/"):
        return False
    if name in CONTROL_CODES:
        return True
    return _CONTROL_WITH_ARG.match(body.strip()) is not None


def _classify(token: str) -> tuple[str, dict[str, str]]:
    if token in ("[[", "{{"):
        return SegmentKind.ESCAPE.value, {"form": "literal_bracket"}
    if token == "\n":
        return SegmentKind.ESCAPE.value, {"form": "newline"}
    if token.startswith("\\"):
        return SegmentKind.ESCAPE.value, {"form": "backslash"}
    if token.startswith("["):
        inner = token[1:-1]
        tag = ""
        if "!" in inner:
            inner, _, tag = inner.partition("!")
        metadata = {"name": inner}
        if tag:
            metadata["flags"] = tag
        return SegmentKind.VARIABLE.value, metadata
    if token.startswith("%"):
        return SegmentKind.VARIABLE.value, {"form": "percent"}
    if token.startswith("{"):
        body = token[1:-1]
        if is_control_code(body):
            return SegmentKind.CONTROL.value, {"code": body}
        return SegmentKind.TAG.value, {"tag": body}
    return SegmentKind.PROTECTED.value, {}


def segmentize(text: str) -> list[Segment]:
    """把 Ren'Py 文本切成 Segment 序列；``"".join(s.value) == text`` 恒成立。"""
    segments: list[Segment] = []
    position = 0
    for match in _TOKEN.finditer(text):
        if match.start() > position:
            segments.append(
                Segment(SegmentKind.TEXT.value, text[position : match.start()])
            )
        token = match.group(0)
        kind, metadata = _classify(token)
        segments.append(Segment(kind, token, translatable=False, metadata=metadata))
        position = match.end()
    if position < len(text):
        segments.append(Segment(SegmentKind.TEXT.value, text[position:]))
    if not segments:
        segments.append(Segment(SegmentKind.TEXT.value, ""))
    return segments


def scan_structure(text: str) -> list[Segment]:
    """只返回受保护结构 —— 核心用它重建译文的"结构指纹"。"""
    return [s for s in segmentize(text) if s.is_protected]
