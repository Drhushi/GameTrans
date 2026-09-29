"""适配协议版本 —— 内核与适配包之间那份"不变的东西"的版本号。

适配包可以独立发布、独立升级，内核也一样。两边各有各的节奏时，唯一能回答
"这个包还能不能被这个内核装"的东西就是这个**协议版本区间**。

判不过就**拒装**（见 :mod:`gametrans.engines.loader`），内核不会为旧协议铺兼容
垫片 —— 垫片一写，"内核不认识任何具体引擎"这条边界就从后面被掏空了。

区间语法刻意做到最小（纯标准库，不引第三方）：

* ``>=1`` / ``>1`` / ``<=2`` / ``<2`` / ``=1`` / ``==1``；
* 逗号分隔表示"并且"，例如 ``>=1,<2``；
* ``*`` 或空串表示不限制。

写法看不懂一律**报错**，不猜 —— 猜出来的兼容性判据比没有判据更危险。
"""

from __future__ import annotations

from dataclasses import dataclass

from gametrans.errors import EngineError

__all__ = [
    "PROTOCOL_VERSION",
    "Comparator",
    "ProtocolSpecError",
    "parse_spec",
    "matches",
    "format_spec",
]

#: 当前内核实现的适配协议版本。
#:
#: 什么时候该加一：适配包的装载方式、声明文件的必填字段、或内核交给适配包的
#: 契约形状发生**不兼容**变化时。加一意味着旧适配包会被拒装并说清原因 ——
#: 这是有意的：宁可让它明确装不上，也不要让它在新内核上悄悄做错事。
PROTOCOL_VERSION = 1

#: 比较符 —— 长符号必须排在短符号前面（``>=`` 先于 ``>``），否则会被切错。
_OPERATORS = (">=", "<=", "==", ">", "<", "=")


class ProtocolSpecError(EngineError):
    """协议版本区间写法看不懂。"""


@dataclass(frozen=True)
class Comparator:
    op: str
    version: int

    def accepts(self, version: int) -> bool:
        if self.op == ">=":
            return version >= self.version
        if self.op == "<=":
            return version <= self.version
        if self.op == ">":
            return version > self.version
        if self.op == "<":
            return version < self.version
        return version == self.version  # "=" 与 "=="

    def __str__(self) -> str:
        return f"{self.op}{self.version}"


def parse_spec(spec: str) -> tuple[Comparator, ...]:
    """把区间写法解析成比较符序列。空串与 ``*`` 解析成"不限制"（空序列）。"""
    text = str(spec or "").strip()
    if text in ("", "*"):
        return ()
    comparators: list[Comparator] = []
    for chunk in text.split(","):
        token = chunk.strip()
        if not token:
            raise ProtocolSpecError(
                f"协议区间里有空的一段：{spec!r}",
                hint="逗号分隔的每一段都要是一个比较式，例如 `>=1,<2`。",
            )
        for op in _OPERATORS:
            if not token.startswith(op):
                continue
            number = token[len(op) :].strip()
            if not number.isdigit():
                raise ProtocolSpecError(
                    f"协议区间里的版本号不是一个整数：{token!r}",
                    hint="版本号只能是整数，例如 `>=1,<2`；写法看不懂时内核不猜。",
                )
            comparators.append(Comparator(op, int(number)))
            break
        else:
            raise ProtocolSpecError(
                f"看不懂的协议区间片段：{token!r}",
                hint="可用写法：>=1 / >1 / <=2 / <2 / =1 / ==1，逗号表示并且，`*` 表示不限制。",
            )
    return tuple(comparators)


def matches(spec: str, version: int = PROTOCOL_VERSION) -> bool:
    """这个区间收不收 ``version``。写法看不懂时抛 :class:`ProtocolSpecError`。"""
    return all(comparator.accepts(version) for comparator in parse_spec(spec))


def format_spec(spec: str) -> str:
    """把区间写法渲染成人读的形式（报错时用）。"""
    comparators = parse_spec(spec)
    if not comparators:
        return "不限"
    return " 且 ".join(str(comparator) for comparator in comparators)
