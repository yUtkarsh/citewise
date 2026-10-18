"""Parameter merges (SPEC Step 3c.1): move a duplicate onto the surviving node and leave
an id stub, so the old id still resolves.

A merge copies every edge of the removed Parameter onto the survivor (same type, same
properties), adds the removed symbol and aliases to the survivor's aliases, renames the
symbol in the SymPy forms of the survivor's equations, deletes the removed node and
creates ``(:MergedId {id: <removed>, merged_into: <survivor>})``. Pins, session facts and
the memory graph keep old ids; ``resolve_id`` and ``merged_map`` follow the stubs.
"""
from __future__ import annotations

import json
import re
import time

from app.graph.schema import GRAPH_SCHEMA_VERSION

MAX_HOPS = 10
SNAPSHOT_DROP = {"embedding", "embed_hash", "embed_model", "alias_text"}


def units_key(units: str | None) -> str:
    """Units compared loosely: case, spaces and the parenthesised SI form are ignored."""
    text = re.sub(r"\(.*?\)", "", (units or "").lower())
    return re.sub(r"[^a-z0-9/^%]+", "", text)


def units_conflict(a: str | None, b: str | None) -> bool:
    ka, kb = units_key(a), units_key(b)
    return bool(ka and kb and ka != kb)


def rename_symbols(text: str | None, mapping: dict[str, str]) -> str | None:
    """Rename whole-word symbols in a SymPy form (``f_c`` in ``f_c*A`` but not in ``f_ce``)."""
    if not text or not mapping:
        return text
    pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(k) for k in sorted(mapping, key=len,
                                                                                         reverse=True)) + r")(?![A-Za-z0-9_])")
    return pattern.sub(lambda m: mapping[m.group(1)], text)


def merged_map(graph) -> dict[str, str]:
    """Every merged id -> the id it finally resolves to."""
    direct = {i: m for i, m in graph.ro_query("MATCH (m:MergedId) RETURN m.id, m.merged_into").result_set}
    out = {}
    for old in direct:
        target, hops = direct[old], 0
        while target in direct and hops < MAX_HOPS:
            target, hops = direct[target], hops + 1
        out[old] = target
    return out


def resolve_id(graph, node_id: str) -> str:
    target, hops = node_id, 0
    while hops < MAX_HOPS:
        rows = graph.ro_query("MATCH (m:MergedId {id: $id}) RETURN m.merged_into", {"id": target}).result_set
        if not rows:
            return target
        target, hops = rows[0][0], hops + 1
    return target


def _props(graph, pid: str) -> dict | None:
    rows = graph.ro_query("MATCH (p:Parameter {id: $id}) RETURN properties(p)", {"id": pid}).result_set
    return dict(rows[0][0]) if rows else None


def _move_edges(graph, removed: str, survivor: str) -> int:
    moved = 0
    out = graph.ro_query("MATCH (:Parameter {id: $id})-[e]->(x) RETURN type(e), labels(x)[0], x.id, properties(e)",
                         {"id": removed}).result_set
    inc = graph.ro_query("MATCH (x)-[e]->(:Parameter {id: $id}) RETURN type(e), labels(x)[0], x.id, properties(e)",
                         {"id": removed}).result_set
    for kind, label, xid, props in out:
        graph.query(f"MATCH (a:Parameter {{id: $a}}) MATCH (b:{label} {{id: $b}}) MERGE (a)-[e:{kind}]->(b) "
                    "SET e += $p", {"a": survivor, "b": xid, "p": dict(props)})
        moved += 1
    for kind, label, xid, props in inc:
        if label == "Parameter" and xid == survivor:
            continue
        graph.query(f"MATCH (a:{label} {{id: $a}}) MATCH (b:Parameter {{id: $b}}) MERGE (a)-[e:{kind}]->(b) "
                    "SET e += $p", {"a": xid, "b": survivor, "p": dict(props)})
        moved += 1
    return moved


def merge_parameter(graph, survivor: str, removed: str, reason: str, method: str, confidence: float,
                    finding: str | None = None) -> dict:
    """Merge ``removed`` into ``survivor``. Refuses parameters with different units."""
    keep, gone = _props(graph, survivor), _props(graph, removed)
    if keep is None or gone is None or survivor == removed:
        return {"merged": False, "why": "both parameters must exist and differ"}
    if units_conflict(keep.get("units"), gone.get("units")):
        return {"merged": False, "why": f"different units: {keep.get('units')} vs {gone.get('units')}"}
    moved = _move_edges(graph, removed, survivor)
    aliases = list(dict.fromkeys(a for a in [*(keep.get("aliases") or []), gone.get("symbol"),
                                             *(gone.get("aliases") or [])]
                                 if a and a != keep.get("symbol")))
    fill = {k: gone[k] for k in ("units", "description", "latex") if not keep.get(k) and gone.get(k)}
    graph.query("MATCH (p:Parameter {id: $id}) SET p.aliases = $aliases, "
                "p.merged_from = coalesce(p.merged_from, []) + [$removed], p += $fill",
                {"id": survivor, "aliases": aliases, "removed": removed, "fill": fill})
    for eid, sympy in graph.ro_query("MATCH (e:Equation)-[:USES|COMPUTES]->(:Parameter {id: $id}) "
                                     "RETURN DISTINCT e.id, e.sympy", {"id": survivor}).result_set:
        renamed = rename_symbols(sympy, {removed: survivor})
        if renamed != sympy:
            graph.query("MATCH (e:Equation {id: $id}) SET e.sympy = $s", {"id": eid, "s": renamed})
    snapshot = {k: v for k, v in gone.items() if k not in SNAPSHOT_DROP}
    graph.query("MATCH (p:Parameter {id: $id}) DETACH DELETE p", {"id": removed})
    graph.query("MERGE (m:MergedId {id: $id}) SET m.merged_into = $into, m.reason = $reason, m.method = $method, "
                "m.confidence = $conf, m.finding = $finding, m.time = $t, m.snapshot = $snap, m.page = $page, "
                "m.source_quote = $quote, m.extraction_method = $method, m.review_status = 'unreviewed', "
                "m.schema_version = $sv, m.bbox = null",
                {"id": removed, "into": survivor, "reason": reason, "method": method, "conf": confidence,
                 "finding": finding, "t": time.time(), "snap": json.dumps(snapshot, ensure_ascii=False, default=str),
                 "page": gone.get("page"), "quote": (gone.get("source_quote") or "")[:300], "sv": GRAPH_SCHEMA_VERSION})
    graph.query("MATCH (m:MergedId {merged_into: $old}) SET m.merged_into = $new", {"old": removed, "new": survivor})
    return {"merged": True, "survivor": survivor, "removed": removed, "edges_moved": moved}
