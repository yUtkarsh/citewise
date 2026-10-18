"""Write Pass 2 results to FalkorDB, one job at a time, as each result is validated.

Writes are ``UNWIND ... MERGE`` with deterministic child ids, and a re-extraction first
removes the clause's or table's previous Pass 2 children, so re-runs never duplicate and
never leave stale facts. Parameters are shared nodes: notation pages set them; a clause
or table only creates the ones the notation lacks (``ON CREATE``).
"""
from __future__ import annotations

import json

from app.graph.schema import GRAPH_SCHEMA_VERSION, QUOTE_CHARS
from app.graph.writers import GraphRows, write
from app.ingest.pass2.notation import term_id
from app.ingest.pass2.runner import Outcome
from app.ingest.pass2.schemas import PASS2_SCHEMA_VERSION, ClauseExtraction, NotationEntry, TableExtraction, TermEntry
from app.ingest.pass2.validate import check_clause, norm_text


def _flags(outcome: Outcome) -> list[str]:
    return [json.dumps(i.as_dict(), ensure_ascii=False) for i in outcome.issues]


def pass2_provenance(page, bbox, quote: str, model: str, status: str, confidence: float = 0.9) -> dict:
    return {
        "page": page,
        "bbox": [round(float(v), 1) for v in bbox] if bbox else None,
        "source_quote": (quote or "")[:QUOTE_CHARS],
        "confidence": confidence,
        "extraction_method": f"claude:{model}",
        "review_status": "unreviewed",
        "schema_version": GRAPH_SCHEMA_VERSION,
        "extraction_schema": PASS2_SCHEMA_VERSION,
        "status": status,
    }


def _model(outcome: Outcome) -> str:
    call = outcome.call
    return (call.usages[-1].get("model") if call and call.usages else None) or "cached"


def _merge_parameters(graph, rows: list[dict], overwrite: bool) -> None:
    if not rows:
        return
    action = "SET p += r" if overwrite else "ON CREATE SET p += r"
    graph.query(f"UNWIND $rows AS r MERGE (p:Parameter {{id: r.id}}) {action}", {"rows": rows})


# ---- notation and definitions -------------------------------------------------------------

def write_notation(graph, entries: list[tuple[NotationEntry, int, str]], defined_in: dict[str, list[str]]) -> None:
    """``entries``: (entry, page, status). ``defined_in``: parameter id -> clause ids."""
    rows = [{"id": e.id, "symbol": e.display, "latex": e.latex, "description": e.description, "units": e.units,
             "aliases": e.aliases, "scope_chapters": e.scope_chapters, "defined_in_text": e.defined_in,
             "source": "notation",
             **pass2_provenance(page, None, e.source_quote, "notation", status)}
            for e, page, status in entries]
    _merge_parameters(graph, rows, overwrite=True)
    g = GraphRows()
    for pid, clause_ids in defined_in.items():
        for cid in clause_ids:
            g.edge(("Parameter", pid), "DEFINED_IN", ("Clause", cid))
    write(graph, g, indexes=False)


def write_terms(graph, terms: list[tuple[TermEntry, int, str]], links: dict[str, list[str]]) -> None:
    g = GraphRows()
    for t, page, status in terms:
        tid = term_id(t.term)
        g.node("Term", {"id": tid, "term": t.term, "definition": t.definition, "aliases": t.aliases,
                        **pass2_provenance(page, None, t.source_quote, "definitions", status)})
        for cid in links.get(t.term, []):
            g.edge(("Clause", cid), "USES_TERM", ("Term", tid))
    write(graph, g, indexes=False)


# ---- clauses -------------------------------------------------------------------------------

CLAUSE_CHILD_EDGES = "HAS_CONDITION|HAS_REQUIREMENT|HAS_EXCEPTION|HAS_CORRECTION"


