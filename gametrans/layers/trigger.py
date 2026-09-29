"""命中规则：一条文本里出现了某个"键"，就算命中。术语表与世界书**共用这一套**。

为什么合并：以前术语走 `Glossary.lookup`（整段子串匹配）、世界书走 `Worldbook.search`
（另一份子串匹配），两套代码两种口径 —— "什么时候触发"没有唯一答案，改一处另一处不动。
SillyTavern 的 World Info 把这两件事放在**同一个键命中机制**里，区别只在内容的作用
（钉住译法 vs 插入背景）。

三条规则（都是实测踩出来的）：

* **拉丁字母键按整词匹配**：`Eve` 不该命中 `Evening` —— 真靶的术语表里就有 `Eve`，
  按子串匹配它会在所有含 "Evening" 的句子上触发。
* **CJK 键按子串匹配**：中文没有词边界，SillyTavern 也建议对 CJK 关掉整词匹配。
* **扫描到"句"一级**：键出现在**某一句**里算命中，而不是"整段任意位置"。
  块级单元动辄几百句，按整段匹配等于"几乎全触发"（ST 的 Scan Depth 就是这个意思：
  限定扫多少内容，不是无限扫）。

命中要**带证据**：命中了哪一句、哪个键 —— 审核时要看的就是这个。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

__all__ = [
    "Hit",
    "TriggerPolicy",
    "hit_counts",
    "is_cjk",
    "is_placeholder_key",
    "key_in_text",
    "keys_in_text",
    "pattern_for",
    "scan_slots",
    "usable_keys",
]

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_WORD_CHARS = r"[0-9A-Za-z_\u00c0-\u024f]"

#: 整个键就是一段占位符 / 模板 token（`[name]`、`{0}`、`<name>`、`%s`、`$name`、`\n`）。
#: 这类东西**不是名字**：真靶实测模型给一条世界书配了键 `[name]`，它会在 99 句彼此无关的
#: 句子上触发（那些句子只是都提到了玩家名）。占位符由术语表与结构层处理。
_PLACEHOLDER_RE = re.compile(
    r"^(?:"
    r"\[[^\[\]]{1,40}\]"
    r"|\{\{?[^{}]{1,40}\}?\}"
    r"|<[^<>]{1,40}>"
    r"|%\d*\$?[sdifrg]"
    r"|\$[A-Za-z_]\w*"
    r"|\\.?"
    r")$"
)


def is_placeholder_key(text: str) -> bool:
    """这个键整条就是占位符 / 模板 token 吗（是的话它不能当触发键）。"""
    stripped = str(text or "").strip()
    return bool(stripped) and bool(_PLACEHOLDER_RE.match(stripped))


def usable_keys(
    keys: Iterable[str], *, min_length: int = 2
) -> list[str]:
    """挑出**能当触发键**的：去掉占位符、空串、以及比 ``min_length`` 还短的。

    短键按定义就是几乎处处命中（R19 的 `term:Back` / `term:Such` 就是这么来的），
    所以这里挡在门外 —— 但**只在模型申报这条通道上**用：人写进文件的键照旧说了算。
    """
    found: list[str] = []
    for key in keys:
        text = str(key or "").strip()
        if not text or len(text) < min_length or is_placeholder_key(text):
            continue
        if text not in found:
            found.append(text)
    return found


def is_cjk(text: str) -> bool:
    """这段文本里有 CJK 字符吗（有就按子串匹配，不做词边界）。"""
    return bool(_CJK_RE.search(text or ""))


@dataclass(frozen=True)
class TriggerPolicy:
    """触发规则。**只有这一处**定义"什么算命中"。"""

    #: 拉丁键要不要整词匹配（CJK 键永远按子串）
    whole_word: bool = True
    case_sensitive: bool = False
    #: 一次扫描最多看多少句（0 = 不限）。ST 里叫 Scan Depth：限定扫多少内容，
    #: 而不是"整段任意位置都算"。
    max_slots: int = 0


def _pattern(key: str, policy: TriggerPolicy) -> re.Pattern[str]:
    flags = 0 if policy.case_sensitive else re.IGNORECASE
    if policy.whole_word and not is_cjk(key):
        # 词边界：左右都不能紧挨着单词字符。`\b` 在 `Eve's` / `Eve.` 上是对的，
        # 但在 `[name]Eve` 这类紧贴标点的写法上过严，所以用"两侧不是单词字符"的自定义边界。
        return re.compile(rf"(?<!{_WORD_CHARS}){re.escape(key)}(?!{_WORD_CHARS})", flags)
    return re.compile(re.escape(key), flags)


def key_in_text(text: str, key: str, *, policy: TriggerPolicy | None = None) -> bool:
    policy = policy or TriggerPolicy()
    if not key or not text:
        return False
    return bool(_pattern(key, policy).search(text))


@lru_cache(maxsize=4096)
def pattern_for(key: str, policy: TriggerPolicy | None = None) -> re.Pattern[str]:
    """某个键的匹配模式。需要**逐处**处理命中的调用方用它（如术语标签的包裹与
    逐槽位对账，见 :func:`gametrans.layers.tags.wrap`）——"什么算命中"仍然只有
    这一处定义。编译结果按 ``(键, 规则)`` 缓存：逐句扫描时重复编译会拖慢整轮。
    """
    return _pattern(str(key or ""), policy or TriggerPolicy())


def keys_in_text(
    text: str, keys: Iterable[str], *, policy: TriggerPolicy | None = None
) -> list[str]:
    """这段文本命中了哪几个键（按给定顺序、去重）。"""
    policy = policy or TriggerPolicy()
    hits: list[str] = []
    for key in keys:
        key = str(key or "").strip()
        if key and key not in hits and key_in_text(text, key, policy=policy):
            hits.append(key)
    return hits


@dataclass(frozen=True)
class Hit:
    """一条命中：命中了第几句（从 0 数）、命中了哪几个键。"""

    slot: int
    keys: tuple[str, ...]


def scan_slots(
    slots: Iterable[str],
    keys: Iterable[str],
    *,
    policy: TriggerPolicy | None = None,
) -> list[Hit]:
    """逐句扫，返回**全部**命中（不止第一条）—— 证据要完整，不能只留第一句。

    ``max_slots`` 限定了扫多少句：预算之外的内容不扫，也就不会触发。
    """
    policy = policy or TriggerPolicy()
    wanted = [str(key).strip() for key in keys if str(key or "").strip()]
    if not wanted:
        return []
    hits: list[Hit] = []
    for index, text in enumerate(slots):
        if policy.max_slots and index >= policy.max_slots:
            break
        matched = keys_in_text(str(text or ""), wanted, policy=policy)
        if matched:
            hits.append(Hit(slot=index, keys=tuple(matched)))
    return hits


def hit_counts(
    slots: Iterable[str],
    key_groups: dict[str, Iterable[str]],
    *,
    policy: TriggerPolicy | None = None,
) -> dict[str, int]:
    """每组键**各会在多少句上命中**（一组 = 一条世界书条目）。

    "这条设定会在多大范围里说话"是审核时最要紧的一个数：键命中 2 句和命中 900 句，
    是两种完全不同的东西。键只编译一次，所以整部游戏的槽位扫一遍也不贵。
    """
    policy = policy or TriggerPolicy()
    compiled = {
        name: [
            _pattern(str(key).strip(), policy)
            for key in keys or ()
            if str(key or "").strip()
        ]
        for name, keys in key_groups.items()
    }
    counts = {name: 0 for name in key_groups}
    for text in slots:
        current = str(text or "")
        for name, patterns in compiled.items():
            if patterns and any(pattern.search(current) for pattern in patterns):
                counts[name] += 1
    return counts
