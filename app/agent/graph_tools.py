"""Graph tools with fixed Cypher: get_dependencies, get_clause, applicable_clauses,
trace and run_cypher (read-only, row cap, timeout). Each returns a ``ToolResult`` with
the nodes and edges it touched."""
from __future__ import annotations

from collections import deque
import json

from app.agent.conditions import FALSE, TRUE, evaluate_all
from app.agent.facts import Fact
from app.agent.results import Label, ToolResult, Touched, clean, edge_label, node_label

MAX_NODES = 60
CYPHER_ROWS = 200
CYPHER_TIMEOUT_MS = 3000
REF_TYPES_UP = ("requires", "uses_value_from", "modifies", "alternative_to", "exception_to")

# (direction, label) -> Cypher returning (neighbour label, neighbour node, edge type, edge src id, edge dst id, edge)
STEPS = {
    ("upstream", "Parameter"): [
        "MATCH (x:Equation)-[e:COMPUTES]->(n:Parameter {id: $id}) RETURN 'Equation', x, 'COMPUTES', x.id, n.id, e",
        "MATCH (x:Table)-[e:GIVES]->(n:Parameter {id: $id}) RETURN 'Table', x, 'GIVES', x.id, n.id, e",
        "MATCH (n:Parameter {id: $id})-[e:DEFINED_IN]->(x:Clause) RETURN 'Clause', x, 'DEFINED_IN', n.id, x.id, e",
        "MATCH (x:Clause)-[e:DEFINES]->(n:Parameter {id: $id}) RETURN 'Clause', x, 'DEFINES', x.id, n.id, e",
    ],
    ("upstream", "Equation"): [
        "MATCH (n:Equation {id: $id})-[e:USES]->(x:Parameter) RETURN 'Parameter', x, 'USES', n.id, x.id, e",
        "MATCH (x:Clause)-[e:CONTAINS]->(n:Equation {id: $id}) RETURN 'Clause', x, 'CONTAINS', x.id, n.id, e",
    ],
    ("upstream", "Table"): [
        "MATCH (x:Clause)-[e:CONTAINS]->(n:Table {id: $id}) RETURN 'Clause', x, 'CONTAINS', x.id, n.id, e",
        "MATCH (x:Clause)-[e:REFERENCES]->(n:Table {id: $id}) WHERE e.type IN ['requires', 'uses_value_from'] "
        "RETURN 'Clause', x, 'REFERENCES', x.id, n.id, e",
    ],
    ("upstream", "Clause"): [
        "MATCH (n:Clause {id: $id})-[e:REFERENCES]->(x) WHERE e.type IN $types AND NOT x:ExternalCode "
        "RETURN labels(x)[0], x, 'REFERENCES', n.id, x.id, e",
        "MATCH (n:Clause {id: $id})-[e:PART_OF]->(x:Clause) RETURN 'Clause', x, 'PART_OF', n.id, x.id, e",
    ],
    ("downstream", "Parameter"): [
        "MATCH (x:Equation)-[e:USES]->(n:Parameter {id: $id}) RETURN 'Equation', x, 'USES', x.id, n.id, e",
        "MATCH (x:Clause)-[e:DEFINES]->(n:Parameter {id: $id}) RETURN 'Clause', x, 'DEFINES', x.id, n.id, e",
        "MATCH (n:Parameter {id: $id})-[e:DEFINED_IN]->(x:Clause) RETURN 'Clause', x, 'DEFINED_IN', n.id, x.id, e",
    ],
    ("downstream", "Equation"): [
        "MATCH (n:Equation {id: $id})-[e:COMPUTES]->(x:Parameter) RETURN 'Parameter', x, 'COMPUTES', n.id, x.id, e",
        "MATCH (x:Clause)-[e:CONTAINS]->(n:Equation {id: $id}) RETURN 'Clause', x, 'CONTAINS', x.id, n.id, e",
        "MATCH (x:Clause)-[e:REFERENCES]->(n:Equation {id: $id}) RETURN 'Clause', x, 'REFERENCES', x.id, n.id, e",
    ],
    ("downstream", "Table"): [
        "MATCH (n:Table {id: $id})-[e:GIVES]->(x:Parameter) RETURN 'Parameter', x, 'GIVES', n.id, x.id, e",
        "MATCH (x:Clause)-[e:REFERENCES]->(n:Table {id: $id}) RETURN 'Clause', x, 'REFERENCES', x.id, n.id, e",
    ],
    ("downstream", "Clause"): [
        "MATCH (x:Clause)-[e:REFERENCES]->(n:Clause {id: $id}) WHERE e.type IS NOT NULL "
        "RETURN 'Clause', x, 'REFERENCES', x.id, n.id, e",
    ],
}
# A downstream walk from a parameter goes through what uses it, not back through the
# clauses that only define it: those are listed but not expanded.
NO_EXPAND = {("downstream", "DEFINES"), ("downstream", "DEFINED_IN"), ("upstream", "PART_OF")}


