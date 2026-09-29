"""开工前的**体检**：资产用不用得上（会拦），以及还有哪一步没做（只提醒，不拦）。

**两件事，两种力度** —— 这个区别是刻意的：

* **资产**（:attr:`AssetPreflight.verdict` / :attr:`~AssetPreflight.blocking`）：
  "机器写进去的资产一条都进不了请求"会**拒绝开工**。那是"软件以为钉住了、其实没钉"，
  继续跑只会把错误译名铺得更大。
* **步骤**（:attr:`AssetPreflight.workflow`）：还有哪一步没做（摘要没生成、
  术语书是空的、请求会过大……）。**只报事实，不拦、不建议** ——
  做不做、按什么顺序做，由用户和 agent 自己决定。这一层的存在只为了一件事：
  让"某一步还没做"在开工时是**看得见的**，而不是要等成品出来才发现。
  所以这里刻意不写"你应该先做 X"：那是判断，不是事实。

下面这段是资产那一半的来历。

为什么要单开一层：真靶实测（该工程，run `89e3e94db4cb`）——46 个单元一次跑完，
盘上躺着 181 条模型申报的实体 + 45 条摘要侧实体，**一条都没进约束**。把 46 次真实请求
发出去的 `prompt_system` + `prompt_user` 逐字扫过（1,734,913 字符）：
`【术语约束】`、`【背景知识】`、`【风格要求】` 各出现 **0 次**。

结果是：整轮在没有资产的状态下翻完，而**流程里没有任何一步会因此报错**。
报告上一切正常（`ok: false` 只是因为 329 条缺译文），代价要到成品才看得见 ——
同一个人名在同一部游戏里出现两种写法（`Lexi` → 莱克西 ×58 / 蕾克西 ×16）。

术语书现在是**五栏**（``key`` / ``profile`` / ``constant`` / ``order`` / ``position``），
注入判定只看内容（有译名或有事实就注入），所以"写进去了却一条都没进请求"仍然是
事故形态。这里不"修"注入，而是把这件事**变成开工前必须回答的一个问题**：

============  ==========================================================
verdict        含义
============  ==========================================================
``ok``         有能用资产，而且至少一条真的会命中本次范围
``no_assets``  **一条能用资产都没有**（全新工程的第一轮就该是这样，允许开工）
``assets_never_fire``  **机器写进去的资产一条都进不了请求** → 拒绝开工
============  ==========================================================

`assets_never_fire` 之所以要**阻断**：那是"软件以为钉住了、其实没钉"的状态，
继续跑只会把错误译名铺得更大。`--asset-gate warn` 可以显式降级成警告
（比如明知某个专名这一轮还没出现，就是想先把别的翻掉）。

"命中"的口径与真正注入时同一套：术语按写法整词命中、设定按写法命中
（`layers/trigger.py`）—— 不另写一套判据，否则预检说命中、装配说没命中，
那个数就没人敢信。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.chapters import CHAPTERS_FILE
from gametrans.layers.tags import pending_writings

__all__ = ["AssetPreflight", "inspect_assets", "request_texts", "workflow_notes"]

#: 预检的判定值
VERDICT_OK = "ok"
VERDICT_NO_ASSETS = "no_assets"
VERDICT_NEVER_FIRE = "assets_never_fire"

#: 只有这一个判定会阻断开工
BLOCKING_VERDICTS = frozenset({VERDICT_NEVER_FIRE})


@dataclass
class AssetPreflight:
    """开工前的一次资产体检。"""

    #: 本次范围内会翻多少个单元
    units: int = 0
    #: 本次范围的槽位数（句一级）
    slots: int = 0
    #: 能用（有译名或有事实）几条
    usable: dict[str, int] = field(default_factory=dict)
    #: **待审更正**几条（改已有的译名 / 事实）：它不影响"能用几条"，但要看得见
    corrections: dict[str, int] = field(default_factory=dict)
    #: 本次范围内**真的会进请求**几条
    hit: dict[str, int] = field(default_factory=dict)
    #: 被世界书字符预算挤掉的标题（挤掉必须看得见，否则和"项目里没有"分不开）
    worldbook_dropped: list[str] = field(default_factory=list)
    #: 能用资产的名字（术语的原文 / 世界书的标题），最多列 8 条 ——
    #: "一条都命中不了"时必须点名是哪几条，否则人拿不到可照着做的一句话
    usable_names: list[str] = field(default_factory=list)
    #: 本次是**按范围跑**（`unit_scope` / 阶段切分）而不是全图跑。
    #: 范围跑里"资产没命中"是正常的（那一条本来就落在别的段），所以不拦。
    scope_limited: bool = False
    #: 能用资产里，**机器写进去的**有几条（写它的通道是 model / summary）。
    #: 只有它们参与"审核白做了"那一条拦截 —— 见 :attr:`blocking`。
    machine_written: int = 0
    #: **步骤提醒**：还有哪一步没做。纯陈述句，不含建议、不影响开工。
    #: 每项形如 ``{"code": ..., "text": ...}``，`text` 是给人看的一句事实。
    workflow: list[dict[str, str]] = field(default_factory=list)
    verdict: str = VERDICT_OK

    @property
    def blocking(self) -> bool:
        """要不要**拒绝开工**。

        拦的只有一种形态：**"写进去就没进约束"** —— 一批机器写进术语书的行，本该进
        请求，结果一条都进不了。这正是事故的形态（写入与注入之间掉链子），
        也是"没检查资产有没有注入就继续翻译"要变成不可能的那一处。

        两条刻意排除在拦截之外：

        * **人自己写进文件的条目打不响** —— 那是他的文件，写法在这一段没出现是常态。
          项目里就有这条约定：`test_translation_memory` 明写"加一条原文里根本没有的
          术语，谁的指纹都没变，全部照旧复用"。拿它拦开工等于改契约。
        * **按范围跑**（分块 / 续跑 / 阶段切分）—— 资产落在别的段，本来就不该命中。
        """
        return (
            self.verdict in BLOCKING_VERDICTS
            and not self.scope_limited
            and self.machine_written > 0
        )

    @property
    def usable_total(self) -> int:
        return sum(self.usable.values())

    @property
    def corrections_total(self) -> int:
        return sum(self.corrections.values())

    @property
    def hit_total(self) -> int:
        return sum(self.hit.values())

    def summary(self) -> str:
        """一句人能照着做的话（进报告与交互层）。"""
        usable = "、".join(f"{k} {v}" for k, v in sorted(self.usable.items())) or "无"
        corrections = "、".join(f"{k} {v}" for k, v in sorted(self.corrections.items())) or "无"
        hit = "、".join(f"{k} {v}" for k, v in sorted(self.hit.items())) or "无"
        head = {
            VERDICT_OK: f"资产预检通过：能用 {usable}，本次命中 {hit}",
            VERDICT_NO_ASSETS: "这一轮**零资产**：术语书 / 风格里一条能用的都没有",
            VERDICT_NEVER_FIRE: (
                f"有 {self.usable_total} 条能用资产"
                f"（{'、'.join(self.usable_names) or '未列名'}），"
                "但本次范围里**一条都命中不了** —— 等于没钉"
            ),
        }.get(self.verdict, self.verdict)
        tail = ""
        if self.verdict == VERDICT_NEVER_FIRE:
            if self.blocking:
                tail = (
                    f"。其中 {self.machine_written} 条是**机器写进去的** ——"
                    "写入与注入之间掉链子了。先确认写法与要翻的文本对得上"
                    "（写法必须是原文侧），或显式降级：--asset-gate warn"
                )
            elif self.scope_limited:
                tail = "（这一次是按范围 / 阶段跑的，别的段可能用得上；全图跑没命中才是真没钉）"
            elif self.machine_written:
                tail = (
                    "。其中 "
                    f"{self.machine_written} 条是机器写进去的，但这一次是显式"
                    "降级（--asset-gate warn）放行的 —— 是不是真没钉，自己确认"
                )
            else:
                tail = "（人自己写进文件的条目打不响是常态：写法在这一段没出现，不拦开工）"
        if self.corrections_total:
            tail += (
                f"（另有 {self.corrections_total} 条**待审更正**没裁：{corrections} ——"
                " `resource.term.pending.list` 看队列，采用之前书里一个字节都不动）"
            )
        return head + tail

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "units": self.units,
            "slots": self.slots,
            "usable": dict(self.usable),
            "corrections": dict(self.corrections),
            "hit": dict(self.hit),
            "usable_total": self.usable_total,
            "corrections_total": self.corrections_total,
            "hit_total": self.hit_total,
            "blocking": self.blocking,
            "worldbook_dropped": list(self.worldbook_dropped),
            "usable_names": list(self.usable_names),
            "scope_limited": self.scope_limited,
            "machine_written": self.machine_written,
            "workflow": [dict(item) for item in self.workflow],
            "summary": self.summary(),
        }


def request_texts(nodes: Iterable[Any]) -> list[str]:
    """把一批结点摊成**请求体里真实出现的文本**。

    口径必须是"发出去的那份文本"，不是"槽位原文"：说话人标注
    （``- [id:...] 单元内第 2/7 句 说话人：Eileen``）也在请求里，而术语表里
    钉的常常正是**人名**。拿槽位原文当口径，会把 `Eileen → 艾琳` 这种
    "只以说话人身份出现"的条目误报成"永远打不响"，从而拦下一次本来正常的跑批。

    没有槽位清单的老单元整条算一句 —— 与 :meth:`ProjectSession.source_slots` 同口径。
    """
    texts: list[str] = []
    for node in nodes:
        unit = getattr(node, "unit", None)
        if unit is None:
            continue
        unit_speaker = str(getattr(getattr(unit, "context", None), "speaker", "") or "").strip()
        payload = getattr(getattr(unit, "locator", None), "payload", None) or {}
        entries = payload.get("slots") or []
        if not entries:
            texts.append(
                f"{unit.source or ''} 说话人：{unit_speaker}" if unit_speaker else str(unit.source or "")
            )
            continue
        for entry in entries:
            source = str(entry.get("source") or "")
            speaker = str(
                entry.get("display_speaker") or entry.get("speaker") or unit_speaker or ""
            ).strip()
            texts.append(f"{source} 说话人：{speaker}" if speaker else source)
    return texts


#: 单次请求的槽位数超过这个数就提一句。来历：真靶实测 90 条的单元在 300 秒处被
#: 服务端回 HTTP 400（见 `TranslateOptions.unit_budget`）。
LARGE_UNIT_SLOTS = 90

#: 步骤提醒的文案模板：**只有事实与计数，没有"建议""应该""请先"**。
#: 这几句是"提醒而不是禁止"这条口径的落点，改它们之前先确认没把判断塞回去。
WORKFLOW_TEXTS = {
    "summaries": "摘要：{units} 个单元里 {covered} 个有事件卡",
    "glossary": "术语书：{usable} 条定译（引擎申报 {characters} 个角色名）",
    "unit_budget": (
        "请求大小：{units} 个单元的槽位数超过 {limit}（最大 {largest}），"
        "unit_budget={budget}"
    ),
    # 章是**项目申报的事实**（`<工作区>/chapters.json`），内核不替它猜。没申报时调度器
    # 会退回严格分层（`schedule.py::ChapterParallelScheduler`），而那条 notes 只有 `plan`
    # 命令会渲染 —— `translate` 不读 notes，于是"章没带进工作区"在那次跑批里等于没说过。
    # 实测代价：同一张图 phases 从 15 变成 32（数字在报告里，但没人拿它对照）。
    # 文案只陈述"申报了没有"，**不写应该怎么做**。
    "chapters": "章：{state}（剧情单元 {units} 个）",
    # 还没定译的写法：翻译时它们在原文里是 `⟦写法⟧`（模型不替它们起名字），
    # 定译是审核那一步的事（`layers/tags.py`）。只陈述"还有几条"，不劝。
    "term_tags": "待定译：术语书里 {writings} 个写法还没有译名（翻译时是 ⟦写法⟧）",
    # 术语书**会长出没人看过的东西**：模型在响应里申报的**新实体**、以及**填上原来空着的
    # 译名**直接进书；改已有的值（加事实 / 加写法 / 改译名）要走待审（`TermBook.apply`，
    # 口径见 2026-09-28 晚那次重做）。所以"我整理过的那份"和"现在注入的那份"可能不是同一份。
    # 只陈述条数：书多大、其中几条事实来自模型那条通道。
    # ⚠️ 数的是**改动日志的累计条数**（日志只追加）：整理过书之后它不会归零 —— 这条读数回答的
    # 是"历史上被自动收下过多少"，不是"现在书里有多少没人看过"。
    "termbook_review": (
        "术语书：{rows} 行 / {facts} 条事实，其中 {machine} 条事实是跑批中"
        "**模型申报自动收下**的（`termbook.changes.jsonl` 的模型通道），人还没看过"
    ),
    # **说话人显示名**：引擎把它单列一栏（`node_class=TranslateSpeaker`），骨架里没有
    # 落点 —— 要让它跟着翻译，得写进 `<工作区>/writeback/supplements.jsonl`（显式声明）。
    # 不声明的话：译文翻好了、账上有、写回时进 `unknown_slot`，而游戏里"谁在说话"
    # 那一栏一直是原文（真靶 2026-09-29 走查实测 78 条）。
    # 只陈述事实与条数。
    "speaker_names": (
        "说话人名：图上 {names} 个（引擎单列的一栏），已声明 {declared} 个 —— "
        "没声明的那些写回时没有落点，游戏里会是原文"
    ),
    # 模型提出的**更正**（改已有的译名/设定）：不采纳不生效，但也**不处理就一直在**。
    "termbook_pending": "术语书：{pending} 条待审更正没处理（采纳或驳回前都不生效）",
}

#: `chapters` 提醒里"申报状态"的两种说法 —— 事实，不含判断。
CHAPTER_DECLARED = "已申报"
CHAPTER_MISSING = "未申报（工作区里没有 chapters.json）"

#: 改动日志里**模型那条通道**的字样（`termbook.changes.jsonl` 的 `why` 前缀）。
#: 另有 `【通道】agent`（agent 整理）与人写的 —— 只有这条代表"没人看过就进了书"。
MODEL_CHANNEL = "【通道】model"


def workflow_notes(
    *,
    graph: Any,
    resources: Any,
    summaries: dict[str, dict] | None,
    unit_budget: int,
    workdir: Path | None = None,
) -> list[dict[str, str]]:
    """数一遍**还有哪一步没做**。只数事实，不判断该不该做。

    每条都只看"盘上现在有什么"，不预测、不排序、不建议。某一步做了就不出现 ——
    所以这份清单天然是"相对于当前状态"的，不需要谁去维护一个进度表。

    计数一律用**单元**（`PathGraph.translatable_nodes()`）：摘要是按单元攒的，
    拿别的粒度去数就会出现"提醒说没做、其实做了"这种自相矛盾。
    """
    notes: list[dict[str, str]] = []

    # ① 摘要：图上每个可译单元算一个待办
    units: dict[str, dict] = {}
    characters: list[str] = []
    if graph is not None:
        for node in graph.nodes.values():
            unit = getattr(node, "unit", None)
            if unit is None:
                continue
            units[str(unit.id)] = unit
        characters = sorted((getattr(graph, "metadata", None) or {}).get("characters") or {})
    total_units = len(units)
    index = summaries or {}
    # 摘要按 **unit_id** 命中（`load_summaries` 两把键都建，一个单元对一条卡）。
    # 只数得上号的那些：拿 label 去比会让同一 label 下的多个单元重复计数。
    covered = sum(1 for unit_id in units if unit_id in index)
    if total_units and covered < total_units:
        notes.append(
            {
                "code": "summaries",
                "text": WORKFLOW_TEXTS["summaries"].format(
                    units=total_units, covered=covered
                ),
            }
        )
    elif total_units == 0 and not index:
        # 连图都还没有：摘要无从谈起，如实说一句
        notes.append(
            {"code": "summaries", "text": WORKFLOW_TEXTS["summaries"].format(units=0, covered=0)}
        )

    # ② 术语书：一条定译都没有，而引擎自己申报了角色名
    usable = (
        len([e for e in resources.termbook.terms() if e.is_injectable])
        if resources is not None
        else 0
    )
    if usable == 0:
        notes.append(
            {
                "code": "glossary",
                "text": WORKFLOW_TEXTS["glossary"].format(
                    usable=usable, characters=len(characters)
                ),
            }
        )

    # ③ 待定译：术语书里一条译名都没有的写法。翻译时它们在原文里是 `⟦写法⟧`，
    #    名字要人等审核那一步定（定完写回时自动渲染）。**只陈述条数**。
    waiting = pending_writings(resources.termbook) if resources is not None else []
    if waiting:
        notes.append(
            {
                "code": "term_tags",
                "text": WORKFLOW_TEXTS["term_tags"].format(writings=len(waiting)),
            }
        )

    # ④ 术语书**是谁写的**：跑批中模型申报的新写法/新事实是**免审追加**的（`TermBook.apply`），
    #    下一批就注入 —— 所以"我整理过的那份"与"现在注入的那份"不是同一份，而这件事
    #    原先没有任何一处会说。这里只陈述条数（书多大、其中几条来自那条通道、几条待审）。
    if resources is not None:
        book = getattr(resources, "termbook", None)
        pending_access = getattr(book, "pending", None) if book is not None else None
        pending = len(pending_access.records()) if pending_access is not None else 0
        if pending:
            notes.append(
                {
                    "code": "termbook_pending",
                    "text": WORKFLOW_TEXTS["termbook_pending"].format(pending=pending),
                }
            )
        changes = getattr(book, "changes", None) if book is not None else None
        machine = 0
        if changes is not None:
            try:
                # 通道字样是 `【通道】model`（模型申报）/ `【通道】agent`（agent 整理）/
                # 人写的。这里只数**模型那条通道**：它代表"没人看过就进了书"。
                machine = sum(
                    1
                    for record in changes.records()
                    if str(record.get("what")) == "profile"
                    and MODEL_CHANNEL in str(record.get("why") or "")
                )
            except Exception:  # noqa: BLE001 —— 读不到改动日志不该让开跑前那一步炸掉
                machine = 0
        if machine:
            entries = book.entries()
            notes.append(
                {
                    "code": "termbook_review",
                    "text": WORKFLOW_TEXTS["termbook_review"].format(
                        rows=len(entries),
                        facts=sum(len(entry.facts) for entry in entries),
                        machine=machine,
                    ),
                }
            )

    # ⑤ 说话人名：引擎单列的那一栏（`node_class=TranslateSpeaker`）没有骨架落点，
    #    要跟着翻译就得写进 `<工作区>/writeback/supplements.jsonl`。
    #    不声明时游戏里那一栏是原文，而报告里**一个字都不会提**（真靶 78 条实测）。
    if graph is not None and resources is not None:
        speaker_names: set[str] = set()
        for node in graph.nodes.values():
            unit = getattr(node, "unit", None)
            if unit is None:
                continue
            payload = getattr(getattr(unit, "locator", None), "payload", None) or {}
            for slot in payload.get("slots") or []:
                # 引擎申报的形态类名，原样比对（`TranslateSpeaker` 是 Ren'Py 那一侧的写法）
                if str(slot.get("node_class") or "") != "TranslateSpeaker":
                    continue
                name = str(slot.get("source") or "").strip()
                if name:
                    speaker_names.add(name)
        declared = 0
        supplements = getattr(resources, "supplements", None)
        if supplements is not None:
            try:
                declared = len(
                    {
                        str(item.source).strip()
                        for item in supplements.entries()
                        if str(item.source).strip()
                    }
                    & speaker_names
                )
            except Exception:  # noqa: BLE001 —— 读不到声明不该让开跑前那一步炸掉
                declared = 0
        if speaker_names and declared < len(speaker_names):
            notes.append(
                {
                    "code": "speaker_names",
                    "text": WORKFLOW_TEXTS["speaker_names"].format(
                        names=len(speaker_names), declared=declared
                    ),
                }
            )

    # ④ 请求大小：不切分时，有多少单元会一次塞进去
    if unit_budget <= 0 and graph is not None:
        sizes = []
        for node in graph.nodes.values():
            unit = getattr(node, "unit", None)
            if unit is None:
                continue
            payload = getattr(getattr(unit, "locator", None), "payload", None) or {}
            entries = payload.get("slots") or []
            sizes.append(len(entries) if entries else 1)
        oversized = [size for size in sizes if size > LARGE_UNIT_SLOTS]
        if oversized:
            notes.append(
                {
                    "code": "unit_budget",
                    "text": WORKFLOW_TEXTS["unit_budget"].format(
                        units=len(oversized),
                        limit=LARGE_UNIT_SLOTS,
                        largest=max(oversized),
                        budget=unit_budget,
                    ),
                }
            )

    # ④ 章：项目申报的事实带进工作区了没有。
    # 只有"图上有剧情单元"时才说 —— 界面/工具文本本来就没有章，那时这句话是噪音。
    story_units = 0
    if graph is not None:
        for node in graph.nodes.values():
            unit = getattr(node, "unit", None)
            if unit is None:
                continue
            label = str((getattr(unit, "metadata", None) or {}).get("structure_label") or "")
            # 剧情单元的结构坐标是 label 名（`act1`）；界面文件那级是路径（`game/ui.rpy`），
            # 它没有 label，也就无从归章 —— 与 `chapters.py` 的命中口径一致。
            if label and "/" not in label:
                story_units += 1
    if story_units and workdir is not None:
        try:
            declared = (Path(workdir) / CHAPTERS_FILE).is_file()
        except OSError:  # pragma: no cover - 路径不可读时当成没申报，别让提醒本身炸掉
            declared = False
        notes.append(
            {
                "code": "chapters",
                "text": WORKFLOW_TEXTS["chapters"].format(
                    state=CHAPTER_DECLARED if declared else CHAPTER_MISSING,
                    units=story_units,
                ),
            }
        )
    return notes


def inspect_assets(
    resources: Any,
    nodes: Iterable[Any],
    *,
    use_glossary: bool = True,
    use_worldbook: bool = True,
    use_style: bool = True,
    scope_limited: bool = False,
    graph: Any = None,
    summaries: dict[str, dict] | None = None,
    unit_budget: int = 0,
    workdir: Path | None = None,
) -> AssetPreflight:
    """算这一次开工前的两件事：**资产用不用得上**（会拦）与**还有哪一步没做**（只提醒）。

    **只读，不发网络，不改任何东西。**

    ``graph`` / ``summaries`` / ``unit_budget`` / ``workdir`` 只喂给 :func:`workflow_notes`
    （步骤提醒），不参与资产判定，也**不影响** :attr:`AssetPreflight.blocking`。
    """
    nodes = list(nodes)
    slots = request_texts(nodes)
    preflight = AssetPreflight(
        units=len(nodes), slots=len(slots), scope_limited=bool(scope_limited)
    )
    # 步骤提醒先算：它只看盘上现在有什么，与资产判定互不影响。
    preflight.workflow = workflow_notes(
        graph=graph,
        resources=resources,
        summaries=summaries,
        unit_budget=unit_budget,
        workdir=workdir,
    )

    usable_glossary = [
        e for e in resources.termbook.terms() if e.is_injectable
    ] if use_glossary else []
    usable_worldbook = [
        e for e in resources.termbook.profiles() if e.is_injectable
    ] if use_worldbook else []
    usable_style = (
        [e for e in resources.style.entries()] if use_style else []
    )

    preflight.usable = {
        "术语": len(usable_glossary),
        "设定": len(usable_worldbook),
        "风格": len(usable_style),
    }
    # 待审更正不影响"能用几条"（它一个字都没改），但必须看得见：排着队没人裁，
    # 那条更正就等于没发生（见 `termbook.pending.jsonl`）。
    preflight.corrections = resources.pending_correction_counts()
    names = [entry.writing for entry in usable_glossary if entry.writing]
    names += [entry.writing for entry in usable_worldbook if entry.writing]
    preflight.usable_names = names[:8]
    machine = resources.machine_written()
    preflight.machine_written = sum(
        1
        for entry in [*usable_glossary, *usable_worldbook]
        if entry.writing in machine
    )

    hits_glossary = 0
    if usable_glossary and slots:
        # 与真正注入同一条路：逐句扫、拉丁键整词（`TermBook.lookup_in_slots`）。
        # 扫的是**请求体里那份文本**（含说话人标注），见 `request_texts`。
        hits_glossary = len(resources.termbook.lookup_in_slots(slots, limit=10_000))
    hits_worldbook = 0
    if usable_worldbook and slots:
        kept, dropped = resources.termbook.trigger(slots, limit=10_000, budget_chars=10**9)
        hits_worldbook = len(kept)
        preflight.worldbook_dropped = list(dropped)
    # 上面两行读数扫的是**请求里那份文本**（`request_texts`：正文 + 说话人标注），
    # 与真正注入的命中判据同一份口径。
    # 风格按作用域解析后注入（没有键这一层），所以"能用"就等于"会进"；
    # 它与术语 / 世界书是**两个池子**（§1.4.2），不互相抢额度。
    preflight.hit = {
        "术语": hits_glossary,
        "设定": hits_worldbook,
        "风格": len(usable_style),
    }

    if preflight.usable_total == 0:
        preflight.verdict = VERDICT_NO_ASSETS
    elif preflight.hit_total == 0:
        preflight.verdict = VERDICT_NEVER_FIRE
    else:
        preflight.verdict = VERDICT_OK
    return preflight
