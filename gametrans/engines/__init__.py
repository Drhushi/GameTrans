"""引擎支持包的机制层（契约 + 身份声明 + 协议版本 + 装载 + 注册表）。

本包只放**机制**：``base`` 定义契约，``manifest`` 读身份声明，``protocol`` 判协议
版本，``loader`` 从目录装载，``registry`` 管理名册。具体引擎一律在
``gametrans.engines.<name>/`` 子包里 —— 它们**不被 import**，而是由装载器按目录
装载，和用户自己装在全局层 ``engines/`` 里的适配包走同一条路。
"""

from __future__ import annotations

from gametrans.engines.base import (
    EngineDetection,
    EngineSupportPack,
    EngineWriteBackResult,
    ExtractContext,
    WriteBackContext,
)
from gametrans.engines.registry import EngineRegistry, default_registry

__all__ = [
    "EngineDetection",
    "EngineRegistry",
    "EngineSupportPack",
    "EngineWriteBackResult",
    "ExtractContext",
    "WriteBackContext",
    "default_registry",
]
