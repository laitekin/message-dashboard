"""人名脱敏与增量同步。不调用真实模型和数据源。"""

import json
import shutil
from datetime import datetime

from aftersales_events.extract import render_window
from aftersales_events.fetch import load_raw
from aftersales_events.models import RawRecord, RawResponse, Window, WindowExtraction
from aftersales_events.preprocess import preprocess
from aftersales_events.privacy import Pseudonymizer, build_directory
from aftersales_events.segment import segment
from aftersales_events.sync import Sync

from test_pipeline import ROOT, FakeExtractor, _ev

REAL_NAMES = ["客户甲", "支持小王", "销售小李", "客户乙", "产品小赵", "测试科技", "示例客户"]
FIXTURE = ROOT / "tests" / "fixtures" / "sample_response.json"


def _windows(raw, settings):
    msgs, _ = preprocess(raw, settings.enabled_types, settings.section("preprocess"))
    return segment(msgs, settings.section("segment"))


def test_directory_roles(raw):
    d = build_directory(raw)
    assert d["支持小王"] == "内部" and d["客户甲"] == "客户" and d["产品小赵"] == "内部"


def test_masked_prompt_has_no_real_names(raw, settings):
    directory = build_directory(raw)
    for w in _windows(raw, settings):
        p = Pseudonymizer(w, directory)
        text, idmap = render_window(p.masked_window())
        assert not [n for n in REAL_NAMES if n in text], text
        assert "客户群" in text and ("员工A" in text or "客户A" in text)
        # 编号和时间不变，证据映射照常
        assert idmap == render_window(w)[1]


def test_mentions_and_content_names(raw, settings):
    w = _windows(raw, settings)[0]
    m0 = w.messages[0]
    msgs = [m0.model_copy(update=dict(content="@销售小李 帮忙找下支持小王，还有 @路人丙 看看", entities={"mentions": ["销售小李", "路人丙"]}))]
    w2 = w.model_copy(update=dict(messages=msgs))
    p = Pseudonymizer(w2, build_directory(raw))
    text, _ = render_window(p.masked_window())
    assert "销售小李" not in text and "支持小王" not in text and "路人丙" not in text
    assert "@员工" in text and "@人员A" in text


def test_restore_maps_aliases_back(raw, settings):
    w = _windows(raw, settings)[0]
    p = Pseudonymizer(w, build_directory(raw))
    alias_in, alias_cus = p.fwd["支持小王"], p.fwd["客户甲"]
    ext = WindowExtraction(events=[_ev("故障", f"{alias_cus} 报 502", ["m1"], channel=f"{alias_in} 已切")])
    ext.events[0].owner = alias_in
    ext.events[0].summary = f"{alias_cus}反馈 502，{alias_in}已处理"
    out = p.restore(ext).events[0]
    assert out.owner == "支持小王" and out.title == "客户甲 报 502"
    assert out.summary == "客户甲反馈 502，支持小王已处理" and out.attributes[0].value == "支持小王 已切"


def test_alias_letters_do_not_collide():
    t = datetime(2026, 11, 11, 10, 0)
    recs = [RawRecord(id=str(i), group_number="G", group_name="g-1380000009", sender_number=f"s{i}",
                      display_name=f"用户{i:02d}", content=f"消息{i}", send_time=f"2026-11-11 10:{i:02d}:00", is_inner=0)
            for i in range(30)]
    raw = RawResponse(groups=[{"group_number": "G", "group_name": "g-1380000009", "records": recs}])
    from aftersales_events.config import load_settings
    s = load_settings(ROOT / "config")
    w = _windows(raw, s)[0]
    p = Pseudonymizer(w, build_directory(raw))
    aliases = list(p.fwd.values())
    assert len(set(aliases)) == 30 and "客户AA" in aliases and "客户A" in aliases
    assert p.restore_text("客户AA 和 客户A") == "用户26 和 用户00"


class CountingExtractor(FakeExtractor):
    def __init__(self):
        self.calls = 0
        self.seen = []

    def extract(self, w: Window):
        self.calls += 1
        self.seen.append(render_window(w)[0])
        return super().extract(w)