def clear_clause(graph, cid: str) -> None:
    graph.query(f"MATCH (:Clause {{id: $id}})-[:{CLAUSE_CHILD_EDGES}]->(n) DETACH DELETE n", {"id": cid})
    graph.query("MATCH (:Clause {id: $id})-[e:DEFINES]->() DELETE e", {"id": cid})
    graph.query("MATCH (:Clause {id: $id})-[:CONTAINS]->(q:Equation) WHERE q.inline = true DETACH DELETE q",
                {"id": cid})
    graph.query("MATCH (:Clause {id: $id})-[:CONTAINS]->(:Equation)-[e:COMPUTES|USES]->() DELETE e", {"id": cid})
    graph.query("MATCH (:Clause {id: $id})-[e:REFERENCES]->() SET e.type = null", {"id": cid})


def write_clause(graph, outcome: Outcome, notation_ids: set[str]) -> None:
    clause, model = outcome.job.clause, _model(outcome)
    status = outcome.status
    clear_clause(graph, clause.id)
    props = {"id": clause.id, "status": status, "flags": _flags(outcome), "extraction_schema": PASS2_SCHEMA_VERSION,
             "extraction_method": "claude:" + model if outcome.call else "pymupdf",
             "confidence": 0.5 if status == "flagged" else 0.9}
    result: ClauseExtraction | None = outcome.output
    g = GraphRows()
    if result is None:
        g.node("Clause", props)
        write(graph, g, indexes=False)
        return
    check = check_clause(result, clause, notation_ids)
    props |= {"text_corrected": check.corrected_text,
              "unmapped": json.dumps([u.model_dump() for u in result.unmapped], ensure_ascii=False)}
    g.node("Clause", props)

    prov = lambda quote: pass2_provenance(clause.page, None, quote, model, status)  # noqa: E731
    params = [{"id": p.id, "symbol": p.symbol, "latex": p.latex, "description": p.description, "units": p.units,
               "source": "clause", **prov(p.source_quote)} for p in result.parameters]
    _merge_parameters(graph, params, overwrite=False)
    for p in result.parameters:
        g.edge(("Clause", clause.id), "DEFINES", ("Parameter", p.id))

    equations = {e.id: e for e in clause.equations}
    inline = 0
    for eq in result.equations:
        if eq.equation_id in equations:
            src = equations[eq.equation_id]
            eid = src.id
            g.node("Equation", {"id": eid, "latex": eq.latex, "sympy": eq.sympy, "status": status,
                                "extraction_schema": PASS2_SCHEMA_VERSION, "extraction_method": "claude:" + model})
        else:
            inline += 1
            eid = f"{clause.id}#eq{inline}"
            g.node("Equation", {"id": eid, "number": None, "latex": eq.latex, "sympy": eq.sympy, "inline": True,
                                **prov(eq.source_quote)})
            g.edge(("Clause", clause.id), "CONTAINS", ("Equation", eid))
        missing = [{"id": pid, "symbol": pid, "source": "equation", **prov(eq.source_quote)}
                   for pid in [eq.computes, *eq.uses] if pid not in notation_ids]
        _merge_parameters(graph, missing, overwrite=False)
        g.edge(("Equation", eid), "COMPUTES", ("Parameter", eq.computes))
        for pid in dict.fromkeys(eq.uses):
            g.edge(("Equation", eid), "USES", ("Parameter", pid))

    for label, edge, tag, items in (
            ("Condition", "HAS_CONDITION", "cond", [{"text": c.text, "q": c.source_quote} for c in result.conditions]),
            ("Requirement", "HAS_REQUIREMENT", "req",
             [{"text": r.text, "modal": r.modal, "q": r.source_quote} for r in result.requirements]),
            ("Exception", "HAS_EXCEPTION", "exc", [{"text": x.text, "q": x.source_quote} for x in result.exceptions])):
        for n, item in enumerate(items, 1):
            nid = f"{clause.id}#{tag}{n}"
            quote = item.pop("q")
            g.node(label, {"id": nid, **item, **prov(quote)})
            g.edge(("Clause", clause.id), edge, (label, nid))

    for n, corr in enumerate(result.symbol_corrections, 1):
        line = next((f for f in clause.flagged if corr.raw in f.text), None)
        nid = f"{clause.id}#corr{n}"
        g.node("SymbolCorrection", {
            "id": nid, "raw": corr.raw, "corrected": corr.corrected, "uncertain": corr.uncertain, "note": corr.note,
            "line_id": line.id if line else None,
            **pass2_provenance(line.page if line else clause.page, line.bbox if line else None, corr.raw, model,
                               "flagged" if corr.uncertain else status)})
        g.edge(("Clause", clause.id), "HAS_CORRECTION", ("SymbolCorrection", nid))
    write(graph, g, indexes=False)

    by_text = {norm_text(r.text): r.text for r in clause.references}
    for ref in result.references:
        if text := by_text.get(norm_text(ref.text)):
            graph.query("MATCH (:Clause {id: $id})-[e:REFERENCES]->() WHERE e.text = $text SET e.type = $type",
                        {"id": clause.id, "text": text, "type": ref.type})