def _brief(label: str, props: dict) -> dict:
    out = {"label": label, "id": props.get("id")}
    for f in ("title", "symbol", "description", "caption", "latex", "units", "page"):
        if props.get(f) is not None:
            value = props[f]
            out[f] = value[:140] if isinstance(value, str) else value
    if props.get("status") == "flagged":
        out["status"] = "flagged"
    return out


def find_node(graph, node_id: str) -> tuple[str, dict] | None:
    rows = graph.ro_query("MATCH (n) WHERE n.id = $id RETURN labels(n)[0], n LIMIT 5", {"id": node_id}).result_set
    order = ["Parameter", "Equation", "Table", "Clause", "Row", "Term", "Figure", "Footnote"]
    rows.sort(key=lambda r: order.index(r[0]) if r[0] in order else 99)
    return (rows[0][0], rows[0][1].properties) if rows else None


def get_dependencies(graph, node_id: str, direction: str = "upstream", depth: int = 3) -> ToolResult:
    start = find_node(graph, node_id)
    if start is None:
        return ToolResult(f"no node with id {node_id!r}; use find_entry first", {"id": node_id}, error=True)
    t, labels = Touched(), []
    t.node(*(start[0], node_id))
    seen = {(start[0], node_id)}
    found, queue = [], deque([(start[0], node_id, 0)])
    while queue and len(found) < MAX_NODES:
        label, nid, level = queue.popleft()
        if level >= depth:
            continue
        for query in STEPS.get((direction, label), []):
            for x_label, x, kind, src, dst, e in graph.ro_query(query, {"id": nid, "types": list(REF_TYPES_UP)}).result_set:
                props = x.properties
                key = (x_label, props.get("id"))
                t.edge(src, kind, dst)
                if (lab := edge_label(src, kind, dst, e.properties)) is not None:
                    labels.append(lab)
                if key in seen or len(found) >= MAX_NODES:
                    continue
                seen.add(key)
                t.node(*key)
                if (lab := node_label(props, x_label)) is not None:
                    labels.append(lab)
                ref = {**_brief(x_label, props), "via": f"{src} -{kind}-> {dst}", "depth": level + 1}
                if kind == "REFERENCES" and e.properties.get("type"):
                    ref["reference_type"] = e.properties["type"]
                found.append(ref)
                if (direction, kind) not in NO_EXPAND:
                    queue.append((x_label, props.get("id"), level + 1))
    by_label: dict[str, int] = {}
    for f in found:
        by_label[f["label"]] = by_label.get(f["label"], 0) + 1
    summary = f"{direction} of {start[0]} {node_id}: {len(found)} nodes " + \
              ", ".join(f"{n} {k}" for k, n in sorted(by_label.items()))
    return ToolResult(summary, {"start": _brief(*start), "direction": direction, "nodes": found}, t, labels)


