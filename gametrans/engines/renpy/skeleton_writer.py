"""把译文填进官方骨架 —— 让中文真的出现在引擎里。

## 为什么这一步是目标的核心

前面所有工作都止于"结构正确"。实测已经证明：产物结构正确、引擎能加载、原文一字未动，
**中文依旧不显示** —— 因为必须按引擎给的 id / 原文把译文填进对应位置。
这个模块就是那一步。

## 分层要求（必须守住）

* 它属于**适配层**：认识具体引擎的骨架格式；
* **位置信息是模块内部的**，不进内核的 :class:`~gametrans.core.slots.Slot` ——
  把 `line/start/end` 塞进内核模型，等于把引擎格式知识漏进内核；
* 内核只提供"哪条槽位对应哪句译文"（:class:`~gametrans.core.bindings.SlotBindings`），
  不参与定位。

## 三条不肯让步的规矩

1. **只改字面量。** 注释、控制流、缩进、空行一个字都不动 ——
   官方注释是对账线索，抹掉它等于自毁证据。
2. **keyed 槽位的每一处都要填。** 同一原文在字符串表里只有一条，但可能有多对
   `old`/`new`；只填第一处，游戏里就会"有的菜单是中文、有的还是英文"。
3. **幂等。** 对账会反复跑；不幂等就会一次比一次脏。

## 文本口径（违反它不会报错，只会静默丢内容）

原文与译文都是**字面量内容本身**，不是"真实文本"：

* 槽位身份（keyed 的键）用引擎写下的那段内容，`\n` / `\"` 原样带着 ——
  与 :mod:`~gametrans.engines.renpy.skeleton` 的解析侧同一口径。写回侧自己
  解除一遍转义，键就对不上：译文永远填不进去（真游戏实测 6 条）。
* 译文里的转义是 provider 按提示词保留的，写回时**只补必要的转义**，
  不把已有的 `\"` / `\n` 再转义一遍（真游戏实测 28 行因此出现可见的反斜杠）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.bindings import SlotBindings, USABLE_STATUS
from gametrans.core.slots import SlotKeying
# 尾注判据只有一处实现（解析与填回必须同口径 —— 各写一份迟早对不上，R50）；
# `_split_say` 也一样：读"这条槽位的原文"必须与解析侧同一套语法理解
from gametrans.engines.renpy.skeleton import _split_say, is_speaker_prefix, is_trailer

__all__ = [
    "SUPPLEMENT_FILE",
    "SkeletonWriteResult",
    "plan_fills",
    "render_supplement_file",
    "write_skeleton",
    "write_supplement_file",
]


#: ``translate zh_CN <id>:``
_BLOCK_RE = re.compile(r"^translate\s+[\w.\-]+\s+(?P<id>[\w.\-]+)\s*:\s*$")
#: ``translate zh_CN strings:``
_STRINGS_RE = re.compile(r"^translate\s+[\w.\-]+\s+strings\s*:\s*$")
#: ``# game/script.rpy:30``
_LOCATOR_RE = re.compile(r"^#\s+[^\s:]+:\d+\s*$")
# 说话人前缀判据**只有一处实现**（R52：各写一份的后果是"扫描认得、写回不填"）：
# 这里原先有一份私有 `_PREFIX_RE`，它不认带姿态属性的说话人（``her normal "…"``），
# 于是那一整类块永远填不进去 —— 词条数与 lint 都正常，一半对白却还是原文。
#: 缩进 + 可选 ``new`` + 引号字符串
_NEW_RE = re.compile(r"^(?P<indent>[ \t]+)new\b")


def _is_quoted(text: str) -> bool:
    stripped = text.strip()
    if not (stripped.startswith('"') and stripped.endswith('"')) or len(stripped) < 2:
        return False
    cursor = 0
    body = stripped[1:-1]
    while cursor < len(body):
        if body[cursor] == "\\":
            cursor += 2
            continue
        if body[cursor] == '"':
            return False
        cursor += 1
    return True


#: 引擎认得的转义字符：``\n`` ``\t`` ``\"`` ``\'`` ``\\``。
#: 已经成对的这些是**字面量原文的一部分**，原样保留；只有不成对的反斜杠才需要补转义。
KNOWN_ESCAPES = frozenset('nt"\'\\')


def _escape(text: str) -> str:
    """把译文编码回骨架字符串字面量 —— **只补必要的转义**。

    这里刻意不做"见到反斜杠就翻倍"：译文与原文同属一套口径（骨架里的字面量内容），
    provider 保留的 ``\\"`` / ``\\n`` 再转义一遍，游戏里就会出现**看得见的反斜杠**
    （真游戏实测 28 行写成 ``谁说的\\\"我们\\\"``，玩家看到的是 ``谁说的\\"我们\\"``）。

    处理三件事：裸引号 → ``\\"``；真换行 / 制表符 → ``\\n`` / ``\\t``；
    **不成对的**反斜杠 → ``\\\\``。判据是"这个转义引擎认不认"，不是"这里有没有反斜杠"。
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in KNOWN_ESCAPES:
            out.append(text[index : index + 2])
            index += 2
            continue
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _scan_spans(line: str) -> list[tuple[int, int, str]]:
    """一行里全部引号字面量的 ``(起, 止, 内容)`` —— 尊重转义。"""
    found: list[tuple[int, int, str]] = []
    index = 0
    while index < len(line):
        start = line.find('"', index)
        if start < 0:
            break
        cursor = start + 1
        buffer: list[str] = []
        closed = False
        while cursor < len(line):
            char = line[cursor]
            if char == "\\" and cursor + 1 < len(line):
                buffer.append(char)
                buffer.append(line[cursor + 1])
                cursor += 2
                continue
            if char == '"':
                closed = True
                cursor += 1
                break
            buffer.append(char)
            cursor += 1
        if not closed:
            break
        found.append((start, cursor, "".join(buffer)))
        index = cursor
    return found


