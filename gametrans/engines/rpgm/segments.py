"""RPG Maker MV 文本的 Segment 切分（Engine Adapter 侧）。

**为什么在适配层**：``\\C[6]`` 是颜色、``\\V[13]`` 是变量、``<br>`` 是断行、
``%1`` 是消息模板占位 —— 全是这个引擎的语法，内核只认识 ``Segment.kind`` 这个抽象枚举。

判据按**真靶实测清单**落定（探针 ``corpora/probe-rpgm/codes.py``，23,908 个字符串里
1,045 个含标记）：

    \\C[n] 498、\\n 423、<br> 305、\\PX[n] 68、%1..%9 64、\\V[n] 47、
    \\\\. 9、\\\\! 6、\\\\^ 2、\\FS[n] 1、\\G 1、\\\\ 5，
    另有 YEP_MessageCore 的 \\fn<字体名> / \\fi / \\fb

（``\\fb`` 不在第一版清单里，是**跑真靶 scan 时报告出来的**；补进清单的经过写在
:data:`MEASURED_CONTROL_CODES` 上。）

**没量到的控制码怎么处理**：不"猜进"清单（口径：先按实测清单落定），但**也不许被当成
正文**。它们按引擎的通用形状 ``\\X`` / ``\\X[n]`` / ``\\X<...>`` 归为受保护结构，
并由 :func:`unclassified_control_codes` 单独点名报出来 —— 于是"遇到再补"是看得见的，
而不是被静默翻坏。

切出来的段满足一条硬性质：**拼回去必须逐字节等于原文**（校验器与一致性检查都依赖它）。
"""

from __future__ import annotations

import re

from gametrans.core.models import Segment, SegmentKind

__all__ = [
    "MEASURED_CONTROL_CODES",
    "scan_structure",
    "segmentize",
    "unclassified_control_codes",
]

#: 真靶实测出现过的控制码（大写归一后）。清单之外的按"未分类"处理并报出来。
#:
#: ``fb`` 是**第一次跑真靶 scan 才发现的**：报告里冒出 19 个幽灵码
#: （``FBIF`` / ``FBMAYBE`` / ``FBSHE`` …），追下去全是 ``\fb`` 粗体开关被贪婪匹配
#: 连下一个单词的首字母一起吞了（``\fbIf outfitMod.js…``）。它和 ``\fi`` 同一族，
#: 于是按"遇到再补"补进清单，并补上回归用例。
#:
#: ``.`` / ``!`` / ``^`` 三条是**第二次跑真靶**才发现的：探针本来就数到了它们
#: （``\.`` 9、``\!`` 6、``\^`` 2），但第一版清单漏抄了 —— 于是报告把它们报成
#: "没验证过的码"。清单与实现不一致，比漏报更糟：用户会照着清单去补本来就在的东西。
#: ``tests/test_rpgm_segments.py::MeasuredCodesTest`` 现在逐条钉住这个清单。
MEASURED_CONTROL_CODES: frozenset[str] = frozenset(
    {"C", "V", "PX", "FS", "G", "fn", "fi", "fr", "fb", ".", "!", "^"}
)

#: 会插值出**内容**的码（变量 / 角色名 / 队伍成员名 / 消息模板占位）。
_VARIABLE_CODES = frozenset({"V", "N", "P"})

#: 单字符控制码：颜色以外的显示控制。``\G`` 是货币单位，``\$`` 是金币窗口，
#: ``\{`` / ``\}`` 是字号增减，``\_`` 是半角空格，其余是停顿与瞬显。
_SINGLE_CHAR_CODES = frozenset("G${}._|!><^")

#: 带 ``[数字]`` 参数的长码。大小写不敏感 —— 真靶里写的是 ``\\fs[40]``（小写）。
_LONG_CODES = "N|P|V|C|I|FS|PX|PY|AF|AC|KR"

