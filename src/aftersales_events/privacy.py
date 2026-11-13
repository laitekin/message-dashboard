"""人名脱敏：送给模型之前把人名换成代号，模型返回后再把代号换回真名。

- 名单来自原始数据里所有发送人的 display_name / nick_name，并按 is_inner 区分内部和客户。
- 每个窗口单独编号：发送人按出现顺序编为「员工A」「客户A」……；正文里提到的名单内人名
  用同一套代号；名单外的 @提及 编为「人员A」。
- 群名里通常有客户公司名，送给模型时换成「客户群 <UID>」。
- 模型输出的标题、摘要、跟进人、属性值中的代号会被换回真名，证据编号不受影响。

局限：正文里出现、但既不是群成员昵称也没有 @ 的人名（例如“找一下张三”）识别不到。
"""

from __future__ import annotations

import re
from typing import Any

from .models import ExtractedEvent, RawResponse, Window, WindowExtraction

PREFIX = {"内部": "员工", "客户": "客户", "其他": "人员"}
MIN_NAME_LEN = 2  # 单字昵称只在发送人字段替换，不在正文里替换，避免误伤


def build_directory(*raws: RawResponse) -> dict[str, str]:
    """人名 → 角色（内部 / 客户）。同一个名字两种角色都出现时按内部处理。"""
    names: dict[str, str] = {}
    for raw in raws:
        for g in raw.groups:
            for r in g.records:
                role = "内部" if r.is_inner else "客户"
                for n in (r.display_name, r.nick_name):
                    n = (n or "").strip()
                    if n and names.get(n) != "内部":
                        names[n] = role
    return names


def _letters(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _pattern(words) -> re.Pattern | None:
    words = sorted({w for w in words if w}, key=len, reverse=True)
    return re.compile("|".join(map(re.escape, words))) if words else None


class Pseudonymizer:
    def __init__(self, window: Window, directory: dict[str, str], mask_group_name: bool = True):
        self.window = window
        self.mask_group_name = mask_group_name
        self.fwd: dict[str, str] = {}
        self._counters = {k: 0 for k in PREFIX}

        for m in window.messages:
            self._alias(m.sender, m.role)
        text = "\n".join(m.content for m in window.messages)
        for name, role in sorted(directory.items(), key=lambda kv: -len(kv[0])):
            if len(name) >= MIN_NAME_LEN and name in text:
                self._alias(name, role)
        known = [k for k in self.fwd if len(k) >= MIN_NAME_LEN]
        for m in window.messages:
            for n in m.entities.get("mentions", []):
                if not any(n.startswith(k) for k in known):
                    self._alias(n, "其他")

        self.rev = {v: k for k, v in self.fwd.items()}
        self._fwd_re = _pattern(k for k in self.fwd if len(k) >= MIN_NAME_LEN)
        self._rev_re = _pattern(self.rev)

    def _alias(self, name: str, role: str) -> str:
        name = (name or "").strip()
        if not name:
            return name
        if name not in self.fwd:
            role = role if role in PREFIX else "其他"
            self.fwd[name] = PREFIX[role] + _letters(self._counters[role])
            self._counters[role] += 1
        return self.fwd[name]

    def mask_text(self, s: str) -> str:
        return self._fwd_re.sub(lambda m: self.fwd[m.group(0)], s) if (s and self._fwd_re) else s

    def restore_text(self, s: str) -> str:
        return self._rev_re.sub(lambda m: self.rev[m.group(0)], s) if (s and self._rev_re) else s

    def masked_window(self) -> Window:
        w = self.window
        msgs = [
            m.model_copy(update=dict(
                sender=self.fwd.get(m.sender.strip(), m.sender),
                sender_id="",
                content=self.mask_text(m.content),
                group_name="",
                entities={k: [self.mask_text(v) for v in vs] for k, vs in m.entities.items()},
            ))
            for m in w.messages
        ]
        group_name = (f"客户群 {w.customer_uid}" if w.customer_uid else "客户群") if self.mask_group_name else self.mask_text(w.group_name)
        return w.model_copy(update=dict(messages=msgs, group_name=group_name))

    def restore(self, ext: WindowExtraction) -> WindowExtraction:
        def fix(e: ExtractedEvent) -> ExtractedEvent:
            owner = e.owner
            if owner:
                owner = self.rev.get(owner.strip(), self.restore_text(owner))
            return e.model_copy(update=dict(
                title=self.restore_text(e.title),
                summary=self.restore_text(e.summary),
                owner=owner,
                attributes=[a.model_copy(update=dict(value=self.restore_text(a.value))) for a in e.attributes],
            ))

        return WindowExtraction(events=[fix(e) for e in ext.events])


class PseudonymizingExtractor:
    """包装任意抽取器：送出去的是脱敏后的窗口，拿回来的结果换回真名。"""

    def __init__(self, inner: Any, directory: dict[str, str], cfg: dict[str, Any] | None = None):
        self.inner = inner
        self.directory = directory
        self.mask_group_name = bool((cfg or {}).get("mask_group_names", True))

    def extract(self, window: Window) -> WindowExtraction:
        p = Pseudonymizer(window, self.directory, self.mask_group_name)
        return p.restore(self.inner.extract(p.masked_window()))


def wrap(extractor: Any, directory: dict[str, str], cfg: dict[str, Any]) -> Any:
    return PseudonymizingExtractor(extractor, directory, cfg) if cfg.get("pseudonymize_names", True) else extractor
