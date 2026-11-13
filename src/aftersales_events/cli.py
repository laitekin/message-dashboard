"""命令行入口：aes fetch | preview | run | eval"""

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
    print(f"结果：{out / 'events.jsonl'}，报告：{out / 'report.md'}")


def cmd_eval(args, settings) -> None:
    from .evaluate import evaluate, format_report, load_jsonl

    res = evaluate(load_jsonl(args.pred), load_jsonl(args.gold))
    print(format_report(res))


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

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args.func(args, load_settings(args.config))


if __name__ == "__main__":
    main()
