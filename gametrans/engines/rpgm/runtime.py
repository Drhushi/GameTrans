"""RPGM 运行时翻译插件：把内容清单落成一份可安装的运行时产物。

这条路线（契约 R37 的路线乙）的形状：

* **游戏数据零改动** —— 插件在引擎载入数据之后改写内存里的 `$dataSystem` /
  `$dataItems` / `$dataMap` 等对象，而不是去改磁盘上的 JSON；
* 因此**一份产物可以带多种语言**，玩家在设置菜单里切；
* 译文表**按位置定键**（`Map044.json#events[28].pages[1].list[21].parameters[0][3]`），
  于是 R35 的"同原文不同位置可以不同译"在这里**一条都不丢**（社区那类按原文定键的
  方案会塌掉 23.5%，见 R40）。

译文表里的"路径"是**引擎数据结构里的位置**，不是文件行号 —— 数据文件是压缩存储的，
按行定位既不可靠也没意义。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "INSTALL_NOTE_NAME",
    "ORIGINAL_LANGUAGE",
    "PLUGIN_FILE_NAME",
    "PLUGIN_NAME",
    "TABLE_DIR",
    "build_table",
    "install_note",
    "merge_plugin_table",
    "plugin_entry",
    "plugin_source",
    "plugin_path",
    "write_bundle",
]

#: 插件文件与插件名（`js/plugins.js` 里要写的名字）。
PLUGIN_FILE_NAME = "GametransRuntime.js"
PLUGIN_NAME = "GametransRuntime"
#: 译文表放哪（相对引擎的 web 根，也就是 `www/`）。
TABLE_DIR = "translations"
#: 补丁里那份安装说明 —— 这条路线要人工解压，说明得跟着走。
INSTALL_NOTE_NAME = "GAMETRANS-INSTALL.txt"
#: 源语言已知不了时，那一份原文表用的语言代码（它不是一个语言主张，只是"原文"）。
ORIGINAL_LANGUAGE = "original"

_PLUGIN_SOURCE = Path(__file__).resolve().parent / "runtime" / PLUGIN_FILE_NAME


def plugin_path() -> Path:
    return _PLUGIN_SOURCE


def plugin_source() -> str:
    """插件源码（随补丁分发的那一份）。

    读不出来是**硬错误**：产物缺了插件就没法工作，不能悄悄产出一个半成品。
    """
    if not _PLUGIN_SOURCE.is_file():
        raise FileNotFoundError(f"运行时插件源码缺失：{_PLUGIN_SOURCE}")
    return _PLUGIN_SOURCE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 译文表
# --------------------------------------------------------------------------- #


def _string_paths(unit: Any, text: str) -> list[tuple[str, str]]:
    """一条 unit → 它在引擎数据里**真正存放字符串的那个位置**的路径列表。

    注意 unit 的结构路径锚在 ``list[101 的下标]`` 上（"一条消息"的起点），
    而正文其实在紧随的 401 行的 ``parameters[0]`` 里 —— 所以这里要换算成
    字符串自己的位置，插件才不用认识"消息块"这个概念。

    多行消息返回**多条** ``(路径, 该行译文)``：译文按原行数重新折行，
    这样游戏里不会多出空行。
    """
    payload = unit.locator.payload
    structural = payload["structural_path"]
    file_name, _, rest = structural.partition("#")
    category = payload.get("category", "")

    if unit.type == "dialogue" and "text_indices" in payload:
        lines = _rewrap(text, len(payload["text_indices"]))
        prefix = rest.rsplit(".list[", 1)[0]
        return [
            (f"{file_name}#{prefix}.list[{index}].parameters[0]", line)
            for index, line in zip(payload["text_indices"], lines)
        ]

    if unit.type == "choice":
        anchor = payload["choice_command_index"]
        choice_index = payload["choice_index"]
        prefix = rest.rsplit(".list[", 1)[0]
        return [
            (f"{file_name}#{prefix}.list[{anchor}].parameters[0][{choice_index}]", text)
        ]

    if "scroll_text_index" in payload:
        anchor = payload["scroll_text_index"]
        prefix = rest.rsplit(".list[", 1)[0]
        return [(f"{file_name}#{prefix}.list[{anchor}].parameters[0]", text)]

    if "command_index" in payload:
        anchor = payload["command_index"]
        prefix = rest.rsplit(".list[", 1)[0]
        return [(f"{file_name}#{prefix}.list[{anchor}].parameters[1]", text)]

    # 数据库字段与 System.json 的词条：结构路径本身就是字符串的位置
    return [(structural, text)]


def _rewrap(text: str, line_count: int) -> list[str]:
    """把一整段译文按原来的行数重新折行。

    原消息有 N 行 401，游戏就渲染 N 行；译文只有一段，所以按字符数均分。
    中文没有词边界，均分是安全的；N=1 时（真靶 2,694/2,751 都是）原样返回。
    """
    if line_count <= 1:
        return [text]
    if "\n" in text:
        parts = text.split("\n")
        if len(parts) == line_count:
            return parts
        text = "".join(parts)
    width = max(1, -(-len(text) // line_count))
    chunks = [text[i : i + width] for i in range(0, len(text), width)]
    while len(chunks) < line_count:
        chunks.append("")
    # 多出来的尾巴并进最后一行，宁可某行偏长也不要凭空多出一行
    if len(chunks) > line_count:
        chunks = chunks[: line_count - 1] + ["".join(chunks[line_count - 1 :])]
    return chunks


def build_table(pairs: Iterable[tuple[Any, str]]) -> dict[str, str]:
    """把 ``(unit, 要写进去的文本)`` 落成 ``{字符串路径: 文本}``。

    **调用方负责筛掉不该进产物的译文**（没过闸门的、状态非 ok 的）——
    所以这里不看不问"这算不算可用"，只做位置换算。这样"谁被挡下了"这件事
    只有一个地方说了算（写回层与导出前校验），不会有两套判据。

    同一份函数同时用来造两种表：目标语言传译文，源语言传原文。
    """
    table: dict[str, str] = {}
    for unit, text in pairs:
        if not text or not text.strip():
            continue
        for path, value in _string_paths(unit, text):
            table[path] = value
    return dict(sorted(table.items()))


def plugin_entry(
    *, default_language: str = "zh", languages: Iterable[str] | None = None
) -> dict[str, Any]:
    """我们在引擎插件表里的那一条。

    ``Languages`` 是插件判断"要不要往设置菜单加语言行"的依据 —— 只带一种语言时
    加一行切不动的菜单项是噪声。
    """
    shipped = [str(item) for item in (languages or [default_language]) if str(item)]
    if default_language in shipped:
        shipped = [default_language] + [item for item in shipped if item != default_language]
    return {
        "name": PLUGIN_NAME,
        "status": True,
        "description": "gametrans 运行时翻译：载入后改写数据 + 设置菜单里切换语言。",
        "parameters": {
            "Default Language": default_language,
            "Languages": ",".join(shipped),
            "Translation Path": f"{TABLE_DIR}/",
        },
    }


def merge_plugin_table(
    original: str,
    *,
    default_language: str = "zh",
    languages: Iterable[str] | None = None,
) -> str:
    """把我们的插件条目**并入**引擎原有的插件表。

    这条纪律与 Ren'Py 侧"只换字面量、其余一字不动"是同一件事：那张表里装着游戏
    十来个插件的全部参数（菜单列表、字体、存档文案……），抹掉任何一条，游戏的行为
    就变了 —— 而"游戏数据零改动"这句话也就不成立了。

    幂等：重复并入不会长出第二条；已经存在时按新参数替换那一条。
    """
    start = original.find("[")
    if start < 0:
        raise ValueError("插件表里没有找到 `[` —— 这不是一份 RPG Maker 的 plugins.js")
    try:
        existing, _end = json.JSONDecoder().raw_decode(original, start)
    except json.JSONDecodeError as exc:
        raise ValueError(f"插件表不是合法 JSON：{exc}") from exc
    if not isinstance(existing, list):
        raise ValueError("插件表不是一个数组")

    table = [
        entry
        for entry in existing
        if not (isinstance(entry, dict) and entry.get("name") == PLUGIN_NAME)
    ]
    table.append(plugin_entry(default_language=default_language, languages=languages))

    return (
        "// Generated by RPG Maker.\n"
        "// Do not edit this file directly.\n"
        "var $plugins =\n"
        + json.dumps(table, ensure_ascii=False, indent=0)
        + ";\n"
    )


def write_bundle(
    output_dir: Path,
    *,
    tables: dict[str, dict[str, str]],
    default_language: str,
    original_plugin_table: str,
) -> list[Path]:
    """把可安装的一套产物写到 ``output_dir``（目录结构对齐引擎的 web 根）。

    产出：

    * ``js/plugins/GametransRuntime.js`` —— 插件本体；
    * ``js/plugins.js`` —— 引擎的插件表 + 我们那一条（原有条目一条不改）；
    * ``translations/<语言>.json`` —— **每一种**语言一份按位置定键的译文表。

    安装方式就是把这三样覆盖到游戏的 ``www/`` 下。游戏的数据文件一个字都不动。
    """
    output_dir = Path(output_dir)
    written: list[Path] = []

    plugin_dest = output_dir / "js" / "plugins" / PLUGIN_FILE_NAME
    plugin_dest.parent.mkdir(parents=True, exist_ok=True)
    plugin_dest.write_text(plugin_source(), encoding="utf-8")
    written.append(plugin_dest)

    for language in sorted(tables):
        table_dest = output_dir / TABLE_DIR / f"{language}.json"
        table_dest.parent.mkdir(parents=True, exist_ok=True)
        table_dest.write_text(
            json.dumps(tables[language], ensure_ascii=False, indent=0), encoding="utf-8"
        )
        written.append(table_dest)

    note_dest = output_dir / INSTALL_NOTE_NAME
    note_dest.write_text(
        install_note(tables=tables, default_language=default_language), encoding="utf-8"
    )
    written.append(note_dest)

    plugins_dest = output_dir / "js" / "plugins.js"
    plugins_dest.parent.mkdir(parents=True, exist_ok=True)
    plugins_dest.write_text(
        merge_plugin_table(
            original_plugin_table,
            default_language=default_language,
            languages=sorted(tables),
        ),
        encoding="utf-8",
    )
    written.append(plugins_dest)

    return written


def install_note(*, tables: dict[str, dict[str, str]], default_language: str) -> str:
    """补丁里那份说明。这条路线要人工解压，所以"装哪儿、怎么切、怎么卸"必须跟着走。"""
    names = sorted(tables)
    table_lines = "\n".join(
        f"    www\\{TABLE_DIR}\\{name}.json"
        + ("        ← 译文表（按位置定键）" if name == default_language else "        ← 原文那一份，供切回原文")
        for name in names
    )
    return f"""gametrans 运行时翻译补丁
