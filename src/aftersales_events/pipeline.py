"""串起整条流水线：原始数据 → 预处理 → 切分 → 抽取 → 合并 → 输出。"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .extract import Extractor, dump_extraction, to_events
from .merge import merge_events
from .models import Event, RawResponse, Window
from .preprocess import preprocess
from .segment import segment

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    events: list[Event]
    windows: list[Window]
    stats: dict = field(default_factory=dict)


def run(raw: RawResponse, settings: Settings, extractor: Extractor, out_dir: str | Path | None = None) -> RunResult:
    types = settings.enabled_types
    messages, pstats = preprocess(raw, types, settings.section("preprocess"))
    windows = segment(messages, settings.section("segment"))
    log.info("预处理：%s；窗口数：%d", pstats, len(windows))

    out = Path(out_dir) if out_dir else None
    if out:
        (out / "windows").mkdir(parents=True, exist_ok=True)

    def work(w: Window) -> list[Event]:
        try:
            ext = extractor.extract(w)
        except Exception as e:  # 单个窗口失败不影响整体
            log.error("window %s 抽取失败：%s", w.window_id, e)
            return []
        if out:
            safe = w.window_id.replace(":", "_").replace("#", "_")
            (out / "windows" / f"{safe}.json").write_text(dump_extraction(ext), encoding="utf-8")
        return to_events(w, ext, {t.name for t in types})

    concurrency = int(settings.section("llm").get("concurrency", 4))
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        raw_events = [e for evs in pool.map(work, windows) for e in evs]
    events = merge_events(raw_events, settings.section("merge"))

    stats = {
        "preprocess": pstats,
        "windows": len(windows),
        "events_before_merge": len(raw_events),
        "events": len(events),
        "by_type": {t.name: sum(1 for e in events if e.type == t.name) for t in types},
    }
    if out:
        write_outputs(events, stats, out)
    return RunResult(events=events, windows=windows, stats=stats)


def write_outputs(events: list[Event], stats: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(e.model_dump_json() + "\n")
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(render_report(events, stats), encoding="utf-8")


def render_report(events: list[Event], stats: dict) -> str:
    lines = ["# 事件清洗报告", "", f"- 窗口数：{stats['windows']}", f"- 事件数：{stats['events']}（合并前 {stats['events_before_merge']}）",
             f"- 预处理：{stats['preprocess']}", ""]
    by_type: dict[str, list[Event]] = {}
    for e in events:
        by_type.setdefault(e.type, []).append(e)
    for t, evs in by_type.items():
        lines.append(f"## {t}（{len(evs)}）")
        lines.append("")
        for e in evs:
            lines.append(f"### [{e.level}] {e.title} · {e.status}")
            lines.append(f"- 群：{e.group_name}　客户：{e.customer_uid or '-'}　跟进：{e.owner or '-'}")
            lines.append(f"- 时间：{e.start_time:%m-%d %H:%M} ~ {e.end_time:%m-%d %H:%M}　置信度：{e.confidence:.2f}")
            lines.append(f"- {e.summary}")
            if e.attributes:
                lines.append("- 属性：" + "；".join(f"{k}={v}" for k, v in e.attributes.items()))
            lines.append(f"- 证据：{', '.join(e.evidence_msg_ids)}")
            lines.append("")
    return "\n".join(lines)
