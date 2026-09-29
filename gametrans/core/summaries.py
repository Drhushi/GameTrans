"""场摘要（标题 + 事件卡）：模型生成的**声明式产物**，住在 ``<工作区>/summaries.json``。

它不是图的事实：图只回答"有哪些文本、谁先谁后"；摘要是给人看的第二层，
所以单独一个文件、带 provenance（哪个模型、什么时候、依据多少字符），
**删掉它图照样成立**（节点回落到原文首句）。

文件形状::

    {
      "generated_by": "deepseek-chat",
      "generated_at": "2026-09-23T18:40:00+00:00",
      "chapter": "第 1 章",
      "synopsis": "……整章梗概……",
      "scenes": {
        "act1": {"title": "断肩之后的重启条件", "summary": "【场次】act1……", "unit_id": "unit_…"}
      }
    }
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["SUMMARIES_FILE", "load_summaries"]

#: 摘要文件的名字（住在工作区里，跟着项目走）。
SUMMARIES_FILE = "summaries.json"


def load_summaries(workdir: Path | None) -> dict[str, dict]:
    """读摘要并建索引：``场名 / unit_id → {title, summary}``。

    文件不在、读不动、格式不对都当"没有摘要"（节点会回落到原文，不报错）。
    """
    if workdir is None:
        return {}
    try:
        payload = json.loads((Path(workdir) / SUMMARIES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    index: dict[str, dict] = {}
    for scene, item in (payload.get("scenes") or {}).items():
        if not isinstance(item, dict):
            continue
        entry = {
            "title": str(item.get("title") or ""),
            "summary": str(item.get("summary") or ""),
        }
        index[str(scene)] = entry
        unit_id = str(item.get("unit_id") or "")
        if unit_id:
            index[unit_id] = entry
    return index
