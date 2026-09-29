"""provider 契约：一批进去，一批出来。

provider 是**可替换的模型接入点**，不是翻译策略的所在地：它只根据收到的请求生成
译文，不决定取哪些上下文、不写长期知识、不改游戏源码（Guide §15）。
"""

from __future__ import annotations

import json
import math
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, TYPE_CHECKING

from gametrans import prompts
from gametrans.core.ids import INTERNAL_PREFIXES, alias_index, id_forms, within_one_edit

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，避免循环导入
    from gametrans.core.tasks import TranslationTask


@dataclass
class CallRecord:
    """一次模型调用的计量。

    取不到的字段写 ``None`` —— "没拿到"与"是 0"是两件事，报告里必须能分开：
    前者是计量缺口，后者才是真的没有 token。

    ``requested_model`` 是**请求里发出去的那个名字**，``reported_model`` 是**响应里回来的那个**。
    两者分开留：M0 实测它们不一致（配置写 ``deepseek-v4-flash``，服务端报
    ``deepseek-v4.1-flash``），合成一个字段就再也看不出服务端漂移。
    """

    provider: str
    requested_model: str = ""
    reported_model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: 服务端自报的成本（单位由服务端决定，本项目不做货币换算）
    credit: float | None = None
    #: 提示词缓存：命中与未命中分开记，命中率才不是编出来的
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    #: 服务端为什么停下（``stop`` / ``length`` / ...）。空串 = 没报。
    #:
    #: 真靶实测（2026-09-20）：推理型模型把一个单元的**整个**输出预算花在推理上、
    #: 正文一个字都没出，而当时报告里既没有这个字段、也没有推理用量，运维者只能看到
    #: "响应不是合法 JSON"。截断是**可判定**的失败，所以它必须是个读数。
    finish_reason: str = ""
    #: 输出 token 里有多少花在推理上（``completion_tokens_details.reasoning_tokens``）。
    reasoning_tokens: int | None = None
    latency_ms: float | None = None
    items: int = 0
    unit_ids: list[str] = field(default_factory=list)
    phase: str = ""
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "requested_model": self.requested_model,
            "reported_model": self.reported_model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "credit": self.credit,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "finish_reason": self.finish_reason,
            "reasoning_tokens": self.reasoning_tokens,
            "latency_ms": self.latency_ms,
            "items": self.items,
            "unit_ids": list(self.unit_ids),
            "phase": self.phase,
            "ok": self.ok,
            "error": self.error,
        }


class CallLog:
    """一次运行里的模型调用流水。provider 往里写，报告从里读。

    并行模式下多个批次同时调用 provider，所以写入要加锁：少记一条，成本就被低估，
    而低估的成本不会自己暴露出来。
    """

    def __init__(self) -> None:
        self._records: list[CallRecord] = []
        self._lock = threading.Lock()

    def add(self, record: CallRecord) -> CallRecord:
        with self._lock:
            self._records.append(record)
        return record

    def records(self) -> list[CallRecord]:
        with self._lock:
            return list(self._records)

    def summary(self) -> dict[str, Any]:
        """给报告的汇总。token 取真的拿到过的那几次之和；一次都没拿到就是 ``None``。"""
        records = self.records()
        with_usage = [
            r
            for r in records
            if r.prompt_tokens is not None or r.completion_tokens is not None
        ]
        latencies = sorted(r.latency_ms for r in records if r.latency_ms is not None)

        def total(attr: str) -> int | None:
            """按**字段**统计：只有服务端报过这个字段才给数字，否则 ``None``。

            拿"报过的次数"当分母是错的：一次只报了 prompt 的调用不该让 completion 变成 0。
            """
            seen = [getattr(r, attr) for r in records if getattr(r, attr) is not None]
            return sum(seen) if seen else None

        credits = [r.credit for r in records if r.credit is not None]
        cache_hit = total("cache_hit_tokens")
        cache_miss = total("cache_miss_tokens")
        cache_total = (
            None
            if cache_hit is None and cache_miss is None
            else (cache_hit or 0) + (cache_miss or 0)
        )

        by_model: dict[str, int] = {}
        by_phase: dict[str, int] = {}
        for record in records:
            label = record.reported_model or record.requested_model or record.provider
            by_model[label] = by_model.get(label, 0) + 1
            name = record.phase or "translate"
            by_phase[name] = by_phase.get(name, 0) + 1

        return {
            "calls": len(records),
            "ok_calls": sum(1 for r in records if r.ok),
            "failed_calls": sum(1 for r in records if not r.ok),
            "usage_missing_calls": len(records) - len(with_usage),
            "prompt_tokens": total("prompt_tokens"),
            "completion_tokens": total("completion_tokens"),
            "total_tokens": total("total_tokens"),
            "credit_total": round(sum(credits), 6) if credits else None,
            "credit_missing_calls": len(records) - len(credits),
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "cache_hit_rate": (
                round((cache_hit or 0) / cache_total, 6) if cache_total else None
            ),
            "latency_ms_p50": _percentile(latencies, 0.50),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "latency_ms_total": round(sum(latencies), 3) if latencies else None,
            "by_model": by_model,
            "by_phase": by_phase,
            "records": [record.to_dict() for record in records],
        }


