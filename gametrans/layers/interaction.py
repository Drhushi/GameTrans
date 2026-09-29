"""交互层：软件直接面向用户的信息面 + agent 的改写接口。

**这一层是给人看的，不是给 agent 看的。**

各层把工作信息产出成 :class:`View`，经**可见性策略**过滤后直接渲染给用户 ——
全程不需要 agent 参与。策略回答的是"哪些信息是用户关心的、哪些应该对用户透明"，
并且它本身是用户可以改的持久化配置。

与此同时，agent 保留**改写权**：它可以注入注记、改标题、整段替换内容、把某条
隐藏掉，或者直接向用户发布公告。改写一律留下署名，用户始终知道哪句话不是软件
自己说的。

状态落在 ``<root>/state.json``，是普通 JSON —— 和资源层一样，"文件即资源"，
agent 与用户都能直接读写。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from gametrans.errors import InteractionError

STATE_FILE = "state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Visibility(str, Enum):
    """一条信息对用户的可见程度。"""

    #: 直接呈现给用户
    USER = "user"
    #: 只进 agent 的视野，对用户透明
    AGENT = "agent"
    #: 调试信息，默认对用户透明
    DEBUG = "debug"
    #: 谁都不主动展示
    HIDDEN = "hidden"

    @classmethod
    def from_value(cls, value: Any) -> "Visibility":
        # 注意：`class Visibility(str, Enum)` 的 `str(member)` 得到的是
        # "Visibility.AGENT" 而不是 "agent"，所以枚举实例必须先原样返回。
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.USER


#: 出厂默认可见性策略：用户关心的是"项目怎么样了、这次跑出什么结果"，
#: 而不是"第 37 条文本翻完了"这种流水账 —— 后者默认对用户透明，但 agent 照常能看到。
#:
#: 这一层只是**兜底**：用户在 ``state.json`` 里写的任何规则都整层压过它。
DEFAULT_RULES: dict[str, Visibility] = {
    "project.*": Visibility.USER,
    "run.*": Visibility.USER,
    "engine.*": Visibility.USER,
    "resource.*": Visibility.USER,
    "agent.message": Visibility.USER,
    "scan.file": Visibility.AGENT,
    "translate.batch": Visibility.AGENT,
    "translate.unit": Visibility.DEBUG,
    "writeback.file": Visibility.AGENT,
}


@dataclass
class VisibilityPolicy:
    """主题 → 可见性。

    解析分两层：**用户规则**整层优先于**出厂默认规则**，层内再按"精确 > 最长通配"
    比较。这样用户一句 ``writeback.* → user`` 就能推翻出厂的 ``writeback.file → agent``，
    而不会被默认规则挡回去 —— 用户永远对自己看到什么有最终决定权。
    """

    default: Visibility = Visibility.USER
    rules: dict[str, Visibility] = field(default_factory=dict)

    @staticmethod
    def _match(rules: dict[str, Visibility], topic: str) -> Visibility | None:
        if topic in rules:
            return rules[topic]
        best: Visibility | None = None
        best_length = -1
        for pattern, visibility in rules.items():
            if pattern.endswith("*") and topic.startswith(pattern[:-1]):
                if len(pattern) > best_length:
                    best, best_length = visibility, len(pattern)
        return best

    def resolve(self, topic: str) -> Visibility:
        user = self._match(self.rules, topic)
        if user is not None:
            return user
        shipped = self._match(DEFAULT_RULES, topic)
        return shipped if shipped is not None else self.default

    def set(self, topic: str, visibility: Visibility | str) -> None:
        self.rules[topic] = Visibility.from_value(visibility)

    def to_dict(self) -> dict[str, Any]:
        return {
            "default": self.default.value,
            "rules": {k: v.value for k, v in sorted(self.rules.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VisibilityPolicy":
        rules = {
            str(k): Visibility.from_value(v) for k, v in (data.get("rules") or {}).items()
        }
        return cls(default=Visibility.from_value(data.get("default", "user")), rules=rules)


@dataclass
class ViewSection:
    """视图里的一小节。``title`` 为空就是一段裸文本。"""

    lines: list[str] = field(default_factory=list)
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "lines": list(self.lines)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ViewSection":
        return cls(title=str(data.get("title", "")), lines=[str(x) for x in (data.get("lines") or [])])


@dataclass
class View:
    """呈现给用户的一条信息。"""

    view_id: str
    topic: str
    title: str
    sections: list[ViewSection] = field(default_factory=list)
    severity: str = "info"  # info | success | warning | error
    source: str = "software"  # software | agent | user
    created_at: str = field(default_factory=_now)
    seq: int = 0
    pinned: bool = False
    hidden: bool = False
    hidden_by: str | None = None
    overridden_by: str | None = None
    visibility: Visibility = Visibility.USER

    def to_dict(self, visibility: Visibility | None = None) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "topic": self.topic,
            "title": self.title,
            "sections": [s.to_dict() for s in self.sections],
            "severity": self.severity,
            "source": self.source,
            "created_at": self.created_at,
            "seq": self.seq,
            "pinned": self.pinned,
            "hidden": self.hidden,
            "hidden_by": self.hidden_by,
            "overridden_by": self.overridden_by,
            "visibility": (visibility or self.visibility).value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "View":
        return cls(
            view_id=str(data["view_id"]),
            topic=str(data["topic"]),
            title=str(data["title"]),
            sections=[ViewSection.from_dict(s) for s in (data.get("sections") or [])],
            severity=str(data.get("severity", "info")),
            source=str(data.get("source", "software")),
            created_at=str(data.get("created_at", _now())),
            seq=int(data.get("seq", 0)),
            pinned=bool(data.get("pinned", False)),
            hidden=bool(data.get("hidden", False)),
            hidden_by=data.get("hidden_by"),
            overridden_by=data.get("overridden_by"),
            visibility=Visibility.from_value(data.get("visibility", "user")),
        )


_SEVERITY_MARK = {"info": "·", "success": "✓", "warning": "!", "error": "!!"}


class InteractionLayer:
    """面向用户的信息面 + agent 改写入口。"""

    def __init__(self, root: Path, policy: VisibilityPolicy | None = None) -> None:
        self.root = Path(root)
        self.state_path = self.root / STATE_FILE
        self.load_warnings: list[str] = []
        self._views: list[View] = []
        self._policy = policy or VisibilityPolicy()
        self._seq = 0
        self._load()

    # ---- 生命周期 -----------------------------------------------------------

    def ensure(self) -> "InteractionLayer":
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._save()
        return self

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.load_warnings.append(
                f"{self.state_path.name} 无法解析（{exc}），交互层已回退到默认配置"
            )
            return
        if not isinstance(payload, dict):
            self.load_warnings.append(
                f"{self.state_path.name} 顶层不是对象，交互层已回退到默认配置"
            )
            return

        raw_policy = payload.get("policy")
        if isinstance(raw_policy, dict) and raw_policy:
            self._policy = VisibilityPolicy.from_dict(raw_policy)

        for raw in payload.get("views") or []:
            try:
                self._views.append(View.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                self.load_warnings.append(f"忽略了一条无法解析的视图：{raw!r}")
        self._seq = max((v.seq for v in self._views), default=0)

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "views": [v.to_dict(self._policy.resolve(v.topic)) for v in self._views],
            "policy": self._policy.to_dict(),
        }
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # ---- 策略（用户可改） ---------------------------------------------------

    def policy(self) -> VisibilityPolicy:
        return self._policy

    def set_policy(self, topic: str, visibility: Visibility | str) -> VisibilityPolicy:
        self._policy.set(topic, Visibility.from_value(visibility))
        self._save()
        return self._policy

    # ---- 软件 → 用户 --------------------------------------------------------

    def publish(self, view: View) -> View:
        """落一条视图。软件各层直接调用 —— 不需要经过 agent。"""
        view.visibility = self._policy.resolve(view.topic)
        self._seq += 1
        view.seq = self._seq
        self._views = [v for v in self._views if v.view_id != view.view_id]
        self._views.append(view)
        self._save()
        return view

    def emit(
        self,
        topic: str,
        title: str,
        *,
        lines: list[str] | None = None,
        severity: str = "info",
        source: str = "software",
        view_id: str | None = None,
    ) -> View:
        sections = [ViewSection(lines=[str(x) for x in lines])] if lines else []
        return self.publish(
            View(
                view_id=view_id or f"{topic}:{uuid.uuid4().hex[:8]}",
                topic=topic,
                title=title,
                sections=sections,
                severity=severity,
                source=source,
            )
        )

    def announce(self, message: str, *, severity: str = "info", topic: str = "agent.message") -> View:
        """agent 直接对用户说话。署名会标成 agent，用户知道这不是软件自己说的。"""
        return self.emit(topic, "agent 提示", lines=[message], severity=severity, source="agent")

    # ---- agent → 交互层改写 -------------------------------------------------

    def _require(self, view_id: str) -> View:
        for view in self._views:
            if view.view_id == view_id:
                return view
        raise InteractionError(
            f"交互层里没有这条视图：{view_id!r}",
            hint="先用 `gametrans ui views` 查看现有视图 id。",
        )

    def override(
        self,
        view_id: str,
        *,
        title: str | None = None,
        sections: list[ViewSection] | None = None,
        note: str | None = None,
        by: str = "agent",
    ) -> View:
        """agent 改写一条已经发给用户（或即将发给用户）的视图。"""
        view = self._require(view_id)
        if title is not None:
            view.title = title
        if sections is not None:
            view.sections = list(sections)
        if note:
            view.sections.append(ViewSection(title="agent 注记", lines=[note]))
        view.overridden_by = by
        self._save()
        return view

    def hide(self, view_id: str, *, hidden: bool = True, by: str = "software") -> View:
        view = self._require(view_id)
        view.hidden = hidden
        view.hidden_by = by if hidden else None
        self._save()
        return view

    def pin(self, view_id: str, *, pinned: bool = True) -> View:
        view = self._require(view_id)
        view.pinned = pinned
        self._save()
        return view

    def clear(self) -> int:
        """清掉非置顶视图；置顶的（通常是"项目状态"）留着。"""
        before = len(self._views)
        self._views = [v for v in self._views if v.pinned]
        self._save()
        return before - len(self._views)

    # ---- 读取 ---------------------------------------------------------------

    def all_views(self) -> list[View]:
        """全部视图，含对用户透明的那些 —— agent 用这个。"""
        return sorted(self._views, key=lambda v: v.seq)

    def visible_views(self) -> list[View]:
        """用户实际会看到的东西：策略判定可见，且没被隐藏。"""
        views = [
            v
            for v in self._views
            if not v.hidden and self._policy.resolve(v.topic) is Visibility.USER
        ]
        views.sort(key=lambda v: (not v.pinned, -v.seq))
        return views

    def snapshot(self) -> dict[str, Any]:
        return {
            "views": [v.to_dict(self._policy.resolve(v.topic)) for v in self.all_views()],
            "policy": self._policy.to_dict(),
        }

    def render(self, views: list[View] | None = None) -> str:
        """渲染成给用户看的纯文本。

        传 ``views`` 可以只渲染指定的一批（CLI 用它只讲"本次命令发生了什么"）；
        不传则渲染当前全部用户可见视图。空的时候返回空串，调用方自行决定要不要打印。
        """
        if views is None:
            views = self.visible_views()
        lines: list[str] = []
        for view in views:
            mark = _SEVERITY_MARK.get(view.severity, "·")
            lines.append(f"[{mark}] {view.title}")
            if view.overridden_by:
                lines.append(f"    （本条已被 {view.overridden_by} 改写）")
            for section in view.sections:
                if section.title:
                    lines.append(f"  {section.title}:")
                for line in section.lines:
                    lines.append(f"    {line}")
            lines.append("")
        if not lines:
            return ""
        return "\n".join(lines).rstrip() + "\n"
