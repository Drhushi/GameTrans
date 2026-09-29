"""离线占位 provider。

存在的意义不是"翻译得好"，而是让**整条链路可以零网络、零密钥地跑通并被测试**。
默认 provider 是它就是出于这个考虑。
"""

from __future__ import annotations

from gametrans.providers.base import (
    DeclaredTerm,
    LLMProvider,
    TranslationRequest,
    TranslationResult,
    coerce_declared,
)


class MockProvider(LLMProvider):
    """按固定模板产出占位译文，输入相同则输出相同。

    ``declared_terms`` 给它一份**固定申报**（默认空）：真实模型申报什么只有真跑才知道，
    但"申报 → 候选 → 批准 → 下一轮进约束"这条**回路**通不通是器材问题，
    必须能零网络地反复验（见 `tests/test_declared_asset_loop.py`）。
    """

    name = "mock"
    display_name = "占位翻译（离线）"
    summary = "不调用任何模型，按固定模板产出占位译文，用于离线跑通与自动化测试。"
    requires_credentials = False

    def __init__(
        self,
        template: str = "【{lang}】{source}",
        *,
        declared_terms: tuple[DeclaredTerm, ...] = (),
    ) -> None:
        self.template = template
        #: 每一条结果都带上这份申报 —— 真实模型也是这样（申报跟着响应走，不按条目分）
        self.declared_terms = tuple(coerce_declared(item) for item in declared_terms)
        #: 收到过的请求。留给 agent / 用户核对"到底发出去了什么"，
        #: 也让"术语表真的进了提示词"这件事可以被端到端断言。
        self.requests: list[TranslationRequest] = []

    def translate(self, request: TranslationRequest) -> list[TranslationResult]:
        self.requests.append(request)
        return [
            TranslationResult(
                unit_id=item.unit_id,
                target=self.template.format(
                    lang=request.target_language, source=item.source
                ),
                provider=self.name,
                declared_terms=self.declared_terms,
            )
            for item in request.items
        ]

    @property
    def last_request(self) -> TranslationRequest | None:
        return self.requests[-1] if self.requests else None

    def clear_requests(self) -> None:
        self.requests.clear()
