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


class OpenAICompatExtractor:
    """通过 OpenAI 兼容的 /chat/completions 接口调用模型（DeepSeek、公司网关等）。

    这类接口没有严格的结构化输出，因此：
    - 在 system prompt 末尾附上 JSON 格式说明，并开启 JSON 模式（网关不支持时自动关闭重试）；
    - 解析时逐个校验事件，格式不对的单个事件丢弃，不影响同一窗口里的其他事件。
    只用标准库发请求，不引入额外依赖。
    """

    JSON_INSTRUCTIONS = """

## 输出格式
只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。格式如下（没有事件时 events 为空数组）：
{"events": [{"type": "故障", "title": "15 字以内标题", "summary": "1-3 句话", "status": "跟进中", "level": "P2",
 "owner": "内部人员姓名或 null", "evidence": ["m1", "m3"], "attributes": [{"key": "channel", "value": "原文"}], "confidence": 0.9}]}
- status 只能是：新建 / 跟进中 / 已解决 / 已关闭；level 只能是：P0 / P1 / P2 / P3
- evidence 只能引用本段出现的消息编号；confidence 为 0 到 1 之间的小数"""

    RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

    def __init__(self, types: list[EventTypeDef], cfg: dict[str, Any], provider: str = "deepseek",
                 post: Any | None = None, sleep: Any | None = None):
        import os
        import time

        pcfg = cfg.get(provider) or {}
        env = provider.upper()
        self.provider = provider
        self.base_url = (pcfg.get("base_url") or "").rstrip("/")
        self.api_key = os.environ.get(f"{env}_API_KEY", "")
        if not self.base_url:
            raise ValueError(f"缺少 {env}_BASE_URL 或 llm.{provider}.base_url")
        if not self.api_key:
            raise ValueError(f"缺少环境变量 {env}_API_KEY")
        self.model = pcfg.get("model") or "deepseek-chat"
        self.max_tokens = int(pcfg.get("max_tokens", cfg.get("max_tokens", 8000)))
        self.temperature = pcfg.get("temperature", 0)
        self.timeout = float(pcfg.get("timeout_seconds", 300))
        self.retries = int(pcfg.get("retries", 3))
        self.json_mode = bool(pcfg.get("json_mode", True))
        self.system_prompt = build_system_prompt(types) + self.JSON_INSTRUCTIONS
        self._post = post or self._http_post
        self._sleep = sleep or time.sleep

    def _http_post(self, url: str, body: dict) -> tuple[int, dict]:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return e.code, {"error": raw[:300].decode(errors="replace")}

    def _request(self, window: Window, body: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        for attempt in range(self.retries + 1):
            try:
                code, data = self._post(url, body)
            except OSError as e:  # 网络错误、超时
                code, data = 0, {"error": str(e)}
            if code == 200:
                return data
            if code == 400 and "response_format" in body:
                # 部分网关/模型不支持 JSON 模式：去掉后重试，提示词里已有格式要求
                log.warning("window %s: 接口不接受 JSON 模式，关闭后重试", window.window_id)
                self.json_mode = False
                body = {k: v for k, v in body.items() if k != "response_format"}
                continue
            if (code == 0 or code in self.RETRY_STATUS) and attempt < self.retries:
                wait = min(30, 2 ** attempt * 2)
                log.warning("window %s: HTTP %s，%ss 后重试：%s", window.window_id, code, wait, str(data)[:200])
                self._sleep(wait)
                continue
            raise RuntimeError(f"{self.provider} 接口错误 HTTP {code}: {str(data)[:300]}")
        raise RuntimeError(f"{self.provider} 接口多次重试仍失败")

    @staticmethod
    def parse(text: str, window_id: str = "") -> WindowExtraction:
        """容错解析：去掉代码块标记、截取最外层 JSON 对象，逐个校验事件。"""
        t = (text or "").strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else ""
            t = t.rsplit("```", 1)[0]
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end < start:
            log.warning("window %s: 输出不是 JSON，跳过：%s", window_id, t[:200])
            return WindowExtraction(events=[])
        try:
            data = json.loads(t[start:end + 1])
        except json.JSONDecodeError as e:
            log.warning("window %s: JSON 解析失败（%s），跳过", window_id, e)
            return WindowExtraction(events=[])
        items = data.get("events", []) if isinstance(data, dict) else []
        events = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("attributes"), dict):  # 兼容 {"k": "v"} 写法
                item = {**item, "attributes": [{"key": k, "value": str(v)} for k, v in item["attributes"].items()]}
            try:
                events.append(ExtractedEvent.model_validate(item))
            except Exception as e:
                log.warning("window %s: 丢弃格式不正确的事件 %s：%s", window_id, str(item)[:120], str(e).splitlines()[0])
        return WindowExtraction(events=events)

    def extract(self, window: Window) -> WindowExtraction:
        user_text, _ = render_window(window)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user_text}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        data = self._request(window, body)
        choice = (data.get("choices") or [{}])[0]
        u = data.get("usage") or {}
        log.info("window %s: in=%s cache_hit=%s out=%s", window.window_id, u.get("prompt_tokens"),
                 u.get("prompt_cache_hit_tokens", (u.get("prompt_tokens_details") or {}).get("cached_tokens")), u.get("completion_tokens"))
        if choice.get("finish_reason") == "length":
            log.warning("window %s: 输出被 max_tokens 截断，跳过", window.window_id)
            return WindowExtraction(events=[])
        return self.parse((choice.get("message") or {}).get("content") or "", window.window_id)


PROVIDERS = ("claude", "deepseek")


def make_extractor(types: list[EventTypeDef], cfg: dict[str, Any]) -> Extractor:
    provider = (cfg.get("provider") or "claude").lower()
    if provider == "claude":
        return ClaudeExtractor(types, cfg)
    if provider in PROVIDERS:
        return OpenAICompatExtractor(types, cfg, provider)
    raise ValueError(f"不支持的 llm.provider：{provider}（可选 {', '.join(PROVIDERS)}）")


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
