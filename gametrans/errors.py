"""分层的错误类型。

设计原则（对应 spec 的 failure modes）：所有面向用户的失败都抛 ``GameTransError``
的子类，携带可执行的修复建议；CLI 顶层捕获后打印人话，绝不把裸 traceback 甩给
用户；只有 ``--debug`` 才展开堆栈。
"""

from __future__ import annotations


class GameTransError(Exception):
    """全部可预期错误的基类。"""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        if self.hint:
            return f"{self.message}\n提示：{self.hint}"
        return self.message


class ConfigError(GameTransError):
    """配置文件缺失、损坏或字段非法。"""


class ProjectError(GameTransError):
    """项目路径非法、未初始化或缺少必要文件。"""


class EngineError(GameTransError):
    """引擎支持包缺失、重复或加载失败。"""


class ExtractError(GameTransError):
    """提取阶段无法继续。"""


class TranslateError(GameTransError):
    """翻译阶段无法继续（单条失败不在此列，见报告机制）。"""


class WriteBackError(GameTransError):
    """写回阶段无法继续。"""


class SkeletonError(GameTransError):
    """引擎骨架的生成、解析或填充无法继续（适配层）。"""


class ResourceError(GameTransError):
    """资源层的读写或校验失败。"""


class InteractionError(GameTransError):
    """交互层的视图/策略操作失败。"""


class WebError(GameTransError):
    """WebUI 面板启动或服务失败。"""


class AgentQueueError(GameTransError):
    """挂单队列（agent 作答通道）的读写或状态错。"""


class ProviderError(GameTransError):
    """LLM provider 未配置或调用失败。

    ``retryable`` / ``status`` 是给**调用方**看的结构化事实，不用从错误文本里猜：
    限流（429）与 5xx 是**暂时**的，等一会儿再来一次就该好；401/403/404/参数错是
    **永久**的，重试只是把同样的错再买一遍。真靶上丢过一整场戏（90 条槽位全落成
    "没有译文"）就是因为 429 与"连不上"被混成一类、一律不重试。
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool = False,
        status: int = 0,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message, hint=hint)
        self.retryable = retryable
        self.status = status
        #: 服务端明说要等多久（`Retry-After`）。0 = 服务端没说，由调用方退避。
        self.retry_after = retry_after
