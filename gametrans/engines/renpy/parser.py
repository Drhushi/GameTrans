"""Ren'Py 脚本 (.rpy) 的最小解析器。

只做一件事：把缩进敏感的 .rpy 文本切成带类型、带行号、带缩进的语句序列。
它刻意不构建图、不碰文件系统 —— 那些是 extractor 的活。

本期覆盖：``label`` / ``menu`` / 菜单选项 / 菜单标题 / 旁白 / 角色对话 /
``define``（角色名与字面量）/ ``strings`` 块。
拿不准的一律标成 ``unsupported`` 交给上层记账，绝不猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- 语句模式 ---------------------------------------------------------------

LABEL_RE = re.compile(r"^label\s+([A-Za-z_]\w*)\s*(?:\(.*\))?\s*:$")
#: ``menu:`` / ``menu <名字>:``。
#:
#: **具名菜单在引擎里就是一个 label**（真游戏随包 `renpy/parser.py:700`
#: `rv.append(ast.Label(loc, label, [], None))`，`:687` 还会 `set_global_label(name)`）：
#: 别的脚本里 `jump evequestions` 是合法跳转，引擎给它的文本块 id 也是
#: `evequestions_<hash>`。不认这个名字的后果实测过（真靶 12 个具名菜单、39 次跳转）：
#: 跳转被报成"目标在整个工程里没有定义"，一条边都不建。
#:
#: 认名字**不等于**为它新建一个节点：节点＝一场戏（一个 `label`），菜单是这场戏**内部**的东西。
MENU_RE = re.compile(r"^menu\s*(?P<name>[A-Za-z_]\w*)?\s*(?:\(.*\))?\s*:$")
#: ``screen <名字>():`` —— 屏幕语言块的开头。
#: 认它只为一件事：块体里的 `label _(...)` / `label "..."` 是**显示控件**，
#: 不是 `label` 语句（`renpy/parser.py` 里 `label` 是语句、屏幕语言里 `label` 是控件，
#: 判据是**它在哪个块里**）。
SCREEN_RE = re.compile(r"^screen\s+[A-Za-z_]\w*\s*(?:\(.*\))?\s*:$")
#: ``$ <变量> = "字面量"`` —— **纯字符串常量赋值**。
#:
#: 存在的理由只有一个：屏幕动作里的 ``Jump(<变量>)`` 靠它读出目标
#: （真靶 `screen countdown` 的 `timer … Jump(timer_jump)`，而
#: `$ timer_jump = 'frozeupsaki'` 写在 `menu1` 体里）。RHS 必须**只有**一个字面量，
#: 否则（`"a" + "b"` 这种）不许当常量。
PURE_STRING_ASSIGN_RE = re.compile(
    r"^\$\s*(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<quote>['\"])(?P<value>(?:\\.|(?!\2).)*)\2\s*$"
)
#: 屏幕动作 / python 块里的 ``Jump(<表达式>)``（``renpy.jump`` 同义）。
SCREEN_JUMP_RE = re.compile(r"(?:renpy\.)?Jump\s*\(\s*(?P<arg>[^()]*?)\s*\)")
#: ``jump act_two`` / ``call act_two`` / ``call act_two from _call_act_two_1``
#: —— 玩家真的会从 A 走到 B，这个先后**不是猜的**。
#:
#: ``from`` 子句必须认：**现代 Ren'Py 会给每一个 ``call`` 自动写上
#: ``call X from _call_X_N``**（官方文档 Call Statement 一节）。不认它的后果是
#: 这一类调用**一条边都读不到**，而且不会进任何账（那行既没有字符串字面量、
#: 也不是 ``jump``，直接落进"其它"分支）—— 真靶上 11 处 ``call X from Y``
#: 全部消失，``evesip2`` 因此被错算成"没有前驱的区域"。
#:
#: 目标是表达式时（``call expression next_label``）**仍然故意匹配不上**：
#: 静态读不出目标，宁可没有边也不猜（真靶上是 0 处）。
JUMP_RE = re.compile(
    r"^(?P<verb>jump|call)\s+(?P<target>[A-Za-z_]\w*)\s*"
    r"(?:\(.*\))?\s*"
    r"(?:from\s+(?P<from>[A-Za-z_]\w*))?\s*$"
)
#: ``jump expression timer_jump`` / ``call expression dynamic_label``。
#:
#: 目标是表达式 → **静态读不出目标**，所以不建边（`JUMP_RE` 仍然匹配不上它，
#: `JUMP_RE` 那条判据是对的）。但它必须被**认出来**，两件事都因此成立：
#:
#: * `jump expression` 一样**截断顺序流**（控制确实走了，不会再落到下一条语句）；
#: * 它要**如实记账**（`dynamic_jump_unresolved`）—— 真靶 `frozeupsaki` 只能由
#:   倒计时屏幕 `Jump(timer_jump)` 到达，读不出又不报，它就被当成"没有前驱"
#:   排进第 0 批（登记册 R71）。"不猜"不等于"不说"。
DYNAMIC_JUMP_RE = re.compile(
    r"^(?P<verb>jump|call)\s+expression\s+(?P<expr>\S.*?)\s*$"
)
CHOICE_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:if\s+.+?)?\s*:$')
#: 说话人：`e` / `her normal` 这样的标识符（可带属性），**或**引号字符串
#: （`"Phi" "Hi there!"` —— Ren'Py 允许用字面名字当说话人，常见于旁白型角色）。
#: 只认标识符时，这种写法会掉进兜底分支被当成"结构字符串"，正文被取成说话人名，
#: 于是源码侧没有这条单位 → 对齐时上下文全空 → 整段内容丢掉 label 归属。
SPEAKER_RE = r'(?:[A-Za-z_][\w\.]*(?:\s+[A-Za-z_]\w*)*|"(?:[^"\\]|\\.)*")'
SAY_RE = re.compile(
    rf'^(?P<who>{SPEAKER_RE})?\s*'
    r'"(?P<what>(?:[^"\\]|\\.)*)"\s*'
    r"(?P<trailer>.*)$"
)

#: 尾注：``with <表达式>`` / ``if <条件>:`` / ``nointeract``。
#: **判据只有这一份**——骨架侧与源码侧共用它。真靶上 216 条台词写的是
#: ``with Shake((0.5, 1.0, 0.5, 1.0), 1.0, dist=5)``：只认 ``with dissolve``
#: 的写法会把这些块漏掉（骨架侧漏了槽位、源码侧漏了结构归属，两个方向都出现过）。
IF_TRAILER_RE = re.compile(r"^if\s+.+:\s*$", re.IGNORECASE)
#: 括号配对表（尾注里的表达式必须自洽）
BRACKET_PAIRS = {")": "(", "]": "[", "}": "{"}


def _brackets_balanced(text: str) -> bool:
    stack: list[str] = []
    for char in text:
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != BRACKET_PAIRS[char]:
                return False
    return not stack


def is_trailer(text: str) -> bool:
    """句尾那一段是不是**尾注**（``with <表达式>`` / ``if <条件>:`` / ``nointeract``）。

    放宽是有边界的：括号必须配对，表达式里不许出现引号（出现引号说明我们的字面量
    扫描已经切错了位置）。宁可报"认不出"，也不要把一整段代码当台词抽走。
    """
    stripped = text.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if lowered == "nointeract":
        return True
    if lowered.startswith("with "):
        expression = stripped[len("with "):].strip()
        if not expression or '"' in expression or "'" in expression:
            return False
        return _brackets_balanced(expression)
    return bool(IF_TRAILER_RE.match(stripped))
DEFINE_RE = re.compile(r"^define\s+([\w\.]+)\s*=\s*(.+)$")
CHARACTER_RE = re.compile(r'^Character\s*\(\s*"((?:[^"\\]|\\.)*)"')
LITERAL_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"$')
STRINGS_BLOCK_RE = re.compile(r"^(?:translate\s+[\w\.\-]+\s+)?strings\s*:$")
OLD_RE = re.compile(r'^old\s+"((?:[^"\\]|\\.)*)"\s*$')
NEW_RE = re.compile(r'^new\s+"((?:[^"\\]|\\.)*)"\s*$')
PYTHON_LINE_RE = re.compile(r"^(?:\$|(?:init\s+)?python\b)")
PYTHON_BLOCK_RE = re.compile(r"^(?:(?:init\s+)?python|init)\s*:$")
ASSIGN_RE = re.compile(r"^\$\s*([A-Za-z_]\w*)")
ANY_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

#: 资源扩展名 —— 命中它的字面量是路径，不是文本
ASSET_SUFFIX_RE = re.compile(
    r"\.(?:png|jpe?g|webp|gif|ogg|mp3|wav|opus|ttf|otf|ttc|json|txt|rpy|rpyc|zip|webm|mp4)$",
    re.IGNORECASE,
)
#: 标识符/键/属性名：没有空白、没有标点（点、横线除外）
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")


#: 下标键：``x["key"]`` / ``x[0]["key"]`` —— 方括号前面是标识符、右括号或右方括号
SUBSCRIPT_RE_TEMPLATE = r"[A-Za-z0-9_\)\]]\s*\[\s*\"{lit}\"\s*\]"


def looks_structural(literal: str, line: str = "", *, python_context: bool = False) -> bool:
    """这段字面量像不像「结构性字符串」而不是待译文本。

    真实游戏上被误报成「覆盖缺口」的绝大多数是：资源路径、格式模板
    （``matrixtransform_{}_{}``）、下标键（``data["master"]``）、ATL/样式里的属性名
    （``xpos`` / ``state`` / ``camera``）。

    判据刻意**保守**，因为两侧出错代价不同：

    * 把文本误判成结构 → 那段话**永远不会被翻**（看不见的损失）；
    * 把结构误判成文本 → 只是报告里多一条噪音（看得见的成本）。

    所以下面的每一条都只放过「明显不是散文」的：

    * 空/单字符、资源路径、带占位符的模板；
    * ``["key"]`` 这种**下标键**（前面是标识符/右括号）；
    * **非 python 上下文**里的单词标识符（ATL 属性名、样式属性）。

    python 上下文里的单 token 字面量**不放过** —— ``$ options = ["Stay", "Leave"]``
    这种数据字面量完全可能就是界面文本（示例工程里就有一条），宁可报出来。
    """
    stripped = literal.strip()
    if not stripped or len(stripped) <= 1:
        return True
    if ASSET_SUFFIX_RE.search(literal):
        return True
    if ("/" in literal or "\\" in literal) and " " not in stripped:
        return True
    if "{" in literal and "}" in literal:
        # 模板：没有空格，或者空格只出现在逗号之后（``OffsetMatrix({}, {}, {})*``）
        without_arg_spaces = literal.replace(", ", ",").replace(" ,", ",")
        if " " not in without_arg_spaces.strip():
            return True
    import re as _re

    if _re.search(SUBSCRIPT_RE_TEMPLATE.format(lit=_re.escape(literal)), line):
        return True
    if not python_context and IDENTIFIER_RE.fullmatch(stripped) and len(stripped) <= 48:
        return True
    return False


_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "'": "'"}


def split_comment(line: str) -> str:
    """去掉行尾注释，但尊重字符串字面量里的 ``#``。"""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in line:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def decode_string(raw: str) -> str:
    """把 .rpy 字符串字面量里的转义还原成真实文本，供翻译使用。"""
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def encode_string(text: str) -> str:
    """把译文重新编码成 .rpy 字符串字面量。"""
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def has_string_literal(text: str) -> bool:
    return ANY_STRING_RE.search(text) is not None


