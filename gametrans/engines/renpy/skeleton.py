"""官方骨架解析 —— 把引擎生成的 ``tl/<lang>/`` 骨架读成槽位清单。

## 定位

这是**适配层**的一部分：它认识具体引擎的文件格式，内核不认识。
它只做一件事 —— **读引擎已经算好的结果**：

* 不自己算 translate 块 id（那是引擎的哈希规则）；
* 不自己解析游戏源码（那是引擎的解析器）；
* 不从零判断"什么算可译文本"（骨架已经筛过了）。

骨架是用户跑一条官方命令就能得到的产物，每条带着 **id + 源文件 + 行号 + 原文**。
读完交给 :class:`~gametrans.core.slots.SlotSet`，再交给单位组装与绑定 ——
于是"引擎给槽位 → 我们组装 → 译文填回槽位"这条流向才有源头。

## 两种形态

```
# game/script.rpy:30
translate zh_CN act1_220d1b9a:
    # "Girl" "..."
    "Girl" "..."                      ← identified：有 id、按 id 锚定

translate zh_CN strings:
    # game/script.rpy:154
    old "Introduce yourself."         ← keyed：没有 id、按原文锚定
    new "Introduce yourself."
```

## 一条容易忽略的细节

块内语句写的是 ``"Girl" "..."`` —— **说话人在引号外面**，是角色不是台词。
骨架的注释行``# "Girl" "..."`` 把原样记着，所以说话人从注释里取（它是上下文），
原文取语句里的那个字面量。我们的自研解析器当初就是在这里漏掉了整类内容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from gametrans.core.slots import Slot, SlotKeying, SlotSet

__all__ = [
    "SkeletonParseError",
    "ParsedSkeleton",
    "parse_skeleton_text",
    "parse_skeleton_dir",
]


class SkeletonParseError(Exception):
    """骨架本身坏了（例如引号没闭合）—— 硬错误优于猜一个截断的原文。"""


#: ``# game/script.rpy:30`` —— 引擎写在每个块/字符串上方的位置提示
LOCATOR_RE = re.compile(r"^#\s+(?P<file>[^\s:]+):(?P<line>\d+)\s*$")
#: ``translate zh_CN <id>:`` —— 一个 translate 块
BLOCK_RE = re.compile(r"^translate\s+(?P<lang>[\w.\-]+)\s+(?P<id>[\w.\-]+)\s*:\s*$")
#: ``translate zh_CN strings:`` —— 字符串表块
STRINGS_BLOCK_RE = re.compile(r"^translate\s+[\w.\-]+\s+strings\s*:\s*$")
#: 语言包自带的**接线块**（不是内容）：``translate zh_CN python:`` / ``translate zh_CN style x is y:``
#: —— 译者给自己这门语言换字体、改样式用的，没有定位注释，也不该被当成待译文本。
WIRING_BLOCK_RE = re.compile(r"^translate\s+[\w.\-]+\s+(?:python|style)\b.*:\s*$")
#: ``old "..."`` —— 字符串表的原文一侧
OLD_RE = re.compile(r"^old\s+(?P<quote>.*)$")
#: 注释掉的语句，例如 ``# "Girl" "..."``
COMMENTED_STATEMENT_RE = re.compile(r"^#\s*(?P<statement>.+?)\s*$")
#: ``if <条件>:`` —— 条件本身不解析，只要求以冒号收尾
#: 尾注判据**只有一份**，住在 `parser` 里（源码侧与骨架侧共用同一套语法理解）。
from gametrans.engines.renpy.parser import IF_TRAILER_RE, is_trailer  # noqa: E402,F401


#: 说话人前缀：Ren'Py 的角色标识符，**允许下划线**（``nvl_narrator`` / ``mc_nvl``
#: 都是真实游戏里的合法角色名），**并允许若干姿态属性**（``her normal`` /
#: ``him nude surprised`` —— 属性是立绘表情，不是身份）。只排除明显不是标识符的东西。
SPEAKER_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:\s+[A-Za-z_][A-Za-z0-9_]*)*$")
#: 块体里出现的 **非 say 语句**开头的关键字。骨架是引擎生成的，块体里基本只有 say，
#: 但 screen / menu 这些语句里也有字符串字面量 —— 认出来就别当台词抽走。
NON_SAY_KEYWORDS = frozenset(
    {
        "screen", "menu", "style", "define", "default", "python", "init", "label",
        "show", "hide", "scene", "play", "stop", "queue", "voice", "image",
        "transform", "translate", "call", "jump", "return", "if", "elif", "else",
        "while", "for", "pass", "set", "window", "nvl", "with", "text", "textbutton",
        "add", "use", "on", "at", "layout", "frame", "vbox", "hbox", "grid", "side",
        "key", "bar", "imagebutton", "button", "viewport", "fixed", "null",
    }
)


def is_speaker_prefix(prefix: str) -> bool:
    """引号前面那截是不是"说话人［+ 姿态属性］"。

    **解析侧与写回侧共用这一份判据**（``_split_say`` 与
    ``skeleton_writer._block_fill_span``）。各写一份的后果实测过两次：先是尾注正则
    （R50），后是这份前缀判据 —— 扫描认得带属性 say 的块、写回却拒绝填它们，
    产物词条数与 ``lint`` 都正常，但一半对白还是原文（R52）。

    守卫看**首个 token**：``if flag "…"`` 这种前缀在字形上就是"角色＋属性"，
    光靠标识符判据挡不住，只能靠关键字挡。
    """
    if not prefix:
        return False
    if not SPEAKER_PREFIX_RE.match(prefix):
        return False
    return prefix.split()[0] not in NON_SAY_KEYWORDS


def _string_literals(text: str) -> list[str]:
    """取出文本里全部双引号字面量的内容（不含引号、不解除转义）。

    用了极简扫描而不是正则：``(?:[^"\\\\]|\\\\.)*`` 这种回溯正则在长行上容易出问题，
    而这段逻辑一眼能看明白。
    """
    return [value for _start, _end, value in _scan_literals(text)]


def _scan_literals(text: str) -> list[tuple[int, int, str]]:
    """依次找出全部双引号字面量的 ``(起, 止, 内容)``，尊重转义。

    骨架里的 say 语句可能有**两个**引号片段（说话人本身也是字面量：
    ``"Girl" "台词"``），所以需要"依次取出"而不是"只取第一个"。

    三引号（``\"\"\"...\"\"\"``）整段跳过：那是 Python 块里的文档字符串，不是台词。
    不认它的话，``\"\"\"文本`` 会被读成"一个空串 + 一个没闭合的串"。
    """
    found: list[tuple[int, int, str]] = []
    index = 0
    while index < len(text):
        start = text.find('"', index)
        if start < 0:
            break
        if text.startswith('"""', start):
            end = text.find('"""', start + 3)
            if end < 0:
                raise SkeletonParseError(
                    f"三引号没有闭合：{text[start:start + 40]!r}… —— 骨架数据坏了，不猜原文。"
                )
            index = end + 3
            continue
        cursor = start + 1
        buffer: list[str] = []
        closed = False
        while cursor < len(text):
            char = text[cursor]
            if char == "\\" and cursor + 1 < len(text):
                buffer.append(char)
                buffer.append(text[cursor + 1])
                cursor += 2
                continue
            if char == '"':
                closed = True
                cursor += 1
                break
            buffer.append(char)
            cursor += 1
        if not closed:
            raise SkeletonParseError(
                f"引号没有闭合：{text[start:start + 40]!r}… —— 骨架数据坏了，不猜原文。"
            )
        found.append((start, cursor, "".join(buffer)))
        index = cursor
    return found


def _is_closed_statement(text: str) -> bool:
    """这段文本的引号闭合了吗（用来判断台词是否还没写完、要接下一行）。"""
    try:
        _scan_literals(text)
    except SkeletonParseError:
        return False
    return True


def _split_say(statement: str) -> tuple[str, str, str, str] | None:
    """把一句 say 拆成 ``(说话人, 台词, 尾注, 说话人形态)``；不是 say 形态返回 ``None``。

    第 4 项是 ``"literal"``（``"Girl" "台词"``：**说话人本身就是要翻的字面量**）或
    ``"identifier"``（``e "台词"``：说话人是脚本变量）。两者必须分开 —— 名字框里那串字
    既不是变量、也不在字符串表里，是要交付的文本（见 :meth:`_Builder.add_block`）。

    真实骨架里出现过的形态：

    ==========================  ==========  ==============
    语句                        说话人      台词
    ==========================  ==========  ==============
    ``"The station was empty"``  ``""``      ``The station was empty``
    ``e "You're late again."``   ``e``       ``You're late again.``
    ``"Girl" "Okay, he said"``   ``"Girl"``  ``Okay, he said``
    ``e "..." with dissolve``    ``e``       ``...``
    ==========================  ==========  ==============

    认不出就返回 ``None`` —— 宁可报出来，也不要把一段代码当台词抽走。
    """
    literals = _scan_literals(statement)
    if not literals:
        return None

    first_start, first_end, first_value = literals[0]

    # 两个引号片段：说话人本身就是字面量（``"Girl" "台词"``）
    if len(literals) >= 2:
        second_start, second_end, second_value = literals[1]
        if statement[:first_start].strip():
            return None  # 前面还有东西，不是这种形态
        trailing = statement[second_end:].strip()
        if trailing and not is_trailer(trailing):
            return None
        return first_value, second_value, trailing, "literal"

    # 一个引号片段：前面是说话人标识符（或什么都不给 = 旁白）。
    # 判据只有一份，写回侧调的也是它（见 is_speaker_prefix 的说明）。
    prefix = statement[:first_start].strip()
    trailing = statement[first_end:].strip()
    if prefix and not is_speaker_prefix(prefix):
        return None
    if trailing and not is_trailer(trailing):
        return None
    return prefix, first_value, trailing, "identifier"


def _original_of(
    comment: str,
    raw_speaker: str,
    trailer: str,
    *,
    allow_speaker_change: bool,
) -> tuple[str, str] | None:
    """从块体上方的原文注释里取 ``(原文, 原文里的说话人)``；取不到就返回 ``None``。

    **尾注必须一致** —— 不一致说明注释与块体不是同一条语句，宁可留空也不张冠李戴。

    ``allow_speaker_change`` 给**单语句块**用：译者会把说话人换掉（真靶上把
    ``s "…"`` 改成 ``"???" "…"``，神秘角色不给名字，见 R47）。那不是"注释对不上"，
    是作者的写法 —— 不放宽的话，这类块在内容里会变成"原文为空"，永远翻不了。
    多语句块里注释可能属于块内别的语句，所以那时仍然要求说话人也一致。
    """
    original = _split_say(comment) if comment else None
    if original is None or original[2] != trailer:
        return None
    if not allow_speaker_change and original[0].strip() != raw_speaker.strip():
        return None
    return original[1], original[0].strip()


def _speaker_of(raw_speaker: str) -> str | None:
    """把 :func:`_split_say` 给的"说话人原文"归一成名字。

    ``e``                 → ``e``
    ``"Girl"``            → ``Girl``（带引号的说话人，引号已由调用方剥掉）
    ``her normal``        → ``her``（姿态属性不是身份）
    ``him nude surprised``→ ``him``
    ``""``                → ``None``（旁白，没有说话人）
    """
    speaker = (raw_speaker or "").strip().strip('"').strip()
    if not speaker:
        return None
    # 角色后面可能跟姿态属性（``her normal`` / ``him nude surprised``）：
    # 说话人是**角色本身**。不剥掉属性的话，同一个角色的不同表情会被下游当成不同说话人。
    return speaker.split()[0]


@dataclass
class ParsedSkeleton:
    """一次骨架解析的结果。"""

    slots: list[Slot] = field(default_factory=list)
    #: 认不出来 / 不合常理的地方，逐条说明 —— 不猜，也不静默丢掉
    warnings: list[str] = field(default_factory=list)

    def by_engine_id(self, engine_id: str) -> Slot | None:
        for slot in self.slots:
            if slot.engine_id == engine_id:
                return slot
        return None

    def by_source(self, source: str) -> Slot | None:
        """按原文找 keyed 槽位（字符串表按原文定键，这是它的自然查询方式）。"""
        for slot in self.slots:
            if slot.keying is SlotKeying.KEYED and slot.source == source:
                return slot
        return None

    def to_slot_set(self) -> SlotSet:
        slots = SlotSet()
        for slot in self.slots:
            slots.add(slot)
        return slots

    def summary(self) -> dict[str, object]:
        identified = sum(1 for slot in self.slots if slot.keying is SlotKeying.IDENTIFIED)
        keyed = sum(1 for slot in self.slots if slot.keying is SlotKeying.KEYED)
        return {
            "slots": len(self.slots),
            "identified": identified,
            "keyed": keyed,
            "occurrences_total": sum(slot.occurrences for slot in self.slots),
            "warnings": len(self.warnings),
        }


class _Builder:
    """把解析结果聚起来。

    槽位先收在列表里（keyed 的合并交给 :class:`SlotSet` 做 —— 那是它已经测过的职责），
    这样"合并"这件事只有一处实现。
    """

    def __init__(self) -> None:
        self.slots: list[Slot] = []
        self.warnings: list[str] = []

    def add_block(
        self,
        identifier: str,
        file: str,
        line: int,
        statement: str,
        comment: str,
        *,
        allow_speaker_change: bool = True,
    ) -> None:
        # 引号没闭合会在这一步抛硬错误（_scan_literals 里）—— 数据坏了就不猜原文
        split = _split_say(statement)
        if split is None:
            self.warnings.append(
                f"块 {identifier} 的语句不是 say 形态，已跳过：{statement!r}"
            )
            return
        raw_speaker, current, trailer, speaker_form = split

        # 引擎在块体上方留一行原文提示（``# e "原文"``），那是**原文**的权威记录：
        # 这一块填过译文之后，块体里的字面量已经是译文，而注释始终是原文。
        # 条件收得很紧 —— 只有"说话人与尾注都与块体一致、只有字面量不同"才采信，
        # 这样多语句块里注释与语句对不上时不会张冠李戴。
        source = current
        original_speaker = ""
        taken = _original_of(
            comment, raw_speaker, trailer, allow_speaker_change=allow_speaker_change
        )
        if taken is not None:
            source, original_speaker = taken

        # 说话人优先从注释取：注释是原样记录，说话人带引号时更可靠。
        comment_literals = _string_literals(comment) if comment else []
        if len(comment_literals) >= 2:
            speaker: str | None = comment_literals[0]
        else:
            speaker = _speaker_of(raw_speaker)

        metadata: dict[str, object] = {"statement": statement}
        if speaker:
            metadata["speaker"] = speaker
        # 引号说话人（``"Girl" "台词"``）：名字本身也是要翻的文本，单独收一条按原文定键
        # 的槽位。判定看**形态**（`_split_say` 已经把两种情况分开了）—— 只靠"值长什么样"
        # 分不出来（`e` 和 `"Girl"` 在值上都是普通字符串）。名字取注释里的原样字面量：
        # 块体里那一份可能已经被填过译文。
        if speaker_form == "literal":
            name = comment_literals[0] if len(comment_literals) >= 2 else _speaker_of(raw_speaker)
            if name:
                metadata["speaker_is_literal"] = True
                self.add_speaker_name(name, file, line)
        if trailer:
            metadata["trailer"] = trailer
        if source != current:
            metadata["filled_literal"] = current
        # 译者改过说话人（真靶上把 `s` 换成 `"???"` 这种）—— 如实记一笔，
        # 免得下游以为"原文的说话人"就是块体里那个
        if original_speaker and original_speaker != raw_speaker.strip().strip('"').strip():
            metadata["speaker_original"] = original_speaker

        self.slots.append(
            Slot(
                keying=SlotKeying.IDENTIFIED,
                engine_id=identifier,
                source=source,
                file=file,
                line=line,
                # 引擎自己的形态类名：块就是它给对话用的那种（内核按自己的词汇归一）
                node_class="TranslateSay",
                metadata=metadata,
            )
        )

    def add_keyed(self, source: str, file: str, line: int) -> None:
        self.slots.append(
            Slot(
                keying=SlotKeying.KEYED,
                source=source,
                file=file,
                line=line,
                # 字符串表条目在引擎里就是 TranslateString；它不带独立 id，键是原文
                node_class="TranslateString",
            )
        )

    def add_speaker_name(self, name: str, file: str, line: int) -> None:
        """引号说话人的**名字**也是一条要翻的文本。

        引擎的清单里没有它（实测：`"Officer" "…"` 这类 829 处、名字种类十几个，
        字符串表和翻译块里一条都不占），它就写在块体的第一个字面量里。所以这里按
        **原文定键**收成一条槽位：同一名字只翻一次，写回时填回每一处说话人位置。

        ``metadata["anchor"]`` 标出它的锚点：``"block"`` = 锚在骨架块上（**没有**
        源码结构坐标），``"structure"``（缺省）= 锚在源码结构上。分组策略按这个字段
        分流，不必认识任何引擎的形态类名 —— 格式知识留在适配层。
        """
        self.slots.append(
            Slot(
                keying=SlotKeying.KEYED,
                source=name,
                file=file,
                line=line,
                node_class="TranslateSpeaker",
                metadata={"anchor": "block"},
            )
        )

    def finish(self) -> None:
        """收尾：把"同原文合并"交给 :class:`SlotSet` 做。

        合并规则只有一处实现（那边已经测过），这里不重复造。
        """
        merged = SlotSet()
        for slot in self.slots:
            merged.add(slot)
        self.slots = merged.all()

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def parse_skeleton_text(text: str, *, language: str = "") -> ParsedSkeleton:
    """解析一份骨架文本。

    ``language`` 只用于报错措辞；不给也能解析（骨架里的语言字段只是描述）。
    """
    builder = _Builder()
    lines = text.splitlines()

    pending_locator: tuple[str, int] | None = None
    pending_comment = ""
    in_strings = False
    index = 0

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        index += 1

        if not stripped:
            continue

        locator = LOCATOR_RE.match(stripped)
        if locator:
            pending_locator = (locator.group("file"), int(locator.group("line")))
            continue

        # 字符串表里的一对：old ... 后面跟 new ...
        if in_strings:
            old = OLD_RE.match(stripped)
            if old:
                if pending_locator is None:
                    builder.warn(f"字符串表条目没有位置注释：{stripped!r}")
                    continue
                # 用字面量扫描器取内容 —— 字符串里可能有转义引号
                # （例如 ``old "Placed \n\"%s\"\n on clipboard"``），
                # 正则的 ``[^"]*`` 会在转义引号处断掉。
                values = _string_literals(old.group("quote"))
                if not values:
                    builder.warn(f"字符串表条目没有可读的字符串字面量：{stripped!r}")
                    continue
                builder.add_keyed(values[0], pending_locator[0], pending_locator[1])
                continue
            if stripped.startswith("new "):
                continue  # new 是译文位，骨架里等于原文，不需要单独成槽位
            if stripped.startswith("#"):
                continue

        commented = COMMENTED_STATEMENT_RE.match(stripped)
        if commented:
            candidate = commented.group("statement")
            # 注释掉的**语句**（块内原文提示）留着，解析块时用
            if not commented.group(1).startswith("game/") and not BLOCK_RE.match(candidate):
                pending_comment = candidate
            continue

        if STRINGS_BLOCK_RE.match(stripped):
            in_strings = True
            pending_locator = None
            continue

        if WIRING_BLOCK_RE.match(stripped):
            # 接线块（换字体 / 改样式）不是内容：静默跳过，别报成"认不出的东西"
            in_strings = False
            pending_locator = None
            pending_comment = ""
            continue

        block = BLOCK_RE.match(stripped)
        if block:
            in_strings = False
            identifier = block.group("id")
            if pending_locator is None:
                builder.warn(f"块 {identifier} 上方没有 ``# 文件:行号`` 位置注释，无法锚定")
                pending_locator = None
                pending_comment = ""
                continue

            block_locator = pending_locator
            pending_locator = None
            pending_comment = ""

            # 收集块体：直到下一个顶层结构
            body: list[str] = []
            cursor = index
            while cursor < len(lines):
                candidate = lines[cursor]
                candidate_stripped = candidate.strip()
                if candidate_stripped and not candidate[:1].isspace():
                    break
                body.append(candidate)
                cursor += 1
            index = cursor

            statements: list[str] = []
            comment = ""
            #: 台词可以**跨行**（真靶工程的 `chinese/script3.rpy` 就有一句带换行的台词）：
            #: 引号没闭合时把后续行接上，而不是当成两条语句 —— 当成两条会在扫描字面量时
            #: 抛"引号没有闭合"，一个这样的块就能让整盘扫描失败（"稳定产出结构图"的头号敌人）。
            pending = ""
            for line in body:
                line_stripped = line.strip()
                if pending:
                    # 跨行字符串内部：空行也是内容，`#` 也不是注释
                    pending = f"{pending}\n{line if line_stripped else ''}"
                    if _is_closed_statement(pending):
                        statements.append(pending.strip())
                        pending = ""
                    continue
                if not line_stripped:
                    continue
                if line_stripped.startswith("#"):
                    # 块体里的注释就是"原文提示"，说话人（若有）也在里面
                    inline = COMMENTED_STATEMENT_RE.match(line_stripped)
                    if inline:
                        comment = inline.group("statement")
                    continue
                if not _is_closed_statement(line_stripped):
                    pending = line_stripped
                    continue
                statements.append(line_stripped)
            if pending:
                # 到块尾还没闭合：按它自己的口径处理（add_block 会记一条警告，不猜原文）
                statements.append(pending.strip())

            if not statements:
                builder.warn(f"块 {identifier} 是空的（没有语句）")
                continue

            # 引号没闭合这类坏数据仍然抛硬错误（"不猜原文"）—— 兜底在**文件粒度**：
            # `parse_skeleton_dir` 会记账后跳过那一份，不让整盘扫描失败。
            builder.add_block(
                identifier,
                block_locator[0],
                block_locator[1],
                statements[0],
                comment,
                # 单语句块里注释只可能属于这一条，说话人被译者改过也认；
                # 多语句块里注释可能属于块内别的语句，那时仍然要求说话人一致
                allow_speaker_change=len(statements) == 1,
            )
            continue

        # 剩下的都不认识 —— 如实报出来
        if stripped.startswith("translate"):
            builder.warn(f"认不出的 translate 形态：{stripped!r}")
        elif pending_locator is not None or in_strings:
            builder.warn(f"骨架里有认不出的行：{stripped!r}")

    builder.finish()
    return ParsedSkeleton(slots=builder.slots, warnings=builder.warnings)


def parse_skeleton_dir(tl_dir: Path, *, skip: Iterable[str] = ()) -> ParsedSkeleton:
    """解析一个 ``tl/<lang>/`` 目录下的全部骨架文件。

    ``skip`` 用来排除引擎自带内容（例如 ``common.rpy``，那是引擎内置字符串清单，
    **不是游戏内容**，见契约 C5）。
    """
    root = Path(tl_dir)
    if not root.is_dir():
        raise SkeletonParseError(
            f"骨架目录不存在或不是目录：{root}。"
            f"先跑一次官方工具生成骨架（例如官方启动器的 `translate <语言>` 命令）。"
        )

    skipped = set(skip)
    combined = _Builder()
    for path in sorted(root.glob("*.rpy")):
        if path.name in skipped:
            continue
        try:
            parsed = parse_skeleton_text(path.read_text(encoding="utf-8"))
        except SkeletonParseError as exc:
            # 一份文件读不出来（比如被截断）不该让整盘扫描失败：如实记账后跳过它。
            # 静默跳过是另一回事 —— 那会让"少了多少内容"没人知道。
            combined.warn(f"{path.name}: 整份读不出来，已跳过（{exc}）")
            continue
        combined.slots.extend(parsed.slots)
        combined.warnings.extend(f"{path.name}: {message}" for message in parsed.warnings)

    combined.finish()
    return ParsedSkeleton(slots=combined.slots, warnings=combined.warnings)
