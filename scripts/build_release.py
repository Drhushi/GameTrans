"""把当前检出打成可分发的 Release 包。

为什么值得单独有个脚本：发布包的**内容规则**（哪些进、哪些不进）本身就该被检视、
被机检。写在脚本里，测试可以直接引用它；写成一段一次性命令，下次发版就得把规则
重新想一遍 —— 而"想漏了"的代价是把约 8GB 游戏语料打进去。

包内布局与 0.1.0 一致：``GameTrans-<版本>/`` 一层，里面是随内核发布的
``gametrans/``、``assets/``、随包脚本（:data:`RELEASE_SCRIPTS`）与根文件。
``tests/``、``lab/``、``corpora/`` 是本地资产，一律不进包；
``scripts/`` 下的**开发与实验工具**（`run_tests.py`、`backup_evidence.py`、
`view_*.py`）同样不进包 —— 它们依赖本地才有的目录，进了包也跑不起来。

同一个检出打出来的包是**逐字节相同**的（时间戳钉死、条目排序），所以"这份包是不是
那份源码打出来的"可以用哈希回答。

用法::

    python scripts/build_release.py                  # 打到 dist/
    python scripts/build_release.py --out ../dist    # 换输出目录
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

__all__ = [
    "RELEASE_DIRECTORIES",
    "RELEASE_ROOT_FILES",
    "RELEASE_SCRIPTS",
    "release_files",
    "release_version",
    "build",
]

#: 随内核发布的目录（整目录进包，不需要逐个文件地列）。
RELEASE_DIRECTORIES = ("gametrans", "assets")

#: 随内核发布的脚本 —— **白名单，不是整目录**。
#:
#: `desktop_shell.py` 是桌面壳（PySide6 可选装，缺失时 open_panel 自动退回），
#: 不进包的话发布包里壳永远不可用 —— 曾是真缺口。
#: `scripts/` 里其余的是开发工具（`run_tests.py`、`build_exe.py`、`view_*.py`），
#: `lab/` 里是判据器材与跑批入口，它们依赖 `lab/data/` 与 `corpora/` 这些
#: **不随包发布的目录**，对拿到包的人一行都跑不起来。按目录整包进来会把发布件变成
#: 一堆"存在但用不了"的石碑；而且实验器材与评估集不该跟着发行版到处走。
RELEASE_SCRIPTS = ("build_release.py", "open_panel.py", "desktop_shell.py")

#: 随内核发布的根文件。
RELEASE_ROOT_FILES = (
    "LICENSE",
    "pyproject.toml",
    ".gitignore",
    "打开面板.pyw",
    "打开面板.bat",
)

#: 绝不进包的东西：字节码缓存。
_SKIP_DIRECTORIES = {"__pycache__"}
_SKIP_SUFFIXES = (".pyc", ".pyo")

#: 钉死的时间戳 —— 打包不该因为文件"什么时候被改过"而产生不同的字节。
_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def release_version(root: Path) -> str:
    """发布包的版本号取自 ``pyproject.toml``（发行元数据里那一份）。"""
    payload = tomllib.loads((Path(root) / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(payload["project"]["version"])
    if not version:
        raise RuntimeError("pyproject.toml 里没有版本号，不知道该把包叫什么")
    return version


def release_files(root: Path) -> list[Path]:
    """这个检出里**该进发布包**的文件（相对检出根，排序后）。"""
    root = Path(root)
    missing = [name for name in RELEASE_ROOT_FILES if not (root / name).is_file()]
    if missing:
        raise RuntimeError(
            f"{root} 里少了 {missing} —— 这不像一个完整的检出（是不是在错误的目录下打包？）"
        )

    files = [Path(name) for name in RELEASE_ROOT_FILES]
    files += [Path("scripts") / name for name in RELEASE_SCRIPTS]
    missing_scripts = [name for name in RELEASE_SCRIPTS if not (root / "scripts" / name).is_file()]
    if missing_scripts:
        raise RuntimeError(f"scripts/ 里少了随包发布的 {missing_scripts}")
    for name in RELEASE_DIRECTORIES:
        directory = root / name
        if not directory.is_dir():
            raise RuntimeError(f"{root} 里没有 {name}/ —— 发布包会少一整个目录")
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if _SKIP_DIRECTORIES.intersection(path.parts) or path.suffix in _SKIP_SUFFIXES:
                continue
            files.append(path.relative_to(root))
    return sorted(set(files), key=lambda item: item.as_posix())


def build(
    root: Path,
    out_dir: Path,
    *,
    version: str | None = None,
    force: bool = False,
) -> Path:
    """打一个发布包，返回它的路径。"""
    root = Path(root).resolve()
    out_dir = Path(out_dir)
    if not force:
        _refuse_uncommitted_source(root)

    version = version or release_version(root)
    prefix = f"GameTrans-{version}"
    files = release_files(root)

    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{prefix}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for directory in _package_directories(files, prefix):
            zf.writestr(_entry(directory, is_dir=True), b"")
        for relative in files:
            zf.writestr(_entry(f"{prefix}/{relative.as_posix()}"), (root / relative).read_bytes())
    return archive


def _package_directories(files: list[Path], prefix: str) -> list[str]:
    """包里要有哪些目录条目（有些解压工具喜欢它们，也让人一眼看出布局）。"""
    directories = {prefix}
    for relative in files:
        parts = relative.as_posix().split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add(f"{prefix}/{'/'.join(parts[:index])}")
    return sorted(directories)


def _entry(name: str, *, is_dir: bool = False) -> zipfile.ZipInfo:
    """一个条目 —— 时间戳与权限都钉死，所以同样的源码打出同样的字节。"""
    info = zipfile.ZipInfo(f"{name.rstrip('/')}/" if is_dir else name, date_time=_FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o40755 if is_dir else 0o644) << 16
    if is_dir:
        info.external_attr |= 0x10
    return info


def _refuse_uncommitted_source(root: Path) -> None:
    """没入库的源码一律拒打 —— 这正是"发布包缺文件"的起因。

    本地跑得起来只是因为那些文件躺在你的磁盘上；别人按索引取源码打出来的包会缺它们，
    而缺的往往正是新加的适配包声明（``engine.json``），现场表现是"一个引擎都没有"。
    """
    if shutil.which("git") is None:
        print("[!] 本机没有 git，跳过「源码是否全部入库」的检查", file=sys.stderr)
        return
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *RELEASE_DIRECTORIES],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("[!] 这不是一个 git 检出，跳过「源码是否全部入库」的检查", file=sys.stderr)
        return
    untracked = [line for line in result.stdout.splitlines() if line.strip()]
    if untracked:
        listing = "\n".join(f"  - {item}" for item in untracked[:20])
        raise RuntimeError(
            "这些源码还没入库，打出来的包会缺它们：\n"
            f"{listing}\n"
            "先 `git add` 再打包；确实要打一份未入库的源码，加 --force。"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把当前检出打成可分发的 Release 包")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="检出根目录（默认本脚本的上一级）",
    )
    parser.add_argument("--out", default="dist", help="输出目录（默认检出根下的 dist/）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使有源码还没入库也照打（默认拒打：那正是发布包缺文件的起因）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    archive = build(root, out_dir, force=args.force)

    with zipfile.ZipFile(archive) as zf:
        entries = zf.namelist()
    print(f"发布包：{archive}")
    print(f"  版本：{release_version(root)}")
    print(f"  {len(entries)} 个条目，{archive.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
