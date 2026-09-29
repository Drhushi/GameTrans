"""术语标签：术语书里**还没定译**的写法，送模型前包成 ``⟦写法⟧``。

**为什么要它。** 术语书里有一批"只有写法、没有译名"的行（第一批由场摘要抽出来）。
从前模型遇到它们就自己起一个中文名，于是同一个 `Lexi` 在一场里叫「莱克西」、另一场叫
「蕾克西」——真靶实测同一个人两种译名。标签把这件事挪到**一处**去定：请求里要求模型
原样把 ``⟦写法⟧`` 抄回来，翻译那一步不再各自起名；名字由人或 agent 定**一次**，
之后按当前术语书渲染已翻好的译文（见 :func:`render` 与 `layers/writeback.py`）。

三个性质，缺一条这个机制就不成立：

* **标签是写法的纯函数**（``⟦Eve Herschel⟧``）。不需要第二张映射表 —— 术语书就是
  那张表；改口径（换个译名）不必重翻，重新渲染一次即可。
* **它可判**。译文里的标签必须与"这一条原文该出现的标签"逐条相等
  （``core/constraints.py`` 的 ``term_tag_preserved``）：少了 = 模型把标签翻掉了、
  多出 / 改名 = 不知道从哪冒出来的标签，两种都判违规、都不写回，不静默。
* **定译只有一条路可走**：模型申报的译名**照旧能填上
  空译名那一栏**（`layers/knowledge.py::absorb_declared`，留着它省一次定名调用）；
  人要改就走待审通道或直接改那一行 —— 已经产出的译文**不用重翻**，写回那一刻按最新
  术语书渲染。两半合起来才是"一处一个决定"：名字要么来自模型申报、要么来自人，
  但**都落在术语书那一行上**，翻译里不再有第二个说法。

只包**没有译名**的写法：已经有译名的行照旧走"``- 写法 → 译名``"那条硬约束通道，
两条通道不混（否则同一件事有两个说法）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from gametrans.core.constraints import TERM_TAG_RE, term_tags_in
from gametrans.layers.trigger import TriggerPolicy, pattern_for

__all__ = [
    "announce",
    "paused_regions",
    "pending_writings",
    "render",
    "tag",
    "unwritten_writings",
    "wrap",
    "writings_to_tag",
]


def announce(writings: Iterable[str]) -> str:
    """一次请求里那几条"还没定译的名字"的说明段（**只说一次**，不逐行重复）。

    为什么跟着请求走、而不是写进提示词模板：模板是用户可改的，改模板不该让机制失效；
    而这一段只在真的有标签时才出现，没有标签的请求一个字符都不多。
    """
    listed = [str(writing).strip() for writing in writings if str(writing or "").strip()]
    if not listed:
        return ""
    return (
        "【还没定译的名字】\n"
        "下面这些写法在原文里已经包成 ⟦⟧：照原文**原样保留那个标签**，"
        "不要翻译它、拆开它（不必在译文里给它起中文名 —— 名字统一记在术语书上）。\n"
        + "\n".join(f"- {tag(writing)}" for writing in listed)
    )


#: 译文里还能找到的标签 = 这一步还没定的名字（按出现顺序、去重）。
tags_in = term_tags_in


def tag(writing: str) -> str:
    """一个写法的标签写法（唯一一处拼装）。"""
    return f"⟦{str(writing).strip()}⟧"


def unwritten_writings(entry: Any) -> list[str]:
    """这一行里**没有译名**的写法（有译名的写法不包标签，走硬约束那条通道）。"""
    return [
        str(item.get("writing") or "").strip()
        for item in (getattr(entry, "key", None) or [])
        if str(item.get("writing") or "").strip() and not str(item.get("target") or "").strip()
    ]


def pending_writings(termbook: Any) -> list[str]:
    """术语书里**一条译名都没有**的写法（审核时要看的就是这份清单）。

    顺序取书里的行序（就是插入顺序），只去重、不排序 —— 给人看的清单不该自己重排。
    """
    found: list[str] = []
    for entry in getattr(termbook, "entries", lambda: [])():
        for writing in unwritten_writings(entry):
            if writing not in found:
                found.append(writing)
    return found


def writings_to_tag(
    termbook: Any, text: str, *, policy: TriggerPolicy | None = None
) -> list[str]:
    """这段原文里**该包标签**的写法，按它在原文里的出现位置排序。

    命中判定复用注入那一套（:mod:`gametrans.layers.trigger`）——"什么时候触发"
    只有一处定义，包裹与注入才不会各说各话。
    """
    source = str(text or "")
    if not source or termbook is None:
        return []
    policy = policy or TriggerPolicy()
    #: 先做一次便宜的预筛（字面出现），再上正则 —— 术语书几百行、每行都要跑正则的话，
    #: 逐槽位算指纹会把整轮拖慢；字面不出现在这一段里的写法根本不用试。
    lowered = source.lower()
    spotted: list[tuple[int, int, str]] = []
    for entry in getattr(termbook, "entries", lambda: [])():
        for writing in unwritten_writings(entry):
            if writing.lower() not in lowered:
                continue
            match = pattern_for(writing, policy=policy).search(source)
            if match is not None:
                spotted.append((match.start(), -len(writing), writing))
    spotted.sort()
    found: list[str] = []
    for _start, _length, writing in spotted:
        if writing not in found:
            found.append(writing)
    return found


def wrap(text: str, writings: Iterable[str], *, policy: TriggerPolicy | None = None) -> str:
    """把 ``writings`` 在 ``text`` 里逐处包成标签。

    **长写法优先、不叠包**：`Eve Herschel` 里的 `Eve` 不该再单独包一层
    （那样就成了 ``⟦Eve⟧ Herschel``，模型与对账都会看不懂）。
    """
    source = str(text or "")
    wanted = [str(writing).strip() for writing in writings if str(writing or "").strip()]
    if not source or not wanted:
        return source
    policy = policy or TriggerPolicy()
    spans: list[tuple[int, int, str]] = []
    for writing in wanted:
        for match in pattern_for(writing, policy=policy).finditer(source):
            spans.append((match.start(), match.end(), writing))
    if not spans:
        return source
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    pieces: list[str] = []
    cursor = 0
    for start, end, writing in spans:
        if start < cursor:
            continue  # 已被更长的写法吃掉
        pieces.append(source[cursor:start])
        pieces.append(tag(writing))
        cursor = end
    pieces.append(source[cursor:])
    return "".join(pieces)


def render(text: str, termbook: Any) -> tuple[str, list[str]]:
    """把标签按**当前**术语书渲染成译名，返回 ``(渲染后的文本, 还没译名的写法)``。

    渲染不改任何已产出的译文记录：盘上躺着的仍是标签，写回 / 导出那一刻才渲染
    （``layers/writeback.py``）。所以换一个译名不是一次重翻，只是一次重新渲染。

    还没译名的标签**原样留着**并如实报出来 —— 调用方据此拒绝写回那一条，
    绝不把一个 ``⟦…⟧`` 写进游戏文件。译名那一栏自己带标签的**同样当没译名**
    （那是这条不变量的兜底：不管标签是从哪条路进来的，出去的时候都不能是它）。
    """
    source = str(text or "")
    unresolved: list[str] = []

    def swap(match: Any) -> str:
        writing = str(match.group(1)).strip()
        entry = termbook.find(writing) if termbook is not None else None
        target = entry.target_for(writing) if entry is not None else ""
        # 译名那一栏自己还带着标签（历史产物 / 手改出来的行）**不算渲染**：把它换回去
        # 等于把记号写进游戏文件，正是这个方法要挡住的那件事。当"还没译名"处理。
        if str(target or "").strip() and not TERM_TAG_RE.search(str(target)):
            return str(target)
        if writing not in unresolved:
            unresolved.append(writing)
        return match.group(0)

    return TERM_TAG_RE.sub(swap, source), unresolved


def paused_regions(
    termbook: Any,
    flow: Any,
    texts: Mapping[str, Iterable[str]],
) -> dict[str, list[str]]:
    """哪些区域在**等定译**，以及它在等哪些写法。

    口径：**一个区域要用到"别人引入、而名字还没定"的东西，它就还不能开跑。**
    所以要同时满足三件事，缺一不算：

    1. 那个写法**没有译名**（``writings_to_tag`` 已经只挑这种）；
    2. 那个写法所属实体的**引入场不是本区域**（``flow.home``）—— 也就是"这个名字是
       别处先交代的"。这一条顺手排除了"这段自己首见的实体"，所以**没有前驱的区域不会
       因为自己引入的名字没定就被卡住**；
    3. 它在**这一段原文里真的出现**（否则包不成标签，也就谈不上"需要"）。

    ⚠️ 第 2 条刻意用 **引入场**（``flow.home``）而不是 ``flow.why()`` 那份**化简过的**
    前驱集：化简那一刀砍的是"要不要单独等它"（执行顺序），而这里问的是"这段会不会吐出
    `⟦写法⟧`"（产出质量）。实测差别很大 —— Eve 的译名清空后，按化简前驱只挡 3 个区域，
    按引入场挡 27 个；而**那 27 个区域每一个都会吐 `⟦Eve⟧`**，写回时全被置留。

    **刻意不做传递**（第二条口径 —— "定译完才放行"太严苛）：
    前驱被暂停**不连坐**后继。后继要等的话，等的是**它自己用到的名字**：那个实体是前驱
    引入的、还没定译，它自己就会被第 1–3 条挡下。知识注入读的是**术语书**而不是前驱的
    译文，所以前驱没跑并不妨碍后继拿到上下文。

    ``texts`` 是 ``区域 → 那一段的原文``（一个区域可能有多条，给序列即可）。
    返回 ``区域 → 等着的写法``，没被暂停的区域不在表里。纯读，不改任何东西。
    """
    if termbook is None or flow is None or not texts:
        return {}
    owner: dict[str, str] = {}
    for entry in getattr(termbook, "entries", lambda: [])():
        for writing in getattr(entry, "writings", []) or []:
            owner[str(writing)] = str(getattr(entry, "writing", "") or "")

    paused: dict[str, list[str]] = {}
    for region, sources in texts.items():
        region = str(region)
        found: list[str] = []
        for source in sources or ():
            for writing in writings_to_tag(termbook, str(source)):
                entity = owner.get(writing)
                # 引入场就是本区域 → 这个名字是这一段自己交代的，不归它等
                if not entity or flow.home.get(entity, region) == region:
                    continue
                if writing not in found:  # 同一段的多个槽位里出现：只点名一次
                    found.append(writing)
        if found:
            paused[region] = found
    return paused
