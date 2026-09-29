"""Ren'Py 提取器：把 .rpy 语句序列整理成带权路径图。

**它读的是结构，不是内容范围。** "哪些文本要翻"由官方工具产出的骨架回答
（见 `skeleton.py` / `align.py`）；这里只回答"这些文本长在哪"：label 区间、
菜单位置、控制流（jump/call）、重复出现。内容范围由源码解析决定会漏掉
官方提得到的那几类（`screen` 文本、`_()` 字面量等），所以那件事不在这里做。

权重是本层的"意见"：
* 菜单选项 90 —— 直接决定玩家操作，翻错了体验最差
* 角色名   70 —— 贯穿全篇，一致性价值最高
* 对话/旁白 60 —— 主体内容
* 字符串   50 —— 界面文本，量小且独立

重复出现的文本会在建图后被标上 ``occurrences``，让翻译层知道"翻一次到处受益"。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path, PurePosixPath

from gametrans.core.graph import PathGraph, PathGraphBuilder
from gametrans.core.models import (
    Context,
    EdgeType,
    GraphEdge,
    Locator,
    NodeKind,
    TranslationUnit,
)
from gametrans.engines.base import ExtractContext
from gametrans.engines.renpy.controlflow import DynamicJump, EdgeFact, analyze_file
from gametrans.engines.renpy.parser import RpyStatement, decode_string, parse_rpy
from gametrans.engines.renpy.segments import segmentize

#: 把一个"没有内容的 label"沿出边展开时最多走几跳。
#: 真靶上这类 label 都是"只有跳转/杂项"的调度点，一跳就够；给个上限是防环。
MAX_CONTRACTION_HOPS = 8

NODE_KIND: dict[str, NodeKind] = {
    "choice": NodeKind.CHOICE,
    "definition": NodeKind.DEFINITION,
    "say": NodeKind.SAY,
    "string": NodeKind.STRING,
}

#: 默认的内容范围与排除项（真正生效的值由支持包申报，见 ``RenPyPack``）。
DEFAULT_FILE_GLOBS: tuple[str, ...] = ("game/**/*.rpy",)
DEFAULT_EXCLUDED_PARTS: tuple[str, ...] = ("tl", ".gametrans")

#: 显示名里的运行期内插（`Character("[name]")`；也可能嵌在别处，如 `"[name] & Eve"`）。
INTERPOLATION_NAME_RE = re.compile(r"^\[([A-Za-z_]\w*)\]$")
INTERPOLATION_RE = re.compile(r"\[([A-Za-z_]\w*)\]")

#: 默认名的两种**结构性**声明（不从散文里猜）：
#: ``default name = "Johan"``；以及空输入回退 ``if name == "": name = "Johan"``
#: （真靶工程用的是后者，写在 ``renpy.input`` 之后）。
DEFAULT_STATEMENT_RE = re.compile(r'^\s*default\s+([A-Za-z_]\w*)\s*=\s*"((?:[^"\\]|\\.)*)"', re.M)
EMPTY_INPUT_FALLBACK_RE = re.compile(
    r'if\s+([A-Za-z_]\w*)\s*==\s*""\s*:\s*\n?\s*\1\s*=\s*"((?:[^"\\]|\\.)*)"'
)

#: 游戏没有声明默认主角名时用的显示名（免得摘要与提示词里出现 ``[name]``）。
PROTAGONIST_LABEL = "主角"


def _declared_defaults(text: str) -> dict[str, str]:
    """游戏自己声明的默认值（变量 → 字面量），只看**结构性**写法，不猜散文。"""
    found = {
        name: decode_string(literal)
        for name, literal in DEFAULT_STATEMENT_RE.findall(text)
    }
    for name, literal in EMPTY_INPUT_FALLBACK_RE.findall(text):
        found.setdefault(name, decode_string(literal))
    return found


def _display_name(declared: str, defaults: dict[str, str]) -> str:
    """把"运行期才定"的显示名换成游戏声明的默认名；没声明就用「主角」。

    三种形态：
    * 整条就是内插（``[name]``）—— 这是主角的名字：有默认名用默认名，没有用「主角」；
    * 内插嵌在别处（``[name] & Eve``）—— **只换我们有默认名的那些**，其余原样留着：
      认不出来的内插（``[score]`` 这类）不该被当成主角名；
    * 没有内插 —— 原样返回。
    """
    text = declared.strip()
    if not text or "[" not in text:
        return declared
    bare = INTERPOLATION_NAME_RE.match(text)
    if bare:
        return defaults.get(bare.group(1)) or PROTAGONIST_LABEL
    return INTERPOLATION_RE.sub(
        lambda match: defaults.get(match.group(1)) or match.group(0), declared
    )


def _sha(text: str, length: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _tl_relpath(rel: str) -> str:
    """``game/script.rpy`` → ``script.rpy``（tl/<lang>/ 下的相对路径）。"""
    parts = PurePosixPath(rel).parts
    if parts and parts[0] == "game":
        parts = parts[1:]
        return PurePosixPath(*parts).as_posix() if parts else PurePosixPath(rel).name
    return PurePosixPath(rel).name


def discover_rpy_files(
    project_root: Path,
    globs: tuple[str, ...] = DEFAULT_FILE_GLOBS,
    excluded_parts: tuple[str, ...] = DEFAULT_EXCLUDED_PARTS,
) -> list[Path]:
    """按**适配器申报的内容范围**找出源脚本。

    以前这里是"全盘 ``rglob("*.rpy")`` 再减黑名单" —— 等于内核在猜什么算内容，
    而 Ren'Py 发行版恰好把整个运行时塞在游戏目录里。现在范围由支持包申报：
    内容是 ``game/**/*.rpy``，``tl/``（自己的产物）与 ``.gametrans/``（工作区）排除。
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in globs:
        for path in sorted(project_root.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            rel = path.relative_to(project_root)
            if any(part in excluded_parts for part in rel.parts):
                continue
            seen.add(path)
            found.append(path)
    return sorted(found)


def _sequence_edges(
    graph: PathGraph,
    facts: list[EdgeFact],
    report: RunReport | None = None,
    declared: set[str] | None = None,
    menu_owner: dict[str, str] | None = None,
) -> list[GraphEdge]:
    """把控制流事实变成 ``sequence`` 边（区域粒度）。

    为什么这条边可以直接参与排序：它不是启发式猜的"谁在谁之前" ——
    脚本里写着 ``jump act_two``，玩家就是从 act_one 走到 act_two；
    没有 ``jump`` 时**顺序落到下一个 label** 也是 Ren'Py 的语言语义
    （见 :mod:`gametrans.engines.renpy.controlflow`）。``provenance="engine"``
    如实标明来源是引擎自己的控制流。

    两件以前没做、真靶上撞出来的事：

    * **穿过"没有内容的 label"**：真靶上 ``jump protect_saki`` 指向的 label 存在，
      但它里面一句可译文本都没有 —— 按"端点必须是区域"的旧口径这条边会被**静默丢掉**。
      现在把无内容的 label 沿它自己的出边展开，接到它之后第一个有内容的区域上。
    * **悬空跳转如实报出来**：真靶 12 个跳转目标里有 11 个在整个工程里**根本没定义**
      （`textingthecats` 被跳 7 次）。这些边确实不该建，但不能悄悄没了 ——
      报成一条 warning，让人看得见。
    """
    regions = {region.region_id: region for region in graph.regions()}
    #: 「这个目标定义了没有」是**源码事实**（`label` 声明），不是"图里有没有它的容器"。
    #: 从容器推出来的话，容器一被动（比如删空壳）这里就会把有定义的 label 报成悬空 ——
    #: 实测过：删掉空壳容器之后，`questiontime1` / `protect_saki` 立刻被误报成没定义。
    defined = set(declared or ()) | {
        str(node.metadata.get("label"))
        for node in graph.nodes.values()
        if node.kind is NodeKind.LABEL and node.metadata.get("label")
    }
    outgoing: dict[str, list[EdgeFact]] = {}
    for fact in facts:
        outgoing.setdefault(fact.source, []).append(fact)

    def toward_owner(label: str) -> str:
        """具名菜单不是节点：指向它的跳转接到**拥有它的那场戏**上（R22）。

        引擎会为 `menu <名字>:` 生成一个空 label（`renpy/parser.py:700`），但节点＝一场戏
        —— 所以这个跳转等价于"跳进那场戏的中间"，接到拥有它的那个 label 上即可。
        """
        seen: set[str] = set()
        while menu_owner and label in menu_owner and label not in seen:
            seen.add(label)
            label = menu_owner[label]
        return label

    def resolve(label: str, seen: set[str] | None = None) -> set[str]:
        """把一个 label 展开成"它之后第一个**有内容**的区域"集合。"""
        label = toward_owner(label)
        if label in regions:
            return {label}
        seen = set() if seen is None else seen
        if label in seen or len(seen) >= MAX_CONTRACTION_HOPS:
            return set()
        seen.add(label)
        found: set[str] = set()
        for fact in outgoing.get(label, ()):
            found |= resolve(fact.target, seen)
        return found

    best: dict[tuple[str, str], GraphEdge] = {}
    dangling: dict[str, int] = {}
    for fact in facts:
        if fact.target not in defined and fact.target not in regions:
            dangling[fact.target] = dangling.get(fact.target, 0) + 1
        if fact.source not in regions:
            # 源 label 自己没有内容（空壳调度点）：它的关系由**它的前驱**穿过来表达
            # （前驱那条边会把这里当"穿过无内容的 label"继续往前解析）。从它再发一条
            # 只会把同一件事重复一遍 —— 真靶上那 25 条假边有一半是这么来的（R70）。
            continue
        sources = {fact.source}
        targets = resolve(fact.target)
        for source_region in sorted(sources):
            for target_region in sorted(targets):
                if source_region == target_region:
                    continue
                key = (source_region, target_region)
                note = fact.note or f"{fact.how}：{fact.source} → {fact.target}"
                if source_region != fact.source or target_region != fact.target:
                    note += "（穿过无内容的 label）"
                edge = GraphEdge(
                    source=source_region,
                    target=target_region,
                    type=EdgeType.SEQUENCE.value,
                    provenance="engine",
                    direction="control_flow",
                    note=note,
                )
                # 同一对区域可能由"显式跳转"和"顺序流"两条路都指向：
                # 保留**更确定**的那条（`fact.certain`），note 用显式跳转的写法。
                previous = best.get(key)
                if previous is None or (
                    fact.how != "fallthrough" and previous.note.startswith("fallthrough")
                ):
                    best[key] = edge
    if report is not None and dangling:
        report.add_issue(
            "warnings",
            code="jump_target_not_found",
            message=(
                f"{len(dangling)} 个跳转目标在整个工程里没有定义（例如 "
                f"{', '.join(sorted(dangling)[:5])}）—— 这些边不建，但如实报出来"
            ),
            detail={
                "targets": dict(sorted(dangling.items(), key=lambda kv: -kv[1])[:20]),
                "total": sum(dangling.values()),
            },
        )
    return sorted(best.values(), key=lambda e: (e.source, e.target))



def branch_edges(graph: PathGraph) -> list[GraphEdge]:
    """同一个菜单下**相邻**选项之间的 ``branch`` 边（Unit 级）。

    方向刻意留 ``reading_order``：并列分支之间没有先后，参与排序就会把并行度
    压成串行链。``provenance="engine"`` —— 菜单是引擎结构，不是我们猜的。

    **同一对选项只发一条**：菜单可以嵌套，同一组选项因此会被多个 MENU 容器各看到一次
    （真靶 Livingwithyou 上 22 对选项各被发了 2 遍）。那是一模一样的结构事实，重复的边
    会把分支度数算高一倍。
    """
    edges: list[GraphEdge] = []
    seen: set[tuple[str, str]] = set()
    for node in graph.nodes.values():
        if node.kind is not NodeKind.MENU:
            continue
        choices = [
            graph.nodes[child]
            for child in node.children
            if child in graph.nodes
            and graph.nodes[child].unit is not None
            and graph.nodes[child].kind is NodeKind.CHOICE
        ]
        for left, right in zip(choices, choices[1:]):
            assert left.unit is not None and right.unit is not None
            pair = (left.unit.id, right.unit.id)
            if pair in seen:
                continue
            seen.add(pair)
            edges.append(
                GraphEdge(
                    source=left.unit.id,
                    target=right.unit.id,
                    type=EdgeType.BRANCH.value,
                    provenance="engine",
                    direction="reading_order",
                    note="同一菜单下的并列选项",
                )
            )
    return edges


def _resolve_dynamic_jumps(
    dynamic: list[DynamicJump],
    assignments: dict[str, list[tuple[str, int, str]]],
    declared: set[str],
) -> tuple[list[EdgeFact], list[dict[str, Any]], list[DynamicJump]]:
    """给"目标不在语句里"的跳转做**常量传播**；补不出来的如实留着。

    判据（每条都可复核）：

    * 目标表达式**本身是字面量**（`Jump("act2")`）→ 它就是目标；
    * 目标表达式是**变量**，且全工程只有**唯一一个**字符串常量赋值 → 用那个值
      （真靶：`Jump(timer_jump)` + `$ timer_jump = 'frozeupsaki'`）；
    * 边的起点＝**赋值那一句所在的 label**（"玩家在哪里把这个值定下来的"）；
      字面量那种用跳转点所在的 label。

    补出来的边 ``certain=False``、note 里写明证据，并由调用方单独报一条
    `dynamic_jump_resolved_by_constant` —— 它是**静态推导**出来的，不是源码里写着的
    一句 `jump`，必须看得见。其余一律不猜。
    """
    unique: dict[str, tuple[str, int, str]] = {}
    for var, entries in assignments.items():
        distinct = {value for value, _line, _label in entries}
        if len(distinct) == 1:
            unique[var] = entries[0]
    facts: list[EdgeFact] = []
    resolved: list[dict[str, Any]] = []
    unresolved: list[DynamicJump] = []
    for site in dynamic:
        expression = site.expression.strip()
        target = expression.strip("'\"")
        source = site.source
        evidence = ""
        if site.literal:
            evidence = f"跳转目标就是字面量 {expression}"
        elif expression in unique:
            value, at, owner = unique[expression]
            target = value
            source = owner or site.source
            evidence = f"常量传播：{expression} = {value!r}（第 {at} 行）"
        else:
            unresolved.append(site)
            continue
        if not target or target not in declared or not source or source == target:
            unresolved.append(site)
            continue
        facts.append(
            EdgeFact(
                source,
                target,
                "screen-action" if site.how == "screen-action" else "constant",
                False,
                note=f"{evidence}；跳转点在第 {site.line} 行（{site.how}）",
            )
        )
        resolved.append(
            {
                **site.to_dict(),
                "target": target,
                "edge": f"{source} → {target}",
                "evidence": evidence,
            }
        )
    return facts, resolved, unresolved


def _report_dynamic_jumps(
    ctx: ExtractContext,
    unresolved: list[DynamicJump],
    resolved: list[dict[str, Any]] | None = None,
) -> None:
    """把"目标不在语句里"的跳转如实报出来（补出边的也要报，写明证据）。

    真靶 `frozeupsaki` 只能由倒计时屏幕 `Jump(timer_jump)` 到达，而
    `$ timer_jump = 'frozeupsaki'` 写在 `menu1` 体里 —— 读不出又不报，它就被当成
    "没有前驱"，排进第 0 批（登记册 R71）。"不猜"是对的，「不说」不是。
    """
    if resolved:
        ctx.report.add_issue(
            "warnings",
            code="dynamic_jump_resolved_by_constant",
            message=(
                f"{len(resolved)} 处跳转的目标不在语句里，按**常量传播**补出了边"
                "（证据在下面：变量 = 字面量，以及它写在哪一行）"
            ),
            detail={"count": len(resolved), "samples": resolved[:20]},
        )
    if not unresolved:
        return
    ctx.report.add_issue(
        "warnings",
        code="dynamic_jump_unresolved",
        message=(
            f"{len(unresolved)} 处跳转的目标是表达式（`jump/call expression …` 或屏幕动作），"
            "常量传播也补不出来：这些边不建，但如实报出来"
        ),
        detail={
            "count": len(unresolved),
            "samples": [item.to_dict() for item in unresolved[:20]],
        },
    )


class RenPyExtractor:
    """把一个 Ren'Py 工程抽成带权路径图。"""

    def extract(self, ctx: ExtractContext) -> PathGraph:
        project_root = ctx.project_root
        display_root = project_root.name
        builder = PathGraphBuilder("renpy")
        root_id = builder.add_container(display_root, NodeKind.ROOT)

        #: 控制流事实（含**顺序流**）：由 `controlflow.analyze_file` 从每个文件读出。
        #: 以前这里只收 `jump`，于是"顺序落到下一个 label"这条最大的流一条边都没有。
        flow: list[EdgeFact] = []
        #: **目标不在语句里**的跳转（`jump/call expression …`、屏幕动作 `Jump(…)`）：
        #: 能常量传播就补一条边，补不出来如实记账（R71）。
        dynamic: list[DynamicJump] = []
        #: 字符串常量赋值 ``变量 → [(字面量, 行号, 所在 label)]``：常量传播的输入
        assignments: dict[str, list[tuple[str, int, str]]] = {}
        #: 源码里声明过的 label —— 「目标定义了没有」的权威来源（不是图上的容器）。
        declared: set[str] = set()
        #: 具名菜单 → 拥有它的那个 label（跳转要接到那场戏上，不为菜单新建节点）。
        menu_owner: dict[str, str] = {}
        # 内容范围由支持包申报（`file_globs` / `excluded_parts`），提取器只是执行者
        globs = tuple(ctx.options.get("file_globs") or DEFAULT_FILE_GLOBS)
        excludes = tuple(ctx.options.get("excluded_parts") or DEFAULT_EXCLUDED_PARTS)
        files = discover_rpy_files(project_root, globs, excludes)
        if not files:
            ctx.report.warn(
                f"在 {project_root} 下没有找到任何 .rpy 文件",
                code="no_rpy_files",
            )

        for path in files:
            rel = path.relative_to(project_root).as_posix()
            file_display = f"{display_root}/{rel}"
            file_id = builder.add_container(
                rel,
                NodeKind.FILE,
                parent=root_id,
                metadata={"relpath": rel, "file": file_display},
            )
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                ctx.report.add_issue(
                    "failed",
                    code="unreadable_file",
                    message=f"无法读取 {rel}：{exc}",
                    ref=file_display,
                )
                continue
            try:
                self._extract_file(
                    builder, ctx, file_id, file_display, rel, text, flow, dynamic, declared,
                    menu_owner, assignments,
                )
            except Exception as exc:  # noqa: BLE001
                # 真实工程里总会有没见过的写法；一个文件解析炸了不该拖垮整次提取。
                # 但也不能悄悄咽下去 —— 记成 failed，让 agent 与用户都看得见。
                ctx.report.add_issue(
                    "failed",
                    code="extract_crashed",
                    message=f"{rel} 提取过程中出错：{type(exc).__name__}: {exc}",
                    ref=file_display,
                    detail={"relpath": rel},
                )

        graph = builder.build()
        self.apply_occurrences(graph)
        # 目标不在语句里的跳转：**先做常量传播**（能补出唯一目标就补一条边），
        # 补出来的边和手写的 jump 一起交给 `_sequence_edges` 变成 `sequence` 边。
        resolved_facts, resolved, unresolved = _resolve_dynamic_jumps(
            dynamic, assignments, declared
        )
        flow.extend(resolved_facts)
        # 控制流先后：源码里写着的 jump/call，**加上顺序落到下一个 label 的那条流**
        # （后者是 Ren'Py 的语言语义，以前一条都没建 —— 真靶 38 个 label 里 29 个会顺序流）
        graph.dependencies.extend(
            _sequence_edges(graph, flow, ctx.report, declared, menu_owner)
        )
        # 并列分支：同菜单下的相邻选项。**这是引擎结构知识**，所以由适配器声明，
        # 内核不再靠 NodeKind.MENU/CHOICE 去推导（见 tests/test_engine_decoupling.py）
        graph.dependencies.extend(branch_edges(graph))
        _report_dynamic_jumps(ctx, unresolved, resolved)
        return graph

    # ---- 单文件 -------------------------------------------------------------

    def _extract_file(
        self,
        builder: PathGraphBuilder,
        ctx: ExtractContext,
        file_id: str,
        file_display: str,
        rel: str,
        text: str,
        flow: list[EdgeFact] | None = None,
        dynamic: list[DynamicJump] | None = None,
        declared: set[str] | None = None,
        menu_owner: dict[str, str] | None = None,
        assignments: dict[str, list[tuple[str, int, str]]] | None = None,
    ) -> None:
        statements = parse_rpy(text)
        # 控制流事实（含顺序流）由专门的模块读 —— 引擎语义住在一处，
        # 而且它是**对任何 Ren'Py 工程都成立**的规则，不是给某个靶子打补丁。
        facts = analyze_file(statements)
        if flow is not None:
            flow.extend(facts.facts)
        if dynamic is not None:
            dynamic.extend(facts.dynamic)
        if declared is not None:
            declared.update(facts.labels)
        if assignments is not None:
            for var, entries in facts.string_assignments.items():
                assignments.setdefault(var, []).extend(entries)
        lines = text.splitlines()
        tl_rel = _tl_relpath(rel)
        #: 同一 (文件, label, 类型, 原文) 在本文件里第几次出现 —— 只用来给完全相同的
        #: 文本去重，**不含位置信息**。这样在别处插一行不会改掉任何已有 id。
        key_counts: dict[str, int] = {}
        #: 角色变量 → 显示名（``define e = Character("Eileen")`` 给出 e→Eileen）。
        #: 上游（依赖图、提示词）要的是**人名**；写回要的是**变量名**（原脚本里就是
        #: ``e "..."``）。两个都留着，各给各的用。
        #:
        #: 主角常常是**运行期内插**（``Character("[name]")``）：这里按游戏自己声明的
        #: 默认名解析（见 :func:`_declared_defaults`），没声明就用「主角」。不解析的话
        #: 摘要与提示词里一直显示 ``[name]``，读不出是谁在说话。
        declared_defaults = _declared_defaults(text)
        characters: dict[str, str] = {
            str(stmt.name): _display_name(str(stmt.text), declared_defaults)
            for stmt in statements
            if stmt.kind == "define_character" and stmt.name and stmt.text
        }
        if characters:
            # 落盘成图级事实：读原始骨架的消费者（摘要脚本）拿到的是变量名，
            # 只有这张表能把它换成显示名。
            builder.metadata.setdefault("characters", {}).update(characters)

        label_spans = self._label_spans(statements, len(lines))

        current_label: str | None = None
        current_label_id: str | None = None
        current_menu_id: str | None = None
        menu_indent = 0
        menu_index = 0
        seen_labels: set[str] = set()
        seq = 0
        unsupported_seq = 0

        for stmt in statements:
            # 离开菜单块：缩进退回菜单层级即视为菜单结束
            if current_menu_id is not None and stmt.indent <= menu_indent:
                current_menu_id = None

            if stmt.kind == "label":
                # 真实工程里偶见同名 label；重定义时用行号区分，保证 id 与 path 都唯一
                segment = f"#{stmt.name}"
                if stmt.name in seen_labels:
                    segment = f"#{stmt.name}@{stmt.line}"
                seen_labels.add(str(stmt.name))
                current_label = stmt.name
                current_label_id = builder.add_container(
                    segment,
                    NodeKind.LABEL,
                    parent=file_id,
                    separator="",
                    metadata={"label": stmt.name, "line": stmt.line},
                )
                current_menu_id = None
                menu_index = 0
                continue

            if stmt.kind == "menu":
                if current_label_id is None:
                    continue
                # 一个 label 里出现多个 menu 是真实工程的常态 —— 必须带序号，
                # 否则节点 id 会撞车（这正是拿真实游戏试出来的第一个崩溃）。
                # **具名菜单**（`menu evequestions:`）：名字是引擎认的 label，必须留着
                # ——它是合法跳转目标、引擎给它的文本块 id 也用它当前缀；但它不因此
                # 成为一个节点（节点＝一场戏），所以只记在容器元数据与 menu_owner 里。
                meta: dict[str, object] = {"line": stmt.line}
                if stmt.name:
                    meta["menu"] = stmt.name
                    if current_label:
                        menu_owner[str(stmt.name)] = current_label
                    declared.add(str(stmt.name))
                current_menu_id = builder.add_container(
                    f"menu[{menu_index}]",
                    NodeKind.MENU,
                    parent=current_label_id,
                    metadata=meta,
                )
                menu_index += 1
                menu_indent = stmt.indent
                continue

            if stmt.kind in ("define_character", "define_string"):
                unit_kind = "definition" if stmt.kind == "define_character" else "string"
                seq += 1
                self._add_unit(
                    builder, file_display, rel, tl_rel, None, unit_kind, stmt, key_counts, characters,
                    defaults=declared_defaults,
                    parent=file_id, span=(0, 0),
                )
                continue

            if stmt.kind == "strings_old":
                seq += 1
                self._add_unit(
                    builder, file_display, rel, tl_rel, None, "string", stmt, key_counts, characters,
                    defaults=declared_defaults,
                    parent=file_id, span=(0, 0),
                )
                continue

            if stmt.kind == "structural":
                # 看得出不是待译文本（键/属性名/路径/模板），但仍如实记账 ——
                # 只是记在 skipped 而不是 unsupported 里，别把真正的缺口淹掉
                ctx.report.add_issue(
                    "skipped",
                    code=stmt.meta.get("reason", "structural_string"),
                    message=(
                        f"{file_display}:{stmt.line} 处是标识符/键/路径一类的结构性字符串，"
                        "不是待译文本，已跳过"
                    ),
                    ref=f"{file_display}:{stmt.line}",
                    detail={
                        "file": file_display,
                        "line": stmt.line,
                        "text": stmt.text,
                        "raw": stmt.raw,
                    },
                )
                continue

            if stmt.kind == "jump":
                # 控制流由 `controlflow.analyze_file` 统一读（含顺序流），这里只跳过：
                # 同一件事只有一个住址，免得"两处都在收 jump"以后对不上。
                continue

            if stmt.kind == "unsupported":
                unsupported_seq += 1
                token = stmt.name or f"line{stmt.line}"
                node_id = f"unsupported:{Path(rel).stem}:{token}"
                if unsupported_seq > 1:
                    node_id = f"{node_id}#{unsupported_seq}"
                builder.add_unsupported(
                    node_id,
                    path=f"unsupported[{token}]",
                    parent=file_id,
                    reason=stmt.meta.get("reason", ""),
                    metadata={"file": file_display, "line": stmt.line, "text": stmt.text},
                )
                ctx.report.add_issue(
                    "unsupported",
                    code=stmt.meta.get("reason", "unclassified_string"),
                    message=(
                        f"{file_display}:{stmt.line} 处含字符串字面量，"
                        "本期无法安全提取，已跳过（内容保持原样）"
                    ),
                    ref=node_id,
                    detail={
                        "file": file_display,
                        "line": stmt.line,
                        "text": stmt.text,
                        "raw": stmt.raw,
                    },
                )
                continue

            if stmt.kind in ("say", "choice"):
                if current_label_id is None:
                    ctx.report.add_issue(
                        "skipped",
                        code="statement_outside_label",
                        message=f"{file_display}:{stmt.line} 的文本不在任何 label 内，已跳过",
                        ref=file_display,
                    )
                    continue
                if stmt.kind == "choice":
                    parent = current_menu_id or current_label_id
                else:
                    parent = (
                        current_menu_id
                        if current_menu_id is not None and stmt.indent > menu_indent
                        else current_label_id
                    )
                seq += 1
                self._add_unit(
                    builder, file_display, rel, tl_rel, current_label, stmt.kind, stmt,
                    key_counts, characters,
                    parent=parent,
                    defaults=declared_defaults,
                    span=label_spans.get(current_label or "", (0, 0)),
                )

    # ---- 辅助 ---------------------------------------------------------------

    def _add_unit(
        self,
        builder: PathGraphBuilder,
        file_display: str,
        rel: str,
        tl_rel: str,
        label: str | None,
        unit_kind: str,
        stmt: RpyStatement,
        key_counts: dict[str, int],
        characters: dict[str, str],
        *,
        parent: str,
        span: tuple[int, int],
        defaults: dict[str, str] | None = None,
    ) -> str:
        # id 只由**内容**决定：项目根相对路径 + label + 类型 + 原文（+ 同文本的第几次出现）。
        # 刻意不含项目目录名与语句位置 —— 否则改目录名或在前面插一行，已存的译文就全失联了。
        key = f"{rel}|{label}|{unit_kind}|{stmt.text}"
        occurrence = key_counts.get(key, 0)
        key_counts[key] = occurrence + 1
        unit_id = f"{unit_kind}_{_sha(f'{key}|{occurrence}')}"
        raw = stmt.text or ""
        # 提示词与依赖图看到的是人名；写回用的是 payload 里的变量名。
        # 引号说话人本身就是**显示名**（引擎原样显示），它不在角色表里，
        # 但它是这条文本的角色 —— 照样进 characters。它里面嵌的内插（`"[name] & Eve"`）
        # 同样要按游戏声明的默认名解析，否则摘要里会漏出 `[name]`。
        quoted = bool(stmt.meta.get("speaker_quoted"))
        speaker_name = characters.get(stmt.speaker or "", stmt.speaker)
        if quoted and speaker_name:
            speaker_name = _display_name(speaker_name, defaults or {})
        if stmt.speaker in characters:
            speaker_characters = [characters[stmt.speaker]]
        elif quoted and speaker_name:
            speaker_characters = [speaker_name]
        else:
            speaker_characters = []
        unit = TranslationUnit.from_text(
            id=unit_id,
            type=unit_kind,
            source=raw,
            scanner=segmentize,
            locator=Locator(
                # Locator.file 是**项目根相对**路径：核心据此就能验证"这个文件存在"，
                # 不需要理解任何引擎语义。展示用的完整路径留在 payload 里。
                file=rel,
                line=stmt.line,
                kind="line",
                payload={
                    # —— 以下全部是 Ren'Py 专属信息，内核从不解释，只在写回时原样交还 ——
                    "file": file_display,
                    "display_path": file_display,
                    "relpath": rel,
                    "tl_relpath": tl_rel,
                    "line": stmt.line,
                    "indent": stmt.indent,
                    "label": label,
                    "label_start": span[0],
                    "label_end": span[1],
                    "kind": unit_kind,
                    "speaker": stmt.speaker,
                    "define_target": stmt.name,
                    "raw_line": stmt.raw,
                },
            ),
            context=Context(
                # 提示词与依赖图看到的是人名；写回用的是 payload 里的变量名
                speaker=speaker_name,
                characters=speaker_characters,
                scene=label,
                note=f"label {label}" if label else Path(rel).stem,
            ),
        )
        return builder.add_unit(
            unit_id,
            unit,
            parent=parent,
            kind=NODE_KIND.get(unit_kind, NodeKind.STRING),
        )

    @staticmethod
    def _label_spans(
        statements: list[RpyStatement], total_lines: int
    ) -> dict[str, tuple[int, int]]:
        """算出每个 label 的正文行区间（不含 ``label`` 那一行）。"""
        labels = [s for s in statements if s.kind == "label"]
        spans: dict[str, tuple[int, int]] = {}
        for index, stmt in enumerate(labels):
            start = stmt.line + 1
            if index + 1 < len(labels):
                end = labels[index + 1].line - 1
            else:
                end = total_lines
            if stmt.name:
                spans[stmt.name] = (start, end)
        return spans

    @staticmethod
    def apply_occurrences(graph: PathGraph) -> None:
        """把"同一句话在多处出现"这件事写进权重，供翻译层与 agent 参考。

        一个单元含多句之后，"单元原文"是拼接出来的，逐单元比原文已经比不出重复
        （实测：同一句出现在两场戏里，两个单元的 occurrences 都成了 1）。
        所以按**槽位**数：单元的 occurrences 取它成员里最高的那个计数。
        """
        counts: Counter[str] = Counter()
        for node in graph.nodes.values():
            if node.unit is None:
                continue
            keys = node.unit.metadata.get("slot_keys") or []
            entries = (node.unit.locator.payload or {}).get("slots") or []
            for entry in entries:
                source = str(entry.get("source") or "")
                if not source and entry.get("slot_key") in keys:
                    source = str(node.unit.metadata.get("raw_source") or "")
                if source:
                    counts[source] += 1
        if not counts:
            return
        for node in graph.nodes.values():
            if node.unit is None:
                continue
            best = 1
            for entry in (node.unit.locator.payload or {}).get("slots") or []:
                source = str(entry.get("source") or "")
                if source:
                    best = max(best, counts[source])
            if best > 1:
                node.weight = replace(node.weight, occurrences=best)