def _percentile(values: list[float], fraction: float) -> float | None:
    """线性插值百分位（与统计工具的默认口径一致）。没有观测就不给数字。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    weight = position - low
    return float(ordered[low] * (1.0 - weight) + ordered[high] * weight)


@dataclass
class RequestDump:
    """把**真正发给模型的东西**逐次写成文件（拦截取证）。

    谁都能说"请求里有/没有这段内容"，只有文件不会说谎。所以：

    * 默认关（环境变量 ``GAMETRANS_DUMP_REQUESTS`` 指向目录时开）；
    * 一次调用一个文件，按发送顺序编号；
    * 内容就是**请求体本身**（``messages`` 两段 + 模型参数），与 provider 发出去的一致；
    * 落盘失败**不影响翻译** —— 取证坏了是取证的事。
    """

    directory: Path
    calls: int = 0
    written: list[str] = field(default_factory=list)
    failure: str = ""

    def record(self, payload: dict[str, Any], *, phase: str = "", items: int = 0) -> None:
        self.calls += 1
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            name = f"{self.calls:04d}-{phase or 'call'}.json"
            path = self.directory / name
            body = dict(payload)
            body["_dump"] = {
                "call": self.calls,
                "phase": phase,
                "items": items,
                "model": str(payload.get("model") or ""),
            }
            path.write_text(
                json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            self.written.append(str(path))
        except Exception as exc:  # noqa: BLE001 - 取证失败不许影响主流程
            self.failure = f"{type(exc).__name__}: {exc}"


def open_request_dump(environment: dict[str, str] | None = None) -> "RequestDump | None":
    """按环境变量决定要不要落盘。未设或为空 → 不落盘。"""
    import os

    source = environment if environment is not None else os.environ
    target = str(source.get("GAMETRANS_DUMP_REQUESTS") or "").strip()
    if not target:
        return None
    return RequestDump(directory=Path(target))


@dataclass
class TranslationItem:
    """交给模型的一条待译内容。"""

    unit_id: str
    source: str
    path: str = ""
    context: str = ""
    speaker: str | None = None
    kind: str = ""
    #: 必须原样保留的结构 token（变量、标签、控制码……）。声明给模型看，
    #: 校验器再兜底 —— 两者缺一不可。
    protected: list[str] = field(default_factory=list)
    #: 这条内容对应的翻译任务（Guide §14：Executor 拿到的是 Task，不是一句字符串）
    task_id: str = ""
    #: 这一条所属的**翻译单元**（一个单元含多句时，同一个单元的各句共享它）。
    #: 标出来模型才知道"这几句是一场戏里的连续对白"，不是彼此无关的散句。
    unit_id_of: str = ""
    #: 单元的**可读名**（`act25` 这种）—— 给模型看的；`unit_id_of` 是内部身份，回填用。
    #: 让模型读到一长串哈希没有意义，读懂"这是哪一场戏"有意义。
    unit_label: str = ""
    #: 它在这个单元里的位置（``3/26``），同样只用于标注
    position: str = ""
    #: **已经有译文、只作为上下文出现**的那一条（翻译记忆精确命中）。
    #:
    #: 为什么不是"从请求里删掉"：单元的前提是"这几句是**连续对白**"。
    #: 把中间的句子摘掉之后，模型看到的是一句**悬空的回应**，而且"单元内第 i/n 句"
    #: 的编号会被重排（真靶实测：`Third time this week.` 从"第 4/7 句"变成"第 1/4 句"，
    #: 而它本来是上一句的回答）。所以带上它、标出来、**只不要求回译**。
    #: 顺带得到一个好处：已定译的写法就摆在模型眼前，等于把记忆当示例喂进去
    #: （TM 增强翻译的常见做法，见 `layers/memory.py` 的出处说明）。
    resolved: bool = False
    #: `resolved=True` 时，**已经沿用的那句译文**。要摆给模型看：
    #:
    #: 只给原文是不够的 —— 模型看不到"这句已经定成什么"，就没法判断上下文读起来通不通顺，
    #: 也没法沿用同一个写法（上下文里出现一个与它不同的称呼，模型只能猜）。
    #: 把既有译文摆出来，一是让上下文完整，二是**顺带把记忆当示例喂进去**
    #: （TM 增强翻译的常见做法，出处见 `layers/memory.py` 的模块说明）。
    reused_target: str = ""
    #: 这一条**要不要模型回译**。`resolved=True` 但 `expects_answer=False` 是 `keep` 档
    #: （已定译，只当上下文）；两者都为真则是 `polish` 档（已定译，但请模型顺手看一眼
    #: 通不通顺）。两者的区别只体现在**要不要带 id**：带 id 才是"要回填的条目"。
    expects_answer: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "source": self.source,
            "path": self.path,
            "context": self.context,
            "speaker": self.speaker,
            "kind": self.kind,
            "protected": list(self.protected),
            "task_id": self.task_id,
            "unit_id_of": self.unit_id_of,
            "unit_label": self.unit_label,
            "position": self.position,
            "resolved": self.resolved,
            "reused_target": self.reused_target,
            "expects_answer": self.expects_answer,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationItem":
        return cls(
            unit_id=str(data["unit_id"]),
            source=str(data["source"]),
            path=str(data.get("path", "")),
            context=str(data.get("context", "")),
            speaker=data.get("speaker"),
            kind=str(data.get("kind", "")),
            protected=[str(t) for t in (data.get("protected") or [])],
            task_id=str(data.get("task_id", "")),
            unit_id_of=str(data.get("unit_id_of", "")),
            unit_label=str(data.get("unit_label", "")),
            position=str(data.get("position", "")),
            resolved=bool(data.get("resolved", False)),
            reused_target=str(data.get("reused_target", "")),
            expects_answer=bool(data.get("expects_answer", True)),
        )


@dataclass
class TranslationRequest:
    """一次翻译调用的完整输入。

    ``knowledge`` 是上下文装配出来的背景资料，``instructions`` 是用户自定义的翻译
    要求 —— 两者都会被拼进提示词。

    ``tasks`` 是这次调用的**完整任务载荷**（Guide §14）：每个 item 都能通过
    ``task_id`` 找到它的 Task，里面带着检索结果、术语/风格/引擎约束与来源。不理解
    新载荷的 provider 只读 ``items`` 也能正常工作 —— 降级是显式的，不是隐藏的。
    """

    items: list[TranslationItem]
    target_language: str
    source_language: str = "auto"
    knowledge: str = ""
    instructions: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tasks: list["TranslationTask"] = field(default_factory=list)
    #: 这次调用的计量账本。不给就不记账 —— 老调用方与轻量 provider 不受影响。
    calls: "CallLog | None" = None
    #: 调用阶段标签（批量翻译 / 修复重试……），进计量记录，供按阶段拆成本
    phase: str = ""
    #: **调用流水**：provider 每次真实调用往这里追加一条（请求原文 + 服务端原始回复）。
    #: 不给就不留痕。留给它是因为"报告里只有 token 数"没法复盘一次实验到底发了什么、
    #: 模型到底回了什么 —— 出了问题只能靠重建请求去猜。
    transcript: list[dict[str, Any]] | None = None
    #: **请求落盘**（拦截）：设了它就把真正发出去的请求体逐次写成文件。
    #: 讨论"请求里到底有什么"时看文件，不看任何人的记忆或推断。
    dump: "RequestDump | None" = None
    #: **请求模板**：这一次请求长什么样（system 提示词 + 正文排版）全由它决定。
    #: 渲染只有一份实现（:mod:`gametrans.prompts`）—— 生产、拦截落盘、面板预览
    #: 走的是同一条路径，不存在"看一份、跑另一份"。
    prompt_template: dict[str, Any] = field(default_factory=prompts.named)
    #: 这一轮用的是**哪个模板**（名字）。只用来记账：台账要能回答"这一行是哪套模板
    #: 产出的"，而模板是用户自己的东西（以后还能来自创意工坊），所以记名字本身。
    template_name: str = ""
    #: **短 id 对照表**：``短编号 → 完整槽位身份``。只在"发给模型用短编号"的口径下才带上；
    #: 写回时按它还原，所以短编号只活在线路上，不进任何落盘身份。
    short_ids: dict[str, str] = field(default_factory=dict)
    #: **这一轮之前已经说过的话**（会话历史）：``[{"role": "user"/"assistant", "content": ...}]``。
    #:
    #: 长单元一次发不完时的形状：第一轮发第 1..N 条，第二轮只发第 N+1..2N 条 —— 前几轮的
    #: 原文与译文由这里带着（``[system] + history + [user]``），所以**不用重发、也不用写"继续"**。
    #: 历史**只追加不修改**：前缀稳定才吃得到服务端的 cache。
    history: list[dict[str, str]] = field(default_factory=list)
    #: ``别名 → 完整身份`` 的缓存（见 :meth:`id_aliases`）。渲染时不重建。
    _aliases: dict[str, str] | None = field(default=None, repr=False, compare=False)
    #: ``单元名 + 短编号 → 完整身份`` 的缓存（见 :meth:`unit_label_aliases`）。
    _label_aliases: dict[str, str] | None = field(default=None, repr=False, compare=False)
    #: 近失回配认回来的条目：``[(模型写的, 认成哪个身份)]``（见 :meth:`near_miss_hits`）。
    _near_miss: list[tuple[str, str]] = field(default_factory=list, repr=False, compare=False)

    def task_for(self, unit_id: str) -> "TranslationTask | None":
        for task in self.tasks:
            if task.unit_id == unit_id:
                return task
        return None

    # ---- 身份的回填 ---------------------------------------------------------

    #: 这些前缀是**内部**的分类标记（`id:` 是引擎清单的键、`keyed:` 是字符串表的键）。
    #: 它们对模型没有任何意义，实测模型会把它们规范化掉 —— 于是回填对不上、译文被静默丢。
    #: 规则本身在内核一处（:mod:`gametrans.core.ids`），面板判"哪几条没拿到"用的是同一份。
    _INTERNAL_PREFIXES: tuple[str, ...] = INTERNAL_PREFIXES

    @classmethod
    def _id_forms(cls, unit_id: str) -> set[str]:
        """一个身份**可能被写成**的所有样子（都要能还原回来）。见 :func:`id_forms`。"""
        return id_forms(unit_id, prefixes=cls._INTERNAL_PREFIXES)

    def id_aliases(self) -> dict[str, str]:
        """``可能的写法 → 完整身份``。**有歧义的一律不认** —— 两个条目都能对上同一个
        写法时，猜一个就是静默错位；宁可让它对不上，由 :meth:`resolve_id` 返回 ``None``
        并在报告里报出来。
        """
        if self._aliases is None:
            index, _ambiguous = alias_index([item.unit_id for item in self.items])
            self._aliases = index
        return self._aliases

    def resolve_id(self, raw: str) -> str | None:
        """把模型回填的那个字符串还原成完整身份；对不上或**有歧义**返回 ``None``。

        四种写法都认：完整身份、内部分类前缀被吃掉之后的样子（真靶上就是它）、
        compact 口径的单元内短编号（``short_ids``），以及**单元名 + 短编号**
        （模型照 `== 单元 act10 ==` 拼回来的那种，见下面 :meth:`_unit_label_aliases`）。
        """
        text = str(raw or "").strip()
        if not text:
            return None
        # 短编号走**同一套归一**：模型会把方括号一起抄回来（发 `[0001]`、回 `[[0001]]`），
        # 逐字比较同样会把整批成果判成没拿到（与 id: 前缀被吃掉是同一个坑）。
        for form in self._id_forms(text):
            if form in self.short_ids:
                return self.short_ids[form]
        aliases = self.id_aliases()
        found = {aliases[form] for form in self._id_forms(text) if form in aliases}
        if len(found) == 1:
            return next(iter(found))
        label_aliases = self.unit_label_aliases()
        found = {
            label_aliases[form]
            for form in self._id_forms(text)
            if form in label_aliases
        }
        if len(found) == 1:
            return next(iter(found))
        return self._near_miss_id(text)

    #: 近失回配只认**像哈希的那一段**：8 位以上的十六进制尾段。`act13_abc` 那种短编号
    #: 差一个字母就可能是别的条目 —— 那种宁可不认。
    NEAR_MISS_MIN_LEN = 8

    @classmethod
    def _hash_like(cls, text: str) -> bool:
        tail = str(text).rsplit("_", 1)[-1].strip("[]")
        return len(tail) >= cls.NEAR_MISS_MIN_LEN and all(
            char in "0123456789abcdef" for char in tail.lower()
        )

    def _near_miss_id(self, raw: str) -> str | None:
        """**抄错一个字符**的短哈希：认，但只在**唯一**能对上时认（真靶 2026-09-29）。

        形态：模型自己给短编号补上内部前缀、再把哈希抄坏一个字 —— `[79cfb185]` 回成
        `id:act13_c79cfb185`、`[2ab7c426]` 回成 `id:textingthecats_2ab7b426`。
        逐字比会把这两条已经译好的句子丢掉（槽位落成"没有译文"），而猜错会把译文贴到
        邻居身上 —— 所以规则是：**候选恰好一个才认，两个以上一律不认**（照旧报"没对上"）。
        认回来的一律记进 :meth:`near_miss_hits`，由调用方写进报告 —— 猜的和抄对的不一样。
        """
        text = str(raw or "").strip()
        if not self._hash_like(text):
            return None
        tables = (self.short_ids, self.id_aliases(), self.unit_label_aliases())
        candidates: set[str] = set()
        for table in tables:
            candidates.update(key for key in table if abs(len(key) - len(text)) <= 1)
        hits: dict[str, str] = {}
        for form in self._id_forms(text):
            if not self._hash_like(form):
                continue
            for key in candidates:
                if not self._hash_like(key):
                    continue
                if within_one_edit(form, key):
                    for table in tables:
                        resolved = table.get(key)
                        if resolved:
                            hits[resolved] = key
        if len(hits) != 1:
            return None
        identity = next(iter(hits))
        self._near_miss.append((text, identity))
        return identity

    def near_miss_hits(self) -> list[tuple[str, str]]:
        """模型抄坏的 id 里，被**唯一近失**认回来的那些（``[(写错的, 认成的)]``）。"""
        return list(self._near_miss)

    def unit_label_aliases(self) -> dict[str, str]:
        """``单元名 + 短编号 → 完整身份``（**有歧义的一律不认**）。

        为什么要有这一种：模型会照单元标题把短编号拼回全 —— `== 单元 act10 ==` 那个块里
        的短编号 `sakiquestions_fdeb3b98` 被回成 `act10_sakiquestions_fdeb3b98`。
        真靶实测（2026-09-29，act5+act10 那一段）：821 条里 **75 条**这么丢的。
        只在**唯一**能对上时认领 —— 猜一个就是静默错位。
        """
        if self._label_aliases is not None:
            return self._label_aliases
        by_full: dict[str, list[str]] = {}
        for identity, full in self.short_ids.items():
            by_full.setdefault(str(full), []).append(str(identity))
        index: dict[str, str] = {}
        ambiguous: set[str] = set()
        for item in self.items:
            label = str(getattr(item, "unit_label", "") or "").strip()
            if not label:
                continue
            for identity in by_full.get(str(item.unit_id), ()):
                if identity.startswith(label + "_"):
                    continue  # 短编号本来就带着单元名，不是这一种
                alias = f"{label}_{identity}"
                if alias in index and index[alias] != item.unit_id:
                    ambiguous.add(alias)
                    continue
                index.setdefault(alias, str(item.unit_id))
        for alias in ambiguous:
            index.pop(alias, None)
        self._label_aliases = index
        return index

    def render(self) -> str:
        """按这一次的**请求模板**渲染正文，并把短编号对照表填进 :attr:`short_ids`。

        **这是唯一的渲染入口**：``full`` / ``compact`` 以及用户自定义的模板，差别全在
        :mod:`gametrans.prompts` 的模板数据里（``show_position`` / ``short_ids`` 这些开关），
        这里没有第二份排版代码 —— 此前"改一份、跑另一份"就是栽在这上面。
        """
        body, short_ids = prompts.render_body(
            self.prompt_template or prompts.named(),
            target_language=self.target_language,
            source_language=self.source_language,
            knowledge=self.knowledge,
            instructions=self.instructions,
            items=list(self.items),
        )
        self.short_ids.clear()
        self.short_ids.update(short_ids)
        return body

    def system_message(self) -> str:
        """这一次请求的 system 消息 —— 与正文同一份模板、同一条渲染路径。

        provider 只调这一个方法，不去别处找 system 提示词：出厂那份、用户改过的那份、
        脚本预览出来的那份，都只能来自 :mod:`gametrans.prompts`。
        """
        return prompts.render_system(
            self.prompt_template or prompts.named(),
            target_language=self.target_language,
            source_language=self.source_language,
        )


class DeclaredTerm(NamedTuple):
    """模型申报的**一个实体**：``(原文写法, 译名, 设定)``。

    这就是术语书里的一行。``source`` 是这个词在**原文里真的出现过的写法** ——
    它同时是这一行的身份、设定那一侧的触发词，以及给人看的名字（用目标语言写的话，
    这条设定永远打不响：注入扫的是原文槽位，真靶实测 6/6 条中文键在 3,269 个槽位上
    命中 0 句，R62）。``target``（译名）与 ``profile``（设定）都可以空，空的那一栏
    就当没申报。
    """

    source: str
    target: str = ""
    profile: str = ""


def coerce_declared(entry: Any) -> "DeclaredTerm":
    """把一份申报规整成 :class:`DeclaredTerm`。

    接受三种写法：``DeclaredTerm`` 本身、``(原文, 译名)``、``(原文, 译名, 设定)``。
    器材与测试里"给一份固定申报"用元组最顺手，没必要逼着它们先包一层。
    """
    if isinstance(entry, DeclaredTerm):
        return entry
    values = list(entry) if isinstance(entry, (tuple, list)) else [entry]
    source = str(values[0]).strip() if values else ""
    target = str(values[1]).strip() if len(values) > 1 and values[1] else ""
    profile = str(values[2]).strip() if len(values) > 2 and values[2] else ""
    return DeclaredTerm(source=source, target=target, profile=profile)


@dataclass
class TranslationResult:
    """模型给出的一条译文。``unit_id`` 必须原样回填。"""

    unit_id: str
    target: str
    provider: str
    error: str | None = None
    #: 模型**自己申报**的本批实体（原文写法 → 译名 / 设定，见 :class:`DeclaredTerm`）。
    #:
    #: 这是"边翻译边产出"的来源：整部游戏离线抽术语（要靠"重复且一致"）在一段文本里
    #: 几乎必然全落空；让模型顺手申报，覆盖每一句、不多花一次调用。
    #: 申报一律是**模型产出**（hypothesis），只能落成候选或"未验证参考"，进不了术语约束。
    declared_terms: tuple[DeclaredTerm, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "target": self.target,
            "provider": self.provider,
            "error": self.error,
            "declared_terms": [
                {"source": item.source, "target": item.target, "profile": item.profile}
                for item in self.declared_terms
            ],
        }


class LLMProvider(ABC):
    """一个模型接入点。"""

    name: str = ""
    display_name: str = ""
    summary: str = ""
    #: 需要凭证的 provider 会在开工前被检查，避免跑到一半才发现没配 Key
    requires_credentials: bool = False

    def is_configured(self) -> bool:
        return True

    def configure(self, **credentials: Any) -> None:
        """把工作区里存的凭证交给它（默认什么都不做）。

        内核不认识任何 provider 的私有字段：它只把 ``credentials`` 原样转交，由 provider
        自己决定认哪几个键。环境变量的优先级由 provider 自己把握 —— 一般是环境变量优先。
        """

    @abstractmethod
    def translate(self, request: TranslationRequest) -> list[TranslationResult]:
        """翻译一批条目。抛 :class:`~gametrans.errors.ProviderError` 表示整批失败。"""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "summary": self.summary,
            "requires_credentials": self.requires_credentials,
            "is_configured": self.is_configured(),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"<{type(self).__name__} name={self.name!r}>"
