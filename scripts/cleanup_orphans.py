"""Delete Parameter nodes left over from re-extractions: no edges at all, and created by a
clause, equation or table extraction (``source`` clause / equation / table). Re-extracting a
clause removes its DEFINES edges but keeps the Parameter nodes it had merged, so a symbol
the new answer no longer defines stays behind with nothing pointing to it.

Notation entries (source notation) and Terms are kept even when nothing links to them:
they are the document's own dictionary and glossary.

    python -m scripts.cleanup_orphans --code fema356 [--dry-run]
"""
from __future__ import annotations

import argparse
import sys

from app.graph.connection import get_db

LEFTOVER_SOURCES = ["clause", "equation", "table"]
MATCH = "MATCH (p:Parameter) WHERE NOT (p)--() AND p.source IN $sources"


def orphans(graph) -> list[tuple[str, str]]:
    return [tuple(r) for r in graph.ro_query(f"{MATCH} RETURN p.id, p.source ORDER BY p.id",
                                             {"sources": LEFTOVER_SOURCES}).result_set]


def counts(graph) -> dict:
    rows = graph.ro_query("MATCH (n) WHERE NOT (n)--() RETURN labels(n)[0], coalesce(n.source, ''), count(*)").result_set
    return {f"{label}{'/' + source if source else ''}": n for label, source, n in rows}


def cleanup(graph, dry_run: bool = False) -> dict:
    found = orphans(graph)
    before = counts(graph)
    if not dry_run and found:
        graph.query(f"{MATCH} DELETE p", {"sources": LEFTOVER_SOURCES})
    return {"leftover_parameters": len(found), "deleted": 0 if dry_run else len(found), "ids": [i for i, _ in found],
            "orphans_before": before, "orphans_after": before if dry_run else counts(graph)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--code", required=True, help="code name, e.g. fema356 (graph code_fema356)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be deleted; delete nothing")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    report = cleanup(get_db().select_graph(f"code_{args.code}"), args.dry_run)
    print(f"leftover parameters: {report['leftover_parameters']}, deleted: {report['deleted']}")
    print(f"nodes with no edges before: {report['orphans_before']}")
    print(f"nodes with no edges after:  {report['orphans_after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
