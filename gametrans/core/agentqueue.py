"""挂单队列：翻译流水线与作答 agent 之间的一叠"待办请求"。

provider=agent 时，流水线把**本来要发给 API 的完整请求**（system + 会话历史 + 正文）
原样落盘成一张挂单，然后停在原地等答案；agent（人/agent 会话，走它自己的额度）
用 ``agent next`` 取单、自己作答、``agent submit`` 交回。答案回到 provider 手里后，
与 API 响应走**同一条**解析与校验链 —— 这条队列只是换了个运输方式，不造第二条写盘路。

挂单是工作区里的一等事实：跑批中途断了，盘上的挂单还在（``agent status`` 看得见），
答案也可以晚一点交 —— 与"任务状态落盘、断了可续"是同一条纪律。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gametrans.errors import AgentQueueError

#: 工作区里挂单目录的名字（``<workdir>/agent-requests/``）
QUEUE_DIRNAME = "agent-requests"

#: ``0001-translate.json`` 里开头那段序号
_SEQ_PATTERN = re.compile(r"^(\d+)-")

_STATUS_PENDING = "pending"
_STATUS_CLAIMED = "claimed"
_STATUS_ANSWERED = "answered"
_STATUS_COLLECTED = "collected"

#: 认领多久没交答案就算认领者死了，单子可以给别人 —— 没有这条，
#: 一个作答方中途崩溃就会让那张单永远轮不到下一个人。
CLAIM_STALE_SECONDS = 600.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再 ``os.replace``：读的那一半永远不会看到写了一半的挂单。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(tmp, path)


class AgentQueue:
    """一个工作区的挂单队列。两个进程（跑批 / 作答）各开一份，靠文件对话。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---- 写路径（跑批侧 park / 作答侧 submit） ------------------------------

    def park(
        self,
        *,
        messages: list[dict[str, str]],
        expected: int,
        unit_ids: list[str],
        phase: str = "",
    ) -> dict[str, Any]:
        """把一次请求挂成单：返回的 dict 就是挂单全文（含 ``request_id``）。

        ``expected`` 是这份请求要几条译文（响应级"少一条就作废"的判据用得着），
        ``unit_ids`` 是待译身份清单 —— 给取单的 agent 对账用，正文里本来就有一份。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        seq = 0
        for path in self.root.glob("*.json"):
            match = _SEQ_PATTERN.match(path.stem)
            if match:
                seq = max(seq, int(match.group(1)))
        label = re.sub(r"[^0-9A-Za-z_-]+", "-", phase or "call").strip("-") or "call"
        record = {
            "request_id": f"{seq + 1:04d}-{label}",
            "status": _STATUS_PENDING,
            "created_at": _now(),
            "claimed_at": "",
            "answered_at": "",
            "collected_at": "",
            "phase": phase,
            "expected": int(expected),
            "unit_ids": [str(uid) for uid in unit_ids],
            "messages": [dict(message) for message in messages],
            "answer": "",
        }
        _atomic_write(self.root / f"{record['request_id']}.json", record)
        return record

    def claim(self, request_id: str) -> dict[str, Any]:
        """认领一张单：只有 ``pending`` 或**过期认领**的单能领。

        认领让"取单"与"作答"之间不再有撞车窗口 —— 两个作答方同时取单时，
        各拿到**不同**的单，谁的译文都不白做。认领者崩了也没关系：超过
        :data:`CLAIM_STALE_SECONDS` 没交答案，单子回到可领取池。
        """
        record = self.load(request_id)
        status = record["status"]
        if status == _STATUS_CLAIMED and not self._claim_stale(record):
            raise AgentQueueError(
                f"挂单 {request_id} 已被认领（{record.get('claimed_at', '')}），不能重复认领",
                hint="认领超过 " f"{CLAIM_STALE_SECONDS:.0f} 秒没交答案会自动放回池子里。",
            )
        if status not in (_STATUS_PENDING, _STATUS_CLAIMED):
            raise AgentQueueError(
                f"挂单 {request_id} 已经是 {status} 状态，不能认领",
                hint="用 agent status 看它现在的样子。",
            )
        record["status"] = _STATUS_CLAIMED
        record["claimed_at"] = _now()
        _atomic_write(self._path(request_id), record)
        return record

    @staticmethod
    def _claim_stale(record: dict[str, Any]) -> bool:
        """认领是否已过期（按挂单自己写的时刻算，不依赖读的那台钟）。"""
        claimed_at = str(record.get("claimed_at") or "")
        if not claimed_at:
            return True
        try:
            moment = datetime.fromisoformat(claimed_at)
        except ValueError:
            return True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - moment).total_seconds() >= CLAIM_STALE_SECONDS

    def submit(self, request_id: str, answer: str) -> dict[str, Any]:
        """交一份答案。``pending`` / ``claimed`` 都能收 —— 已答过的以**第一份**为准。"""
        record = self.load(request_id)
        if record["status"] not in (_STATUS_PENDING, _STATUS_CLAIMED):
            raise AgentQueueError(
                f"挂单 {request_id} 已经是 {record['status']} 状态，不能再交答案",
                hint="以第一份答案为准；要看它现在的样子用 agent status。",
            )
        record["answer"] = str(answer or "")
        record["status"] = _STATUS_ANSWERED
        record["answered_at"] = _now()
        _atomic_write(self._path(request_id), record)
        return record

    def mark_collected(self, request_id: str) -> None:
        """答案已被流水线收走（进了校验链）——只改状态，不动内容。"""
        record = self.load(request_id)
        if record["status"] != _STATUS_ANSWERED:
            return
        record["status"] = _STATUS_COLLECTED
        record["collected_at"] = _now()
        _atomic_write(self._path(request_id), record)

    # ---- 读路径 -------------------------------------------------------------

    def load(self, request_id: str) -> dict[str, Any]:
        path = self._path(request_id)
        if not path.is_file():
            raise AgentQueueError(
                f"挂单不存在：{request_id}",
                hint=f"用 agent status 看 {self.root} 里现在有哪些单。",
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentQueueError(f"挂单 {request_id} 读不出来：{exc}") from None

    def _path(self, request_id: str) -> Path:
        # request_id 由本模块生成（序号-标签），不认路径分隔符 —— 防呆
        if "/" in request_id or "\\" in request_id or ".." in request_id:
            raise AgentQueueError(f"挂单 id 不合法：{request_id!r}")
        return self.root / f"{request_id}.json"

    def next_pending(self) -> dict[str, Any] | None:
        """最老的一张可领取单（``pending``，或认领过期被放回的）先答；没有就 ``None``。"""
        records = [
            r
            for r in self._all()
            if r["status"] == _STATUS_PENDING
            or (r["status"] == _STATUS_CLAIMED and self._claim_stale(r))
        ]
        if not records:
            return None
        records.sort(key=lambda r: r["request_id"])
        return records[0]

    def claim_next(self) -> dict[str, Any] | None:
        """取最老的一张可领取单并**认领**它 —— 取单即占坑，别的作答方拿不到同一张。"""
        parked = self.next_pending()
        if parked is None:
            return None
        return self.claim(parked["request_id"])

    def wait_for_answer(
        self, request_id: str, *, timeout_s: float, poll_s: float = 2.0
    ) -> str | None:
        """等一张挂单被答。等到返回答案文本；超时返回 ``None``（挂单原样留在盘上）。"""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            record = self.load(request_id)
            if record["status"] in (_STATUS_ANSWERED, _STATUS_COLLECTED):
                return str(record["answer"])
            if time.monotonic() >= deadline:
                return None
            time.sleep(max(0.05, float(poll_s)))

    def status(self) -> dict[str, Any]:
        """队列现状：按状态计数 + 逐条清单（老单在前）。给 agent status 用。"""
        records = sorted(self._all(), key=lambda r: r["request_id"])
        counts = {
            _STATUS_PENDING: 0,
            _STATUS_CLAIMED: 0,
            _STATUS_ANSWERED: 0,
            _STATUS_COLLECTED: 0,
        }
        for record in records:
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        return {
            "root": str(self.root),
            "counts": counts,
            "unclaimed": counts[_STATUS_PENDING]
            + counts[_STATUS_CLAIMED]
            + counts[_STATUS_ANSWERED],
            "items": [
                {
                    "request_id": record["request_id"],
                    "status": record["status"],
                    "phase": record.get("phase", ""),
                    "expected": record.get("expected", 0),
                    "created_at": record.get("created_at", ""),
                }
                for record in records
            ],
        }

    def _all(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue  # 半张单（写一半被杀）宁可少列也不报错挡道
        return records
