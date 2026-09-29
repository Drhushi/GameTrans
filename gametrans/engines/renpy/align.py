"""把官方骨架的槽位缝回源码结构 —— **内容归引擎，结构归我们**。

## 它解决什么

官方工具给的是"哪些文本要翻、它们的 id 是什么、它们在源文件第几行"（一份平铺清单），
给不出叙事结构。源码读得出结构（label 区间、菜单归属、控制流），但读不出"哪些文本
算内容"（`screen` 文本、`_()` 字面量它都会漏，见契约 §1.1.1）。

两边的坐标系是同一个：``(源文件, 行号)``。这个模块把槽位按那个坐标缝到源码结构上：

* 缝上了 → 槽位挂进它该在的 label / menu 里，节点类型与优先级沿用源码读到的结构；
* 缝不上 → **仍然是要翻的内容**，挂在文件节点上并报 ``unaligned_slot``；
* 源码里有、引擎清单里没有 → 那不是内容（引擎才是权威），报 ``not_in_engine_list``。

## 分层

模块在**适配层**：它认识骨架格式（经 :mod:`~gametrans.engines.renpy.skeleton`）
与源码语法（经 :mod:`~gametrans.engines.renpy.extractor`），两者都是引擎私有知识。
交给内核的仍然只有统一形状的图与单位。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.bindings import USABLE_STATUS, SlotBindings, UnitBinding
from gametrans.core.graph import PathGraph, PathGraphBuilder
from gametrans.core.models import (
    Context,
    GraphEdge,
    Locator,
    NodeKind,
    PathNode,
    Scanner,
    TranslationArtifact,
    TranslationStatus,
    TranslationUnit,
)
from gametrans.core.report import RunReport
from gametrans.core.slots import Slot, SlotSet
from gametrans.core.units import GroupByStructureKey, assemble_units
from gametrans.engines.renpy.extractor import RenPyExtractor

__all__ = ["Alignment", "align_graph", "bind_translations"]

#: 引擎自己的形态类名 → 内核的类型词汇（契约 R4：归一化是适配层的活）
TYPE_BY_CLASS: dict[str, str] = {
    "TranslateSay": "say",
    "TranslateString": "string",
    "TranslateBlock": "block",
}

#: 引擎的形态类名 → 节点类别（内核的类别词汇里没有 "block"，按不可分的整块文本算）
KIND_BY_CLASS: dict[str, NodeKind] = {
    "TranslateSay": NodeKind.SAY,
    "TranslateString": NodeKind.STRING,
    "TranslateBlock": NodeKind.STRING,
}

#: 诊断里最多列几条样本：真实工程上缝不上的可能有几百条，全列出来只会淹掉别的信息
SAMPLE_LIMIT = 20

#: **Ren'Py 自带的界面 / 开发工具文件** —— 不是这款游戏的剧本。
#:
#: 它们之所以出现在 `game/` 里，是因为发行版把引擎自己的工具文件也打进去了
#: （真靶工程里 `ActionEditor.rpy` 161KB、`ActionEditor_screens.rpy` 161KB 就是
#: Ren'Py 的动作编辑器）。Ren'Py 官方文档对同一类东西有明确说法：`tl/None/common.rpym`
#: 里的内置字符串"**你的工程代码里找不到它们**"，却仍会随游戏发行。
#:
#: 这条事实用来回答"这段文本属不属于游戏的叙事内容"。**它不影响内容范围** ——
#: 界面文本照样要翻（内容范围由引擎的字符串表给，§0.1），它只影响**产不产资产**
#: （术语候选 / 世界书候选）。真靶上只出现在这 7 个文件里的候选有 17 条
#: （`ActionEditor`、`warper`、`spline editor`、`Legacy GUI`、`Ren'Py 界面文本`…）。
#:
#: 判据用**文件名**，是刻意的：这几个名字是 Ren'Py 工程模板与它自带工具的文件名，
#: 是"引擎侧的事实"；用"文本里出现 ActionEditor 这个词"之类的启发式会把
#: 游戏里恰好提到编辑器的那句对白也误伤。
INTERFACE_FILES = frozenset(
    {
        "ActionEditor.rpy",
        "ActionEditor_screens.rpy",
        "ActionEditor_config.rpy",
        "screens.rpy",
        "ui.rpy",
        "options.rpy",
        "gui.rpy",
        "keymap.rpy",
        "menu_screen.rpy",
        "sound_viewer.rpy",
        "image_viewer.rpy",
    }
)

#: 内容类别的两个值（内核只按申报判，不认识引擎的文件名）
CONTENT_STORY = "story"
CONTENT_INTERFACE = "interface"


def content_class_for_file(relpath: str) -> str:
    """这个**源文件**里的是叙事内容还是界面 / 开发工具（适配层申报的事实）。"""
    name = str(relpath or "").replace("\\", "/").rsplit("/", 1)[-1]
    return CONTENT_INTERFACE if name in INTERFACE_FILES else CONTENT_STORY


@dataclass
class Alignment:
    """一次对齐的结果。"""

    graph: PathGraph
    #: 缝到源码结构上的槽位数
    aligned: int = 0
    #: 引擎给了、但源码里找不到对应位置的槽位
    unaligned: list[dict[str, Any]] = field(default_factory=list)
    #: 归属由**引擎声明**给出（块 id 前缀 / 容器 / label 区间），但源码里没有对应位置
    #: —— 内容照翻、归属不丢，可它是"骨架与源码不同步"的信号，必须看得见
    no_source_position: list[dict[str, Any]] = field(default_factory=list)
    #: 源码里有、引擎清单里没有的文本（它们不是内容）
    not_in_engine_list: list[dict[str, Any]] = field(default_factory=list)
    #: 源码读出来的依赖边里，端点在新图上不存在的（如实记账，不留在图上）
    dropped_edges: int = 0

    def metrics(self) -> dict[str, Any]:
        return {
            "aligned_slots": self.aligned,
            "unaligned_slots": len(self.unaligned),
            "slots_without_source_position": len(self.no_source_position),
            "not_in_engine_list": len(self.not_in_engine_list),
            "dropped_edges": self.dropped_edges,
        }


def _relpath(file: str, project_root: Path) -> str:
    """把引擎给的路径写法归一到**项目根相对**的 POSIX 路径。

    实测引擎对同一批内容会给两种写法（相对路径 / 绝对路径，见契约 C2b），
    不归一会让同一个文件被劈成两处，槽位就缝不上源码结构。
    """
    text = str(file or "").replace("\\", "/").strip()
    root = str(Path(project_root)).replace("\\", "/").rstrip("/")
    if root and text.lower().startswith(root.lower() + "/"):
        text = text[len(root) + 1 :]
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if parts and parts[0].endswith(":"):
        parts = parts[1:]
    return "/".join(parts)


def _node_relpath(node: PathNode, project_root: Path) -> str:
    payload = node.unit.locator.payload if node.unit and node.unit.locator else {}
    raw = str(payload.get("relpath") or (node.unit.locator.file if node.unit and node.unit.locator else ""))
    return _relpath(raw, project_root)


def _container_relpath(node: PathNode, graph: PathGraph, project_root: Path) -> str:
    """容器属于哪个文件：往上找到 FILE 节点，读它申报的 relpath。"""
    current = node
    seen: set[str] = set()
    while current is not None and current.node_id not in seen:
        if current.kind is NodeKind.FILE:
            return _relpath(str(current.metadata.get("relpath") or current.path), project_root)
        seen.add(current.node_id)
        current = graph.nodes.get(current.parent) if current.parent else None
    return ""


def _container_ancestors(
    structure: PathGraph, start_id: str | None
) -> list[PathNode]:
    """从 ``start_id`` 往上，按"近→远"取全部容器结点（不含可译叶）。"""
    chain: list[PathNode] = []
    seen: set[str] = set()
    current_id = start_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        node = structure.nodes.get(current_id)
        if node is None:
            break
        if node.kind is not NodeKind.UNSUPPORTED:
            chain.append(node)
        current_id = node.parent
    return chain


def _character_names(structure: PathGraph, project_root: Path) -> dict[str, str]:
    """``角色变量 → 显示名``（``define d = Character("Desiree")`` 给出 ``d → Desiree``）。

    从**源码结构图**里的定义结点读：变量名在 ``locator.payload["define_target"]``，
    显示名就是这条定义结点的原文。注意内容图里没有这些结点 —— 引擎的可译清单不含角色
    定义，对齐之后它们不会出现在产物图上（真靶实测：内容图 0 个 definition 结点，
    源码图 26 个）。
    """
    names: dict[str, str] = {}
    for node in structure.nodes.values():
        if node.kind is not NodeKind.DEFINITION or node.unit is None:
            continue
        payload = node.unit.locator.payload if node.unit.locator else {}
        key = str(payload.get("define_target") or "").strip()
        value = str(node.unit.source or "").strip()
        if key and value and key not in names:
            names[key] = value
    return names


def _text_order_pairs(
    leaves: list[PathNode],
    slots: list[Slot],
    by_line: dict[tuple[str, int], PathNode],
    by_text: dict[tuple[str, str], list[PathNode]],
    project_root: Path,
) -> dict[str, PathNode]:
    """行号对不上的那些，按**原文 + 出现次序**一一配上。

    只在**同文件同原文**范围内配对，跨文件不配 —— 跨文件会让归属错得无声无息。

    这是**应急**：真靶上骨架是脏的（锚点过期）时才用得上。骨架一旦用官方工具重生成，
    99% 的槽位由 `line` 直接命中，这条路只剩零星几条（该工程实测 1 条）。
    """
    by_key: dict[tuple[str, str], list[PathNode]] = {}
    for node in leaves:
        rel = _node_relpath(node, project_root)
        by_key.setdefault((rel, node.unit.source), []).append(node)
    for nodes in by_key.values():
        nodes.sort(key=lambda item: int(item.unit.locator.line if item.unit.locator else 0))

    pending: dict[tuple[str, str], list[Slot]] = {}
    for slot in slots:
        rel = _relpath(slot.file, project_root)
        line = int(slot.line or 0)
        if (rel, line) in by_line:
            continue  # 行号已经能对上，不必走这一步
        pending.setdefault((rel, slot.source), []).append(slot)
    for members in pending.values():
        members.sort(key=lambda item: int(item.line or 0))

    pairs: dict[str, PathNode] = {}
    for key, candidates in by_key.items():
        waiting = pending.get(key)
        if not waiting:
            continue
        for slot, node in zip(waiting, candidates):
            pairs[slot.key] = node
    return pairs


def _label_region_node(structure: PathGraph, label: PathNode) -> PathNode:
    """label 之上的**剧本级区域**（顶层 label 容器 / 文件）。

    引擎自己的跳转边挂在顶层结构上（`jump act2`），单元结点则挂在每个场景 label 下，
    所以两者之间需要这一层来对齐 —— 否则"单元之间的关系"就只能停在 label 名字上。
    """
    current = structure.nodes.get(label.parent) if label.parent else None
    top = label
    seen = {label.node_id}
    while current is not None and current.node_id not in seen:
        seen.add(current.node_id)
        if current.kind in (NodeKind.FILE, NodeKind.ROOT):
            return top
        top = current
        current = structure.nodes.get(current.parent) if current.parent else None
    return top


def _owning_label_name(structure: PathGraph, node: PathNode | None) -> str | None:
    """往上找最近的 `label`，返回它的名字。

    菜单 / 文件这类容器都靠它归到一场戏上：**节点＝一场戏**，菜单是这场戏内部的东西。
    """
    seen: set[str] = set()
    current = node
    while current is not None and current.node_id not in seen:
        seen.add(current.node_id)
        if current.kind is NodeKind.LABEL and current.metadata.get("label"):
            return str(current.metadata["label"])
        current = structure.nodes.get(current.parent) if current.parent else None
    return None


def _engine_label_name(
    engine_id: str | None,
    labels_by_name: dict[str, str],
    menu_owner: dict[str, str],
) -> str | None:
    """引擎块 id 的前缀 → 归属的 label 名（拿不到就返回 ``None``）。

    引擎写的翻译块 id 就是 ``<label>_<8 位 md5>``（真游戏随包
    ``renpy/translation/__init__.py:317``：``base = label.replace(".", "_") + "_" + digest``），
    所以识别型槽位**不必靠行号配对去猜**归属 —— 引擎已经写在 id 里了。
    具名菜单虽然不是节点，但它的 id 前缀是菜单名，要顺着 `menu_owner` 找到那场戏。
    """
    text = str(engine_id or "")
    if "_" not in text:
        return None
    prefix = text.rsplit("_", 1)[0]
    #: 同名 id 会带数字尾缀（`unique_identifier` → `act1_a20cefa7_1`）：先剥掉再认。
    if prefix.rsplit("_", 1)[-1].isdigit() and "_" in prefix:
        prefix = prefix.rsplit("_", 1)[0]
    if prefix in labels_by_name:
        return prefix
    return menu_owner.get(prefix)


def _structure_coordinate(
    structure: PathGraph,
    slot: Slot,
    rel: str,
    parent: str | None,
    root_id: str | None,
) -> tuple[str, str, str]:
    """一条槽位属于哪个**翻译单元**：返回 ``(边界键, 单元名, 所属剧本级区域)``。

    **一个 label 就是一个单元**：边界键取 **label 名本身**（`label:` 那一行的位置，
    也就是 `_label_region_id` 的口径）；label 下层的 `menu` 只记进
    ``structure_label``（形如 ``label/menu[1]``），**不再单独成单元**。

    为什么不让菜单切开单元：菜单分支多的时候，一个场景会被切成许多小单元，
    "单元之间的关系"就只能在那些小单元之间说，层级反而更乱。菜单的位置信息留着
    （`structure_label`），要按分支细分时随时能读出来。
    """
    chain = _container_ancestors(structure, parent or root_id)
    label = next((node for node in chain if node.kind is NodeKind.LABEL), None)
    if label is not None:
        name = str(label.metadata.get("label") or label.path.rsplit("#", 1)[-1] or label.node_id)
        region = _label_region_node(structure, label)
        region_name = str(
            region.metadata.get("label") or region.path.rsplit("#", 1)[-1] or region.node_id
        )
        menus = [
            str(menu.path.rsplit("/", 1)[-1])
            for menu in reversed([item for item in chain if item.kind is NodeKind.MENU])
        ]
        readable = name + ("/" + "/".join(menus) if menus else "")
        return name, readable, region_name
    return rel, rel, rel


def _common_parent(structure: PathGraph, parents: list[str | None]) -> str | None:
    """能覆盖全部成员的最深容器 —— 单元挂在它下面，归属不撒谎。"""
    chains: list[list[str]] = []
    for parent in parents:
        if parent is None:
            return None
        chains.append([node.node_id for node in _container_ancestors(structure, parent)])
    if not chains:
        return None
    shared: set[str] = set(chains[0])
    for chain in chains[1:]:
        shared &= set(chain)
    for node_id in chains[0]:  # 近→远，第一个共享的就是最深的那层
        if node_id in shared:
            return node_id
    return None


def _merge_context(matched: list[PathNode], members: list[dict[str, Any]]) -> Context:
    """单元的结构上下文 = 成员里**缝上源码的那些**给出的并集。

    只从真正缝上的成员取（缝不上的没有结构信息可给，不许从旁边的成员身上"借"）。
    """
    speakers: list[str] = []
    scenes: list[str] = []
    notes: list[str] = []
    characters: list[str] = []
    for node in matched:
        context = node.unit.context if node.unit is not None else None
        if context is None:
            continue
        if context.speaker and context.speaker not in speakers:
            speakers.append(str(context.speaker))
        if context.scene and context.scene not in scenes:
            scenes.append(str(context.scene))
        if context.note and context.note not in notes:
            notes.append(str(context.note))
        for name in context.characters:
            if name not in characters:
                characters.append(str(name))
    if not speakers:
        for member in members:
            variable = str(member.get("variable") or "")
            if variable and variable not in speakers:
                speakers.append(variable)
    return Context(
        speaker=speakers[0] if len(speakers) == 1 else None,
        characters=characters or speakers,
        scene=scenes[0] if scenes else None,
        note="；".join(notes[:1]),
    )


def _single(values: Iterable[str]) -> str | None:
    """一组取值里唯一的那一个；不唯一就没有单一取值（不编一个代表）。"""
    unique = sorted({value for value in values if value})
    return unique[0] if len(unique) == 1 else None


def _classify(slot: Slot, matched: PathNode | None) -> tuple[NodeKind, str]:
    """节点类别 / 单位类型。

    缝上了就沿用**源码读到的结构**（菜单选项是 choice、定义是 definition），
    缝不上就只用引擎给的形态类名 —— 拿不到就按字符串算，不编细节。
    """
    if matched is not None and matched.unit is not None:
        kind = matched.kind
        unit_type = matched.unit.type
    else:
        name = str(slot.node_class or "")
        kind = KIND_BY_CLASS.get(name, NodeKind.STRING)
        unit_type = TYPE_BY_CLASS.get(name, "string")
    return kind, unit_type


def align_graph(
    structure: PathGraph,
    slots: SlotSet,
    *,
    project_root: Path,
    report: RunReport | None = None,
    scanner: Scanner | None = None,
) -> Alignment:
    """按 ``(源文件, 行号)`` 把 ``slots`` 缝进 ``structure``，产出内容的图。

    ``structure`` 是源码读出来的结构图（容器 + 源码侧的可译叶，后者只当**线索**用）；
    ``slots`` 是引擎给的内容清单，**它才是内容范围**。
    """
    builder = PathGraphBuilder(structure.engine)
    # 容器照搬：结构事实来自源码，槽位只是往上挂
    for node_id in structure.walk():
        node = structure.nodes[node_id]
        if node.unit is not None or node.kind is NodeKind.UNSUPPORTED:
            continue
        builder.adopt(node)

    leaves = [node for node in structure.nodes.values() if node.unit is not None]
    by_line: dict[tuple[str, int], PathNode] = {}
    by_text: dict[tuple[str, str], list[PathNode]] = {}
    for node in leaves:
        rel = _node_relpath(node, project_root)
        by_line.setdefault((rel, int(node.unit.locator.line if node.unit.locator else 0)), node)
        by_text.setdefault((rel, node.unit.source), []).append(node)

    containers: dict[tuple[str, int], str] = {}
    for node in structure.nodes.values():
        if node.kind not in (NodeKind.LABEL, NodeKind.MENU):
            continue
        line = node.metadata.get("line")
        rel = _container_relpath(node, structure, project_root)
        if rel and line:
            containers[(rel, int(line))] = node.node_id

    #: **归属的权威序**（内容归引擎，结构也归引擎）：
    #:
    #: ① **引擎块 id 的前缀就是 `<label>_<hash>`**（真游戏随包
    #:    `renpy/translation/__init__.py:317`：`base = label + "_" + digest`）。
    #:    识别型槽位的归属**引擎已经写在 id 里**，不必靠行号配对去猜 ——
    #:    真靶 292 条缝不上的槽位里有 78 条正是这一类，它们的 id 前缀 100% 对得上
    #:    真实 label / 具名菜单（`act13_…`、`textingthecats_…`）。
    #: ② 拿不到 id（字符串表 keyed）时，看 `# file:line` 落在哪个容器（label / menu）。
    #: ③ 再没有就取同文件里**行号在它之前最近的那个 label**。
    #: ④ 整个文件都没有 label（界面 / 工具文件）才用文件当结构键 —— 那不是"散句"，
    #:    那是这个文件自己的字符串表（引擎也是按文件分块的）。
    labels_by_name: dict[str, str] = {}
    label_of_line: dict[str, list[tuple[int, str]]] = {}
    for node in structure.nodes.values():
        name = str(node.metadata.get("label") or "") if node.kind is NodeKind.LABEL else ""
        if not name:
            continue
        labels_by_name.setdefault(name, node.node_id)
        rel = _container_relpath(node, structure, project_root)
        line = int(node.metadata.get("line") or 0)
        if rel and line:
            label_of_line.setdefault(rel, []).append((line, name))
    for entries in label_of_line.values():
        entries.sort()
    #: 具名菜单 → **拥有它的那个 label**（具名菜单在引擎里是个空 label，但节点＝一场戏，
    #: 所以它里面的文本与指向它的跳转都归到那场戏上）。
    menu_owner: dict[str, str] = {}
    for node in structure.nodes.values():
        name = str(node.metadata.get("menu") or "") if node.kind is NodeKind.MENU else ""
        if name:
            owner = _owning_label_name(structure, node)
            if owner:
                menu_owner[name] = owner

    file_nodes: dict[str, str] = {}
    for node in structure.nodes.values():
        if node.kind is NodeKind.FILE:
            file_nodes[_relpath(str(node.metadata.get("relpath") or node.path), project_root)] = node.node_id

    root_id = structure.roots[0] if structure.roots else None
    # 角色变量 → 显示名：提示词里给人名，不给脚本变量名（见下面的 display_speaker）。
    # 提取层已经解析过"运行期内插"的显示名（`Character("[name]")` → 游戏声明的默认名
    # 或「主角」），并把它作为图级事实带过来；没有时才退回从定义结点现推。
    characters = dict(structure.metadata.get("characters") or {})
    if not characters:
        characters = _character_names(structure, project_root)
    builder.metadata["characters"] = dict(characters)
    builder.metadata.setdefault("defaults", dict(structure.metadata.get("defaults") or {}))

    # 行号对不上的那些：按"原文 + 出现次序"先配好（见 _text_order_pairs 的说明）
    text_pairs = _text_order_pairs(
        leaves, slots.all(), by_line, by_text, project_root
    )

    # ---- 1) 先给每条槽位定位，并记下它的**结构坐标** -------------------------
    # 结构坐标 = 最近的 label（没有 label 就退到文件）＋ 其下的 menu。
    # 它决定"哪几条槽位属于同一个翻译单元"，也就是图上的一条文本。
    # 按行号/原文认槽位这件事与从前一样，变的是组装粒度：一个结构段 = 一个单位。
    placements: dict[str, dict[str, Any]] = {}
    unaligned: list[dict[str, Any]] = []
    no_source_position: list[dict[str, Any]] = []
    aligned = 0
    claimed: set[str] = set()
    for slot in slots.all():
        rel = _relpath(slot.file, project_root)
        line = int(slot.line or 0)

        matched: PathNode | None = None
        how = ""
        candidate = by_line.get((rel, line))
        if candidate is not None:
            # **一行可以对应多条槽位**：引擎在同一个 say 语句上给"说话人名 + 台词"两条，
            # 在菜单那一行上给多个选项。按行命中是**精确坐标**，不必抢先后 ——
            # 真靶 72 条缝不上的剧情台词就是这么被"这一行已被占用"挡在门外的。
            matched, how = candidate, "line"
        else:
            same_text = [
                node for node in by_text.get((rel, slot.source), []) if node.node_id not in claimed
            ]
            if len(same_text) == 1:
                # 同一行上有多个内容时（菜单那一类），引擎给的行号指在菜单语句上，
                # 按原文在同文件里唯一命中才认。
                matched, how = same_text[0], "text"
            else:
                # 原文重复出现（或压根没有行号）：按出现次序一一对上。
                # 这是确定性配对，不是猜 —— 两边的顺序都来自各自的文档顺序。
                ordered = text_pairs.get(slot.key)
                if ordered is not None and ordered.node_id not in claimed:
                    matched, how = ordered, "text-order"
        if matched is not None:
            claimed.add(matched.node_id)

        # ---- 归属：谁和谁属于同一场戏（这一条决定节点划分）----------------------
        # 权威序见上面 `labels_by_name` / `menu_owner` 的注释；行号配对**不决定归属**，
        # 它只负责补说话人 / 类别 / 权重（尽力而为）。
        container_id = containers.get((rel, line))
        owner = _engine_label_name(slot.engine_id, labels_by_name, menu_owner)
        if owner is None and container_id:
            owner = _owning_label_name(structure, structure.nodes.get(container_id))
        if owner is None and matched is not None:
            owner = _owning_label_name(structure, matched)
        if owner is None:
            prior = [name for at, name in (label_of_line.get(rel) or []) if at <= line]
            owner = prior[-1] if prior else None
        if owner is not None and not how:
            how = "engine-id" if _engine_label_name(
                slot.engine_id, labels_by_name, menu_owner
            ) else ("container" if container_id else "label-range")

        parent: str | None = container_id
        # 缝上的那条语句自己的容器最准（菜单层级就在它的祖先链上）；但它必须与
        # 上面算出来的归属**一致**才用 —— 否则单元会挂到别人那场戏下面。
        if parent is None and matched is not None and matched.parent:
            if owner is None or _owning_label_name(structure, matched) == owner:
                parent = matched.parent
        if parent is None and owner is not None:
            parent = labels_by_name.get(owner)
        if parent is None and matched is not None:
            parent = matched.parent
        if parent is None:
            parent = file_nodes.get(rel) or root_id
        if how:
            aligned += 1
            if owner is not None and matched is None:
                # 归属有了（引擎声明的），但源码里没有对应位置：这不是"散句"，
                # 可它说明骨架与源码不同步 —— 单独记一笔，别混进"没归属"里。
                no_source_position.append(
                    {"slot": slot.key, "file": rel, "line": line, "source": slot.source}
                )
        else:
            how = "unaligned"
            unaligned.append(
                {
                    "slot": slot.key,
                    "file": rel,
                    "line": line,
                    "source": slot.source,
                }
            )

        kind, unit_type = _classify(slot, matched)
        # 写回与审计要的是**脚本里的变量名**（骨架里就写着 `e "..."`），
        # 提示词与依赖图要的是人名 —— 两个都留着，各给各的用（与源码提取器同一口径）。
        variable = str(slot.metadata.get("speaker") or "")
        if not variable and matched is not None and matched.unit is not None:
            variable = str(matched.unit.locator.payload.get("speaker") or "")

        structure_key, structure_label, structure_region = _structure_coordinate(
            structure, slot, rel, parent, root_id
        )
        # 归属以**权威序**为准：`_structure_coordinate` 是照着源码容器链算的，
        # 缝不上源码时它会退到文件 —— 那正是"散句桶"的来源。这里覆盖回 owner。
        if owner is not None:
            structure_key = owner
            structure_region = owner
            if not structure_label or structure_label == rel:
                structure_label = owner
        slot.metadata["boundary_key"] = structure_key
        slot.metadata["structure_label"] = structure_label
        slot.metadata["structure_region"] = structure_region
        # **内容类别**是适配层申报的事实（哪些文件是引擎自带的界面 / 开发工具）：
        # 内核拿它决定"产不产资产"。内容范围不受影响 —— 界面文本照样翻。
        slot.metadata["content_class"] = content_class_for_file(rel)
        # 说话人两个都留：**变量名**给写回（原脚本里就是 `d "..."`），
        # **显示名**给模型（`define d = Character("Desiree")` 里的人名）。
        # 只留变量名时，提示词里会出现 `d：台词` 这种半截东西（实测过）。
        display_speaker = characters.get(variable) if variable else None

        placements[slot.key] = {
            "rel": rel,
            "line": line,
            "how": how,
            "matched": matched,
            "parent": parent,
            "kind": kind,
            "unit_type": unit_type,
            "variable": variable or "",
            "region": structure_region,
            #: 菜单层级（形如 ``act21/menu[0]``）。**它现在住在槽位的载荷里**：
            #: 空壳容器不再进图之后（R76），这是"这段文本属于哪个菜单"在产品里的家。
            "structure_label": structure_label,
            "display_speaker": display_speaker or "",
        }

    # ---- 2) 按结构坐标组装翻译单元：一个单元可以含多句 -----------------------
    units = assemble_units(slots, GroupByStructureKey(), scanner=scanner)

    # ---- 3) 每个单元成为图上的一个结点（一条文本、一次翻译请求） -------------
    remap: dict[str, str] = {}
    for unit in units:
        keys = [key for key in (unit.metadata.get("slot_keys") or []) if key in placements]
        if not keys:  # pragma: no cover - assemble_units 只会从槽位产出单位
            continue
        members = [placements[key] for key in keys]
        first = members[0]
        # 单元里的槽位按**文档顺序**排：同一结构段内的语句先后就是阅读顺序
        members.sort(key=lambda item: (item["rel"], item["line"]))

        matched_nodes = [m["matched"] for m in members if m["matched"] is not None]
        context = _merge_context(matched_nodes, members)
        kinds = [m["kind"] for m in members]
        type_name = (
            first["unit_type"]
            if len({m["unit_type"] for m in members}) == 1
            else "mixed"
        )
        # 挂到**能覆盖全部成员的最深容器**：同一段里的槽位自然挂在同一个 label 下
        parent = _common_parent(structure, [m["parent"] for m in members]) or root_id

        enriched = replace(
            unit,
            type=type_name,
            locator=Locator(
                file=first["rel"],
                line=first["line"],
                kind="line",
                payload={
                    # 一个单元含多条槽位：每条槽位的引擎信息原样留着，回填时按槽位找位置
                    "relpath": first["rel"],
                    "line": first["line"],
                    "alignment": first["how"],
                    "speaker": _single(m["variable"] for m in members),
                    "slots": [
                        {
                            "slot_key": key,
                            "source": slots.get(key).source if slots.get(key) else "",
                            # 这一句自己的受保护 token（变量、{w}、控制码……）。
                            # 按句存而不是按单元存：一个单元几百句时，逐句重复整段的
                            # token 会把请求放大几十倍（真靶实测 94% 的字符是这个）。
                            "protected": (
                                [
                                    segment.value
                                    for segment in (scanner(slots.get(key).source) if scanner else [])
                                    if segment.is_protected
                                ]
                                if slots.get(key)
                                else []
                            ),
                            "engine_id": slots.get(key).engine_id if slots.get(key) else None,
                            "keying": slots.get(key).keying.value if slots.get(key) else "",
                            "node_class": slots.get(key).node_class if slots.get(key) else "",
                            "line": m["line"],
                            "alignment": m["how"],
                            # 菜单层级（`act21/menu[0]`）：空壳容器删掉之后（R76），
                            # "这一句在哪个菜单里"只能住在这儿。
                            "structure_label": m.get("structure_label") or "",
                            "speaker": m["variable"] or None,
                            "display_speaker": m.get("display_speaker") or None,
                            # 这一条槽位**锤在哪**：`"block"` = 锚在骨架块上（引号说话人的
                            # 名字就是这一类）。它是"这个名字在游戏里当名字用"的直接证据，
                            # 术语判据要靠它 —— 否则 `Coach` / `Barista` 这种
                            # "通用名词恰好当说话人栏用"的名字会被当成普通词误杀。
                            "anchor": (
                                slots.get(key).metadata.get("anchor")
                                if slots.get(key)
                                else None
                            ),
                            "locations": (
                                [spot.to_dict() for spot in slots.get(key).locations]
                                if slots.get(key)
                                else []
                            ),
                        }
                        for key, m in zip(keys, members)
                    ],
                },
            ),
            context=context,
            metadata={
                **unit.metadata,
                "structure_label": unit.metadata.get("structure_label", ""),
                "structure_region": next(
                    (placements[key]["region"] for key in keys if placements[key].get("region")),
                    "",
                ),
                "alignment": first["how"],
            },
        )
        builder.add_unit(
            enriched.id,
            enriched,
            parent=parent,
            # 段内混了多种内容 → `mixed`：真靶上 30 个剧情场景（对白 + 字符串表条目）
            # 以前一律记成 `string`，报告与面板于是说"37 个字符串结点"（R73）。
            kind=kinds[0] if len(set(kinds)) == 1 else NodeKind.MIXED,
        )
        remap[unit.id] = enriched.id
        for member in members:
            if member["matched"] is not None:
                # 源码侧那张图里的旧叶结点也指向新单位：搬依赖边时要能认出来
                remap[member["matched"].node_id] = enriched.id

    graph = builder.build(prune_empty=True)
    # 结构事实随槽位一起搬过来：先把源码图的依赖边按新节点 id 重指，
    # 再按新图重算"同一句在几处出现"，最后补上菜单并列选项之间的 branch 边
    # （branch 边的两端原本是源码侧的叶节点，重指不了，只能在内容图上重算）。
    dropped = _carry_dependencies(structure, graph, remap)
    RenPyExtractor.apply_occurrences(graph)

    unclaimed = [node for node in leaves if node.node_id not in claimed]
    not_listed = [
        {
            "file": _node_relpath(node, project_root),
            "line": int(node.unit.locator.line if node.unit.locator else 0),
            "source": node.unit.source,
        }
        for node in unclaimed
        if node.unit is not None
    ]

    alignment = Alignment(
        graph=graph,
        aligned=aligned,
        unaligned=unaligned,
        no_source_position=no_source_position,
        not_in_engine_list=not_listed,
        dropped_edges=dropped,
    )
    if report is not None:
        _record(report, alignment, slots)
    return alignment


def _carry_dependencies(structure: PathGraph, graph: PathGraph, remap: dict[str, str]) -> int:
    """把源码图上读出来的依赖边搬到内容图上，返回**搬不过去**因此被丢掉的条数。

    端点有两种：区域 id（label 名，jump/call 那类）与节点 id。

    两件事在这里做掉，都是"投影到单元粒度"必然要处理的：

    * **自环丢掉**：源码侧并列选项原本是两个叶节点，投影到"一个 label 一个单元"之后
      两端落进**同一个单元** —— 那条边就变成了自己指自己。留着它只会让
      "汇合/分支"这类读数全部失真（真靶上 111 条选项边全部退化成自环）。
      内核的 :meth:`PathGraph.project_dependencies_onto_units` 早就是这么做的，
      适配层这条投影路径漏了同一件事。
    * **去重**：同一对端点可能由"显式跳转"和"顺序流"两条路都指向。同一件事留一条。
    """
    known = set(graph.nodes) | graph.region_ids()
    seen: set[tuple[str, str, str]] = set()
    dropped = 0
    for edge in structure.dependencies:
        source = remap.get(edge.source, edge.source)
        target = remap.get(edge.target, edge.target)
        key = (source, target, edge.type)
        if source == target or source not in known or target not in known or key in seen:
            dropped += 1
            continue
        seen.add(key)
        graph.dependencies.append(replace(edge, source=source, target=target))
    return dropped
    return dropped


def _record(report: RunReport, alignment: Alignment, slots: SlotSet) -> None:
    """把对齐结果如实记进报告：缝不上的、不在清单里的，都要看得见。"""
    report.metrics["slots"] = len(slots)
    report.metrics["slots_by_keying"] = {
        "identified": len(slots.by_keying("identified")),
        "keyed": len(slots.by_keying("keyed")),
    }
    report.metrics.update(alignment.metrics())

    if alignment.unaligned:
        report.add_issue(
            "skipped",
            code="unaligned_slot",
            message=(
                f"{len(alignment.unaligned)} 条引擎槽位没缝到源码结构上，已挂在文件节点下"
                "（内容照翻，只是拿不到 label / 菜单归属）"
            ),
            detail={
                "count": len(alignment.unaligned),
                "samples": alignment.unaligned[:SAMPLE_LIMIT],
            },
        )

    if alignment.no_source_position:
        report.add_issue(
            "skipped",
            code="slot_without_source_position",
            message=(
                f"{len(alignment.no_source_position)} 条槽位的归属来自引擎声明"
                "（块 id 前缀 / 容器），但源码里找不到对应位置 —— 内容照翻、归属不丢，"
                "可这多半说明骨架与源码不同步（骨架该重新生成）"
            ),
            detail={
                "count": len(alignment.no_source_position),
                "samples": alignment.no_source_position[:SAMPLE_LIMIT],
            },
        )

    if alignment.not_in_engine_list:
        report.add_issue(
            "skipped",
            code="not_in_engine_list",
            message=(
                f"{len(alignment.not_in_engine_list)} 条源码文本不在引擎的可译清单里，"
                "已按引擎口径排除（`define` 里的角色名、未 `_()` 包裹的字面量等）"
            ),
            detail={
                "count": len(alignment.not_in_engine_list),
                "samples": alignment.not_in_engine_list[:SAMPLE_LIMIT],
            },
        )


def bind_translations(
    slots: SlotSet,
    units: Iterable[TranslationUnit],
    translations: dict[str, TranslationArtifact],
    withheld: dict[str, TranslationArtifact] | None = None,
    empty_ok: Iterable[str] = (),
) -> SlotBindings:
    """把**按槽位存的译文**变成**按槽位记账的绑定**。

    记录键就是槽位身份（一个单元含多句时逐句一条），所以这里是逐条对应、不做推断：
    一个单元含 N 条槽位，就按它元数据里的 ``slot_keys`` **逐条**去记录里取，取不到
    就是"这条没有译文"。**不许按单元整段回填** —— 那会把一段话写进每一格。

    ``withheld`` 是内核在导出前校验里挡下的译文：**不写进产物**（状态不是可用），
    但要作为"记了、不能用"登记，而不是当作"压根没记"—— 后者会让人去重翻一遍。

    记录里的槽位已经不在当前骨架里（游戏更新过、骨架重生成过）时的记法：
    以 ``allow_unknown`` 记下，让写回层把它报成"没落地"而不是悄悄丢掉。
    """
    bindings = SlotBindings(slots)
    known = {slot.key for slot in slots}
    ordered = list(units)
    #: 被声明"有意留空"的单位：空译文也要真的写进产物
    declared_empty = {str(item) for item in (empty_ok or ())}

    for unit in ordered:
        keys = [key for key in (unit.metadata.get("slot_keys") or []) if key in known]
        if not keys:
            continue
        bindings.bind(UnitBinding(unit_id=unit.id, slot_keys=keys))

    def _status_of(record: TranslationArtifact) -> str:
        return getattr(record.status, "value", None) or str(record.status)

    for unit in ordered:
        keys = [key for key in (unit.metadata.get("slot_keys") or []) if key in known]
        for key in keys:
            record = translations.get(key)
            if record is None:
                continue
            # 记了、但不许用（没过闸门）：**照样进绑定**，状态原样带着 ——
            # 于是记账口径是"翻了但没过"，不是"压根没翻"（两者处置相反）。
            # 写回层看到不可用状态本来就不会写，所以进绑定不等于会写进产物。
            bindings.record(
                key,
                target=record.translated_text,
                status=USABLE_STATUS if record.is_usable else _status_of(record),
                provider=record.provenance.provider if record.provenance else "",
                model=record.provenance.model if record.provenance else "",
                allow_unknown=False,
                # 偏离声明记在**槽位键**上（records 按槽位记账），单元级声明也一并认
                allow_empty=key in declared_empty or unit.id in declared_empty,
            )

    for unit in ordered:
        keys = [key for key in (unit.metadata.get("slot_keys") or []) if key in known]
        for key in keys:
            record = (withheld or {}).get(key)
            if record is None:
                continue
            # 自报可用、却被导出前校验挡下（记录里的结论与重算的不一致）：它不是"待复核"，
            # 而是"这一轮跳过" —— 用 skipped 表示，仍然不是可用状态，因此不会被写进去。
            status = _status_of(record)
            if status == USABLE_STATUS:
                status = TranslationStatus.SKIPPED.value
            bindings.record(
                key,
                target=record.translated_text,
                status=status,
                provider=record.provenance.provider if record.provenance else "",
                model=record.provenance.model if record.provenance else "",
                allow_unknown=False,
            )

    # 记了译文、但图里没有对应单位的：也要能被写回层看见（否则用户以为全翻好了）
    known_units = {unit.id for unit in ordered}
    for unit_id, record in translations.items():
        if unit_id in known_units:
            continue
        key = str(record.metadata.get("slot_key") or "")
        if not key:
            continue
        status = getattr(record.status, "value", None) or str(record.status)
        bindings.record(
            key,
            target=record.translated_text,
            status=USABLE_STATUS if record.is_usable else status,
            allow_unknown=True,
        )

    return bindings
