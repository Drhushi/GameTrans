"""请求台账：把**拦截下来的请求**与**真实调用流水**合成一张表。

为什么要合并：两边的信息大半是重复的 —— 拦截落盘的是"本来要发出去的请求体"，
真实流水是"真发出去的那次请求 + 对方回来的原文"。同一份请求体在两处各存一遍，
分成两张表看就得人肉对照。这里按**请求正文的指纹**对上号：

* 真跑过的：以真实流水那行为准（它多出回复、用量、错误、拿到/没拿到哪几条），
  同时标出"它也被拦截过"；
* 只拦了没发过的：单独一行，标 ``sent: false`` —— 这正是"离线预演"要看的那些。

指纹只取 ``system + user`` 两段正文，不含模型名/参数：模型换了不该算成两条请求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gametrans.core.ids import alias_index, resolve_identity
from gametrans.core.report import CALLS_SUFFIX, load_report
from gametrans.prompts import digest

#: 拦截落盘的请求体长什么样：``{"messages": [...], "_dump": {...}}``。
INTERCEPT_DIR_NAME = "requests"


def _answered_and_missing(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    """这一次问了哪几条、哪几条真回来了、因此哪几条**没拿到**。

    两个坑都在真靶上踩过，判定必须避开：

    1. 比对**不能逐字比** —— 模型会把 `id:` 这类内部前缀规范化掉，逐字比会把整批成功的
       调用报成"一条都没拿到"。归一规则与翻译层共用 :mod:`gametrans.core.ids`。
    2. **`unit_ids` 里含"只当上下文"的命中句** —— 那些条目本来就不该被回译
       （`expects_answer=False`），把它们算进"该拿到"会凭空报缺失。新流水记了
       ``expected_ids``（真要回译的那几条）；老流水没这个字段，就退到**信这一层自己的
       判断**：``ok=true`` 表示翻译层认定这次回复够用，那就不该报缺失。
    """
    asked = [str(unit) for unit in (entry.get("unit_ids") or [])]
    index, ambiguous = alias_index(asked)
    # 线路上用的是短身份时（``id_map``：短身份 → 完整身份），先按那张表还原 ——
    # 拿短身份去比完整身份会对不上，于是把一次成功的调用报成"一条都没拿到"。
    wire = entry.get("id_map")
    wire = {str(k): str(v) for k, v in wire.items()} if isinstance(wire, dict) else {}
    parsed = entry.get("parsed")
    parsed = parsed if isinstance(parsed, list) else []
    answered: list[str] = []
    for row in parsed:
        if not isinstance(row, dict) or row.get("unit_id") is None:
            continue
        raw = str(row["unit_id"])
        key = raw.strip()
        hit = (
            wire.get(key)
            or wire.get(key.strip("[]").strip())   # 模型把方括号一起抄回来
            or resolve_identity(raw, index, ambiguous)
        )
        if hit and hit not in answered:
            answered.append(hit)
    got = set(answered)

    expected = entry.get("expected_ids")
    if isinstance(expected, list) and expected:
        return answered, [str(unit) for unit in expected if str(unit) not in got]
    if bool(entry.get("ok", True)):
        # 老流水（没有 expected_ids）：这一层说"够用"，那就没有"没拿到"可言
        return answered, []
    return answered, [unit for unit in asked if unit not in got]


def _messages(body: dict[str, Any]) -> tuple[str, str]:
    messages = body.get("messages") or []
    system = next(
        (str(m.get("content") or "") for m in messages if m.get("role") == "system"), ""
    )
    user = next(
        (str(m.get("content") or "") for m in messages if m.get("role") == "user"), ""
    )
    return system, user


def call_entries(directory: Path, *, run: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """真实调用流水（``reports/<run>-calls.jsonl``）：一次调用一行，最近的运行排最前。"""
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not directory.is_dir():
        return [], warnings

    paths = sorted(directory.glob(f"*{CALLS_SUFFIX}"), key=_mtime, reverse=True)
    for path in paths:
        run_id = path.name[: -len(CALLS_SUFFIX)]
        if run and not run_id.startswith(run):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            warnings.append(f"{path.name} 读不出来，已跳过")
            continue
        pending = _latency_index(directory, run_id)
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"{path.name} 第 {index} 行读不出来，已跳过")
                continue
            if isinstance(entry, dict):
                entries.append(
                    _call_row(entry, run_id=run_id, index=index,
                              timing=_take_latency(pending, entry))
                )
    return entries, warnings


def intercepted_entries(directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """拦截下来的请求体（``requests/*.json``，由 ``dump_translate_request.py`` 落盘）。"""
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not directory.is_dir():
        return [], warnings

    for path in sorted(directory.glob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            warnings.append(f"{path.name} 读不出来，已跳过")
            continue
        if not isinstance(body, dict) or "messages" not in body:
            warnings.append(f"{path.name} 不像一个请求体，已跳过")
            continue
        system, user = _messages(body)
        meta = body.get("_dump") if isinstance(body.get("_dump"), dict) else {}
        entries.append(
            {
                "kind": "intercepted",
                "sent": False,
                "id": f"intercept:{path.name}",
                "name": path.name,
                "run_id": "",
                "index": int(meta.get("call") or 0),
                "phase": str(meta.get("phase") or ""),
                "model": str(meta.get("model") or body.get("model") or ""),
                "items": int(meta.get("items") or 0),
                "unit_ids": [],
                "ok": None,
                "error": "",
                "usage": {},
                "answered": [],
                "missing": [],
                "digest": digest(system + "\n" + user),
                "prompt_system": system,
                "prompt_user": user,
                "response_raw": "",
                "parsed": [],
                "prompt_chars": len(system) + len(user),
                "response_chars": 0,
            }
        )
    return entries, warnings


def _call_row(
    entry: dict[str, Any], *, run_id: str, index: int, timing: dict[str, Any] | None = None
) -> dict[str, Any]:
    system = str(entry.get("prompt_system") or "")
    user = str(entry.get("prompt_user") or "")
    answered, missing = _answered_and_missing(entry)
    usage = entry.get("usage")
    parsed = entry.get("parsed")
    timing = timing or {}
    latency = timing.get("latency_ms")
    asked = int(entry.get("items") or 0)
    return {
        "kind": "call",
        "sent": True,
        "id": f"{run_id}:{index}",
        "name": f"{run_id}-calls.jsonl",
        "run_id": run_id,
        "index": index,
        "phase": str(entry.get("phase") or ""),
        "template": str(entry.get("template") or ""),
        "template_label": str(entry.get("template_label") or ""),
        "template_digest": str(entry.get("template_digest") or ""),
        "model": str(entry.get("model") or ""),
        "items": asked,
        "unit_ids": [str(unit) for unit in (entry.get("unit_ids") or [])],
        "ok": bool(entry.get("ok", True)),
        "error": str(entry.get("error") or ""),
        "usage": dict(usage) if isinstance(usage, dict) else {},
        "answered": answered,
        "missing": missing,
        # 耗时（报告侧的读数）：每次调用的墙钟，以及**每条约耗时** ——
        # 后者是"这个模型上一条预算能装几条"的唯一实测口径（真靶约 7.4 秒/条）
        "latency_ms": latency,
        "seconds_per_item": (
            round(latency / 1000.0 / asked, 2) if latency and asked else None
        ),
        "finish_reason": str(timing.get("finish_reason") or ""),
        "reasoning_tokens": timing.get("reasoning_tokens"),
        "digest": digest(system + "\n" + user),
        "prompt_system": system,
        "prompt_user": user,
        "response_raw": str(entry.get("response_raw") or ""),
        "parsed": parsed if isinstance(parsed, list) else [],
        "prompt_chars": len(system) + len(user),
        "response_chars": len(str(entry.get("response_raw") or "")),
    }


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _latency_index(directory: Path, run_id: str) -> list[dict[str, Any]]:
    """这一次运行里每次调用的**耗时**读数（来自运行报告的 ``llm.records``）。

    流水（``calls.jsonl``）里**没有**时间字段 —— 它记的是"发了什么、回了什么"。
    耗时在运行报告的 ``metrics.llm.records`` 里。两边按 ``(unit_ids, ok)`` 对上号，
    **不按行号**：某次调用如果在写流水之前就炸了，行号会错位，而"问了哪几条、成没成"不会。
    """
    report = load_report(directory, run_id)
    if report is None:
        return []
    llm = report.metrics.get("llm") if isinstance(report.metrics, dict) else None
    records = (llm or {}).get("records") if isinstance(llm, dict) else None
    return [dict(record) for record in (records or []) if isinstance(record, dict)]


def _take_latency(records: list[dict[str, Any]], entry: dict[str, Any]) -> dict[str, Any]:
    """按 ``(unit_ids, ok)`` 从待配列表里取走一条；对不上就什么都不给（不硬凑）。"""
    wanted = [str(unit) for unit in (entry.get("unit_ids") or [])]
    ok = bool(entry.get("ok", True))
    for position, record in enumerate(records):
        if [str(unit) for unit in (record.get("unit_ids") or [])] != wanted:
            continue
        if bool(record.get("ok", True)) != ok:
            continue
        records.pop(position)
        return record
    return {}


def merged(
    workdir: Path, *, run: str = ""
) -> tuple[list[dict[str, Any]], list[str]]:
    """合成一张表：**真跑过的排前面（按运行/行号），只拦截过的排后面**。

    同一份请求体两边都有时只留一行（真实流水那行），并给它标 ``intercepted_too``。
    """
    calls, warnings = call_entries(Path(workdir) / "reports", run=run)
    intercepted, more = intercepted_entries(Path(workdir) / INTERCEPT_DIR_NAME)
    warnings += more

    seen = {row["digest"] for row in calls}
    extra = [row for row in intercepted if row["digest"] not in seen]
    intercepted_digests = {row["digest"] for row in intercepted}
    for row in calls:
        # 真实流水里那行如果也拦过，标出来（人想知道"这次发出去之前先冻过一份没有"）
        row["intercepted_too"] = row["digest"] in intercepted_digests
    for row in extra:
        row["intercepted_too"] = False
    return calls + extra, warnings


#: 列表里**不带**的字段：正文与逐条译文只在详情接口里给。
#: 面板每轮都在轮询列表，而一次真跑就是上百万字符 —— 装在一个响应里会把它拖死。
LIGHT_DROP: tuple[str, ...] = ("prompt_system", "prompt_user", "response_raw", "parsed")


def light(row: dict[str, Any]) -> dict[str, Any]:
    """列表用的一行：把正文换成字符数（字符数在 ``prompt_chars`` / ``response_chars``）。"""
    return {key: value for key, value in row.items() if key not in LIGHT_DROP}


def find(workdir: Path, ident: str) -> dict[str, Any] | None:
    """按 id 取回**一条**（含正文）。

    id 的形状由本模块决定：真跑过的是 ``<run>:<行号>``，只拦过的是 ``intercept:<文件名>``。
    找不到就返回 ``None``（调用方报 404，不编一个空壳出来）。
    """
    rows, _warnings = merged(Path(workdir))
    for row in rows:
        if row["id"] == ident:
            return row
    return None


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一眼看得见的几笔账：发了几次、成几次、拦了几条没发的、过程里丢过哪些槽位。

    **口径要写清**：``missed_in_some_call`` 是"**某一次调用**没拿到的槽位并集"——
    同一条槽位在后面某次调用里拿到了，也仍然算在里面。它是"过程里丢过什么"，
    不是"最终还缺哪些"（后者要读 ``translations.jsonl``，不在这里算）。
    """
    sent = [row for row in rows if row["sent"]]
    missed = sorted({unit for row in sent for unit in row["missing"]})
    waited = [row["latency_ms"] for row in sent if row.get("latency_ms")]
    answered_items = sum(row["items"] for row in sent if row.get("latency_ms"))
    return {
        "rows": len(rows),
        "sent": len(sent),
        "failed": sum(1 for row in sent if not row["ok"]),
        "intercepted_only": len(rows) - len(sent),
        "missed_in_some_call": missed,
        "missed_count": len(missed),
        # —— 时间账：真靶上"一次调用多久、一条多久"此前只能靠人估 ——
        "timed_calls": len(waited),
        "total_ms": round(sum(waited), 1) if waited else None,
        "p50_ms": _percentile(waited, 0.50),
        "p95_ms": _percentile(waited, 0.95),
        "seconds_per_item": (
            round(sum(waited) / 1000.0 / answered_items, 2) if waited and answered_items else None
        ),
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    """最近秩法的分位数；没有读数就 ``None``（不拿 0 冒充）。"""
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered))) - 1))
    return round(ordered[position], 1)
