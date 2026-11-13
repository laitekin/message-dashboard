"""按 UID 处理：关注列表（自动）、手动任务队列、多数据源 sync、看板接口。不调用真实模型和数据源。"""

import json
import threading
from datetime import datetime, timedelta

import pytest

from aftersales_events.fetch import load_raw
from aftersales_events.jobs import JobQueue, set_watch, watch_uids
from aftersales_events.sync import Sync, run_jobs

from test_pipeline import ROOT
from test_privacy_sync import CountingExtractor

FIXTURE = ROOT / "tests" / "fixtures" / "sample_response.json"
UID = "1380000002"


def split_fetch(tmp_path, calls):
    """uid 0 只返回 R:1001；按 UID 1380000002 才返回 R:1002（和真实数据源一样：带 UID 的群不在「全部群」里）。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def fetch(uid, start, end, cfg):
        calls.append((uid, start))
        groups = [g for g in data["groups"] if (g["group_number"] == "R:1002") == (uid == UID)] if start == "2026-11-11" else []
        dst = tmp_path / "data" / "raw" / f"{uid}_{start}_{end}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps({"code": 0, "uid": uid, "start": start, "end": end, "total": sum(len(g["records"]) for g in groups),
                                   "groups": groups}, ensure_ascii=False), encoding="utf-8")
        return load_raw(dst), dst

    return fetch


def make(settings, tmp_path, monkeypatch, ex, calls):
    monkeypatch.chdir(tmp_path)
    return Sync(settings, extractor=ex, fetch_fn=split_fetch(tmp_path, calls), now=lambda: datetime(2026, 11, 12, 9, 0),
                runtime_path=tmp_path / "data" / "runtime_settings.json")


def events_out(tmp_path):
    return [json.loads(l) for l in (tmp_path / "out" / "live" / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_job_queue(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a = q.add(UID, 2, "张三")
    assert q.flag.exists() and a["state"] == "pending"
    b = q.add(UID, 7, "李四")  # 同一个 UID 还在排队：合并，范围取更长的
    assert b["id"] == a["id"] and b["days"] == 7 and len(q.snapshot()) == 1
    for bad in [("0", 2), ("abc", 2), (UID, 0), (UID, 8)]:
        with pytest.raises(ValueError):
            q.add(*bad, "x")
    q.clear_flag()
    assert not q.flag.exists()
    got = q.claim()
    assert [j["id"] for j in got] == [a["id"]] and q.claim() == []  # 处理中的不会被重复取走
    q.finish(a["id"], {"events": 1})
    assert q.snapshot()[0]["state"] == "done" and q.snapshot()[0]["result"] == {"events": 1}
    q.add("1380000003", 1, "x")
    assert q.snapshot()[0]["state"] == "pending"  # 进行中的排在前面
    # 处理程序中途退出：长时间停在「处理中」的任务重新取出
    j = q.claim()[0]
    raw = json.loads(q.path.read_text())
    raw["jobs"][0]["started_at"] = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
    q.path.write_text(json.dumps(raw))
    assert [x["id"] for x in q.claim()] == [j["id"]]


def test_watch_list(tmp_path):
    rt = tmp_path / "runtime.json"
    rt.write_text(json.dumps({"llm": {"model": "m1"}}))
    lst, changed = set_watch(rt, UID, True, "张三")
    assert changed and [w["uid"] for w in lst] == [UID] and lst[0]["by"] == "张三"
    assert set_watch(rt, UID, True, "李四")[1] is False
    data = json.loads(rt.read_text())
    assert data["llm"] == {"model": "m1"} and data["history"][-1]["field"] == "watch"  # 不影响模型设置
    assert watch_uids(rt) == [UID]
    set_watch(rt, UID, False, "张三")
    assert watch_uids(rt) == []
    with pytest.raises(ValueError):
        set_watch(rt, "12", True, "x")


def test_manual_job_then_watch(settings, tmp_path, monkeypatch):
    calls = []
    # 1. 定时处理只覆盖「全部群」：R:1002 的事件出不来
    st = make(settings, tmp_path, monkeypatch, CountingExtractor(), calls).run(uid="0")
    assert {e["group_number"] for e in events_out(tmp_path)} == {"R:1001"}
    assert st["watch_uids"] == [] and set(st["sources"]) == {"0"}

    # 2. 手动处理这个 UID
    q = JobQueue(tmp_path / "data" / "sync_requests.json")
    job = q.add(UID, 2, "张三")
    calls.clear()
    ex = CountingExtractor()
    res = run_jobs(settings, q.path, tmp_path / "data" / "sync-now.lock", sync=make(settings, tmp_path, monkeypatch, ex, calls))
    assert calls == [(UID, "2026-11-12"), (UID, "2026-11-11")]
    assert ex.calls == 1 and not q.flag.exists()
    done = q.snapshot()[0]
    assert done["id"] == job["id"] and done["state"] == "done"
    assert done["result"]["groups"] == 1 and done["result"]["events"] == 1 and done["result"]["extracted"] == 1
    evs = events_out(tmp_path)
    assert {e["group_number"] for e in evs} == {"R:1001", "R:1002"} and len(evs) == 4
    index = json.loads((tmp_path / "data" / "windows" / "index.json").read_text())
    assert f"{UID}|2026-11-11" in index and "2026-11-11" in index
    assert res["jobs"][0]["events"] == 1

    # 3. 下一次定时处理不会把手动处理的结果冲掉
    make(settings, tmp_path, monkeypatch, CountingExtractor(), calls).run(uid="0")
    assert len(events_out(tmp_path)) == 4

    # 4. 加入关注列表后，定时处理也拉这个 UID；内容没变，命中缓存不再调用模型
    set_watch(tmp_path / "data" / "runtime_settings.json", UID, True, "张三")
    calls.clear()
    ex = CountingExtractor()
    st = make(settings, tmp_path, monkeypatch, ex, calls).run(uid="0")
    assert (UID, "2026-11-11") in calls and ex.calls == 0
    assert st["watch_uids"] == [UID] and st["sources"][UID]["events"] == 1 and st["events"] == 4
    status = json.loads((tmp_path / "data" / "sync_status.json").read_text())
    assert status["watch_uids"] == [UID]

    # 5. 队列为空时什么也不做
    assert run_jobs(settings, q.path, tmp_path / "data" / "sync-now.lock", sync=make(settings, tmp_path, monkeypatch, ex, calls)) == {"jobs": []}


def test_job_fetch_failure_is_reported(settings, tmp_path, monkeypatch):
    from aftersales_events.fetch import FetchError

    monkeypatch.chdir(tmp_path)

    def broken(uid, start, end, cfg):
        raise FetchError("HTTP 502")

    q = JobQueue(tmp_path / "data" / "sync_requests.json")
    q.add(UID, 1, "张三")
    sync = Sync(settings, extractor=CountingExtractor(), fetch_fn=broken, now=lambda: datetime(2026, 11, 12, 9, 0))
    run_jobs(settings, q.path, tmp_path / "data" / "sync-now.lock", sync=sync)
    j = q.snapshot()[0]
    assert j["state"] == "failed" and "HTTP 502" in j["error"]


def test_board_watch_and_jobs_api(settings, tmp_path):
    import shutil

    from aftersales_events.serve import BoardApp, make_server
    from test_serve import req

    (tmp_path / "raw").mkdir()
    (tmp_path / "out").mkdir()
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    (tmp_path / "raw" / "0_2026-11-11_2026-11-11.json").write_text(json.dumps({**data, "groups": data["groups"][:1]}, ensure_ascii=False))
    (tmp_path / "raw" / f"{UID}_2026-11-11_2026-11-11.json").write_text(json.dumps({**data, "groups": data["groups"][1:]}, ensure_ascii=False))
    shutil.copy(tmp_path / "raw" / f"{UID}_2026-11-11_2026-11-11.json", tmp_path / "raw" / "1380000003_2026-11-11_2026-11-11.json")
    app = BoardApp(settings, tmp_path / "out", tmp_path / "raw", tmp_path / "state.json",
                   runtime_path=tmp_path / "runtime.json", queue_path=tmp_path / "q.json")
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        p = json.loads(req(base + "/api/data")[1])
        assert p["jobs"]["enabled"] and p["jobs"]["watch"] == [] and p["jobs"]["jobs"] == []
        groups = {g["id"]: g for g in p["groups"]}
        assert "src_uid" not in groups["R:1001"] and groups["R:1002"]["src_uid"] in (UID, "1380000003")
        assert groups["R:1002"]["messages"] == 2  # 两个 UID 共有的群，消息去重（不去重会是 4）

        st, body = req(base + "/api/watch", {"uid": UID, "watch": True}, user="zhangsan")
        body = json.loads(body)
        assert st == 200 and body["watch"][0]["uid"] == UID and body["watch"][0]["by"] == "zhangsan"
        assert body["jobs"][0]["uid"] == UID and body["jobs"][0]["days"] == 7 and body["jobs"][0]["reason"] == "watch"
        assert (tmp_path / "q.pending").exists()
        assert json.loads(req(base + "/api/settings")[1])["history"] == []  # 关注列表的修改不混进模型修改记录

        st, body = req(base + "/api/jobs", {"uid": "1380000003", "days": 2}, user="lisi")
        assert st == 200 and json.loads(body)["job"]["by"] == "lisi"
        for bad in [{"uid": "1", "days": 2}, {"uid": UID, "days": 30}, {"uid": UID, "days": "2"}, {"uid": UID, "days": True}, []]:
            assert req(base + "/api/jobs", bad)[0] == 400
        assert req(base + "/api/watch", {"uid": UID, "watch": "yes"})[0] == 400
        assert req(base + "/api/jobs", {"uid": UID, "days": 2}, ctype="text/plain")[0] == 415

        st, body = req(base + "/api/watch", {"uid": UID, "watch": False}, user="zhangsan")
        assert json.loads(body)["watch"] == []
        assert len(json.loads(req(base + "/api/data")[1])["jobs"]["jobs"]) == 2  # 取消关注不会再加任务
    finally:
        srv.shutdown()
        srv.server_close()
