"""翻译记忆：同句不重复花钱。

落盘在 ``.gametrans/resources/memory.jsonl``，每行一个 JSON 对象 —— 和术语表、世界书
一个待遇：**人能看懂、agent 能直接编辑**。按目标语言分桶（日语译文不能当中文译文用）。

两条边界，写在最前面免得日后走偏：

* **精确命中才自动复用** —— 归一化空白后逐字相同。相近的句子未必能照抄；
* **模糊命中只作参考** —— 塞进提示词给模型看，绝不自动采用；相似度阈值是拍脑袋定的
  起点（``validated=False`` 的味道），要用真实语料校准。

**设计出处（借来的机制，标在这里）** —— 这不是我们发明的做法，是 CAT 工具与
TM-NMT 里已经成熟的机制，我们只是把它接进"单元级提示词翻译"这条流水线：

* **精确命中自动套用**：SDL Trados 的 auto-propagation 与 OmegaT 的
  "auto-propagation of unique translations"（相同句段直接套用已有译文）。
  <https://docs.rws.com/en-US/trados-studio-2024-sr1-1187677/step-7-reusing-previously-translated-files-perfectmatch--332273>
  （Trados PerfectMatch：源文件未变时整套复用）
  <https://sources.debian.org/src/omegat/3.6.0.10+dfsg-3/docs/ca/instantStartGuide.html/>
* **匹配档位（100% / 95–99% / 85–94% / 75–84%）与各自处置**：业界通行分档。
  <https://help.smartcat.com/translation-memory-create-import-export-and-manage/>
* **TM 喂给神经翻译的两种形态**（检索式引导 / 记忆增强）：
  Gu et al., AAAI 2019 <https://ojs.aaai.org/index.php/AAAI/article/download/12013/11872>；
  Cai et al., ACL 2021 <https://aclanthology.org/2021.acl-long.246.pdf>
* **一件必须说清的区别**：DeepSeek 的"上下文硬盘缓存"**不是** TM ——
  它是 **prompt 前缀缓存**（同一段前缀重复出现时省 prefill），
  不认"同一句话出现在不同上下文里"。两者可叠加，但不能互相替代。
  <https://api-docs.deepseek.com/zh-cn/guides/kv_cache/>

**我们与上面几家的差别**（免得被当成"照搬"）：我们的复用必须过**同一道结构闸门**
（受保护 token / 占位符 / 控制码逐句校验），而且复用的来源与指纹都留痕 ——
CAT 工具的复用是"内容直接落地"，我们这里还要过"能不能写回引擎"这一关。
"""

from __future__ import annotations

import difflib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gametrans.core.models import Issue

MEMORY_FILE = "memory.jsonl"

#: "别人的成品"这个来源的标记 —— 读游戏里已有的译文（``resource harvest``）写进来的
#: 那些条目就带它。它同时是**能不能复用**的判据之一：这类条目没有知识指纹（见 lookup），
#: 所以默认不复用，只有调用方显式声明才放行。来源字符串只此一处，改名不会有漏改。
IMPORTED_PROVIDER = "imported"

#: 模糊匹配的默认阈值与上限。占位值，未校准。
DEFAULT_THRESHOLD = 0.6
DEFAULT_LIMIT = 3

#: 少于这个长度的原文不进模糊候选池：短句相似度噪声太大（"Yes." vs "No."）
MIN_FUZZY_LENGTH = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text: str) -> str:
    """精确匹配前只做一件事：把连续空白折成一个空格并去掉首尾。

    刻意**不**做大小写折叠 —— 大小写在多数语言里是有意义的。
    """
    return " ".join(text.split())


@dataclass
class MemoryEntry:
    """一条记忆：某个目标语言下，这句话这么译。"""

    source: str
    target: str
    language: str
    provider: str = ""
    #: 被复用过多少次（精确命中即 +1，看它能省多少）
    reuses: int = 0
    updated_at: str = field(default_factory=_now)
    #: 产出这条译文时的知识指纹。对不上就说明术语/风格已经变了 —— 不能复用。
    knowledge_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "language": self.language,
            "provider": self.provider,
            "reuses": self.reuses,
            "updated_at": self.updated_at,
            "knowledge_fingerprint": self.knowledge_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        return cls(
            source=str(data["source"]),
            target=str(data["target"]),
            language=str(data.get("language", "")),
            provider=str(data.get("provider", "")),
            reuses=int(data.get("reuses", 0) or 0),
            updated_at=str(data.get("updated_at", _now())),
            # 老格式没有这一项 —— 回退空串，于是它证明不了自己还有效。
            knowledge_fingerprint=str(data.get("knowledge_fingerprint", "")),
        )

    @property
    def key(self) -> tuple[str, str]:
        return (normalize(self.source), self.language)


