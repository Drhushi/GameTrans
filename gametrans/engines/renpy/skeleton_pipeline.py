"""骨架流水线 —— 调官方引擎生成骨架、把译文填回去。

## 为什么需要它

机制早就验证通了，但**用户还点不到**：没有入口去"让引擎生成骨架"、
也没有入口去"把译文填进去"。这个模块把那两步补上，让整条链路可被控制面调用。

## 分层

它属于**适配层**：要认识引擎启动器、要调外部命令、知道骨架放在哪个目录。
内核不参与 —— 内核只给"哪条槽位对应哪句译文"（``SlotBindings``）。

## 三条不肯让步的规矩

1. **命令怎么拼、骨架在哪，是适配层对引擎的唯一约定** —— 拼法写在这里，
   不散落到控制面；
2. **不自己造骨架格式** —— 骨架由官方工具产出，我们只读、只填；
3. **失败要分得清** —— 找不到启动器 / 引擎返回非零 / 超时 / 骨架目录不存在，
   四种处置完全不同，都给**可执行的提示**，不甩裸 traceback。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from gametrans.core.bindings import SlotBindings, bind_units
from gametrans.core.slots import SlotSet
from gametrans.core.units import BoundaryPolicy, GroupByFile, assemble_units
from gametrans.core.workflow import SlotWorkflow
from gametrans.engines.renpy.skeleton import parse_skeleton_dir, parse_skeleton_text
from gametrans.engines.renpy.skeleton_writer import write_skeleton
from gametrans.errors import SkeletonError

__all__ = [
    "EngineRunResult",
    "SkeletonFillResult",
    "SkeletonSource",
    "build_translate_command",
    "run_engine_translate",
    "translation_dir",
    "resolve_launcher",
    "ensure_skeleton",
    "prepare_skeleton",
    "fill_skeleton_dir",
    "DEFAULT_SKIP",
]

#: 走"官方骨架"这条路时默认不动的文件（引擎自带的界面文本清单）
DEFAULT_SKIP = ("common.rpy",)

#: 调一次引擎最多等多久。官方工具在真实工程上约 5 秒；给足余量，但不无限等。
DEFAULT_TIMEOUT = 300


@dataclass
class EngineRunResult:
    ok: bool
    command: list[str] = field(default_factory=list)
    output: str = ""
    returncode: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "command": list(self.command),
            "output": self.output[-4000:],
        }


@dataclass
class SkeletonFillResult:
    written: int = 0
    skipped: int = 0
    files: int = 0
    #: 内容没变、因此没被改写的文件数（重跑幂等靠它，不靠"看起来一样"）
    unchanged: int = 0
    #: 这一轮真正被改写的文件路径
    files_written: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    #: 产物里**已经有译文**、按默认没动的位置（要覆盖得显式声明 `overwrite_existing`）
    kept_existing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "skipped": self.skipped,
            "files": self.files,
            "unchanged": self.unchanged,
            "files_written": list(self.files_written),
            "unknown": list(self.unknown),
            "backups": list(self.backups),
            "kept_existing": len(self.kept_existing),
        }


@dataclass
class SkeletonSource:
    """官方骨架现在的样子：它在哪、里面有哪些槽位、这一轮是不是刚生成的。"""

    language: str
    directory: Path
    slots: SlotSet
    warnings: list[str] = field(default_factory=list)
    #: 非空表示这一轮真的调了官方引擎去生成/刷新骨架
    engine_run: EngineRunResult | None = None

    @property
    def generated(self) -> bool:
        return self.engine_run is not None

    def summary(self) -> dict[str, Any]:
        payload = dict(self.slots.summary())
        payload.update(
            {
                "language": self.language,
                "directory": str(self.directory),
                "generated": self.generated,
                "warnings": len(self.warnings),
            }
        )
        return payload


def build_translate_command(launcher: Path, project_root: Path, language: str) -> list[str]:
    """拼出"让官方引擎生成/刷新某语言骨架"的命令。

    这是**适配层对引擎的唯一约定**：启动器 + 工程目录 + ``translate <语言>``。
    放在这里而不是散落到控制面 —— 换引擎时只改这一处。
    """
    return [str(launcher), str(project_root), "translate", language]


def _default_runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def run_engine_translate(
    *,
    launcher: Path,
    project_root: Path,
    language: str,
    timeout: int = DEFAULT_TIMEOUT,
    run: Callable[..., Any] | None = None,
) -> EngineRunResult:
    """调官方引擎生成/刷新骨架，返回结果或抛**带提示**的错误。"""
    launcher = Path(launcher)
    project_root = Path(project_root)

    if not launcher.exists():
        raise SkeletonError(
            f"找不到引擎启动器：{launcher}",
            hint=(
                "先下载官方引擎并把它所在目录填进来："
                "`gametrans engine option set sdk_path <目录>`，或在面板的引擎设置里填。"
            ),
        )

    command = build_translate_command(launcher, project_root, language)
    runner = run or _default_runner
    try:
        completed = runner(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SkeletonError(
            f"引擎生成骨架超时（超过 {timeout} 秒）：{' '.join(command)}",
            hint="工程过大或引擎卡住时，可以调大超时；也确认工程目录确实是该引擎的工程。",
        ) from None
    except OSError as exc:
        raise SkeletonError(
            f"启动引擎失败：{exc}",
            hint="确认启动器可执行，以及当前用户有权限运行它。",
        ) from None

    output = f"{getattr(completed, 'stdout', '') or ''}{getattr(completed, 'stderr', '') or ''}"
    returncode = int(getattr(completed, "returncode", 0) or 0)

    if returncode != 0:
        raise SkeletonError(
            f"引擎生成骨架失败（退出码 {returncode}）：{' '.join(command)}\n{output[-2000:]}",
            hint=(
                "先单独跑一次这条命令看报错。常见原因：工程目录不对、"
                "工程本身有语法错误、语言代码写错。"
            ),
        )
    return EngineRunResult(ok=True, command=command, output=output, returncode=0)


def resolve_launcher(sdk: Path, launchers: Iterable[str]) -> Path | None:
    """SDK 目录里的启动器 —— 找不到返回 ``None``，由调用方决定怎么说。"""
    root = Path(sdk)
    for name in launchers:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _has_skeleton(tl_dir: Path) -> bool:
    root = Path(tl_dir)
    return root.is_dir() and any(root.glob("*.rpy"))


def ensure_skeleton(
    *,
    project_root: Path,
    language: str,
    options: dict[str, Any] | None = None,
    launchers: Iterable[str] = ("renpy.exe", "renpy.sh", "renpy.py"),
    timeout: int = DEFAULT_TIMEOUT,
    run: Callable[..., Any] | None = None,
) -> SkeletonSource:
    """拿到**官方骨架**：已有就解析，没有就让官方工具生成，两样都不行就报错。

    骨架是"哪些文本要翻、它们的 id 是什么"的权威答案，所以这一步拿不到就不该继续
    —— 自己按源码猜内容范围会漏掉官方提得到的那几类文本，而产物看起来完全正常。

    ``options`` 是项目的 ``engine_options``（内核不解释的字典）；本函数只认里面
    ``sdk_path`` 这一个键。``run`` 供测试注入假的引擎调用，产品路径不传。
    """
    options = dict(options or {})
    language = str(language or "").strip()
    if not language:
        raise SkeletonError(
            "没有指定目标语言，无法确定该用哪份骨架",
            hint="在项目配置里设置目标语言：`gametrans config set target_language <语言>`。",
        )

    tl_dir = translation_dir(project_root, language)
    if _has_skeleton(tl_dir):
        parsed = parse_skeleton_dir(tl_dir, skip=DEFAULT_SKIP)
        return SkeletonSource(
            language=language,
            directory=tl_dir,
            slots=parsed.to_slot_set(),
            warnings=list(parsed.warnings),
        )

    raw = str(options.get("sdk_path") or "").strip()
    if not raw:
        raise SkeletonError(
            f"还没有 {language} 的官方骨架：{tl_dir}",
            hint=(
                "骨架由官方工具产出。两条路：① 用官方 SDK 跑一次"
                f"`<引擎> <游戏目录> translate {language}`，再把产物放进 game/tl/{language}/；"
                "② 把 SDK 根目录填进来，让本工具去跑："
                "`gametrans engine option set sdk_path <SDK 目录>`（面板的引擎设置里也能填）。"
            ),
        )

    sdk = Path(raw)
    launcher = resolve_launcher(sdk, launchers) if sdk.is_dir() else None
    if launcher is None:
        raise SkeletonError(
            f"官方 SDK 不可用：{sdk}",
            hint=(
                "填 SDK 的**根目录**（里面同时有 renpy/ 与启动器的那一层），"
                "用 `gametrans engine option set sdk_path <SDK 目录>` 改。"
            ),
        )

    engine = run_engine_translate(
        launcher=launcher,
        project_root=project_root,
        language=language,
        timeout=timeout,
        run=run,
    )
    if not _has_skeleton(tl_dir):
        raise SkeletonError(
            f"引擎跑完了，但没有产出骨架：{tl_dir}",
            hint="确认工程目录是引擎的工程，以及语言代码（例如 zh_CN）是引擎认的那个。",
        )

    parsed = parse_skeleton_dir(tl_dir, skip=DEFAULT_SKIP)
    return SkeletonSource(
        language=language,
        directory=tl_dir,
        slots=parsed.to_slot_set(),
        warnings=list(parsed.warnings),
        engine_run=engine,
    )


def translation_dir(project_root: Path, language: str) -> Path:
    """骨架目录：``<工程>/game/tl/<语言>``。

    这是引擎的目录约定，所以待在适配层。
    """
    return Path(project_root) / "game" / "tl" / language


def prepare_skeleton(
    *,
    launcher: Path,
    project_root: Path,
    language: str,
    policy: BoundaryPolicy | None = None,
    skip: Iterable[str] = DEFAULT_SKIP,
    timeout: int = DEFAULT_TIMEOUT,
    run: Callable[..., Any] | None = None,
) -> tuple[SlotWorkflow, EngineRunResult]:
    """生成骨架并把它读成槽位/单位绑定 —— 一次调用拿到"可以开始翻译"的状态。

    返回 ``(工作流, 引擎调用结果)``。工作流里的译文是空的（还没翻），
    调用方用 ``apply_results`` / ``record_slots`` 往里记。
    """
    engine = run_engine_translate(
        launcher=launcher,
        project_root=project_root,
        language=language,
        timeout=timeout,
        run=run,
    )

    tl = translation_dir(project_root, language)
    parsed = parse_skeleton_dir(tl, skip=tuple(skip))
    slots: SlotSet = parsed.to_slot_set()
    workflow = SlotWorkflow(slots, policy or GroupByFile())
    return workflow, engine


def fill_skeleton_dir(
    tl_dir: Path,
    bindings: SlotBindings,
    *,
    skip: Iterable[str] = DEFAULT_SKIP,
    backup: bool = True,
    overwrite_existing: bool = False,
) -> SkeletonFillResult:
    """把译文填回骨架目录里的每个文件。

    * 每个文件改写前留一份 ``.bak``（骨架是官方产物，不能无声覆盖）；
    * ``skip`` 里的文件不动（默认排除引擎自带的界面文本清单）；
    * **默认只填空缺**：已经有译文的位置一个字都不动，记进 ``kept_existing`` ——
      产物常常落在译者自己已经翻好的语言目录里；要覆盖得显式声明；
    * 内容没变的文件**不写、也不留备份** —— 重跑不该一次比一次脏；
    * ``written`` 是"这份骨架里现在有几条译文"，``files`` / ``files_written`` 才是
      "这一轮改了几个文件"：重跑时前者不变、后者为 0；
    * ``unknown`` 只收**每个文件里都找不到落点**的槽位，不静默丢 ——
      一条译文只可能落在其中一个文件里，所以"这份文件里没有"不是没落地。
    """
    root = Path(tl_dir)
    if not root.is_dir():
        raise SkeletonError(
            f"骨架目录不存在：{root}",
            hint=(
                "先用官方引擎生成骨架："
                "`gametrans engine option set sdk_path <引擎目录>` 之后跑一次骨架生成。"
            ),
        )

    skipped_names = set(skip)
    result = SkeletonFillResult()
    #: "处处都找不到落点"的槽位集合。按文件各报一遍再并起来，得到的是**全部槽位**
    #: （真游戏 3,259 条实测如此），报告会变成"一条都没落地"的假警报。
    nowhere: set[str] | None = None
    nowhere_order: list[str] = []

    for path in sorted(root.glob("*.rpy")):
        if path.name in skipped_names:
            continue

        # 按**字节**读：既要拿到原行尾（下面写回时保持），也要避免文本模式把 CRLF 归一
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        filled = write_skeleton(text, bindings, overwrite_existing=overwrite_existing)

        # 账先记全：译文在不在骨架里，与这一轮改没改文件是两件事
        result.written += filled.written
        result.skipped += filled.skipped
        for key in filled.kept_existing:
            if key not in result.kept_existing:
                result.kept_existing.append(key)
        here = set(filled.unknown)
        if nowhere is None:
            # 顺序取自第一份文件：绑定集合是同一份，各文件报出来的顺序一致
            nowhere_order = list(filled.unknown)
            nowhere = here
        else:
            nowhere &= here

        if filled.text == text:
            # 译文与骨架现状一致：这一轮没有要改的东西
            result.unchanged += 1
            continue

        if backup:
            backup_path = path.with_name(path.name + ".bak")
            shutil.copyfile(path, backup_path)
            result.backups.append(str(backup_path))

        try:
            # 行尾保持原样：Windows 上按文本模式写会把整份文件翻成 CRLF —— 内容没变、
            # 每一行的字节却都变了，"只换字面量"就没法用 diff 核对了。
            path.write_text(
                filled.text, encoding="utf-8", newline="\r\n" if b"\r\n" in raw else "\n"
            )
        except OSError as exc:
            # 备份已经落在前面，所以这里只需要说清"哪个文件、为什么"：
            # 裸 traceback 会让用户以为整个工程坏了。
            raise SkeletonError(
                f"改写骨架失败：{path}（{type(exc).__name__}: {exc}）",
                hint=(
                    f"这一轮的备份在 {path.name}.bak，原文件可以从它恢复；"
                    "确认这个文件没被别的程序占用、也不是只读，再重跑一次。"
                ),
            ) from None

        result.files += 1
        result.files_written.append(str(path))

    if result.files == 0 and result.unchanged == 0:
        raise SkeletonError(
            f"骨架目录里没有可填的文件：{root}",
            hint="确认引擎已经生成过骨架（目录里应当有 .rpy 文件）。",
        )

    result.unknown = [key for key in nowhere_order if nowhere and key in nowhere]
    return result
