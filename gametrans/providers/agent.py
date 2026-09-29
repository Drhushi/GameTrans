"""agent 额度接入点：不连 API，批次挂单等 agent 会话作答。

省钱的思路很直接：翻译流水线该做的都照做（调度、上下文装配、术语注入、约束校验），
唯独"问模型"这一步不开 API —— 把**本来要发给 API 的完整请求**原样挂单到工作区
（:mod:`gametrans.core.agentqueue`），由 agent 会话用自己的额度取单作答、交回来。
答案回到这里之后与 API 响应走**同一条**解析链（:func:`parse_answer_text`），
后续的条数核对、id 回填、术语标签守恒、对应闸全都照旧 —— 换的是运输方式，
不是质量判据。

等待是**阻塞**的：跑批进程停在挂单上轮询答案文件。谁在等、等多久，都要在报告与
账本里看得见；超时按一次失败调用记账（挂单原样留在盘上，答案仍可补交）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from gametrans.core.agentqueue import AgentQueue, QUEUE_DIRNAME
from gametrans.errors import AgentQueueError, ProviderError
from gametrans.providers.base import (
    CallRecord,
    LLMProvider,
    TranslationRequest,
    TranslationResult,
)
from gametrans.providers.openai_compat import parse_answer_text

#: 等答案的默认时长（秒）。作答的是一个人/一个 agent 会话，隔一会儿才回是常态；
#: 太短会把好端端的一批判成失败。环境变量 ``GAMETRANS_AGENT_WAIT`` 可改。
DEFAULT_WAIT_SECONDS = 3600.0


class AgentQuotaProvider(LLMProvider):
    """把"问模型"换成"挂单等作答"的 provider。

    ``queue_root`` 是挂单目录（``<workdir>/agent-requests/``）；没给（比如测试装配
    没带工作区）就如实报告未配置，而不是跑一半才发现没地方挂单。
    """

    name = "agent"
    display_name = "agent（会话额度）"
    summary = "不连 API：批次挂单到工作区，由 agent 会话用自己的额度作答"
    requires_credentials = False

    def __init__(
        self,
        queue_root: Path | str | None = None,
        *,
        wait_seconds: float | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.queue_root = Path(queue_root) if queue_root else None
        if wait_seconds is None:
            wait_seconds = _wait_from_env()
        self.wait_seconds = float(wait_seconds)
        self.poll_seconds = float(poll_seconds)

    def is_configured(self) -> bool:
        return self.queue_root is not None

    def translate(self, request: TranslationRequest) -> list[TranslationResult]:
        if not request.items:
            return []
        if self.queue_root is None:
            raise ProviderError(
                f"{self.display_name} 没有挂单位置（不知道工作区在哪）",
                hint="内核装配时应传入工作区路径；单独拿它做实验请显式给 queue_root。",
            )
        queue = AgentQueue(self.queue_root)
        messages = self._messages(request)
        parked = queue.park(
            messages=messages,
            expected=sum(1 for item in request.items if item.expects_answer),
            unit_ids=[item.unit_id for item in request.items],
            phase=request.phase,
        )
        if request.dump is not None:
            # 与 openai 通道同一份口径：落的就是"真正发出去的请求"——这里是挂单全文
            request.dump.record(
                {**payload_of(messages), "agent_request_id": parked["request_id"],
                 "agent_queue": str(self.queue_root)},
                phase=request.phase,
                items=len(request.items),
            )
        started = time.perf_counter()
        try:
            answer = queue.wait_for_answer(
                parked["request_id"],
                timeout_s=self.wait_seconds,
                poll_s=self.poll_seconds,
            )
        except AgentQueueError as exc:
            self._record(
                request,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                ok=False,
                error=str(exc),
            )
            raise ProviderError(str(exc)) from None
        latency_ms = (time.perf_counter() - started) * 1000.0
        if answer is None:
            message = (
                f"挂单 {parked['request_id']} 等待作答超时（等了 {self.wait_seconds:.0f} 秒）"
            )
            self._record(request, latency_ms=latency_ms, ok=False, error=message)
            raise ProviderError(
                message,
                hint=(
                    "挂单原样留在盘上，答案仍可补交：agent 用 "
                    f"`agent submit {parked['request_id']} --answer <json>` 交回后重跑这一轮"
                    "（已交过答案的直接重跑即可）；或调大 GAMETRANS_AGENT_WAIT。"
                ),
            )
        try:
            results = parse_answer_text(answer, provider=self.name)
        except ProviderError:
            self._record(request, latency_ms=latency_ms, ok=False, error="答案格式不合法")
            raise
        queue.mark_collected(parked["request_id"])
        self._record(request, latency_ms=latency_ms, ok=True)
        return results

    # ---- 零件 ---------------------------------------------------------------

    @staticmethod
    def _messages(request: TranslationRequest) -> list[dict[str, str]]:
        """与 openai 兼容通道同一份组装：system + 会话历史 + 正文（都来自请求模板）。"""
        return [
            {"role": "system", "content": request.system_message()},
            *[dict(message) for message in request.history],
            {"role": "user", "content": request.render()},
        ]

    def _record(
        self,
        request: TranslationRequest,
        *,
        latency_ms: float,
        ok: bool,
        error: str = "",
    ) -> None:
        if request.calls is None:
            return
        # token 与 credit 一律留空：agent 额度没有服务端账单，"没拿到"与"是 0"
        # 必须分得开 —— 报告里 usage_missing 的那些就是这一类。
        request.calls.add(
            CallRecord(
                provider=self.name,
                requested_model="agent-session",
                latency_ms=latency_ms,
                items=len(request.items),
                unit_ids=[item.unit_id for item in request.items],
                phase=request.phase,
                ok=ok,
                error=error,
            )
        )


def payload_of(messages: list[dict[str, str]]) -> dict[str, Any]:
    """给拦截落盘用的请求体形状（与 openai 通道的 payload 对齐，去掉模型参数）。"""
    return {"messages": [dict(message) for message in messages]}


def _wait_from_env() -> float:
    raw = str(os.environ.get("GAMETRANS_AGENT_WAIT") or "").strip()
    if not raw:
        return DEFAULT_WAIT_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_WAIT_SECONDS


__all__ = [
    "AgentQuotaProvider",
    "DEFAULT_WAIT_SECONDS",
    "QUEUE_DIRNAME",
    "payload_of",
]
