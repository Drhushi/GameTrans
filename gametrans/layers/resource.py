"""资源层：翻译资源（**术语书**）+ 引擎资源（引擎支持包名册）。

设计要点 —— **资源是文件，不是黑盒**：

* ``termbook.jsonl``：术语书就是这一份文件，**一行一个实体**，人写给人的 JSON。
  一行五栏（照 SillyTavern 的世界书条目裁出来的部分）：

  ==========  ==================================================================
  ``key``     **一组写法**，每个写法带自己的译名：``[{"writing","target"}, …]``。
              **任一写法在原文里命中即触发这一行**；``key[0].writing`` 只是身份
              与显示名，没有任何逻辑优先（`Eve Herschel` 命中时用它自己的
              `伊芙·赫歇尔` 渲染，不是 `伊芙`）。``key`` 为空的行不注入。
  ``profile`` **追加式事实列表**，一个元素一条事实（"24岁，是学生"）。注入时用
              ``；`` 连成一行。
  ``constant`` ``True`` = 蓝灯（没有写法命中也永远注入：世界观 / 风格类）；
              ``False`` = 绿灯（按键触发：人名 / 地名 / 组织）。
  ``order``   整数，**数值大的更靠后注入**（更靠近提示词末尾）。蓝灯行先注入。
  ``position`` 只有 ``"terms"`` 与 ``"tail"`` 两档，见 :func:`injection_sort_key`。
  ==========  ==================================================================

  注入判定只看内容：**有任何非空 ``key[].target`` 或任何一条非空 ``profile`` 事实
  就注入**，两者都空就不注入。这里没有审核状态闸门。

* ``termbook.changes.jsonl``：变更日志（追加式）。每次状态变更一条，作用是新旧对照，
  以及让"统一替换"能从差异里取回**输掉的那个写法** —— 所以条目里不再存 alternatives。
* ``termbook.pending.jsonl``：待审更正。**追加免审、更正要审**：改已有事实或改已有
  译名不许直接改，先落成一条带稳定 id 的提案，人或 agent 采用之后才替换。

翻译层在翻译过程中对游戏世界产生的理解（谁是谁、专名怎么译、语气如何）落盘到这一份
文件，于是下一批、下一个章节、甚至下一个同世界观的项目都能复用同一套理解 —— 这就是
"一致性"的物理载体。agent 也可以直接编辑它，软件负责校验与把相关内容注入翻译上下文。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.models import Issue
from gametrans.layers.knowledge import APPROVERS, KnowledgeError
from gametrans.layers.memory import TranslationMemory
from gametrans.layers.deviations import DeviationStore
from gametrans.layers.supplements import SupplementSet
from gametrans.layers.style import STYLE_FILE, StyleEntry, StyleGuide
from gametrans.layers.tags import writings_to_tag
from gametrans.layers.trigger import TriggerPolicy, hit_counts, key_in_text, scan_slots
from gametrans.engines.registry import EngineRegistry

TERMBOOK_FILE = "termbook.jsonl"
CHANGES_FILE = "termbook.changes.jsonl"
PENDING_FILE = "termbook.pending.jsonl"

#: ``position`` 的两档。``tail`` 是"提示词靠末尾那一段"的落点：产品今天还没有独立的
#: 末尾段，所以它排在【术语书】段内的最后（见 :func:`injection_sort_key`）。
POSITION_TERMS = "terms"
POSITION_TAIL = "tail"
POSITIONS: tuple[str, ...] = (POSITION_TERMS, POSITION_TAIL)

#: ``order`` 的缺省值：新行落在中间，往前调小、往后调大。
DEFAULT_ORDER = 100

#: 变更 / 待审记录里 ``what`` 的取值。``constant`` / ``order`` / ``position`` 只是
#: 排序与开关，没有"事实"的语义，所以它们**直接改**（待审只受理 ``key`` 与 ``profile``）。
CHANGE_KINDS: tuple[str, ...] = ("profile", "key", "constant", "order", "position")
PENDING_KINDS: tuple[str, ...] = ("profile", "key")

#: **直接入库**的写入者（不排队等复核）：人拍的板与旧台账迁移 —— 它们本来就是显式动作。
#: 模型在翻译里捎带出来的（`by="model"` / `"agent"`）走待审："条目被改了就要审"。
DIRECT_WRITERS: frozenset[str] = frozenset({"human", "import"})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def why_text(why: str, by: str) -> str:
    """``why`` 里带上**通道**（谁写的）：记录形状照规格只有那六个字段，通道只能住这里。"""
    text = str(why or "").strip()
    channel = str(by or "").strip()
    if not channel:
        return text
    return f"{CHANNEL_PREFIX}{channel}；{text}" if text else f"{CHANNEL_PREFIX}{channel}"


def _channel_of(why: str) -> str:
    text = str(why or "")
    if not text.startswith(CHANNEL_PREFIX):
        return ""
    return text[len(CHANNEL_PREFIX):].split("；", 1)[0].strip()


MEMORY_FILE = "memory.jsonl"
SUPPLEMENTS_FILE = "supplements.jsonl"
DEVIATIONS_FILE = "deviations.jsonl"


# --------------------------------------------------------------------------- #
# 归一：五栏里那两栏是列表，进来的时候什么形状都有
# --------------------------------------------------------------------------- #


def _norm_key(raw: Any) -> list[dict[str, str]]:
    """写法列表归一 → ``[{"writing","target"}, …]``。

    接受行内形状（``{"writing","target"}``）、旧的 ``{"source","target"}``、
    ``{写法: 译名}``、以及裸字符串。同一个写法只留一条：后出现的那条**非空**译名补上
    前面的空译名，已有的非空译名不被清掉。
    """
    items = raw.items() if isinstance(raw, dict) else (raw or ())
    merged: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for item in items:
        if isinstance(item, dict):
            writing = str(item.get("writing") or item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
        elif isinstance(item, str):
            writing, target = item.strip(), ""
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            writing, target = str(item[0] or "").strip(), str(item[1] or "").strip()
        else:
            continue
        if not writing:
            continue
        slot = merged.get(writing)
        if slot is None:
            merged[writing] = {"writing": writing, "target": target}
            order.append(writing)
            continue
        if target and not slot["target"]:
            slot["target"] = target
    return [merged[writing] for writing in order]


def _norm_profile(raw: Any) -> list[str]:
    """事实列表归一 → 非空字符串列表（保序、去重）。

    **字符串当作一条事实**（旧形状那一栏就是一个字符串）：不按行拆，拆了会把一条设定
    变成两条，而"这条设定本来该不该分"是写的人当场决定的事。面板与命令行要写多条事实
    时给的是列表（或按行拆好的列表，见 :func:`facts_from_text`）。
    """
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    facts: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in facts:
            facts.append(text)
    return facts


def facts_from_text(text: Any) -> list[str]:
    """面板 / 命令行那一栏是**一段文本**：一行一条事实。"""
    return _norm_profile([line for line in str(text or "").splitlines()])


def _merge_into(current: "TermEntry", entry: "TermEntry") -> "TermEntry":
    """把 ``entry`` **逐栏并进** ``current``：只填不覆盖（缺的那一栏当"没意见"）。

    命令行只给一栏时不该把另一栏清空（旧代码里那条"非空沿用"的底线）；要整行替换
    走 ``add(exact=True)``（面板的编辑表单）。
    """
    merged = replace(
        current,
        key=[{"writing": item["writing"], "target": item["target"]} for item in current.key],
        profile=list(current.profile),
    )
    for item in entry.key:
        slot = next(
            (row for row in merged.key if row["writing"] == item["writing"]), None
        )
        if slot is None:
            merged.key.append({"writing": item["writing"], "target": item["target"]})
        elif item["target"]:
            slot["target"] = item["target"]
    _merge_profile(merged.profile, entry.profile)
    merged.constant = entry.constant or merged.constant
    if entry.order != DEFAULT_ORDER:
        merged.order = entry.order
    if entry.position != POSITION_TERMS:
        merged.position = entry.position
    return merged


def same_fact(left: str, right: str) -> bool:
    """两条事实是不是**同一件事**（去标点后互为子串即算）。

    为什么不是逐字比：模型是**一批一句**地申报设定的，同一个实体被交代过几次就有几条
    措辞略异的说法 —— 真靶工程实测 `BSU` 那一行 24 条、`SIT` 那一行 42 条，
    绝大多数是同义复述（"BSU 校队的队名。"/"BSU 校队名称。"）。逐字去重一条都挡不住，
    于是这一行一命中就把几十句近义话塞进请求，而且一拆行就分不清哪条属于谁。
    """
    def plain(text: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or ""))

    left, right = plain(left), plain(right)
    if not left or not right:
        return False
    return left in right or right in left


#: 「换个说法说的同一件事」的相似度门槛（去标点后的 2-gram Jaccard）。
#:
#: 为什么在 :func:`same_fact` 之外还要这一条：`same_fact` 是"去标点后互为子串"，
#: 挡得住逐字与子串，**挡不住换语序与插词**。真靶实测（2026-09-29，act3）：模型申报
#: 的 3 条"新事实"与书上已有事实的相似度是 0.85 / 0.87 / 0.74 —— 全是同一件事换了个说法，
#: 而它们一条都没被 `same_fact` 拦住，全排进了待审队列。
NEAR_FACT_THRESHOLD = 0.6


def fact_similarity(left: str, right: str) -> float:
    """两条事实有多像（去标点后的 2-gram Jaccard）—— 0 到 1。"""
    def grams(text: str) -> set[str]:
        plain = re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or ""))
        return {plain[i:i + 2] for i in range(max(0, len(plain) - 1))}

    left_grams, right_grams = grams(left), grams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def near_fact(left: str, right: str, *, threshold: float = NEAR_FACT_THRESHOLD) -> bool:
    """两条事实是不是**换个说法说的同一件事**（相似度 ≥ 门槛）。

    ⚠️ 它**不做决定**，只用来标注与报数 —— 待审队列里那条提案照样排队，`why` 里多一句
    "疑似与已有第 N 条重复"，复核的人一眼能看出这是复述而不是新知识。
    """
    return fact_similarity(left, right) >= threshold


def _merge_profile(profile: list[str], incoming: Iterable[str]) -> int:
    """把 ``incoming`` 里**没说过**的事实并进 ``profile``，返回追加了几条。

    判"没说过"用 :func:`same_fact`（近义），不是逐字 —— 这是这条通道唯一的收敛点。
    """
    added = 0
    for fact in incoming:
        text = str(fact or "").strip()
        if not text or any(same_fact(text, kept) for kept in profile):
            continue
        profile.append(text)
        added += 1
    return added


def _require_human(by: str, action: str) -> str:
    """采用 / 丢弃 / 直接编辑都要**人和 agent** 拍板，模型身份一律拒绝。"""
    identity = str(by or "").strip().lower()
    if identity not in APPROVERS:
        raise KnowledgeError(
            f"审核身份 {by!r} 不能{action}（只认 {', '.join(sorted(APPROVERS))}）",
            hint="模型产出只能提提案；要落成事实必须由人或 agent 拍板。",
        )
    return identity


# --------------------------------------------------------------------------- #
# 术语书：一份文件，一行一个实体（五栏）
# --------------------------------------------------------------------------- #


@dataclass
class TermEntry:
    """术语书的一行 = **一个实体**，五栏。"""

    #: 一组写法：``[{"writing": "Eve", "target": "伊芙"}, …]``。
    #: **任一写法在原文里命中即触发这一行**，每个写法用它自己的译名渲染。
    key: list[dict[str, str]] = field(default_factory=list)
    #: 追加式事实列表：一个元素一条事实。
    profile: list[str] = field(default_factory=list)
    #: 蓝灯：没有写法命中也永远注入（世界观 / 风格类）。
    constant: bool = False
    #: 注入顺序：数值大的更靠后；蓝灯行与 ``position`` 会先分组，见 `injection_sort_key`。
    order: int = DEFAULT_ORDER
    #: ``"terms"`` / ``"tail"``
    position: str = POSITION_TERMS

    def __post_init__(self) -> None:
        self.key = _norm_key(self.key)
        self.profile = _norm_profile(self.profile)
        self.order = DEFAULT_ORDER if self.order is None else int(self.order)
        if self.position not in POSITIONS:
            self.position = POSITION_TERMS

    # ---- 派生 ---------------------------------------------------------------

    @property
    def writing(self) -> str:
        """身份与显示名 = ``key[0].writing``。

        **它没有任何逻辑优先**：命中判定与渲染都是按整个 ``key`` 逐条来的。它只回答
        "这一行叫什么"，用来在面板上显示、在变更日志里指认是哪一行。
        """
        return self.key[0]["writing"] if self.key else ""

    @property
    def writings(self) -> list[str]:
        return [item["writing"] for item in self.key]

    @property
    def targets(self) -> list[tuple[str, str]]:
        """带译名的写法（``(写法, 译名)``，按写入顺序）—— 注入时逐条出行。"""
        return [
            (item["writing"], item["target"]) for item in self.key if item["target"]
        ]

    @property
    def facts(self) -> list[str]:
        return [fact for fact in self.profile if fact]

    @property
    def is_term(self) -> bool:
        """有非空 ``key[].target`` —— 这一行出译名行。"""
        return bool(self.targets)

    @property
    def is_profile(self) -> bool:
        """有非空事实 —— 这一行出设定行。"""
        return bool(self.facts)

    @property
    def is_injectable(self) -> bool:
        """注入判定：**有写法，而且有译名或有事实**。

        ``key`` 为空的行不注入（它连"什么时候该说话"都没有）；这里**没有审核状态**。
        """
        return bool(self.key) and (self.is_term or self.is_profile)

    @property
    def trigger_keys(self) -> list[str]:
        """拿什么去触发 —— 全部写法（有译名的与没译名的都算这一行的写法）。"""
        return [item["writing"] for item in self.key if item["writing"]]

    def target_for(self, writing: str) -> str:
        """某个写法自己的译名（写法不在这一行里就是空串）。"""
        wanted = str(writing or "").strip()
        for item in self.key:
            if item["writing"] == wanted:
                return item["target"]
        lowered = wanted.lower()
        for item in self.key:
            if item["writing"].lower() == lowered:
                return item["target"]
        return ""

    def matches(self, text: str, *, policy: TriggerPolicy | None = None) -> bool:
        """这个词（**任一写法**）在不在文本里出现（整词规则见 `layers/trigger`）。"""
        policy = policy or TriggerPolicy()
        return any(
            key_in_text(text, writing, policy=policy)
            for writing in self.trigger_keys
        )

    def hit_writings(self, text: str, *, policy: TriggerPolicy | None = None) -> list[str]:
        """文本里命中了这一行的哪几个写法（按写入顺序）。"""
        policy = policy or TriggerPolicy()
        return [
            writing
            for writing in self.trigger_keys
            if key_in_text(text, writing, policy=policy)
        ]

    def profile_line(self) -> str:
        """事实连成一行（注入时就是这么用的）。"""
        return "；".join(self.facts)

    # ---- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """**只有这五栏**。删掉的栏一个都不写（不写兼容用的空栏）。"""
        return {
            "key": [
                {"writing": item["writing"], "target": item["target"]} for item in self.key
            ],
            "profile": list(self.profile),
            "constant": self.constant,
            "order": self.order,
            "position": self.position,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TermEntry":
        """读一行。**新形状直读；旧形状折成新形状**。

        旧形状的折法（读路径要能读旧文件，一次都不写盘）：

        * ``source`` 与 ``variants[].source`` → ``key`` 的写法列表，各自的 ``target``
          尽量带上（``source`` 用行上的 ``target``，变体用它自己的 ``target``）；
        * ``profile`` 字符串非空 → **单元素事实列表**；
        * 其余旧栏（``case_sensitive`` / ``tags`` / ``status`` / ``evidence`` /
          ``provenance`` / ``decided_by`` / ``note``）**丢掉**。
        """
        if not isinstance(data, dict):
            return cls()
        if "key" in data:
            return cls(
                key=list(data.get("key") or []),
                profile=list(data.get("profile") or []),
                constant=bool(data.get("constant")),
                order=data.get("order"),
                position=str(data.get("position") or POSITION_TERMS),
            )
        key: list[dict[str, str]] = []
        source = str(data.get("source") or "").strip()
        if source:
            key.append({"writing": source, "target": str(data.get("target") or "").strip()})
        for variant in data.get("variants") or ():
            if isinstance(variant, dict):
                writing = str(variant.get("source") or "").strip()
                target = str(variant.get("target") or "").strip()
            else:
                writing, target = str(variant or "").strip(), ""
            if writing:
                key.append({"writing": writing, "target": target})
        profile = str(data.get("profile") or "").strip()
        return cls(
            key=key,
            profile=[profile] if profile else [],
            constant=bool(data.get("constant", False)),
        )

    @staticmethod
    def is_old_shape(data: Any) -> bool:
        """这一行是旧形状吗（有 ``source`` 而没有 ``key``）。"""
        return (
            isinstance(data, dict)
            and "key" not in data
            and "source" in data
        )


def injection_sort_key(entry: TermEntry) -> tuple[int, int, int, str]:
    """注入顺序：``(position, constant, order, 身份)``。

    三条规格合成一个键：

    * ``tail`` 的行排在【术语书】段内的**最后** —— 所以 ``position`` 是最外层。将来若
      加独立的"提示词末尾段"，这里就是那个落点：换掉这一档的去处即可，条目不用动；
    * 蓝灯行（``constant``）**先注入**；
    * 其余按 ``order`` **升序**（数值大的更靠后、更靠近提示词末尾）；
    * 身份兜底，保证同一份文件任何一次读出来的顺序都一样（幂等与指纹都靠它）。
    """
    return (
        0 if entry.position == POSITION_TERMS else 1,
        0 if entry.constant else 1,
        entry.order,
        entry.writing,
    )


# --------------------------------------------------------------------------- #
# 【术语书】的注入形状 —— **两份渲染器共用这一处**
# --------------------------------------------------------------------------- #

#: 设定行的标签。术语书一行一个实体、两栏（叫法 / 设定），注入时合成一段。
#:
#: ⚠️ 以前设定行也写成 ``- 写法 → 一段中文``，跟译名行同一个箭头，而且左边只挂这一行的
#: **第一个**写法（`Fiona`）—— 于是一个多写法的行在请求里长这样：:
#:
#:     - Fiona → 菲奥娜
#:     - Fiona → BSU 女排队员，球场上最难缠的对手；去年因一起事件被停学。；…
#:     - Fiona Valentine → 菲奥娜·瓦伦丁
#:
#: 读起来像"这个写法还能这么译"，而全名那一行看上去"还没有设定"。真靶实测（2026-09-29）：
#: 模型把**已经写过的事实**又申报了一遍，而且三条全落在全名上
#: （`Fiona Valentine` / `Saki Natsume` / `Zoe Lyn Campbell`）。
SETTING_LABEL = "设定｜"

#: 段头那一句口径。**跟着请求走**而不是只写在提示词模板里：模板是用户可改的，改模板
#: 不该让这一段没法读（与 :func:`gametrans.layers.tags.announce` 同一个理由）。
TERMBOOK_LEGEND = (
    "（`- 写法 → 译名` 是那个写法的叫法；`- {label}<写法> / <写法>：…` 是**这一行那个实体**"
    "的设定，左边列的写法都属于它 —— 两种都已经定过）".format(label=SETTING_LABEL)
)


def term_content(writing: str, target: str) -> str:
    """一条译名行的正文（不含行首的 ``- ``）。"""
    return f"{writing} → {target}"


def setting_content(entry: TermEntry) -> str:
    """一条设定行的正文（不含行首的 ``- ``）：左栏列**这一行的全部写法**。

    列全的理由：设定说的是**这个实体**，它有几组写法就都算。只挂第一个写法，别的写法
    在请求里看着就像"还没有设定"（真靶踩过，见 :data:`SETTING_LABEL`）。
    """
    return f"{SETTING_LABEL}{' / '.join(entry.writings)}：{entry.profile_line()}"


def row_key(content: str) -> str:
    """这一条注入行属于术语书里的**哪一行** —— 同一行的几栏要连着放。

    译名行取左栏的写法（``Eve → 伊芙`` → ``Eve``）；设定行取左栏的第一个写法
    （``设定｜Eve / Eve Herschel：…`` → ``Eve``），于是与那一行的第一条译名行同键。
    """
    text = str(content)
    if text.startswith(SETTING_LABEL):
        text = text[len(SETTING_LABEL):]
    text = text.partition("：")[0].partition(" → ")[0]
    return text.partition(" / ")[0].strip()


# --------------------------------------------------------------------------- #
# 两份外部文件：变更日志 + 待审更正
# --------------------------------------------------------------------------- #


#: 变更日志里**通道**的固定前缀。记录形状照规格只有那六个字段
#: （``at`` / ``what`` / ``writing`` / ``old`` / ``new`` / ``why``），所以"这条是哪条
#: 通道写的"只能写在 ``why`` 里 —— 预检要靠它认出"机器写进去的行"（见
#: :meth:`ChangeLog.writers`）。
CHANNEL_PREFIX = "【通道】"


class ChangeLog:
    """``termbook.changes.jsonl``：**变更日志**，一行一条，只追加。

    记录形状：``{"at", "what", "writing", "old", "new", "why"}``。

    两个用处：新旧对照（人要能看见"这一栏原来是什么"），以及**统一替换**要知道
    "输掉的那个写法是哪个" —— 它从差异里的 ``old`` 取（见 :meth:`revisions`），
    所以条目里不再存 alternatives。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        found: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                found.append(payload)
        return found

    def append(
        self,
        *,
        what: str,
        writing: str = "",
        old: Any = "",
        new: Any = "",
        why: str = "",
        by: str = "",
    ) -> dict[str, Any]:
        record = {
            "at": _now(),
            "what": str(what),
            "writing": str(writing or ""),
            "old": old,
            "new": new,
            "why": why_text(why, by),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def writers(self) -> dict[str, str]:
        """``{写法: 这条是哪条通道写的}`` —— 取**最早**那条变更记录里的通道。

        预检靠它认出"机器写进去的行"（``model`` / ``summary``）：
        "机器写的资产一条都进不了请求"是事故形态，人自己写的不是。
        """
        found: dict[str, str] = {}
        for record in self.records():
            writing = str(record.get("writing") or "").strip()
            if not writing or writing in found:
                continue
            channel = _channel_of(str(record.get("why") or ""))
            if channel:
                found[writing] = channel
        return found

    def revisions(self) -> dict[str, dict[str, Any]]:
        """按写法汇总 ``key`` 那一栏的历次改动。

        ``{写法: {"target": 现在的译名, "lost": [被换掉的译名, …]}}`` —— 现译名取这条
        写法上**最后一次**变更的 ``new``；被换掉的取所有非空且不等于现译名的 ``old``
        （按出现顺序、去重）。统一替换按 ``lost`` 扫已落盘译文。
        """
        found: dict[str, dict[str, Any]] = {}
        for record in self.records():
            if str(record.get("what")) != "key":
                continue
            writing = str(record.get("writing") or "").strip()
            if not writing:
                continue
            slot = found.setdefault(writing, {"target": "", "lost": []})
            new = str(record.get("new") or "").strip()
            old = str(record.get("old") or "").strip()
            if new:
                slot["target"] = new
            if old:
                slot["lost"].append(old)
        for writing, slot in found.items():
            current = slot["target"]
            lost: list[str] = []
            for text in slot["lost"]:
                if text and text != current and text not in lost:
                    lost.append(text)
            slot["lost"] = lost
            if not current:
                # 最后一次变更是把译名清空（删写法那一类）：不拿它当"现译名"。
                found[writing] = {"target": "", "lost": lost}
        return found

    def lost_targets(self, writing: str) -> list[str]:
        """这个写法历史上被换掉的译名（统一替换的替换源只来自这里）。"""
        return list(self.revisions().get(str(writing or "").strip(), {}).get("lost") or [])


class PendingChanges:
    """``termbook.pending.jsonl``：**待审更正**，一行一条提案，带稳定 id。

    记录形状：``{"at", "what", "writing", "index", "old", "new", "why", "id"}``。

    id 由 ``(what, writing, index, new)`` 内容寻址，所以同一个提案重复提也只占一行 ——
    这条通道是**幂等**的（重复跑不会把队列撑长）。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def make_id(what: str, writing: str, index: Any, new: Any) -> str:
        payload = json.dumps(
            [str(what), str(writing or ""), index, new], ensure_ascii=False, sort_keys=True
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        return f"pc:{digest}"

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        found: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("id"):
                found.append(payload)
        return found

    def get(self, pid: str) -> dict[str, Any] | None:
        for record in self.records():
            if record.get("id") == pid:
                return record
        return None

    def for_writing(self, writing: str) -> list[dict[str, Any]]:
        wanted = str(writing or "").strip()
        return [
            record
            for record in self.records()
            if str(record.get("writing") or "") == wanted
        ]

    def propose(
        self,
        *,
        what: str,
        writing: str = "",
        index: Any = None,
        old: Any = "",
        new: Any = "",
        why: str = "",
    ) -> dict[str, Any]:
        """提一条更正。同一个内容已经排着队就不重复提（返回已有的那条）。"""
        if what not in PENDING_KINDS:
            raise KnowledgeError(
                f"待审只受理 {' / '.join(PENDING_KINDS)} 两栏（收到 {what!r}）",
                hint="constant / order / position 是开关与排序，不是事实，可以直接改。",
            )
        pid = self.make_id(what, writing, index, new)
        existing = self.get(pid)
        if existing is not None:
            return existing
        record = {
            "at": _now(),
            "what": str(what),
            "writing": str(writing or ""),
            "index": index,
            "old": old,
            "new": new,
            "why": str(why or ""),
            "id": pid,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def drop(self, pid: str) -> bool:
        wanted = str(pid or "").strip()
        if not wanted:
            return False
        records = self.records()
        kept = [record for record in records if record.get("id") != wanted]
        if len(kept) == len(records):
            return False
        self._write(kept)
        return True

    def drop_for(self, *, what: str = "", writing: str = "") -> int:
        """撤掉某一处的提案（**人已经拍了板**：旧提案留着会在"采用"时把人的决定覆盖回去）。"""
        records = self.records()
        kept = [
            record
            for record in records
            if not (
                (not what or str(record.get("what")) == what)
                and (not writing or str(record.get("writing") or "") == str(writing))
            )
        ]
        removed = len(records) - len(kept)
        if removed:
            self._write(kept)
        return removed

    def _write(self, records: list[dict[str, Any]]) -> None:
        body = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        self.path.write_text(body + "\n" if body else "", encoding="utf-8")


class TermBook:
    """术语书：读写 ``termbook.jsonl`` —— **一行一个实体**，人写给人的 JSON。

    旧工程里有三份东西，第一次走写路径时会折进来（原文件改名 ``.bak`` 留着）：

    * ``glossary.jsonl``（原文 → 定译） → 填这一行 ``key`` 里某个写法的 ``target``；
    * ``worldbook.jsonl``（触发词 → 设定） → 填同一行的 ``profile``；
    * 更早的 ``worldbook.md``（Markdown） → 先读成 jsonl 那一侧再折。

    折的时候**按写法合行**：同一个词上的译名与设定合并成一行。**读路径不写盘**：正本
    不在时照样把旧文件读成合并后的样子，但不因为读了一下就动别人的工程。

    形状升级同理：正本已经是**旧五栏以外的那种行形状**时，读路径照样读得进来（:meth:`TermEntry.from_dict`
    会折），第一次走写路径（:meth:`ensure` / :meth:`add` / :meth:`apply`）才整份改写成
    新形状，并把旧文件留成 ``termbook.jsonl.bak``。
    """

    #: 正本
    NAME = TERMBOOK_FILE
    #: **否决清单**：判过"这个词不该进书"的原文写法。它单独一份，因为术语书里
    #: 只该有**在用的行** —— 但"已经否决过"这件事必须留着：不留，"下一个人再申报一次
    #: 就会重新登记，否决被静默抹掉"（R59 记过的坑）。
    REJECTED_NAME = "termbook.rejected.jsonl"
    #: 旧文件（只作为**一次性折叠**的输入存在，读路径也认）
    LEGACY_JSONL = ("glossary.jsonl", "worldbook.jsonl")
    LEGACY_MARKDOWN = "worldbook.md"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.changes = ChangeLog(self.path.parent / CHANGES_FILE)
        self.pending = PendingChanges(self.path.parent / PENDING_FILE)
        #: (文件指纹 → 行, 问题)。翻译层每条文本都要查它，不缓存的话真靶会把同一个
        #: 文件重解析上万次（实测 26 秒）。
        self._cache: tuple[Any, list[TermEntry], list[Issue]] | None = None
        self._lock = threading.RLock()

    # ---- 旧文件在哪 ---------------------------------------------------------

    @property
    def legacy_paths(self) -> tuple[Path, ...]:
        return tuple(self.path.parent / name for name in self.LEGACY_JSONL)

    @property
    def legacy_markdown(self) -> Path:
        return self.path.parent / self.LEGACY_MARKDOWN

    @property
    def rejected_path(self) -> Path:
        """否决清单住哪（与正本同一个目录，面板不读它）。"""
        return self.path.parent / self.REJECTED_NAME

    @property
    def backup_path(self) -> Path:
        """形状升级前的旧文件留在这里。"""
        return self.path.with_name(self.path.name + ".bak")

    # ---- 否决清单 -----------------------------------------------------------

    def rejected(self) -> dict[str, dict[str, Any]]:
        """``{原文写法: 那条否决记录}`` —— 判过"不该进书"的词。"""
        path = self.rejected_path
        if not path.exists():
            return {}
        found: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and str(payload.get("source") or "").strip():
                found[str(payload["source"])] = payload
        return found

    def is_rejected(self, writing: str) -> bool:
        """这个词被判过"不进书"吗（书里已经有行时不算 —— 人后来加回来了）。"""
        wanted = str(writing).strip()
        if not wanted or self.get(wanted) is not None:
            return False
        return wanted in self.rejected()

    def pending_writings(self) -> set[str]:
        """哪几个写法**正排着队等复核**（模型捎带出来的改动都在这儿）。

        这就是"书上悬着的东西"：复核之前书里那些行**还是旧值**，所以一个节点只要在原文里
        碰上其中任何一个，那一轮就不该翻（见 `layers/translate.py` 的开跑闸）。

        ⚠️ **按行展开**：一条提案挂在那一行的**身份**上（`entry.writing`），而一行往往有好几个
        写法（`Fiona` / `Fiona Valentine`）。只拿提案里那个写法去比，原文里写短名就漏过去了 ——
        而那一行**整行**都在等复核。所以这里连同该行的**全部写法**一起报出来。
        """
        found: set[str] = set()
        for record in self.pending.records():
            writing = str(record.get("writing") or "").strip()
            if not writing:
                continue
            found.add(writing)
            # 提案的 `new` 里带着"要挂上来的新写法"（`{writing, target}`）—— 它还不属于任何行
            new = record.get("new")
            if isinstance(new, dict) and str(new.get("writing") or "").strip():
                found.add(str(new["writing"]).strip())
            entry = self.find(writing)
            if entry is not None:
                found.update(str(item).strip() for item in entry.writings if str(item).strip())
        return found

    def record_rejection(self, writing: str, *, reason: str = "", by: str = "") -> None:
        """记下"这个词不进书"（追加/覆盖一条），并把书里那一行撤掉。"""
        wanted = str(writing).strip()
        if not wanted:
            return
        payload = json.dumps(
            {"source": wanted, "reason": reason, "by": by, "at": _now()},
            ensure_ascii=False,
        )
        self.rejected_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            line
            for line in (
                self.rejected_path.read_text(encoding="utf-8").splitlines()
                if self.rejected_path.exists()
                else []
            )
            if line.strip()
        ]
        kept = []
        for line in lines:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if isinstance(existing, dict) and existing.get("source") == wanted:
                continue
            kept.append(line)
        kept.append(payload)
        self.rejected_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        self.remove(wanted, why=reason or "已否决")

    def clear_rejection(self, writing: str) -> bool:
        """撤掉一条否决（人显式把这个词加回书里时由 :meth:`add` 调用）。"""
        wanted = str(writing).strip()
        records = self.rejected()
        if wanted not in records:
            return False
        kept = [
            json.dumps(record, ensure_ascii=False)
            for key, record in records.items()
            if key != wanted
        ]
        body = "\n".join(kept)
        self.rejected_path.write_text(body + "\n" if body else "", encoding="utf-8")
        return True

    def status_of(self, writing: str) -> str | None:
        """这个词现在算哪种状态：书里有行 → ``""``（生效）；否决清单里 → ``"rejected"``。

        书里没有、清单里也没有 → ``None``（从没见过）。**没有"待审"这个状态** ——
        审核只发生在更正上，而更正住在 ``termbook.pending.jsonl``，不在这条通道上。
        """
        if self.get(writing) is not None:
            return ""
        if self.is_rejected(writing):
            return "rejected"
        return None

    # ---- 读写 ---------------------------------------------------------------

    @staticmethod
    def _file_stamp(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _stamp(self) -> Any:
        """缓存指纹：正本与所有旧文件都算进来 —— 过渡期里旧文件才是数据源。"""
        return (
            self._file_stamp(self.path),
            tuple(self._file_stamp(p) for p in self.legacy_paths),
            self._file_stamp(self.legacy_markdown),
        )

    def ensure(self) -> "TermBook":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            if any(p.exists() for p in (*self.legacy_paths, self.legacy_markdown)):
                self.migrate_legacy()
            else:
                self.path.write_text("", encoding="utf-8")
        else:
            self.reshape_old_shape()
        return self

    def _raw_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def reshape_old_shape(self) -> int:
        """正本是**旧形状**时：整份改写成新形状，旧文件留 ``.bak``。返回改写的行数。

        只在写路径上做（:meth:`ensure`）。读路径照样读得进来（``from_dict`` 会折），
        所以读别人的工程不会动它一个字节。
        """
        raw = self._raw_lines()
        if not raw:
            return 0
        old_shape = 0
        new_lines: list[str] = []
        for line in raw:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)  # 解析不了的行原样留着，不替人做决定
                continue
            if TermEntry.is_old_shape(payload):
                old_shape += 1
                entry = TermEntry.from_dict(payload)
                if entry.writing:
                    new_lines.append(json.dumps(entry.to_dict(), ensure_ascii=False))
                continue
            new_lines.append(line)
        if not old_shape:
            return 0
        if not self.backup_path.exists():
            self.backup_path.write_text(
                "\n".join(raw) + "\n" if raw else "", encoding="utf-8"
            )
        self._write(new_lines)
        return old_shape

    def migrate_legacy(self) -> int:
        """把旧的术语表 / 世界书折成一份 ``termbook.jsonl``，旧文件改名 ``.bak``。

        只在**写路径**上做（:meth:`ensure`）；读路径不动机器上的文件。
        """
        if self.path.exists():
            return 0
        entries, _problems = self._from_legacy()
        if not entries and not any(
            p.exists() for p in (*self.legacy_paths, self.legacy_markdown)
        ):
            return 0
        lines = [
            json.dumps(entry.to_dict(), ensure_ascii=False)
            for entry in entries
            if entry.writing
        ]
        body = "\n".join(lines)
        self.path.write_text(body + "\n" if body else "", encoding="utf-8")
        for old in (*self.legacy_paths, self.legacy_markdown):
            if old.exists():
                old.rename(old.with_name(old.name + ".bak"))
        with self._lock:
            self._cache = None
        return len(lines)

    def _parsed(self) -> tuple[list[TermEntry], list[Issue]]:
        stamp = self._stamp()
        cached = self._cache
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2]
        with self._lock:
            cached = self._cache
            if cached is not None and cached[0] == stamp:
                return cached[1], cached[2]
            if self.path.exists():
                entries, problems = self._parse(self._raw_lines())
            else:
                entries, problems = self._from_legacy()
            self._cache = (stamp, entries, problems)
            return entries, problems

    def _parse(self, lines: list[str]) -> tuple[list[TermEntry], list[Issue]]:
        entries: list[TermEntry] = []
        problems: list[Issue] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(
                    Issue(
                        code="termbook_bad_json",
                        message=f"第 {index} 行不是合法 JSON：{exc.msg}",
                        ref=f"{self.path}:{index}",
                        detail={"line": line},
                    )
                )
                continue
            if not isinstance(payload, dict):
                problems.append(
                    Issue(
                        code="termbook_bad_json",
                        message=f"第 {index} 行不是 JSON 对象",
                        ref=f"{self.path}:{index}",
                        detail={"line": line},
                    )
                )
                continue
            entry = TermEntry.from_dict(payload)
            if not entry.writing:
                # 没有写法就没有身份：既寻址不到、也判不了"什么时候该说话"。原样留在
                # 文件里（不是我们替人删的），但不当一行读进来。
                problems.append(
                    Issue(
                        code="termbook_missing_field",
                        message=f"第 {index} 行缺少写法（key 为空的行不注入）",
                        ref=f"{self.path}:{index}",
                        detail={"line": line},
                    )
                )
                continue
            if not entry.is_injectable and not entry.writings:
                # 走到这里说明这一行连写法都没有（上面那条 already continue 了）——
                # 保留一个如实报出的出口，免得将来有人放宽上面那条时静默放行。
                problems.append(
                    Issue(
                        code="termbook_empty_entry",
                        message=(
                            f"「{entry.writing}」既没有写法，也没有译名与事实"
                        ),
                        ref=f"{self.path}#{entry.writing}",
                    )
                )
            entries.append(entry)
        return entries, problems

    # ---- 旧格式 → 合并后的行 ------------------------------------------------

    def _from_legacy(self) -> tuple[list[TermEntry], list[Issue]]:
        """把旧文件读成**合并后的行**（读路径用它，迁移也用它）。"""
        rows: dict[str, TermEntry] = {}
        order: list[str] = []
        problems: list[Issue] = []

        def slot(writing: str) -> TermEntry:
            if writing not in rows:
                rows[writing] = TermEntry(key=[{"writing": writing, "target": ""}])
                order.append(writing)
            return rows[writing]

        # ① 术语表：填 target
        glossary = self.path.parent / "glossary.jsonl"
        if glossary.exists():
            for payload in _read_jsonl(glossary, problems):
                writing = str(payload.get("source") or "").strip()
                if not writing:
                    continue
                row = slot(writing)
                target = str(payload.get("target") or "").strip()
                if target and not row.target_for(writing):
                    row.key[0]["target"] = target
        # ② 世界书 jsonl：填 profile
        worldbook = self.path.parent / "worldbook.jsonl"
        if worldbook.exists():
            for payload in _read_jsonl(worldbook, problems):
                writing = str(payload.get("source") or "").strip()
                if not writing:
                    continue
                row = slot(writing)
                fact = str(payload.get("target") or "").strip()
                if fact and fact not in row.profile:
                    row.profile.append(fact)
                row.constant = row.constant or bool(payload.get("constant", False))
        # ③ 更早的 Markdown：读成世界书那一侧
        markdown = self.legacy_markdown
        if markdown.exists() and not worldbook.exists():
            try:
                text = markdown.read_text(encoding="utf-8")
            except OSError:
                text = ""
            for writing, fact, constant in _parse_worldbook_markdown(text):
                row = slot(writing)
                if fact and fact not in row.profile:
                    row.profile.append(fact)
                row.constant = row.constant or constant
        return [rows[writing] for writing in order], problems

    # ---- 查询 ---------------------------------------------------------------

    def entries(self) -> list[TermEntry]:
        """全部行（一个实体一行）。"""
        return self._parsed()[0]

    def terms(self) -> list[TermEntry]:
        """有译名的行（``key`` 里有非空 ``target``）。"""
        return [entry for entry in self.entries() if entry.is_term]

    def profiles(self) -> list[TermEntry]:
        """有事实的行（``profile`` 里有非空事实）。"""
        return [entry for entry in self.entries() if entry.is_profile]

    def injectable(self) -> list[TermEntry]:
        """会注入的行（有译名或有事实）—— 注入判定只看内容。"""
        return [entry for entry in self.entries() if entry.is_injectable]

    def candidates(self) -> list[dict[str, Any]]:
        """**待审更正**（不是"待审的条目"）：更正住在 pending 那一份文件里。"""
        return self.pending.records()

    def validate(self) -> list[Issue]:
        return self._parsed()[1]

    def summary(self) -> dict[str, Any]:
        entries = self.entries()
        return {
            "path": str(self.path),
            "entries": len(entries),
            "terms": len([e for e in entries if e.is_term]),
            "profiles": len([e for e in entries if e.is_profile]),
            "constants": len([e for e in entries if e.constant]),
            "tail": len([e for e in entries if e.position == POSITION_TAIL]),
            "pending": len(self.pending.records()),
            "changes": len(self.changes.records()),
            "rejected": len(self.rejected()),
        }

    def find(self, writing: str) -> TermEntry | None:
        """按**任一写法**取这一行（`Eve Herschel` 已经是 `Eve` 那一行的写法时取回同一行）。

        **行身份优先**：同一个写法理论上只住一行，但一次整份写歪就可能让它同时挂在两行上
        （新行刚拿到它、旧行还没被删）。这时"按身份命中"永远是对的答案 —— 拿错行去改 /
        去删是静默错位。精确命中优先；都没有再按不区分大小写找一遍（大小写那一栏已经
        删了，两种写法本来就是同一个实体，这里不做区分是为了不把同一个实体登记两次）。
        """
        wanted = str(writing).strip()
        if not wanted:
            return None
        for entry in self.entries():
            if entry.writing == wanted:
                return entry
        for entry in self.entries():
            if wanted in entry.writings:
                return entry
        lowered = wanted.lower()
        for entry in self.entries():
            if entry.writing.lower() == lowered:
                return entry
        for entry in self.entries():
            if any(item["writing"].lower() == lowered for item in entry.key):
                return entry
        return None

    #: :meth:`find` 的旧名字（读的一方只想"按写法取一行"）。
    get = find

    def lookup(self, text: str, *, limit: int = 32) -> list[TermEntry]:
        """找出文本里实际出现的**译名行**，长词优先（避免短词吃掉长词）。"""
        hits = [e for e in self.terms() if e.matches(text)]
        hits.sort(key=lambda e: (-max(len(key) for key in e.trigger_keys), e.writing))
        return hits[:limit]

    def lookup_in_slots(
        self, slots: list[str], *, limit: int = 32
    ) -> list[tuple[TermEntry, list[str]]]:
        """逐句扫，返回 ``(条目, 证据)`` —— 证据是"命中了哪几句"。

        **任一写法命中就算这一行命中**：``Eve`` 那一行认 ``Eve Herschel`` 这场命中。
        """
        found: dict[str, tuple[TermEntry, list[str]]] = {}
        for entry in self.terms():
            if not entry.writing:
                continue
            hits = scan_slots(slots, entry.trigger_keys)
            if hits:
                found[entry.writing] = (
                    entry,
                    [str(slots[hit.slot])[:120] for hit in hits][:5],
                )
        ordered = sorted(
            found.values(), key=lambda pair: (-len(pair[0].writing), pair[0].writing)
        )
        return ordered[:limit]

    # ---- 变更 ---------------------------------------------------------------

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(lines)
        self.path.write_text(body + "\n" if body else "", encoding="utf-8")
        with self._lock:
            self._cache = None

    def _log_diff(
        self,
        old: TermEntry | None,
        new: TermEntry,
        *,
        writing: str,
        why: str,
        by: str = "",
    ) -> list[tuple[str, str]]:
        """把一次写入的差异记进变更日志，返回**被改到的位置**（``(what, writing)``）。

        返回它是因为写的人还要拿它去撤掉那一处已经失效的待审提案。
        """
        touched: list[tuple[str, str]] = []
        old_key = old or TermEntry()
        old_writings = old_key.writings
        new_writings = new.writings
        for name in [*old_writings, *(w for w in new_writings if w not in old_writings)]:
            before = old_key.target_for(name)
            after = new.target_for(name)
            if before == after:
                continue
            self.changes.append(
                what="key", writing=name, old=before, new=after, why=why, by=by
            )
            touched.append(("key", name))
        if old_key.facts != new.facts:
            # 事实是**列表**：能按位置对上的记成"改这一条"，多出来的记成"加一条"，
            # 少掉的记成"删一条"。这样统一替换与复核都能看到"原来是哪一句"。
            for index in range(max(len(old_key.facts), len(new.facts))):
                before = old_key.facts[index] if index < len(old_key.facts) else ""
                after = new.facts[index] if index < len(new.facts) else ""
                if before == after:
                    continue
                self.changes.append(
                    what="profile",
                    writing=writing or new.writing,
                    old=before,
                    new=after,
                    why=why,
                    by=by,
                )
            touched.append(("profile", writing or new.writing))
        for field_name, before, after in (
            ("constant", old_key.constant, new.constant),
            ("order", old_key.order, new.order),
            ("position", old_key.position, new.position),
        ):
            if before == after:
                continue
            self.changes.append(
                what=field_name,
                writing=writing or new.writing,
                old=before,
                new=after,
                why=why,
                by=by,
            )
            touched.append((field_name, writing or new.writing))
        return touched

    def _drop_superseded(self, touched: Iterable[tuple[str, str]]) -> int:
        """人拍了板之后，把这一处还没裁的提案撤掉。"""
        removed = 0
        for what, writing in touched:
            removed += self.pending.drop_for(what=what, writing=writing)
        return removed

    def add(
        self,
        entry: TermEntry,
        *,
        exact: bool = False,
        by: str = "human",
        why: str = "",
    ) -> TermEntry:
        """**人拍的板**：写进这一行，直接生效、不进待审队列。

        找不到同一行（任一写法都不在书里）就新开一行；同一行的判定是"给进来的
        **任一写法**已经在某一行里"。写之前把这一行上还没裁的待审提案撤掉 ——
        人已经拍了板，旧提案留着会在"采用"时把人的决定覆盖回去（防呆）。

        ``exact=False``（缺省）= **逐栏合并**：给进来的写法按并集并进那一行、非空译名
        覆盖空译名、新事实追加、``constant/order/position`` 只在非缺省时改。命令行
        `resource.term.add <写法> <译名>` 只带一栏，不该顺手把另一栏清空 —— 那是
        "一处一个决定"要防的事故。

        ``exact=True`` = **整行按给进来的那份写**（面板的编辑表单：它把五栏**全部**
        读出来又全部送回来，所以"清掉一条事实"这件事表达得出来）。调用方必须给出完整
        的一行，本方法不做任何字段兜底。

        空 ``key`` 的行写不进去（没有身份可以寻址），原样返回。
        """
        if not entry.writing:
            return entry
        self.ensure()
        if self.is_rejected(entry.writing):
            # 人（或 agent）显式又把这个词加回书里 —— 那条否决就该撤掉。
            self.clear_rejection(entry.writing)
        current = self.find(entry.writing)
        reason = why or f"{by} 直接改"
        if current is None:
            touched = self._log_diff(None, entry, writing=entry.writing, why=reason, by=by)
            self._replace_row(None, entry)
            self._drop_superseded(touched)
            return entry
        row = entry if exact else _merge_into(current, entry)
        touched = self._log_diff(current, row, writing=entry.writing, why=reason, by=by)
        self._replace_row(current, row)
        self._drop_superseded(touched)
        return self.find(row.writing) or row

    def apply(
        self,
        entry: TermEntry,
        *,
        by: str = "agent",
        why: str = "",
    ) -> TermEntry:
        """**只追加免审；改动进待审**（模型 / 摘要侧 / agent 的写入通道）。

        逐条规则（**条目被改了就要审**；免审的只剩"填上原来空着的译名"）：

        * 写法不在这一行里 → **进待审**（这也是"改了这一行"）；
        * 写法在、译名是空的而提案非空 → **直接写上**（记变更日志）；
        * 写法在、译名非空且与提案不同 → **不改**，写进待审队列（:meth:`PendingChanges.propose`）；
        * **新事实**（不在 ``profile`` 里）→ **进待审**，不再直接追加 —— 追加一条事实**就是**
          改这一行的 ``profile``，与"改已有的事实那一句"是同一件事（
          "就是条目被修改了=需要审核，和'改事实'不是一个意思吗"）。以前它是免审追加的，
          书因此在跑批中自己长大（真靶 148 条事实里 116 条从这条缝里进来）；
          **疑似换个说法说的同一件事**（:func:`near_fact`）照样排队，但那一条的 ``why``
          里会多一句"疑似与已有第 N 条重复"—— 只标注，不替复核的人做决定；
        * 已有的那几句事实一个字符都不动（要改就显式提更正，走 ``index``）。
        * ``constant`` / ``order`` / ``position`` 与提案不同 → 直接改并记日志（它们是开关与排序，
          不是知识，也不改注入的**条数**）。

        找不到同一行（任一写法都不在书里）时**新开一行**，整份写法与事实一次写进去 ——
        新实体照旧进书（"不是让新知识不进书"）。

        ⚠️ **这条通道的上游有一道闸**：模型的申报先落**候选**，跨批重复且一致到
        ``TranslateOptions.auto_approve_terms`` 次才自动走到这里；把它设成 0 就只落候选、
        等复核（不要书自己长）。所以这里仍然是"**已经批准之后**怎么落书"。

        **已否决的写法一律跳过**：否决清单是这条通道的护栏（"下一个人再申报一次就会
        重新登记，否决被静默抹掉"是 R59 记过的坑）。人显式加回来走 :meth:`add`，
        那是拍板动作，会主动撤掉那条否决。
        """
        if not entry.writing:
            return entry
        if self.is_rejected(entry.writing):
            return entry
        self.ensure()
        # **谁写的**决定审不审：人拍板与旧台账迁移是**直接入库**（它们本来就是显式动作、
        # 已经在别处被审过），其余（模型在翻译里捎带出来的）**改了这一行就排队等复核**。
        direct = by in DIRECT_WRITERS
        reason = why or f"{by} 追加"
        current = self.find(entry.writing)
        if current is None:
            touched = self._log_diff(None, entry, writing=entry.writing, why=reason, by=by)
            self._replace_row(None, entry)
            self._drop_superseded(touched)
            return entry
        merged = replace(
            current,
            key=[
                {"writing": item["writing"], "target": item["target"]}
                for item in current.key
            ],
            profile=list(current.profile),
        )
        proposals: list[dict[str, Any]] = []
        for item in entry.key:
            writing = item["writing"]
            wanted = item["target"]
            slot = next(
                (row for row in merged.key if row["writing"] == writing), None
            )
            if slot is None:
                # 这一行的**新写法**：也是"改了这一行" → 进待审。
                # `writing` 记**这一行的身份**（不然 `adopt` 找不到该挂到哪一行），
                # 新写法与它的译名放在 `new` 里。
                if wanted:
                    if direct:
                        merged.key.append({"writing": writing, "target": wanted})
                    else:
                        proposals.append(
                            {"what": "key", "writing": entry.writing, "index": None,
                             "old": "", "new": {"writing": writing, "target": wanted}}
                        )
                continue
            if not wanted:
                continue
            if not slot["target"]:
                slot["target"] = wanted
                continue
            if slot["target"] != wanted:
                proposals.append(
                    {
                        "what": "key",
                        "writing": writing,
                        "index": merged.key.index(slot),
                        "old": slot["target"],
                        "new": wanted,
                    }
                )
        # **新事实进待审**：追加一条事实就是改这一行的 profile，与"改已有那一句"同一件事。
        for fact in entry.profile:
            text = str(fact).strip()
            if not text:
                continue
            if any(same_fact(text, known) for known in merged.profile):
                continue
            if direct:
                merged.profile.append(text)
                continue
            note = ""
            for index, known in enumerate(merged.profile):
                if near_fact(text, known):
                    note = (
                        f"；疑似与已有第 {index + 1} 条重复"
                        f"（相似度 {fact_similarity(text, known):.2f}，换个说法说的同一件事）"
                    )
                    break
            proposals.append(
                {"what": "profile", "writing": entry.writing, "index": None,
                 "old": "", "new": text, "note": note}
            )
        # 待审只受理 key 与 profile：另外三栏是开关与排序，写了就生效。这一条通道只能
        # **往非缺省的方向**调（False / 100 / "terms" 一律当"没意见"）—— 要改回缺省，
        # 走 `add`（人拍的板，整行替换）。
        merged.constant = entry.constant or merged.constant
        if entry.order != DEFAULT_ORDER:
            merged.order = entry.order
        if entry.position != POSITION_TERMS:
            merged.position = entry.position
        touched = self._log_diff(current, merged, writing=entry.writing, why=reason, by=by)
        self._replace_row(current, merged)
        self._drop_superseded(touched)
        for proposal in proposals:
            note = str(proposal.pop("note", "") or "")
            self.pending.propose(why=f"{reason}{note}" if note else reason, **proposal)
        return self.find(merged.writing) or merged

    def absorb(
        self,
        writing: str,
        *,
        target: str = "",
        fact: str = "",
        by: str = "model",
        why: str = "",
    ) -> TermEntry:
        """一个写法的申报并进来（模型申报那条通道的入口）。

        只是把"一条申报"包成一行再交给 :meth:`apply` —— 那条通道的全部规则（免审追加 /
        改动进待审）都在 ``apply`` 上，这里不再重复一遍。
        """
        return self.apply(
            TermEntry(
                key=[{"writing": str(writing), "target": str(target or "")}],
                profile=[str(fact)] if str(fact or "").strip() else [],
            ),
            by=by,
            why=why,
        )

    def propose(
        self,
        *,
        what: str,
        writing: str,
        index: Any = None,
        new: Any = "",
        why: str = "",
    ) -> dict[str, Any]:
        """显式提一条**更正**（改已有事实 / 改已有译名）。

        ``apply`` 只能发现"译名被改了"这一类；"改哪一条事实"必须由提的人给出位置
        （``index``），所以另开这个入口 —— 待审队列是故意的：改已有的东西不许悄悄发生。
        """
        self.ensure()
        entry = self.find(writing)
        if entry is None:
            raise KnowledgeError(
                f"术语书里没有「{writing}」这一行，没有可以更正的东西",
                hint="新增走追加（`resource.term.add` / `apply`），不需要提更正。",
            )
        if what == "key":
            index = (
                index
                if index is not None
                else next(
                    (
                        position
                        for position, item in enumerate(entry.key)
                        if item["writing"] == writing
                    ),
                    None,
                )
            )
            old = entry.key[index]["target"] if index is not None and index < len(entry.key) else ""
        elif what == "profile":
            old = entry.profile[index] if index is not None and index < len(entry.profile) else ""
        else:
            raise KnowledgeError(
                f"待审只受理 {' / '.join(PENDING_KINDS)} 两栏（收到 {what!r}）"
            )
        return self.pending.propose(
            what=what, writing=writing, index=index, old=old, new=new, why=why
        )

    def adopt(self, pid: str, *, by: str, why: str = "") -> dict[str, Any]:
        """**采用**一条待审更正：把 ``old`` 换成 ``new``，写变更日志，从队列里删掉。"""
        identity = _require_human(by, "采用一条待审更正")
        record = self.pending.get(pid)
        if record is None:
            raise KnowledgeError(f"待审队列里没有这条提案：{pid!r}")
        entry = self.find(str(record.get("writing") or ""))
        if entry is None:
            raise KnowledgeError(
                f"术语书里没有「{record.get('writing')}」这一行，采用不了这条提案"
            )
        what = str(record.get("what") or "")
        index = record.get("index")
        new = record.get("new") or ""
        if what == "key":
            if isinstance(new, dict):
                # 这一行的**新写法**（`new` 里带着写法与它的译名）
                entry.key = list(entry.key) + [
                    {"writing": str(new.get("writing") or ""),
                     "target": str(new.get("target") or "")}
                ]
                old = ""
            else:
                slot = next(
                    (item for item in entry.key if item["writing"] == record.get("writing")),
                    None,
                )
                if slot is None:
                    raise KnowledgeError(
                        f"「{record.get('writing')}」已经不在这一行的写法列表里，采用不了"
                    )
                old = slot["target"]
                slot["target"] = str(new)
        elif what == "profile":
            if index is None:
                entry.profile = list(entry.profile) + [str(new)]
                old = ""
            elif not (0 <= int(index) < len(entry.profile)):
                raise KnowledgeError(
                    f"这条提案指着第 {index} 条事实，而这一行现在有 {len(entry.profile)} 条"
                )
            else:
                old = entry.profile[int(index)]
                entry.profile[int(index)] = str(new)
        else:
            raise KnowledgeError(f"不认识的待审栏位：{what!r}")
        reason = str(record.get("why") or why or f"{identity} 采用待审更正")
        self.changes.append(
            what=what,
            writing=str(record.get("writing") or ""),
            old=old,
            new=new,
            why=reason,
            by=identity,
        )
        self._replace_row(self.find(str(record.get("writing") or "")), entry)
        self.pending.drop(pid)
        return {"adopted": record, "entry": (self.find(entry.writing) or entry).to_dict()}

    def discard(self, pid: str) -> dict[str, Any]:
        """**丢弃**一条待审更正（不改书，只从队列里删掉）。"""
        record = self.pending.get(pid)
        if record is None:
            raise KnowledgeError(f"待审队列里没有这条提案：{pid!r}")
        self.pending.drop(pid)
        return {"discarded": record}

    def _replace_row(self, current: TermEntry | None, new: TermEntry) -> None:
        """按身份替换一行（同一行 = 写法有交集），其余行原样保留。"""
        writings = set(current.writings) if current is not None else set()
        lines = self._raw_lines()
        out: list[str] = []
        done = False
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            if not done and isinstance(payload, dict):
                found = TermEntry.from_dict(payload)
                if found.writing and set(found.writings) & writings:
                    out.append(json.dumps(new.to_dict(), ensure_ascii=False))
                    done = True
                    continue
            out.append(line)
        if not done:
            out.append(json.dumps(new.to_dict(), ensure_ascii=False))
        self._write(out)

    def import_rows(
        self,
        records: Iterable[dict[str, Any]],
        *,
        by: str = "agent",
        why: str = "",
    ) -> dict[str, Any]:
        """**整份换上**：让书最终恰好是给进来的这几行（拆行 / 合行 / 批量改名一次做完）。

        为什么要有这条通道：`add --exact` 一次只能写一行，而"把一个写法从这一行搬到那一行"
        天然是**两行的事** —— 缺了它，agent 只能直接改 `termbook.jsonl`，那样**一点变更
        日志都不留**（谁在什么时候拆了哪一行，事后查不出来）。这条通道把同样的动作走一遍
        `add` / `remove`，于是每一步都进 `termbook.changes.jsonl`。

        顺序是有讲究的（拆行时按错的顺序会写歪）：

        1. **先删**不在新文件里的行 —— 合并（`SIT` + `Senland Institute of Technology`
           并成一行）时，旧的那一行得先腾开；
        2. **再写**新文件里的行，**身份已经存在的先写** —— 拆行（`BSU` 拆出
           `Wild Cats`）时，先被改的那一行把写法吐出来，新行才拿得到它；反过来
           `Wild Cats` 会撞回 `BSU` 那一行。

        ``records`` 是同 ``termbook.jsonl`` 一样形状的行（``key`` / ``profile`` /
        ``constant`` / ``order`` / ``position``）。空 ``key`` 的行会被跳过并报出来，
        不静默丢。纯写口，不做网络、不跑模型。
        """
        wanted: list[TermEntry] = []
        skipped: list[int] = []
        for index, record in enumerate(records or ()):
            entry = TermEntry.from_dict(record) if isinstance(record, dict) else TermEntry()
            if entry.writing:
                wanted.append(entry)
            else:
                skipped.append(index + 1)

        self.ensure()
        before = [entry.writing for entry in self.entries()]
        target_ids = {entry.writing for entry in wanted}
        removed: list[str] = []
        for identity in before:
            if identity in target_ids:
                continue
            if self.remove(identity, why=why or "整份换上：这一行不在新文件里", by=by):
                removed.append(identity)

        existing = set(before)
        # 身份已在书里的先写：把"写法从旧行搬到新行"按能成的顺序做出来
        ordered = sorted(wanted, key=lambda entry: 0 if entry.writing in existing else 1)
        written: list[str] = []
        for entry in ordered:
            self.add(entry, exact=True, by=by, why=why or "整份换上：整行按给进来的那份写")
            written.append(entry.writing)

        after = [entry.writing for entry in self.entries()]
        return {
            "rows": len(after),
            "written": written,
            "removed": removed,
            "skipped_rows": skipped,
            "order": after,
        }

    def remove(self, writing: str, *, why: str = "", by: str = "human") -> bool:
        """按**任一写法**删掉整行（译名与事实一起删）。行身份优先，见 :meth:`find`。"""
        self.ensure()
        current = self.find(writing)
        if current is None:
            return False
        self.changes.append(
            what="key",
            writing=current.writing,
            old=current.to_dict(),
            new=None,
            why=why or f"删除「{current.writing}」",
            by=by,
        )
        removed = False
        kept: list[str] = []
        for line in self._raw_lines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if isinstance(payload, dict):
                found = TermEntry.from_dict(payload)
                if found.writing and set(found.writings) & set(current.writings):
                    removed = True
                    continue
            kept.append(line)
        if removed:
            self._write(kept)
            self.pending.drop_for(writing=current.writing)
        return removed

    # ---- 设定那一侧的插入 ---------------------------------------------------

    def trigger(
        self,
        slots: list[str],
        *,
        limit: int = 8,
        budget_chars: int = 1200,
        max_scan_slots: int = 0,
        policy: TriggerPolicy | None = None,
    ) -> tuple[list[tuple[TermEntry, list[str]]], list[str]]:
        """**按写法插入设定行**：返回 ``(命中条目+证据, 被预算挤掉的写法)``。

        排序照 :func:`injection_sort_key`：``tail`` 在最后，段内**蓝灯先插**、其余按
        ``order`` 升序。字符预算（``budget_chars``）用尽就停 —— 剩下的如实列进"被挤掉"，
        不静默吞掉。``max_scan_slots`` 是扫描深度（0 = 全扫）。
        """
        base = policy or TriggerPolicy()
        scan_policy = TriggerPolicy(
            whole_word=base.whole_word,
            case_sensitive=base.case_sensitive,
            max_slots=max_scan_slots or base.max_slots,
        )
        usable = sorted(
            (entry for entry in self.profiles() if entry.is_injectable),
            key=injection_sort_key,
        )
        triggered: list[tuple[TermEntry, list[str]]] = []
        for entry in usable:
            if entry.constant:
                triggered.append((entry, []))
                continue
            hits = scan_slots(slots, entry.trigger_keys, policy=scan_policy)
            if hits:
                triggered.append(
                    (entry, [str(slots[hit.slot])[:120] for hit in hits][:5])
                )
        kept: list[tuple[TermEntry, list[str]]] = []
        dropped: list[str] = []
        used = 0
        for entry, evidence in triggered:
            cost = len(entry.profile_line()) + len(entry.writing)
            if kept and used + cost > budget_chars:
                dropped.append(entry.writing)
                continue
            kept.append((entry, evidence))
            used += cost
        return kept[:limit], dropped


# --------------------------------------------------------------------------- #
# 旧文件的解析（只在折叠与只读兼容时用）
# --------------------------------------------------------------------------- #


def _read_jsonl(path: Path, problems: list[Issue]) -> list[dict[str, Any]]:
    """旧 jsonl：一行一个 JSON 对象。解析不了的行报一条问题，不中断。"""
    found: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(
                Issue(
                    code="termbook_legacy_bad_json",
                    message=f"{path.name} 第 {index} 行不是合法 JSON：{exc.msg}",
                    ref=f"{path}:{index}",
                )
            )
            continue
        if isinstance(payload, dict):
            found.append(payload)
    return found


def _parse_worldbook_markdown(text: str) -> list[tuple[str, str, bool]]:
    """更早的 ``worldbook.md``：``## 标题`` 一节一条，键取 ``- keys:`` 的第一条。

    返回 ``(触发词, 设定, 蓝灯)``。这条通道只为了**老工程不丢数据**而存在。``priority``
    不再折算（新设计里 ``order`` 说了算）；标题与触发词不同时，标题拼在设定前面留着。
    """
    found: list[tuple[str, str, bool]] = []
    sections: list[tuple[str, list[str]]] = []
    title: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                sections.append((title, buffer))
            title = line[3:].strip()
            buffer = []
        elif title is not None:
            buffer.append(line)
    if title is not None:
        sections.append((title, buffer))

    for title, lines in sections:
        keys: list[str] = []
        constant = False
        body_lines: list[str] = []
        in_meta = True
        for line in lines:
            stripped = line.strip()
            if in_meta and stripped.startswith("- "):
                name, _, value = stripped[2:].partition(":")
                if name.strip().lower() == "keys":
                    keys = [v.strip() for v in value.split(",") if v.strip()]
                elif name.strip().lower() == "constant":
                    constant = value.strip().lower() in ("true", "yes", "1", "on")
                continue
            if stripped:
                in_meta = False
            body_lines.append(line)
        writing = keys[0] if keys else title
        body = "\n".join(body_lines).strip()
        if title and title != writing:
            body = f"原标题：{title}\n{body}".strip()
        found.append((writing, body, constant))
    return found


# --------------------------------------------------------------------------- #
# 上下文装配
# --------------------------------------------------------------------------- #


def drift_report(records: Any, book: Any) -> dict[str, list[str]]:
    """**盘上译文没按术语书写**的那些记录 —— 报出来，不顺过去。

    判据（按**行**，不是按写法）：这一行的任一写法在这条**原文**里出现，而它的
    **全部译名**在**译文**里一个都没出现。按行判是为了避开"全名被简写成短名"的假警报
    （原文里 `Lexi Anne Miller`、译文里"莱克西"，短名的译名在，判为已遵守）。

    ⚠️ 它**判不了**"模型用了另一个中文名"：机器不知道「沙希」和「咲」是同一个实体 ——
    所以这是**提醒**，不是断言。它的用途是让"这一条没按定译写"变成看得见的一件事，
    而不是让流程顺过去（真靶那次就是两种写法并存、报告里一切正常）。

    返回 ``{行的写法: [unit_id, …]}``；没有不符合的就是空字典。
    """
    from gametrans.layers.trigger import TriggerPolicy, key_in_text

    policy = TriggerPolicy()
    found: dict[str, list[str]] = {}
    for record in records or []:
        if not getattr(record, "is_usable", False):
            continue
        source = str(getattr(record, "source", "") or "")
        if not source:
            continue
        target = "".join(
            str(getattr(segment, "value", "") or "")
            for segment in (getattr(record, "translated_segments", None) or [])
        )
        if not target:
            continue
        for entry in book.entries():
            writings = list(entry.writings)
            targets = [target_text for _, target_text in entry.targets if target_text]
            if not targets:
                continue  # 还没定译：盘上留的是标签，不归这条判
            if not any(key_in_text(source, writing, policy=policy) for writing in writings):
                continue
            if any(target_text in target for target_text in targets):
                continue
            found.setdefault(writings[0] if writings else entry.writing, []).append(
                str(getattr(record, "unit_id", ""))
            )
    return found


@dataclass
class ResourceContext:
    """准备注入翻译提示词的背景知识。

    只有**给模型看的**东西在这里：**术语书里跟这段文本有关的那几行**、风格（含自定义
    要求）、相近译法。一行一个实体，注入时合成**一段**（【术语书】）—— 有译名的写法
    逐条出行、有事实的出行设定，同一个实体的几栏连着放。
    """

    #: 相关性筛过的那几行（一行一个实体），**已经按注入顺序排好**
    entries: list[TermEntry] = field(default_factory=list)
    #: 这段原文里**还没有译名**的写法（已经在原文里包成 ``⟦写法⟧``，见 layers/tags.py）。
    #: 它是"这次注入了哪几个待定名字"的留档：渲染出每个标签的说明行，对账（
    #: `core/constraints.py`）拿同一份判译文有没有把它们弄丢。
    tags: list[str] = field(default_factory=list)
    #: 翻译记忆里相近句子的**参考译法**（只作参考，不自动采用）
    suggestions: list[str] = field(default_factory=list)
    #: 风格要求 + 自定义要求（一等资源，见 layers/style.py）
    style: list[StyleEntry] = field(default_factory=list)
    #: 因为**配额/预算**没进去的条数（按类记：worldbook…）。看得见才叫记账。
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def terms(self) -> list[TermEntry]:
        """有译名的那几行。"""
        return [entry for entry in self.entries if entry.is_term]

    @property
    def profiles(self) -> list[TermEntry]:
        """有事实的那几行。"""
        return [entry for entry in self.entries if entry.is_profile]

    @property
    def has_content(self) -> bool:
        return bool(self.entries or self.suggestions or self.style)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "tags": list(self.tags),
            "suggestions": list(self.suggestions),
            "style": [s.to_dict() for s in self.style],
            "dropped": dict(self.dropped),
        }

    def render(self) -> str:
        """一段【术语书】：**每个写法的译名**逐条出行、设定行挂在整行身份上。

        只送 `- 写法 → 译名` 与 `- 设定｜写法 / 写法：事实；事实`：变更日志、待审提案
        都是**给人看 / 给机器看**的，模型不需要看。**任一写法**的译名都单独成行 ——
        `Eve Herschel` 这个写法在原文里出现时，也要拿到它自己的译名。设定那一行左边
        列出**整行的写法**：设定说的是这个实体（形状见 :data:`SETTING_LABEL`）。

        还没有译名的写法**不在这里说**（它没有内容可说）：它在请求里是另一段
        （见 :func:`gametrans.layers.tags.announce`），说一次就够。
        """
        lines: list[str] = []
        for entry in self.entries:
            for writing, target in entry.targets:
                lines.append(f"- {term_content(writing, target)}")
            if entry.is_profile:
                lines.append(f"- {setting_content(entry)}")
        blocks: list[str] = []
        if lines:
            blocks.append("【术语书】\n" + TERMBOOK_LEGEND + "\n" + "\n".join(lines))
        if self.suggestions:
            # 明确写"仅供参考"：相近的句子未必能照抄，采用与否是翻译层的事
            blocks.append("【相近译法（仅供参考，不要照抄）】\n" + "\n".join(self.suggestions))
        rendered_style = StyleGuide.render(self.style)
        if rendered_style:
            blocks.append(rendered_style)
        return "\n\n".join(blocks)


def knowledge_fingerprint(context: ResourceContext) -> str:
    """一段背景知识的指纹，用来判断"这条译文的知识状态变了没有"。

    归一化到与条目顺序无关，所以只关心**内容**变没变，不关心它碰巧排在第几。
    自定义要求也在这里面 —— 它就是 ``aspect="custom"`` 的风格条目（见
    :meth:`ResourceLayer.set_custom_instructions`），不再另开一条特判。
    """
    canonical = {
        "entries": sorted(
            (entry.to_dict() for entry in context.entries),
            key=lambda item: str((item.get("key") or [{}])[0].get("writing") or ""),
        ),
        # 待定译的写法也要进指纹：书里给它定上译名，这一段的知识状态就变了
        # （提示词里那几行从 `⟦Eve⟧` 变成 `Eve → 伊芙`），该点名过期。
        "tags": sorted(context.tags),
        "style": sorted(
            (s.to_dict() for s in context.style),
            key=lambda item: (item["aspect"], item["scope"]),
        ),
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 资源层门面
# --------------------------------------------------------------------------- #


class ResourceLayer:
    """资源层的统一入口：翻译资源 + 引擎资源。"""

    def __init__(self, root: Path, registry: EngineRegistry | None = None) -> None:
        self.root = Path(root)
        self.registry = registry or EngineRegistry()
        #: 只有**给模型看的材料**住在这里：术语书（一行一个实体）
        self.termbook = TermBook(self.root / TERMBOOK_FILE)
        # 风格指南：一等资源，和术语书一个待遇（见 layers/style.py）
        # 自定义要求（配置里那段自由文本）也走这条通道，见 set_custom_instructions
        self.style = StyleGuide(self.root / STYLE_FILE)
        # 翻译记忆：同句复用的落盘处（见 gametrans/layers/memory.py）
        self.memory = TranslationMemory(self.root / MEMORY_FILE)
        # 下面两样是**给写回与校验看的清单**，不是给模型看的材料 —— 所以不住在
        # `resources/` 里：目录本身就说明"谁读它"（见 layers/supplements.py、
        # layers/deviations.py）。旧位置那份由各自的 `ensure()` 搬一次（写路径才搬）。
        self.supplements = SupplementSet(
            self.writeback_root / SUPPLEMENTS_FILE, legacy_path=self.root / SUPPLEMENTS_FILE
        )
        self.deviations = DeviationStore(
            self.writeback_root / DEVIATIONS_FILE, legacy_path=self.root / DEVIATIONS_FILE
        )
        #: 自定义要求：渲染成一条 `aspect="custom"` 的风格条目（见 context_for）
        self.custom_instructions: str = ""
        #: 设定行的插入预算（字符）与扫描深度（句数，0 = 全扫）—— 照 SillyTavern 的
        #: Context%/Budget 与 Scan Depth 两个旋钮（见 TermBook.trigger）。
        self.worldbook_budget_chars: int = 1200
        self.worldbook_scan_slots: int = 0
        #: 写法命中的规则（拉丁写法整词、区分大小写与否）
        self.trigger_policy: TriggerPolicy = TriggerPolicy()
        #: 资源文件摘要的缓存（"文件没变就不重算"）
        self._versions_cache: (
            tuple[tuple[Any, Any], dict[str, str]] | None
        ) = None

    @property
    def writeback_root(self) -> Path:
        """写回/校验清单住哪：工作区里和 ``resources/`` 平级的 ``writeback/``。"""
        return self.root.parent / "writeback"

    def set_custom_instructions(self, text: str) -> None:
        """把"自定义要求"接到风格通道上。

        它本来就是一条口径要求，和 `style.jsonl` 里的条目是同一类东西，只是没结构
        （没有 aspect / scope）。所以**一个通道、一处指纹**：进了风格，就自动进
        提示词的【风格要求】段，也自动进知识指纹，不再另开特判。
        """
        self.custom_instructions = str(text or "").strip()

    def ensure(self) -> "ResourceLayer":
        self.root.mkdir(parents=True, exist_ok=True)
        self.writeback_root.mkdir(parents=True, exist_ok=True)
        self.termbook.ensure()
        self.style.ensure()
        self.memory.ensure()
        self.supplements.ensure()
        self.deviations.ensure()
        # 旧台账：读数已经不看它了（R59），所以老工程里那份要**折进**术语书，
        # 不能静默忽略 —— 静默忽略等于丢数据。只在写路径上做（读路径不写任何东西）。
        self.fold_legacy_ledger()
        return self

    #: 旧台账文件名。它已经不是存储了 —— 只作为**一次性折叠**的输入存在（R59）。
    LEGACY_LEDGER_FILE = "knowledge.jsonl"

    def fold_legacy_ledger(self) -> int:
        """把旧台账里的条目并进术语书，然后删掉那个文件。返回折叠条数。

        每一条按它**自己的内容**写进术语书那一行；有一条写不进去就**不动那个文件**，
        宁可留着让人看见，也不半途丢一半。
        """
        legacy = self.root / self.LEGACY_LEDGER_FILE
        if not legacy.exists():
            return 0
        try:
            payloads = [
                json.loads(line)
                for line in legacy.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return 0
        if not payloads:
            legacy.unlink()
            return 0
        folded = 0
        try:
            for data in payloads:
                self._absorb(data)
                folded += 1
        except (OSError, KeyError):
            return 0
        legacy.unlink()
        return folded

    def _absorb(self, data: dict[str, Any]) -> None:
        """把一条旧台账条目落到术语书里。

        设定条目的**触发词**从旧的 ``keys[0]`` 还原（那时候键与标题是两个字段）；
        没有键才退回 ``content``。旧的 ``status`` / ``tags`` 都不是新形状的栏位了：
        折进来就是**已经生效的一行**（旧台账里 pending 的行同样落进来，因为新形状
        没有"待审的条目"这个状态 —— 要审的是**更正**，见 pending 那一份文件）。
        """
        content = str(data.get("content") or "").strip()
        if not content:
            return
        kind = str(data.get("type") or "")
        scope = str(data.get("scope") or "")
        target = str(data.get("target") or "").strip()
        profile = str(data.get("profile") or "").strip()
        if scope.startswith("worldbook:") or kind in ("entity_profile", "worldbook_candidate"):
            # 旧世界书条目的**触发词**是行上的 ``keys[0]``（那时候键与标题是两个字段）；
            # 没有键才退回标题（``scope`` 是 ``worldbook:<标题>``），再退回内容。
            title = scope.split(":", 1)[1].strip() if scope.startswith("worldbook:") else ""
            title = title or content
            keys = [
                str(key).strip() for key in (data.get("keys") or ()) if str(key).strip()
            ]
            writing = keys[0] if keys else title
            fact = target or profile or content
            self.termbook.apply(
                TermEntry(
                    key=[{"writing": writing, "target": ""}],
                    profile=[fact] if fact else [],
                ),
                by="import",
                why="从旧的知识台账（knowledge.jsonl）折叠进来",
            )
            return
        self.termbook.apply(
            TermEntry(
                key=[{"writing": content, "target": target}],
                profile=[profile] if profile else [],
            ),
            by="import",
            why="从旧的知识台账（knowledge.jsonl）折叠进来",
        )

    # ---- 引擎资源 -----------------------------------------------------------

    def engine_packs(self) -> list[dict[str, Any]]:
        """引擎资源视图：当前软件认识哪些引擎、各自能干什么。"""
        return [pack.describe() for pack in self.registry.all()]

    # ---- 查询与校验 ---------------------------------------------------------

    def profile_hit_counts(
        self, slots: list[str], *, limit: int = 64
    ) -> dict[str, int]:
        """每一条设定的**写法会在多少句上命中**（按行身份回报条数）。

        "这条设定会在多大范围里说话"是复核时最要紧的一个数：命中 2 句和命中 900 句
        是两种完全不同的东西。**用真靶的原文槽位量出来**，不拍阈值。
        """
        entries = self.termbook.profiles()[:limit]
        return hit_counts(slots, {entry.writing: entry.trigger_keys for entry in entries})

    def context_for(
        self,
        text: str,
        *,
        slots: list[str] | None = None,
        max_glossary: int = 32,
        max_worldbook: int = 8,
        worldbook_budget_chars: int | None = None,
        use_glossary: bool = True,
        use_worldbook: bool = True,
        use_style: bool = True,
        use_knowledge: bool = True,
        unit_id: str | None = None,
        speaker: str | None = None,
        scene: str | None = None,
        region_id: str | None = None,
        custom_instructions: str | None = None,
    ) -> ResourceContext:
        """挑出与这段文本相关的术语书行与风格要求，供组装提示词。

        **译名行与设定行共用同一条命中规则**（:mod:`gametrans.layers.trigger`）：
        拉丁写法整词匹配、CJK 写法子串匹配；``slots`` 给定时**扫到句一级**并带回证据。
        一行的**任一写法**命中都算命中。

        ``use_*`` 决定**实际会注入**哪些东西 —— 指纹要跟实际注入的内容走，否则
        "关掉译名那一栏"这件事在指纹里看不出来。

        ``use_knowledge`` 管的是"**整份知识参不参与**"（关掉＝这一层的对照臂）。

        ``custom_instructions`` 不给就用手上这一份（:meth:`set_custom_instructions`）；
        它渲染成一条 ``aspect="custom"`` 的风格条目，走风格的通道与配额。
        """
        scan = list(slots) if slots else [text]
        # 一行一个实体，两栏各按各的通道找：译名靠字面命中，设定靠写法命中。
        found: dict[str, TermEntry] = {}
        if use_glossary:
            hits = (
                self.termbook.lookup_in_slots(scan, limit=max_glossary)
                if slots
                else [(entry, []) for entry in self.termbook.lookup(text, limit=max_glossary)]
            )
            for entry, _evidence in hits:
                found[entry.writing] = entry
        dropped: dict[str, int] = {}
        if use_worldbook:
            budget = (
                self.worldbook_budget_chars
                if worldbook_budget_chars is None
                else worldbook_budget_chars
            )
            kept, trimmed = self.termbook.trigger(
                scan,
                limit=max_worldbook,
                budget_chars=budget,
                max_scan_slots=self.worldbook_scan_slots,
                policy=self.trigger_policy,
            )
            for entry, _evidence in kept:
                # 同一个词两边都命中时并成一行：译名与设定本来就是同一个实体的两个面。
                current = found.get(entry.writing)
                if current is None:
                    found[entry.writing] = entry
                elif not current.is_profile:
                    found[entry.writing] = replace(current, profile=list(entry.profile))
            if trimmed:
                dropped["worldbook"] = len(trimmed)
        requirement = (
            self.custom_instructions
            if custom_instructions is None
            else str(custom_instructions or "").strip()
        )
        style = (
            self._with_custom_requirement(
                self.style.for_task(
                    unit_id=unit_id, speaker=speaker, scene=scene, region_id=region_id
                ),
                requirement,
            )
            if use_style
            else []
        )
        if not use_knowledge:
            # 对照臂：整份知识都不看
            found = {}
        entries = self._materialize(found.values(), use_glossary, use_worldbook)
        # 这次要在原文里包成标签的写法：**只有关掉术语那一栏时才算没有**
        # （对照臂"不看术语书"必须连标签一起关掉，否则这一层从标签漏进去）。
        # 从书本身扫，而不是从 `found` 里挑：只有写法、没有译名也没有设定的行
        # 不进任何一条注入通道，却照样该被包标签。
        tagged = (
            writings_to_tag(self.termbook, text if not slots else "\n".join(scan),
                            policy=self.trigger_policy)
            if use_glossary
            else []
        )
        return ResourceContext(
            entries=entries,
            tags=tagged,
            style=style,
            dropped=dropped,
        )

    @staticmethod
    def _materialize(
        found: Iterable[TermEntry], use_glossary: bool, use_worldbook: bool
    ) -> list[TermEntry]:
        """按开关把用不上的那一栏清掉 —— 一行一个实体，但**注入什么要跟开关一致**。

        关掉译名那一栏时把每个写法的 ``target`` 清空、关掉设定那一栏时把事实清空：
        否则一行里另一栏会搭便车进请求，对照臂（关某一层）就白做了，指纹也跟着失真。
        最后按 :func:`injection_sort_key` 排好注入顺序。
        """
        rows: list[TermEntry] = []
        for entry in found:
            row = entry
            if not use_glossary and row.is_term:
                row = replace(
                    row,
                    key=[
                        {"writing": item["writing"], "target": ""} for item in row.key
                    ],
                )
            if not use_worldbook and row.is_profile:
                row = replace(row, profile=[])
            if row.is_injectable:
                rows.append(row)
        return sorted(rows, key=injection_sort_key)

    @staticmethod
    def _with_custom_requirement(
        style: list[StyleEntry], requirement: str
    ) -> list[StyleEntry]:
        """把自定义要求作为 ``aspect="custom"`` 的一条并进风格里。

        文件里已经有一条同 aspect 的就不重复加 —— 同一件事两处记，迟早对不上。
        """
        if not requirement:
            return style
        if any(entry.aspect == "custom" for entry in style):
            return style
        return [
            *style,
            StyleEntry(
                aspect="custom",
                value=requirement,
                scope="global",
                note="来自自定义要求",
                source="user",
            ),
        ]

    def fingerprint_for(
        self,
        text: str,
        *,
        use_glossary: bool = True,
        use_worldbook: bool = True,
        use_style: bool = True,
        use_knowledge: bool = True,
        unit_id: str | None = None,
        speaker: str | None = None,
        scene: str | None = None,
        region_id: str | None = None,
        custom_instructions: str | None = None,
    ) -> str:
        """这条文本**当前**对应的知识指纹。

        关键在于它只看**与这条文本相关**的知识：不相关的条目增长了，指纹不变，
        于是那条译文不会被误判成过期 —— 这是"事后补译"不会退化成全量重翻的原因。
        """
        merged = self.context_for(
            text,
            use_glossary=use_glossary,
            use_worldbook=use_worldbook,
            use_style=use_style,
            use_knowledge=use_knowledge,
            unit_id=unit_id,
            speaker=speaker,
            scene=scene,
            region_id=region_id,
            custom_instructions=custom_instructions,
        )
        return knowledge_fingerprint(merged)

    def validate(self) -> list[Issue]:
        return [
            *self.termbook.validate(),
            *self.style.validate(),
            *self.memory.validate(),
            *self.supplements.validate(),
            *self.deviations.validate(),
        ]

    # ---- 待审更正 -----------------------------------------------------------

    def pending_corrections(self) -> list[dict[str, Any]]:
        """列待审更正（面板 / 命令行 / MCP 都走这里）。"""
        return self.termbook.pending.records()

    def adopt_correction(self, pid: str, *, by: str, why: str = "") -> dict[str, Any]:
        """采用一条待审更正（人拍的板）。"""
        return self.termbook.adopt(pid, by=by, why=why)

    def discard_correction(self, pid: str) -> dict[str, Any]:
        """丢弃一条待审更正。"""
        return self.termbook.discard(pid)

    def term_revisions(self) -> dict[str, dict[str, Any]]:
        """变更日志按写法汇总 —— 统一替换的替换源从这里取（不再存在条目里的 alternatives）。"""
        return self.termbook.changes.revisions()

    #: **机器写进去的**通道（模型申报 / 摘要侧抽取）。预检拿它认"哪些行是机器写的"：
    #: 机器写的资产一条都进不了请求，是"写进去就没进约束"的事故形态；人自己写的不算。
    MACHINE_WRITERS: frozenset[str] = frozenset({"model", "summary"})

    def machine_written(self) -> set[str]:
        """哪几行是**机器写进去的**（按变更日志最早的通道判断）。"""
        writers = self.termbook.changes.writers()
        return {
            writing
            for writing, channel in writers.items()
            if channel in self.MACHINE_WRITERS
        }

    def pending_correction_counts(self) -> dict[str, int]:
        """待审更正按栏分：``{"术语": 改译名的条数, "世界书": 改事实的条数}``。"""
        counts = {"术语": 0, "世界书": 0}
        for record in self.termbook.pending.records():
            key = "术语" if str(record.get("what")) == "key" else "世界书"
            counts[key] += 1
        return counts

    def versions(self) -> dict[str, str]:
        """资源文件的版本摘要，写进 ``TranslationArtifact.resource_versions``。

        记的是**整个文件**的摘要（而不是这条文本命中的行）——它回答"这条译文是在
        哪一版术语书下产出的"，与 :meth:`fingerprint_for` 回答的"它命中了什么知识"
        是两件事，两个都要留。
        """
        stamps = (
            self._file_stamp(self.termbook.path),
            self._file_stamp(self.style.path),
        )
        if self._versions_cache is not None and self._versions_cache[0] == stamps:
            return dict(self._versions_cache[1])
        versions: dict[str, str] = {}
        for name, path in (
            ("termbook", self.termbook.path),
            ("style", self.style.path),
        ):
            try:
                payload = Path(path).read_bytes()
            except OSError:
                continue
            versions[name] = hashlib.sha256(payload).hexdigest()[:16]
        self._versions_cache = (stamps, versions)
        return dict(versions)

    @staticmethod
    def _file_stamp(path: Path) -> tuple[int, int] | None:
        try:
            stat = Path(path).stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def summary(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "termbook": self.termbook.summary(),
            "style": self.style.summary(),
            "engine_packs": self.engine_packs(),
            "memory": self.memory.summary(),
            "supplements": self.supplements.summary(),
            "deviations": self.deviations.summary(),
        }
