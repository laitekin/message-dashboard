"""aes relay：只监听本机的取数中转服务，供看板实时查看聊天记录。

数据源只允许通过 vpn-proxy 容器访问，执行 docker exec 需要 docker 权限（基本等同 root）。
为了不让对外的看板服务持有这个权限，单独起一个很小的服务：

    GET /chat?uid=0&date=YYYY-MM-DD   请求数据源并原样返回 JSON（不落盘）
    GET /chat?uid=<UID>&start=YYYY-MM-DD&end=YYYY-MM-DD
                                      按日期范围请求（最多 31 天），用于按 UID 查询关联的群
    GET /healthz

只接受数字 UID 和合法日期（范围不超过 31 天），同一时间最多 2 个并发请求，只应监听 127.0.0.1。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .fetch import FetchError, request

log = logging.getLogger(__name__)

RE_UID = re.compile(r"^\d{1,20}$")
MAX_RANGE_DAYS = 31


def make_relay(cfg: dict[str, Any], host: str = "127.0.0.1", port: int = 8472,
               request_fn: Callable = request, max_concurrency: int = 2) -> ThreadingHTTPServer:
    sem = threading.BoundedSemaphore(max_concurrency)

    class Handler(BaseHTTPRequestHandler):
        server_version = "aes-relay"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlsplit(self.path)
            if u.path == "/healthz":
                return self._send(200, {"ok": True})
            if u.path != "/chat":
                return self._send(404, {"error": "not found"})
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            uid = q.get("uid", "0")
            start = q.get("start") or q.get("date", "")
            end = q.get("end") or start
            try:
                span = (date.fromisoformat(end) - date.fromisoformat(start)).days
            except ValueError:
                return self._send(400, {"error": "date / start / end 需为 YYYY-MM-DD"})
            if not 0 <= span < MAX_RANGE_DAYS:
                return self._send(400, {"error": f"日期范围需在 1 到 {MAX_RANGE_DAYS} 天之间"})
            if not RE_UID.match(uid):
                return self._send(400, {"error": "uid 需为数字"})
            if not sem.acquire(timeout=90):
                return self._send(503, {"error": "取数繁忙，请稍后再试"})
            try:
                return self._send(200, request_fn(uid, start, end, cfg))
            except FetchError as e:
                return self._send(502, {"error": str(e)[:300]})
            except Exception as e:  # docker 不可用等
                log.exception("relay 请求失败")
                return self._send(502, {"error": f"取数失败：{type(e).__name__}"})
            finally:
                sem.release()

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
