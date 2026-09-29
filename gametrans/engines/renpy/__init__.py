"""Ren'Py 引擎支持包 —— 本项目的第一个引擎适配，也是一个可复制的样板。

要把 gametrans 接到别的引擎（RPG Maker、KiriKiri……），照抄这个目录的骨架即可：
一个 :class:`~gametrans.engines.base.EngineSupportPack` 子类 + 自己的解析/写回实现 +
一份 ``engine.json``（名字 / 版本 / 支持的适配协议 / 入口）。然后把这个目录放进
引擎目录（``~/.gametrans/engines/``）就会被装载 —— **内核一行都不用改**。
"""

from __future__ import annotations

__version__ = "0.1.1"

from gametrans.engines.renpy.pack import RenPyPack

__all__ = ["RenPyPack", "__version__"]
