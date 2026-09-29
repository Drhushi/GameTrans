"""Ren'Py 的**控制流事实**：哪些 label 之间真的会连着走。

判定规则全部来自 Ren'Py 的**语言语义**（出处：官方文档 *Label & Control Flow*
<https://www.renpy.org/doc/html/label.html> 与 *Dialogue and Narration*
<https://www.renpy.org/doc/html/dialogue.html>；`call`/`return` 的返回语义见
*Call Statement* 一节），对任何 Ren'Py 工程都成立，**不是给某个靶子打补丁**：

为什么要有这个模块：提取器原来只把源码里**写着的** ``jump`` / ``call`` 变成边，
于是"顺序执行"这条最大的流一条都没建 —— 真靶实测：38 个 label 里 **29 个**会落到
下一个 label（Ren'Py 的语义：``label a:`` 的体跑完若没遇到 ``jump`` / ``return``，
控制就落到同文件的下一条语句），而图里只有 20 条边。

* ``jump`` / ``return`` **确定**截断顺序流（``return`` 是离开当前 label，
  不会再落到下一条语句）；
* ``call`` **不**截断 —— 被调用的 label 返回之后，控制继续往下走；
* ``menu`` 能落到底 **当且仅当**它的**至少一个**选项能落到底（所有选项都 jump 走时，
  菜单下面那句根本到不了）；
* ``if / elif / else`` 能落到底：**没有 else 就一定可以**（条件不成立那条路），
  有 else 时看有没有任一支能落到底；
* 其余块（``while`` / ``for`` / ``python`` / ``init``）按"**可能**落到底"处理 ——
  这是保守方向（宁多一条边、不漏一条），并在返回值里如实标注是"确定的"还是"可能的"。

**保守方向为什么选这一边**：这张图的用途是"把状态送到需要它的单元"。
漏一条边 = 某个单元永远拿不到它需要的上下文（静默失效，正是本项目最怕的形态）；
多一条边 = 多送一点上下文，有预算管着，最坏是多花 token。

> 与外部工作的关系（避免误以为这是新算法）：**用图做上下文选择本身不是新贡献**
> —— Document Graph for NMT (EMNLP 2021)、GRAFT (EMNLP 2025)、TransGraph (EACL 2026)、
> G²C-MT (IJCAI 2026) 都已发表，其中 G²C-MT 直接把上下文选择表述成图上的路径发现问题。
> 本模块**不发明检索算法**，它做的是**把可执行程序的控制流读准**：
> GRAFT 的依赖是 LLM 从文本**推断**的，G²C-MT 的边是相似度/邻接/关键词重叠，
> 而这里的每条边都是 ``provenance="engine"`` —— 引擎自己写着的。
> 详细的边界见 V1 文档 §2.3、§3.5。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gametrans.engines.renpy.parser import RpyStatement

__all__ = ["DynamicJump", "EdgeFact", "FlowFacts", "analyze_file"]

#: 语句是否**确定**截断顺序流。
#:
#: ``return`` 一定截断（它离开当前 label）；``jump`` 一定截断；
#: **``call`` 不截断** —— 被调用的 label 返回之后，控制继续往下走。
#: （把 `call` 当 `jump` 处理会让每一段"先 call 子过程、再继续演"的剧本丢掉顺序流，
#: 真靶上就是这么丢的。）
def _is_return(stmt: RpyStatement) -> bool:
    return stmt.kind != "jump" and stmt.raw.strip().startswith("return")


def _is_jump_away(stmt: RpyStatement) -> bool:
    if _is_return(stmt):
        return True
    return stmt.kind == "jump" and str(stmt.meta.get("verb") or "jump") != "call"


def _block_body(
    statements: list[RpyStatement], index: int, indent: int
) -> tuple[list[RpyStatement], int]:
    """``index`` 处那条语句开的块体（缩进更大的连续区间）。

    返回 ``(体, 块结束后第一个同级语句的下标)``。
    """
    body: list[RpyStatement] = []
    cursor = index + 1
    while cursor < len(statements) and statements[cursor].indent > indent:
        body.append(statements[cursor])
        cursor += 1
    return body, cursor


def _is_conditional(stmt: RpyStatement) -> bool:
    text = stmt.raw.strip()
    return text.startswith("if ") or text.startswith("elif ") or text == "else:"


def _menu_branches(
    statements: list[RpyStatement], index: int, indent: int
) -> tuple[list[list[RpyStatement]], int]:
    """菜单的每个选项 = 一支。返回 ``(各支的语句列表, 菜单之后的下标)``。

    **选项的缩进不能写死成 ``indent + 1``**：Ren'Py 的惯例是 `menu:` 与它的选项
    差一整级（4 个空格），而这一级里有几条空格是风格问题。取块体里**最小的那个缩进**
    才是"选项所在的层"——写死 `+1` 会让所有选项都找不到，菜单整体退化成"能落到底"。
    """
    body, after = _block_body(statements, index, indent)
    child_indent = min((s.indent for s in body), default=indent + 1)
    branch_starts = [
        cursor
        for cursor in range(index + 1, len(statements))
        if statements[cursor].kind == "choice"
        and statements[cursor].indent == child_indent
    ]
    branches: list[list[RpyStatement]] = []
    for order, start in enumerate(branch_starts):
        stop = (
            branch_starts[order + 1]
            if order + 1 < len(branch_starts)
            else len(statements)
        )
        branches.append([s for s in statements[start + 1 : stop] if s.indent > child_indent])
    return branches, after


def _conditional_branches(
    statements: list[RpyStatement], index: int, indent: int
) -> tuple[list[list[RpyStatement]], int, bool]:
    """``if / elif / else`` 链的每一支。返回 ``(各支, 链之后的下标, 有没有 else)``。"""
    heads = [index]
    _, cursor = _block_body(statements, index, indent)
    # `elif` / `else` 与 `if` 同级，是**兄弟语句**，不是块体的孩子
    while cursor < len(statements):
        stmt = statements[cursor]
        if stmt.indent != indent:
            break
        text = stmt.raw.strip()
        if text.startswith("elif ") or text == "else:":
            heads.append(cursor)
            _, cursor = _block_body(statements, cursor, indent)
            continue
        break
    branches = [_block_body(statements, head, indent)[0] for head in heads]
    has_else = statements[heads[-1]].raw.strip() == "else:"
    return branches, cursor, has_else


def _branch_falls_through(branch: list[RpyStatement]) -> bool:
    """一支（菜单的某个选项体 / 某个 if 分支体）会不会落到底。"""
    return bool(branch) and _sequence_may_fall_through(branch, branch[0].indent)


def _sequence_may_fall_through(
    statements: list[RpyStatement], base_indent: int
) -> bool:
    """这一串同级语句跑完之后，控制会不会落到它**下面**那条语句。

    **只看这一层自己的语句**（``indent == base_indent``）：深一层的是某个块的内部，
    由块的头部语句负责消化。这一点被真靶打过一次脸 —— 第一版没有这个判断，
    于是一路走进菜单选项体内部，撞见里面某一个 ``jump`` 就判成"整段跳走了"，
    把 ``act9 → act10``（act9 结尾是 ``with Pause (2.0)``，明明会顺序流）这类边**误杀**，
    连带把 act10 / act11 错算成"没有前驱的区域"。
    """
    index = 0
    while index < len(statements):
        stmt = statements[index]
        if stmt.indent < base_indent:
            break
        if stmt.indent > base_indent:
            # 属于某个块的内部；块由它的头部语句消化，这里不单独看
            index += 1
            continue
        if _is_jump_away(stmt):
            return False
        if stmt.kind == "menu":
            branches, index = _menu_branches(statements, index, stmt.indent)
            if branches and not any(_branch_falls_through(b) for b in branches):
                # 每一支都截断了 → 菜单下面那句到不了
                return False
            continue
        if _is_conditional(stmt):
            branches, cursor, has_else = _conditional_branches(
                statements, index, stmt.indent
            )
            if has_else and not any(_branch_falls_through(b) for b in branches):
                return False
            index = cursor
            continue
        index += 1
    return True


@dataclass(frozen=True)
class EdgeFact:
    """一条控制流边，以及它是"确定的"还是"保守推出来的"。"""

    source: str
    target: str
    #: ``jump`` / ``call`` / ``fallthrough`` / ``screen-action`` / ``constant``
    #: —— 写进边的 note，复盘时看得见来历
    how: str
    #: True = 引擎语义上确定会走；False = 保守认为可能走
    certain: bool
    #: 这一条**自己带的说明**（常量传播那类要说清证据）；空则用默认写法
    note: str = ""


@dataclass(frozen=True)
class DynamicJump:
    """**目标不在语句里**的跳转（`jump/call expression …`、屏幕动作 `Jump(…)`）。

    不直接建边是对的（值是运行时算出来的），但必须说得出来：真靶 `frozeupsaki`
    只能由倒计时屏幕 `Jump(timer_jump)` 到达，而 `$ timer_jump = 'frozeupsaki'`
    写在 `menu1` 体里 —— 静默丢掉它，这个入口就被当成"没有前驱"排进第 0 批（R71）。
    目标能**常量传播**出唯一值时，由调用方补一条边，并在报告里写明证据。
    """

    source: str
    verb: str
    expression: str
    line: int
    #: ``jump-statement``（`jump expression X`）/ ``screen-action``（`Jump(X)`）
    how: str = "jump-statement"
    #: 表达式本身是字面量（`Jump("label")`）时为 True
    literal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.source,
            "verb": self.verb,
            "expression": self.expression,
            "line": self.line,
            "how": self.how,
            "literal": self.literal,
        }


@dataclass
class FlowFacts:
    """一个文件里的控制流事实。"""

    facts: list[EdgeFact] = field(default_factory=list)
    #: 该文件里出现的 label（按行序）
    labels: list[str] = field(default_factory=list)
    #: 目标读不出来的跳转（按行去重）：由调用方常量传播，或如实报出来
    dynamic: list[DynamicJump] = field(default_factory=list)
    #: 字符串常量赋值 ``变量 → [(字面量, 行号, 所在的 label)]``：常量传播的输入
    string_assignments: dict[str, list[tuple[str, int, str]]] = field(default_factory=dict)
    #: 被 jump/call 指向、但**本文件里没有定义**的目标 → 由调用方按全项目判"悬空"
    targets: dict[str, dict[str, str]] = field(default_factory=dict)


def _enclosing_label(labels: list[RpyStatement], line: int) -> str:
    """``line`` 落在哪个 label 体内（取**最近的那个行号在前**的 label）。

    屏幕动作可能写在顶层的 `screen` 里（不属于任何 label），这时靠"哪一行在哪个 label
    体内"来归属；实在没有就返回空串，由调用方决定怎么记账。
    """
    found = ""
    for label in labels:
        if int(label.line or 0) < line and label.name:
            found = str(label.name)
        elif int(label.line or 0) >= line:
            break
    return found


def analyze_file(statements: list[RpyStatement]) -> FlowFacts:
    """从一个文件的语句序列里读出控制流事实。

    **label 的缩进是多少都要算。** Ren'Py 的 label 是全局的，缩进只是书写风格；
    真靶里 `label menu1:` 缩进 4（嵌在 `questiontime1` 的体里），第一版只认
    ``indent == 0`` 的 label，于是 `menu1` 整个被跳过 ——
    它里面那两条 ``jump protect_saki`` / ``jump tellguyssaki`` 一条都没读出来，
    `menu1` 与 `sakilewd1` 因此双双被错算成"没有前驱的区域"。

    体的边界也按缩进定：**下一个"同级或更浅"的 label** 才是边界；
    比它深的 label 是**嵌在它体里**的，不能截断它（截断了就会漏掉顺序流）。
    """
    result = FlowFacts()
    labels = [stmt for stmt in statements if stmt.kind == "label"]
    result.labels = [str(stmt.name) for stmt in labels if stmt.name]
    #: 读不出目标的跳转按**行**去重：嵌套 label 会让同一句被外层 label 也算一遍，
    #: 逐行去重之后"有几处读不出来"才是真数（逐条归属留给 R70 那条修法）。
    dynamic_lines: set[int] = set()
    #: 字符串常量赋值是**文件级事实**（`init:` 块里、label 体里都算）：常量传播的输入。
    #: 判据是解析器打的标记（`string_constant`），不是语句类别 ——
    #: 这类语句同时还要照旧进"结构性字符串 / 该翻不敢动"那两栏账目。
    for stmt in statements:
        if stmt.meta.get("string_constant") and stmt.name:
            line = int(stmt.line or 0)
            result.string_assignments.setdefault(str(stmt.name), []).append(
                (str(stmt.text or ""), line, _enclosing_label(labels, line))
            )
    #: 屏幕动作 / python 块里的 `Jump(X)`：它属于**哪一场戏**（screen 本身可能不在任何
    #: label 里，那就按"哪一行在哪个 label 体内"归属）。
    for stmt in statements:
        if stmt.kind != "dynamic_jump":
            continue
        line = int(stmt.line or 0)
        if line in dynamic_lines:
            continue
        dynamic_lines.add(line)
        result.dynamic.append(
            DynamicJump(
                source=_enclosing_label(labels, line),
                verb="jump",
                expression=str(stmt.text or ""),
                line=line,
                how="screen-action",
                literal=bool(stmt.meta.get("literal")),
            )
        )

    ### 顺序流：体跑完控制落到**它在所属 block 里的下一条语句**（引擎的 chain_block），
    ### 那条语句属于哪个 label 就落到哪个 label —— 不是"文件里的下一个 label"。
    ### 归属是**最内层** label：子 label 的语句不算在外层头上（否则一条 jump 会被
    ### 展开成 N×M 条边，真靶 60 条边里 25 条是这么来的，R70）。
    for order, label in enumerate(labels):
        name = str(label.name or "")
        if not name:
            continue
        body = _own_body(statements, labels, order)
        for stmt in body:
            if stmt.kind == "jump" and stmt.name:
                result.targets.setdefault(name, {}).setdefault(
                    str(stmt.name), str(stmt.meta.get("verb") or "jump")
                )
                result.facts.append(
                    EdgeFact(name, str(stmt.name), str(stmt.meta.get("verb") or "jump"), True)
                )
            elif stmt.kind == "jump" and stmt.meta.get("expression"):
                if int(stmt.line or 0) in dynamic_lines:
                    continue
                dynamic_lines.add(int(stmt.line or 0))
                result.dynamic.append(
                    DynamicJump(
                        source=name,
                        verb=str(stmt.meta.get("verb") or "jump"),
                        expression=str(stmt.text or ""),
                        line=int(stmt.line or 0),
                    )
                )
            elif stmt.kind == "label" and stmt.name:
                # 体里嵌着 `label M:`：进入本 label 就会**走进 M 的体**（引擎的
                # `Label.chain` 把 M 的体接在本体里）。所以这是一条真实的入口关系。
                result.facts.append(EdgeFact(name, str(stmt.name), "nested-label", True))
        # 顺序流：体跑完控制落到**它在所属 block 里的下一条语句**（引擎的 chain_block），
        # 那条语句属于哪个 label 就落到哪个 label —— 不是"文件里的下一个 label"。
        # 体里最后一条是嵌套 label 时不在这里发：那种情况下落到下一句的是**子 label**
        # （`Label.chain` 把子 label 的 next 接成外层的 next），它自己会发那条边。
        if body and not _ends_with_nested_label(body):
            if _sequence_may_fall_through(body, min(s.indent for s in body)):
                successor = _label_of_successor(statements, labels, order)
                if successor and successor != name:
                    result.facts.append(EdgeFact(name, successor, "fallthrough", True))
    return result


def _ends_with_nested_label(body: list[RpyStatement]) -> bool:
    """体里最后一条语句是不是一个嵌进来的 `label`。"""
    for stmt in reversed(body):
        if stmt.kind == "label" and stmt.name:
            return True
        return False
    return False


def _own_body(
    statements: list[RpyStatement], labels: list[RpyStatement], index: int
) -> list[RpyStatement]:
    """``labels[index]`` **自己**的语句（不含嵌在它体里的子 label 的语句）。

    引擎的语义是逐语句链起来的，而嵌套 label 的语句属于**它自己**：
    把子 label 的语句也算进外层，同一句 `jump` 就会既算在 `menu1` 头上、又算在
    `questiontime1` 头上（真靶上就是这么把一条跳转放大成 12 条边的，R70）。
    """
    label = labels[index]
    stop_line = 10**9
    for later in labels[index + 1 :]:
        if later.indent <= label.indent:
            stop_line = later.line
            break
    own: list[RpyStatement] = []
    for stmt in statements:
        if not (label.line < stmt.line < stop_line and stmt.indent > label.indent):
            continue
        # 更深（更晚声明）的 label 认领它下面的语句：谁最内层，句子归谁。
        owner = _innermost_owner(labels, index, stmt)
        if owner == index:
            own.append(stmt)
    return own


def _innermost_owner(
    labels: list[RpyStatement], index: int, stmt: RpyStatement
) -> int:
    """``stmt`` 属于哪一个 label（取**最内层**的那个；``index`` 是候选外层）。"""
    owner = index
    for later_index in range(index + 1, len(labels)):
        later = labels[later_index]
        if int(later.line or 0) >= int(stmt.line or 0):
            break
        if later.indent > labels[owner].indent and stmt.indent > later.indent:
            owner = later_index
    return owner


def _label_of_successor(
    statements: list[RpyStatement], labels: list[RpyStatement], index: int
) -> str:
    """``labels[index]`` 的体跑到底之后，控制落到**哪个 label**（没有就空串）。

    引擎的语义（`renpy/ast.py:570 chain_block` / `:1125 Label.chain`）：一个体的末节点
    接到 `next`＝**它在所属 block 里的下一条语句**；那条语句可能是另一个顶层 label
    （于是走进它的体），也可能在某个 label 的体里（于是回到那场戏中间）。
    """
    body = _own_body(statements, labels, index)
    if not body:
        # 空体：直接落到本 label 语句在所属 block 里的下一条语句
        successor_line = _next_statement_line(statements, int(labels[index].line or 0))
    else:
        last_line = max(int(stmt.line or 0) for stmt in body)
        successor_line = _next_statement_line(statements, last_line)
    if successor_line is None:
        return ""
    # 下一条语句本身就是一个 label 语句 → 走进它的体
    target = next((s for s in statements if int(s.line or 0) == successor_line), None)
    if target is not None and target.kind == "label" and target.name:
        return str(target.name)
    return _label_containing(statements, labels, successor_line)


def _next_statement_line(statements: list[RpyStatement], line: int) -> int | None:
    """``line`` 之后、**同级或更浅**的第一条语句的行号（引擎的 `next`）。

    更深的是某个块的内部（由块的头部语句消化），不算"下一条语句"。找不到返回 ``None``。
    """
    current = next((s for s in statements if int(s.line or 0) == line), None)
    if current is None:
        return None
    for stmt in statements:
        at = int(stmt.line or 0)
        if at <= line:
            continue
        if stmt.indent <= current.indent:
            return at
    return None


def _label_containing(
    statements: list[RpyStatement], labels: list[RpyStatement], line: int
) -> str:
    """哪一行属于哪个 label 的体（取最内层的那个）；不属于任何 label 就空串。"""
    best = ""
    best_indent = -1
    for label in labels:
        if int(label.line or 0) >= line or not label.name:
            continue
        stop_line = 10**9
        for later in labels:
            if int(later.line or 0) > int(label.line or 0) and later.indent <= label.indent:
                stop_line = int(later.line or 0)
                break
        stmt = next((s for s in statements if int(s.line or 0) == line), None)
        if stmt is None or stmt.indent <= label.indent or not (int(label.line or 0) < line < stop_line):
            continue
        if label.indent > best_indent:
            best, best_indent = str(label.name), label.indent
    return best
