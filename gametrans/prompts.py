"""请求模板：一次翻译请求长什么样，**只有这一个地方说了算**。

**为什么要独立成模块**（这是复盘一次工程事故后收的口径）：此前"每条两行"与
"每条一行"是两份各自抄了一遍的渲染器（``providers/base.py`` 的 ``_render_full`` /
``_render_compact``），system 提示词也有两份字面量（``providers/openai_compat.py`` 的
``SYSTEM_PROMPT`` / ``SYSTEM_PROMPT_COMPACT``）。后果是：**改了一份、跑的是另一份**，
而看请求的工具（``scripts/dump_translate_request.py --annotation``）还能挑着渲染 ——
给用户看的是一份，工程实际用的是另一份。

收法：两种口径只是**同一份模板的两个预设值**，渲染只有一个函数。生产、拦截落盘、
面板预览全部走 :func:`resolve` + :func:`render_body`，没有第二条路可走。

模板是数据（dict），所以人可以在面板里看、改、存、切；agent 走同一组配置键。
改了模板必须**能生效**：它是配置项，随 :class:`~gametrans.config.ProjectConfig` 落盘。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from gametrans.core.ids import INTERNAL_PREFIXES, id_forms

#: 默认使用的模板名。``compact`` 与 ``full`` 的内容完全相同，只是不发位置、不发
#: 「现已定为」行、编号用单元内四位短号 —— 真靶同一单元实测总 token 少 35%，见
#: ``tests/test_prompt_template.py`` 与 STATUS.md 的登记。
DEFAULT_TEMPLATE_NAME = "compact"

# --------------------------------------------------------------------------- #
# 预设模板
# --------------------------------------------------------------------------- #

#: 出厂 system 提示词（``full`` 口径）。改口径请改**这里**，不要在别处再抄一份。
SYSTEM_FULL = (
    "你是一名专业的游戏本地化译者，负责把游戏内文本翻译成目标语言。\n"
    "要求：\n"
    "1. 保持角色语气、称呼习惯与专名一致性。【术语书】是**已经定过的东西**，两种行："
    "`- 写法 → 译名` 是那个写法的**叫法**；`- 设定｜写法 / 写法：…` 是**那一行那个实体**"
    "的**设定**（左边列的写法都属于同一个实体，这些设定对它们都成立）。两者都当**参考**用："
    "优先沿用；但它们不是死命令 —— 明显读不通、或与当前语境冲突时按语境来"
    "（全书一致比逐句服从更要紧，改了就在 note 里说一句）。\n"
    "2. **原文里的文字要尽可能全部译成目标语言**：人名、地名、组织名、称号、招牌，"
    "以及台词里夹着的外语词，都要给出目标语言的译法（音译或意译）；**不要照抄原文**，"
    "也不要留下没译的原文。实在不宜译的缩写或品牌写法可以保留原文，但要**在第一处就定下来**、"
    "全篇一致，并把它列进 terms —— 同一处一会儿译、一会儿不译是不合格的。"
    "只有第 3 条说的受保护 token，以及原文里本来就不是给人看的文字"
    "（变量名、函数名、文件名这类代码标识符）保持原样。\n"
    "3. 原文里的格式标记、占位符、控制码与转义序列必须原样保留。\n"
    "   每条待译内容会列出它自己的受保护 token；不要翻译、不要增删它们。\n"
    "4. 每条待译内容的第一行是**标注**（方括号里是条目 id，可能还有说话人），"
    "它下面的那一行才是要翻译的正文；只翻译正文，不要把标注写进译文。\n"
    "5. 只输出 JSON，不要任何解释文字。输出结构必须是：\n"
    '   {"translations": [{"unit_id": "<原样回填>", "target": "<译文>"}],\n'
    '    "terms": [{"source": "<原文里的写法>", "target": "<你用的译名>",\n'
    '               "profile": "<这个词背后的设定，写不出就省掉这一栏>"}]}\n'
    "6. unit_id 必须与输入完全一致，条目数量必须与输入相同，顺序保持一致。\n"
    "7. terms 里**只列需要统一、后面还会再遇到**的专名：人名、地名、组织、作品或队伍名、"
    "称号这类。**不要列普通名词与日常词** —— 职业称谓（coach / dean）、食物饮料与菜单项"
    "（Capuccino / Latte）、界面选项、日常物品：它们按语境当场译就行，写进来只会把书塞满。"
    "**【术语书】里已经给出译名的写法不要再列进 terms** —— 它已经统一过了，重复申报只会多"
    "一条没人要的复核；terms 只用来报书上**还没有的新专名**，以及那一行**译名还空着**的写法"
    "（原文里写作 ⟦…⟧ 的那种）。"
    "source 照抄原文里的写法，target 照你写出来的译名抄；没有就填空数组。\n"
    "8. 每一条可以再带一栏 profile（**写不出就省掉**）：这个词背后的**稳定设定**，"
    "用**译文语言**写一句话 —— 它指谁、和谁什么关系、是哪个地方或组织、有什么一贯的规则。"
    "**提到别的实体时用它已经定下的译名**，不要写原文写法 —— 那一栏左边已经是原文写法了，"
    "再写一遍读起来是同义反复。"
    "**【术语书】里已经写过的事实不要再写一遍** —— 换个说法、调个语序、加个修饰都算同一件事；"
    "profile 只用来补书上**还没有**的稳定设定，说不准是不是新的就别写。"
    "**不要复述这一段里发生了什么**（事件只属于这一段，不是跨段设定），不要写猜测，"
    "也不要把这一段的主角当成整个世界的主角。\n"
    "   反例（这些都是**不该写**的）：“两人一起进城游玩、荡过秋千、玩过水”、"
    "“他当时很生气”、“这段文本用占位符表示主角名字”。\n"
    "9. source 必须是**原文的语言**里真的出现过的写法（通常就是那个名字在原文里的拼法）；"
    "写成译文语言的话，profile 这一栏永远触发不了。**占位符**（方括号或花括号里包着"
    "变量名的那种）也不要当 source —— 它会在所有提到这个变量的句子上触发。"
)

#: 紧凑口径的 system 提示词：与 ``full`` 只差第 4、6 条（怎么读标注、id 怎么回填）。
#: 第 4 条必须与**正文的实际形状**对上：曾经写成"每条只有一行"，而正文是两行 ——
#: 模型于是把标注行里的 ``说话人：`` 当成正文抄进译文（真靶 91/558 条）。
SYSTEM_COMPACT = (
    "你是一名专业的游戏本地化译者，负责把游戏内文本翻译成目标语言。\n"
    "要求：\n"
    "1. 保持角色语气、称呼习惯与专名一致性。【术语书】是**已经定过的东西**，两种行："
    "`- 写法 → 译名` 是那个写法的**叫法**；`- 设定｜写法 / 写法：…` 是**那一行那个实体**"
    "的**设定**（左边列的写法都属于同一个实体，这些设定对它们都成立）。两者都当**参考**用："
    "优先沿用；但它们不是死命令 —— 明显读不通、或与当前语境冲突时按语境来"
    "（全书一致比逐句服从更要紧，改了就在 note 里说一句）。\n"
    "2. **原文里的文字要尽可能全部译成目标语言**：人名、地名、组织名、称号、招牌，"
    "以及台词里夹着的外语词，都要给出目标语言的译法（音译或意译）；**不要照抄原文**，"
    "也不要留下没译的原文。实在不宜译的缩写或品牌写法可以保留原文，但要**在第一处就定下来**、"
    "全篇一致，并把它列进 terms —— 同一处一会儿译、一会儿不译是不合格的。"
    "只有第 3 条说的受保护 token，以及原文里本来就不是给人看的文字"
    "（变量名、函数名、文件名这类代码标识符）保持原样。\n"
    "3. 原文里的格式标记、占位符、控制码与转义序列必须原样保留：**原文里那些被花括号"
    "或方括号包起来、明显不是人话的写法**（变量、标记、控制码），在译文里一律照原样"
    "写回去 —— 一个字符都不改、不增、不删、不换位置。\n"
    "4. 【待译内容】里每条是**两行**：第一行是标注 `- [编号] (第 i/n 句) 说话人：`"
    "（方括号里的编号是**回填用的 id**，圆括号里的句序与结尾的 `名字：` 都只是标注），"
    "下面那一行才是要翻译的正文；"
    "**不要把标注写进译文**。标了〖已定译〗的条目分两种：**没有编号**的只有一行正文，"
    "那一行就是**已经定稿的译文**，是给你读上下文用的，不必回填；**带编号**的照常回填，"
    "原文在正文行，已经定下来的译法写在「现已定为」那一行 —— 只有它明显读不通、"
    "或与当前术语冲突时才改，并在 note 里说明理由。\n"
    "5. 只输出 JSON，不要任何解释文字。输出结构必须是：\n"
    '   {"translations": [{"unit_id": "<原样回填>", "target": "<译文>"}],\n'
    '    "terms": [{"source": "<原文里的写法>", "target": "<你用的译名>",\n'
    '               "profile": "<这个词背后的设定，写不出就省掉这一栏>"}]}\n'
    "6. unit_id 必须与输入里**每条第一行开头那个方括号**里的编号**逐字一致**"
    "（一行里只有那一个方括号；圆括号里的句序不是 id，不要拿它当编号），"
    "条目数量必须与输入里**带编号的条目**相同，顺序保持一致。\n"
    "7. terms 里**只列需要统一、后面还会再遇到**的专名：人名、地名、组织、作品或队伍名、"
    "称号这类。**不要列普通名词与日常词** —— 职业称谓（coach / dean）、食物饮料与菜单项"
    "（Capuccino / Latte）、界面选项、日常物品：它们按语境当场译就行，写进来只会把书塞满。"
    "**【术语书】里已经给出译名的写法不要再列进 terms** —— 它已经统一过了，重复申报只会多"
    "一条没人要的复核；terms 只用来报书上**还没有的新专名**，以及那一行**译名还空着**的写法"
    "（原文里写作 ⟦…⟧ 的那种）。"
    "source 照抄原文里的写法，target 照你写出来的译名抄；没有就填空数组。\n"
    "8. 每一条可以再带一栏 profile（**写不出就省掉**）：这个词背后的**稳定设定**，"
    "用**译文语言**写一句话 —— 它指谁、和谁什么关系、是哪个地方或组织、有什么一贯的规则。"
    "**提到别的实体时用它已经定下的译名**，不要写原文写法 —— 那一栏左边已经是原文写法了，"
    "再写一遍读起来是同义反复。"
    "**【术语书】里已经写过的事实不要再写一遍** —— 换个说法、调个语序、加个修饰都算同一件事；"
    "profile 只用来补书上**还没有**的稳定设定，说不准是不是新的就别写。"
    "**不要复述这一段里发生了什么**（事件只属于这一段，不是跨段设定），不要写猜测，"
    "也不要把这一段的主角当成整个世界的主角。\n"
    "   反例（这些都是**不该写**的）：“两人一起进城游玩、荡过秋千、玩过水”、"
    "“他当时很生气”、“这段文本用占位符表示主角名字”。\n"
    "9. source 必须是**原文的语言**里真的出现过的写法（通常就是那个名字在原文里的拼法）；"
    "写成译文语言的话，profile 这一栏永远触发不了。**占位符**（方括号或花括号里包着"
    "变量名的那种）也不要当 source —— 它会在所有提到这个变量的句子上触发。"
)

#: 记忆命中（〖已定译〗）的两段说明：``keep`` 档只当上下文，``polish`` 档照样回译。
NOTE_KEEP = (
    "（标了〖已定译〗的条目**已经有译文**，见它下面的「现已定为」那一行："
    "它是给你读上下文用的，也是这段里已经确定的叫法，**沿用**它 ——"
    "这几条不必出现在你的 translations 里。）"
)
NOTE_ASK = (
    "（标了〖已定译〗的条目**已经有译文**，见它下面的「现已定为」那一行："
    "那是这段里已经确定的叫法。**照它回填**；只有当它与上下文明显读不通时"
    "才可微调，并在 note 里说明理由。）"
)

#: 紧凑口径的命中句说明：那里的正文**就是**那句已定译（原文不再摆出来），
#: 所以措辞里不提「现已定为」那一行 —— 提了就是让模型去找一行不存在的东西。
NOTE_KEEP_COMPACT = (
    "（标了〖已定译〗的条目**已经有译文**，它下面那一行就是那句定稿的译文："
    "这几条是给你读上下文用的，**不必出现在你的 translations 里**。）"
)
NOTE_ASK_COMPACT = (
    "（标了〖已定译〗的条目**已经有译文**，它下面那一行就是那句定稿的译文："
    "这几条照样要回填；只有当它与上下文明显读不通、或与当前术语冲突时才可改动，"
    "并在 note 里说明理由。）"
)

#: 出厂预设。**两份口径都是这份 dict 的值**，没有第二处副本。
BUILTIN_TEMPLATES: dict[str, dict[str, Any]] = {
    "full": {
        "label": "完整",
        "system": SYSTEM_FULL,
        "header": "目标语言：{target_language}\n源语言：{source_language}",
        "unit_header": "== 单元 {unit}（同一个单元的这几句是连续对白，按顺序翻，各自成条）==",
        "note_keep": NOTE_KEEP,
        "note_ask": NOTE_ASK,
        "item_head": "- {head}",
        "source_line": "  {source}",
        "resolved_line": "  现已定为：{resolved}",
        "protected_line": "  必须原样保留：{protected}",
        "id_format": "[{id}]",
        "marker_text": "〖已定译〗",
        "position_format": "单元内第 {position} 句",
        "speaker_format": "说话人：{speaker}",
        "head_separator": " ",
        "show_id": True,
        "show_id_only_for_asked": True,
        "show_position": True,
        "show_marker": True,
        "show_speaker": True,
        "show_resolved": True,
        "show_protected_for_resolved": False,
        "short_ids": False,
        "with_memory_notes": True,
    },
    "compact": {
        "label": "紧凑",
        "system": SYSTEM_COMPACT,
        "header": "目标语言：{target_language}\n源语言：{source_language}",
        "unit_header": "== 单元 {unit} ==",
        "note_keep": NOTE_KEEP_COMPACT,
        "note_ask": NOTE_ASK_COMPACT,
        "item_head": "- {head}",
        "source_line": "  {source}",
        "resolved_line": "  现已定为：{resolved}",
        "protected_line": "",
        "id_format": "[{id}]",
        "marker_text": "〖已定译〗",
        # 位置用**圆括号**：这一行里只能有一个方括号（id）—— 两个方括号时模型分不清
        # 哪个是 id，真靶实测 16 次调用里 69/661 条回的是句序（`"234"` / `"142/320"`），
        # 译文全都对、只是 key 对不上，整批报废。形状上只有一个可选，比提醒可靠。
        "position_format": "({position})",
        "speaker_format": "{speaker}：",
        "head_separator": " ",
        "show_id": True,
        "show_id_only_for_asked": True,
        "show_position": True,
        "show_marker": True,
        "show_speaker": True,
        "show_resolved": False,
        "show_protected": False,
        "show_protected_for_resolved": False,
        "short_ids": True,
        "with_memory_notes": False,
        "resolved_shows_target": True,
    },
}

#: 面板表单据此渲染、校验据此拒绝错误模板。**前端不维护第二份**。
FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "label", "label": "模板名（给人看的）", "kind": "text"},
    {"key": "system", "label": "system 提示词", "kind": "multiline",
     "help": "整条 system 消息。可用占位符：{target_language} / {source_language}"},
    {"key": "header", "label": "请求开头", "kind": "multiline",
     "help": "可用：{target_language} / {source_language}"},
    {"key": "unit_header", "label": "单元分隔行", "kind": "text", "help": "可用：{unit}"},
    {"key": "note_keep", "label": "命中句说明（keep 档）", "kind": "multiline", "help": "留空则不写"},
    {"key": "note_ask", "label": "命中句说明（polish 档）", "kind": "multiline", "help": "留空则不写"},
    {"key": "item_head", "label": "条目行", "kind": "text",
     "help": "可用：{head}（标注）/ {source}（正文）。留空则正文只走正文行"},
    {"key": "source_line", "label": "正文行", "kind": "text", "help": "可用：{source}；留空＝正文已在条目行里"},
    {"key": "resolved_line", "label": "已定译行", "kind": "text", "help": "可用：{resolved}"},
    {"key": "protected_line", "label": "受保护 token 行", "kind": "text", "help": "可用：{protected}"},
    {"key": "id_format", "label": "编号写法", "kind": "text", "help": "可用：{id}"},
    {"key": "marker_text", "label": "命中标记文字", "kind": "text"},
    {"key": "position_format", "label": "位置写法", "kind": "text", "help": "可用：{position}"},
    {"key": "speaker_format", "label": "说话人写法", "kind": "text", "help": "可用：{speaker}"},
    {"key": "head_separator", "label": "标注之间的分隔符", "kind": "text"},
    {"key": "show_id", "label": "标注里带编号", "kind": "bool"},
    {"key": "show_id_only_for_asked", "label": "只有「要回译」的条目才带编号", "kind": "bool",
     "help": "命中句只当上下文时不带编号，模型才不会把它当成要回填的条目"},
    {"key": "show_position", "label": "标注里带「第 i/n 句」", "kind": "bool"},
    {"key": "show_marker", "label": "标注里带〖已定译〗", "kind": "bool"},
    {"key": "show_speaker", "label": "标注里带说话人", "kind": "bool"},
    {"key": "show_resolved", "label": "命中句写出「现已定为」", "kind": "bool"},
    {"key": "resolved_shows_target", "label": "命中句只摆译文（不摆原文）", "kind": "bool",
     "help": "开：命中句的正文直接就是那句定译；关：摆原文 + 「现已定为」行"},
    {"key": "show_protected", "label": "每条列出受保护 token", "kind": "bool",
     "help": "关掉就只在 system 里说一遍（标记照样要求原样保留）"},
    {"key": "show_protected_for_resolved", "label": "命中句也列受保护 token", "kind": "bool"},
    {"key": "short_ids", "label": "发给模型短编号", "kind": "bool",
     "help": "去掉 id: / keyed: 这类内部分类前缀；短编号只活在线路上，写回时按对照表还原"},
    {"key": "with_memory_notes", "label": "带命中句说明段", "kind": "bool"},
)

_STRING_KEYS = tuple(f["key"] for f in FIELDS if f["kind"] != "bool")
_BOOL_KEYS = tuple(f["key"] for f in FIELDS if f["kind"] == "bool")

#: 花括号组（不含换行、长度有限）。JSON 输出格式与引擎控制码靠它 + 名字名单区分。
_PLACEHOLDER = re.compile(r"\{([^{}\n]{0,40})\}")

#: 渲染时**一定**会被填的占位符（校验拿它区分"占位符"与"JSON 里的花括号"）。
#: 注意这些样例**不许出现任何引擎专有的东西**（文件扩展名、控制码）：内核不认识引擎，
#: `tests/test_engine_boundary.py` 与 `tests/test_layer_contract.py` 会逐字扫这一层。
SAMPLE: dict[str, str] = {
    "target_language": "chinese",
    "source_language": "auto",
    "unit": "第一场",
    "head": "[demo_0001] 说话人：Phi",
    "id": "demo_0001",
    "source": "Hi there!",
    "resolved": "嗨，你好呀！",
    "protected": "⟨变量⟩ ⟨标记⟩",
    "position": "1",
    "speaker": "Phi",
}


# --------------------------------------------------------------------------- #
# 校验 / 解析
# --------------------------------------------------------------------------- #


def fill(pattern: str, values: dict[str, Any], *, where: str = "") -> str:
    """只替换**名单里的**占位符，其余花括号原样留着。

    为什么不用 ``str.format``：system 提示词里写着 JSON 输出格式
    （``{"translations": [...]}``），``str.format`` 会把 ``"translations"`` 当字段名直接
    报错；``{w}`` / ``{size=+25}`` 这类引擎控制码也会被误伤。所以这里只认
    :data:`SAMPLE` 里那几个名字，别的一律不动 —— **渲染永不失败**，
    严格性放在 :func:`validate`（保存那一刻）上。

    ``where`` 只让调用点读起来知道在填哪一段。
    """
    text = str(pattern)

    def swap(match: "re.Match[str]") -> str:
        name = match.group(1)
        return str(values[name]) if name in values else match.group(0)

    return _PLACEHOLDER.sub(swap, text)


def unknown_placeholders(pattern: str) -> list[str]:
    """看着像占位符、却不在名单里的花括号组。

    排除两种"不是占位符"的东西：JSON 输出格式（带引号或冒号）与普通说明文字。
    这样"把 ``{source}`` 拼错成 ``{sorce}``"会在保存那一刻被点出来，而不是静默发出去。
    """
    found: list[str] = []
    for match in _PLACEHOLDER.finditer(str(pattern)):
        body = match.group(1).strip()
        if not body or body in SAMPLE:
            continue
        if '"' in body or ":" in body or " " in body:
            continue
        found.append(body)
    return found


def validate(data: Any, *, where: str = "模板") -> list[str]:
    """模板的问题清单；空 = 通过。**保存前必须过这一关**，否则生产会在半路炸。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"{where} 必须是一个对象"]
    known = set(_STRING_KEYS) | set(_BOOL_KEYS)
    for key in data:
        if key not in known:
            problems.append(f"{where} 里有不认识的字段：{key}")
    for key in _BOOL_KEYS:
        if key in data and not isinstance(data[key], bool):
            problems.append(f"{where}.{key} 必须是 true/false")
    for key in _STRING_KEYS:
        value = data.get(key, BUILTIN_TEMPLATES[DEFAULT_TEMPLATE_NAME][key])
        if not isinstance(value, str):
            problems.append(f"{where}.{key} 必须是字符串")
            continue
        if not value and key in ("system", "header", "item_head"):
            problems.append(f"{where}.{key} 不能为空")
            continue
        unknown = unknown_placeholders(value)
        if unknown:
            problems.append(
                f"{where}.{key} 里这些花括号不是占位符：{'、'.join(unknown[:3])}"
            )
    return problems