# ---- tables --------------------------------------------------------------------------------

def write_table(graph, outcome: Outcome) -> None:
    job, model, status = outcome.job, _model(outcome), outcome.status
    table = job.table
    part = int(job.id.rsplit("#part", 1)[1]) if "#part" in job.id else 1
    if part == 1:
        graph.query("MATCH (:Table {id: $id})-[e:GIVES]->() DELETE e", {"id": table.id})
    graph.query(
        "MATCH (t:Table {id: $id}) SET t.status = CASE WHEN $part > 1 AND t.status = 'flagged' THEN 'flagged' "
        "ELSE $status END, t.flags = CASE WHEN $part > 1 THEN coalesce(t.flags, []) + $flags ELSE $flags END, "
        "t.extraction_schema = $schema, t.extraction_method = $method",
        {"id": table.id, "part": part, "status": status, "flags": _flags(outcome), "schema": PASS2_SCHEMA_VERSION,
         "method": "claude:" + model})
    result: TableExtraction | None = outcome.output
    if result is None:
        return
    prov = lambda quote: pass2_provenance(table.pages[0] if table.pages else None, None, quote, model, status)  # noqa
    _merge_parameters(graph, [{"id": h.parameter_id, "symbol": h.header_text, "units": h.units, "source": "table",
                               **prov(h.header_text)} for h in result.header_parameters], overwrite=False)
    g = GraphRows()
    for h in result.header_parameters:
        g.edge(("Table", table.id), "GIVES", ("Parameter", h.parameter_id),
               {"col": h.col, "units": h.units, "header_text": h.header_text})
    blanks: dict[int, list[dict]] = {}
    for b in result.blank_cells:
        blanks.setdefault(b.row, []).append(b.model_dump())
    for row in result.rows:
        g.node("Row", {"id": f"{table.id}#r{row.row}", "conditions": json.dumps(row.conditions, ensure_ascii=False),
                       "values": json.dumps([v.model_dump() for v in row.values], ensure_ascii=False),
                       "blank_cells": json.dumps(blanks.pop(row.row, []), ensure_ascii=False), "status": status})
    for index, items in blanks.items():
        g.node("Row", {"id": f"{table.id}#r{index}", "blank_cells": json.dumps(items, ensure_ascii=False),
                       "status": "flagged"})
    write(graph, g, indexes=False)
    for rule in result.footnote_rules:
        graph.query("MATCH (:Table {id: $tid})-[:HAS_FOOTNOTE]->(f:Footnote {marker: $marker}) "
                    "SET f.rule_kind = $kind, f.rule = $rule, f.applies_to_rows = $rows, f.applies_to_cols = $cols",
                    {"tid": table.id, "marker": rule.marker, "kind": rule.kind, "rule": rule.rule,
                     "rows": rule.applies_to_rows, "cols": rule.applies_to_cols})
