"""命令行入口：aes fetch | preview | run | eval | board | serve"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import load_settings
from .fetch import fetch, load_raw


def _out_dir(base: str | None, uid: str) -> Path:
    return Path(base or f"out/{uid}_{datetime.now():%Y%m%d_%H%M%S}")


def cmd_fetch(args, settings) -> None:
    raw, path = fetch(args.uid, args.start, args.end or args.start, settings.section("fetch"))
    print(f"已保存 {path}：{len(raw.groups)} 个群，{raw.total} 条消息")


def _load_input(args, settings):
    if args.input:
        return load_raw(args.input)
    if not args.uid:
        sys.exit("需要 --input 或 --uid/--start")
    raw, path = fetch(args.uid, args.start, args.end or args.start, settings.section("fetch"))
    print(f"已拉取并保存 {path}")
    return raw


def cmd_preview(args, settings) -> None:
    """只跑规则层 + 切分，输出窗口 prompt，不调模型。"""
    from .extract import DryRunExtractor
    from .pipeline import run

    raw = _load_input(args, settings)
    out = _out_dir(args.out, raw.uid or "preview")
    res = run(raw, settings, DryRunExtractor(settings.enabled_types, out / "prompts"), out_dir=None)
    print(json.dumps(res.stats["preprocess"], ensure_ascii=False))
    print(f"窗口数：{len(res.windows)}；prompt 已写入 {out / 'prompts'}")


def cmd_run(args, settings) -> None:
    from .extract import ClaudeExtractor
    from .pipeline import run

    raw = _load_input(args, settings)
    out = _out_dir(args.out, raw.uid or "run")
    res = run(raw, settings, ClaudeExtractor(settings.enabled_types, settings.section("llm")), out_dir=out)
    print(json.dumps(res.stats, ensure_ascii=False, indent=2))
    print(f"结果：{out / 'events.jsonl'}，报告：{out / 'report.md'}，看板：{out / 'board.html'}")


def cmd_eval(args, settings) -> None:
    from .evaluate import evaluate, format_report, load_jsonl

    res = evaluate(load_jsonl(args.pred), load_jsonl(args.gold))
    print(format_report(res))


def cmd_board(args, settings) -> None:
    """把一个或多个 events.jsonl（或包含它们的目录）合成一个 HTML 看板。"""
    from .board import find_event_files, load_events, write_board
    from .preprocess import preprocess

    files = find_event_files(args.paths)
    if not files:
        sys.exit(f"在 {' '.join(args.paths)} 下没有找到 events.jsonl")
    events = load_events(files)
    messages = []
    for path in args.input or []:
        msgs, _ = preprocess(load_raw(path), settings.enabled_types, settings.section("preprocess"))
        messages.extend(msgs)
    out = write_board(args.out, events, settings.enabled_types, messages if args.input else None, title=args.title)
    print(f"看板已生成 {out}：{len(events)} 个事件（来自 {len(files)} 个文件）")


def cmd_serve(args, settings) -> None:
    from .serve import BoardApp, make_server

    app = BoardApp(settings, args.out_dir, args.raw_dir, args.state, title=args.title)
    server = make_server(app, args.host, args.port)
    print(f"看板服务已启动：http://{args.host}:{server.server_address[1]}/（事件目录 {args.out_dir}，修改记录 {args.state}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="aes", description="售后事件清洗")
    p.add_argument("--config", help="配置目录（默认 ./config）")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_src(sp):
        sp.add_argument("--input", help="已保存的原始 JSON 文件")
        sp.add_argument("--uid")
        sp.add_argument("--start", help="YYYY-MM-DD")
        sp.add_argument("--end", help="YYYY-MM-DD（默认同 start）")
        sp.add_argument("--out", help="输出目录")

    sp = sub.add_parser("fetch", help="拉取原始聊天记录到 data/raw/")
    sp.add_argument("--uid", required=True)
    sp.add_argument("--start", required=True)
    sp.add_argument("--end")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("preview", help="只跑预处理+切分，导出 prompt（不调模型）")
    add_src(sp)
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("run", help="完整流水线：抽取事件")
    add_src(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("eval", help="与人工标注对比")
    sp.add_argument("--pred", required=True)
    sp.add_argument("--gold", required=True)
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("board", help="生成 HTML 事件看板")
    sp.add_argument("paths", nargs="*", default=["out"], help="events.jsonl 文件或包含它们的目录（默认 out/）")
    sp.add_argument("--input", action="append", help="原始 JSON，用于在看板里显示证据消息原文（可多次指定）")
    sp.add_argument("--out", default="out/board.html", help="输出文件（默认 out/board.html）")
    sp.add_argument("--title", default="售后事件看板")
    sp.set_defaults(func=cmd_board)

    sp = sub.add_parser("serve", help="启动看板服务（多人共享修改）")
    sp.add_argument("--out-dir", default="out", help="扫描其中所有 events.jsonl（默认 out/）")
    sp.add_argument("--raw-dir", default="data/raw", help="原始 JSON 目录，用于显示证据原文（默认 data/raw/）")
    sp.add_argument("--state", default="data/board_state.json", help="人工修改与修改记录的保存位置")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8470)
    sp.add_argument("--title", default="售后事件看板")
    sp.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args.func(args, load_settings(args.config))


if __name__ == "__main__":
    main()
