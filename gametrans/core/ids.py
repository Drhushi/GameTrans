"""槽位身份的**归一**：一个身份可能被写成哪几种样子。

真靶实测（登记册 R40）：我们发 `id:door2_a20cefa7`，模型回 `door2_a20cefa7`
（前缀被它规范化掉），有时还会把方括号一起抄回来 `[door2_a20cefa7]`。
于是"这条译文对应哪个槽位"不能用逐字比较 —— 逐字比会把**整批成果判成没拿到**。

规则只有两条，交替做两轮：**去掉内部分类前缀**、**去掉方括号**。

为什么放在内核而不是 provider 层：这张面板要按同一套规则判"哪几条没拿到"
（``core/exchanges.py``），而内核不许反过来 import provider。两处各抄一遍，
下一次改规则就会只改一处 —— 那正是"看一份、跑另一份"的老毛病。
"""

from __future__ import annotations

#: 内部**分类**标记：对模型没有意义，实测会被它规范化掉。
INTERNAL_PREFIXES: tuple[str, ...] = ("id:", "keyed:", "unit:")


def id_forms(unit_id: str, *, prefixes: tuple[str, ...] = INTERNAL_PREFIXES) -> set[str]:
    """一个身份**可能被写成**的所有样子（都要能还原回来）。"""
    forms = {str(unit_id).strip()}
    for _ in range(2):
        forms |= {form.strip("[]").strip() for form in forms}
        for form in list(forms):
            for prefix in prefixes:
                if form.startswith(prefix) and len(form) > len(prefix):
                    forms.add(form[len(prefix) :])
    forms |= {form.strip("[]").strip() for form in forms}
    return {form for form in forms if form}


def alias_index(unit_ids: list[str]) -> tuple[dict[str, str], set[str]]:
    """``可能的写法 → 完整身份``，以及**有歧义**的写法集合。

    两个条目都能对上同一个写法时，猜一个就是静默错位 —— 所以那种写法进
    ``ambiguous``，由调用方拒绝认领（宁可报"没对上"，也不猜）。
    """
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for unit_id in unit_ids:
        for form in id_forms(unit_id):
            if form in index and index[form] != unit_id:
                ambiguous.add(form)
                continue
            index.setdefault(form, unit_id)
    for form in ambiguous:
        index.pop(form, None)
    return index, ambiguous


def resolve_identity(raw: str, index: dict[str, str], ambiguous: set[str]) -> str | None:
    """把模型回填的那个字符串还原成完整身份；对不上或有歧义返回 ``None``。"""
    for form in id_forms(raw):
        if form in ambiguous:
            return None
        if form in index:
            return index[form]
    return None


def within_one_edit(a: str, b: str) -> bool:
    """两个字符串是不是只差**一个**字符（多打 / 少打 / 敲错 / 相邻换位）。

    用途只有一个：模型抄短哈希时多打或少打一个字符（真靶 2026-09-29：`79cfb185`
    被回成 `c79cfb185`）。完全相同**不算**近失 —— 那条路归 :func:`resolve_identity`。
    """
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diff) == 1:
            return True
        return (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and a[diff[1]] == b[diff[0]]
        )
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    skipped = False
    i = j = 0
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1
    return True
