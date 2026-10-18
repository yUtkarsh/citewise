"""``plan_procedure``: a backward walk from a goal (a table, equation, parameter or
clause) to the ordered steps that lead to it, dependencies first.

What counts as a dependency, by node kind:
- a parameter depends on the equations that compute it and the tables that give it;
- an equation depends on its inputs;
- a table depends on the clause that contains it (it governs how the table is used), or
  without one, on the clauses that require its values;
- a clause depends on the equations it contains and on what it references as
  ``requires`` or ``uses_value_from``; a table it contains, and a sub-clause of the goal
  clause, come after it.
  A top-level clause (a chapter) is listed as a step but not expanded.

Steps are clauses, tables and equations; parameters become each step's inputs, marked
known when a session fact gives them. Ties are ordered by page. Decision points are
found from the graph, not guessed: a parameter with more than one source, a reference
typed ``alternative_to`` or ``exception_to``, a table with several header columns (which
column applies), and a table with several rows (which row matches).
"""
from __future__ import annotations

from collections import defaultdict
import json

from app.agent.facts import Fact
from app.agent.graph_tools import find_node
from app.agent.results import ToolResult, Touched, edge_label, node_label
from app.agent.search import find_entry

MAX_DEPTH = 4
MAX_NODES = 45
STEP_LABELS = ("Clause", "Table", "Equation")
DEP_TYPES = ["requires", "uses_value_from"]
DECISION_TYPES = ["alternative_to", "exception_to"]

# label -> Cypher returning (dependency label, dependency node, edge src, edge type, edge dst, edge)
DEPENDS_ON = {
    "Parameter": [
        "MATCH (x:Equation)-[e:COMPUTES]->(n:Parameter {id: $id}) RETURN 'Equation', x, x.id, 'COMPUTES', n.id, e",
        "MATCH (x:Table)-[e:GIVES]->(n:Parameter {id: $id}) RETURN 'Table', x, x.id, 'GIVES', n.id, e",
    ],
    "Equation": [
        "MATCH (n:Equation {id: $id})-[e:USES]->(x:Parameter) RETURN 'Parameter', x, n.id, 'USES', x.id, e",
    ],
    "Table": [
        "MATCH (x:Clause)-[e:CONTAINS]->(n:Table {id: $id}) RETURN 'Clause', x, x.id, 'CONTAINS', n.id, e",
    ],
    "Clause": [
        "MATCH (n:Clause {id: $id})-[e:CONTAINS]->(x:Equation) RETURN 'Equation', x, n.id, 'CONTAINS', x.id, e",
        "MATCH (n:Clause {id: $id})-[e:REFERENCES]->(x) WHERE e.type IN $types AND (x:Clause OR x:Table OR x:Equation) "
        "RETURN labels(x)[0], x, n.id, 'REFERENCES', x.id, e",
    ],
}
# Walked from a clause, but the dependency points back: the clause comes first.
CONTAINED_TABLES = "MATCH (n:Clause {id: $id})-[e:CONTAINS]->(x:Table) RETURN 'Table', x, n.id, 'CONTAINS', x.id, e"
TABLE_FALLBACK = ("MATCH (x:Clause)-[e:REFERENCES]->(n:Table {id: $id}) WHERE e.type IN $types "
                  "RETURN 'Clause', x, x.id, 'REFERENCES', n.id, e")
SUBCLAUSES = "MATCH (x:Clause)-[e:PART_OF]->(n:Clause {id: $id}) RETURN 'Clause', x, x.id, 'PART_OF', n.id, e"
ALTERNATIVES = ("MATCH (n:Clause {id: $id})-[e:REFERENCES]->(x) WHERE e.type IN $types "
                "RETURN x.id, e.type, e.text, coalesce(x.title, x.caption)")


def _title(label: str, p: dict) -> str:
    if label == "Equation":
        return p.get("latex") or ""
    return (p.get("title") or p.get("caption") or p.get("description") or "")[:140]


