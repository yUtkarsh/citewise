"""Inferred GIVES edges (SPEC Step 3c.2, the ``Table 16-3 GIVES v_u`` example): a table
column supplies a parameter that the code's equations use, but the table's own header
parameter (``fc_walls``) is a different node from the equations' parameter (``f_prime_c``).

Items are the header parameters no equation uses. Candidates come from code: parameters
some equation uses or computes, with units that do not conflict, ranked by the local
embedding of the table caption plus the header against the parameter's own embedding;
a shared symbol or alias spelling counts too. One call per table (``MODEL_GLOBAL``)
links columns to candidates. A link with confidence >= 0.7 is stored as
``(Table)-[:GIVES {method: inferred, via: <header id>, col, cols, header_text, units}]->
(Parameter)``; the rest go to review. Readers of a table's columns (compute, calculate,
review) skip inferred GIVES edges, so column mapping is unchanged; the procedure walk
and ``get_dependencies`` follow them, labelled "inferred".
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

import numpy as np

from app.graph.merges import units_conflict
from app.ingest.global_pass.calls import Ask, accepted
from app.ingest.global_pass.edges import inferred_edge
from app.ingest.global_pass.findings import Finding
from app.ingest.global_pass.schemas import CONFIDENCE_FLOOR, GivesLinks, check_gives
from app.ingest.global_pass.similarity import encode, load_vectors

PER_HEADER = 3
MIN_SIMILARITY = 0.82  # cosine of caption + header against the parameter's embedding (local bge model)
ROW_LABELS = 12


@dataclass
class Column:
    id: str  # the table's header parameter
    header: str
    units: str | None
    col: int | None
    cols: list[int]
    candidates: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class TableItem:
    id: str
    caption: str
    page: int | None
    rows: list[str]
    columns: list[Column]


def _key(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def used_parameters(graph) -> dict[str, dict]:
    rows = graph.ro_query("MATCH (e:Equation)-[:USES|COMPUTES]->(p:Parameter) WHERE coalesce(p.source, '') <> 'table' "
                          "RETURN p.id, p.symbol, p.description, p.units, p.aliases, collect(DISTINCT e.id)[0..4]"
                          ).result_set
    return {r[0]: {"id": r[0], "symbol": r[1], "description": r[2] or "", "units": r[3], "aliases": r[4] or [],
                   "equations": r[5]} for r in rows}


def table_items(graph) -> list[TableItem]:
    rows = graph.ro_query(
        "MATCH (t:Table)-[e:GIVES]->(p:Parameter) WHERE coalesce(e.method, '') <> 'inferred' "
        "AND NOT ()-[:USES|COMPUTES]->(p) "
        "RETURN t.id, t.caption, t.page, p.id, coalesce(e.header_text, p.id), coalesce(e.units, p.units), e.col, "
        "coalesce(e.cols, [e.col]) ORDER BY t.page, t.id, e.col").result_set
    items: dict[str, TableItem] = {}
    for tid, caption, page, pid, header, units, col, cols in rows:
        item = items.setdefault(tid, TableItem(tid, caption or tid, page, [], []))
        item.columns.append(Column(pid, header, units, col, [c for c in cols if c is not None]))
    for item in items.values():
        labels = []
        for (conds,) in graph.ro_query("MATCH (:Table {id: $id})-[:HAS_ROW]->(r) WHERE r.conditions IS NOT NULL "
                                       "RETURN r.conditions ORDER BY r.index", {"id": item.id}).result_set:
            labels += [c for c in json.loads(conds or "[]") if c not in labels]
        item.rows = labels[:ROW_LABELS]
    return list(items.values())


def find_candidates(items: list[TableItem], targets: dict[str, dict], vectors, encoder) -> None:
    ids = [i for i in vectors.ids if i in targets]
    matrix = np.stack([vectors.vector(i) for i in ids]) if ids else None
    texts = [f"{item.caption}. {c.header}" + (f" ({c.units})" if c.units else "")
             for item in items for c in item.columns]
    vecs = iter(encode(encoder, texts))
    for item in items:
        for c in item.columns:
            v = next(vecs)
            scores = (matrix @ v) if matrix is not None else []
            keys = {_key(c.id), _key(c.header)}
            found = {}
            for pid, s in zip(ids, scores):
                p = targets[pid]
                shared = keys & {_key(p["symbol"]), _key(pid), *(_key(a) for a in p["aliases"])}
                if (s >= MIN_SIMILARITY or shared) and not units_conflict(c.units, p["units"]):
                    found[pid] = float(s) + (0.1 if shared else 0.0)
            c.candidates = sorted(found.items(), key=lambda x: -x[1])[:PER_HEADER]


def prompt_text(item: TableItem, targets: dict[str, dict]) -> str:
    lines = [f"Table {item.id}: {item.caption}"]
    if item.rows:
        lines.append("Row labels: " + "; ".join(item.rows))
    lines.append("")
    for c in item.columns:
        if not c.candidates:
            continue
        lines.append(f"Column header {c.id} (header text: {c.header}; units: {c.units or 'none given'}). Candidates:")
        for pid, _ in c.candidates:
            p = targets[pid]
            lines.append(f"  - id: {pid} | symbol {p['symbol']} | {p['description'][:160]} | units: "
                         f"{p['units'] or 'none given'} | used by {', '.join(p['equations'])}")
    return "\n".join(lines)


@dataclass
class Plan:
    items: list[TableItem]
    asks: list[Ask]
    targets: dict[str, dict]


def plan(graph, encoder, model: str) -> Plan:
    targets = used_parameters(graph)
    items = table_items(graph)
    find_candidates(items, targets, load_vectors(graph, "Parameter"), encoder)
    asks = []
    for item in items:
        offered = {c.id: {pid for pid, _ in c.candidates} for c in item.columns if c.candidates}
        if offered:
            asks.append(Ask(item.id, "global_gives", prompt_text(item, targets), GivesLinks,
                            lambda out, offered=offered: check_gives(out, offered), model))
    return Plan(items, asks, targets)


def decide(p: Plan, results: dict, model: str) -> tuple[list[dict], list[Finding]]:
    edges, findings = [], []
    by_id = {i.id: i for i in p.items}
    for ask in p.asks:
        item = by_id[ask.key]
        columns = {c.id: c for c in item.columns}
        out: GivesLinks | None = accepted(results.get(ask.key))
        if out is None:
            findings.append(Finding("inferred_gives", item.id, f"Table {item.id}: no valid answer from Claude",
                                    [("Table", item.id)], page=item.page, confidence=0.0, method=f"claude:{model}"))
            continue
        weak, guarded = [], []
        for link in out.links:
            c, target = columns[link.header_id], p.targets[link.parameter_id]
            if units_conflict(c.units, target["units"]):  # never link different units
                guarded.append(f"{c.id} -> {link.parameter_id} ({c.units} vs {target['units']})")
                continue
            edge = inferred_edge(("Table", item.id), "GIVES", ("Parameter", link.parameter_id), link.confidence,
                                 link.reason, model, via=c.id, col=c.col, cols=c.cols, header_text=c.header,
                                 units=c.units)
            (edges if link.confidence >= CONFIDENCE_FLOOR else weak).append(edge)
        if weak or guarded:
            note = f"; refused for different units: {', '.join(guarded)}" if guarded else ""
            findings.append(Finding(
                "inferred_gives", item.id,
                (f"Table {item.id} may supply " + ", ".join(f"{e['dst']} (via {e['props']['via']}, "
                                                            f"{e['props']['confidence']:.2f})" for e in weak)
                 if weak else f"Table {item.id}: links refused") + note,
                [("Table", item.id)] + [("Parameter", e["dst"]) for e in weak], page=item.page,
                proposal={"type": "edges", "edges": weak} if weak else None,
                confidence=max((e["props"]["confidence"] for e in weak), default=0.0), method=f"claude:{model}",
                quote=item.caption))
    return edges, findings
