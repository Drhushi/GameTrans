"""翻译层：按带权路径图调度翻译，串行或并行。

执行器（Executor）只做一件事：**按 Task 生成译文**（Guide §15）。所以这里的流水线是

    Unit → TranslationTask（Task Planner）
         → RetrievedContext（Context Resolver，按检索阶梯取候选）
         → ContextPackage（Context Builder，组织成模型输入）
         → provider 调用
         → 结构校验（Quality Gate 的第一半）
         → 状态落盘（Task Store）

它做四件事：

1. 为每条待译内容建一条 Task，把上下文、术语/风格/引擎约束与来源装进去；
2. 按路径图的权重顺序分批，串行或并行地交给 provider；
3. **单项失败隔离** —— 一批炸了只影响那一批，缺条目的单独记账，整轮照跑到底；
4. 把批量进度投给交互层（默认对用户透明，agent 看得到）。

provider 只是"一批进去一批出来"的黑盒，因此换模型不动这一层的任何代码。
"""

from __future__ import annotations

import collections
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator

from gametrans.core.constraints import (
    default_constraints,
    slot_gauge,
    validate,
)
from gametrans.core.agentqueue import AgentQueue, QUEUE_DIRNAME
from gametrans.core.graph import PathGraph
from gametrans.core.models import (
    Constraint,
    ConstraintType,
    PLACEHOLDER_ERROR,
    PathNode,
    Provenance,
    Scanner,
    Segment,
    SegmentKind,
    TranslationArtifact,
    TranslationStatus,
    TranslationUnit,
)
from gametrans.core.report import RunReport
from gametrans.core.schedule import TranslationPlan, default_scheduler, scheduler_named
from gametrans.core.tasks import (
    ContextRequest,
    ExecutionPolicy,
    TaskPlanner,
    TaskState,
    TranslationTask,
    ValidationPolicy,
)
from gametrans.errors import ProviderError, TranslateError
from gametrans.layers.context import (
    DEFAULT_LAYERS,
    MUTUALLY_EXCLUSIVE_ARMS,
    RETRIEVAL_LADDER,
    ContextBuilder,
    ContextResolver,
    RetrievalPolicy,
)
from gametrans.layers import entityflow
from gametrans.layers.memory import IMPORTED_PROVIDER, MemoryEntry, MemorySnapshot, normalize
from gametrans.layers.naming import produces_assets
from gametrans.layers.preflight import VERDICT_OK, AssetPreflight, inspect_assets
from gametrans.layers.resource import (
    ResourceContext,
    ResourceLayer,
    drift_report,
)
from gametrans.layers.tags import announce, paused_regions, wrap, writings_to_tag
from gametrans.providers.base import (
    CallLog,
    DeclaredTerm,
    LLMProvider,
    RequestDump,
    TranslationItem,
    TranslationRequest,
    TranslationResult,
    coerce_declared,
    open_request_dump,
)
from gametrans.providers.mock import MockProvider
from gametrans.providers.openai_compat import OpenAICompatProvider
from gametrans.providers.registry import ProviderRegistry, build_provider_registry

__all__ = [
    "LLMProvider",
    "MockProvider",
    "OpenAICompatProvider",
    "ProviderRegistry",
    "TranslateLayer",
    "TranslateOptions",
    "TranslationItem",
    "TranslationRequest",
    "TranslationResult",
    "build_provider_registry",
]

VALID_MODES = ("auto", "serial", "parallel")

#: 前驱集的两种算法（见 :attr:`TranslateOptions.predecessors`）。
VALID_PREDECEDENCE: tuple[str, ...] = ("knowledge", "control")

#: 开工前资产预检的两种处置：拦，或只报。
VALID_ASSET_GATES = ("block", "warn")

#: provider 暂时性故障（429 / 5xx）的退避表，单位秒。第 i 次重试等第 i 项。
#: 真靶上那 90 条槽位是被一次 429 整批丢掉的 —— 限流不是"白花钱"那种连不上，
#: 等一会儿再来一次就该好。测试把它改成 ``(0.0, ...)``，免得为了重试睡十几秒。
PROVIDER_RETRY_BACKOFF: tuple[float, ...] = (2.0, 8.0, 20.0)

#: 记忆命中的句子摆进请求之后，给不给予改动它的权力。
#: ``keep`` = 只当上下文（默认，省钱零风险）；``polish`` = 允许为通顺改动，改动进修订提案。
VALID_MEMORY_REUSE_MODES = ("keep", "polish")

#: "同一单元里两条原文不同的句子译文一字不差"这条判据的**长度门**（只留可读字符数）。
#: 为什么要门：一个语气词本来就只有一两种写法（`Hmmm...` / `Hmm...` 都是"嗯……"），
#: 拿它当"挂错原文"会淹掉真信号 —— 真靶全量实测：不加门 426 条假警报，加了 0 条。
_DUPLICATE_MIN_SOURCE = 12
_DUPLICATE_MIN_TARGET = 4
#: "一条译文整段埋在另一条里"那条判据的最小可读长度：短到 `嗯？` / `哦。` 这一步的
#: 串，本来就只能被包含在长句里，不构成信号。
_BURIED_MIN_CHARS = 6

#: **一次装不下就自动转多轮**时，每轮最多几条。150 是真靶量出来的保守值
#: （该工程 act25 / deepseek-flash：150 条一轮的实测输出 3,730–5,356 token，
#: 落在 8K 默认输出预算里；2,000 条级别的单元一次发完在 64K 预算下仍然被截断）。
DEFAULT_ROUND_LINES = 150

#: 条数闸的档位名 → 报告里给人看的说法（`layers/context.DEFAULT_ITEM_CAPS` 的键）。
#: 报"被挤掉了几条"时用中文说清楚是**哪一档**被挤了 —— 挤掉术语书里命中的行，
#: 后果是模型自己起名字。
QUOTA_GROUP_NAMES: dict[str, str] = {
    "termbook_hit": "术语书里命中的行",
    "termbook": "术语书",
    "style": "风格要求",
}

#: 进提示词的上下文段。其余段（任务目标、当前原文、来源……）在 Task 与 item 上已经有了。
ASSEMBLED_SECTIONS = (
    "terminology_constraints",
    "style_constraints",
    "relevant_knowledge",
)

#: **每批跑完**的回调：``(批名, 这一批申报的实体)`` —— 一个实体一项
#: （``DeclaredTerm``：原文写法 + 译名 / 设定）。
#:
#: 一批 = 计划里一轮并行调用（攒批之后是一组层）。回调在这一批的调用全部返回之后、
#: 下一批发出之前执行，所以它在这里做的合并/批准**下一批立刻能用上**。
BatchHook = Callable[[str, list["DeclaredTerm"]], None]


def _target_of(book: Any, writing: str) -> str:
    """书上这个写法现在的译名（没有这一行、或这一行还没译名 → 空串）。"""
    entry = book.find(str(writing).strip()) if book is not None else None
    return entry.target_for(str(writing).strip()) if entry is not None else ""


def declared_constraints(
    unit: TranslationUnit, policy: ValidationPolicy
) -> list[Constraint]:
    """这次 Task 要求校验器检查哪些约束。

    ``require_structure=False`` 时只留输出形状那一条；``extra_constraints`` 原样带上 ——
    内核不认识的约束名不会假装检查过（``core/constraints.py::validate`` 会跳过它们，
    也不把它们算进 ``checked``）。
    """
    declared = default_constraints(unit)
    if not policy.require_structure:
        declared = [
            c
            for c in declared
            if c.constraint_type == ConstraintType.OUTPUT_SHAPE_VALID.value
        ]
    declared.extend(
        Constraint(constraint_type=name, target=unit.id)
        for name in policy.extra_constraints
    )
    return declared


