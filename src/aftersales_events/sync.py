"""aes sync：定时增量处理，供每小时运行；aes sync --requests 处理看板上提交的手动任务。

流程：
  1. 拉取最近 N 天（默认今天和昨天）的聊天记录：「全部群」（uid 0）+ 关注列表里的 UID，
     每个数据源每天一个原始文件，覆盖保存；
  2. 每天分别预处理、切分成窗口；
  3. 按窗口内容算指纹，缓存里有的直接复用，只有新增或有新消息的窗口才调用模型；
  4. 每天记录一份「当前有效窗口」清单，同一天旧版本窗口的结果不再参与输出；
  5. 汇总保留期内所有天的事件，跨窗口、跨天合并，原子写入 out/live/events.jsonl；
  6. 写同步状态文件，清理过期的原始文件和缓存。

单个窗口抽取失败不会写缓存，下次运行自动重试。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .extract import Extractor, to_events
from .fetch import FetchError, fetch, load_raw
from .jobs import watch_uids
from .merge import merge_events
from .models import Event, Window
from .preprocess import preprocess
from .privacy import build_directory, wrap
from .segment import segment

log = logging.getLogger(__name__)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def fingerprint(w: Window, model_tag: str) -> str:
    h = hashlib.sha1(model_tag.encode())
    for m in w.messages:
        h.update(f"\x1e{m.id}\x1f{m.role}\x1f{m.sender}\x1f{m.send_time.isoformat()}\x1f{m.content}".encode())
    return h.hexdigest()[:20]


def active_model(settings: Settings) -> tuple[str, str | None]:
    llm = settings.section("llm")
    provider = llm.get("provider", "claude")
    return provider, (llm.get("model") if provider == "claude" else (llm.get(provider) or {}).get("model"))


def model_tag(settings: Settings) -> str:
    """旧版缓存指纹包含模型名；只用于把旧缓存迁移到新指纹。"""
    provider, model = active_model(settings)
    return f"{provider}:{model}:{cache_tag(settings)}"


def cache_tag(settings: Settings) -> str:
    """缓存指纹不含模型名：换模型只影响之后新增或变化的对话，已处理的不重跑（需要时用 --reprocess）。"""
    return f"names={settings.section('privacy').get('pseudonymize_names', True)}"


class Sync:
    def __init__(self, settings: Settings, extractor: Extractor | None = None,
                 fetch_fn: Callable = fetch, now: Callable[[], datetime] = datetime.now,
                 runtime_path: str | Path | None = None, list_models_fn: Callable | None = None):
        from .config import apply_runtime
        from .extract import list_models

        self.settings = settings
        # 看板「设置」页选择的模型优先
        self.runtime_model = apply_runtime(settings, runtime_path) if runtime_path else None
        self.runtime_path = runtime_path
        self.list_models_fn = list_models_fn or list_models
        self.cfg = settings.section("sync")
        self.fetch_cfg = settings.section("fetch")
        self.extractor = extractor
        self.fetch_fn = fetch_fn
        self.now = now
        self.raw_dir = Path(self.fetch_cfg.get("raw_dir", "data/raw"))
        self.cache_dir = Path(self.cfg.get("cache_dir", "data/windows"))
        self.index_file = self.cache_dir / "index.json"
        self.out_file = Path(self.cfg.get("out_file", "out/live/events.jsonl"))
        self.status_file = Path(self.cfg.get("status_file", "data/sync_status.json"))
        self.keep_days = int(self.cfg.get("keep_days", 30))

    # ------------------------------------------------------------ 缓存
    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _load_cache(self, key: str) -> list[Event] | None:
        p = self._cache_path(key)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return [Event.model_validate(e) for e in data["events"]]

    def _save_cache(self, key: str, w: Window, events: list[Event], tag: str) -> None:
        _atomic_write(self._cache_path(key), json.dumps({
            "key": key, "window_id": w.window_id, "group_number": w.group_number, "model": tag,
            "extracted_at": self.now().isoformat(timespec="seconds"),
            "events": [json.loads(e.model_dump_json()) for e in events],
        }, ensure_ascii=False))

    def _load_index(self) -> dict[str, list[str]]:
        return json.loads(self.index_file.read_text(encoding="utf-8")) if self.index_file.exists() else {}

    # ------------------------------------------------------------ 主流程
    def run(self, uid: str = "0", days: int | None = None, reprocess: bool = False,
            max_windows: int | None = None, skip_fetch: bool = False, watch: bool = True) -> dict:
        """定时处理：「全部群」（uid 0）+ 关注列表里的 UID，各取最近 N 天。写同步状态文件。"""
        days = int(days or self.cfg.get("days", 2))
        uids = [uid] + ([u for u in watch_uids(self.runtime_path) if u != uid] if watch else [])
        status = self.process([(u, days) for u in uids], reprocess=reprocess, max_windows=max_windows, skip_fetch=skip_fetch)
        status["uid"] = uid
        status["watch_uids"] = uids[1:]
        _atomic_write(self.status_file, json.dumps(status, ensure_ascii=False, indent=1))
        return status

    def process(self, sources: list[tuple[str, int]], reprocess: bool = False,
                max_windows: int | None = None, skip_fetch: bool = False) -> dict:
        """处理若干数据源：每个 (uid, 天数) 按天拉取 → 切分 → 只抽取新增/变化的对话 → 合并输出。

        结果按「数据源 + 日期」登记在 index.json（uid 0 沿用旧格式：键就是日期），
        所以手动处理过的 UID 不会被下一次定时处理覆盖掉。抽取阶段不加锁（缓存按内容寻址），
        只有更新清单和写输出这一小段持有提交锁，手动处理不用等定时处理跑完。
        """
        started = time.monotonic()
        today = self.now().date()
        max_windows = int(max_windows if max_windows is not None else self.cfg.get("max_windows_per_run", 500))
        dates = sorted({(today - timedelta(days=i)).isoformat() for _, n in sources for i in range(n)}, reverse=True)
        status: dict[str, Any] = {"started_at": self.now().isoformat(timespec="seconds"), "dates": dates,
                                  "fetch": {}, "sources": {}, "errors": []}

        # 1. 拉取（每个数据源每天一个原始文件）
        raws: dict[tuple[str, str], Any] = {}
        for uid, n in sources:
            src = status["sources"].setdefault(uid, {"dates": [], "fetch": {}})
            for i in range(n):
                d = (today - timedelta(days=i)).isoformat()
                src["dates"].append(d)
                path = self.raw_dir / f"{uid}_{d}_{d}.json"
                if not skip_fetch:
                    try:
                        raw, path = self.fetch_fn(uid, d, d, self.fetch_cfg)
                        src["fetch"][d] = {"groups": len(raw.groups), "messages": raw.total}
                    except (FetchError, OSError, ValueError) as e:
                        src["fetch"][d] = {"error": str(e)[:300]}
                        status["errors"].append(f"拉取{'' if uid == '0' else f' UID {uid}'} {d} 失败：{str(e)[:200]}")
                        log.error("拉取 uid=%s %s 失败：%s", uid, d, e)
                if path.exists():
                    raws[(uid, d)] = load_raw(path)
        status["fetch"] = status["sources"].get("0", {}).get("fetch", {})  # 兼容旧的状态显示

        # 2. 预处理 + 切分（按数据源、按天）
        types = self.settings.enabled_types
        directory = build_directory(*raws.values())
        tag = cache_tag(self.settings)
        legacy = model_tag(self.settings)
        keyed: dict[str, list[tuple[str, Window]]] = {}
        for (uid, d), raw in raws.items():
            msgs, _ = preprocess(raw, types, self.settings.section("preprocess"))
            keyed[index_key(uid, d)] = [(fingerprint(w, tag), w) for w in segment(msgs, self.settings.section("segment"))]

        # 3. 只抽取缓存里没有的窗口（同一段对话出现在多个数据源里只抽一次）
        for pairs in keyed.values():
            for key, w in pairs:  # 兼容旧版（指纹含模型名）的缓存
                new, old = self._cache_path(key), self._cache_path(fingerprint(w, legacy))
                if not new.exists() and old.exists():
                    new.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old, new)
        provider, model = active_model(self.settings)
        status["llm"] = {"provider": provider, "model": model, "override": self.runtime_model}
        todo: dict[str, Window] = {}
        for pairs in keyed.values():
            for key, w in pairs:
                if reprocess or not self._cache_path(key).exists():
                    todo.setdefault(key, w)
        # 超出单次上限时先处理最近的对话，其余留到下次
        ordered = sorted(todo.items(), key=lambda kv: kv[1].end, reverse=True)
        run_now, deferred = ordered[:max_windows], ordered[max_windows:]
        n_windows = len({k for pairs in keyed.values() for k, _ in pairs})
        status.update(windows=n_windows, cached=n_windows - len(todo), to_extract=len(todo), deferred=len(deferred),
                      extracted=0, failed=0, max_windows=max_windows, available_models=self.list_models_fn(self.settings.section("llm")))

        ok_keys: set[str] = set()
        if run_now:
            if self.extractor is None:
                from .extract import make_extractor
                self.extractor = make_extractor(types, self.settings.section("llm"))
            ex = wrap(self.extractor, directory, self.settings.section("privacy"))
            valid = {t.name for t in types}

            def work(item: tuple[str, Window]) -> bool:
                key, w = item
                try:
                    events = to_events(w, ex.extract(w), valid)
                except Exception as e:  # 单个窗口失败不影响整体，下次重试
                    log.error("window %s 抽取失败：%s", w.window_id, e)
                    return False
                self._save_cache(key, w, events, f"{provider}:{model}")
                return True

            with ThreadPoolExecutor(max_workers=max(1, int(self.settings.section("llm").get("concurrency", 4)))) as pool:
                results = list(pool.map(work, run_now))
            ok_keys = {k for (k, _), ok in zip(run_now, results) if ok}
            status["extracted"] = sum(results)
            status["failed"] = len(results) - sum(results)
            if status["failed"]:
                status["errors"].append(f"{status['failed']} 个窗口抽取失败，下次运行会重试")
        for uid, src in status["sources"].items():
            keys = {k for d in src["dates"] for k, _ in keyed.get(index_key(uid, d), [])}
            src.update(windows=len(keys), extracted=len(keys & ok_keys), pending=len(keys & set(todo)) - len(keys & ok_keys))

        # 4-6. 更新清单、汇总输出、清理：持有提交锁（很短）
        with self._commit_lock():
            index = self._load_index()
            for ik, pairs in keyed.items():
                keys = [k for k, _ in pairs if self._cache_path(k).exists()]
                # 有新消息的窗口这次没抽成功（失败或延后）时，先保留它上一个版本的结果
                pending = {w.window_id for k, w in pairs if not self._cache_path(k).exists()}
                if pending:
                    for old in index.get(ik, []):
                        p = self._cache_path(old)
                        if old not in keys and p.exists() and json.loads(p.read_text(encoding="utf-8")).get("window_id") in pending:
                            keys.append(old)
                index[ik] = list(dict.fromkeys(keys))
            cutoff = (today - timedelta(days=self.keep_days)).isoformat()
            index = {k: ks for k, ks in sorted(index.items()) if index_day(k) >= cutoff}
            _atomic_write(self.index_file, json.dumps(index, ensure_ascii=False, indent=1))

            events: list[Event] = []
            for k in dict.fromkeys(k for ks in index.values() for k in ks):  # 多个数据源共有的对话只算一次
                events.extend(self._load_cache(k) or [])
            merged = merge_events(events, self.settings.section("merge"))
            _atomic_write(self.out_file, "".join(e.model_dump_json() + "\n" for e in merged))
            status.update(events_before_merge=len(events), events=len(merged),
                          groups_with_events=len({e.group_number for e in merged}))
            for uid, src in status["sources"].items():
                gids = {w.group_number for d in src["dates"] for _, w in keyed.get(index_key(uid, d), [])}
                src["groups"] = len(gids)
                src["events"] = sum(1 for e in merged if e.group_number in gids or (uid != "0" and e.customer_uid == uid))
            status["cleaned"] = self._cleanup(index, cutoff)

        status["finished_at"] = self.now().isoformat(timespec="seconds")
        status["duration_seconds"] = round(time.monotonic() - started, 1)
        status["ok"] = not status["errors"]
        return status

    @contextmanager
    def _commit_lock(self):
        lock = self.cache_dir / "commit.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _cleanup(self, index: dict[str, list[str]], cutoff: str) -> dict:
        live = {k for ks in index.values() for k in ks}
        removed_cache = 0
        if self.cache_dir.exists():
            for p in self.cache_dir.glob("*/*.json"):
                # 只删已不被引用、且超过一天的缓存，避免误删正在使用的（包括另一个进程刚抽取、还没登记的）
                if p.stem not in live and time.time() - p.stat().st_mtime > 86400:
                    p.unlink()
                    removed_cache += 1
        removed_raw = 0
        if self.raw_dir.exists():
            for p in self.raw_dir.glob("*_*_*.json"):
                parts = p.stem.split("_")
                if len(parts) == 3 and parts[2] < cutoff:
                    p.unlink()
                    removed_raw += 1
        return {"cache": removed_cache, "raw": removed_raw}


def index_key(uid: str, d: str) -> str:
    """index.json 的键：uid 0 就是日期（兼容旧格式），其他 UID 为 "<uid>|<日期>"。"""
    return d if uid == "0" else f"{uid}|{d}"


def index_day(key: str) -> str:
    return key.rsplit("|", 1)[-1]


def _try_lock(lock_path: str | Path):
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = lock_path.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def run_locked(settings: Settings, lock_path: str | Path, runtime_path: str | Path | None = None, **kwargs) -> dict:
    """同一时间只允许一个定时 sync 运行（上一次还没跑完时直接跳过）。"""
    f = _try_lock(lock_path)
    if f is None:
        log.warning("上一次 sync 还在运行，跳过本次")
        return {"skipped": True}
    with f:
        return Sync(settings, runtime_path=runtime_path).run(**kwargs)


def run_jobs(settings: Settings, queue_path: str | Path, lock_path: str | Path, runtime_path: str | Path | None = None,
             max_windows: int | None = None, sync: Sync | None = None) -> dict:
    """处理手动任务队列，直到队列为空。每个任务单独处理、单独记录结果。"""
    from .jobs import JobQueue

    f = _try_lock(lock_path)
    if f is None:
        log.warning("另一个手动处理正在运行，跳过本次")
        return {"skipped": True}
    q = JobQueue(queue_path)
    done = []
    max_windows = int(max_windows if max_windows is not None else settings.section("sync").get("job_max_windows", 150))
    with f:
        while True:
            q.clear_flag()  # 先删标记再取任务：之后新加的任务会重新留下标记
            jobs = q.claim()
            if not jobs:
                break
            sync = sync or Sync(settings, runtime_path=runtime_path)
            for job in jobs:
                log.info("手动处理 UID %s 最近 %s 天（%s 提交）", job["uid"], job["days"], job.get("by"))
                try:
                    st = sync.process([(job["uid"], int(job["days"]))], max_windows=max_windows)
                except Exception as e:
                    log.exception("手动处理 UID %s 失败", job["uid"])
                    q.finish(job["id"], error=f"{type(e).__name__}: {e}")
                    done.append({"id": job["id"], "error": str(e)[:200]})
                    continue
                src = st["sources"].get(job["uid"], {})
                result = {k: src.get(k) for k in ("groups", "windows", "extracted", "pending", "events")}
                result.update(dates=src.get("dates"), duration_seconds=st["duration_seconds"], llm=st.get("llm"))
                fetch_errors = [v["error"] for v in (src.get("fetch") or {}).values() if "error" in v]
                if fetch_errors and len(fetch_errors) == len(src.get("dates") or []):
                    q.finish(job["id"], result, error="拉取失败：" + fetch_errors[0])
                else:
                    if st["errors"]:
                        result["warnings"] = st["errors"][:5]
                    q.finish(job["id"], result)
                done.append({"id": job["id"], **result})
    return {"jobs": done}
