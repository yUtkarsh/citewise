"""``plan_procedure``: a backward walk from a goal (a table, equation, parameter or
clause) to the ordered steps that lead to it, dependencies first.

What counts as a dependency, by node kind:
- a parameter depends on the equations that compute it and the tables that give it (up
  to MAX_SOURCES, nearest the goal's page first; the full count goes into a 'source'
  decision), and on the clauses that define it (up to MAX_DEFINING, nearest first;
  listed as steps, not expanded);
- an equation depends on its inputs;
- a table depends on the clause that contains it (it governs how the table is used), or
  without one, on the clauses that require its values;
- a clause depends on the equations it contains, on every equation it references
  (referring to an equation is using it, typed or not), and on the clauses and tables it
  references as ``requires`` or ``uses_value_from``; a table it contains, and a
  sub-clause of the goal clause, come after it.
  A top-level clause (a chapter) is listed as a step but not expanded. Moving into a
  sub-clause of the goal does not count toward MAX_DEPTH.

Steps are clauses, tables and equations; parameters become each step's inputs, marked
known when a session fact gives them. Ties are ordered by page. Decision points are
found from the graph, not guessed: a parameter with more than one source, a reference
typed ``alternative_to`` or ``exception_to``, a table with several header columns (which
column applies), and a table with several rows (which row matches).
"""
from __future__ import annotations

from collections import defaultdict
import json
import re

from app.agent.facts import Fact
from app.agent.graph_tools import find_node
from app.agent.results import ToolResult, Touched, edge_label, node_label
from app.agent.search import STOPWORDS, find_entry

MAX_DEPTH = 4
MAX_NODES = 90
MAX_DEFINING = 3  # defining clauses per parameter
MAX_SOURCES = 3  # computing equations and giving tables per parameter
STEP_LABELS = ("Clause", "Table", "Equation")
DEP_TYPES = ["requires", "uses_value_from"]
DECISION_TYPES = ["alternative_to", "exception_to"]
ALTERNATIVES_SHOWN = 3
MIN_SHARED, MIN_COVERAGE = 2, 0.6  # a clause title matches a text goal
SMALL_PLAN = 6  # fewer steps than this from a text goal: tell the model to retry with a clause id
GOAL_STOPWORDS = STOPWORDS | {"evaluate", "evaluating", "evaluation", "existing", "calculate", "calculating",
                              "determine", "procedure", "step", "steps", "wise", "using", "how", "get", "find",
                              "building", "built"}

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
        "MATCH (n:Clause {id: $id})-[e:REFERENCES]->(x) WHERE e.type IN $types AND (x:Clause OR x:Table) "
        "RETURN labels(x)[0], x, n.id, 'REFERENCES', x.id, e",
        "MATCH (n:Clause {id: $id})-[e:REFERENCES]->(x:Equation) WHERE NOT coalesce(e.text, '') STARTS WITH 'link to p' "
        "AND (e.type IS NULL OR NOT e.type IN ['see_also', 'alternative_to', 'exception_to']) "
        "RETURN 'Equation', x, n.id, 'REFERENCES', x.id, e",
    ],
}
_COLS = "AS label, x AS node, {src} AS src, '{kind}' AS kind, {dst} AS dst, e AS edge"
DEFINING = (f"MATCH (n:Parameter {{id: $id}})-[e:DEFINED_IN]->(x:Clause) RETURN 'Clause' "
            + _COLS.format(src="n.id", kind="DEFINED_IN", dst="x.id")
            + f" UNION MATCH (x:Clause)-[e:DEFINES]->(n:Parameter {{id: $id}}) RETURN 'Clause' "
            + _COLS.format(src="x.id", kind="DEFINES", dst="n.id"))
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


def _words(text: str) -> set[str]:
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", text or "")}
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words - GOAL_STOPWORDS if len(w) > 1}


