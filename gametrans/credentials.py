"""模型凭证：单独一个文件，和项目配置分开存。

为什么分开：``project.json`` 是**可以直接贴出来**的普通配置（面板会展示它、报告会引用它、
文档邀请人手工编辑它），把密钥混进去，迟早会跟着某次输出漏出去。凭证因此住在
``<工作区>/credentials.json``：在版本库之外（``.gametrans/`` 已被忽略）、权限收到 0600、
对外**只显示尾 4 位**。

环境变量优先于这个文件 —— 它是"没设环境变量时的兜底"，不是覆盖。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from gametrans.errors import ConfigError

__all__ = [
    "CREDENTIALS_FILE",
    "CREDENTIAL_FIELDS",
    "Credentials",
    "mask_secret",
    "normalize_base_url",
]

CREDENTIALS_FILE = "credentials.json"

#: 这个文件管哪些字段。``api_key`` 是秘密，另外两个是"跟着密钥一起走的接入参数"。
CREDENTIAL_FIELDS: tuple[str, ...] = ("api_key", "base_url", "model")

#: 路径里的连续斜杠。协议那部分（``http://``）由 urlsplit 分开处理，不会被误伤。
_REPEATED_SLASHES = re.compile(r"/{2,}")


def normalize_base_url(value: str) -> str:
    """把手填的接口地址收干净：路径里的重复斜杠合并、尾部斜杠去掉。

    多一个斜杠是常见的手滑，代价却是调用直接失败：``http://host//v1`` 会让服务端回
    307，而跟在 307 后面的 POST 不会自动重发。与其让人对着 307 猜，不如在这里收掉。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.scheme and not parts.netloc:
        # 没写协议的：只收斜杠，不替人补协议
        return _REPEATED_SLASHES.sub("/", raw).rstrip("/")
    path = _REPEATED_SLASHES.sub("/", parts.path).rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def mask_secret(value: str) -> str:
    """只留下尾巴，够确认"是哪一把"就够了。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "…"
    return "…" + text[-4:]


@dataclass
class Credentials:
    """一份凭证。``problems`` 是**读取时**的诊断，不写回文件。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    def masked(self) -> dict[str, Any]:
        """对外的一律用这一份：密钥只剩尾巴。"""
        return {
            "api_key": mask_secret(self.api_key),
            "configured": self.configured,
            "base_url": self.base_url,
            "model": self.model,
        }

    def storage_dict(self) -> dict[str, str]:
        """**含明文密钥**，只在写文件与装配 provider 时用，不许塞进任何响应。"""
        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model": self.model,
        }

    def merged(self, **fields: Any) -> "Credentials":
        payload = self.storage_dict()
        for key, value in fields.items():
            if key not in CREDENTIAL_FIELDS:
                raise ConfigError(
                    f"未知的凭证字段：{key!r}",
                    hint=f"可写：{', '.join(CREDENTIAL_FIELDS)}。",
                )
            if value is None:
                continue
            text = str(value).strip()
            payload[key] = normalize_base_url(text) if key == "base_url" else text
        return Credentials(**payload)

    # ---- 磁盘 ---------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Credentials":
        """读不出来就当没配 —— 不报错、也不覆盖那个坏文件（它可能是人手写坏的）。"""
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = getattr(exc, "msg", str(exc))
            return cls(problems=[f"{path.name} 读不出来（{detail}），这次按未配置处理"])
        if not isinstance(payload, dict):
            return cls(problems=[f"{path.name} 顶层不是对象，这次按未配置处理"])
        return cls(
            api_key=str(payload.get("api_key") or ""),
            # 读的时候也收一遍：显示出来的就是实际会用的那个形态
            base_url=normalize_base_url(str(payload.get("base_url") or "")),
            model=str(payload.get("model") or ""),
        )

    @classmethod
    def load_values(cls, path: Path) -> tuple[dict[str, Any], list[str]]:
        """读**原始键值**（不补默认、不归一），供分层解析用。

        为什么不能用 :meth:`load`：它把缺的键补成空串，于是"没设过"与"设成了空"
        变得无法区分 —— 而四层配置里正是靠这个区别决定要不要回落下一层。
        """
        path = Path(path)
        if not path.exists():
            return {}, []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = getattr(exc, "msg", str(exc))
            return {}, [f"{path.name} 读不出来（{detail}），这次按未配置处理"]
        if not isinstance(payload, dict):
            return {}, [f"{path.name} 顶层不是对象，这次按未配置处理"]
        return dict(payload), []

    def save(self, path: Path) -> None:
        """写盘并尽量把权限收到 0600（POSIX）。失败就如实报错，不静默丢弃。"""
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.storage_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _restrict(path)
        except OSError as exc:
            raise ConfigError(
                f"凭证写不进去：{exc}",
                hint=f"确认 {path.parent} 可写。",
            ) from None


