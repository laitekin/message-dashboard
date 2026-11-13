"""事件合并：同一件事可能横跨多个窗口（重叠窗口、跨天跟进），合并为一个事件。

合并条件（同群 + 同类型 且满足其一）：
  1. 证据消息有交集（重叠窗口导致的重复）
  2. 时间相近（window_hours 内）且标题相似度 ≥ title_similarity
"""

from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any

from .models import Event

STATUS_ORDER = {"新建": 0, "跟进中": 1, "已解决": 2, "已关闭": 3}
LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _should_merge(a: Event, b: Event, window: timedelta, sim: float) -> bool:
    if a.group_number != b.group_number or a.type != b.type:
        return False
    if set(a.evidence_msg_ids) & set(b.evidence_msg_ids):
        return True
    close = b.start_time - a.end_time <= window and a.start_time - b.end_time <= window
    return close and _similar(a.title, b.title) >= sim


def _combine(a: Event, b: Event) -> Event:
    later = a if a.end_time >= b.end_time else b
    ids = list(dict.fromkeys(a.evidence_msg_ids + b.evidence_msg_ids))
    attrs = {**a.attributes, **b.attributes} if later is b else {**b.attributes, **a.attributes}
    return a.model_copy(
        update=dict(
            start_time=min(a.start_time, b.start_time),
            end_time=max(a.end_time, b.end_time),
            # 状态以时间上更晚的为准，等级取更高（数字更小）
            status=later.status,
            level=min(a.level, b.level, key=LEVEL_ORDER.__getitem__),
            title=later.title,
            summary=later.summary,
            owner=later.owner or a.owner or b.owner,
            evidence_msg_ids=ids,
            attributes=attrs,
            confidence=max(a.confidence, b.confidence),
            source_windows=list(dict.fromkeys(a.source_windows + b.source_windows)),
        )
    )


def merge_events(events: list[Event], cfg: dict[str, Any]) -> list[Event]:
    window = timedelta(hours=float(cfg.get("window_hours", 72)))
    sim = float(cfg.get("title_similarity", 0.55))
    merged: list[Event] = []
    for e in sorted(events, key=lambda x: x.start_time):
        for i, m in enumerate(merged):
            if _should_merge(m, e, window, sim):
                merged[i] = _combine(m, e)
                break
        else:
            merged.append(e)
    return sorted(merged, key=lambda x: (LEVEL_ORDER[x.level], x.start_time))
