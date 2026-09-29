"""内容范围：RPG Maker MV 里**哪些文本是玩家真能看见的**。

这是适配层最核心的一份"引擎知识"，也是本项目 §0.1 原则在 RPGG 侧的落地：
**内容由引擎的数据结构决定，不由"看起来像不像英文"猜。**

判据来自引擎自己（``js/rpg_objects.js`` / ``rpg_windows.js`` / ``rpg_managers.js``）：

============  ==========================================================
真能看见的     证据
============  ==========================================================
对话 401       消息窗口正文。真靶实测 2,751 块，**每一块前都有自己的 101**；
               块就是"一条消息"，所以一条消息 = 一个翻译单位（不是一行）
选项 102       ``parameters[0]`` 是选项文案数组，直接决定玩家操作
滚动文本 405   系统消息（`\\C[2]` 那类滚动字）
改名指令       320/324/325 改的是玩家看得见的角色名 / 昵称 / 简介
数据库文本     Actors(name/nickname/profile)、Classes(name)、Skills(name/description/
               message1/message2)、Items/Weapons/Armors(name/description)、
               Enemies(name)、States(name/message1..4)
System 词条    ``gameTitle`` / ``currencyUnit`` / 类型名数组 /
               ``terms.basic`` / ``terms.commands`` / ``terms.params`` / ``terms.messages``
地图显示名     ``displayName``（引擎画在屏幕上；缺省时才退回地图树名）
============  ==========================================================

**算不出来就不算内容**（它们仍被如实记账，不静默丢）：

* **编辑器专用标签**：事件名、地图树名、公共事件名、图块名、动画名、敌群名、
  开关/变量名 —— 只在编辑器里显示（真靶 1,606 个事件名，绝大多数是 ``EV001``）；
* **开发者元数据**：``note`` 字段（插件标记，玩家看不见）、脚本/插件指令本身；
* **还拿不准的候选**：``355/655`` 脚本指令与 ``356/357`` 插件指令的**参数里可能有
  可见文案**（真靶的 ``D_TEXT Silicone: \\V[1]cc 30`` 就是在屏幕上画字）。它们
  既不是确定的正文，也不能当没有 —— 单独计数并给样本，由人或 agent 决定要不要纳入。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from gametrans.core.models import NodeKind
from gametrans.engines.rpgm.segments import unclassified_control_codes

__all__ = [
    "Candidate",
    "ContentScan",
    "MESSAGE_COMMANDS",
    "collect_content",
]

#: 一个消息块的两半：``101`` 打开消息窗，紧随其后的 ``401`` 是正文行。
SHOW_TEXT = 101
TEXT_LINE = 401
SHOW_CHOICES = 102
SCROLL_TEXT = 405
CHANGE_ACTOR_NAME = 320
CHANGE_ACTOR_NICKNAME = 324
CHANGE_ACTOR_PROFILE = 325
SCRIPT = 355
SCRIPT_CONT = 655
PLUGIN_COMMAND = 356
PLUGIN_COMMAND_CONT = 357

MESSAGE_COMMANDS: frozenset[int] = frozenset({SHOW_TEXT, TEXT_LINE})

#: YEP_MessageCore / 真靶作者惯用的"名字框"写法：``\\n<说话人>``
SPEAKER = re.compile(r"\\n<([^>\n]+)>")

#: 数据库里玩家**看得见**的文本字段 —— (字段名, 单位类型)
DB_TEXT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "Actors.json": (
        ("name", "entity_name"),
        ("nickname", "entity_name"),
        ("profile", "entity_description"),
    ),
    "Classes.json": (("name", "entity_name"),),
    "Skills.json": (
        ("name", "entity_name"),
        ("description", "entity_description"),
        ("message1", "ui_term"),
        ("message2", "ui_term"),
    ),
    "Items.json": (("name", "entity_name"), ("description", "entity_description")),
    "Weapons.json": (("name", "entity_name"), ("description", "entity_description")),
    "Armors.json": (("name", "entity_name"), ("description", "entity_description")),
    "Enemies.json": (("name", "entity_name"),),
    "States.json": (
        ("name", "entity_name"),
        ("message1", "ui_term"),
        ("message2", "ui_term"),
        ("message3", "ui_term"),
        ("message4", "ui_term"),
    ),
}

#: **只在编辑器里显示**的字段：是文本，但不是玩家可见内容。
#: ``CommonEvents.json`` / ``Troops.json`` 不在这里 —— 它们除了名字还带指令表，
#: 归 :func:`_command_holder` 处理（那里会顺手把名字记成编辑器专用）。
EDITOR_ONLY_DB_FIELDS: dict[str, tuple[str, ...]] = {
    "Tilesets.json": ("name",),
    "Animations.json": ("name",),
}

#: System.json 里玩家看得见的平凡字段。
SYSTEM_PLAIN_FIELDS = (
    "gameTitle",
    "currencyUnit",
    "armorTypes",
    "elements",
    "equipTypes",
    "skillTypes",
    "weaponTypes",
)
#: System.json 里只在编辑器里显示的字段。
SYSTEM_EDITOR_ONLY_FIELDS = ("switches", "variables")

UNIT_NODE_KIND: dict[str, NodeKind] = {
    "choice": NodeKind.CHOICE,
    "entity_name": NodeKind.DEFINITION,
    "dialogue": NodeKind.SAY,
    "entity_description": NodeKind.STRING,
    "ui_term": NodeKind.STRING,
}

#: 拿不准的候选：既不是确定的正文，也不能当没有。
UNCERTAIN_PLUGIN_COMMAND = "plugin_command_text_candidate"
UNCERTAIN_SCRIPT_COMMAND = "script_command_text_candidate"
UNCERTAIN_CONTROL_CODE = "unclassified_control_code"


@dataclass
class Candidate:
    """一条玩家可见文本，带着它在引擎数据结构里的确切位置。"""

    structural_path: str
    file: str
    category: str
    unit_type: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    speaker: str | None = None
    scene: str | None = None
    location: str | None = None
    note: str = ""

    @property
    def node_kind(self) -> NodeKind:
        return UNIT_NODE_KIND.get(self.unit_type, NodeKind.STRING)

    @property
    def fingerprint(self) -> str:
        """原文指纹：位置漂了之后按它把译文重新缝回同一条文本。"""
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:16]

    @property
    def file_name(self) -> str:
        """它长在哪一份数据文件里（``Map001.json``）—— 由结构路径的前半段给出。"""
        return self.structural_path.split("#", 1)[0]


@dataclass
class ContentScan:
    """一次内容清点：要翻的、不翻的、空的、拿不准的，各归各类。"""

    candidates: list[Candidate] = field(default_factory=list)
    #: 确定不是内容（编辑器专用标签等）：类目 → 条数
    excluded: dict[str, int] = field(default_factory=dict)
    excluded_samples: dict[str, list[str]] = field(default_factory=dict)
    #: 内容范围内但原文为空：类目 → 条数（空原文不是"翻译失败"，见契约 R29）
    empty: dict[str, int] = field(default_factory=dict)
    #: 拿不准的候选（脚本/插件指令）：code → {count, samples, why}
    uncertain: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 实测清单之外的控制码：码名 → 出现次数
    unclassified_codes: dict[str, int] = field(default_factory=dict)
    unclassified_samples: list[str] = field(default_factory=list)

    def count(self, category: str) -> int:
        return sum(1 for c in self.candidates if c.category == category)

    def by_category(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for candidate in self.candidates:
            found[candidate.category] = found.get(candidate.category, 0) + 1
        return dict(sorted(found.items()))

    # ---- 记账 ---------------------------------------------------------------

    def exclude(self, reason: str, text: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1
        samples = self.excluded_samples.setdefault(reason, [])
        if len(samples) < 5:
            samples.append(text[:80])

    def count_empty(self, category: str) -> None:
        self.empty[category] = self.empty.get(category, 0) + 1

    def uncertain_hit(self, code: str, why: str, sample: str) -> None:
        entry = self.uncertain.setdefault(code, {"count": 0, "samples": [], "why": why})
        entry["count"] += 1
        if len(entry["samples"]) < 5:
            entry["samples"].append(sample[:120])

    def note_unclassified_code(self, code: str, sample: str) -> None:
        """实测清单之外的控制码 —— 点名，好让"遇到再补"看得见。"""
        self.unclassified_codes[code] = self.unclassified_codes.get(code, 0) + 1
        if len(self.unclassified_samples) < 5:
            self.unclassified_samples.append(sample[:120])


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def collect_content(data: dict[str, Any]) -> ContentScan:
    """把整份数据清点成"要翻什么、不翻什么、还拿不准什么"。"""
    scan = ContentScan()
    for name in sorted(data):
        payload = data[name]
        if name == "System.json":
            _system(payload, scan)
        elif name in DB_TEXT_FIELDS or name in EDITOR_ONLY_DB_FIELDS:
            _database(name, payload, scan)
        elif name in ("CommonEvents.json", "Troops.json"):
            _command_holder(name, payload, scan)
        elif name == "MapInfos.json":
            _map_infos(payload, scan)
        elif name.startswith("Map") and name.endswith(".json"):
            _map(name, payload, scan)
    return scan


# --------------------------------------------------------------------------- #
# System.json
# --------------------------------------------------------------------------- #


def _system(payload: Any, scan: ContentScan) -> None:
    if not isinstance(payload, dict):
        return
    name = "System.json"
    for key in SYSTEM_PLAIN_FIELDS:
        value = payload.get(key)
        if isinstance(value, str):
            _plain(name, f"{key}", key, value, scan)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                _plain(name, f"{key}[{index}]", key, item, scan)
    for key in SYSTEM_EDITOR_ONLY_FIELDS:
        for item in payload.get(key) or []:
            if isinstance(item, str) and item.strip():
                scan.exclude(f"system_{key}_name", item)

    terms = payload.get("terms")
    if not isinstance(terms, dict):
        return
    for group, entries in terms.items():
        if isinstance(entries, dict):
            for key, value in entries.items():
                _terms(name, f"terms.{group}.{key}", f"System.terms.{group}", value, scan)
        elif isinstance(entries, list):
            for index, value in enumerate(entries):
                _terms(name, f"terms.{group}[{index}]", f"System.terms.{group}", value, scan)


def _plain(
    name: str, suffix: str, category_key: str, value: Any, scan: ContentScan
) -> None:
    category = f"System.{category_key}"
    if not isinstance(value, str):
        # 引擎那里根本没有这一条（真靶的 terms.messages 里真的有 null）——
        # 这不是"空原文"，是"没有这一项"，不记账
        return
    if not value.strip():
        scan.count_empty(category)
        return
    _emit(
        scan,
        Candidate(
            structural_path=f"{name}#{suffix}",
            file="",
            category=category,
            unit_type="ui_term",
            text=value,
            payload={"field": suffix, "system_key": category_key},
            scene="System",
            note="引擎内建界面词条",
        ),
    )


def _terms(
    name: str, suffix: str, category: str, value: Any, scan: ContentScan
) -> None:
    if not isinstance(value, str):
        return
    if not value.strip():
        scan.count_empty(category)
        return
    _emit(
        scan,
        Candidate(
            structural_path=f"{name}#{suffix}",
            file="",
            category=category,
            unit_type="ui_term",
            text=value,
            payload={"field": suffix},
            scene="System",
            note="引擎内建界面词条",
        ),
    )


# --------------------------------------------------------------------------- #
# 数据库文件（Actors / Items / States / …）
# --------------------------------------------------------------------------- #


def _database(name: str, payload: Any, scan: ContentScan) -> None:
    if not isinstance(payload, list):
        return
    fields = DB_TEXT_FIELDS.get(name, ())
    editor_only = EDITOR_ONLY_DB_FIELDS.get(name, ())
    stem = name[: -len(".json")]
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            continue
        for field_name, unit_type in fields:
            value = entry.get(field_name)
            category = f"{stem}.{field_name}"
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                scan.count_empty(category)
                continue
            _emit(
                scan,
                Candidate(
                    structural_path=f"{name}#{index}.{field_name}",
                    file="",
                    category=category,
                    unit_type=unit_type,
                    text=value,
                    payload={
                        "json_index": index,
                        "field": field_name,
                        "entry_id": entry.get("id"),
                    },
                    scene=stem,
                    note=f"{name} 第 {index} 项",
                ),
            )
        for field_name in editor_only:
            value = entry.get(field_name)
            if isinstance(value, str) and value.strip():
                scan.exclude(f"{stem}_{field_name}", value)
        note = entry.get("note")
        if isinstance(note, str) and note.strip():
            scan.exclude("developer_note", note)


def _map_infos(payload: Any, scan: ContentScan) -> None:
    if not isinstance(payload, list):
        return
    for entry in payload:
        if isinstance(entry, dict):
            value = entry.get("name")
            if isinstance(value, str) and value.strip():
                scan.exclude("map_info_name", value)


# --------------------------------------------------------------------------- #
# 事件指令（地图 / 公共事件 / 敌群）
# --------------------------------------------------------------------------- #


def _map(name: str, payload: Any, scan: ContentScan) -> None:
    if not isinstance(payload, dict):
        return
    display = payload.get("displayName")
    location = display if isinstance(display, str) and display.strip() else None
    stem = name[: -len(".json")]
    if isinstance(display, str) and display.strip():
        _emit(
            scan,
            Candidate(
                structural_path=f"{name}#displayName",
                file="",
                category="Map.displayName",
                unit_type="ui_term",
                text=display,
                payload={"field": "displayName"},
                # 地图显示名挂在文件节点下（它就是这份地图的属性，不属于某个事件）
                scene=stem,
                location=None,
                note="地图显示名",
            ),
        )

    for event_index, event in enumerate(payload.get("events") or []):
        if not isinstance(event, dict):
            continue
        event_name = event.get("name")
        if isinstance(event_name, str) and event_name.strip():
            scan.exclude("event_name", event_name)
        event_id = event.get("id", event_index)
        for page_index, page in enumerate(event.get("pages") or []):
            if not isinstance(page, dict):
                continue
            container = f"events[{event_index}].pages[{page_index}]"
            _walk_commands(
                commands=page.get("list") or [],
                scan=scan,
                name=name,
                container=container,
                scene=f"{name}#events[{event_index}]",
                location=location,
                note=f"{name} 事件 {event_id} 第 {page_index} 页",
            )


def _command_holder(name: str, payload: Any, scan: ContentScan) -> None:
    """``CommonEvents.json`` / ``Troops.json``：数组里每一项带一段指令表。"""
    if not isinstance(payload, list):
        return
    stem = name[: -len(".json")]
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            continue
        label = entry.get("name")
        if isinstance(label, str) and label.strip():
            scan.exclude(f"{stem}_name", label)
        pages = entry.get("pages")
        if isinstance(pages, list):
            holders = [("pages", page_index, page) for page_index, page in enumerate(pages)]
        else:
            holders = [("", 0, entry)]
        for _kind, holder_index, holder in holders:
            if not isinstance(holder, dict):
                continue
            container = (
                f"{index}" if not _kind else f"{index}.pages[{holder_index}]"
            )
            _walk_commands(
                commands=holder.get("list") or [],
                scan=scan,
                name=name,
                container=container,
                scene=f"{name}#{index}",
                location=None,
                note=f"{name} 第 {index} 项",
            )


def _walk_commands(
    *,
    commands: list[Any],
    scan: ContentScan,
    name: str,
    container: str,
    scene: str,
    location: str | None,
    note: str,
) -> None:
    total = len(commands)
    index = 0
    while index < total:
        command = commands[index]
        if not isinstance(command, dict):
            index += 1
            continue
        code = command.get("code")
        params = command.get("parameters") or []

        if code in MESSAGE_COMMANDS:
            run_start = index if code == TEXT_LINE else index + 1
            text_indices = _run_of_text(commands, run_start)
            if text_indices:
                _message(
                    scan=scan,
                    commands=commands,
                    name=name,
                    container=container,
                    scene=scene,
                    location=location,
                    note=note,
                    anchor=index,
                    text_indices=text_indices,
                    preceded_by_show_text=code == SHOW_TEXT,
                )
                index = text_indices[-1] + 1
                continue
            index += 1
            continue

        if code == SHOW_CHOICES:
            choices = params[0] if params and isinstance(params[0], list) else []
            for choice_index, choice in enumerate(choices):
                _choice(
                    scan=scan,
                    name=name,
                    container=container,
                    scene=scene,
                    location=location,
                    note=note,
                    anchor=index,
                    choice_index=choice_index,
                    text=choice,
                )
            index += 1
            continue

        if code == SCROLL_TEXT:
            _scroll(
                scan=scan,
                name=name,
                container=container,
                scene=scene,
                note=note,
                anchor=index,
                text=params[0] if params else "",
            )
            index += 1
            continue

        if code in (CHANGE_ACTOR_NAME, CHANGE_ACTOR_NICKNAME, CHANGE_ACTOR_PROFILE):
            if len(params) > 1:
                _renamed(
                    scan=scan,
                    name=name,
                    container=container,
                    scene=scene,
                    note=note,
                    anchor=index,
                    code=int(code),
                    text=params[1],
                )
            index += 1
            continue

        if code in (SCRIPT, SCRIPT_CONT):
            body = params[0] if params else ""
            if isinstance(body, str) and _WORDY.search(body):
                scan.uncertain_hit(
                    UNCERTAIN_SCRIPT_COMMAND,
                    "脚本指令可能直接往消息窗写字（`$gameMessage.add(\"…\")`），"
                    "也可能只是逻辑；参数原文在数据里，是否纳入由人或 agent 决定",
                    body,
                )
            index += 1
            continue

        if code in (PLUGIN_COMMAND, PLUGIN_COMMAND_CONT):
            body = params[0] if params else ""
            if isinstance(body, str) and _WORDY.search(body):
                scan.uncertain_hit(
                    UNCERTAIN_PLUGIN_COMMAND,
                    "插件指令的参数里可能带屏幕上会显示的文案"
                    "（真靶的 `D_TEXT Silicone: \\V[1]cc 30` 就是画在屏幕上的字）",
                    body,
                )
            index += 1
            continue

        index += 1


_WORDY = re.compile(r"[A-Za-z]{2,}")


def _run_of_text(commands: list[Any], start: int) -> list[int]:
    """从 ``start`` 起连续的 401 行下标。一个 101 配一段 401 就是"一条消息"。"""
    found: list[int] = []
    index = start
    while index < len(commands):
        command = commands[index]
        if not isinstance(command, dict) or command.get("code") != TEXT_LINE:
            break
        found.append(index)
        index += 1
    return found


def _message(
    *,
    scan: ContentScan,
    commands: list[Any],
    name: str,
    container: str,
    scene: str,
    location: str | None,
    note: str,
    anchor: int,
    text_indices: list[int],
    preceded_by_show_text: bool,
) -> None:
    lines: list[str] = []
    for index in text_indices:
        params = commands[index].get("parameters") or []
        lines.append(params[0] if params and isinstance(params[0], str) else "")
    text = "\n".join(lines)
    path = f"{name}#{container}.list[{anchor}]"
    if not text.strip():
        scan.count_empty("dialogue")
        return
    match = SPEAKER.search(text)
    speaker = match.group(1).strip() if match else None
    _emit(
        scan,
        Candidate(
            structural_path=path,
            file="",
            category="Map.dialogue" if name.startswith("Map") else f"{_stem(name)}.dialogue",
            unit_type="dialogue",
            text=text,
            payload={
                "show_text_index": anchor,
                "text_indices": list(text_indices),
                "preceded_by_show_text": preceded_by_show_text,
                "command_codes": [TEXT_LINE] * len(text_indices),
            },
            speaker=speaker,
            scene=scene,
            location=location,
            note=note,
        ),
    )
    _note_control_codes(scan, text)


def _choice(
    *,
    scan: ContentScan,
    name: str,
    container: str,
    scene: str,
    location: str | None,
    note: str,
    anchor: int,
    choice_index: int,
    text: Any,
) -> None:
    path = f"{name}#{container}.list[{anchor}].choice[{choice_index}]"
    category = "Map.choice" if name.startswith("Map") else f"{_stem(name)}.choice"
    if not isinstance(text, str) or not text.strip():
        scan.count_empty(category)
        return
    _emit(
        scan,
        Candidate(
            structural_path=path,
            file="",
            category=category,
            unit_type="choice",
            text=text,
            payload={
                "choice_command_index": anchor,
                "choice_index": choice_index,
            },
            scene=scene,
            location=location,
            note=f"{note} 的选项",
        ),
    )
    _note_control_codes(scan, text)


def _scroll(
    *,
    scan: ContentScan,
    name: str,
    container: str,
    scene: str,
    note: str,
    anchor: int,
    text: Any,
) -> None:
    path = f"{name}#{container}.list[{anchor}]"
    category = "Map.scroll_text" if name.startswith("Map") else f"{_stem(name)}.scroll_text"
    if not isinstance(text, str) or not text.strip():
        scan.count_empty(category)
        return
    _emit(
        scan,
        Candidate(
            structural_path=path,
            file="",
            category=category,
            unit_type="dialogue",
            text=text,
            payload={"scroll_text_index": anchor},
            scene=scene,
            note=note,
        ),
    )
    _note_control_codes(scan, text)


def _renamed(
    *,
    scan: ContentScan,
    name: str,
    container: str,
    scene: str,
    note: str,
    anchor: int,
    code: int,
    text: Any,
) -> None:
    path = f"{name}#{container}.list[{anchor}]"
    category = (
        "Map.actor_text" if name.startswith("Map") else f"{_stem(name)}.actor_text"
    )
    if not isinstance(text, str) or not text.strip():
        scan.count_empty(category)
        return
    _emit(
        scan,
        Candidate(
            structural_path=path,
            file="",
            category=category,
            unit_type="entity_name" if code != CHANGE_ACTOR_PROFILE else "entity_description",
            text=text,
            payload={"command_code": code, "command_index": anchor},
            scene=scene,
            note=f"{note}（运行时改名）",
        ),
    )
    _note_control_codes(scan, text)


def _note_control_codes(scan: ContentScan, text: str) -> None:
    for code in unclassified_control_codes(text):
        scan.note_unclassified_code(code, f"\\{code} ← {text}")


def _stem(name: str) -> str:
    return name[: -len(".json")] if name.endswith(".json") else name


def _emit(scan: ContentScan, candidate: Candidate) -> None:
    scan.candidates.append(candidate)