def _block_fill_span(line: str) -> tuple[int, int, str] | None:
    """块内 say 语句里**该被替换**的那个字面量跨度，以及它**现在**写的是什么。

    ``"The station was empty"``  → 第 1 个字面量
    ``e "You're late again."``   → 第 1 个字面量（``e`` 不是字面量）
    ``"Girl" "台词"``            → **第 2 个**字面量（说话人本身是字面量）

    "现在写的是什么"要带出去：默认只填空缺，判断依据就是它。
    """
    spans = _scan_spans(line)
    if not spans:
        return None

    first_start, first_end, first_value = spans[0]
    if len(spans) >= 2:
        return (spans[1][0], spans[1][1], spans[1][2])

    prefix = line[:first_start].strip()
    trailer = line[first_end:].strip()
    # 判据与解析侧**同一份**（R52：各写一份的后果是扫描认得、写回不填）
    if prefix and not is_speaker_prefix(prefix):
        return None
    if trailer and not is_trailer(trailer):
        return None
    return (first_start, first_end, first_value)


def _new_fill_span(line: str) -> tuple[int, int, str] | None:
    """``new "..."`` 里该被替换的字面量跨度，以及它**现在**写的是什么。"""
    if not _NEW_RE.match(line):
        return None
    spans = _scan_spans(line)
    if not spans:
        return None
    return (spans[0][0], spans[0][1], spans[0][2])


@dataclass
class _FillTarget:
    """一处待填位置。**仅本模块内部使用**，不进内核模型。"""

    line: int  # 0-based
    start: int
    end: int
    #: 这个位置**现在**写的是什么 —— 默认"只填空缺"就是看它跟**原文**一不一样
    current: str = ""
    #: 这条槽位的原文（块体上方注释里记着；keyed 就是 ``old``）——
    #: 骨架刚生成时这一侧写的就是原文，所以"与原文相同"等于"还没人翻"
    original: str = ""