def resolve_goal(graph, goal: str, encode) -> tuple[tuple[str, dict] | None, list[dict]]:
    found = find_node(graph, goal)
    if found:
        return found, []
    hits = find_entry(graph, goal, encode, labels=["Table", "Equation", "Clause", "Parameter"]).data["candidates"]
    if not hits:
        return None, []
    top = find_node(graph, hits[0]["id"])
    return top, hits[1:6]


def walk(graph, start: tuple[str, str], t: Touched, labels: list):
    """Breadth-first walk of DEPENDS_ON from start. Returns (nodes, deps): nodes maps
    (label, id) -> props; deps maps a node to the nodes it depends on."""
    nodes: dict[tuple[str, str], dict] = {}
    deps: dict[tuple[str, str], set] = defaultdict(set)
    frontier = [(start, 0)]
    nodes[start] = find_node(graph, start[1])[1]
    subtree = {start[1]} if start[0] == "Clause" else set()  # the goal clause and its sub-clauses
    while frontier and len(nodes) < MAX_NODES:
        (label, nid), level = frontier.pop(0)
        if level >= MAX_DEPTH or (label == "Clause" and nid not in subtree and (nodes[(label, nid)].get("depth") or 9) <= 1):
            continue
        queries = [(q, False) for q in DEPENDS_ON.get(label, [])]
        if label == "Clause":
            queries.append((CONTAINED_TABLES, True))
            if nid in subtree:
                queries.append((SUBCLAUSES, True))
        if label == "Table" and not graph.ro_query(queries[0][0], {"id": nid}).result_set:
            queries = [(TABLE_FALLBACK, False)]
        for query, reverse in queries:
            for x_label, x, src, kind, dst, e in graph.ro_query(query, {"id": nid, "types": DEP_TYPES}).result_set:
                if kind == "PART_OF":
                    subtree.add(x.properties.get("id"))
                key = (x_label, x.properties.get("id"))
                if key == (label, nid):
                    continue
                t.edge(src, kind, dst)
                if (lab := edge_label(src, kind, dst, e.properties)) is not None:
                    labels.append(lab)
                if reverse:
                    deps[key].add((label, nid))
                else:
                    deps[(label, nid)].add(key)
                if key not in nodes and len(nodes) < MAX_NODES:
                    nodes[key] = x.properties
                    t.node(*key)
                    frontier.append((key, level + 1))
    return nodes, deps


def step_dependencies(nodes, deps) -> dict[tuple, set]:
    """Dependencies between steps only: a parameter in between is passed through."""
    out = {}
    for key in nodes:
        if key[0] not in STEP_LABELS:
            continue
        seen, stack, found = set(), list(deps.get(key, ())), set()
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            if d[0] in STEP_LABELS:
                found.add(d)
            else:
                stack += list(deps.get(d, ()))
        out[key] = found - {key}
    return out


def order(nodes, step_deps) -> list[tuple]:
    """Dependencies first; among the steps that are ready, the lowest page first. A cycle
    is broken at its lowest page."""
    page = lambda k: (nodes[k].get("page") or 10 ** 6, k[1])  # noqa: E731
    remaining = {k: set(v) for k, v in step_deps.items()}
    done: list[tuple] = []
    while remaining:
        ready = sorted((k for k, v in remaining.items() if not (v & remaining.keys())), key=page)
        pick = ready[0] if ready else sorted(remaining, key=page)[0]
        done.append(pick)
        remaining.pop(pick)
    return done


def table_choices(graph, table_id: str) -> dict:
    gives = graph.ro_query("MATCH (:Table {id: $id})-[e:GIVES]->(p:Parameter) RETURN p.id, e.header_text "
                           "ORDER BY e.col", {"id": table_id}).result_set
    rows = graph.ro_query("MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row) WHERE r.values IS NOT NULL "
                          "RETURN r.conditions", {"id": table_id}).result_set
    groups = []
    for (conds,) in rows:
        first = (json.loads(conds or "[]") or [""])[0]
        if first and first not in groups:
            groups.append(first)
    return {"columns": [h or p for p, h in gives], "rows": len(rows), "row_groups": groups[:10]}


