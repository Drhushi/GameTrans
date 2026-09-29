"""操作注册表 —— CLI 与 MCP server 的**唯一真相来源**。

每个操作只声明一次：名字、说明、参数、处理函数。CLI 用它生成 argparse 子命令，
MCP server 用它生成 ``tools/list`` 的 JSON Schema。于是"每个 CLI 命令都有对应的
MCP tool"是构造出来的性质，不需要靠人肉对表去维持 —— 加一个操作，两个入口同时
就有了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from gametrans.config import SCHEMA as CONFIG_SCHEMA
from gametrans.config import GLOBAL_FIELDS, coerce_value
from gametrans.core.models import DIRECTIONS, GraphEdge, TranslationStatus
from gametrans.core.constraints import term_tags_in
from gametrans.core import schedule
from gametrans.core.agentqueue import AgentQueue, QUEUE_DIRNAME
from gametrans.core.session import ProjectSession
from gametrans.layers import entityflow
from gametrans.layers.deviations import Deviation
from gametrans.layers.entities import collect as collect_term_candidates
from gametrans.layers.knowledge import KnowledgeError
from gametrans.layers.translate import DEFAULT_ROUND_LINES
from gametrans.layers.staleness import find_stale
from gametrans.layers.tags import paused_regions
from gametrans.core.summaries import load_summaries
from gametrans.layers.supplements import Supplement
from gametrans.errors import ConfigError, GameTransError, ProjectError

# --------------------------------------------------------------------------- #
# 参数与操作
# --------------------------------------------------------------------------- #

PARAM_TYPES = ("str", "int", "bool", "path", "csv", "json")

_JSON_TYPES = {
    "str": "string",
    "int": "integer",
    "bool": "boolean",
    "path": "string",
    "csv": "array",
    "json": "object",
}


@dataclass
class Param:
    name: str
    type: str = "str"
    help: str = ""
    required: bool = False
    default: Any = None
    choices: list[str] | None = None
    positional: bool = False
    #: 命令行 flag 的覆盖写法（默认由 name 推成 ``--some-name``）。
    #: 只在"按名字推出来的不好念"时用，例如 ``global_scope`` 想显示成 ``--global``。
    flag: str = ""
    #: 这个参数是**一段话**而不是一个词/一行（面板据此渲染成多行输入框）。
    multiline: bool = False

    def __post_init__(self) -> None:
        if self.type not in PARAM_TYPES:
            raise ValueError(f"未知参数类型：{self.type!r}（可用：{', '.join(PARAM_TYPES)}）")

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": _JSON_TYPES[self.type],
            "description": self.help or self.name,
        }
        if self.choices:
            schema["enum"] = list(self.choices)
        if self.type == "csv":
            schema["items"] = {"type": "string"}
        if self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass
class Context:
    """一次操作调用的上下文。CLI 与 MCP 都构造它。"""

    project_root: Path
    workdir: Path | None = None
    json_mode: bool = False
    quiet: bool = False
    view_watermark: int = 0
    _session: ProjectSession | None = None

    def session(self) -> ProjectSession:
        """打开（或复用）项目会话。"""
        if self._session is None:
            if not self.project_root.is_dir():
                raise ProjectError(
                    f"项目路径不存在或不是目录：{self.project_root}",
                    hint="用 --project 指定游戏根目录（包含游戏脚本的那个目录）。",
                )
            self.attach(ProjectSession.open(self.project_root, workdir=self.workdir))
        return self._session

    def attach(self, session: ProjectSession) -> ProjectSession:
        """接上一个会话，并记下"此刻已有的视图"作为水位线。"""
        self._session = session
        self.view_watermark = max(
            (v.seq for v in session.interaction.all_views()), default=0
        )
        return session

    def new_user_views(self) -> list[Any]:
        """本次调用新产生、且用户可见的视图。

        交互层的视图是持久化的"项目工作信息"，但一次命令的人类输出只应该讲
        这次发生了什么 —— 否则每跑一条命令都会把历史信息重播一遍。
        """
        session = self._session
        if session is None:
            return []
        return [v for v in session.interaction.visible_views() if v.seq > self.view_watermark]

    def peek(self) -> ProjectSession | None:
        return self._session


@dataclass
class Operation:
    name: str
    summary: str
    handler: Callable[[Context, dict[str, Any]], dict[str, Any]]
    params: list[Param] = field(default_factory=list)
    group: str = "general"
    #: 自定义的人类可读渲染。为 None 时由 CLI 走通用渲染。
    render: Callable[[dict[str, Any]], str] | None = None
    #: 是否会改动项目/工作区状态。默认按"会改"处理 —— 只读面板据此决定放不放行。
    mutates: bool = True

    @property
    def tool_name(self) -> str:
        return tool_name_for(self.name)


def tool_name_for(operation_name: str) -> str:
    """``resource.glossary.add`` → ``resource_glossary_add``。"""
    return operation_name.replace(".", "_")


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _summary_for(node: Any, index: dict[str, dict]) -> dict | None:
    """这条文本的摘要：先按 ``unit_id`` 命中，再按**场名**（label / 地图）命中。

    两把键都要认：生成摘要时手里是场名（label），而面板拿的是 unit_id —— 只认一把，
    另一边就永远命中不了，而图上看起来只是"没标题"，看不出是键没对上。
    """
    if not index:
        return None
    unit = node.unit
    if unit is None:
        return None
    hit = index.get(unit.id)
    if hit is not None:
        return hit
    scene = str(unit.context.scene or "") if unit.context else ""
    return index.get(scene)


def graph_node_payload(
    node, region: str | None = None, summary: dict | None = None, *, full: bool = True,
    pending_tags: bool = False,
) -> dict[str, Any]:
    """一个节点给面板 / agent 看的载荷。

    ``region`` 是它所属的**阅读区域**（label、文件那种"一块"）。面板画依赖图时
    必须知道它：后端的控制流与依赖边连的是区域 id，而画布画的是单元 ——
    没有这层对应关系，绝大多数边会因为"端点在画布上找不到"被静默丢掉。

    ``full=False`` 去掉 ``locator`` 与 ``segments``：那是**整段原文**（一个单元动辄
    300+ 句、几十 KB），列给面板看要几十份就是十几 MB —— 浏览器拉不动，页面永远
    停在"加载中"。要看某一条的全部细节走 ``/api/units/<id>`` 那一个端点。
    """
    unit = node.unit
    payload = {
        "node_id": node.node_id,
        "path": node.path,
        "kind": node.kind.value,
        "region": region,
        "weight": node.weight.to_dict(),
        "parent": node.parent,
        "children": list(node.children),
        "source": unit.source if unit else None,
        "speaker": unit.context.speaker if unit else None,
        "context": unit.context.render() if unit else None,
        # 章 / 场景再给一份**结构化的**：`context` 是给人读的整句，
        # 画布要靠章分组画带子，不能去解析那句话。
        "chapter": unit.context.chapter if unit else None,
        "scene": unit.context.scene if unit else None,
        # 模型生成的**场摘要**（声明式产物，住在工作区的 summaries.json）：
        # `title` 是图上直接显示的那一行，`summary` 给抽屉/tooltip 看。没有就都是 null，
        # 画布回落到原文首句 —— 摘要是额外一层，不是图能否成立的前提。
        "title": (summary or {}).get("title") or None,
        "summary": (summary or {}).get("summary") or None,
        "unit_id": unit.id if unit else None,
        "type": unit.type if unit else None,
        # 这一条里还有没定译的名字（原文里是 `⟦写法⟧`）：面板据此标出来，
        # **不影响"可用"** —— 见 :func:`term_tag_index`。
        "term_tags_pending": bool(pending_tags),
        "resource_refs": list(unit.resource_refs) if unit else None,
    }
    if full:
        payload["locator"] = unit.locator.to_dict() if unit and unit.locator else None
        payload["segments"] = [s.to_dict() for s in unit.segments] if unit else None
    return payload


#: 节点翻译状态的取值。**刻意不合并成一个百分比**：三种缺口该被区别对待，
#: 一句"覆盖率 92%"会把"没翻"和"翻了但没过闸门"混成同一个数字 —— 那正是
#: 登记项 R19 之前踩过的坑（真缺口被噪音淹掉）。
NODE_STATUSES: tuple[str, ...] = (
    "usable",
    "needs_review",
    "untranslated",
    "not_translatable",
)


def translation_status_index(records: Iterable[Any]) -> dict[str, str]:
    """``键 → 状态``：**槽位键**与**单元键**都进索引。

    记录是按槽位记账的（一个单元含多句时逐句一条），而画布是按**结点（单元）**着色的：
    所以这里把"槽位键"也收进来，节点查自己的 id、也查自己那几条槽位的键。
    一次遍历建索引：画布一次要算几十上百个节点，逐节点回头线性找记录是 O(n·m)。
    """
    index: dict[str, str] = {}
    for record in records:
        state = "usable" if record.is_usable else "needs_review"
        key = str(record.unit_id)
        # 同一条单元有多句时取"最差的那一档"：只要有一句不能用，这个结点就不算全好
        previous = index.get(key)
        if previous != "needs_review":
            index[key] = state
    return index


def node_translation_status(node, index: dict[str, str]) -> str:
    """一个节点当前处于哪一档；``not_translatable`` 是"根本没有可翻的内容"。

    节点是**单元**，记录是**槽位**：所以先看单元自己的键，再看它每条槽位的键，
    全部可用才算 ``usable``（有一句不能用就是 ``needs_review``）。
    """
    if node.unit is None:
        return "not_translatable"
    unit = node.unit
    direct = index.get(unit.id)
    if direct:
        return direct
    keys = [str(key) for key in (unit.metadata.get("slot_keys") or [])]
    states = [index[key] for key in keys if key in index]
    if not states:
        return "untranslated"
    if len(states) < len(keys):
        # 有槽位没记录：这个结点还没全落
        return "untranslated"
    return "needs_review" if "needs_review" in states else "usable"


def term_tag_index(records: Iterable[Any]) -> set[str]:
    """哪些键的译文里还带着 ``⟦写法⟧`` —— 名字还没定译（见 :mod:`gametrans.layers.tags`）。

    口径与 :func:`translation_status_index` 一致：槽位键与单元键都收，画布按结点查一次
    就能标出"这一场里有名字还没定"。**它不影响"可用"**：文本已经翻好了，缺的只是名字，
    写回那一刻按术语书渲染。
    """
    return {
        str(record.unit_id)
        for record in records
        if term_tags_in(getattr(record, "target", "") or "")
    }


def node_has_pending_tags(node, index: set[str]) -> bool:
    """这个结点里有没有还没定译的名字（它自己的键或它任一槽位的键命中就算）。"""
    if node.unit is None or not index:
        return False
    if node.unit.id in index:
        return True
    return any(
        str(key) in index for key in (node.unit.metadata.get("slot_keys") or [])
    )


def graph_edge_payload(edge) -> dict[str, Any]:
    """边上画布要的全部字段。

    ``orders``（这条边能不能拿去决定先后）不在 ``GraphEdge.to_dict()`` 里 —— 那是
    落盘格式，动它等于改契约；但它正是画布分层要的判据，所以在载荷里补出来。
    """
    payload = edge.to_dict()
    payload["orders"] = edge.orders
    return payload


def _find_node(graph, ref: str):
    if ref in graph.nodes:
        return graph.nodes[ref]
    for node in graph.nodes.values():
        if node.path == ref:
            return node
    for node in graph.nodes.values():
        if node.unit is not None and node.unit.id == ref:
            return node
    raise GameTransError(
        f"路径图里找不到：{ref!r}",
        hint="用 `gametrans graph show` 看看有哪些逻辑路径。",
    )


def _coerce_config_value(key: str, raw: str) -> Any:
    """CLI 侧的值归一 —— 与面板共用 ``config.coerce_value``（一份实现，两个入口）。"""
    return coerce_value(key, raw)


def missing_arguments(operation: Operation, args: dict[str, Any]) -> list[str]:
    """还缺哪些必需参数。

    CLI 由 argparse 挡住缺参，MCP 由 JSON Schema 的 ``required`` 挡住，
    面板得自己挡 —— 三条路都对着注册表里同一个 ``required`` 声明。
    """
    missing: list[str] = []
    for param in operation.params:
        if not param.required:
            continue
        value = args.get(param.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(param.name)
    return missing


# --------------------------------------------------------------------------- #
# 处理函数
# --------------------------------------------------------------------------- #


def _project_init(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ProjectSession.init(
        ctx.project_root,
        workdir=ctx.workdir,
        engine=args.get("engine"),
        target_language=args.get("target_language") or "zh_CN",
        # **不给默认值**：没显式指定就不写进项目文件，让它跟全局层/出厂默认走。
        # 给个 "mock" 当默认写进去，等于每个新项目都永久压住全局配好的 provider ——
        # "全局配一次、处处通用"当场失效，而且看不出来（项目文件里明明写着 mock）。
        provider=args.get("provider"),
    )
    ctx.attach(session)
    session.interaction.emit(
        "project.status",
        "项目已初始化",
        lines=[
            f"引擎：{session.config.engine}",
            f"目标语言：{session.config.target_language}",
            f"工作区：{session.workdir}",
        ],
        severity="success",
    )
    return {
        "engine": session.config.engine,
        "target_language": session.config.target_language,
        "provider": session.config.provider,
        "workspace": str(session.workdir),
    }


def _project_status(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    status = session.status()
    for warning in status["load_warnings"]:
        session.interaction.emit("project.warning", "配置需要留意", lines=[warning], severity="warning")
    session.interaction.emit(
        "project.status",
        "项目状态",
        lines=[
            f"引擎：{status['engine']}",
            f"目标语言：{status['target_language']}",
            "路径图："
            + (f"{status['graph']['translatable']} 条可译" if status["graph"]["present"] else "尚未提取"),
            "译文："
            + (f"{status['translations']['ok']} 条可用" if status["translations"]["present"] else "尚未翻译"),
            f"补丁：{len(status['patches'])} 个",
        ],
    )
    return status


def _engine_list(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from gametrans.bootstrap import build_registry

    registry = build_registry()
    return {
        "packs": registry.describe_all(),
        # 装不上的包也要列出来 —— 静默跳过等于"装了但没生效"，最难查的一类。
        "rejected": [item.to_dict() for item in registry.rejected()],
        "problems": registry.problems(),
    }


def _engine_info(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from gametrans.bootstrap import build_registry

    registry = build_registry()
    pack = registry.get(args["name"])
    info = pack.describe()
    # 来自哪、盖掉了谁：用户魔改过适配包时，这两样是唯一能说清现状的东西。
    info.update(registry.origin_of(pack.name))
    return {"pack": info}


def _engine_detect(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from gametrans.bootstrap import build_registry

    target = Path(args.get("path") or ctx.project_root).expanduser()
    return {"detections": [d.to_dict() for d in build_registry().detect(target)]}


def _engine_options(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """当前引擎的私有选项与外部工具链状态。"""
    session = ctx.session()
    return {
        "engine": session.engine,
        "options": dict(session.config.engine_options),
        "toolchain": session.toolchain_status(),
    }


def _engine_option_set(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    options = session.update_engine_option(args["key"], args["value"])
    status = session.toolchain_status()
    session.interaction.emit(
        "engine.options",
        "引擎设置已更新",
        lines=[f"{args['key']} = {args['value']}", str(status.get("detail", ""))],
        severity="success" if status.get("usable") else "warning",
    )
    return {"options": options, "toolchain": status}


def _engine_option_clear(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    return {"options": session.clear_engine_option(args["key"])}


def _scan(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.session().scan().to_dict()


def _graph_show(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    graph = session.graph
    if graph is None:
        raise GameTransError("还没有路径图", hint="先运行 `gametrans scan`。")
    limit = int(args.get("limit") or 20)
    translatable = graph.translatable_nodes()[:limit]
    remaining = limit - len(translatable)
    containers = (
        [n for n in graph.iter_nodes() if n.is_container][:remaining] if remaining > 0 else []
    )
    summaries = load_summaries(session.workdir)
    tagged = term_tag_index(session.translations())
    return {
        "total": len(graph.nodes),
        "translatable": graph.stats()["translatable"],
        "nodes": [
            graph_node_payload(
                n, region=graph.region_of(n.node_id), summary=_summary_for(n, summaries),
                pending_tags=node_has_pending_tags(n, tagged),
            )
            for n in [*translatable, *containers]
        ],
    }


def _graph_node(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    graph = session.graph
    if graph is None:
        raise GameTransError("还没有路径图", hint="先运行 `gametrans scan`。")
    node = _find_node(graph, args["ref"])
    return {"node": graph_node_payload(node, region=graph.region_of(node.node_id))}


def _skeleton_status(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """报告引擎骨架里有什么。**纯只读**，不写盘、不改任何状态。

    刻意不要求先配好引擎：骨架目录在就有东西可报 —— 只读查询的门槛应当低。

    **引擎格式与目录约定由适配器回答**，内核只转达（核心不认识具体引擎）。

    逐文件拆分**总是列出**：它是这个入口的主要用途（看清哪个文件还没弄完）。
    不做成开关 —— CLI 的 bool 参数一律默认 False，做成开关反而要人多打一次。
    """
    return ctx.session().skeleton_status(
        language=str(args.get("language") or ""),
        include_files=True,
    )


def _language_facts(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """语言包事实：游戏认哪些语言、字体从哪来、语言目录里现有什么。

    **纯只读、纯事实**：只说"引擎里写着什么、文件在不在"，不替 agent 拿主意
    （用哪个字体、语言入口怎么接由 agent 决定）。引擎语法由适配器回答，内核只转达。
    """
    return ctx.session().language_facts(language=str(args.get("language") or ""))


def _ir(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """导出 Localization IR 并跑一遍提取层一致性校验。"""
    session = ctx.session()
    project = session.project_ir()
    if project is None:
        raise GameTransError(
            "还没有提取结果，导出不了 IR",
            hint="先运行 `gametrans scan`（或带 --scan 一起跑）。",
        )
    limit = int(args.get("limit") or 0)
    conformance = project.conformance()
    payload = project.to_dict(include_units=True)
    if limit > 0:
        payload["units"] = payload["units"][:limit]
    return {
        **payload,
        "conformance": conformance.to_dict(),
        "export_target": session.export_target().to_dict(),
    }


def _graph_dependencies(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """列出依赖边：候选、已确认、悬空分开报，agent 据此决定确认哪些方向。"""
    session = ctx.session()
    graph = session.graph
    if graph is None:
        raise GameTransError("还没有路径图", hint="先运行 `gametrans scan`。")
    edges = [{**edge.to_dict(), "orders": edge.orders} for edge in graph.dependencies]
    return {
        "dependencies": edges,
        "total": len(edges),
        "ordering": len(graph.ordering_dependencies()),
        "dangling": [e.to_dict() for e in graph.dangling_dependencies()],
        "regions": sorted(graph.region_ids()),
        # 边上的 `weight` 是载荷（传了几条知识）—— 派生的，所以连"按哪一版术语书算的"
        # 一起给出来；对不上就说明盘上这批数过期了。
        "payload": _payload_freshness(session, graph),
        "payload_total": sum(int(edge.weight) for edge in graph.dependencies),
    }


def _coerce_direction(raw: Any) -> str:
    direction = str(raw or "agent")
    if direction not in DIRECTIONS:
        raise ConfigError(
            f"未知的依赖方向：{direction!r}",
            hint=f"可用：{', '.join(DIRECTIONS)}。只有 control_flow / agent 会参与排序。",
        )
    return direction


def _graph_knowledge(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """把**知识边**写进图（或撤掉）—— 边由 :mod:`gametrans.layers.entityflow` 现算。

    为什么要有它：算出来的前驱集默认只活在 `plan` 的输出里（不落盘）。想让**图本身**
    就是新结构（面板画的就是它、`graph dependencies` 列的就是它），就得落进 ``graph.json``。

    三种处置，都**幂等**：

    * ``apply``（默认）：先撤掉上一轮写进去的知识边，再按当前术语书写一遍 ——
      控制边照旧留着（阅读顺序是引擎的事实）；
    * ``replace``：连**控制流排序边一起撤掉**，于是这张图上的先后只剩知识边。
      引擎边不会真丢 —— 下一次 `scan` 会由引擎重新给回来；
    * ``remove``：只撤知识边，图回到引擎边的样子。

    ``scan`` 从引擎重建整张图，但**保住 ``provenance=agent`` 的边**（见
    :meth:`ProjectSession.scan`），所以这里写下的东西不会被下一次重扫抹掉。

    它同时**重算载荷**（每条边的 ``weight`` = 当前术语书算出的"提供者首见 ∩ 消费者
    用到"条目数，见 :func:`gametrans.layers.entityflow.apply_payloads`）。所以这一个
    命令也是"把图上的派生量刷成当前值"的入口。
    """
    return _refresh_knowledge(ctx.session(), mode=str(args.get("mode") or "apply"))


def _refresh_knowledge(
    session: Any, *, mode: str = "apply", strict: bool = True
) -> dict[str, Any]:
    """按当前术语书把**知识边 + 载荷**重算一遍、落盘 —— 刷新图的唯一入口。

    ``strict=True``（`graph.knowledge` 命令）：你点名要写知识边，算不出来就报错。
    ``strict=False``（术语书写完顺手刷）：刷不动就**如实记一句**，不拦这一次写入 ——
    改术语本身不该因为"还没 scan / 摘要还没做"而失败。
    """
    graph = session.graph
    if graph is None:
        return {"skipped": "还没有路径图（先 `gametrans scan`）"}
    options = session.translate_options()
    flow = session.translate_layer.entity_flow(graph, session.resources, options)
    if flow is None or not getattr(flow, "entities", None):
        # `remove` 只撤边，不需要术语书；其余模式要写知识边，就必须有知识流。
        if mode != "remove" and strict:
            raise GameTransError(
                "术语书里没有「在剧情场原文里出现过」的写法，算不出知识边",
                hint="先做场摘要，再跑 `gametrans resource term candidates` 把写法登记进术语书。",
            )
        if mode != "remove":
            return {"skipped": "术语书里没有能算的知识流，图没动"}
        # 载荷照旧重算：没有知识流，每条边就是"一条都没传"（归零，而不是留着旧值）。
        flow = entityflow.KnowledgeFlow()

    def _drop_knowledge() -> int:
        before = len(graph.dependencies)
        graph.dependencies = [
            edge for edge in graph.dependencies if str(edge.type) != KNOWLEDGE_EDGE_TYPE
        ]
        return before - len(graph.dependencies)

    rounds_before = len(graph.region_layers())
    removed = _drop_knowledge()
    replaced = 0
    if mode == "replace":
        before = len(graph.dependencies)
        # 排序边 = 方向已确认的那些（`edge.orders`）—— 控制流的先后就靠它们
        graph.dependencies = [edge for edge in graph.dependencies if not edge.orders]
        replaced = before - len(graph.dependencies)
    written = 0
    if mode != "remove":
        seen: set[tuple[str, str]] = set()
        for region, providers in sorted(flow.predecessors.items()):
            # 一条边上挂**全部**"这个前驱交代、这一场用到"的实体（`topics`）——
            # 它同时是上下文注入的键：后面的场即使没字面提到，也能看到前情。
            reasons: dict[str, list[str]] = {}
            for provider, entity in flow.why(region):
                reasons.setdefault(provider, []).append(entity)
            for provider in sorted(providers):
                key = (provider, region)
                if key in seen:
                    continue
                seen.add(key)
                graph.add_dependency(
                    GraphEdge(
                        source=provider,
                        target=region,
                        type=KNOWLEDGE_EDGE_TYPE,
                        direction="agent",
                        topics=sorted(reasons.get(provider) or []),
                        note="术语书 + 原文命中算出的知识边（见 layers/entityflow.py）",
                        provenance="agent",
                    )
                )
                written += 1
    # 载荷：按当前术语书重写**每条边**的 weight。它描述的是"这条边传了几条知识"，
    # 与写不写知识边无关 —— 撤掉知识边之后，控制流边的载荷照样要在。
    payload_source = ""
    if session.resources is not None:
        payload_source = str(session.resources.versions().get("termbook") or "")
    payloads = entityflow.apply_payloads(graph, flow, source=payload_source)
    session.save_graph()
    return {
        "mode": mode,
        "knowledge_edges": written,
        "knowledge_edges_removed": removed,
        "ordering_edges_replaced": replaced,
        "total": len(graph.dependencies),
        "payloads": payloads,
        #: 这批载荷是按哪一版术语书算的（sha256 前 16 位）。术语书一动它就该变 ——
        #: 对不上就是"盘上的载荷过期了"，能查出来，不用猜。
        "payload_source": payload_source,
        "rounds_before": rounds_before,
        "rounds_after": len(graph.region_layers()),
        "entities": len(flow.entities),
        "note": (
            "知识边已写进 graph.json（provenance=agent），载荷（边 weight）也按当前术语书"
            "重算了一遍。`gametrans plan` 与面板读的都是这张图；重扫不会抹掉 agent 边。"
        ),
        "flow": flow.readings(),
    }


def _refresh_graph_after_terms(session: Any) -> dict[str, Any]:
    """术语书写完之后**顺手刷图** —— 尽力而为，刷不动也不拦这次写入。

    为什么要自动：知识边与载荷都是"术语书 × 原文命中"的派生物，书一动它们就过期。
    写术语书的入口有五六个（命令行三个、待审采用、摘要抽候选、跑批里的自动批准），
    靠每个入口自己记得刷新是不可靠的 —— 所以统一收口在这里，调用方只管写书。

    真漏了一处也不至于静默：图上的载荷带着 ``payload_source`` 戳（术语书版本），
    读取方（plan / 面板）会比出来"盘上这个数是按旧书算的"。
    """
    try:
        return _refresh_knowledge(session, strict=False)
    except Exception as exc:  # noqa: BLE001
        # 刷新失败不让"改术语"这件事失败，但**必须说出来** —— 静默才是危险的那个。
        return {"skipped": f"刷新图失败：{exc}"}


def _graph_depend(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """记下一条依赖边（默认就是"agent 拍板"的方向）。

    挑中一条启发式候选、把方向确认下来，这条边才会真正参与调度排序；不确认的话
    它只用来补上下文。
    """
    session = ctx.session()
    graph = session.graph
    if graph is None:
        raise GameTransError("还没有路径图", hint="先运行 `gametrans scan`。")
    source = str(args.get("source") or "")
    target = str(args.get("target") or "")
    known = graph.region_ids() | {
        node.unit.id for node in graph.nodes.values() if node.unit is not None
    }
    unknown = [ref for ref in (source, target) if ref and ref not in known]
    if not source or not target or unknown:
        raise GameTransError(
            f"依赖边的端点不认识：{'、'.join(unknown) or '（缺少端点）'}",
            hint="用 `gametrans graph dependencies` 看看有哪些区域 id。",
        )
    graph.remove_dependency(source, target)
    edge = graph.add_dependency(
        GraphEdge(
            source=source,
            target=target,
            type=str(args.get("type") or ""),
            direction=_coerce_direction(args.get("direction")),
            topics=[str(t) for t in (args.get("topics") or [])],
            note=str(args.get("note") or ""),
            provenance="agent",
        )
    )
    session.save_graph()
    return {
        "dependency": {**edge.to_dict(), "orders": edge.orders},
        "total": len(graph.dependencies),
    }


def _graph_undepend(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    graph = session.graph
    if graph is None:
        raise GameTransError("还没有路径图", hint="先运行 `gametrans scan`。")
    source = str(args.get("source") or "")
    target = str(args.get("target") or "")
    removed = graph.remove_dependency(source, target)
    if removed:
        session.save_graph()
    return {"removed": removed, "total": len(graph.dependencies)}


#: 图的两个时刻怎么比"结构变了没有"：按**边**（端点 + 类型）比，不看载荷。
#: 载荷（weight）是派生读数，书一动它就变；这里要盯的是**先后关系**本身。
def _edge_keys(graph: Any) -> set[tuple[str, str, str]]:
    if graph is None:
        return set()
    return {
        (str(edge.source), str(edge.target), str(edge.type))
        for edge in graph.dependencies
    }


def _closure_keys(graph: Any) -> dict[str, frozenset[str]]:
    """区域 → 它的**传递前驱集**。这才是"谁该等谁"的完整事实。

    只看边会误报：知识边有一道**化简**（"等晚的那个就已经把早的等掉了"），
    所以一条边挪个位置、传递闭包不变时，先后关系其实一个字都没变
    —— 真靶 2026-09-29 那次 +1/-1 就是这种（`act21` 从"直接等 act12"改成"等 act18"，
    而 act18 本身已经等到 act12）。
    """
    if graph is None:
        return {}
    return {
        region: frozenset(graph.predecessor_closure(region))
        for region in graph.region_ids()
    }


def _graph_change(
    before: set[tuple[str, str, str]], after: set[tuple[str, str, str]]
) -> dict[str, list[str]] | None:
    """两次之间的边差；没变就返回 ``None``（**只报事实，不判断该不该变**）。"""
    added = sorted(f"{src} → {dst}（{kind or 'control'}）" for src, dst, kind in after - before)
    removed = sorted(f"{src} → {dst}（{kind or 'control'}）" for src, dst, kind in before - after)
    if not added and not removed:
        return None
    return {"added": added, "removed": removed}


def _ordering_change(
    before: dict[str, frozenset[str]], after: dict[str, frozenset[str]]
) -> list[str]:
    """哪些区域的**传递前驱**真的变了（变了才叫"顺序变了"）。"""
    changed: list[str] = []
    for region in sorted(set(before) | set(after)):
        if before.get(region, frozenset()) != after.get(region, frozenset()):
            changed.append(region)
    return changed


def _translate(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    # 跑批会在收尾时按新的术语书**刷新图**（知识边 + 载荷）。刷完必须**出声**：
    # 图上的先后就是下一次翻译的顺序，设计要求"图一变就停下来让他看"
    # —— 而以前这一步是静默的（只在 --json 的 result["graph"] 里）。
    edges_before = _edge_keys(session.graph)
    closures_before = _closure_keys(session.graph)
    outcome = session.translate(
        provider=args.get("provider"),
        target_language=args.get("target_language"),
        batch_size=args.get("batch_size"),
        mode=args.get("mode"),
        concurrency=args.get("concurrency"),
        # 不给就按项目配置（默认 `auto` → 适配层申报的首级）；给 `none` 是"这一轮不分组"
        group_by=(args.get("group_by") or None),
        # `csv` 没给时给的是空列表 —— 空列表在这里的意思是"没声明范围"，归一成 None。
        # 只翻某一段（分块/续跑）用得上：之前只有库里有这个参数，命令行下不去。
        unit_scope=(tuple(args["unit_scope"]) if args.get("unit_scope") else None),
        # 第一轮 / 续跑：阶段是可以切成一段段跑的（停在哪、从哪接着跑都由调用方说了算）。
        # `scheduler` 这个参数名与 `plan` 命令保持一致，落到 TranslateOptions 叫
        # `plan_strategy`（那里管的是"用哪个策略算计划"）。
        plan_strategy=(args.get("scheduler") or ""),
        start_phase=args.get("start_phase"),
        stop_after_phase=args.get("stop_after_phase"),
        asset_gate=args.get("asset_gate"),
        # 命中的句子怎么处理：`keep` 只要新句子（省钱），`polish` 让模型为通顺提改动
        # （改动只进提案，见 `_collect_polish_proposals`）。没给就是 `None` ——
        # 空字符串会被 `translate_options` 当成"显式给了空档位"而报错。
        memory_reuse_mode=(args.get("memory_reuse_mode") or None),
        # 一个单元一次最多问几条槽位（0 = 不切）。见 TranslateOptions.unit_budget。
        unit_budget=args.get("unit_budget"),
        # 一个单元分几轮问完（0 = 一次发完；>0 = 每轮最多这么多条，同一会话续写）。
        # 见 TranslateOptions.round_lines；没给时"撞输出上限"仍会自动转多轮。
        round_lines=args.get("round_lines"),
        # 小批量并行：按不超过这么多单元攒一批（0 = 严格按计划的分层走）。
        batch_units=args.get("batch_units"),
        # "重复且一致就自动批准"的批数门槛（0 = 关，只落候选）。没给就用默认的 2。
        auto_approve_terms=args.get("auto_approve_terms"),
        # `--no-produce` 关掉"跑完把译文变成资产"（对照臂用）
        produce_candidates=not bool(args.get("no_produce")),
        reuse_imported=bool(args.get("reuse_imported")),
        # 参数名由注册表统一加 `--` 前缀生成（`context_layers` → `--context-layers`），
        # 所以这里必须按注册表里的名字取，不能望文生义写成 `context-layers`。
        # `csv` 类型在没给参数时给的是**空列表**、不是 None —— 空列表在这里的意思是
        # "没声明档位"（= 阶梯全开），所以归一成 None；照搬空列表会变成"一层都不取"。
        context_layers=(args.get("context_layers") or None),
        # 前驱集怎么算：knowledge（控制边 + 知识边，默认）/ control（只看控制边）
        predecessors=args.get("predecessors"),
    )
    ok = sum(1 for r in outcome.records if r.is_usable)
    review = sum(1 for r in outcome.records if r.status is TranslationStatus.NEEDS_REVIEW)
    failed = sum(1 for r in outcome.records if r.status is TranslationStatus.FAILED)
    imported = int(outcome.report.metrics.get("memory_imported_hits") or 0)
    # 步骤提醒：**只陈述盘上现在的事实**（不拦、不建议）。开工时看得见，
    # 而不是等成品出来才发现 —— 见 `layers/preflight.py::workflow_notes`。
    # 刻意**不加"还没做"这类前缀**：有的提醒说的是"已申报/已做"这种**正面事实**
    # （章那一栏补上文件后会显示"已申报"），加前缀就会出现"还没做 · 章：已申报"。
    # 文案自己已经说清了是什么事，这里原样转发。
    notices = [
        str(item.get("text") or "")
        for item in (outcome.report.metrics.get("workflow_detail") or [])
    ]
    # **模型又把已知事实报了一遍**：这个数偏高就说明
    # 请求里的【术语书】没被读懂，或提示词那条口径被换掉了。报告里另有 warning 一条。
    repeated = int(outcome.report.metrics.get("declared_facts_repeated") or 0) + int(
        outcome.report.metrics.get("declared_facts_near_duplicate") or 0
    )
    # **译文挂了错的原文**：一条译文结构全过、状态 ok，
    # 却不是这一句的译文 —— 这种"格式没问题但内容不对"的错误以前一条都不报。
    mismatched = int(outcome.report.metrics.get("correspondence_failed_records") or 0)
    summary_lines = [
        *[line for line in notices if line],
        f"{ok} 条可用",
        f"复用游戏里已有的译文 {imported} 条" if imported else "",
        f"{review} 条待复核（结构校验没过）" if review else "",
        f"{failed} 条没有译文" if failed else "没有失败条目",
        f"{mismatched} 条译文与原文对不上（已置留，逐条看一眼）" if mismatched else "",
        f"模型申报的设定里 {repeated} 条是书上已经写过的事实" if repeated else "",
        f"provider：{outcome.report.metrics.get('provider')}",
    ]
    session.interaction.emit(
        "run.summary",
        "翻译完成",
        lines=[line for line in summary_lines if line],
        severity="warning" if (failed or review or repeated or mismatched) else "success",
    )
    result = outcome.to_dict()
    # 跑批里模型申报 / 自动批准会改术语书 → 知识边与载荷就过期了。按约定**中途不逐批
    # 重写盘、收尾刷一次**（graph.json 每 1,024 单元约 2.4 MB，逐批重写不划算）。
    result["graph"] = _refresh_graph_after_terms(session)
    changed = _graph_change(edges_before, _edge_keys(session.graph))
    if changed:
        ordering = _ordering_change(closures_before, _closure_keys(session.graph))
        report_issue = {
            "added": changed["added"][:20],
            "removed": changed["removed"][:20],
            "added_count": len(changed["added"]),
            "removed_count": len(changed["removed"]),
            #: 传递前驱真的变了的区域 —— **空的就说明先后关系没变**（只是声明挪了位）
            "ordering_changed": ordering,
        }
        verdict = (
            f"**顺序变了**：{len(ordering)} 个区域的传递前驱不一样了"
            f"（{'、'.join(ordering[:5])}）"
            if ordering
            else "传递闭包**没变** —— 只是声明的落点挪了位，先后关系一个字没变"
        )
        outcome.report.add_issue(
            "warnings",
            code="graph_structure_changed",
            message=(
                f"这一轮跑完，**图结构变了**：知识边 +{len(changed['added'])} / "
                f"-{len(changed['removed'])}（现有 "
                f"{len(session.graph.dependencies) if session.graph else 0} 条）。"
                f"{verdict}。顺序就是下一次翻译的顺序 —— 按你的约定，看一眼再接着跑"
            ),
            detail=report_issue,
        )
        result["graph_structure_changed"] = report_issue
        # 报告之外**再出一次声**：命令行与面板读的是交互事件，warning 埋在 JSON 里没人看。
        session.interaction.emit(
            "graph.changed",
            "图结构变了",
            lines=[
                f"知识边 +{len(changed['added'])} / -{len(changed['removed'])}",
                *[f"新增：{line}" for line in changed["added"][:5]],
                *[f"撤销：{line}" for line in changed["removed"][:5]],
                verdict,
            ],
            severity="warning",
        )
        # ⚠️ 报告**在 `session.translate()` 里已经写过一次**（`_save_report`），这条警告是
        # 之后才加上的 —— 不补写一次，它就只活在交互事件里，报告里查无此条。
        session._save_report(outcome.report)
    return result


SCHEDULERS: tuple[Any, ...] = tuple(schedule.SCHEDULERS)

#: 知识边的类型名（`graph.knowledge` 写下的那些；与引擎边的空/`control_flow` 区分开）
KNOWLEDGE_EDGE_TYPE = "knowledge"


def _scheduler_named(name: str) -> Any:
    """按名字取调度策略。**不替调用方挑一个"更好的"** —— 没点名就用默认（照搬依赖分层）。

    注册表在 :mod:`gametrans.core.schedule`（`translate` 也要用它，见 ``plan_strategy``）；
    这里只把"不认识的名字"翻成控制面能读的 ``ConfigError``。
    """
    try:
        return schedule.scheduler_named(name)
    except ValueError as exc:
        raise ConfigError(
            str(exc), hint=f"可用：{', '.join(s.name for s in SCHEDULERS)}。"
        ) from None


def _payload_freshness(session: Any, graph: Any) -> dict[str, Any]:
    """盘上的载荷（边 ``weight``）是按哪一版术语书算的 —— 对不上就是**过期**。

    载荷是派生量：术语书一动，它就旧了。这里只**陈述事实**，不拦也不自动重算 ——
    重算入口是 `graph.knowledge`（术语书写完会自动调它，见 ``_refresh_knowledge``）。
    """
    stored = str((getattr(graph, "metadata", {}) or {}).get("payload_source") or "")
    current = ""
    if session.resources is not None:
        current = str(session.resources.versions().get("termbook") or "")
    if not stored:
        return {
            "stored": "",
            "current": current,
            "stale": True,
            "note": "图上的载荷还没算过（跑 `gametrans graph knowledge`）",
        }
    return {
        "stored": stored,
        "current": current,
        "stale": stored != current,
        "note": "" if stored == current else "术语书改过之后图上的载荷没跟着重算",
    }


def _progress_readings(session: Any, graph: Any, flow: Any = None) -> dict[str, Any]:
    """**推进的两个参数**，每个区域一行 —— 开销在节点上、重要性在出边上。

    * ``cost``：这个区域里各单元的 token 估算之和（输入 + 输出，一次直出）；
    * ``dependents``：有多少下游区域的边从它出发（"被后面多少节点依赖"）；
    * ``payload_out``：出边载荷合计（"一共向外交代了多少条知识"）。

    三个都只**读**，都不参与排序（设计口径：先不考虑排序）。

    ``paused_by`` 是第四样、也是唯一会**挡住开工**的一栏：这个区域要用到的前驱实体里
    还有写法没定译（见 :func:`gametrans.layers.tags.paused_regions`）。``None`` = 没被
    暂停；``[]`` = 自己不缺名字、但**前驱被暂停**，所以它也开不了跑。
    """
    regions = [r.region_id for r in graph.regions()]
    cost: dict[str, int] = {region: 0 for region in regions}
    texts: dict[str, list[str]] = {}
    for node in graph.translatable_nodes():
        region = graph.region_of(node.node_id)
        if region:
            cost[region] = cost.get(region, 0) + int(node.weight.cost)
            payload = (getattr(getattr(node.unit, "locator", None), "payload", None) or {})
            texts.setdefault(region, []).extend(
                str(slot.get("source") or "") for slot in (payload.get("slots") or [])
            )
    dependents: dict[str, int] = {region: 0 for region in regions}
    payload_out: dict[str, int] = {region: 0 for region in regions}
    for dependency in graph.ordering_dependencies():
        provider = graph.as_region(dependency.source)
        consumer = graph.as_region(dependency.target)
        if provider == consumer or provider not in payload_out:
            continue
        dependents[provider] += 1
        payload_out[provider] += int(dependency.weight)
    termbook = getattr(getattr(session, "resources", None), "termbook", None)
    paused = paused_regions(termbook, flow, texts)
    rows = [
        {
            "region": region,
            "cost": cost.get(region, 0),
            "dependents": dependents.get(region, 0),
            "payload_out": payload_out.get(region, 0),
            "paused_by": paused.get(region),
        }
        for region in regions
    ]
    return {
        "regions": rows,
        "cost_total": sum(cost.values()),
        "payload_total": sum(payload_out.values()),
        # 等定译的区域：**清单空**表示被上游拖住，不是自己缺名字
        "paused": dict(sorted(paused.items())),
        "paused_count": len(paused),
        "freshness": _payload_freshness(session, graph),
        "note": (
            "开销 = 各单元 token 估算之和（一次直出，不含固定上下文与分轮重复）；"
            "重要性 = 出边数 + 出边载荷合计。两者都只读、都不参与排序。"
            "等定译 = 这一段要用到前驱引入、而名字还没定的写法（空清单 = 前驱被暂停）。"
        ),
    }


def _plan(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """这次会按什么顺序翻 —— **谁等谁**，以及"最少要跑几轮"。

    主视图是两样东西：

    * ``predecessors``：每个区域的**前驱集**。执行语义只有这一条 —— 前驱全完成，自己就能开跑；
    * ``rounds_min`` / ``critical_path``：**最长依赖链**。它是"最少要跑几轮"，一个读数。

    ``phases`` 仍然返回，但它**是派生视图**（前驱集的拓扑分层），眼下还给执行器当 barrier 用；
    它不代表机制，也不该被当成"必要的轮数"。只读：不执行翻译、不改任何配置。
    """
    session = ctx.session()
    graph = session.graph
    if graph is None:
        raise GameTransError("还没有路径图", hint="先运行 `gametrans scan`。")
    scheduler = _scheduler_named(str(args.get("scheduler") or ""))
    options = session.translate_options(
        predecessors=args.get("predecessors")
    )
    plan = session.translate_layer.plan_for(
        graph, options, scheduler=scheduler, resources=session.resources
    )
    describe = scheduler.describe()
    # **只有一张前驱表**：算它的时候用哪种输入，表就是哪种 —— 不并排给两张。
    # 知识边那张见 `layers/entityflow.py`（一个区域的前驱 = 它引用的实体的引入场）。
    flow = session.translate_layer.entity_flow(graph, session.resources, options)
    if flow is not None and flow.entities:
        predecessors = flow.predecessors
        rounds = len(plan.phases)
        path = entityflow.longest_chain(predecessors, sorted(predecessors))
        source = "knowledge"
    else:
        predecessors = graph.region_predecessors()
        rounds = len(graph.region_layers())
        path = graph.critical_path()
        source = "control"
    payload = {
        # —— 机制 ——
        "predecessors": {
            region: sorted(providers) for region, providers in sorted(predecessors.items())
        },
        "predecessor_source": source,
        # —— 派生读数 ——
        "rounds_min": rounds,
        "critical_path": path,
        # —— 兼容视图：执行器还在用它做 barrier ——
        "strategy": plan.strategy,
        "validated": bool(describe.get("validated", True)),
        "note": str(describe.get("note") or ""),
        "notes": list(plan.notes),
        "phases": [phase.to_dict() for phase in plan.phases],
        "problems": plan.validate(),
        "regions": len(plan.region_order()),
        "schedulers": [entry.describe() for entry in SCHEDULERS],
    }
    if flow is not None:
        # 这一轮算进去多少实体、"最早"有多少处含糊 —— 和轮数并排看，免得把
        # "术语书还没做全"读成"可以并行"。
        payload["flow"] = flow.readings()
    # 推进的两个参数：开销（节点侧）与重要性（出边侧）。只读，不参与排序。
    payload["progress"] = _progress_readings(session, graph, flow)
    return payload


def _staleness(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """哪些译文该重做：过期 / 缺失 / 不可用分开点名，判不了的单列。

    这是"知识后到"能变便宜的那一步 —— 只点名，不重译。开关必须与当初翻译时一致，
    所以照配置里生效的 ``use_*`` 取值，而不是自己拍一个。
    """
    session = ctx.session()
    graph = session.graph
    if graph is None:
        raise GameTransError("还没有路径图", hint="先运行 `gametrans scan`。")
    config = session.config
    report = find_stale(
        graph,
        session.resources,
        session.translations(),
        use_glossary=bool(config.use_glossary),
        use_worldbook=bool(config.use_worldbook),
        use_style=bool(config.use_style),
        use_knowledge=bool(config.use_knowledge),
        # 自定义要求会进提示词，所以它也是一种"知识状态"：改了它要能点名受影响的译文
        custom_instructions=str(config.custom_instructions or ""),
    )
    payload = report.to_dict()
    payload["switches"] = {
        "use_glossary": bool(config.use_glossary),
        "use_worldbook": bool(config.use_worldbook),
        "use_style": bool(config.use_style),
        "use_knowledge": bool(config.use_knowledge),
        "custom_instructions": len(str(config.custom_instructions or "")),
    }
    return payload


def _writeback(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.session().writeback(
        overwrite_existing=bool(args.get("overwrite_existing"))
    ).to_dict()

def _pack(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    return ctx.session().pack().to_dict()


def _revalidate(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """判据变了，按新判据重新裁定当初被挡下的译文（只放行，不收紧）。"""
    session = ctx.session()
    outcome = session.revalidate_translations()
    if outcome["checked"]:
        session.interaction.emit(
            "translate.summary",
            "已按当前判据重新裁定",
            lines=[
                f"重看 {outcome['checked']} 条",
                f"放行 {len(outcome['released'])} 条",
                f"仍然挡住 {len(outcome['still_blocked'])} 条",
            ],
            severity="success" if outcome["released"] else "info",
        )
    return outcome


def _unify(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """统一替换：裁决之后把已经落盘的旧写法换掉（收口的最后一步）。

    替换源**只来自撞车证据**（那条术语记录下的"另一种译名"），不做全局正则替换；
    逐条重过结构校验，过不了的**不写**并如实列出来。`--dry-run` 先看会改几条。
    """
    session = ctx.session()
    outcome = session.unify_translations(
        by=str(args.get("by") or "human"),
        source=str(args.get("source") or ""),
        dry_run=bool(args.get("dry_run")),
    )
    lines = [
        f"处理的术语 {len(outcome['terms'])} 条",
        f"{'会改' if outcome['dry_run'] else '已改'} {outcome['changed']} 条译文",
    ]
    if outcome["rejected"]:
        lines.append(f"没过校验、没写 {len(outcome['rejected'])} 条")
    if not outcome["terms"]:
        lines.append("没有撞过车的术语 —— 没有可替换的写法（是不是还没裁决？）")
    session.interaction.emit(
        "translate.summary",
        "统一替换",
        lines=lines,
        severity="warning" if outcome["rejected"] else "info",
    )
    return outcome


def _term_list(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """列出术语书：**一行一个实体，五栏**（key / profile / constant / order / position）。

    ``--hints`` 额外把"**拆行 / 合行要看的依据**"摆出来：

    * 每条事实**提到了本行哪些写法** —— "这条设定说的是学校还是球队"，靠的是这句话本身；
    * 每个写法**各出现在哪些场次** —— 同一行里的写法各走各的场次，往往说明它们不是同一个
      实体（真靶那次的 `BSU` / `Wild Cats` / `BSU Wild Cats` 就是这样）。

    ⚠️ 不用 `entityflow` 的引入场：那是**行级**的，同一行里所有写法共用同一个值，摆出来等于
    没说。这里要的是**逐写法**的口径，所以直接按触发规则扫图（与注入、与包标签**同一套**规则）。

    刻意**不给结论**（不说"该拆"）：归属是语义判断，工具只把事实摆齐。
    """
    session = ctx.session()
    book = session.resources.termbook
    payload: dict[str, Any] = {
        "entries": [e.to_dict() for e in book.entries()],
        "pending": book.pending.records(),
        "summary": book.summary(),
    }
    if not args.get("hints"):
        return payload

    from gametrans.layers.trigger import TriggerPolicy, key_in_text

    graph = session.graph
    policy = TriggerPolicy()
    regions: list[tuple[str, str]] = []
    if graph is not None:
        # **按文档顺序**取（`walk()`），不是权重序：这一栏要回答的是"它头一次出现在哪一场
        # 剧情里"，按权重排出来的"头几场"是没意义的。
        for node_id in graph.walk():
            node = graph.nodes.get(node_id)
            unit = getattr(node, "unit", None) if node is not None else None
            source = str(unit.source or "") if unit is not None else ""
            if source:
                regions.append((graph.region_of(node_id) or "", source))

    hints: list[dict[str, Any]] = []
    for entry in book.entries():
        facts = [
            {
                "fact": fact,
                # 这条事实的字面里出现了本行的哪几个写法（按本行写法顺序）
                "mentions": [
                    writing
                    for writing in entry.writings
                    if writing.lower() in str(fact).lower()
                ],
            }
            for fact in entry.facts
        ]
        seen: list[dict[str, Any]] = []
        for writing in entry.writings:
            hit = [region for region, source in regions if key_in_text(source, writing, policy=policy)]
            seen.append(
                {
                    "writing": writing,
                    "regions": len(hit),
                    # 只看前几个：这一栏是给人扫一眼的，不是全量导出
                    "first": [region for region in hit[:6] if region],
                }
            )
        hints.append({"writing": entry.writing, "writings": seen, "facts": facts})
    payload["hints"] = hints
    payload["hints_note"] = (
        "`facts[].mentions` = 这条事实的字面里出现了本行哪些写法；"
        "`writings[].regions` / `.first` = 这个写法在原文里各出现在几场、头几场是哪些。"
        "两者都不是结论 —— 同一行里的写法各走各的场次、或事实各说各的名字，"
        "通常说明它们不是同一个实体；要不要拆由你判断（拆完走 `resource term import`）。"
    )
    return payload


def _entry_from_args(args: dict[str, Any]) -> "TermEntry":
    """把参数折成一行（五栏）。

    ``key`` 是写法列表：面板 / MCP 给数组，命令行给一段 JSON。只给 ``source`` +
    ``target`` 时是"一行一个写法"的简写（背兼容命令行习惯的那条路）。
    """
    from gametrans.layers.resource import TermEntry, facts_from_text

    raw = args.get("key")
    key: Any = []
    if raw in (None, "", [], {}):
        source = str(args.get("source") or "").strip()
        if source:
            key = [{"writing": source, "target": str(args.get("target") or "").strip()}]
    elif isinstance(raw, (list, dict)):
        key = raw
    else:
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise KnowledgeError(
                f"key 不是合法的 JSON：{exc.msg}",
                hint='形如 [{"writing": "Eve", "target": "伊芙"}]：一组写法，各带自己的译名。',
            ) from exc
        key = payload
    profile = args.get("profile")
    if isinstance(profile, str) or profile is None:
        facts = facts_from_text(profile)
    else:
        facts = list(profile)
    return TermEntry(
        key=key,
        profile=facts,
        constant=bool(args.get("constant")),
        order=args.get("order"),
        position=str(args.get("position") or "terms"),
    )


def _term_add(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """**人拍的板**：写进这一行（找不到同一行就新开一行）。

    ``exact=True`` = **整行按给进来的那份写**（面板的编辑表单：五栏全部读出来又全部
    送回来，所以"清掉一条事实 / 删掉一个写法"表达得出来）；缺省是**逐栏合并**，
    命令行只给一栏时不会顺手把另一栏清空。

    **只有写法、没有译名也没有事实的行是允许的** —— 那种行的用途是"这个名字还没定译"：
    翻译时它在原文里被包成 ``⟦写法⟧``（见 :mod:`gametrans.layers.tags`），定译之后按
    术语书渲染。从前这里会拒掉它，因为那时空行确实什么都注不进去。

    变更会记进 ``termbook.changes.jsonl``；这一行上还没裁的待审提案会被撤掉
    （人已经拍了板，旧提案留着会在"采用"时把人的决定覆盖回去）。
    """
    entry = _entry_from_args(args)
    if not entry.writing:
        raise KnowledgeError(
            "这一行没有写法（`key` 为空）—— 既寻址不到，也判不了「什么时候该说话」。",
            hint="给一个写法（`source` / `--key`），或者干脆别写这一行。",
        )
    entry = ctx.session().resources.termbook.add(
        entry,
        exact=bool(args.get("exact")),
        by=str(args.get("by") or "human"),
        why=str(args.get("why") or ""),
    )
    return {
        "entry": entry.to_dict(),
        "graph": _refresh_graph_after_terms(ctx.session()),
    }


def _term_import(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """**整份换上术语书**：拆行 / 合行 / 批量改名一次做完，每一步都进变更日志。

    缺这条通道时，agent 只能直接改 `termbook.jsonl` —— 动作对，但**一点审计痕迹都不留**
    （真靶实测：一次 29 行的整理之后 `changes: 0`，谁在什么时候拆了哪一行查不出来）。
    """
    path = Path(str(args.get("file") or "")).expanduser()
    if not path.is_file():
        raise GameTransError(
            f"找不到这份 JSONL：{path}",
            hint="形状同 `termbook.jsonl`：一行一个实体，五栏 `key` / `profile` / "
                 "`constant` / `order` / `position`。",
        )
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GameTransError(
                f"{path.name} 第 {number} 行不是合法 JSON：{exc.msg}",
                hint="整份换上要么全成要么不动 —— 坏行不跳过，否则你会以为写进去了。",
            ) from exc
        if not isinstance(payload, dict):
            raise GameTransError(f"{path.name} 第 {number} 行不是对象")
        records.append(payload)

    session = ctx.session()
    book = session.resources.termbook
    if args.get("dry_run"):
        wanted = [
            str(((record.get("key") or [{}])[0]).get("writing") or "")
            for record in records
        ]
        wanted = [writing for writing in wanted if writing]
        current = [entry.writing for entry in book.entries()]
        return {
            "dry_run": True,
            "would_write": len(wanted),
            "would_remove": [writing for writing in current if writing not in set(wanted)],
            "order": wanted,
            "current_rows": len(current),
        }

    outcome = book.import_rows(
        records,
        by=str(args.get("by") or "agent"),
        why=str(args.get("why") or ""),
    )
    outcome["graph"] = _refresh_graph_after_terms(session)
    return outcome


def _term_remove(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    writing = str(args.get("writing") or args.get("source") or "")
    removed = ctx.session().resources.termbook.remove(
        writing, why=str(args.get("why") or "")
    )
    return {
        "removed": removed,
        "writing": writing,
        "graph": _refresh_graph_after_terms(ctx.session()),
    }


def _term_propose(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """提一条**更正**（改已有的译名 / 改已有的某条事实）—— 它先排队，不直接生效。"""
    record = ctx.session().resources.termbook.propose(
        what=str(args.get("what") or ""),
        writing=str(args.get("writing") or ""),
        index=args.get("index"),
        new=str(args.get("new") or ""),
        why=str(args.get("why") or ""),
    )
    return {"pending": record}


def _term_pending_list(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """列出**待审更正**（改已有的译名 / 事实）：采用之前一个字节都不动。"""
    records = ctx.session().resources.pending_corrections()
    writing = str(args.get("writing") or "").strip()
    if writing:
        records = [r for r in records if str(r.get("writing") or "") == writing]
    return {"pending": records, "total": len(records)}


def _term_pending_adopt(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """**采用**一条待审更正：把 ``old`` 换成 ``new``、记变更日志、从队列里删掉。

    ``by`` 只认 human / user / agent —— 模型身份提得了提案，拍不了板。
    """
    return {
        **ctx.session().resources.adopt_correction(
            str(args.get("id") or ""),
            by=str(args.get("by") or "human"),
            why=str(args.get("why") or ""),
        ),
        "graph": _refresh_graph_after_terms(ctx.session()),
    }


def _term_pending_drop(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """**丢弃**一条待审更正（不改书，只从队列里删掉）。"""
    return ctx.session().resources.discard_correction(str(args.get("id") or ""))


def _deviation_list(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """列出声明的偏离。**纯只读。**"""
    entries = ctx.session().resources.deviations.entries()
    return {"entries": [entry.to_dict() for entry in entries], "total": len(entries)}


def _deviation_add(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """声明一条偏离：这条译文**有意**偏离判据的哪一条。

    * ``empty`` —— 有意留空（抹掉不该出现的英文、或界面留白）；
    * ``expression`` —— 经批准的表达式改写（例如接 helper 把运行时数字转成中文）。
    """
    entry = Deviation(
        unit_id=str(args.get("unit_id") or ""),
        kind=str(args.get("kind") or ""),
        by=str(args.get("by") or "agent"),
        reason=str(args.get("reason") or ""),
    )
    ctx.session().resources.deviations.add(entry)
    return {"entry": entry.to_dict()}


def _deviation_remove(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    removed = ctx.session().resources.deviations.remove(
        str(args.get("unit_id") or ""), str(args.get("kind") or "")
    )
    return {"removed": removed, "unit_id": str(args.get("unit_id") or "")}


def _resource_validate(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    problems = ctx.session().resources.validate()
    return {
        "problems": [p.to_dict() for p in problems],
        "ok": not problems,
    }


def _resource_term_candidates(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """从场摘要抽实体：**一个实体一行**写进术语书（新形状五栏）。

    零模型成本，读工作区的 ``summaries.json`` + ``graph.json``；**幂等** ——
    已经登记过的写法（含已否决）跳过，重复调用不会重复产行。新增免审，
    要改已有的译名会进待审队列（不在这一步直接改）。
    """
    session = ctx.session()
    outcome = collect_term_candidates(
        session.workdir,
        session.resources.termbook,
        min_scenes=int(args.get("min_scenes") or 2),
    )
    outcome["language"] = str(args.get("language") or "")
    outcome["graph"] = _refresh_graph_after_terms(session)
    return outcome


def _render_term_candidates(data: dict[str, Any]) -> str:
    lines = [
        f"摘要侧实体：新增 {data.get('created_count', 0)} 行（只登记写法，没有译名）/ "
        f"跳过 {data.get('skipped_count', 0)} 条（已登记过的写法不再产行）"
    ]
    blocked = dict(data.get("blocked") or {})
    if data.get("blocked_lower_initial"):
        blocked["首字母不是大写"] = int(data["blocked_lower_initial"])
    if data.get("below_min_scenes"):
        blocked[f"跨场不足 {data.get('min_scenes', 2)} 场"] = int(data["below_min_scenes"])
    if blocked:
        lines.append("挡下：" + "、".join(f"{name} {count}" for name, count in blocked.items()))
    created = [
        str(((row.get("key") or [{}])[0]).get("writing") or "")
        for row in data.get("created") or []
    ]
    if created:
        shown = "、".join(created[:24])
        lines.append(f"新增行：{shown}{'…' if len(created) > 24 else ''}")
    return "\n".join(lines)


def _harvest_existing(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """把游戏里已有的译文读进来，沉淀成资产。**只读游戏目录。**"""
    session = ctx.session()
    return session.harvest_existing(
        language=str(args.get("language") or ""),
        min_occurrences=int(args.get("min_occurrences") or 2),
        max_candidate_length=int(args.get("max_candidate_length") or 24),
    )


def _supplement_list(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """列出声明的补充条目。**纯只读。**"""
    entries = ctx.session().resources.supplements.entries()
    return {"entries": [entry.to_dict() for entry in entries], "total": len(entries)}


def _supplement_add(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """声明一条补充条目：引擎不枚举、但玩家看得见的文本。

    落盘在资源层的声明文件里；**产物**由写回一步落成语言包（真机实测：这些条目在
    运行时会被引擎查到并翻译，不用动游戏源码）。
    """
    entry = Supplement(
        source=str(args.get("source") or ""),
        target=str(args.get("target") or ""),
        by=str(args.get("by") or "agent"),
        reason=str(args.get("reason") or ""),
        file=str(args.get("file") or ""),
        line=int(args.get("line") or 0),
    )
    ctx.session().resources.supplements.add(entry)
    return {"entry": entry.to_dict()}


def _supplement_remove(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    removed = ctx.session().resources.supplements.remove(str(args.get("source") or ""))
    return {"removed": removed, "source": str(args.get("source") or "")}


# ---- 任务状态（只读） -----------------------------------------------------


def _tasks(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """列出任务状态，或按 task_id / unit_id 看一条任务的完整载荷。

    状态是持久化的，因此这个入口回答的是"上一轮跑到哪了"，而不只是"内存里有什么"。
    """
    session = ctx.session()
    ref = str(args.get("ref") or "").strip()
    if ref:
        return {"task": session.task_detail(ref)}
    return session.task_summary(
        status=str(args.get("status") or ""),
        limit=int(args.get("limit") or 0),
    )


# ---- agent 作答通道（挂单队列） ---------------------------------------------


def _queue_for(ctx: Context) -> "AgentQueue":
    """当前项目的挂单队列。队列在工区里，两个进程靠文件对话，不需要共享内存。"""
    return AgentQueue(ctx.session().workdir / QUEUE_DIRNAME)


def _agent_next(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """取最老的一条挂单并**认领**它（取单即占坑），把本该发给 API 的完整请求原样交给作答方。"""
    queue = _queue_for(ctx)
    parked = queue.claim_next()
    if parked is None:
        return {
            "empty": True,
            "message": (
                "挂单队列是空的：没有等待作答的请求。"
                "先用 --provider agent 跑一轮 translate 才会有挂单。"
            ),
        }
    return {
        "request_id": parked["request_id"],
        "phase": parked.get("phase", ""),
        "expected": parked.get("expected", 0),
        "unit_ids": parked.get("unit_ids", []),
        "created_at": parked.get("created_at", ""),
        "messages": parked.get("messages", []),
        "answer_format": (
            'JSON：{"translations": [{"unit_id": "…", "target": "…"}]}，'
            '可选 "terms": [{"source": "…", "target": "…", "profile": "…"}]'
        ),
        "how": (
            f"gametrans agent submit {parked['request_id']} --answer <上面的JSON>"
        ),
    }


def _agent_submit(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    queue = _queue_for(ctx)
    parked = queue.submit(str(args["request_id"]), str(args.get("answer") or ""))
    return {
        "request_id": parked["request_id"],
        "status": parked["status"],
        "queue": queue.status(),
    }


def _agent_status(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    return _queue_for(ctx).status()


# ---- 风格资源 -------------------------------------------------------------


def _style_list(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    guide = ctx.session().resources.style
    return {
        "entries": [entry.to_dict() for entry in guide.entries()],
        "problems": [issue.to_dict() for issue in guide.validate()],
    }


def _style_add(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from gametrans.layers.style import StyleEntry

    entry = ctx.session().resources.style.add(
        StyleEntry(
            aspect=args["aspect"],
            value=args["value"],
            scope=str(args.get("scope") or "global"),
            note=str(args.get("note") or ""),
            priority=int(args.get("priority") or 50),
        )
    )
    return {"entry": entry.to_dict()}


def _style_remove(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    removed = ctx.session().resources.style.remove(
        args["aspect"], str(args.get("scope") or "global")
    )
    return {"removed": removed, "aspect": args["aspect"]}


def _ui_views(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    if args.get("all"):
        views = session.interaction.all_views()
    else:
        views = session.interaction.visible_views()
    return {
        "scope": "all" if args.get("all") else "user",
        "views": [v.to_dict(session.interaction.policy().resolve(v.topic)) for v in views],
    }


def _ui_render(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    return {"rendered": ctx.session().interaction.render()}


def _ui_policy_set(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    session.interaction.set_policy(args["topic"], args["visibility"])
    return {
        "topic": args["topic"],
        "visibility": args["visibility"],
        "policy": session.interaction.policy().to_dict(),
    }


def _ui_announce(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    view = session.interaction.announce(
        args["message"], severity=args.get("severity") or "info"
    )
    return {"view": view.to_dict()}


def _ui_override(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    view = session.interaction.override(
        args["view_id"],
        title=args.get("title"),
        note=args.get("note"),
    )
    return {"view": view.to_dict()}


def _ui_hide(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.session()
    view = session.interaction.hide(args["view_id"], hidden=not args.get("unhide"))
    return {"view": view.to_dict()}


def _ui_clear(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    removed = ctx.session().interaction.clear()
    return {"removed": removed}


def _config_show(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from gametrans.userconfig import global_config_path

    session = ctx.session()
    return {
        "config": session.config.to_dict(),
        "path": str(session.workdir / "project.json"),
        # **每一项的生效值来自哪一层** —— 四层之后没有它，"我改了怎么没生效"没法查
        "sources": _config_sources(session),
        "layers": {
            "project": {"path": str(session.workdir / "project.json")},
            "global": {
                "path": str(global_config_path()),
                "values": dict(session.global_config),
                "fields": list(GLOBAL_FIELDS),
            },
        },
        # 凭证只出现在这里，且只有尾巴（见 gametrans/credentials.py）
        "credentials": session.credentials_view(),
    }


def _config_sources(session: Any) -> dict[str, str]:
    from gametrans.userconfig import resolve_config_sources

    return resolve_config_sources(session._project_values, session.global_config)


def _config_path(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """四层各自的文件在哪 —— 平台目录不好猜，所以必须能问出来。"""
    import os

    from gametrans.credentials import CREDENTIALS_FILE
    from gametrans.userconfig import (
        ENV_HOME,
        global_config_path,
        global_credentials_path,
    )

    session = ctx.session()
    return {
        "project": {
            "config": str(session.workdir / "project.json"),
            "credentials": str(session.workdir / CREDENTIALS_FILE),
        },
        "global": {
            "dir": str(global_config_path().parent),
            "config": str(global_config_path()),
            "credentials": str(global_credentials_path()),
            "relocated_by": ENV_HOME if os.environ.get(ENV_HOME) else "",
        },
        "precedence": [
            "环境变量（只有凭证有）",
            "项目配置（工作区里那份）",
            "全局默认（上面那个目录）",
            "出厂默认",
        ],
    }


def _config_set(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """改一项配置。

    默认改**项目层**（与一直以来的行为一致）；``--global`` 改全局层（一次配好、
    所有项目通用）。凭证字段走凭证文件那条路，其余进 project.json。
    """
    from gametrans.credentials import CREDENTIAL_FIELDS
    from gametrans.userconfig import save_global_config, save_global_credentials

    session = ctx.session()
    key = args["key"]
    raw = args["value"]
    use_global = bool(args.get("global_scope"))

    if key in CREDENTIAL_FIELDS:
        if use_global:
            values = dict(session.global_credentials)
            values[key] = raw
            save_global_credentials(values)
            session.reload_layers()
        else:
            session.update_credentials(**{key: raw})
        return {
            "config": session.config.to_dict(),
            "credentials": session.credentials_view(),
            "changed": {key: "（已清除）" if not str(raw).strip() else "…" + str(raw)[-4:]},
            "layer": "global" if use_global else "project",
        }

    if use_global:
        if key not in GLOBAL_FIELDS:
            raise ConfigError(
                f"{key} 不能放进全局配置 —— 它属于某个具体项目",
                hint=_why_project_only(key),
            )
        values = dict(session.global_config)
        values[key] = _coerce_config_value(key, raw)
        save_global_config(values)
        session.reload_layers()
    else:
        session.update_config(**{key: _coerce_config_value(key, raw)})
    return {
        "config": session.config.to_dict(),
        "changed": {key: session.config.to_dict().get(key)},
        "layer": "global" if use_global else "project",
        "sources": _config_sources(session),
    }


def _config_unset(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """删掉某一层的某一项 —— **回落下一层**，而不是设成默认值。

    这是四层配置里唯一的撤销语义：设成空串/默认值是"我就要这个值"。
    """
    from gametrans.credentials import CREDENTIAL_FIELDS
    from gametrans.userconfig import clear_global_config_field, clear_global_credential_field

    session = ctx.session()
    key = args["key"]
    use_global = bool(args.get("global_scope"))

    if key in CREDENTIAL_FIELDS:
        if use_global:
            clear_global_credential_field(key)
            session.reload_layers()
        else:
            session.update_credentials(**{key: None})
        return {
            "credentials": session.credentials_view(),
            "removed": key,
            "layer": "global" if use_global else "project",
        }

    if use_global:
        clear_global_config_field(key)
        session.reload_layers()
    else:
        session.update_config(**{key: None})
    return {
        "removed": key,
        "layer": "global" if use_global else "project",
        "config": session.config.to_dict(),
        "sources": _config_sources(session),
    }


def _why_project_only(key: str) -> str:
    from gametrans.userconfig import project_only_reason

    return project_only_reason(key)


def _config_providers(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """列出 provider 与它们的当前状态（凭证从工作区读，密钥不回显）。"""
    providers = ctx.session().providers
    return {"providers": [p.describe() for p in providers.all()]}


def _render_ui_views(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for view in data.get("views", []):
        lines.append(f"[{view['visibility']}] {view['title']}  <{view['view_id']}>")
        if view.get("overridden_by"):
            lines.append(f"    （已被 {view['overridden_by']} 改写）")
        for section in view.get("sections", []):
            if section.get("title"):
                lines.append(f"  {section['title']}:")
            for line in section.get("lines", []):
                lines.append(f"    {line}")
    return "\n".join(lines) if lines else f"（{data.get('scope', 'user')} 范围内没有视图）"


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

_PROJECT = Param("project", "path", "游戏项目根目录", default=".")

OPERATIONS: list[Operation] = [
    # —— 项目 ——
    Operation(
        "project.init",
        "初始化 gametrans 工作区（自动探测引擎）",
        _project_init,
        [
            Param("engine", "str", "指定引擎支持包（默认自动探测）"),
            Param("target_language", "str", "目标语言", default="zh_CN"),
            Param(
                "provider",
                "str",
                "翻译 provider（不指定就不写进项目，跟全局层/出厂默认走）",
            ),
        ],
        group="project",
    ),
    Operation("project.status", "查看项目各层的当前状态", _project_status, group="project"),
    # —— 引擎资源 ——
    Operation("engine.list", "列出已安装的引擎支持包", _engine_list, group="engine"),
    Operation(
        "engine.info",
        "查看某个引擎支持包的能力",
        _engine_info,
        [Param("name", "str", "引擎支持包名", positional=True)],
        group="engine",
    ),
    Operation(
        "engine.detect",
        "探测目录使用的是什么引擎",
        _engine_detect,
        [Param("path", "path", "要探测的目录（默认项目目录）")],
        group="engine",
    ),
    # —— 提取层 ——
    Operation(
        "engine.options",
        "查看当前引擎的私有选项与外部工具链（官方 SDK）状态",
        _engine_options,
        group="engine",
        mutates=False,
    ),
    Operation(
        "engine.option.set",
        "设置一条引擎私有选项（例如外部工具链的路径）",
        _engine_option_set,
        [
            Param("key", "str", "选项名（由引擎支持包约定）", positional=True),
            Param("value", "str", "选项值（路径等）", positional=True),
        ],
        group="engine",
    ),
    Operation(
        "engine.option.clear",
        "删掉一条引擎私有选项",
        _engine_option_clear,
        [Param("key", "str", "选项名", positional=True)],
        group="engine",
    ),
    Operation("scan", "提取全部待译内容，产出带权路径图", _scan, group="extract"),
    Operation(
        "graph.show",
        "按权重列出路径图节点",
        _graph_show,
        [Param("limit", "int", "最多返回多少条", default=20)],
        group="extract",
    ),
    Operation(
        "graph.node",
        "按逻辑路径/node_id/unit_id 查看一个节点",
        _graph_node,
        [Param("ref", "str", "逻辑路径、node_id 或 unit_id", positional=True)],
        group="extract",
    ),
    Operation(
        "graph.dependencies",
        "列出依赖边：候选（方向未确认）与已确认分开报",
        _graph_dependencies,
        group="extract",
        mutates=False,
    ),
    Operation(
        "graph.depend",
        "记下/确认一条依赖边（确认方向后它才参与调度排序）",
        _graph_depend,
        [
            Param("source", "str", "起点：区域 id（谁提供上下文）", positional=True),
            Param("target", "str", "终点：区域 id（谁消费上下文）", positional=True),
            Param(
                "direction",
                "str",
                "方向：control_flow / agent 才参与排序",
                default="agent",
                choices=["control_flow", "reading_order", "agent"],
            ),
            Param("topics", "csv", "知识点，例如 character:Eileen,world:Kingsway"),
            Param("note", "str", "备注"),
        ],
        group="extract",
    ),
    Operation(
        "graph.undepend",
        "删掉一条依赖边",
        _graph_undepend,
        [
            Param("source", "str", "起点：区域 id", positional=True),
            Param("target", "str", "终点：区域 id", positional=True),
        ],
        group="extract",
    ),
    Operation(
        "graph.knowledge",
        "把知识边写进图（术语书 + 原文命中算出来的先后），或撤掉",
        _graph_knowledge,
        [
            Param(
                "mode",
                "str",
                "apply（默认，加知识边）/ replace（连控制流排序边一起撤，图上只剩新结构）/ "
                "remove（只撤知识边）",
                default="apply",
                choices=["apply", "replace", "remove"],
            ),
        ],
        group="extract",
    ),
    Operation(
        "ir",
        "导出 Localization IR 并校验提取层一致性",
        _ir,
        [Param("limit", "int", "最多返回多少条 Unit（0 = 全部）", default=0)],
        group="extract",
        mutates=False,
    ),
    # —— 骨架（引擎产物，只读查看）——
    Operation(
        "skeleton.status",
        "查看引擎生成的骨架里有什么（槽位数、两种定键方式的分布、按文件的拆分）",
        _skeleton_status,
        [Param("language", "str", "目标语言；留空则自动取磁盘上唯一的那个")],
        group="skeleton",
        mutates=False,
    ),
    # —— 语言包（事实面：给 agent 说清"该说什么"，不替它拿主意）——
    Operation(
        "language.facts",
        "查看语言包事实：游戏认哪些语言代码、字体从哪来、语言目录里现有什么",
        _language_facts,
        [Param("language", "str", "目标语言；留空则用项目配置里的目标语言")],
        group="language",
        mutates=False,
    ),
    # —— 翻译层 ——
    Operation(
        "translate",
        "按路径图翻译（串行或并行）",
        _translate,
        [
            Param("provider", "str", "provider 名（mock / openai / agent：挂单等 agent 作答）"),
            Param("target_language", "str", "目标语言"),
            Param("batch_size", "int", "一次调用最多装几条（封顶；段装得下就整段一次）"),
            Param(
                "group_by",
                "str",
                "按引擎结构的哪一级分组（默认按项目配置的 auto：适配层申报的首级）；"
                "none = 这一轮不分组（逐条基线的对照臂）",
            ),
            Param(
                "unit_scope",
                "csv",
                "这一轮只翻这些单元（unit_id 列表，逗号分隔）；不给 = 全图。"
                "分块/续跑用：已完成的分块重跑时一个 token 都不该再问模型",
            ),
            Param("mode", "str", "调度模式", choices=["auto", "serial", "parallel"]),
            Param("concurrency", "int", "并行度"),
            Param("retry_on_violation", "int", "结构校验没过时重试几次（受 max_attempts 封顶）"),
            Param(
                "reuse_imported",
                "bool",
                "把读进来的已有译文（resource harvest）也当命中复用；"
                "默认不认 —— 它们没有知识指纹，证明不了是在当前术语状态下翻的",
            ),
            Param(
                "no_produce",
                "bool",
                "关掉「跑完把译文变成资产」（术语候选）；对照臂用，默认开着",
            ),
            Param(
                "context_layers",
                "csv",
                "这一轮只走检索阶梯的哪几层（direct,structural,knowledge,memory,targeted）；"
                "不给 = 阶梯全开。给消融实验用：声明了才真的少取那几层",
            ),
            Param(
                "scheduler",
                "str",
                "用哪个调度策略算计划（与 plan 命令同一个；默认 chapter-parallel，"
                "没申报章的项目自动退回严格分层）。"
                "seed-bulk 才有「第一轮只为产出资产」的两段形状 —— 它自称未校准，所以只是可选",
            ),
            Param(
                "stop_after_phase",
                "int",
                "跑完第几个阶段就停（0 = 不停）。第一轮＝计划的第一个阶段：跑完停下来，"
                "等人或 agent 给资产拍板，再用 --start-phase 接着跑",
            ),
            Param(
                "start_phase",
                "int",
                "从计划的第几个阶段开始跑（1 起数）。续跑用：第一阶段不会重问一次模型",
            ),
            Param(
                "asset_gate",
                "str",
                "开工前资产预检的处置：block（默认）=「批准过的资产一条都进不了请求」"
                "时拒绝开工；warn = 只报不拦",
                choices=["block", "warn"],
            ),
            Param(
                "memory_reuse_mode",
                "str",
                "命中的句子怎么处理：keep（默认）=请求里摆出原文和已定译、只要新句子；"
                "polish = 命中句照样要模型回译（可以为了上下文通顺提出改动），"
                "改动只记成修订提案、不就地生效",
                choices=["keep", "polish"],
            ),
            Param(
                "predecessors",
                "str",
                "前驱集怎么算：knowledge（默认，控制边 + 知识边 —— 一个区域等它引用的实体的"
                "引入场，章内按它分波、章间照旧按章序）/ control（只看控制边 = 老行为）",
                choices=["knowledge", "control"],
            ),
            Param(
                "unit_budget",
                "int",
                "一个单元一次最多问几条槽位（0 = 不切，默认）。慢模型上单次请求有物理"
                "上限（真靶实测 90 条的单元在 300 秒处被服务端回 HTTP 400），切开才拿得到"
                "译文；切出来的段仍是同一个单元，段间串行并把前一段译文交给后一段当上下文",
            ),
            Param(
                "round_lines",
                "int",
                "一个单元**分几轮**问完（0 = 一次发完，默认；>0 = 每轮最多这么多条）。"
                "长单元一次发不完时用它：每轮只发这一轮的句子，前几轮的原文与译文由会话历史"
                "带着（不重发、不写「继续」），轮内串行。撞输出上限会自动转多轮（每轮 "
                f"{DEFAULT_ROUND_LINES} 条）并把这一轮对半再切，整批报废因此变成只丢一轮",
            ),
            Param(
                "batch_units",
                "int",
                "小批量并行：把本来一层一层串行的计划按不超过这么多**单元**攒成一批，"
                "批内并行、批间串行（0 = 不攒，严格按分层走，默认）。视觉小说的图是一条长链"
                "（真靶 32 层、27 层只有 1 个单元），攒批把轮数按批量降下来；代价是批内后面的"
                "单元看不见前面刚定下的叫法 —— 每批跑完由「合并术语」补，见 auto_approve_terms",
            ),
            Param(
                "auto_approve_terms",
                "int",
                "每批跑完合并术语：同一个原文在**不同的这么多批**里各自被申报过一次且译名"
                "一致 → 由 agent 身份自动批准，下一批就真能用（默认 2；0 = 关，只落候选，"
                "全交给人）。撞车（同一原文多种译名）谁都不批，记进报告的 term_conflicts；"
                "语料判据说「不像专名」的不自动批准；已否决的不复活",
            ),
        ],
        group="translate",
    ),
    # —— 计划与补译定位（只读：一个说顺序，一个说该重做哪些）——
    Operation(
        "plan",
        "看这次翻译会按什么顺序跑：区域 → 有序阶段（可指定调度策略）",
        _plan,
        [
            Param("scheduler", "str", "调度策略名（默认 chapter-parallel，没章自动退回 layered；见返回里的 schedulers）"),
            Param(
                "predecessors",
                "str",
                "前驱集怎么算：knowledge（默认，控制边 + 知识边 —— 一个区域等它引用的实体的"
                "引入场）/ control（只看控制边）。这是同一张表的两种算法，不是两张表",
                choices=["knowledge", "control"],
            ),
        ],
        group="translate",
        mutates=False,
    ),
    Operation(
        "staleness",
        "点名该重做的译文：过期 / 缺失 / 不可用分开报（知识改了走这条精确回补）",
        _staleness,
        group="translate",
        mutates=False,
    ),
    # —— 写回层 ——
    Operation(
        "writeback",
        "把译文写回游戏，生成翻译层（默认只填空缺，不动产物里已有的译文）",
        _writeback,
        [
            Param(
                "overwrite_existing",
                "bool",
                "显式声明：用当前译文覆盖产物里已有的译文（默认只填空缺）",
            )
        ],
        group="writeback",
    ),
    Operation(
        "revalidate",
        "按当前判据重新裁定当初被挡下的译文（只放行，不收紧）",
        _revalidate,
        group="translate",
    ),
    Operation(
        "unify",
        "统一替换：把已落盘译文里**撞车过的旧写法**换成那一条的定译"
        "（裁决之后收口用；替换源只来自撞车证据，逐条重过结构校验并留痕）",
        _unify,
        [
            Param(
                "by",
                "str",
                "谁批准这次替换（human / user / agent；模型身份会被拒）",
            ),
            Param("source", "str", "只处理这一条术语的原文（不给 = 处理全部撞过车的）"),
            Param("dry_run", "bool", "只算不写：先看会改几条、有没有过不了校验的"),
        ],
        group="translate",
    ),
    Operation("pack", "把翻译产物封包成可分发补丁", _pack, group="writeback"),
    # —— 任务状态（持久化，只读查看）——
    Operation(
        "tasks",
        "查看持久化的翻译任务状态；给 --ref 则看某一条的完整载荷（含检索结果）",
        _tasks,
        [
            Param("ref", "str", "task_id 或 unit_id；留空则列出全部"),
            Param("status", "str", "只看某个状态"),
            Param("limit", "int", "最多返回多少条（0 = 全部）", default=0),
        ],
        group="translate",
        mutates=False,
    ),
    # —— agent 作答通道（挂单队列：跑批侧挂单等待，作答侧取单交答案）——
    Operation(
        "agent.next",
        "取最老的一条挂单并认领（本该发给 API 的完整请求，由 agent 用自己的额度作答）",
        _agent_next,
        group="agent",
    ),
    Operation(
        "agent.submit",
        "交一份答案给挂单（只有 pending 的单能收；答案回到同一条校验链）",
        _agent_submit,
        [
            Param("request_id", "str", "挂单 id（agent next 返回的那个）", positional=True),
            Param("answer", "str", "答案正文（JSON：translations 数组，可选 terms）", multiline=True),
        ],
        group="agent",
    ),
    Operation(
        "agent.status",
        "看挂单队列现状：几条在等、几条已答未收",
        _agent_status,
        group="agent",
    ),
    # —— 资源层：术语书（**一行一个实体，五栏**）——
    Operation(
        "resource.term.list",
        "列出术语书（一行一个实体：key 写法列表 / profile 事实列表 / constant / order / position）",
        _term_list,
        [
            Param("hints", "bool", "把整理术语书要看的依据也摆出来：每条事实提到了本行哪些"
                                   "写法、每个写法各出现在哪些场次（不含判断）"),
        ],
        group="resource",
    ),
    Operation(
        "resource.term.add",
        "**人拍的板**：整行按给进来的那份写（五栏都可以只填一栏；找不到同一行就新开一行）",
        _term_add,
        [
            Param("source", "str", "原文写法（简写：等价于只有一个写法的 key）",
                  positional=True),
            Param("target", "str", "这个词的译名（简写用）", positional=True),
            Param(
                "key",
                "json",
                '一组写法，各带自己的译名：[{"writing": "Eve", "target": "伊芙"}]。'
                "任一写法在原文里命中即触发这一行",
            ),
            Param("profile", "str", "事实列表，**一行一条**（注入时用「；」连成一行）",
                  multiline=True),
            Param("constant", "bool", "蓝灯：没有写法命中也每次都注入（世界观 / 风格类）"),
            Param("order", "int", "注入顺序：数值大的更靠后（更靠近提示词末尾）", default=100),
            Param("position", "str", "进哪一段：terms（【术语书】）/ tail（该段最后）",
                  default="terms", choices=["terms", "tail"]),
            Param("exact", "bool", "整行按给进来的那份写（面板的编辑表单用；缺省是逐栏合并）"),
            Param("by", "str", "谁拍的板：human / user / agent", default="human"),
            Param("why", "str", "为什么这么改（记进变更日志）"),
        ],
        group="resource",
    ),
    Operation(
        "resource.term.remove",
        "删除一行（按任一写法）",
        _term_remove,
        [
            Param("source", "str", "任一写法", positional=True, required=True),
            Param("why", "str", "为什么删（记进变更日志）"),
        ],
        group="resource",
    ),
    Operation(
        "resource.term.import",
        "**整份换上**：让术语书最终恰好是这份 JSONL 里的几行 —— 拆行 / 合行 / 批量改名"
        "一次做完，每一步都进变更日志",
        _term_import,
        [
            Param("file", "str", "那份 JSONL（形状同 termbook.jsonl：key / profile /"
                                 " constant / order / position）", positional=True,
                  required=True),
            Param("dry_run", "bool", "只报「会改成什么」，一个字节都不写"),
            Param("by", "str", "谁拍的板：human / user / agent", default="agent"),
            Param("why", "str", "为什么这么改（记进变更日志）"),
        ],
        group="resource",
    ),
    Operation(
        "resource.term.propose",
        "提一条**更正**（改已有的译名 / 某条事实）：先排队，采用之前不生效",
        _term_propose,
        [
            Param("what", "str", "改哪一栏：key（译名）/ profile（事实）",
                  positional=True, required=True, choices=["key", "profile"]),
            Param("writing", "str", "改哪一个写法（profile 那一栏给行身份）",
                  positional=True, required=True),
            Param("index", "int", "改列表里的第几条（改写法的译名时可不给）", default=None),
            Param("new", "str", "改成什么", positional=True, required=True),
            Param("why", "str", "为什么（人要看得懂的一句话）"),
        ],
        group="resource",
    ),
    Operation(
        "resource.term.pending.list",
        "列出待审更正（改已有的译名 / 事实）——采用之前书里一个字节都不动",
        _term_pending_list,
        [Param("writing", "str", "只看这个写法的提案")],
        group="resource",
        mutates=False,
    ),
    Operation(
        "resource.term.pending.adopt",
        "**采用**一条待审更正：把 old 换成 new、记变更日志、从队列里删掉（模型身份会被拒）",
        _term_pending_adopt,
        [
            Param("id", "str", "提案 id", positional=True, required=True),
            Param("by", "str", "谁拍的板：human / user / agent", default="human"),
            Param("why", "str", "为什么（记进变更日志）"),
        ],
        group="resource",
    ),
    Operation(
        "resource.term.pending.drop",
        "**丢弃**一条待审更正（不改书，只从队列里删掉）",
        _term_pending_drop,
        [Param("id", "str", "提案 id", positional=True, required=True)],
        group="resource",
    ),
    Operation("resource.validate", "校验资源文件，列出非法行", _resource_validate, group="resource"),
    # —— 摘要侧实体：从场摘要里抽，零模型成本，按新形状落进术语书 ——
    Operation(
        "resource.term.candidates",
        "从场摘要抽实体（一个实体一行，新写法 / 空译名 / 新事实免审追加，"
        "改已有的译名进待审队列；幂等，重复调用不重复产行）",
        _resource_term_candidates,
        [
            Param("min_scenes", "int", "跨至少这么多场才算候选", default=2),
            Param("language", "str", "目标语言（可选；抽取只看原文，这一项只记在返回里）"),
        ],
        group="resource",
        render=_render_term_candidates,
    ),
    # —— 已有译文：译者的成品是**资产**，读进来沉淀，但不改它 ——
    Operation(
        "resource.harvest",
        "把游戏里已有的译文读进来，沉淀成翻译记忆（只读游戏；观察出来的对应不进术语书）",
        _harvest_existing,
        [
            Param("language", "str", "读哪个语言目录（留空用项目配置的目标语言）"),
            Param(
                "min_occurrences",
                "int",
                "同一原文至少出现几次才算「重复且一致」——**只报数**，不建候选",
                default=2,
            ),
            Param(
                "max_candidate_length",
                "int",
                "超过这个长度的原文不参与「重复且一致」的统计",
                default=24,
            ),
        ],
        group="resource",
    ),
    # —— 声明的偏离：给**已有译文**的"我知道它会偏离规则，但它是对的" ——
    Operation(
        "resource.deviation.list",
        "列出声明的偏离（有意留空 / 经批准的表达式改写）",
        _deviation_list,
        group="resource",
        mutates=False,
    ),
    Operation(
        "resource.deviation.add",
        "声明一条偏离：这条译文有意偏离判据的哪一条（默认不许，声明才放行）",
        _deviation_add,
        [
            Param("unit_id", "str", "哪条译文（unit_id）", positional=True, required=True),
            Param(
                "kind",
                "str",
                "偏离种类：empty（有意留空）/ expression（经批准的表达式改写）",
                positional=True,
                required=True,
                choices=["empty", "expression"],
            ),
            Param("by", "str", "谁拍的板：human / user / agent", default="agent"),
            Param("reason", "str", "为什么这是对的（人看得懂的一句话）"),
        ],
        group="resource",
    ),
    Operation(
        "resource.deviation.remove",
        "删掉一条偏离声明",
        _deviation_remove,
        [
            Param("unit_id", "str", "哪条译文", positional=True, required=True),
            Param("kind", "str", "只删这个种类；留空则删这条译文的全部声明", default=""),
        ],
        group="resource",
    ),
    # —— 声明的补充条目：引擎不枚举、但玩家看得见的文本 ——
    Operation(
        "resource.supplement.list",
        "列出声明的补充条目（引擎不枚举、但玩家看得见的文本）",
        _supplement_list,
        group="resource",
        mutates=False,
    ),
    Operation(
        "resource.supplement.add",
        "声明一条补充条目：这段文本也要翻（说话人名、代码里的界面提示等）",
        _supplement_add,
        [
            Param("source", "str", "原文（运行时被查找的那段文本）", positional=True, required=True),
            Param("target", "str", "译文", positional=True, required=True),
            Param("by", "str", "谁拍的板：human / user / agent", default="agent"),
            Param("reason", "str", "为什么补它（人看得懂的一句话）"),
            Param("file", "str", "出处源码文件（可选，便于核对）"),
            Param("line", "int", "出处行号（可选）", default=0),
        ],
        group="resource",
    ),
    Operation(
        "resource.supplement.remove",
        "删掉一条补充条目",
        _supplement_remove,
        [Param("source", "str", "原文", positional=True, required=True)],
        group="resource",
    ),
    Operation(
        "resource.style.list",
        "列出风格要求（一等翻译资源）",
        _style_list,
        group="resource",
        mutates=False,
    ),
    Operation(
        "resource.style.add",
        "新增/覆盖一条风格要求（按 aspect + scope 定键）",
        _style_add,
        [
            Param("aspect", "str", "风格维度：tone / dialogue / forbidden / naming …", positional=True, required=True),
            Param("value", "str", "要求本身", positional=True, required=True),
            Param("scope", "str", "作用域：global / character:X / scene:X / unit:X / region:X"),
            Param("note", "str", "备注"),
            Param("priority", "int", "优先级（越大越先注入）", default=50),
        ],
        group="resource",
    ),
    Operation(
        "resource.style.remove",
        "删掉一条风格要求",
        _style_remove,
        [
            Param("aspect", "str", "风格维度", positional=True, required=True),
            Param("scope", "str", "作用域（默认 global）"),
        ],
        group="resource",
    ),
    # —— 交互层 ——
    Operation(
        "ui.views",
        "查看交互层视图（默认只列用户可见的）",
        _ui_views,
        [Param("all", "bool", "连对用户透明的信息一起列出")],
        group="ui",
        render=_render_ui_views,
    ),
    Operation(
        "ui.render",
        "按可见性策略渲染给用户的文本",
        _ui_render,
        group="ui",
        render=lambda data: data.get("rendered", ""),
    ),
    Operation(
        "ui.policy.set",
        "设置某类信息对用户可见还是透明",
        _ui_policy_set,
        [
            Param("topic", "str", "主题（支持 前缀* 通配）", positional=True, required=True),
            Param(
                "visibility",
                "str",
                "可见性",
                positional=True,
                required=True,
                choices=["user", "agent", "debug", "hidden"],
            ),
        ],
        group="ui",
    ),
    Operation(
        "ui.announce",
        "以 agent 身份直接向用户发布一条信息",
        _ui_announce,
        [
            Param("message", "str", "要告诉用户的话", positional=True),
            Param("severity", "str", "级别", choices=["info", "success", "warning", "error"]),
        ],
        group="ui",
    ),
    Operation(
        "ui.override",
        "改写一条交互层视图的内容",
        _ui_override,
        [
            Param("view_id", "str", "视图 id", positional=True),
            Param("title", "str", "新的标题"),
            Param("note", "str", "追加一条 agent 注记"),
        ],
        group="ui",
    ),
    Operation(
        "ui.hide",
        "对用户隐藏（或恢复）一条视图",
        _ui_hide,
        [
            Param("view_id", "str", "视图 id", positional=True),
            Param("unhide", "bool", "改成恢复显示"),
        ],
        group="ui",
    ),
    Operation("ui.clear", "清掉非置顶的视图", _ui_clear, group="ui"),
    # —— 配置 ——
    Operation("config.show", "查看当前配置（含每一项来自哪一层）", _config_show, group="config"),
    Operation(
        "config.path",
        "报出四层配置各自的文件在哪",
        _config_path,
        group="config",
    ),
    Operation(
        "config.set",
        "修改一项配置；默认改项目层，--global 改全局层（一次配好、所有项目通用）",
        _config_set,
        [
            Param("key", "str", "配置项（含 api_key / base_url / model）", positional=True),
            Param("value", "str", "新值", positional=True),
            Param(
                "global_scope",
                "bool",
                "写全局层（~/.gametrans），而不是当前项目",
                flag="--global",
            ),
        ],
        group="config",
    ),
    Operation(
        "config.unset",
        "删掉某一层的某一项，让它回落下一层（不是设成默认值）",
        _config_unset,
        [
            Param("key", "str", "配置项", positional=True),
            Param(
                "global_scope",
                "bool",
                "从全局层删，而不是从当前项目删",
                flag="--global",
            ),
        ],
        group="config",
    ),
    Operation("config.providers", "列出可用的翻译 provider", _config_providers, group="config"),
]

_BY_NAME = {op.name: op for op in OPERATIONS}
_BY_TOOL = {op.tool_name: op for op in OPERATIONS}

#: 只读操作：不碰项目/工作区状态。集中列在这里而不是散在 28 个构造调用里，
#: 是为了让"面板能放行什么"一眼可查 —— 默认必须显式登记才只读。
READ_ONLY_OPERATIONS: frozenset[str] = frozenset(
    {
        "project.status",
        "engine.list",
        "engine.info",
        "engine.detect",
        "engine.options",
        "graph.show",
        "graph.node",
        "graph.dependencies",
        "ir",
        "tasks",
        "plan",
        "staleness",
        "resource.term.list",
        "resource.term.pending.list",
        "resource.style.list",
        "resource.validate",
        "resource.supplement.list",
        "resource.deviation.list",
        "skeleton.status",
        "language.facts",
        "ui.views",
        "ui.render",
        "config.show",
        "config.providers",
        "agent.next",
        "agent.status",
    }
)

for _op in OPERATIONS:
    if _op.name in READ_ONLY_OPERATIONS:
        _op.mutates = False

_unknown_readonly = READ_ONLY_OPERATIONS - set(_BY_NAME)
if _unknown_readonly:  # pragma: no cover - 改名时会立刻炸出来
    raise RuntimeError(f"只读清单里有不存在的操作：{sorted(_unknown_readonly)}")


def all_operations() -> list[Operation]:
    return list(OPERATIONS)


def get_operation(name: str) -> Operation:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise GameTransError(
            f"未知操作：{name!r}",
            hint=f"可用操作：{', '.join(sorted(_BY_NAME))}",
        ) from None


def get_operation_by_tool(tool_name: str) -> Operation | None:
    return _BY_TOOL.get(tool_name)


def operation_tree() -> dict[str, Any]:
    """把点号名字折成命令树，供 argparse 生成嵌套子命令。"""
    tree: dict[str, Any] = {}
    for op in OPERATIONS:
        parts = op.name.split(".")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = op
    return tree


def tool_schema(op: Operation) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "project": {
            "type": "string",
            "description": "游戏项目根目录（默认当前目录）",
        },
        "workdir": {
            "type": "string",
            "description": "工作区目录，默认 <项目>/.gametrans",
        },
    }
    required: list[str] = []
    for param in op.params:
        properties[param.name] = param.json_schema()
        if param.required and not param.positional:
            required.append(param.name)
        elif param.positional and param.required:
            required.append(param.name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def tool_descriptor(op: Operation) -> dict[str, Any]:
    return {
        "name": op.tool_name,
        "description": op.summary,
        "inputSchema": tool_schema(op),
        # 让 agent 一眼看出哪些工具会改动项目状态
        "annotations": {"readOnlyHint": not op.mutates, "destructiveHint": op.mutates},
    }


def arguments_for(op: Operation, raw: dict[str, Any]) -> dict[str, Any]:
    """从一次调用给的原始参数里取出这个操作关心的字段，并做 csv 拆分。"""
    args: dict[str, Any] = {}
    for param in op.params:
        value = raw.get(param.name, param.default)
        if param.type == "csv":
            if isinstance(value, str):
                value = [piece.strip() for piece in value.split(",") if piece.strip()]
            elif value is None:
                value = []
        args[param.name] = value
    return args


def result_envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **data}


def error_envelope(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, GameTransError):
        return {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": exc.message,
                "hint": exc.hint,
            },
        }
    return {
        "ok": False,
        "error": {
            "type": type(exc).__name__,
            "message": str(exc),
            "hint": None,
        },
    }


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


__all__ = [
    "Context",
    "Operation",
    "Param",
    "all_operations",
    "arguments_for",
    "error_envelope",
    "get_operation",
    "get_operation_by_tool",
    "json_dumps",
    "operation_tree",
    "result_envelope",
    "tool_descriptor",
    "tool_name_for",
    "tool_schema",
    "TranslationStatus",
]
