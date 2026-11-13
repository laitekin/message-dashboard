"""aes sync：定时增量处理，供每小时运行。

流程：
  1. 拉取最近 N 天（默认今天和昨天）的聊天记录，每天一个原始文件，覆盖保存；
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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .extract import Extractor, to_events
from .fetch import FetchError, fetch, load_raw
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
            max_windows: int | None = None, skip_fetch: bool = False) -> dict:
        started = time.monotonic()
        today = self.now().date()
        days = int(days or self.cfg.get("days", 2))
        max_windows = int(max_windows if max_windows is not None else self.cfg.get("max_windows_per_run", 500))
        dates = [(today - timedelta(days=i)).isoformat() for i in range(days)]
        status: dict[str, Any] = {"started_at": self.now().isoformat(timespec="seconds"), "uid": uid, "dates": dates,
                                  "fetch": {}, "errors": []}

        # 1. 拉取
        raws = {}
        for d in dates:
            path = self.raw_dir / f"{uid}_{d}_{d}.json"
            if not skip_fetch:
                try:
                    raw, path = self.fetch_fn(uid, d, d, self.fetch_cfg)
                    status["fetch"][d] = {"groups": len(raw.groups), "messages": raw.total}
                except (FetchError, OSError, ValueError) as e:
                    status["fetch"][d] = {"error": str(e)[:300]}
                    status["errors"].append(f"拉取 {d} 失败：{str(e)[:200]}")
                    log.error("拉取 %s 失败：%s", d, e)
            if path.exists():
                raws[d] = load_raw(path)

        # 2. 预处理 + 切分（按天）
        types = self.settings.enabled_types
        directory = build_directory(*raws.values())
        windows_by_day: dict[str, list[Window]] = {}
        for d, raw in raws.items():
            msgs, _ = preprocess(raw, types, self.settings.section("preprocess"))
            windows_by_day[d] = segment(msgs, self.settings.section("segment"))

        # 3. 只抽取缓存里没有的窗口
        tag = cache_tag(self.settings)
        legacy = model_tag(self.settings)
        keyed = {d: [(fingerprint(w, tag), w) for w in ws] for d, ws in windows_by_day.items()}
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
        status.update(windows=sum(len(v) for v in keyed.values()), cached=sum(len(v) for v in keyed.values()) - len(todo),
                      to_extract=len(todo), deferred=len(deferred), extracted=0, failed=0,
                      max_windows=max_windows, available_models=self.list_models_fn(self.settings.section("llm")))

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
            status["extracted"] = sum(results)
            status["failed"] = len(results) - sum(results)
            if status["failed"]:
                status["errors"].append(f"{status['failed']} 个窗口抽取失败，下次运行会重试")

        # 4. 更新每天的有效窗口清单（只登记已有缓存的窗口）
        index = self._load_index()
        for d, pairs in keyed.items():
            keys = [k for k, _ in pairs if self._cache_path(k).exists()]
            # 有新消息的窗口这次没抽成功（失败或延后）时，先保留它上一个版本的结果
            pending = {w.window_id for k, w in pairs if not self._cache_path(k).exists()}
            if pending:
                for old in index.get(d, []):
                    p = self._cache_path(old)
                    if old not in keys and p.exists() and json.loads(p.read_text(encoding="utf-8")).get("window_id") in pending:
                        keys.append(old)
            index[d] = list(dict.fromkeys(keys))
        cutoff = (today - timedelta(days=self.keep_days)).isoformat()
        index = {d: ks for d, ks in sorted(index.items()) if d >= cutoff}
        _atomic_write(self.index_file, json.dumps(index, ensure_ascii=False, indent=1))

        # 5. 汇总输出
        events: list[Event] = []
        for ks in index.values():
            for k in ks:
                events.extend(self._load_cache(k) or [])
        merged = merge_events(events, self.settings.section("merge"))
        _atomic_write(self.out_file, "".join(e.model_dump_json() + "\n" for e in merged))
        status.update(events_before_merge=len(events), events=len(merged),
                      groups_with_events=len({e.group_number for e in merged}))

        # 6. 清理
        status["cleaned"] = self._cleanup(index, uid, cutoff)
        status["finished_at"] = self.now().isoformat(timespec="seconds")
        status["duration_seconds"] = round(time.monotonic() - started, 1)
        status["ok"] = not status["errors"]
        _atomic_write(self.status_file, json.dumps(status, ensure_ascii=False, indent=1))
        return status

    def _cleanup(self, index: dict[str, list[str]], uid: str, cutoff: str) -> dict:
        live = {k for ks in index.values() for k in ks}
        removed_cache = 0
        if self.cache_dir.exists():
            for p in self.cache_dir.glob("*/*.json"):
                # 只删已不被引用、且超过一天的缓存，避免误删正在使用的
                if p.stem not in live and time.time() - p.stat().st_mtime > 86400:
                    p.unlink()
                    removed_cache += 1
        removed_raw = 0
        if self.raw_dir.exists():
            for p in self.raw_dir.glob(f"{uid}_*_*.json"):
                parts = p.stem.split("_")
                if len(parts) == 3 and parts[2] < cutoff:
                    p.unlink()
                    removed_raw += 1
        return {"cache": removed_cache, "raw": removed_raw}


def run_locked(settings: Settings, lock_path: str | Path, runtime_path: str | Path | None = None, **kwargs) -> dict:
    """同一时间只允许一个 sync 运行（上一次还没跑完时直接跳过）。"""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("上一次 sync 还在运行，跳过本次")
            return {"skipped": True}
        return Sync(settings, runtime_path=runtime_path).run(**kwargs)
