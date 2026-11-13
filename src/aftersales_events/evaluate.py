"""评估：预测事件 vs 人工标注事件。

匹配规则：同群 + 同类型 + 证据消息有交集 → 视为同一事件（一对一贪心匹配）。
输出每类的 precision / recall / F1，以及匹配上的事件的字段准确率（status、level）。

标注文件为 JSONL，每行至少包含：group_number, type, evidence_msg_ids；可选 status, level, title。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _overlap(a: dict, b: dict) -> int:
    return len(set(a["evidence_msg_ids"]) & set(b["evidence_msg_ids"]))


def evaluate(pred: list[dict], gold: list[dict]) -> dict[str, Any]:
    used: set[int] = set()
    matches: list[tuple[dict, dict]] = []
    for g in gold:
        best, best_ov = None, 0
        for j, p in enumerate(pred):
            if j in used or p["type"] != g["type"] or p["group_number"] != g["group_number"]:
                continue
            ov = _overlap(p, g)
            if ov > best_ov:
                best, best_ov = j, ov
        if best is not None:
            used.add(best)
            matches.append((pred[best], g))

    types = sorted({x["type"] for x in pred} | {x["type"] for x in gold})
    per_type: dict[str, dict[str, float]] = {}
    tp_by = defaultdict(int)
    for _, g in matches:
        tp_by[g["type"]] += 1
    for t in types + ["__all__"]:
        tp = sum(tp_by.values()) if t == "__all__" else tp_by[t]
        np_ = len(pred) if t == "__all__" else sum(1 for x in pred if x["type"] == t)
        ng = len(gold) if t == "__all__" else sum(1 for x in gold if x["type"] == t)
        p = tp / np_ if np_ else 0.0
        r = tp / ng if ng else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        per_type[t] = {"tp": tp, "pred": np_, "gold": ng, "precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3)}

    field_acc = {}
    for fld in ("status", "level"):
        pairs = [(p.get(fld), g.get(fld)) for p, g in matches if g.get(fld)]
        field_acc[fld] = round(sum(a == b for a, b in pairs) / len(pairs), 3) if pairs else None

    return {"per_type": per_type, "field_accuracy": field_acc, "matched": len(matches)}


def format_report(res: dict[str, Any]) -> str:
    lines = [f"{'类型':<8}{'TP':>5}{'预测':>6}{'标注':>6}{'P':>8}{'R':>8}{'F1':>8}"]
    for t, m in res["per_type"].items():
        name = "合计" if t == "__all__" else t
        lines.append(f"{name:<8}{m['tp']:>5}{m['pred']:>6}{m['gold']:>6}{m['precision']:>8}{m['recall']:>8}{m['f1']:>8}")
    lines.append(f"字段准确率（匹配上的事件）：{res['field_accuracy']}")
    return "\n".join(lines)
