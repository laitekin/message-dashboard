"""用假的抽取器跑通整条流水线（不调用模型）。"""

import json
from pathlib import Path

from aftersales_events.evaluate import evaluate, load_jsonl
from aftersales_events.extract import DryRunExtractor, build_system_prompt, render_window
from aftersales_events.models import Attribute, ExtractedEvent, Window, WindowExtraction
from aftersales_events.pipeline import run

ROOT = Path(__file__).resolve().parent.parent


def _ev(type_, title, evidence, status="跟进中", level="P2", **attrs):
    return ExtractedEvent(
        type=type_, title=title, summary=title, status=status, level=level, owner="支持小王",
        evidence=evidence, attributes=[Attribute(key=k, value=v) for k, v in attrs.items()], confidence=0.9,
    )


class FakeExtractor:
    """按消息内容给出固定答案，模拟模型输出（含无效证据、未知类型等异常情况）。"""

    def extract(self, w: Window) -> WindowExtraction:
        text, idmap = render_window(w)
        by_id = {m.id: f"m{i + 1}" for i, m in enumerate(w.messages)}
        evs = []
        if "1" in by_id:
            evs.append(_ev("故障", "opus 502 渠道切换", [by_id["1"], by_id.get("4", "m99")], status="已解决", level="P1",
                           channel="newapi-ss-demo-01"))
            evs.append(_ev("不存在的类型", "x", [by_id["1"]]))
            evs.append(_ev("商机", "无证据", ["m99"]))
        if "6" in by_id:
            evs.append(_ev("资源需求", "opus 提额 500万 TPM", [by_id["6"], by_id["7"]], requested_value="500万 TPM"))
        if "8" in by_id:
            evs.append(_ev("商务需求", "续约折扣", [by_id["8"], by_id["9"]]))
        if "20" in by_id:
            evs.append(_ev("功能需求", "按模型导出用量", [by_id["20"], by_id["22"]], level="P3"))
        return WindowExtraction(events=evs)


def test_system_prompt_contains_types(settings):
    sp = build_system_prompt(settings.enabled_types)
    for t in settings.enabled_types:
        assert f"### {t.name}" in sp
    assert "{event_types}" not in sp


def test_render_window_uses_short_ids(raw, settings):
    from aftersales_events.preprocess import preprocess
    from aftersales_events.segment import segment

    msgs, _ = preprocess(raw, settings.enabled_types, settings.section("preprocess"))
    w = segment(msgs, settings.section("segment"))[0]
    text, idmap = render_window(w)
    assert "[m1]" in text and idmap["m1"] == 0
    assert "<图片>" in text or all(not m.is_image for m in w.messages)


def test_pipeline_end_to_end(raw, settings, tmp_path):
    res = run(raw, settings, FakeExtractor(), out_dir=tmp_path)
    types = sorted(e.type for e in res.events)
    assert types == sorted(["故障", "资源需求", "商务需求", "功能需求"])
    fault = next(e for e in res.events if e.type == "故障")
    assert fault.evidence_msg_ids == ["1", "4"]
    assert fault.attributes["channel"] == "newapi-ss-demo-01"
    assert fault.start_time.hour == 10
    assert (tmp_path / "events.jsonl").exists() and (tmp_path / "report.md").exists()

    pred = load_jsonl(tmp_path / "events.jsonl")
    gold = load_jsonl(ROOT / "eval" / "gold" / "example.jsonl")
    r = evaluate(pred, gold)
    assert r["per_type"]["__all__"]["f1"] == 1.0
    assert r["field_accuracy"]["status"] == 1.0


def test_dry_run_writes_prompts(raw, settings, tmp_path):
    res = run(raw, settings, DryRunExtractor(settings.enabled_types, tmp_path / "prompts"))
    files = list((tmp_path / "prompts").glob("*.md"))
    assert len(files) == len(res.windows) + 1  # + system.md
    assert res.events == []
