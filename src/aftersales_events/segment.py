"""会话切分：同一群内按时间间隔、消息数、字符数切成窗口，窗口间保留少量重叠。"""

from __future__ import annotations

from datetime import timedelta
from itertools import groupby
from typing import Any

from .models import Message, Window


def segment(messages: list[Message], cfg: dict[str, Any]) -> list[Window]:
    gap = timedelta(minutes=int(cfg.get("gap_minutes", 30)))
    max_msgs = int(cfg.get("max_messages", 60))
    max_chars = int(cfg.get("max_chars", 12000))
    overlap = int(cfg.get("overlap", 3))

    windows: list[Window] = []
    msgs = sorted(messages, key=lambda m: (m.group_number, m.send_time, m.id))
    for group_number, it in groupby(msgs, key=lambda m: m.group_number):
        group_msgs = list(it)
        chunks: list[list[Message]] = []
        cur: list[Message] = []
        cur_chars = 0
        for m in group_msgs:
            hard_break = bool(cur) and m.send_time - cur[-1].send_time > gap
            soft_break = bool(cur) and (len(cur) >= max_msgs or cur_chars + len(m.content) > max_chars)
            if hard_break:
                chunks.append(cur)
                cur, cur_chars = [], 0
            elif soft_break:
                chunks.append(cur)
                cur = cur[-overlap:] if overlap else []
                cur_chars = sum(len(x.content) for x in cur)
            cur.append(m)
            cur_chars += len(m.content)
        if cur:
            chunks.append(cur)

        first = group_msgs[0]
        for i, chunk in enumerate(chunks):
            windows.append(
                Window(
                    window_id=f"{group_number}#{chunk[0].send_time:%Y%m%d%H%M}-{i}",
                    group_number=group_number,
                    group_name=first.group_name,
                    customer_uid=first.customer_uid,
                    messages=chunk,
                )
            )
    return windows