@dataclass
class FillPlan:
    """骨架里每一条槽位可以填在哪些位置。

    键一律用**内核的槽位身份键**（``id:<engine_id>`` / ``keyed:<原文>``），
    与 :class:`~gametrans.core.bindings.SlotBindings` 的口径一致 ——
    否则"绑定里有的槽位"和"骨架里有的位置"会对不上。
    """

    identified: dict[str, _FillTarget] = field(default_factory=dict)
    keyed: dict[str, list[_FillTarget]] = field(default_factory=dict)
    #: **说话人字面量**的位置：``(名字原文, 位置)``。名字框里那串字不在字符串表、
    #: 也不是变量，只写在块体的第一个字面量里，所以要单独记一份位置。
    speaker_names: list[tuple[str, _FillTarget]] = field(default_factory=list)


def plan_fills(text: str) -> FillPlan:
    """走一遍骨架，把"每条槽位能填在哪"算出来。

    刻意不复用 ``skeleton.py`` 的解析器：那一个的职责是"抽出槽位给内核"，
    它不该顺带产出引擎内部的坐标。这里独立做一遍定位，两者互不牵连。
    """
    plan = FillPlan()
    lines = text.splitlines()

    in_strings = False
    pending_identified: tuple[str, str] | None = None  # (id, 上一处位置注释行)
    pending_original = ""  # 块体上方注释里记着的原文
    pending_keyed: str | None = None  # 上一个 old 的原文

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        if _STRINGS_RE.match(stripped):
            in_strings = True
            pending_identified = None
            pending_original = ""
            pending_keyed = None
            continue

        block = _BLOCK_RE.match(stripped)
        if block:
            in_strings = False
            pending_identified = (block.group("id"), -1)
            pending_original = ""
            pending_keyed = None
            continue

        if _LOCATOR_RE.match(stripped):
            if pending_identified is not None:
                pending_identified = (pending_identified[0], index)
            continue

        if stripped.startswith("#"):
            # 注释掉的**语句**就是这条槽位的原文（引擎自己写下的）。
            # 骨架刚生成时块体里写的也是原文 —— 两样一比，"有没有人翻过"就分得清。
            commented = stripped[1:].strip()
            if pending_identified is not None and commented:
                split = _split_say(commented)
                if split is not None:
                    pending_original = split[1]
            continue

        if in_strings:
            if stripped.startswith("old "):
                spans = _scan_spans(stripped)
                # 键取**字面量内容本身**，与解析侧同一口径（`skeleton.py` 不解除转义）：
                # 自己再解除一遍，`old "…\n…"` 这类条目的键就对不上，译文永远填不进去。
                pending_keyed = spans[0][2] if spans else None
                continue
            if stripped.startswith("new ") and pending_keyed is not None:
                span = _new_fill_span(line)
                if span is not None:
                    plan.keyed.setdefault(f"keyed:{pending_keyed}", []).append(
                        _FillTarget(index, span[0], span[1], span[2], pending_keyed)
                    )
                continue
            continue

        # 块体里的语句行（缩进行）
        if pending_identified is not None and line[:1].isspace():
            identifier = pending_identified[0]
            span = _block_fill_span(line)
            if span is None:
                continue
            plan.identified[f"id:{identifier}"] = _FillTarget(
                index, span[0], span[1], span[2], pending_original
            )
            # 引号说话人（``"Girl" "…"``）：名字是第一个字面量，它同样要翻。
            # 判定与解析侧同一口径 —— 语句以引号开头才算"说话人是字面量"。
            spans = _scan_spans(line.strip())
            if len(spans) >= 2 and line.strip().startswith('"'):
                name, start, end = spans[0][2], spans[0][0], spans[0][1]
                leading = len(line) - len(line.lstrip())
                plan.speaker_names.append(
                    (
                        name,
                        _FillTarget(
                            index,
                            leading + start,
                            leading + end,
                            name,
                            original=name,
                        ),
                    )
                )
            continue

        # 顶层结构语句（含 ``return`` 之类）—— 结束当前块
        if not line[:1].isspace():
            pending_identified = None

    return plan


@dataclass
class SkeletonWriteResult:
    """一次填充的结果。"""

    text: str
    written: int = 0
    skipped: int = 0
    #: 绑定里有、但骨架里找不到对应位置的槽位（报出来，不静默丢）
    unknown: list[str] = field(default_factory=list)
    #: 骨架里**已经有译文**、按默认没动的位置（要覆盖得显式声明）
    kept_existing: list[str] = field(default_factory=list)
    #: 每一处实际发生的替换，便于人核对
    applied: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "skipped": self.skipped,
            "unknown": list(self.unknown),
            "kept_existing": list(self.kept_existing),
            "applied": list(self.applied),
        }


