"""LLM 抽取：窗口 → 结构化事件。

- 消息用窗口内短编号 m1..mN 表示，模型只需引用编号，再由代码映射回真实消息 ID 和时间，
  避免模型抄错长 ID / 时间。
- 证据编号校验：引用了不存在编号的部分会被丢弃；没有任何有效证据的事件整体丢弃。
"""

from __future__ import annotations

import hashlib
import json
import logging
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from .config import EventTypeDef
from .models import Event, ExtractedEvent, Window, WindowExtraction

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- prompt 渲染


def render_event_types(types: list[EventTypeDef]) -> str:
    parts = []
    for t in types:
        lines = [f"### {t.name}（推送：{t.push_to}）", t.definition]
        if t.include:
            lines.append("算：" + "；".join(t.include))
        if t.exclude:
            lines.append("不算：" + "；".join(t.exclude))
        if t.attributes:
            lines.append("attributes 可用 key：" + "；".join(f"{k}（{v}）" for k, v in t.attributes.items()))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def build_system_prompt(types: list[EventTypeDef]) -> str:
    tpl = resources.files("aftersales_events").joinpath("prompts/system.md").read_text(encoding="utf-8")
    return tpl.replace("{event_types}", render_event_types(types))


def render_window(window: Window) -> tuple[str, dict[str, int]]:
    """返回 (用户消息文本, 短编号 → 窗口内下标)。"""
    idmap: dict[str, int] = {}
    lines = [
        f"群名：{window.group_name}",
        f"客户 UID：{window.customer_uid or '未知'}",
        f"时间范围：{window.start:%Y-%m-%d %H:%M} ~ {window.end:%Y-%m-%d %H:%M}",
        "",
        "## 聊天记录",
    ]
    for i, m in enumerate(window.messages):
        sid = f"m{i + 1}"
        idmap[sid] = i
        content = "<图片>" if m.is_image else m.content.replace("\n", " ⏎ ")
        lines.append(f"[{sid}] {m.send_time:%m-%d %H:%M} {m.role}·{m.sender}: {content}")

    ents: dict[str, list[str]] = {}
    for m in window.messages:
        for k, vs in m.entities.items():
            if k == "urls":
                continue
            bucket = ents.setdefault(k, [])
            bucket.extend(v for v in vs if v not in bucket)
    if ents:
        lines += ["", "## 规则抽取的实体（参考）"]
        lines += [f"- {k}: {', '.join(v[:15])}" for k, v in ents.items()]
    if window.hints:
        lines += ["", f"## 关键词命中的候选类型（参考）\n{', '.join(window.hints)}"]
    lines += ["", "请抽取这段对话中的事件。"]
    return "\n".join(lines), idmap


# ---------------------------------------------------------------- 抽取器


class Extractor(Protocol):
    def extract(self, window: Window) -> WindowExtraction: ...


class ClaudeExtractor:
    """通过 Anthropic SDK 调用 Claude，使用结构化输出直接得到 WindowExtraction。"""

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, types: list[EventTypeDef], cfg: dict[str, Any], client: Any | None = None):
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.cfg = cfg
        self.system_prompt = build_system_prompt(types)

    def extract(self, window: Window) -> WindowExtraction:
        import anthropic

        user_text, _ = render_window(window)
        kwargs: dict[str, Any] = dict(
            model=self.cfg.get("model", "claude-opus-5-5"),
            max_tokens=int(self.cfg.get("max_tokens", 16000)),
            # system prompt 在所有窗口间完全相同 → 缓存
            system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
            output_format=WindowExtraction,
            output_config={"effort": self.cfg.get("effort", "medium")},
        )
        try:
            if self.cfg.get("server_side_fallback", True):
                resp = self.client.beta.messages.parse(betas=[self.FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.parse(**kwargs)
        except anthropic.BadRequestError as e:
            log.error("window %s: 请求无效 %s", window.window_id, e.message)
            raise
        except anthropic.RateLimitError:
            log.warning("window %s: 触发限流（SDK 已自动重试）", window.window_id)
            raise
        except anthropic.APIStatusError as e:
            log.error("window %s: API 错误 %s %s", window.window_id, e.status_code, e.message)
            raise
        except anthropic.APIConnectionError:
            log.error("window %s: 网络错误", window.window_id)
            raise

        if resp.stop_reason == "refusal":
            log.warning("window %s: 模型拒答 (%s)，跳过", window.window_id, getattr(resp.stop_details, "category", None))
            return WindowExtraction(events=[])
        if resp.stop_reason == "max_tokens":
            log.warning("window %s: 输出被 max_tokens 截断，跳过", window.window_id)
            return WindowExtraction(events=[])
        u = resp.usage
        log.info(
            "window %s: in=%s cache_read=%s out=%s",
            window.window_id, u.input_tokens, getattr(u, "cache_read_input_tokens", 0), u.output_tokens,
        )
        return resp.parsed_output or WindowExtraction(events=[])


class DryRunExtractor:
    """不调用模型，只把每个窗口的完整 prompt 写到磁盘，用于检查 prompt / 估算规模。"""

    def __init__(self, types: list[EventTypeDef], out_dir: str | Path):
        self.system_prompt = build_system_prompt(types)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "system.md").write_text(self.system_prompt, encoding="utf-8")

    def extract(self, window: Window) -> WindowExtraction:
        user_text, _ = render_window(window)
        safe = window.window_id.replace(":", "_").replace("#", "_")
        (self.out_dir / f"{safe}.md").write_text(user_text, encoding="utf-8")
        return WindowExtraction(events=[])


# ---------------------------------------------------------------- 后处理


def to_events(window: Window, extraction: WindowExtraction, valid_types: set[str]) -> list[Event]:
    _, idmap = render_window(window)
    events: list[Event] = []
    for i, e in enumerate(extraction.events):
        if e.type not in valid_types:
            log.warning("window %s: 未知事件类型 %r，丢弃", window.window_id, e.type)
            continue
        idx = sorted({idmap[s.strip()] for s in e.evidence if s.strip() in idmap})
        if not idx:
            log.warning("window %s: 事件 %r 无有效证据，丢弃", window.window_id, e.title)
            continue
        msgs = [window.messages[j] for j in idx]
        events.append(_build_event(window, e, msgs, i))
    return events


def _build_event(window: Window, e: ExtractedEvent, msgs: list, i: int) -> Event:
    ids = [m.id for m in msgs]
    digest = hashlib.sha1(f"{window.group_number}|{e.type}|{ids[0]}".encode()).hexdigest()[:12]
    return Event(
        event_id=f"evt_{digest}",
        type=e.type,
        title=e.title,
        summary=e.summary,
        status=e.status,
        level=e.level,
        owner=e.owner,
        customer_uid=window.customer_uid,
        group_number=window.group_number,
        group_name=window.group_name,
        start_time=msgs[0].send_time,
        end_time=msgs[-1].send_time,
        evidence_msg_ids=ids,
        attributes={a.key: a.value for a in e.attributes if a.value},
        confidence=max(0.0, min(1.0, e.confidence)),
        source_windows=[window.window_id],
    )


def dump_extraction(extraction: WindowExtraction) -> str:
    return json.dumps(extraction.model_dump(), ensure_ascii=False, indent=1)
