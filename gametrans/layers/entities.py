"""实体候选：把「这本书里有哪些专名」从已生成的场摘要里抽出来，**零模型成本**。

摘要本来就在开翻之前覆盖全书：每场的摘要里就有【出场人物】与【新出现的信息】，
把它机械抽一遍，就够起一份实体清单。这一步只做三件事：

* **抽** —— 从每场摘要里取专名候选（**只取写法**）；
* **并** —— 同一个实体的不同写法并成一条（长短名、大小写 / 下划线）；
* **核** —— 拿图上的原文数"全靶命中数"：这个词在原文里真出现过几次、跨了几个单元
  （``Saki Natsume`` 也要能对上原文里的 ``Saki_Natsume``，所以下划线当空格算）。

**只有写法，没有译名、没有设定**：
这一步定不了译名 —— 卡里那个"中文译名（原文写法）"的写法是**模型在摘要里顺手起的名字**，
没有审核、一处一个决定的纪律也管不到它；设定同理，摘要里那句"关于这个实体的描述"是否
成立要人判断，不是机械抽取能担保的事。译名与设定由翻译时模型申报 + 人或 agent 审核来定
（见 :mod:`gametrans.layers.tags`：**没有译名的写法在原文里是 ``⟦写法⟧``**）。

落盘形状：新写法直接追加进术语书（一行一个实体、``target`` 空着），由
``TermBook.apply`` 保证"只追加、不覆盖已有的那一栏"。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.graph import PathGraph
from gametrans.errors import ProjectError
from gametrans.layers.resource import TermBook, TermEntry

__all__ = [
    "FIELDS",
    "KEEP_KINDS",
    "MIN_LETTERS",
    "analyze",
    "build",
    "character_names",
    "classify",
    "collect",
    "count_hits",
    "field_text",
    "harvest",
    "has_latin",
    "load_inputs",
    "merge_case",
    "unit_sources",
    "variants_for",
]

#: 摘要里专名最集中的两栏。
FIELDS = ("【出场人物】", "【新出现的信息】")

#: 收哪几类候选：其余类别（称呼/泛称、常见词、变量或泛称、占位符、其他）不落行。
KEEP_KINDS = ("人名", "组织/团体", "简称")

#: 摘要侧实体写进术语书时记在变更日志里的来源（`why` 里也带着它）。
PROVENANCE = "summary"

#: 拉丁词（可带**一个空格或下划线**，好让 `Saki Natsume` 对上 `Saki_Natsume`）。
#: 刻意不含换行：摘要是一行一条，跨行匹配会把两个不相干的名字粘成一个。
WORD = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*(?:[ _]+[A-Za-z][A-Za-z0-9'’\-]*){0,3}")

#: 译名里出现拉丁字母说明这一串不是名字。
LATIN = re.compile(r"[A-Za-z]")

#: 这些不是术语：英文虚词、章节词、罗马数字、各国冠词。
STOP = frozenset(
    """the a an and or of to in on at for with from by is are was were be been being this that these those
chapter scene end start new name note yes no not only but also as it its his her their our your my he she
they we you i ii iii iv v vi vii viii ix x de la le van von der die das und el los las""".split()
)

#: 称呼与泛称：按语境翻，不该做成机械替换的术语。
#: 角色泛称（Receptionist / Commentator 这类）**必须收在这里**：引擎把"说话人是谁"也
#: 打进角色表，于是它们能过"首字母大写 / 角色表"那道闸 —— 真靶上就是这么漏进来的。
ROLE_WORDS = frozenset(
    """girl girls boy boys guy guys man men woman women lady ladies dad mom father mother brother sister
son daughter coach doctor nurse officer clerk phone crowd voice stranger teacher student professor dean
mr mrs ms dr sir madam uncle aunt boss kid kids child children people person someone anyone everyone
receptionist commentator bartender barista driver nurse waitress waiter team teams staff announcer
manager assistant secretary guard janitor vendor barber tailor""".split()
)

#: 纯字母候选的最短长度：`St`（从 `St. Eden Avenue` 切下来的两字母）不是名字。
#: 三个字母的缩写要照旧通过（`BSU` / `SIT`），四个字母的更要（`NCAA`）。
MIN_LETTERS = 3

#: 组织/团体的线索词（命中了就当组织看，不当人名）。
ORG_HINT = (
    "university", "college", "school", "institute", "academy", "team", "wolves", "cats",
    "dragons", "club", "corp", "inc", "ltd", "association", "championship", "cup", "ncaa",
)

#: 普通名词：单蹦出来时多半不是术语（多词的 `SIT Alpha Wolves` 仍算组织）。
#: `team` 刻意不在这里 —— 它是**角色泛称**（球场上"Team"是一个说话人），归 `称呼/泛称`。
COMMON_NOUNS = frozenset(
    """wolf wolves cat cats dragon dragons night morning evening day days little big old young