def _usable_target(bindings: SlotBindings, key: str) -> str | None:
    """这条槽位该填什么；没有可填的返回 ``None``。

    **声明过"有意留空"的条目返回空串**（不是 ``None``）：空串是要真的写进产物的，
    两者含义完全不同 —— 前者是"就让它空着"，后者是"这条没译文，别动它"。
    """
    record = bindings.record_for(key)
    if record is None or record["status"] != USABLE_STATUS:
        return None
    if record["target"]:
        return record["target"]
    return "" if record.get("allow_empty") else None


def write_skeleton(
    text: str,
    bindings: SlotBindings,
    *,
    overwrite_existing: bool = False,
) -> SkeletonWriteResult:
    """把 ``bindings`` 里的译文填进骨架 ``text``，返回新文本。

    * 只改字符串字面量，其余字节不变；
    * keyed 槽位**每一处**都填；
    * 绑定里有、骨架里没有的槽位进 ``unknown``（不静默丢）；
    * **默认只填空缺**：某个位置已经有非空译文就原样留着、记进 ``kept_existing`` ——
      我们的产物常常落在译者自己已经翻好的语言目录里，默认覆盖等于把别人的手艺吃掉；
      确实要用当前译文覆盖那些位置，得显式传 ``overwrite_existing=True``（声明才放行）；
    * 对已填过的骨架再填是原地替换（幂等）。
    """
    plan = plan_fills(text)
    lines = text.splitlines(keepends=True)

    # (行号, 起, 止, 新内容) —— 收集完再应用，避免边改边算位置
    edits: list[tuple[int, int, int, str]] = []
    written = 0
    skipped = 0
    unknown: list[str] = []
    kept_existing: list[str] = []
    applied: list[dict[str, Any]] = []

    for key, target in plan.identified.items():
        new_text = _usable_target(bindings, key)
        if new_text is None:
            skipped += 1
            continue
        if _keep_existing(target, new_text, overwrite_existing):
            kept_existing.append(key)
            continue
        edits.append((target.line, target.start, target.end, _escape(new_text)))
        written += 1
        applied.append({"slot": key, "line": target.line + 1, "kind": "identified"})

    for key, targets in plan.keyed.items():
        new_text = _usable_target(bindings, key)
        if new_text is None:
            skipped += len(targets)
            continue
        for target in targets:
            # 一处一处判：同一个原文可能一处已填、一处在等着补
            if _keep_existing(target, new_text, overwrite_existing):
                kept_existing.append(key)
                continue
            edits.append((target.line, target.start, target.end, _escape(new_text)))
            applied.append({"slot": key, "line": target.line + 1, "kind": "keyed"})
        written += 1

    for name, target in plan.speaker_names:
        # 名字本身就是原文：只要绑定里有这条（keyed 按原文定键），就填
        new_text = _usable_target(bindings, f"keyed:{name}")
        if new_text is None:
            skipped += 1
            continue
        edits.append((target.line, target.start, target.end, _escape(new_text)))
        written += 1
        applied.append({"slot": f"keyed:{name}", "line": target.line + 1, "kind": "speaker"})

    # 绑定里声明了、但骨架里没有位置的槽位：报出来
    for key in _binding_keys(bindings):
        if key.startswith("id:"):
            if key not in plan.identified:
                unknown.append(key)
        else:
            if key not in plan.keyed:
                unknown.append(key)

    # 应用：同一行可能有多处（keyed 不会同 line，但保持一般性），从右往左改
    per_line: dict[int, list[tuple[int, int, str]]] = {}
    for line_no, start, end, replacement in edits:
        per_line.setdefault(line_no, []).append((start, end, replacement))

    for line_no, replacements in per_line.items():
        original = lines[line_no]
        for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
            original = original[:start] + '"' + replacement + '"' + original[end:]
        lines[line_no] = original

    return SkeletonWriteResult(
        text="".join(lines),
        written=written,
        skipped=skipped,
        unknown=unknown,
        kept_existing=kept_existing,
        applied=applied,
    )


