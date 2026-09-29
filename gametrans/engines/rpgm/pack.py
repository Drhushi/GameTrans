"""RPGM 支持包的装配：契约实现 + 能力自述 + 事实面入口。"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from gametrans.core.graph import PathGraph
from gametrans.core.models import Scanner, TranslationUnit
from gametrans.engines.base import (
    EngineDetection,
    EngineSupportPack,
    EngineWriteBackResult,
    ExportTarget,
    ExtractContext,
    PackContents,
    WriteBackContext,
)
from gametrans.engines.rpgm import __version__
from gametrans.engines.rpgm.datafiles import RpgmDataError, resolve_www_root, select_source
from gametrans.engines.rpgm.extractor import RpgmExtractor
from gametrans.engines.rpgm.runtime import (
    INSTALL_NOTE_NAME,
    ORIGINAL_LANGUAGE,
    PLUGIN_FILE_NAME,
    PLUGIN_NAME,
    TABLE_DIR,
    build_table,
    merge_plugin_table,
    write_bundle,
)
from gametrans.engines.rpgm.segments import segmentize
from gametrans.errors import ProjectError, WriteBackError

#: 源语言代码长这样才算"知道源语言是什么"；``auto`` 这种占位不算。
_LANGUAGE_CODE = re.compile(r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,4})?$")


class RpgmPack(EngineSupportPack):
    """RPG Maker MV（NW.js 发布版 / 编辑器工程）支持包。

    与 Ren'Py 支持包最要紧的一条差别：**它没有官方翻译工具**。Ren'Py 那边"哪些文本
    要翻"由 ``renpy translate`` 产出的骨架回答；RPGM 没有这种东西，所以内容范围由本
    适配层按引擎自己的数据结构申报（``content.py``），id 就是引擎数据里的那条路径。

    也正因如此，本包**不需要外部工具链** —— ``toolchain_status`` 如实这么答。
    """

    name = "rpgm"
    display_name = "RPG Maker MV"
    version = __version__
    summary = (
        "RPG Maker MV 引擎：读 data/*.json（含 SRD_DataCompressor 的 LZString 压缩），"
        "按引擎的数据结构申报玩家可见内容。"
    )
    #: 游戏内容在数据目录里。引擎运行时的代码不会被匹配到 —— 范围里没有它。
    file_globs = ("www/data/**/*.json", "data/**/*.json")
    #: 本工具自己的工作区不算内容。
    excluded_parts = (".gametrans",)
    #: 只读闭环 + 写回（运行时插件）。能力位跟着实现走：接了才报。
    capabilities = ("detect", "extract", "writeback", "structure_scan")

    #: 引擎数据库入口 —— 有它才叫"这是一个 MV 工程"。
    SYSTEM_FILE = "System.json"
    #: 引擎运行时的标志文件（用来区分"游戏数据"与"引擎自己"）。
    ENGINE_MARKERS = ("js/rpg_core.js", "js/rmmz_core.js")
    #: 产物落在项目工作区的这里（**不在游戏内容目录里** —— 见 translation_output_dir）。
    PATCH_DIR_PARTS = (".gametrans", "patch")
    #: 补丁里必带的三样；译文表按语言另行枚举。
    PATCH_FIXED = ("js/plugins.js", f"js/plugins/{PLUGIN_FILE_NAME}")
    #: 引擎不认识的扩展名一律不入包（并计数报出来）。
    INSTALL_NOTE = INSTALL_NOTE_NAME

    def __init__(self) -> None:
        self._extractor = RpgmExtractor()

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

        try:
            source = select_source(project_root)
        except RpgmDataError as exc:
            return EngineDetection(
                engine=self.name,
                detected=False,
                confidence=0.0,
                evidence=[exc.message],
            )

        www = source.www_root
        evidence: list[str] = []
        confidence = 0.0

        if source.files:
            evidence.append(f"找到 {len(source.files)} 个数据文件（{source.kind}）")
            confidence += 0.6
        if any(Path(rel).name == self.SYSTEM_FILE for rel in source.files):
            evidence.append(f"存在引擎数据库入口 {self.SYSTEM_FILE}")
            confidence += 0.25
        for marker in self.ENGINE_MARKERS:
            if (www / marker).is_file():
                evidence.append(f"存在引擎运行时 {marker}")
                confidence += 0.15
                break

        return EngineDetection(
            engine=self.name,
            detected=bool(source.files),
            confidence=min(confidence, 1.0),
            evidence=evidence or ["未发现 RPG Maker 工程特征"],
            project_files=len(source.files),
        )

    # ---- 提取 ---------------------------------------------------------------

    def extract(self, ctx: ExtractContext) -> PathGraph:
        """把玩家可见文本抽成带权路径图（内容范围与身份都按引擎的数据结构来）。"""
        return self._extractor.extract(ctx)

    # ---- 写回（路线乙：运行时插件） -----------------------------------------

    def translation_output_dir(
        self,
        project_root: Path,
        target_language: str,
        options: dict[str, Any] | None = None,
    ) -> Path:
        """产物放哪。

        **刻意不落在游戏内容目录里**。基类默认值是"项目根下按语言分目录"，对 Ren'Py
        是对的（`game/tl/<lang>/` 本来就是游戏的一部分），但这条路线是运行时插件，
        产物是"一包要装到 www/ 去的东西"，写进游戏目录就破了"游戏数据零改动"这个前提，
        而且要求游戏目录可写（用 `--workdir` 搬走工作区的人，图的正是游戏只读）。

        所以：跟着**工作区**走 —— ``<工作区>/patch/<语言>``。产物之间的相对路径与
        ``www/`` 对齐，解压覆盖即可安装。内核把 ``workdir`` 放在 ``options`` 里转达。
        """
        workdir = str((options or {}).get("workdir") or "").strip()
        base = Path(workdir) if workdir else Path(project_root).joinpath(*self.PATCH_DIR_PARTS[:-1])
        return base.joinpath(*self.PATCH_DIR_PARTS[-1:], target_language)

    def writeback(self, ctx: WriteBackContext) -> EngineWriteBackResult:
        """把**通过导出前校验的**译文落成一套可安装的运行时产物。

        三件事：① 从 unit + 它的 Artifact 造出按位置定键的译文表（目标语言一份、
        源语言一份）；② 把引擎原有的插件表**并入**我们那一条（原有条目一条不改）；
        ③ 写进 ``ctx.output_dir``，游戏目录一个字都不碰。
        """
        project_root = Path(ctx.project_root)
        if resolve_www_root(project_root) is None:
            raise WriteBackError(
                f"{project_root} 里没有 RPG Maker MV 的工程结构，不产出任何东西",
                hint="期望看到 `www/js/`（发布版）或根目录下直接有 `js/` + `data/`。",
            )

        units = {unit.id: unit for unit in ctx.graph.translatable_units()}
        pairs: list[tuple[Any, str]] = []
        missing = 0
        for unit_id, unit in units.items():
            artifact = ctx.translations.get(unit_id)
            if artifact is None or not artifact.is_usable:
                missing += 1
                continue
            pairs.append((unit, artifact.translated_text))

        if not pairs:
            raise WriteBackError(
                "没有任何可用译文，产出会是一份空补丁",
                hint="先运行 `gametrans translate`，再 `gametrans writeback`。",
            )

        target = str(ctx.target_language or "").strip() or "zh_CN"
        tables: dict[str, dict[str, str]] = {target: build_table(pairs)}
        source = self._source_language(ctx, target)
        lanes = [target]
        if source != target:
            # 源语言那一份用**原文**填：玩家切过去就是未翻译的样子，
            # 于是"设置里能切语言"这件事当场可验，不必等第二份译文。
            tables[source] = build_table([(unit, unit.source) for unit, _ in pairs])
            lanes.append(source)

        original_table = self._original_plugin_table(project_root)

        patch_root = Path(ctx.output_dir)
        # 整份重写：上一次可能带了别的语言，留下的旧表会跟着进补丁 ——
        # 玩家切到那种语言会拿到一份过期译文。
        if patch_root.exists():
            shutil.rmtree(patch_root)
        written = write_bundle(
            patch_root,
            tables=tables,
            default_language=target,
            original_plugin_table=original_table,
        )

        relative = sorted(path.relative_to(patch_root).as_posix() for path in written)
        ctx.report.metrics["rpgm_runtime"] = {
            "output_dir": str(patch_root),
            "target_language": target,
            "languages": lanes,
            "table_entries": len(tables[target]),
            "units_written": len(pairs),
            "units_missing": missing,
            "game_data_untouched": True,
        }
        ctx.report.stage(
            "rpgm_runtime",
            output_dir=str(patch_root),
            languages="、".join(lanes),
            table_entries=len(tables[target]),
        ).finish()

        return EngineWriteBackResult(
            files_written=relative,
            units_written=len(pairs),
            units_missing=missing,
            char_count=sum(len(text) for _, text in pairs),
        )

    @staticmethod
    def _source_language(ctx: WriteBackContext, target: str) -> str:
        """源语言那一份用什么代码。

        配置里 ``source_language`` 默认是 ``auto``（我们并不知道源语言是什么），
        这时用 :data:`~gametrans.engines.rpgm.runtime.ORIGINAL_LANGUAGE` 当代码 ——
        它不是一个语言主张，就是"原文"；好处是**任何情况下都至少有两种可选**，
        设置菜单里那一行才会出现。
        """
        raw = str((ctx.options or {}).get("source_language") or "").strip()
        if raw and raw.lower() != "auto" and _LANGUAGE_CODE.match(raw):
            return raw
        return ORIGINAL_LANGUAGE

    @staticmethod
    def _original_plugin_table(project_root: Path) -> str:
        """读引擎原有的插件表。

        读不懂就**报错**，不产出半成品：那张表里装着游戏十来个插件的全部参数，
        覆盖掉它就是改游戏行为 —— 而这条路线的前提是"游戏数据零改动"。
        """
        www = resolve_www_root(project_root)
        assert www is not None  # writeback 开头已经查过
        path = www / "js" / "plugins.js"
        if not path.is_file():
            return "var $plugins = [];"
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WriteBackError(f"读不了引擎的插件表 {path}：{exc}") from exc
        try:
            merge_plugin_table(original)
        except ValueError as exc:
            raise WriteBackError(
                f"引擎的插件表读不懂（{path}）：{exc}",
                hint=(
                    "宁可不写回也不覆盖它 —— 那张表里是游戏原有插件的全部参数。"
                    "确认这份 js/plugins.js 没被改坏。"
                ),
            ) from exc
        return original

    # ---- 结构与产物 ---------------------------------------------------------

    def structure_scanner(self) -> Scanner:
        """RPGM 的 Segment 切分器：``\\C[n]`` / ``\\V[n]`` / ``\\n`` / ``<br>`` / ``%1``。"""
        return segmentize

    # ---- 分组层级（翻译单元的中间一级） ------------------------------------

    def grouping_levels(self) -> tuple[str, ...]:
        """RPGM 能给出的结构层级：**地图**、**事件**、**资源类别**。

        这是"分组层级由适配层申报"的第二个引擎实例：Ren'Py 报 `label`/文件，
        RPGM 报地图/事件/类别 —— 两级结构根本不是一回事，内核不许假设。
        实测规模：地图级 70 组（中位 26 条/组）、事件级 614 组（**中位仅 2 条**），
        所以"事件"适合当硬切点，"地图"才像一场戏。
        """
        return ("map", "event", "category")

    def group_units(
        self, units: Sequence[TranslationUnit], level: str
    ) -> dict[str, str]:
        keys: dict[str, str] = {}
        for unit in units:
            payload = unit.locator.payload if unit.locator else {}
            structural = str(payload.get("structural_path") or "")
            data_file, _, pointer = structural.partition("#")
            if level == "map":
                keys[unit.id] = data_file or str(unit.locator.file or "")
            elif level == "event":
                event = pointer.split(".pages[", 1)[0] or pointer
                keys[unit.id] = f"{data_file}#{event}" if event else (data_file or unit.id)
            elif level == "category":
                keys[unit.id] = str(payload.get("category") or data_file or "unclassified")
            else:
                return {}
        return keys

    def translation_artifacts(self, output_dir: Path) -> list[Path]:
        """上一次写回产出了哪些文件 —— **由适配器枚举，不靠"目录里的一切"**。"""
        root = Path(output_dir)
        if not root.is_dir():
            return []
        found = [root / rel for rel in self.PATCH_FIXED]
        found.append(root / INSTALL_NOTE_NAME)
        found.extend(sorted((root / TABLE_DIR).glob("*.json")) if (root / TABLE_DIR).is_dir() else [])
        return sorted(path for path in found if path.is_file())

    def pack_contents(self, output_dir: Path) -> PackContents:
        """这类补丁装什么、故意不装什么。

        **装**：插件本体、并入后的插件表、每种语言一份译文表、安装说明。
        **不装**：游戏自己的数据文件（我们从不写它们，所以也永远不会在这里）、
        以及任何人往产物目录里塞的别的东西 —— 后者计数报出来，不静默带走。
        """
        root = Path(output_dir)
        contents = PackContents()
        if not root.is_dir():
            return contents
        declared = self.translation_artifacts(root)
        # 按"相对产物根的路径"比对，**不要 resolve()** —— 那会把 Windows 的 8.3 短名
        # 与长名混在一起，自己跟自己都对不上（实测踩过）。
        shipped = {path.relative_to(root).as_posix() for path in declared}
        contents.files = sorted(declared)

        skipped: Counter[str] = Counter()
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.relative_to(root).as_posix() in shipped:
                continue
            skipped["不是这类补丁要分发的东西"] += 1
        contents.skipped = dict(sorted(skipped.items()))
        return contents

    # ---- 事实面 -------------------------------------------------------------

    def language_facts(
        self,
        project_root: Path,
        *,
        language: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """语言包事实（只读）：locale 现状、谁在认目标语言、字体引用与文件存在性。

        全是引擎私有约定（``System.json`` 的 ``locale``、插件里的 ``locale.match(/^zh/)``、
        ``fonts/*.css`` 的 ``@font-face``、插件参数里的字体名），所以实现待在适配层内部。
        """
        from gametrans.engines.rpgm.language_facts import language_facts

        return language_facts(project_root, language=str(language or ""), options=options)

    def toolchain_status(self, options: dict[str, Any]) -> dict[str, Any]:
        """RPGM 不需要外部工具链 —— 如实这么答，而不是假装缺一个 SDK。"""
        return {
            "required": False,
            "configured": False,
            "usable": True,
            "detail": (
                "RPG Maker MV 没有官方翻译工具：内容范围由适配层按引擎的数据结构申报，"
                "不需要任何外部 SDK。"
            ),
        }

    def skeleton_status(
        self, project_root: Path, options: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """本引擎没有"官方产物骨架"这种东西。"""
        www = resolve_www_root(Path(project_root))
        return {
            "supported": False,
            "detail": (
                "RPG Maker MV 没有官方翻译工具，因此没有「官方骨架」可读；"
                "内容范围与定位都由适配层按引擎数据结构给出（data/*.json 的字段与事件指令）。"
            ),
            "www_root": str(www) if www else "",
        }

    # ---- 导出契约 -----------------------------------------------------------

    def export_target(self, project_root: Path, target_language: str) -> ExportTarget:
        """导出契约：一包解压到 ``www/`` 就装好的运行时产物。

        **游戏数据文件不在产物里** —— 译文由插件在运行时改写内存中的文本，
        不是替换 ``data/*.json``。所以这个补丁可以原样卸载，游戏也不需要还原。
        """
        return ExportTarget(
            format="rpgm_runtime_plugin",
            file_structure=(
                f"js/plugins/{PLUGIN_FILE_NAME} + js/plugins.js + "
                f"{TABLE_DIR}/<语言>.json + {INSTALL_NOTE_NAME}"
            ),
            locator_resolution=(
                "译文表按**引擎数据结构里的路径**定键："
                "locator.payload['structural_path'] 换算成的字符串位置"
                "（如 Map001.json#events[1].pages[0].list[9].parameters[0]）"
            ),
            encoding="utf-8",
            packaging="zip",
            validation_rules=[
                "placeholder_count_preserved",
                "tag_balance_preserved",
                "control_code_preserved",
                "required_newlines_preserved",
                "escape_sequence_preserved",
                "output_shape_valid",
            ],
            metadata={
                "install": (
                    "把补丁解压覆盖到游戏的 www/ 目录即可；"
                    f"要卸载就删掉 {TABLE_DIR}/ 与 js/plugins/{PLUGIN_FILE_NAME}，"
                    "并从 js/plugins.js 里删掉我们那一条"
                ),
                "languages": (
                    "目标语言一份 + 源语言一份（源语言那份用原文填，供玩家切回原文）；"
                    "带两种以上时插件才往设置菜单加语言行"
                ),
                "switchable": True,
                "game_data_untouched": True,
                "requires": "游戏原有的 js/plugins.js（会被并入而不是覆盖）",
            },
        )
