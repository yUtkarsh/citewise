"""Strip footnote markers that a table grid glued onto numbers, in a graph built before
Pass 1 knew about them (no API calls): for every body row, the PDF's lines inside the
row's box are checked for small raised markers right after a digit; a cell ending with
that number plus the marker loses the marker, which is kept in ``cell_markers`` and the
row's ``markers``.

    python -m scripts.fix_cell_markers --code fema356 [--write]

Without ``--write`` it only reports. Re-extract the tables it lists afterwards
(``python -m app.ingest.run <pdf> --pass2 --jobs tables --pages ...``): the pinned
answers of those tables now fail the ``footnote_in_value`` check, so they are asked
again, and a new answer replaces the pin only if it passes.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import sys

import pymupdf

from app.ingest import text_checks as tc
from app.ingest.pass1.tables import _centre_in
from app.ingest.pdf_lines import read_lines


def find(graph, doc, marker_regex: str) -> list[dict]:
    rows = graph.ro_query("MATCH (t:Table)-[:HAS_ROW]->(r:Row) WHERE NOT r.is_header "
                          "RETURN t.id, r.id, r.page, r.bbox, r.cells, r.markers ORDER BY r.page, r.id").result_set
    lines: dict[int, list] = {}
    out = []
    for tid, rid, page, bbox, cells, markers in rows:
        if not page or not bbox:
            continue
        if page not in lines:
            lines[page] = read_lines(doc[page - 1])
        found = [x for line in lines[page] if _centre_in(line.bbox, bbox)
                 for x in tc.value_superscripts(line, marker_regex)]
        old = json.loads(cells or "[]")
        new, by_col = tc.strip_value_markers(old, found)
        if by_col:
            out.append({"table": tid, "row": rid, "page": page, "cells": new, "cell_markers": by_col,
                        "changes": [f"col {c}: {old[c]!r} -> {new[c]!r} (footnote {', '.join(m)})"
                                    for c, m in by_col.items()],
                        "markers": sorted(set(markers or []) | {m for ms in by_col.values() for m in ms})})
    return out


def write(graph, fixes: list[dict]) -> None:
    graph.query("UNWIND $rows AS r MATCH (x:Row {id: r.id}) SET x.cells = r.cells, x.cell_markers = r.cm, "
                "x.markers = r.markers",
                {"rows": [{"id": f["row"], "cells": json.dumps(f["cells"], ensure_ascii=False),
                           "cm": json.dumps({str(c): m for c, m in f["cell_markers"].items()}),
                           "markers": f["markers"]} for f in fixes]})


def main(argv: list[str]) -> int:
    from app.graph.connection import get_db
    from app.api.review_routes import _document_hash, _pdf_for
    from app.profile.build import load_profile, profile_path

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--code", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    name = f"code_{args.code}"
    graph = get_db().select_graph(name)
    pdf_hash = _document_hash(graph)
    pdf, profile = _pdf_for(name, pdf_hash), load_profile(profile_path(pdf_hash))
    fixes = find(graph, pymupdf.open(pdf), profile.footnote.marker_regex)
    tables: dict[str, list[int]] = defaultdict(list)
    for f in fixes:
        tables[f["table"]].append(f["page"])
        print(f"{f['row']} (p. {f['page']}): " + "; ".join(f["changes"]))
    print(f"{sum(len(f['cell_markers']) for f in fixes)} cells in {len(fixes)} rows of {len(tables)} tables: "
          + ", ".join(sorted(tables)))
    print("pages: " + ",".join(str(p) for p in sorted({p for ps in tables.values() for p in ps})))
    if args.write and fixes:
        write(graph, fixes)
        print("written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