_TOKEN = re.compile(
    r"(?P<escape>\\\\)"                                  # \\ 字面反斜杠
    # 带参数的长码必须排在 `\n`（断行）**前面**：`\N[3]` 是角色名插值，
    # 顺序反了会被切成 `\N` + 字面量 `[3]`。
    r"|(?P<long>\\(?:" + _LONG_CODES + r")\[\d+\])"      # \C[6] \V[13] \N[3] \FS[40] ...
    r"|(?P<line_break>\\n)"                              # \n 引擎断行（反斜杠 + n）
    r"|(?P<font>\\fn<[^>\n]*>)"                          # \fn<字体名>
    r"|(?P<toggle>\\f[irb])"                             # \fi 斜体 / \fr 复位 / \fb 粗体
    r"|(?P<single>\\[G${}._|!><^])"                      # 单字符控制码
    # 没量到的：**必须保守**。带参数的靠 [ 或 < 定界，可以长；裸码最多两个字母 ——
    # 否则 `\fbreally` 会把 really 的首字母吞进码名，把正文从可译文本里挖掉。
    r"|(?P<unknown>"
    r"\\(?:[A-Za-z]+(?=\[)|[A-Za-z]+(?=<)|[A-Za-z]{1,2})"
    r"(?:\[\d+\]|<[^>\n]*>)?"
    r")"
    r"|(?P<br><br\s*/?>)"                                # <br> 断行
    r"|(?P<percent>%[1-9])"                              # 消息模板占位
    r"|(?P<newline>\n)",                                 # 401 行之间的真换行
    re.IGNORECASE,
)

#: 只受保护、但**不携带"控制码"**的结构标记。它们不是码，不该进"待补清单"。
_STRUCTURAL_GROUPS = frozenset({"escape", "line_break", "br", "newline"})

_NAME = re.compile(r"\\([A-Za-z]+)")


def _classify(name: str, token: str) -> tuple[str, dict[str, str]]:
    if name == "escape":
        return SegmentKind.ESCAPE.value, {"form": "backslash"}
    if name == "newline":
        return SegmentKind.ESCAPE.value, {"form": "newline"}
    if name == "line_break":
        return SegmentKind.ESCAPE.value, {"form": "line_break", "code": "\\n"}
    if name == "br":
        return SegmentKind.ESCAPE.value, {"form": "line_break", "code": "<br>"}
    if name == "percent":
        return SegmentKind.VARIABLE.value, {"form": "message_argument", "name": token[1:]}
    # \V[n] / \N[n] / \P[n] 插值出内容；其余是显示控制
    code = _code_of(token)
    if code in _VARIABLE_CODES:
        return SegmentKind.VARIABLE.value, {"code": code, "raw": token}
    return SegmentKind.CONTROL.value, {"code": code, "raw": token}


def _code_of(token: str) -> str:
    """从标记里取出码名（``\\C[6]`` → ``C``，``\\fn<x>`` → ``fn``，``\\{`` → ``{``）。"""
    match = _NAME.match(token)
    if not match:
        return token[1:] if len(token) > 1 else token
    raw = match.group(1)
    # 小写双字母开关（\fn / \fi / \fr / \fb）与真靶一致地保留原样；
    # 其余大写归一：真靶里 \fs[40] 与 \FS[40] 是同一个东西
    if raw in ("fn", "fi", "fr", "fb"):
        return raw
    return raw.upper()


def segmentize(text: str) -> list[Segment]:
    """把 RPGM 文本切成 Segment 序列；``"".join(s.value) == text`` 恒成立。"""
    segments: list[Segment] = []
    position = 0
    for match in _TOKEN.finditer(text):
        if match.start() > position:
            segments.append(Segment(SegmentKind.TEXT.value, text[position : match.start()]))
        token = match.group(0)
        group = match.lastgroup or ""
        kind, metadata = _classify(group, token)
        if group not in _STRUCTURAL_GROUPS and _code_of(token) not in MEASURED_CONTROL_CODES:
            metadata["unclassified"] = "true"
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


def unclassified_control_codes(text: str) -> list[str]:
    """这段文本里"引擎认得、但本靶没实测过"的控制码名（去重、按出现顺序）。

    两件事刻意分开：

    * **引擎认得的**一律受保护 —— 不会被当成正文翻坏（按引擎自己的控制码表识别）；
    * **实测验证过的**（:data:`MEASURED_CONTROL_CODES`）才算"落定"。
      其余的点名报出来，于是"遇到再补"是看得见的、可执行的，而不是靠人想起来。

    结构标记（``\\\\`` / 真换行 / ``<br>`` / ``\\n``）不是控制码，不进这个清单。
    """
    found: list[str] = []
    for match in _TOKEN.finditer(text):
        group = match.lastgroup or ""
        if group in _STRUCTURAL_GROUPS:
            continue
        code = _code_of(match.group(0))
        if code not in MEASURED_CONTROL_CODES and code not in found:
            found.append(code)
    return found
