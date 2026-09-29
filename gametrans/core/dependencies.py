"""候选依赖边 —— 软件出草案，agent 做判断。

提取层需要"足够智能的 agent 来代替传统翻译里人类做的整理工作"。但让 agent 从零
构建整张依赖图并不现实：真实工程有数百个区域，每次判断都要走一轮模型调用。

所以分工是：**软件把机械信号算出来**（几乎免费），**agent 负责确认、否决、补全**。

这里只用引擎无关的信号：

* **共享说话人** —— 同一个角色出现在两个区域，后一个需要前一个交代的角色信息
* **共享专名** —— 带大写的词，且不是"总出现在句首的大写"（那就是普通词首字母）
* **整句复用** —— 同一句话在两处出现，这是最强的关系信号

依赖方向一律**沿阅读顺序**：先出现的区域交代，后出现的区域消费。区域顺序来自
:meth:`~gametrans.core.graph.PathGraph.regions`，即首次出现的先后。

每条候选都带 ``provenance="heuristic"``，与 agent 确认过的边区分开 —— 调度决策
必须能追溯到"这是软件猜的"还是"这是 agent 定的"。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from gametrans.core.graph import PathGraph
from gametrans.core.models import GraphEdge

#: 低于这个强度就不提了，免得给 agent 制造噪音
DEFAULT_MIN_STRENGTH = 0.35

#: 各类信号对依赖强度的贡献
SIGNAL_WEIGHTS: dict[str, float] = {
    "character": 0.6,
    "term": 0.4,
    "text": 1.0,
}

#: 短于这个长度的"复用"多半是巧合，不算信号
MIN_REUSED_LENGTH = 8

#: 一条边上最多挂多少知识点。边是给人和 agent 读的：21 条话题里 19 条噪音，
#: 等于没说；而且 topics 会进知识指纹，噪音多了补译判定会跟着抖。
MAX_TOPICS_PER_EDGE = 12

#: 出现在超过这个比例的区域里的"专名"，其实是常用词（Ahem / Alright / Yeah），
#: 不是可查的知识点 —— 按 TF-IDF 的思路把"太普遍"的候选丢掉。
MAX_REGION_RATIO = 0.2

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
PROPER_NOUN_RE = re.compile(r"[A-Z][a-z]{2,}")
SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")

#: 句首常见的普通词。它们总以大写出现，但不是专名 —— 不排掉会把噪音放大成依赖边。
STOPWORDS = frozenset(
    {
        "The", "This", "That", "These", "Those", "There", "Then", "Than", "They",
        "Them", "Their", "Theirs", "She", "Her", "Hers", "He", "Him", "His", "It",
        "Its", "You", "Your", "Yours", "And", "But", "Not", "Now", "What", "When",
        "Where", "Why", "How", "Who", "Whom", "Whose", "Which", "If", "No", "Yes",
        "Oh", "Well", "So", "Do", "Did", "Does", "Can", "Could", "Would", "Should",
        "Will", "Shall", "May", "Might", "Must", "Have", "Has", "Had", "Was", "Were",
        "Are", "Is", "Be", "Been", "Being", "Just", "Only", "Even", "Still", "Yet",
        "Also", "Too", "Very", "Really", "Maybe", "Perhaps", "Please", "Sorry",
        "Thanks", "Thank", "Hello", "Hi", "Hey", "Okay", "Right", "Left", "One",
        "Two", "Three", "Let", "Get", "Got", "Come", "Go", "Look", "See", "Know",
    }
)


def _scan_text(text: str) -> tuple[set[str], set[str]]:
    """扫描一段文本，返回 ``(出现的专名候选, 在非句首位置出现过的)``。"""
    seen: set[str] = set()
    non_initial: set[str] = set()
    for sentence in SENTENCE_SPLIT_RE.split(text):
        words = WORD_RE.findall(sentence)
        for index, word in enumerate(words):
            if word in STOPWORDS or not PROPER_NOUN_RE.fullmatch(word):
                continue
            seen.add(word)
            if index > 0:
                non_initial.add(word)
    return seen, non_initial


def _heuristic_candidates(
    graph: PathGraph, *, min_strength: float = DEFAULT_MIN_STRENGTH
) -> list[GraphEdge]:
    """基于共享实体的启发式候选（占位实现，见 :class:`HeuristicDependencyPolicy`）。

    只处理 label 区域：模块区（``define`` / ``strings`` 那些界面文本）既不提供上下文，
    也不需要上下文，不该被卷进依赖图。

    产出的边一律 ``direction="reading_order"`` —— 它只敢说"这两处有关系"，
    不敢说"谁在谁之前"。
    """
    regions = graph.regions()
    order = {region.region_id: index for index, region in enumerate(regions)}
    usable = [region for region in regions if region.kind != "module"]

    speakers: dict[str, set[str]] = {}
    nouns: dict[str, set[str]] = {}
    texts: dict[str, set[str]] = {}
    occurrences: Counter[str] = Counter()
    non_initial_global: set[str] = set()
    #: 每个专名出现在**多少个区域**里（每区域只计一次）
    region_frequency: Counter[str] = Counter()

    for region in usable:
        region_id = region.region_id
        speakers[region_id] = set()
        nouns[region_id] = set()
        texts[region_id] = set()
        for node_id in region.node_ids:
            unit = graph.nodes[node_id].unit
            if unit is None:
                continue
            if unit.speaker:
                speakers[region_id].add(unit.speaker)
            seen, non_initial = _scan_text(unit.source)
            nouns[region_id] |= seen
            occurrences.update(seen)
            non_initial_global |= non_initial
            normalized = unit.source.strip()
            if len(normalized) >= MIN_REUSED_LENGTH:
                texts[region_id].add(normalized)
        region_frequency.update(nouns[region_id])

    # 全局筛一遍：只出现一次、且永远在句首的大写词，多半只是句子开头，不是专名
    proper_nouns = {
        word
        for word, count in occurrences.items()
        if count >= 2 or word in non_initial_global
    }
    # 再按"出现在多少个区域里"砍一刀：到处都是的候选是常用词
    common_cutoff = max(2, int(len(usable) * MAX_REGION_RATIO))
    knowledge_nouns = {
        word for word in proper_nouns if region_frequency[word] <= common_cutoff
    }

    candidates: list[GraphEdge] = []
    for index, provider in enumerate(usable):
        for consumer in usable[index + 1 :]:
            if order[provider.region_id] > order[consumer.region_id]:
                continue  # 方向必须沿阅读顺序：先出现的交代后出现的

            character_topics: list[str] = []
            opaque_speakers: list[str] = []
            score = 0.0
            for speaker in sorted(speakers[provider.region_id] & speakers[consumer.region_id]):
                score += SIGNAL_WEIGHTS["character"]
                if "[" in speaker or "{" in speaker:
                    opaque_speakers.append(speaker)
                else:
                    character_topics.append(f"character:{speaker}")
            # 玩家角色与具名角色**同时**出现在这条边的两端时，额外声明一个"同场"知识点：
            # 它让世界书里关于这两人关系的那一条，正好在需要它的地方被取到。
            # 这是对"两个 marker 同时在场"的直接陈述，不声称任何它不知道的关系内容。
            if opaque_speakers and character_topics:
                character_topics.append(
                    "character:pair:"
                    + "+".join(sorted(
                        [t.split(":", 1)[1] for t in character_topics] + opaque_speakers
                    ))
                )
            shared_nouns = (nouns[provider.region_id] & nouns[consumer.region_id]) & knowledge_nouns
            # 有区分度的名字优先（区域频率低的先），同频按字母序，保证确定性
            term_topics = [
                f"term:{noun}"
                for noun in sorted(shared_nouns, key=lambda w: (region_frequency[w], w))
            ]
            for topic in term_topics:
                score += SIGNAL_WEIGHTS["term"]
            text_topics = [
                f"text:{text}"
                for text in sorted(texts[provider.region_id] & texts[consumer.region_id])
            ]
            for topic in text_topics:
                score += SIGNAL_WEIGHTS["text"]

            if score < min_strength:
                continue
            # 人物在最前（最稳定、最该进术语表），其次名字，最后整句复用；总数封顶
            topics = (character_topics + term_topics + text_topics)[:MAX_TOPICS_PER_EDGE]
            note_bits = (
                character_topics
                + [f"character:{speaker}（插值名，不可查）" for speaker in opaque_speakers]
                + term_topics
                + text_topics
            )
            candidates.append(
                GraphEdge(
                    source=provider.region_id,
                    target=consumer.region_id,
                    topics=topics,
                    provenance="heuristic",
                    direction="reading_order",
                    note="共享 " + "、".join(note_bits[:4]) if note_bits else "共享实体",
                )
            )
    return candidates


# --------------------------------------------------------------------------- #
# 策略插槽：判断依赖的方法本身是可替换的
# --------------------------------------------------------------------------- #


class DependencyPolicy(Protocol):
    """依赖判断策略。

    这是留给"后人智慧"的插槽。框架只负责**表达**依赖（provider / consumer /
    topics / strength / direction / provenance）并**消费**它（分层、调度）；
    "怎么判断出这些边"是可以整体替换的一件事。

    想接更强的判断方法（控制流分析、LLM 判断、agent 交互确认），实现这个协议即可，
    框架的其他部分一行都不用改。
    """

    name: str
    #: 这套判断方法是否经过真实数据校准。占位实现必须老实说 False。
    validated: bool

    def describe(self) -> dict[str, Any]:
        ...

    def propose(self, graph: PathGraph) -> list[GraphEdge]:
        ...


@dataclass
class HeuristicDependencyPolicy:
    """占位实现：基于共享实体（说话人 / 专名 / 整句复用）的启发式。

    **这不是"判断依赖的正确方法"。** 信号权重与 ``min_strength`` 门槛都是未经真实
    数据校准的初值，存在的意义只是让框架能跑通、让 agent 有草案可改。``score`` 只用来
    决定**这条边提不提**（过门槛），不写进边的 ``weight`` —— 边权重是载荷（提供者首见
    ∩ 消费者用到 的条目数），由术语书现算，不是这里能填的。

    它的能力边界很明确：**能回答"两处有关系"，回答不了"谁在谁之前"。** 所以产出的
    边一律标成未确认方向，不参与调度排序 —— 拿它当排序依据会把并列支线错排成串行链。
    """

    name: str = "heuristic-sharing"
    validated: bool = False
    min_strength: float = DEFAULT_MIN_STRENGTH

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "validated": self.validated,
            "note": (
                "基于共享实体的启发式占位实现：只能判定两处有关系，判定不了先后；"
                "门槛未经真实数据校准。"
            ),
        }

    def propose(self, graph: PathGraph) -> list[GraphEdge]:
        return _heuristic_candidates(graph, min_strength=self.min_strength)


@dataclass
class ManualDependencyPolicy:
    """完全不猜：判断交给外部（agent / 用户），软件只读图里已经有的边。"""

    name: str = "manual"
    validated: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "validated": True,
            "note": "不做任何猜测，只使用外部写进路径图的依赖边。",
        }

    def propose(self, graph: PathGraph) -> list[GraphEdge]:
        return []


#: 默认策略。**扫描不再自动用它** —— 见 :func:`policy_for`。
DEFAULT_POLICY: DependencyPolicy = HeuristicDependencyPolicy()


def policy_for(options: Mapping[str, Any] | None = None) -> DependencyPolicy | None:
    """这次扫描要不要**猜**依赖边。返回 ``None`` = 不猜（默认）。

    为什么不猜：猜出来的边（共享说话人 / 专名 / 整句复用）实测没有效果 ——
    真实工程上它们绝大多数是普通词（`term:Back`、`term:Such`、`text:*Giggle*`），
    抽 30 条人工核查精确率 **40%**，其中 `dependency` 类 **0/15**。而它会进知识
    指纹，噪声一多，补译判定跟着抖。

    去掉的是"默认"，不是"能力"：``{"dependencies": True}``（或 ``"heuristic"``）
    就照旧产出候选，交给 agent / 人确认方向。

    决定顺序的边来自引擎自己的控制流（``jump`` / ``call``），那一条与这个开关无关。
    """
    raw = (options or {}).get("dependencies", False)
    if raw is True:
        return DEFAULT_POLICY
    if isinstance(raw, str) and raw.strip().lower() in ("heuristic", "heuristic-sharing"):
        return DEFAULT_POLICY
    if isinstance(raw, str) and raw.strip().lower() in ("manual", "none", ""):
        return None
    return None


def candidate_dependencies(
    graph: PathGraph,
    *,
    policy: DependencyPolicy | None = None,
    min_strength: float | None = None,
) -> list[GraphEdge]:
    """提出候选依赖边。

    ``policy`` 决定"怎么判断"；``min_strength`` 只是便利参数，等价于换一个阈值不同的
    启发式策略。**候选只是草案，不会自动写进图里** —— 要不要采纳，由 agent 或用户决定。
    """
    if policy is None:
        policy = (
            DEFAULT_POLICY
            if min_strength is None
            else HeuristicDependencyPolicy(min_strength=min_strength)
        )
    return policy.propose(graph)
