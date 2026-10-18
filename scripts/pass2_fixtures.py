"""Pass 2 checkpoint on the public fixture pages (SPEC Build order, Phase 3).

Joins the fixture pages into one PDF, runs Pass 1 into the graph ``code_pass2_fixtures``,
then Pass 2 with real Claude calls in the given CLAUSE_CROPS mode. Uses the committed
FEMA 356 profile with page ranges set for the joined file.

    python -m scripts.pass2_fixtures --clause-crops always [--dry-run] [--docling]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import pymupdf

from app.config import ROOT
from app.graph.connection import get_db
from app.graph.writers import build_rows, write
from app.ingest.pass1.run_pass1 import run_pass1
from app.ingest.pass2.records import from_graph
from app.ingest.pass2.run_pass2 import GraphSink, Pass2Options, run_pass2
from app.llm import CACHE_DIR
from app.profile.build import file_hash
from app.profile.page_map import build_page_map
from app.profile.schema import PageRange, Profile

FIXTURES = ROOT / "fixtures"
BODY = ["text.pdf", "commentary.pdf", "equations.pdf", "equations_brackets.pdf", "table.pdf",
        "table_multi_1.pdf", "table_multi_2.pdf"]
OUT = ROOT / "data" / "fixture_run"
GRAPH = "code_pass2_fixtures"


def joined_pdf() -> pymupdf.Document:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "fixtures.pdf"
    if path.is_file():  # reuse it: a re-saved PDF gets a new hash, which would miss the cache
        return pymupdf.open(path)
    out = pymupdf.open()
    for name in BODY + ["notation.pdf", "definitions.pdf"]:
        with pymupdf.open(FIXTURES / name) as src:
            out.insert_pdf(src)
    out.save(path)
    return pymupdf.open(path)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m scripts.pass2_fixtures")
    parser.add_argument("--clause-crops", choices=["always", "flagged"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--docling", action="store_true", help="build table grids with Docling (slow)")
    args = parser.parse_args(argv)

    doc = joined_pdf()
    pdf = OUT / "fixtures.pdf"
    pdf_hash = file_hash(pdf)
    n = len(BODY)
    profile = Profile.model_validate_json((FIXTURES / "fema356_profile.json").read_text(encoding="utf-8"))
    profile = profile.model_copy(update={"ranges": [
        PageRange(start=1, end=n, kind="body"), PageRange(start=n + 1, end=n + 1, kind="notation"),
        PageRange(start=n + 2, end=n + 2, kind="definitions")]})
    result = run_pass1(doc, pdf, pdf_hash, profile, build_page_map(doc), CACHE_DIR, ROOT / "data" / "crops" / GRAPH,
                       use_docling=args.docling, pages=list(range(1, n + 1)), log=print)
    graph = get_db().select_graph(GRAPH)
    try:
        graph.delete()
    except Exception:
        pass
    graph = get_db().select_graph(GRAPH)
    write(graph, build_rows(result, profile, GRAPH, pdf_hash))
    skel = from_graph(graph, GRAPH)
    print(f"pass 1: {len(skel.clauses)} clauses, {len(skel.tables)} tables, "
          f"{sum(len(c.flagged) for c in skel.clauses)} flagged lines")
    opts = Pass2Options(crop_mode=args.clause_crops, dry_run=args.dry_run, redo=True)
    report = asyncio.run(run_pass2(doc, pdf_hash, profile, skel, None if args.dry_run else GraphSink(graph), opts,
                                   OUT / f"state_{args.clause_crops}.jsonl"))
    out = ROOT / "data" / "reports" / f"pass2_fixtures_{args.clause_crops}{'_dryrun' if args.dry_run else ''}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"report: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
