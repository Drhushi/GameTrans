"""结构约束与机器校验器。

原则：**受保护结构靠结构化 Segment + 校验器双重保障，而不是要求模型"记住不要改"**。

这一层是引擎无关的：它只比较"结构化 token 的多重集合"，至于什么算 token（Ren'Py 的
``[name]``、``{w}``，别的引擎的 ``%s``、``<b>``）由适配器提供的 ``scanner`` 决定
（引擎标签、变量、控制码如何识别属于 Adapter 的职责）。

违反约束的译文不会被丢弃，也不会被写回 —— 它变成
:data:`~gametrans.core.models.TranslationStatus.NEEDS_REVIEW`，进报告等人/agent 拍板。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from gametrans.core.models import (
    Constraint,
    ConstraintType,
    ConstraintViolation,
    Scanner,
    Segment,
    SegmentKind,
    TranslationUnit,
    ValidationResult,
)

__all__ = [
    "KNOWN_CONSTRAINTS",
    "TERM_TAG_RE",
    "SegmentProfile",
    "default_constraints",
    "profile_of",
    "slot_gauge",
    "source_target_correspondence",
    "term_tags_in",
    "validate",
]

#: 本内核真正会执行的约束。声明了别的约束不算数 —— 不许假装检查过。
KNOWN_CONSTRAINTS: frozenset[str] = frozenset(c.value for c in ConstraintType)

#: **丢了就是内容缺失**的标签 —— 判定依据：一份 73,239 条的已发布人工成品里，
#: 这两类（超链接 `{a=…}`、嵌图 `{image=…}`）一次都没被丢掉，所以按"硬性"处理不误伤。
STRUCTURAL_TAGS = frozenset({"a"})
#: 扫描器把 `{image=…}` 归到控制码（它确实控制显示），所以结构控制码单独列一条。
STRUCTURAL_CONTROLS = frozenset({"image"})
#: 字面量括号的转义：丢了，引擎就会去解析那个括号。
STRUCTURAL_ESCAPES = frozenset({"[[", "{{"})

#: **术语标签**的形状：``⟦原文写法⟧``。这里是它的唯一一处定义 —— 生成
#: （:mod:`gametrans.layers.tags`）、对账（本模块）、渲染（写回层）三边认同一个正则。
#: 标签里放的就是术语书那一行的 ``source``（原文写法），所以不需要第二张映射表：
#: 术语书本身就是那张表，换译名只需重新渲染，不必重翻。
TERM_TAG_RE = re.compile(r"⟦([^⟦⟧\n]{1,80})⟧")

#: 「原文 → 译文对应」的两个阈值。**只有明显不成比例才算**：真靶全量 15,755 条上
#: 逐条量过（复算脚本 `.gametrans/correspondence_check.py`），正常句子的原文:译文比
#: 落在 **0.17–1.0**，阈值取 0.12 —— 抓到那几条错配的同时不误伤"成语式短译"
#: （`Took the words right out of my mouth.` → `正合我意。` 是 0.14）。
CORRESPONDENCE_MIN_SOURCE = 24
CORRESPONDENCE_MIN_RATIO = 0.12

#: 标点与符号：它们不携带内容，长度判据不应该被它们撑起来。
_PUNCTUATION_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

#: 受保护标记的**通用形状**（`{image=…}` / `[name]`）—— 只在调用方**没给引擎切分**时
#: 用来兜底（读数脚本、评分尺子那种"手上只有两串文本"的场合）。
_MARKUP_SHAPE = re.compile(r"\{[^{}\n]*\}|\[[^\[\]\n]*\]")


def _readable(text: str) -> str:
    """去掉标点/空白/符号之后的可读部分 —— 判"这句话有没有可读内容"就看它。"""
    return _PUNCTUATION_RE.sub("", str(text or ""))


#: 去掉受保护标记与标点之后，一条译文至少要剩这么多字符；否则原文再长也算"没对上"。
#: 为什么标点要剔掉：`？` / `！` / `……` 这类"译文"结构上完全合法（占位符一个不少），
#: 却显然不是那句原文的译文 —— 真靶上抓到的那几条正是这个形状。
CORRESPONDENCE_MIN_TARGET_CHARS = 4
#: 译文有内容、而原文一个字都没有时，译文要到多长才算"挂错了"。
#: 阈值取 8 是为了放过空原文的转场/图片槽位（`{image=…}` 这类本来就是空原文）。
CORRESPONDENCE_TARGET_WITHOUT_SOURCE = 8


def _target_readable(target: Any, segments: Sequence[Segment] | None) -> str:
    """译文的可读部分：从渲染好的 ``target`` 里剔掉**不可译段**（占位符与标记）。

    为什么不能直接对 ``target`` 数长度：``{w=0.5}`` / ``[name]`` 这些结构标记
    （以及整条就是 ``{a=…}{image=…}`` 的那种"译文"）算进去会把长度撑起来，
    真正的"空译文"就被放过去了。
    """
    text = str(target or "")
    for segment in segments or ():
        if segment.translatable:
            continue
        piece = str(segment.value)
        if piece:
            text = text.replace(piece, " ")
    return _readable(text)


def _strip_markers(text: str, segments: Sequence[Segment] | None) -> str:
    """把**受保护标记整段剔掉**，只留可译文本。

    为什么必须有它：`_readable` 只去标点，`{image=selfie/selfie5.png}` 里的
    ``image selfie selfie5 png`` 会被它当成"可读内容"（实测数出 49 个字符）——
    于是整条就是一张图片的槽位，会被判成"原文有可读内容、译文却没有"。
    真靶实测：18 条写回拒绝里 13 条是这么来的。

    ``segments`` 是引擎给的切分（首选）。**没给时退回通用形状**（`{…}` / `[…]`）——
    否则同一处判据在"有切分"和"只有文本"两种调用下给出两个答案，而后者（读数脚本、
    评分尺子）恰恰最需要它稳定。
    """
    stripped = str(text or "")
    for segment in segments or ():
        if segment.translatable:
            continue
        piece = str(segment.value)
        if piece:
            stripped = stripped.replace(piece, " ")
    if segments is None:
        stripped = _MARKUP_SHAPE.sub(" ", stripped)
    return stripped


def _target_readable(target: Any, segments: Sequence[Segment] | None) -> str:
    """译文的可读部分：从渲染好的 ``target`` 里剔掉**不可译段**（占位符与标记）。

    为什么不能直接对 ``target`` 数长度：``{w=0.5}`` / ``[name]`` 这些结构标记
    （以及整条就是 ``{a=…}{image=…}`` 的那种"译文"）算进去会把长度撑起来，
    真正的"空译文"就被放过去了。
    """
    return _readable(_strip_markers(target, segments))


def source_target_correspondence(
    source_text: str,
    target: str,
    *,
    segments: Sequence[Segment] | None = None,
    source_segments: Sequence[Segment] | None = None,
) -> dict[str, Any] | None:
    """这条译文**看着像不像**这条原文的译文；不像就返回一份账，像就返回 ``None``。

    只做**形状**上的合理性检查，判不了语义 —— 判据三条（都是"明显不称"才算）：

    * 原文有可读内容、译文去掉标记与标点后短得不成比例（真靶上抓到的正是
      `Seriously, could you cut that out? ...` → `？` 这一条）；
    * 译文**去掉标点后什么都不剩** —— 那不是一个句子；
    * 原文一个字都没有，译文却写了一整段（挂错原文的另一种形态）。

    ⚠️ **标尺要和译文同一把**：两边都先剔掉受保护标记再数可读内容。
    只对译文剔、对原文不剔，标记的**内容**会被算成"原文有可读内容" ——
    `{a=image:selfie/selfie5.png}{image=selfie/selfie5small.png}{/a}` 那种纯图片槽位
    会被数成 49 个字符，于是"译文（=原文）没有可读文本"被判成挂错。

    ⚠️ 刻意**不查数字**（试过又撤掉）：真靶上 `6 foot 3` → `一米九`、
    `190 centimeters` → `一米九`、`Guy1` → `路人甲` 都是正常本地化与改写，
    查数字只制造噪声；那几条真错配已经被长度与"同一单元内重复"两条覆盖了。

    这一格是**提醒不是断言**：命中 = 这条不写回、进待复核并点名，由人/agent 看一眼。
    "模型另起了名字""这句本来就不必出现"这类判断不在这一层做。
    """
    # 原文**整条都是受保护标记**（`{a=…}{image=…}{/a}` / `[变量]`，没有可译文本）：
    # 它要译的东西是"没有文字"，译文与它一样就是对的 → 放行。
    # 判据用引擎切分：那一段的 `translatable` 是假的才算"没有文字"。
    # 真靶实测：18 条写回拒绝里 13 条是这种纯图片槽位。
    # ⚠️ 与 `source_empty` 那条不冲突：那条管的是**真空串**的原文配一整段译文。
    if source_segments and not any(segment.translatable for segment in source_segments):
        # 但**对称**这一格还在：原文没有文字，译文也不该冒出文字来。
        target_readable = _target_readable(target, segments)
        if len(target_readable) < CORRESPONDENCE_TARGET_WITHOUT_SOURCE:
            return None
        return {
            "reason": "source_has_no_text",
            "source_chars": 0,
            "target_chars": len(target_readable),
            "target_length": len(str(target or "")),
            "threshold": CORRESPONDENCE_TARGET_WITHOUT_SOURCE,
        }
    source_all = _strip_markers(source_text, source_segments)
    source = _readable(source_all)
    target_readable = _target_readable(target, segments)
    detail: dict[str, Any] = {
        "source_chars": len(source),
        "target_chars": len(target_readable),
        "target_length": len(str(target or "")),
    }
    if not source:
        if len(target_readable) >= CORRESPONDENCE_TARGET_WITHOUT_SOURCE:
            detail["reason"] = "source_empty"
            detail["threshold"] = CORRESPONDENCE_TARGET_WITHOUT_SOURCE
            return detail
        return None
    if len(source) < CORRESPONDENCE_MIN_SOURCE:
        return None
    ratio = len(target_readable) / len(source)
    detail["ratio"] = round(ratio, 3)
    detail["min_ratio"] = CORRESPONDENCE_MIN_RATIO
    if len(target_readable) < CORRESPONDENCE_MIN_TARGET_CHARS:
        detail["reason"] = "target_has_no_readable_text"
        return detail
    if ratio < CORRESPONDENCE_MIN_RATIO:
        detail["reason"] = "target_far_too_short"
        return detail
    return None


def _correspondence_message(unit_id: str, detail: dict[str, Any], source_text: str) -> str:
    reason = str(detail.get("reason") or "")
    if reason in ("source_empty", "source_has_no_text"):
        return (
            f"{unit_id} 的原文是空的，译文却写了 {detail.get('target_chars')} 个字符 —— "
            "这一条译文不是这段原文的译文（多半挂错了）"
        )
    if reason == "target_has_no_readable_text":
        return (
            f"{unit_id} 的译文里没有可读文本（去掉标记与标点后什么都不剩）—— "
            f"原文是「{str(source_text).strip()[:60]}」"
        )
    return (
        f"{unit_id} 的译文短得不成比例：原文 {detail.get('source_chars')} 字，"
        f"译文只有 {detail.get('target_chars')} 字（比 {detail.get('ratio')}，"
        f"下限 {detail.get('min_ratio')}）—— 这一条译文很可能不是这段原文的译文"
    )


def term_tags_in(text: str) -> list[str]:
    """一段文字里的术语标签（按出现顺序、含重复）—— 对账看的是**多重集合**。

    重复也要数：同一句里同一个写法出现两次，就该有两个标签。
    """
    return [match.group(1).strip() for match in TERM_TAG_RE.finditer(str(text or ""))]


def _name_of(value: str) -> str:
    """受保护标记的**身份名**：``{b}`` / ``{/b}`` / ``{cps=5.7}`` / ``[mc!t]`` / ``{w=0.3}``。

    比对口径从"逐字相等"改成"身份相等"：写法的差异（标志、数值、字体名）是本地化的
    一部分，身份的差异才是"游戏里会显示错"。
    """
    body = value.strip()
    if body.startswith("[") and body.endswith("]"):
        return body[1:-1].split("!", 1)[0].strip()
    if body.startswith("{") and body.endswith("}"):
        return body[1:-1].strip().lstrip("/").split("=", 1)[0].strip()
    return body


def _by_name(segments: Sequence[Segment], kind: SegmentKind) -> Counter[str]:
    """某一类受保护标记按**身份名**计数。"""
    counted: Counter[str] = Counter()
    for segment in segments:
        if SegmentKind.from_token(segment.kind) is kind:
            counted[_name_of(segment.value)] += 1
    return counted


def _structural_only(counted: Counter[str], structural: frozenset[str]) -> Counter[str]:
    """只留下"丢了就是内容缺失"的那几个名字，其余的身份差异不算硬性。"""
    return Counter({name: count for name, count in counted.items() if name in structural})



@dataclass
class SegmentProfile:
    """一组 Segment 的可比指纹。只记"结构 token 出现过几次"，不记它们在哪。"""

    variables: Counter[str] = field(default_factory=Counter)
    controls: Counter[str] = field(default_factory=Counter)
    tags: Counter[str] = field(default_factory=Counter)
    #: 换行之外的转义序列（``[[``、``{{``、反斜杠……）
    escapes: Counter[str] = field(default_factory=Counter)
    newlines: int = 0
    text_length: int = 0

    @property
    def has_structure(self) -> bool:
        return bool(self.variables or self.controls or self.tags or self.escapes or self.newlines)

    def describe(self) -> dict[str, Any]:
        return {
            "variables": dict(self.variables),
            "controls": dict(self.controls),
            "tags": dict(self.tags),
            "escapes": dict(self.escapes),
            "newlines": self.newlines,
            "text_length": self.text_length,
        }


def profile_of(segments: Sequence[Segment]) -> SegmentProfile:
    """把 Segment 序列压成可比指纹。

    可译文本**不计入**比较 —— 那是翻译要改的东西；受保护结构必须一模一样。
    """
    profile = SegmentProfile()
    for segment in segments:
        kind = SegmentKind.from_token(segment.kind)
        if kind is SegmentKind.VARIABLE:
            profile.variables[segment.value] += 1
        elif kind is SegmentKind.CONTROL:
            profile.controls[segment.value] += 1
        elif kind is SegmentKind.TAG:
            profile.tags[segment.value] += 1
        elif kind is SegmentKind.ESCAPE:
            # 换行单独算（"必须保留的换行"与"转义序列"是两条不同的约束）
            if segment.value == "\n":
                profile.newlines += 1
            else:
                profile.escapes[segment.value] += 1
        if segment.translatable:
            profile.text_length += len(segment.value.strip())
    return profile


def default_constraints(unit: TranslationUnit) -> list[Constraint]:
    """按 Unit **实际含有**的结构挑约束 —— 没有变量就不声称检查了变量。"""
    profile = profile_of(unit.segments)
    constraints = [
        Constraint(ConstraintType.OUTPUT_SHAPE_VALID.value, target=unit.id),
        # **每个单元都申报这条**：它与"结构有没有坏"无关，是"内容有没有挂错"那一格。
        Constraint(ConstraintType.SOURCE_TARGET_CORRESPONDS.value, target=unit.id),
    ]
    if profile.variables:
        constraints.append(
            Constraint(ConstraintType.PLACEHOLDER_COUNT_PRESERVED.value, target=unit.id)
        )
    if profile.tags:
        constraints.append(
            Constraint(ConstraintType.TAG_BALANCE_PRESERVED.value, target=unit.id)
        )
    if profile.controls:
        constraints.append(
            Constraint(ConstraintType.CONTROL_CODE_PRESERVED.value, target=unit.id)
        )
    if profile.newlines:
        constraints.append(
            Constraint(ConstraintType.REQUIRED_NEWLINES_PRESERVED.value, target=unit.id)
        )
    if profile.escapes:
        constraints.append(
            Constraint(ConstraintType.ESCAPE_SEQUENCE_PRESERVED.value, target=unit.id)
        )
    return constraints


def _diff_violation(
    constraint_type: str,
    unit: TranslationUnit,
    message: str,
    detail: dict[str, Any],
    *,
    severity: str = "error",
) -> ConstraintViolation:
    return ConstraintViolation(
        constraint_type=constraint_type,
        target=unit.id,
        severity=severity,
        message=message,
        detail=detail,
    )


def _missing(
    constraint_type: str,
    unit: TranslationUnit,
    expected: Counter[str],
    actual: Counter[str],
    label: str,
) -> ConstraintViolation | None:
    if expected == actual:
        return None
    lost = sorted((expected - actual).elements())
    gained = sorted((actual - expected).elements())
    return _diff_violation(
        constraint_type,
        unit,
        f"{unit.id} 的{label}被改动：丢失 {lost or '无'}，多出 {gained or '无'}",
        {"lost": lost, "gained": gained, "expected": dict(expected), "actual": dict(actual)},
    )


def _identity_change(
    constraint_type: str,
    unit: TranslationUnit,
    expected: Counter[str],
    actual: Counter[str],
    label: str,
    *,
    show: Any = None,
) -> ConstraintViolation | None:
    """**身份**层面的差异 —— 这才是"游戏里会显示错"，一律硬性拦下。

    ``show`` 把身份名还原成 token 形状（``name`` → ``[name]``）：重试反馈要告诉模型
    **原样保留哪个 token**，只说名字它不知道要写方括号还是花括号。
    """
    if expected == actual:
        return None
    render = show or (lambda name: name)
    lost = sorted(render(name) for name in (expected - actual).elements())
    gained = sorted(render(name) for name in (actual - expected).elements())
    return _diff_violation(
        constraint_type,
        unit,
        f"{unit.id} 的{label}身份被改动：丢失 {lost or '无'}，多出 {gained or '无'}",
        {"lost": lost, "gained": gained, "expected": dict(expected), "actual": dict(actual)},
    )


def _rewrite_notice(
    constraint_type: str,
    unit: TranslationUnit,
    expected: Counter[str],
    actual: Counter[str],
    label: str,
) -> ConstraintViolation | None:
    """**写法**层面的差异 —— 放行，但逐条留痕（"改了什么"必须看得见）。"""
    if expected == actual:
        return None
    lost = sorted((expected - actual).elements())
    gained = sorted((actual - expected).elements())
    return _diff_violation(
        constraint_type,
        unit,
        f"{unit.id} 的{label}写法被调整（身份没变）：丢失 {lost or '无'}，多出 {gained or '无'}",
        {"lost": lost, "gained": gained, "expected": dict(expected), "actual": dict(actual)},
        severity="warning",
    )



def slot_gauge(unit: TranslationUnit, key: str, scanner: Scanner | None = None) -> TranslationUnit:
    """逐句落盘时，闸门要拿**这条槽位自己**的原文当标尺。

    记录是按**槽位**记账的（一个单元含 N 句就有 N 条译文）；拿整个单元（几百句拼起来）
    当标尺去比一句译文，结构必然对不上 —— 每一句都会"丢了"别的句子的占位符，
    于是**整段全被判待复核**。真靶实测（该工程 `act25`，1,963 条）：请求与响应
    逐条比对是**零处**占位符不一致，软件却报了 1,963 条违规；改成按槽位判之后
    误判清零，真丢占位符的句子照样拦下。

    槽位原文取自单元定位载荷里的 ``slots``（适配层写的）；**取不到就退回整个单元**
    —— 老数据上没有逐句原文，退回是唯一不编造的做法。
    """
    for entry in (unit.locator.payload or {}).get("slots") or []:
        if str(entry.get("slot_key") or "") != key:
            continue
        source = str(entry.get("source") or "")
        if source:
            return TranslationUnit.from_text(
                id=key, type=unit.type, source=source, scanner=scanner
            )
    return unit


def validate(
    unit: TranslationUnit,
    target: str,
    *,
    scanner: Scanner | None = None,
    constraints: Iterable[Constraint] | None = None,
    approved: Iterable[str] = (),
    term_tags: Sequence[str] | None = None,
    source_text: str | None = None,
) -> ValidationResult:
    """校验一段译文有没有破坏 ``unit`` 的结构约束。

    ``scanner`` 由适配器提供：它把**译文**按同一套语法重新切分，核心只做比对。
    没有 ``scanner`` 时只能检查"输出形状"，其余约束不会进入 ``checked`` ——
    检查不了的事绝不假装通过。

    ``approved`` 是这条译文**被声明批准**的偏离种类（见
    :mod:`gametrans.layers.deviations`）：``empty`` 让它空着是允许的，
    ``expression`` 允许改写占位符身份。**没声明的一律照旧拦下** —— 否则
    "手滑丢了占位符"就能靠沉默混过去。放行照样留痕（写成 warning）。

    **比对标尺由 ``unit`` 决定**：逐句落盘时要传**这一句自己的**单元视图
    （见 :func:`slot_gauge`），不能拿整个单元 —— 拿整个单元去比一句译文，
    每一句都会"丢了"别的句子的占位符（真靶实测：`act25` 1,963 条全被误判）。

    ``term_tags`` 给**这一条原文该出现的术语标签**（``⟦写法⟧`` 里的写法列表，
    见 :mod:`gametrans.layers.tags`）。给了就当场对账：丢了标签 = 模型自己起了个名字，
    多出 / 改名 = 不知道从哪冒出来的标签 —— 两种都判违规、都不写回。不给就不检查
    （翻译记忆复用来的译文本来就带标签，那是对的，不该被它拦下）。

    ``source_text`` 给**这一条自己的原文**：逐槽位落盘时标尺是这一句的原文，
    不是整个单元的（见 :func:`slot_gauge`）。不给就用 ``unit.source``。
    "原文 → 译文对应"那一条按它比 —— 拿整单元的原文去比一句译文，长度必然不成比例。
    """
    allowed = {str(item) for item in (approved or ())}
    declared = list(constraints) if constraints is not None else default_constraints(unit)
    source_profile = profile_of(unit.segments)
    target_segments: list[Segment] | None = (
        scanner(target) if scanner is not None else None
    )
    target_profile = profile_of(target_segments) if target_segments is not None else None

    violations: list[ConstraintViolation] = []
    checked: list[str] = []

    for constraint in declared:
        kind = constraint.constraint_type
        if kind not in KNOWN_CONSTRAINTS:
            # 适配器声明了内核不认识的约束：不假装检查过
            continue
        if kind == ConstraintType.SOURCE_TARGET_CORRESPONDS.value:
            # 这一条**不依赖 scanner 切分**：它比的是"可读文本的长短"，不是结构 token。
            # 但**有 scanner 时才检查** —— 没有切分器就拆不出"哪些是标记、哪些是文本"，
            # 硬跑会把结构标记算进可读长度（`{image=…}` 那种槽位会被误判），
            # 而误判比不查更糟：同一处判据在两张环境下给出两个答案。
            if target_profile is None:
                continue
            checked.append(kind)
            source_for_check = unit.source if source_text is None else source_text
            # 标尺那一侧也要按**它自己的切分**剔标记 —— 用同一把 scanner 切一次原文。
            # 逐槽位校验时 `unit` 已经就是这一句（`slot_gauge` 造的），切出来就是它的分段。
            source_pieces = (
                unit.segments
                if source_text is None or source_text == unit.source
                else scanner(str(source_for_check))
                if scanner is not None
                else None
            )
            detail = source_target_correspondence(
                str(source_for_check or ""),
                target,
                segments=target_segments,
                source_segments=source_pieces,
            )
            if detail is not None:
                # **声明过的偏离照样放行**（与另外几条同一个口径）：`empty` 说"这里
                # 就是空的"，那"译文里没有可读文本"是同一件事的另一种说法，不该再拦一次；
                # 别的声明只降级成 warning —— 留痕，不静默。
                declared_empty = "empty" in allowed and not str(target).strip()
                violations.append(
                    _diff_violation(
                        kind,
                        unit,
                        _correspondence_message(unit.id, detail, str(source_for_check or "")),
                        {**detail, "approved": sorted(allowed)} if allowed else detail,
                        severity="warning" if declared_empty else "error",
                    )
                )
            continue
        if kind != ConstraintType.OUTPUT_SHAPE_VALID.value and target_profile is None:
            continue
        checked.append(kind)

        if kind == ConstraintType.OUTPUT_SHAPE_VALID.value:
            if not target.strip():
                if "empty" in allowed:
                    # 有意留空：声明过就放行，但留痕 —— 报告上看得见"这里是空的，为什么"
                    violations.append(
                        _diff_violation(
                            kind,
                            unit,
                            f"{unit.id} 的译文为空（已声明为有意留空）",
                            {"target_length": 0, "approved": sorted(allowed)},
                            severity="warning",
                        )
                    )
                else:
                    violations.append(
                        _diff_violation(
                            kind, unit, f"{unit.id} 的译文为空", {"target_length": len(target)}
                        )
                    )
            elif (
                target_profile is not None
                and source_profile.text_length > 0
                and target_profile.text_length == 0
            ):
                violations.append(
                    _diff_violation(
                        kind,
                        unit,
                        f"{unit.id} 的译文只剩结构标记，没有可读文本",
                        target_profile.describe(),
                    )
                )
            continue

        assert target_profile is not None  # 上面已经挡掉没有 scanner 的情况
        if kind == ConstraintType.PLACEHOLDER_COUNT_PRESERVED.value:
            # 身份丢了（`[namelong]` 整个不见）→ 硬性；只是写法变了（`[mc]` → `[mc!t]`）→ 放行留痕
            identity = _identity_change(
                kind,
                unit,
                _by_name(unit.segments, SegmentKind.VARIABLE),
                _by_name(target_segments or [], SegmentKind.VARIABLE),
                "变量/占位符",
                show=lambda name: f"[{name}]",
            )
            if identity is not None and "expression" in allowed:
                # 经批准的表达式改写（例如把 [n] 换成 [helper(n)] 把运行时数字转成中文）
                identity = _diff_violation(
                    kind,
                    unit,
                    f"{unit.id} 的变量/占位符被**批准的表达式改写**改动："
                    f"丢失 {identity.detail.get('lost') or '无'}，多出 {identity.detail.get('gained') or '无'}",
                    {**identity.detail, "approved": sorted(allowed)},
                    severity="warning",
                )
            violation = identity or _rewrite_notice(
                kind, unit, source_profile.variables, target_profile.variables, "变量/占位符"
            )
        elif kind == ConstraintType.TAG_BALANCE_PRESERVED.value:
            # 超链接这类结构标签丢了 = 内容缺失 → 硬性；其余标记的增删改是本地化的活
            violation = _identity_change(
                kind,
                unit,
                _structural_only(_by_name(unit.segments, SegmentKind.TAG), STRUCTURAL_TAGS),
                _structural_only(_by_name(target_segments or [], SegmentKind.TAG), STRUCTURAL_TAGS),
                "结构标签",
                show=lambda name: "{" + name + "}",
            ) or _rewrite_notice(kind, unit, source_profile.tags, target_profile.tags, "标签")
        elif kind == ConstraintType.CONTROL_CODE_PRESERVED.value:
            # `{image=…}` 是嵌图（硬性）；`{w=0.3}` → `{w=0.25}` 是按中文长度调停顿（放行）
            violation = _identity_change(
                kind,
                unit,
                _structural_only(_by_name(unit.segments, SegmentKind.CONTROL), STRUCTURAL_CONTROLS),
                _structural_only(
                    _by_name(target_segments or [], SegmentKind.CONTROL), STRUCTURAL_CONTROLS
                ),
                "结构控制码",
                show=lambda name: "{" + name + "}",
            ) or _rewrite_notice(kind, unit, source_profile.controls, target_profile.controls, "控制码")
        elif kind == ConstraintType.ESCAPE_SEQUENCE_PRESERVED.value:
            # `[[` / `{{` 丢了引擎会去解析那个括号（硬性）；`\"` → `“ ”` 是引号本地化（放行）
            violation = _identity_change(
                kind,
                unit,
                _structural_only(_by_name(unit.segments, SegmentKind.ESCAPE), STRUCTURAL_ESCAPES),
                _structural_only(
                    _by_name(target_segments or [], SegmentKind.ESCAPE), STRUCTURAL_ESCAPES
                ),
                "字面量转义",
            ) or _rewrite_notice(kind, unit, source_profile.escapes, target_profile.escapes, "转义序列")
        elif kind == ConstraintType.REQUIRED_NEWLINES_PRESERVED.value:
            violation = (
                None
                if source_profile.newlines == target_profile.newlines
                else _diff_violation(
                    kind,
                    unit,
                    f"{unit.id} 的必需换行数变了："
                    f"{source_profile.newlines} → {target_profile.newlines}",
                    {
                        "expected": source_profile.newlines,
                        "actual": target_profile.newlines,
                    },
                )
            )
        else:  # pragma: no cover - KNOWN_CONSTRAINTS 已经穷举
            violation = None
        if violation is not None:
            violations.append(violation)

    if term_tags is not None:
        # 术语标签对账：**不走 scanner**（它不是引擎语法，是内核自己包的），
        # 因此没有结构切分器的引擎上照样能验 —— 那一格是"检查不了就不假装"的例外，
        # 因为这里的原始文本自己就够判。
        checked.append(ConstraintType.TERM_TAG_PRESERVED.value)
        violation = _missing(
            ConstraintType.TERM_TAG_PRESERVED.value,
            unit,
            Counter(str(writing).strip() for writing in term_tags),
            Counter(term_tags_in(target)),
            "术语标签（⟦写法⟧）",
        )
        if violation is not None:
            violations.append(violation)

    return ValidationResult(
        ok=not any(v.severity == "error" for v in violations),
        violations=violations,
        checked=checked,
    )
