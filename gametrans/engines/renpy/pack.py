"""Ren'Py 支持包的装配：契约实现 + 文件发现 + 能力自述。"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Sequence

from gametrans.core.graph import PathGraph
from gametrans.core.models import Scanner, TranslationUnit
from gametrans.engines.base import (
    EngineDetection,
    EngineSupportPack,
    EngineWriteBackResult,
    ExistingTranslations,
    ExportTarget,
    ExtractContext,
    PackContents,
    WriteBackContext,
)
from gametrans.engines.renpy import __version__
from gametrans.engines.renpy.align import align_graph, bind_translations
from gametrans.engines.renpy.extractor import RenPyExtractor, discover_rpy_files
from gametrans.engines.renpy.segments import segmentize
from gametrans.engines.renpy.skeleton_pipeline import (
    DEFAULT_SKIP,
    ensure_skeleton,
    fill_skeleton_dir,
    resolve_launcher,
)
from gametrans.engines.renpy.skeleton_writer import (
    SUPPLEMENT_FILE,
    write_supplement_file,
)
from gametrans.errors import ProjectError

#: `old "…"` / `new "…"` 两行一对（引擎骨架里的字符串块）。
_OLD_NEW_RE = re.compile(r'^\s*old\s+"(.*)"\s*$\n^\s*new\s+"(.*)"\s*$', re.M)


def skipped_string_files_report(tl_dir: Path, skipped: Sequence[str]) -> dict[str, int]:
    """`DEFAULT_SKIP` 里那些文件**还剩多少条没翻**——刻意跳过的，但要说得出数。

    为什么要有这个读数：`common.rpy` 装的是 **Ren'Py 自带的界面/无障碍字符串**，
    适配层明确不碰它（见 `skeleton_pipeline.DEFAULT_SKIP`）—— 所以游戏里那几百条
    会一直是原文，**这是设计不是漏翻**。问题在于它以前**一声不响**：要去数
    "成品里还有多少英文"只能人工拆产物（真靶 2026-09-29 实测：`common.rpy` 里
    299 条 `new` 还是英文，而报告里一个字都没提）。

    判据只做一件事：这一对 `old`/`new` **逐字相同、且里面没有汉字** —— 那就是
    "没被翻译过"（原来的写法在，译文等于原文）。
    """
    report = {"files": 0, "strings": 0}
    for name in skipped:
        path = Path(tl_dir) / name
        if not path.is_file():
            continue
        report["files"] += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for old, new in _OLD_NEW_RE.findall(text):
            if old.strip() and old.strip() == new.strip() and not any(
                "\u4e00" <= char <= "\u9fff" for char in new
            ):
                report["strings"] += 1
    return report



class RenPyPack(EngineSupportPack):
    """Ren'Py（.rpy 脚本 / tl 翻译层）支持包。"""

    name = "renpy"
    display_name = "Ren'Py"
    version = __version__
    summary = "Ren'Py 视觉小说引擎：解析 .rpy 脚本，产出官方 tl/<lang>/ 翻译层。"
    #: 游戏内容只在 game/ 下。发行版把 Ren'Py 运行时整个塞在游戏根目录里
    #: （renpy/ 与 lib/），那是引擎自己的代码，不是内容 —— 声明范围即可，
    #: 不需要内核去减黑名单。
    file_globs = ("game/**/*.rpy",)
    #: 本工具自己的产物（tl/）、工作区，以及**引擎运行时**都不算内容。
    #: 运行时通常在整个项目根（`renpy/` + `lib/`），有些打包布局会把它嵌到
    #: `game/` 下面 —— 两种都由适配器**声明**排除，内核不猜目录名。
    excluded_parts = ("tl", ".gametrans", "renpy")
    capabilities = ("detect", "extract", "writeback", "structure_scan")

    #: 语言包里"文本"的扩展名（官方骨架填出来的翻译层）
    TEXT_SUFFIXES = (".rpy",)
    #: 语言包里"字体"的扩展名 —— 中文不换字体显示不出来，必须随包
    FONT_SUFFIXES = (".ttf", ".otf", ".ttc")
    #: 图片类：现阶段不入包（登记簿 R28），但计数报出来
    IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif")
    #: 引擎自己生成的编译缓存：封包时清掉
    COMPILED_SUFFIXES = (".rpyc", ".rpymc")

    def __init__(self) -> None:
        self._extractor = RenPyExtractor()

    # ---- 探测 ---------------------------------------------------------------

    def detect(self, project_root: Path, *, strict: bool = False) -> EngineDetection:
        project_root = Path(project_root)
        if not project_root.exists():
            if strict:
                raise ProjectError(
                    f"项目路径不存在：{project_root}",
                    hint="确认路径拼写，或先用 `gametrans project init <路径>` 建立新工程。",
                )
            return EngineDetection(
                engine=self.name,
                detected=False,
                confidence=0.0,
                evidence=[f"路径不存在：{project_root}"],
            )

        if not project_root.is_dir():
            if strict:
                raise ProjectError(f"项目路径不是目录：{project_root}")
            return EngineDetection(
                engine=self.name, detected=False, confidence=0.0, evidence=["不是目录"]
            )

        rpy_files = discover_rpy_files(
            project_root, self.file_globs, self.excluded_parts
        )
        evidence: list[str] = []
        confidence = 0.0

        if rpy_files:
            evidence.append(f"找到 {len(rpy_files)} 个 .rpy 脚本")
            confidence += 0.5
        if (project_root / "game").is_dir():
            evidence.append("存在 game/ 目录")
            confidence += 0.3
        if (project_root / "game" / "script.rpy").is_file():
            evidence.append("存在 game/script.rpy 入口")
            confidence += 0.1
        if (project_root / "game" / "tl").is_dir():
            evidence.append("已存在 tl/ 翻译目录")
            confidence += 0.1

        return EngineDetection(
            engine=self.name,
            detected=bool(rpy_files),
            confidence=min(confidence, 1.0),
            evidence=evidence or ["未发现 Ren'Py 工程特征"],
            project_files=len(rpy_files),
        )

    # ---- 提取 / 写回 --------------------------------------------------------

    def extract(self, ctx: ExtractContext) -> PathGraph:
        """产出内容图：**内容范围来自官方骨架**，结构事实来自源码。

        * 骨架给"哪些文本要翻、它们的 id 与原文是什么"（拿不到就报错，见
          :func:`~gametrans.engines.renpy.skeleton_pipeline.ensure_skeleton`）；
        * 源码给"这些文本长在哪"（label 区间、菜单归属、控制流）；
        * 两边按 ``(源文件, 行号)`` 缝起来，缝不上的如实报 ``unaligned_slot``。
        """
        source = ensure_skeleton(
            project_root=ctx.project_root,
            language=str(ctx.options.get("target_language") or ""),
            options=dict(ctx.options.get("engine_options") or {}),
            launchers=self.SDK_LAUNCHERS,
            run=ctx.options.get("engine_run"),
        )
        # 把申报的内容范围交给提取器 —— 发现逻辑是通用的，范围是适配器的
        ctx.options.setdefault("file_globs", list(self.file_globs))
        ctx.options.setdefault("excluded_parts", list(self.excluded_parts))
        structure = self._extractor.extract(ctx)
        alignment = align_graph(
            structure,
            source.slots,
            project_root=ctx.project_root,
            report=ctx.report,
            scanner=segmentize,
        )
        ctx.report.metrics["skeleton"] = source.summary()
        return alignment.graph

    def writeback(self, ctx: WriteBackContext) -> EngineWriteBackResult:
        """把译文**填进官方骨架**，不再自己拼 tl 文件。

        骨架是引擎的产物：块 id、控制流、注释、缩进都是引擎算好的，我们只换字面量。
        自己拼一遍的代价实测过 —— 结构全对、能加载，但块 id 与引擎算的不一致，中文不显示。
        """
        source = ensure_skeleton(
            project_root=ctx.project_root,
            language=ctx.target_language,
            options=dict(ctx.options.get("engine_options") or {}),
            launchers=self.SDK_LAUNCHERS,
            run=ctx.options.get("engine_run"),
        )
        bindings = bind_translations(
            source.slots,
            ctx.graph.translatable_units(),
            ctx.translations,
            withheld=ctx.withheld,
            empty_ok=ctx.empty_units,
        )
        filled = fill_skeleton_dir(
            source.directory,
            bindings,
            skip=(*DEFAULT_SKIP, SUPPLEMENT_FILE),
            # 默认只填空缺：产物里已有的译文一个字不动，要覆盖必须在控制面显式声明
            overwrite_existing=ctx.overwrite_existing,
        )
        coverage = bindings.coverage()
        if filled.kept_existing:
            ctx.report.add_issue(
                "skipped",
                code="kept_existing_translation",
                message=(
                    f"{len(filled.kept_existing)} 处已经有译文，按默认没动它们"
                    "（要覆盖请显式声明 overwrite_existing）"
                ),
                detail={"count": len(filled.kept_existing), "samples": filled.kept_existing[:20]},
            )

        # 声明的补充条目：引擎不枚举、但玩家看得见的文本（说话人名、代码里的界面提示……）。
        # 真机实测：落进字符串表就会在运行时被查到 —— 所以**不用动游戏源码**。
        declared = [
            (str(item.get("source") or ""), str(item.get("target") or ""))
            for item in (ctx.supplements or [])
        ]
        written_supplements = write_supplement_file(
            source.directory, ctx.target_language, declared
        )
        ctx.report.metrics["supplements"] = {
            "declared": len([item for item in declared if item[0].strip()]),
            "written": written_supplements,
            "file": SUPPLEMENT_FILE if written_supplements else "",
        }

        ctx.report.metrics["slot_coverage"] = coverage
        ctx.report.metrics["skeleton_fill"] = filled.to_dict()
        # 刻意不碰的那些文件还剩多少条没翻：**适配层的取舍，如实报出来**。
        # 不报的话，"成品里还有几百条英文"只能靠人工拆产物才发现（真靶实测就这样）。
        skipped_report = skipped_string_files_report(source.directory, DEFAULT_SKIP)
        ctx.report.metrics["skipped_string_files"] = skipped_report
        if skipped_report["strings"]:
            ctx.report.add_issue(
                "skipped",
                code="engine_strings_left_untranslated",
                message=(
                    f"产物里还有 {skipped_report['strings']} 条字符串没翻，"
                    f"全在适配层声明**不动**的文件里（{'、'.join(DEFAULT_SKIP)}——"
                    "Ren'Py 自带的界面与无障碍字符串）—— 这是设计，不是漏翻；"
                    "要连它们一起翻就得把那些文件从 `DEFAULT_SKIP` 里拿掉"
                ),
                detail=dict(skipped_report),
            )
        if filled.unknown:
            ctx.report.add_issue(
                "skipped",
                code="unknown_slot",
                message=(
                    f"{len(filled.unknown)} 条译文在骨架里找不到落点"
                    "（骨架被重新生成过，或槽位键变了）"
                ),
                detail={"count": len(filled.unknown), "samples": filled.unknown[:20]},
            )

        return EngineWriteBackResult(
            files_written=list(filled.files_written),
            backups=list(filled.backups),
            units_written=filled.written,
            units_missing=int(coverage["missing"]),
            kept_existing=list(filled.kept_existing),
            char_count=sum(
                len(record.translated_text)
                for record in ctx.translations.values()
                if record.is_usable
            ),
        )

    def pack_contents(self, output_dir: Path) -> PackContents:
        """Ren'Py 语言包的分发内容（登记簿 R28 口径）。

        * **文本必带**：`*.rpy`（官方骨架填出来的翻译层）；
        * **字体必带**：`ttf/otf/ttc` —— 中文不换字体根本显示不出来；
        * **图片暂不入包**：含字菜单图、画廊那类先登记不装（留待以后），但**计数报出来**；
        * **引擎编译缓存与写回备份清掉**：`.rpyc` / `.rpymc` / `*.bak` —— `.rpyc` 是引擎
          每次启动自己生成的，删了不影响；装进去只会把旧字节码发给玩家。
        """
        root = Path(output_dir)
        contents = PackContents()
        if not root.is_dir():
            return contents

        skipped: Counter[str] = Counter()
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in self.COMPILED_SUFFIXES or path.name.lower().endswith(".bak"):
                skipped["引擎编译缓存与写回备份（封包时清掉）"] += 1
                continue
            if suffix in self.TEXT_SUFFIXES or suffix in self.FONT_SUFFIXES:
                contents.files.append(path)
                continue
            if suffix in self.IMAGE_SUFFIXES:
                skipped["图片（现阶段不入包，留待以后）"] += 1
                continue
            skipped["不是这类语言要分发的东西"] += 1

        contents.skipped = dict(sorted(skipped.items()))
        return contents

    def translation_output_dir(
        self,
        project_root: Path,
        target_language: str,
        options: dict[str, Any] | None = None,
    ) -> Path:
        """Ren'Py 的约定：翻译层放在 ``game/tl/<lang>/``。

        这里**故意不用** ``options``：Ren'Py 的产物本来就该是游戏的一部分
        （官方骨架就在那儿长出来的），搬去工作区反而不成立。
        """
        del options
        return Path(project_root) / "game" / "tl" / target_language

    def translation_artifacts(self, output_dir: Path) -> list[Path]:
        """Ren'Py 的产物是 ``tl/<lang>/*.rpy``；目录里别的文件不算（引擎自己认识）。"""
        root = Path(output_dir)
        if not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*.rpy") if p.is_file())

    # ---- 外部工具链（官方 SDK） --------------------------------------------

    def skeleton_status(
        self, project_root: Path, options: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """官方 tl 骨架的现状（只读）。

        骨架的目录约定与文件格式都是引擎私有知识，所以实现待在适配层内部
        （`skeleton_ops`）；这个方法是内核唯一看得见的入口。
        """
        from gametrans.engines.renpy.skeleton_ops import skeleton_status

        return skeleton_status(
            project_root=project_root,
            language=str(kwargs.get("language") or ""),
            include_files=bool(kwargs.get("include_files", True)),
        )

    def existing_translations(
        self,
        project_root: Path,
        *,
        language: str = "",
        options: dict[str, Any] | None = None,
    ) -> ExistingTranslations:
        """这个工程里**已经翻好**的译文（只读）。

        对"手里已经有成品的译者"来说这是最重要的一个事实面：他们的译文是资产。
        实现待在适配层内部（`existing` 模块）—— 目录约定与文件格式都是引擎私有知识。
        """
        del options
        from gametrans.engines.renpy.existing import read_existing_translations

        return read_existing_translations(project_root, language=language)

    def orphan_report(
        self,
        project_root: Path,
        *,
        language: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """引擎自己的孤儿译文报告（跑官方 `lint`，只读输出）。

        实现待在适配层内部（`lint_report`）：输出格式是引擎私有知识。没有官方 SDK 时
        如实说做不到 —— 这一栏绝不假装"没有孤儿"。
        """
        from gametrans.engines.renpy.lint_report import orphan_report as read_orphans

        return read_orphans(
            project_root,
            language=language,
            options=dict(options or {}),
            launchers=self.SDK_LAUNCHERS,
        )

    def language_facts(
        self,
        project_root: Path,
        *,
        language: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """语言包事实（只读）：游戏认哪些语言、字体从哪来、语言目录里现有什么。

        全是引擎私有语法（`config.language` / `Language(...)` / `gui.*_font` /
        `style: font` / `{font=...}` / `tl/<语言>/`），所以实现待在适配层内部
        （`language_facts` 模块）。**只说事实，不拿主意**。
        """
        from gametrans.engines.renpy.language_facts import language_facts

        return language_facts(
            project_root=project_root,
            language=str(language or ""),
            sdk_path=str((options or {}).get("sdk_path") or ""),
            file_globs=self.file_globs,
            excluded_parts=self.excluded_parts,
        )

    #: 官方 SDK 里的启动器候选（Windows / POSIX）
    SDK_LAUNCHERS = ("renpy.exe", "renpy.sh", "renpy.py")

    def toolchain_status(self, options: dict[str, Any]) -> dict[str, Any]:
        """官方 Ren'Py SDK 接上了没有。

        用户自己下载 SDK，把地址填进 ``engine_options["sdk_path"]``（CLI 或面板），
        之后生成 tl 骨架、对账块 id 这类事就能交给官方工具。
        """
        raw = str(options.get("sdk_path") or "").strip()
        if not raw:
            return {
                "required": True,
                "configured": False,
                "usable": False,
                "detail": "还没配置官方 SDK 路径（engine_options.sdk_path）",
                "hint": (
                    "下载官方 Ren'Py SDK 后，把它的目录填进来："
                    "`gametrans engine option set sdk_path <SDK 目录>`，或在面板的引擎设置里填。"
                ),
            }
        sdk = Path(raw)
        if not sdk.is_dir():
            return {
                "required": True,
                "configured": True,
                "usable": False,
                "path": str(sdk),
                "detail": f"这个路径不是目录：{sdk}",
                "hint": "填 SDK 的**根目录**（里面有 renpy/ 与 lib/ 的那一层）。",
            }
        launcher = resolve_launcher(sdk, self.SDK_LAUNCHERS)
        version = self._sdk_version(sdk)
        if launcher is None:
            return {
                "required": True,
                "configured": True,
                "usable": False,
                "path": str(sdk),
                "version": version,
                "detail": (
                    f"目录里找不到启动器（找过：{'、'.join(self.SDK_LAUNCHERS)}）"
                ),
                "hint": "确认这是官方 SDK 的根目录，而不是它下面的 renpy/ 或 launcher/。",
            }
        return {
            "required": True,
            "configured": True,
            "usable": True,
            "path": str(sdk),
            "launcher": str(launcher),
            "version": version,
            "detail": f"官方 SDK 可用（{version or '版本未知'}）",
        }

    #: 版本文件的候选顺序：SDK 的 `vc_version.py` 里是**字面量**，
    #: `__init__.py` 里是表达式（`version = version_dict["version"]`），所以只认前者那种。
    SDK_VERSION_FILES = ("vc_version.py", "vc.py", "__init__.py")

    @staticmethod
    def _sdk_version(sdk: Path) -> str:
        """从 SDK 里读版本号 —— 读不到就返回空串，不猜。

        只接受**带引号的字面量**：`version = '8.5.3.26051504'` ✓；
        `version = version_dict["version"]` ✗（那是表达式，不是版本）。
        """
        for name in RenPyPack.SDK_VERSION_FILES:
            candidate = sdk / "renpy" / name
            try:
                text = candidate.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("version = "):
                    continue
                raw = stripped.split("=", 1)[1].strip()
                if len(raw) > 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
                    return raw[1:-1]
        return ""

    # ---- 结构扫描与导出契约 ------------------------------

    def structure_scanner(self) -> Scanner:
        """Ren'Py 的 Segment 切分器：``[var]`` / ``{tag}`` / ``{w}`` / ``[[`` / 换行。

        核心拿它把**译文**按同一套语法重新切分，再与原文的结构指纹比对。
        """
        return segmentize

    def grouping_levels(self) -> tuple[str, ...]:
        """Ren'Py 能给出的结构层级：`label`（一幕/一场）与文件。

        没有 label 归属的单位（修好解析后应为 0）退到文件级 —— 不让它们自成一组，
        免得"没归属"反而变成了一个独立的翻译单元。
        """
        return ("label", "file")

    def group_units(
        self,
        units: Sequence[TranslationUnit],
        level: str,
        *,
        level_paths: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, str]:
        """按 ``level`` 给每个单位算组键。

        组键取**内核图里的容器结构**（``level_paths``，来自 ``PathGraph.level_paths``），
        取不到那一级时才回落到文件级。**不再看 ``unit.context.scene``**：那段 context
        只在槽位缝上源码时才有（`align.py`），缝到 menu 行（`alignment=container`）与
        完全缝不上的（`alignment=unaligned`）单元拿到的是空 context —— 真靶上 1,263 条
        因此退化成"一条一组"，产生"一次请求只翻一句话"。

        ``level_paths`` 不给时（老调用方）一律退到文件级 —— 那仍然是容器结构里的事实，
        只是粗一级。
        """
        if level not in self.grouping_levels():
            return {}
        keys: dict[str, str] = {}
        for unit in units:
            paths = (level_paths or {}).get(unit.id) or {}
            value = str(paths.get(level) or "")
            if value:
                keys[unit.id] = value
                continue
            # 这一级取不到：用**文件级**（容器结构里的事实，不是逐条自成一组）。
            # 没有 label 归属的单元（引擎清单里缝不上源码的那些）本来就属于"这个文件"。
            rel = str((unit.locator.file if unit.locator else "") or "")
            keys[unit.id] = f"file:{rel}" if rel else f"unit:{unit.id}"
        return keys

    def export_target(self, project_root: Path, target_language: str) -> ExportTarget:
        """Ren'Py 的导出契约：官方 tl 翻译层。"""
        return ExportTarget(
            format="renpy_tl",
            file_structure="game/tl/<target_language>/<script>.rpy",
            locator_resolution="locator.payload['relpath']（源文件）+ payload['line']（行号）",
            encoding="utf-8",
            packaging="zip",
            validation_rules=[
                "placeholder_count_preserved",
                "tag_balance_preserved",
                "control_code_preserved",
                "required_newlines_preserved",
                "output_shape_valid",
            ],
            metadata={
                "string_table": "translate <lang> strings:",
                "block_form": "每个 label 一个 translate 块，控制流原样保留",
                "content_source": "内容范围与块 id 由官方骨架给出（`renpy <project> translate <lang>`）",
                "fill_rule": "只替换字符串字面量；注释、控制流、缩进原样保留",
            },
        )
