"""aes serve：常驻的看板服务，只依赖标准库。

    GET  /                          看板页面（每次请求重新读取 out 目录，新跑出的结果刷新即可看到）
    GET  /api/data                  看板数据（页面每 20 秒轮询一次，同步其他人的修改）
    POST /api/events/<id>           修改 status / level / type / owner，body 为 JSON
    POST /api/events/<id>/revert    恢复为模型原始输出
    GET  /api/settings              模型设置（当前使用、已选择、可用模型、修改记录）
    POST /api/settings              {"model": "..."} 选择抽取模型，下一次 aes sync 生效；null 恢复默认
    GET  /api/groups/<群号>/messages?date=YYYY-MM-DD[&uid=<UID>]
                                    某个群某天的全部聊天记录：通过取数中转（aes relay）实时请求数据源，
                                    只在内存里短暂缓存，不落盘；未配置中转时退回读取已保存的原始文件。
                                    带 UID 的客户群不在 uid=0 的结果里，需要传 uid 才能取到
    GET  /api/uids/<UID>?days=7     按用户 UID 查询关联的群（内部群、客户群等）：最近 N 天（最多 31）
                                    有消息的群、各群消息概况，以及看板里该 UID 的事件
    GET  /healthz

人工修改单独保存在 state 文件（JSON）里，不改动 events.jsonl，并记录谁在什么时候改了什么。
服务只应监听 127.0.0.1；鉴权交给前面的反向代理（例如 Caddy basic_auth），
代理通过 X-Remote-User 头传入用户名，用于记录修改人。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
import urllib.error
import urllib.request
from datetime import date
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from .board import LEVELS, STATUSES, build_payload, clean_group_name, find_event_files, load_events, render_html
from .config import MODEL_RE, Settings, load_runtime
from .fetch import load_raw
from .models import Event, Message
from .preprocess import RE_IMAGE_URL, mask, parse_customer_uid, preprocess

log = logging.getLogger(__name__)

EDITABLE = ("status", "level", "type", "owner")
MAX_BODY = 64 * 1024
MAX_HISTORY = 100
RE_LOOKUP_UID = re.compile(r"^\d{5,20}$")
RE_INTERNAL_GROUP = re.compile(r"^\s*[「\[【(（]?\s*内部")
MAX_LOOKUP_DAYS = 31


class BoardState:
    """人工修改 + 修改记录，原子写入 JSON 文件。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = {"edits": {}, "history": {}}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self.data["edits"] = loaded.get("edits", {})
            self.data["history"] = loaded.get("history", {})

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _result(self, eid: str) -> dict:
        return {"event_id": eid, "edits": self.data["edits"].get(eid, {}), "history": self.data["history"].get(eid, [])}

    def apply(self, event: Event, patch: dict, user: str) -> dict:
        eid = event.event_id
        with self.lock:
            cur = dict(self.data["edits"].get(eid, {}))
            hist = list(self.data["history"].get(eid, []))
            now = datetime.now().isoformat(timespec="seconds")
            for key, value in patch.items():
                orig = getattr(event, key)
                before = cur.get(key, orig)
                if value == before:
                    continue
                if value == orig:
                    cur.pop(key, None)
                else:
                    cur[key] = value
                hist.append({"at": now, "by": user, "field": key, "from": before, "to": value})
            self._store(eid, cur, hist[-MAX_HISTORY:])
            return self._result(eid)

    def revert(self, event: Event, user: str) -> dict:
        eid = event.event_id
        with self.lock:
            hist = list(self.data["history"].get(eid, []))
            if self.data["edits"].get(eid):
                hist.append({"at": datetime.now().isoformat(timespec="seconds"), "by": user, "field": "*", "from": None, "to": None})
            self._store(eid, {}, hist[-MAX_HISTORY:])
            return self._result(eid)

    def _store(self, eid: str, cur: dict, hist: list) -> None:
        for bucket, value in (("edits", cur), ("history", hist)):
            if value:
                self.data[bucket][eid] = value
            else:
                self.data[bucket].pop(eid, None)
        self._save()


