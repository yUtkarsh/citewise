"""Graph-wide checks in Cypher (SPEC Step 3c.4), no API calls:

- parameters an equation uses or computes that nothing defines: no notation entry, no
  clause DEFINES it, no table GIVES it, no DEFINED_IN;
- references whose target clause (or table, figure, equation) is not in the graph;
- cycles in COMPUTES/USES: a parameter that, through equations, depends on itself.

Every hit becomes a medium finding in the review queue.
"""
from __future__ import annotations

from collections import defaultdict

from app.ingest.global_pass.findings import Finding, FindingSet

CHECK_KINDS = {"undefined_parameter", "missing_reference", "dependency_cycle"}

UNDEFINED = (
    "MATCH (e:Equation)-[:USES|COMPUTES]->(p:Parameter) "
    "WHERE coalesce(p.source, '') <> 'notation' AND NOT ()-[:DEFINES]->(p) AND NOT ()-[:GIVES]->(p) "
    "AND NOT (p)-[:DEFINED_IN]->() "
    "OPTIONAL MATCH (c:Clause)-[:CONTAINS]->(e) "
    "RETURN p.id, p.symbol, collect(DISTINCT e.id), collect(DISTINCT c.id), min(coalesce(e.page, c.page))")
UNRESOLVED = "MATCH (c:Clause) WHERE size(c.unresolved_refs) > 0 RETURN c.id, c.page, c.unresolved_refs"
TARGET_IDS = "MATCH (n) WHERE n:Clause OR n:Table OR n:Figure OR n:Equation RETURN n.id, n.number"
DEPENDS = ("MATCH (a:Parameter)<-[:USES]-(e:Equation)-[:COMPUTES]->(b:Parameter) "
           "OPTIONAL MATCH (c:Clause)-[:CONTAINS]->(e) RETURN a.id, b.id, e.id, coalesce(e.page, c.page)")


def undefined_parameters(graph) -> list[Finding]:
    out = []
    for pid, symbol, eqs, clauses, page in graph.ro_query(UNDEFINED).result_set:
        out.append(Finding(
            "undefined_parameter", pid,
            f"parameter {symbol or pid} is used by {', '.join(sorted(eqs))} but nothing defines it "
            "(no notation entry, no DEFINES, no table)",
            [("Parameter", pid)] + [("Equation", e) for e in sorted(eqs)] + [("Clause", c) for c in sorted(clauses)],
            detail="re-extract the equation's clause or add the parameter to the notation",
            page=page))
    return out


def _parse_ref(item: str) -> tuple[str, str]:
    """Pass 1 stores an unresolved reference as '<text> -> <target>'."""
    text, sep, target = item.rpartition(" -> ")
    return (text, target) if sep else (item, item)


def missing_references(graph) -> list[Finding]:
    known = set()
    for nid, number in graph.ro_query(TARGET_IDS).result_set:
        known.update(x for x in (nid, number) if x)
    out = []
    for cid, page, refs in graph.ro_query(UNRESOLVED).result_set:
        for item in dict.fromkeys(refs or []):
            text, target = _parse_ref(item)
            if target in known:
                continue
            out.append(Finding(
                "missing_reference", f"{cid}|{target}",
                f"{cid} refers to '{text}', but {target} is not in the graph",
                [("Clause", cid)], detail="the target may be in another document, or missed by Pass 1",
                page=page, quote=text))
    return out


def _cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with a cycle (size > 1, or a self-loop), by Tarjan."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]

    def visit(v: str) -> None:  # iterative to stay clear of the recursion limit
        work = [(v, iter(sorted(edges.get(v, ()))))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                elif child in on:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in edges.get(node, ()):
                    out.append(sorted(comp))

    for v in sorted(edges):
        if v not in index:
            visit(v)
    return out


def dependency_cycles(graph) -> list[Finding]:
    edges: dict[str, set[str]] = defaultdict(set)  # used parameter -> computed parameter
    via: dict[tuple[str, str], tuple[str, int | None]] = {}
    for a, b, eid, page in graph.ro_query(DEPENDS).result_set:
        edges[a].add(b)
        via[(a, b)] = (eid, page)
    out = []
    for comp in _cycles(edges):
        members = set(comp)
        eqs = sorted({via[(a, b)][0] for a in comp for b in edges[a] if b in members})
        pages = [via[(a, b)][1] for a in comp for b in edges[a] if b in members and via[(a, b)][1]]
        out.append(Finding(
            "dependency_cycle", "|".join(comp),
            f"parameters {', '.join(comp)} depend on each other through {', '.join(eqs)}",
            [("Parameter", p) for p in comp] + [("Equation", e) for e in eqs],
            detail="one of these equations probably has COMPUTES and USES swapped, or solves for an input",
            page=min(pages) if pages else None))
    return out


def run_checks(graph) -> FindingSet:
    found = FindingSet(set(CHECK_KINDS))
    for check in (undefined_parameters, missing_references, dependency_cycles):
        for f in check(graph):
            found.add(f)
    return found
