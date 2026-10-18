"""Inferred edges (SPEC Step 3c.2): tables and notation symbols that no provision links
to are linked by Claude to the provisions that need them.

Unlinked means: a table no provision CONTAINS or REFERENCES with a typed reference (a
PDF link or a commentary mention is not a link the agent walks); a notation symbol with
no DEFINED_IN, no DEFINES and no equation or table using it. Candidates come from code:
provisions that mention the item untyped, provisions named in the item's own text
("Equation (2-2)", "Section 4.3"), the provisions a holding commentary explains, then the
provisions most similar by embedding. One call per item (``MODEL_GLOBAL``, default
``MODEL_REVIEW``) returns up to three links; each with confidence >= 0.7 is stored as
``(Clause)-[:REFERENCES {type, method: inferred}]->(item)``, the rest go to review.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from app.ingest.global_pass.calls import Ask, accepted
from app.ingest.global_pass.commentary import ClauseInfo, is_provision
from app.ingest.global_pass.edges import inferred_edge
from app.ingest.global_pass.findings import Finding
from app.ingest.global_pass.schemas import CONFIDENCE_FLOOR, InferredLinks, check_links
from app.ingest.global_pass.similarity import Vectors, encode

TOP = 8
MIN_SIMILAR = 3  # embedding candidates always get at least this many places
ITEM_CHARS = 1800
NAMED_REF = re.compile(r"\b(Equation|Eq\.|Section|Sections|Table|Figure)\s*\(?([A-Z]?\d+(?:[.-]\d+)*)\)?")


@dataclass
class Item:
    label: str  # Table / Parameter
    id: str
    text: str  # what Claude reads about it
    embed_text: str
    page: int | None
    mentions: list[str]  # provisions that mention it untyped, or hold it
    named: list[str]  # targets named in its own text


def unlinked_tables(graph) -> list[Item]:
    q = lambda c, p=None: graph.ro_query(c, p or {}).result_set  # noqa: E731
    linked = {r[0] for r in q("MATCH (c:Clause)-[:CONTAINS]->(t:Table) WHERE coalesce(c.stream, '') <> 'commentary' "
                              "RETURN t.id")}
    linked |= {r[0] for r in q("MATCH (c:Clause)-[e:REFERENCES]->(t:Table) WHERE e.type IS NOT NULL "
                               "AND coalesce(c.stream, '') <> 'commentary' AND coalesce(e.method, '') <> 'inferred' "
                               "RETURN t.id")}
    linked |= {r[0] for r in q("MATCH (c:Clause)-[e:REFERENCES {method: 'inferred'}]->(t:Table) "
                               "WHERE e.review_status = 'approved' RETURN t.id")}
    items = []
    for tid, caption, page in q("MATCH (t:Table) RETURN t.id, t.caption, t.page ORDER BY t.page"):
        if tid in linked:
            continue
        heads = [h for (h,) in q("MATCH (:Table {id: $id})-[g:GIVES]->(p) RETURN coalesce(g.header_text, p.id)",
                                 {"id": tid})]
        conds = []
        for (c,) in q("MATCH (:Table {id: $id})-[:HAS_ROW]->(r) WHERE r.conditions IS NOT NULL RETURN r.conditions "
                      "ORDER BY r.index LIMIT 12", {"id": tid}):
            conds += [x for x in json.loads(c or "[]") if x not in conds]
        notes = [n for (n,) in q("MATCH (:Table {id: $id})-[:HAS_FOOTNOTE]->(f) RETURN f.text LIMIT 4", {"id": tid})]
        holders = q("MATCH (c:Clause)-[:CONTAINS]->(:Table {id: $id}) RETURN c.id, c.title", {"id": tid})
        mentions = [r[0] for r in q("MATCH (c:Clause)-[e:REFERENCES]->(:Table {id: $id}) "
                                    "WHERE coalesce(e.source, '') <> 'global_pass' RETURN c.id", {"id": tid})]
        explained = [r[0] for r in q("MATCH (h:Clause)-[:CONTAINS]->(:Table {id: $id}) "
                                     "MATCH (h)-[:COMMENTARY_ON]->(p:Clause) RETURN p.id", {"id": tid})]
        text = "\n".join(x for x in [
            f"Table {tid}: {caption or ''}",
            f"Held in: {', '.join(f'{i} {t or ''}' for i, t in holders)}" if holders else "",
            f"Column headers: {'; '.join(heads)}" if heads else "",
            f"Row labels: {'; '.join(conds[:20])}" if conds else "",
            f"Notes: {' | '.join(n[:200] for n in notes if n)}" if notes else ""] if x)
        items.append(Item("Table", tid, text[:ITEM_CHARS], f"{caption or tid}. {'; '.join(heads)}"[:500], page,
                          list(dict.fromkeys([*[i for i, _ in holders], *mentions, *explained])),
                          [m.group(2) for m in NAMED_REF.finditer(" ".join(notes))]))
    return items


def unlinked_parameters(graph) -> list[Item]:
    rows = graph.ro_query(
        "MATCH (p:Parameter {source: 'notation'}) WHERE NOT (p)-[:DEFINED_IN]->() AND NOT ()-[:DEFINES]->(p) "
        "AND NOT ()-[:USES|COMPUTES|GIVES]->(p) AND NOT (:Clause)-[:REFERENCES {review_status: 'approved'}]->(p) "
        "RETURN p.id, p.symbol, p.description, p.units, p.defined_in_text, p.scope_chapters, p.page "
        "ORDER BY p.page, p.id").result_set
    items = []
    for pid, symbol, desc, units, defined_in, scope, page in rows:
        defined = " ".join(defined_in) if isinstance(defined_in, list) else (defined_in or "")
        text = "\n".join(x for x in [
            f"Notation symbol {symbol or pid} (id {pid}): {desc or ''}",
            f"Units: {units}" if units else "",
            f"The notation says it is defined in: {defined}" if defined else "",
            f"Applies in chapters: {', '.join(str(s) for s in scope)}" if scope else ""] if x)
        items.append(Item("Parameter", pid, text, f"{symbol or pid}: {desc or ''}", page, [],
                          [m.group(2) for m in NAMED_REF.finditer(f"{desc or ''} {defined}")]))
    return items


def named_clauses(graph, targets: list[str]) -> list[str]:
    """Provisions holding (or being) the equations, sections and tables an item's text names."""
    out = []
    for t in targets:
        for (cid,) in graph.ro_query(
                "MATCH (c:Clause) WHERE c.id = $t OR c.number = $t RETURN c.id "
                "UNION MATCH (c:Clause)-[:CONTAINS]->(x) WHERE (x:Equation OR x:Table) AND (x.number = $t "
                "OR x.id ENDS WITH (' ' + $t)) RETURN c.id", {"t": t}).result_set:
            out.append(cid)
    return out