def _restrict(path: Path) -> None:
    """尽力把权限锁到 0600。Windows 没有 POSIX 权限位，跳过。"""
    if os.name == "nt":  # pragma: no cover - 只在 POSIX 上生效
        return
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - 文件系统不支持时不该让保存失败
        pass


# --------------------------------------------------------------------------- #
# 分层解析：环境变量 > 项目凭证 > 全局凭证 > 空
# --------------------------------------------------------------------------- #

#: 环境变量名 → 它覆盖哪个字段。两个密钥名都认（``OPENAI_API_KEY`` 是历史兼容）。
ENV_FIELDS: dict[str, str] = {
    "GAMETRANS_API_KEY": "api_key",
    "OPENAI_API_KEY": "api_key",
    "GAMETRANS_BASE_URL": "base_url",
    "GAMETRANS_MODEL": "model",
}


def _present(values: dict[str, Any], key: str) -> bool:
    """这一层**有没有设过**这一项。

    注意与"设成了空串"不同：空串是"我就要空"，会压住下一层；缺键才是"没设过"，
    回落下一层。四层配置里这是唯一的撤销语义 —— 混了就没法撤销上一层。
    """
    return key in values and values[key] is not None


def resolve_credentials(
    project_values: dict[str, Any] | None = None,
    global_values: dict[str, Any] | None = None,
    *,
    include_env: bool = True,
) -> Credentials:
    """按层合并出**实际会用**的凭证。环境变量逐字段压过两层文件。

    ``include_env=False`` 时只看两层文件 —— 面板的回显要的是这个：**显示存下来的
    那一把的尾巴**，再用 ``env_override`` 另外告诉用户"环境变量把它压住了"。
    把环境变量算进来展示，用户会看到一把自己从没存过、也改不掉的密钥。
    """
    project = project_values or {}
    globals_ = global_values or {}
    resolved: dict[str, str] = {}
    for key in CREDENTIAL_FIELDS:
        from_env = ""
        if include_env:
            for name, field in ENV_FIELDS.items():
                if field == key and os.environ.get(name):
                    from_env = os.environ[name]
                    break
        if from_env:
            resolved[key] = str(from_env)
            continue
        if _present(project, key):
            resolved[key] = str(project[key] or "")
            continue
        if _present(globals_, key):
            resolved[key] = str(globals_[key] or "")
            continue
        resolved[key] = ""
    return Credentials(
        api_key=resolved["api_key"],
        base_url=normalize_base_url(resolved["base_url"]),
        model=resolved["model"],
    )


def resolve_credentials_sources(
    project_values: dict[str, Any] | None = None,
    global_values: dict[str, Any] | None = None,
    *,
    include_env: bool = True,
) -> dict[str, str]:
    """每一项的生效值来自哪一层：``env`` / ``project`` / ``global`` / ``empty``。"""
    project = project_values or {}
    globals_ = global_values or {}
    sources: dict[str, str] = {}
    for key in CREDENTIAL_FIELDS:
        from_env = include_env and any(
            field == key and os.environ.get(name) for name, field in ENV_FIELDS.items()
        )
        if from_env:
            sources[key] = "env"
        elif _present(project, key):
            sources[key] = "project"
        elif _present(globals_, key):
            sources[key] = "global"
        else:
            sources[key] = "empty"
    return sources


resolve_credentials.sources = resolve_credentials_sources  # type: ignore[attr-defined]
