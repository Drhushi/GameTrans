"""RPGM 数据文件：在哪、读哪一份、怎么读坏。

**这是引擎私有知识**（内核不认识 ``www/``、``data/compressed/``、``SRD_DataCompressor``）：

* RPG Maker MV 发布版把游戏内容放在 ``www/`` 下（编辑器工程则是根目录下直接就是
  ``data/`` + ``js/``）—— 两种布局都要认；
* ``data/*.json`` 是引擎的数据库（System / Actors / Items / MapNNN / CommonEvents …）；
* 装了 ``SRD_DataCompressor`` 且 ``Read from Compression`` 为真时，引擎**只读**
  ``data/compressed/*.json``（LZString-Base64），明文那份作者通常会删掉；
* 同一个目录里还躺着引擎运行时（``js/rpg_core.js``、``js/libs/``）—— 那是引擎自己的
  代码，不是游戏内容。

**数据源选择的原则**：引擎实际会读哪一份，我们就读哪一份，并把**依据**报出来。
判据读不出来时（没装那个插件）退化成"哪个目录有数据就读哪个"，同样说明退化原因。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gametrans.engines.rpgm.lzstring import LZStringError, decompress_from_base64
from gametrans.errors import ProjectError

__all__ = [
    "DataSource",
    "RpgmDataError",
    "load_data",
    "read_plugins",
    "resolve_www_root",
    "select_source",
]

DATA_DIR = "data"
COMPRESSED_DIR = "compressed"
PLUGIN_TABLE = "js/plugins.js"
PLUGIN_SOURCE_DIR = "js/plugins"
FONT_DIR = "fonts"
COMPRESSOR_PLUGIN = "SRD_DataCompressor"
READ_FROM_COMPRESSION = "Read from Compression"


class RpgmDataError(ProjectError):
    """数据文件找不到、读不了、或解不出来。"""


@dataclass
class DataSource:
    """这一份工程的数据在哪、以及为什么是它。"""

    www_root: Path
    #: ``compressed`` | ``plain``
    kind: str
    directory: Path
    #: www 根相对路径（``data/compressed/Map001.json``）→ 绝对路径
    files: dict[str, Path] = field(default_factory=dict)
    #: 判定依据（给人看的一句话，逐条对应一个事实）
    evidence: list[str] = field(default_factory=list)

    @property
    def layout(self) -> str:
        return self.directory.name if self.kind == "compressed" else DATA_DIR


# --------------------------------------------------------------------------- #
# 定位
# --------------------------------------------------------------------------- #


def _looks_like_www(candidate: Path) -> bool:
    """一个目录像不像"引擎真正读的那一层"。"""
    if not candidate.is_dir():
        return False
    return (
        (candidate / "js").is_dir()
        or (candidate / DATA_DIR).is_dir()
        or (candidate / "index.html").is_file()
    )


def resolve_www_root(project_root: Path) -> Path | None:
    """找出引擎真正读的那一层：``<工程>/www`` 或 ``<工程>`` 本身。

    RPG Maker MV 的发布版是 ``Game.exe`` + ``www/``；编辑器工程与某些解包版
    直接把 ``data/`` + ``js/`` 放在根目录。两种都要认，认不出就返回 ``None``
    （由调用方决定这是"不是本引擎的工程"还是"缺文件"）。
    """
    project_root = Path(project_root)
    if not project_root.is_dir():
        return None
    nested = project_root / "www"
    if _looks_like_www(nested):
        return nested
    if _looks_like_www(project_root):
        return project_root
    return None


def read_plugins(www_root: Path) -> list[dict[str, Any]]:
    """读引擎自己那份插件表（``js/plugins.js`` 里的 ``var $plugins = [...]``）。

    读不出来不算错误：没有插件表只说明"判据缺失"，由数据源选择退回保守策略并说明。
    """
    path = Path(www_root) / PLUGIN_TABLE
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    start = text.find("[")
    if start < 0:
        return []
    try:
        payload, _end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def _json_files(directory: Path) -> dict[str, Path]:
    if not directory.is_dir():
        return {}
    return {
        path.name: path for path in sorted(directory.glob("*.json")) if path.is_file()
    }


def select_source(project_root: Path) -> DataSource:
    """选出引擎实际会读的那一份数据，并给出依据。"""
    project_root = Path(project_root)
    if not project_root.exists():
        raise ProjectError(
            f"项目路径不存在：{project_root}",
            hint="确认路径拼写，或先用 `gametrans project init <路径>` 建立新工程。",
        )

    www = resolve_www_root(project_root)
    if www is None:
        raise RpgmDataError(
            f"{project_root} 里没有 RPG Maker MV 的工程结构",
            hint=(
                "期望看到 `www/js/`（发布版）或根目录下直接有 `js/` + `data/`（编辑器工程）。"
            ),
        )

    compressed = _json_files(www / DATA_DIR / COMPRESSED_DIR)
    plain = _json_files(www / DATA_DIR)
    evidence: list[str] = []

    plugins = read_plugins(www)
    compressor = next(
        (entry for entry in plugins if entry.get("name") == COMPRESSOR_PLUGIN), None
    )

    prefer_compressed = True
    if compressor is None:
        evidence.append(
            f"没有安装数据压缩插件（{COMPRESSOR_PLUGIN} 不在 js/plugins.js 里）："
            "读不出引擎的参数，按「哪个目录有数据就读哪个」处理"
        )
    else:
        raw = str((compressor.get("parameters") or {}).get(READ_FROM_COMPRESSION, ""))
        prefer_compressed = raw.strip().lower() == "true"
        evidence.append(
            f"引擎参数 {COMPRESSOR_PLUGIN} 的 {READ_FROM_COMPRESSION}={raw!r}"
            f"（{PLUGIN_TABLE}）"
        )

    primary = compressed if prefer_compressed else plain
    fallback = plain if prefer_compressed else compressed
    primary_kind = "compressed" if prefer_compressed else "plain"
    fallback_kind = "plain" if prefer_compressed else "compressed"

    if primary:
        kind, directory, files = primary_kind, _directory_of(www, primary_kind), primary
    elif fallback:
        kind, directory, files = fallback_kind, _directory_of(www, fallback_kind), fallback
        evidence.append(
            f"引擎本该读 {primary_kind}，但那个目录里没有数据文件；"
            f"退到 {fallback_kind}"
        )
    else:
        raise RpgmDataError(
            f"{www} 下找不到任何 RPGM 数据文件（既没有 {DATA_DIR}/{COMPRESSED_DIR}/，"
            f"也没有 {DATA_DIR}/）",
            hint="确认这是发布版游戏目录的 www/（或编辑器工程的根目录）。",
        )

    evidence.append(f"数据来源：{kind}（{directory}）共 {len(files)} 个文件")
    return DataSource(
        www_root=www,
        kind=kind,
        directory=directory,
        files={
            path.relative_to(www).as_posix(): path for path in files.values()
        },
        evidence=evidence,
    )


def _directory_of(www: Path, kind: str) -> Path:
    return www / DATA_DIR / COMPRESSED_DIR if kind == "compressed" else www / DATA_DIR


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def load_data(source: DataSource) -> dict[str, Any]:
    """按数据源把每个数据文件读成 Python 对象，键是文件名。

    坏在哪一步要说清楚（是 LZString 解不开，还是解开了但不是 JSON）——
    "某个文件悄悄没了"是最难查的一类问题。
    """
    loaded: dict[str, Any] = {}
    for name, path in source.files.items():
        basename = Path(name).name
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RpgmDataError(f"读不了数据文件 {name}：{exc}") from exc

        if source.kind == "compressed":
            try:
                raw = decompress_from_base64(raw)
            except LZStringError as exc:
                raise RpgmDataError(
                    f"数据文件 {name} 不是合法的 LZString-Base64：{exc}",
                    hint=(
                        "这份工程的数据被 SRD_DataCompressor 压过；"
                        "确认文件没有被别的工具改坏，或改用未压缩的发布版。"
                    ),
                ) from exc
            if not raw:
                raise RpgmDataError(f"数据文件 {name} 解出来是空的（LZString 位流没有内容）")

        try:
            loaded[basename] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RpgmDataError(
                f"数据文件 {name} 解出来不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
            ) from exc
    return loaded