def candidates(graph, item: Item, vector, vectors: Vectors, clauses: dict[str, ClauseInfo],
               provisions: set[str]) -> list[tuple[str, str]]:
    out: dict[str, str] = {}
    for cid in item.mentions:
        if cid in provisions:
            out.setdefault(cid, "holds or mentions it, untyped")
    for cid in named_clauses(graph, item.named):
        if cid in provisions:
            out.setdefault(cid, "named in its text")
    out = dict(list(out.items())[:TOP - MIN_SIMILAR])
    if vector is not None:
        for cid, score in vectors.top(vector, TOP * 3, provisions):
            if len(out) >= TOP:
                break
            out.setdefault(cid, f"similar wording ({score:.2f})")
    return list(out.items())


def prompt_text(item: Item, cands: list[tuple[str, str]], clauses: dict[str, ClauseInfo]) -> str:
    lines = [item.text, "", "Candidate provisions:"]
    lines += [f"- id: {cid} | why: {why} | {clauses[cid].brief()}" for cid, why in cands]
    return "\n".join(lines)


def build_asks(graph, clauses: dict[str, ClauseInfo], vectors: Vectors, encoder, model: str
               ) -> tuple[list[Ask], dict[str, tuple[Item, list]]]:
    provisions = {i for i, c in clauses.items() if is_provision(c)}
    items = unlinked_tables(graph) + unlinked_parameters(graph)
    vecs = encode(encoder, [i.embed_text for i in items]) if encoder is not None else [None] * len(items)
    asks, plan = [], {}
    for item, vector in zip(items, vecs):
        found = candidates(graph, item, vector, vectors, clauses, provisions)
        key = f"{item.label}:{item.id}"
        plan[key] = (item, found)
        if found:
            ids = {c for c, _ in found}
            asks.append(Ask(key, "global_inferred", prompt_text(item, found, clauses), InferredLinks,
                            lambda out, ids=ids: check_links(out, ids), model))
    return asks, plan


def decide(results: dict, plan: dict[str, tuple[Item, list]], model: str) -> tuple[list[dict], list[Finding]]:
    edges, findings = [], []
    for key, (item, found) in plan.items():
        out: InferredLinks | None = accepted(results.get(key)) if found else None
        links = out.links if out is not None else []
        strong = [x for x in links if x.confidence >= CONFIDENCE_FLOOR]
        weak = [x for x in links if x.confidence < CONFIDENCE_FLOOR]
        make = lambda x: inferred_edge(("Clause", x.clause_id), "REFERENCES", (item.label, item.id),  # noqa: E731
                                       x.confidence, x.reason, model, type=x.edge_type, text="inferred")
        edges += [make(x) for x in strong]
        if strong and not weak:
            continue
        if weak:
            reason = (f"{item.label} {item.id} may be needed by {', '.join(x.clause_id for x in weak)} "
                      f"(confidence {max(x.confidence for x in weak):.2f})")
            proposal, conf = {"type": "edges", "edges": [make(x) for x in weak]}, max(x.confidence for x in weak)
        elif not found:
            reason, proposal, conf = f"{item.label} {item.id}: no provision links to it and no candidate was found", \
                None, 0.0
        elif out is None:
            reason, proposal, conf = f"{item.label} {item.id}: no provision links to it; no valid answer from Claude", \
                None, 0.0
        else:
            reason, proposal, conf = f"{item.label} {item.id}: no provision links to it; Claude found none that needs it", \
                None, 0.0
        findings.append(Finding("inferred_edge", key, reason, [(item.label, item.id)] + [("Clause", c) for c, _ in found],
                                detail="candidates: " + "; ".join(f"{c} ({w})" for c, w in found), page=item.page,
                                proposal=proposal, confidence=conf, method=f"claude:{model}",
                                quote=item.text.splitlines()[0]))
    return edges, findings
