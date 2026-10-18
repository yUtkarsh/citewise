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
from app.ingest.pass2.validate import check_clause, image_cells, norm_primes, norm_text


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
    rows = [{"id": e.id, "symbol": norm_primes(e.display), "latex": e.latex, "description": e.description,
             "units": e.units, "aliases": list(dict.fromkeys(norm_primes(a) for a in e.aliases)), "scope_chapters": e.scope_chapters, "defined_in_text": e.defined_in,
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
    props |= {"text_corrected": norm_primes(check.corrected_text) if check.corrected_text else None,
              "unmapped": json.dumps([u.model_dump() for u in result.unmapped], ensure_ascii=False)}
    g.node("Clause", props)

    prov = lambda quote: pass2_provenance(clause.page, None, quote, model, status)  # noqa: E731
    params = [{"id": p.id, "symbol": norm_primes(p.symbol), "latex": p.latex, "description": p.description, "units": p.units,
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

    for n, (corr, target) in enumerate(check.accepted, 1):  # rejected corrections stay in the flags only
        line = next((f for f in clause.flagged if corr.raw in f.text), None)
        nid = f"{clause.id}#corr{n}"
        g.node("SymbolCorrection", {
            "id": nid, "raw": corr.raw, "corrected": norm_primes(corr.corrected), "uncertain": corr.uncertain,
            "note": corr.note, "target": target, "line_id": line.id if line else None,
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
        "t.extraction_schema = $schema, t.extraction_method = $method, t.trust = $trust, t.trust_reason = $reason",
        {"id": table.id, "part": part, "status": status, "flags": _flags(outcome), "schema": PASS2_SCHEMA_VERSION,
         "method": "claude:" + model, "trust": table.trust, "reason": table.trust_reason})
    result: TableExtraction | None = outcome.output
    if result is None:
        return
    prov = lambda quote: pass2_provenance(table.pages[0] if table.pages else None, None, quote, model, status)  # noqa
    _merge_parameters(graph, [{"id": h.parameter_id, "symbol": h.header_text, "units": h.units, "source": "table",
                               **prov(h.header_text)} for h in result.header_parameters], overwrite=False)
    g = GraphRows()
    by_param: dict[str, list] = {}
    for h in result.header_parameters:  # a parameter may sit in several columns (merged-cell grids)
        by_param.setdefault(h.parameter_id, []).append(h)
    for pid, hs in by_param.items():
        g.edge(("Table", table.id), "GIVES", ("Parameter", pid),
               {"col": hs[0].col, "cols": sorted({h.col for h in hs}), "units": hs[0].units,
                "header_text": hs[0].header_text})
    blanks: dict[int, list[dict]] = {}
    for b in result.blank_cells:
        blanks.setdefault(b.row, []).append(b.model_dump())
    from_image = {(r, c) for r, c, _, _ in image_cells(result, table, job.rows)} if table.trust == "image" else set()
    for row in result.rows:
        values = [{**v.model_dump(), "source": "image" if (row.row, v.col) in from_image else "text"}
                  for v in row.values]
        g.node("Row", {"id": f"{table.id}#r{row.row}", "conditions": json.dumps(row.conditions, ensure_ascii=False),
                       "values": json.dumps(values, ensure_ascii=False),
                       "blank_cells": json.dumps(blanks.pop(row.row, []), ensure_ascii=False), "status": status})
    for index, items in blanks.items():
        g.node("Row", {"id": f"{table.id}#r{index}", "blank_cells": json.dumps(items, ensure_ascii=False),
                       "status": "flagged"})
    write(graph, g, indexes=False)
    answered = {f"{table.id}#r{r.row}" for r in result.rows} | {f"{table.id}#r{i}" for i in blanks}
    left_out = [f"{table.id}#r{r.index}" for r in (job.rows or []) if not r.is_header
                and f"{table.id}#r{r.index}" not in answered]
    if left_out:  # rows this answer leaves out keep no values from an earlier answer
        graph.query("UNWIND $ids AS id MATCH (r:Row {id: id}) SET r.values = null, r.conditions = null",
                    {"ids": left_out})
    if part == 1:
        write_cell_corrections(graph, table, job.rows or table.rows, result, model, status)
    for rule in result.footnote_rules:
        graph.query("MATCH (:Table {id: $tid})-[:HAS_FOOTNOTE]->(f:Footnote {marker: $marker}) "
                    "SET f.rule_kind = $kind, f.rule = $rule, f.applies_to_rows = $rows, f.applies_to_cols = $cols",
                    {"tid": table.id, "marker": rule.marker, "kind": rule.kind, "rule": rule.rule,
                     "rows": rule.applies_to_rows, "cols": rule.applies_to_cols})


def cell_corrections(table, rows, result: TableExtraction) -> list[dict]:
    """Raw grid text and the answer's text for every suspect cell of the answered rows:
    a value cell's corrected text is the value as read; a label cell's is the condition
    that best matches it. Like a clause's symbol corrections, kept for review."""
    from difflib import SequenceMatcher

    from app.ingest.pass2.records import is_suspect_cell

    by_index = {r.index: r for r in rows}
    out = []
    for answer in result.rows:
        row = by_index.get(answer.row)
        if row is None:
            continue
        values = {v.col: v.value for v in answer.values}
        for col, raw in enumerate(row.cells):
            if not raw.strip() or not is_suspect_cell(raw, table.suspect_spans):
                continue
            corrected = values.get(col)
            if corrected is None and answer.conditions:
                corrected = max(answer.conditions, key=lambda c: SequenceMatcher(None, raw, c).ratio())
            if corrected is not None and corrected != raw:
                out.append({"row": answer.row, "col": col, "raw": raw, "corrected": corrected,
                            "where": "value" if col in values else "conditions"})
    return out


def write_cell_corrections(graph, table, rows, result: TableExtraction, model: str, status: str) -> None:
    graph.query("MATCH (:Table {id: $id})-[:HAS_CORRECTION]->(c:SymbolCorrection) DETACH DELETE c", {"id": table.id})
    g = GraphRows()
    for c in cell_corrections(table, rows, result):
        nid = f"{table.id}#r{c['row']}#c{c['col']}"
        g.node("SymbolCorrection", {"id": nid, "raw": c["raw"], "corrected": c["corrected"], "uncertain": False,
                                    "note": f"suspect grid cell (row {c['row']}, col {c['col']}) read from the image",
                                    "target": c["where"], "line_id": None,
                                    **pass2_provenance(table.pages[0] if table.pages else None, None, c["raw"], model,
                                                       status)})
        g.edge(("Table", table.id), "HAS_CORRECTION", ("SymbolCorrection", nid))
    write(graph, g, indexes=False)


def write_second_read(graph, outcome: Outcome) -> None:
    """Record the second read on each image-sourced value (second_read, confirmed) and add
    its notes and flags to the table; a disagreement flags the table."""
    job = outcome.job
    table = job.table
    reads = {(c.row, c.col): c for c in outcome.output.cells} if outcome.output else {}
    for index in sorted({r for r, _, _, _ in job.cells}):
        rid = f"{table.id}#r{index}"
        found = graph.query("MATCH (r:Row {id: $id}) RETURN r.values", {"id": rid}).result_set
        if not found or not found[0][0]:
            continue
        values = json.loads(found[0][0])
        for value in values:
            read = reads.get((index, value["col"]))
            if value.get("source") == "image" and read is not None:
                value["second_read"] = read.value
                value["confirmed"] = read.number is not None and value["number"] is not None and \
                    abs(read.number - value["number"]) <= 1e-9 * max(1.0, abs(value["number"]))
        graph.query("MATCH (r:Row {id: $id}) SET r.values = $values",
                    {"id": rid, "values": json.dumps(values, ensure_ascii=False)})
    graph.query("MATCH (t:Table {id: $id}) SET t.flags = coalesce(t.flags, []) + $flags, "
                "t.status = CASE WHEN $flagged THEN 'flagged' ELSE t.status END",
                {"id": table.id, "flags": _flags(outcome), "flagged": outcome.status == "flagged"})