school schools university city town street road bridge cafe park gym court game games match matches
chapter prologue epilogue ending endings story season round point points set sets""".split()
)

PLACEHOLDER = re.compile(r"[\[\]{}%]")
LOWER_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")
PERSON = re.compile(r"^[A-Z][A-Za-z'’\-]+(?:[\s_]+[A-Z][A-Za-z'’\-]+){0,2}$")

#: 卡片里"这里什么都没有"的写法 —— 它们不是描述。
_EMPTY_MARKS = ("未交代", "无", "没有", "（无）", "-")


def has_latin(term: str) -> bool:
    """这个写法的**原文侧是不是拉丁写法**（纯中日韩字符的写法不算）。

    为什么这条能当闸：术语书那一行的意义是"**原文里出现这个写法时**按它译"。摘要里有一批
    **本来就没有英文原文**的中文词（`篮球教练` / `解说员` / `坏结局` / `???`），
    它们落成行只会得到"原文命中 0"的死条目 —— 谁也没法在原文里撞上它。
    """
    return LATIN.search(str(term)) is not None


def classify(term: str) -> str:
    """给候选分个类 —— 只为了让人一眼扫过去，不是自动采用判据。"""
    if PLACEHOLDER.search(term):
        return "占位符"
    if LOWER_TOKEN.match(term):
        # mc / name / nvl 这类：通常是变量或泛称，多半不该进术语书
        return "变量或泛称"
    words = [word.lower() for word in re.split(r"[ _]+", term)]
    if words and all(word in ROLE_WORDS for word in words):
        return "称呼/泛称"
    low = term.lower()
    # 单蹦的普通名词（`Team` / `Little` / `Night`）不当专名：它们几乎都是句子里的词
    if len(words) == 1 and words[0] in COMMON_NOUNS:
        return "常见词"
    if any(hint in low for hint in ORG_HINT):
        return "组织/团体"
    if term.isupper() and len(term) >= 2:
        return "简称"
    if PERSON.match(term):
        return "人名"
    return "其他"


def merge_case(found: dict[str, dict]) -> dict[str, dict]:
    """把同一个名字的不同写法并成一条。

    两类合并：**大小写**（`Wild Cats` / `WILD CATS`）与**空格/下划线**
    （`Eve Herschel` / `Eve_Herschel` —— 稿子里写空格、引擎变量里写下滑线，
    是同一个人的同一个名字）。出现场次最多的写法当正条，其余记进 ``case_variants``。
    """
    buckets: dict[str, list[str]] = {}
    for term in found:
        key = re.sub(r"[ _]+", " ", term).lower()
        buckets.setdefault(key, []).append(term)
    merged: dict[str, dict] = {}
    for _key, terms in buckets.items():
        terms.sort(key=lambda term: (-len(found[term]["scenes"]), term))
        head, *rest = terms
        entry = dict(found[head])
        # 浅拷贝会把并进来的场次写回 `found`，同一份读数被用第二次就不干净了
        entry["scenes"] = list(entry.get("scenes") or [])
        entry["case_variants"] = rest
        for other in rest:
            for scene in found[other]["scenes"]:
                if scene not in entry["scenes"]:
                    entry["scenes"].append(scene)
        merged[head] = entry
    return merged


def field_text(summary: str, field: str) -> str:
    """摘要里某一栏的正文（到下一栏为止）。"""
    match = re.search(re.escape(field) + r"(.*?)(?=【|$)", summary or "", re.S)
    return match.group(1) if match else ""


def harvest(summaries: dict) -> dict[str, dict]:
    """``候选 → {scenes: [...], first: 场名}``（**只有写法与出处，没有译名与设定**）。

    场序 = 摘要里的出现顺序；写法照原文（WORD 抽出来的那个）。
    """
    found: dict[str, dict] = {}
    for scene, item in (summaries.get("scenes") or {}).items():
        text = "\n".join(field_text(item.get("summary") or "", field) for field in FIELDS)
        for match in WORD.finditer(text):
            term = match.group(0).strip(" '’-")
            if len(term) < 2 or term.lower() in STOP:
                continue
            entry = found.setdefault(term, {"scenes": [], "first": scene})
            if scene not in entry["scenes"]:
                entry["scenes"].append(scene)
    return found


def variants_for(term: str, others: Iterable[str]) -> list[str]:
    """同一名字的长短写法：`Lexi` 与 `Lexi Anne Miller` 互相认得出来。"""
    parts = set(re.split(r"[\s_]+", term.lower()))
    out = []
    for other in others:
        if other == term:
            continue
        other_parts = set(re.split(r"[\s_]+", other.lower()))
        if parts < other_parts or other_parts < parts:
            out.append(other)
    return sorted(out)


def unit_sources(graph: PathGraph) -> list[tuple[str, str]]:
    """图上全部可译单元的 ``(单元 id, 原文)``。"""
    return [
        (str(node.unit.id), str(node.unit.source))
        for node in graph.nodes.values()
        if node.unit is not None
    ]


def _term_pattern(term: str) -> re.Pattern[str]:
    """候选在原文里的样子：下划线当空格，大小写不敏感，两侧不许贴着字母。"""
    return re.compile(
        r"(?<![A-Za-z])"
        + r"[\s_]+".join(re.escape(part) for part in re.split(r"[\s_]+", term))
        + r"(?![A-Za-z])",
        re.IGNORECASE,
    )


def count_hits(
    units: Iterable[tuple[str, str]], terms: Iterable[str]
) -> dict[str, tuple[int, list[str]]]:
    """``候选 → (原文命中次数, 命中的单元清单)``。"""
    result: dict[str, tuple[int, list[str]]] = {}
    for term in terms:
        pattern = _term_pattern(term)
        hits = 0
        seen: list[str] = []
        for unit_id, source in units:
            count = len(pattern.findall(source))
            if count:
                hits += count
                if unit_id not in seen:
                    seen.append(unit_id)
        result[term] = (hits, seen)
    return result


def analyze(summaries: dict, graph: PathGraph) -> list[dict]:
    """读数的形状：**一个写法一行**（含类别、场次、命中、长短/大小写写法）。

    lab 读数与产品通道共用这一份：产品通道再按实体把它们并成一行（:func:`entity_groups`）。
    **没有译名与设定** —— 这一步只回答"这本书里有哪些写法"。
    """
    found = merge_case(harvest(summaries))
    hits = count_hits(unit_sources(graph), list(found))
    names = list(found)
    rows: list[dict] = []
    for term, entry in found.items():
        hit_count, hit_units = hits[term]
        kind = classify(term)
        rows.append(
            {
                # 这是**读数形状**（一个写法一行，带统计），不是术语书那一行的形状：
                # 写到 termbook.jsonl 上的行由 `collect` 现折（见那里的说明）。
                "source": term,
                "note": (
                    f"摘要侧候选（{kind}）：跨 {len(entry['scenes'])} 场、"
                    f"原文命中 {hit_count} 次 / {len(hit_units)} 个单元。"
                    "只抽了写法；译名与设定由翻译时申报 + 审核来定。"
                ),
                "case_sensitive": kind in ("人名", "组织/团体", "简称", "地名"),
                "tags": ["candidate", "from-summary"],
                "status": "pending_validation",
                "evidence": list(entry["scenes"]),
                "provenance": PROVENANCE,
                "decided_by": "",
                # 以下是给人看的统计，不影响解析
                "kind": kind,
                "scenes": len(entry["scenes"]),
                "first_scene": entry["first"],
                "hits": hit_count,
                "units": len(hit_units),
                "variants": variants_for(term, names),
                "case_variants": entry.get("case_variants") or [],
            }
        )
    rows.sort(key=lambda row: (-row["units"], -row["scenes"], -row["hits"], row["source"]))
    return rows


def build(project: Path) -> list[dict]:
    """读一个游戏工程的 ``.gametrans/``，产出**每个写法一行**的候选读数。"""
    summaries, graph = load_inputs(Path(project) / ".gametrans")
    return analyze(summaries, graph)


def load_inputs(workdir: Path) -> tuple[dict, PathGraph]:
    """读工作区的 ``summaries.json`` 与 ``graph.json``。

    读不到就明确报错，不静默当"没有摘要"：这条通道的产出全从这两份文件来，
    悄悄返回空清单会让人以为"这本书里没有专名"。
    """
    workdir = Path(workdir)
    summaries_path = workdir / "summaries.json"
    graph_path = workdir / "graph.json"
    try:
        summaries = json.loads(summaries_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectError(
            f"读不到场摘要：{summaries_path}",
            hint="摘要要先做（见 lab/readouts/scene_summaries.py），再抽候选。",
        ) from exc
    if not isinstance(summaries, dict):
        raise ProjectError(f"场摘要的形状不对（应该是一个对象）：{summaries_path}")
    try:
        graph = PathGraph.from_dict(json.loads(graph_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ProjectError(
            f"读不到路径图：{graph_path}",
            hint="先跑 `gametrans scan`，命中次数要靠图上的原文量。",
        ) from exc
    return summaries, graph


def character_names(graph: PathGraph) -> set[str]:
    """引擎申报的角色表：``变量名 → 显示名``，两边的写法都算（小写存）。"""
    table = (getattr(graph, "metadata", None) or {}).get("characters") or {}
    items: Iterable[Any]
    if isinstance(table, dict):
        items = [piece for pair in table.items() for piece in pair]
    elif isinstance(table, (list, tuple, set)):
        items = list(table)
    else:
        items = []
    return {str(name).strip().lower() for name in items if str(name or "").strip()}


# --------------------------------------------------------------------------- #
# 产品通道：一个实体一行，写进术语书
# --------------------------------------------------------------------------- #


def entity_groups(rows: list[dict]) -> list[list[dict]]:
    """把"一个写法一行"的读数按**同一个实体**分组。

    分组依据是读数自己的 ``variants`` / ``case_variants``：写法之间的关系是词集包含
    或大小写差异，任一行都能看到整组。用并查集而不是"取某一行的写法集合"是必须的 ——
    `SIT` ⊂ `SIT Alpha Wolves`、`Wild Cats` ⊂ `BSU Wild Cats` 这类链上，中间那一行
    的集合比两端都大，按单行取集合会把同一个实体拆成两组。
    """
    parent: dict[str, str] = {str(row["source"]): str(row["source"]) for row in rows}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for row in rows:
        source = str(row["source"])
        for name in [*(row.get("variants") or []), *(row.get("case_variants") or [])]:
            key = str(name)
            if key not in parent:
                continue
            root, other = find(source), find(key)
            if root != other:
                parent[other] = root
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(find(str(row["source"])), []).append(row)
    return list(groups.values())


def _pick_primary(members: list[dict]) -> dict:
    """这一行的主写法：**出现场次最多的那个**，同场次取更短的那个（`Eve` 优先于 `Eve Herschel`）。"""
    return min(
        members,
        key=lambda row: (
            -int(row.get("scenes") or 0),
            len(re.split(r"[\s_]+", str(row["source"]))),
            str(row["source"]),
        ),
    )


def writes_in_order(members: list[dict], primary: dict) -> list[str]:
    """一行里写法的顺序：**身份排在最前**（那个出现场次最多的写法），其余按场次、再按名字。

    为什么身份要按场次而不是字母序：`Natsume` / `Saki` / `Saki Natsume` 是同一个实体，
    而"这一行叫什么"是给人看的 —— 字母序会把这一行叫成 `Natsume`（她只在少数场次里
    用姓出场），看着就不像主角之一。同场次时取**更短**的那个（`Eve` 优先于
    `Eve Herschel`），这是 :func:`_pick_primary` 一直在用的口径。
    """
    rest = sorted(
        (row for row in members if str(row["source"]) != str(primary["source"])),
        key=lambda row: (-int(row.get("scenes") or 0), str(row["source"])),
    )
    return [str(primary["source"]), *[str(row["source"]) for row in rest]]


def collect(workdir: Path, termbook: TermBook, *, min_scenes: int = 2) -> dict[str, Any]:
    """摘要侧实体 → 术语书的一行（一个实体一行）。**幂等、只写写法**。

    只落**写法**：``target`` 与 ``profile`` 都空着（见模块说明）。那些写法在翻译时
    在原文里是 ``⟦写法⟧``，由模型申报 + 人或 agent 审核定译。

    写入走 ``TermBook.apply`` —— 只有"新写法"是新增（免审追加）；已有的写法一个字节
    都不动（这一步没有译名与设定可写）。已否决的写法不许借这一步复活。
    """
    summaries, graph = load_inputs(Path(workdir))
    rows = analyze(summaries, graph)
    characters = character_names(graph)
    units = unit_sources(graph)
    registered = {
        writing for entry in termbook.entries() for writing in entry.writings
    } | set(termbook.rejected())

    groups = [
        sorted(members, key=lambda row: str(row["source"])) for members in entity_groups(rows)
    ]
    skipped: list[str] = []
    blocked: dict[str, int] = {}
    lower_initial = 0
    below_min_scenes = 0
    survivors: list[tuple[list[dict], dict, str, int, set[str], list[str]]] = []
    for members in groups:
        # **原文侧必须是拉丁写法**（见 :func:`has_latin`）：纯中文的写法不落行。这道闸在
        # 分类之前 —— 中文角色泛称（`解说员`）本来就该记在"纯中文写法"里，不是"称呼/泛称"。
        members = [row for row in members if has_latin(str(row["source"]))]
        if not members:
            blocked["纯中文写法"] = blocked.get("纯中文写法", 0) + 1
            continue
        primary = _pick_primary(members)
        kind = str(primary["kind"])
        if kind not in KEEP_KINDS:
            blocked[kind] = blocked.get(kind, 0) + 1
            continue
        writings = [str(row["source"]) for row in members]
        if any(writing in registered for writing in writings):
            skipped.append(str(primary["source"]))
            continue
        if all(writing.isalpha() and len(writing) < MIN_LETTERS for writing in writings):
            # `St`（从 `St. Eden Avenue` 切下来的两字母）不是名字；`BSU` / `SIT` 是三个字母。
            # 这道闸只看写法本身，所以放在"够不够场次"前面 —— 两字母的记号不该靠场次多来豁免。
            blocked["写法过短"] = blocked.get("写法过短", 0) + 1
            continue
        scenes: list[str] = []
        for row in members:
            for scene in row.get("evidence") or []:
                if scene not in scenes:
                    scenes.append(str(scene))
        if len(scenes) < max(1, int(min_scenes or 1)):
            below_min_scenes += 1
            continue
        source = str(primary["source"])
        if not (source[:1].isupper() or source.lower() in characters):
            lower_initial += 1
            continue
        counts = count_hits(units, writings)
        hit_count = sum(hits for hits, _units in counts.values())
        if hit_count == 0:
            # 这一行的用处是"原文里出现这个写法时按它译"；原文里一次都没出现就没有用处
            # （真靶上 `Johan` 在原文里是 `[name]`、`Nighten` 是网名，都命中 0）。
            blocked["原文里没出现"] = blocked.get("原文里没出现", 0) + 1
            continue
        unit_ids = {unit_id for _hits, ids in counts.values() for unit_id in ids}
        survivors.append((members, primary, kind, hit_count, unit_ids, scenes))  # type: ignore[arg-type]

    created: list[dict[str, Any]] = []
    for members, primary, kind, hit_count, unit_ids, scenes in survivors:
        # 写法的顺序 = 身份在最前（见 writes_in_order）：落到盘上就是"这一行叫什么"。
        writings = writes_in_order(members, primary)
        saved = termbook.apply(
            TermEntry(key=[{"writing": writing, "target": ""} for writing in writings]),
            by=PROVENANCE,
            why=(
                f"摘要侧实体（{kind}）：跨 {len(scenes)} 场、"
                f"原文命中 {hit_count} 次 / {len(unit_ids)} 个单元（**只登记写法**）"
            ),
        )
        registered.update(writings)
        created.append(saved.to_dict())

    return {
        "created": created,
        "created_count": len(created),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "blocked": blocked,
        "blocked_lower_initial": lower_initial,
        "below_min_scenes": below_min_scenes,
        "min_scenes": int(min_scenes or 1),
        "candidates_seen": len(rows),
    }