def get_table(graph, table_id: str) -> ToolResult:
    """A table's columns, row conditions and footnotes, without its values: numbers come
    from compute, which matches the row and maps the columns."""
    from app.agent.compute import _load_table, condition_params, table_headers

    table, rows, notes = _load_table(graph, table_id)
    if table is None:
        return ToolResult(f"no table {table_id!r}; use find_entry first", {"id": table_id}, error=True)
    headers = table_headers(graph, table_id)
    conditions = condition_params(rows, headers)
    t, labels = Touched(), []
    t.node("Table", table_id)
    if (lab := node_label(table, "Table")) is not None:
        labels.append(lab)
    for r in rows:
        t.node("Row", r["id"])
        t.edge(table_id, "HAS_ROW", r["id"])
    for n in notes:
        t.node("Footnote", n.get("id"))
        t.edge(table_id, "HAS_FOOTNOTE", n.get("id"))
    governing = graph.ro_query("MATCH (c:Clause)-[:CONTAINS]->(:Table {id: $id}) RETURN c.id, c.title",
                               {"id": table_id}).result_set
    for cid, _ in governing:
        t.node("Clause", cid)
        t.edge(cid, "CONTAINS", table_id)
    data = {"id": table_id, "caption": table.get("caption"), "pages": table.get("pages") or [table.get("page")],
            **({"flagged": True} if table.get("status") == "flagged" else {}),
            "value_columns": [{"id": h.id, "header": h.header_text, "cols": h.cols, "units": h.units}
                              for h in headers if h.id not in conditions],
            "condition_columns": sorted(conditions),
            "rows": [{"row": r["id"], "conditions": r["_conditions"]} for r in rows],
            "footnotes": [{"marker": n.get("marker"), "text": n.get("text"), "kind": n.get("rule_kind"),
                           "rule": n.get("rule"), "applies_to_rows": n.get("applies_to_rows")} for n in notes],
            "governing_clauses": [{"id": i, "title": tt} for i, tt in governing],
            "values": "not shown here: call compute with this table id, the row_id you matched and your reason"}
    summary = f"{table_id}: {len(rows)} rows, {len(data['value_columns'])} value columns, {len(notes)} footnotes"
    return ToolResult(summary, data, t, labels)


def get_clause(graph, clause_id: str) -> ToolResult:
    found = find_node(graph, clause_id)
    if found and found[0] in ("Table", "Row"):
        return get_table(graph, clause_id.split("#r")[0])
    rows = graph.ro_query("MATCH (c:Clause {id: $id}) RETURN c", {"id": clause_id}).result_set
    if not rows:
        return ToolResult(f"no clause {clause_id!r}; use find_entry first", {"id": clause_id}, error=True)
    c = rows[0][0].properties
    t, labels = Touched(), []
    t.node("Clause", clause_id)
    if (lab := node_label(c, "Clause")) is not None:
        labels.append(lab)
    q = lambda cy: graph.ro_query(cy, {"id": clause_id}).result_set  # noqa: E731
    parent = q("MATCH (:Clause {id: $id})-[:PART_OF]->(p:Clause) RETURN p.id, p.title")
    children = q("MATCH (k:Clause)-[:PART_OF]->(:Clause {id: $id}) RETURN k.id, k.title ORDER BY k.order")
    refs = q("MATCH (:Clause {id: $id})-[e:REFERENCES]->(x) RETURN labels(x)[0], x.id, e.text, e.type, "
             "coalesce(x.title, x.caption)")
    eqs = q("MATCH (:Clause {id: $id})-[:CONTAINS]->(e:Equation) RETURN e.id, e.latex, e.crop, e.page, e.status")
    tables = q("MATCH (:Clause {id: $id})-[:CONTAINS]->(x:Table) RETURN x.id, x.caption, x.page")
    parts = {kind: [r[0] for r in q(f"MATCH (:Clause {{id: $id}})-[:{edge}]->(x) RETURN x.text")]
             for kind, edge in (("conditions", "HAS_CONDITION"), ("requirements", "HAS_REQUIREMENT"),
                                ("exceptions", "HAS_EXCEPTION"))}
    commentary = q("MATCH (k:Clause)-[:COMMENTARY_ON]->(:Clause {id: $id}) RETURN k.id")
    for pid, _ in parent:
        t.node("Clause", pid)
        t.edge(clause_id, "PART_OF", pid)
    references, seen = [], set()
    links = [r for r in refs if (r[2] or "").startswith("link to p")]
    for label, xid, text, kind, title in [r for r in refs if r not in links] + links:
        is_link = (text or "").startswith("link to p")
        if is_link and xid in seen:
            continue  # a PDF link to a target the text already references
        seen.add(xid)
        t.node(label, xid)
        t.edge(clause_id, "REFERENCES", xid)
        ref = {"label": label, "id": xid, "text": text, "type": kind, "title": title}
        if is_link:
            ref["kind"] = "pdf link"  # a hyperlink in the PDF at the reference, not typed by Pass 2
        references.append(ref)
    for eid, *_ in eqs:
        t.node("Equation", eid)
        t.edge(clause_id, "CONTAINS", eid)
    for tid, *_ in tables:
        t.node("Table", tid)
        t.edge(clause_id, "CONTAINS", tid)
    text = c.get("text_corrected") or c.get("text")
    data = {"id": clause_id, "title": c.get("title"), "page": c.get("page"), "page_end": c.get("page_end"),
            "bbox": c.get("bbox"), "status": c.get("status"),
            "text": text, "text_is_corrected": bool(c.get("text_corrected")),
            "parent": [{"id": i, "title": tt} for i, tt in parent],
            "children": [{"id": i, "title": tt} for i, tt in children][:30],
            "references": references[:40],
            "equations": [{"id": i, "latex": la, "crop": cr, "page": pg, **({"flagged": True} if st == "flagged" else {})}
                          for i, la, cr, pg, st in eqs],
            "tables": [{"id": i, "caption": cap, "page": pg} for i, cap, pg in tables],
            "commentary": [r[0] for r in commentary], **{k: v[:15] for k, v in parts.items()}}
    summary = f"{clause_id} {c.get('title') or ''} (p. {c.get('page')}): {len(references)} references, " \
              f"{len(eqs)} equations, {len(tables)} tables" + (", flagged" if c.get("status") == "flagged" else "")
    return ToolResult(summary, clean(data), t, labels)


