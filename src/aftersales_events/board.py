"""事件看板：把 events.jsonl 渲染成一个单文件 HTML 看板。

- 不依赖服务端：数据内嵌在页面里，双击即可打开，可直接发给同事。
- 列可以按 状态 / 类型 / 等级 / 跟进人 切换；支持搜索与筛选。
- 拖拽卡片即可修改对应字段（存在浏览器本地），可导出修改后的 events.jsonl。
- 提供原始数据时，点开卡片能看到证据消息原文（已脱敏）。
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Iterable

from .config import EventTypeDef
from .models import Event, Message

STATUSES = ["新建", "跟进中", "已解决", "已关闭"]
LEVELS = ["P0", "P1", "P2", "P3"]


def find_event_files(paths: Iterable[str | Path]) -> list[Path]:
    """文件原样返回；目录则递归查找其中所有 events.jsonl（按修改时间从旧到新）。"""
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files.extend(sorted(p.rglob("events.jsonl"), key=lambda f: f.stat().st_mtime))
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(f"找不到 {p}")
    return files


def load_events(paths: Iterable[str | Path]) -> list[Event]:
    """读取多个 events.jsonl；同一 event_id 出现多次时，以后读到的（更新的）为准。"""
    by_id: dict[str, Event] = {}
    for f in find_event_files(paths):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = Event.model_validate_json(line)
                by_id[e.event_id] = e
    return list(by_id.values())


def evidence_messages(events: list[Event], messages: Iterable[Message]) -> dict[str, dict]:
    """只保留事件引用到的消息，避免把整份聊天记录塞进页面。"""
    wanted = {i for e in events for i in e.evidence_msg_ids}
    out: dict[str, dict] = {}
    for m in messages:
        if m.id in wanted:
            out[m.id] = {
                "t": m.send_time.isoformat(),
                "role": m.role,
                "sender": m.sender,
                "content": "<图片>" if m.is_image else m.content,
            }
    return out


def build_payload(
    events: list[Event],
    types: list[EventTypeDef] | None = None,
    messages: Iterable[Message] | None = None,
    title: str = "售后事件看板",
) -> dict:
    type_defs = [{"name": t.name, "push_to": t.push_to} for t in (types or [])]
    known = {t["name"] for t in type_defs}
    for e in events:  # 配置里已停用、但历史结果里还有的类型也要能显示
        if e.type not in known:
            known.add(e.type)
            type_defs.append({"name": e.type, "push_to": ""})
    ids = sorted(e.event_id for e in events)
    return {
        "title": title,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        # 用事件集合生成看板 ID：同一批事件重复生成时，浏览器里的修改不会丢
        "board_id": hashlib.sha1("|".join(ids).encode()).hexdigest()[:12],
        "statuses": STATUSES,
        "levels": LEVELS,
        "types": type_defs,
        "events": [json.loads(e.model_dump_json()) for e in events],
        "messages": evidence_messages(events, messages) if messages is not None else {},
    }


def _embed(payload: dict) -> str:
    # 转义 "<"，防止聊天内容里出现 </script> 之类的字符串破坏页面
    return json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")


def render_board(events: list[Event], types=None, messages=None, title: str = "售后事件看板") -> str:
    tpl = resources.files("aftersales_events").joinpath("templates/board.html").read_text(encoding="utf-8")
    return tpl.replace("__AES_TITLE__", html.escape(title)).replace("__AES_DATA__", _embed(build_payload(events, types, messages, title)))


def write_board(path: str | Path, events: list[Event], types=None, messages=None, title: str = "售后事件看板") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_board(events, types, messages, title), encoding="utf-8")
    return path