def title_matches(graph, goal: str) -> list[dict]:
    """Clauses whose title the goal covers: at least MIN_SHARED title words appear in the
    goal and they make up at least MIN_COVERAGE of the title. Best first: more shared
    words, fuller cover, provisions before commentary, shallower clauses."""
    want = _words(goal)
    out = []
    for cid, title, stream, depth, page in graph.ro_query(
            "MATCH (c:Clause) WHERE c.title IS NOT NULL RETURN c.id, c.title, c.stream, c.depth, c.page").result_set:
        have = _words(title)
        shared = want & have
        if len(shared) >= MIN_SHARED and len(shared) / len(have) >= MIN_COVERAGE:
            out.append({"label": "Clause", "id": cid, "title": title, "page": page,
                        "why": f"title matches the goal ({', '.join(sorted(shared))})",
                        "_rank": (-len(shared), -len(shared) / len(have), stream == "commentary", depth or 9, cid)})
    return sorted(out, key=lambda x: x["_rank"])


def resolve_goal(graph, goal: str, encode) -> tuple[tuple[str, dict] | None, list[dict]]:
    """A node id resolves to itself. Free text goes through find_entry, and a clause whose
    title matches the goal is preferred over the search's top hit. Returns the goal and
    up to three alternatives."""
    found = find_node(graph, goal)
    if found:
        return found, []
    hits = [{**h, "why": "find_entry " + "+".join(h.get("matched_by", []))} for h in find_entry(
        graph, goal, encode, k=12, labels=["Table", "Equation", "Clause", "Parameter"]).data["candidates"]]
    ranked, seen = [], set()
    for c in title_matches(graph, goal)[:6] + hits:
        if c["id"] not in seen:
            seen.add(c["id"])
            ranked.append({k: v for k, v in c.items() if k in ("label", "id", "title", "caption", "symbol", "why")})
    if not ranked:
        return None, []
    return find_node(graph, ranked[0]["id"]), ranked[1:1 + ALTERNATIVES_SHOWN]


