"""适配包的**装载器** —— 随内核发布的和用户自己装的走同一条路。

这是"内核与引擎适配分开发布"落地的地方。规矩四条：

1. **目录即安装**。适配包 = 一个目录 + 一份 ``engine.json``。用户把目录放进引擎目录
   （``~/.gametrans/engines/``）就生效，不需要改内核任何一行代码；
2. **内置的也是普通适配包**，只是随内核一起发布。于是"我不需要自带的那两个"
   等于"不放它们"，而不是"改一行注册代码然后每次升级都冲突"；
3. **用户装的同名包盖掉内置的**，而且是**有痕**地盖：谁盖了谁、各自什么版本都记下来；
4. **装不上要记账**。协议不匹配、声明坏掉、import 炸掉、两个包同名 —— 一律进
   "被拒清单"并带原因。静默跳过是最坏的一种失败：用户以为装上了，现场什么都看不出来。

装载器按路径显式加载用户包，**不往 ``sys.path`` 里塞目录** —— 否则第三方代码可以
悄悄遮蔽内核模块，这种"装个适配包把工具装坏"的坑不该存在。
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from gametrans.engines.base import EngineSupportPack
from gametrans.engines.manifest import PackManifest, read_manifest
from gametrans.engines.protocol import (
    PROTOCOL_VERSION,
    ProtocolSpecError,
    format_spec,
    matches,
)
from gametrans.engines.registry import (
    SOURCE_BUILTIN,
    SOURCE_USER,
    EngineRegistry,
    PackOrigin,
    RejectedPack,
)
from gametrans.errors import EngineError
from gametrans.userconfig import engines_dir

__all__ = ["PACK_MANIFEST_NAME", "load_registry"]

#: 与身份声明同名的常量从这里再导出一次（调用方只需要认识装载器）。
PACK_MANIFEST_NAME = "engine.json"

#: 用户包在 ``sys.modules`` 里的模块名前缀。带前缀是为了可辨认，不污染别的名字。
_MODULE_PREFIX = "gametrans_loaded_engine_"


class _Refused(Exception):
    """有理由的拒绝 —— 理由本身是要给用户看的。"""


@dataclass(frozen=True)
class _Candidate:
    directory: Path
    source: str
    manifest: PackManifest


def load_registry(
    *,
    builtin_root: Path | None = None,
    user_root: Path | None = None,
) -> EngineRegistry:
    """装载全部适配包，返回名册（含被拒清单与说明）。

    ``builtin_root`` 默认是随内核发布的那个引擎目录；``user_root`` 默认是全局层里的
    ``engines/``。两个参数只在测试与"换个地方装"时才需要传。
    """
    builtin_root = (
        Path(builtin_root) if builtin_root is not None else Path(__file__).resolve().parent
    )
    user_root = Path(user_root) if user_root is not None else engines_dir()

    rejected: list[RejectedPack] = []
    problems: list[str] = []

    builtins = _discover(builtin_root, SOURCE_BUILTIN, rejected)
    users = _discover(user_root, SOURCE_USER, rejected)

    builtin_by_name = {candidate.manifest.name: candidate for candidate in builtins}
    users_by_name: dict[str, list[_Candidate]] = {}
    for candidate in users:
        users_by_name.setdefault(candidate.manifest.name, []).append(candidate)

    packs: list[EngineSupportPack] = []
    origins: dict[str, PackOrigin] = {}
    shadows: dict[str, PackOrigin] = {}
    taken: set[str] = set()

    # 先装用户那份。**装成了才作数** —— 不然后面内置那份已经被"接管"掉了，
    # 用户只会看到"这个引擎消失了"，而磁盘上明明还躺着一份能用的内置包。
    for name, group in sorted(users_by_name.items()):
        builtin = builtin_by_name.get(name)
        if len(group) > 1:
            # 同名冲突：不静默二选一（选错了用户永远查不出来）。
            for candidate in group:
                rejected.append(
                    RejectedPack(
                        name=name,
                        source=SOURCE_USER,
                        location=str(candidate.directory),
                        version=candidate.manifest.version,
                        reason=(
                            f"有 {len(group)} 个本地适配包都叫 {name!r}，"
                            "无法判断该用哪一个，因此一个都没装"
                        ),
                    )
                )
            problems.append(_fallback_note(name, builtin, len(group)))
            continue

        candidate = group[0]
        try:
            pack = _instantiate(candidate, problems)
        except _Refused as exc:
            rejected.append(_rejection(candidate, str(exc)))
            problems.append(_fallback_note(name, builtin, 1))
            continue
        except Exception as exc:  # 第三方代码：什么都可能发生，不许拖垮整份名册
            rejected.append(_rejection(candidate, f"导入失败：{type(exc).__name__}: {exc}"))
            problems.append(_fallback_note(name, builtin, 1))
            continue

        packs.append(pack)
        taken.add(name)
        origins[name] = PackOrigin(
            source=SOURCE_USER,
            location=str(candidate.directory),
            version=candidate.manifest.version,
            protocol=candidate.manifest.protocol,
            authors=candidate.manifest.authors,
        )
        if builtin is not None:
            shadows[name] = _origin_of_builtin(builtin)

    for name, candidate in sorted(builtin_by_name.items()):
        if name in taken:
            continue
        try:
            pack = _instantiate(candidate, problems)
        except _Refused as exc:
            rejected.append(_rejection(candidate, str(exc)))
            continue
        except Exception as exc:
            rejected.append(_rejection(candidate, f"导入失败：{type(exc).__name__}: {exc}"))
            continue
        packs.append(pack)
        origins[name] = _origin_of_builtin(candidate)

    packs.sort(key=lambda pack: pack.name)
    return EngineRegistry(
        packs,
        origins=origins,
        shadows=shadows,
        rejected=rejected,
        problems=problems,
    )


def _origin_of_builtin(candidate: _Candidate) -> PackOrigin:
    return PackOrigin(
        source=SOURCE_BUILTIN,
        location=str(candidate.directory),
        version=candidate.manifest.version,
        protocol=candidate.manifest.protocol,
        authors=candidate.manifest.authors,
    )


def _fallback_note(name: str, builtin: _Candidate | None, count: int) -> str:
    """本地那份没装上时，说清"现在到底是谁在生效"。"""
    what = (
        f"本地有 {count} 个适配包叫 {name!r}（已全部忽略）"
        if count > 1
        else f"本地适配包 {name!r} 没装上（见被拒清单）"
    )
    if builtin is None:
        return f"{what}；该引擎当前不可用。"
    return f"{what}；当前生效的是内置包 {builtin.manifest.version}。"


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #


def _discover(root: Path, source: str, rejected: list[RejectedPack]) -> list[_Candidate]:
    """扫一个目录里的适配包（一层，一个子目录一个包）。"""
    root = Path(root)
    if not root.is_dir():
        return []
    candidates: list[_Candidate] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        if directory.name.startswith((".", "_")):
            continue  # 缓存目录之类，不是"想装但装不上"
        try:
            manifest = read_manifest(directory)
        except EngineError as exc:
            rejected.append(
                RejectedPack(
                    name=directory.name,
                    source=source,
                    location=str(directory),
                    reason=_render(exc),
                )
            )
            continue
        candidates.append(_Candidate(directory=directory, source=source, manifest=manifest))
    return candidates


def _rejection(candidate: _Candidate, reason: str) -> RejectedPack:
    return RejectedPack(
        name=candidate.manifest.name,
        source=candidate.source,
        location=str(candidate.directory),
        version=candidate.manifest.version,
        reason=reason,
    )


def _render(exc: EngineError) -> str:
    return f"{exc.message}（{exc.hint}）" if exc.hint else exc.message


# --------------------------------------------------------------------------- #
# 实例化
# --------------------------------------------------------------------------- #


def _instantiate(candidate: _Candidate, problems: list[str]) -> EngineSupportPack:
    manifest = candidate.manifest
    _check_protocol(manifest)
    module = _import_module(candidate)

    target = getattr(module, manifest.entry, None)
    if target is None:
        raise _Refused(
            f"模块里没有 {manifest.entry!r} 这个属性 —— "
            "声明里的 entry 要指向适配包实例（或它的类）"
        )
    pack = target() if isinstance(target, type) else target
    if not isinstance(pack, EngineSupportPack):
        raise _Refused(
            f"{manifest.entry!r} 不是 EngineSupportPack 的实例，"
            "内核不认识这个对象"
        )

    if str(pack.name or "") != manifest.name:
        raise _Refused(
            f"声明里写的是 {manifest.name!r}，实现却自称 {pack.name!r} —— "
            "名字必须唯一权威，两处不一致就没法确定这个包装的到底是哪个引擎"
        )
    if str(pack.version or "") != manifest.version:
        problems.append(
            f"适配包 {manifest.name!r} 的声明版本是 {manifest.version}，"
            f"实现却自称 {pack.version}；以声明为准，请作者改齐。"
        )
        pack.version = manifest.version
    return pack


def _check_protocol(manifest: PackManifest) -> None:
    try:
        accepted = matches(manifest.protocol)
    except ProtocolSpecError as exc:
        raise _Refused(f"协议区间写法看不懂：{_render(exc)}") from None
    if accepted:
        return
    raise _Refused(
        f"它声明支持适配协议 {format_spec(manifest.protocol)}"
        f"（写法 {manifest.protocol!r}），本内核的适配协议版本是 {PROTOCOL_VERSION}。"
        "升级内核，或换一个支持这个协议版本的适配包 —— "
        "内核不会为旧协议铺兼容垫片，那会把引擎隔离这条边界从后面掏空"
    )


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #


def _import_module(candidate: _Candidate):
    if candidate.source == SOURCE_BUILTIN:
        return importlib.import_module(f"gametrans.engines.{candidate.directory.name}")
    return _import_from_path(candidate)


def _import_from_path(candidate: _Candidate):
    """按路径装载一个用户适配包。

    模块名是合成的（``gametrans_loaded_engine_<名字>``），所以**不进 sys.path**、
    也盖不住内核模块。每次装载前清掉同名旧模块：同一个进程里反复装载
    （测试、面板里改完再看）必须读到当前的代码，而不是上一次的缓存。
    """
    entry = candidate.directory / "__init__.py"
    if not entry.is_file():
        raise _Refused(f"{candidate.directory} 里没有 __init__.py，无法作为适配包装载")

    module_name = _MODULE_PREFIX + re.sub(r"\W", "_", candidate.manifest.name)
    for key in [
        key
        for key in list(sys.modules)
        if key == module_name or key.startswith(f"{module_name}.")
    ]:
        del sys.modules[key]

    spec = importlib.util.spec_from_file_location(
        module_name, entry, submodule_search_locations=[str(candidate.directory)]
    )
    if spec is None or spec.loader is None:
        raise _Refused(f"{candidate.directory} 无法作为 Python 包装载")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module
