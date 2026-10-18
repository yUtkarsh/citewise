"""Run the global pass on one code graph (SPEC Step 3c).

    python -m app.ingest.global_pass --code fema356 [--parts checks,commentary,inferred,gives,resolve]
                                     [--dry-run] [--max-cost 4.0] [--concurrency 10]

``checks`` makes no API calls. The other parts make one Claude call per item; ``--dry-run``
prints the number of calls, how many are cached, and an estimated cost, and calls nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import ROOT

PARTS = ("checks", "commentary", "inferred", "gives", "resolve")
REPORT_DIR = ROOT / "data" / "reports"


def main(argv: list[str]) -> int:
    from app.graph.connection import get_db
    from app.ingest.global_pass.run import GlobalOptions, run_global

    parser = argparse.ArgumentParser(description="Global pass on one code graph")
    parser.add_argument("--code", required=True, help="code name, e.g. fema356 (graph code_fema356)")
    parser.add_argument("--parts", default=",".join(PARTS), help=f"comma list of {', '.join(PARTS)}")
    parser.add_argument("--dry-run", action="store_true", help="estimate calls and cost; call nothing, write nothing")
    parser.add_argument("--max-cost", type=float, default=4.0, help="USD cap for all Claude calls of this run")
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args(argv)
    parts = [p.strip() for p in args.parts.split(",") if p.strip()]
    unknown = sorted(set(parts) - set(PARTS))
    if unknown:
        print(f"unknown parts: {', '.join(unknown)}")
        return 2
    graph = get_db().select_graph(f"code_{args.code}")
    report = asyncio.run(run_global(graph, GlobalOptions(parts=parts, dry_run=args.dry_run, max_cost=args.max_cost,
                                                         concurrency=args.concurrency), log=print))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{args.code}_global{'_dryrun' if args.dry_run else ''}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
