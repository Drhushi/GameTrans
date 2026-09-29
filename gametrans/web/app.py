"""面板的路由与 API。

路由与传输层分开：:meth:`WebApp.route` 是纯函数式的（method + path + query → Response），
所以路由逻辑可以脱离 socket 测试，:mod:`gametrans.web.server` 只负责把它接到 HTTP 上。

**可写面**（其余仍然只读）：

* ``/api/project`` —— POST 切换到另一个游戏目录（未初始化就顺手初始化；浏览器与桌面壳共用）；
* ``/api/engine-option`` —— 引擎私有选项（官方 SDK 路径等）；
* ``/api/config`` —— 项目配置与模型凭证；
* ``/api/operations/<name>`` —— :data:`PANEL_WRITE_OPERATIONS` 里那几个内容与审核类写操作。

执行类写操作（提取 / 翻译 / 写回 / 封包 / 初始化）不出现在面板里：它们是长任务，
要进度、要中断、要回滚，那些只能在 CLI 或 MCP 里跑。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gametrans import __version__, prompts
from gametrans.config import GLOBAL_FIELDS, VALID_MODES
from gametrans.core import exchanges
from gametrans.core.ids import id_forms
from gametrans.core.models import TranslationArtifact, TranslationStatus
from gametrans.core.report import REPORT_DIR_NAME, load_report, report_rows
from gametrans.core.session import DEFAULT_WORKDIR_NAME, ProjectSession
from gametrans.errors import GameTransError, ProjectError
from gametrans.core.summaries import load_summaries
from gametrans.core.constraints import term_tags_in
from gametrans.layers.tags import pending_writings
from gametrans.userconfig import panel_state_file
from gametrans.operations import (
    NODE_STATUSES,
    Context,
    Operation,
    all_operations,
    arguments_for,
    error_envelope,
    get_operation_by_tool,
    graph_edge_payload,
    graph_node_payload,
    _summary_for,
    missing_arguments,
    node_has_pending_tags,
    node_translation_status,
    result_envelope,
    term_tag_index,
    tool_schema,
    translation_status_index,
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

DEFAULT_GRAPH_LIMIT = 30
DEFAULT_TRANSLATION_SAMPLE = 20
DEFAULT_REPORT_LIMIT = 50
DEFAULT_EXCHANGE_LIMIT = 50

#: 面板据此显隐功能，**不靠版本号猜**。没接的能力一律报 ``false``：
#: 报成 true 而功能不存在，面板会亮出一个点下去就报错的按钮，比不显示更糟。
#: 括号里是需求单里的编号，接一条翻一条。
PANEL_CAPABILITIES: dict[str, bool] = {
    "events": False,  # R1 实时事件流：未接
    "graph_edges": True,  # R4 依赖边数据
    "unit_detail": False,  # R2 单元 / 任务详情：未接
    "translation_edit": True,  # R3 译文编辑保存
    "reports": True,  # R5 运行报告读取
    "staleness": False,  # R6 过期译文检测：未接
}


def _frozen_launch() -> bool:
    """是否跑在 PyInstaller 冻结版里（agent 接入卡的配置形状随它变）。"""
    return bool(getattr(sys, "frozen", False))


def _source_root() -> str:
    """源码运行时的检出根（`python -m gametrans mcp` 的 cwd）。"""
    return str(Path(__file__).resolve().parent.parent.parent)

#: 会改变"项目看起来是什么样"的工作区文件。面板每请求核对一次它们的时间戳/大小，
#: 有变化就丢掉缓存会话 —— 否则面板会一直显示打开那一刻的旧状态。
WATCHED_FILES = (
    "project.json",
    "credentials.json",
    "graph.json",
    "translations.jsonl",
    "resources/termbook.jsonl",
    # 待审更正与变更日志：面板要看得见"改了什么、还排着几条"
    "resources/termbook.pending.jsonl",
    "resources/termbook.changes.jsonl",
    # 旧文件也算进来：正本还没写盘时，面板读的就是它们（见 TermBook._from_legacy）。
    "resources/glossary.jsonl",
    "resources/worldbook.jsonl",
    "resources/worldbook.md",
    "resources/style.jsonl",
    "interaction/state.json",
    # 这两个决定**面板看起来是什么样**，和 graph.json 同一条道理：
    # * `chapters.json`：读盘时会重新盖章戳（`session._load_graph`），章带、分行全靠它；
    # * `summaries.json`：节点的标题与事件卡。
    # 少盯一个，用户手改完这两个文件后面板会一直显示旧样子，而现象与"没保存"分不开。
    "chapters.json",
    "summaries.json",
)

#: 面板允许直接调用的**写**操作。刻意收窄到"内容与审核"这一类：
#: 它们单次、局部、可逆（再删一次就回去了）。执行类（提取 / 翻译 / 写回 / 封包 /
#: 初始化）是长任务，需要进度、中断与回滚，那些只能在 CLI 或 MCP 里跑。
PANEL_WRITE_OPERATIONS: frozenset[str] = frozenset(
    {
        "resource.term.add",
        "resource.term.remove",
        "resource.term.propose",
        "resource.style.add",
        "resource.style.remove",
        "resource.term.pending.adopt",
        "resource.term.pending.drop",
        # 摘要侧实体：往术语书里追加新行（零模型成本、幂等），
        # 与"改内容资产"是同一类动作（单次、局部、可逆）。
        "resource.term.candidates",
    }
)


@dataclass
class Response:
    """一条 HTTP 响应。"""

    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "Response":
        return cls(
            status=status,
            body=json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
        )

    def json_body(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _config_sources(session) -> dict:
    """配置字段逐项来自哪一层。四层之后没有它，"我改了怎么没生效"没法查。"""
    from gametrans.userconfig import resolve_config_sources

    return resolve_config_sources(session._project_values, session.global_config)


def _flag(query: dict[str, list[str]], name: str) -> bool:
    raw = (query.get(name) or ["0"])[0]
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _int_arg(query: dict[str, list[str]], name: str, default: int, ceiling: int) -> int:
    try:
        value = int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default
    return max(0, min(value, ceiling))


def _json_object(body: bytes) -> tuple[dict[str, Any], "Response | None"]:
    """解析写口的请求体，返回 ``(payload, 错误响应)`` —— 两者恰有一个有意义。

    坏请求体一律 400 且**不落盘**：写口读不懂的东西，绝不猜着执行。
    """
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, Response.json(
            {
                "ok": False,
                "error": {
                    "type": "BadRequest",
                    "message": "请求体不是合法 JSON",
                    "hint": None,
                },
            },
            status=400,
        )
    if not isinstance(payload, dict):
        return {}, Response.json(
            {
                "ok": False,
                "error": {
                    "type": "BadRequest",
                    "message": "请求体必须是一个 JSON 对象",
                    "hint": None,
                },
            },
            status=400,
        )
    return payload, None


#: 「最近打开」最多记几个项目：列表足够扫一眼，也不会无限膨胀。
RECENT_PROJECT_CAP = 8


def merge_recent(previous: Any, path: str, *, cap: int = RECENT_PROJECT_CAP) -> dict[str, Any]:
    """把这次打开的项目挪到「最近打开」最前面（去重、挤掉超出的旧条目）。

    记忆文件是 launcher（open_panel）与面板切换共用的 ``.gametrans/panel.json``；
    ``previous`` 是文件里的旧内容（可能是任意垃圾），坏掉就当没记过。
    """
    old = previous if isinstance(previous, dict) else {}
    recent: list[str] = []
    for item in old.get("recent") or []:
        item = str(item)
        if item != path and item not in recent:
            recent.append(item)
    recent.insert(0, path)
    # 认识的键之外原样保留（桌面壳在同一个文件里记主题偏好，不能被冲掉）
    out = dict(old)
    out["project"] = path
    out["recent"] = recent[:cap]
    return out


# ---- 检查更新 ---------------------------------------------------------------

#: 「检查更新」问谁：GitHub Releases 的 latest。面板只绑 127.0.0.1，这是它
#: 唯一一次主动出网 —— 用户点了按钮才发。发布页 https://github.com/Drhushi/GameTrans/releases
RELEASES_LATEST_URL = "https://api.github.com/repos/Drhushi/GameTrans/releases/latest"
UPDATE_CHECK_TIMEOUT = 8.0


def fetch_latest_release(url: str = RELEASES_LATEST_URL) -> tuple[str, str]:
    """问 GitHub 要最新发布的 ``(tag, 页面链接)``；网络 / 限流的异常由调用方转成人话。"""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "gametrans-panel"},
    )
    with urllib.request.urlopen(request, timeout=UPDATE_CHECK_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise ValueError("响应里没有 tag_name")
    return tag, str(payload.get("html_url") or "")


def parse_version(text: str) -> tuple[int, ...] | None:
    """把 ``v0.1.2`` / ``0.1.2`` 拆成可比较的整数段；没有数字就 ``None``（当未知处理）。"""
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts) if parts else None


def update_available(current: str, latest: str) -> bool:
    """latest 是否比 current 新 —— 两边都能拆成数字才比，否则宁可说没有。"""
    cur, new = parse_version(current), parse_version(latest)
    return bool(cur and new and new > cur)


class WebApp:
    """把项目会话渲染成 HTTP。"""

    def __init__(self, project_root: Path, *, workdir: Path | None = None) -> None:
        # 归一成绝对路径：面板会被以相对路径拉起（`gametrans web .`），而提取层拿
        # `project_root.name` 当图上的根容器名 —— 留着 `.` 会让根容器名为空，
        # 文件节点 id 与骨架里的 `# game/...` 对不上，scan 直接崩。
        self.project_root = Path(project_root).expanduser().resolve()
        self.workdir = Path(workdir).expanduser().resolve() if workdir else None
        self._explicit_workdir = workdir is not None
        # 切换项目后记住「上一次打开的」的落点（open_panel 读它）；测试里换成临时路径。
        # 解析收口在 userconfig.panel_state_file —— 三方共用，冻结打包后也不会指进只读目录
        self.launcher_marker = panel_state_file()
        self.static_root = Path(__file__).resolve().parent / "static"
        self._session: ProjectSession | None = None
        self._fingerprint: tuple[Any, ...] | None = None
        # ThreadingHTTPServer 会并发处理请求，会话的懒加载要加锁
        self._lock = threading.Lock()

    # ---- 会话 ---------------------------------------------------------------

    def workspace_root(self) -> Path:
        return self.workdir if self.workdir else self.project_root / DEFAULT_WORKDIR_NAME

    def _fingerprint_now(self) -> tuple[Any, ...]:
        base = self.workspace_root()
        parts: list[Any] = []
        for name in WATCHED_FILES:
            path = base / name
            try:
                stat = path.stat()
            except OSError:
                parts.append((name, None, None))
            else:
                parts.append((name, stat.st_mtime_ns, stat.st_size))
        return tuple(parts)

    def session(self) -> ProjectSession:
        """拿到当前会话；工作区变了就换一个新的。

        面板是长驻进程，而工作区会被**别的进程**改（CLI、agent、用户手改文件）——
        缓存一个会话到底会让面板一直停在打开那一刻的状态。
        """
        with self._lock:
            fingerprint = self._fingerprint_now()
            if self._session is None or fingerprint != self._fingerprint:
                self._session = ProjectSession.open(
                    self.project_root, workdir=self.workdir
                )
                # 打开之后再取一次：open() 可能补齐缺失的工作区文件
                self._fingerprint = self._fingerprint_now()
            return self._session

    def rebind(self, project_root: Path) -> None:
        """把面板换绑到另一个游戏目录（设置页「切换项目」用）。

        换根、弃缓存会话即可：session() 本来就会按指纹重建，其余路由全部
        从 ``self.project_root`` 出发，没有第二个要改的状态。
        """
        with self._lock:
            self.project_root = Path(project_root).expanduser().resolve()
            self._session = None
            self._fingerprint = None

    def _remember_launcher_project(self, game: Path) -> None:
        """切完项目记住它并挪到「最近打开」最前面（open_panel 与设置页共用）。

        写不进去不影响切换本身。
        """
        try:
            previous: Any = {}
            try:
                previous = json.loads(self.launcher_marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            self.launcher_marker.parent.mkdir(parents=True, exist_ok=True)
            self.launcher_marker.write_text(
                json.dumps(merge_recent(previous, str(game)), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _api_projects(self) -> Response:
        """最近打开过的项目：设置页「项目」卡一键切换的数据源。

        记忆文件里已经不存在的目录不再给出；当前项目哪怕没记上（写文件失败过）
        也补在最前面，保证"现在开着的"永远一键可见。
        """
        raw: list[str] = []
        try:
            data = json.loads(self.launcher_marker.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = [str(item) for item in (data.get("recent") or [])]
        except (OSError, ValueError):
            pass
        if str(self.project_root) not in raw:
            raw.insert(0, str(self.project_root))
        projects: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            path = Path(item)
            if not path.is_dir() or str(path) in seen:
                continue
            seen.add(str(path))
            projects.append({"path": str(path), "name": path.name})
        return Response.json({"ok": True, "projects": projects})

    def _api_project(self, method: str, body: bytes) -> Response:
        """GET 报当前项目；POST ``{"path": ...}`` 切到另一个游戏目录。

        目标没工作区就按面板的默认口径初始化（引擎探测失败会抛错 → 400）；
        显式指定过 ``--workdir`` 的服务不许切 —— 工作区跟着项目走，换绑会指向
        旧项目的工作区，这种组合只能重新起服务。
        """
        if method == "GET":
            return Response.json({"ok": True, "project_root": str(self.project_root)})
        if method != "POST":
            return Response.json(
                {"ok": False, "error": {"message": "这个接口只接受 GET 或 POST"}}, status=405
            )
        if self._explicit_workdir:
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": "WorkdirPinned",
                        "message": "这个面板是用显式 --workdir 起的，不支持就地切换项目",
                        "hint": "换项目请重新起一个面板：gametrans web -p <新项目>",
                    },
                },
                status=400,
            )
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response.json(
                {"ok": False, "error": {"message": "请求体不是合法 JSON"}}, status=400
            )
        raw = str((payload or {}).get("path") or "").strip()
        if not raw:
            return Response.json(
                {"ok": False, "error": {"message": "要给一个游戏目录：{\"path\": …}"}},
                status=400,
            )
        game = Path(raw).expanduser().resolve()
        if not game.is_dir():
            raise GameTransError(f"不是目录：{game}")
        if not (game / DEFAULT_WORKDIR_NAME / "project.json").is_file():
            ProjectSession.init(game, target_language="zh_CN")
        self.rebind(game)
        self._remember_launcher_project(game)
        return Response.json({"ok": True, "project_root": str(game)})

    # ---- 路由 ---------------------------------------------------------------

    def route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]] | None = None,
        body: bytes = b"",
    ) -> Response:
        query = query or {}
        try:
            return self._route(method, path, query, body)
        except ProjectError as exc:
            # 项目还没初始化是"状态不对"，不是"服务器出错"
            return Response.json(error_envelope(exc), status=409)
        except GameTransError as exc:
            return Response.json(error_envelope(exc), status=400)
        except Exception as exc:  # noqa: BLE001 - 面板不该把堆栈甩给浏览器
            return Response.json(error_envelope(exc), status=500)

    def _route(
        self, method: str, path: str, query: dict[str, list[str]], body: bytes = b""
    ) -> Response:
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])

        if path == "/api/ping":
            return Response.json(
                {
                    "ok": True,
                    "tool": "gametrans",
                    "version": __version__,
                    "capabilities": dict(PANEL_CAPABILITIES),
                    # 面板据此渲染「AI agent 接入」卡里的 MCP 配置形状：
                    # 冻结版给 executable（GameTrans.exe 全路径 + ["mcp"]），
                    # 源码版给 source_root（检出根，python -m gametrans mcp 的 cwd）。
                    "frozen": _frozen_launch(),
                    **(
                        {"executable": sys.executable}
                        if _frozen_launch()
                        else {"source_root": _source_root()}
                    ),
                }
            )
        if path == "/api/status":
            return self._api_status()
        if path == "/api/project":
            return self._api_project(method, body)
        if path == "/api/projects":
            return self._api_projects()
        if path == "/api/views":
            return self._api_views(query)
        if path == "/api/policy":
            return self._api_policy()
        if path == "/api/graph":
            return self._api_graph(query)
        if path == "/api/translations":
            return self._api_translations(query)
        if path.startswith("/api/translations/"):
            return self._api_translation(method, path[len("/api/translations/") :], body)
        if path == "/api/reports":
            return self._api_reports(query)
        if path.startswith("/api/reports/"):
            return self._api_report(path[len("/api/reports/") :])
        if path == "/api/exchanges":
            return self._api_exchanges(query)
        if path.startswith("/api/exchanges/"):
            return self._api_exchange(path[len("/api/exchanges/") :])
        if path.startswith("/api/units/"):
            return self._api_unit(path[len("/api/units/") :])
        if path == "/api/prompt-templates":
            return self._api_prompt_templates(method, body)
        if path == "/api/prompt-preview":
            return self._api_prompt_preview(method, body)
        if path == "/api/termbook":
            return self._api_termbook()
        if path == "/api/style":
            return self._api_style()
        if path == "/api/termbook-pending":
            return self._api_termbook_pending()
        if path == "/api/engine-option":
            return self._api_engine_option(method, body)
        if path == "/api/update-check":
            return self._api_update_check()
        if path == "/api/config":
            return self._api_config(method, body)
        if path.startswith("/api/operations/"):
            return self._api_operation(method, path, body)
        if path == "/api/operations":
            return self._api_operations()

        if path.startswith("/api/"):
            return self._not_found(f"没有这个接口：{path}")
        return self._not_found(f"没有这个页面：{path}")

    # ---- API ----------------------------------------------------------------

    def _api_status(self) -> Response:
        result = result_envelope(self.session().status())
        # 顶栏的进度按**单元**算（与路径图的颜色同一个判定函数）：translations.jsonl
        # 里的记录是**槽位**级的，一个单元多条槽位会把记录数翻好几倍，拿记录数当
        # 分子会算出 1893% 这种读数。
        session = self.session()
        graph = session.graph
        if graph is not None:
            statuses = translation_status_index(session.translations())
            by_status = dict.fromkeys(NODE_STATUSES, 0)
            for node in graph.nodes.values():
                name = node_translation_status(node, statuses)
                by_status[name] = by_status.get(name, 0) + 1
            result["graph"]["by_status"] = by_status
        return Response.json(result)

    def _api_views(self, query: dict[str, list[str]]) -> Response:
        session = self.session()
        everything = _flag(query, "all")
        policy = session.interaction.policy()
        views = (
            session.interaction.all_views()
            if everything
            else session.interaction.visible_views()
        )
        return Response.json(
            {
                "ok": True,
                "scope": "all" if everything else "user",
                "views": [v.to_dict(policy.resolve(v.topic)) for v in views],
            }
        )

    def _api_policy(self) -> Response:
        return Response.json({"ok": True, "policy": self.session().interaction.policy().to_dict()})

    def _api_graph(self, query: dict[str, list[str]]) -> Response:
        session = self.session()
        graph = session.graph
        # 场摘要（标题 / 事件卡）只读一次盘：它是声明式产物，不在图里
        summaries = load_summaries(session.workdir)
        limit = _int_arg(query, "limit", DEFAULT_GRAPH_LIMIT, 500)
        offset = _int_arg(query, "offset", 0, 100_000)
        kind = (query.get("kind") or [""])[0].strip()
        wanted_status = (query.get("status") or [""])[0].strip()
        needle = (query.get("q") or [""])[0].strip().lower()
        if graph is None:
            return Response.json(
                {
                    "ok": True,
                    "present": False,
                    "total": 0,
                    "translatable": 0,
                    "matched": 0,
                    "offset": offset,
                    "limit": limit,
                    "by_status": {},
                    "nodes": [],
                    "edges": [],
                }
            )

        stats = graph.stats()
        statuses = translation_status_index(session.translations())
        tagged = term_tag_index(session.translations())
        by_status = dict.fromkeys(NODE_STATUSES, 0)
        for node in graph.nodes.values():
            name = node_translation_status(node, statuses)
            by_status[name] = by_status.get(name, 0) + 1

        nodes = graph.translatable_nodes()
        if kind:
            nodes = [node for node in nodes if node.kind.value == kind]
        if wanted_status:
            nodes = [
                node
                for node in nodes
                if node_translation_status(node, statuses) == wanted_status
            ]
        if needle:
            nodes = [
                node
                for node in nodes
                if needle
                in f"{node.path} {node.kind.value} {node.unit.source if node.unit else ''}".lower()
            ]
        matched = len(nodes)
        page = nodes[offset : offset + limit] if limit else nodes[offset:]

        # 用户看的是前驱关系：只给**知识边**（最早引入场 → 使用场）。
        # 控制边参与计算，但不是用户关心的前驱；它跟"方向未确认"的边一样
        # 属于底账，只在 agent 视图（all=1）给。
        edges = (
            graph.dependencies
            if _flag(query, "all")
            else [e for e in graph.dependencies if e.direction == "agent"]
        )

        return Response.json(
            {
                "ok": True,
                "present": True,
                "total": stats["nodes"],
                "translatable": stats["translatable"],
                "by_kind": stats["by_kind"],
                "char_count": stats["char_count"],
                # 过滤 / 分页后的口径：面板要能说"符合条件的共 N 个、画布显示其中 M 个"
                "matched": matched,
                "offset": offset,
                "limit": limit,
                "by_status": by_status,
                "nodes": [
                    {
                        **graph_node_payload(
                            node,
                            region=graph.region_of(node.node_id),
                            summary=_summary_for(node, summaries),
                            # 列表不带整段原文：一个单元 300+ 句，几十份就是十几 MB，
                            # 浏览器拉不动（真靶上页面会一直停在"加载中"）
                            full=False,
                            pending_tags=node_has_pending_tags(node, tagged),
                        ),
                        "status": node_translation_status(node, statuses),
                    }
                    for node in page
                ],
                # 有生效边才画得出 DAG；没有边时前端退回节点自带的 parent / children 铺树
                "edges": [graph_edge_payload(edge) for edge in edges],
            }
        )

    def _api_reports(self, query: dict[str, list[str]]) -> Response:
        """账本页的历史：每次运行的钱账与质量账。

        报告是**跑过就落盘**的（``reports/<run-id>-<命令>.json``），所以面板能看到
        不是它自己发起的运行 —— CLI 与 MCP 跑完的账同样在这里。
        """
        command = (query.get("command") or [""])[0].strip()
        limit = _int_arg(query, "limit", DEFAULT_REPORT_LIMIT, 500)
        offset = _int_arg(query, "offset", 0, 100_000)
        rows, warnings = report_rows(
            self.workspace_root() / REPORT_DIR_NAME, command=command
        )
        return Response.json(
            {
                "ok": True,
                "total": len(rows),
                "offset": offset,
                "limit": limit,
                "command": command,
                "reports": rows[offset : offset + limit] if limit else rows[offset:],
                # 读不出来的那几份如实报出来；不因为一个坏字节抹掉整页历史
                "warnings": warnings,
            }
        )

    def _api_report(self, run_id: str) -> Response:
        """一次运行的**全量**报告：阶段明细、逐条问题、LLM 计量。"""
        report = load_report(self.workspace_root() / REPORT_DIR_NAME, run_id.strip("/"))
        if report is None:
            return self._not_found(f"没有这份运行报告：{run_id}")
        return Response.json({"ok": True, "report": report.to_dict()})

    # ---- 请求台账：拦截下来的请求 + 真发出去的调用 ---------------------------

    def _api_exchanges(self, query: dict[str, list[str]]) -> Response:
        """**一张表**看所有请求：真发出去过的（带回复）与只拦截过没发的。

        列表只给摘要（正文换成字符数）：面板每轮都轮询它，而一次真跑就是上百万字符。
        正文在 ``/api/exchanges/<id>``。
        """
        limit = _int_arg(query, "limit", DEFAULT_EXCHANGE_LIMIT, 500)
        offset = _int_arg(query, "offset", 0, 100_000)
        run = (query.get("run") or [""])[0].strip()
        rows, warnings = exchanges.merged(self.workspace_root(), run=run)
        window = rows[offset : offset + limit] if limit else rows[offset:]
        return Response.json(
            {
                "ok": True,
                "total": len(rows),
                "offset": offset,
                "limit": limit,
                "run": run,
                "exchanges": [exchanges.light(row) for row in window],
                "summary": exchanges.summary(rows),
                "warnings": warnings,
            }
        )

    def _api_exchange(self, ident: str) -> Response:
        """一条请求的**正文**：system / user 提示词、服务端原话、逐条译文、哪几条没拿到。"""
        row = exchanges.find(self.workspace_root(), ident.strip("/"))
        if row is None:
            return self._not_found(f"没有这条请求：{ident}")
        return Response.json({"ok": True, "exchange": row})

    def _api_unit(self, node_id: str) -> Response:
        """一个**单元**的现场：它每一句的原文/译文/状态 + 碰过它的那些请求。

        以前点节点只给"撞上的那一条槽位记录"（同一结点里其余几句看不见，所以看起来
        还是"一句一节点"的旧样子）。判定与拼装都在这里做：单元有哪些槽位、每条槽位现在
        是什么状态，只有后端同时拿得到图与记录。
        """
        session = self.session()
        graph = session.graph
        if graph is None:
            return Response.json(
                {"ok": False, "error": {"type": "ProjectError",
                                        "message": "这个工作区还没有 graph.json，先跑一次 scan"}},
                status=409,
            )
        node = graph.nodes.get(node_id.strip("/"))
        if node is None or node.unit is None:
            return self._not_found(f"没有这个单元结点：{node_id}")
        unit = node.unit
        payload = getattr(getattr(unit, "locator", None), "payload", None) or {}
        entries = payload.get("slots") or []
        keys = [str(key) for key in (unit.metadata.get("slot_keys") or [])]
        if not keys:
            keys = [str(entry.get("slot_key") or "") for entry in entries if entry.get("slot_key")]
        if not keys:
            keys = [unit.id]

        records = {str(record.unit_id): record for record in session.translations()}
        slots: list[dict[str, Any]] = []
        for order, key in enumerate(keys, start=1):
            entry = next(
                (item for item in entries if str(item.get("slot_key") or "") == key), {}
            )
            record = records.get(key)
            slots.append(
                {
                    "slot_key": key,
                    "order": order,
                    "source": str(entry.get("source") or (record.source if record else "")),
                    # 说话人只取自 locator —— 译文记录里没有这个字段（它不是译文的属性）
                    "speaker": entry.get("speaker"),
                    "position": f"{order}/{len(keys)}",
                    "protected": list(entry.get("protected") or []),
                    "line": entry.get("line"),
                    "artifact": (
                        {**record.to_dict(), "target": record.target} if record else None
                    ),
                }
            )

        unit_forms: set[str] = set()
        for key in keys:
            unit_forms |= id_forms(key)
        calls = []
        for row in exchanges.merged(self.workspace_root())[0]:
            asked: set[str] = set()
            for unit_id in row.get("unit_ids") or []:
                asked |= id_forms(str(unit_id))
            if asked & unit_forms:
                calls.append(exchanges.light(row))

        return Response.json(
            {
                "ok": True,
                "unit": {
                    "node_id": node.node_id,
                    "unit_id": unit.id,
                    "label": unit.metadata.get("label") or node.path,
                    "path": node.path,
                    "kind": node.kind.value,
                    "region": graph.region_of(node.node_id),
                    "speaker": unit.context.speaker if unit.context else None,
                    "slots": len(keys),
                },
                "slots": slots,
                "calls": calls,
            }
        )

    # ---- 请求模板：看 / 改 / 存 / 切（人与 agent 同一个入口） ---------------

    def _api_prompt_templates(self, method: str, body: bytes) -> Response:
        """读回全部模板与当前生效的那个；``POST`` 切换 / 另存 / 删除 / 重置。

        人与 agent 走的是**同一组配置键**（``prompt_template`` / ``prompt_templates``），
        面板只是它们的表单 —— 不存在"面板改一份、CLI 改另一份"。
        """
        session = self.session()
        if method == "GET":
            return Response.json(self._templates_payload(session))

        payload, bad = _json_object(body)
        if bad is not None:
            return bad
        action = str(payload.get("action") or "").strip()
        name = str(payload.get("name") or "").strip()
        if action not in ("activate", "save", "delete", "reset"):
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": "BadRequest",
                        "message": f"不认识的 action：{action!r}",
                        "hint": "可用：activate（切到某个）/ save（另存或覆盖）/ "
                                "delete（删掉自定义的）/ reset（恢复出厂）",
                    },
                },
                status=400,
            )
        if action in ("activate", "save", "delete") and not name:
            return Response.json(
                {"ok": False, "error": {"type": "BadRequest", "message": "缺少 name"}},
                status=400,
            )

        saved = {str(key): dict(value) for key, value in session.config.prompt_templates.items()}
        settings: dict[str, Any] = {}
        if action == "activate":
            if name not in prompts.resolve(saved):
                return self._not_found(f"没有这个模板：{name}")
            settings["prompt_template"] = name
        elif action == "save":
            template = payload.get("template")
            problems = prompts.validate(template, where=f"模板 {name}")
            if problems:
                return Response.json(
                    {
                        "ok": False,
                        "error": {
                            "type": "ConfigError",
                            "message": "模板没通过校验，没有保存：" + "；".join(problems[:4]),
                            "hint": "占位符只能是 "
                                    + "、".join("{" + key + "}" for key in prompts.SAMPLE),
                        },
                    },
                    status=400,
                )
            saved[name] = dict(template)
            settings["prompt_templates"] = saved
            settings["prompt_template"] = name
        elif action == "delete":
            if name not in saved:
                return self._not_found(f"这个模板不是自定义的，删不了：{name}")
            saved.pop(name)
            settings["prompt_templates"] = saved
            if session.config.prompt_template == name:
                settings["prompt_template"] = prompts.DEFAULT_TEMPLATE_NAME
        else:  # reset
            settings["prompt_templates"] = {}
            settings["prompt_template"] = prompts.DEFAULT_TEMPLATE_NAME

        result = session.apply_settings(settings)
        return Response.json({"ok": True, "action": action, **self._templates_payload(session),
                              "applied": result.get("sources") is not None})

    def _templates_payload(self, session) -> dict[str, Any]:
        saved = session.config.prompt_templates or {}
        return {
            "ok": True,
            "active": session.config.prompt_template,
            "templates": prompts.resolve(saved),
            "builtin": list(prompts.BUILTIN_TEMPLATES),
            "saved": sorted(str(name) for name in saved),
            "default": prompts.DEFAULT_TEMPLATE_NAME,
            # 表单照它渲染：后端说了算，前端不维护第二份字段表
            "fields": [dict(field) for field in prompts.FIELDS],
        }

    def _api_prompt_preview(self, method: str, body: bytes) -> Response:
        """**用生产那条路**把请求装配出来给人看（离线，不发网络、不花钱）。

        这是"阴阳代码"的解药：预览调的是 :meth:`TranslateLayer.build_request` 与
        :meth:`TranslationRequest.system_message` / ``render``，与真跑逐字同源。
        """
        if method != "POST":
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": "MethodNotAllowed",
                        "message": "预览要用 POST（它带参数：用哪个模板、看几个单元）",
                    },
                },
                status=405,
            )
        payload, bad = _json_object(body)
        if bad is not None:
            return bad
        session = self.session()
        graph = session.graph
        if graph is None:
            return Response.json(
                {"ok": False, "error": {"type": "ProjectError",
                                        "message": "这个工作区还没有 graph.json，先跑一次 scan"}},
                status=409,
            )
        units = _int_arg({"units": [str(payload.get("units") or 1)]}, "units", 1, 20)
        name = str(payload.get("name") or "").strip()
        compare_name = str(payload.get("compare") or "").strip()
        want_unit = str(payload.get("unit") or "").strip()

        def pick(which: str) -> tuple[str, dict[str, Any]]:
            label = which or session.config.prompt_template
            return label, prompts.active(label, session.config.prompt_templates)

        label_a, template = pick(name)
        label_b, template_b = pick(compare_name)
        comparing = bool(compare_name) and compare_name != label_a
        nodes = [node for node in graph.translatable_nodes() if node.unit is not None]
        nodes.sort(key=lambda node: -len((node.unit.metadata or {}).get("slot_keys") or []))
        # 单元清单给前端做选择器：默认预览最大的那个，但要能挑到"记忆里有货"的单元 ——
        # 否则"已定译那几行"永远看不到。
        catalogue = [
            {
                "node_id": node.node_id,
                "unit_id": node.unit.id,
                "label": (node.unit.metadata or {}).get("label") or node.path,
                "slots": len((node.unit.metadata or {}).get("slot_keys") or []),
            }
            for node in nodes[:40]
        ]
        if want_unit:
            nodes = [node for node in nodes if node.node_id == want_unit or node.unit.id == want_unit]
            if not nodes:
                return self._not_found(f"这个工作区里没有这个单元：{want_unit}")
        options = session.translate_options(prompt_template=template)
        options_b = (
            session.translate_options(prompt_template=template_b) if comparing else None
        )
        previews = []
        for node in nodes[:units]:
            request = session.translate_layer.build_request(
                [node], session.resources, options, graph,
                project_id=session.project_root.name, use_memory=True,
            )
            body: dict[str, Any] = {
                "unit_id": node.node_id,
                "unit_label": (node.unit.metadata or {}).get("label") or node.path,
                "items": len(request.items),
                "asked": sum(1 for item in request.items if getattr(item, "expects_answer", True)),
                "system": request.system_message(),
                "user": request.render(),
                "short_ids": dict(request.short_ids),
            }
            if options_b is not None:
                other = session.translate_layer.build_request(
                    [node], session.resources, options_b, graph,
                    project_id=session.project_root.name, use_memory=True,
                )
                body["compare"] = {
                    "name": label_b,
                    "items": len(other.items),
                    "asked": sum(
                        1 for item in other.items if getattr(item, "expects_answer", True)
                    ),
                    "system": other.system_message(),
                    "user": other.render(),
                }
            previews.append(body)
        return Response.json(
            {
                "ok": True,
                "template": label_a,
                "template_body": template,
                "compare": label_b if comparing else None,
                "units": catalogue,
                "previews": previews,
            }
        )

    def _api_translation(self, method: str, rest: str, body: bytes) -> Response:
        """单条译文的手改与复核。

        写路径**不裸写文件**：交给 :meth:`ProjectSession.save_translation`，那条路与
        translate / writeback 共用同一个 ``validate`` 闸门。面板要是自己拼 JSONL，
        改出来的译文就会绕过结构校验，到写回时才炸。
        """
        parts = [part for part in rest.split("/") if part]
        if not parts:
            return self._not_found("没有指定要改哪一条译文")
        unit_id = parts[0]
        action = parts[1] if len(parts) > 1 else ""
        if action not in ("", "review"):
            return self._not_found(f"没有这个译文接口：{action}")
        if method != "POST":
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": "MethodNotAllowed",
                        "message": "这个接口只接受 POST",
                        "hint": None,
                    },
                },
                status=405,
            )

        session = self.session()
        if not any(record.unit_id == unit_id for record in session.translations()):
            # 先确认存在再动盘：宁可 404，也不要凭一个 unit_id 凭空造一条译文
            return self._not_found(f"没有这条译文：{unit_id}")

        payload, failure = _json_object(body)
        if failure is not None:
            return failure
        if action == "review":
            return self._review_translation(session, unit_id, payload)
        return self._save_translation(session, unit_id, payload)

    @staticmethod
    def _save_translation(
        session: ProjectSession, unit_id: str, payload: dict[str, Any]
    ) -> Response:
        target = payload.get("target")
        if not isinstance(target, str) or not target.strip():
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": "MissingArgument",
                        "message": "保存译文需要 target，且不能是空白",
                        "hint": '请求体形如 {"target": "译文"}。',
                    },
                },
                status=400,
            )
        try:
            record = session.save_translation(unit_id, target, agent="panel")
        except GameTransError as exc:
            return Response.json(error_envelope(exc), status=400)
        # 校验结论随响应一起回去：面板要就地告诉用户"为什么没通过"，
        # 而不是只说一句保存成功
        return Response.json({"ok": True, "artifact": record.to_dict()})

    @staticmethod
    def _review_translation(
        session: ProjectSession, unit_id: str, payload: dict[str, Any]
    ) -> Response:
        action = str(payload.get("action") or "").strip()
        try:
            record = session.review_translation(unit_id, action)
        except GameTransError as exc:
            return Response.json(error_envelope(exc), status=400)
        return Response.json({"ok": True, "artifact": record.to_dict()})

    def _api_translations(self, query: dict[str, list[str]]) -> Response:
        records = self.session().translations()
        usable = [r for r in records if r.is_usable]
        review = [r for r in records if r.status is TranslationStatus.NEEDS_REVIEW]
        # "没有译文"与"有译文但没通过校验"分开报 —— 用户看到的结论不该把两者混起来
        broken = [
            r
            for r in records
            if not r.is_usable and r.status is not TranslationStatus.NEEDS_REVIEW
        ]
        limit = _int_arg(query, "limit", DEFAULT_TRANSLATION_SAMPLE, 500)
        sample: list[TranslationArtifact] = (broken + usable)[:limit]
        return Response.json(
            {
                "ok": True,
                "present": bool(records),
                "total": len(records),
                "usable": len(usable),
                "needs_review": len(review),
                "failed": len(broken),
                # 译文里还带着 `⟦写法⟧` 的条数 = "名字还没定译"的那些。它们**算翻好了**
                # （文本已产出，缺的只是名字），写回那一刻才渲染 —— 所以要单独报，
                # 不能混进"失败"里，也不能等到写回才发现。
                "with_term_tags": sum(1 for r in records if term_tags_in(r.target)),
                # 面板只拿它数数与列几行：**不带整段 locator**（那是几十 KB 一条，
                # 两百条就是几 MB，浏览器会一直卡在加载中）。看某一条的细节走
                # `/api/units/<id>`。
                "sample": [
                    {
                        "unit_id": r.unit_id,
                        "path": r.path,
                        "status": r.status.value,
                        "target": r.target,
                        # 这一条里还没定译的名字（原文写法），面板据此标出"待定译"
                        "term_tags": term_tags_in(r.target),
                        "provider": r.provenance.provider,
                        "model": r.provenance.model,
                        "error": r.error,
                    }
                    for r in sample
                ],
            }
        )

    def _api_termbook(self) -> Response:
        """术语书：**一行一个实体，五栏**（key / profile / constant / order / position）。

        ``pending`` 是**待审更正**（改已有的译名 / 事实）—— 它住在
        ``termbook.pending.jsonl``，不在这份文件里，所以随这一份一起发给面板。
        ``pending_naming`` 是**一条译名都没有的写法**：那些写法在原文里是 ``⟦写法⟧``，
        等人或 agent 在审核这一步定译（见 `layers/tags.py`）。
        """
        book = self.session().resources.termbook
        pending = pending_writings(book)
        return Response.json(
            {
                "ok": True,
                "entries": [e.to_dict() for e in book.entries()],
                "summary": book.summary(),
                "pending": book.pending.records(),
                "pending_naming": pending,
            }
        )

    def _api_style(self) -> Response:
        """风格指南：面板要能看见"改了什么风格"，写口在内容编辑那一段。"""
        guide = self.session().resources.style
        return Response.json(
            {
                "ok": True,
                "entries": [e.to_dict() for e in guide.entries()],
                "problems": [i.to_dict() for i in guide.validate()],
            }
        )

    def _api_termbook_pending(self) -> Response:
        """待审更正：改已有的译名 / 事实**先排队**，采用之前书里一个字节都不动。"""
        book = self.session().resources.termbook
        return Response.json(
            {"ok": True, "pending": book.pending.records(), "summary": book.summary()}
        )

    def _api_engine_option(self, method: str, body: bytes) -> Response:
        """面板上**唯一**的写口：当前引擎的私有选项（官方 SDK 路径等）。

        刻意收窄：只接受 ``{key, value}`` 两个短字符串，写进 ``engine_options``；
        键值的含义由适配器解释，内核不猜。除此之外面板仍然只读。
        """
        if method != "POST":
            return Response.json(
                {"ok": False, "error": {"message": "这个接口只接受 POST"}}, status=405
            )
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response.json(
                {"ok": False, "error": {"message": "请求体不是合法 JSON"}}, status=400
            )
        if not isinstance(payload, dict):
            return Response.json(
                {"ok": False, "error": {"message": "请求体必须是一个 JSON 对象"}}, status=400
            )
        key = str(payload.get("key") or "").strip()
        value = str(payload.get("value") or "").strip()
        if not key:
            return Response.json({"ok": False, "error": {"message": "缺少 key"}}, status=400)
        try:
            session = self.session()
            if value:
                options = session.update_engine_option(key, value)
            else:
                options = session.clear_engine_option(key)
        except GameTransError as exc:
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": exc.message,
                        "hint": exc.hint,
                    },
                },
                status=400,
            )
        return Response.json(
            {
                "ok": True,
                "engine": session.engine,
                "options": options,
                "toolchain": session.toolchain_status(),
            }
        )

    def _api_operations(self) -> Response:
        return Response.json(
            {"ok": True, "operations": [_describe(op) for op in all_operations()]}
        )

    def _api_update_check(self) -> Response:
        """「检查更新」（设置页按钮）。

        信封**永远 ``ok: true``**（这次请求本身成功了），检查结果用 ``checked``
        区分：查不到（断网 / 限流 / 还没发布过）给 ``checked: false`` + 一句人话
        的 ``reason`` —— 不用 ``ok: false``，那会踩进 api.js 的错误约定（它把它
        当接口事故，前端还得特判）。"""
        current = __version__
        try:
            tag, url = fetch_latest_release()
        except urllib.error.HTTPError as exc:
            reason = (
                "GitHub 上还没有发布版 —— 发过 Release 之后这里就能查到"
                if exc.code == 404
                else f"GitHub 回了 HTTP {exc.code}（多半是限流，过会儿再试）"
            )
            return Response.json({"ok": True, "current": current, "checked": False, "reason": reason})
        except (urllib.error.URLError, TimeoutError, OSError):
            return Response.json({
                "ok": True,
                "current": current,
                "checked": False,
                "reason": "连不上 GitHub（断网，或网络到不了它）",
            })
        except Exception as exc:  # noqa: BLE001 - 其余失败同样只值一句人话
            return Response.json({
                "ok": True,
                "current": current,
                "checked": False,
                "reason": f"检查不了更新：{exc}",
            })
        return Response.json({
            "ok": True,
            "current": current,
            "checked": True,
            "latest": tag,
            "update_available": update_available(current, tag),
            "url": url or "https://github.com/Drhushi/GameTrans/releases",
        })

    # ---- 配置与凭证 ---------------------------------------------------------

    #: 面板配置表单的字段说明：标签、含义、控件与取值范围都在这里给。
    #: 前端照它渲染，不维护第二份 —— 校验在后端，字段的含义也该由后端说。
    #: provider 不在 CONFIG_GROUPS 里：它跟密钥是同一次接入，前端在「模型接入」卡渲染。
    CONFIG_FIELDS: dict[str, dict[str, Any]] = {
        "target_language": {
            "label": "目标语言",
            "help": "译文语言代码；要跟游戏语言入口认的代码一致",
            "placeholder": "zh_CN",
        },
        "source_language": {
            "label": "源语言",
            "help": "auto 表示让模型自己判断",
            "placeholder": "auto",
        },
        "provider": {
            "label": "接入方式（provider）",
            "help": "openai：任何 OpenAI 兼容接口（DeepSeek、Kimi…）；mock：本地假模型，空跑流程用",
            "choices": ["openai", "mock"],
        },
        "mode": {
            "label": "调度模式",
            "help": "auto：按并行度自动决定；serial：强制逐条翻；parallel：强制并行",
            "choices": list(VALID_MODES),
            "advanced": True,
        },
        "batch_size": {
            "label": "一次调用最多装几条",
            "help": "单次调用封顶：段能装下就整段一次，装不下才按它拆",
            "min": 1,
            "advanced": True,
        },
        "group_by": {
            "label": "翻译单元取哪一级结构",
            "help": (
                "auto：按适配层申报的首级分段；"
                "留空：不分组（逐条，仅作对照）；也可填具体层级名"
            ),
            "type": "text",
            "advanced": True,
        },
        "concurrency": {
            "label": "并行度",
            "help": "同时进行的请求数",
            "min": 1,
            "advanced": True,
        },
        "max_attempts": {
            "label": "单条最多问几次",
            "help": "同一条任务问模型的总次数上限（含第一次）",
            "min": 1,
            "advanced": True,
        },
        "retry_on_violation": {
            "label": "结构不过重试几次",
            "help": "结构校验没过时带着反馈重试；0 = 不重试",
            "min": 0,
            "advanced": True,
        },
        "use_glossary": {"label": "注入译名", "help": "术语书里有译名的行随任务发给模型", "advanced": True},
        "use_worldbook": {"label": "注入设定", "help": "术语书里被触发的设定行进上下文", "advanced": True},
        "use_style": {"label": "注入风格指南", "help": "命中的风格要求进上下文", "advanced": True},
        "use_knowledge": {
            "label": "术语书参与",
            "help": "关掉＝连已批准的那些也不看（对照臂：只剩原文与风格）",
            "advanced": True,
        },
        "use_memory": {"label": "复用翻译记忆", "help": "相同原文直接复用既有译文", "advanced": True},
        "require_structure": {
            "label": "结构校验",
            "help": "关掉后只剩输出形状检查，不查占位符与标签",
            "advanced": True,
        },
        "custom_instructions": {
            "label": "自定义要求",
            "help": "每次翻译都会附给模型的额外要求",
            "type": "text",
            "advanced": True,
        },
    }

    #: 面板配置表单里的分段。前端照它渲染，不在 HTML 里写死字段名。
    #: 接口地址与模型名不在这里 —— 它们跟密钥一起归凭证那一组。
    CONFIG_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("语言", ("target_language", "source_language")),
        ("执行", ("mode", "group_by", "batch_size", "concurrency", "max_attempts", "retry_on_violation")),
        (
            "注入什么",
            (
                "use_glossary",
                "use_worldbook",
                "use_style",
                "use_knowledge",
                "use_memory",
                "require_structure",
            ),
        ),
        ("自定义要求", ("custom_instructions",)),
    )

    def _api_config(self, method: str, body: bytes) -> Response:
        """读 / 写项目配置与模型凭证。

        ``GET`` 给当前值 + 可写键清单 + 分段；``POST`` 一次可以交上来多项，
        **先全部校验再落盘**（任一项非法就整批不改）。
        """
        if method not in ("GET", "POST"):
            return Response.json(
                {"ok": False, "error": {"message": "这个接口只接受 GET 或 POST"}},
                status=405,
            )
        session = self.session()
        if method == "GET":
            from gametrans.userconfig import global_config_path

            sources = _config_sources(session)
            credentials = session.credentials_view()
            return Response.json(
                {
                    "ok": True,
                    "config": session.config.to_dict(),
                    "credentials": credentials,
                    "allowed_keys": list(session.writable_config_keys()),
                    "protected_keys": list(session.PANEL_PROTECTED_KEYS),
                    "groups": [
                        {"title": title, "keys": list(keys)}
                        for title, keys in self.CONFIG_GROUPS
                    ],
                    # 每个字段的标签 / 说明 / 可选值 / 下限：前端照它渲染表单
                    "fields": dict(self.CONFIG_FIELDS),
                    "warnings": list(session.load_warnings),
                    # —— 分层带来的新增信息（只增不改） ——
                    # 每一项的生效值来自哪一层；前端据此在字段旁标出处
                    "sources": {**sources, **{
                        key: credentials["sources"][key]
                        for key in credentials.get("sources", {})
                    }},
                    "global_fields": list(GLOBAL_FIELDS),
                    "layers": {
                        "project": {"config": str(session.workdir / "project.json")},
                        "global": {
                            "dir": str(global_config_path().parent),
                            "config": str(global_config_path()),
                            "config_values": dict(session.global_config),
                        },
                    },
                }
            )
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response.json(
                {"ok": False, "error": {"message": "请求体不是合法 JSON"}}, status=400
            )
        if not isinstance(payload, dict):
            return Response.json(
                {"ok": False, "error": {"message": "请求体必须是一个 JSON 对象"}}, status=400
            )
        # 写到哪一层：不带就写项目层（与一直以来的行为一致，不会突然改全局）
        layer = str(payload.pop("layer", "project") or "project")
        try:
            result = session.apply_settings(payload, layer=layer)
        except GameTransError as exc:
            return Response.json(error_envelope(exc), status=400)
        return Response.json({"ok": True, **result})

    def _api_operation(self, method: str, path: str, body: bytes = b"") -> Response:
        key = path[len("/api/operations/") :].strip("/")
        operation = get_operation_by_tool(key) or next(
            (op for op in all_operations() if op.name == key), None
        )
        if operation is None:
            return self._not_found(f"没有这个操作：{key}")

        if method != "POST":
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": "MethodNotAllowed",
                        "message": f"{operation.name} 需要通过 POST 调用",
                        "hint": None,
                    },
                },
                status=405,
            )

        if operation.mutates and operation.name not in PANEL_WRITE_OPERATIONS:
            cli = "gametrans " + operation.name.replace(".", " ")
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": "ReadOnlyDashboard",
                        "message": f"面板不执行这个写操作：{operation.name!r}",
                        "hint": (
                            "面板只开放内容与审核类写操作（术语书 / 风格 / 知识）。"
                            f"其余写路径请用 CLI：`{cli}`，或 MCP 工具 `{operation.tool_name}`。"
                        ),
                    },
                },
                status=501,
            )

        try:
            arguments = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response.json(
                {"ok": False, "error": {"message": "请求体不是合法 JSON"}}, status=400
            )
        if not isinstance(arguments, dict):
            return Response.json(
                {"ok": False, "error": {"message": "请求体必须是一个 JSON 对象"}}, status=400
            )

        ctx = Context(project_root=self.project_root, workdir=self.workdir, json_mode=True)
        arguments = arguments_for(operation, arguments)
        missing = missing_arguments(operation, arguments)
        if missing:
            return Response.json(
                {
                    "ok": False,
                    "error": {
                        "type": "MissingArgument",
                        "message": f"{operation.name} 缺少必需参数：{', '.join(missing)}",
                        "hint": None,
                    },
                },
                status=400,
            )
        try:
            data = operation.handler(ctx, arguments)
        except GameTransError as exc:
            return Response.json(error_envelope(exc), status=400)
        return Response.json(result_envelope(data))

    # ---- 静态资源 -----------------------------------------------------------

    def _static(self, relative: str) -> Response:
        from urllib.parse import unquote

        root = self.static_root.resolve()
        rel = unquote(relative)
        try:
            target = (root / rel).resolve()
        except (OSError, ValueError):
            return self._forbidden(rel)
        # 路径穿越防护：解析后的路径必须落在 static/ 之内
        if target != root and root not in target.parents:
            return self._forbidden(rel)
        if not target.is_file():
            return self._not_found(f"没有这个静态资源：{rel}")
        content_type = CONTENT_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        return Response(status=200, body=target.read_bytes(), content_type=content_type)

    # ---- 错误 ---------------------------------------------------------------

    @staticmethod
    def _not_found(message: str) -> Response:
        return Response.json(
            {"ok": False, "error": {"type": "NotFound", "message": message, "hint": None}},
            status=404,
        )

    @staticmethod
    def _forbidden(path: str) -> Response:
        return Response.json(
            {
                "ok": False,
                "error": {
                    "type": "Forbidden",
                    "message": f"拒绝了越界的静态资源路径：{path!r}",
                    "hint": None,
                },
            },
            status=403,
        )


def _describe(operation: Operation) -> dict[str, Any]:
    return {
        "name": operation.name,
        "tool_name": operation.tool_name,
        "summary": operation.summary,
        "group": operation.group,
        "mutates": operation.mutates,
        # 面板能不能直接 POST 它：只读操作一律可以，写操作看白名单
        "panel_callable": not operation.mutates
        or operation.name in PANEL_WRITE_OPERATIONS,
        "inputSchema": tool_schema(operation),
        "params": [
            {
                "name": p.name,
                "type": p.type,
                "required": p.required,
                "help": p.help,
                "positional": p.positional,
                "choices": p.choices,
                "default": p.default,
                "multiline": p.multiline,
            }
            for p in operation.params
        ],
    }