@dataclass(frozen=True)
class MemoryHit:
    """一条模糊命中的建议（带相似度，供参考）。"""

    entry: MemoryEntry
    score: float


class MemorySnapshot:
    """记忆的只读视图：成员与内容冻结在取快照的那一刻。

    见 :meth:`TranslationMemory.snapshot` —— 批内查询读它，本批新提交的条目下一批才可见。
    复用计数仍记在实时库上，统计口径不变。
    """

    __slots__ = ("_store", "_entries", "_needles")

    def __init__(
        self,
        store: "TranslationMemory",
        entries: dict[tuple[str, str], MemoryEntry],
        needles: dict[tuple[str, str], str],
    ) -> None:
        self._store = store
        self._entries = entries
        self._needles = needles

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(
        self,
        text: str,
        language: str,
        *,
        fingerprint: str | None = None,
        allow_imported: bool = False,
    ) -> MemoryEntry | None:
        entry = self._store._lookup_in(
            self._entries,
            text,
            language,
            fingerprint=fingerprint,
            allow_imported=allow_imported,
        )
        if entry is not None:
            self._store._note_reuse(entry)
        return entry


class TranslationMemory:
    """翻译记忆的文件门面。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[tuple[str, str], MemoryEntry] = {}
        #: 条目键 → 归一化后的原文（加载时算一次，见 :meth:`_load`）
        self._needles: dict[tuple[str, str], str] = {}
        self._loaded = False
        #: 有未落盘的改动（复用计数等）
        self._dirty = False
        #: 翻译层是**多线程**跑批次的（ThreadPoolExecutor），记忆要能被并发读
        self._lock = threading.RLock()

    # ---- 读写 ---------------------------------------------------------------

    def ensure(self) -> "TranslationMemory":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        return self

    def _load(self) -> None:
        """把文件读进内存。

        注意顺序：**先建好局部字典，最后才置 `_loaded`**。反过来写的话，另一个线程会
        撞进"看起来已加载、实际是空字典"的窗口，于是随机漏命中（真实发生过的 flaky）。

        原文的归一化形式**在这里算一次**并缓存：相近译法的查询要拿它跟每条记忆比，
        放在查询里算就是"每条记忆 × 每次查询"（真实工程上 9.7M 次正则替换）。
        """
        with self._lock:
            if self._loaded:
                return
            entries: dict[tuple[str, str], MemoryEntry] = {}
            needles: dict[tuple[str, str], str] = {}
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = MemoryEntry.from_dict(json.loads(line))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue  # 非法行由 validate() 报出来，读取时跳过
                    entries[entry.key] = entry
                    needles[entry.key] = normalize(entry.source)
            self._entries = entries
            self._needles = needles
            self._loaded = True

    def _raw_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def _write(self, lines: Iterable[str]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._loaded = False
            self._dirty = False

    # ---- 查询 ---------------------------------------------------------------

    def lookup(
        self,
        text: str,
        language: str,
        *,
        count: bool = True,
        fingerprint: str | None = None,
        allow_imported: bool = False,
    ) -> MemoryEntry | None:
        """精确命中（归一化空白后逐字相同）。

        ``count=True`` 时顺手把复用计数 +1 —— 但**只在内存里**，落盘由调用方
        在跑完一批之后统一触发（见 :meth:`flush`），否则几千条文本会退化成 O(n²)
        的文件写。

        ``fingerprint`` 给定时只认**同一知识状态**下产出的那条：术语表/风格改过之后
        旧译文就是未命中，得重新问模型。不给就只按原文命中（供只读查询用）。

        ``allow_imported`` 是给"游戏里已经有译文"那一类用的：读进来的成品（来源
        :data:`IMPORTED_PROVIDER`）压根没有"当时的我方知识状态"可对，指纹恒为空，
        所以默认与"老格式条目"一个待遇 —— **证明不了有效就不复用**。调用方声明之后
        才放行，而且**只放行这一种**：我们自己的译文一旦指纹对不上，照样算过期。
        """
        self._load()
        with self._lock:
            entry = self._lookup_in(
                self._entries,
                text,
                language,
                fingerprint=fingerprint,
                allow_imported=allow_imported,
            )
            if entry is not None and count:
                entry.reuses += 1
                entry.updated_at = _now()
                self._dirty = True
            return entry

    def _lookup_in(
        self,
        entries: dict[tuple[str, str], MemoryEntry],
        text: str,
        language: str,
        *,
        fingerprint: str | None,
        allow_imported: bool,
    ) -> MemoryEntry | None:
        """命中判据只有这一处（归一化原文 + 指纹）：实时库与快照都走它。"""
        entry = entries.get((normalize(text), language))
        if entry is None:
            return None
        if fingerprint is not None and entry.knowledge_fingerprint != fingerprint:
            if not (
                allow_imported
                and entry.provider == IMPORTED_PROVIDER
                and not entry.knowledge_fingerprint
            ):
                # 包括"记的时候还没这套指纹"的老条目 —— 证明不了有效就不能复用。
                return None
        return entry

    def _note_reuse(self, entry: MemoryEntry) -> None:
        """把一次复用记在**实时**条目上：只标脏，不当场落盘。

        落盘仍由调用方在跑完一批之后统一触发（见 :meth:`flush`）—— 每条命中都重写
        整个文件的话，几千条记忆会退化成 O(n²) 的写。
        （公开的 :meth:`count_reuse` 是给外部调用用的，它自己会落盘。）
        """
        with self._lock:
            live = self._entries.get(entry.key)
            if live is None:
                return
            live.reuses += 1
            live.updated_at = _now()
            self._dirty = True

    def snapshot(self) -> "MemorySnapshot":
        """此刻的记忆视图 —— **这一批开始前已提交的集合**，批内不再变。

        为什么需要：跑批是并发的，"后来者"能看见什么取决于谁先跑完 —— 输入就绑在时钟上了
        （同一张图跑两次，同一个单元可能拿到不同输入，三次重复的对照随之作废）。
        批内查询一律读快照，本批新提交的条目**下一批才可见**。

        浅拷贝：判据只有"成员"与"条目内容"，复用计数不参与命中，所以不必深拷。
        """
        self._load()
        with self._lock:
            return MemorySnapshot(self, dict(self._entries), dict(self._needles))

    def flush(self) -> None:
        """把内存里的改动落盘（复用计数、批量写入都靠它）。"""
        with self._lock:
            if self._dirty:
                self._flush()

    def suggest(
        self,
        text: str,
        language: str,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        limit: int = DEFAULT_LIMIT,
    ) -> list[MemoryHit]:
        """相近译法（**只作参考**，不自动采用）。"""
        self._load()
        needle = normalize(text)
        if len(needle) < MIN_FUZZY_LENGTH:
            return []
        hits: list[MemoryHit] = []
        for key, entry in self._entries.items():
            if entry.language != language:
                continue
            candidate = self._needles.get(key)
            if candidate is None:  # pragma: no cover - 缓存与条目应当同步
                candidate = normalize(entry.source)
            if len(candidate) < MIN_FUZZY_LENGTH:
                continue
            # 便宜的预筛：长度差太多就不可能像
            ratio = len(needle) / max(len(candidate), 1)
            if ratio < 0.5 or ratio > 2.0:
                continue
            score = difflib.SequenceMatcher(None, needle, candidate).ratio()
            if score >= threshold:
                hits.append(MemoryHit(entry=entry, score=round(score, 4)))
        hits.sort(key=lambda hit: (-hit.score, hit.entry.source))
        return hits[:limit]

    def entries(self, language: str | None = None) -> list[MemoryEntry]:
        self._load()
        values = [
            entry
            for entry in self._entries.values()
            if language is None or entry.language == language
        ]
        return sorted(values, key=lambda entry: (entry.language, entry.source))

    # ---- 写入 ---------------------------------------------------------------

    def remember(
        self,
        source: str,
        target: str,
        *,
        language: str,
        provider: str = "",
        knowledge_fingerprint: str = "",
    ) -> MemoryEntry:
        """记下/覆盖一条。同一个 (原文, 语言) 只留一条。

        **空原文直接拒绝**：记忆的键就是原文，空原文等于一个"谁都能对上"的键 ——
        真靶上真的留下过这种垃圾（整单元写回时原文是空的，8,157 条里第一条就是），
        它既不能复用，还会让任何空字符串去查到它。
        """
        if not str(source).strip():
            raise ValueError("记忆条目的原文不能为空：这条既复用不了，还会污染空串查询")
        self.ensure()
        self._load()
        entry = MemoryEntry(
            source=source,
            target=target,
            language=language,
            provider=provider,
            knowledge_fingerprint=knowledge_fingerprint,
        )
        self._entries[entry.key] = entry
        self._flush()
        return entry

    def remember_many(
        self,
        entries: list[tuple[str, str, str]],
        *,
        language: str,
        provider: str = "",
    ) -> int:
        """批量记下 —— 一次落盘。

        三条一组：``(原文, 译文, 产出它时的知识指纹)``。

        单条 remember() 每次都要重写整个文件，几千条文本会退化成 O(n²) 的文件写；
        翻译层因此把新译文攒起来，跑完一次性写回。

        空原文的行**丢掉**（返回的是真正存下的条数）：批量落盘是跑完才做的，
        为一条垃圾把整轮翻译打断不值得；但也不许它进库（见 :meth:`remember`）。
        """
        if not entries:
            return 0
        self.ensure()
        self._load()
        stored = 0
        for source, target, fingerprint in entries:
            if not str(source).strip():
                continue
            entry = MemoryEntry(
                source=source,
                target=target,
                language=language,
                provider=provider,
                knowledge_fingerprint=fingerprint,
            )
            self._entries[entry.key] = entry
            stored += 1
        self._flush()
        return stored

    def bump_reuses(self, keys: list[tuple[str, str]]) -> int:
        """批量累加复用计数（同样只落盘一次）。"""
        if not keys:
            return 0
        self._load()
        bumped = 0
        for key in keys:
            entry = self._entries.get(key)
            if entry is None:
                continue
            entry.reuses += 1
            entry.updated_at = _now()
            bumped += 1
        if bumped:
            self._flush()
        return bumped

    def count_reuse(self, entry: MemoryEntry) -> None:
        """被复用一次：计数 +1 并落盘（用来回答"这条记忆省了多少"）。"""
        self._load()
        current = self._entries.get(entry.key)
        if current is None:
            return
        current.reuses += 1
        current.updated_at = _now()
        self._flush()

    def remove(self, source: str, language: str) -> bool:
        self._load()
        key = (normalize(source), language)
        removed = self._entries.pop(key, None) is not None
        self._needles.pop(key, None)
        if removed:
            self._flush()
        return removed

    def _flush(self) -> None:
        # 调用方在持锁状态下进来（RLock，可重入）
        lines = [
            json.dumps(entry.to_dict(), ensure_ascii=False)
            for entry in sorted(
                self._entries.values(), key=lambda e: (e.language, e.source)
            )
        ]
        self._write(lines)

    # ---- 校验与摘要 ---------------------------------------------------------

    def validate(self) -> list[Issue]:
        """非法行如实报出来（读取时它们被跳过，但不能装作没看见）。"""
        issues: list[Issue] = []
        for index, line in enumerate(self._raw_lines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("不是一个 JSON 对象")
                MemoryEntry.from_dict(payload)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                issues.append(
                    Issue(
                        code="invalid_memory_line",
                        message=f"memory.jsonl 第 {index} 行不合法：{exc}",
                        ref=f"{self.path}:{index}",
                    )
                )
        return issues

    def summary(self) -> dict[str, Any]:
        entries = self.entries()
        by_language: dict[str, int] = {}
        for entry in entries:
            by_language[entry.language] = by_language.get(entry.language, 0) + 1
        return {
            "path": str(self.path),
            "entries": len(entries),
            "by_language": dict(sorted(by_language.items())),
        }
