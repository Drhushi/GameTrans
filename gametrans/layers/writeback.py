"""写回层：调用引擎支持包产出翻译补丁，并封包成可分发归档。

内核在这一层只做三件事：把译文交给引擎支持包、把产物封包、把过程记进报告。
"译文该摆到文件的哪个位置"完全由引擎支持包决定 —— 这正是换引擎时内核不用改的
原因。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from gametrans import __version__
from gametrans.core.constraints import KNOWN_CONSTRAINTS, slot_gauge, validate
from gametrans.core.graph import PathGraph
from gametrans.core.models import Scanner, Segment, TranslationArtifact, TranslationStatus
from gametrans.core.report import RunReport
from gametrans.engines.base import ExportTarget, WriteBackContext
from gametrans.engines.registry import EngineRegistry
from gametrans.errors import WriteBackError
from gametrans.layers.tags import render

MANIFEST_NAME = "manifest.json"


@dataclass
class PatchOutcome:
    """一次封包的结果。"""

    archive: str
    bytes: int
    files: list[str] = field(default_factory=list)
    units_written: int = 0
    target_language: str = ""
    engine: str = ""
    #: 这次运行的完整报告（警告与冲突都在里面）—— 与另外几个 Outcome 同一形状
    report: RunReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive": self.archive,
            "bytes": self.bytes,
            "files": list(self.files),
            "units_written": self.units_written,
            "target_language": self.target_language,
            "engine": self.engine,
        }


class WriteBackLayer:
    """把译文写回游戏，并制作可分发补丁。"""

    def __init__(self, registry: EngineRegistry) -> None:
        self.registry = registry

    def run(
        self,
        *,
        graph: PathGraph,
        translations: dict[str, TranslationArtifact],
        project_root: Path,
        engine: str,
        target_language: str,
        report: RunReport,
        output_dir: Path | None = None,
        interaction: Any = None,
        supplements: list[dict[str, Any]] | None = None,
        deviations: dict[str, frozenset[str]] | None = None,
        options: dict[str, Any] | None = None,
        termbook: Any = None,
    ):
        pack = self.registry.get(engine)
        if not pack.supports("writeback"):
            raise WriteBackError(
                f"引擎支持包 {engine!r} 没有写回能力",
                hint=f"该包申报的能力：{', '.join(pack.capabilities) or '（无）'}",
            )

        resolved_output = Path(output_dir) if output_dir else pack.translation_output_dir(
            Path(project_root), target_language, options=dict(options or {})
        )
        project_root = Path(project_root)

        # —— 导出前必须验证：先验，再让适配器动手 ——
        export_target = pack.export_target(project_root, target_language)
        self._check_export_target(export_target, report)
        #: 声明的偏离：判据按它放行（"有意留空"还要真的把空串写进去）
        declared = {str(unit): frozenset(kinds) for unit, kinds in (deviations or {}).items()}
        scanner = pack.structure_scanner() if pack.supports("structure_scan") else None
        verified = self.verify_before_export(
            graph=graph,
            translations=translations,
            project_root=project_root,
            export_target=export_target,
            report=report,
            scanner=scanner,
            approved=declared,
        )
        # **术语标签就在这一刻渲染**（盘上的译文一个字都不动）：换一个译名不是一次重翻，
        # 只是重新渲染一次。还没定译的标签不渲染、那一条不写回（见 _render_term_tags）。
        if termbook is not None:
            verified = self._render_term_tags(verified, termbook, report)

        ctx = WriteBackContext(
            project_root=project_root,
            engine=engine,
            target_language=target_language,
            graph=graph,
            translations=verified,
            # 被挡下的那些：不许写，但适配器记账时要能把它们与"没翻"分开
            withheld={uid: art for uid, art in translations.items() if uid not in verified},
            output_dir=resolved_output,
            report=report,
            options=dict(options or {}),
            # 显式声明才覆盖产物里已有的译文；默认只填空缺（保护译者已有的手艺）
            overwrite_existing=bool((options or {}).get("overwrite_existing")),
            # 声明的补充条目：内核只转达，怎么落盘由适配器决定
            supplements=list(supplements or []),
            empty_units={unit for unit, kinds in declared.items() if "empty" in kinds},
        )
        result = pack.writeback(ctx)
        rejected = len(translations) - len(verified)
        report.metrics["pre_export_rejected"] = rejected

        if interaction is not None:
            lines = [
                f"写入 {result.units_written} 条",
                f"缺失 {result.units_missing} 条",
                f"文件 {len(result.files_written)} 个",
                f"目录 {resolved_output}",
            ]
            if rejected:
                # "缺失"里有的是没翻，有的是被导出前校验挡下来的 —— 两件事必须分开说
                lines.insert(2, f"拒绝 {rejected} 条（未通过导出前校验）")
            unresolved_units = int(report.metrics.get("term_tag_unresolved_units") or 0)
            if unresolved_units:
                names = report.metrics.get("term_tag_unresolved") or []
                lines.insert(
                    2,
                    f"待定译标签 {unresolved_units} 条（{len(names)} 个名字没定译："
                    f"{'、'.join(str(name) for name in names[:5])}）—— 未渲染、未写回",
                )
            interaction.emit(
                "writeback.summary",
                f"已生成 {target_language} 翻译层",
                lines=lines,
                severity="warning" if (result.units_missing or rejected) else "success",
            )
        return result, resolved_output

    # ---- 导出前验证 -------------------------------------------

    @staticmethod
    def _check_export_target(export_target: ExportTarget, report: RunReport) -> None:
        """把适配器声明的导出契约与内核真正会执行的约束对一遍。

        声明了内核不认识的校验规则时如实报出来 —— 契约里的空头承诺比没有契约更糟。
        """
        report.metrics["export_target"] = export_target.to_dict()
        unknown = [r for r in export_target.validation_rules if r not in KNOWN_CONSTRAINTS]
        if unknown:
            report.warn(
                "导出契约声明了内核不执行的校验规则：" + "、".join(sorted(set(unknown))),
                code="unknown_validation_rule",
            )
        if not export_target.format:
            report.warn("导出契约没有声明 format", code="export_target_incomplete")

    @staticmethod
    def verify_before_export(
        *,
        graph: PathGraph,
        translations: dict[str, TranslationArtifact],
        project_root: Path,
        export_target: ExportTarget,
        report: RunReport,
        scanner: Scanner | None = None,
        approved: dict[str, frozenset[str]] | None = None,
    ) -> dict[str, TranslationArtifact]:
        """逐条验证 Artifact 能不能安全写回；不能的**不进**适配器，并记账。

        验证四件事：Locator 能定位到目标资源、**导出前重算**受保护元素是否
        满足约束、Unit 的结构确实被扫描过、编码与包装方式符合目标引擎要求。

        关于"重算"：Artifact 自报的 ``validation`` 只是参考 —— 导出边界自己拿 Unit 与
        译文再验一遍。否则一个没有结论的 Artifact（外部调用者手搓的、旧数据读出来的）
        就能靠着"没人验过"混进游戏文件。
        """
        units = {
            node.unit.id: node.unit
            for node in graph.nodes.values()
            if node.unit is not None
        }
        # 记录可以是**按槽位**的（一个单元含多句时逐句一条），所以再建一张
        # "槽位键 → 它所属的单元"的索引：两种键都要能定位到单元。
        unit_of_slot: dict[str, str] = {}
        for unit in units.values():
            for key in unit.metadata.get("slot_keys") or []:
                unit_of_slot.setdefault(str(key), unit.id)
        if export_target.encoding.lower().replace("-", "") not in ("utf8", "utf8sig"):
            report.warn(
                f"导出契约声明的编码是 {export_target.encoding}，内核按 UTF-8 处理",
                code="encoding_mismatch",
            )

        verified: dict[str, TranslationArtifact] = {}
        line_counts: dict[str, int] = {}
        for unit_id, artifact in translations.items():
            unit = units.get(unit_id)
            if unit is None and unit_id in unit_of_slot:
                unit = units.get(unit_of_slot[unit_id])
            locator = artifact.locator or (unit.locator if unit else None)
            if locator is None or not locator.file:
                report.add_issue(
                    "conflicts",
                    code="locator_missing",
                    message=f"{unit_id} 没有可用的 Locator，导出时跳过",
                    ref=unit_id,
                )
                continue
            if locator.kind == "line" and locator.line <= 0:
                report.add_issue(
                    "conflicts",
                    code="locator_unresolved",
                    message=f"{unit_id} 的行定位无效（line={locator.line}），导出时跳过",
                    ref=unit_id,
                )
                continue
            source_path = project_root / locator.file
            if not source_path.is_file():
                report.add_issue(
                    "conflicts",
                    code="locator_unresolved",
                    message=f"{unit_id} 的 Locator 指向的文件不存在：{locator.file}",
                    ref=unit_id,
                    detail={"file": locator.file},
                )
                continue
            if locator.kind == "line":
                # 行号必须真的落在文件里 —— "文件存在"不等于"这一行存在"，
                # 否则写回器会记成"已写入"，而文件里其实什么都没改
                total = line_counts.get(locator.file)
                if total is None:
                    try:
                        total = len(
                            source_path.read_text(encoding="utf-8").splitlines()
                        )
                    except (OSError, UnicodeDecodeError):
                        total = 0
                    line_counts[locator.file] = total
                if locator.line > total:
                    report.add_issue(
                        "conflicts",
                        code="locator_unresolved",
                        message=(
                            f"{unit_id} 的行定位超出文件范围"
                            f"（line={locator.line}，文件只有 {total} 行）"
                        ),
                        ref=unit_id,
                        detail={"file": locator.file, "line": locator.line, "lines": total},
                    )
                    continue
            if unit is not None and unit.metadata.get("segments_unverified"):
                report.add_issue(
                    "conflicts",
                    code="unit_unverified",
                    message=f"{unit_id} 的 segment 不是扫描出来的，无法确认受保护元素，不写回",
                    ref=unit_id,
                    detail={"metadata": dict(unit.metadata)},
                )
                continue

            # 重算：不信自报结论。
            # 自报失败与重算失败，任何一个成立都不写回 —— 记录的失败本身就是信号，
            # 不能因为"现在这条看着没事"就放行。
            # 标尺与翻译侧同一把（slot_gauge）：记录按槽位记账，拿整个单元当标尺会把
            # "with 转场 / 选项条件"这类**别的槽位**的结构算到这句头上（R52 同款）。
            gauge = (
                slot_gauge(unit, unit_id, scanner)
                if unit is not None and unit_id != unit.id
                else unit
            )
            recorded = artifact.validation
            rederived = (
                validate(
                    gauge,
                    artifact.target,
                    scanner=scanner,
                    approved=(approved or {}).get(unit_id, ()),
                )
                if unit is not None
                else None
            )
            failing = next(
                (v for v in (recorded, rederived) if v is not None and not v.ok), None
            )
            if failing is not None:
                # 声明过的偏离 + 重算已放行 = 记录里的**旧裁定**还没重算。这一步只报不代劳：
                # 旧结论是那次运行的证据，静默用它等于把"当时为什么挡下"抹掉。
                stale = (
                    failing is recorded
                    and rederived is not None
                    and rederived.ok
                    and bool((approved or {}).get(unit_id))
                )
                report.add_issue(
                    "conflicts",
                    code="stale_verdict" if stale else "constraint_violation",
                    message=(
                        f"{unit_id} 记录里的旧裁定没过（这条已声明偏离）—— "
                        "跑一次 `gametrans revalidate` 按当前判据重算后即可写回"
                        if stale
                        else f"{unit_id} 没有通过结构校验，不写回"
                    ),
                    ref=unit_id,
                    detail={
                        "violations": [v.to_dict() for v in failing.violations],
                        "recheck": rederived.to_dict() if rederived is not None else None,
                    },
                )
                continue
            if artifact.status is not TranslationStatus.OK:
                # 校验没结论但状态不是 ok（失败 / 待复核）也要在这里挡住 ——
                # 不指望写回器"顺手跳过"
                report.add_issue(
                    "conflicts",
                    code="status_not_ok",
                    message=f"{unit_id} 的状态是 {artifact.status.value}，不写回",
                    ref=unit_id,
                    detail={"status": artifact.status.value, "error": artifact.error},
                )
                continue
            if artifact.locator is None:
                report.warn(
                    f"{unit_id} 的 Artifact 没有自带 Locator，导出时回退到 Unit 的",
                    code="artifact_without_locator",
                    ref=unit_id,
                )
            verified[unit_id] = artifact
        return verified

    # ---- 封包 ---------------------------------------------------------------

    @staticmethod
    def _render_term_tags(
        translations: dict[str, TranslationArtifact],
        termbook: Any,
        report: RunReport,
    ) -> dict[str, TranslationArtifact]:
        """把译文里的 ``⟦写法⟧`` 渲染成**当前**术语书里的译名（见 `layers/tags.py`）。

        这是"标签机制"的最后一步：翻译时没定译的写法留在标签里，定译之后**不必重翻** ——
        写回 / 导出这一刻按书渲染一次即可。盘上的译文记录一个字符都不动，所以换一个译名
        只是一次重新渲染，随时可逆。

        **还没有译名的标签不渲染、那一条也不写回**，并逐条点名。把一个 ``⟦…⟧`` 交给
        适配器写进游戏文件比少写一条糟得多：少写一条在"缺失"里看得见，乱写一条没人会
        发现（它看起来就是一段正常的中文）。
        """
        rendered: dict[str, TranslationArtifact] = {}
        unresolved: dict[str, list[str]] = {}
        replaced = 0
        for unit_id, artifact in translations.items():
            segments: list[Segment] = []
            missing: list[str] = []
            for segment in artifact.translated_segments:
                text, unfound = render(segment.value, termbook)
                if text != segment.value:
                    replaced += 1
                segments.append(replace(segment, value=text))
                for writing in unfound:
                    if writing not in missing:
                        missing.append(writing)
            if missing:
                unresolved[unit_id] = missing
                report.add_issue(
                    "conflicts",
                    code="term_tag_unresolved",
                    message=(
                        f"{unit_id} 里还有没定译的名字（{'、'.join(missing)}）："
                        "这一条不写回 —— 先在术语书里给它定个译名，再重新写回"
                    ),
                    ref=unit_id,
                    detail={"writings": missing},
                )
                continue
            rendered[unit_id] = replace(artifact, translated_segments=segments)
        if unresolved:
            report.metrics["term_tag_unresolved_units"] = len(unresolved)
            report.metrics["term_tag_unresolved"] = sorted(
                {writing for writings in unresolved.values() for writing in writings}
            )
        report.metrics["term_tag_rendered"] = replaced
        return rendered

    def pack(
        self,
        *,
        patch_root: Path,
        project_name: str,
        engine: str,
        target_language: str,
        output_dir: Path,
        units_written: int = 0,
        packaging: str = "zip",
        report: RunReport | None = None,
        interaction: Any = None,
    ) -> PatchOutcome:
        patch_root = Path(patch_root)
        if packaging != "zip":
            raise WriteBackError(
                f"引擎支持包声明的包装方式是 {packaging!r}，本内核只会打 zip",
                hint="把 ExportTarget.packaging 设为 'zip'，或让适配器提供自己的封包实现。",
            )
        # "什么算产物"由适配器回答 —— 内核不认识产物格式，也不猜扩展名。
        # 装什么、为什么不装（写回备份、引擎编译缓存、暂不入包的图片）由适配器申报，
        # 内核只管照单打包并把"没装的"如实报出来。
        pack = self.registry.get(engine)
        contents = pack.pack_contents(patch_root)
        files = sorted(Path(path) for path in contents.files if Path(path).is_file())
        if report is not None:
            report.metrics["pack_contents"] = contents.to_dict()
        if not files:
            raise WriteBackError(
                f"{patch_root} 里没有可打包的产物",
                hint="先运行 `gametrans writeback` 生成翻译文件。",
            )

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        archive = output_dir / f"{project_name}-{target_language}-patch.zip"

        manifest = {
            "tool": "gametrans",
            "tool_version": __version__,
            "engine": engine,
            "target_language": target_language,
            "packaging": packaging,
            "units_written": units_written,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": [
                {
                    "path": PurePosixPath(path.relative_to(patch_root)).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in files
            ],
        }

        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, PurePosixPath(path.relative_to(patch_root)).as_posix())
            zf.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )

        outcome = PatchOutcome(
            archive=str(archive),
            bytes=archive.stat().st_size,
            files=[f["path"] for f in manifest["files"]],
            units_written=units_written,
            target_language=target_language,
            engine=engine,
        )
        if report is not None:
            report.metrics["archive"] = outcome.archive
            report.metrics["archive_bytes"] = outcome.bytes
        if interaction is not None:
            interaction.emit(
                "run.summary",
                "翻译补丁已封包",
                lines=[
                    f"归档 {archive.name}",
                    f"大小 {outcome.bytes} 字节",
                    *(
                        [f"未入包 {count} 个：{reason}" for reason, count in contents.skipped.items()]
                        if contents.skipped
                        else []
                    ),
                ],
                severity="success",
            )
        return outcome
