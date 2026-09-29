"""已有译文 —— 把一个**填过的** ``tl/<语言>/`` 读成"原文 → 译文"对。

## 它和 `skeleton.py` 的分工

`skeleton.py` 读的是**内容范围**：哪些文本要翻、它们的 id 是什么、原文是什么。
它刻意跳过 ``new``（译文位）——对"还没翻"的工程，那一侧本来就没有信息。

这里读的是**资产**：译者已经落下的字。同一套语法、同一个文件，但取的是另一侧：

* 对白块：块体里的字面量是**译文**，块体上方那行注释（``# e "原文"``）才是原文；
* 字符串表：``old`` / ``new`` 就是一对。

两条通道共用同一批扫描器（``_split_say`` / ``_string_literals``），不另造一套语法理解。

## 三条口径

| 情况 | 处置 |
|---|---|
| 多语句块（引擎允许拆句/并句） | 记 ``complex_blocks``，**不猜**怎么配 |
| 译文位是空的（``new ""`` / ``e ""``） | 记 ``empty``，不算"已翻" |
| 译文与原文一字不差 | 记 ``same_as_source``，也不算"已翻" |

这三条与成功配对的条数一起进报告 —— 账要对得上，读者才知道覆盖率是真的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from gametrans.engines.base import ExistingTranslation, ExistingTranslations
from gametrans.engines.renpy.skeleton import (
    BLOCK_RE,
    COMMENTED_STATEMENT_RE,
    LOCATOR_RE,
    OLD_RE,
    STRINGS_BLOCK_RE,
    WIRING_BLOCK_RE,
    _original_of,
    _scan_literals,
    _split_say,
    _string_literals,
)
from gametrans.engines.renpy.skeleton_ops import DEFAULT_SKIP
from gametrans.engines.renpy.skeleton_pipeline import translation_dir
from gametrans.errors import ProjectError

__all__ = ["read_existing_translations"]

#: 报告里最多留多少条警告（坏文件多的时候别把报告淹掉，条数仍然如实计）
WARNING_LIMIT = 200


class _Accumulator:
    """把逐文件读出来的东西聚起来，并守住"账要对得上"。"""

    def __init__(self, language: str, directory: Path) -> None:
        self.language = language
        self.directory = str(directory)
        self.entries: list[ExistingTranslation] = []
        self.blocks = 0
        self.complex_blocks = 0
        self.unreadable_blocks = 0
        self.empty_blocks = 0
        self.same_blocks = 0
        #: 译者改过说话人的块数（真靶上把 `s` 换成 `"???"` 这种）
        self.speaker_changed_blocks = 0
        self.strings_total = 0
        self.empty_strings = 0
        self.same_strings = 0
        self.files: list[str] = []
        self.warnings: list[str] = []
        self._extra_warnings = 0

    def warn(self, message: str) -> None:
        if len(self.warnings) < WARNING_LIMIT:
            self.warnings.append(message)
        else:
            self._extra_warnings += 1

    def finish(self) -> ExistingTranslations:
        warnings = list(self.warnings)
        if self._extra_warnings:
            warnings.append(f"（另有 {self._extra_warnings} 条警告未列出）")
        return ExistingTranslations(
            language=self.language,
            directory=self.directory,
            entries=self.entries,
            blocks=self.blocks,
            complex_blocks=self.complex_blocks,
            unreadable_blocks=self.unreadable_blocks,
            empty_blocks=self.empty_blocks,
            same_blocks=self.same_blocks,
            speaker_changed_blocks=self.speaker_changed_blocks,
            strings_total=self.strings_total,
            empty_strings=self.empty_strings,
            same_strings=self.same_strings,
            files=self.files,
            warnings=warnings,
        )


def read_existing_translations(
    project_root: Path,
    *,
    language: str,
    skip: Iterable[str] = DEFAULT_SKIP,
) -> ExistingTranslations:
    """读 ``<工程>/game/tl/<语言>/`` 里已经填好的译文（只读，不写盘）。

    ``skip`` 用来排除引擎自带内容（``common.rpy`` 是引擎内置字符串清单，
    **不是这个游戏的内容**）。
    """
    project_root = Path(project_root)
    language = str(language or "").strip()
    if not language:
        raise ProjectError(
            "没有指定要读哪个语言的已有译文",
            hint="告诉我要读哪一个语言目录（例如 `chinese`），或先设好项目的目标语言。",
        )

    directory = translation_dir(project_root, language)
    if not directory.is_dir():
        available = sorted(
            child.name
            for child in (project_root / "game" / "tl").iterdir()
            if child.is_dir()
        ) if (project_root / "game" / "tl").is_dir() else []
        hint = (
            f"这个工程里现有的语言目录：{'、'.join(available)}。"
            if available
            else "这个工程里还没有任何语言目录 —— 先用官方工具生成一次骨架。"
        )
        raise ProjectError(
            f"这个工程里没有 {language} 的译文目录：{directory}",
            hint=hint,
        )

    skipped = set(skip)
    accumulator = _Accumulator(language, directory)
    # 语言目录可以嵌套（发行版里长成 `tl/<语言>/scripts/characters/...`），
    # 所以整棵树都要走，不能只看第一层。
    for path in sorted(directory.rglob("*.rpy")):
        if path.name in skipped:
            continue
        relative = path.relative_to(project_root).as_posix()
        accumulator.files.append(relative)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:  # 读不了就如实说，不当成空文件
            accumulator.warn(f"{relative}: 读不出来（{exc}）")
            continue
        _read_text(text, relative=relative, sink=accumulator)
    return accumulator.finish()


def _read_text(text: str, *, relative: str, sink: _Accumulator) -> None:
    lines = text.splitlines()
    in_strings = False
    pending_old = ""
    index = 0

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        line_no = index + 1
        index += 1

        if not stripped:
            continue

        locator = LOCATOR_RE.match(stripped)
        if locator:
            continue  # 位置注释：原文注释的解析在下面按"注释掉的语句"处理

        if in_strings:
            old = OLD_RE.match(stripped)
            if old:
                values = _string_literals(old.group("quote"))
                if not values:
                    sink.warn(f"{relative}:{line_no} 的 old 里没有可读的字面量，已跳过")
                    pending_old = ""
                    continue
                pending_old = values[0]
                continue
            if stripped.startswith("new "):
                if not pending_old:
                    sink.warn(f"{relative}:{line_no} 的 new 前面没有 old，已跳过")
                    continue
                values = _string_literals(stripped)
                if not values:
                    sink.warn(f"{relative}:{line_no} 的 new 里没有可读的字面量，已跳过")
                    pending_old = ""
                    continue
                _pair_string(pending_old, values[0], relative, line_no, sink)
                pending_old = ""
                continue
            if stripped.startswith("#"):
                continue

        commented = COMMENTED_STATEMENT_RE.match(stripped)
        if commented:
            # 注释掉的**语句**（块体上方的原文提示）在解析块时用；别的注释不用管
            candidate = commented.group("statement")
            if not candidate.startswith("game/") and not BLOCK_RE.match(candidate):
                pass  # 由下面的块解析就地取用，这里不缓存跨块状态
            continue

        if STRINGS_BLOCK_RE.match(stripped):
            in_strings = True
            pending_old = ""
            continue

        if WIRING_BLOCK_RE.match(stripped):
            # 接线块（换字体 / 改样式）不是内容
            in_strings = False
            pending_old = ""
            continue

        block = BLOCK_RE.match(stripped)
        if block:
            in_strings = False
            pending_old = ""
            body: list[str] = []
            cursor = index
            while cursor < len(lines):
                candidate = lines[cursor]
                if candidate.strip() and not candidate[:1].isspace():
                    break
                body.append(candidate)
                cursor += 1
            index = cursor
            _read_block(
                identifier=block.group("id"),
                body=body,
                first_line=line_no,
                relative=relative,
                sink=sink,
            )
            continue

        if stripped.startswith("translate"):
            sink.warn(f"{relative}:{line_no} 认不出的 translate 形态：{stripped!r}")


def _pair_string(source: str, target: str, relative: str, line_no: int, sink: _Accumulator) -> None:
    sink.strings_total += 1
    if not target.strip():
        sink.empty_strings += 1
        return
    if target == source:
        sink.same_strings += 1
        return
    sink.entries.append(
        ExistingTranslation(
            source=source,
            target=target,
            kind="string",
            file=relative,
            line=line_no,
        )
    )


def _read_block(
    *,
    identifier: str,
    body: list[str],
    first_line: int,
    relative: str,
    sink: _Accumulator,
) -> None:
    sink.blocks += 1
    statements: list[str] = []
    comment = ""
    for line in body:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        if line_stripped.startswith("#"):
            inline = COMMENTED_STATEMENT_RE.match(line_stripped)
            if inline:
                comment = inline.group("statement")
            continue
        statements.append(line_stripped)

    if len(statements) > 1:
        # 引擎允许把一句拆成多句、也允许多句并成一句 —— 一对一配对不成立，不猜
        sink.complex_blocks += 1
        return
    if not statements:
        sink.unreadable_blocks += 1
        sink.warn(f"{relative}:{first_line} 的块 {identifier} 里没有语句")
        return

    split = _split_say(statements[0])
    if split is None:
        sink.unreadable_blocks += 1
        sink.warn(f"{relative}:{first_line} 的块 {identifier} 不是 say 形态：{statements[0]!r}")
        return
    # `_split_say` 从"解析侧与写回侧共用一份判据"那次改动起返回 **4** 项
    # （第 4 项是说话人形态：literal = 名字本身就是要翻的字面量 / identifier = 脚本变量）。
    # 这里只用到前 3 项，但**必须按 4 项解包** —— 少写一项就是整条"读已有译文"的通道
    # 一进来就 ValueError（13 个用例全红）。
    raw_speaker, target, trailer, _speaker_form = split

    original = _original_of(comment, raw_speaker, trailer, allow_speaker_change=True)
    if original is None:
        # 没有可信的原文注释就不猜 —— 块体里的字面量此刻是译文，拿它当原文等于
        # 把"中文"记成"原文"，之后所有复用都建立在错的基础上。
        sink.unreadable_blocks += 1
        sink.warn(f"{relative}:{first_line} 的块 {identifier} 上方没有可信的原文注释，未采信")
        return
    source, original_speaker = original

    if not target.strip():
        sink.empty_blocks += 1
        return
    if target == source:
        sink.same_blocks += 1
        return

    body_speaker = raw_speaker.strip().strip('"').strip()
    # 姿态属性不算身份：``her normal`` / ``him nude surprised`` 的说话人是角色本身。
    # 两侧都取角色名再比 —— 否则换个表情就会被算成"译者改过说话人"。
    body_character = body_speaker.split()[0] if body_speaker else ""
    original_character = original_speaker.split()[0] if original_speaker else ""
    if original_character and original_character != body_character:
        # 译者改过说话人（真靶上把 `s` 换成 `"???"`）—— 是写法，不是错配
        sink.speaker_changed_blocks += 1

    sink.entries.append(
        ExistingTranslation(
            source=source,
            target=target,
            kind="say",
            file=relative,
            line=first_line,
            engine_id=identifier,
            speaker=original_character or body_character,
        )
    )


def _speaker_of(comment: str, raw_speaker: str) -> str:
    """说话人优先从原文注释取（那边是原样记录，带引号的说话人更可靠）。"""
    literals = _string_literals(comment) if comment else []
    if len(literals) >= 2:
        return literals[0]
    return raw_speaker.strip().strip('"').strip()


# `_scan_literals` 由 skeleton 提供、这里也用得上（多行字面量的扫描口径必须只有一处），
# 显式引用一次，免得被当成未使用的导入删掉。
_ = _scan_literals
