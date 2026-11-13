"""aes serve：常驻的看板服务，只依赖标准库。

    GET  /                          看板页面（每次请求重新读取 out 目录，新跑出的结果刷新即可看到）
    GET  /api/data                  看板数据（页面每 20 秒轮询一次，同步其他人的修改）
    POST /api/events/<id>           修改 status / level / type / owner，body 为 JSON
    POST /api/events/<id>/revert    恢复为模型原始输出
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
from urllib.parse import urlsplit

from .board import LEVELS, STATUSES, build_payload, find_event_files, load_events, render_html
from .config import Settings
from .fetch import load_raw
from .models import Event, Message
from .preprocess import preprocess

log = logging.getLogger(__name__)

EDITABLE = ("status", "level", "type", "owner")
MAX_BODY = 64 * 1024
MAX_HISTORY = 100


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
                 title: str = "售后事件看板"):
        self.settings = settings
        self.out_dir = Path(out_dir)
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.state = BoardState(state_path)
        self.title = title
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

    def payload(self, user: str | None = None) -> dict:
        st = self.state.snapshot()
        return build_payload(self.events(), self.settings.enabled_types, self.messages(), self.title,
                             edits=st["edits"], history=st["history"], api=True, user=user)

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
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            m = re.fullmatch(r"/api/events/([\w-]+)(/revert)?", urlsplit(self.path).path)
            if not m:
                return self._json(404, {"error": "not found"})
            # 只接受 JSON：跨站表单无法在不触发预检的情况下发送 application/json
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(415, {"error": "需要 Content-Type: application/json"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "请求体过大"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "JSON 格式错误"})
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
