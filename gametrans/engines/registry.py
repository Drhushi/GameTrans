"""引擎支持包注册表 —— 只懂机制，不认识任何具体引擎。

名册对用户可见：``gametrans engine list`` 就是它的渲染结果。用户"装一个引擎适配包"
这件事，落到代码上就是把一个目录放进引擎目录，由
:mod:`gametrans.engines.loader` 装载后 :meth:`EngineRegistry.register` 进这个名册。

注册表除了"有哪些包"，还必须回答"**它从哪来、什么版本、盖掉了谁、谁被拒了**"：
适配包可以独立发布、独立升级、甚至被用户魔改，这几样答不出来，出了事就只能猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from gametrans.engines.base import EngineDetection, EngineSupportPack
from gametrans.errors import EngineError

__all__ = ["EngineRegistry", "PackOrigin", "RejectedPack", "default_registry"]

#: 适配包来源的两档取值。
SOURCE_BUILTIN = "builtin"
SOURCE_USER = "user"


@dataclass(frozen=True)
class PackOrigin:
    """一个适配包从哪来 —— 面板、报告、报错都要能说出这件事。"""

    source: str
    location: str
    version: str = ""
    protocol: str = ""
    authors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "location": self.location,
            "version": self.version,
            "protocol": self.protocol,
            "authors": list(self.authors),
        }


@dataclass(frozen=True)
class RejectedPack:
    """一个**想装但没装成**的适配包。

    静默跳过是这个设计里最不能接受的一种失败：用户以为装上了、实际没生效，
    现场又什么都看不出来。所以每个被拒的目录都要在这里留一条，带原因。
    """

    name: str
    source: str
    location: str
    version: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "location": self.location,
            "version": self.version,
            "reason": self.reason,
        }


class EngineRegistry:
    """已装载引擎支持包的容器。"""

    def __init__(
        self,
        packs: Iterable[EngineSupportPack] = (),
        *,
        origins: dict[str, PackOrigin] | None = None,
        shadows: dict[str, PackOrigin] | None = None,
        rejected: Iterable[RejectedPack] = (),
        problems: Iterable[str] = (),
    ) -> None:
        self._packs: dict[str, EngineSupportPack] = {}
        self._origins: dict[str, PackOrigin] = dict(origins or {})
        #: 名字 → 被它盖掉的那个包（目前只可能是内置包）
        self._shadows: dict[str, PackOrigin] = dict(shadows or {})
        self._rejected: list[RejectedPack] = list(rejected)
        self._problems: list[str] = list(problems)
        for pack in packs:
            self.register(pack)

    # ---- 名册 -------------------------------------------------------------

    def register(
        self,
        pack: EngineSupportPack,
        *,
        origin: PackOrigin | None = None,
        shadows: PackOrigin | None = None,
    ) -> EngineSupportPack:
        if not pack.name:
            raise EngineError("引擎支持包必须声明 name")
        if pack.name in self._packs:
            raise EngineError(
                f"引擎支持包 {pack.name!r} 已经注册过了",
                hint="同一个引擎名只能有一个包；请先卸载或改名。",
            )
        self._packs[pack.name] = pack
        if origin is not None:
            self._origins[pack.name] = origin
        if shadows is not None:
            self._shadows[pack.name] = shadows
        return pack

    def unregister(self, name: str) -> None:
        if name not in self._packs:
            raise EngineError(
                f"未注册的引擎支持包：{name!r}",
                hint=f"当前可用：{', '.join(self.names()) or '（无）'}",
            )
        del self._packs[name]

    def get(self, name: str) -> EngineSupportPack:
        try:
            return self._packs[name]
        except KeyError:
            available = ", ".join(self.names()) or "（无）"
            raise EngineError(
                f"未找到引擎支持包：{name!r}",
                hint=(
                    f"当前已注册：{available}。"
                    "用 `gametrans engine list` 查看可用引擎与**被拒的包**；"
                    f"装新的适配包就是把它的目录放进 {_engines_dir()}。"
                ),
            ) from None

    def names(self) -> list[str]:
        return sorted(self._packs)

    def all(self) -> list[EngineSupportPack]:
        return [self._packs[name] for name in self.names()]

    def __contains__(self, name: object) -> bool:
        return name in self._packs

    def __iter__(self) -> Iterator[EngineSupportPack]:
        return iter(self.all())

    def __len__(self) -> int:
        return len(self._packs)

    # ---- 来路与记账 -------------------------------------------------------

    def origins(self) -> dict[str, PackOrigin]:
        return dict(self._origins)

    def shadows(self) -> dict[str, PackOrigin]:
        return dict(self._shadows)

    def rejected(self) -> list[RejectedPack]:
        return list(self._rejected)

    def problems(self) -> list[str]:
        return list(self._problems)

    def origin_of(self, name: str) -> dict[str, Any]:
        """一个包的来路（没有记录时如实说"不知道"，不假装是内置的）。"""
        origin = self._origins.get(name)
        payload = (
            origin.to_dict()
            if origin is not None
            else {"source": "unknown", "location": "", "version": "", "protocol": ""}
        )
        shadow = self._shadows.get(name)
        payload["shadows"] = shadow.to_dict() if shadow is not None else None
        return payload

    def describe_all(self) -> list[dict[str, Any]]:
        """名册的完整视图：能力自述 + 来自哪、盖掉了谁。"""
        described: list[dict[str, Any]] = []
        for pack in self.all():
            info = pack.describe()
            info.update(self.origin_of(pack.name))
            described.append(info)
        return described

    # ---- 探测 -------------------------------------------------------------

    def detect(self, project_root: Path) -> list[EngineDetection]:
        """对每个已注册引擎做一次宽容探测，按置信度降序返回。"""
        results = [pack.detect(project_root) for pack in self.all()]
        results.sort(key=lambda d: (-d.confidence, d.engine))
        return results


def _engines_dir() -> str:
    """用户放适配包的目录。只有报错提示用它，所以按需取，避免模块级依赖。"""
    from gametrans.userconfig import engines_dir

    return str(engines_dir())


def default_registry() -> EngineRegistry:
    """默认名册：随内核发布的适配 + 本机用户装的适配，走同一条装载路径。

    这里用一个函数内 import 指向组合根，避免 ``registry`` ↔ ``bootstrap``
    的循环依赖。``gametrans.bootstrap`` 不是具体引擎，因此不违反隔离约束。
    """
    from gametrans.bootstrap import build_registry

    return build_registry()
