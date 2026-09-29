"""Engine Adapter 契约。

内核只通过这些面认识一个引擎：

* :meth:`EngineSupportPack.detect`   —— 这个目录是不是我的游戏？
* :meth:`EngineSupportPack.extract`  —— 把游戏抽成 Localization IR（带权路径图）
* :meth:`EngineSupportPack.writeback`—— 把译文写回文件并产出补丁素材
* :meth:`EngineSupportPack.scan_structure` —— 按本引擎语法切出受保护结构（校验用）
* :meth:`EngineSupportPack.export_target`  —— 声明的导出契约

引擎专属的数据一律放进 :attr:`~gametrans.core.models.Locator.payload` 这个不透明
字典，在提取与写回之间原样传递，内核不解释。哪些"由 Adapter 决定"、哪些"由核心
决定"，见 :class:`EngineSupportPack` 的契约划定。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from gametrans.core.graph import PathGraph
from gametrans.core.models import Scanner, Segment, TranslationArtifact, TranslationUnit
from gametrans.core.report import RunReport


@dataclass
class EngineDetection:
    """一次引擎探测的结果。``confidence`` 用于多引擎并存时排序。"""

    engine: str
    detected: bool
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    project_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "detected": self.detected,
            "confidence": round(self.confidence, 4),
            "evidence": list(self.evidence),
            "project_files": self.project_files,
        }


@dataclass
class ExtractContext:
    """提取层的输入。"""

    project_root: Path
    engine: str
    report: RunReport
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteBackContext:
    """写回层的输入。

    ``translations`` 以 ``unit_id`` 为键；引擎支持包不认识 ``unit_id`` 的语义，
    只负责把译文按自己的语法摆到 ``artifact.locator`` 指出的位置。

    ``withheld`` 是**内核在导出前校验里挡下的**那些译文（``unit_id`` → Artifact）。
    它们**不许写进产物**，但也不是"没翻译"—— 适配器记账时必须能把它们与真正的缺口
    分开报，否则"翻了但没过闸门"会被记成"压根没翻"，读报告的人就去重翻一遍。
    """

    project_root: Path
    engine: str
    target_language: str
    graph: PathGraph
    translations: dict[str, TranslationArtifact]
    output_dir: Path
    report: RunReport
    options: dict[str, Any] = field(default_factory=dict)
    withheld: dict[str, TranslationArtifact] = field(default_factory=dict)
    #: **声明的补充条目**（引擎不枚举、但玩家看得见的文本）：原样转达给适配器，
    #: 内核不解释它是什么，也不决定它怎么落盘
    supplements: list[dict[str, Any]] = field(default_factory=list)
    #: 被声明"有意留空"的单位：它们的空译文要真的写进产物（别的空译文一律不动）
    empty_units: set[str] = field(default_factory=set)
    #: **显式声明**用当前译文覆盖产物里已有的译文。
    #: 默认 False ＝ 只填空缺：产物里已经有人翻过的地方一个字都不动
    #: （我们的产物经常落在译者自己已经翻好的语言目录里，默认覆盖等于吃掉别人的手艺）。
    overwrite_existing: bool = False


@dataclass
class ExportTarget:
    """导出契约。

    核心不认识 ``.rpy``、``.json`` 或任何引擎文件格式 —— 它只把这个声明原样报给
    agent 与用户，并把 ``validation_rules`` 与内核真正执行的约束对照。
    """

    #: 目标产物类型自述，例如 ``renpy_tl``
    format: str = ""
    #: 目标文件与目录结构，例如 ``game/tl/<lang>/<script>.rpy``
    file_structure: str = ""
    #: 如何依据 Locator 找到回写点（人类可读的说明，由适配器负责真实实现）
    locator_resolution: str = ""
    encoding: str = "utf-8"
    #: 包装方式：``zip`` / ``none``（目录原样）
    packaging: str = "zip"
    #: 写回前要求核心执行的约束（取值来自
    #: :class:`~gametrans.core.models.ConstraintType`）；内核不认识的规则会被报出来
    validation_rules: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "file_structure": self.file_structure,
            "locator_resolution": self.locator_resolution,
            "encoding": self.encoding,
            "packaging": self.packaging,
            "validation_rules": list(self.validation_rules),
            "metadata": dict(self.metadata),
        }


@dataclass
class EngineWriteBackResult:
    """写回层的产物清单。"""

    files_written: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    units_written: int = 0
    units_missing: int = 0
    char_count: int = 0
    #: 产物里已经有译文、按默认没动的位置（要覆盖得显式声明）
    kept_existing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_written": list(self.files_written),
            "backups": list(self.backups),
            "units_written": self.units_written,
            "units_missing": self.units_missing,
            "char_count": self.char_count,
            "kept_existing": len(self.kept_existing),
        }


@dataclass
class ExistingTranslation:
    """游戏里**已经翻好**的一条：原文 → 已有译文。

    这是"资产"而不是"待办"：读它是为了复用它（翻译记忆、术语候选），
    不是为了让谁去改它。所以这一层只有读，没有写。
    """

    source: str
    target: str
    #: ``say``（对白块）/ ``string``（字符串表的一对）
    kind: str = "say"
    #: 项目根相对的出处（R42 那条教训：内核只认这个基准）
    file: str = ""
    line: int = 0
    #: 引擎给这一块的 id（字符串表没有 id，按原文定键）
    engine_id: str = ""
    speaker: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "engine_id": self.engine_id,
            "speaker": self.speaker,
        }


@dataclass
class ExistingTranslations:
    """一个语言目录里已有的全部译文，外加**没能配成对**的那些的如实计数。

    "没配上对"不是小事，所以单独报而不是丢掉：多语句块（引擎允许拆句/并句）没法
    一对一配；空译文位不算译文；语句认不出来就不猜。三者与成功配对的条数必须有账。
    """

    language: str = ""
    directory: str = ""
    entries: list[ExistingTranslation] = field(default_factory=list)
    #: 对白形态块总数（见下面的恒等式）
    blocks: int = 0
    #: 多语句块：拆句/并句，一对一配对不成立
    complex_blocks: int = 0
    #: 认不出来的块（语句不是 say 形态，或拿不到可信的原文注释）
    unreadable_blocks: int = 0
    #: 块里译文位是空的（还没翻）
    empty_blocks: int = 0
    #: 块里译文与原文一字不差（占着位子没翻）
    same_blocks: int = 0
    #: 译者改过说话人的块数（原文注释里的说话人与块体不同 —— 是写法，不是错配）
    speaker_changed_blocks: int = 0
    #: 字符串表里读到的 ``old``/``new`` 对总数（含没翻的）
    strings_total: int = 0
    empty_strings: int = 0
    same_strings: int = 0
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def say(self) -> int:
        return sum(1 for entry in self.entries if entry.kind == "say")

    @property
    def string(self) -> int:
        return sum(1 for entry in self.entries if entry.kind == "string")

    @property
    def empty(self) -> int:
        return self.empty_blocks + self.empty_strings

    @property
    def same_as_source(self) -> int:
        return self.same_blocks + self.same_strings

    def accounting(self) -> dict[str, int]:
        """两个恒等式的左半边 —— 报告里一眼能核对的账。

        * ``blocks == say + complex_blocks + unreadable_blocks + empty_blocks + same_blocks``
        * ``strings_total == string + empty_strings + same_strings``
        """
        return {
            "blocks": self.blocks,
            "blocks_accounted_for": (
                self.say + self.complex_blocks + self.unreadable_blocks
                + self.empty_blocks + self.same_blocks
            ),
            "strings_total": self.strings_total,
            "strings_accounted_for": self.string + self.empty_strings + self.same_strings,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "directory": self.directory,
            "files": len(self.files),
            "blocks": self.blocks,
            "pairs": {"say": self.say, "string": self.string, "total": len(self.entries)},
            "complex_blocks": self.complex_blocks,
            "unreadable_blocks": self.unreadable_blocks,
            "empty_blocks": self.empty_blocks,
            "same_blocks": self.same_blocks,
            "speaker_changed_blocks": self.speaker_changed_blocks,
            "strings_total": self.strings_total,
            "empty_strings": self.empty_strings,
            "same_strings": self.same_strings,
            "accounting": self.accounting(),
            "warnings": list(self.warnings),
        }


@dataclass
class PackContents:
    """封包该装什么，以及**故意没装的**（口径由适配器定，内核不猜扩展名）。

    ``skipped`` 的键是"为什么没装"的类别名（适配器给的说明文字），值是条数 ——
    没装的东西必须计数报出来，否则用户以为补丁里有字体，装上去是豆腐块。
    """

    files: list[Path] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"files": len(self.files), "skipped": dict(self.skipped)}


def with_original_text(
    orphans: dict[str, Any],
    translations: list["ExistingTranslation"],
) -> dict[str, Any]:
    """按 ``engine_id`` 把孤儿条目与**已读到的旧译文**对上，带出原文与译文。

    这段配对数的是适配器两侧给的东西（孤儿清单 + 已有译文），所以放在契约层 ——
    内核里不出现任何引擎语法，它也就能对任何引擎的适配包复用。

    对不上也照报（``source``/``target`` 留空）：引擎说有孤儿、我们的读取却没看到它，
    这本身就是要让人知道的事实。
    """
    by_id = {
        str(entry.engine_id): entry for entry in translations if getattr(entry, "engine_id", "")
    }
    enriched: list[dict[str, Any]] = []
    matched = 0
    for item in orphans.get("entries", []):
        original = by_id.get(str(item.get("engine_id") or ""))
        enriched.append(
            {
                **item,
                "source": getattr(original, "source", "") if original else "",
                "target": getattr(original, "target", "") if original else "",
                "found_in_reading": original is not None,
            }
        )
        if original is not None:
            matched += 1
    return {**orphans, "entries": enriched, "matched": matched}


class EngineSupportPack(ABC):
    """一个游戏引擎的适配包。

    子类必须声明 ``name`` / ``display_name`` / ``version`` / ``file_globs``，
    并实现 :meth:`detect` 与 :meth:`extract`。写回与结构扫描能力可选，用
    ``capabilities`` 如实申报 —— agent 会读 ``describe()`` 决定能做什么。
    """

    name: str = ""
    display_name: str = ""
    version: str = "0.0.0"
    summary: str = ""
    #: **内容范围**：本引擎的游戏内容在哪（相对项目根的通配符）。
    #: 内核不猜"什么算游戏内容" —— 发行版常常把引擎运行时塞在同一个目录里，
    #: 这件事只有适配器知道，所以由适配器申报。
    file_globs: tuple[str, ...] = ()
    #: 相对路径里出现这些片段就不算内容（本工具自己的产物、工作区等）
    excluded_parts: tuple[str, ...] = (".gametrans",)
    capabilities: tuple[str, ...] = ("detect", "extract")

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    @abstractmethod
    def detect(self, project_root: Path, *, strict: bool = False) -> EngineDetection:
        """判断目录里是不是本引擎的工程。

        ``strict=True`` 时，路径不存在等硬错误应抛 :class:`~gametrans.errors.ProjectError`；
        默认宽容模式只回报 ``detected=False``，便于一次性探测多个引擎。
        """

    @abstractmethod
    def extract(self, ctx: ExtractContext) -> PathGraph:
        """抽出全部待译内容，整理成带权路径图。"""

    def writeback(self, ctx: WriteBackContext) -> EngineWriteBackResult:
        raise NotImplementedError(f"引擎支持包 {self.name!r} 未实现写回能力")

    # ---- 结构扫描 ----------------------------------------

    def structure_scanner(self) -> Scanner | None:
        """返回本引擎的 Segment 切分器，供核心校验**译文**有没有破坏受保护结构。

        核心只做比对，不认识任何语法 —— "什么算变量、什么算标签"由适配器回答。
        没有这个能力的适配包返回 ``None``，核心会如实报告"未做结构校验"，
        而不是假装通过。
        """
        return None

    def scan_structure(self, text: str) -> list[Segment]:
        """把一段文本切成受保护结构（便捷入口，等价于 ``structure_scanner()``）。"""
        scanner = self.structure_scanner()
        return scanner(text) if scanner is not None else []

    # ---- 分组层级（翻译单元的中间一级） ------------------------------------

    def grouping_levels(self) -> tuple[str, ...]:
        """本引擎能提供的**分组层级**名称（引擎结构坐标）。

        翻译单元有三层粒度：写回粒度（引擎槽位，恒定）／**单元粒度**（结构段）／
        调用粒度（预算）。中间这一级由适配层申报：Ren'Py 是 `label`/文件，RPGM 是
        地图／事件／资源类别。内核只按申报的层级分组，**不许自己假设任何层级**。
        """
        return ()

    def group_units(
        self, units: Sequence[TranslationUnit], level: str
    ) -> dict[str, str]:
        """把单位按 ``level`` 归组：``{unit_id: 组键}``。

        不认识的层级返回空字典 —— 内核据此**报错**，不静默退回"不分组"
        （静默退回等于实验条件悄悄失效，这类事故本项目已经吃过一次）。
        """
        return {}

    # ---- 外部工具链（官方 SDK 等） ------------------------------------------

    def skeleton_status(self, project_root: Path, options: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """本引擎"官方产物"的现状（只读）。

        有些引擎有官方工具会先产出一份骨架（翻译层骨架、字符串清单等），
        那份骨架是"什么算可译内容"的权威答案。这个方法只**回答现状**，
        不写盘、不改状态 —— 因此不需要事先配好外部工具链。

        默认实现回答"本引擎没有这种产物"。有这种产物的适配包自己实现。
        """
        return {
            "supported": False,
            "detail": f"{self.display_name or self.name} 没有（或未适配）官方产物骨架。",
        }

    def existing_translations(
        self,
        project_root: Path,
        *,
        language: str = "",
        options: dict[str, Any] | None = None,
    ) -> ExistingTranslations:
        """这个工程里**已经翻好**的译文（只读事实面）。

        用途是"把译者的成品当资产"：读出来进翻译记忆、观察术语候选，全程不改它。
        只有认识自己产物格式的适配包才答得上来，所以默认实现如实说"我这里没有"。
        """
        return ExistingTranslations(
            language=language,
            warnings=[
                f"{self.display_name or self.name} 还没有申报「已有译文」这个事实面。"
            ],
        )

    def orphan_report(
        self,
        project_root: Path,
        *,
        language: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """**孤儿译文**：游戏更新后，哪些旧译文对不上当前内容了（只读事实面）。

        这件事只有引擎自己算得出来（``tl/<语言>/`` 目录里，一条孤儿块与一条正常块长得
        一模一样），所以默认实现如实说"我这里没有这个能力"，而不是报一个空的"没有孤儿"。
        """
        return {
            "supported": False,
            "language": language,
            "reason": f"{self.display_name or self.name} 还没有申报孤儿译文报告",
        }

    def toolchain_status(self, options: dict[str, Any]) -> dict[str, Any]:
        """本引擎需要的外部工具链现在能不能用。

        ``options`` 是项目的 ``engine_options``（对内核不透明的字典）。内核从不知道
        "sdk_path" 是什么，也不检查文件存不存在 —— 那是适配器的事；内核只负责把它
        转达给用户界面与 agent。

        默认回答"我不需要外部工具链"。需要的那种（例如 Ren'Py 官方 SDK）自己实现。
        """
        return {
            "required": False,
            "configured": False,
            "usable": False,
            "detail": f"{self.display_name or self.name} 不需要外部工具链",
        }

    # ---- 导出契约 ---------------------------------------------

    def export_target(self, project_root: Path, target_language: str) -> ExportTarget:
        """声明导出契约。默认实现是保守的"什么都不敢承诺"。"""
        return ExportTarget(
            format=f"{self.name or 'unknown'}_default",
            file_structure=str(self.translation_output_dir(project_root, target_language)),
            locator_resolution="locator",
        )

    def translation_output_dir(
        self,
        project_root: Path,
        target_language: str,
        options: dict[str, Any] | None = None,
    ) -> Path:
        """译文该往哪个目录写。

        内核不硬编码任何引擎的目录习惯，所以这件事必须由支持包回答。默认值是
        "项目根下按语言分目录"这个**不假定任何引擎约定**的形状；真实适配包必须覆盖它。

        ``options`` 里带着内核已经知道、而适配器自己算不出来的东西（引擎私有选项、
        以及 ``workdir`` —— 工作区可能被 ``--workdir`` 搬到别处，产物该跟着走）。
        有些引擎的产物本来就该落在游戏里（Ren'Py 的 ``game/tl/<lang>/``），
        有些恰恰相反（运行时插件那条路线，产物落进游戏就破了"游戏数据零改动"）——
        所以这个决定必须留给适配器，内核只负责把它需要的信息给全。
        """
        return Path(project_root) / target_language

    def translation_artifacts(self, output_dir: Path) -> list[Path]:
        """上一次写回产出了哪些文件。

        **内核不猜文件扩展名**，所以"什么算译文产物"由适配器回答；
        默认实现认为目录下的每个文件都算。
        """
        root = Path(output_dir)
        if not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*") if p.is_file())

    def pack_contents(self, output_dir: Path) -> PackContents:
        """这类语言的**分发内容**：装什么、为什么不装别的。

        口径由适配器定 —— 内核不猜扩展名，也不知道"字体"对一个引擎意味着什么。
        默认实现就是"装写回产出的那些产物"。
        """
        return PackContents(files=self.translation_artifacts(output_dir))

    def language_facts(
        self,
        project_root: Path,
        *,
        language: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """这个工程认哪些语言、文本靠什么字体显示、语言目录里现在有什么。

        **这是事实面，不是判据**：只报"引擎里写着什么、文件在不在"，不替调用方拿主意
        —— 用哪个字体、字体从哪来、语言入口怎么接，都由 agent 决定。
        默认实现如实说"这个引擎还没申报"，不假装知道。
        """
        return {
            "language": language,
            "supported": False,
            "detail": f"{self.display_name or self.name} 还没有申报语言包事实",
        }

    def describe(self) -> dict[str, Any]:
        """能力自述。agent 与交互层都靠它知道"这个包能干什么"。"""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "summary": self.summary,
            "file_globs": list(self.file_globs),
            "excluded_parts": list(self.excluded_parts),
            "capabilities": list(self.capabilities),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"<{type(self).__name__} name={self.name!r} version={self.version!r}>"
