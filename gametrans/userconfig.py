"""全局默认层 —— ``~/.gametrans/``。

这一层装的是"**我这个用户、这台机器**"的习惯，与具体哪个游戏无关：接入三件套、
provider、每批条数、并行度、自定义要求。换一个项目不用再配一遍。

三个刻意的设计：

* **两个文件，不是一个**。``config.json`` 是能直接贴出来的普通配置，
  ``credentials.json`` 是 0600 的密钥 —— 这条规矩不因为搬到全局就破
  （理由见 :mod:`gametrans.credentials`）。
* **位置可被 ``GAMETRANS_HOME`` 覆盖**，优先级最高。测试与 CI 靠它把全局层关进
  临时目录，绝不读写开发机上那份真实配置；也顺便支持"配置跟着工具走"。
* **只收与游戏无关的字段**。``target_language`` / ``source_language`` 这类一律**拒绝**
  并说明理由 —— 见 :data:`gametrans.config.PROJECT_ONLY_FIELDS` 里 R30 的那条教训。
  光在文档里写一句"别放"不够：那要靠人记得。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gametrans.config import (
    GLOBAL_FIELDS,
    PROJECT_ONLY_FIELDS,
    SCHEMA,
    resolve_config,
    resolve_config_sources,
)
from gametrans.credentials import (
    CREDENTIAL_FIELDS,
    Credentials,
    resolve_credentials,
    resolve_credentials_sources,
)
from gametrans.errors import ConfigError

__all__ = [
    "ENV_HOME",
    "ENGINES_DIR_NAME",
    "GLOBAL_CONFIG_FILE",
    "GLOBAL_CREDENTIALS_FILE",
    "engines_dir",
    "global_dir",
    "global_config_path",
    "global_credentials_path",
    "load_global_config",
    "save_global_config",
    "load_global_credentials",
    "save_global_credentials",
    "clear_global_config_field",
    "clear_global_credential_field",
    "project_only_reason",
    # 分层解析的入口放在这里：调用方只需要认识"分层"这一件事
    "resolve_config",
    "resolve_config_sources",
    "resolve_credentials",
    "resolve_credentials_sources",
    "resolve_all",
]


def resolve_all(
    project_config: dict[str, Any] | None = None,
    project_credentials: dict[str, Any] | None = None,
):
    """一次把两样都解析出来（配置 + 凭证），顺带给出各字段来源。

    返回 ``(config, credentials, sources)``，``sources`` 里两张表分开：
    ``{"config": {...}, "credentials": {...}}``。
    """
    global_config, _ = load_global_config()
    global_credentials, _ = load_global_credentials()
    config = resolve_config(project_config or {}, global_config)
    credentials = resolve_credentials(project_credentials or {}, global_credentials)
    sources = {
        "config": resolve_config_sources(project_config or {}, global_config),
        "credentials": resolve_credentials_sources(
            project_credentials or {}, global_credentials
        ),
    }
    return config, credentials, sources

#: 覆盖全局层位置的环境变量（最高优先级）。
ENV_HOME = "GAMETRANS_HOME"
#: 全局目录名 —— 与项目工作区同名，但语义不同（见模块文档与契约登记）。
GLOBAL_DIR_NAME = ".gametrans"
GLOBAL_CONFIG_FILE = "config.json"
GLOBAL_CREDENTIALS_FILE = "credentials.json"
#: 引擎适配包装在全局层的这个子目录里（一个子目录一个适配包）。
ENGINES_DIR_NAME = "engines"

#: 拒绝时给出的理由 —— 把它写在这里，而不是散在报错字符串里。
#:
#: 措辞刻意**不点具体引擎的名**：这是内核的常量，内核不认识任何具体引擎
#: （`tests/test_engine_boundary.py` 会扫代码里的引擎名）。例子照样给得出来，
#: 因为"有的游戏认 `chinese` 而不是 `zh_CN`"这件事本身与引擎是谁无关。
_PROJECT_ONLY_REASON: dict[str, str] = {
    "target_language": (
        "目标语言是**每个游戏不一样**的东西：游戏认的语言代码各不相同"
        "（有的游戏把它注册成 `chinese` 而不是 `zh_CN`），继承错了，译文进得去、"
        "玩家点不到 —— 整个交付就废了（契约 R30）。请把它设在项目里。"
    ),
    "source_language": (
        "源语言描述的是**这个游戏的内容是什么语言**，而且它会决定产物里带哪一份"
        "“原文”表（某些引擎那边就是 `<代码>.json` 这个文件名）。放在全局会让每个"
        "项目的产物形状都被改掉。请把它设在项目里。"
    ),
    "engine": "引擎由探测决定，不是配置项；写进全局没有任何意义。",
    "require_structure": (
        "要不要结构闸门是**项目级的风险决定**（关掉它等于放弃受保护结构的校验），"
        "不该被一条全局默认静默关掉。请把它设在项目里。"
    ),
    "engine_options": (
        "引擎私有选项（例如某个引擎要的 SDK 路径）属于某个具体引擎，"
        "放到全局会跨引擎串味。请把它设在项目里。"
    ),
}


def global_dir() -> Path:
    """全局层在哪。``GAMETRANS_HOME`` 优先，否则 ``~/.gametrans``。"""
    import os

    override = str(os.environ.get(ENV_HOME) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / GLOBAL_DIR_NAME


def global_config_path() -> Path:
    return global_dir() / GLOBAL_CONFIG_FILE


def engines_dir() -> Path:
    """用户装的引擎适配包放在哪。

    刻意放在**全局层**（而不是应用目录里）：适配包是"我这台机器上装的东西"，
    而应用目录是覆盖式解压的 —— 放那里的话，用户魔改过的适配会在升级时被冲掉，
    而那正是他要解决的问题。跟着全局层走，也就跟着 ``GAMETRANS_HOME`` 走，
    测试因此能把整个装载路径关进临时目录。
    """
    return global_dir() / ENGINES_DIR_NAME


def global_credentials_path() -> Path:
    return global_dir() / GLOBAL_CREDENTIALS_FILE


#: 面板 launcher 状态文件（panel.json：最近项目 + 壳偏好）。
PANEL_STATE_FILE = "panel.json"
#: 冻结版被装进只读目录（如 Program Files）时，状态退到的目录名。
FROZEN_STATE_DIR_NAME = "GameTrans"


def panel_state_file() -> Path:
    """面板 launcher 状态（``panel.json``）在哪 —— 三个使用方共用这一份解析。

    使用方：``web.app``（切项目后记住）、``open_panel``（上一次的项目）、
    ``desktop_shell``（最近列表 + 主题偏好）。三处各自解析就会漂移；冻结打包
    （PyInstaller）后更会各自悄悄指进包内的只读目录 —— 写不进去又不报错，
    症状是"最近列表永远是空的"。收口到这里，落点按优先级：

    1. ``GAMETRANS_HOME``（测试与 CI 用它把状态关进沙箱）；
    2. 冻结版：exe 旁 ``.gametrans``（便携盘 / 用户目录下可写就写旁边）；
    3. 冻结版：exe 旁只读（装进了 Program Files）退 ``%LOCALAPPDATA%/GameTrans``；
    4. 源码检出：``<检出根>/.gametrans``（老落点，不动）。
    """
    import os
    import sys

    override = str(os.environ.get(ENV_HOME) or "").strip()
    if override:
        return Path(override).expanduser() / PANEL_STATE_FILE
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent / GLOBAL_DIR_NAME
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".writable-probe"
            probe.write_bytes(b"")
            probe.unlink()
            return candidate / PANEL_STATE_FILE
        except OSError:
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / FROZEN_STATE_DIR_NAME
            base.mkdir(parents=True, exist_ok=True)
            return base / PANEL_STATE_FILE
    return Path(__file__).resolve().parent.parent.parent / GLOBAL_DIR_NAME / PANEL_STATE_FILE


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def reject_project_only(payload: dict[str, Any], path: Path) -> None:
    offenders = sorted(set(payload) & set(PROJECT_ONLY_FIELDS))
    if not offenders:
        return
    first = offenders[0]
    raise ConfigError(
        f"全局配置里不能有这些字段：{'、'.join(offenders)}（{path}）",
        hint=project_only_reason(first),
    )


def project_only_reason(key: str) -> str:
    """为什么这个字段不能放进全局层 —— 报错时要说清理由，不能只说"不许"。"""
    return _PROJECT_ONLY_REASON.get(key, "它只属于某个具体项目。")


# 兼容旧名字（本模块内部与测试都用 reject_project_only）
_reject_project_only = reject_project_only


def load_global_config() -> tuple[dict[str, Any], list[str]]:
    """读全局配置。**目录或文件不存在都只是"没配过"**，不是错误。

    坏文件：警告 + 当作没配过，**绝不覆盖那个文件**（它可能是人手写坏的，
    清掉就等于把人的东西弄丢了）。项目专属字段则直接**报错拒绝**。
    """
    path = global_config_path()
    if not path.is_file():
        return {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "msg", str(exc))
        return {}, [f"{path} 读不出来（{detail}），这次当作没有全局配置"]
    if not isinstance(payload, dict):
        return {}, [f"{path} 顶层不是对象，这次当作没有全局配置"]

    _reject_project_only(payload, path)

    known: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in payload.items():
        if key not in SCHEMA or key not in GLOBAL_FIELDS:
            warnings.append(f"{path} 里有不是全局字段的 {key!r}，已忽略")
            continue
        expected = SCHEMA[key]
        if expected is int and isinstance(value, bool):
            warnings.append(f"{path} 的 {key} 期望整数，实际是布尔值，已忽略")
            continue
        if not isinstance(value, expected):
            warnings.append(
                f"{path} 的 {key} 期望 {expected.__name__}，"
                f"实际是 {type(value).__name__}，已忽略"
            )
            continue
        known[key] = value
    return known, warnings


def save_global_config(values: dict[str, Any]) -> Path:
    """写全局配置。只写传进来的那些键（不补默认值 —— 那会把出厂默认烤进全局层）。"""
    _reject_project_only(values, global_config_path())
    unknown = sorted(set(values) - set(GLOBAL_FIELDS))
    if unknown:
        raise ConfigError(
            f"这些字段不能放进全局配置：{'、'.join(unknown)}",
            hint=f"可以放：{', '.join(GLOBAL_FIELDS)}。",
        )
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(values), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------- #
# 凭证
# --------------------------------------------------------------------------- #


def load_global_credentials() -> tuple[dict[str, Any], list[str]]:
    """读全局密钥的**原始键值**（不补默认）。

    不存在就当没配过；坏文件警告后按未配处理，**不覆盖**。
    返回原始键值而不是 :class:`~gametrans.credentials.Credentials`：后者会把缺的键
    补成空串，"没设过"与"设成了空"就分不出来了 —— 而分层解析正好靠这个区别。
    """
    return Credentials.load_values(global_credentials_path())


def save_global_credentials(fields: dict[str, Any] | Credentials) -> Path:
    """写全局密钥 —— 与项目那份同一套规矩：0600、只回显尾 4 位。

    给 dict 时**只写传进来的那些键**（与配置层同一条纪律）：把缺的键补成空串会
    让文件里出现一堆 `"base_url": ""`，用户读起来以为"这里被设成空了"。
    给 :class:`~gametrans.credentials.Credentials` 时才整份覆盖（调用方明确要这个语义）。
    """
    from gametrans.credentials import CREDENTIAL_FIELDS, normalize_base_url

    path = global_credentials_path()
    if isinstance(fields, Credentials):
        payload: dict[str, Any] = dict(fields.storage_dict())
    else:
        payload = {}
        for key, value in fields.items():
            if key not in CREDENTIAL_FIELDS:
                raise ConfigError(
                    f"未知的凭证字段：{key!r}",
                    hint=f"可写：{', '.join(CREDENTIAL_FIELDS)}。",
                )
            if value is None:
                continue
            text = str(value)
            payload[key] = normalize_base_url(text) if key == "base_url" else text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    from gametrans.credentials import _restrict

    _restrict(path)
    return path


def clear_global_credential_field(key: str) -> Path:
    """从全局密钥里**删掉**某一项。

    与"设成空串"不是一回事：删掉才回落下一层（项目、或环境变量、或没有）；
    空串是"我就要空"。四层配置里这是唯一的撤销语义。
    """
    if key not in CREDENTIAL_FIELDS:
        raise ConfigError(
            f"未知的凭证字段：{key!r}", hint=f"可写：{', '.join(CREDENTIAL_FIELDS)}。"
        )
    path = global_credentials_path()
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}  # 坏文件不在这里修，交给读取路径去报警告
    payload.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    from gametrans.credentials import _restrict

    _restrict(path)
    return path


def clear_global_config_field(key: str) -> Path:
    """从全局配置里**删掉**某一项（回落出厂默认），而不是设成默认值。"""
    if key not in GLOBAL_FIELDS:
        raise ConfigError(
            f"这个字段不在全局配置里：{key!r}", hint=f"可以放：{', '.join(GLOBAL_FIELDS)}。"
        )
    payload, _warnings = load_global_config()
    payload.pop(key, None)
    return save_global_config(payload)
