"""加载 config/settings.yaml 与 config/event_types.yaml。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path(os.environ.get("AES_CONFIG_DIR", "config"))


@dataclass
class EventTypeDef:
    name: str
    definition: str
    enabled: bool = True
    push_to: str = ""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)


@dataclass
class Settings:
    raw: dict[str, Any]
    event_types: list[EventTypeDef]

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {}) or {}

    @property
    def enabled_types(self) -> list[EventTypeDef]:
        return [t for t in self.event_types if t.enabled]


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(config_dir: str | Path | None = None) -> Settings:
    d = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    raw = _read_yaml(d / "settings.yaml")
    types_raw = _read_yaml(d / "event_types.yaml").get("types", [])
    types = []
    for t in types_raw:
        # YAML 会把 500 / 502 等关键词解析成数字，统一转成字符串
        t["keywords"] = [str(k) for k in t.get("keywords", [])]
        t["attributes"] = {str(k): str(v) for k, v in (t.get("attributes") or {}).items()}
        types.append(EventTypeDef(**t))
    pre = raw.setdefault("preprocess", {})
    pre["noise_messages"] = [str(x) for x in pre.get("noise_messages", [])]

    # 环境变量覆盖
    llm = raw.setdefault("llm", {})
    if os.environ.get("AES_MODEL"):
        llm["model"] = os.environ["AES_MODEL"]
    if os.environ.get("AES_EFFORT"):
        llm["effort"] = os.environ["AES_EFFORT"]
    fetch = raw.setdefault("fetch", {})
    if os.environ.get("AES_FETCH_MODE"):
        fetch["mode"] = os.environ["AES_FETCH_MODE"]
    if os.environ.get("AES_FETCH_URL"):
        fetch["url"] = os.environ["AES_FETCH_URL"]
    return Settings(raw=raw, event_types=types)
