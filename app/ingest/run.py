"""Ingest one PDF: profile (built or loaded), then Pass 1 into FalkorDB.

Usage (PowerShell):
    python -m app.ingest.run data/fema356.pdf --pass1 [--fresh] [--reprofile]
                             [--profile PATH] [--no-docling] [--no-write]
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

import pymupdf

from app.config import ROOT
from app.graph.connection import get_db
from app.graph.schema import graph_name
from app.graph.writers import build_rows, write
from app.ingest.pass1.run_pass1 import Pass1Result, run_pass1
from app.llm import CACHE_DIR
from app.profile.build import build_profile, file_hash, load_profile, profile_path, save_profile
from app.profile.page_map import build_page_map
from app.profile.schema import Profile

REPORT_DIR = ROOT / "data" / "reports"
CROPS_DIR = ROOT / "data" / "crops"


def get_profile(doc: pymupdf.Document, pdf_hash: str, args, page_map, log) -> Profile:
    if args.profile:
        log(f"profile: loaded {args.profile}")
        return load_profile(Path(args.profile))
    path = profile_path(pdf_hash)
    if path.is_file() and not args.reprofile:
        log(f"profile: loaded {path.relative_to(ROOT)}")
        return load_profile(path)
    profile, _ = build_profile(doc, page_map, log=log)
    save_profile(profile, path)
    log(f"profile: saved {path.relative_to(ROOT)}")
    return profile


def summary(result: Pass1Result, profile: Profile, counts: dict[str, int] | None, seconds: float) -> dict:
    refs = result.references
    internal = [r for r in refs if r.kind != "external"]
    reasons = Counter(r.split(":")[0] for f in result.flagged for r in f.reasons)
    flagged_pages = Counter(f.page for f in result.flagged)
    v = profile.validation
    return {
        "document": {"title": profile.title, "edition": profile.edition, "jurisdiction": profile.jurisdiction,
                     "pages": profile.page_count, "segmented_pages": len(profile.segmented_pages())},
        "profile_validation": v.model_dump(exclude={"issues"}) if v else None,
        "clauses_per_stream": dict(Counter(c.stream for c in result.clauses)),
        "clauses_per_depth": dict(sorted(Counter(c.depth for c in result.clauses).items())),
        "commentary_linked": sum(1 for c in result.clauses if c.commentary_on
                                 and c.commentary_on in {x.id for x in result.clauses}),
        "commentary_unmatched": sum(1 for c in result.clauses if c.commentary_on
                                    and c.commentary_on not in {x.id for x in result.clauses}),
        "tables": len(result.tables),
        "tables_multi_page": sum(1 for t in result.tables if len(set(t.pages)) > 1),
        "table_merge_conflicts": sum(t.merge_conflict for t in result.tables),
        "table_grid_methods": dict(result.table_methods),
        "uncaptioned_grids_skipped": result.skipped_grids,
        "table_rows": sum(len(t.rows) for t in result.tables),
        "equations": len(result.equations),
        "where_items": len(result.where_items),
        "figures": len(result.figures),
        "references_resolved": sum(1 for r in internal if r.target_id),
        "references_unresolved": sum(1 for r in internal if not r.target_id),
        "references_by_method": dict(Counter(r.method for r in internal if r.target_id)),
        "external_codes": len({r.target_id for r in refs if r.kind == "external"}),
        "flagged_lines": len(result.flagged),
        "flagged_by_reason": dict(reasons),
        "flagged_top_pages": flagged_pages.most_common(8),
        "coverage": round(result.coverage, 4),
        "low_coverage_ranges": result.low_coverage,
        "graph_writes": counts,
        "timings_s": {**result.timings, "total": round(seconds, 1)},
    }


def print_summary(s: dict) -> None:
    d = s["document"]
    print("\n" + "=" * 72)
    print(f"{d['title']} | {d['edition']} | {d['jurisdiction']}")
    print(f"pages {d['pages']}, segmented {d['segmented_pages']}")
    v = s["profile_validation"]
    if v:
        print(f"profile validation: {v['status']} (coverage {v['coverage']:.1%}, {v['heading_count']} headings, "
              f"order {v['order_violations']}, hierarchy {v['hierarchy_violations']}, revised {v['revised']})")
    print(f"clauses per stream: {s['clauses_per_stream']}   per depth: {s['clauses_per_depth']}")
    print(f"commentary linked to provisions: {s['commentary_linked']} (unmatched {s['commentary_unmatched']})")
    print(f"tables: {s['tables']} ({s['tables_multi_page']} multi-page, {s['table_merge_conflicts']} merge conflicts, "
          f"{s['table_rows']} rows, grids {s['table_grid_methods']}, uncaptioned grids skipped {s['uncaptioned_grids_skipped']})")
    print(f"equations: {s['equations']} (where-items {s['where_items']})   figures: {s['figures']}")
    print(f"references: {s['references_resolved']} resolved, {s['references_unresolved']} unresolved "
          f"{s['references_by_method']}; external codes {s['external_codes']}")
    print(f"flagged lines: {s['flagged_lines']} {s['flagged_by_reason']}  top pages {s['flagged_top_pages']}")
    print(f"coverage: {s['coverage']:.1%}   low-coverage pages: {s['low_coverage_ranges']}")
    if s["graph_writes"]:
        print(f"graph writes: {s['graph_writes']}")
    print(f"timings (s): {s['timings_s']}")
    print("=" * 72)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m app.ingest.run")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--pass1", action="store_true", help="run Pass 1 (the skeleton)")
    parser.add_argument("--fresh", action="store_true", help="delete the document's graph first")
    parser.add_argument("--reprofile", action="store_true", help="rebuild the profile even if one is saved")
    parser.add_argument("--profile", help="use this profile JSON instead of building one")
    parser.add_argument("--no-docling", action="store_true", help="find_tables() only (fast)")
    parser.add_argument("--no-write", action="store_true", help="skip FalkorDB writes")
    args = parser.parse_args(argv)

    start = time.time()
    log = lambda msg: print(msg, flush=True)  # noqa: E731
    pdf = args.pdf.resolve()
    pdf_hash = file_hash(pdf)
    doc = pymupdf.open(pdf)
    page_map = build_page_map(doc)
    log(f"page map: {doc.page_count} pages")
    profile = get_profile(doc, pdf_hash, args, page_map, log)
    if not args.pass1:
        return 0

    name = graph_name(pdf)
    result = run_pass1(doc, pdf, pdf_hash, profile, page_map, CACHE_DIR, CROPS_DIR / name,
                       use_docling=not args.no_docling, log=log)
    counts = None
    if not args.no_write:
        graph = get_db().select_graph(name)
        if args.fresh:
            try:
                graph.delete()
            except Exception:  # graph did not exist yet
                pass
            graph = get_db().select_graph(name)
        counts = write(graph, build_rows(result, profile, name, pdf_hash))
        log(f"graph: wrote {name}")
    report = summary(result, profile, counts, time.time() - start)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{pdf_hash[:12]}_pass1.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print_summary(report)
    print(f"report: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
