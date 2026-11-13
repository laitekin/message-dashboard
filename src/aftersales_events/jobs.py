"""按 UID 处理事件：关注列表（自动）+ 手动处理队列。

带 UID 的客户群和对应的内部群不在 uid=0「全部群」的结果里，只有按 UID 请求数据源才会返回。

- 关注列表保存在运行时设置（data/runtime_settings.json 的 watch_uids），看板上可增删；
  每小时的 aes sync 除了「全部群」，也按这些 UID 各拉一次，走同一套抽取流程。
- 手动处理：看板把任务写进队列文件（data/sync_requests.json），并创建标记文件
  data/sync_requests.pending；systemd 的 aftersales-sync-now.path 发现标记后启动
  `aes sync --requests` 处理队列。处理程序每轮先删标记再取任务，处理期间新加的任务会留下标记，
  处理结束后 systemd 再启动一次，不会漏掉。

队列文件的读写用 flock 互斥（看板和处理程序是两个进程）。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import load_runtime

RE_UID = re.compile(r"^\d{5,20}$")
MAX_JOB_DAYS = 7
MAX_PENDING = 20
MAX_WATCH = 50
KEEP_FINISHED = 50
STALE_RUNNING = timedelta(hours=2)  # 处理程序中途退出时，超过这么久的「处理中」任务重新排队


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 关注列表（运行时设置）

def watch_list(runtime_path: str | Path | None) -> list[dict]:
    if not runtime_path:
        return []
    out, seen = [], set()
    for w in load_runtime(runtime_path).get("watch_uids") or []:
        uid = str((w or {}).get("uid", "")) if isinstance(w, dict) else str(w)
        if RE_UID.match(uid) and uid not in seen:
            seen.add(uid)
            out.append(w if isinstance(w, dict) else {"uid": uid})
    return out


def watch_uids(runtime_path: str | Path | None) -> list[str]:
    return [w["uid"] for w in watch_list(runtime_path)]


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def set_watch(runtime_path: str | Path, uid: str, watch: bool, user: str, note: str | None = None) -> tuple[list[dict], bool]:
    """加入/移出关注列表，记录修改人。返回 (新列表, 是否有变化)。"""
    if not RE_UID.match(uid or ""):
        raise ValueError("UID 需为 5 到 20 位数字")
    path = Path(runtime_path)
    with _locked(path):
        rt = load_runtime(path)
        cur = [w for w in rt.get("watch_uids") or [] if isinstance(w, dict) and RE_UID.match(str(w.get("uid", "")))]
        has = any(w["uid"] == uid for w in cur)
        if watch == has:
            return cur, False
        now = _now()
        if watch:
            if len(cur) >= MAX_WATCH:
                raise ValueError(f"最多关注 {MAX_WATCH} 个 UID")
            cur.append({"uid": uid, "by": user, "at": now, **({"note": note[:40]} if note else {})})
        else:
            cur = [w for w in cur if w["uid"] != uid]
        rt["watch_uids"] = cur
        rt["history"] = (rt.get("history", []) + [{"at": now, "by": user, "field": "watch", "from": None if watch else uid, "to": uid if watch else None}])[-100:]
        _atomic_write(path, json.dumps(rt, ensure_ascii=False, indent=1))
        return cur, True


# ---------------------------------------------------------------- 手动处理队列

class JobQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.flag = self.path.with_suffix(".pending")

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("jobs") or []
        except (OSError, ValueError):
            return []

    def _save(self, jobs: list[dict]) -> None:
        active = [j for j in jobs if j["state"] in ("pending", "running")]
        done = [j for j in jobs if j["state"] not in ("pending", "running")][-KEEP_FINISHED:]
        _atomic_write(self.path, json.dumps({"jobs": active + sorted(done, key=lambda j: j.get("finished_at") or "")}, ensure_ascii=False, indent=1))

    def snapshot(self) -> list[dict]:
        with _locked(self.path):
            return self._load()

    def add(self, uid: str, days: int, user: str, reason: str = "manual") -> dict:
        if not RE_UID.match(uid or ""):
            raise ValueError("UID 需为 5 到 20 位数字")
        if not (isinstance(days, int) and 1 <= days <= MAX_JOB_DAYS):
            raise ValueError(f"天数需在 1 到 {MAX_JOB_DAYS} 之间")
        with _locked(self.path):
            jobs = self._load()
            same = next((j for j in jobs if j["state"] == "pending" and j["uid"] == uid), None)
            if same:  # 同一个 UID 已在排队：合并，取更长的范围
                same["days"] = max(same["days"], days)
                job = same
            else:
                if sum(j["state"] == "pending" for j in jobs) >= MAX_PENDING:
                    raise ValueError("排队的任务太多，请稍后再试")
                job = {"id": secrets.token_hex(4), "uid": uid, "days": days, "by": user, "reason": reason,
                       "at": _now(), "state": "pending"}
                jobs.append(job)
            self._save(jobs)
            self.flag.touch()
            return dict(job)

    def claim(self) -> list[dict]:
        """取出排队中的任务（以及中途退出、长时间停在「处理中」的任务），标记为处理中。"""
        with _locked(self.path):
            jobs = self._load()
            stale = (datetime.now() - STALE_RUNNING).isoformat(timespec="seconds")
            picked = []
            for j in jobs:
                if j["state"] == "pending" or (j["state"] == "running" and (j.get("started_at") or "") < stale):
                    j["state"], j["started_at"] = "running", _now()
                    picked.append(dict(j))
            if picked:
                self._save(jobs)
            return picked

    def finish(self, job_id: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        with _locked(self.path):
            jobs = self._load()
            for j in jobs:
                if j["id"] == job_id:
                    j["state"] = "failed" if error else "done"
                    j["finished_at"] = _now()
                    if result is not None:
                        j["result"] = result
                    if error:
                        j["error"] = error[:300]
            self._save(jobs)

    def clear_flag(self) -> None:
        try:
            self.flag.unlink()
        except FileNotFoundError:
            pass