@dataclass
class RpyStatement:
    """一条被识别出来的 .rpy 语句。"""

    kind: str
    line: int
    indent: int
    raw: str
    text: str | None = None
    speaker: str | None = None
    name: str | None = None
    block: bool = False
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}@{self.line}"


def _classify(body: str, line: int, indent: int, ctx: str) -> RpyStatement:
    stmt = RpyStatement(kind="other", line=line, indent=indent, raw=body)

    # 1) Python 上下文：块内或 `$` 开头的行。含字符串字面量的一律不敢动。
    if ctx == "python" or PYTHON_LINE_RE.match(body):
        # 纯字符串常量赋值（`$ timer_jump = 'frozeupsaki'`）：**多记一个标记**，
        # 供常量传播读屏幕动作的跳转目标用。分类仍走原来的判据 ——
        # 它是"结构性字符串"还是"该翻但不敢动"，是另一件事，不许因为加了标记就少报一笔。
        pure = PURE_STRING_ASSIGN_RE.match(body)
        if pure:
            stmt.name = pure.group("name")
            stmt.text = decode_string(pure.group("value"))
            stmt.meta["string_constant"] = True
            if looks_structural(stmt.text or "", body, python_context=True):
                stmt.kind = "structural"
                stmt.meta["reason"] = "structural_string"
            else:
                stmt.kind = "unsupported"
                stmt.meta["reason"] = "inline_python_string"
            return stmt
        if has_string_literal(body):
            assign = ASSIGN_RE.match(body)
            stmt.name = assign.group(1) if assign else None
            stmt.text = ANY_STRING_RE.search(body).group(1)  # type: ignore[union-attr]
            if looks_structural(stmt.text or "", body, python_context=True):
                stmt.kind = "structural"
                stmt.meta["reason"] = "structural_string"
            else:
                stmt.kind = "unsupported"
                stmt.meta["reason"] = "inline_python_string"
            return stmt
        stmt.block = PYTHON_BLOCK_RE.match(body) is not None
        if stmt.block:
            stmt.kind = "python_block"
        return stmt

    # 2) strings 块内部的 old/new 对
    if ctx == "strings":
        old = OLD_RE.match(body)
        if old:
            stmt.kind = "strings_old"
            stmt.text = decode_string(old.group(1))
            return stmt
        new = NEW_RE.match(body)
        if new:
            stmt.kind = "strings_new"
            stmt.text = decode_string(new.group(1))
            return stmt

    # 3) 块开头的结构语句
    if STRINGS_BLOCK_RE.match(body):
        stmt.kind = "strings_block"
        stmt.block = True
        return stmt

    if SCREEN_RE.match(body):
        # `screen <名字>():`：它开的是一个**屏幕语言块**。认它只有一个目的 ——
        # 块体里的 `label _(...)` / `label "..."` 是**显示控件**，不是 `label` 语句
        # （真靶 screens.rpy 里 `label _(message):` 被当成 label，图上于是长出一个
        # 叫 `_` 的假场景，把 2 条无关的界面字符串单独收成一个节点）。
        stmt.kind = "screen"
        stmt.block = True
        return stmt

    # 屏幕动作 / python 块里的 `Jump(<表达式>)`：**它是一条跳转**，只是目标可能读不出。
    # 真靶 `screen countdown` 的 `timer … Jump(timer_jump)` 就是它 ——
    # 不认这一行，`frozeupsaki` 永远没有前驱，会被排进第 0 批。
    if ctx in ("screen", "python", "other") or body.startswith("$"):
        screen_jump = SCREEN_JUMP_RE.search(body)
        if screen_jump:
            arg = screen_jump.group("arg").strip()
            stmt.kind = "dynamic_jump"
            stmt.text = arg
            stmt.meta["literal"] = bool(
                re.fullmatch(r"""['"][^'"]*['"]""", arg or "")
            )
            return stmt

    if ctx != "screen":
        # 屏幕语言块里没有 `label` / `menu` 语句：这两个词在那儿是显示控件。
        label = LABEL_RE.match(body)
        if label:
            stmt.kind = "label"
            stmt.name = label.group(1)
            stmt.block = True
            return stmt

        menu = MENU_RE.match(body)
        if menu:
            stmt.kind = "menu"
            stmt.block = True
            # 具名菜单：名字要留着（它是合法跳转目标），但它不因此成为一个节点。
            stmt.name = menu.group("name")
            return stmt

    jump = JUMP_RE.match(body)
    if jump:
        stmt.kind = "jump"
        stmt.name = jump.group("target")
        stmt.meta["verb"] = jump.group("verb")
        return stmt

    dynamic = DYNAMIC_JUMP_RE.match(body)
    if dynamic:
        # 目标读不出来：**kind 仍是 jump**（于是它照样截断顺序流），
        # 但没有 name，且带上表达式本身供记账用（`controlflow` 只收事实，
        # 建不建边由 `extractor` 决定）。
        stmt.kind = "jump"
        stmt.meta["verb"] = dynamic.group("verb")
        stmt.meta["expression"] = True
        stmt.text = dynamic.group("expr")
        return stmt

    define = DEFINE_RE.match(body)
    if define:
        target, value = define.group(1), define.group(2).strip()
        character = CHARACTER_RE.match(value)
        if character:
            stmt.kind = "define_character"
            stmt.name = target
            stmt.text = decode_string(character.group(1))
            return stmt
        literal = LITERAL_RE.match(value)
        if literal:
            stmt.kind = "define_string"
            stmt.name = target
            stmt.text = decode_string(literal.group(1))
            return stmt
        stmt.kind = "define_other"
        stmt.name = target
        return stmt

    # 4) 菜单上下文里的选项
    if ctx == "menu":
        choice = CHOICE_RE.match(body)
        if choice:
            stmt.kind = "choice"
            stmt.text = decode_string(choice.group(1))
            stmt.block = True
            return stmt

    # 5) 旁白与角色对话
    say = SAY_RE.match(body)
    if say and is_trailer(say.group("trailer") or ""):
        stmt.kind = "say"
        who = (say.group("who") or "").strip()
        if who.startswith('"') and who.endswith('"'):
            # 引号说话人是**显示名**（引擎原样显示），不是变量名 —— 标出来，
            # 让提取层按"人名"处理它，而不是去角色表里查一个不存在的键。
            stmt.speaker = decode_string(who[1:-1]).strip() or None
            stmt.meta["speaker_quoted"] = True
        else:
            stmt.speaker = who or None
        stmt.text = decode_string(say.group("what"))
        return stmt

    # 6) 兜底：还带着字符串字面量却认不出来 —— 记账，不猜。
    #    先分一下"这看起来是不是待译文本"：标识符/键/路径/模板不是文本，
    #    它们进 skipped 而不是 unsupported，免得把真正的缺口淹掉（保守判据见
    #    looks_structural：一句话仍然算 unsupported）。
    if has_string_literal(body):
        stmt.text = ANY_STRING_RE.search(body).group(1)  # type: ignore[union-attr]
        if looks_structural(stmt.text or "", body):
            stmt.kind = "structural"
            stmt.meta["reason"] = "structural_string"
        else:
            stmt.kind = "unsupported"
            stmt.meta["reason"] = "unclassified_string"
    return stmt


def parse_rpy(text: str) -> list[RpyStatement]:
    """把 .rpy 文本解析成语句序列（含空行以外的全部结构化行）。"""
    statements: list[RpyStatement] = []
    stack: list[tuple[int, str]] = []

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        cleaned = split_comment(raw_line)
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip())
        body = cleaned.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        ctx = stack[-1][1] if stack else "root"

        stmt = _classify(body, lineno, indent, ctx)
        statements.append(stmt)

        if stmt.block:
            child_ctx = {
                "menu": "menu",
                "choice": "choice",
                "strings_block": "strings",
                "python_block": "python",
                "label": "label",
                "screen": "screen",
            }.get(stmt.kind, "other")
            stack.append((indent, child_ctx))

    return statements
