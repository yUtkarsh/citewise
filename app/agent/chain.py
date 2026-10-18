"""Chained compute (``compute`` with ``resolve_inputs: true``): an equation's missing
inputs are computed from what the graph says supplies them, recursively, up to
MAX_DEPTH levels, and every intermediate value is recorded with its source.

A missing input is resolved only when the graph leaves no judgment to make:

- exactly one source supplies it: one equation that COMPUTES it, or one table that GIVES
  it (a stated column, or an inferred GIVES edge naming the column it comes ``via``).
  Inequalities that COMPUTE it are limits, not sources. Where several equations and no
  table supply it, each equation's applicability conditions decide (see ``_select``):
  the conditions' inputs are resolved the same way, recursively, and the one equation
  whose conditions all hold is used; failing that, the equations are used only if every
  one evaluates and they all give the same value;
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

from dataclasses import dataclass, field, replace
import json
import re

from app.agent import conditions as cond
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
    params: list[tuple[str, set[str]]] | None = None  # every parameter's id and symbol keys, read once


def _is_equality(sympy_text: str) -> bool:
    """'lhs = rhs' at the top level (relations inside a Piecewise's parentheses do not count)."""
    top = sympy_text
    while re.search(r"\([^()]*\)", top):
        top = re.sub(r"\([^()]*\)", "", top)
    return "=" in top and not re.search(r"[<>!]", top)


def _sources(graph, pid: str) -> tuple[list[str], list[tuple[str, str, bool]]]:
    eqs = [eid for eid, sympy_text in graph.ro_query(
        "MATCH (e:Equation)-[:COMPUTES]->(:Parameter {id: $id}) RETURN e.id, e.sympy ORDER BY e.id",
        {"id": pid}).result_set if not sympy_text or _is_equality(sympy_text)]
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
    if len(eqs) > 1 and not tables:
        return _select(graph, pid, eqs, chain, depth, path)
    if len(eqs) + len(tables) > 1:
        options = eqs + [t for t, _, _ in tables]
        chain.missing[pid] = (f"{len(options)} sources supply it ({', '.join(options[:8])}): which applies is a "
                              "judgment; choose one and compute it")
        return False
    if eqs:
        return _from_equation(graph, pid, eqs[0], chain, depth, path)
    return _from_table(graph, pid, *tables[0], chain)


COMPARISON = re.compile(r"[\w.'′]+(?:\s*(?:<=|>=|≤|≥|<|>)\s*[\w.'′]+)+")
CONDITION_WORD = re.compile(r"\\text\{\s*(?:for|if|when|where)\b")
LATEX_OPS = [(r"\leq", "≤"), (r"\geq", "≥"), (r"\le", "≤"), (r"\ge", "≥"), (r"\lt", "<"), (r"\gt", ">")]
NUMBER = re.compile(r"[-+]?\d*\.?\d+")


def _plain_latex(latex: str) -> str:
    for macro, op in LATEX_OPS:
        latex = latex.replace(macro, op)
    latex = re.sub(r"\\text\{([^}]*)\}", r" \1 ", latex)
    return re.sub(r"\\(?:left|right|,|;|!)", " ", latex).replace("{", "").replace("}", "")


def comparisons(text: str) -> list[tuple[str, str, str]]:
    """Every comparison in a condition's words, chains split into pairs:
    'for 0 < T < T0' -> [('0', '<', 'T'), ('T', '<', 'T0')]."""
    out = []
    for match in COMPARISON.finditer(text.replace("<=", "≤").replace(">=", "≥")):
        parts = [p.strip().rstrip(".") for p in re.split(r"(≤|≥|<|>)", match.group(0))]
        out += [(parts[i], parts[i + 1], parts[i + 2]) for i in range(0, len(parts) - 2, 2)]
    return out


def _symbol_key(text: str) -> str:
    return re.sub(r"[\s_{}\\]+", "", text or "").replace("′", "'")


def _params(graph, chain: Chain) -> list[tuple[str, set[str]]]:
    if chain.params is None:
        chain.params = [(pid, {_symbol_key(k) for k in [pid, symbol, latex, *(aliases or [])] if k})
                        for pid, symbol, latex, aliases in graph.ro_query(
                            "MATCH (p:Parameter) RETURN p.id, p.symbol, p.latex, p.aliases").result_set]
    return chain.params


def _parameter_id(graph, token: str, near: set[str], chain: Chain) -> str | None:
    """The parameter a condition's symbol names: preferably one the equation uses, else the only match."""
    key = _symbol_key(token)
    found = [pid for pid, keys in _params(graph, chain) if key in keys]
    local = [pid for pid in found if pid in near]
    return (local or found)[0] if len(local or found) == 1 else None


def _equation_conditions(graph, eid: str) -> list[str]:
    """The words that say when an equation applies: its own ``conditions`` (when extracted),
    the condition written beside it in its LaTeX, and its clause's conditions unless they name
    a different equation of that clause."""
    row = graph.ro_query("MATCH (e:Equation {id: $id}) OPTIONAL MATCH (c:Clause)-[:CONTAINS]->(e) "
                         "RETURN e.number, e.latex, e.conditions, c.id LIMIT 1", {"id": eid}).result_set
    if not row:
        return []
    number, latex, own, clause = row[0]
    texts = list(json.loads(own) if isinstance(own, str) else own or [])
    found = CONDITION_WORD.search(latex or "")
    if found:
        texts.append(_plain_latex(latex[found.start():]))
    if clause:
        numbers = {n for (n,) in graph.ro_query("MATCH (:Clause {id: $c})-[:CONTAINS]->(e:Equation) WHERE e.number "
                                                 "IS NOT NULL RETURN e.number", {"c": clause}).result_set}
        for (text,) in graph.ro_query("MATCH (:Clause {id: $c})-[:HAS_CONDITION]->(x) RETURN x.text ORDER BY x.id",
                                      {"c": clause}).result_set:
            named = {n for n in numbers if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", text or "")}
            if not named or number in named:
                texts.append(text)
    return texts


def _value(side: str, facts: dict[str, Fact]) -> float | None:
    if NUMBER.fullmatch(side):
        return float(side)
    fact = facts.get(side)
    return fact.number() if fact is not None else None


def _check(graph, pid: str, eid: str, chain: Chain, depth: int, path: tuple[str, ...]) -> dict:
    """Evaluate one candidate equation's conditions, resolving the parameters they compare."""
    near = {r[0] for q in ("MATCH (:Equation {id: $id})-[:USES|COMPUTES]->(p:Parameter) RETURN p.id",
                           "MATCH (c:Clause)-[:CONTAINS]->(:Equation {id: $id}), (c)-[:DEFINES]->(p:Parameter) "
                           "RETURN p.id")
            for r in graph.ro_query(q, {"id": eid}).result_set}  # the parameters it uses, and those its clause defines
    evaluations, unchecked = [], []
    for text in _equation_conditions(graph, eid):
        pairs = comparisons(text)
        if not pairs:
            unchecked.append(text)
            continue
        for lhs, op, rhs in pairs:
            sides = []
            for side in (lhs, rhs):
                if NUMBER.fullmatch(side):
                    sides.append(side)
                    continue
                sid = _parameter_id(graph, side, near, chain)
                if sid and sid != pid:
                    resolve(graph, sid, chain, depth + 1, path + (pid,))
                sides.append(sid or side)
            left, right = (_value(s, chain.facts) for s in sides)
            ev = cond.Evaluation(f"{sides[0]} {op} {sides[1]}", cond.UNKNOWN, sides[0], op, sides[1], left, right)
            if left is None or right is None:
                ev.why = "not known: " + ", ".join(s for s, v in zip(sides, (left, right)) if v is None)
            else:
                ok = {"≤": left <= right, "≥": left >= right, "<": left < right, ">": left > right}[op]
                ev.status = cond.TRUE if ok else cond.FALSE
                ev.why = f"{sides[0]} = {left:g}, {sides[1]} = {right:g}"
            evaluations.append({**ev.as_dict(), "from": text})
    statuses = [e["status"] for e in evaluations]
    status = (cond.FALSE if cond.FALSE in statuses else
              cond.TRUE if statuses and all(s == cond.TRUE for s in statuses) else cond.UNKNOWN)
    return {"equation": eid, "status": status, "conditions": evaluations, "not_checked": unchecked}


def _fork(chain: Chain) -> Chain:
    return replace(chain, facts=dict(chain.facts), steps=list(chain.steps), missing=dict(chain.missing),
                   touched=Touched(), labels=[])


def _select(graph, pid: str, eqs: list[str], chain: Chain, depth: int, path: tuple[str, ...]) -> bool:
    """Several equations compute ``pid``: use the one whose conditions all hold, or, when the
    conditions decide nothing, all of them if each evaluates and they agree."""
    verdicts = [_check(graph, pid, eid, chain, depth, path) for eid in eqs]
    holds = [v for v in verdicts if v["status"] == cond.TRUE]
    live = [v["equation"] for v in verdicts if v["status"] != cond.FALSE]
    others = [{k: v[k] for k in ("equation", "status", "conditions")} for v in verdicts if v not in holds[:1]]
    if len(holds) == 1:
        if not _from_equation(graph, pid, holds[0]["equation"], chain, depth, path):
            return False
        chain.steps[-1]["selected_by"] = {"rule": "its conditions hold", "conditions": holds[0]["conditions"],
                                          "not_checked": holds[0]["not_checked"], "other_equations": others}
        return True
    if len(holds) == 0 and len(live) > 1:
        values = []
        for eid in live:
            trial = _fork(chain)
            if not _from_equation(graph, pid, eid, trial, depth, path):
                break
            values.append(trial.facts[pid].number())
        if len(values) == len(live) and all(abs(v - values[0]) <= 1e-6 * max(1.0, abs(values[0])) for v in values):
            _from_equation(graph, pid, live[0], chain, depth, path)
            chain.steps[-1]["selected_by"] = {"rule": f"its conditions decide nothing, but all {len(live)} applicable "
                                              f"equations give the same value", "agrees_with": live[1:],
                                              "other_equations": others}
            return True
    status = "; ".join(f"{v['equation']}: {v['status']}" + (
        f" ({', '.join(c['why'] for c in v['conditions'] if c['status'] == cond.UNKNOWN)[:80]})"
        if v["status"] == cond.UNKNOWN and v["conditions"] else "") for v in verdicts)
    chain.missing[pid] = (f"{len(eqs)} equations compute it and their conditions select "
                          f"{'none' if not holds else len(holds)} ({status}): which applies is a judgment; "
                          "choose one and compute it")
    return False


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
