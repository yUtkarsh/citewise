"""Write a Pass 1 result to FalkorDB: indexes first, then batched UNWIND ... MERGE,
so re-running never duplicates nodes or edges."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json

from app.graph.schema import FULLTEXT, INDEXES, provenance
from app.ingest.pass1.model import Clause
from app.ingest.pass1.run_pass1 import Pass1Result
from app.profile.schema import Profile

BATCH = 500


@dataclass
class GraphRows:
    nodes: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    # (src label, type, dst label) -> [{"src", "dst", "props"}]
    edges: dict[tuple[str, str, str], list[dict]] = field(default_factory=lambda: defaultdict(list))

    def node(self, label: str, props: dict) -> None:
        self.nodes[label].append(props)

    def edge(self, src: tuple[str, str], kind: str, dst: tuple[str, str], props: dict | None = None) -> None:
        self.edges[(src[0], kind, dst[0])].append({"src": src[1], "dst": dst[1], "props": props or {}})


def _clause_props(c: Clause, unresolved: list[str]) -> dict:
    return {
        "id": c.id, "number": c.number, "stream": c.stream, "kind": c.kind, "title": c.title,
        "text": c.text, "depth": c.depth, "page_end": c.page_end, "order": c.order,
        "unresolved_refs": unresolved[:50],
        "regions": json.dumps([[page, *[round(float(v), 1) for v in box]] for page, box in c.regions]),
        "suspect_spans": json.dumps(c.suspect_spans, ensure_ascii=False),
        **provenance(c.page, c.bbox, f"{c.number} {c.title}", "pymupdf"),
    }


def build_rows(result: Pass1Result, profile: Profile, doc_id: str, file_hash: str) -> GraphRows:
    g = GraphRows()
    g.node("Document", {
        "id": doc_id, "title": profile.title, "edition": profile.edition, "jurisdiction": profile.jurisdiction,
        "self_designation": profile.self_designation, "file_hash": file_hash, "page_count": profile.page_count,
        "profile_version": profile.schema_version, "coverage": round(result.coverage, 4),
        "ranges": json.dumps([r.model_dump() for r in profile.ranges]),
        "low_coverage": json.dumps(result.low_coverage),
        **provenance(None, None, profile.title, "profile"),
    })
    clause_ids = {c.id for c in result.clauses}
    unresolved = defaultdict(list)
    for ref in result.references:
        if ref.target_id is None:
            unresolved[ref.source_id].append(ref.text)

    previous = None
    for c in result.clauses:
        g.node("Clause", _clause_props(c, unresolved[c.id]))
        parent = c.parent_id if c.parent_id in clause_ids else None
        g.edge(("Clause", c.id), "PART_OF", ("Clause", parent) if parent else ("Document", doc_id))
        if c.commentary_on and c.commentary_on in clause_ids:
            g.edge(("Clause", c.id), "COMMENTARY_ON", ("Clause", c.commentary_on), {"method": "number_prefix"})
        if previous:
            g.edge(("Clause", previous), "NEXT", ("Clause", c.id))
        previous = c.id

    for t in result.tables:
        first = t.segments[0]
        g.node("Table", {
            "id": t.id, "number": t.number, "caption": t.caption, "pages": t.pages, "n_rows": len(t.rows),
            "n_cols": len(first.col_x), "crops": [s.crop for s in t.segments], "header_crop": t.header_crop,
            "merge_conflict": t.merge_conflict, "grid_method": [s.method for s in t.segments],
            **provenance(first.page, first.bbox, t.caption, first.method, 0.5 if t.merge_conflict else 1.0),
        })
        if t.clause_id:
            g.edge(("Clause", t.clause_id), "CONTAINS", ("Table", t.id))
        fn_ids = {}
        for n, f in enumerate(t.footnotes):
            fid = f"{t.id}#fn{f.marker}" if f.marker not in fn_ids else f"{t.id}#fn{f.marker}~{n}"
            fn_ids.setdefault(f.marker, fid)
            g.node("Footnote", {"id": fid, "marker": f.marker, "text": f.text,
                                **provenance(f.page, f.bbox, f.text, "pymupdf")})
            g.edge(("Table", t.id), "HAS_FOOTNOTE", ("Footnote", fid))
        method_on = {s.page: s.method for s in t.segments}
        for row in t.rows:
            rid = f"{t.id}#r{row.index}"
            g.node("Row", {"id": rid, "index": row.index, "cells": json.dumps(row.cells, ensure_ascii=False),
                           "is_header": row.is_header, "markers": row.markers,
                           "cell_markers": json.dumps({str(c): m for c, m in row.cell_markers.items()}),
                           "grid_method": method_on.get(row.page, first.method),
                           **provenance(row.page, row.bbox, " | ".join(c for c in row.cells if c), first.method)})
            g.edge(("Table", t.id), "HAS_ROW", ("Row", rid), {"index": row.index})
            for marker in row.markers:
                if marker in fn_ids:
                    g.edge(("Row", rid), "FOOTNOTE", ("Footnote", fn_ids[marker]))

    for e in result.equations:
        g.node("Equation", {"id": e.id, "number": e.number, "raw_text": e.raw_text,
                            "raw_spans": json.dumps(e.raw_spans, ensure_ascii=False), "crop": e.crop,
                            **provenance(e.page, e.bbox, e.raw_text, "pymupdf")})
        if e.clause_id:
            g.edge(("Clause", e.clause_id), "CONTAINS", ("Equation", e.id))
    equation_ids = {e.id for e in result.equations}
    counts = defaultdict(int)
    for w in result.where_items:
        if not w.owner_id:
            continue
        counts[w.owner_id] += 1
        wid = f"{w.owner_id}#w{counts[w.owner_id]}"
        g.node("WhereItem", {"id": wid, "symbol": w.symbol, "definition": w.definition, "unit": w.unit,
                             **provenance(w.page, w.bbox, f"{w.symbol} = {w.definition}", "pymupdf")})
        owner_label = "Equation" if w.owner_id in equation_ids else "Clause"
        g.edge((owner_label, w.owner_id), "WHERE", ("WhereItem", wid))

    for f in result.figures:
        g.node("Figure", {"id": f.id, "number": f.number, "caption": f.caption, "crop": f.crop,
                          **provenance(f.page, f.bbox, f.caption, "pymupdf")})
        if f.clause_id:
            g.edge(("Clause", f.clause_id), "CONTAINS", ("Figure", f.id))

    labels = {"clause": "Clause", "table": "Table", "figure": "Figure", "equation": "Equation"}
    externals = set()
    for ref in result.references:
        if ref.target_id is None or ref.source_id not in clause_ids:
            continue
        if ref.kind == "external":
            if ref.target_id not in externals:
                externals.add(ref.target_id)
                g.node("ExternalCode", {"id": ref.target_id, "name": ref.target_id,
                                        **provenance(ref.page, None, ref.text, "regex")})
            g.edge(("Clause", ref.source_id), "REFERENCES", ("ExternalCode", ref.target_id),
                   {"text": ref.text, "method": ref.method, "type": "external"})
        else:
            g.edge(("Clause", ref.source_id), "REFERENCES", (labels[ref.kind], ref.target_id),
                   {"text": ref.text, "method": ref.method})

    for fl in result.flagged:
        g.node("FlaggedLine", {"id": fl.id, "text": fl.text, "reasons": fl.reasons,
                               **provenance(fl.page, fl.bbox, fl.text, "pymupdf")})
        if fl.clause_id:
            g.edge(("Clause", fl.clause_id), "HAS_FLAG", ("FlaggedLine", fl.id))
    return g


def ensure_indexes(graph) -> None:
    for statement in INDEXES + [FULLTEXT]:
        try:
            graph.query(statement)
        except Exception as exc:  # index already exists
            if "already" not in str(exc).lower():
                raise


def _batches(rows: list[dict]):
    for i in range(0, len(rows), BATCH):
        yield rows[i:i + BATCH]


def write(graph, rows: GraphRows, indexes: bool = True) -> dict[str, int]:
    """MERGE every node by id and every edge by (src, type, dst). Returns counts written."""
    if indexes:
        ensure_indexes(graph)
    counts = {}
    for label, nodes in rows.nodes.items():
        for batch in _batches(nodes):
            graph.query(f"UNWIND $rows AS r MERGE (n:{label} {{id: r.id}}) SET n += r", {"rows": batch})
        counts[label] = len(nodes)
    for (src, kind, dst), edges in rows.edges.items():
        query = (f"UNWIND $rows AS r MATCH (a:{src} {{id: r.src}}) MATCH (b:{dst} {{id: r.dst}}) "
                 f"MERGE (a)-[e:{kind}]->(b) SET e += r.props")
        for batch in _batches(edges):
            graph.query(query, {"rows": batch})
        counts[kind] = counts.get(kind, 0) + len(edges)
    return counts