def plan_procedure(graph, goal: str, facts: dict[str, Fact], encode=None) -> ToolResult:
    root, alternatives = resolve_goal(graph, goal, encode)
    if root is None:
        return ToolResult(f"no node found for goal {goal!r}", {"goal": goal}, error=True)
    root_key = (root[0], root[1]["id"])
    t, labels = Touched(), []
    t.node(*root_key)
    nodes, deps = walk(graph, root_key, t, labels)
    step_deps = step_dependencies(nodes, deps)
    ordered = order(nodes, step_deps)
    number = {k: i + 1 for i, k in enumerate(ordered)}

    sources = defaultdict(list)  # parameter -> steps that compute or give it
    for key, ds in deps.items():
        if key[0] == "Parameter":
            sources[key[1]] += [d for d in ds if d[0] in STEP_LABELS]
    steps, decisions = [], []
    for key in ordered:
        label, nid = key
        p = nodes[key]
        if (lab := node_label(p, label)) is not None:
            labels.append(lab)
        step = {"step": number[key], "label": label, "id": nid, "title": _title(label, p), "page": p.get("page"),
                "after": sorted(number[d] for d in step_deps.get(key, ()) if d in number)}
        if p.get("status") == "flagged":
            step["status"] = "flagged"
        if label == "Equation":
            inputs = []
            for d in sorted(deps.get(key, ())):
                if d[0] != "Parameter":
                    continue
                fact = facts.get(d[1])
                inputs.append({"id": d[1], "description": (nodes[d].get("description") or "")[:100],
                               "known": fact is not None, **({"value": fact.value, "unit": fact.unit} if fact else {}),
                               "from_steps": sorted(number[s] for s in sources.get(d[1], []) if s in number)})
            step["inputs"] = inputs
        elif label == "Table":
            choice = table_choices(graph, nid)
            step["choose"] = choice
            if len(choice["columns"]) > 2:
                decisions.append({"kind": "column", "at": nid, "options": choice["columns"],
                                  "why": "the table gives several columns; the facts must say which applies"})
            if choice["rows"] > 1:
                decisions.append({"kind": "row", "at": nid, "options": choice["row_groups"],
                                  "why": "the row is matched to the component's description"})
        elif label == "Clause":
            for xid, kind, text, title in graph.ro_query(ALTERNATIVES, {"id": nid, "types": DECISION_TYPES}).result_set:
                t.node("Clause", xid)
                t.edge(nid, "REFERENCES", xid)
                decisions.append({"kind": kind, "at": nid, "options": [nid, xid], "text": text, "title": title,
                                  "why": f"{nid} is an {kind.replace('_', ' ')} {xid}: which one applies is a choice"})
        steps.append(step)
    for pid, srcs in sources.items():
        if len({s for s in srcs}) > 1:
            decisions.append({"kind": "source", "at": pid, "options": sorted(s[1] for s in srcs),
                              "why": f"{pid} has more than one source in the code"})
    missing = sorted({i["id"] for s in steps for i in s.get("inputs", []) if not i["known"] and not i["from_steps"]})
    data = {"goal": {"label": root_key[0], "id": root_key[1], "title": _title(root[0], root[1])},
            "steps": steps, "decisions": decisions, "inputs_needed": missing,
            "other_goal_candidates": alternatives,
            "note": "steps come from the graph's links only; if a step you expect is missing, the graph does not "
                    "link it here: find it with find_entry and say so"}
    summary = f"{len(steps)} steps to {root_key[1]}, {len(decisions)} decisions, {len(missing)} inputs needed"
    return ToolResult(summary, data, t, list({(x.target, x.label): x for x in labels}.values()))