def applicable_clauses(graph, scope: str, facts: dict[str, Fact], limit: int = 40) -> ToolResult:
    """The scope clause and its sub-clauses, each with its conditions checked against the facts."""
    rows = graph.ro_query("MATCH (s:Clause {id: $id}) OPTIONAL MATCH (k:Clause)-[:PART_OF*1..4]->(s) "
                          "WITH s, collect(DISTINCT k) AS ks RETURN [s] + ks", {"id": scope}).result_set
    if not rows:
        return ToolResult(f"no clause {scope!r}; use find_entry first", {"scope": scope}, error=True)
    t, labels, out = Touched(), [], []
    for node in rows[0][0][:limit]:
        c = node.properties
        conds = [r[0] for r in graph.ro_query("MATCH (:Clause {id: $id})-[:HAS_CONDITION]->(x) RETURN x.text",
                                              {"id": c["id"]}).result_set]
        excs = [r[0] for r in graph.ro_query("MATCH (:Clause {id: $id})-[:HAS_EXCEPTION]->(x) RETURN x.text",
                                             {"id": c["id"]}).result_set]
        evals = evaluate_all(conds, facts)
        status = "does not apply" if any(e.status == FALSE for e in evals) else \
            "applies" if all(e.status == TRUE for e in evals) else "undetermined: judge the conditions"
        t.node("Clause", c["id"])
        if (lab := node_label(c, "Clause")) is not None:
            labels.append(lab)
        out.append({"id": c["id"], "title": c.get("title"), "page": c.get("page"), "status": status,
                    "conditions": [e.as_dict() for e in evals][:8], "exceptions": excs[:5]})
    counts = {s: sum(1 for o in out if o["status"].startswith(s)) for s in ("applies", "does not", "undetermined")}
    return ToolResult(f"{len(out)} clauses under {scope}: {counts['applies']} apply, {counts['does not']} do not, "
                      f"{counts['undetermined']} undetermined", {"scope": scope, "clauses": out}, t, labels)


