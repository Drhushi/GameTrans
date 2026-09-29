"""翻译层用到的模型接入抽象。

内核不认识任何具体厂商：只认识"把一批待译条目变成一批译文"这件事。接哪家模型
是配置问题，不是架构问题 —— 火山方舟、DeepSeek、本地 vLLM 只要讲 OpenAI 兼容
协议，就能用同一个 provider。
"""

from __future__ import annotations

from gametrans.providers.base import (
    LLMProvider,
    TranslationItem,
    TranslationRequest,
    TranslationResult,
)
from gametrans.providers.mock import MockProvider
from gametrans.providers.openai_compat import OpenAICompatProvider
from gametrans.providers.registry import ProviderRegistry, build_provider_registry

__all__ = [
    "LLMProvider",
    "MockProvider",
    "OpenAICompatProvider",
    "ProviderRegistry",
    "TranslationItem",
    "TranslationRequest",
    "TranslationResult",
    "build_provider_registry",
]