def _bounded_edit_distance(left: str, right: str, limit: int) -> int:
    """两条字符串的编辑距离；超过 ``limit`` 就返回 ``limit + 1``（省时间，也够判）。

    只给"这两句原文是不是同一句的两种写法"用 —— 真靶上作者自己打了不少错字
    （`Eve's thoguhts` / `thoguhts`、`on the gym` / `at the gym`、
    `too oddly too specific`），它们共用一条译文是**对的**。
    """
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, 1):
        current = [i]
        for j, char_right in enumerate(right, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_left != char_right),
                )
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _same_sentence_variants(sources: list[str]) -> bool:
    """这几条原文是不是**同一句的两种写法**（错字 / 多一个词 / 语序小改）。

    判据：任意两条的**有界编辑距离** ≤ 长的那条的三成（至多 12 个字符）。
    这条线是量出来的（真靶全量 15,755 条）：

    * 两成 → 还剩 78 条假警报（`You too. {w}See you on Monday.` /
      `You, too. {w}I'll see you on Monday.` 这类差 5–7 个字符的改写）；
    * 三成 → 0 条假警报，而且**抓得住真错位**：实测那几条真错位的原文完全不同，
      编辑距离 40 往上，差着量级。

    ⚠️ 一样是**提醒不是断言**：这里放过的只是"看着像同一句的改写"。
    """
    readable = [str(value or "").strip() for value in sources]
    for index, left in enumerate(readable):
        for right in readable[index + 1 :]:
            longest = max(len(left), len(right))
            limit = max(3, min(12, longest * 3 // 10))
            if _bounded_edit_distance(left, right, limit) > limit:
                return False
    return True


def _unit_correspondence_issues(
    unit: Any,
    keys: list[str],
    records: dict[str, Any],
    source_of: Any,
) -> dict[str, dict[str, Any]]:
    """一个单元里"译文与原文对不上"的槽位 —— **逐条槽位判不出来、要整个单元一起看**。

    判逐句长度的那一半住 `core/constraints.py`（`SOURCE_TARGET_CORRESPONDS`，比的是
    "这条译文像不像这条原文的译文"）。这里补的是**同一单元内部的错位**，它只有在拿到
    整段的时候才看得出来：

    * 两条例子的原文不同、**译文一字不差** —— 真靶上抓到的正是这个形状（模型按位置
      回填时错位一句，整段的译文都往前串了一位）；
    * 一条译文**整段埋在**另一条里，而两条的原文长度差着量级（拿整个单元当一条回来、
      或反过来的那半边）。

    ⚠️ 判据只做形状判断，判不了语义："两句话真的翻成同一句中文"是可能的 ——
    所以命中只是**置留 + 点名**，由人/agent 看一眼，不是断言它错。

    返回 ``{key: {reason, ...}}``，住处是 `_reject_mismatched_records`（那里才拿得到
    "这一批的全部槽位"，含记忆命中的那些）。
    """
    issues: dict[str, dict[str, Any]] = {}
    readable: dict[str, tuple[str, str]] = {}
    for key in keys:
        record = records.get(key)
        if record is None or getattr(record, "target", "") is None:
            continue
        text = str(record.target or "").strip()
        # 空目标不参与（那一类归"译文为空"那条判据）：这里只管"有字、但可能挂错了"。
        if not text:
            continue
        readable[key] = (text, str(source_of(key) or "").strip())

    def _shape(value: str) -> str:
        """只留**可读内容**：标点与空白不参与判等。

        为什么需要它：真靶上一个单元里 `...` / `.......` / `..........` 各有好几条，
        按原始字符比是"不同的原文共用一条译文"，按内容比是**同一句** —— 后者才是事实。
        不加这一步，全量上会报出两千多条假警报（实测 2,461 条 → 0 条）。
        """
        return "".join(char for char in value if char.isalnum())

    # —— ① 原文不同、译文一字不差 ——
    by_target: dict[str, list[str]] = {}
    for key, (text, _source) in readable.items():
        by_target.setdefault(text, []).append(key)
    for text, group in by_target.items():
        if len(group) < 2:
            continue
        # ⚠️ **短句不查这一条**：`Hmmm...` / `Hmm...` 都翻成"嗯……"、`HEY!` / `Hey!`
        # 都翻成"喂！"是对的 —— 一个语气词本来就只有一两种写法。真靶全量上，
        # 不做这道长度门会报出 426 条这种假警报（做了是 0 条）。
        if len(_shape(text)) < _DUPLICATE_MIN_TARGET:
            continue
        if any(len(_shape(readable[key][1])) < _DUPLICATE_MIN_SOURCE for key in group):
            continue
        distinct_sources = {_shape(readable[key][1]) for key in group}
        if len(distinct_sources) < 2:
            continue  # 原文本来就一模一样：共用一条译文是对的（真靶 30.4% 的槽位是重复原文）
        if _same_sentence_variants([readable[key][1] for key in group]):
            # 同**一句**的两种写法（作者打错字 / 多一个词）：共用一条译文是对的。
            # 真靶上这一类占绝大多数（`You look lovely with you hair up.` /
            # `with your hair up`），不放过它们这条判据就没法用。
            continue
        # **整组都置留**，不是挑一条：一个译文挂到两条不同的原文上，机器判不出哪条对 ——
        # 挑一条等于替人做了那个判断，而且挑法（插入顺序）还是任意的。
        for key in group:
            issues[key] = {
                "reason": "duplicate_across_slots",
                "unit_id": str(getattr(unit, "id", "")),
                "target": text,
                "source": readable[key][1],
                "same_target_slots": [item for item in group if item != key][:5],
            }

    # —— ② 一条译文整段埋在另一条里，而原文长度差着量级 ——
    # 用**可读内容**比（标点不算）：`嗯？` 不该被当成"埋在"长句里的那一条。
    def _buried_text(value: str) -> str:
        return _shape(value)

    order = sorted(readable, key=lambda key: -len(_buried_text(readable[key][0])))
    for index, long_key in enumerate(order):
        long_text = _buried_text(readable[long_key][0])
        for short_key in order[index + 1 :]:
            short_text = _buried_text(readable[short_key][0])
            if len(short_text) * 2 > len(long_text):
                break  # 已经排序：后面的只会更接近，不再可能是"埋在整段里"的那一条
            if len(short_text) < _BURIED_MIN_CHARS or short_text not in long_text:
                continue
            if len(short_text) >= max(3, len(long_text) * 1 // 4):
                continue
            long_source = readable[long_key][1]
            short_source = readable[short_key][1]
            if len(short_source) >= 0.6 * max(1, len(long_source)):
                # 两条原文本来就长短差不多：那是"短句被并进长句"的正常翻法，不是错位
                continue
            issues[long_key] = {
                "reason": "target_pasted_from_longer",
                "unit_id": str(getattr(unit, "id", "")),
                "target": long_text[:200],
                "source": long_source,
                "buried_slot": short_key,
                "buried_target": short_text,
            }
    return issues


def _default_template() -> dict[str, Any]:
    """没显式给模板时的兜底：出厂默认那一个（见 ``prompts.DEFAULT_TEMPLATE_NAME``）。

    延迟 import 是为了不让 layers 在导入期就拉上 prompts（那一层不认识模板名，
    只认识"这一次请求长什么样"这份内容）。
    """
    from gametrans import prompts

    return prompts.named()


@dataclass
class TranslateOptions:
    """翻译层的全部可调项。对应交互层里"用户可以配置的主 API 与翻译逻辑"。"""

    provider: str = "mock"
    target_language: str = "zh_CN"
    source_language: str = "auto"
    concurrency: int = 4
    #: auto 会在有多批且并发 > 1 时自动并行
    mode: str = "auto"
    #: 一次调用最多装几条。**50 是"单次调用封顶"，不是"批大小"**：段能装下就整段一次，
    #: 装不下才按它拆。来历：实测整段一次质量最高、成本最低（每单元 token −31%），
    #: 但一次断掉要丢一轮的 20%；封到一轮预算的约 5%（≈40–60 条）能把中断损失压回 1.4%。
    batch_size: int = 50
    #: **按引擎结构的哪一级把单位分组**。这里 ``""`` = 不分组（供基线臂与逐条实验用）；
    #: **产品默认走配置层**：`project.json` 的 ``group_by`` 默认是 ``"auto"``，由
    #: :meth:`ProjectSession.default_grouping_level` 解析成适配层申报的首级
    #: （Ren'Py 是 `label`、RPGM 是 `map`），适配层一级都没申报时退回不分组。
    #:
    #: 这是三级粒度里的中间那一级：**分组决定谁和谁共享背景资料**，``batch_size`` 只决定
    #: 这一组要拆成几次调用 —— 预算压力不改变单元边界。层级名由适配层申报
    #: （``grouping_levels``）；内核不认识的层级**报错**，不静默退回"不分组"
    #: （静默退回等于实验条件悄悄失效）。
    group_by: str = ""
    use_glossary: bool = True
    use_worldbook: bool = True
    #: 风格指南是一等资源，因此和术语一样有开关（Guide §13）
    use_style: bool = True
    #: 整份知识参不参与（**对照臂**用）：关掉就是"这一轮假装术语书是空的"。
    #: 它管的是注入，不是别的什么东西的开关。
    use_knowledge: bool = True
    #: **前驱集怎么算** —— 一个参数，两种输入：
    #:
    #: * ``knowledge``（默认）：控制边 **+ 知识边**。一个区域的前驱 = 它引用的那些实体的
    #:   **引入场**（哪一场第一次交代了它）—— 见 :mod:`gametrans.layers.entityflow`。
    #:   章内按它分波，章间照旧按章序。
    #: * ``control``：只看控制边（照旧：玩家会怎么走就是翻译顺序）。
    #:
    #: 刻意**不是**多一张前驱表：这是同一张表的两种算法，A/B 与回退都靠它。
    #: 也刻意**不进 `to_dict()`**（和 `memory_reuse_mode` 一样）：换算法是"这一轮我认了"
    #: 的执行条件，不是替以后每一次背书的项目配置。
    predecessors: str = "knowledge"
    #: 复用翻译记忆（精确命中零成本复用；模糊命中只作参考）
    use_memory: bool = True
    #: **相近译法**要不要查。默认不查：它是"全表 + difflib"，实测 12,999 条记忆时
    #: 一次全量跑批要 42 分钟、换 0 条参考译法（见 register）。
    #: 刻意**不进 `to_dict()`**：这是"我认了这个开销"的运行期声明，不是项目配置。
    fuzzy_memory: bool = False
    #: **注入的条数闸**（`layers/context.DEFAULT_ITEM_CAPS` 的覆盖）。`None` = 出厂那套。
    #: 档位就两个：``termbook``（术语书，按行算，额度不够时先保原文里命中的）/ ``style``。
    #: 刻意**不进 `to_dict()`**：这是"这一批我认了"的运行期声明，不是项目配置 ——
    #: 报告里说"要收口就抬闸"时它得真能抬，但抬多高由这一次跑的人说了算。
    item_caps: dict[str, int] | None = None
    #: 把**读进来的**已有译文也算命中：游戏里本来就有译文（`resource harvest` 读进来、
    #: 来源标为 imported）的那些条目。默认 False —— 它们没有知识指纹，证明不了自己是
    #: 在当前术语/风格状态下翻的；要用就得由调用方显式声明。
    #: 刻意**不进 `to_dict()`**（和 fuzzy_memory 一样）：这是"这一批我认了"的运行期声明，
    #: 不是可持久化的项目配置 —— 写进 project.json 等于替以后每一次背书。
    reuse_imported: bool = False
    #: **旧译文摆进请求之后，给不给予改动它的权力**。两档：
    #:
    #: * ``keep``（默认）—— 已定译的句子**只作上下文**，不要求回译：省钱、零风险，
    #:   但第一次的写法会被整套沿用（一致的错也是它）。
    #: * ``polish`` —— **允许模型为了上下文通顺而改动**它：模型照常回译这几条，
    #:   凡与已定译不同的都记成**修订提案**（`memory_revision_proposals`），
    #:   **不就地覆盖** —— 覆盖要等人或 agent 拍板（与改名机制同一条纪律）。
    #:
    #: 与 `fuzzy_memory` / `reuse_imported` 一样刻意**不进 `to_dict()`**：这是
    #: 本次调用的执行条件（也是消融臂），不是可持久化的项目配置。
    memory_reuse_mode: str = "keep"
    #: 结构校验没过时，带着「缺了什么」再问模型几次（0 = 不重试）
    retry_on_violation: int = 1
    #: 同一个 Task 最多问几次模型；超过就进 NEEDS_REVIEW，不无限重试
    max_attempts: int = 3
    #: 结构校验开关。关掉它只剩输出形状检查 —— 校验策略必须真的被执行，
    #: 声明了却不生效的开关比没有这个开关更糟
    require_structure: bool = True
    custom_instructions: str = ""
    #: **这一轮只走检索阶梯的哪几层**（``None`` = 阶梯全开，与从前一致）。
    #:
    #: 这是给消融实验用的执行条件：RQ3 要把"只给句子"与"加上结构上下文"分开比较，
    #: 前提是系统真的能少取那几层。此前阶梯写死在 :meth:`retrieval_policy` 里，
    #: 于是"关掉结构上下文"根本做不出来 —— 两组跑的其实是同一组。
    #: 层名不认识时**报错**，不静默退回全开（静默退回等于实验条件悄悄失效）。
    context_layers: tuple[str, ...] | None = None
    #: **这一轮只翻这些单元**（``None`` = 全图）。给分块 / 续跑用。
    #:
    #: 一次跑批几千次模型调用，中途被杀不能白花：已完成的分块留在盘上，重跑时那部分
    #: 一个 token 都不该再问模型。做到这条只有一条路 —— 把"这一轮只翻这些"变成执行条件。
    #:
    #: **范围只影响"翻哪些"，不影响"看到什么"**：上下文仍从全图取。若按范围裁剪图，
    #: 被裁掉的邻居 / 容器 / 依赖就消失了，"续跑出来的译文"与"一次跑完的译文"不再是
    #: 同一个条件，消融实验当场失效。
    unit_scope: tuple[str, ...] | None = None
    #: 跑完**把译文变成资产**：从这一轮的译文里观察"重复且一致"的对应，登记成术语候选。
    #: 候选一律 `pending_validation`（模型整理出来的对应达不到人工水平），要人或 agent
    #: 批准才进术语表 —— 所以打开它不会让模型输出反向变成约束。
    #:
    #: 默认开：关掉它，一轮翻完就只剩"一堆译文"，资产要靠人再跑一条离线命令才出现
    #: （这正是"边翻译边产出"之前没兑现的地方）。**对照臂要关掉它**才有干净的基线。
    #: 与 `reuse_imported` / `unit_scope` 一样，刻意**不进 `to_dict()`**：它是运行期声明，
    #: 不是项目配置。
    produce_candidates: bool = True
    #: **开工前的资产预检怎么处置**：``block`` = "批准过的资产一条都进不了请求"时拒绝开工；
    #: ``warn`` = 只报不拦（明知某个专名这一轮还没出现时用）。
    #:
    #: 被拦的只有一种情形（``assets_never_fire``）：**有能用资产、却一条都命中不了**。
    #: "零资产"与"只有待批候选"都**允许开工**（全新工程的第一轮就该是零资产），
    #: 但必须在报告里点名 —— 真靶那次就是"181 条候选一条没批、46 个单元照跑"，
    #: 而流程里没有任何一步会因此出声。
    asset_gate: str = "block"
    #: **从计划的第几个阶段开始跑**（1 起数）。续跑用：第一轮跑完停下来等批准，
    #: 批准之后再从第二阶段接着跑，第一阶段一个字都不重问。
    start_phase: int = 1
    #: **跑完第几个阶段就停**（0 = 不停）。第一轮＝计划的第一个阶段：跑完停下，
    #: 等人或 agent 给资产拍板 —— 闸门是代码，不是口头约定。
    stop_after_phase: int = 0
    #: **用哪个调度策略算计划**（空 = 默认照搬依赖分层）。指定 ``seed-bulk`` 才有
    #: "第一轮只为产出资产"的两段形状；它自称未校准，所以只是可选，不当默认。
    plan_strategy: str = ""
    #: **这一轮用的是哪个模板**（名字，不是内容）。只用来记账：台账要能回答"这一行是
    #: 哪套模板产出的"，而模板是用户自己的东西（以后还能从创意工坊拿），所以记的是
    #: **它的名字**，不替它起名字，也不在这里做任何特殊化。
    #: 与 `unit_scope` 一样刻意**不进 `to_dict()`** —— 它跟着 `prompt_template` 走，
    #: 不是另一项可持久化配置。
    template_name: str = ""
    #: **一个单元一次最多问几条槽位**（0 = 不切，`一个单元 = 一次请求` 的设计默认）。
    #:
    #: 这不是"分块翻译"：切出来的段仍是**同一个单元**，单元身份、任务、校验、写回
    #: 全不变，段与段之间**串行**、并按 IN/OUT 把前一段的译文交给后一段当上下文
    #: （见 :meth:`TranslateLayer._iter_runs`）。
    #:
    #: 为什么必须有这个旋钮：消息一次能装多少**不由我们决定**。真靶工程的
    #: `script` 单元 90 条，用 GLM-5.3-Flash 无论重试几次都过不去（81 条那次在 308 秒
    #: 处被服务端回 HTTP 400），同一条请求切一半（41 条）304 秒跑完、41 条全回来。
    #: 上限是**时间**：慢模型上长单元切不开，就整场戏一条都拿不到。
    unit_budget: int = 0
    #: **一个单元分几轮问完**（0 = 一次发完，老形状；>0 = 每轮最多这么多条）。
    #:
    #: 为什么要有它：**输出侧一次装不下**。真靶实测（该工程 act25 / deepseek-flash，
    #: 2026-09-27）：1,972 条一次发完，输出预算给到 64K 仍然撞上限（`finish_reason=length`），
    #: 响应是半截 JSON → 整批报废（195 秒 + 7 万输入 + 6.5 万输出全白花）；同一个单元切成
    #: 14 轮（每轮 150 条）全部回来，墙钟 210.7 秒、输入 102 万 token 里 **91% 命中缓存**。
    #:
    #: 形状：第 1 轮只发第 1..N 条，第 2 轮只发第 N+1..2N 条 —— 前几轮的原文与译文由
    #: `TranslationRequest.history` 带着（**不重发、也不写"继续"**），历史只追加不改，
    #: 前缀因此稳定、吃得到服务端的 cache。轮内串行、单元间并行。
    #:
    #: `unit_budget` 与它是两种形状（独立请求+窗口 vs 同一会话续写）：**同时给时以它为准**
    #: （`unit_budget` 不再生效），并在报告的 `notes` 里说明。
    round_lines: int = 0
    #: **小批量并行**：把本来"一层一层串行"的计划，按不超过这么多**单元**攒成一批，
    #: 批内并行、批间串行（0 = 不攒，严格按计划的分层走 —— 默认，形状不变）。
    #:
    #: 为什么要有它：视觉小说的图是一条长链（真靶 32 层，其中 27 层只有 1 个单元），
    #: 严格分层跑等于把叙事长度 1:1 变成轮数。攒批的代价是**批内后面的单元看不见
    #: 前面单元刚定下的叫法** —— 这个代价不在这里还，而是跑完之后由"合并术语 +
    #: 冲突裁决 + 统一替换"补回来（用户在 2026-09-22 定的路线）。
    batch_units: int = 0
    #: **"重复且一致就自动批准"的批数门槛**（0 = 关；默认 2）。
    #:
    #: 设计口径（路线第 ② 步）：攒批之后，批内的单元看不见前面刚定下的
    #: 叫法，所以每批跑完把"同一个原文在**不同的批**里各自被申报过一次
    #: 且译名一致"的申报写进术语书 —— 下一批就真能用上。判断权仍在人手里：0 关掉自动
    #: 写入（一条都不写，全交给人），写进去之后要改就走待审更正队列。
    #:
    #: 为什么门槛不能是 1：一批 = 一轮调用，**一批里申报过一次就批准**等于"模型申报、
    #: 模型批准自己"。为什么是"批"而不是"条数"：批内的调用并行发出、互相看不见，
    #: 同一批里说两遍不是两份独立证据。
    #:
    #: 与 `produce_candidates` / `asset_gate` 一样刻意**不进 `to_dict()`**：它是本次运行的
    #: 纪律声明（也是对照臂），不是可持久化的项目配置 —— 写进 `project.json` 等于替以后
    #: 每一次背书。
    auto_approve_terms: int = 2
    extra: dict[str, Any] = field(default_factory=dict)
    #: **请求模板**（内容，不是名字）：这一次请求长什么样由它决定。
    #: 由 :meth:`ProjectSession.translate_options` 从配置里解析好再传下来 ——
    #: 这一层不认识"模板名"，也不去翻配置。
    prompt_template: dict[str, Any] = field(default_factory=lambda: _default_template())

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(
                f"未知的翻译模式：{self.mode!r}（可用：{', '.join(VALID_MODES)}）"
            )
        if str(self.predecessors) not in VALID_PREDECEDENCE:
            raise ValueError(
                f"未知的前驱集算法：{self.predecessors!r}"
                f"（可用：{', '.join(VALID_PREDECEDENCE)}）"
            )
        if self.batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        if self.unit_budget < 0:
            raise ValueError("unit_budget 不能为负（0 = 不切分，一个单元一次请求）")
        if self.round_lines < 0:
            raise ValueError("round_lines 不能为负（0 = 一次发完，>0 = 每轮最多这么多条）")
        if self.batch_units < 0:
            raise ValueError("batch_units 不能为负（0 = 不攒批，严格按计划的分层走）")
        if self.auto_approve_terms < 0:
            raise ValueError("auto_approve_terms 不能为负（0 = 不自动批准，只落候选）")
        if self.auto_approve_terms == 1:
            raise ValueError(
                "auto_approve_terms 不能是 1：一批里申报过一次就批准，等于让模型批准自己"
                "（要自动批准，门槛至少 2 —— 同一个原文要在不同的两批里各自被申报过一次）"
            )
        # 命令行上没法传空串，所以给一个同义词：`none` = 不分组。
        # 它落到内核之前一律归成 ""，免得后面到处都是"两种写法都表示不分组"的分支。
        if str(self.group_by).strip().lower() in ("none", "-"):
            self.group_by = ""
        if self.concurrency <= 0:
            raise ValueError("concurrency 必须为正整数")
        if self.context_layers is not None:
            if isinstance(self.context_layers, str):
                # 传字符串会把每个字符当一层（'direct' → d,i,r,e,c,t），
                # 那种档位跑出来的读数是假的，必须在入口就拒
                raise ValueError(
                    "context_layers 应当是一串层名的元组，不是字符串"
                    f"（收到 {self.context_layers!r}）"
                )
            unknown = [name for name in self.context_layers if name not in RETRIEVAL_LADDER]
            if unknown:
                raise ValueError(
                    f"未知的检索层：{', '.join(unknown)}"
                    f"（可用：{', '.join(RETRIEVAL_LADDER)}）"
                )
            # 两条对照臂互斥（"结构关系"与"等预算平铺"没有第三种含义）：同时声明就是
            # 条件定义错误，当场报错，不许静默二选一 —— 那会让两组实验跑成同一组
            for arm in MUTUALLY_EXCLUSIVE_ARMS:
                if arm <= set(self.context_layers):
                    names = "、".join(sorted(arm))
                    raise ValueError(
                        f"这些检索层互斥，不能同时请求：{names}"
                        "（它们是「结构关系」与「等预算平铺」两条对照臂）"
                    )
        if self.unit_scope is not None:
            if isinstance(self.unit_scope, str):
                raise ValueError("unit_scope 应当是一串 unit id 的元组，不是字符串")
            # 归一：去重 + 排序。调用方给的顺序不影响结果（"同一批"要看集合，不看排列）
            scope = tuple(sorted({str(uid) for uid in self.unit_scope if str(uid)}))
            if not scope:
                # 空范围不是"什么都不翻"，多半是调用方算错了 —— 静默当成"翻 0 条"
                # 会让分块推进看起来正常、实际一条都没落盘
                raise ValueError("unit_scope 不能为空元组（要翻全图就别给这个参数）")
            self.unit_scope = scope
        if self.memory_reuse_mode not in VALID_MEMORY_REUSE_MODES:
            raise ValueError(
                f"未知的记忆复用档位：{self.memory_reuse_mode!r}"
                f"（可用：{', '.join(VALID_MEMORY_REUSE_MODES)}）"
            )
        if self.asset_gate not in VALID_ASSET_GATES:            raise ValueError(
                f"未知的资产预检处置：{self.asset_gate!r}"
                f"（可用：{', '.join(VALID_ASSET_GATES)}）"
            )
        if self.start_phase < 1:
            raise ValueError(f"start_phase 从 1 起数，收到 {self.start_phase}")
        if self.stop_after_phase < 0:
            raise ValueError(f"stop_after_phase 不能为负，收到 {self.stop_after_phase}")
        if self.stop_after_phase and self.stop_after_phase < self.start_phase:
            # "从第三阶段开始、跑完第一阶段就停"是一条永远跑不到东西的指令。
            # 静默当成"什么都不跑"会让一次续跑看起来正常、实际一条都没落盘。
            raise ValueError(
                f"stop_after_phase({self.stop_after_phase}) 早于"
                f" start_phase({self.start_phase})：这条指令一条都跑不到"
            )

    def effective_context_layers(self) -> tuple[str, ...]:
        """这一轮实际会走的检索层，**按阶梯顺序**排列（与执行顺序一致）。

        声明的是"要哪几层"，执行顺序照阶梯来 —— 顺序影响取数先后与预算分配。
        """
        if self.context_layers is None:
            return DEFAULT_LAYERS
        wanted = set(self.context_layers)
        return tuple(name for name in RETRIEVAL_LADDER if name in wanted)

    def to_dict(self) -> dict[str, Any]:
        # 刻意不含 fuzzy_memory / reuse_imported / unit_scope：它们是本次调用的临时
        # 控制信息，不是可持久化的项目配置
        return {
            "provider": self.provider,
            "target_language": self.target_language,
            "source_language": self.source_language,
            "concurrency": self.concurrency,
            "mode": self.mode,
            "batch_size": self.batch_size,
            "group_by": self.group_by,
            # 一个模型一次装得下多少条是**这台机器 + 这个模型**的事实，所以可持久化：
            # 换模型要重新量，但不必每次命令行都重复声明。
            "unit_budget": self.unit_budget,
            # 一轮最多几条：同样是"这个模型一次回得完多少"的事实，可持久化。
            "round_lines": self.round_lines,
            # 小批量并行的批量（0 = 不攒批）：它是"这台机器 + 这个模型 + 这个交付节奏"
            # 的事实，换了要重量，所以和 unit_budget 一样可持久化。
            "batch_units": self.batch_units,
            "use_glossary": self.use_glossary,
            "use_worldbook": self.use_worldbook,
            "use_style": self.use_style,
            "use_knowledge": self.use_knowledge,
            "use_memory": self.use_memory,
            "retry_on_violation": self.retry_on_violation,
            "max_attempts": self.max_attempts,
            "require_structure": self.require_structure,
            "custom_instructions": self.custom_instructions,
            # 请求模板（内容，不是名字）：它进请求体、属于"我这个用户/这台机器"的习惯，
            # 所以是**可持久化**的（放进 to_dict 才不会在写回配置时被静默丢掉）
            "prompt_template": self.prompt_template,
        }

    # ---- 协议视图 -----------------------------------------------------------

    def execution_policy(self) -> ExecutionPolicy:
        return ExecutionPolicy(
            provider=self.provider,
            mode=self.mode,
            batch_size=self.batch_size,
            concurrency=self.concurrency,
            retry_on_violation=self.retry_on_violation,
            max_attempts=self.max_attempts,
        )

    def validation_policy(self) -> ValidationPolicy:
        return ValidationPolicy(require_structure=self.require_structure)

    def retrieval_policy(self) -> RetrievalPolicy:
        return RetrievalPolicy(
            # 声明的档位必须真的传下去：不传就等于阶梯全开，消融实验量到的两组是同一组
            layers=self.effective_context_layers(),
            fuzzy_memory=self.fuzzy_memory,
            use_glossary=self.use_glossary,
            use_worldbook=self.use_worldbook,
            use_style=self.use_style,
            use_knowledge=self.use_knowledge,
            use_memory=self.use_memory,
            # 条数闸的档位是可调的（`None` = 出厂那套）：报告里说"要收口就抬闸"时，
            # 得真有个地方能抬 —— 否则那句话是空头支票。
            item_caps=self.item_caps,
        )


@dataclass
class _BatchOutcome:
    index: int
    size: int
    ok: int
    error: str | None
    phase: str = ""


@dataclass
class _RunContext:
    """一次翻译运行的共享状态，省得把五个参数一路穿下去。"""

    graph: PathGraph
    resources: ResourceLayer
    options: TranslateOptions
    report: RunReport
    #: 适配器提供的结构扫描器；没有它就只能做形状检查（如实报告，不假装通过）
    scanner: Scanner | None = None
    #: 适配器提供的分组器：``(units, level) -> {unit_id: 组键}``。三级粒度里中间那一级
    #: （单元＝结构段）由它兑现；内核只按申报的层级分组，不认识就报错。
    grouping: Callable[[list[Any], str], dict[str, str]] | None = None
    #: node_id → 它在图里的**文档顺序**（适配器建树的先后）。
    #:
    #: **一次请求里的条目必须按这个顺序排**，不能按权重排。权重序是"先翻谁"的调度序，
    #: 拿它当请求内的顺序，等于把一整段对白按句子长短打乱发给模型 —— 上下文承接、
    #: 指代、语气全断，而报告上一切正常（实测真靶 `start` 段 23 条：第 1 条是叙事中段、
    #: 开场那句 "Hi there!" 排到最后）。顺序保持是理论里的硬约束之一。
    doc_order: dict[str, int] = field(default_factory=dict)
    #: 项目标识：进 task_id，让同一 Unit 在不同项目里是不同的 Task
    project_id: str = ""
    #: node_id → 这个节点所属区域所需要的知识点
    #: 命中的记忆 key（跑完一次性累加复用计数，省得每条都重写文件）
    reused: list[tuple[str, str]] = field(default_factory=list)
    #: 这一轮新产出的译文：``(原文, 译文, 产出它时的知识指纹)``（跑完一次性写回记忆）
    new_memory: list[tuple[str, str, str]] = field(default_factory=list)
    #: **这一轮内**已经产出的译文，立即可查：``(归一化原文, 语言) → MemoryEntry``。
    #:
    #: 为什么要有它：新译文原本只攒在 `new_memory` 里、**跑完才落盘**，
    #: 于是"同一句在后面某个单元里又出现"在同一轮里命中不了 —— 真靶实测 15,755 条槽位里
    #: **4,792 条（30.4%）是重复原文**（`...` 一条就出现 2,605 次），
    #: 这些重复全部白问了一次模型（`memory_hits: 0`），而且同一个 `*Chuckle*`
    #: 被翻成 73 种写法这种一致性问题也是这么来的。
    #:
    #: 键与落盘记忆完全一致（归一化原文 + 目标语言），所以"这一轮内命中"与
    #: "下一轮命中"是**同一条判据**，不存在两套口径。
    #:
    #: ⚠️ **一个必须承认的限制**：并发的批会在前一批翻完之前就做完记忆切分，
    #: 所以"同一波并发里"的重复句仍会各问一次。跨轮、跨阶段（阶段之间是屏障）、
    #: 以及串行模式下，复用都是完整的。要做到"同一波内也复用"，得把重复句
    #: 调度到首现批次之后 —— 那是排程问题，不是判据问题。
    memory_cache: dict[tuple[str, str], Any] = field(default_factory=dict)
    #: 被"译文与原文对不上"这条判据**置留**的槽位：``槽位身份 → 它的原文``。
    #:
    #: 为什么要记下来：置留发生在收下之后（`_reject_mismatched_records` 要看整个单元），
    #: 而收下的那一刻译文已经进了 `new_memory` / `memory_cache`。错的译文一旦进记忆，
    #: 下一轮同一句原文会**直接复用它、一个模型字符都不发** —— 真靶 2026-09-29 抓到的
    #: 五条错配就是这么在盘上活了下来的。所以落盘前按这份名单把它们从记忆里摘掉。
    mismatched_sources: dict[str, str] = field(default_factory=dict)
    #: **这一批开始时**的记忆视图与"这一轮已产出"的副本：批内一切命中判据都读它们。
    #: 为什么：跑批是并发的，"后来者"能看见什么不该取决于谁先跑完 —— 否则同一张图两次跑
    #: 输入就可能不同，三次重复的对照随之作废。冻结在批次边界上，本批新提交的下一批才可见。
    memory_snapshot: MemorySnapshot | None = None
    memory_cache_snapshot: dict[tuple[str, str], Any] = field(default_factory=dict)
    #: `memory_reuse_mode="polish"` 档里，模型对"已定译"那句给出的**不同写法**。
    #:
    #: 记录成**修订提案**，**不就地覆盖** —— 覆盖已确认的译名要人或 agent 拍板
    #: （与改名机制同一条纪律：谁拍的板、旧值、新值、理由，一条都不能少）。
    polish_proposals: list[dict[str, Any]] = field(default_factory=list)
    #: (node_id, 是否算知识点) → 知识指纹。取记忆、给产物盖章、写回记忆都要用它，
    #: 一种组合只算一次（`fingerprint_for` 不便宜，真实工程上几千条要好几秒）
    fingerprints: dict[tuple[str, bool], str] = field(default_factory=dict)
    #: 每批实际发出的请求（用来汇总"给了多少条参考译法"）
    requests: list[TranslationRequest] = field(default_factory=list)
    #: 资源文件摘要（本次运行取一次；每条文本都重算是纯浪费）
    resource_versions: dict[str, str] = field(default_factory=dict)
    #: unit_id → 上一条译文违反了哪些约束（给重试提示用）
    last_verdict: dict[str, list[str]] = field(default_factory=dict)
    #: 槽位 / 单元键 → **这一条原文该出现的术语标签**（``⟦写法⟧`` 里的写法，按出现顺序）。
    #:
    #: 它是"发出去的是什么"的留档：包裹在组装请求时发生（`_tag_items`），对账在读回响应时
    #: 拿它对（`_accept_target`）—— 不在两处各算一次，否则术语书在这中间变了，两边就会
    #: 各说各话（同一批里另一个单元刚批准一条译名，正是会发生的情况）。
    term_tags: dict[str, list[str]] = field(default_factory=dict)
    #: unit_id → 这条译文**被声明的偏离**（empty / expression）；一轮算一次
    approved: dict[str, frozenset[str]] = field(default_factory=dict)
    #: 这一轮产出的全部 Task（跑完交给 Task Store 落盘）
    tasks: list[TranslationTask] = field(default_factory=list)
    #: **调用流水**：每一次真实调用一条（请求原文 + 服务端原始回复），跑完由调用方落盘。
    #: 报告里只有 token 数，复盘一次实验时那样不够 —— 得能看见发了什么、回了什么。
    transcript: list[dict[str, Any]] = field(default_factory=list)
    #: 模型这一轮**自己申报的实体**（原文写法 → 译名 / 设定），去重后按出现顺序。
    #: 由 `_invoke` 从响应里收集；跑完交给调用方落成候选（见 §1.4 的断言纪律）。
    declared_terms: list[DeclaredTerm] = field(default_factory=list)
    #: 已经收过的原文，去重（同一个名字在不同批里被申报多次只留一条）
    _declared_seen: set[str] = field(default_factory=set)
    #: **这一批**刚申报的实体（每批开跑前清空，批跑完交给 `batch_hook`）。
    #:
    #: 为什么不直接用上面那个列表：它是**整轮去重后**的（同一个原文只留第一次的说法），
    #: 而跨批合并唯一要看的证据恰恰是"重复"与"前后不一致" —— 去重把两样都吃掉了。
    batch_terms: list[DeclaredTerm] = field(default_factory=list)
    _batch_terms_seen: set[tuple[str, str, str]] = field(default_factory=set)
    #: unit_id → 这一轮又问过模型几次（状态历史与 attempts 都按它走）
    retries: dict[str, int] = field(default_factory=dict)
    #: unit_id 的集合：这一批报废的原因是**模型答了但我们读不出来**（不是连不上）。
    #: 两类要分开处置：答坏了重问一次常常就好，连不上重试只是白花钱。
    malformed_units: set[str] = field(default_factory=set)
    #: 检索器**一轮只建一个**：它按说话者建的索引是 O(全图) 的，每条文本重建一次
    #: 就把整轮退化成 O(n²)（真实工程上实测到 6 倍变慢）
    resolver: "ContextResolver | None" = None
    #: 这一轮全部模型调用的计量（token / 延迟）。并行批次共享一个账本，加锁写入。
    calls: CallLog = field(default_factory=CallLog)
    #: **请求落盘**：设了它就把真正发出去的请求体逐次写成文件（默认关，看环境变量）
    dump: RequestDump | None = None


class TranslateLayer:
    """按图调度翻译。"""

    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    # ---- 任务规划与上下文装配 -----------------------------------------------

    def plan_task(self, node: PathNode, ctx: _RunContext) -> TranslationTask:
        """把一个节点变成一条待执行的 Task（Guide §4 的 Task Planner）。"""
        unit = node.unit
        assert unit is not None
        planner = TaskPlanner(
            project_id=ctx.project_id,
            target_language=ctx.options.target_language,
            execution_policy=ctx.options.execution_policy(),
            validation_policy=ctx.options.validation_policy(),
        )
        return planner.plan(
            unit,
            context_request=ContextRequest(
                # Task 里记的 kinds 就是这一轮会走的层（与 retrieval_policy 同一份口径），
                # 否则"实验条件"只是调用方的一厢情愿：写着四层、实际取了四层
                kinds=ctx.options.effective_context_layers(),
            ),
        )

    def ready_task(
        self,
        node: PathNode,
        ctx: _RunContext,
        *,
        skip_layers: tuple[str, ...] = (),
    ) -> TranslationTask:
        """规划 + 检索，得到 READY 状态的 Task。"""
        task = self.plan_task(node, ctx)
        retrieved = self._resolver(ctx).resolve(
            task, unit=node.unit, node_id=node.node_id, skip_layers=skip_layers
        )
        return task.advance(TaskState.READY, context=retrieved)

    @staticmethod
    def _resolver(ctx: _RunContext) -> ContextResolver:
        """这一轮的检索器（只建一次，见 :attr:`_RunContext.resolver`）。

        在这里把"自定义要求"接到资源层上 —— 它是**一条风格条目**，检索与指纹两条路
        都从资源层读它，于是不会出现"检索时按一个值算、判过期时按另一个值算"。
        """
        if ctx.resolver is None:
            ctx.resources.set_custom_instructions(ctx.options.custom_instructions)
            ctx.resolver = ContextResolver(
                ctx.resources, graph=ctx.graph, policy=ctx.options.retrieval_policy()
            )
        return ctx.resolver

    @staticmethod
    def _assemble(tasks: list[TranslationTask]) -> str:
        """把若干 Task 的 ContextPackage 拼成一段背景资料（同一段只出现一次）。

        拼的是**能改变译文**的那几段：【术语书】（译名与设定）、风格要求（含自定义要求）、
        相近译法，外加邻接文本。

        容器层级、区域、知识点 id 这些**机器已有的结构**刻意不拼进去（Guide §21）：
        它们的作用是驱动检索（哪些知识该被取来），不是喂给模型重建一遍。它们仍然留在
        Task 的 ``retrieved_context`` 里，供审计回答"这条译文依据了什么"。
        """
        blocks: list[str] = []
        seen: set[str] = set()

        def push(text: str) -> None:
            text = text.strip()
            if text and text not in seen:
                seen.add(text)
                blocks.append(text)

        for task in tasks:
            # 只组织真正要进提示词的三段：Builder 的其余段在这里没人看，白算
            package = ContextBuilder(ASSEMBLED_SECTIONS).build(
                task, task.retrieved_context
            )
            for name in ASSEMBLED_SECTIONS:
                push(package.sections.get(name, ""))
            neighbours = [
                item.content
                for item in task.retrieved_context.items
                if item.layer == "direct" and item.type == "neighbour"
            ]
            if neighbours:
                push("【相邻文本】\n" + "\n".join(f"- {text}" for text in neighbours))
        return "\n\n".join(blocks)

    def build_request(
        self,
        nodes: list[PathNode],
        resources: ResourceLayer,
        options: TranslateOptions,
        graph: PathGraph | None = None,
        *,
        project_id: str = "",
        tasks: list[TranslationTask] | None = None,
        calls: CallLog | None = None,
        phase: str = "",
        context_tasks: list[TranslationTask] | None = None,
        transcript: list[dict[str, Any]] | None = None,
        dump: RequestDump | None = None,
        use_memory: bool = False,
        tagged: dict[str, list[str]] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> TranslationRequest:
        """组装一次 provider 调用。

        传 ``graph`` 才会把**依赖图给出的知识点**算进背景资料 —— 那些是"这段文本需要
        知道、但它自己没提到"的东西，光靠字面匹配拿不到。

        每条内容都先变成一条 Task（带检索结果与约束），再进请求：Executor 收到的是
        Task 的集合，而不是一串裸字符串（Guide §14、§15）。

        ``history`` 是**这一轮之前已经说过的话**（长单元分轮时由
        :meth:`_run_as_rounds` 逐轮追加）：prefill 进 ``[system] + history + [user]``，
        所以同一场戏的前几轮不用重发、也不写"继续"。
        """
        ctx = _RunContext(
            graph=graph or PathGraph(engine=""),
            resources=resources,
            options=options,
            report=RunReport(command="translate", project_root=".", engine=""),
            scanner=None,
            project_id=project_id,
        )
        planned = [self.ready_task(node, ctx) for node in nodes if node.unit is not None]
        if tasks is not None:
            tasks.extend(planned)
        if use_memory:
            # 面板预览专用：真跑是**先**分记忆、再建请求（见 `_split_memory` 的调用点）。
            # 不分这一步，预览里就永远没有"已定译"那几行 —— 而那正是最该看的东西。
            # 只影响这一次装配：命中与否都不落盘、不发网络。
            _, nodes, _ = self._split_memory(nodes, ctx)
            planned = [self.ready_task(node, ctx) for node in nodes if node.unit is not None]
        by_unit = {task.unit_id: task for task in planned}
        items = self._items_for(nodes, by_unit)
        # 还没定译的写法在**这里**包成 `⟦写法⟧`：送出去的就是它，读回来的按它对账
        # （见 `_accept_target` 的 `term_tags`）。已定译的写法照旧走"写法 → 译名"硬约束。
        items = self._tag_items(items, resources, options, tagged)
        # 分组时，背景资料取自**整个结构段**（组内每一批都看到同一份），
        # 这样"单元"就是真的单元；不分组时与从前一致（取本批自己的）。
        knowledge = self._assemble(context_tasks if context_tasks is not None else planned)
        # 这一段跟着请求走、不进用户可改的模板：模板改了机制不该跟着失效。
        if tagged:
            listed: list[str] = []
            for writings in tagged.values():
                for writing in writings:
                    if writing not in listed:
                        listed.append(writing)
            if listed:
                knowledge = "\n\n".join(part for part in (knowledge, announce(listed)) if part)
        return TranslationRequest(
            items=items,
            target_language=options.target_language,
            source_language=options.source_language,
            knowledge=knowledge,
            # 自定义要求走**风格通道**（见资源层的 set_custom_instructions），不再
            # 另开一段 —— 所以这里没有 instructions；这一段是**修复批**专用的。
            instructions="",
            metadata={
                "memory_suggestions": knowledge.count("（相似度 ") if knowledge else 0
            },
            tasks=planned,
            calls=calls,
            phase=phase,
            transcript=transcript,
            dump=dump,
            prompt_template=options.prompt_template,
            template_name=options.template_name,
            # 会话历史（长单元分轮时给）：与正文同一份请求体，`[system] + history + [user]`
            history=list(history or []),
        )

    @staticmethod
    def _items_for(
        nodes: list[PathNode], by_unit: dict[str, TranslationTask]
    ) -> list[TranslationItem]:
        """把一批结点摊成**按槽位**的条目：一个单元含多句时，每句一条。

        为什么必须摊开（实测过两边的坏法）：

        * 一个单元只发一条 → 245 条槽位只能拿到同一段话（写回把整段往每条槽位里写）；
        * 干脆一句一条 → 又退回"一次请求一句话"，单元上下文全丢（这轮事故的起因）。

        所以载荷是"一个单元 = 一次请求"，但请求里**逐句列出**、每条带自己的槽位身份，
        模型逐句给译文，写回就能逐条落到位置上。单元身份记在
        :attr:`TranslationItem.unit_id`（合同一组，提示词里标出来）。
        """
        items: list[TranslationItem] = []
        #: 一次请求里**同一条 id 只许出现一次**（不变量）。真靶上出现过同一个结点被塞进来
        #: 两遍 → 请求翻倍、模型只答一半、修复白跑（136 条里只回 65 条）。上游为什么会重复
        #: 是另一件事（由 `_repair` 的警告去查），这里先把请求本身守住 —— 重复条目对模型
        #: 只是噪声，没有任何好处。
        seen_ids: set[str] = set()

        def take(item: TranslationItem) -> bool:
            if item.unit_id in seen_ids:
                return False
            seen_ids.add(item.unit_id)
            items.append(item)
            return True

        for node in nodes:
            unit = node.unit
            if unit is None:
                continue
            keys = list(unit.metadata.get("slot_keys") or [])
            # 没有定位信息（测试夹具、老数据）时按"整条当一条"处理，不是崩掉。
            locator = getattr(unit, "locator", None)
            entries = (getattr(locator, "payload", None) or {}).get("slots") or []
            if not keys or not entries:
                # 兜底：没有槽位清单的单元（老数据/测试夹具）整条当一条条目
                take(
                    TranslationItem(
                        unit_id=unit.id,
                        source=unit.source,
                        path=node.path,
                        context=unit.context.render(),
                        speaker=unit.context.speaker,
                        kind=unit.type,
                        protected=[s.value for s in unit.protected_segments],
                        task_id=by_unit[unit.id].task_id if unit.id in by_unit else "",
                    )
                )
                continue
            by_key = {str(entry.get("slot_key") or ""): entry for entry in entries}
            pending = set(keys)
            reused = dict((unit.metadata or {}).get("context_slot_targets") or {})
            # 请求里列**全部**槽位、按文档顺序、编号用真实总数：
            # 记忆命中的那几条留在原位当上下文（标 `resolved`，不要求回译）。
            # 摘掉它们会让"连续对白"断掉、编号失真（真靶实测，见 `_reduced_node`）。
            #
            # **单元内切分**是唯一的例外：那时载荷只列"这一段 + 前面的窗口"
            # （`window_slot_keys`），因为把整单元摊在每次请求里会让慢模型每次都读到
            # 整个单元（真靶：25 条一段却仍列 90 条 → 四次调用全部在 300 秒处超时）。
            # 编号仍然按**整个单元**算，读者看到的还是"第 4/7 句"。
            full_order = [str(entry.get("slot_key") or "") for entry in entries] or list(keys)
            window = [str(key) for key in (unit.metadata.get("window_slot_keys") or [])]
            if window:
                keep = set(window)
                entries = [
                    entry
                    for entry in entries
                    if str(entry.get("slot_key") or "") in keep
                ]
            position_of = {key: index for index, key in enumerate(full_order, start=1)}
            order_keys = [str(entry.get("slot_key") or "") for entry in entries] or list(keys)
            for order, key in enumerate(order_keys, start=1):
                entry = by_key.get(key) or {}
                # 受保护 token 取**这一句自己的**：一个单元几百句时，把整段的 token
                # 逐句重复一遍能把请求放大几十倍（真靶实测 5.47M 字符里 94% 是这个）。
                # 注意"这一句没有 token"（空列表）与"没有这条信息"是两回事：
                # 前者就该什么都没有，后者才回落到整个单元的清单。
                if "protected" in entry:
                    own_protected = [str(token) for token in (entry.get("protected") or [])]
                else:
                    own_protected = [s.value for s in unit.protected_segments]
                take(
                    TranslationItem(
                        unit_id=key,
                        source=str(entry.get("source") or unit.source),
                        path=node.path,
                        # 结构上下文取自单元（这一句属于哪场戏）
                        context=unit.context.render(),
                        # 提示词里给**人名**（显示名），别给脚本变量名（`d` 那种半截东西）；
                        # 没有显示名时才回落到变量名，最后才是单元级的说话人
                        speaker=(
                            entry.get("display_speaker")
                            or entry.get("speaker")
                            or unit.context.speaker
                        ),
                        kind=unit.type,
                        protected=own_protected,
                        task_id=by_unit[unit.id].task_id if unit.id in by_unit else "",
                        unit_id_of=unit.id,
                        unit_label=str(
                            (unit.metadata or {}).get("structure_label") or ""
                        ),
                        position=f"{position_of.get(key, order)}/{len(full_order)}",
                        resolved=bool(reused.get(key)),
                        reused_target=str(reused.get(key) or ""),
                        expects_answer=key in pending,
                    )
                )
        return items

    @staticmethod
    def _tag_items(
        items: list[TranslationItem],
        resources: ResourceLayer,
        options: TranslateOptions,
        tagged: dict[str, list[str]] | None = None,
    ) -> list[TranslationItem]:
        """把"术语书里还没定译"的写法在本条原文里包成 ``⟦写法⟧``。

        只包**没有译名**的写法；关掉术语那一栏（对照臂）时一个都不包 —— 那一臂的意思
        就是"不看术语书"，而标签是术语书的一部分（从标签漏进去，对照就白做了）。

        ``tagged`` 给了就两边都留着：已经记过的键**以记录为准**（重出/修复那一条必须
        与首次发出去的一模一样），没记过的才现算。差别的来源是真实存在的 ——
        同一批里另一个单元刚把某条译名批准了，这一刻的书与发出去那一刻的书就不是一份。
        """
        if not items or not options.use_glossary:
            return items
        book = resources.termbook
        policy = resources.trigger_policy
        out: list[TranslationItem] = []
        for item in items:
            recorded = (tagged or {}).get(item.unit_id)
            writings = list(recorded) if recorded else writings_to_tag(
                book, item.source, policy=policy
            )
            if not writings:
                out.append(item)
                continue
            if tagged is not None:
                tagged[item.unit_id] = writings
            out.append(
                replace(item, source=wrap(item.source, writings, policy=policy))
            )
        return out

    # ---- 调度 ---------------------------------------------------------------

    def run(
        self,
        graph: PathGraph,
        resources: ResourceLayer,
        options: TranslateOptions,
        report: RunReport,
        *,
        interaction: Any = None,
        plan: TranslationPlan | None = None,
        scanner: Scanner | None = None,
        grouping: Callable[[list[Any], str], dict[str, str]] | None = None,
        project_id: str = "",
        tasks_out: list[TranslationTask] | None = None,
        terms_out: list[DeclaredTerm] | None = None,
        transcript_out: list[dict[str, Any]] | None = None,
        batch_hook: "BatchHook | None" = None,
        summaries: dict[str, dict] | None = None,
        workdir: Path | None = None,
    ) -> list[TranslationArtifact]:
        """按计划翻译整张图。

        ``plan`` 决定**执行顺序**：阶段按顺序跑，阶段内部自由并行。不传就用默认调度
        策略（照搬依赖分层，不额外引入策略）。

        ``batch_hook`` 是**每批跑完**（不是整轮跑完）的回调，签名见 :data:`BatchHook`。
        它拿到的正好是这一批的申报；调用方在这一刻合并术语 / 批准候选，下一批的请求
        就会带上新的叫法。钩子返回后本层会**重新取一次资源版本**，让每条译文如实记下
        "它跑的时候生效的是哪一版"。

        ``scanner`` 是适配器提供的结构切分器（见
        :meth:`~gametrans.engines.base.EngineSupportPack.structure_scanner`）。给了它，
        每条译文都会被机器校验受保护结构有没有被破坏；没给就只做形状检查，并在报告里
        如实记下"未做结构校验"。

        ``tasks_out`` 是一个可选的收集器：这一轮产出的每条 Task 都会被放进去，由调用方
        决定落盘到哪（状态必须持久化，Guide §17）。

        注意**执行顺序与返回顺序是两件事**：无论计划怎么排，返回的记录一律按路径图
        的权重序 —— 下游（写回、报告）不该因为调度方式不同而拿到不同的排列。
        """
        provider = self.providers.get(options.provider)
        if not provider.is_configured():
            raise ProviderError(
                f"provider {provider.name!r} 尚未配置完成，先不开始翻译",
                hint=(
                    "补齐凭证后重试；若只是想跑通链路，用 --provider mock。"
                ),
            )

        if plan is None:
            plan = scheduler_named(options.plan_strategy).plan(
                graph, flow=self.entity_flow(graph, resources, options)
            )
        problems = plan.validate()
        if problems:
            raise TranslateError(
                "翻译计划有问题：" + "；".join(problems),
                hint="一个区域只能出现在一个阶段里；检查计划的 phases 字段。",
            )
        # 第一轮 / 续跑：阶段是可以切成一段段跑的。切之前先把越界的档位拒掉 ——
        # 静默当成"跑 0 个阶段"会让一次续跑看起来正常、实际一条都没落盘。
        phase_count = len(plan.phases)
        if options.start_phase > phase_count:
            raise TranslateError(
                f"计划只有 {phase_count} 个阶段，start_phase={options.start_phase} 越界",
                hint="用 `gametrans plan` 看这次实际有几个阶段。",
            )
        if options.stop_after_phase > phase_count:
            # 停下来比计划晚：那就等于不停（多要的阶段本来就不存在），如实归一
            options.stop_after_phase = phase_count

        nodes = graph.translatable_nodes()
        if options.unit_scope is not None:
            # 范围只筛"翻哪些"：`graph` / `topics` / 资源一律仍按**全图**建，
            # 这样分块跑的上下文与一次跑完逐条相同（测试卡着这一条）
            wanted = set(options.unit_scope)
            available = {node.unit.id for node in nodes if node.unit is not None}
            missing = sorted(wanted - available)
            if missing:
                # 静默跳过 = 那块永远没人翻，而报告说"完成"
                raise TranslateError(
                    f"unit_scope 里有图上不存在的单元：{', '.join(missing)}",
                    hint="范围要按当前 scan 的 unit id 给；换了提取结果之后 id 会变。",
                )
            nodes = [node for node in nodes if node.unit is not None and node.unit.id in wanted]
        ctx = _RunContext(
            graph=graph,
            resources=resources,
            options=options,
            report=report,
            scanner=scanner,
            grouping=grouping,
            project_id=project_id,
            # 文档顺序只算一次：一次请求里的条目要按它排（不是按权重排）
            doc_order={node_id: index for index, node_id in enumerate(graph.walk())},
            # 资源摘要同样只取一次
            resource_versions=resources.versions(),
            # 声明的偏离也取一次：判据要按它放行"有意留空 / 经批准的表达式改写"
            approved=resources.deviations.by_unit(),
            # 请求落盘（默认关）：设了 GAMETRANS_DUMP_REQUESTS 就把真正发出去的请求写成文件
            dump=open_request_dump(),
        )
        if ctx.dump is not None:
            report.metrics["requests_dump"] = str(ctx.dump.directory)
        if scanner is None:
            report.warn(
                "适配器没有提供结构扫描器，本次只做输出形状校验",
                code="structure_scan_unavailable",
            )
        # ---- 注入会被截断的单元本轮不进------------------
        # 这一条文本**字面命中的定译**超过条数闸时，那几个名字就不会进请求 —— 送出去
        # 等于让模型自己起名字。真靶就是这么坏的：`Saki → 咲` 在 20 次调用里有 **12 次
        # 根本没进请求**（每次恰好 12 条、按排序截尾），盘上于是冒出「沙希」，
        # 看起来像模型前后不一致，其实是**它没收到那条**。
        #
        # 所以这里**不是**事后告警，而是事前**暂停**：宁可等，也不要翻出模型自造的名字。
        # 出口和"等定译"一样 —— 抬闸（`item_caps`）或把这一段拆小（`--unit-scope`）。
        resolver = self._resolver(ctx)
        kept: list[PathNode] = []
        trimmed: dict[str, dict[str, int]] = {}
        for node in nodes:
            if node.unit is None:
                continue
            retrieved = resolver.resolve(
                self.plan_task(node, ctx), unit=node.unit, node_id=node.node_id
            )
            if retrieved.dropped.get("termbook_hit"):
                trimmed[node.unit.id] = dict(retrieved.dropped)
                continue
            kept.append(node)
        if trimmed:
            totals: dict[str, int] = {}
            for counts in trimmed.values():
                for group, count in counts.items():
                    totals[group] = totals.get(group, 0) + count
            report.metrics["units_paused_for_injection"] = len(trimmed)
            report.metrics["context_quota_dropped"] = dict(sorted(totals.items()))
            named = "、".join(
                f"{QUOTA_GROUP_NAMES.get(group, group)} {count} 条"
                for group, count in sorted(totals.items())
            )
            report.add_issue(
                "warnings",
                code="units_paused_for_trimmed_injection",
                message=(
                    f"{len(trimmed)} 个单元**因为注入会被条数闸截断而暂停**，本轮没跑"
                    f"（被截掉：{named}）。那些名字没送到模型手上，翻出来是它自己起的名字"
                    "—— 先把闸抬上去（`item_caps`），或把这一段拆小（`--unit-scope`），再跑"
                ),
                detail={"paused": {unit: counts for unit, counts in sorted(trimmed.items())}},
            )
            nodes = kept
            if not nodes:
                raise TranslateError(
                    "本轮所有单元的注入都会被条数闸截断，一条也跑不了",
                    hint="抬 `item_caps`（`termbook` 那一档），或缩小范围（`--unit-scope`）。",
                )

        region_of = {node.node_id: graph.region_of(node.node_id) for node in nodes}

        # ---- 涉及"条目改过、还没复核"的单元本轮不进--------
        # 规则一句话：**条目被改了就要审**（`TermBook.apply`）—— 包括"追加一条事实"，
        # 它就是改这一行的 `profile`。所以在复核之前，书里那一行**还是旧值**；一个节点
        # 只要在原文里碰上这一行，就不该翻：拿旧值翻等于把待审的东西当没发生。
        # 与"等定译""注入被截断"合起来是一个机制：**书上有悬着的东西，就等**。
        pending_rows = resources.termbook.pending_writings()
        if pending_rows:
            from gametrans.layers.preflight import request_texts
            from gametrans.layers.trigger import TriggerPolicy, key_in_text

            trigger = TriggerPolicy()
            review_held: dict[str, list[str]] = {}
            keep: list[PathNode] = []
            for node in nodes:
                # 扫的是**请求里那份文本**（正文 + 说话人标注）—— 与注入的命中断据同一口径。
                # 只扫正文时，"说话人只出现"的行一边进不了请求、一边待审也拦不住，
                # 两边各自看一半（2026-09-29 修）。
                text = "\n".join(request_texts([node]))
                hit = sorted(word for word in pending_rows if key_in_text(text, word, policy=trigger))
                if hit:
                    review_held[str(node.unit.id)] = hit
                    continue
                keep.append(node)
            if review_held:
                report.metrics["units_paused_for_review"] = len(review_held)
                report.metrics["pending_writings"] = sorted(pending_rows)
                held = "、".join(
                    f"{unit}←{'、'.join(marks)}" for unit, marks in sorted(review_held.items())[:8]
                )
                report.add_issue(
                    "warnings",
                    code="units_paused_for_review",
                    message=(
                        f"{len(review_held)} 个单元**涉及改过、还没复核的条目**，本轮没跑：{held}"
                        f"{'…' if len(review_held) > 8 else ''}。"
                        "模型捎带出来的改动一律排队等复核（复核之前书里还是旧值）—— "
                        "`resource term pending list` 看队列，采纳或驳回之后重跑即自动继续"
                    ),
                    detail={
                        "paused": {unit: marks for unit, marks in sorted(review_held.items())},
                        "pending": sorted(pending_rows),
                    },
                )
                nodes = keep
                if not nodes:
                    raise TranslateError(
                        "本轮所有单元都涉及还没复核的条目，一条也跑不了",
                        hint="先处理待审队列：`resource term pending list`，采纳或驳回。",
                    )
                region_of = {node.node_id: graph.region_of(node.node_id) for node in nodes}

        # ---- 开工前的资产预检 -------------------------------------------------
        # 先回答"这一轮到底用上了几条资产"，再决定开不开工。真靶那次是 181 条候选一条
        # 没批、46 个单元照跑，而流程里没有一步会因此出声（见 `layers/preflight.py`）。
        # 范围按 `unit_scope` 之后的 `nodes` 算；阶段再切窄时预检只会更宽松 ——
        # 拦的是"批准过却一条都进不去"，那是方向性问题，与切到第几阶段无关。
        preflight = self._preflight(
            resources,
            nodes,
            options,
            report,
            graph=graph,
            summaries=summaries,
            workdir=workdir,
        )
        if preflight.blocking and options.asset_gate == "block":
            raise TranslateError(
                preflight.summary(),
                hint=(
                    "要么把术语的原文写法改成原文里真有的那一种（键必须是原文侧），"
                    "要么显式降级：--asset-gate warn。"
                ),
            )

        # ---- 等定译的区域本轮不进------------------------
        # 一个区域要用到"前驱引入、而名字还没定"的写法，它就还不能开跑：翻出来的是
        # 一堆 `⟦写法⟧`，写回那一刻全被置留。**显式点名（--unit-scope）优先于这条闸** ——
        # 那是人/agent 当场拍的板。没有前驱的区域不受影响（`why()` 排除了自己首见的实体）。
        if options.unit_scope is None:
            flow = self.entity_flow(graph, resources, options)
            if flow is not None and flow.entities:
                texts: dict[str, list[str]] = {}
                for node in nodes:
                    region = region_of.get(node.node_id)
                    if not region:
                        continue
                    payload = (
                        getattr(getattr(node.unit, "locator", None), "payload", None) or {}
                    )
                    texts.setdefault(region, []).extend(
                        str(slot.get("source") or "") for slot in (payload.get("slots") or [])
                    )
                paused = paused_regions(resources.termbook, flow, texts)
                if paused:
                    before = len(nodes)
                    nodes = [n for n in nodes if region_of.get(n.node_id) not in paused]
                    report.metrics["paused_regions"] = len(paused)
                    report.metrics["paused_units"] = before - len(nodes)
                    report.metrics["paused"] = {
                        region: writings for region, writings in sorted(paused.items())
                    }
                    held = ", ".join(
                        f"{region}←{'、'.join(writings) if writings else '（前驱被暂停）'}"
                        for region, writings in sorted(paused.items())[:8]
                    )
                    report.add_issue(
                        "warnings",
                        code="regions_paused_for_naming",
                        message=(
                            f"{len(paused)} 个区域在**等定译**，本轮没跑：{held}"
                            f"{'…' if len(paused) > 8 else ''}。"
                            "把那些写法定下来（`resource term add <写法> <译名>`；"
                            "确定保留原文就写成同一个写法），再跑一次就自动继续"
                        ),
                        detail={"paused": {r: list(w) for r, w in sorted(paused.items())}},
                    )
                    if not nodes:
                        raise TranslateError(
                            "本轮所有单元都在等定译，一条也跑不了",
                            hint=(
                                "先看 `gametrans plan` 的 `progress.paused`，"
                                "把那些写法定下来再跑。"
                            ),
                        )

        # 阶段内按全局权重序取成员：这样"只有一个阶段"时，行为与不做调度完全一致。
        # `group_by` 非空时改成**按结构段分组**：同一组内的多次调用共享背景资料
        # （三级粒度：单元＝结构段，调用＝预算批）。
        phases = plan.phases[options.start_phase - 1 : (options.stop_after_phase or None)]
        # 先把节点按阶段分一次堆，再逐阶段处理。原先每个阶段都扫一遍全部节点，
        # 在长链上是 O(阶段数 × 节点数)（2,048 个单元的线性工程实测 190 毫秒；
        # 分堆之后 1 毫秒）。**顺序不变**：仍按 `nodes` 的全局权重序取成员。
        phase_index_of_region: dict[str, int] = {}
        for index, phase in enumerate(phases):
            for region in phase.regions:
                phase_index_of_region.setdefault(region, index)
        members_by_phase: dict[int, list[PathNode]] = {}
        for node in nodes:
            index = phase_index_of_region.get(region_of.get(node.node_id))
            if index is not None:
                members_by_phase.setdefault(index, []).append(node)
        # **这一轮真的会翻哪些槽位** —— 阶段切窄（`--start-phase` / `--stop-after-phase`）时
        # `nodes` 仍覆盖全图，但只有落在被选中阶段里的才开跑。台账**照样覆盖全图**
        # （见 `_order`：排列不该因为调度方式而变），但**计数与告警只算这一轮的** ——
        # 否则"跑一段、停下看看、再接着跑"每段都会报一万多条 `missing_in_response`、
        # `ok: false`，读数被噪音淹没（真靶实测第一段就 15,209 条）。
        in_run: set[str] = set()
        for group in members_by_phase.values():
            for node in group:
                if node.unit is None:
                    continue
                in_run.add(node.unit.id)
                in_run.update(str(key) for key in (node.unit.metadata.get("slot_keys") or []))
        phase_batches: list[tuple[str, list[tuple[list[PathNode], str]]]] = []
        in_scope = 0
        for index, phase in enumerate(phases):
            members_nodes = members_by_phase.get(index, [])
            in_scope += len(members_nodes)
            phase_batches.append((phase.name, self._plan_batches(members_nodes, ctx)))
        # **小批量并行**：把相邻的几层攒成一批。一层内部本来就没有先后约束，所以
        # 攒批只放弃"跨层看得见前面刚定下的叫法"这一点，换来的是轮数按批量降下来。
        group_sizes: list[int] = []
        if options.batch_units > 0 and len(phase_batches) > 1:
            merged: list[tuple[str, list[tuple[list[PathNode], str]]]] = []
            current: list[tuple[list[PathNode], str]] = []
            current_units = 0
            names: list[str] = []
            for name, batches in phase_batches:
                count = sum(len(group) for group, _key in batches)
                if current and current_units + count > options.batch_units:
                    merged.append(("+".join(names), current))
                    group_sizes.append(current_units)
                    current, current_units, names = [], 0, []
                current.extend(batches)
                current_units += count
                names.append(name)
            if current:
                merged.append(("+".join(names), current))
                group_sizes.append(current_units)
            phase_batches = merged
        total_batches = sum(len(batches) for _, batches in phase_batches)
        group_keys = {key for _, batches in phase_batches for _, key in batches if key}
        if group_keys:
            # 报告里露一手：这一轮有多少个"结构段"（单元），以及它们怎么被拆成调用
            report.metrics["unit_groups"] = len(group_keys)
        parallel_allowed = options.mode == "parallel" or (
            options.mode == "auto" and options.concurrency > 1
        )

        report.metrics.update(
            {
                "provider": provider.name,
                "target_language": options.target_language,
                "units": len(nodes),
                "units_in_scope": in_scope,
                "batches": total_batches,
                "phases": len(plan.phases),
                "phases_run": len(phases),
                "batch_units": options.batch_units,
                # 攒批之后真正串行跑了几轮，以及每轮几个单元（0 批时不报这一栏）
                "batch_groups": len(phase_batches) if group_sizes else 0,
                "batch_group_sizes": group_sizes,
                "plan_strategy": plan.strategy,
                "concurrency": options.concurrency if parallel_allowed else 1,
                "mode": "parallel" if parallel_allowed else "serial",
            }
        )
        if options.start_phase > 1:
            report.metrics["started_at_phase"] = options.start_phase
        if options.stop_after_phase:
            report.metrics["stopped_after_phase"] = options.stop_after_phase
            self._note_stop(plan, phases, options, report)
        by_unit: dict[str, TranslationArtifact] = {}
        outcomes: list[_BatchOutcome] = []
        for phase_name, batches in phase_batches:
            # 每批开跑前清空"这一批的申报"缓冲：批内并发，跑完统一交给钩子
            ctx.batch_terms.clear()
            ctx._batch_terms_seen.clear()
            for outcome, records in self._run_batches(
                provider, batches, ctx, phase_name, parallel_allowed
            ):
                by_unit.update(records)
                outcomes.append(outcome)
            if batch_hook is not None:
                # **批与批之间**（不是整轮跑完）：这一批的申报在这里交出去，攒批路线
                # 第 ② 步的"合并术语"就是在这一步发生的，下一批才可能真的用上它。
                batch_hook(phase_name, list(ctx.batch_terms))
                report.metrics["batch_flushes"] = (
                    report.metrics.get("batch_flushes", 0) + 1
                )
                # 钩子可能改了术语表（批准了这一批的申报）：**重新取一次资源版本**。
                # 必须**换一个 dict**，不能就地改 —— `resource_versions` 是按引用挂在
                # 每条译文上的，就地改会把前面那批的版本一起改成最后一版，于是
                # "这句是在哪一版术语表下翻的"就再也查不出来了。
                ctx.resource_versions = ctx.resources.versions()

        # 术语标签：这一轮把哪些"还没译名"的写法包成了 `⟦写法⟧`、现在还剩几条没定译。
        # 记账（不是拦）：盘上躺着标签的译文照样算"翻好了"，写回那一刻才渲染 ——
        # 所以这两件事都要看得见，否则"跑完了但一句话里没有名字"没人会知道。
        if ctx.term_tags:
            tagged_writings: list[str] = []
            for writings in ctx.term_tags.values():
                for writing in writings:
                    if writing not in tagged_writings:
                        tagged_writings.append(writing)
            report.metrics["term_tag_units"] = len(ctx.term_tags)
            report.metrics["term_tag_writings"] = tagged_writings
            report.metrics["term_tag_occurrences"] = sum(
                len(writings) for writings in ctx.term_tags.values()
            )
            report.metrics["term_tag_pending"] = [
                writing
                for writing in tagged_writings
                if not _target_of(ctx.resources.termbook, writing)
            ]
        # 修订提案是**跑的过程中**攒出来的，只能在批次跑完之后记账（早于此处的
        # 记账必然是 0，曾经真的这么错过一次）。
        if ctx.polish_proposals:
            report.metrics["memory_revision_proposals"] = len(ctx.polish_proposals)
            report.add_issue(
                "warnings",
                code="memory_revision_proposals",
                message=(
                    f"模型为上下文通顺提出了 {len(ctx.polish_proposals)} 条修订"
                    "（针对已定译的句子）—— **没有就地生效**，"
                    "批准之前仍按已定译沿用；要改请人或 agent 拍板"
                ),
                detail={"proposals": ctx.polish_proposals[:20]},
            )

        if interaction is not None:
            self._publish_progress(interaction, outcomes, provider.name)

        # 请求落盘的结果如实记账：写成了几个文件、还是失败了（失败不影响翻译）
        if ctx.dump is not None:
            report.metrics["requests_dumped"] = len(ctx.dump.written)
            if ctx.dump.failure:
                report.metrics["requests_dump_failed"] = ctx.dump.failure
                report.warn(
                    f"请求落盘失败（翻译不受影响）：{ctx.dump.failure}",
                    code="requests_dump_failed",
                )

        # 记忆的读写都攒到最后一次性落盘：单条写会退化成 O(n²) 的文件写
        self._flush_memory(ctx, provider)

        # 覆盖率兜底：没被任何批次碰过的 Unit 也要有 Task —— 否则"每条译文都能追溯到
        # 一条任务"就成了碰运气。
        covered = {task.unit_id for task in ctx.tasks}
        for node in nodes:
            if node.unit is None or node.unit.id in covered:
                continue
            ctx.tasks.append(
                self._finalize_task(
                    self.plan_task(node, ctx),
                    by_unit.get(node.unit.id),
                    ctx,
                    records=by_unit,
                    unit=node.unit,
                )
            )

        # 任务状态：调用方拿去做持久化（Guide §17：状态必须持久化，可中断、可恢复）
        if tasks_out is not None:
            tasks_out.extend(ctx.tasks)
        # 模型申报的实体：调用方拿去落成**候选**（模型产出 = hypothesis，批准要人）
        if terms_out is not None:
            terms_out.extend(ctx.declared_terms)
        # 调用流水：调用方拿去落盘
        if transcript_out is not None:
            transcript_out.extend(ctx.transcript)

        ordered = self._order(nodes, by_unit, provider, report, in_run=in_run)
        # 记录按**槽位**记账，任务按**单元**：先把"槽位 → 任务"摊平，再给记录盖 task_id
        task_ids: dict[str, str] = {}
        for task in ctx.tasks:
            task_ids.setdefault(task.unit_id, task.task_id)
        for node in nodes:
            unit = node.unit
            if unit is None or unit.id not in task_ids:
                continue
            for key in TranslateLayer._slot_keys_of(unit):
                task_ids.setdefault(key, task_ids[unit.id])
        for record in ordered:
            if not record.task_id:
                record.task_id = task_ids.get(record.unit_id, "")
        # **读数只算这一轮真会翻的那些**：台账覆盖全图（`_order` 的排列承诺），但"翻得怎么样"
        # 说的是这一轮的事 —— 把没排进这一轮的单元算进来，分段推进每段都会是"失败一万多条"。
        counted = [r for r in ordered if r.unit_id in in_run]
        report.metrics["units_ok"] = sum(1 for r in counted if r.is_usable)
        report.metrics["units_failed"] = sum(
            1 for r in counted if r.status is TranslationStatus.FAILED
        )
        report.metrics["units_needs_review"] = sum(
            1 for r in counted if r.status is TranslationStatus.NEEDS_REVIEW
        )
        report.metrics["translated_chars"] = sum(
            len(r.target) for r in counted if r.is_usable
        )
        report.metrics.setdefault("retry_attempts", 0)
        report.metrics.setdefault("retry_recovered", 0)
        report.metrics["memory_hits"] = sum(
            1 for r in counted if r.provenance.preset == "memory"
        )
        report.metrics["memory_reused_chars"] = sum(
            len(r.target) for r in counted if r.provenance.preset == "memory"
        )
        # 复用里有多少条是**别人的成品**（读进来的已有译文）—— 声明复用之后，
        # 这个数字就是"这些内容没花钱"的证据，也是唯一能看见它的地方（开关本身不持久化）
        report.metrics["memory_imported_hits"] = sum(
            1
            for r in ordered
            if (r.provenance.metadata or {}).get("memory_provider") == IMPORTED_PROVIDER
        )
        report.metrics["tasks"] = len(ctx.tasks)
        for state in TaskState:
            report.metrics[f"tasks_{state.value}"] = sum(
                1 for task in ctx.tasks if task.status is state
            )
        report.metrics["context_strategies"] = sorted(
            {task.retrieved_context.strategy for task in ctx.tasks if task.retrieved_context.strategy}
        )
        report.metrics["context_degraded"] = sum(
            1 for task in ctx.tasks if task.retrieved_context.degraded
        )
        # ---- 翻出来的东西有没有按术语书写------
        # 判据按**行**：这一行的写法在这条原文里出现、而它的全部译名在译文里一个都没有。
        # 报出来点名字，让人看一眼 —— 机器判不了"模型用了另一个中文名"（「沙希」和「咲」
        # 是不是同一个实体，只有人/模型知道），所以这是提醒不是断言。
        drifted = drift_report(counted, resources.termbook)
        if drifted:
            report.metrics["termbook_not_followed"] = {
                writing: len(units) for writing, units in sorted(drifted.items())
            }
            head = "、".join(
                f"{writing}（{len(units)} 条）"
                for writing, units in sorted(drifted.items())[:8]
            )
            samples = [
                unit for units in drifted.values() for unit in units
            ][:10]
            report.add_issue(
                "warnings",
                code="termbook_not_followed",
                message=(
                    f"有 {len(drifted)} 行术语在译文里没按定译写：{head}"
                    f"{'…' if len(drifted) > 8 else ''}。"
                    "译文里找不到那一行的译名 —— 可能是模型另起了名字，也可能是这句本来"
                    "就不必出现这个名字；逐条看一眼再决定（不要拿它反过来改术语书）"
                ),
                detail={"rows": {w: units[:20] for w, units in sorted(drifted.items())},
                        "samples": samples},
            )
        # 模型调用的计量进报告：token 与延迟是成本实验的唯一来源，事后估算不算数
        report.metrics["llm"] = ctx.calls.summary()
        return ordered

    def plan_for(
        self,
        graph: PathGraph,
        options: TranslateOptions | None = None,
        *,
        scheduler: Any = None,
        resources: ResourceLayer | None = None,
    ) -> TranslationPlan:
        """给出"这一次会按什么顺序翻"，让 agent 看得见、改得动。

        提取层理想完成后，依赖边是齐的，所以这个计划会真实反映先后约束。

        ``resources`` 给了才能按**知识边**算前驱（要读术语书）—— 没有它就只有控制边。
        """
        chosen = scheduler if scheduler is not None else default_scheduler()
        return chosen.plan(
            graph, flow=self.entity_flow(graph, resources, options) if resources else None
        )

    @staticmethod
    def entity_flow(
        graph: PathGraph,
        resources: ResourceLayer | None,
        options: TranslateOptions | None = None,
    ) -> Any:
        """按 :attr:`TranslateOptions.predecessors` 决定要不要算实体流图。

        ``control`` = 只看控制边（返回 ``None`` = 调度器走老算法）；``knowledge`` = 算。
        零模型成本、不落盘 —— 算一次是秒级，所以每次都现算，不做缓存。
        """
        value = str(getattr(options, "predecessors", "knowledge") or "knowledge")
        if value != "knowledge" or resources is None or graph is None:
            return None
        return entityflow.build(graph, resources.termbook)

    @staticmethod
    def _preflight(
        resources: ResourceLayer,
        nodes: list[PathNode],
        options: TranslateOptions,
        report: RunReport,
        *,
        graph: PathGraph | None = None,
        summaries: dict[str, dict] | None = None,
        workdir: Path | None = None,
    ) -> AssetPreflight:
        """开工前算一次"这一轮用得上几条资产"与"还有哪一步没做"，并记进报告。

        "有资产却一条都进不去"是要**拦**的方向性问题；"零资产"与"只有待批候选"
        允许开工，但必须在报告里点名 —— 真靶那次的形态正是后者，而它当时一声不响。

        步骤提醒（``inspection.workflow``）只报事实、不拦也不建议：做不做由用户和
        agent 自己决定，这里的职责只是让"某一步还没做"在开工时看得见。

        ``workdir`` 只喂给步骤提醒（判断"项目申报的事实带进工作区了没有"），不参与资产判定。
        """
        inspection = inspect_assets(
            resources,
            nodes,
            use_glossary=options.use_glossary,
            use_worldbook=options.use_worldbook,
            use_style=options.use_style,
            # 按范围跑（分块 / 续跑 / 阶段切分）时"资产没命中"是正常的
            scope_limited=options.unit_scope is not None or options.start_phase > 1
            or bool(options.stop_after_phase),
            graph=graph,
            summaries=summaries,
            unit_budget=int(options.unit_budget or 0),
            workdir=workdir,
        )
        # 挂单通道的两条步骤提醒（只陈述，不拦也不建议）：
        # ① 用 agent 额度跑批的人得知道"停在等待上不是卡死"；② 盘上有旧挂单未收
        #    这件事不随本轮 provider 变化 —— 它是工作区的事实。
        if options.provider == "agent":
            inspection.workflow.append(
                {
                    "code": "agent_transport",
                    "text": (
                        "provider=agent：每批会挂单到工作区 agent-requests/ 等 agent 作答"
                        " —— 跑批停在等待上不是卡死，答案用 agent submit 交回"
                    ),
                }
            )
        if workdir is not None:
            unclaimed = AgentQueue(Path(workdir) / QUEUE_DIRNAME).status()["unclaimed"]
            if unclaimed:
                inspection.workflow.append(
                    {
                        "code": "agent_queue_pending",
                        "text": f"挂单队列里有 {unclaimed} 条未收（工作区 agent-requests/）",
                    }
                )
        report.metrics["asset_preflight"] = inspection.verdict
        report.metrics["workflow_notes"] = [item["code"] for item in inspection.workflow]
        # 文案原样带上：调用方要能把它**印出来**（人看不到的提醒等于没有）。
        report.metrics["workflow_detail"] = [dict(item) for item in inspection.workflow]
        report.metrics["assets_usable_glossary"] = inspection.usable.get("术语", 0)
        report.metrics["assets_usable_worldbook"] = inspection.usable.get("设定", 0)
        # 待审更正不影响"能用几条"，但要在报告里看得见（采用之前书里一个字节都不动）
        report.metrics["assets_corrections_glossary"] = inspection.corrections.get("术语", 0)
        report.metrics["assets_corrections_worldbook"] = inspection.corrections.get("世界书", 0)
        report.metrics["assets_hit_glossary"] = inspection.hit.get("术语", 0)
        report.metrics["assets_hit_worldbook"] = inspection.hit.get("设定", 0)
        if inspection.verdict == VERDICT_OK:
            return inspection
        severity = "failed" if inspection.blocking else "warnings"
        report.add_issue(
            severity,
            code=f"asset_preflight_{inspection.verdict}",
            message=inspection.summary(),
            detail=inspection.to_dict(),
        )
        return inspection

    @staticmethod
    def _note_stop(
        plan: TranslationPlan,
        phases: list[Any],
        options: TranslateOptions,
        report: RunReport,
    ) -> None:
        """第一轮跑完就停：说清"停在哪儿、还有几个阶段没跑、接着跑的命令是什么"。

        没有这句话，一次"停在第一个阶段"的跑批在报告里与"跑完了"分不开 ——
        而这正是本轮的要点：**停下来是为了让人或 agent 给资产拍板**。
        """
        remaining = len(plan.phases) - len(phases)
        names = "、".join(phase.name for phase in phases) or "（空）"
        resume = options.stop_after_phase + 1
        report.add_issue(
            "warnings",
            code="translation_stopped_for_review",
            message=(
                f"第一轮（阶段 {names}）跑完就停了：还有 {remaining} 个阶段没跑。"
                "现在该做的是**给这一轮产出的资产拍板**"
                "（`gametrans resource term list` 看行、`resource.term.pending.list` 看"
                "待审更正 → `resource.term.pending.adopt` 采用，人或 agent 都可以），然后接着跑："
                f"`gametrans translate --start-phase {resume}`（第一轮不会重问一次模型）"
            ),
            detail={
                "phases_run": [phase.name for phase in phases],
                "phases_remaining": [phase.name for phase in plan.phases[len(phases) :]],
                "resume_command": f"gametrans translate --start-phase {resume}",
            },
        )

    def _plan_batches(
        self, nodes: list[PathNode], ctx: _RunContext
    ) -> list[tuple[list[PathNode], str]]:
        """把一批节点切成"调用批"，返回 ``(节点, 组键)``；组键为空表示没有分组。

        **这是三级粒度中间那一级的落点**：组键 = 引擎结构坐标（如 Ren'Py 的 `label`、
        RPGM 的地图）。同一组的调用**共享背景资料**；``batch_size`` 只决定这一组要拆成
        几次调用 —— 预算压力因此不改变单元边界。
        """
        size = max(1, ctx.options.batch_size)
        level = ctx.options.group_by
        # 一次请求里的条目**按文档顺序**排（不是权重序）：顺序保持是理论里的硬约束，
        # 也是"一个单元"能成立的前提。权重序只决定先翻哪一段。
        nodes = sorted(nodes, key=lambda node: ctx.doc_order.get(node.node_id, 0))
        if not level:
            return [(nodes[i : i + size], "") for i in range(0, len(nodes), size)]
        if not nodes:
            # 这个阶段里没有本次范围内的单元（分块翻译时很常见）：空就是空，
            # 不是"适配器不认层级"—— 两者混为一谈会让正常的分块跑批报错。
            return []
        if ctx.grouping is None:
            raise TranslateError(
                f"声明了按 {level!r} 分组，但适配器没有提供分组器",
                hint="分组层级必须由适配器申报（grouping_levels）；不申报就别声明 group_by。",
            )
        units = [node.unit for node in nodes if node.unit is not None]
        keys = ctx.grouping(units, level) or {}
        if not keys:
            raise TranslateError(
                f"适配器不认分组层级 {level!r}",
                hint="层级名要按适配器申报的来；不认识就报错，不静默退回「不分组」。",
            )
        grouped: dict[str, list[PathNode]] = {}
        for node in nodes:
            key = keys.get(node.unit.id) if node.unit is not None else None
            # 拿不到组键的单位自成一組：它不该被塞进别人的结构段里
            grouped.setdefault(key or f"unit:{node.node_id}", []).append(node)
        batches: list[tuple[list[PathNode], str]] = []
        for key, members in grouped.items():
            for index in range(0, len(members), size):
                batches.append((members[index : index + size], key))
        return batches

    def _run_batches(
        self,
        provider: LLMProvider,
        batches: list[tuple[list[PathNode], str]],
        ctx: _RunContext,
        phase_name: str,
        parallel_allowed: bool,
    ) -> list[tuple[_BatchOutcome, dict[str, TranslationArtifact]]]:
        """跑完一个阶段里的全部批次。阶段内部才谈得上并行。"""
        if not batches:
            return []

        # 同一组的调用共享背景资料：这一组的 Task 只算一次，供组内每一批复用。
        group_nodes: dict[str, list[PathNode]] = {}
        for members, key in batches:
            if key:
                group_nodes.setdefault(key, []).extend(members)
        group_tasks = {
            key: [self.ready_task(node, ctx) for node in members if node.unit is not None]
            for key, members in group_nodes.items()
        }

        def work(index: int, batch: tuple[list[PathNode], str]):
            batch_nodes, group_key = batch
            # 批边界 = 可见性的快照点：批内只看得到"这一批开始前已提交"的东西。
            # 记忆表内部自己有锁；这里只是把"这一轮已产出"的字典复制一份。
            ctx.memory_snapshot = ctx.resources.memory.snapshot()
            ctx.memory_cache_snapshot = dict(ctx.memory_cache)
            records, pending, error = self._split_memory(batch_nodes, ctx)
            task_of: dict[str, TranslationTask] = {}
            if ctx.options.round_lines > 0:
                # **多轮形状**：一个单元一个会话，轮内串行、单元间照旧并行。
                # 与下面那条路（独立请求 + 窗口）互斥：给了 round_lines 就以它为准。
                for node in pending:
                    rounds_error = self._run_as_rounds(
                        provider,
                        node,
                        records,
                        ctx,
                        self._accept_slot,
                        phase=phase_name or "translate",
                        lines=ctx.options.round_lines,
                        context_tasks=group_tasks.get(group_key),
                        dump=ctx.dump,
                        task_of=task_of,
                    )
                    error = rounds_error or error
            else:
                for run in self._iter_runs(pending, records, ctx):
                    request = self.build_request(
                        run,
                        ctx.resources,
                        ctx.options,
                        ctx.graph,
                        project_id=ctx.project_id,
                        tasks=None,
                        calls=ctx.calls,
                        phase=phase_name or "translate",
                        context_tasks=group_tasks.get(group_key),
                        transcript=ctx.transcript,
                        dump=ctx.dump,
                        tagged=ctx.term_tags,
                    )
                    ctx.requests.append(request)
                    task_of.update({task.unit_id: task for task in request.tasks})
                    called, error = self._invoke(provider, request, run, ctx, self._accept_slot)
                    records.update(called)
                    self._repair(provider, run, records, ctx, self._accept_slot)
                    if ctx.options.memory_reuse_mode == "polish":
                        TranslateLayer._collect_polish_proposals(run, records, ctx)
                    # **一次装不下就自动转多轮**（不用人记着开开关）：
                    # 撞输出上限的那一轮是整批报废的形态，代价由 read 承担。
                    if (
                        self._truncated(ctx, error)
                        and len(run) == 1
                        and run[0].unit is not None
                    ):
                        unit = run[0].unit
                        ctx.report.warn(
                            f"{unit.id} 一次装不下（撞输出上限）→ 自动转多轮，"
                            f"每轮最多 {DEFAULT_ROUND_LINES} 条",
                            code="auto_rounds_on_truncation",
                            ref=unit.id,
                        )
                        # 那一次失败的记录不该留在报告里：它已经被多轮替代了
                        ctx.report.failed = [
                            issue
                            for issue in ctx.report.failed
                            if not (
                                issue.ref in {unit.id, *(unit.metadata.get("slot_keys") or [])}
                                and issue.code in {"constraint_violation", "provider_error", "missing_in_response"}
                            )
                        ]
                        error = self._run_as_rounds(
                            provider,
                            run[0],
                            records,
                            ctx,
                            self._accept_slot,
                            phase=phase_name or "translate",
                            lines=DEFAULT_ROUND_LINES,
                            context_tasks=group_tasks.get(group_key),
                            dump=ctx.dump,
                            task_of=task_of,
                        ) or None
            # 记忆命中的那些也有任务 —— 它们同样要过校验，同样要能追溯。
            # 但它们不再需要"相近译法"：这一条已经精确命中，再扫一遍全部记忆只是白花钱
            # （真实工程上第二轮会因此慢一个数量级）。
            for node in batch_nodes:
                if node.unit is None or node.unit.id in task_of:
                    continue
                if node.unit.id in records:
                    task_of[node.unit.id] = self.ready_task(
                        node, ctx, skip_layers=("memory",)
                    )
            return index, batch_nodes, records, error, task_of

        indexed = list(enumerate(batches))
        if parallel_allowed and len(batches) > 1:
            with ThreadPoolExecutor(max_workers=ctx.options.concurrency) as pool:
                raw = list(pool.map(lambda pair: work(*pair), indexed))
        else:
            raw = [work(index, batch) for index, batch in indexed]

        results: list[tuple[_BatchOutcome, dict[str, TranslationArtifact]]] = []
        for index, batch, records, error, task_of in raw:
            # 这一批里"整单元的译文对不对得上"——**逐条槽位判不出来的那半**。
            # `_invoke` 只看得见它自己那一次请求的槽位，而这一批里还有记忆命中的槽位
            # （它们不在 `records` 里，所以躲过了那一关）。两处按同一份判据合起来查一次。
            correspondence_rejected = self._reject_mismatched_records(batch, records, ctx)
            outcome = _BatchOutcome(
                index=index,
                size=len(batch),
                ok=sum(1 for r in records.values() if r.is_usable),
                error=error,
                phase=phase_name,
            )
            # 状态机跑完这一批：每条都留下可解释的历史（Guide §17、§22）
            for node in batch:
                if node.unit is None:
                    continue
                task = task_of.get(node.unit.id) or self.plan_task(node, ctx)
                ctx.tasks.append(
                    self._finalize_task(
                        task,
                        records.get(node.unit.id),
                        ctx,
                        records=records,
                        unit=node.unit,
                    )
                )
            if correspondence_rejected:
                # 任务的终态是"这一批收下来的样子"，但**置留要落在槽位记录上** ——
                # 判据命中的那几条已经在 `_reject_mismatched_records` 里改成待复核，
                # 这里只把读数与点名补齐（与 `_invoke` 那条路同一套 metrics/warning）。
                self._report_correspondence(correspondence_rejected, ctx)
            results.append((outcome, records))
        return results

    def _reject_mismatched_records(
        self,
        batch: list[PathNode],
        records: dict[str, TranslationArtifact],
        ctx: _RunContext,
    ) -> dict[str, dict[str, Any]]:
        """批级对账：一个单元的**全部**槽位（含记忆命中的）一起看有没有"译文挂错原文"。

        与 `_invoke` 里那一次是**同一条判据**（`_unit_correspondence_issues`），差别只在
        看得见的槽位更多：记忆命中的槽位不经过模型、也不在这一次的 `records` 里，
        但它们的译文同样要接受"这像不像原文的译文"这一问（真靶抓到的正是记忆里那几条）。

        返回 ``{槽位: 账}``（调用方拿去出读数与告警）。
        """
        found: dict[str, dict[str, Any]] = {}
        for node in batch:
            unit = node.unit
            if unit is None:
                continue
            keys = [
                key for key in (self._slot_keys_of(unit) or [unit.id]) if records.get(key)
            ]
            if not keys:
                continue
            # ⚠️ 用 `update` 而不是 `setdefault`：**同一个槽位可能出现在两个单元里**
            # （真靶上 `The station was empty at this hour.` 在 `start` 与 `flashback`
            # 各有一条），拿"谁先到"决定收哪一条账会把后面的静默丢掉。
            found.update(
                _unit_correspondence_issues(
                    unit,
                    keys,
                    records,
                    lambda key: self._slot_source(unit, key),
                )
            )
        for key, detail in found.items():
            # 记账**不看当时的状态**：这一条可能已经被逐条那半边判过（那也说明它进不了
            # 记忆，但它进 `new_memory` 的时机在收下那一刻、早于任何一条判据）。
            ctx.mismatched_sources[key] = str(
                detail.get("source") or (records.get(key).source if records.get(key) else "")
            )
            record = records.get(key)
            if record is None or record.status is not TranslationStatus.OK:
                continue
            records[key] = replace(
                record,
                status=TranslationStatus.NEEDS_REVIEW,
                error=(
                    "译文与原文对不上（"
                    + {
                        "duplicate_across_slots": "同一单元里两条原文不同的句子译文一字不差",
                        "target_pasted_from_longer": "这条译文整段埋在另一条的译文里",
                    }.get(str(detail.get("reason")), str(detail.get("reason")))
                    + "）"
                ),
            )
        return found

    @staticmethod
    def _report_correspondence(
        found: dict[str, dict[str, Any]], ctx: _RunContext
    ) -> None:
        """把"译文挂错原文"这件事说出去：metrics + 一条 warning（命令行与面板都看得见）。"""
        ctx.report.metrics["correspondence_failed_records"] = (
            ctx.report.metrics.get("correspondence_failed_records", 0) + len(found)
        )
        units = ctx.report.metrics.setdefault("correspondence_failed_units", [])
        for key in sorted(found):
            unit_id = str(found[key].get("unit_id") or "")
            if unit_id and unit_id not in units:
                units.append(unit_id)
        ctx.report.add_issue(
            "failed",
            code="correspondence_violation",
            message=(
                f"{len(found)} 条译文与原文对不上（同一单元里两条原文不同的句子译文一字不差／"
                "一条译文整段埋在另一条里）—— 这几条不写回、不进记忆，逐条看一眼："
                + "、".join(sorted(found)[:8])
            ),
            detail={"records": {key: found[key] for key in sorted(found)[:20]}},
        )

    @staticmethod
    def _finalize_task(
        task: TranslationTask,
        record: TranslationArtifact | None,
        ctx: _RunContext,
        *,
        records: dict[str, TranslationArtifact] | None = None,
        unit: Any = None,
    ) -> TranslationTask:
        """把一条任务推到它的终态，并把状态历史留在 provenance 里。

        FAIL 不等于程序崩溃（Guide §16）：没产出译文进 ``BLOCKED``，结构没过进
        ``NEEDS_REVIEW``，走过重试的如实记下 ``RETRYING``。

        记录是**按槽位**的（一个单元含多句），所以任务是"整个单元都落下"才算完成：
        任一条槽位没译文进 ``BLOCKED``，任一条没过结构闸门进 ``NEEDS_REVIEW``。
        """
        statuses = []
        if records is not None and unit is not None:
            keys = TranslateLayer._slot_keys_of(unit) or [unit.id]
            statuses = [
                records[key].status for key in keys if records.get(key) is not None
            ]
            if len(statuses) < len(keys):
                statuses.append(TranslationStatus.FAILED)
        elif record is not None:
            statuses = [record.status]
        if task.status is TaskState.DISCOVERED:
            task = task.advance(TaskState.READY)
        task = task.advance(TaskState.TRANSLATING)
        task = task.advance(TaskState.VALIDATING)
        # 每重试一次就多走一轮 RETRYING → TRANSLATING → VALIDATING：
        # 状态历史与 attempts 要如实反映"又问过几次"
        for _ in range(ctx.retries.get(task.unit_id, 0)):
            task = task.advance(TaskState.RETRYING)
            task = task.advance(TaskState.TRANSLATING)
            task = task.advance(TaskState.VALIDATING)
        if not statuses:
            return task.advance(TaskState.BLOCKED, note="这一轮没有产出译文")
        if TranslationStatus.FAILED in statuses:
            note = "provider 没有给出译文"
            if records is not None and unit is not None:
                missing = [
                    key
                    for key in (TranslateLayer._slot_keys_of(unit) or [unit.id])
                    if records.get(key) is None
                    or records[key].status is TranslationStatus.FAILED
                ]
                note = f"{len(missing)} 条槽位没有译文"
            return task.advance(TaskState.BLOCKED, note=note)
        if TranslationStatus.NEEDS_REVIEW in statuses:
            return task.advance(TaskState.NEEDS_REVIEW)
        return task.advance(TaskState.COMPLETED)

    @staticmethod
    def _wait_before_retry(
        attempt: int, error: ProviderError, ctx: _RunContext
    ) -> None:
        """限流之后**等一会儿**再问：不是忙等，也不能不等。

        等多久的优先级：服务端明说的 `Retry-After` → 退避表。测试把退避表改成 0
        （见 ``PROVIDER_RETRY_BACKOFF``），生产上默认 2s / 8s / 20s。
        """
        index = min(max(attempt - 1, 0), len(PROVIDER_RETRY_BACKOFF) - 1)
        delay = error.retry_after or (
            PROVIDER_RETRY_BACKOFF[index] if PROVIDER_RETRY_BACKOFF else 0.0
        )
        if delay <= 0:
            return
        ctx.report.warn(
            f"provider 暂时性故障（{error.message}）：{delay:g} 秒后重试第 {attempt} 次",
            code="provider_retry",
        )
        time.sleep(delay)

    def _iter_runs(
        self,
        pending: list[PathNode],
        records: dict[str, TranslationArtifact],
        ctx: _RunContext,
    ) -> Iterator[list[PathNode]]:
        """把"这一批要问的东西"**按需**排成几次请求，按文档顺序（生成器）。

        默认（``unit_budget <= 0``）就是一次 —— "一个单元 = 一次请求"是设计的默认形状。

        但**单次请求有物理上限**：真靶实测（该工程 / GLM-5.3-Flash）一个 90 条槽位的
        单元（`script`）无论怎么重试都过不去 —— 81 条那次在 **308 秒**处被服务端回
        HTTP 400，而同一批切一半（41 条）**304 秒跑完、41 条全回来**；同轮 `start`
        23 条 241 秒、`frozeupsaki` 16 条 157 秒（≈7.4 秒/条）。所以上限是**时间**，
        不是长度：慢模型上长单元必须切开，否则整场戏一条都拿不到。

        切法守住四件事：

        * **顺序不变**：切出来的段按文档顺序**依次**发，不是并发；
        * **状态传下去**（IN/OUT）：第 k+1 段看得见第 k 段已经产出的译文 —— 从
          ``records`` 里**现读**（所以是生成器，不能提前把几段都拼好），摆进
          ``context_slot_targets``（它本来就是"命中句在请求里的形状"，见 R67），
          于是同一场戏里的称呼不会因为被切开而各自为政；
        * **身份不变**：切的是**槽位**，单元身份、任务、校验、写回全部照旧；
        * **载荷也要收窄**：请求里**只列这一段 + 它前面的窗口**，不是整个单元。

        预算管的是"**一次请求里列几条**"，所以按**载荷**算、不是按"还要问几条"：
        一个已经翻了大半、只剩 7 条待问的单元，若仍把 90 条摊在请求里，既慢又更容易
        被服务端的内容过滤整批拒掉（真靶：GLM 的内容过滤回 `code 1301`，一条露骨
        台词就能让同批那些无辜的句子一起拿不到译文）。
        """
        budget = int(ctx.options.unit_budget or 0)
        if not pending:
            return
        if budget <= 0 or all(
            len(TranslateLayer._payload_keys(node.unit)) <= budget
            for node in pending
            if node.unit is not None
        ):
            yield pending
            return
        split_units: set[str] = set()
        split_chunks = 0
        for node in pending:
            unit = node.unit
            if unit is None:
                continue
            asked = TranslateLayer._slot_keys_of(unit)
            full = TranslateLayer._payload_keys(unit)
            if len(full) <= budget:
                yield [node]
                continue
            split_units.add(unit.id)
            base = dict((unit.metadata or {}).get("context_slot_targets") or {})
            chunks = TranslateLayer._budget_chunks(unit, full, asked, budget)
            split_chunks += len(chunks)
            for chunk in chunks:
                produced = {
                    key: records[key].translated_text
                    for key in full[: full.index(chunk[0])]
                    if key in records
                }
                # 窗口＝紧挨着这一段前面的那些句子（已经产出的带译文，见 IN/OUT）
                head = full.index(chunk[0])
                window = full[max(0, head - budget) : head] + chunk
                yield [
                    TranslateLayer._with_reused_targets(
                        node, {**base, **produced}, chunk, window=window
                    )
                ]
        if split_units:
            ctx.report.metrics["units_split"] = (
                ctx.report.metrics.get("units_split", 0) + len(split_units)
            )
            # 按结构切出来的段数（真靶 act25 = 1,972 条槽位 / 27 个结构块）：
            # "一个大节点被切成几段"是速度与成本的第一手读数。
            ctx.report.metrics["split_chunks"] = (
                ctx.report.metrics.get("split_chunks", 0) + split_chunks
            )

    @staticmethod
    def _truncated(ctx: _RunContext, error: str | None) -> bool:
        """这一轮是不是**撞输出上限**被截断的（而不是别的原因答坏了）。

        判据两条，宁可保守：服务端给的 `finish_reason=length`；或错误里带着它
        （provider 把 `finish_reason` 写进了错误提示）。判错的代价只是"多切一刀"，
        而漏判的代价是整轮报废 —— 所以宁可多判。
        """
        records = ctx.calls.records()
        if records and str(records[-1].finish_reason or "") == "length":
            return True
        return "finish_reason=length" in str(error or "")

    @staticmethod
    def _assistant_message(keys: list[str], records: dict[str, TranslationArtifact]) -> str:
        """把这一轮**收下的译文**拼成模型自己的回复（下一轮的历史里要摆出来）。

        为什么不原样留模型的原始回复文本：那要 provider 把 `response_raw` 交上来，
        而这条路径上并不是每种 provider 都有流水槽位；用**收下的译文**重建，语义一致、
        一定可解析，而且天然过滤掉多回/乱序的那些（历史只摆真正采用了的）。
        """
        rows = [
            {"unit_id": key, "target": records[key].translated_text}
            for key in keys
            if key in records and records[key].translated_text
        ]
        return json.dumps({"translations": rows}, ensure_ascii=False)

    def _round_chunks(self, unit: Any, keys: list[str], lines: int) -> list[list[str]]:
        """把"还要问的条"切成轮：**优先落在结构块边界上**，块比一轮还大时才在块内切。

        与 `_budget_chunks` 同一条口径（块内不切），差别只在预算的含义：那边是
        "一次请求列几条"，这边是"一轮要它回几条"。块边界优先是**偏好不是规则**：
        切不开就按条数硬切，正确性不受影响。
        """
        return TranslateLayer._budget_chunks(unit, keys, keys, max(1, lines))

    def _run_as_rounds(
        self,
        provider: LLMProvider,
        node: PathNode,
        records: dict[str, TranslationArtifact],
        ctx: _RunContext,
        accept_slot: Callable[..., TranslationArtifact],
        *,
        phase: str,
        lines: int,
        context_tasks: list[TranslationTask] | None = None,
        dump: RequestDump | None = None,
        task_of: dict[str, TranslationTask] | None = None,
    ) -> str | None:
        """把一个单元**分多轮问完**：同一会话、历史只追加、撞上限就再切一刀。

        返回最后一次的错误（成功就是 ``None``）。三条要点：

        * **轮内串行**（模型要看得见自己刚翻的），**单元间并行**交给上层；
        * 每轮**只发这一轮的句子**，不写"继续"、不重发前文（历史带着）；
        * 撞 `finish_reason=length` 就把这一轮**对半再切**，而不是丢整场 —— 自适应降级；
          切到只剩一条还撞上限，就如实记失败（那是这条文本本身的问题，不是轮长）。
        """
        unit = node.unit
        assert unit is not None
        history: list[dict[str, str]] = []
        queue = self._round_chunks(unit, TranslateLayer._slot_keys_of(unit), lines)
        rounds = splits = 0
        error: str | None = None
        while queue:
            chunk = queue.pop(0)
            rounds += 1
            round_node = TranslateLayer._with_reused_targets(node, {}, chunk, window=chunk)
            request = self.build_request(
                [round_node],
                ctx.resources,
                ctx.options,
                ctx.graph,
                project_id=ctx.project_id,
                tasks=None,
                calls=ctx.calls,
                phase=phase or "translate",
                context_tasks=context_tasks,
                transcript=ctx.transcript,
                dump=dump,
                tagged=ctx.term_tags,
                history=history,
            )
            ctx.requests.append(request)
            if task_of is not None:
                task_of.update({task.unit_id: task for task in request.tasks})
            called, error = self._invoke(provider, request, [round_node], ctx, accept_slot)
            if self._truncated(ctx, error) and len(chunk) > 1:
                # 自适应降级：这一轮对半再切，历史不动（这一轮没有采用任何东西）
                half = len(chunk) // 2
                queue = [chunk[:half], chunk[half:]] + queue
                splits += 1
                continue
            if error:
                break
            records.update(called)
            self._repair(provider, [round_node], records, ctx, accept_slot)
            # 历史：这一轮的原文（请求正文）+ 这一轮收下的译文（模型自己的回复）
            history.append({"role": "user", "content": request.render()})
            history.append({"role": "assistant", "content": self._assistant_message(chunk, called)})
        # 两个读数分开：`rounds_total` = 这次跑批一共发了几次"轮"（多轮形状下就是调用数），
        # `units_rounds` = 其中**真的分了轮**的单元数（>1 轮的才是"长单元"）。
        # 合成一个数会让人以为每个单元都分了轮。
        ctx.report.metrics["rounds_total"] = ctx.report.metrics.get("rounds_total", 0) + rounds
        if rounds > 1:
            ctx.report.metrics["units_rounds"] = ctx.report.metrics.get("units_rounds", 0) + 1
        ctx.report.metrics["rounds_split_on_length"] = (
            ctx.report.metrics.get("rounds_split_on_length", 0) + splits
        )
        # 中间那些"撞上限→对半再切"的尝试不该留在失败清单里：它们已经被后面的轮次替代了。
        # 只有**整段问完**时才清 —— 没问完就得如实留着（那才是真失败）。
        asked = TranslateLayer._slot_keys_of(unit)
        if all(key in records for key in asked):
            refs = {unit.id, *asked}
            ctx.report.failed = [
                issue
                for issue in ctx.report.failed
                if not (
                    issue.ref in refs
                    and issue.code
                    in {"constraint_violation", "provider_error", "missing_in_response"}
                )
            ]
        return error

    @staticmethod
    def _budget_chunks(
        unit: Any, full: list[str], asked: list[str], budget: int
    ) -> list[list[str]]:
        """把"这一段要问的条"按**节点内部的结构块**切成几次请求。

        切法是"块内不切、块攒到预算封顶就切一刀"，而不是按条数硬剁：

        * 结构块 = 单元里的一个 `label/menu[n]`（适配层申报的坐标，见
          `core/units.py` 写进 `metadata["blocks"]` 的那一份）；
        * 块比预算还大时（一个菜单分支里几百句）才按预算再切 —— 物理上限还是要守；
        * 没有块信息（老图 / 适配层没申报）时退回"按条数切"，与从前逐字一致。

        这样切出来的段边界落在**引擎自己的结构上**，也是"节点内部有结构、
        可以按它分割"这句话的落点。
        """
        blocks = [
            block
            for block in ((unit.metadata or {}).get("blocks") or [])
            if isinstance(block, dict)
        ]
        if not blocks:
            return [asked[index : index + budget] for index in range(0, len(asked), budget)]
        wanted = set(asked)
        chunks: list[list[str]] = []
        current: list[str] = []
        for block in blocks:
            start = int(block.get("start") or 0)
            count = int(block.get("count") or 0)
            keys = [key for key in full[start : start + count] if key in wanted]
            if not keys:
                continue
            if len(keys) > budget:
                if current:
                    chunks.append(current)
                    current = []
                # 块比预算还大时按**均分**再切，不按固定步长剁出"只剩一条"的小请求
                # （每次调用都有固定开销：系统提示 + 背景资料，1 条的请求照样要付）。
                parts = -(-len(keys) // budget)
                step = -(-len(keys) // parts)
                for index in range(0, len(keys), step):
                    chunks.append(keys[index : index + step])
                continue
            if len(current) + len(keys) > budget:
                chunks.append(current)
                current = []
            current.extend(keys)
        if current:
            chunks.append(current)
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _payload_keys(unit: Any) -> list[str]:
        """单元**载荷里全部槽位**的键（文档顺序）—— 判断"一次请求列几条"用它。

        :meth:`_slot_keys_of` 给的是"**还要问**哪几条"（记忆命中的已经被摘掉），
        两者在"翻了大半的单元"上差得很远：预算管的是请求里**列出来**的条数。
        """
        entries = ((getattr(unit, "locator", None).payload or {}).get("slots") or [])
        keys = [str(entry.get("slot_key") or "") for entry in entries]
        keys = [key for key in keys if key]
        return keys or TranslateLayer._slot_keys_of(unit)

    def _invoke(
        self,
        provider: LLMProvider,
        request: TranslationRequest,
        nodes: list[PathNode],
        ctx: _RunContext,
        accept_slot: Callable[
            [LLMProvider, str, PathNode, str, _RunContext, str], TranslationArtifact
        ],
    ) -> tuple[dict[str, TranslationArtifact], str | None]:
        """调用一次 provider 并把结果**逐条槽位**收下来。异常一律降级成 failed 记录。

        为什么按槽位收：一次请求装的是一个单元（一场戏）的几十到几百句，译文要落回
        **每一条槽位**上。按单元收就只能把整段往每条槽位里写，那是静默错位。
        """
        error: ProviderError | None = None
        attempts = 0
        budget = max(1, int(ctx.options.max_attempts or 1))
        while True:
            try:
                results = provider.translate(request)
                break
            except ProviderError as exc:
                # **暂时性故障自己重试**：限流（429）与 5xx 等一会儿再来一次就该好。
                # 真靶上 `script` 那 90 条槽位就是被一次 429 全丢掉的：整批落成
                # "没有译文"、`retry_attempts=0`、命令行照样报"翻译完成"。
                attempts += 1
                if not exc.retryable or attempts >= budget:
                    error = exc
                    break
                ctx.report.metrics["retry_attempts"] = (
                    ctx.report.metrics.get("retry_attempts", 0) + 1
                )
                TranslateLayer._wait_before_retry(attempts, exc, ctx)
            except Exception as exc:  # noqa: BLE001 - 一批的意外不该拖垮整轮
                message = f"{type(exc).__name__}: {exc}"
                return self._fail_all(nodes, provider, ctx, message, "provider_crash"), message
        if error is not None:
            # **答坏了**（回了一份读不出来的响应）与**连不上**要分开：
            # 前者重问一次常常就好，一次坏回复会废掉整批；后者重试只是白花钱。
            malformed = "不是合法 JSON" in error.message or "缺少 translations 数组" in error.message
            records = self._fail_all(nodes, provider, ctx, error.render(), "provider_error")
            if malformed:
                ctx.malformed_units.update(node.unit.id for node in nodes if node.unit)
            return records, error.message
        if attempts:
            ctx.report.metrics["retry_recovered"] = (
                ctx.report.metrics.get("retry_recovered", 0) + 1
            )

        returned: dict[str, TranslationResult] = {}
        # 短编号只活在线路上；内部分类前缀（`id:` / `keyed:`）同样只是我们自己的写法 ——
        # 实测模型会把前缀规范化掉，逐字匹配就会把**整批译文静默丢掉**。所以按请求里的
        # 对照表 + 别名还原成完整槽位身份，对不上的**如实报出来**（不猜、不吞）。
        unmatched: list[str] = []
        for result in results:
            resolved = request.resolve_id(result.unit_id)
            if resolved is None:
                unmatched.append(result.unit_id)
                continue
            returned[resolved] = result
        # 抄坏一个字符、被**唯一近失**认回来的那几条：认下来了，但要出现在报告里 ——
        # "猜的"和"抄对的"不是一回事，读数上得能分开（真靶 2026-09-29）。
        near_miss = request.near_miss_hits()
        if near_miss:
            ctx.report.metrics["response_ids_near_miss"] = (
                ctx.report.metrics.get("response_ids_near_miss", 0) + len(near_miss)
            )
            ctx.report.add_issue(
                "warnings",
                code="response_ids_near_miss",
                message=(
                    f"{len(near_miss)} 条译文的 unit_id 与待译条目的 id **差一个字符**"
                    "（模型抄短哈希时多打/少打了一个字符），按唯一近失认回来了 —— "
                    "逐条见 detail；两个候选都能对上时不会认（宁可报没拿到）"
                ),
                detail={
                    "pairs": [f"{raw} → {identity}" for raw, identity in near_miss[:20]],
                    "count": len(near_miss),
                },
            )
        if unmatched:
            ctx.report.add_issue(
                "warnings",
                code="response_ids_unmatched",
                message=(
                    f"{len(unmatched)} 条译文的 unit_id 对不上任何待译条目"
                    "（模型多半把 `id:` / `keyed:` 这类前缀规范化掉了），"
                    "这些译文没有采用 —— 要的是哪些 id 见 detail"
                ),
                detail={
                    "returned": unmatched[:20],
                    "expected": [item.unit_id for item in request.items][:20],
                    "count": len(unmatched),
                },
            )
        # **缺一条 → 只重问，不回填**（真靶 2026-09-29 实证：模型少回一条时，后面每条
        # 拿到的是**下一条**的译文，一路滑到末尾 —— 25 条错配、以后每轮从记忆里复现）。
        # 为什么不能只标"这条没拿到"就完事：同一份响应里"没拿到"与"挂错"是**同一个原因**
        # 的两种表现，而挂错的那些长度说得通、结构一个字没坏，事后分不出来（实测：长度类
        # 判据在正常调用上就有 2%–33% 的噪声，那次平移只排到 26.5% —— 没有分辨率）。
        # 所以判据放在**响应级**：要了几条就得回几条，少一条就整份作废重问。
        # 例外只有一种：**少的是最后几条**（`tail_missing`）—— 那种"少"不改变前面的
        # 一一对应，照旧逐条收，缺的那几条如实记 failed。
        wanted = [item.unit_id for item in request.items if item.expects_answer]
        missing = [unit_id for unit_id in wanted if unit_id not in returned]
        if missing and not TranslateLayer._missing_at_tail(request, returned):
            message = (
                f"要回 {len(wanted)} 条、只回了 {len(wanted) - len(missing)} 条，"
                f"而且少的不是尾部（缺：{'、'.join(missing[:5])}）—— 这种形状下"
                "模型极易把后面每条都往前挪一位，所以整份响应作废、重问一次"
            )
            ctx.report.add_issue(
                "failed",
                code="response_incomplete_shift_risk",
                message=message,
                detail={
                    "returned": len(wanted) - len(missing),
                    "expected": len(wanted),
                    "missing": missing[:20],
                },
            )
            ctx.malformed_units.update(node.unit.id for node in nodes if node.unit)
            return self._fail_all(nodes, provider, ctx, message, "response_shift_risk"), message
        self._collect_declared(results, ctx, nodes)
        records: dict[str, TranslationArtifact] = {}
        for node in nodes:
            unit = node.unit
            if unit is None:
                continue
            fingerprint = TranslateLayer._fingerprint(ctx, node.node_id, unit.source)
            keys = self._slot_keys_of(unit)
            if not keys:
                # 没有槽位清单的单元（老数据/夹具）：整条收下
                result = returned.get(unit.id)
                if result is None or not result.target.strip():
                    records[unit.id] = self._missing_record(provider, node, ctx, fingerprint)
                else:
                    records[unit.id] = self._accept(provider, node, result.target, ctx, fingerprint)
                continue
            # 逐句收下；记忆在跑完时按**单元**一次性沉淀（见 _flush_memory）
            pieces: list[tuple[int, str]] = []
            for order, key in enumerate(keys):
                result = returned.get(key)
                # 指纹按**这句自己的原文**算：同一单元里每句命中的知识不同，
                # "改一条术语只作废命中句"就靠这一条（staleness 按槽位对账）
                slot_fingerprint = TranslateLayer._fingerprint(
                    ctx, node.node_id, TranslateLayer._slot_source(unit, key)
                )
                if result is None or not result.target.strip():
                    records[key] = self._missing_record(
                        provider,
                        node,
                        ctx,
                        slot_fingerprint,
                        unit_id=key,
                        source=TranslateLayer._slot_source(unit, key),
                    )
                    continue
                records[key] = accept_slot(provider, key, node, result.target, ctx, slot_fingerprint)
                pieces.append((order, result.target))
            if len(pieces) == len(keys) and ctx.options.use_memory:
                # 逐句沉淀：键是这句自己的原文，指纹也按**这句自己的原文**算
                # （scope-free）—— 同一句落在两个单元里必须算出同一个指纹，
                # 否则记忆键（原文）撞车时后写的会把先写的判成未命中（真靶 24%
                # 的单元栽在这个坑里）；按单元算就会撞，按句算撞了也无害。
                by_order = {order: text for order, text in pieces}
                for order, key in enumerate(keys):
                    target = by_order.get(order, "")
                    if not target:
                        continue
                    ctx.new_memory.append(
                        (
                            TranslateLayer._slot_source(unit, key),
                            target,
                            TranslateLayer._fingerprint(
                                ctx,
                                node.node_id,
                                TranslateLayer._slot_source(unit, key),
                                scope_free=True,
                            ),
                        )
                    )
        # **收尾对账**不在这里做：`_invoke` 只看得见这一次请求的槽位，而同一批里
        # 记忆命中的槽位不在 `records` 里 —— 判据要拿到"整单元的全部槽位"才算数，
        # 所以它住 `_reject_mismatched_records`（批级，一处实现）。
        return records, None

    @staticmethod
    def _slot_keys_of(unit: Any) -> list[str]:
        """单元的槽位身份清单。空清单表示这条单元没有逐槽位信息（老数据/夹具）。"""
        return [str(key) for key in (unit.metadata.get("slot_keys") or [])]

    def _missing_record(
        self,
        provider: LLMProvider,
        node: PathNode,
        ctx: _RunContext,
        fingerprint: str,
        *,
        unit_id: str = "",
        source: str = "",
    ) -> TranslationArtifact:
        """模型响应里没有这一条：如实记 failed，不假装翻过。"""
        unit = node.unit
        assert unit is not None
        key = unit_id or unit.id
        text = source or unit.source
        ctx.report.add_issue(
            "failed",
            code="missing_in_response",
            message=f"{node.path} 在 provider 响应里缺失，这一条没有译文",
            ref=key,
            detail={"path": node.path, "source": text},
        )
        return TranslationArtifact.from_target(
            unit_id=key,
            source=text,
            target="",
            status=TranslationStatus.FAILED,
            provider=provider.name,
            path=node.path,
            locator=unit.locator,
            error="模型响应里没有这一条",
            knowledge_fingerprint=fingerprint,
        )

    @staticmethod
    def _produces_assets(nodes: list[PathNode]) -> bool:
        """这一批内容该不该产资产（术语 / 世界书候选）。

        判据是**适配层申报的内容类别**（``unit.metadata["content_class"]``）：只有叙事
        内容产资产。真靶 46 个单元里有 7 个是 Ren'Py 自带的界面 / 开发工具文件，世界书的
        「ActionEditor 的剪贴板 / warper / Ren'Py 界面文本」、术语的「Legacy GUI /
        spline editor」共 17 条就是从那儿申报出来的 —— 不是模型胡说，是我们让它对工具
        界面也产设定。**内容范围不变**（这些文本照样翻，§0.1），变的只是产不产资产。

        一批里有任一叙事单元就照收：模型申报是跟着**响应**走的，不按条目分，
        一批混了两种内容时按"有叙事"算，宁多登记一条候选，也不把叙事单元的东西丢掉。
        """
        classes = [
            str((node.unit.metadata or {}).get("content_class") or "")
            for node in nodes
            if node.unit is not None
        ]
        if not classes:
            return True
        return any(produces_assets(name) for name in classes)

    @staticmethod
    def _collect_declared(
        results: list[TranslationResult],
        ctx: _RunContext,
        nodes: list[PathNode] | None = None,
    ) -> None:
        """收下模型在响应里**申报的实体**（原文写法 → 译名 / 设定），去重后排队。

        这是"边翻译边产出"的来源：整份离线抽术语靠"重复且一致"，在一个段里几乎全落空；
        申报覆盖每一句，而且不多花一次调用。收下来的东西**只是候选**（模型产出 =
        hypothesis），落盘与批准由调用方按 §1.4 的纪律处置。

        申报的形状就是术语书的一行 —— 两栏都可以空，空的那一栏当没申报。

        ``nodes`` 是这一批的结点：拿它判"这批内容该不该产资产"（见
        :meth:`_produces_assets`）。界面 / 开发工具单元申报的东西**不登记**，
        但**如实计数** —— 不然和"模型没申报"分不开。
        """
        if nodes is not None and not TranslateLayer._produces_assets(nodes):
            skipped = sum(len(result.declared_terms) for result in results)
            if skipped:
                ctx.report.metrics["asset_declarations_skipped_non_narrative"] = (
                    ctx.report.metrics.get("asset_declarations_skipped_non_narrative", 0)
                    + skipped
                )
            return
        for result in results:
            for entry in result.declared_terms:
                entry = coerce_declared(entry)
                text = str(entry.source).strip()
                translated = str(entry.target).strip()
                setting = str(getattr(entry, "profile", "") or "").strip()
                if not text or not (translated or setting):
                    continue
                # **每一次申报都记进"这一批"**（含与前面批次不一致的那些）：跨批合并要看的
                # 正是"重复"与"撞车"，而 `declared_terms` 那个整轮去重的列表两样都留不下。
                fingerprint = (text, translated, setting)
                if fingerprint not in ctx._batch_terms_seen:
                    ctx._batch_terms_seen.add(fingerprint)
                    ctx.batch_terms.append(
                        DeclaredTerm(source=text, target=translated, profile=setting)
                    )
                if text in ctx._declared_seen:
                    continue
                ctx._declared_seen.add(text)
                ctx.declared_terms.append(
                    DeclaredTerm(source=text, target=translated, profile=setting)
                )

    @staticmethod
    def _repair(
        provider: LLMProvider,
        batch: list[PathNode],
        records: dict[str, TranslationArtifact],
        ctx: _RunContext,
        accept_slot: Callable[
            [LLMProvider, str, PathNode, str, _RunContext, str], TranslationArtifact
        ] | None = None,
    ) -> None:
        """把这一批里"结构校验没过"与"模型答坏了"的条目挑出来，再问一次。

        预算取 ``min(retry_on_violation, max_attempts - 1)``：一个是"愿意再问几次"，
        另一个是"这条任务总共允许问几次模型"，两个上限都得算数 —— 声明了却不生效的
        上限比没有上限更糟。只重试这两类；provider 整体连不上不走这里
        （那是另一类问题，重试只是白花钱）。

        "答坏了"这一类必须重问：一次读不出来的回复会**废掉整批**，而重问同一批
        通常就好 —— 真靶实测那次 12 条同批全废，重发一次 12 条全过。
        """
        budget = min(
            ctx.options.retry_on_violation, max(0, ctx.options.max_attempts - 1)
        )
        if budget <= 0:
            return
        broken: list[PathNode] = []
        for node in batch:
            unit = node.unit
            if unit is None:
                continue
            # 要重问的两类：**那批响应读不出来**（整单元都没落），以及
            # **有槽位没过结构闸门**（`needs_review` 是"能再修一次"的信号）。
            keys = TranslateLayer._slot_keys_of(unit) or [unit.id]
            unreadable = unit.id in ctx.malformed_units and any(
                records.get(key) is None for key in keys
            )
            rejected = any(
                records.get(key) is not None
                and records[key].status is TranslationStatus.NEEDS_REVIEW
                for key in keys
            )
            if unreadable or rejected:
                broken.append(node)
        # 同一个结点被塞进来两遍 → 修复批会把整个单元的条目发两遍（真靶实测 68 → 136，
            # 模型只答 65、修复白跑）。请求级已经守住不重复（见 `_items_for`），这里是
            # **根因的取证**：谁把它塞了两遍，报告里说清楚，不再靠猜。
        duplicated = [
            unit_id
            for unit_id, count in collections.Counter(
                node.unit.id for node in broken if node.unit is not None
            ).items()
            if count > 1
        ]
        if duplicated:
            ctx.report.add_issue(
                "warnings",
                code="repair_batch_has_duplicate_units",
                message=(
                    f"修复批里有 {len(duplicated)} 个单元被重复列入（请求里已去重，不会重复发）"
                ),
                detail={"units": duplicated[:20], "batch_size": len(batch)},
            )
        for _attempt in range(budget):
            if not broken:
                return
            # 第一次问过的那次不算重试；这里只给"又问过"的那几轮计数。
            # 计数按**槽位**走（记录就是按槽位记账的）：一个单元里两句违规，
            # 就是两次重试，不是一次 —— 报告回答的是"为几句交了重试的钱"。
            if _attempt == 0:
                broken_keys = sum(
                    1
                    for node in broken
                    if node.unit is not None
                    for key in TranslateLayer._slot_keys_of(node.unit) or [node.unit.id]
                    if records.get(key) is None
                    or records[key].status is TranslationStatus.NEEDS_REVIEW
                )
                ctx.report.metrics["retry_attempts"] = (
                    ctx.report.metrics.get("retry_attempts", 0) + broken_keys
                )
            # 状态机要如实反映"又问过一次"（Guide §17 的 RETRYING）
            for node in broken:
                if node.unit is not None:
                    ctx.retries[node.unit.id] = ctx.retries.get(node.unit.id, 0) + 1
            request = TranslateLayer._repair_request(broken, ctx)
            try:
                results = provider.translate(request)
            except ProviderError as exc:
                # 重问还是答坏了：如实把新原因写回那几条，别把它当"已修复"
                TranslateLayer._record_still_broken(broken, records, ctx, exc.message)
                return
            except Exception:  # noqa: BLE001 - 重试失败不改变已有结论
                return
            # 修复批的结果与第一遍一样是**模型产出**：它申报的资产也要收（真靶上第 4 次
            # 调用是 repair，它申报的那条世界书条目当时被静默丢了，见 R63）。
            TranslateLayer._collect_declared(results, ctx, broken)
            returned = {result.unit_id: result for result in results}
            still_broken: list[PathNode] = []
            for node in broken:
                unit = node.unit
                assert unit is not None
                fixed = TranslateLayer._try_accept_repair(
                    provider, node, returned, records, ctx, _attempt + 1, accept_slot
                )
                if fixed:
                    ctx.malformed_units.discard(unit.id)
                else:
                    still_broken.append(node)
            broken = still_broken

    @staticmethod
    def _record_still_broken(
        broken: list[PathNode],
        records: dict[str, TranslationArtifact],
        ctx: _RunContext,
        message: str,
    ) -> None:
        """重问又答坏了：把新原因写回那几条（状态仍是 failed）。"""
        for node in broken:
            unit = node.unit
            if unit is None:
                continue
            # 按槽位记：只改那些本来就没落下的
            for key in TranslateLayer._slot_keys_of(unit) or [unit.id]:
                record = records.get(key)
                if record is None:
                    continue
                record.error = message
            ctx.report.add_issue(
                "failed",
                code="provider_error",
                message=message,
                ref=unit.id,
                detail={"path": node.path, "retried": True},
            )

    @staticmethod
    def _count_recovered(
        ctx: _RunContext,
        node: PathNode,
        *,
        was_malformed: bool,
        count: int = 1,
    ) -> None:
        """重试救回来了：计数 + 留一条可见的痕迹（两类原因分开记）。

        计数按**修好的槽位数**走，与 ``retry_attempts`` 同口径；警告每个单元只留一条。
        """
        ctx.report.metrics["retry_recovered"] = (
            ctx.report.metrics.get("retry_recovered", 0) + count
        )
        ctx.report.add_issue(
            "warnings",
            code=(
                "malformed_response_recovered"
                if was_malformed
                else "constraint_violation_recovered"
            ),
            message=(
                f"{node.path} 那批响应没能读出来，重问一次后拿到了译文"
                if was_malformed
                else f"{node.path} 前面几轮没有保住受保护结构，重试后修好了"
            ),
            ref=node.unit.id if node.unit else "",
            detail={"path": node.path},
        )

    @staticmethod
    def _try_accept_repair(
        provider: LLMProvider,
        node: PathNode,
        returned: dict[str, TranslationResult],
        records: dict[str, TranslationArtifact],
        ctx: _RunContext,
        attempt: int,
        accept_slot: Callable[
            [LLMProvider, str, PathNode, str, _RunContext, str], TranslationArtifact
        ] | None = None,
    ) -> bool:
        """重问回来的这一个单元能不能收下。全落下了才返回 ``True``。

        "那批响应根本读不出来、重问一次才拿到"是重试的正主；单元含多句之后逐条做
        结构重试不划算（一条不过要重发整个单元），所以这里只负责把重问回来的
        译文按槽位收下，报告里如实记一笔。
        """
        unit = node.unit
        assert unit is not None
        keys = TranslateLayer._slot_keys_of(unit)
        if not keys:
            # 没有逐槽位信息的单元（老数据/夹具）：整条收下，但**仍然看它过没过闸门** ——
            # 不过闸门就不算修好（否则状态机会把一条 needs_review 记成已修复）
            result = returned.get(unit.id)
            if result is None or not result.target.strip():
                return False
            record = TranslateLayer._accept(
                provider,
                node,
                result.target,
                ctx,
                TranslateLayer._fingerprint(ctx, node.node_id, unit.source),
            )
            record.provenance.metadata = {
                **(record.provenance.metadata or {}), "retries": attempt}
            records[unit.id] = record
            if record.status is TranslationStatus.NEEDS_REVIEW:
                return False
            TranslateLayer._count_recovered(ctx, node, was_malformed=False)
            return True
        remaining = 0
        rejected = 0
        was_malformed = unit.id in ctx.malformed_units
        for key in keys:
            result = returned.get(key)
            if result is None or not result.target.strip():
                remaining += 1
                continue
            # 修复回来的记录同样按**这句自己的原文**打指纹（与首轮收录同口径）
            slot_fingerprint = TranslateLayer._fingerprint(
                ctx, node.node_id, TranslateLayer._slot_source(unit, key)
            )
            if accept_slot is not None:
                record = accept_slot(provider, key, node, result.target, ctx, slot_fingerprint)
            else:  # pragma: no cover - 调用方总会给
                record = TranslateLayer._accept(provider, node, result.target, ctx, slot_fingerprint)
            # 修复来的译文要留痕：这是第几次重试的产出（0 = 首轮，不算重试）
            record.provenance.metadata = {
                **(record.provenance.metadata or {}), "retries": attempt}
            records[key] = record
            if record.status is TranslationStatus.NEEDS_REVIEW:
                rejected += 1
        if remaining or rejected:
            # 还有没落的、或重问回来的仍然没过闸门 —— 如实算"没修好"
            return False
        TranslateLayer._count_recovered(
            ctx, node, was_malformed=was_malformed, count=len(keys) - remaining - rejected
        )
        # 原来那些失败记录不该留在报告里 —— 它们已经被修好了
        covered = set(keys) | {unit.id}
        ctx.report.failed = [
            issue
            for issue in ctx.report.failed
            if not (issue.ref in covered and issue.code in {"constraint_violation", "provider_error"})
        ]
        return True

    @staticmethod
    def _repair_request(
        broken: list[PathNode], ctx: _RunContext
    ) -> TranslationRequest:
        """重试请求：只带违规的条目，并把"缺了什么"写清楚。

        它照样是 Task 驱动的：先给这几条重规划 Task（状态从 ``RETRYING`` 走回
        ``TRANSLATING``），再按同样的 Context 装配规则拼载荷。
        """
        planned: list[TranslationTask] = []
        items: list[TranslationItem] = []
        by_unit: dict[str, TranslationTask] = {}
        for node in broken:
            unit = node.unit
            if unit is None:
                continue
            task = TranslateLayer.__new__(TranslateLayer).ready_task(node, ctx)
            planned.append(task)
            by_unit[unit.id] = task
        # 载荷与首次请求同一套规则：一个单元一次请求，里面逐句列出（每句带自己的槽位）
        items = TranslateLayer._items_for([node for node in broken if node.unit is not None], by_unit)
        local_tags: dict[str, list[str]] = {}
        items = TranslateLayer._tag_items(
            items, ctx.resources, ctx.options, local_tags
        )
        details: list[str] = []
        for node in broken:
            unit = node.unit
            if unit is None:
                continue
            for key in TranslateLayer._slot_keys_of(unit) or [unit.id]:
                artifact = ctx.last_verdict.get(key)
                missing = ", ".join(artifact) if artifact else ""
                details.append(f"- [{key}] 上一条译文没有通过结构校验：{missing}")
        instructions = (
            "【结构校验未通过，请重出】\n"
            + "\n".join(details)
            + "\n必须原样保留每条待译内容列出的受保护 token（数量与顺序都不能变）。"
        )
        listed = [writing for writings in local_tags.values() for writing in writings]
        listed = [writing for index, writing in enumerate(listed) if writing not in listed[:index]]
        if listed:
            instructions += "\n" + announce(listed)
        return TranslationRequest(
            items=items,
            target_language=ctx.options.target_language,
            source_language=ctx.options.source_language,
            knowledge=TranslateLayer._assemble(planned),
            instructions=instructions,
            metadata={"repair": True},
            tasks=planned,
            calls=ctx.calls,
            phase="repair",
            transcript=ctx.transcript,
            dump=ctx.dump,
            # 重出也是同一条路径：模板必须跟着这次运行的选项走，不能退回出厂那一份
            # （否则"用户改了模板"在重出这条路上静默失效）
            prompt_template=ctx.options.prompt_template,
            template_name=ctx.options.template_name,
        )

    def _split_memory(
        self, batch: list[PathNode], ctx: _RunContext
    ) -> tuple[dict[str, TranslationArtifact], list[PathNode], str | None]:
        """把一批分成"记忆里有"和"得问模型"两拨。

        精确命中才走这里（归一化空白后逐字相同）；相近的句子只是提示词里的参考。

        **单元级命中**才是省钱的：一个单元只要它整段原文命中过记忆，整段就不用问了。
        单元没命中时，逐句再试一次 —— 一个单元里常见的重复句（"..."、"Yes."）能各自
        命中，剩下的句子才进请求。
        """
        records: dict[str, TranslationArtifact] = {}
        if not ctx.options.use_memory:
            return records, list(batch), None
        polish = ctx.options.memory_reuse_mode == "polish"
        pending: list[PathNode] = []
        for node in batch:
            unit = node.unit
            if unit is None:
                continue
            keys = self._slot_keys_of(unit)
            if not keys:
                # 没有逐槽位信息的单元（老数据/夹具）：只能整条查
                entry = self._memory_lookup(unit.source, node, ctx)
                if entry is None:
                    pending.append(node)
                else:
                    records[unit.id] = self._from_memory(node, entry, ctx)
                    if polish:
                        pending.append(node)
                continue
            # 逐句查，**指纹按这句自己的原文算**（scope-free）：同一句在任何单元里
            # 算出的指纹都一样 —— 记忆键是原文，撞车（同一句落在两个单元）时两边
            # 的指纹一致，谁也不会把谁判成未命中。
            pending_keys: list[str] = []
            reused_targets: dict[str, str] = {}
            for key in keys:
                source = TranslateLayer._slot_source(unit, key)
                entry = self._memory_lookup(source, node, ctx)
                if entry is None:
                    pending_keys.append(key)
                    continue
                records[key] = self._from_memory_slot(key, source, node, entry, ctx)
                reused_targets[key] = str(entry.target)
            if polish:
                # **允许改动**那一档：整个单元照样发出去（含已命中的那几句），
                # 已定译的写法作为上下文与默认答案摆在请求里（见 `reused_target`）。
                # 模型回什么由 `_collect_polish_proposals` 与已定译比对，**不就地覆盖**。
                pending.append(TranslateLayer._with_reused_targets(node, reused_targets, {}))
                continue
            if pending_keys:
                # 只把**没命中的那几句**送模型：整个单元一起重问等于白花已复用的钱
                context = {key: reused_targets[key] for key in reused_targets}
                pending.append(TranslateLayer._with_reused_targets(node, context, pending_keys))
        return records, pending, None

    @staticmethod
    def _with_reused_targets(
        node: PathNode,
        targets: dict[str, str],
        pending_keys: list[str],
        *,
        window: list[str] | None = None,
    ) -> PathNode:
        """把"已定译的那几句**及其译文**"挂到结点上，让请求能把它们摆出来。

        ``pending_keys`` 非空＝`keep` 档：只收窄"还要问模型的"那些键（记账按它走）；
        为空＝`polish` 档：整个单元都问，已定译的只是多了个"现已定为"给人看。
        槽位载荷一条都不摘 —— 摘掉会让"连续对白"断掉、编号失真（真靶实测）。

        **两件事互相独立**：挂上下文（``targets``）与收窄待问集合（``pending_keys``）。
        早先"没有上下文就原样返回"的写法把二者绑在一起，于是"第一段没有任何上下文"
        时根本没收窄 —— 整单元 7 条被当成第一段发出去，切分等于没切（回归测试
        `tests/test_unit_split.py::test_a_long_unit_is_split_into_budget_sized_requests`）。

        ``window`` 是**请求里要列出来的槽位**（单元内切分用）：只列"这一段 + 它前面的
        窗口"，不再把整个单元摊在请求里。位置编号仍按**整个单元**算（见 `_items_for`），
        所以"单元内第 4/7 句"不会因为载荷被收窄而变成"第 1/6 句"。
        """
        unit = node.unit
        if unit is None:
            return node
        original = list(unit.metadata.get("slot_keys") or [])
        narrowed = (
            [key for key in original if key in set(pending_keys)]
            if pending_keys
            else original
        )
        if not targets and narrowed == original and window is None:
            return node
        metadata = {**unit.metadata, "slot_keys": narrowed}
        if targets:
            metadata["context_slot_targets"] = dict(targets)
        if window is not None:
            metadata["window_slot_keys"] = list(window)
        return replace(node, unit=replace(unit, metadata=metadata))

    @staticmethod
    def _collect_polish_proposals(
        nodes: list[PathNode],
        records: dict[str, TranslationArtifact],
        ctx: _RunContext,
    ) -> None:
        """`polish` 档：模型对"已定译"那句给了不同写法时，记成**修订提案**。

        两件事同时做：

        * **不就地覆盖** —— 已确认的译名仍然生效（`records` 被改回已定译的那一条）。
          让模型的润色直接落地等于"模型自己批准自己"，与合同 §1.4 的纪律冲突；
        * **如实留痕** —— 旧值、新值、是哪一句、哪个单元，进提案清单，交人或 agent 二选一。

        只比较**同一条槽位**（`unit_id` 就是槽位身份），所以不存在"串了别的句子"的可能。
        """
        for node in nodes:
            unit = node.unit
            if unit is None:
                continue
            reused = dict((unit.metadata or {}).get("context_slot_targets") or {})
            if not reused:
                continue
            for key, original in reused.items():
                record = records.get(key)
                if record is None:
                    continue
                if record.translated_text.strip() == str(original).strip():
                    continue
                TranslateLayer._note_polish_proposal(
                    ctx,
                    {
                        "unit_id": key,
                        "unit_label": str((unit.metadata or {}).get("structure_label") or ""),
                        "source": record.source,
                        "current": str(original),
                        "proposed": record.translated_text,
                        "reason": record.error or "",
                    },
                )
                records[key] = TranslateLayer._with_reused_target(record, str(original))

    @staticmethod
    def _note_polish_proposal(ctx: _RunContext, proposal: dict[str, Any]) -> None:
        """同一条修订提案只记一次 —— 同一句原文在多处出现，模型会给出同样的改写。

        合并键是``(原文, 已定译, 提出改成什么)``：这三样一样就是同一个决定，
        重复记只会在"拍板清单"里灌水。提出过的单元都留痕，便于回看影响面。
        """
        signature = (
            str(proposal.get("source") or ""),
            str(proposal.get("current") or ""),
            str(proposal.get("proposed") or ""),
        )
        for existing in ctx.polish_proposals:
            if (
                str(existing.get("source") or ""),
                str(existing.get("current") or ""),
                str(existing.get("proposed") or ""),
            ) == signature:
                units = existing.setdefault("units", [str(existing.get("unit_id") or "")])
                if proposal["unit_id"] not in units:
                    units.append(str(proposal["unit_id"]))
                return
        ctx.polish_proposals.append(dict(proposal))

    @staticmethod
    def _with_reused_target(record: TranslationArtifact, target: str) -> TranslationArtifact:
        """把记录改回"已定译"的那一条（`polish` 档下提案没被批准之前，它仍然生效）。"""
        changed = replace(
            record,
            translated_segments=[Segment(SegmentKind.TEXT.value, target)],
            error="模型提出了修订，但未批准之前仍按已定译沿用",
        )
        return changed

    @staticmethod
    def _reduced_node(node: PathNode, keys: list[str]) -> PathNode:
        """同一个结点，但"待译"只保留 ``keys`` 这几条槽位 —— 用于"部分命中记忆"的那部分。

        **槽位载荷一条都不摘**：命中的那几句要**留在请求里当上下文**
        （标 `〖已定译·沿用〗`，见 `TranslationItem.resolved`）。
        摘掉它们的代价是真靶上实测出来的：`Third time this week.` 是上一句的回答，
        被摘掉之后模型看到的是一句悬空的回应，而且"单元内第 i/n 句"的编号会被重排
        （第 4/7 句变成第 1/4 句）。所以这里只把"待译集合"收窄：

        * ``metadata["slot_keys"]`` = **还要问模型的**那几条（记账、`_invoke`、`_repair` 都按它走）；
        * ``metadata["context_slot_keys"]`` = **已经命中的**那几条（只用于请求里的上下文）。
        """
        unit = node.unit
        if unit is None or not keys:
            return node
        all_keys = [str(key) for key in (unit.metadata.get("slot_keys") or [])]
        if not all_keys:
            return node
        pending = [key for key in all_keys if key in set(keys)]
        if len(pending) == len(all_keys):
            return node
        return replace(
            node,
            unit=replace(
                unit,
                metadata={
                    **unit.metadata,
                    "slot_keys": pending,
                    "context_slot_keys": [key for key in all_keys if key not in set(pending)],
                },
            ),
        )

    def _memory_lookup(
        self,
        source: str,
        node: PathNode,
        ctx: _RunContext,
        *,
        fingerprint_source: str | None = None,
    ) -> Any:
        """查翻译记忆（按原文逐字命中）。

        ``fingerprint_source`` 给**槽位**用：一条句子的记忆是按它所属单元的指纹存下的，
        用句子自己算指纹就永远对不上（换了个原文 = 换了个知识状态）。

        第一次查按指纹；查不到且调用方声明了 ``reuse_imported`` 时，用"读进来的已有
        译文"（它们的指纹本来就是空的）再查一次 —— 那是**这一次的声明**，不是默认行为。
        """
        fingerprint = TranslateLayer._fingerprint(
            ctx,
            node.node_id,
            fingerprint_source or source,
                        scope_free=True,
        )
        # **先看这一轮自己产出的**：新译文原本跑完才落盘，于是同一句在同一轮里
        # 第二次出现时命中不了。真靶 30.4% 的槽位是重复原文，全在这儿白问一遍模型。
        # 判据与落盘记忆完全同一条（归一化原文 + 指纹），所以不存在"两套命中口径"。
        fresh = ctx.memory_cache_snapshot.get((normalize(source), ctx.options.target_language))
        if fresh is not None and (
            not fingerprint or fresh.knowledge_fingerprint == fingerprint
        ):
            fresh.reuses += 1
            return fresh
        # 已提交的记忆读**这一批的快照**，不读实时库：本批新提交的条目下一批才可见。
        memory = ctx.memory_snapshot if ctx.memory_snapshot is not None else ctx.resources.memory
        entry = memory.lookup(
            source,
            ctx.options.target_language,
            # 只认同一资源状态下产出的那条：术语表事后补上时，旧译文就不算命中了。
            # 不算知识点 —— 换个场景不是资源变化（见 _fingerprint 的说明）。
            fingerprint=fingerprint,
        )
        if entry is not None or not ctx.options.reuse_imported:
            return entry
        return memory.lookup(
            source,
            ctx.options.target_language,
            fingerprint=fingerprint,
            allow_imported=True,
        )

    def _from_memory_slot(
        self,
        key: str,
        source: str,
        node: PathNode,
        entry: Any,
        ctx: _RunContext,
    ) -> TranslationArtifact:
        """逐句命中：按槽位记下这条复用来的译文。

        记忆条目的键是**整段原文**（一整个单元），它的译文就是整段的译文；这里按槽位
        记下"这一段属于这一句"，写回时逐条落到位置上。复用来的译文**照样要过结构校验**，
        标尺是**这一句自己的原文**（拿整个单元当标尺会误判，见 :meth:`_accept_slot`）。
        """
        unit = node.unit
        assert unit is not None
        ctx.reused.append(entry.key)
        validation = validate(
            # 记忆命中的是**这一句**（`source` 就是它的原文），标尺也用它自己
            slot_gauge(unit, key, ctx.scanner),
            entry.target,
            scanner=ctx.scanner,
            constraints=declared_constraints(unit, ctx.options.validation_policy()),
            approved=ctx.approved.get(unit.id, ()),
        )
        status = TranslationStatus.OK
        error: str | None = None
        if not validation.ok:
            status = TranslationStatus.NEEDS_REVIEW
            error = validation.errors[0].message
            ctx.report.add_issue(
                "failed",
                code="constraint_violation",
                message=f"{error}（来自翻译记忆，原文未变但结构对不上）",
                ref=key,
                detail={"path": node.path, "source": "memory"},
            )
        return TranslationArtifact(
            unit_id=key,
            translated_segments=[Segment(SegmentKind.TEXT.value, entry.target)],
            locator=TranslateLayer._placement_locator(unit, key),
            status=status,
            provenance=Provenance(
                provider="memory",
                preset="memory",
                metadata={"memory_provider": entry.provider},
            ),
            validation=validation,
            resource_versions=ctx.resource_versions,
            source=source,
            path=node.path,
            error=error,
            # 复用只发生在"scope-free 指纹与当前一致"的前提下；落给 staleness 的
            # 则是**此刻**这条槽位的 scope-aware 指纹 —— 之后知识再变，它照样能被
            # 点名过期（拿记忆条目自己的指纹对账会永远对不上，两边口径不同）。
            knowledge_fingerprint=TranslateLayer._fingerprint(ctx, node.node_id, source),
        )

    def _from_memory(
        self, node: PathNode, entry: Any, ctx: _RunContext
    ) -> TranslationArtifact:
        """把一条记忆命中收成 Artifact —— 复用来的译文**照样要过结构校验**。"""
        unit = node.unit
        assert unit is not None
        ctx.reused.append(entry.key)
        validation = validate(
            unit,
            entry.target,
            scanner=ctx.scanner,
            constraints=declared_constraints(unit, ctx.options.validation_policy()),
            approved=ctx.approved.get(unit.id, ()),
        )
        status = TranslationStatus.OK
        error: str | None = None
        if not validation.ok:
            status = TranslationStatus.NEEDS_REVIEW
            error = validation.errors[0].message
            ctx.report.add_issue(
                "failed",
                code="constraint_violation",
                message=f"{error}（来自翻译记忆，原文未变但结构对不上）",
                ref=unit.id,
                detail={
                    "path": node.path,
                    "source": "memory",
                    "violations": [v.to_dict() for v in validation.violations],
                },
            )
        return TranslationArtifact(
            unit_id=unit.id,
            translated_segments=[Segment(SegmentKind.TEXT.value, entry.target)],
            locator=unit.locator,
            status=status,
            provenance=Provenance(
                provider="memory",
                preset="memory",
                metadata={"memory_provider": entry.provider},
            ),
            validation=validation,
            resource_versions=ctx.resource_versions,
            source=unit.source,
            path=node.path,
            error=error,
            knowledge_fingerprint=TranslateLayer._fingerprint(ctx, node.node_id, unit.source),
        )

    def _flush_memory(self, ctx: _RunContext, provider: LLMProvider) -> None:
        """把这一轮的记忆读写一次性落盘，并汇总"给了多少参考译法"。

        ⚠️ **被置留的译文不许进记忆**：它是在收下之后才被判成"与原文对不上"的
        （见 `_RunContext.mismatched_sources`），而记忆一旦收下，下一轮同一句原文就会
        直接复用它、连模型都不问 —— 那正是真靶上那五条错配活了下来的方式。落盘前按
        原文摘掉：**从记忆里漏出去一条错的，比少复用一条贵得多**。
        """
        suggestions = sum(
            int(request.metadata.get("memory_suggestions") or 0)
            for request in ctx.requests
        )
        ctx.report.metrics["memory_suggestions"] = suggestions
        if not ctx.options.use_memory:
            return
        entries = ctx.new_memory
        # 被置留的那几条的原文：**从记忆里整条摘掉**（连同这一轮的内存缓存）。
        # 宁可连"同一句在别处翻对了"的那条复用一起放弃 —— 从记忆里漏出去一条错的，
        # 下一轮它会**替代模型**输出（连问都不问），比少复用一条贵得多。
        blocked = {
            normalize(source)
            for source in ctx.mismatched_sources.values()
            if str(source).strip()
        }
        if blocked:
            entries = [entry for entry in ctx.new_memory if normalize(entry[0]) not in blocked]
            for source in blocked:
                ctx.memory_cache.pop((source, ctx.options.target_language), None)
            withheld = len(ctx.new_memory) - len(entries)
            if withheld:
                ctx.report.metrics["memory_entries_withheld"] = (
                    ctx.report.metrics.get("memory_entries_withheld", 0) + withheld
                )
        ctx.resources.memory.remember_many(
            entries,
            language=ctx.options.target_language,
            provider=provider.name,
        )
        # 只有命中、没有新译文时也要把复用计数落盘
        ctx.resources.memory.flush()

    @staticmethod
    def _accept(
        provider: LLMProvider,
        node: PathNode,
        target: str,
        ctx: _RunContext,
        fingerprint: str,
    ) -> TranslationArtifact:
        """把**单元**的译文收成 Artifact（没有逐槽位信息的单元走这条）。"""
        unit = node.unit
        assert unit is not None
        return TranslateLayer._accept_target(
            provider, node, target, ctx, fingerprint, key=unit.id, text=unit.source
        )

    @staticmethod
    def _accept_slot(
        provider: LLMProvider,
        key: str,
        node: PathNode,
        target: str,
        ctx: _RunContext,
        fingerprint: str,
    ) -> TranslationArtifact:
        """把一个单元的译文收成**一条槽位**的 Artifact。

        一个单元含多句，模型逐句给译文；这一条就是其中一句。术语、风格、知识点这些
        **单元级**的约束不变，比对标尺换成**这一句自己的原文**（:func:`slot_gauge`）——
        拿整个单元当标尺会把别的句子的占位符算到这一句头上（真靶 `act25` 1,963 条全被误判）。
        """
        unit = node.unit
        assert unit is not None
        return TranslateLayer._accept_target(
            provider,
            node,
            target,
            ctx,
            fingerprint,
            key=key,
            text=TranslateLayer._slot_source(unit, key),
            # **逐句进记忆**：真靶 15,755 条槽位里 30.4% 是重复原文，
            # 整段存的效果等于"只有整段一字不差地又出现一次才命中"，
            # 而那在真靶上一段都没有（最高覆盖率的单元只有 70%）。
            remember=True,
            gauge_key=key,
        )

    @staticmethod
    def _accept_target(
        provider: LLMProvider,
        node: PathNode,
        target: str,
        ctx: _RunContext,
        fingerprint: str,
        *,
        key: str,
        text: str,
        remember: bool = True,
        gauge_key: str = "",
    ) -> TranslationArtifact:
        """把一段译文收成 Artifact —— 先过结构校验，再决定它能不能写回。

        ``gauge_key`` 只在"这一段是一条槽位"时有值：那时比对标尺是**这条槽位自己的
        原文**（见 :func:`~gametrans.core.constraints.slot_gauge`）。
        """
        unit = node.unit
        assert unit is not None
        gauge = (
            slot_gauge(unit, gauge_key, ctx.scanner) if gauge_key else unit
        )
        validation = validate(
            gauge,
            target,
            scanner=ctx.scanner,
            constraints=declared_constraints(unit, ctx.options.validation_policy()),
            approved=ctx.approved.get(unit.id, ()),
            # 这一条原文该出现的术语标签（没包过标签的条目给 None = 不检查）
            term_tags=ctx.term_tags.get(key),
        )
        status = TranslationStatus.OK
        error: str | None = None
        if not validation.ok:
            status = TranslationStatus.NEEDS_REVIEW
            worst = validation.errors[0]
            error = worst.message
            ctx.last_verdict[key] = [
                f"{v.constraint_type}（{v.message}）" for v in validation.violations
            ]
            ctx.report.add_issue(
                "failed",
                code="constraint_violation",
                message=worst.message,
                ref=key,
                detail={
                    "path": node.path,
                    "violations": [v.to_dict() for v in validation.violations],
                },
            )
        # 空原文没有可用的键：记下来只会变成"谁都能对上"的垃圾（真靶上就是这样攒出
        # 8,157 条，第一条的原文是空的）。整单元写回的老路径会走到这儿。
        if (
            remember
            and str(text).strip()
            and status is TranslationStatus.OK
            and ctx.options.use_memory
        ):
            # 攒着，跑完一次性写回记忆（落盘是 O(文件) 的操作，几千条不能一次一条）
            memory_fingerprint = TranslateLayer._fingerprint(
                ctx, node.node_id, text, scope_free=True
            )
            # 这一条要是本来就"已定译"，进记忆的只能是**那个已定译**：`polish` 档下
            # 模型给的是**提案**，没批准就不算数。真靶上真的栽过 —— 提案被当成本轮
            # 记忆的源，下一个单元"命中"的是提案，于是提案自己成了"已定译"，
            # 而且提案比对时新旧两边一模一样，提案就丢了（模型自己批准了自己）。
            agreed = (unit.metadata or {}).get("context_slot_targets") or {}
            stored = str(agreed.get(key, target))
            ctx.new_memory.append((text, stored, memory_fingerprint))
            # **同时放进这一轮的内存缓存**：后面的单元立刻能用上，
            # 不必等到跑完落盘（`memory_cache` 的说明见 `_RunContext`）。
            ctx.memory_cache[
                (normalize(text), ctx.options.target_language)
            ] = MemoryEntry(
                source=text,
                target=stored,
                language=ctx.options.target_language,
                provider=provider.name,
                knowledge_fingerprint=memory_fingerprint,
            )
        return TranslationArtifact(
            unit_id=key,
            translated_segments=[Segment(SegmentKind.TEXT.value, target)],
            locator=TranslateLayer._placement_locator(unit, gauge_key),
            status=status,
            provenance=TranslateLayer._provenance(provider, ctx),
            validation=validation,
            resource_versions=ctx.resource_versions,
            source=text,
            path=node.path,
            error=error,
            knowledge_fingerprint=fingerprint,
        )

    @staticmethod
    def _slot_source(unit: Any, key: str) -> str:
        """这条槽位的原文（模型看到的就是它，回填校验也要用它）。"""
        for entry in (unit.locator.payload or {}).get("slots") or []:
            if str(entry.get("slot_key") or "") == key:
                return str(entry.get("source") or "")
        return unit.source

    @staticmethod
    def _placement_locator(unit: Any, key: str) -> Any:
        """逐句记录只需要**落在哪**，不需要整个单元的槽位表。

        真靶实测（2026-09-21）：一条槽位记录把整个单元的 `payload["slots"]` 整份抄下来，
        `act25` 那条 **714,797 字节**、全表 **4.2 GB** —— 面板把整表读进内存时直接卡死。
        写回真正用到的只有 `(file, line, kind)`（见 `layers/writeback.py` 与
        `engines/renpy/skeleton_pipeline.py`），所以逐句记录只留定位相关的那几个键。

        ``key`` 为空表示这条记录代表**整个单元**（单元只有一条槽位，或适配层没给逐句
        信息）：那时保留原样的 locator，包括它的槽位表。
        """
        locator = getattr(unit, "locator", None)
        if not key or locator is None:
            return locator
        payload = dict(getattr(locator, "payload", None) or {})
        payload.pop("slots", None)
        return replace(locator, payload=payload)

    @staticmethod
    def _provenance(provider: LLMProvider, ctx: _RunContext) -> "Provenance":
        """记录这条译文是从哪来的。"""
        return Provenance(
            provider=provider.name,
            model=str(getattr(provider, "model", "") or ""),
            preset=ctx.options.provider,
            metadata={"batch_size": ctx.options.batch_size, "mode": ctx.options.mode},
        )

    @staticmethod
    def _fingerprint(
        ctx: _RunContext,
        node_id: str,
        source: str,
        *,
        scope_free: bool = False,
    ) -> str:
        """记下这条文本翻译时**实际注入**的知识状态。

        没有它，"哪些译文因为知识变了而需要重做"就判断不了，事后补译只能退化成
        全量重翻。要算进去的东西：

        * 文本自己命中的术语与世界书（扫的是**请求里那份文本**，含说话人标注）
        * **风格**：它按作用域解析后实际落在这一条上的部分
        * 各个 ``use_*`` 开关 —— 关掉术语表本身就是一种状态变化

        作用域（单位 / 说话人 / 场景）任何时候都要算：风格是按作用域解析的，不传
        作用域就解析不出这一条实际注入过什么，指纹也就答不上"它为什么过期"。

        ``scope_free=True``：**连作用域也不看**。它是给翻译记忆查/存用的 ——
        记忆的键是原文，一句话在 A 场戏和 B 场戏里必须算出同一个指纹，否则同一句
        在两处出现时会互相把对方的记忆判成未命中。
        """
        # 缓存键必须带原文：同一单元里每条槽位的原文不同，命中的知识也不同 ——
        # 共用一个键会让整组槽位拿到第一条的指纹（记忆与 staleness 全部错位）
        cache_key = (node_id, source, scope_free)
        cached = ctx.fingerprints.get(cache_key)
        if cached is not None:
            return cached
        speaker = None
        scene = None
        unit = None
        if node_id in ctx.graph:
            unit = ctx.graph.get(node_id).unit
        if unit is not None and unit.context is not None:
            speaker = unit.context.speaker
            scene = unit.context.scene
        value = ctx.resources.fingerprint_for(
            source,
            use_glossary=ctx.options.use_glossary,
            use_worldbook=ctx.options.use_worldbook,
            use_style=ctx.options.use_style,
            use_knowledge=ctx.options.use_knowledge,
            unit_id=None if scope_free else (unit.id if unit is not None else None),
            speaker=None if scope_free else speaker,
            scene=None if scope_free else scene,
            # 自定义要求也会进提示词，所以要进指纹；否则改了口径谁也不会被判过期（R46）
            custom_instructions=ctx.options.custom_instructions,
        )
        ctx.fingerprints[cache_key] = value
        return value

    @staticmethod
    def _missing_at_tail(
        request: TranslationRequest, returned: dict[str, Any]
    ) -> bool:
        """没回的那些，是不是**最后几条**。

        为什么只放过这一种：平移之所以危险，是"少一条 → 后面每条都往前挪一位"。
        少在尾部时前面逐条一一对应，挪不动 —— 那几条如实记 failed 就行。
        少在中间（真靶那次少的是第 27 条）会让**它后面的每一条**都挪位，必须整份重问。
        """
        wanted = [item.unit_id for item in request.items if item.expects_answer]
        missing = [unit_id for unit_id in wanted if unit_id not in returned]
        if missing:
            # 判据：**没回的全部排在回来了的前面那条之后** —— 也就是"缺的是末尾一段"。
            # 只回一条的请求也走这条（没答的那条就在末尾），它没有被挪位的东西。
            answered = [index for index, uid in enumerate(wanted) if uid in returned]
            absent = [index for index, uid in enumerate(wanted) if uid not in returned]
            return not answered or min(absent) > max(answered)
        return True

    def _fail_all(
        self,
        nodes: list[PathNode],
        provider: LLMProvider,
        ctx: _RunContext,
        message: str,
        code: str,
    ) -> dict[str, TranslationArtifact]:
        records: dict[str, TranslationArtifact] = {}
        for node in nodes:
            unit = node.unit
            if unit is None:
                continue
            records[unit.id] = TranslationArtifact.from_target(
                unit_id=unit.id,
                source=unit.source,
                target="",
                status=TranslationStatus.FAILED,
                provider=provider.name,
                path=node.path,
                locator=unit.locator,
                error=message,
                knowledge_fingerprint=TranslateLayer._fingerprint(ctx, node.node_id, unit.source),
            )
            ctx.report.add_issue(
                "failed",
                code=code,
                message=message,
                ref=unit.id,
                detail={"path": node.path},
            )
        return records

    @staticmethod
    def _order(
        nodes: list[PathNode],
        by_unit: dict[str, TranslationArtifact],
        provider: LLMProvider,
        report: RunReport,
        *,
        in_run: set[str] | None = None,
    ) -> list[TranslationArtifact]:
        """按路径顺序把记录排出来。

        记录的键是**槽位**（一个单元含多句时逐句一条），所以这里先按槽位清单取，
        取不到再退回"整个单元一条"的老形状 —— 报告与写回的排列不该因为调度方式而变。

        ``in_run`` 是**这一轮真会翻的槽位**（阶段切窄时它比 `nodes` 窄）。台账照样覆盖
        全图，但**只有落在 ``in_run`` 里的缺口才报 `missing_in_response`** —— 没排进这一轮的
        单元本来就该没有译文，把它报成"失败"会让分段推进每段都刷出一万多条噪音。
        """
        ordered: list[TranslationArtifact] = []
        for node in nodes:
            unit = node.unit
            if unit is None:
                continue
            keys = TranslateLayer._slot_keys_of(unit)
            if keys:
                for index, key in enumerate(keys):
                    record = by_unit.get(key)
                    if record is None:
                        record = TranslationArtifact.from_target(
                            unit_id=key,
                            source=TranslateLayer._slot_source(unit, key),
                            target="",
                            status=TranslationStatus.FAILED,
                            provider=provider.name,
                            path=node.path,
                            # ⚠️ 这里**必须**走 `_placement_locator`：逐句记录只需要"落在哪"。
                            # 2026-09-21 那次是给**真记录**剥掉的（见那个方法的说明），
                            # 这条**空记录**漏了 —— 于是阶段切窄时（`--stop-after-phase`，
                            # `nodes` 仍覆盖全图）每个没排进这一轮的槽位都造一条背着
                            # **整份槽位表**的空记录：真靶 `act25` 一个单元 1,972 条槽位 →
                            # 1,972² ≈ 390 万条槽位字典，写盘时 `"\n".join` 直接 MemoryError。
                            locator=TranslateLayer._placement_locator(unit, key),
                            error=PLACEHOLDER_ERROR,
                        )
                        if in_run is None or key in in_run:
                            report.add_issue(
                                "failed",
                                code="missing_in_response",
                                message=f"{node.path} 第 {index + 1}/{len(keys)} 句没有产出译文",
                                ref=key,
                                detail={"path": node.path},
                            )
                    ordered.append(record)
                continue
            record = by_unit.get(unit.id)
            if record is None:
                record = TranslationArtifact.from_target(
                    unit_id=unit.id,
                    source=unit.source,
                    target="",
                    status=TranslationStatus.FAILED,
                    provider=provider.name,
                    path=node.path,
                    locator=unit.locator,
                    error=PLACEHOLDER_ERROR,
                )
                if in_run is None or unit.id in in_run:
                    report.add_issue(
                        "failed",
                        code="missing_in_response",
                        message=f"{node.path} 没有产出译文",
                        ref=unit.id,
                        detail={"path": node.path},
                    )
            ordered.append(record)
        return ordered

    @staticmethod
    def _publish_progress(interaction: Any, outcomes: list[_BatchOutcome], provider: str) -> None:
        """把批量进度投给交互层。

        ``translate.batch`` 在默认策略下对用户透明 —— 用户不需要看流水账，
        但 agent 通过 ``ui views`` 随时能看到。
        """
        total = len(outcomes)
        for position, outcome in enumerate(outcomes, start=1):
            where = f"阶段 {outcome.phase} · " if outcome.phase else ""
            lines = [f"{where}批 {position}/{total}", f"{outcome.ok}/{outcome.size} 条成功", f"provider: {provider}"]
            severity = "info"
            if outcome.error:
                lines.append(f"失败原因：{outcome.error}")
                severity = "warning"
            interaction.emit(
                "translate.batch",
                f"翻译批次 {position}/{total}",
                lines=lines,
                severity=severity,
            )