def trace(graph, items: list[dict]) -> ToolResult:
    """Clause, table, row, equation and page citations for the answer path, with labels."""
    t, labels, citations = Touched(), [], []
    for item in items:
        nid = item.get("id") if isinstance(item, dict) else str(item)
        found = find_node(graph, nid)
        if found is None:
            citations.append({"id": nid, "citation": f"{nid}: not in the graph"})
            continue
        label, p = found
        t.node(label, nid)
        if (lab := node_label(p, label)) is not None:
            labels.append(lab)
        page = p.get("page")
        if label == "Clause":
            text = f"{nid} {p.get('title') or ''}".strip()
        elif label == "Row":
            table_id, index = nid.rsplit("#r", 1)
            conds = "; ".join(json.loads(p.get("conditions") or "[]"))
            text = f"{table_id}, row {index}" + (f" ({conds})" if conds else "")
            t.node("Table", table_id)
            t.edge(table_id, "HAS_ROW", nid)
            table = find_node(graph, table_id)
            if table and (lab := node_label(table[1], "Table")) is not None:
                labels.append(lab)
        elif label == "Equation":
            clause = graph.ro_query("MATCH (c:Clause)-[:CONTAINS]->(:Equation {id: $id}) RETURN c.id, c.title, c",
                                    {"id": nid}).result_set
            text = nid + (f" (in {clause[0][0]} {clause[0][1] or ''}".rstrip() + ")" if clause else "")
            if clause and not any(x.target == nid for x in labels) and \
                    (lab := node_label(clause[0][2].properties, "Clause")) is not None:
                labels.append(Label(nid, lab.label, f"extracted with clause {clause[0][0]}: {lab.reason}"))
        elif label == "Parameter":
            text = f"{p.get('symbol') or nid}: {p.get('description') or ''}"[:160]
        else:
            text = f"{nid} {p.get('caption') or p.get('term') or ''}".strip()
        citations.append({"id": nid, "label": label, "page": page,
                          "citation": f"{text}, p. {page}" if page else text,
                          **({"status": "flagged"} if p.get("status") == "flagged" else {})})
    lines = [c["citation"] for c in citations]
    return ToolResult(f"{len(citations)} citations", {"citations": citations, "lines": lines}, t,
                      list({(x.target, x.label): x for x in labels}.values()))


HIDDEN = "hidden: table values come from compute (table id, row_id, reason)"
ROW_VALUE_KEYS = ("values", "cells", "blank_cells")


def _hide_row_values(props: dict) -> dict:
    return {k: (HIDDEN if k in ROW_VALUE_KEYS else v) for k, v in props.items()}


def _serialize(value, t: Touched):
    if hasattr(value, "properties") and hasattr(value, "labels"):
        label = value.labels[0] if value.labels else "Node"
        t.node(label, value.properties.get("id"))
        props = clean(value.properties)
        return {"label": label, **(_hide_row_values(props) if label == "Row" else props)}
    if isinstance(value, dict):
        out = {k: _serialize(v, t) for k, v in value.items()}
        return _hide_row_values(out) if "values" in out and "conditions" in out else out
    if hasattr(value, "properties") and hasattr(value, "relation"):
        return {"type": value.relation, **clean(value.properties)}
    if isinstance(value, list):
        return [_serialize(v, t) for v in value]
    return value


def run_cypher(graph, query: str) -> ToolResult:
    """Free-form read-only Cypher on the code graph: FalkorDB's read-only mode rejects
    writes; results are capped at CYPHER_ROWS rows and the query times out. Table row
    values are hidden: they come from compute, which matches rows and maps columns."""
    try:
        result = graph.ro_query(query, timeout=CYPHER_TIMEOUT_MS)
    except Exception as exc:  # syntax errors, writes refused by read-only mode, timeouts
        return ToolResult(f"query failed: {str(exc)[:200]}", {"query": query}, error=True)
    t = Touched()
    header = [h[1] if isinstance(h, (list, tuple)) else h for h in (result.header or [])]
    hidden = {i for i, h in enumerate(header) if str(h).split(".")[-1] in ROW_VALUE_KEYS}
    rows = [[HIDDEN if i in hidden else _serialize(v, t) for i, v in enumerate(row)]
            for row in result.result_set[:CYPHER_ROWS]]
    capped = len(result.result_set) > CYPHER_ROWS
    return ToolResult(f"{len(rows)} rows" + (f" (capped at {CYPHER_ROWS})" if capped else ""),
                      {"columns": header, "rows": rows, "capped": capped}, t)


__all__ = ["get_dependencies", "get_clause", "applicable_clauses", "trace", "run_cypher", "find_node", "Label"]
