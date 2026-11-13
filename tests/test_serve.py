"""看板服务：页面、数据接口、修改与恢复。"""

import json
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from aftersales_events.pipeline import run
from aftersales_events.serve import BoardApp, make_server

from test_pipeline import ROOT, FakeExtractor


@pytest.fixture
def server(raw, settings, tmp_path):
    run(raw, settings, FakeExtractor(), out_dir=tmp_path / "out" / "run1")
    (tmp_path / "raw").mkdir()
    shutil.copy(ROOT / "tests" / "fixtures" / "sample_response.json", tmp_path / "raw" / "0_2026-11-11_2026-11-11.json")
    (tmp_path / "sync_status.json").write_text(json.dumps({"llm": {"provider": "deepseek", "model": "deepseek-v4-pro"},
                                                         "available_models": ["deepseek-v4-pro", "deepseek-v4.1-flash"]}))
    app = BoardApp(settings, tmp_path / "out", tmp_path / "raw", tmp_path / "state.json",
                   sync_status=tmp_path / "sync_status.json", runtime_path=tmp_path / "runtime.json")
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app, tmp_path
    srv.shutdown()
    srv.server_close()


def req(url, body=None, ctype="application/json", user=None):
    headers = {"Content-Type": ctype} if body is not None else {}
    if user:
        headers["X-Remote-User"] = user
    r = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers,
                               method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_page_and_data(server):
    base, _, _ = server
    code, html = req(base + "/")
    assert code == 200 and 'id="aes-data"' in html
    code, body = req(base + "/api/data", user="alice")
    data = json.loads(body)
    assert data["api"] is True and data["user"] == "alice"
    assert len(data["events"]) == 4 and data["messages"]
    groups = {g["id"]: g for g in data["groups"]}
    assert groups["R:1001"]["messages"] == 7 and groups["R:1001"]["inner"] == 3 and groups["R:1001"]["senders"] == 3
    assert groups["R:1002"]["name"] == "示例客户&支持团队-1380000002" and groups["R:1002"]["uid"] == "1380000002"
    assert data["sync"]["llm"]["model"] == "deepseek-v4-pro"
    assert data["model_settings"]["in_use"] == "deepseek-v4-pro" and data["model_settings"]["selected"] is None
    assert req(base + "/healthz") == (200, "ok")
    assert req(base + "/nope")[0] == 404


def test_edit_history_and_revert(server):
    base, app, tmp = server
    eid = json.loads(req(base + "/api/data")[1])["events"][0]["event_id"]
    code, body = req(f"{base}/api/events/{eid}", {"status": "已关闭", "owner": " 小李 "}, user="alice")
    res = json.loads(body)
    assert code == 200 and res["edits"]["status"] == "已关闭" and res["edits"]["owner"] == "小李"
    assert {h["field"] for h in res["history"]} == {"status", "owner"} and res["history"][0]["by"] == "alice"
    # 修改持久化到文件，并影响数据版本
    saved = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
    assert saved["edits"][eid]["status"] == "已关闭"
    v1 = json.loads(req(base + "/api/data")[1])["version"]
    # 改回原值后，该字段不再算作修改
    orig = next(e for e in app.events() if e.event_id == eid)
    res = json.loads(req(f"{base}/api/events/{eid}", {"status": orig.status})[1])
    assert "status" not in res["edits"] and len(res["history"]) == 3
    res = json.loads(req(f"{base}/api/events/{eid}/revert", {})[1])
    assert res["edits"] == {} and res["history"][-1]["field"] == "*"
    assert json.loads(req(base + "/api/data")[1])["version"] != v1


def test_rejects_bad_requests(server):
    base, _, _ = server
    eid = json.loads(req(base + "/api/data")[1])["events"][0]["event_id"]
    assert req(f"{base}/api/events/{eid}", {"status": "瞎写"})[0] == 400
    assert req(f"{base}/api/events/{eid}", {"title": "x"})[0] == 400
    assert req(f"{base}/api/events/{eid}", {"owner": "x" * 41})[0] == 400
    assert req(f"{base}/api/events/{eid}", {"status": "已关闭"}, ctype="text/plain")[0] == 415
    assert req(f"{base}/api/events/evt_missing", {"status": "已关闭"})[0] == 404


def test_model_settings(server):
    base, app, tmp = server
    code, body = req(base + "/api/settings", {"model": "deepseek-v4.1-flash"}, user="bob")
    res = json.loads(body)
    assert code == 200 and res["selected"] == "deepseek-v4.1-flash" and res["history"][-1]["by"] == "bob"
    assert json.loads((tmp / "runtime.json").read_text())["llm"]["model"] == "deepseek-v4.1-flash"
    assert req(base + "/api/settings", {"model": "gpt-x"})[0] == 400          # 网关上没有
    assert req(base + "/api/settings", {"model": "bad name;rm"})[0] == 400    # 格式不对
    assert req(base + "/api/settings", {"nope": 1})[0] == 400
    assert req(base + "/api/settings", {"model": "x"}, ctype="text/plain")[0] == 415
    res = json.loads(req(base + "/api/settings", {"model": None})[1])
    assert res["selected"] is None and len(res["history"]) == 2
    assert json.loads(req(base + "/api/settings")[1])["provider"] == "deepseek"


def test_group_messages_from_stored_raw(server):
    base, _, _ = server
    code, body = req(base + "/api/groups/" + urllib.parse.quote("R:1001", safe="") + "/messages?date=2026-11-11")
    d = json.loads(body)
    assert code == 200 and d["source"] == "stored" and d["group"]["id"] == "R:1001"
    # 全部消息（包括“收到”这类噪声和 OCR 重复），按 id 去重
    assert [m["id"] for m in d["messages"]] == ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
    m7 = next(m for m in d["messages"] if m["id"] == "7")
    assert "13812345678" not in m7["content"] and "<PHONE>" in m7["content"] and m7["sender"] == "销售小李"
    assert next(m for m in d["messages"] if m["id"] == "5")["image"] is True
    assert d["evidence"]["1"][0]["type"] == "故障"
    assert req(base + "/api/groups/R%3A1001/messages?date=bad")[0] == 400
    assert req(base + "/api/groups/R%3A1001/messages?date=2020-01-01")[0] == 404


def test_group_messages_via_relay_not_saved(raw, settings, tmp_path):
    from aftersales_events.relay import make_relay

    calls = []

    def fake_request(uid, start, end, cfg):
        calls.append((uid, start, end))
        return json.loads((ROOT / "tests" / "fixtures" / "sample_response.json").read_text(encoding="utf-8"))

    relay = make_relay({}, "127.0.0.1", 0, request_fn=fake_request)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    rbase = f"http://127.0.0.1:{relay.server_address[1]}"
    assert req(rbase + "/chat?uid=0&date=nope")[0] == 400
    assert req(rbase + "/chat?uid=abc&date=2026-11-11")[0] == 400

    (tmp_path / "out").mkdir()
    app = BoardApp(settings, tmp_path / "out", None, tmp_path / "state.json", relay_url=rbase)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        d = json.loads(req(base + "/api/groups/R%3A1002/messages?date=2026-11-11")[1])
        assert d["source"] == "live" and d["total"] == 3 and calls == [("0", "2026-11-11", "2026-11-11")]
        req(base + "/api/groups/R%3A1001/messages?date=2026-11-11")
        assert len(calls) == 1  # 同一天走内存缓存
        assert not list(tmp_path.rglob("*.json"))  # 不落盘
    finally:
        srv.shutdown(); srv.server_close(); relay.shutdown(); relay.server_close()
