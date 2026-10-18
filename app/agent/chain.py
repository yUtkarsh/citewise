"""Chained compute (``compute`` with ``resolve_inputs: true``): an equation's missing
inputs are computed from what the graph says supplies them, recursively, up to
MAX_DEPTH levels, and every intermediate value is recorded with its source.

A missing input is resolved only when the graph leaves no judgment to make:

- exactly one source supplies it: one equation that COMPUTES it, or one table that GIVES
  it (a stated column, or an inferred GIVES edge naming the column it comes ``via``);
- an equation: not flagged high, an equality, and every input resolvable in turn;
- a table: the session facts decide the row (every condition of exactly one row holds;
  interpolation where a footnote permits it), and the row holds exactly one number in
  a column mapped to that parameter.

Anything else stops that branch, and the result lists what is missing and why: no
source, several sources (which equation or table applies is a choice), a row the facts
do not decide, a column that is not mapped (``calculate`` with columns the agent picks
is a judgment), a flagged equation, or the depth limit. Nothing is written to the
session facts: intermediate values exist only in the result's chain.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.compute import compute_equation, compute_table
from app.agent.facts import Fact, MemoryFacts
from app.agent.results import Label, ToolResult, Touched

MAX_DEPTH = 6
COMPUTED = "computed"


@dataclass
class Chain:
    facts: dict[str, Fact]
    steps: list[dict] = field(default_factory=list)  # every value found, inputs first
    missing: dict[str, str] = field(default_factory=dict)  # parameter -> why it could not be resolved
    touched: Touched = field(default_factory=Touched)
    labels: list[Label] = field(default_factory=list)


def _sources(graph, pid: str) -> tuple[list[str], list[tuple[str, str, bool]]]:
    eqs = [r[0] for r in graph.ro_query("MATCH (e:Equation)-[:COMPUTES]->(:Parameter {id: $id}) RETURN e.id "
                                        "ORDER BY e.id", {"id": pid}).result_set]
    tables = [(t, via or pid, method == "inferred") for t, via, method in graph.ro_query(
        "MATCH (t:Table)-[e:GIVES]->(:Parameter {id: $id}) RETURN t.id, e.via, e.method ORDER BY t.id",
        {"id": pid}).result_set]
    return eqs, list(dict.fromkeys(tables))


def _absorb(chain: Chain, result: ToolResult) -> None:
    for (label, nid) in result.touched.nodes:
        chain.touched.node(label, nid)
    for edge in result.touched.edges:
        chain.touched.edges[edge] = None
    chain.labels += result.labels


def resolve(graph, pid: str, chain: Chain, depth: int, path: tuple[str, ...]) -> bool:
    """Make ``chain.facts[pid]`` known if the graph allows it without a judgment."""
    fact = chain.facts.get(pid)
    if fact is not None and fact.number() is not None:
        return True
    if pid in path:
        chain.missing[pid] = "circular: it depends on itself through " + " -> ".join(path + (pid,))
        return False
    if depth > MAX_DEPTH:
        chain.missing[pid] = f"depth limit {MAX_DEPTH} reached"
        return False
    eqs, tables = _sources(graph, pid)
    if len(eqs) + len(tables) == 0:
        chain.missing[pid] = "no equation or table in the graph supplies it: ask the user"
        return False
    if len(eqs) + len(tables) > 1:
        options = eqs + [t for t, _, _ in tables]
        chain.missing[pid] = (f"{len(options)} sources supply it ({', '.join(options[:8])}): which applies is a "
                              "judgment; choose one and compute it")
        return False
    if eqs:
        return _from_equation(graph, pid, eqs[0], chain, depth, path)
    return _from_table(graph, pid, *tables[0], chain)


def _from_equation(graph, pid: str, eid: str, chain: Chain, depth: int, path: tuple[str, ...]) -> bool:
    for _ in range(MAX_DEPTH + 1):  # resolve the inputs it reports missing, then evaluate it
        result = compute_equation(graph, eid, chain.facts, solve_for=pid)
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("computed"):
            _absorb(chain, result)
            r = data["result"]
            chain.facts[pid] = Fact(pid, r["value"], r.get("unit"), COMPUTED)
            chain.steps.append({"id": pid, "value": r["value"], "display": r["display"], "unit": r.get("unit"),
                                "source": {"equation": eid, "clause": data.get("clause"), "page": data.get("page")},
                                "inputs": data.get("inputs", []), "depth": depth})
            return True
        missing = [m["id"] for m in data.get("missing", [])]
        if not missing:  # flagged high, no SymPy form, an inequality, several solutions
            chain.missing[pid] = f"{eid} cannot be evaluated: {result.summary}"
            return False
        if not all([resolve(graph, m, chain, depth + 1, path + (pid,)) for m in missing]):
            chain.missing.setdefault(pid, f"{eid} needs {', '.join(m for m in missing if m in chain.missing)}")
            return False
    chain.missing[pid] = f"{eid}: inputs did not settle"
    return False


def _from_table(graph, pid: str, table_id: str, column: str, inferred: bool, chain: Chain) -> bool:
    result = compute_table(graph, table_id, MemoryFacts(dict(chain.facts)))
    data = result.data if isinstance(result.data, dict) else {}
    if result.error or not data.get("row"):
        chain.missing[pid] = f"{table_id}: the session facts do not decide the row ({result.summary[:160]})"
        return False
    if data.get("column_mapping") is None:
        chain.missing[pid] = f"{table_id} {data['row']}: values are not mapped to columns (a calculate judgment)"
        return False
    found = [v for v in data.get("values", []) if v.get("param") == column and v.get("number") is not None]
    if len(found) != 1:
        chain.missing[pid] = (f"{table_id} {data['row']}: {len(found)} numbers in column {column}; which one applies "
                              "is a judgment")
        return False
    _absorb(chain, result)
    v = found[0]
    if inferred:
        chain.touched.edge(table_id, "GIVES", pid)
        chain.labels.append(Label(f"{table_id}|GIVES|{pid}", "inferred",
                                  f"{table_id} column {column} supplies {pid}: an edge inferred by the global pass"))
    chain.facts[pid] = Fact(pid, v["number"], v.get("unit"), "matched_row", label=v.get("header"))
    chain.steps.append({"id": pid, "value": v["number"], "display": v.get("value"), "unit": v.get("unit"),
                        "source": {"table": table_id, "row": data["row"], "column": column, "inferred_column": inferred,
                                   "interpolated": bool(data.get("interpolation")), "page": data.get("page")},
                        "depth": None})
    return True


def compute_chained(graph, target: str, facts: dict[str, Fact], solve_for: str | None = None) -> ToolResult:
    """``compute`` on an equation, resolving its missing inputs first (see the module doc)."""
    first = compute_equation(graph, target, facts, solve_for)
    data = first.data if isinstance(first.data, dict) else {}
    if first.error or data.get("computed") or not data.get("missing"):
        return first
    chain = Chain(dict(facts))
    for m in data["missing"]:
        resolve(graph, m["id"], chain, 1, (data.get("result", {}).get("id") or solve_for or "",))
    final = compute_equation(graph, target, chain.facts, solve_for)
    _absorb(chain, final)
    t = final.touched
    for (label, nid) in chain.touched.nodes:
        t.node(label, nid)
    for edge in chain.touched.edges:
        t.edges[edge] = None
    labels = list({(x.target, x.label): x for x in final.labels + chain.labels}.values())
    out = {**final.data, "resolve_inputs": True,
           "chain": [{k: v for k, v in s.items() if v is not None} for s in chain.steps],
           "still_missing": [{"id": k, "why": v} for k, v in chain.missing.items()]}
    if final.data.get("computed"):
        summary = final.summary + f" (resolved {len(chain.steps)} inputs through the graph)"
    else:
        summary = (f"not computed: resolved {len(chain.steps)} inputs; still missing "
                   + "; ".join(f"{k} ({v[:90]})" for k, v in list(chain.missing.items())[:6]))
    return ToolResult(summary, out, t, labels)
