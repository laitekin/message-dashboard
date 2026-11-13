"""看板：读取事件、内嵌数据、CLI。"""

import json
import re

from aftersales_events.board import build_payload, load_events, render_board
from aftersales_events.cli import main
from aftersales_events.pipeline import run

from test_pipeline import ROOT, FakeExtractor


def _payload(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="aes-data">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_run_writes_board_with_evidence(raw, settings, tmp_path):
    res = run(raw, settings, FakeExtractor(), out_dir=tmp_path)
    data = _payload((tmp_path / "board.html").read_text(encoding="utf-8"))
    assert len(data["events"]) == len(res.events)
    assert [t["name"] for t in data["types"]] == [t.name for t in settings.enabled_types]
    # 只内嵌被引用的消息，且内容来自预处理（已脱敏）
    cited = {i for e in res.events for i in e.evidence_msg_ids}
    assert set(data["messages"]) == cited
    assert "502" in data["messages"]["1"]["content"]


def test_load_events_dir_dedups_by_id(raw, settings, tmp_path):
    run(raw, settings, FakeExtractor(), out_dir=tmp_path / "a")
    run(raw, settings, FakeExtractor(), out_dir=tmp_path / "b")
    events = load_events([tmp_path])
    assert len(events) == 4


def test_unknown_type_column_and_script_escape(raw, settings, tmp_path):
    res = run(raw, settings, FakeExtractor(), out_dir=tmp_path)
    e = res.events[0].model_copy(update={"type": "已停用类型", "title": "</script><b>x"})
    html = render_board([e], settings.enabled_types, title="<看板>")
    assert "</script><b>" not in html
    assert "<title>&lt;看板&gt;</title>" in html
    data = _payload(html)
    assert data["events"][0]["title"] == "</script><b>x"
    assert data["types"][-1]["name"] == "已停用类型"
    assert data["messages"] == {}


def test_board_id_stable(raw, settings, tmp_path):
    res = run(raw, settings, FakeExtractor())
    a = build_payload(res.events)["board_id"]
    b = build_payload(list(reversed(res.events)))["board_id"]
    assert a == b


def test_cli_board(raw, settings, tmp_path, capsys):
    run(raw, settings, FakeExtractor(), out_dir=tmp_path / "run1")
    out = tmp_path / "board.html"
    main(["--config", str(ROOT / "config"), "board", str(tmp_path / "run1"), "--out", str(out),
          "--input", str(ROOT / "tests" / "fixtures" / "sample_response.json"), "--title", "测试看板"])
    data = _payload(out.read_text(encoding="utf-8"))
    assert data["title"] == "测试看板" and len(data["events"]) == 4 and data["messages"]
    assert "4 个事件" in capsys.readouterr().out