========================================

怎么安装
----------------------------------------
把本压缩包里的内容**原样解压覆盖到游戏的 www/ 目录**：

    <游戏>\\www\\

装完应该是这样：

    www\\js\\plugins\\{PLUGIN_FILE_NAME}        ← 翻译插件
    www\\js\\plugins.js                         ← 引擎的插件表（多了我们那一条，其余一条不改）
{table_lines}

游戏自己的数据文件（data\\、img\\、audio\\、fonts\\ 等）**一个都没改** ——
译文是在游戏启动后由插件改写内存里的文本，不是替换文件。

怎么切语言
----------------------------------------
进游戏 → 设置（Options / Configuration）→ 有一行 “Language / 语言”，
选中后按确定键或左右键切换。选择会存进设置存档（config.rpgsave），下次进游戏还是它。

本补丁带了 {len(names)} 种：{'、'.join(names)}。默认 {default_language}。

怎么卸载
----------------------------------------
删掉这两处：

    www\\{TABLE_DIR}\\                 （整个目录）
    www\\js\\plugins\\{PLUGIN_FILE_NAME}

再把 www\\js\\plugins.js 里 name 为 "{PLUGIN_NAME}" 的那一条删掉即可。
游戏数据从未被改动，所以不需要还原任何东西。

出问题怎么查
----------------------------------------
按 F12 打开控制台，看有没有 gametrans 开头的报错（译文表读不到会打印警告）。
译文表放在 www\\{TABLE_DIR}\\ 下，文件名就是语言代码。
"""
