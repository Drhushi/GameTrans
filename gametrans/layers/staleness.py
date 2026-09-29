"""补译定位：找出"因为相关知识变了而需要重做"的译文。

这是让「先降级并行、事后补译」成立的前提。没有它，补译就只能是"全量重翻一遍"，
那比严格拓扑序还贵 —— 那这条路线就没意义了。

做法是比对**指纹**：翻译时记下这条文本命中的术语/世界书内容的指纹，现在再算一次。
不一样就说明它的知识状态变了。

这个办法会自动收窄范围：一句不提任何术语的旁白永远不会过期；提到 "Eileen" 的那条，
只在 Eileen 的词条真的改了的时候才过期。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gametrans.core.graph import PathGraph
from gametrans.core.models import TranslationArtifact, is_placeholder
from gametrans.layers.resource import ResourceLayer, knowledge_fingerprint

__all__ = [
    "StaleUnit",
    "StalenessReport",
    "find_stale",
    "knowledge_fingerprint",
]


@dataclass
class StaleUnit:
    """一条需要重新处理的译文，附带足以让 agent 判断的上下文。"""

    unit_id: str
    path: str
    source: str
    reason: str
    recorded: str = ""
    current: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "path": self.path,
            "source": self.source,
            "reason": self.reason,
            "recorded_fingerprint": self.recorded,
            "current_fingerprint": self.current,
        }


@dataclass
class StalenessReport:
    """一次过期检查的结果。

    五类分开报，因为它们该被区别对待：

    * ``unusable`` —— 有记录但状态不是 ok（失败 / 待复核），**必须重做或交人处理**
    * ``stale`` —— 知识变了，可以放心重译
    * ``untracked`` —— 当时没记指纹，判断不了；**报出来让人决定**，不要默默重烧一遍
    * ``missing`` —— 压根还没有译文。含两类：**连记录都没有**的，与**占位记录**
      （`models.PLACEHOLDER_ERROR`：台账覆盖全图时给"没排进这一轮"的槽位留的空记录）——
      后者不是失败，别把它读成"翻坏了"
    * ``fresh`` —— 计数，不用重做
    """

    unusable: list[StaleUnit] = field(default_factory=list)
    stale: list[StaleUnit] = field(default_factory=list)
    untracked: list[StaleUnit] = field(default_factory=list)
    missing: list[StaleUnit] = field(default_factory=list)
    fresh: int = 0

    @property
    def needs_work(self) -> list[StaleUnit]:
        """确定该动手的：不可用 + 过期 + 缺失。``untracked`` 不在此列，需要人来拍板。"""
        return [*self.unusable, *self.missing, *self.stale]

    def to_dict(self) -> dict[str, Any]:
        return {
            "unusable": [s.to_dict() for s in self.unusable],
            "stale": [s.to_dict() for s in self.stale],
            "untracked": [s.to_dict() for s in self.untracked],
            "missing": [s.to_dict() for s in self.missing],
            "fresh": self.fresh,
            "needs_work": len(self.needs_work),
        }


def find_stale(
    graph: PathGraph,
    resources: ResourceLayer,
    records: list[TranslationArtifact],
    *,
    use_glossary: bool = True,
    use_worldbook: bool = True,
    use_style: bool = True,
    use_knowledge: bool = True,
    custom_instructions: str = "",
) -> StalenessReport:
    """逐条比对知识指纹，判断哪些译文需要重做。

    对账粒度是**槽位**：翻译记录按槽位记账（一个单元含多句时逐句一条），
    指纹也是按那句自己的原文算的 —— "改一条术语只作废提到它的那几句"靠这个。

    两处口径必须**逐项一致**：翻译层记指纹时算进了文本命中的术语与世界书、依赖图给的
    知识点、按作用域解析到这一条上的**风格**、已批准的**知识**，以及各个 ``use_*``
    开关。判定时少算任一项，两边的指纹就永远对不上 —— 表现是"什么都没改，全部译文
    都被点名成过期"，事后补译当场退化成全量重翻。

    所以这里传的不只是开关，还有**每条自己的作用域**（说话人 / 场景）—— 风格是按
    作用域解析的，不传作用域就解析不出当初实际注入了什么。自定义翻译要求同理：
    它会进提示词，就得进指纹，否则改了口径一条都不会被判过期（见 R46）。
    """
    by_key = {str(record.unit_id): record for record in records}
    report = StalenessReport()

    for node in graph.translatable_nodes():
        unit = node.unit
        if unit is None:
            continue
        context = unit.context
        # 槽位原文住在定位载荷里；老数据没有逐槽位信息时整单元一条
        slot_sources = {
            str(entry.get("slot_key") or ""): str(entry.get("source") or "")
            for entry in (unit.locator.payload or {}).get("slots") or []
            if entry.get("slot_key")
        }
        keys = [str(key) for key in (unit.metadata.get("slot_keys") or [])] or [unit.id]
        for key in keys:
            if key in slot_sources:
                source = slot_sources[key]
                if not source.strip():
                    # **空原文的兜底槽位**（真靶 act3/act4/act7/act8 各有两条 `*_0ae9bcd0`）：
                    # 它没有原文可翻，也就没有知识依赖。以前这里退到 `unit.source`（整单元
                    # 两万多字符）去算指纹，与记录里那份空指纹**永远对不上** —— 读数里
                    # 于是永远挂着 7 条"过期"，重跑也修不掉。
                    continue
            else:
                # 老数据没有逐槽位信息：整单元一条
                source = unit.source
            # 指纹按**这一条槽位的原文 + 作用域**算：知识状态变了，指纹才变。
            current = resources.fingerprint_for(
                source,
                use_glossary=use_glossary,
                use_worldbook=use_worldbook,
                use_style=use_style,
                use_knowledge=use_knowledge,
                unit_id=unit.id,
                speaker=context.speaker if context else None,
                scene=context.scene if context else None,
                custom_instructions=custom_instructions,
            )
            record = by_key.get(key)

            if record is None:
                report.missing.append(
                    StaleUnit(
                        unit_id=key,
                        path=node.path,
                        source=source,
                        reason="还没有译文",
                        current=current,
                    )
                )
                continue

            if not record.is_usable:
                if is_placeholder(record):
                    # **还没轮到它**：台账照旧覆盖全图，没排进这一轮的槽位占一条空记录
                    # （见 `models.PLACEHOLDER_ERROR`）。它不是"试过、坏了"，所以归
                    # `missing` —— 否则一次"跑到第 3 阶段就停"的跑批会把全工程读成翻坏了。
                    report.missing.append(
                        StaleUnit(
                            unit_id=key,
                            path=node.path,
                            source=source,
                            reason="还没有译文（前面几轮没排到它）",
                            current=current,
                        )
                    )
                    continue
                # 状态不是 ok（失败 / 待复核）：它不是"新鲜"，而是**确定要处理** ——
                # 补译输入漏掉它，就等于把坏条目永远留在工作区里
                report.unusable.append(
                    StaleUnit(
                        unit_id=key,
                        path=node.path,
                        source=source,
                        reason=f"译文不可用（状态 {record.status.value}）",
                        recorded=record.knowledge_fingerprint,
                        current=current,
                    )
                )
                continue

            if not record.is_tracked:
                report.untracked.append(
                    StaleUnit(
                        unit_id=key,
                        path=node.path,
                        source=source,
                        reason="翻译时没有记录知识状态，判断不了是否过期",
                        current=current,
                    )
                )
                continue

            if record.knowledge_fingerprint != current:
                report.stale.append(
                    StaleUnit(
                        unit_id=key,
                        path=node.path,
                        source=source,
                        reason="相关知识在翻译之后发生了变化",
                        recorded=record.knowledge_fingerprint,
                        current=current,
                    )
                )
                continue

            report.fresh += 1

    return report
