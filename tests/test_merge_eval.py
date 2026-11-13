from datetime import datetime, timedelta

from aftersales_events.evaluate import evaluate
from aftersales_events.merge import merge_events
from aftersales_events.models import Event

T0 = datetime(2026, 11, 11, 10, 0)


def ev(eid, type_, title, ids, start_h=0, end_h=0, status="跟进中", level="P2", group="G"):
    return Event(
        event_id=eid, type=type_, title=title, summary=title, status=status, level=level, owner=None,
        customer_uid="1", group_number=group, group_name="g", start_time=T0 + timedelta(hours=start_h),
        end_time=T0 + timedelta(hours=end_h), evidence_msg_ids=ids, confidence=0.8, source_windows=[eid],
    )


CFG = {"window_hours": 72, "title_similarity": 0.55}


def test_merge_by_evidence_overlap():
    out = merge_events([ev("a", "故障", "渠道异常", ["1", "2"]), ev("b", "故障", "完全不同标题", ["2", "3"], 1, 2, status="已解决", level="P1")], CFG)
    assert len(out) == 1
    m = out[0]
    assert m.evidence_msg_ids == ["1", "2", "3"]
    assert m.status == "已解决" and m.level == "P1"


def test_merge_by_title_and_time():
    out = merge_events([ev("a", "故障", "opus 渠道异常", ["1"]), ev("b", "故障", "opus 渠道异常需切换", ["9"], 24, 25)], CFG)
    assert len(out) == 1


def test_no_merge_different_type_or_group_or_far():
    evs = [
        ev("a", "故障", "opus 渠道异常", ["1"]),
        ev("b", "商机", "opus 渠道异常", ["1"]),
        ev("c", "故障", "opus 渠道异常", ["1"], group="H"),
        ev("d", "故障", "opus 渠道异常", ["5"], 200, 201),
    ]
    assert len(merge_events(evs, CFG)) == 4


def test_evaluate_partial():
    pred = [
        {"group_number": "G", "type": "故障", "evidence_msg_ids": ["1", "2"], "status": "已解决"},
        {"group_number": "G", "type": "商机", "evidence_msg_ids": ["5"]},
    ]
    gold = [
        {"group_number": "G", "type": "故障", "evidence_msg_ids": ["2"], "status": "跟进中"},
        {"group_number": "G", "type": "资源需求", "evidence_msg_ids": ["7"]},
    ]
    r = evaluate(pred, gold)
    assert r["per_type"]["__all__"]["precision"] == 0.5
    assert r["per_type"]["__all__"]["recall"] == 0.5
    assert r["per_type"]["故障"]["f1"] == 1.0
    assert r["field_accuracy"]["status"] == 0.0
