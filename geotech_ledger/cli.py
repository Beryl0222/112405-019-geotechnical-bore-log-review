"""命令行入口：读取资料包，输出 JSON 台账与 Markdown 交接报告。

用法：
    python3 -m geotech_ledger.cli data/case.json --outdir out
    python3 -m geotech_ledger.cli data/case.json --events out/events.jsonl
    python3 -m geotech_ledger.cli data/case.json --trace-sample S06
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # 直接以脚本方式运行（python -I cli.py ...）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from geotech_ledger.pipeline import run_pipeline
    from geotech_ledger.reports import build_report, render_markdown
    from geotech_ledger.trace import trace_depth, trace_elevation, trace_sample
else:
    from .pipeline import run_pipeline
    from .reports import build_report, render_markdown
    from .trace import trace_depth, trace_elevation, trace_sample


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="钻探层位证据台账（合成演练）")
    parser.add_argument("case_path", help="资料包 JSON 路径")
    parser.add_argument("--outdir", help="报告与台账输出目录")
    parser.add_argument("--events", help="事件流 JSONL 输出路径")
    parser.add_argument("--trace-sample", help="回查样品编号（含旧编号）")
    parser.add_argument("--trace-depth", nargs=2, metavar=("HOLE", "DEPTH"), help="按深度回查")
    parser.add_argument("--trace-elevation", nargs=2, metavar=("HOLE", "ELEV"), help="按标高回查")
    args = parser.parse_args(argv)

    case = json.loads(Path(args.case_path).read_text(encoding="utf-8"))
    result = run_pipeline(case)

    if args.outdir:
        Path(args.outdir).mkdir(parents=True, exist_ok=True)
    if args.events:
        events_path = Path(args.events)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        result.ledger.save_events(events_path)
    if args.outdir:
        out = Path(args.outdir)
        report = build_report(result, case)
        (out / "ledger.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out / "handoff.md").write_text(render_markdown(report), encoding="utf-8")
        print(f"已写出: {out/'ledger.json'}, {out/'report.json'}, {out/'handoff.md'}")

    if args.trace_sample:
        print(json.dumps(trace_sample(result.ledger, args.trace_sample), ensure_ascii=False, indent=2))
    if args.trace_depth:
        hole, depth = args.trace_depth
        print(json.dumps(trace_depth(result.ledger, hole, float(depth)), ensure_ascii=False, indent=2))
    if args.trace_elevation:
        hole, elev = args.trace_elevation
        print(json.dumps(trace_elevation(result.ledger, hole, float(elev)), ensure_ascii=False, indent=2))

    if not any([args.outdir, args.events, args.trace_sample, args.trace_depth, args.trace_elevation]):
        report = build_report(result, case)
        print(render_markdown(report))
    if result.warnings:
        print("\n[录入提示]", file=sys.stderr)
        for w in result.warnings:
            print(f"- {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