def make_sync(settings, tmp_path, monkeypatch, extractor, raw_path=FIXTURE):
    monkeypatch.chdir(tmp_path)

    def fake_fetch(uid, start, end, cfg):
        dst = tmp_path / "data" / "raw" / f"{uid}_{start}_{end}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if start == "2026-11-11":
            shutil.copy(raw_path, dst)
        else:  # 今天还没有消息
            dst.write_text(json.dumps({"code": 0, "uid": uid, "start": start, "end": end, "total": 0, "groups": []}))
        return load_raw(dst), dst

    return Sync(settings, extractor=extractor, fetch_fn=fake_fetch, now=lambda: datetime(2026, 11, 12, 9, 0))


def test_sync_incremental(settings, tmp_path, monkeypatch):
    ex = CountingExtractor()
    st = make_sync(settings, tmp_path, monkeypatch, ex).run(uid="0")
    assert st["ok"] and st["windows"] == 3 and st["extracted"] == 3 and st["cached"] == 0
    assert st["dates"] == ["2026-11-12", "2026-11-11"]
    out = tmp_path / "out" / "live" / "events.jsonl"
    events = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert sorted(e["type"] for e in events) == sorted(["故障", "资源需求", "商务需求", "功能需求"])
    # 送给模型的内容里没有真名
    assert not [n for n in REAL_NAMES for t in ex.seen if n in t]
    status = json.loads((tmp_path / "data" / "sync_status.json").read_text(encoding="utf-8"))
    assert status["events"] == 4 and status["groups_with_events"] == 2

    # 第二次运行：数据没变，全部命中缓存，不再调用模型
    ex2 = CountingExtractor()
    st2 = make_sync(settings, tmp_path, monkeypatch, ex2).run(uid="0")
    assert ex2.calls == 0 and st2["cached"] == 3 and st2["events"] == 4

    # 有新消息：只有变化的那个窗口重新抽取，结果替换旧版本
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["groups"][1]["records"].append({**data["groups"][1]["records"][-1], "id": "23", "content": "已经排进下个版本了",
                                         "send_time": "2026-11-11 09:20:00", "created_at": "2026-11-11 09:30:00"})
    newer = tmp_path / "newer.json"
    newer.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    ex3 = CountingExtractor()
    st3 = make_sync(settings, tmp_path, monkeypatch, ex3, raw_path=newer).run(uid="0")
    assert ex3.calls == 1 and st3["cached"] == 2 and st3["events"] == 4
    index = json.loads((tmp_path / "data" / "windows" / "index.json").read_text())
    assert len(index["2026-11-11"]) == 3


def test_sync_keeps_previous_version_when_extraction_fails(settings, tmp_path, monkeypatch):
    make_sync(settings, tmp_path, monkeypatch, CountingExtractor()).run(uid="0")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["groups"][1]["records"].append({**data["groups"][1]["records"][-1], "id": "23", "content": "补充一下",
                                         "send_time": "2026-11-11 09:20:00"})
    newer = tmp_path / "newer.json"
    newer.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    class Broken:
        def extract(self, w):
            raise RuntimeError("gateway down")

    st = make_sync(settings, tmp_path, monkeypatch, Broken(), raw_path=newer).run(uid="0")
    assert st["failed"] == 1 and not st["ok"] and st["events"] == 4  # 功能需求事件仍在


def test_sync_fetch_failure_uses_saved_file(settings, tmp_path, monkeypatch):
    make_sync(settings, tmp_path, monkeypatch, CountingExtractor()).run(uid="0")
    from aftersales_events.fetch import FetchError

    def failing(*a, **k):
        raise FetchError("HTTP 403")

    s = Sync(settings, extractor=CountingExtractor(), fetch_fn=failing, now=lambda: datetime(2026, 11, 12, 10, 0))
    st = s.run(uid="0")
    assert not st["ok"] and "403" in st["errors"][0] and st["events"] == 4
