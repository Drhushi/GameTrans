"""组合根 —— 内核与"有哪些适配包"之间唯一的那道门。

内核（core / layers / providers / cli / mcp_server / web）一律不知道 Ren'Py 与
RPG Maker 的存在；它们只接受注入进来的
:class:`~gametrans.engines.registry.EngineRegistry`。

**这里也不再 import 任何具体引擎。** 适配包是被"装载"的，不是被 import 的：

* 随内核发布的适配（``gametrans/engines/<名字>/``）本身就是一个普通适配包目录；
* 用户自己装的放在全局层的 ``engines/`` 里（见 :func:`gametrans.userconfig.engines_dir`）；
* 两者由 :mod:`gametrans.engines.loader` 走同一条路装载，同名时用户那份接管。

所以"接一个新引擎"是从此**一行内核代码都不用改**：写一个目录、放一份身份声明、
丢进引擎目录即可。``tests/test_engine_boundary.py`` 把这条边界钉死在 AST 上 ——
连本文件也不豁免。
"""

from __future__ import annotations

from typing import Any

from gametrans.engines.registry import EngineRegistry
from gametrans.providers.registry import ProviderRegistry

__all__ = ["build_registry", "build_providers"]


def build_registry() -> EngineRegistry:
    """装载全部引擎适配包（随内核发布的 + 本机用户装的）。"""
    from gametrans.engines.loader import load_registry

    return load_registry()


def build_providers(
    credentials: dict[str, str] | None = None,
    *,
    request_overrides: dict[str, Any] | None = None,
    workdir: Any = None,
) -> ProviderRegistry:
    """装载全部内置 LLM provider。

    ``credentials`` 是工作区里存的凭证（见 :mod:`gametrans.credentials`）。内核把整份
    字典原样转交给每个 provider，由它自己决定认哪几个键 —— 组合根不认识任何 provider
    的私有字段，也不判断谁需要密钥。

    ``request_overrides`` 是请求参数的透传项（例如推理型模型的 ``max_tokens``）。
    它由配置层解析好，组合根同样**不解释内容**，只转交。

    ``workdir`` 是项目工作区：agent 额度通道（provider ``agent``）的挂单目录
    （``<workdir>/agent-requests/``）由它定位。没带工作区的装配（老调用方/纯实验）
    照旧成立，只是那个 provider 会如实报告"没地方挂单"。
    """
    from gametrans.providers.agent import AgentQuotaProvider
    from gametrans.providers.mock import MockProvider
    from gametrans.providers.openai_compat import OpenAICompatProvider

    registry = ProviderRegistry()
    registry.register(MockProvider())
    # 300s：推理型模型（GLM-5.3 之类）翻译长段落时要思考很久，默认 60s 会误伤
    provider = OpenAICompatProvider(timeout=300.0, request_overrides=request_overrides or {})
    if credentials:
        provider.configure(**credentials)
    registry.register(provider)
    registry.register(
        AgentQuotaProvider(
            queue_root=(workdir / "agent-requests") if workdir else None
        )
    )
    return registry