class BoardApp:
    def __init__(self, settings: Settings, out_dir: str | Path, raw_dir: str | Path | None, state_path: str | Path,
                 title: str = "售后事件看板", sync_status: str | Path | None = None,
                 runtime_path: str | Path | None = None, relay_url: str | None = None):
        self.settings = settings
        self.out_dir = Path(out_dir)
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.state = BoardState(state_path)
        self.title = title
        self.sync_status = Path(sync_status) if sync_status else None
        self.runtime_path = Path(runtime_path) if runtime_path else None
        self.relay_url = relay_url.rstrip("/") if relay_url else None
        self._live: dict[str, tuple[float, dict, str]] = {}
        self._live_lock = threading.Lock()
        self._rt_lock = threading.Lock()
        self._msg_lock = threading.Lock()
        self._msg_key: tuple = ()
        self._msgs: list[Message] = []

    def events(self) -> list[Event]:
        if not self.out_dir.exists():
            return []
        files = find_event_files([self.out_dir])
        return load_events(files) if files else []

    def messages(self) -> list[Message]:
        """原始数据预处理比较慢，按文件修改时间缓存。"""
        files = sorted(self.raw_dir.rglob("*.json")) if self.raw_dir and self.raw_dir.exists() else []
        key = tuple((str(f), f.stat().st_mtime_ns) for f in files)
        with self._msg_lock:
            if key != self._msg_key:
                msgs: list[Message] = []
                for f in files:
                    try:
                        m, _ = preprocess(load_raw(f), self.settings.enabled_types, self.settings.section("preprocess"))
                        msgs.extend(m)
                    except Exception as e:  # 单个文件损坏不影响看板
                        log.warning("读取原始数据 %s 失败：%s", f, e)
                self._msgs, self._msg_key = msgs, key
            return self._msgs

    def sync_info(self) -> dict | None:
        if not (self.sync_status and self.sync_status.exists()):
            return None
        try:
            return json.loads(self.sync_status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------ 聊天记录（实时）
    LIVE_TTL_TODAY = 120
    LIVE_TTL_PAST = 1800
    LIVE_CACHE_ENTRIES = 24  # 往上翻会连续加载多天；另外留几条给按 UID 查询

    def _stored_raw(self, uid: str, start: str, end: str) -> tuple[list[dict], str]:
        """未配置实时取数时，从已保存的原始文件里找（文件名为 <uid>_<start>_<end>.json）。"""
        files = sorted(self.raw_dir.glob("*_*_*.json")) if self.raw_dir and self.raw_dir.exists() else []
        hits = []
        for f in files:
            parts = f.stem.rsplit("_", 2)
            if len(parts) == 3 and parts[1] <= end and parts[2] >= start and (uid == "0" or parts[0] in ("0", uid)):
                hits.append(f)
        if not hits:
            raise LookupError(f"没有 {start}{'' if start == end else ' ~ ' + end} 的数据（未配置实时取数）")
        groups = []
        for f in hits:
            for g in json.loads(f.read_text(encoding="utf-8")).get("groups") or []:
                # 按 UID 查时，「全部群」文件里只取群名带这个 UID 的群
                if uid == "0" or f.stem.startswith(uid + "_") or parse_customer_uid(g.get("group_name") or "") == uid:
                    groups.append(g)
        fetched = datetime.fromtimestamp(max(f.stat().st_mtime for f in hits)).isoformat(timespec="seconds")
        return groups, fetched

    def _raw(self, uid: str, start: str, end: str, fresh: bool = False) -> tuple[list[dict], str, str]:
        """返回 (群列表, 来源 live/stored, 获取时间)。"""
        if not self.relay_url:
            groups, fetched = self._stored_raw(uid, start, end)
            return groups, "stored", fetched
        key = f"{uid}|{start}|{end}"
        ttl = self.LIVE_TTL_TODAY if end >= date.today().isoformat() else self.LIVE_TTL_PAST
        with self._live_lock:
            hit = self._live.get(key)
            if hit and not fresh and time.monotonic() - hit[0] < ttl:
                return hit[1].get("groups") or [], "live", hit[2]
        url = f"{self.relay_url}/chat?{urlencode({'uid': uid, 'start': start, 'end': end})}"
        try:
            with urllib.request.urlopen(url, timeout=100) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error")
            except Exception:
                msg = None
            raise RuntimeError(msg or f"取数失败 HTTP {e.code}") from e
        except OSError as e:
            raise RuntimeError(f"取数服务不可用：{e}") from e
        fetched = datetime.now().isoformat(timespec="seconds")
        with self._live_lock:
            self._live[key] = (time.monotonic(), data, fetched)
            for old in sorted(self._live, key=lambda k: self._live[k][0])[:-self.LIVE_CACHE_ENTRIES]:
                self._live.pop(old, None)
        return data.get("groups") or [], "live", fetched

    @staticmethod
    def _records(group: dict | None) -> list[dict]:
        """按时间排序并按 id 去重（按范围查询时同一条消息可能出现多次）。"""
        out, seen = [], set()
        for r in sorted((group or {}).get("records") or [], key=lambda r: (r.get("send_time") or r.get("created_at") or "", str(r.get("id")))):
            rid = str(r.get("id"))
            if rid not in seen:
                seen.add(rid)
                out.append(r)
        return out

    def group_messages(self, gid: str, d: str, fresh: bool = False, uid: str = "0") -> dict:
        groups, source, fetched = self._raw(uid, d, d, fresh)
        parts = [g for g in groups if g.get("group_number") == gid]
        group = {"group_name": parts[-1].get("group_name"), "records": [r for g in parts for r in g.get("records") or []]} if parts else None
        msgs = []
        for r in self._records(group):
            content = (r.get("content") or "").strip()
            image = content == "[图片]" or (bool(RE_IMAGE_URL.findall(content)) and len(RE_IMAGE_URL.sub("", content).strip()) < 5)
            msgs.append({
                "id": str(r.get("id")),
                "t": (r.get("send_time") or r.get("created_at") or "").replace(" ", "T"),
                "role": "内部" if r.get("is_inner") else "客户",
                "sender": r.get("display_name") or r.get("nick_name") or r.get("sender_number") or "",
                "content": mask(content),  # 手机号、邮箱、密钥照样脱敏
                "image": image,
            })
        evidence: dict[str, list] = {}
        for e in self.events():
            if e.group_number == gid:
                for mid in e.evidence_msg_ids:
                    evidence.setdefault(mid, []).append({"event_id": e.event_id, "title": e.title, "type": e.type})
        name = (group or {}).get("group_name") or next((e.group_name for e in self.events() if e.group_number == gid), gid)
        return {"group": {"id": gid, "name": name}, "date": d, "source": source, "fetched_at": fetched,
                "total": len(msgs), "messages": msgs, "evidence": evidence}

    def uid_lookup(self, uid: str, days: int = 7, fresh: bool = False) -> dict:
        """按用户 UID 查询关联的群：数据源按 UID 返回该用户的内部群、客户群等（这些群不在 uid=0 的结果里）。"""
        end = date.today()
        start = date.fromordinal(end.toordinal() - days + 1)
        groups, source, fetched = self._raw(uid, start.isoformat(), end.isoformat(), fresh)
        merged: dict[str, dict] = {}
        for g in groups:  # 按范围查询时同一个群可能按天分成多段
            gid = g.get("group_number")
            if gid:
                m = merged.setdefault(gid, {"group_number": gid, "group_name": g.get("group_name") or "", "records": []})
                m["records"].extend(g.get("records") or [])
                m["group_name"] = g.get("group_name") or m["group_name"]
        out = []
        for gid, g in merged.items():
            recs = self._records(g)
            name = g["group_name"] or (recs[-1].get("group_name") if recs else "") or gid
            times = [(r.get("send_time") or r.get("created_at") or "").replace(" ", "T") for r in recs]
            times = [t for t in times if t]
            out.append({
                "id": gid, "name": clean_group_name(name), "raw_name": name,
                "uid": parse_customer_uid(name) or uid,
                "internal": bool(RE_INTERNAL_GROUP.match(name)),
                "messages": len(recs),
                "inner": sum(1 for r in recs if r.get("is_inner")),
                "customer": sum(1 for r in recs if not r.get("is_inner")),
                "senders": len({r.get("sender_number") or r.get("display_name") for r in recs}),
                "first": min(times) if times else None, "last": max(times) if times else None,
            })
        out.sort(key=lambda g: (not g["internal"], g["last"] or ""), reverse=True)  # 客户群在前，再按最近消息
        gids = {g["id"] for g in out}
        event_ids = [e.event_id for e in self.events() if e.customer_uid == uid or e.group_number in gids]
        return {"uid": uid, "start": start.isoformat(), "end": end.isoformat(), "days": days, "source": source,
                "fetched_at": fetched, "groups": out, "event_ids": event_ids}

    def model_settings(self) -> dict:
        """模型设置：provider 和实际使用的模型以最近一次 sync 为准（看板服务本身不持有 API key）。"""
        sync = self.sync_info() or {}
        llm = sync.get("llm") or {}
        rt = load_runtime(self.runtime_path) if self.runtime_path else {}
        return {
            "editable": self.runtime_path is not None,
            "provider": llm.get("provider"),
            "in_use": llm.get("model"),
            "selected": (rt.get("llm") or {}).get("model"),
            "available": sync.get("available_models") or [],
            "history": rt.get("history", [])[-20:],
            "updated_at": rt.get("updated_at"),
            "updated_by": rt.get("updated_by"),
        }

    def set_model(self, model: object, user: str) -> dict:
        if not self.runtime_path:
            raise ValueError("未配置运行时设置文件，无法修改")
        if model is not None and not (isinstance(model, str) and re.fullmatch(MODEL_RE, model.strip())):
            raise ValueError("模型名格式不正确")
        model = model.strip() if model else None
        available = (self.sync_info() or {}).get("available_models") or []
        if model and available and model not in available:
            raise ValueError(f"网关上没有模型 {model}")
        with self._rt_lock:
            rt = load_runtime(self.runtime_path)
            before = (rt.get("llm") or {}).get("model")
            if model != before:
                now = datetime.now().isoformat(timespec="seconds")
                rt.setdefault("llm", {})["model"] = model
                rt["history"] = (rt.get("history", []) + [{"at": now, "by": user, "field": "model", "from": before, "to": model}])[-MAX_HISTORY:]
                rt["updated_at"], rt["updated_by"] = now, user
                self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.runtime_path.with_name(self.runtime_path.name + ".tmp")
                tmp.write_text(json.dumps(rt, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, self.runtime_path)
        return self.model_settings()

    def payload(self, user: str | None = None) -> dict:
        st = self.state.snapshot()
        p = build_payload(self.events(), self.settings.enabled_types, self.messages(), self.title,
                          edits=st["edits"], history=st["history"], api=True, user=user, sync=self.sync_info())
        p["model_settings"] = self.model_settings()
        p["today"] = date.today().isoformat()
        p["live_chat"] = bool(self.relay_url) or bool(self.raw_dir)
        return p

    def validate(self, patch: object, events: list[Event]) -> dict:
        if not isinstance(patch, dict) or not patch:
            raise ValueError("请求体应为非空 JSON 对象")
        types = {t.name for t in self.settings.enabled_types} | {e.type for e in events}
        allowed = {"status": set(STATUSES), "level": set(LEVELS), "type": types}
        out = {}
        for k, v in patch.items():
            if k == "owner":
                if v is not None and not (isinstance(v, str) and 0 < len(v.strip()) <= 40):
                    raise ValueError("跟进人需为 1 到 40 个字符，或 null")
                out[k] = v.strip() if v else None
            elif k in allowed and v in allowed[k]:
                out[k] = v
            else:
                raise ValueError(f"不支持的修改：{k}={v!r}")
        return out


def make_handler(app: BoardApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "aes-board"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _user(self) -> str:
            return (self.headers.get("X-Remote-User") or "").strip()[:40] or "匿名"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, render_html(app.payload(self._user())).encode(), "text/html; charset=utf-8")
            elif path == "/api/data":
                self._json(200, app.payload(self._user()))
            elif path.startswith("/api/groups/") and path.endswith("/messages"):
                gid = unquote(path[len("/api/groups/"):-len("/messages")])
                qs = parse_qs(urlsplit(self.path).query)
                d = (qs.get("date") or [date.today().isoformat()])[0]
                fresh = (qs.get("fresh") or ["0"])[0] == "1"
                uid = (qs.get("uid") or ["0"])[0] or "0"
                try:
                    date.fromisoformat(d)
                except ValueError:
                    return self._json(400, {"error": "date 需为 YYYY-MM-DD"})
                if not gid or len(gid) > 100:
                    return self._json(400, {"error": "群号不正确"})
                if uid != "0" and not RE_LOOKUP_UID.match(uid):
                    return self._json(400, {"error": "UID 需为 5 到 20 位数字"})
                try:
                    self._json(200, app.group_messages(gid, d, fresh, uid))
                except LookupError as e:
                    self._json(404, {"error": str(e)})
                except RuntimeError as e:
                    self._json(502, {"error": str(e)})
            elif path.startswith("/api/uids/"):
                uid = unquote(path[len("/api/uids/"):]).strip()
                qs = parse_qs(urlsplit(self.path).query)
                if not RE_LOOKUP_UID.match(uid):
                    return self._json(400, {"error": "UID 需为 5 到 20 位数字"})
                try:
                    days = int((qs.get("days") or ["7"])[0])
                except ValueError:
                    days = 0
                if not 1 <= days <= MAX_LOOKUP_DAYS:
                    return self._json(400, {"error": f"days 需在 1 到 {MAX_LOOKUP_DAYS} 之间"})
                try:
                    self._json(200, app.uid_lookup(uid, days, (qs.get("fresh") or ["0"])[0] == "1"))
                except LookupError as e:
                    self._json(404, {"error": str(e)})
                except RuntimeError as e:
                    self._json(502, {"error": str(e)})
            elif path == "/api/settings":
                self._json(200, app.model_settings())
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            else:
                self._json(404, {"error": "not found"})

        def _read_json(self):
            # 只接受 JSON：跨站表单无法在不触发预检的情况下发送 application/json
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._json(415, {"error": "需要 Content-Type: application/json"})
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(413, {"error": "请求体过大"})
                return None
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "JSON 格式错误"})
                return None

        def do_POST(self):
            path = urlsplit(self.path).path
            if path == "/api/settings":
                body = self._read_json()
                if body is None:
                    return
                if not isinstance(body, dict) or "model" not in body:
                    return self._json(400, {"error": "需要 model 字段"})
                try:
                    return self._json(200, app.set_model(body["model"], self._user()))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            m = re.fullmatch(r"/api/events/([\w-]+)(/revert)?", path)
            if not m:
                return self._json(404, {"error": "not found"})
            body = self._read_json()
            if body is None:
                return
            events = app.events()
            event = next((e for e in events if e.event_id == m.group(1)), None)
            if event is None:
                return self._json(404, {"error": "事件不存在"})
            try:
                if m.group(2):
                    res = app.state.revert(event, self._user())
                else:
                    res = app.state.apply(event, app.validate(body, events), self._user())
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self._json(200, res)

    return Handler


def make_server(app: BoardApp, host: str = "127.0.0.1", port: int = 8470) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
