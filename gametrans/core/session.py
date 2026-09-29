"""项目会话 —— agent 的操作对象。

会话把五层串成一条流水线，并把每一步的产物落盘成可读文件：

.. code-block:: text

    <项目>/.gametrans/
      project.json          配置（用户/agent 可直接改）
      graph.json            最近一次提取的带权路径图
      translations.jsonl    译文
      resources/
        termbook.jsonl            术语书：一行一个实体（五栏）
        termbook.changes.jsonl    变更日志（新旧对照 + 统一替换的替换源）
        termbook.pending.jsonl    待审更正（改已有的译名 / 事实要先提提案）
      interaction/state.json 面向用户的视图与可见性策略
      reports/<run-id>.json  每次运行的完整结构化报告
      dist/                 封包好的补丁

关键点是**每一步都可以单独跑**：agent 可以只 scan、只 translate 一批、改完术语书
再重跑 writeback —— 不需要把整条流水线当成一个不可分割的黑盒。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gametrans import __version__
from gametrans.config import (
    CONFIG_FILE,
    ProjectConfig,
    coerce_value,
    load_project_values,
    save_project_values,
)
from gametrans.credentials import CREDENTIALS_FILE, CREDENTIAL_FIELDS, Credentials
from gametrans.core.chapters import apply_chapters
from gametrans.core.dependencies import policy_for
from gametrans.core.graph import PathGraph
from gametrans.core.ir import ProjectIR
from gametrans.core.summaries import load_summaries
from gametrans.core.constraints import default_constraints, slot_gauge, validate
from gametrans.core.models import (
    ConstraintType,
    GraphEdge,
    Segment,
    SegmentKind,
    TranslationArtifact,
    TranslationStatus,
    TranslationUnit,
    is_placeholder,
)
from gametrans.core.report import RunReport
from gametrans.engines.base import ExportTarget, ExtractContext, with_original_text
from gametrans.engines.registry import EngineRegistry
from gametrans.errors import (
    ConfigError,
    EngineError,
    GameTransError,
    ProjectError,
    WriteBackError,
)
from gametrans.layers.interaction import InteractionLayer
from gametrans.layers.resource import ResourceLayer, TermEntry
from gametrans.layers.knowledge import (
    APPROVERS,
    DEFAULT_MIN_BATCHES,
    ConsolidationPlan,
    DeclarationEvidence,
    KnowledgeError,
    KnowledgeUpdate,
    candidate_id,
    plan_consolidation,
)
from gametrans.layers.memory import IMPORTED_PROVIDER
from gametrans.layers.naming import CorpusEvidence, corpus_evidence_from_graph, judge_terms
from gametrans.layers.translate import TranslateLayer, TranslateOptions
from gametrans.layers.writeback import PatchOutcome, WriteBackLayer
from gametrans.providers.base import DeclaredTerm, coerce_declared as _coerce
from gametrans.core.tasks import TaskState, TaskStore, TranslationTask

DEFAULT_WORKDIR_NAME = ".gametrans"
GRAPH_FILE = "graph.json"
TRANSLATIONS_FILE = "translations.jsonl"
TASKS_FILE = "tasks.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _absolute(path: Path) -> Path:
    """把项目根 / 工作区归成**绝对路径**再进会话。

    为什么不能在会话里留相对路径：提取层拿 ``project_root.name`` 当图上的根容器名
    （``renpy_demo``），子路径都挂在它下面。``Path(".")`` 的 ``.name`` 是空串 ——
    于是根容器名、文件节点 id（``game/script.rpy``）、骨架里的 ``# game/script.rpy``
    三处命名空间对不上，对齐阶段留下悬空子节点，``walk()`` 直接 ``KeyError``。
    CLI 的 ``--project`` 默认就是当前目录（相对写法），所以命令行一跑就炸、
    而测试里传的是绝对路径 —— 这正是它一直没被机检抓住的原因。
    """
    return Path(path).expanduser().resolve()


class _BatchKnowledge:
    """**边跑边积累**的执行者：每批跑完把这一批的申报并进术语书，并合并成写不写的结论。

    每批（一批 = 一轮调用）做三件事：

    1. **并已有**：书里已经有那个写法的，新写法 / 空译名 / 新事实**免审追加**
       （改已有的译名会由 `TermBook.apply` 自己送进待审队列）；
    2. **查重复**：同一个原文在**不同的批**里各自被申报过一次且译名一致 → 由 **agent**
       身份**写进书**（这就是它的写入门槛，不是审核状态）；
    3. **挑冲突**：同一原文出现多种译名 → 先落第一个，其余进待审更正（第 ③ 步）。

    三条不许越过的线：

    * 证据单位是**批**：批内的调用并行发出、互相看不见，同批里说两遍不是两份证据
      （算两份就等于让模型给自己作证）；
    * **不 force**：``termhood:drop`` 是语料判据说"这个词不像专名"（真靶上 `play`
      全小写出现 213 次）—— 写进书就等于把普通词变成每句都注入的硬约束；
    * **不复活**：书里已经有这一行的（人写的 / 模型写的 / **已否决**）一律不重判。
    """

    def __init__(self, session: "ProjectSession", *, min_batches: int) -> None:
        self.session = session
        self.min_batches = min_batches
        self.evidence: list[DeclarationEvidence] = []
        #: 原文 → 第一次的说法（与 `declared_terms` 同一个口径：只用来报数）
        self.declared: dict[str, str] = {}
        #: 原文 → 这一轮申报过的事实（写进行上的 `profile`）
        self.settings: dict[str, list[str]] = {}
        self.created: list[dict[str, Any]] = []
        #: 同一个词又说了一遍事实 —— 追加进去的条数（只用来报数）
        self.profiles_merged = 0
        #: 申报的事实**书上已经写过**的条数（措辞近乎逐字，被当重复吞掉）
        self.repeated_facts = 0
        #: 申报的事实**换个说法说的同一件事**的条数（会进待审队列）。
        #: 这两个数就是"模型没看见已知信息"的报警读数。
        self.near_duplicate_facts = 0
        #: 译名那一栏抄回来的是标签本身的写法（跨批累加、去重；它们一个都没落笔）
        self.tagged_targets: list[str] = []
        self.flushes = 0
        #: 每一批的结论；报告取**最后一次**（它包含了全部累积证据）
        self.latest = ConsolidationPlan()
        #: 本轮由 agent 写进书的全部行（跨批累加）
        self.approved: list[tuple[str, str, list[str]]] = []
        #: 进待审队列的更正条数（跨批累加）
        self.pending_proposals = 0
        self._verdicts: dict[str, tuple[bool, str]] = {}
    def settings_only(self) -> list[tuple[str, list[str]]]:
        """**只申报了事实、没有译名**的那些（跨批判据看的是译名，它们没有可比对的东西）。

        它们这一轮写不进书，但**必须点名**：只报一个总数，"模型没申报"与"申报了
        事实、但缺译名所以没写"看起来一模一样（R63 记过的坑）。
        """
        book = self.session.resources.termbook
        return [
            (source, list(facts))
            for source, facts in self.settings.items()
            if facts and book.status_of(source) is None
        ]

    # ---- 每批一次 -----------------------------------------------------------

    def flush(
        self,
        group: str,
        terms: list["DeclaredTerm"],
    ) -> None:
        """一批跑完：并已有 → 合并 → （按判据）写进书 / 提更正。"""
        self.flushes += 1
        for entry in terms:
            source = str(entry.source).strip()
            target = str(entry.target).strip()
            self.declared.setdefault(source, target or str(entry.profile))
            if target:
                self.evidence.append(DeclarationEvidence(group, source, target))
            setting = str(entry.profile).strip()
            if source and setting:
                bucket = self.settings.setdefault(source, [])
                if setting not in bucket:
                    bucket.append(setting)
        if not terms:
            return
        fresh = [entry for entry in terms if entry.source not in self._verdicts]
        if fresh:
            # 语料判据一次算一批新词（语料本身按图缓存）；判过的词不重复判 ——
            # 真靶的语料是 15k 个槽位，逐批重判整份会把这一步变成分钟级
            self._verdicts.update(self.session.judge_terms(fresh))
        # ① 书里已经有那个写法：免审追加（approved 给空集 = 不许新开行，只并已有的）
        self.session.record_declared_terms(
            terms, verdicts=self._verdicts, approved=frozenset()
        )
        self.profiles_merged += getattr(self.session, "_last_profile_merged", 0)
        self.repeated_facts += getattr(self.session, "_last_repeated_facts", 0)
        self.near_duplicate_facts += getattr(self.session, "_last_near_duplicate_facts", 0)
        for writing in getattr(self.session, "_last_tagged_targets", ()) or ():
            if writing not in self.tagged_targets:
                self.tagged_targets.append(writing)
        # 门槛 0 = 关掉这条通道的自动写入（"我自己来"）：**判据照样算、照样报**
        # （撞车与"重复且一致"的清单在这个档位下更有用 —— 全要人看），只是不写进书。
        threshold = self.min_batches if self.min_batches >= 2 else DEFAULT_MIN_BATCHES
        state: dict[str, str] = {}
        for source in self.declared:
            status = self.session.resources.termbook.status_of(source)
            if status is not None:
                state[source] = status
        plan = plan_consolidation(
            self.evidence,
            min_batches=threshold,
            verdicts=self._verdicts,
            state=state,
        )
        self.latest = plan
        # 撞车的那些**写进待审队列**：先落第一个申报（免审追加），其余成为更正提案，
        # 由人或 agent 在 `resource.term.pending.*` 上拍板。`apply` 自己分流 —— 书里
        # 已经有这一行时第一个也只会变成提案，一个字节都不动。
        for conflict in plan.conflict:
            for variant, groups in conflict.targets.items():
                before = len(self.session.resources.termbook.pending.records())
                self.session.resources.termbook.apply(
                    TermEntry(
                        key=[{"writing": conflict.source, "target": variant}],
                        profile=list(self.settings.get(conflict.source) or []),
                    ),
                    by="agent",
                    why=(
                        f"跨批撞车：同一个原文在 {'、'.join(groups)} 批里被译成多种，"
                        "先落第一种，其余进待审更正"
                    ),
                )
                if len(self.session.resources.termbook.pending.records()) > before:
                    self.pending_proposals += 1
        if self.min_batches < 2:
            return
        allowed = {source for source, _target, _groups in plan.approve}
        created = self.session.record_declared_terms(
            terms, verdicts=self._verdicts, approved=allowed
        )
        self.created.extend(created)
        written = {str(row.get("content") or "") for row in created}
        for source, target, groups in plan.approve:
            if source in written:
                self.approved.append((source, target, groups))


def _batch_knowledge_hook(knowledge: "_BatchKnowledge"):
    """把 :meth:`_BatchKnowledge.flush` 包成翻译层要的那个回调。"""

    def hook(group: str, terms: list[DeclaredTerm]) -> None:
        knowledge.flush(group, terms)

    return hook


def _unverified_structure(unit: TranslationUnit, result: Any) -> str | None:
    """这次校验**没碰过**结构约束吗？碰过就返回 ``None``，没碰过返回原因。

    ``validate`` 在没有 scanner 时只查"输出形状"，占位符 / 标签 / 控制码这些一概
    进不了 ``checked`` —— 不假装检查过是它的正确行为。问题在于调用方：此时若照着
    ``result.ok`` 判"可用"，就等于签了一张没验过的合格证。所以手改这条路径必须
    自己问一句"结构到底验没验"。
    """
    declared = {c.constraint_type for c in default_constraints(unit)}
    declared.discard(ConstraintType.OUTPUT_SHAPE_VALID.value)
    if declared and not declared & set(result.checked):
        return (
            "当前引擎没有申报结构切分器：占位符 / 标签 / 控制码这些结构约束未被校验，"
            "因此这条译文不能判为可用"
        )
    return None


@dataclass
class ScanOutcome:
    graph: PathGraph
    report: RunReport

    def to_dict(self) -> dict[str, Any]:
        return {"graph": self.graph.stats(), "report": self.report.to_dict()}


@dataclass
class TranslateOutcome:
    records: list[TranslationArtifact]
    report: RunReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "report": self.report.to_dict(),
        }


@dataclass
class WriteBackOutcome:
    files: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    units_written: int = 0
    units_missing: int = 0
    #: 其中有多少条是**有译文但被导出前校验挡下**的（不是"没翻"）
    units_rejected: int = 0
    char_count: int = 0
    output_dir: str = ""
    report: RunReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "backups": list(self.backups),
            "units_written": self.units_written,
            "units_missing": self.units_missing,
            "units_rejected": self.units_rejected,
            "char_count": self.char_count,
            "output_dir": self.output_dir,
            "report": self.report.to_dict() if self.report else None,
        }


class ProjectSession:
    """一个项目的操作句柄。"""

    def __init__(
        self,
        project_root: Path,
        *,
        registry: EngineRegistry | None = None,
        providers: Any = None,
        workdir: Path | None = None,
        config: ProjectConfig | None = None,
        project_values: dict[str, Any] | None = None,
        load_warnings: list[str] | None = None,
    ) -> None:
        from gametrans.bootstrap import build_providers, build_registry
        from gametrans.userconfig import (
            load_global_config,
            load_global_credentials,
            resolve_config,
            resolve_credentials,
        )

        self.project_root = Path(project_root)
        self.workdir = Path(workdir) if workdir else self.project_root / DEFAULT_WORKDIR_NAME
        self.registry = registry if registry is not None else build_registry()
        self.load_warnings = list(load_warnings or [])

        # **项目文件里实际有些什么**（不补默认值）。生效配置由它 + 全局层解析出来。
        # 分开保管是这套分层的地基：把生效配置整份写回项目文件，就等于把全局层的值
        # 烤进项目里，从此那一项再也跟不上全局层，用户也分不清哪个值是真的。
        self._project_values: dict[str, Any] = dict(project_values or {})

        global_config, global_warnings = load_global_config()
        self.global_config = global_config
        self.load_warnings.extend(global_warnings)
        self.config = (
            config
            if config is not None
            else resolve_config(
                self._project_values, global_config, warnings=self.load_warnings
            )
        )

        # 凭证同一条链：环境变量 > 项目 > 全局 > 空
        global_credential_values, credential_warnings = load_global_credentials()
        self.global_credentials = global_credential_values
        self.load_warnings.extend(credential_warnings)
        project_credential_values, problems = Credentials.load_values(
            self.workdir / CREDENTIALS_FILE
        )
        self._project_credential_values = project_credential_values
        self._credential_problems = list(problems)
        self.load_warnings.extend(problems)
        self.credentials = resolve_credentials(
            project_credential_values, global_credential_values
        )
        self.providers = (
            providers
            if providers is not None
            else build_providers(
                credentials=self.credentials.storage_dict(),
                request_overrides=self.config.request_overrides,
                workdir=self.workdir,
            )
        )

        self.resources = ResourceLayer(self.workdir / "resources", registry=self.registry)
        self.interaction = InteractionLayer(self.workdir / "interaction")
        # 任务状态：翻译层产出的每条 Task 都落到这里（Guide §17：状态必须持久化）
        self.tasks = TaskStore(self.workdir / TASKS_FILE)
        # 刻意**不**在这里 ensure()：打开会话是只读动作，不该往工作区写文件。
        # 资源层与交互层在真正要写的时候各自 ensure()（只读操作因此零副作用）。

        self.translate_layer = TranslateLayer(self.providers)
        self.writeback_layer = WriteBackLayer(self.registry)
        self._graph: PathGraph | None = None
        self._graph_stamp: tuple[int, int] | None = None
        self._translations: list[TranslationArtifact] | None = None
        self._translations_stamp: tuple[int, int] | None = None
        self._engine: str | None = None

    # ---- 引擎解析 -----------------------------------------------------------

    @property
    def engine(self) -> str:
        """当前项目的引擎名。

        名字**只能**来自配置或探测，内核自己不认识任何引擎。配置里缺引擎
        （例如配置文件损坏后回退默认值）就现场重新探测，而不是假设一个默认引擎。
        """
        if self._engine is None:
            self._engine = self._resolve_engine()
        return self._engine

    def _resolve_engine(self) -> str:
        declared = self.config.engine
        if declared:
            if declared not in self.registry:
                raise EngineError(
                    f"配置指定的引擎支持包 {declared!r} 没有安装",
                    hint=(
                        f"当前已安装：{', '.join(self.registry.names()) or '（无）'}。"
                        "用 `gametrans engine list` 查看，或安装对应的支持包。"
                    ),
                )
            return declared
        detected = next(
            (d for d in self.registry.detect(self.project_root) if d.detected), None
        )
        if detected is None:
            raise EngineError(
                f"无法识别 {self.project_root} 使用的游戏引擎",
                hint=(
                    f"当前已安装的引擎支持包：{', '.join(self.registry.names()) or '（无）'}。"
                    "用 `gametrans project init` 重新初始化并显式指定 --engine。"
                ),
            )
        return detected.engine

    # ---- 生命周期 -----------------------------------------------------------

    @classmethod
    def init(
        cls,
        project_root: Path,
        *,
        engine: str | None = None,
        target_language: str = "zh_CN",
        provider: str | None = None,
        workdir: Path | None = None,
        registry: EngineRegistry | None = None,
        providers: Any = None,
        **overrides: Any,
    ) -> "ProjectSession":
        from gametrans.bootstrap import build_registry

        project_root = _absolute(project_root)
        if not project_root.is_dir():
            raise ProjectError(
                f"项目路径不存在或不是目录：{project_root}",
                hint="确认游戏根目录的路径 —— 它应当是包含游戏脚本的那个目录，而不是它的父目录。",
            )
        registry = registry if registry is not None else build_registry()
        workdir = _absolute(workdir) if workdir else project_root / DEFAULT_WORKDIR_NAME

        if (workdir / CONFIG_FILE).exists():
            raise ProjectError(
                f"{project_root} 已经是一个 gametrans 项目（{workdir / CONFIG_FILE} 已存在）",
                hint="用 `gametrans project status` 查看现状；确实要重来请先删除该工作区目录。",
            )

        if engine is None:
            detected = next((d for d in registry.detect(project_root) if d.detected), None)
            if detected is None:
                raise EngineError(
                    f"无法识别 {project_root} 使用的游戏引擎",
                    hint=(
                        f"当前已安装的引擎支持包：{', '.join(registry.names()) or '（无）'}。"
                        "可以用 --engine 显式指定。"
                    ),
                )
            engine = detected.engine

        # **只写"显式定下来"的那几项**，不把整份默认配置倒进项目文件。
        # 倒进去的代价是：每一默认值都永久压住全局层 —— 于是"全局配一次、处处通用"
        # 这件事从第一天起就失效，而且看不出来（项目里明明写着 provider: mock）。
        project_values: dict[str, Any] = {
            "engine": engine,
            "target_language": target_language,
        }
        # provider 只有调用方**显式给了**才写进项目；没给就让它跟全局层/出厂默认走。
        if provider is not None:
            project_values["provider"] = provider
        for key, value in overrides.items():
            if value is None:
                continue
            # init 的参数写错名字要当场报错，别等到某次读取才发现
            ProjectConfig().validate_field(key)
            project_values[key] = coerce_value(key, value) if key != "engine_options" else value

        config = ProjectConfig.from_dict(project_values)[0]

        session = cls(
            project_root,
            registry=registry,
            providers=providers,
            workdir=workdir,
            config=config,
            project_values=project_values,
        )
        # init 是**显式建工作区**的动作，所以在这里把脚手架落下来（用户马上能看到
        # 可读的资源文件）。open() 则绝不写盘 —— 只读操作零副作用。
        session.resources.ensure()
        session.interaction.ensure()
        session.save_config()
        return session

    @classmethod
    def open(
        cls,
        project_root: Path,
        *,
        workdir: Path | None = None,
        registry: EngineRegistry | None = None,
        providers: Any = None,
    ) -> "ProjectSession":
        project_root = _absolute(project_root)
        workdir = _absolute(workdir) if workdir else project_root / DEFAULT_WORKDIR_NAME
        config_path = workdir / CONFIG_FILE
        if not config_path.is_file():
            raise ProjectError(
                f"{project_root} 还不是 gametrans 项目（找不到 {config_path}）",
                hint="先运行 `gametrans project init <项目路径>` 初始化工作区。",
            )
        project_values, warnings = load_project_values(config_path)
        return cls(
            project_root,
            registry=registry,
            providers=providers,
            workdir=workdir,
            project_values=project_values,
            load_warnings=warnings,
        )

    def save_config(self) -> None:
        """把**项目层显式设过的那些键**写回项目文件。

        刻意不写生效配置：那里面含着全局层的值，整份写回就等于把全局值烤进项目文件。
        """
        save_project_values(self.workdir / CONFIG_FILE, self._project_values)

    def update_config(self, **fields: Any) -> ProjectConfig:
        """改项目层的配置。``None`` 表示**删掉这一项**（回落全局层），不是设成空。

        写的时候严格：值先归一化并校验，坏值当场报错，绝不让它进文件。
        """
        from gametrans.userconfig import resolve_config

        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "engine_options":
                self.config.validate_field(key)
                if value is None:
                    self._project_values.pop(key, None)
                elif isinstance(value, dict):
                    # 并集语义：给新键合并、不给的保留（删要显式传空字典覆盖）
                    values[key] = {**self._project_values.get(key, {}), **value}
                else:
                    raise ConfigError(f"engine_options 必须是一个对象，收到 {value!r}")
                continue
            self.config.validate_field(key)
            values[key] = None if value is None else coerce_value(key, value)

        for key, value in values.items():
            if value is None:
                self._project_values.pop(key, None)
            else:
                self._project_values[key] = value
        self.config = resolve_config(
            self._project_values, self.global_config, warnings=self.load_warnings
        )
        self._engine = None  # 引擎可能被改掉，缓存作废
        self.save_config()
        return self.config

    def reload_layers(self) -> ProjectConfig:
        """按磁盘上的当前状态把两层重新解析一遍。

        为什么需要它：``--global`` 写的是**另一个文件**（全局层），而这个会话的
        ``config`` 是打开时解析好的快照。不重解析，命令的返回值就还是旧值 ——
        用户会看到"我刚刚设的值没生效"，而其实只是回显过期了。
        """
        from gametrans.bootstrap import build_providers
        from gametrans.userconfig import (
            load_global_config,
            load_global_credentials,
            resolve_config,
            resolve_credentials,
        )

        self.global_config, warnings = load_global_config()
        self.load_warnings.extend(warnings)
        self._project_values, project_warnings = load_project_values(
            self.workdir / CONFIG_FILE
        )
        self.load_warnings.extend(project_warnings)

        self.global_credentials, credential_warnings = load_global_credentials()
        self.load_warnings.extend(credential_warnings)
        self._project_credential_values, problems = Credentials.load_values(
            self.workdir / CREDENTIALS_FILE
        )
        self._credential_problems = list(problems)

        self.config = resolve_config(
            self._project_values, self.global_config, warnings=self.load_warnings
        )
        self.credentials = resolve_credentials(
            self._project_credential_values, self.global_credentials
        )
        self.providers = build_providers(
            credentials=self.credentials.storage_dict(),
            request_overrides=self.config.request_overrides,
            workdir=self.workdir,
        )
        self._engine = None
        return self.config

    # ---- 配置与凭证（控制面共用的写路径） -----------------------------------

    #: 面板可以写的配置键：``SCHEMA`` 减去「由探测或专用入口决定」的那几个。
    PANEL_PROTECTED_KEYS = ("engine", "engine_options")

    @classmethod
    def writable_config_keys(cls) -> tuple[str, ...]:
        """能通过统一写路径改的配置键。引擎与引擎选项不在其中 ——
        前者由探测决定，后者走 :meth:`update_engine_option` 那条专用通路。"""
        from gametrans.config import SCHEMA

        return tuple(sorted(k for k in SCHEMA if k not in cls.PANEL_PROTECTED_KEYS))

    def apply_config(self, payload: dict[str, Any]) -> ProjectConfig:
        """按「先全部校验、再一次性落盘」写配置。

        任一项非法就整批不改 —— 半途生效会留下一个没人预期的中间状态。
        """
        coerced = self._validated_config(payload)
        if not coerced:
            return self.config
        return self.update_config(**coerced)

    def _validated_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把配置那一半校验并归一 —— **纯计算，不碰磁盘**。"""
        from gametrans.errors import ConfigError
        from gametrans.prompts import validate as validate_template

        if not isinstance(payload, dict):
            raise ConfigError("配置必须是一个 JSON 对象")
        coerced: dict[str, Any] = {}
        for key, raw in payload.items():
            if key in self.PANEL_PROTECTED_KEYS:
                raise ConfigError(
                    f"这一项不能从面板改：{key}",
                    hint=(
                        "引擎由探测决定；引擎选项走「引擎设置」那一栏。"
                        "可改的字段见 /api/config 的 allowed_keys。"
                    ),
                )
            if key not in self.writable_config_keys():
                raise ConfigError(
                    f"未知的配置字段：{key!r}",
                    hint=f"可写字段：{', '.join(self.writable_config_keys())}",
                )
            # 模板在**保存这一刻**就校验：占位符写错的模板一旦落盘，生产会跑到半路才炸，
            # 而那时已经花掉的钱没法退。校验是纯计算，不通过就整批不改。
            if key == "prompt_templates":
                if not isinstance(raw, dict):
                    raise ConfigError("prompt_templates 必须是一个对象：模板名 → 模板")
                problems: list[str] = []
                for name, body in raw.items():
                    problems += [f"{name}：{item}" for item in validate_template(body, where=f"模板 {name}")]
                if problems:
                    raise ConfigError(
                        "模板里有写错的地方，没有保存：" + "；".join(problems[:5]),
                        hint="占位符必须用 {target_language} / {source_language} / "
                             "{head} / {id} / {source} / {resolved} / {protected} / "
                             "{position} / {speaker} / {unit} 这些名字。",
                    )
            coerced[key] = coerce_value(key, raw)
        return coerced

    def credentials_view(self) -> dict[str, Any]:
        """对外的凭证视图：**只有尾巴**，外加"每一项来自哪一层"。

        ``api_key`` / ``base_url`` / ``model`` 显示的是**两层文件里存下来的那一份**
        （不含环境变量），再用 ``env_override`` 告诉用户环境变量压过了它 ——
        把环境变量算进展示，用户会看到一把自己从没存过、也改不掉的密钥。

        四层之后多出来的是 ``sources``（逐字段来源）与 ``layers``（两层各自存了什么）。
        已有的键一个不改 —— 前端正在用它们。
        """
        import os

        from gametrans.userconfig import (
            global_credentials_path,
            resolve_credentials,
            resolve_credentials_sources,
        )

        stored = resolve_credentials(
            self._project_credential_values, self.global_credentials, include_env=False
        )
        view = stored.masked()
        view["env_override"] = bool(
            os.environ.get("GAMETRANS_API_KEY") or os.environ.get("OPENAI_API_KEY")
        )
        view["path"] = str(self.workdir / CREDENTIALS_FILE)
        view["problems"] = list(self._credential_problems)
        # —— 以下是分层带来的新增信息（只增不改） ——
        view["sources"] = resolve_credentials_sources(
            self._project_credential_values, self.global_credentials
        )
        view["layers"] = {
            "project": {"path": str(self.workdir / CREDENTIALS_FILE)},
            "global": {"path": str(global_credentials_path())},
        }
        return view

    def update_credentials(self, **fields: Any) -> Credentials:
        """写**项目层**的凭证并让 provider 立刻拿到它（同一个进程里不需要重开）。

        ``None`` 表示删掉这一项（回落全局层），不是设成空串。
        """
        from gametrans.bootstrap import build_providers
        from gametrans.userconfig import resolve_credentials

        for key in fields:
            if key not in CREDENTIAL_FIELDS:
                raise ConfigError(
                    f"未知的凭证字段：{key!r}",
                    hint=f"可写：{', '.join(CREDENTIAL_FIELDS)}。",
                )
        for key, value in fields.items():
            if value is None:
                self._project_credential_values.pop(key, None)
            else:
                self._project_credential_values[key] = str(value)

        Credentials(**{
            key: str(self._project_credential_values.get(key) or "")
            for key in CREDENTIAL_FIELDS
        }).save(self.workdir / CREDENTIALS_FILE)

        self.credentials = resolve_credentials(
            self._project_credential_values, self.global_credentials
        )
        self.providers = build_providers(
            credentials=self.credentials.storage_dict(),
            request_overrides=self.config.request_overrides,
            workdir=self.workdir,
        )
        return self.credentials

    def apply_settings(
        self, payload: dict[str, Any], *, layer: str = "project"
    ) -> dict[str, Any]:
        """控制面的一次写入：配置与模型凭证可以一起交上来。

        ``api_key`` 走凭证文件，其余走配置 —— 对调用方是一个入口，对磁盘是两个文件
        （理由见 :mod:`gametrans.credentials`）。

        ``layer`` 决定写**哪一层**：``project``（默认，与一直以来的行为一致）或
        ``global``（"我这个用户、这台机器"的习惯，一次配好所有项目通用）。
        写全局层时只接受 :data:`~gametrans.config.GLOBAL_FIELDS` 里的字段 ——
        目标语言这类每游戏不同的东西会被拒绝并说明理由。

        **两半都先校验干净，再落盘**；落盘时先写凭证（它要建文件、更可能失败），
        再写配置。校验没过或凭证没写成功时，配置那一半保持原样 —— 不做半途生效。
        """
        from gametrans.errors import ConfigError
        from gametrans.userconfig import save_global_config, save_global_credentials

        if not isinstance(payload, dict):
            raise ConfigError("配置必须是一个 JSON 对象")
        if layer not in ("project", "global"):
            raise ConfigError(
                f"layer 只能是 project 或 global，收到 {layer!r}",
                hint="不带 layer 时默认写项目层。",
            )
        credentials = {
            key: payload[key] for key in CREDENTIAL_FIELDS if key in payload
        }
        config_payload = {
            key: value for key, value in payload.items() if key not in CREDENTIAL_FIELDS
        }

        if layer == "global":
            # 全局层：只收"与具体哪个游戏无关"的字段
            from gametrans.config import GLOBAL_FIELDS

            offenders = sorted(set(config_payload) - set(GLOBAL_FIELDS))
            if offenders:
                from gametrans.userconfig import project_only_reason

                raise ConfigError(
                    f"这些字段不能放进全局配置：{'、'.join(offenders)}",
                    hint=project_only_reason(offenders[0]),
                )
            validated_global = {
                key: coerce_value(key, value) for key, value in config_payload.items()
            }
            # 先校验凭证形状（纯计算），再落盘
            from gametrans.credentials import Credentials

            Credentials().merged(**credentials)
            if credentials:
                values = dict(self.global_credentials)
                values.update({key: str(value) for key, value in credentials.items()})
                save_global_credentials(values)
            if validated_global:
                values = dict(self.global_config)
                values.update(validated_global)
                save_global_config(values)
            self.reload_layers()
            return {
                "config": self.config.to_dict(),
                "credentials": self.credentials_view(),
                "layer": "global",
                "sources": self.config_sources(),
            }

        # 先校验（纯计算），两半都通过才轮到磁盘
        validated = self._validated_config(config_payload)
        self.credentials.merged(**credentials)
        if credentials:
            self.update_credentials(**credentials)
        if validated:
            self.update_config(**validated)
        return {
            "config": self.config.to_dict(),
            "credentials": self.credentials_view(),
            "layer": "project",
            "sources": self.config_sources(),
        }

    def config_sources(self) -> dict[str, str]:
        """配置字段逐项来自哪一层：``project`` / ``global`` / ``default``。"""
        from gametrans.userconfig import resolve_config_sources

        return resolve_config_sources(self._project_values, self.global_config)

    # ---- 各层入口 -----------------------------------------------------------

    def translate_options(self, **overrides: Any) -> TranslateOptions:
        from gametrans import prompts

        # 配置里的 "auto" 在这里落地成具体层级：**解析发生在套用 overrides 之前**，
        # 这样调用方显式传 `group_by=""` 就是"这一次不分组"，不会被 auto 覆盖回去。
        configured_level = (
            self.default_grouping_level()
            if self.config.group_by == "auto"
            else self.config.group_by
        )
        payload = {
            "provider": self.config.provider,
            "target_language": self.config.target_language,
            "source_language": self.config.source_language,
            "concurrency": self.config.concurrency,
            "mode": self.config.mode,
            "batch_size": self.config.batch_size,
            "unit_budget": self.config.unit_budget,
            "round_lines": self.config.round_lines,
            "batch_units": self.config.batch_units,
            "group_by": configured_level,
            "use_glossary": self.config.use_glossary,
            "use_worldbook": self.config.use_worldbook,
            "use_style": self.config.use_style,
            "use_knowledge": self.config.use_knowledge,
            "use_memory": self.config.use_memory,
            "retry_on_violation": self.config.retry_on_violation,
            "max_attempts": self.config.max_attempts,
            "require_structure": self.config.require_structure,
            "custom_instructions": self.config.custom_instructions,
            # 模板名在这里**解析成实际内容**：下游（layers/providers）拿到的是一份完整模板，
            # 不再自己去翻配置 —— 少一个"谁说了算"的分叉
            "prompt_template": prompts.active(
                self.config.prompt_template, self.config.prompt_templates
            ),
            # 名字另记一份给台账用（用户自定义/工坊模板时，只有名字是它真正的身份）
            "template_name": self.config.prompt_template,
        }
        payload.update({k: v for k, v in overrides.items() if v is not None})
        for key in ("context_layers", "unit_scope"):
            if payload.get(key) is not None:
                # 列表归一成元组：CLI / MCP 传进来的是 list，而 TranslateOptions
                # 按元组做校验与顺序归一
                payload[key] = tuple(payload[key])
        return TranslateOptions(**payload)

    # ---- 协议视图 ---------------------------------------

    def structure_scanner(self):
        """当前引擎的结构切分器；适配器没申报这个能力时返回 ``None``。"""
        pack = self.registry.get(self.engine)
        if not pack.supports("structure_scan"):
            return None
        return pack.structure_scanner()

    def unit_grouping(self):
        """当前引擎的分组器（翻译单元的中间那一级）；没申报任何层级时返回 ``None``。

        适配层申报"有哪些结构层级"，内核按申报的分组；内核不认识层级时**报错**，
        不静默退回"不分组"。

        **容器结构由内核给**（``PathGraph.level_paths``）：组键必须来自图里的容器树，
        不能来自"槽位缝上源码时才有"的提取元信息 —— 后者会让缝到 menu 行与缝不上的
        单元丢掉 label 归属，退化成一个文件里的一条自成一组（"一次请求只翻一句话"）。
        """
        pack = self.registry.get(self.engine)
        levels = pack.grouping_levels()
        if not levels:
            return None
        graph = self.graph

        def group(units, level: str) -> dict[str, str]:
            paths = graph.level_paths((level,)) if graph is not None else {}
            return pack.group_units(units, level, level_paths=paths)

        return group

    def default_grouping_level(self) -> str:
        """配置里写 ``group_by: "auto"`` 时，实际用哪一级：**适配层申报的首级**。

        Ren'Py 是 `label`、RPGM 是 `map` —— 也就是实测里质量最好、成本最低的那一级
        （整段一次调用）。适配层一级都没申报时返回空串（＝不分组）：那是"这个引擎给不出
        结构层级"，不是配置错误，所以这里不报错。
        """
        pack = self.registry.get(self.engine)
        levels = pack.grouping_levels()
        return levels[0] if levels else ""

    def export_target(self) -> ExportTarget:
        """当前引擎声明的导出契约。"""
        pack = self.registry.get(self.engine)
        return pack.export_target(self.project_root, self.config.target_language)

    def project_ir(self) -> ProjectIR | None:
        """把最近一次提取的结果整理成 Localization IR（没有提取结果时为 ``None``）。"""
        graph = self.graph
        if graph is None:
            return None
        pack = self.registry.get(self.engine)
        return ProjectIR.from_graph(
            graph,
            project_id=self.project_root.name,
            engine_id=self.engine,
            source_language=self.config.source_language,
            target_languages=[self.config.target_language],
            resources={
                "engine": pack.describe(),
                "translation": self.resources.summary(),
            },
        )

    # ---- 引擎选项（对内核不透明） -------------------------------------------

    #: 选项值的长度上限：它会被写进 project.json，也太长多半是粘错了
    MAX_ENGINE_OPTION_LENGTH = 4096

    def toolchain_status(self) -> dict[str, Any]:
        """当前引擎的外部工具链状态（由适配器回答）。"""
        return self.registry.get(self.engine).toolchain_status(dict(self.config.engine_options))

    def skeleton_status(self, *, language: str = "", include_files: bool = True) -> dict[str, Any]:
        """当前引擎"官方产物骨架"的现状（只读，由适配器回答）。

        内核不认识任何引擎的目录约定与文件格式 —— 它只把请求转给适配器、
        把回答原样报给控制面。
        """
        pack = self.registry.get(self.engine)
        payload = dict(
            pack.skeleton_status(
                self.project_root,
                dict(self.config.engine_options),
                language=language,
                include_files=include_files,
            )
        )
        payload.setdefault("engine", self.engine)
        return payload

    def language_facts(self, *, language: str = "") -> dict[str, Any]:
        """语言包事实（只读，由适配器回答）。

        内核不认识 `config.language`、`{font=...}`、`tl/<语言>/` 这些引擎语法，
        所以它只把请求转给适配器、把回答原样报给控制面。**这是事实面**：
        用哪个字体、语言入口怎么接，由 agent 拿主意。
        """
        pack = self.registry.get(self.engine)
        payload = dict(
            pack.language_facts(
                self.project_root,
                language=str(language or self.config.target_language or ""),
                options=dict(self.config.engine_options),
            )
        )
        payload.setdefault("engine", self.engine)
        return payload

    def update_engine_option(self, key: str, value: str) -> dict[str, Any]:
        """记一条引擎私有选项。

        内核**不解释**这些键值（``sdk_path`` 只是适配器约定的名字），它只负责：
        校验形状、落盘、把它转达给界面。这样加新引擎不需要动内核。
        """
        name = str(key or "").strip()
        text = str(value or "").strip()
        if not name:
            raise ConfigError(
                "引擎选项的名字不能为空",
                hint="例如 `gametrans engine option set sdk_path <SDK 目录>`。",
            )
        if len(text) > self.MAX_ENGINE_OPTION_LENGTH:
            raise ConfigError(
                f"引擎选项 {name} 的值太长（>{self.MAX_ENGINE_OPTION_LENGTH} 字符）",
                hint="多半是粘错了；这里只该填一个路径或一个短值。",
            )
        options = dict(self.config.engine_options)
        options[name] = text
        self.update_config(engine_options=options)
        return options

    def clear_engine_option(self, key: str) -> dict[str, Any]:
        """删掉一条引擎选项。

        注意 ``ProjectConfig.merged()`` 对 ``engine_options`` 是**并集**语义
        （给新键合并、不给的保留），所以"删除"要显式覆盖成删完的那一份 ——
        传空字典是删不掉的。
        """
        options = dict(self.config.engine_options)
        options.pop(str(key or "").strip(), None)
        merged = self.config.merged(engine_options=options)
        merged.engine_options = options
        self.config = merged
        self.save_config()
        return options

    def scan(self, **options: Any) -> ScanOutcome:
        report = RunReport(
            command="scan",
            project_root=str(self.project_root),
            engine=self.engine,
        )
        pack = self.registry.get(self.engine)
        stage = report.stage("extract")
        # 适配器要拿到目标语言（决定用哪份官方骨架）与引擎私有选项（怎么调官方工具）。
        # 内核不解释这些键，只负责转达 —— 加新引擎不需要动这里。
        options.setdefault("target_language", self.config.target_language)
        options.setdefault("engine_options", dict(self.config.engine_options))
        graph = pack.extract(
            ExtractContext(
                project_root=self.project_root,
                engine=self.engine,
                report=report,
                options=dict(options),
            )
        )
        # 提取完只留**引擎自己给的先后**（jump / call，`provenance=engine`）。
        # 以前这里会再叠一层启发式候选（共享说话人 / 专名 / 整句复用），实测没有效果：
        # 真实工程上它算出来的绝大多数是普通词（`term:Back`、`term:Such`），抽 30 条
        # 人工核查精确率 40%、`dependency` 类 0/15；而它会进知识指纹，噪声一多补译
        # 判定跟着抖。所以默认**不猜** —— 要它得显式给 `dependencies=True`。
        policy = policy_for(options)
        if policy is not None:
            candidates = policy.propose(graph)
            graph.dependencies.extend(candidates)
            stage.metrics["dependency_policy"] = policy.describe()
        else:
            stage.metrics["dependency_policy"] = {
                "name": "manual",
                "validated": True,
                "note": "不猜依赖边：只用引擎给的先后（jump / call）。",
            }
        # **agent 写下的边要保住**。`scan` 是从引擎重建整张图的，从前会把
        # `graph.depend` / `graph.knowledge` 写进去的边一起抹掉 —— 那是静默丢人的决定。
        # 只保 `provenance=agent` 的那些：引擎边不用保（引擎自己会重新给）。
        kept = self._agent_edges_from_disk()
        if kept:
            graph.dependencies.extend(kept)
            stage.metrics["agent_edges_kept"] = len(kept)
        # 依赖边必须落在**单元结点**上：图上一条文本就是一个翻译单元，"谁依赖谁"
        # 只能在单元之间说。引擎给的跳转边与上面算出来的候选边端点都可能是区域名，
        # 在这里统一投影一次 —— 实测不投影的话，402 条边里 382 条挂在区域上，
        # 46 个单元有 22 个一条边都没有。
        projected, self_loops = graph.project_dependencies_onto_units()
        if projected or self_loops:
            stage.metrics["edges_projected_onto_units"] = projected
            stage.metrics["edges_dropped_self_loop"] = self_loops
        # 章由项目申报（`<工作区>/chapters.json`）：盖戳只改单元自带的 `context.chapter`，
        # 没申报就一个都不盖 —— 章界是"读出来的事实/人工确认"，不是这里猜的。
        chaptered = apply_chapters(graph, self.workdir)
        if chaptered:
            stage.metrics["units_chaptered"] = chaptered
        stage.metrics.update(graph.stats())
        stage.finish(ok=report.ok)

        self._graph = graph
        self._save_graph(graph)
        self._save_report(report)

        dependencies = len(graph.dependencies)
        # 文案必须与事实一致：引擎给的先后（jump/call/顺序流）是**已确认**的，
        # 说成"方向未确认"会让人以为排序没生效（R73）。
        ordering = len(graph.ordering_dependencies())
        self.interaction.emit(
            "scan.summary",
            "内容提取完成",
            lines=[
                f"引擎：{self.engine}",
                f"可译条目 {graph.stats()['translatable']} 条",
                f"依赖边 {dependencies} 条（其中 {ordering} 条能定先后）"
                if dependencies
                else "",
                f"待确认 {len(report.unsupported)} 处",
            ],
            severity="warning" if report.unsupported else "success",
        )
        return ScanOutcome(graph=graph, report=report)

    def translate(self, **overrides: Any) -> TranslateOutcome:
        # 这是**写路径**：在这里做资源层的收尾（补缺文件、把早先落在 resources/ 里的
        # 校验清单搬一次）。**打开工程与只读查询一个字都不写** —— 那是一条既有红线。
        self.resources.ensure()
        report = RunReport(
            command="translate",
            project_root=str(self.project_root),
            engine=self.engine,
        )
        graph = self.graph
        if graph is None:
            scan_outcome = self.scan()
            graph = scan_outcome.graph
            report.stages.extend(scan_outcome.report.stages)
            report.unsupported.extend(scan_outcome.report.unsupported)

        options = self.translate_options(**overrides)
        # 参数一旦显式给出就写回配置，这样"agent 改过参数"这件事是持久的。
        # ``context_layers`` 与 ``unit_scope`` 刻意**排除在外**：它们是**这一次调用**的
        # 执行条件（消融档位 / 分块范围），不是项目的持久配置。写进去的后果：下一次不给
        # 参数时不再回落默认，档位与范围会粘住，两组实验条件悄悄串成一组。
        persistable = {
            k: v
            for k, v in overrides.items()
            if v is not None
            and k not in ("context_layers", "unit_scope")
            and k in TranslateOptions().to_dict()
        }
        if persistable:
            self.update_config(**persistable)

        stage = report.stage("translate")
        tasks: list[TranslationTask] = []
        declared: list[DeclaredTerm] = []
        transcript: list[dict[str, Any]] = []
        # **边翻译边产出**：每批跑完就把这一批的申报落成候选，并按"重复且一致"合并
        # （见 `_BatchKnowledge`）。关掉 produce_candidates 就整条通道都不走。
        knowledge = (
            _BatchKnowledge(self, min_batches=options.auto_approve_terms)
            if options.produce_candidates
            else None
        )
        records = self.translate_layer.run(
            graph,
            self.resources,
            options,
            report,
            interaction=self.interaction,
            scanner=self.structure_scanner(),
            grouping=self.unit_grouping(),
            project_id=self.project_root.name,
            tasks_out=tasks,
            terms_out=declared,
            transcript_out=transcript,
            batch_hook=_batch_knowledge_hook(knowledge) if knowledge else None,
            # 步骤提醒要用它数"还有几个单元没事件卡"。它只进提醒，不进请求。
            summaries=load_summaries(self.workdir),
            # 步骤提醒还要知道"项目申报的事实"（章界）带进工作区了没有。
            workdir=self.workdir,
        )
        if transcript:
            # **调用流水**：每次调用的请求原文 + 服务端回复原样落盘。
            # 报告里只有 token 数，复盘实验时不够 —— 得能逐字看见发了什么、回了什么。
            self._append_transcript(transcript, report)
        stage.metrics.update(
            {
                "records": len(records),
                "ok": sum(1 for r in records if r.is_usable),
                "failed": sum(1 for r in records if not r.is_usable),
                "provider": options.provider,
                "tasks": len(tasks),
            }
        )
        # 放行的"写法调整"要留痕：逐条细节在译文记录里（每条自带的校验结论），
        # 报告里给一个总数 + 一条提醒，免得"静默放行"变成没人知道发生了什么。
        adjusted = [
            record
            for record in records
            if record.validation is not None
            and record.validation.ok
            and record.validation.warnings
        ]
        if adjusted:
            stage.metrics["structure_adjusted"] = len(adjusted)
            report.add_issue(
                "warnings",
                code="structure_rewrite_released",
                message=(
                    f"{len(adjusted)} 条译文把受保护标记的写法做了本地化调整"
                    "（身份没变，已放行；逐条见译文记录里的校验结论）"
                ),
                detail={"count": len(adjusted), "samples": [r.unit_id for r in adjusted[:20]]},
            )
        stage.finish(ok=report.ok)

        # 状态先落盘再返回：中断/复跑时，磁盘上的状态就是这一轮的真实结果。
        # **只翻了一部分就要合并落盘**，否则这一轮的记录会把上一轮的整份抹掉：
        # 分块跑（`--unit-scope`）与**阶段切分**（`--start-phase` / `--stop-after-phase`）
        # 都算"只翻了一部分" —— 后者原先漏了，于是"跑一段、停下看看、再接着跑"这条
        # 玩法第一步就把账清了。
        self.tasks.save_many(tasks)
        self._save_translations(
            records,
            merge=(
                options.unit_scope is not None
                or options.start_phase > 1
                or bool(options.stop_after_phase)
            ),
        )
        # **边翻译边产出**：跑完把这一轮的译文变成资产。
        #
        # 只用**一条**通道：模型在响应里**当场申报**的专名/术语（覆盖每一句，段内就够用）。
        # 旧的"整份译文里重复且一致"那条判据已经从这里去掉 —— 它是离线全量抽术语的判据，
        # 在一次翻译请求（一个段）里几乎必然全落空，实测 38 组里 33 组因"只出现一次"被拦，
        # 剩下的净是纯标点与整句。
        #
        # 落盘与合并不在这里：它们**每批跑完**就在 `_BatchKnowledge` 里做了（见那里的说明）。
        # 这里只负责记账 —— 跑完之后把这一轮的结果如实报出来。
        if knowledge is not None:
            self._report_batch_knowledge(knowledge, report, declared)
        # **对应闸那一格永远在**（见 §19）。命中才出现的读数，前端只能靠"有没有这个键"
        # 判空 —— 于是名字一改就静默显示 `—`，而那恰恰是给"最难发现的那类错"用的读数。
        # 0 与"跑了但没命中"是同一个意思，如实写出来。
        report.metrics["correspondence_failed_records"] = int(
            report.metrics.get("correspondence_failed_records") or 0
        )
        self._save_report(report)
        return TranslateOutcome(records=records, report=report)

    def _report_batch_knowledge(
        self,
        knowledge: "_BatchKnowledge",
        report: RunReport,
        declared: list["DeclaredTerm"],
    ) -> None:
        """把"边跑边积累"这一轮的账报清楚：新开多少行、撞了什么车、提了几条更正。

        自动写入**必须出声**：它是机器写的（`by=agent`）。判据、门槛、写进去哪几条
        都要在报告里一次看全，否则"模型申报→自动生效"与"人写进去"在事后看起来一模一样。
        """
        created = knowledge.created
        plan = knowledge.latest
        report.metrics["candidates_created"] = len(created)
        report.metrics["declared_terms"] = len(declared) or len(knowledge.declared)
        # 一行两栏：带译名的行数 = 术语面；带事实的行数 = 设定面（同一行可能两边都算）
        report.metrics["terms_declared"] = sum(
            1 for item in declared if str(item.target).strip()
        )
        report.metrics["profiles_declared"] = sum(
            1 for item in declared if str(item.profile).strip()
        )
        # 同一个词又说了一遍事实：免审追加进去的条数（不是丢，是并）
        report.metrics["profiles_merged"] = knowledge.profiles_merged
        # **模型又说了一遍书上已经写过的事**。两类分开报：
        # `same_fact` 吞掉的（近乎逐字）与"换个说法"的（会进待审）—— 合起来就是这个事故的读数。
        repeated = knowledge.repeated_facts
        near = knowledge.near_duplicate_facts
        report.metrics["declared_facts_repeated"] = repeated
        report.metrics["declared_facts_near_duplicate"] = near
        if repeated + near >= 3:
            report.add_issue(
                "warnings",
                code="declared_facts_already_known",
                message=(
                    f"这一轮模型申报的设定里，{repeated + near} 条是**书上已经写过的事实**"
                    f"（{repeated} 条近乎逐字、被当重复吞掉；{near} 条换了个说法、进了待审队列）。"
                    "翻译的请求里本来就摆着这些事实 —— 数量偏高时先看【术语书】那一段的形状"
                    "与提示词第 7、8 条（`- 设定｜写法：…` 是已定过的东西，不必再申报一遍）"
                ),
                detail={"repeated": repeated, "near_duplicate": near},
            )
        report.metrics["auto_approve_terms"] = knowledge.min_batches
        report.metrics["terms_auto_approved"] = len(knowledge.approved)
        # "满足判据、可以写进书"的条数 —— 与"已经自动写掉的"分开报：
        # 门槛 0（全交给人）时前者非零、后者恒 0，这正是"等你看"的意思
        report.metrics["terms_ready"] = len(plan.approve)
        report.metrics["terms_waiting"] = len(plan.waiting)
        report.metrics["terms_dropped"] = len(plan.dropped)
        report.metrics["terms_conflicts"] = len(plan.conflict)
        report.metrics["terms_settled"] = len(plan.settled)
        report.metrics["term_changes_pending"] = knowledge.pending_proposals
        # 译名那一栏抄回来的是标签本身：**没落笔**的条数。它不出现在任何"写了几行"里，
        # 不点名就只剩"这个名字还没定译"这半句，看不出模型试过而机制挡下了。
        tagged_targets = list(knowledge.tagged_targets)
        report.metrics["terms_targets_tagged"] = len(tagged_targets)
        if tagged_targets:
            report.add_issue(
                "warnings",
                code="terms_targets_were_tags",
                message=(
                    f"模型有 {len(tagged_targets)} 条申报把**标签本身**（⟦写法⟧）当成了译名："
                    "这些没有落笔，名字仍然没定（写法照旧按 ⟦写法⟧ 送出去，"
                    "写回那一刻还没译名就不写回、并点名）。要定名就那一行上直接填"
                ),
                detail={"writings": tagged_targets[:20]},
            )
        if knowledge.approved:
            report.add_issue(
                "warnings",
                code="terms_auto_approved",
                message=(
                    f"由 agent 身份写进术语书 {len(knowledge.approved)} 条：判据 = 同一个"
                    f"原文在**不同的 {knowledge.min_batches} 批**里各自被申报过一次且译名一致"
                    "（每批跑完合并一次，后面的批已经用上了）。要推翻就走"
                    " `resource.term.pending.*` 或直接改那一行"
                ),
                detail={
                    "threshold": knowledge.min_batches,
                    "approved": [
                        {"source": source, "target": target, "groups": groups}
                        for source, target, groups in knowledge.approved[:20]
                    ],
                },
            )
        elif knowledge.min_batches < 2 and plan.approve:
            report.add_issue(
                "warnings",
                code="terms_ready_for_review",
                message=(
                    f"有 {len(plan.approve)} 条术语满足「重复且一致」（跨 ≥2 批）："
                    "本次**没有**自动写入（auto_approve_terms=0），等你或 agent 拍板"
                ),
                detail={
                    "ready": [
                        {"source": source, "target": target, "groups": groups}
                        for source, target, groups in plan.approve[:20]
                    ]
                },
            )
        if plan.conflict:
            report.add_issue(
                "warnings",
                code="term_conflicts",
                message=(
                    f"有 {len(plan.conflict)} 个原文被申报成了**多种译名** —— 先落第一种，"
                    "其余进了待审更正队列，要人或 agent 采用/丢弃"
                    "（`resource.term.pending.list` 看队列；统一替换按采用之后的变更日志走）"
                ),
                detail={"conflicts": [item.to_dict() for item in plan.conflict[:20]]},
            )
        if created:
            report.add_issue(
                "warnings",
                code="term_candidates_produced",
                message=(
                    f"模型这一轮申报了 {report.metrics['declared_terms']} 个实体"
                    f"（带译名 {report.metrics['terms_declared']}、带事实 "
                    f"{report.metrics['profiles_declared']}），"
                    f"写进术语书 {len(created)} 行（已经是生效的一行：有译名或事实就注入）"
                ),
                detail={
                    "samples": [c["id"] for c in created[:20]],
                    "declared": [
                        {"source": item.source, "target": item.target, "profile": item.profile}
                        for item in declared[:20]
                    ],
                },
            )
        settings_only = knowledge.settings_only()
        # 门槛 0（"我自己来"）时 `plan.approve` 非空而一条都没写 —— 那份清单正是
        # 要看的东西，所以它也算"有账可报"。
        if (
            created
            or plan.approve
            or plan.conflict
            or plan.waiting
            or plan.dropped
            or settings_only
            or tagged_targets
        ):
            self._write_candidate_review(
                created,
                report,
                conflicts=plan.conflict,
                plan=plan,
                settings_only=settings_only,
                tagged_targets=tagged_targets,
            )

    def _append_transcript(
        self, transcript: list[dict[str, Any]], report: RunReport
    ) -> None:
        """把调用流水写进 `reports/<run>-calls.jsonl`（一次调用一行，可逐字复盘）。"""
        directory = self.workdir / "reports"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{report.run_id}-calls.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for entry in transcript:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        report.metrics["calls_transcript"] = str(path)
        report.metrics["calls_logged"] = len(transcript)

    def source_slots(self) -> list[str]:
        """全工程的**原文槽位**清单（句一级）。

        用来量"某条设定的写法会在多少句上命中" —— 这是"这条设定会在多大范围里说话"
        的唯一实测口径。槽位取自单元定位信息；没有槽位清单的老单元整条算一句。
        """
        graph = self.graph
        if graph is None:
            return []
        slots: list[str] = []
        for node in graph.translatable_nodes():
            unit = node.unit
            if unit is None:
                continue
            payload = getattr(getattr(unit, "locator", None), "payload", None) or {}
            entries = payload.get("slots") or []
            if not entries:
                slots.append(unit.source)
                continue
            for entry in entries:
                slots.append(str(entry.get("source") or ""))
        return slots

    def _write_candidate_review(
        self,
        created: list[dict[str, Any]],
        report: RunReport,
        conflicts: list[Any] = (),
        plan: Any = None,
        settings_only: list[tuple[str, list[str]]] = (),
        tagged_targets: list[str] = (),
    ) -> None:
        """把这一轮的账写成一份**给人过目**的清单（`reports/<run>-candidates.md`）。

        为什么单独写一份：机器写进去的东西得有人看才知道对不对（判据是"跨批一致"，
        不是"人看过"）。埋在 `termbook.jsonl` 里等于没人看；GUI 又要另开服务器。
        一份 markdown 里给出写法、各自的译名、事实、以及采用/丢弃的命令。

        **没写进去的也要列出来**（R63）：还差证据的（``waiting``）与判为普通词的
        （``dropped``）都在这里点名 —— 只报"有冲突"或"写了几行"，看的人就分不清
        "模型没申报"与"申报了但被挡下"。

        设定那一栏另外给一个数：**它的写法会在真靶多少句上命中**。命中 2 句和命中 900 句
        是两种完全不同的东西 —— 这是"这条设定会在多大范围里说话"的唯一实测口径。

        ``conflicts`` 是**同一原文多种译名**的那些。看的人要知道"每个写法各出现在哪些
        批里"。
        """
        path = self.workdir / "reports" / f"{report.run_id}-candidates.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        slots = self.source_slots()
        hits = self.resources.profile_hit_counts(slots) if slots else {}
        broad = max(20, len(slots) // 10) if slots else 0
        pending = self.resources.pending_corrections()
        waiting = list(getattr(plan, "waiting", ()) or ())
        dropped = list(getattr(plan, "dropped", ()) or ())
        ready = list(getattr(plan, "approve", ()) or ()) if not created else []
        lines = [
            f"# 这一轮写进术语书的行：{len(created)} 行",
            "",
            f"运行 `{report.run_id}`　工程 `{self.project_root.name}`　目标语言 `{self.config.target_language}`",
            "",
            "**这些已经生效**（有译名或有事实就会注入）。判据是「同一个原文在**不同的批**里"
            "各自被申报过一次且译名一致」，是机器写的，所以列在这里给你过目。",
            "",
            "术语那一栏带**语料判据**（`layers/naming.py`）：判 `termhood:drop` 的"
            "（出现过全小写形态的普通词，`play` / `coach` 那类）**根本没写进来**。",
            "",
            f"| # | 写法 → 译名 | 事实 | 设定写法命中（全靶 {len(slots)} 句） |",
            "|---:|---|---|---|",
        ]
        for index, row in enumerate(created, 1):
            # 这一行是**盘上的真值**：`created` 只是"这一批写了哪几条"的收据，
            # 事实可能在后面的批里又追加过（免审追加）。
            entry = self.resources.termbook.get(str(row.get("content") or ""))
            writing = str(row.get("content") or "")
            if entry is not None:
                writings = "、".join(
                    f"`{name}` → `{target or '—'}`" for name, target in entry.targets
                ) or f"`{writing}`"
                facts = "；".join(entry.facts)
            else:
                writings = f"`{writing}`"
                facts = str(row.get("profile") or "")
            reach = "—"
            if facts:
                count = hits.get(writing, 0)
                reach = f"{count} 句"
                if count == 0:
                    reach += "　⚠ 打不响"
                elif broad and count >= broad:
                    reach += f"　⚠ 太宽（≥{broad}）"
            lines.append(f"| {index} | {writings} | {facts or '—'} | {reach} |")
        lines += [
            "",
            "## 怎么处置",
            "",
            "```",
            "# 看全部（含待审更正）",
            f"python -m gametrans --project {self.project_root} resource.term.list",
            "",
            "# 直接改一行（人拍的板，直接生效；`key` 是写法列表、`profile` 一行一条事实）",
            f"python -m gametrans --project {self.project_root} resource.term.add <写法> <译名>",
            "",
            "```",
            "",
        ]
        if ready:
            # 门槛 0（"我自己来"）：判据够了但一条都没自动写 —— 这一节就是"等你看"
            lines += [
                f"## 够判据、等你拍板：{len(ready)} 条（`auto_approve_terms=0`）",
                "",
                "写进去就生效。要哪一条就自己写一行（`resource.term.add <写法> <译名>`）。",
                "",
                "| 原文 | 译名 | 在哪些批里这么用过 |",
                "|---|---|---|",
            ]
            for source, target, groups in ready:
                lines.append(f"| `{source}` | `{target}` | {'、'.join(str(g) for g in groups)} |")
            lines.append("")
        if waiting:
            # 只见过一批：跨批判据还没给够，先不写进书 —— **但必须点名**，
            # 否则"模型没申报"与"申报了在等第二份证据"看起来一样（R63）。
            lines += [
                f"## 还差一份证据：{len(waiting)} 条（同一个原文只在 1 批里出现过）",
                "",
                "| 原文 | 译名 | 在哪些批里这么用过 |",
                "|---|---|---|",
            ]
            for source, target, groups in waiting:
                lines.append(f"| `{source}` | `{target}` | {'、'.join(str(g) for g in groups)} |")
            lines.append("")
        if dropped:
            # 语料判据说"不像专名"：不写进书，理由照抄出来
            lines += [
                f"## 被语料判据挡下：{len(dropped)} 条（出现过全小写形态的普通词）",
                "",
                "判据只给建议。不同意就自己写一行（`resource.term.add`），那是拍板动作。",
                "",
                "| 原文 | 译名 | 在哪些批里这么用过 |",
                "|---|---|---|",
            ]
            for source, target, groups in dropped:
                lines.append(f"| `{source}` | `{target}` | {'、'.join(str(g) for g in groups)} |")
            lines.append("")
        if tagged_targets:
            # 模型把标签当名字交回来了：**没落笔**。不点名的话，"模型试过、机制挡下"
            # 与"模型压根没申报"在事后看起来一样 —— 那正是这份清单存在的理由。
            lines += [
                f"## 把标签当名字交回来了：{len(tagged_targets)} 条（没落笔）",
                "",
                "这些申报的「译名」就是 `⟦写法⟧` 本身 —— 标签是「还没定译」的记号，不是名字，",
                "所以一条都没写进书：写法照旧包标签送出去，名字仍然等人或 agent 定。",
                "要定名就直接在那一行上填（或在面板术语书页填）。",
                "",
                "| 写法 |",
                "|---|",
            ]
            for writing in tagged_targets:
                lines.append(f"| `{writing}` |")
            lines.append("")
        if settings_only:
            # 只申报了事实：跨批判据比的是**译名**，没有译名就没有可比对的东西
            lines += [
                f"## 只申报了事实：{len(settings_only)} 条（没有译名，跨批判据比不了）",
                "",
                "先补一个译名（`resource.term.add <写法> <译名>`），或者等模型在别的批里给出译名。",
                "",
                "| 原文 | 事实 |",
                "|---|---|",
            ]
            for source, facts in settings_only:
                lines.append(f"| `{source}` | {'；'.join(facts)} |")
            lines.append("")
        if pending:
            lines += [
                f"## 待审更正：{len(pending)} 条（改已有的译名 / 事实）",
                "",
                "这些**还没有生效** —— 采用才替换。`writing` 是哪一个写法（事实那一栏是行身份），",
                "`index` 是列表里的第几条。",
                "",
                "| id | 栏位 | 写法 | # | 原来 | 改成 | 为什么 |",
                "|---|---|---|---:|---|---|---|",
            ]
            for record in pending:
                lines.append(
                    f"| `{record.get('id')}` | {record.get('what')} "
                    f"| `{record.get('writing')}` | {record.get('index')} "
                    f"| {record.get('old') or '—'} | {record.get('new') or '—'} "
                    f"| {record.get('why') or '—'} |"
                )
            lines += [
                "",
                "```",
                f"python -m gametrans --project {self.project_root} resource.term.pending.list",
                f"python -m gametrans --project {self.project_root} resource.term.pending.adopt <id> --by human",
                f"python -m gametrans --project {self.project_root} resource.term.pending.drop <id>",
                "```",
                "",
            ]
        if conflicts:
            # 撞车：先落了第一种，其余都在待审队列里（这一节说明它们是怎么来的）
            lines += [
                "## 撞车：同一个原文不止一种译名",
                "",
                "先落了**第一种**（已生效），其余成了上面那几条待审更正 ——"
                "要改用哪一种，采用那一条即可。",
                "",
                "| 原文 | 译名 | 在哪些批里这么用过 |",
                "|---|---|---|",
            ]
            for conflict in conflicts:
                targets = conflict.targets if hasattr(conflict, "targets") else conflict["targets"]
                source = conflict.source if hasattr(conflict, "source") else conflict["source"]
                for target, groups in targets.items():
                    lines.append(
                        f"| `{source}` | `{target}` | {'、'.join(str(g) for g in groups)} |"
                    )
            lines.append("")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report.metrics["candidates_review"] = str(path)

    def record_declared_terms(
        self,
        triples: list["DeclaredTerm"],
        *,
        verdicts: dict[str, tuple[bool, str]] | None = None,
        approved: Any = None,
    ) -> list[dict[str, Any]]:
        """把模型申报的**实体**（原文写法 → 译名 / 事实）并进术语书。

        写入走 `TermBook.apply`：**新增（新写法 / 空译名 / 新事实）免审追加，改已有的
        译名进待审队列**。所以这条通道自己不再产"候选"，只产"行"与"提案"。

        ``approved`` 是**允许新开行**的原文写法集合（跨批合并的结论）。给 ``None`` =
        全部允许；给空集 = 只并已有的行，一条新行都不开（这一轮先用它并已有）。

        写之前先过一遍**语料判据**（`layers/naming.py`）：真靶 181 条申报里混着
        `play`（小写形态 213 次）、`coach`(164)、`player`(61) 这类普通词，判"不保留"的
        一律不写进书。

        ``verdicts`` 是已经判过的意见（分批落盘时由调用方按**整轮**缓存传进来）：
        不传就现算这一批。
        """
        self._last_profile_merged = 0
        self._last_tagged_targets = []
        self._last_repeated_facts = 0
        self._last_near_duplicate_facts = 0
        if not triples:
            return []
        items = [_coerce(item) for item in triples]
        update = KnowledgeUpdate(self.resources.termbook, min_occurrences=2)
        judged = verdicts if verdicts is not None else self.judge_terms(items)
        created = [
            item.to_dict()
            for item in update.absorb_declared(items, verdicts=judged, approved=approved)
        ]
        self._last_profile_merged = update.merged_profiles
        self._last_tagged_targets = list(update.tagged_targets)
        self._last_repeated_facts = update.repeated_facts
        self._last_near_duplicate_facts = update.near_duplicate_facts
        return created

    def judge_terms(
        self, triples: list["DeclaredTerm"]
    ) -> dict[str, tuple[bool, str]]:
        """用**语料证据**判一批申报（见 `layers/naming.py`）。语料只算一次，按图缓存。"""
        corpus = self.term_corpus()
        return judge_terms(
            [(item.source, item.target) for item in (_coerce(t) for t in triples)],
            corpus,
        )

    def term_corpus(self) -> CorpusEvidence:
        """整份原文语料 + 引擎自己说的人物名（按图缓存：真靶上算一次要扫 15k 个槽位）。"""
        cached = getattr(self, "_term_corpus_cache", None)
        if cached is not None and cached[0] is self.graph:
            return cached[1]
        corpus = corpus_evidence_from_graph(self.graph) if self.graph is not None else CorpusEvidence()
        self._term_corpus_cache = (self.graph, corpus)
        return corpus

    def revalidate_translations(self) -> dict[str, Any]:
        """按**当前**判据重新裁定当初被闸门挡下的译文。**只放行，不收紧。**

        判据会变（这次就变了：把合法的手艺从"违规"改成"放行"）。判据一变，存在
        `translations.jsonl` 里的旧结论就过期了 —— 不改它，那些译文永远写不回去，
        而报告会一直说是它们有问题。所以给一条显式通路：重新算一遍、如实报出
        哪几条从"待复核"变成"可用"。

        只处理 `needs_review` 这一类：别的状态（没译文 / 模型没答）不是判据造成的，
        重新裁定它们等于改口径掩盖事实。
        """
        graph = self.graph
        if graph is None:
            raise WriteBackError(
                "还没有提取结果，无法重新裁定",
                hint="先运行 `gametrans scan`。",
            )
        units = {
            node.unit.id: node.unit for node in graph.nodes.values() if node.unit is not None
        }
        # 译文按**槽位**记账（一个单元含 N 句就有 N 条），所以记录的键既可能是单元 id，
        # 也可能是它自己的槽位键 —— 两种都要能找回单元。只认单元 id 的话，逐句落盘的
        # 那一类记录会被**静默跳过**（真靶实测：`act25` 1,963 条一条都不看，报告说
        # "checked: 0"，看着像"没有要重算的"）。
        unit_of_slot: dict[str, str] = {}
        for unit in units.values():
            for key in unit.metadata.get("slot_keys") or []:
                unit_of_slot.setdefault(str(key), unit.id)
        scanner = self.structure_scanner()
        records = self.translations()
        released: list[dict[str, Any]] = []
        still_blocked: list[dict[str, Any]] = []

        for record in records:
            if record.status is not TranslationStatus.NEEDS_REVIEW:
                continue
            unit = units.get(record.unit_id)
            if unit is None and record.unit_id in unit_of_slot:
                unit = units.get(unit_of_slot[record.unit_id])
            if unit is None:
                continue
            result = validate(
                # 记录是按**槽位**记账的，标尺也得是这一句自己的原文（同一个单元里
                # 别的句子的占位符不算在这一句头上）。
                slot_gauge(unit, record.unit_id, scanner),
                record.translated_text,
                scanner=scanner,
                approved=self.resources.deviations.by_unit().get(record.unit_id, ()),
            )
            if result.ok:
                record.status = TranslationStatus.OK
                record.error = None
                released.append(
                    {
                        "unit_id": record.unit_id,
                        "source": record.source,
                        "adjusted": [v.message for v in result.warnings],
                    }
                )
            else:
                still_blocked.append(
                    {"unit_id": record.unit_id, "errors": [v.message for v in result.errors]}
                )
            record.validation = result

        if released or still_blocked:
            self._save_translations(records)
        return {
            "checked": len(released) + len(still_blocked),
            "released": released,
            "still_blocked": still_blocked,
        }

    # ---- 人工编辑与复核 -----------------------------------------------------

    def unify_translations(
        self, *, by: str = "human", source: str = "", dry_run: bool = False
    ) -> dict[str, Any]:
        """**统一替换**：把已落盘译文里"输掉的那个写法"换成现在的定译（R67 的执行）。

        来历：一轮跑下来同一个名字会长出两种写法（真靶重放读数 R83：46 次调用里 27 个
        原文撞车 —— `Lexi` 莱克西×18 / 蕾克西×4、`Saki` 沙希×14 / 咲×8）。裁决
        （`resource.term.pending.adopt` 采用一条更正）解决的是"以后用哪个"；**已经写
        下去的旧写法得有人去改** —— 纯机械的查找替换，按 R67 的口径由软件做。

        四条边界（少一条就会变成"悄悄改了译文"）：

        * **替换源只来自变更日志的差异**：只换 ``termbook.changes.jsonl`` 里记过的、
          被换掉的那些译名（``old``），不做全局正则替换 —— 软件不认识语义；
        * **换完重过结构校验**：过不了就**不写**那一条，如实列进 ``rejected``
          （为了"替换成功"绕过校验比不替换更糟）；
        * **每一处改动留痕**：旧值、新值、哪条术语、谁批的，进那条记录的 ``metadata``
          （译文账里查得到），报告里给总数；
        * **谁批谁拍板**：``by`` 只认 human / user / agent，模型身份被拒（同采用那一道门）。

        ``source`` 给了就只处理那一条术语（复核的时候用）；``dry_run`` 只算不写。
        """
        identity = str(by or "").strip().lower()
        if identity not in APPROVERS:
            raise KnowledgeError(
                f"审核身份 {by!r} 不能做统一替换（只认 {', '.join(sorted(APPROVERS))}）",
                hint="替换会改已落盘的译文，和采用更正一样必须由人或 agent 拍板。",
            )
        graph = self.graph
        if graph is None:
            raise WriteBackError(
                "还没有提取结果，没有可以替换的译文",
                hint="先运行 `gametrans scan`。",
            )
        units = {
            node.unit.id: node.unit for node in graph.nodes.values() if node.unit is not None
        }
        # 记录按**槽位**记账，标尺也得能按槽位键找回单元（与 revalidate 同一条口径）
        unit_of_slot: dict[str, str] = {}
        for unit in units.values():
            for key in unit.metadata.get("slot_keys") or []:
                unit_of_slot.setdefault(str(key), unit.id)

        plan: list[tuple[str, str, list[str]]] = []
        for writing, revision in self.resources.term_revisions().items():
            if source and writing != source:
                continue
            target = str(revision.get("target") or "")
            alternatives = [str(text) for text in (revision.get("lost") or [])]
            if target and alternatives:
                plan.append((writing, target, alternatives))

        scanner = self.structure_scanner()
        approved = self.resources.deviations.by_unit()
        records = self.translations()
        changed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        if plan:
            for record in records:
                text = record.target
                hits = [
                    (term, target, alternative)
                    for term, target, alternatives in plan
                    for alternative in alternatives
                    if alternative and alternative in text
                ]
                if not hits:
                    continue
                unit = units.get(record.unit_id) or units.get(
                    unit_of_slot.get(record.unit_id, "")
                )
                if unit is None:
                    # 找不回单元的记录不许动：没有标尺就证明不了换完还是好的
                    rejected.append(
                        {
                            "unit_id": record.unit_id,
                            "reason": "这条记录对不上任何单元（换了没法过校验）",
                        }
                    )
                    continue
                # 先在**副本**上换：校验不过就整条不动，绝不半途写进去
                segments = [replace(segment) for segment in record.translated_segments]
                for segment in segments:
                    value = segment.value
                    for _term, target, alternative in hits:
                        value = value.replace(alternative, target)
                    segment.value = value
                candidate = replace(record, translated_segments=segments)
                result = validate(
                    slot_gauge(unit, record.unit_id, scanner),
                    candidate.translated_text,
                    scanner=scanner,
                    approved=approved.get(record.unit_id, ()),
                )
                if not result.ok:
                    rejected.append(
                        {
                            "unit_id": record.unit_id,
                            "reason": result.errors[0].message if result.errors else "校验没过",
                            "would_be": candidate.translated_text,
                        }
                    )
                    continue
                metadata = dict(record.metadata or {})
                metadata["unified"] = [
                    *(metadata.get("unified") or []),
                    *[
                        {
                            "term": term,
                            "from": alternative,
                            "to": target,
                            "by": identity,
                            "at": _now_iso(),
                        }
                        for term, target, alternative in hits
                    ],
                ]
                # 试跑**不许留痕**：这些记录对象就是本次会话的当前账，改在它们身上
                # 等于"没落盘但已经生效"——随后任何一次写回都会把试跑的改动带出去
                if not dry_run:
                    record.translated_segments = segments
                    record.validation = result
                    record.status = TranslationStatus.OK
                    record.error = None
                    record.metadata = metadata
                changed.append(
                    {
                        "unit_id": record.unit_id,
                        "term": hits[0][0],
                        "replacements": [
                            {"from": alternative, "to": target}
                            for _term, target, alternative in hits
                        ],
                    }
                )
        if changed and not dry_run:
            self._save_translations(records)
        return {
            "terms": [
                {"source": term, "target": target, "alternatives": alternatives}
                for term, target, alternatives in plan
            ],
            "changed": len(changed),
            "details": changed,
            "rejected": rejected,
            "dry_run": bool(dry_run),
            "written": bool(changed) and not dry_run,
        }

    def unit_by_id(self, unit_id: str) -> TranslationUnit:
        """按 unit_id 找回翻译单元；没有提取结果或对不上就报错，不猜。

        记录是按**槽位**记账的（一个单元含多句时逐句一条），所以槽位键也认：
        面板手改/复核拿到的就是槽位键，它得能找回这条槽位所属的单元 ——
        结构闸门要拿整个单元当标尺。
        """
        graph = self.graph
        if graph is None:
            raise WriteBackError(
                "还没有提取结果，没有可改的译文",
                hint="先运行 `gametrans scan`。",
            )
        for node in graph.nodes.values():
            if node.unit is not None and node.unit.id == unit_id:
                return node.unit
        for node in graph.nodes.values():
            unit = node.unit
            if unit is None:
                continue
            if unit_id in (unit.metadata.get("slot_keys") or []):
                return unit
        raise WriteBackError(
            f"路径图里没有这个翻译单元：{unit_id!r}",
            hint="unit_id 可能来自更早的一次 scan；重新 scan 后再看。",
        )

    def save_translation(
        self, unit_id: str, target: str, *, agent: str = "panel"
    ) -> TranslationArtifact:
        """改一条已有译文，并**重走同一道结构闸门**。

        面板 / agent 手改不是后门：这里用的就是 translate 层当初用的那个 ``validate``
        （结构由适配器的 scanner 切分、核心只做比对）。所以"面板改出来的译文绕过了
        校验、到写回时才炸"不会发生 —— 不过闸门的当场标成 ``needs_review``，而写回层
        只认 ``is_usable``。

        引擎没申报结构切分器时**一律不判可用**：那种情况下占位符 / 标签 / 控制码这些
        约束根本没进 ``checked``，签"校验通过"等于撒谎。

        改出来的字**照样落盘**：用户写的东西不该因为没过闸门就被丢掉 —— 状态才是闸门。
        """
        unit = self.unit_by_id(unit_id)
        records = self.translations()
        record = next((r for r in records if r.unit_id == unit_id), None)
        if record is None:
            raise WriteBackError(
                f"这条单元还没有译文记录：{unit_id!r}",
                hint="面板改的是已有的译文；从无到有的初翻请用 `gametrans translate`。",
            )

        scanner = self.structure_scanner()
        segments = scanner(target) if scanner is not None else []
        if not segments:
            segments = [Segment(SegmentKind.TEXT.value, target, translatable=True)]
        # 闸门按**这一条**当标尺：记录是按槽位记账的，拿整个单元的原文去比一句译文，
        # 结构必然对不上（占位符数量不一样）。所以要拿这条槽位自己的原文来判。
        gauge = self._slot_gauge(unit, unit_id)
        result = validate(gauge, target, scanner=scanner)

        record.translated_segments = segments
        record.validation = result
        unverified = _unverified_structure(unit, result)
        if unverified:
            record.status = TranslationStatus.NEEDS_REVIEW
            record.error = unverified
        elif result.ok:
            record.status = TranslationStatus.OK
            record.error = None
        else:
            record.status = TranslationStatus.NEEDS_REVIEW
            record.error = "; ".join(v.message for v in result.errors) or "结构校验未通过"

        previous_agent = record.provenance.agent
        record.provenance.agent = agent
        record.provenance.metadata["edited_at"] = _now_iso()
        if previous_agent:
            record.provenance.metadata["edited_from"] = previous_agent

        self._save_translations(records)
        return record

    def _slot_gauge(self, unit: Any, key: str) -> Any:
        """手改一条译文时，闸门要拿**这条槽位自己**的原文当标尺。

        实现搬到内核契约层了（:func:`gametrans.core.constraints.slot_gauge`）——
        翻译层逐句落盘时要用同一条规矩，两处各写一份就是"同一套判据写两遍"。
        """
        return slot_gauge(unit, key, self.structure_scanner())

    def review_translation(self, unit_id: str, action: str) -> TranslationArtifact:
        """收件箱的通过 / 打回：**只改状态，不改内容**。

        "通过"翻不过校验那一关：``is_usable`` 是"状态 + 非空 + 校验通过"三者的合取，
        所以人工点通过不会把结构不合格的译文放进写回 —— 闸门不是投票。
        """
        if action not in ("approve", "reject"):
            raise ConfigError(
                f"不认识的复核动作：{action!r}",
                hint="只能是 approve（通过）或 reject（打回）。",
            )
        records = self.translations()
        record = next((r for r in records if r.unit_id == unit_id), None)
        if record is None:
            raise WriteBackError(
                f"没有这条译文：{unit_id!r}",
                hint="用 `gametrans tasks` 或面板收件箱看看有哪些条目。",
            )
        if action == "approve":
            # 复核通过前**按这条槽位自己的原文**重算一次校验：记录是逐槽位的，而当初的
            # 校验结论可能来自"整个单元"那把标尺（几百句拼起来），那样一票通过就永远
            # 翻不过 `is_usable` —— 人工点"通过"也救不回来。
            unit = self.unit_by_id(unit_id)
            gauge = self._slot_gauge(unit, unit_id)
            record.validation = validate(
                gauge, record.target, scanner=self.structure_scanner()
            )
            record.status = TranslationStatus.OK
            record.error = None
        else:
            record.status = TranslationStatus.NEEDS_REVIEW
            record.error = record.error or "人工复核打回"
        self._save_translations(records)
        return record

    def writeback(self, **options: Any) -> WriteBackOutcome:
        # 写路径：收尾资源层（声明的补充条目就住在这条路上，见 1.4.1 的 C 层）
        self.resources.ensure()
        graph = self.graph
        if graph is None:
            raise WriteBackError(
                "还没有提取结果，无法写回",
                hint="先运行 `gametrans scan`（或直接 `gametrans translate`，它会自动补上提取）。",
            )
        records = self.translations()
        if not records:
            raise WriteBackError(
                "还没有任何译文，无法写回",
                hint="先运行 `gametrans translate`。",
            )

        report = RunReport(
            command="writeback",
            project_root=str(self.project_root),
            engine=self.engine,
        )
        stage = report.stage("writeback")
        # 译文按**槽位键**组织：一个单元含多句时，每个槽位一条记录，写回时按槽位取。
        # 这正是磁盘上的形状（`translations.jsonl` 一行一个槽位），传下去不必换算。
        translations = {r.unit_id: r for r in records}
        options.setdefault("engine_options", dict(self.config.engine_options))
        # 默认只填空缺；要覆盖产物里已有的译文，调用方得显式声明这一项
        options.setdefault("overwrite_existing", False)
        # 源语言是适配器要用的一个普通配置值（它决定产物里带哪一份"原文"表）。
        # 与 engine_options 一样：内核只转达，不解释。
        options.setdefault("source_language", self.config.source_language)
        # 工作区可能被 `--workdir` 搬走；产物该跟着它走，而不是落回游戏目录里。
        options.setdefault("workdir", str(self.workdir))
        result, output_dir = self.writeback_layer.run(
            graph=graph,
            translations=translations,
            project_root=self.project_root,
            engine=self.engine,
            target_language=self.config.target_language,
            report=report,
            interaction=self.interaction,
            supplements=[entry.to_dict() for entry in self.resources.supplements.entries()],
            deviations=self.resources.deviations.by_unit(),
            options=dict(options),
            # 术语标签在**这一刻**渲染（还没定译的不渲染、不写回）——盘上的译文不动
            termbook=self.resources.termbook,
        )
        stage.metrics.update(result.to_dict())
        stage.finish(ok=report.ok)
        self._save_report(report)

        return WriteBackOutcome(
            files=list(result.files_written),
            backups=list(result.backups),
            units_written=result.units_written,
            units_missing=result.units_missing,
            units_rejected=int(report.metrics.get("pre_export_rejected") or 0),
            char_count=result.char_count,
            output_dir=str(output_dir),
            report=report,
        )

    def pack(self, **options: Any) -> PatchOutcome:
        pack = self.registry.get(self.engine)
        target_language = options.get("target_language") or self.config.target_language
        patch_root = pack.translation_output_dir(
            self.project_root,
            target_language,
            options={
                "engine_options": dict(self.config.engine_options),
                "workdir": str(self.workdir),
            },
        )
        # "什么算产物"由适配器回答 —— 内核不认识 .rpy，也不该认识
        if not pack.translation_artifacts(patch_root):
            raise WriteBackError(
                f"还没找到 {target_language} 的翻译产物：{patch_root}",
                hint="先运行 `gametrans writeback` 生成翻译文件，再 `gametrans pack`。",
            )

        records = self.translations()
        if not records:
            # 骨架本身也是 tl/ 下的产物，所以"目录里有文件"不再等于"翻过了" ——
            # 这条闸门按**译文**判断，否则会把一份没翻过的骨架当成补丁发出去。
            raise WriteBackError(
                f"还没有任何译文，补丁里会是原样的骨架：{patch_root}",
                hint="先运行 `gametrans translate`，再 `gametrans writeback`。",
            )

        report = RunReport(
            command="pack",
            project_root=str(self.project_root),
            engine=self.engine,
        )
        # 报出去的条数必须与**真正写进补丁的**一致：补丁是上一次 writeback 的产物，
        # 所以直接读那次运行留下的报告，而不是在这儿重新数一遍（重新数会把
        # 字符串表去重掉的条目也算进去，凭空多报）。
        units_written = self._last_writeback_units()
        if units_written is None:
            # 骨架一生成就在 tl/ 里，所以"目录里有文件"不代表填过译文；
            # 没填过就封包，发出去的会是**原样的骨架** —— 那不是翻译补丁。
            raise WriteBackError(
                f"还没有 writeback 过，补丁里会是原样的骨架：{patch_root}",
                hint="先运行 `gametrans writeback`，再 `gametrans pack`。",
            )
        changed = self._artifacts_newer_than_the_writeback(pack, patch_root)
        if changed:
            # 产物被动过（例如引擎重新生成了骨架），那份报告就不再描述补丁里的东西
            units_written = 0
            report.warn(
                f"{len(changed)} 个产物在上一次 writeback 之后变过，补丁里的条数无法确认："
                + "、".join(Path(item).name for item in changed[:5]),
                code="artifacts_changed_since_writeback",
            )
        outcome = self.writeback_layer.pack(
            patch_root=patch_root,
            project_name=self.project_root.name,
            engine=self.engine,
            target_language=target_language,
            output_dir=self.workdir / "dist",
            units_written=units_written,
            packaging=self.export_target().packaging,
            report=report,
            interaction=self.interaction,
        )
        report.stage("pack", **outcome.to_dict()).finish()
        self._save_report(report)
        outcome.report = report
        return outcome

    def _artifacts_newer_than_the_writeback(self, pack: Any, patch_root: Path) -> list[str]:
        """上一次 writeback 之后被动过的产物。

        补丁里的内容就是这些产物，而条数来自那次运行的报告；两者不一致时报出来的数字
        就是假的。这里只做时间戳比较 —— 内核不认识产物格式，"它变了没有"由文件系统回答。
        """
        report_path = self._last_writeback_report()
        if report_path is None:
            return []
        stamp = report_path.stat().st_mtime_ns
        return [
            str(path)
            for path in pack.translation_artifacts(patch_root)
            if path.stat().st_mtime_ns > stamp
        ]

    def _last_writeback_report(self) -> Path | None:
        directory = self.workdir / "reports"
        if not directory.is_dir():
            return None
        reports = sorted(
            directory.glob("*-writeback.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        return reports[-1] if reports else None

    def _last_writeback_units(self) -> int | None:
        """上一次 writeback 实际写入的条数（从它留下的结构化报告里读）。"""
        path = self._last_writeback_report()
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        for stage in payload.get("stages") or []:
            if stage.get("name") == "writeback":
                metrics = stage.get("metrics") or {}
                if "units_written" in metrics:
                    return int(metrics["units_written"])
        return None

    # ---- 工作区读写 ---------------------------------------------------------

    @property
    def graph(self) -> PathGraph | None:
        """当前路径图。

        每次都核对 ``graph.json`` 的时间戳/大小：长驻进程（比如 WebUI 面板）里
        别的进程重新 scan 之后，这里的缓存必须失效，不能一直吐出旧图。
        """
        path = self.workdir / GRAPH_FILE
        stamp = self._stamp(path)
        if self._graph is not None and stamp == self._graph_stamp:
            return self._graph

        self._graph = None
        self._graph_stamp = stamp
        if stamp is None:
            return None
        try:
            graph = PathGraph.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
            self.load_warnings.append(f"{GRAPH_FILE} 读不出来，已忽略")
            self._graph = None
            return None
        # **读盘时重盖一次章戳**：`chapters.json` 是项目的申报事实，可以后于 scan 出现
        # （先扫、读一遍剧情、再申报章界是正常顺序）。scan 时盖的那一次会落进 graph.json，
        # 但"申报晚于扫描"的图里一个章戳都没有 —— 那时面板会把 45 个单元排成一行，
        # 看起来像一条直线、也找不到章界。重盖是幂等的，没申报就什么都不改。
        apply_chapters(graph, self.workdir)
        self._graph = graph
        return self._graph

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def translations(self) -> list[TranslationArtifact]:
        path = self.workdir / TRANSLATIONS_FILE
        stamp = self._stamp(path)
        if stamp is None:
            self._translations = None
            self._translations_stamp = None
            return []
        # 这份文件是真猎物那种体量（一个 800 场的工程上百 MB）。面板一屏要读好几次，
        # 每次重解一遍 JSON 就是十几秒的"内容一直加载不出来"。按章戳缓存，
        # 写路径改完文件章戳就变，不会读到旧账。
        if self._translations is not None and stamp == self._translations_stamp:
            return self._translations
        records: list[TranslationArtifact] = []
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(TranslationArtifact.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError):
                self.load_warnings.append(f"{TRANSLATIONS_FILE} 第 {index} 行无法解析，已跳过")
        self._translations = records
        self._translations_stamp = stamp
        return records

    def save_graph(self) -> None:
        """把当前路径图写回工作区（agent 改了依赖边之后要落地）。"""
        graph = self.graph
        if graph is None:
            raise WriteBackError(
                "还没有路径图，没什么可保存的",
                hint="先运行 `gametrans scan`。",
            )
        self._save_graph(graph)

    def _agent_edges_from_disk(self) -> list[GraphEdge]:
        """上一份 ``graph.json`` 里**agent 写下的边**（``provenance=agent``）。

        为什么要单列：``scan`` 是从引擎重建整张图的，重扫会把 agent 写下的边一起抹掉。
        引擎边不用保（引擎会重新给）；agent 的那些是**人或读数拍下来的判断**
        （`graph depend` 确认的方向、`graph knowledge` 写入的知识边），丢了不会自己回来。
        """
        path = self.workdir / GRAPH_FILE
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found: list[GraphEdge] = []
        for item in payload.get("dependencies") or []:
            if not isinstance(item, dict):
                continue
            try:
                edge = GraphEdge.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            if str(getattr(edge, "provenance", "")) == "agent":
                found.append(edge)
        return found

    def _save_graph(self, graph: PathGraph) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        path = self.workdir / GRAPH_FILE
        path.write_text(
            json.dumps(graph.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        # 刚写下去的图就是当前图，顺手把时间戳对齐，免得下次访问白读一遍
        self._graph = graph
        self._graph_stamp = self._stamp(path)

    def _save_translations(
        self, records: list[TranslationArtifact], *, merge: bool = False
    ) -> None:
        """落盘译文账。

        ``merge=True`` 是给**分块跑**用的：这一轮只翻了一块，整份重写会把上一块的译文
        从盘上抹掉 —— 任务账按任务合并、译文账却只剩最后一块，而报告上看不出来。
        整跑（``merge=False``）仍然是整份覆盖：一次跑完的结果就是这一轮的全部记录，
        旧账不该留在盘上。

        ⚠️ 合并时**占位记录不许覆盖已有的记录**（`models.is_placeholder`）：阶段切窄的
        跑批（`--start-phase` / `--stop-after-phase`）会把全图交给台账、给"不在这一轮"
        的槽位写占位，而按槽位合并会把前几轮真翻好的那几条盖成"未产出译文" ——
        真靶 2026-09-28 就是这么把 `start` / `act1` 翻好的译文盖没的。
        """
        self.workdir.mkdir(parents=True, exist_ok=True)
        final = records
        if merge:
            merged: dict[str, TranslationArtifact] = {
                existing.unit_id: existing for existing in self.translations()
            }
            for record in records:
                existing = merged.get(record.unit_id)
                if (
                    existing is not None
                    and is_placeholder(record)
                    and not is_placeholder(existing)
                ):
                    continue
                merged[record.unit_id] = record
            final = list(merged.values())
        lines = [json.dumps(r.to_dict(), ensure_ascii=False) for r in final]
        path = self.workdir / TRANSLATIONS_FILE
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        # 刚写下去的账就是当前账，顺手对齐章戳（与 save_graph 同样的做法）：
        # 不指望文件系统的 mtime 分辨率来通知缓存失效
        self._translations = final
        self._translations_stamp = self._stamp(path)

    def _save_report(self, report: RunReport) -> None:
        report.finish()
        directory = self.workdir / "reports"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{report.run_id}-{report.command}.json").write_text(
            report.to_json(), encoding="utf-8"
        )

    # ---- 状态 ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        graph = self.graph
        records = self.translations()
        dist = self.workdir / "dist"
        patches = sorted(p.name for p in dist.glob("*.zip")) if dist.is_dir() else []
        return {
            "tool_version": __version__,
            "project_root": str(self.project_root),
            "workspace": str(self.workdir),
            "engine": self.engine,
            "target_language": self.config.target_language,
            "provider": self.config.provider,
            # 凭证只出现在这里，且只有尾巴（见 gametrans/credentials.py）
            "credentials": self.credentials_view(),
            "graph": {
                "present": graph is not None,
                **(graph.stats() if graph is not None else {}),
            },
            "translations": {
                "present": bool(records),
                "total": len(records),
                "ok": sum(1 for r in records if r.is_usable),
                "needs_review": sum(
                    1 for r in records if r.status is TranslationStatus.NEEDS_REVIEW
                ),
                # "没有译文"与"有译文但没通过校验"是两件事，计数也要分开
                "failed": sum(1 for r in records if r.status is TranslationStatus.FAILED),
            },
            "resources": self.resources.summary(),
            "export_target": self.export_target().to_dict(),
            # 引擎私有选项 + 它的外部工具链状态（都由适配器回答，内核只转达）
            "engine_options": dict(self.config.engine_options),
            "toolchain": self.toolchain_status(),
            "interaction": {
                "views": len(self.interaction.all_views()),
                "visible": len(self.interaction.visible_views()),
            },
            "patches": patches,
            "load_warnings": list(self.load_warnings),
        }

    # ---- 任务状态（只读视图） -----------------------------------------------

    #: 任务视图里每条带哪些字段 —— 够 agent 判断"哪条卡住了、为什么卡住"
    TASK_FIELDS = (
        "task_id",
        "unit_id",
        "status",
        "attempts",
        "target_language",
        "source_version",
    )

    def task_summary(self, *, status: str = "", limit: int = 0) -> dict[str, Any]:
        """各状态各有多少条 + 逐条摘要。纯只读。"""
        tasks = self.tasks.all()
        if status:
            known = {state.value for state in TaskState}
            if status not in known:
                raise ConfigError(
                    f"未知的任务状态：{status!r}",
                    hint=f"可用：{', '.join(sorted(known))}。",
                )
            tasks = [task for task in tasks if task.status.value == status]
        if limit > 0:
            tasks = tasks[:limit]
        return {
            "counts": self.tasks.counts(),
            "total": len(tasks),
            "problems": self.tasks.problems(),
            "tasks": [self._task_view(task) for task in tasks],
        }

    def task_detail(self, ref: str) -> dict[str, Any]:
        """按 task_id 或 unit_id 取一条 Task 的完整载荷（含检索结果）。"""
        for task in self.tasks.all():
            if task.task_id == ref:
                return task.to_dict()
        matches = self.tasks.for_unit(ref)
        if matches:
            return matches[-1].to_dict()
        raise GameTransError(
            f"没有这条任务：{ref!r}",
            hint="用 `gametrans tasks` 看看有哪些 task_id；也可以直接用 unit_id 查。",
        )

    @classmethod
    def _task_view(cls, task: TranslationTask) -> dict[str, Any]:
        return {
            **{field: getattr(task, field) for field in cls.TASK_FIELDS},
            "status": task.status.value,
            "source": task.source,
            "context": {
                "strategy": task.retrieved_context.strategy,
                "layers": dict(task.retrieved_context.layers),
                "items": len(task.retrieved_context.items),
                "degraded": task.retrieved_context.degraded,
                "starved": list(task.retrieved_context.starved),
            },
            "constraints": {
                "terminology": len(task.terminology_constraints),
                "style": len(task.style_constraints),
                "engine": len(task.engine_constraints),
            },
            "history": list(task.provenance.get("history") or []),
        }

    # ---- 知识审核闭环 -------------------------------------------------------

    def knowledge_update(self, *, min_occurrences: int = 2) -> dict[str, Any]:
        """翻译 → 观察（Guide §18）。

        观察的判据仍然是"同一原文在译文里重复且一致"，但它**只报数、不登记**：
        这条通道产出的东西真靶实测 26/26 都不是术语（整句 / 语气词 / 标点串）。
        要往术语书里加实体，走两条路：模型的申报（响应顺带）与摘要侧的实体清单。
        """
        records = [r for r in self.translations() if r.is_usable]
        update = KnowledgeUpdate(self.resources.termbook, min_occurrences=min_occurrences)
        update.observe(
            [(record.unit_id, record.source, record.target) for record in records],
            approved_sources=[entry.writing for entry in self.resources.termbook.injectable()],
        )
        return {
            "candidates": [],
            # "重复且一致"的条数照实报 —— 报的是挡在术语书外面的数，不是产出的数
            "settled": update.observed_settled,
            "observed": len(records),
            "note": "这条通道只进翻译记忆，不再往术语书产条目",
            "summary": self.resources.termbook.summary(),
        }

    # ---- 已有译文（译者的成品是**资产**，不是待办） --------------------------

    def harvest_existing(
        self,
        *,
        language: str = "",
        min_occurrences: int = 2,
        max_candidate_length: int = 24,
    ) -> dict[str, Any]:
        """把游戏里**已经翻好**的译文读进来，沉淀成翻译记忆。

        三条底线：

        * **只读游戏**：只读 ``tl/<语言>/``，一个字节都不写回去（产物落工作区）；
        * **不产术语候选**：观察出来的对应只进翻译记忆 —— 术语书该由"这一场里谁登场、
          哪些名字要定译"来产，不是由"这句话出现了两次"来产（这里口径，
          真靶实测这条通道产出的 26 条全是整句、语气词与标点串）；
        * **账要对得上**：配上的、没配上的（多语句块 / 空译文 / 认不出）分开报。
        """
        language = str(language or self.config.target_language or "").strip()
        pack = self.registry.get(self.engine)
        found = pack.existing_translations(
            self.project_root,
            language=language,
            options=dict(self.config.engine_options),
        )

        pairs = [
            (entry.source, entry.target)
            for entry in found.entries
            if entry.source.strip() and entry.target.strip()
        ]
        memory_written = self.resources.memory.remember_many(
            [(source, target, "") for source, target in pairs],
            language=language,
            # 来源写清楚：这些不是我们翻的，是别人的成品。
            # 这个标记同时决定"能不能被复用"—— 默认不复用，要 `translate --reuse-imported`
            # 才认（见 TranslationMemory.lookup 的 allow_imported）。
            provider=IMPORTED_PROVIDER,
        )

        approved = [entry.writing for entry in self.resources.termbook.injectable()]
        strings = [
            (entry.engine_id or entry.source, entry.source, entry.target)
            for entry in found.entries
            if entry.kind == "string"
        ]
        short_say = [
            (entry.engine_id or entry.source, entry.source, entry.target)
            for entry in found.entries
            if entry.kind == "say" and len(entry.source) <= max_candidate_length
        ]
        from_strings = KnowledgeUpdate(
            self.resources.termbook, min_occurrences=1, min_length=2
        )
        from_strings.observe(strings, approved_sources=approved)
        from_dialogue = KnowledgeUpdate(
            self.resources.termbook, min_occurrences=min_occurrences, min_length=4
        )
        from_dialogue.observe(short_say, approved_sources=approved)
        # 观察只报数：术语书里的实体不从这里来（见 observe 的说明）
        settled = from_strings.observed_settled + from_dialogue.observed_settled

        return {
            "language": language,
            "directory": found.directory,
            "files": len(found.files),
            "blocks": found.blocks,
            "pairs": {
                "say": found.say,
                "string": found.string,
                "total": len(found.entries),
            },
            "complex_blocks": found.complex_blocks,
            "unreadable_blocks": found.unreadable_blocks,
            "empty": found.empty,
            "empty_blocks": found.empty_blocks,
            "empty_strings": found.empty_strings,
            "same_as_source": found.same_as_source,
            "same_blocks": found.same_blocks,
            "same_strings": found.same_strings,
            "speaker_changed_blocks": found.speaker_changed_blocks,
            "strings_total": found.strings_total,
            # 两个恒等式：块与字符串表的账各自要能对上
            "accounting": found.accounting(),
            "warnings": list(found.warnings),
            "memory": {
                "written": memory_written,
                "stored": len(self.resources.memory.entries(language=language)),
                "path": str(self.resources.memory.path),
            },
            "knowledge": {
                "created": 0,
                # 观察只报数：有多少条"重复且一致"的对应被挡在术语书外面
                "settled": settled,
                "from_strings": 0,
                "from_dialogue": 0,
                "approved": 0,
                # 观察出来的进翻译记忆，不进术语书（2026-09-26 起）
                "candidates": [],
                "summary": self.resources.termbook.summary(),
            },
            # "会注入的行"才叫进了术语书（有译名或有事实）；待审的是**更正**，
            # 它住在 `termbook.pending.jsonl`，不在书里 —— 两个数要分开报。
            "termbook_entries": len(self.resources.termbook.injectable()),
            "termbook_pending": len(self.resources.pending_corrections()),
            # 孤儿译文：**引擎自己**的回答（旧译文对不上当前内容的那批）。
            # 这是"游戏更新后只补改动"的权威信号 —— 光看语言目录分不出来。
            "orphans": self._orphan_view(pack, language, found.entries),
            # 这条路径不写游戏目录 —— 写空的清单比一句"我没动它"更可信
            "game_files_written": [],
        }

    def _orphan_view(
        self,
        pack: Any,
        language: str,
        entries: list[Any],
    ) -> dict[str, Any]:
        """问适配器要孤儿报告，并把每条孤儿对回我们读到的旧译文。

        配对用的是适配器给的两侧（孤儿清单 + 已有译文），所以这里不需要认识任何引擎。
        """
        report = pack.orphan_report(
            self.project_root,
            language=language,
            options=dict(self.config.engine_options),
        )
        if not report.get("supported"):
            return report
        return with_original_text(report, entries)