def _keep_existing(target: _FillTarget, new_text: str, overwrite_existing: bool) -> bool:
    """这个位置要不要**留着不动**。

    默认只填空缺。**"空缺"的判据是"与原文一样"**，不是"是不是空串" —— 引擎生成的
    骨架两侧写的都是原文（`new "Introduce yourself."`），"非空"并不等于"有人翻过"。
    真正该留着的是这三种之外的情况：已经有人翻成另一种文字了。

    例外两条：显式声明覆盖；以及"当前写的正是我们这次要写的东西"（改了也是白改，
    不该因此报"我动了它"）。
    """
    if overwrite_existing:
        return False
    if not target.current:
        return False  # 空位：填
    if target.original and target.current == target.original:
        return False  # 还是引擎写的原文：没人翻过，填
    return target.current != new_text


#: 声明的补充条目落在语言目录里的这个文件（以 `zz` 开头：官方工具按源码文件名生成镜像，
#: 这个名字游戏永远不会用到，将来重新生成骨架也不会撞上）。
SUPPLEMENT_FILE = "zz_supplements.rpy"

#: 写进补充文件的开场白 —— 让后来的人知道这些条目是**声明**来的，不是引擎给的
_SUPPLEMENT_HEADER = """\
# 由 gametrans 写入：引擎不枚举、但玩家看得见的文本（字面量说话人名、
# 代码里的界面提示等）。条目来自**显式声明**（工作区的 supplements.jsonl），
# 真机实测：这些条目在运行时会被引擎查到并翻译。

translate {language} strings:"""


def render_supplement_file(language: str, entries: Iterable[tuple[str, str]]) -> str:
    """把声明的补充条目渲染成一份独立语言文件（字符串表）。

    * 只写 ``old``/``new`` 两侧，别的一律不碰；
    * 内容完全由声明决定 —— 每次都整份重写，所以天然幂等；
    * 转义交给 :func:`_escape`：声明里的引号/换行落成合法字面量。
    """
    lines = [_SUPPLEMENT_HEADER.format(language=str(language or "").strip())]
    for source, target in entries:
        lines.append("")
        lines.append(f'    old "{_escape(source)}"')
        lines.append(f'    new "{_escape(target)}"')
    return "".join(line + "\n" for line in lines)


def _sibling_line_ending(directory: Path, exclude: Path) -> str:
    """同目录里别的 ``.rpy`` 用什么行尾 —— 新文件跟着走（拿不到就 LF）。"""
    for path in sorted(directory.glob("*.rpy")):
        if path == exclude:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if raw:
            return "\r\n" if b"\r\n" in raw else "\n"
    return "\n"


def write_supplement_file(
    directory: Path, language: str, entries: Iterable[tuple[str, str]]
) -> int:
    """把补充条目落成语言目录里的文件；没有声明就把旧文件删掉。返回写入的条数。"""
    root = Path(directory)
    path = root / SUPPLEMENT_FILE
    prepared = [
        (str(source), str(target)) for source, target in entries if str(source).strip()
    ]
    if not prepared:
        if path.exists():
            path.unlink()
        return 0
    root.mkdir(parents=True, exist_ok=True)
    # 行尾跟着**这个语言目录里别的文件**走（引擎在 Windows 上写 CRLF、别处写 LF）：
    # 一个新文件与它旁边 8 个文件行尾不同，是纯粹的无谓字节差异。
    ending = _sibling_line_ending(root, path)
    path.write_text(render_supplement_file(language, prepared), encoding="utf-8", newline=ending)
    return len(prepared)


def _binding_keys(bindings: SlotBindings) -> list[str]:
    """绑定里出现过的全部槽位键。

    包含两类：属于当前槽位集合的，以及**孤儿**（记了译文但当前槽位集合里没有）——
    后者正是最需要被报成"没落地"的东西，不能只遍历槽位集合把它们漏掉。
    """
    keys = [slot.key for slot in bindings.slots]
    keys.extend(bindings.orphan_keys())
    return keys
