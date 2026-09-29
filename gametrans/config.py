"""项目配置 —— 就是工作区里那个 ``project.json``。

三条原则：

* **文件即配置**：用户和 agent 都能直接改，改完立刻生效。
* **损坏不致命**：读不懂的字段回退默认值并留下警告，不因为一行写错就打不开项目。
* **字段白名单**：写错字段名直接报错，避免"配了但没生效"这种最难查的问题。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gametrans.errors import ConfigError

VALID_MODES = ("auto", "serial", "parallel")

CONFIG_FILE = "project.json"

#: 字段白名单 + 期望类型。写错名字或类型会得到明确反馈，而不是静默忽略。
#:
#: ``base_url`` / ``model`` 刻意**不在**这里：它们跟着密钥一起走凭证文件
#: （:mod:`gametrans.credentials`）。同一份值存两处，就会出现"面板从这儿回显、
#: 写入却落到那儿"，看上去像被自动清掉了。
SCHEMA: dict[str, type] = {
    "engine": str,
    "target_language": str,
    "source_language": str,
    "provider": str,
    "concurrency": int,
    "batch_size": int,
    #: 一个单元一次最多问几条槽位（0 = 不切）。它是"这个模型一次装得下多少"的事实，
    #: 换了模型要重新量，所以可持久化：不必每次命令行都重复声明。
    "unit_budget": int,
    #: 一个单元分几轮问完（0 = 一次发完；>0 = 每轮最多这么多条）。
    #: 同样是"这个模型一次回得完多少"的事实，可持久化。
    "round_lines": int,
    #: 小批量并行的批量：把本来一层一层串行的计划按不超过这么多**单元**攒成一批
    #: （0 = 不攒，严格按分层走）。它是"这个模型 + 这个交付节奏"的事实，可持久化。
    "batch_units": int,
    #: 翻译单元的中间一级：按引擎结构的哪一层把单位分组（"" = 不分组）。
    #: 它是"规则"而不是"这次调用的条件"——换了就该一直用，所以可持久化。
    "group_by": str,
    "mode": str,
    "use_glossary": bool,
    "use_worldbook": bool,
    "use_style": bool,
    "use_knowledge": bool,
    "use_memory": bool,
    "retry_on_violation": int,
    "max_attempts": int,
    "require_structure": bool,
    "custom_instructions": str,
    #: 生效的请求模板名 + 用户自己改/存的模板。两者都由 :mod:`gametrans.prompts` 解释。
    "prompt_template": str,
    "prompt_templates": dict,
    #: 透传给 provider 的**请求参数覆写**（例如 ``{"max_tokens": 384000}``）。
    #: 见 :data:`REQUEST_OVERRIDE_RESERVED`：有主的键不许在这里改。
    "request_overrides": dict,
}

#: 请求体里**已经有主**的键：覆写它们会让"谁说了算"变得查不出来。
#:
#: * ``model`` 归凭证层（``credentials.json`` / ``GAMETRANS_MODEL``）——
#:   两处都能定模型名，就会出现"面板回显 A、实际用 B"；
#: * ``messages`` 归翻译层的装配（提示词是它的产出，不是配置项）；
#: * ``stream`` 归协议（本项目按非流式解析响应）。
REQUEST_OVERRIDE_RESERVED: tuple[str, ...] = ("model", "messages", "stream")

#: 环境变量里的覆写（JSON 对象）。与 ``GAMETRANS_MODEL`` 同一套优先级：环境变量优先。
REQUEST_OVERRIDES_ENV = "GAMETRANS_REQUEST_JSON"

#: **只属于这个项目**、绝不许从全局层来的字段。
#:
#: 前两个是 R30 的教训：目标语言与源语言是**每个游戏不一样**的东西 —— Ren'Py 那个靶
#: 的语言入口代码是 ``chinese`` 而不是 ``zh_CN``，RPGM 那边它同时是 ``$dataSystem.locale``
#: 与产物里译文表的文件名。继承错一个，整个交付就废（译文进得去、玩家点不到）。
#: 后三个是"这个引擎 / 这个项目"的事：引擎由探测决定，引擎选项（例如 Ren'Py 的
#: ``sdk_path``）放到全局会跨引擎串味，结构闸门该不该开是项目级风险决定。
PROJECT_ONLY_FIELDS: tuple[str, ...] = (
    "engine",
    "target_language",
    "source_language",
    "require_structure",
    "engine_options",
)

#: **可以放进全局默认**的字段 —— "我这个用户、这台机器"的习惯，与具体哪个游戏无关。
GLOBAL_FIELDS: tuple[str, ...] = (
    "provider",
    "concurrency",
    "batch_size",
    "unit_budget",
    "round_lines",
    "batch_units",
    "group_by",
    "mode",
    "use_glossary",
    "use_worldbook",
    "use_style",
    "use_knowledge",
    "use_memory",
    "retry_on_violation",
    "max_attempts",
    "custom_instructions",
    "prompt_template",
    "prompt_templates",
    "request_overrides",
)

#: 某一项的生效值是从哪一层来的。四层之后没有它就没法回答"我改了怎么没生效"。
SourceLayer = str  # "project" | "global" | "default"


@dataclass
class ProjectConfig:
    """一个 gametrans 项目的全部可调项。"""

    #: 留空表示"由探测决定"。内核不知道任何具体引擎的名字 ——
    #: 具体引擎名只能从 ``EngineRegistry`` 里探测出来。
    engine: str = ""
    target_language: str = "zh_CN"
    source_language: str = "auto"
    provider: str = "mock"
    concurrency: int = 4
    #: 一次调用最多装几条（**单次调用封顶**，不是"批大小"）：段能装下就整段一次，
    #: 装不下才按它拆。来历见 `layers/translate.py::TranslateOptions.batch_size`。
    batch_size: int = 50
    #: 一个单元一次最多问几条槽位（0 = 不切分，一个单元一次请求）。
    #: 上限是**时间**不是长度：真靶实测（GLM-5.3-Flash）≈7.4 秒/条，服务端在 300 秒
    #: 附近回 HTTP 400，于是 90 条的单元怎么重试都拿不到；切到 25 条以内才稳。
    unit_budget: int = 0
    #: 一个单元**分几轮**问完（0 = 一次发完；>0 = 每轮最多这么多条）。
    #: 上限这一次不是时间而是**输出**：真靶实测（该工程 act25 / deepseek-flash，2026-09-27）
    #: 1,972 条一次发完，输出预算给到 64K 仍被截断（`finish_reason=length`）→ 整批报废；
    #: 切成 14 轮（每轮 150 条）全部回来，墙钟只多 10%、输入 91% 命中缓存。
    #: 没给这个数时，"撞输出上限"也会自动转多轮（每轮 150 条）并把该轮对半再切。
    round_lines: int = 0
    #: 小批量并行的批量（0 = 不攒批，严格按计划的分层走）。
    #: 攒批放弃的是"批内后面的单元看得见前面刚定下的叫法"，代价由跑完之后的
    #: 合并术语 / 冲突裁决 / 统一替换补回来。
    batch_units: int = 0
    #: 翻译单元的中间一级：按引擎结构的哪一层分组。
    #: ``"auto"`` = 用适配层申报的首级（Ren'Py `label` / RPGM `map`），适配层一级都没申报
    #: 时退回不分组；``""`` = 明确不分组（逐条基线）；其它值 = 指定层级名（不认识就报错）。
    group_by: str = "auto"
    mode: str = "auto"
    use_glossary: bool = True
    use_worldbook: bool = True
    #: 风格指南是一等资源，因此和术语一样有开关
    use_style: bool = True
    #: 已确认的长期知识（知识库里的事实）是否注入
    use_knowledge: bool = True
    #: 复用翻译记忆（精确命中零成本复用；模糊命中只作参考）
    use_memory: bool = True
    #: 结构校验没过时重试几次（0 = 不重试）
    retry_on_violation: int = 1
    #: 同一条任务最多问几次模型（重试次数再多也封顶在它）
    max_attempts: int = 3
    #: 结构校验开关；关掉只剩输出形状检查
    require_structure: bool = True
    custom_instructions: str = ""
    #: 现在生效的**请求模板名**（见 :mod:`gametrans.prompts`：出厂有 ``full`` / ``compact``，
    #: 用户可另存自己的）。这里只存名字，内容在下一项 —— 提示词进请求体，属于
    #: "我这个用户、这台机器"的习惯，所以放全局层也合理。
    #: 字面量与 :data:`gametrans.prompts.DEFAULT_TEMPLATE_NAME` 由测试钉住不许漂。
    prompt_template: str = "compact"
    #: 用户改过或另存的模板：``名字 → 模板``，同名覆盖出厂预设。
    prompt_templates: dict[str, Any] = field(default_factory=dict)
    #: 透传给模型接入点的**请求参数覆写**。装的是"这个接入点/这个模型"的事：
    #: 例如推理型模型要给 ``max_tokens`` 一个足够大的值，否则推理会把默认输出预算
    #: 吃光、正式答复一个字都出不来（真靶实测见 ``tests/test_request_overrides.py``）。
    #: 内核不解释这些键，原样交给 provider；有主的键见 :data:`REQUEST_OVERRIDE_RESERVED`。
    request_overrides: dict[str, Any] = field(default_factory=dict)
    #: 透传给引擎支持包的私有配置，内核不解释
    engine_options: dict[str, Any] = field(default_factory=dict)

    # ---- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> tuple["ProjectConfig", list[str]]:
        config = cls()
        warnings: list[str] = []
        for key, value in (data or {}).items():
            if key == "engine_options":
                if isinstance(value, dict):
                    config.engine_options = dict(value)
                else:
                    warnings.append("project.json 的 engine_options 不是对象，已回退为空")
                continue
            expected = SCHEMA.get(key)
            if expected is None:
                warnings.append(f"project.json 里有未知字段 {key!r}，已忽略")
                continue
            if expected is int and isinstance(value, bool):
                warnings.append(f"project.json 的 {key} 期望整数，实际是布尔值，已回退默认值")
                continue
            if not isinstance(value, expected):
                warnings.append(
                    f"project.json 的 {key} 期望 {expected.__name__}，"
                    f"实际是 {type(value).__name__}，已回退默认值"
                )
                continue
            setattr(config, key, value)

        if config.mode not in VALID_MODES:
            warnings.append(
                f"project.json 的 mode={config.mode!r} 不是可用值"
                f"（{', '.join(VALID_MODES)}），已回退为 auto"
            )
            config.mode = "auto"
        if config.batch_size <= 0:
            fallback = cls().batch_size
            warnings.append(
                f"project.json 的 batch_size 必须为正整数，已回退为 {fallback}"
            )
            config.batch_size = fallback
        if config.concurrency <= 0:
            warnings.append("project.json 的 concurrency 必须为正整数，已回退为 4")
            config.concurrency = 4
        if config.unit_budget < 0:
            warnings.append("project.json 的 unit_budget 不能为负，已回退为 0（不切分）")
            config.unit_budget = 0
        if config.round_lines < 0:
            warnings.append("project.json 的 round_lines 不能为负，已回退为 0（一次发完）")
            config.round_lines = 0
        if config.batch_units < 0:
            warnings.append("project.json 的 batch_units 不能为负，已回退为 0（不攒批）")
            config.batch_units = 0
        return config, warnings

    # ---- 磁盘 ---------------------------------------------------------------

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> tuple["ProjectConfig", list[str]]:
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = getattr(exc, "msg", str(exc))
            return cls(), [f"{path.name} 读不出来（{detail}），已回退到默认配置"]
        if not isinstance(payload, dict):
            return cls(), [f"{path.name} 顶层不是对象，已回退到默认配置"]
        return cls.from_dict(payload)

    # ---- 变更 ---------------------------------------------------------------

    def merged(self, **overrides: Any) -> "ProjectConfig":
        config, warnings = ProjectConfig.from_dict({**self.to_dict(), **overrides})
        if warnings:
            raise ConfigError("；".join(warnings))
        # from_dict 只处理了已知字段，engine_options 需要单独合并
        extra = overrides.get("engine_options")
        if isinstance(extra, dict):
            config.engine_options = {**self.engine_options, **extra}
        return config

    def validate_field(self, key: str) -> type:
        expected = SCHEMA.get(key)
        if expected is None and key != "engine_options":
            raise ConfigError(
                f"未知的配置字段：{key!r}",
                hint=f"可用字段：{', '.join(sorted([*SCHEMA, 'engine_options']))}",
            )
        return expected or dict


def coerce_value(key: str, raw: Any) -> Any:
    """把外面来的值归一成这个字段该有的类型。

    CLI 的命令行、面板的表单都会给字符串（``"false"``、``"6"``），JSON 调用给的又是
    原生类型 —— 两条路走**同一份**归一化，免得出现"命令行能设、面板设不了"这种分叉。
    """
    expected = SCHEMA.get(key)
    if expected is None:
        raise ConfigError(
            f"未知的配置字段：{key!r}",
            hint=f"可用字段：{', '.join(sorted(SCHEMA))}",
        )
    if expected is bool:
        if isinstance(raw, bool):
            return raw
        lowered = str(raw).strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
        raise ConfigError(f"配置 {key} 需要布尔值，收到 {raw!r}", hint="用 true / false。")
    if expected is int:
        if isinstance(raw, bool):
            raise ConfigError(f"配置 {key} 需要整数，收到 {raw!r}")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ConfigError(
                f"配置 {key} 需要整数，收到 {raw!r}",
                hint=f"例如 `gametrans config set {key} 4`。",
            ) from None
    if expected is dict:
        # 命令行与面板表单给的是字符串，所以这一项按 **JSON 对象** 收：
        # `gametrans config set request_overrides '{"max_tokens": 384000}'`。
        if isinstance(raw, dict):
            return dict(raw)
        text = str(raw).strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"配置 {key} 需要一个 JSON 对象，解析失败：{exc.msg}",
                hint=f'例如 `gametrans config set {key} \'{{"max_tokens": 384000}}\'`。',
            ) from None
        if not isinstance(parsed, dict):
            raise ConfigError(
                f"配置 {key} 需要一个 JSON 对象，收到 {type(parsed).__name__}",
                hint='写成 {"max_tokens": 384000} 这样的对象。',
            )
        return parsed
    if isinstance(raw, (dict, list)):
        raise ConfigError(f"配置 {key} 需要文本，收到 {raw!r}")
    return str(raw)


# --------------------------------------------------------------------------- #
# 分层解析：环境变量 > 项目 > 全局默认 > 出厂默认
# --------------------------------------------------------------------------- #


def resolve_config(
    project_values: dict[str, Any] | None = None,
    global_values: dict[str, Any] | None = None,
    *,
    warnings: list[str] | None = None,
) -> ProjectConfig:
    """按层合并成一份**生效配置**。

    **在 dict 这一层合并**，而不是先把各层灌进 dataclass 再取非默认值 ——
    后者分不清"用户显式设成了默认值"与"用户没设过"，而四层配置里这正是
    唯一的撤销语义（删掉项目里那一项才回落全局）。

    **读的时候宽容**：某一层里类型不对的值回退默认值并记一条警告（"损坏不致命"，
    见 :mod:`gametrans.config` 开头那三条原则）。严格只发生在**写**的时候
    （:func:`coerce_value`），这样人手写坏一个字段不会让项目直接打不开。

    配置字段没有环境变量层（只有凭证有，见 :mod:`gametrans.credentials`）——
    这里如实不假装有。
    """
    project = project_values or {}
    globals_ = global_values or {}
    merged: dict[str, Any] = {}
    # 项目专属字段绝不采信全局层来的值（加载全局层时已经报过错，这里是第二道）
    for key, value in globals_.items():
        if key in PROJECT_ONLY_FIELDS:
            continue
        merged[key] = value
    merged.update(project)

    config, found = ProjectConfig.from_dict(merged)
    if warnings is not None:
        warnings.extend(found)
    return config


def _sources(
    project_values: dict[str, Any],
    global_values: dict[str, Any],
    *,
    fallback: str,
) -> dict[str, str]:
    sources: dict[str, str] = {}
    for key in [*SCHEMA, "engine_options"]:
        if key in project_values:
            sources[key] = "project"
        elif key in global_values and key in GLOBAL_FIELDS:
            sources[key] = "global"
        else:
            sources[key] = fallback
    return sources


def resolve_config_sources(
    project_values: dict[str, Any] | None = None,
    global_values: dict[str, Any] | None = None,
) -> dict[str, str]:
    """每一项的生效值来自哪一层。没有它，"我改了怎么没生效"没法查。"""
    return _sources(project_values or {}, global_values or {}, fallback="default")


resolve_config.sources = resolve_config_sources  # type: ignore[attr-defined]


def save_project_values(path: Path, values: dict[str, Any]) -> None:
    """把**显式设过的那些键**写进项目文件 —— 不是把生效配置整份倒出去。

    这条是防"烘进来"的关键：生效配置里有全局层的值，整份写回就等于把全局值
    烤进项目文件，从此那一项再也跟不上全局层的变化，用户也分不清哪个值是真的。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(values), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_project_values(path: Path) -> tuple[dict[str, Any], list[str]]:
    """读项目文件里的**原始键值**（不补默认值），并给出可读性警告。"""
    path = Path(path)
    if not path.exists():
        return {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "msg", str(exc))
        return {}, [f"{path.name} 读不出来（{detail}），已回退到默认配置"]
    if not isinstance(payload, dict):
        return {}, [f"{path.name} 顶层不是对象，已回退到默认配置"]
    return dict(payload), []