def resolve(saved: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """出厂预设 ∪ 用户保存的（同名覆盖）。**这是拿到模板的唯一入口。**"""
    merged: dict[str, dict[str, Any]] = {
        name: dict(body) for name, body in BUILTIN_TEMPLATES.items()
    }
    for name, body in (saved or {}).items():
        if not isinstance(body, dict):
            continue
        base = dict(BUILTIN_TEMPLATES[DEFAULT_TEMPLATE_NAME])
        base.update(merged.get(name, {}))
        base.update({key: value for key, value in body.items() if key in base})
        merged[str(name)] = base
    return merged


def active(name: str, saved: dict[str, Any] | None = None) -> dict[str, Any]:
    """当前生效的模板；名字不认识就退回出厂默认（并在报告里看得见，见 ``problems``）。"""
    templates = resolve(saved)
    return dict(templates.get(str(name) or DEFAULT_TEMPLATE_NAME,
                              templates[DEFAULT_TEMPLATE_NAME]))


def named(name: str = DEFAULT_TEMPLATE_NAME) -> dict[str, Any]:
    """出厂预设的**一份拷贝**（调用方改了它不会污染预设，也不会影响别人）。"""
    return active(name)


def unknown_name(name: str, saved: dict[str, Any] | None = None) -> bool:
    return bool(name) and str(name) not in resolve(saved)


# --------------------------------------------------------------------------- #
# 渲染 —— 唯一的那个函数
# --------------------------------------------------------------------------- #


def render_system(template: dict[str, Any], *, target_language: str, source_language: str) -> str:
    values = {"target_language": target_language, "source_language": source_language}
    return fill(template["system"], values, where="system 提示词").strip()


def render_body(
    template: dict[str, Any],
    *,
    target_language: str,
    source_language: str,
    knowledge: str,
    instructions: str,
    items: list[Any],
) -> tuple[str, dict[str, str]]:
    """把待译内容渲染成请求正文，并给出**短编号对照表**（短编号 → 完整身份）。

    这是唯一的渲染实现：``full`` 与 ``compact`` 的差别全在模板数据里 ——
    ``show_position`` / ``show_resolved`` / ``short_ids`` 这些开关。
    """
    short_ids: dict[str, str] = {}
    values = {"target_language": target_language, "source_language": source_language}
    parts: list[str] = [fill(template["header"], values, where="请求开头")]
    if knowledge.strip():
        parts += ["", "【背景知识】", knowledge.strip()]
    if instructions.strip():
        parts += ["", "【翻译要求】", instructions.strip()]
    parts += ["", "【待译内容】"]

    resolved_any = any(item.resolved for item in items)
    # 命中句**要不要回译**才是选哪段说明的依据（`polish` 档才会请模型顺手看一眼）：
    # 只看"有没有条目要回译"会把 keep 档误判成 polish 档
    asks_resolved = any(item.resolved and item.expects_answer for item in items)
    if template.get("with_memory_notes", True) and resolved_any:
        note = template["note_ask"] if asks_resolved else template["note_keep"]
        if note.strip():
            parts.append(note)

    current_unit = ""
    taken: set[str] = set()
    for item in items:
        unit = getattr(item, "unit_id_of", "") or ""
        if unit and unit != current_unit:
            current_unit = unit
            label = getattr(item, "unit_label", "") or unit
            parts.append(fill(template["unit_header"], {**values, "unit": label},
                              where="单元分隔行"))
        asked = bool(getattr(item, "expects_answer", True))
        # 命中句（keep 档）只当上下文：给它的**正文就是那句已定译**，原文不必再摆一遍。
        # 省下的是整段原文，而且它的译文本来就是定稿 —— 摆原文只是让人读得顺，
        # 模型要的是"这句已经定了，别动它"（polish 档要改它，那时它是 asked，走另一支）。
        quiet_resolved = bool(
            template.get("resolved_shows_target")
            and item.resolved
            and getattr(item, "reused_target", "")
            and not asked
        )
        identity = item.unit_id
        if template.get("short_ids"):
            identity = _short_identity(
                item.unit_id, taken, getattr(item, "unit_label", "") or ""
            )
            taken.add(identity)
            short_ids[identity] = item.unit_id

        head: list[str] = []
        wants_id = template.get("show_id", True) and (
            asked or not template.get("show_id_only_for_asked", True)
        )
        if wants_id and not quiet_resolved:
            head.append(fill(template["id_format"], {"id": identity}, where="编号写法"))
        if template.get("show_marker", True) and item.resolved:
            head.append(str(template["marker_text"]))
        if template.get("show_position", True) and getattr(item, "position", "") \
                and not quiet_resolved:
            head.append(fill(template["position_format"], {"position": item.position},
                             where="位置写法"))
        if template.get("show_speaker", True) and getattr(item, "speaker", ""):
            head.append(fill(template["speaker_format"], {"speaker": item.speaker},
                             where="说话人写法"))

        item_values = {
            **values,
            "head": str(template["head_separator"]).join(head),
            "source": item.source,
            "resolved": getattr(item, "reused_target", "") or "",
            "protected": " ".join(getattr(item, "protected", ()) or ()),
        }
        parts.append(fill(template["item_head"], item_values, where="条目行"))
        body_values = dict(item_values)
        if quiet_resolved:
            body_values["source"] = item_values["resolved"]
        if str(template["source_line"]).strip():
            parts.append(fill(template["source_line"], body_values, where="正文行"))
        if not quiet_resolved and item.resolved and getattr(item, "reused_target", "") and \
                str(template["resolved_line"]).strip():
            parts.append(fill(template["resolved_line"], item_values, where="已定译行"))
        tokens = item_values["protected"]
        show_tokens = bool(tokens) and template.get("show_protected", True) and (
            template.get("show_protected_for_resolved", False) or not item.resolved
        )
        if show_tokens and str(template["protected_line"]).strip():
            parts.append(fill(template["protected_line"], item_values, where="受保护 token 行"))

    return "\n".join(parts), short_ids


def _short_identity(unit_id: str, taken: set[str], unit_key: str = "") -> str:
    """发给模型的身份：**去掉内部分类前缀**（``id:`` / ``keyed:``），单元名能对上就一并去掉。

    ``unit_key`` 是这个条目所属单元自己的写法（``== 单元 X ==`` 里那个 X）。它对不上就
    不动 —— 少剥一层只是多几个字符，剥错一层就是两个条目共用一个身份。

    ``taken`` 是这一次请求里已经用掉的写法：撞了就退到更长的形式（必要时加序号）。
    绝不返回一个已经用过的写法 —— 译文落到别人头上是**静默错位**，比"对不上"难查得多。
    """
    text = str(unit_id)
    for prefix in INTERNAL_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
            break
    key = str(unit_key or "").strip()
    if key and text.startswith(key + "_") and len(text) > len(key) + 1:
        candidate = text[len(key) + 1:]
        if candidate not in taken:
            return candidate
    if text not in taken:
        return text
    for form in sorted(id_forms(unit_id), key=len):
        if form not in taken:
            return form
    index = 2
    while f"{text}-{index}" in taken:
        index += 1
    return f"{text}-{index}"


def digest(text: str) -> str:
    """请求正文的指纹：把"拦截下来的请求"与"真发出去的那次"对上号用。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def template_digest(template: dict[str, Any]) -> str:
    """这份模板的摘要 —— 台账用它回答"这一行是哪套模板产出的"。

    ⚠️ 它**不进知识指纹**：模板以后是创意工坊里用户自己的
    东西，换一套模板就把全工程译文判成过期、触发一次静默全量重翻，代价太大。所以
    "换模板会作废旧译文"这件事**不成立**，要重做就显式重跑；追溯走台账里记的摘要。

    ``label`` **不算在内**：它只是给人看的标题，不进请求，改个名字不该换掉这个摘要。
    其余字段全都进 —— 它们都会改变模型看到的东西（system、条目行、受保护 token 行、
    以及那些 show_* 开关）。
    """
    payload = {key: value for key, value in dict(template or {}).items() if key != "label"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
