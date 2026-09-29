"""把"模型申报的专名/术语"用**确定性的语料证据**分类，并守住"哪类内容才产资产"。

**为什么要有这一组**：真靶实测（该工程，一次全量翻译）——模型申报 181 条，落成
181 条候选，`evidence` 是空的、`confidence` 一律 0.5。里面混着 `play`（它在原文里
命中 **213 次**，其中 213 次是全小写形态）、`coach`（164）、`player`（61）、`setter`（43）、
`scholarship`（43）这类**普通词**；一旦批准，它们会变成"每一句都注入的硬约束"，
还挤掉真正该钉的专名。

判据必须来自**语料**，不能来自模型自述。分界是这一条：

* **这个词有没有以全小写形态出现过** —— 有 → 普通词（`play` 小写 213 次 / 首字母大写 5 次）；
  没有 → 专名（`Orlando` 小写 0 次 / 大写 22 次，`Lexi` 0 / 181，`Herschel` 0 / 13）。

> **旧判据（"抬头率"）在本靶上恰好是反的**，记在这里免得有人再改回去：它数的是
> "大写出现里有多少次落在句子/分句开头"，想把 `Play`（句首大写）和 `Orlando`（专名）
> 分开。但我们的槽位是**逐句**的 —— 每条槽位文本的开头按定义就是"句首"，
> 于是 `Orlando` / `Baskerville` / `Campbell` / `Herschel` / `Valentine` / `Miller` /
> `Natsume` / `Mary` / `Barista` 全被判成"句首大写的普通词"，误杀 9 个真专名。
> 大小写**形态**（而不是位置）才有区分力。

另外两条证据与来源无关，直接采信：

* **人物名**：出现在引擎的角色定义表里（`define e = Character("Eve")` 的显示名），
  或出现在说话人栏里 —— 引擎自己说它是人物名；
* **词中大写**：`LopaPhi`、`KeyFrames` 这类驼峰写法（品牌 / 专名性写法）。

**"哪类内容才产资产"**也在这里定（策略是我们的，事实由适配层申报）：
适配层在单位元数据里申报 ``content_class``（``"story"`` / ``"interface"``），
只有 ``story`` 才产术语与世界书候选。界面与开发工具文件的文本**照样要翻**（内容范围
是引擎给的，见契约 §0.1），只是别拿它去产设定 —— 真靶 17 条只出现在
`ActionEditor*.rpy` / `screens.rpy` / `ui.rpy` 这类文件里的候选就是这么进来的。

用法（供内核与脚本共用）::

    from gametrans.layers.naming import classify_term, source_evidence
    verdict = classify_term("play", evidence)

判据只给**建议**（写在读数里、并决定这条申报这一轮**写不写进术语书**），
"丢还是留"仍由人或 agent 拍板 —— 不同意就自己写一行（`resource.term.add`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "ASSET_SOURCE_CONTENT_CLASSES",
    "DROP_TAG",
    "CorpusEvidence",
    "TermEvidence",
    "classify_term",
    "corpus_evidence_from_graph",
    "judge_terms",
    "produces_assets",
    "source_evidence",
]

#: 判定为"专名"要过的门槛（只以大写形态出现过几次才算数）
MIN_OCCURRENCES = 2

#: 只有这些内容类别才产资产。**空 = 适配层没申报 → 照旧产**（缺申报不许变成悄悄停产）。
ASSET_SOURCE_CONTENT_CLASSES = frozenset({"story"})

#: 被判定为"不该当术语"的候选身上打的标签。批准这样的条目要显式 force。
DROP_TAG = "termhood:drop"

_WORD_CHARS = "A-Za-z0-9'’"


def produces_assets(content_class: str) -> bool:
    """这个内容类别该不该产资产（术语候选 / 世界书候选）。

    没申报（空串）时返回 ``True``：**"适配层没说"不许变成"悄悄停产"** —— 那会让
    一个没实现这个能力的适配包看起来"跑了、只是产不出东西"。
    """
    text = str(content_class or "").strip()
    if not text:
        return True
    return text in ASSET_SOURCE_CONTENT_CLASSES


@dataclass
class CorpusEvidence:
    """整份原文语料上的事实（一次算好，所有候选共用）。"""

    text: str = ""
    #: 引擎角色定义表里的显示名（`Character("Eve")` → `Eve`）
    character_names: frozenset[str] = field(default_factory=frozenset)
    #: 角色定义的**代号**（`define e = ...` → `e`）—— 单字母代号按名字处理没有意义，
    #: 但多字母代号（`bi`、`dy`）能让"这个词在原文里从不大写"也仍然算人物名。
    character_aliases: frozenset[str] = field(default_factory=frozenset)
    #: 出现在说话人槽位上的原文（对白块里当名字用的那些句子）
    speaker_slots: frozenset[str] = field(default_factory=frozenset)


@dataclass
class TermEvidence:
    """一个候选词在语料上的证据。"""

    term: str
    occurrences: int = 0
    #: 以**首字母大写**形态出现的次数（`Play` 记这里）
    capitalized_occurrences: int = 0
    #: 以**全小写**形态出现的次数（`play` 记这里）—— 判"普通词"的就是它
    lowercase_occurrences: int = 0
    inner_caps: bool = False
    is_character_name: bool = False
    quoted: bool = False

    @property
    def has_lowercase_form(self) -> bool:
        return self.lowercase_occurrences > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "term": self.term,
            "occurrences": self.occurrences,
            "capitalized": self.capitalized_occurrences,
            "lowercase": self.lowercase_occurrences,
            "inner_caps": self.inner_caps,
            "character_name": self.is_character_name,
            "quoted": self.quoted,
        }


def _count_forms(text: str, term: str) -> tuple[int, int, int, bool, bool]:
    """返回 ``(总次数, 首字母大写次数, 全小写次数, 词中大写, 引号内出现过)``。"""
    if not term:
        return 0, 0, 0, False, False
    pattern = re.compile(
        rf"(?<![{_WORD_CHARS}]){re.escape(term)}(?![{_WORD_CHARS}])", re.IGNORECASE
    )
    total = capitalized = lowercase = 0
    inner_caps = False
    quoted = False
    for match in pattern.finditer(text):
        total += 1
        piece = match.group(0)
        if piece[:1].isupper():
            capitalized += 1
        else:
            lowercase += 1
        # 词中大写 = **词内部**的大写（`KeyFrames` / `LopaPhi` / `Ren'Py`）。
        # 两条都得当心，都是真靶上撞出来的：
        # * 判据要按**词**看，不能拿整串看 —— `Wild Cats` 的第二个词首字母也是大写，
        #   那是"每个词都大写"的普通专名短语，不是驼峰写法；
        # * **全大写**的渲染（台词里喊 `COACH`）不算驼峰 —— 拿它判会把 `coach` /
        #   `player` 这类普通词误判成品牌写法（第一次跑对照实验时就这么错了）。
        if any(
            not word.isupper() and any(ch.isupper() for ch in word[1:])
            for word in piece.split()
        ):
            inner_caps = True
        left = text[max(0, match.start() - 1) : match.start()]
        if left in ("\"", "“", "‘", "'"):
            quoted = True
    return total, capitalized, lowercase, inner_caps, quoted


def source_evidence(text: str, term: str, corpus: CorpusEvidence) -> TermEvidence:
    """把候选词在语料上的事实算出来。"""
    total, capitalized, lowercase, inner_caps, quoted = _count_forms(text, term)
    words = term.split()
    is_character = term in corpus.character_names or (
        len(words) == 1 and term in corpus.character_aliases
    )
    # 说话人槽位上的名字也是**人物名**的强证据（`"Girl" "…"` 里的 Girl）
    if not is_character and term in corpus.speaker_slots:
        is_character = True
    return TermEvidence(
        term=term,
        occurrences=total,
        capitalized_occurrences=capitalized,
        lowercase_occurrences=lowercase,
        inner_caps=inner_caps,
        is_character_name=is_character,
        quoted=quoted,
    )


def classify_term(term: str, evidence: TermEvidence) -> tuple[bool, str]:
    """决定这个候选**该不该当术语约束**，并给出可复核的理由。

    返回 ``(是否保留, 理由)``。理由会写进候选身上 —— 让人和 agent 都能一眼看出
    "这条为什么留下/为什么挡下"，而不是只看一个分数。
    """
    term = term.strip()
    if not term:
        return False, "空词"

    # 1) 引擎自己说是人物名 —— 最高优先，直接留下（大小写形态管不着）
    if evidence.is_character_name:
        return True, f"人物名（引擎角色定义/说话人栏见过它）·出现 {evidence.occurrences} 次"

    # 2) 语料里从没出现过 —— 证明不了它是这部游戏里的东西
    if evidence.occurrences == 0:
        return False, "原文语料里没出现过 · 无法证明它是这款游戏里的东西"

    # 3) 非拉丁词：大小写判据不适用，如实说，不假装判过
    if not re.search(r"[A-Za-z]", term):
        return True, f"非拉丁词，大小写判据不适用（判断权在人手里）·出现 {evidence.occurrences} 次"

    # 4) 驼峰/词中大写：品牌、专名性写法（`LopaPhi` / `KeyFrames`）
    if evidence.inner_caps:
        return True, f"词中有大写（品牌/驼峰写法）·出现 {evidence.occurrences} 次"

    # 5) **有全小写形态出现过 → 普通词**。这是本靶上唯一有区分力的那条判据。
    if evidence.has_lowercase_form:
        return False, (
            f"出现过全小写形态 {evidence.lowercase_occurrences} 次"
            f"（首字母大写 {evidence.capitalized_occurrences} 次）"
            f"· 普通词，不是专名"
        )

    words = term.split()
    # 6) 多词短语：**至少两个词大写**才算专名性短语（地名 / 队名 / 作品名）。
    #
    # 门槛为什么从"一个词大写"抬到两个：真靶上 `Basketball coach`（只有一个词大写，
    # 因为它在句首/菜单里被大写）就这么混进了术语书，同类的还有 `volleyball coach`、
    # `women's volleyball team`、`middle blocker` —— 它们是**普通短语**（职业/位置），
    # 不是名字。真正的多词专名（`Bella Vista` / `Mr. Campbell` / `Wild Cats`）两个词都大写。
    if len(words) >= 2:
        uppercase_words = sum(1 for word in words if word[:1].isupper())
        if uppercase_words >= 2:
            return True, (
                f"多词专名（{uppercase_words}/{len(words)} 个词大写）"
                f"·出现 {evidence.occurrences} 次"
            )
        return False, (
            f"多词普通短语（{uppercase_words}/{len(words)} 个词大写，不够两个）"
            f"·出现 {evidence.occurrences} 次"
        )

    # 7) 单词：只以大写形态出现过 → 专名。
    #    刻意**不按频次挡**：只出现一次的人名照样要钉（不钉才会长出第二种写法，
    #    真靶的 `莱克西 / 蕾克西` 就是这么来的）；频次只写进理由，让人自己掂量。
    #
    # 这一条挡不住的东西要说清楚：**菜单项 / 饮料食物这类普通名词**只要从没以小写
    # 出现过（`Capuccino`），形态上和人名一模一样 —— 判据分不出来，防线在提示词
    # （不列普通名词）与人的眼睛上。别把这里当成"已经判过了"。
    thin = "（证据薄：只出现 1 次）" if evidence.occurrences < MIN_OCCURRENCES else ""
    return True, (
        f"只以大写形态出现 {evidence.capitalized_occurrences} 次 —— 专名{thin}"
    )


def corpus_evidence_from_graph(graph: Any) -> CorpusEvidence:
    """从路径图里取语料与"引擎自己说的人物名"。

    两样都从**已经提取好的结构**里读，不额外扫盘：

    * 语料 = 全部槽位原文（与 :meth:`ProjectSession.source_slots` 同一口径）；
    * 人物名 = 每个槽位上的 ``display_speaker``（适配层从
      ``define e = Character("Eve")`` 读出来的显示名）+ 多字母角色代号。
    """
    texts: list[str] = []
    names: set[str] = set()
    aliases: set[str] = set()
    speakers: set[str] = set()
    for node in graph.translatable_nodes():
        unit = getattr(node, "unit", None)
        if unit is None:
            continue
        payload = getattr(getattr(unit, "locator", None), "payload", None) or {}
        entries = payload.get("slots") or []
        if not entries:
            texts.append(str(unit.source or ""))
            continue
        for entry in entries:
            texts.append(str(entry.get("source") or ""))
            speaker = str(entry.get("display_speaker") or "").strip()
            if speaker:
                names.add(speaker)
            variable = str(entry.get("speaker") or "").strip()
            if len(variable) > 1:
                aliases.add(variable)
            # 锚在**骨架块**上的槽位就是"引号说话人的名字"（`"Girl" "…"` 里的 Girl）：
            # 引擎把这一串当名字用，这是比大小写形态更硬的证据。
            if str(entry.get("anchor") or "") == "block":
                source = str(entry.get("source") or "").strip()
                if source:
                    speakers.add(source)
    return CorpusEvidence(
        text="\n".join(texts),
        character_names=frozenset(names),
        character_aliases=frozenset(aliases),
        speaker_slots=frozenset(speakers),
    )


def judge_terms(
    pairs: Iterable[tuple[str, str]], corpus: CorpusEvidence
) -> dict[str, tuple[bool, str]]:
    """一次把一批申报判完：``{原文: (是否保留, 理由)}``。"""
    verdicts: dict[str, tuple[bool, str]] = {}
    for source, _target in pairs:
        text = str(source).strip()
        if not text or text in verdicts:
            continue
        verdicts[text] = classify_term(text, source_evidence(corpus.text, text, corpus))
    return verdicts
