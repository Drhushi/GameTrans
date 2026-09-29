"""结构化运行报告。

每一次命令都产出一份报告：agent 读它判断下一步，用户看交互层渲染后的版本。
这是 spec 里「单项失败隔离 + 全量结构化报告」的落地载体 —— 单条文本翻不出来
只进 ``failed``，整批照跑。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gametrans.core.models import Issue

#: 报告文件名：``<run-id>-<命令>.json``（由 ``ProjectSession._save_report`` 写下）。
REPORT_SUFFIX = ".json"

#: 报告落在工作区的哪个子目录。
REPORT_DIR_NAME = "reports"

#: 调用流水的文件名后缀：``<run-id>-calls.jsonl``（由 ``ProjectSession._append_transcript``
#: 写下，一次调用一行，可逐字复盘）。注意它**不是** :data:`REPORT_SUFFIX`：
#: ``*.json`` 匹配不到 ``*.jsonl``，所以 :func:`report_rows` 不会把它当成一份报告读进来。
CALLS_SUFFIX = "-calls.jsonl"

#: 列表里给问题分桶留的是**计数**而不是全部条目：账本页要看"这次跑得干不干净"，
#: 逐条原因属于详情接口。整份倒进列表只会在几百次运行后把响应撑爆。
BUCKET_COUNTS = "issue_counts"

#: 允许的问题分类。新增分类必须同时更新这里，避免拼写错误静默丢数据。
BUCKETS: tuple[str, ...] = (
    "failed",
    "skipped",
    "unsupported",
    "conflicts",
    "warnings",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StageReport:
    """一个阶段的独立报告。"""

    name: str
    metrics: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    ok: bool = True
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> "StageReport":
        self.notes.append(message)
        return self

    def finish(self, *, ok: bool = True) -> "StageReport":
        self.finished_at = _now()
        self.ok = ok
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metrics": dict(self.metrics),
            "notes": list(self.notes),
        }


@dataclass
class RunReport:
    """一次命令运行的完整结果。``ok`` 为假当且仅当有 ``failed``。"""

    command: str
    project_root: str
    engine: str | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    stages: list[StageReport] = field(default_factory=list)

    failed: list[Issue] = field(default_factory=list)
    skipped: list[Issue] = field(default_factory=list)
    unsupported: list[Issue] = field(default_factory=list)
    conflicts: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    metrics: dict[str, Any] = field(default_factory=dict)

    # ---- 累积 ---------------------------------------------------------------

    def add_issue(
        self,
        bucket: str,
        code: str,
        message: str,
        *,
        ref: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Issue:
        if bucket not in BUCKETS:
            raise ValueError(f"未知的问题分类：{bucket!r}（可用：{', '.join(BUCKETS)}）")
        issue = Issue(code=code, message=message, ref=ref, detail=dict(detail or {}))
        getattr(self, bucket).append(issue)
        return issue

    def warn(self, message: str, *, code: str = "warning", ref: str | None = None) -> Issue:
        return self.add_issue("warnings", code, message, ref=ref)

    def stage(self, name: str, **metrics: Any) -> StageReport:
        report = StageReport(name=name, metrics=dict(metrics))
        self.stages.append(report)
        return report

    def finish(self) -> "RunReport":
        self.finished_at = _now()
        return self

    # ---- 状态 ---------------------------------------------------------------

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def issue_count(self) -> int:
        return sum(len(getattr(self, bucket)) for bucket in BUCKETS)

    # ---- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command": self.command,
            "project_root": self.project_root,
            "engine": self.engine,
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metrics": dict(self.metrics),
            "stages": [s.to_dict() for s in self.stages],
            **{bucket: [i.to_dict() for i in getattr(self, bucket)] for bucket in BUCKETS},
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunReport":
        report = cls(
            command=str(data.get("command", "")),
            project_root=str(data.get("project_root", "")),
            engine=data.get("engine"),
            run_id=str(data.get("run_id", uuid.uuid4().hex[:12])),
            started_at=str(data.get("started_at", _now())),
            finished_at=data.get("finished_at"),
            metrics=dict(data.get("metrics") or {}),
        )
        report.stages = [
            StageReport(
                name=str(s.get("name", "")),
                metrics=dict(s.get("metrics") or {}),
                started_at=str(s.get("started_at", _now())),
                finished_at=s.get("finished_at"),
                ok=bool(s.get("ok", True)),
                notes=list(s.get("notes") or []),
            )
            for s in (data.get("stages") or [])
        ]
        for bucket in BUCKETS:
            setattr(
                report,
                bucket,
                [Issue.from_dict(i) for i in (data.get(bucket) or [])],
            )
        return report


# --------------------------------------------------------------------------- #
# 读回 —— 账本页要看的是历史，不只是当前这一次
# --------------------------------------------------------------------------- #


def report_rows(
    directory: Path, *, command: str = ""
) -> tuple[list[dict[str, Any]], list[str]]:
    """把 ``reports/`` 读成账本页的行，**最近一次排在最前**。

    返回 ``(rows, warnings)``。坏掉的那一份跳过并如实报出，绝不让一个坏字节
    抹掉整页历史 —— 报告是事后取证用的，丢一份就是丢一次运行。
    """
    ordered: list[tuple[tuple[str, int], dict[str, Any]]] = []
    warnings: list[str] = []
    if not directory.is_dir():
        return [], warnings

    for path in sorted(directory.glob(f"*{REPORT_SUFFIX}")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            warnings.append(f"{path.name} 读不出来，已跳过")
            continue
        if not isinstance(payload, dict):
            warnings.append(f"{path.name} 不是一个报告对象，已跳过")
            continue
        name = str(payload.get("command", "")) or _command_from_name(path)
        if command and name != command:
            continue
        ordered.append(((_stamp(payload), _mtime(path)), _row(payload, path, name)))

    ordered.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in ordered], warnings


def load_report(directory: Path, run_id: str) -> RunReport | None:
    """按 run_id 取回**整份**报告（含逐条问题与阶段明细）；没有就返回 ``None``。

    ``run_id`` 只允许字母数字：它会被拼进 glob，放行通配符等于让人拿 URL 去扫目录。
    """
    if not run_id.isalnum() or not directory.is_dir():
        return None
    for path in sorted(directory.glob(f"{run_id}-*{REPORT_SUFFIX}")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return RunReport.from_dict(payload)
    return None


def _command_from_name(path: Path) -> str:
    """文件名兜底：``<run-id>-<命令>.json``，而 run_id 里没有 ``-``。"""
    stem = path.stem
    return stem.split("-", 1)[1] if "-" in stem else ""


def _row(payload: dict[str, Any], path: Path, command: str) -> dict[str, Any]:
    return {
        "run_id": str(payload.get("run_id") or path.stem.split("-", 1)[0]),
        "command": command,
        "project_root": str(payload.get("project_root", "")),
        "engine": payload.get("engine"),
        "ok": bool(payload.get("ok", True)),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "metrics": dict(payload.get("metrics") or {}),
        "stages": [
            stage for stage in (payload.get("stages") or []) if isinstance(stage, dict)
        ],
        # 列表只给分桶计数：逐条原因属于详情接口，全倒进来会在几百次运行后撑爆响应
        BUCKET_COUNTS: {bucket: len(payload.get(bucket) or []) for bucket in BUCKETS},
    }


def _stamp(payload: dict[str, Any]) -> str:
    """排序用的时间戳；缺 ``finished_at`` 的（中断留下的）退到开始时间。"""
    return str(payload.get("finished_at") or payload.get("started_at") or "")


def _mtime(path: Path) -> int:
    """同一秒内的两次运行靠文件时间戳分先后 —— 报告时间戳只精确到秒。"""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0

