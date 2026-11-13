"""拉取聊天记录。

支持直接发送 HTTP 请求，或在有权访问数据源的 Docker 容器内执行请求。
原始 JSON 会落盘到 data/raw/，后续清洗全部离线进行。
"""

from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .models import RawResponse


class FetchError(RuntimeError):
    pass


def _body(uid: str, start: str, end: str) -> str:
    return json.dumps({"uid": uid, "start": start, "end": end}, ensure_ascii=False)


def fetch_docker(uid: str, start: str, end: str, cfg: dict[str, Any]) -> str:
    container = cfg.get("container", "fetch-proxy")
    timeout = int(cfg.get("timeout_seconds", 60))
    cmd = [
        "docker", "exec", container,
        "curl", "-sS", "--fail-with-body", "--max-time", str(timeout),
        "-X", "POST", cfg["url"],
        "-H", "Content-Type: application/json;charset=UTF-8",
        "-d", _body(uid, start, end),
    ]
    ssh_prefix = list(cfg.get("ssh_prefix") or [])
    if ssh_prefix:
        # 经 ssh 执行时远端会再做一次 shell 解析，需要整体转义
        cmd = [*ssh_prefix, shlex.join(cmd)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    if proc.returncode != 0:
        raise FetchError(f"docker exec 失败 (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout[:300]}")
    return proc.stdout


def fetch_direct(uid: str, start: str, end: str, cfg: dict[str, Any]) -> str:
    req = urllib.request.Request(
        cfg["url"],
        data=_body(uid, start, end).encode(),
        headers={"Content-Type": "application/json;charset=UTF-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=int(cfg.get("timeout_seconds", 60))) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        hint = "（请检查数据源的访问权限和网络配置）" if e.code == 403 else ""
        raise FetchError(f"HTTP {e.code}{hint}") from e


def request(uid: str, start: str, end: str, cfg: dict[str, Any]) -> dict:
    """请求数据源并校验返回，不落盘。"""
    if not cfg.get("url"):
        raise FetchError("请先通过 AES_FETCH_URL 或 fetch.url 配置数据源地址")
    mode = cfg.get("mode", "direct")
    text = fetch_docker(uid, start, end, cfg) if mode == "docker" else fetch_direct(uid, start, end, cfg)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise FetchError(f"返回不是 JSON：{text[:200]}") from e
    if data.get("code") != 0:
        raise FetchError(f"接口返回错误：code={data.get('code')} msg={data.get('msg')}")
    return data


def fetch(uid: str, start: str, end: str, cfg: dict[str, Any]) -> tuple[RawResponse, Path]:
    """请求并保存到 raw_dir（供离线清洗）。"""
    data = request(uid, start, end, cfg)
    raw_dir = Path(cfg.get("raw_dir", "data/raw"))
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{uid}_{start}_{end}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)  # 原子替换，看板读取时不会读到半个文件
    return RawResponse.model_validate(data), path


def load_raw(path: str | Path) -> RawResponse:
    return RawResponse.model_validate_json(Path(path).read_text(encoding="utf-8"))
