"""规则预处理：标准化、去重、降噪、脱敏、实体抽取、关键词提示。

这一层零成本、可解释，产出直接喂给 LLM，降低模型负担。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Iterable

from .config import EventTypeDef
from .models import Message, RawResponse

# ---------------------------------------------------------------- 正则

RE_UID_IN_GROUP = re.compile(r"(\d{9,11})(?:<[^>]*>)?\s*$")
RE_URL = re.compile(r"https?://[^\s，。）)\]]+")
RE_IMAGE_URL = re.compile(r"https?://ticket-imgs\.qnssl\.com/\S+|https?://\S+\.(?:png|jpe?g|gif|webp)(?:\?\S*)?", re.I)
RE_MENTION = re.compile(r"@([一-龥A-Za-z0-9_\-]{1,20})")
RE_REQUEST_ID = re.compile(r"(?:request[_-]?id[=:：\s]*)?\b((?:chatptmsg|req|chatcmpl|msg)[-_][0-9a-zA-Z]{8,})", re.I)
RE_MODEL = re.compile(
    r"\b((?:claude|gpt|gemini|qwen|deepseek|doubao|glm|kimi|moonshot|grok|llama|mistral|minimax|ernie|hunyuan|o[134])"
    r"[-\w.]*)",
    re.I,
)
RE_QUANTITY = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:万|千|百|k|K|w|W|m|M)?\s*(?:TPM|RPM|QPS|TPS|qps|tpm|rpm|并发|tokens?|元|块|万元|G|GB|T|TB|Mbps|Gbps|ms|秒|s\b))"
)
RE_CHANNEL = re.compile(r"\b(newapi-[\w\-]+|channel[-_ ]?\d+)", re.I)

# 脱敏
RE_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})")
RE_LABELED_SECRET = re.compile(r"((?:api[_-]?key|apikey|token|secret|password|密码|密钥)\s*[:=：]\s*)(\S+)", re.I)
RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

RE_ONLY_SYMBOLS = re.compile(r"^[\s\W_]*$", re.U)


def parse_customer_uid(group_name: str) -> str | None:
    m = RE_UID_IN_GROUP.search(group_name or "")
    return m.group(1) if m else None


def mask(text: str) -> str:
    text = RE_EMAIL.sub("<EMAIL>", text)
    text = RE_PHONE.sub("<PHONE>", text)
    text = RE_SECRET.sub(lambda m: m.group(1)[:6] + "***", text)
    text = RE_LABELED_SECRET.sub(lambda m: m.group(1) + "***", text)
    return text


def extract_entities(text: str) -> dict[str, list[str]]:
    def uniq(xs: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(x.strip() for x in xs if x.strip()))

    ents = {
        "models": uniq(RE_MODEL.findall(text)),
        "request_ids": uniq(RE_REQUEST_ID.findall(text)),
        "quantities": uniq(RE_QUANTITY.findall(text)),
        "channels": uniq(RE_CHANNEL.findall(text)),
        "mentions": uniq(RE_MENTION.findall(text)),
        "urls": uniq(u for u in RE_URL.findall(text) if not RE_IMAGE_URL.match(u)),
    }
    return {k: v for k, v in ents.items() if v}


def keyword_hints(text: str, types: list[EventTypeDef]) -> list[str]:
    low = text.lower()
    return [t.name for t in types if any(k.lower() in low for k in t.keywords)]


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.strip().replace("/", "-"))


def is_noise(content: str, noise_set: set[str]) -> bool:
    c = content.strip()
    return not c or c in noise_set or bool(RE_ONLY_SYMBOLS.match(c))


def preprocess(raw: RawResponse, types: list[EventTypeDef], cfg: dict[str, Any]) -> tuple[list[Message], dict[str, int]]:
    """返回 (按群+时间排序的消息, 统计信息)。"""
    noise_set = set(cfg.get("noise_messages", []))
    dedup_window = timedelta(seconds=int(cfg.get("dedup_window_seconds", 600)))
    do_mask = cfg.get("mask_secrets", True)

    stats = {"raw": 0, "duplicate": 0, "noise": 0, "image": 0, "kept": 0}
    out: list[Message] = []
    seen_ids: set[str] = set()

    for g in raw.groups:
        uid = parse_customer_uid(g.group_name)
        recs = sorted(g.records, key=lambda r: (r.send_time or r.created_at, r.id))
        last_seen: dict[tuple[str, str], datetime] = {}
        for r in recs:
            stats["raw"] += 1
            if r.id in seen_ids:
                stats["duplicate"] += 1
                continue
            seen_ids.add(r.id)
            content = (r.content or "").strip()
            t = _parse_time(r.send_time or r.created_at)

            key = (r.sender_number, content)
            prev = last_seen.get(key)
            last_seen[key] = t
            if prev is not None and t - prev <= dedup_window and len(content) > 2:
                # 长内容在短时间内重复 → OCR 重复采集；"1"/"好的"等短内容交给噪声规则
                stats["duplicate"] += 1
                continue

            if is_noise(content, noise_set):
                stats["noise"] += 1
                continue

            image_urls = RE_IMAGE_URL.findall(content)
            is_image = content == "[图片]" or (bool(image_urls) and len(RE_IMAGE_URL.sub("", content).strip()) < 5)
            if is_image:
                stats["image"] += 1

            text = mask(content) if do_mask else content
            out.append(
                Message(
                    id=r.id,
                    group_number=g.group_number,
                    group_name=g.group_name,
                    customer_uid=uid,
                    sender_id=r.sender_number,
                    sender=r.display_name or r.nick_name or r.sender_number,
                    role="内部" if r.is_inner else "客户",
                    send_time=t,
                    content=text,
                    is_image=is_image,
                    entities=extract_entities(content),
                    hints=keyword_hints(content, types),
                )
            )
            stats["kept"] += 1
    return out, stats
