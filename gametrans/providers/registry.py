"""provider 名册。和引擎名册一样：agent 可以查询、可以换、可以自己注册。"""

from __future__ import annotations

from typing import Iterable, Iterator

from gametrans.errors import ProviderError
from gametrans.providers.base import LLMProvider


class ProviderRegistry:
    def __init__(self, providers: Iterable[LLMProvider] = ()) -> None:
        self._providers: dict[str, LLMProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: LLMProvider) -> LLMProvider:
        if not provider.name:
            raise ProviderError("provider 必须声明 name")
        if provider.name in self._providers:
            raise ProviderError(
                f"provider {provider.name!r} 已经注册过了",
                hint="同一个名字只能有一个 provider；请先注销或改名。",
            )
        self._providers[provider.name] = provider
        return provider

    def unregister(self, name: str) -> None:
        if name not in self._providers:
            raise ProviderError(
                f"未注册的 provider：{name!r}",
                hint=f"当前可用：{', '.join(self.names()) or '（无）'}",
            )
        del self._providers[name]

    def get(self, name: str) -> LLMProvider:
        try:
            return self._providers[name]
        except KeyError:
            available = ", ".join(self.names()) or "（无）"
            raise ProviderError(
                f"未找到 provider：{name!r}",
                hint=(
                    f"当前可用：{available}。"
                    "用 `gametrans config providers` 查看详情；"
                    "接新模型只需实现 LLMProvider 并注册进 bootstrap。"
                ),
            ) from None

    def names(self) -> list[str]:
        return sorted(self._providers)

    def all(self) -> list[LLMProvider]:
        return [self._providers[name] for name in self.names()]

    def describe(self) -> list[dict]:
        return [p.describe() for p in self.all()]

    def __contains__(self, name: object) -> bool:
        return name in self._providers

    def __iter__(self) -> Iterator[LLMProvider]:
        return iter(self.all())

    def __len__(self) -> int:
        return len(self._providers)


def build_provider_registry() -> ProviderRegistry:
    """内置 provider 名册。用函数内 import 指向组合根，避免循环依赖。"""
    from gametrans.bootstrap import build_providers

    return build_providers()