def walk(graph, start: tuple[str, str], t: Touched, labels: list, source_counts: dict | None = None):
    """Breadth-first walk of DEPENDS_ON from start. Returns (nodes, deps): nodes maps
    (label, id) -> props; deps maps a node to the nodes it depends on. ``source_counts``
    collects, per parameter, how many sources the code has when more than MAX_SOURCES."""
    nodes: dict[tuple[str, str], dict] = {}
    deps: dict[tuple[str, str], set] = defaultdict(set)
    frontier = [(start, 0)]
    nodes[start] = find_node(graph, start[1])[1]
    subtree = {start[1]} if start[0] == "Clause" else set()  # the goal clause and its sub-clauses
    leaves: set[tuple[str, str]] = set()  # defining clauses: steps, not expanded
    root_page = nodes[start].get("page") or 0
    while frontier and len(nodes) < MAX_NODES:
        (label, nid), level = frontier.pop(0)
        if level >= MAX_DEPTH or (label, nid) in leaves or \
                (label == "Clause" and nid not in subtree and (nodes[(label, nid)].get("depth") or 9) <= 1):
            continue
        if label == "Parameter":
            defining = graph.ro_query(DEFINING, {"id": nid}).result_set
            defining.sort(key=lambda r: (r[3] != "DEFINED_IN", abs((r[1].properties.get("page") or 0) - root_page)))
            for x_label, x, src, kind, dst, e in defining[:MAX_DEFINING]:
                key = (x_label, x.properties.get("id"))
                t.edge(src, kind, dst)
                deps[(label, nid)].add(key)
                if key not in nodes and len(nodes) < MAX_NODES:
                    nodes[key] = x.properties
                    t.node(*key)
                    leaves.add(key)
        queries = [(q, False) for q in DEPENDS_ON.get(label, [])]
        if label == "Clause":
            queries.append((CONTAINED_TABLES, True))
            if nid in subtree:
                queries.append((SUBCLAUSES, True))
        if label == "Table" and not graph.ro_query(queries[0][0], {"id": nid}).result_set:
            queries = [(TABLE_FALLBACK, False)]
        results = []
        for query, reverse in queries:
            results += [(row, reverse) for row in graph.ro_query(query, {"id": nid, "types": DEP_TYPES}).result_set]
        if label == "Parameter" and len(results) > MAX_SOURCES:
            if source_counts is not None:
                source_counts[nid] = len(results)
            results.sort(key=lambda rr: abs((rr[0][1].properties.get("page") or 0) - root_page))
            results = results[:MAX_SOURCES]
        for (x_label, x, src, kind, dst, e), reverse in results:
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
                frontier.append((key, level + (0 if kind == "PART_OF" else 1)))  # the goal's own parts: no hop
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
    source_counts: dict[str, int] = {}
    nodes, deps = walk(graph, root_key, t, labels, source_counts)
    step_deps = step_dependencies(nodes, deps)
    ordered = order(nodes, step_deps)
    number = {k: i + 1 for i, k in enumerate(ordered)}

    sources = defaultdict(list)  # parameter -> steps that compute or give it
    defines = defaultdict(list)  # clause -> parameters it defines
    for key, ds in deps.items():
        if key[0] == "Parameter":
            sources[key[1]] += [d for d in ds if d[0] in ("Equation", "Table")]
            for d in ds:
                if d[0] == "Clause":
                    defines[d].append(key[1])
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
                defined = sorted(number[c] for c in deps.get(d, ()) if c[0] == "Clause" and c in number)
                inputs.append({"id": d[1], "description": (nodes[d].get("description") or "")[:100],
                               "known": fact is not None, **({"value": fact.value, "unit": fact.unit} if fact else {}),
                               "from_steps": sorted(number[s] for s in sources.get(d[1], []) if s in number
                                                    and s[0] != "Clause"),
                               "defined_in_steps": defined})
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
            if defines.get(key):
                step["defines"] = sorted(defines[key])
            for xid, kind, text, title in graph.ro_query(ALTERNATIVES, {"id": nid, "types": DECISION_TYPES}).result_set:
                t.node("Clause", xid)
                t.edge(nid, "REFERENCES", xid)
                decisions.append({"kind": kind, "at": nid, "options": [nid, xid], "text": text, "title": title,
                                  "why": f"{nid} is an {kind.replace('_', ' ')} {xid}: which one applies is a choice"})
        steps.append(step)
    for pid, srcs in sources.items():
        if len({s for s in srcs}) > 1 or pid in source_counts:
            total = source_counts.get(pid, len(set(srcs)))
            decisions.append({"kind": "source", "at": pid, "options": sorted(s[1] for s in srcs),
                              "why": f"{pid} has {total} sources in the code" +
                                     (f"; the {len(set(srcs))} nearest are listed" if pid in source_counts else "")})
    missing = sorted({i["id"] for s in steps for i in s.get("inputs", []) if not i["known"] and not i["from_steps"]})
    data = {"goal": {"label": root_key[0], "id": root_key[1], "title": _title(root[0], root[1])},
            "steps": steps, "decisions": decisions, "inputs_needed": missing,
            "other_goal_candidates": alternatives,
            "note": "steps come from the graph's links only; if a step you expect is missing, the graph does not "
                    "link it here: find it with find_entry and say so"}
    summary = f"{len(steps)} steps to {root_key[1]}, {len(decisions)} decisions, {len(missing)} inputs needed"
    if root_key[1] != goal:  # resolved from text: say what was chosen and what else fits
        data["goal"]["resolved_from"] = goal
        summary += f"; goal {goal!r} resolved to {root_key[0]} {root_key[1]} ({data['goal']['title'][:60]})"
        if alternatives:
            summary += "; alternatives: " + ", ".join(a["id"] for a in alternatives)
        if len(steps) < SMALL_PLAN:
            data["retry"] = (f"only {len(steps)} steps: if that looks too small for the question, call plan_procedure "
                             "again with a clause id as the goal (an alternative above, or one found with find_entry)")
            summary += f". {data['retry']}"
    return ToolResult(summary, data, t, list({(x.target, x.label): x for x in labels}.values()))
