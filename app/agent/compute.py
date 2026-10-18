"""``compute``: every number the agent reports comes from here, never from Claude.

Equations are evaluated in SymPy from their stored form, with inputs taken only from
the session facts; a missing input is reported, not guessed. An equation with a high
equation flag (its LaTeX and SymPy forms disagree, or it does not parse) is not
computed: the result carries the flag, the LaTeX and the crop instead.

Tables: each row's conditions are checked against the facts (``app.agent.conditions``).
A row is picked when its conditions all hold, or Claude names it with a reason (a
description match); a pick that contradicts a decided condition is refused. When the
fact falls between two rows that differ only in one threshold and a footnote allows
interpolation, the values are interpolated linearly. Values are mapped to header
parameters by column; aligning by order is a fallback, used only when the row has as
many values as the table has header parameters, and it is labelled. A header parameter
that names a condition column (it holds the row's condition text, not a value) is never
returned as a value.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from app.agent.conditions import FALSE, TRUE, UNKNOWN, Evaluation, evaluate_all, norm
from app.agent.facts import Fact, FactStore, RowMatch
from app.agent.results import ASSUMED, Label, ToolResult, Touched, clean, flag_reasons, node_label
from app.ingest.pass2.validate import SEVERITY, is_blank, parse_relation

ORDER = "aligned by order"
EQUATION_CODES = {c for c, s in SEVERITY.items()
                  if s == "high" and (c.startswith("equation") or c in ("sympy_parse", "undefined_symbol"))}
DIMENSIONLESS = {"", "-", "g", "rad", "radian", "radians", "unitless", "dimensionless", "none", "ratio"}
MAX_CANDIDATES = 12


def fmt(x: float) -> str:
    return f"{x:.6g}"


def _flags(value) -> list[dict]:
    out = []
    for f in value or []:
        try:
            out.append(json.loads(f) if isinstance(f, str) else dict(f))
        except (ValueError, TypeError):
            continue
    return out


def _props(graph, query: str, params: dict) -> list[dict]:
    return [row[0].properties if hasattr(row[0], "properties") else row[0]
            for row in graph.ro_query(query, params).result_set]


# ---- units -------------------------------------------------------------------------------

def unit_tokens(units: str | None) -> list[str]:
    """'kips (kN)' -> ['kips', 'kN']."""
    return re.findall(r"[A-Za-z][A-Za-z0-9.²³/^-]*", units or "")


def is_dimensionless(unit: str | None) -> bool:
    return (unit or "").strip().lower() in DIMENSIONLESS


def result_unit(expr, inputs: dict[str, Fact]) -> str | None:
    """The unit of ``expr`` from its inputs' units ('' when dimensionless), or None when
    the units do not combine (terms with different units)."""
    import sympy

    subs = {sympy.Symbol(n): (1 if is_dimensionless(f.unit) else sympy.Symbol((f.unit or "").strip()))
            for n, f in inputs.items()}
    u = sympy.simplify(expr.subs(subs))
    if u.is_number:
        return ""
    terms = u.as_ordered_terms() if u.is_Add else [u]
    parts = {t.as_coeff_Mul()[1] for t in terms}
    return str(parts.pop()).replace("**", "^") if len(parts) == 1 else None


# ---- equations ------------------------------------------------------------------------------

def find_equations(graph, target: str) -> list[dict]:
    eqs = _props(graph, "MATCH (e:Equation {id: $id}) RETURN e", {"id": target})
    if not eqs:
        eqs = _props(graph, "MATCH (e:Equation) WHERE e.number = $id RETURN e", {"id": target})
    if not eqs:
        eqs = _props(graph, "MATCH (e:Equation)-[:COMPUTES]->(:Parameter {id: $id}) RETURN e ORDER BY e.page",
                     {"id": target})
    return eqs


def high_equation_flags(eq: dict, clause: dict | None) -> list[dict]:
    """High equation flags on the equation, or on its clause naming this equation."""
    names = [n for n in (eq.get("id"), eq.get("number")) if n]
    out = [f for f in _flags(eq.get("flags")) if f.get("reason") in EQUATION_CODES]
    for f in _flags((clause or {}).get("flags")):
        if f.get("reason") in EQUATION_CODES and any(n in str(f.get("detail", "")) for n in names):
            out.append(f)
    return out


def compute_equation(graph, target: str, facts: dict[str, Fact], solve_for: str | None = None) -> ToolResult:
    import sympy

    eqs = find_equations(graph, target)
    if not eqs:
        return ToolResult(f"no equation found for {target!r}", {"target": target}, error=True)
    if len(eqs) > 1:
        return ToolResult(f"{len(eqs)} equations compute {target}; call compute with one equation id",
                          {"choices": [{"id": e["id"], "latex": e.get("latex"), "page": e.get("page")} for e in eqs]})
    eq = eqs[0]
    eid = eq["id"]
    t = Touched()
    t.node("Equation", eid)
    clause = next(iter(_props(graph, "MATCH (c:Clause)-[:CONTAINS]->(:Equation {id: $id}) RETURN c", {"id": eid})), None)
    if clause:
        t.node("Clause", clause["id"])
        t.edge(clause["id"], "CONTAINS", eid)
    labels = [x for x in (node_label(eq, "Equation"), node_label(clause or {}, "Clause")) if x]
    base = {"equation": eid, "latex": eq.get("latex"), "sympy": eq.get("sympy"), "page": eq.get("page"),
            "crop": eq.get("crop"), "clause": clause["id"] if clause else None,
            "clause_title": clause.get("title") if clause else None}

    if high := high_equation_flags(eq, clause):
        reasons = "; ".join(f"{f.get('reason')}: {str(f.get('detail', ''))[:200]}" for f in high)
        if not any(x.target == eid for x in labels):
            labels.append(Label(eid, "unreviewed", f"Equation flagged high: {reasons}"))
        return ToolResult(f"not computed: {eid} is flagged high ({', '.join(f['reason'] for f in high)}); "
                          f"report its LaTeX and crop, labelled unreviewed",
                          {**base, "computed": False, "flags": high}, t, labels)
    try:
        lhs, rhs = parse_relation(eq.get("sympy") or "")
    except ValueError as exc:
        return ToolResult(f"not computed: {eid} has no usable SymPy form ({exc})", {**base, "computed": False},
                          t, labels, error=True)
    if rhs.atoms(sympy.Sum) or lhs.atoms(sympy.Sum):
        return ToolResult(f"not computed: {eid} sums over levels; compute supports scalar inputs only",
                          {**base, "computed": False}, t, labels)

    params = {p["id"]: p for p in _props(graph, "MATCH (:Equation {id: $id})-[:USES|COMPUTES]->(p:Parameter) "
                                                "RETURN p", {"id": eid})}
    target_sym = sympy.Symbol(solve_for) if solve_for else (lhs if lhs.is_Symbol else None)
    if target_sym is None:
        return ToolResult(f"not computed: say which symbol of {eid} to solve for", {**base, "computed": False},
                          t, labels, error=True)
    if lhs == target_sym:
        expr = rhs
    else:
        solutions = sympy.solve(sympy.Eq(lhs, rhs), target_sym)
        if len(solutions) != 1:
            return ToolResult(f"not computed: {eid} has {len(solutions)} solutions for {target_sym}",
                              {**base, "computed": False}, t, labels, error=True)
        expr = solutions[0]
    for pid in params:
        t.node("Parameter", pid)
    if target_sym.name in params:
        t.edge(eid, "COMPUTES", target_sym.name)

    needed = sorted(s.name for s in expr.free_symbols)
    for name in needed:
        if name in params:
            t.edge(eid, "USES", name)
    missing = [n for n in needed if n not in facts or facts[n].number() is None]
    if missing:
        where = {w.get("symbol"): w.get("definition") for w in
                 _props(graph, "MATCH (:Equation {id: $id})-[:WHERE]->(w:WhereItem) RETURN w", {"id": eid})}
        info = []
        for n in missing:
            p = params.get(n, {})
            definition = next((d for s, d in where.items() if s and _same_symbol(s, n, p)), None)
            info.append({"id": n, "symbol": p.get("symbol"), "description": p.get("description"),
                         "units": p.get("units"), "where_text": definition})
        return ToolResult(f"missing inputs for {eid}: {', '.join(missing)}; ask the user, or offer typical values "
                          f"labelled as assumptions", {**base, "computed": False, "missing": info}, t, labels)

    inputs = {n: facts[n] for n in needed}
    warnings = []
    for n, fact in inputs.items():
        declared = unit_tokens(params.get(n, {}).get("units"))
        if fact.unit and declared and not is_dimensionless(fact.unit) and \
                fact.unit.lower() not in {d.lower() for d in declared}:
            warnings.append(f"{n} is in {fact.unit}, the code gives {params[n].get('units')}; no conversion made")
        if fact.source == ASSUMED:
            labels.append(Label(n, ASSUMED, f"{n} = {fact.value} is an accepted typical value, not from the code"))
        if fact.source == "agent_selected":
            labels.append(Label(n, "inputs selected by agent",
                                f"{n} = {fact.value} was picked by the agent from a table it could not map"))
    value = float(expr.subs({sympy.Symbol(n): sympy.Float(f.number()) for n, f in inputs.items()}))
    unit = result_unit(expr, inputs)
    if unit is None:
        warnings.append("the input units do not combine; result unit not checked")
    data = {**base, "computed": True,
            "inputs": [{"id": n, "value": f.value, "unit": f.unit, "source": f.source} for n, f in inputs.items()],
            "result": {"id": target_sym.name, "value": value, "display": fmt(value), "unit": unit or None},
            "warnings": warnings}
    summary = f"{eid}: {target_sym.name} = {fmt(value)}{(' ' + unit) if unit else ''}"
    return ToolResult(summary, data, t, labels)


def _same_symbol(where_symbol: str, pid: str, param: dict) -> bool:
    key = lambda s: re.sub(r"[\s_{}′']", "", s or "").lower()  # noqa: E731
    return key(where_symbol) in {key(pid), key(param.get("symbol"))}


# ---- tables -------------------------------------------------------------------------------

@dataclass
class Header:
    id: str
    cols: list[int]
    header_text: str
    units: str | None
    order: int


def table_headers(graph, table_id: str) -> list[Header]:
    rows = graph.ro_query("MATCH (:Table {id: $id})-[e:GIVES]->(p:Parameter) "
                          "RETURN p.id, coalesce(e.cols, [e.col]), e.header_text, e.units ORDER BY e.col",
                          {"id": table_id}).result_set
    return [Header(pid, sorted(c for c in cols if c is not None), text or pid, units, i)
            for i, (pid, cols, text, units) in enumerate(rows)]


def condition_params(rows: list[dict], headers: list[Header]) -> set[str]:
    """Header parameters that name condition columns. A candidate: in some row, every one
    of its columns holds a grid cell that is not one of the row's values and whose text
    is part of the row's conditions (e.g. 'C' in 'Conforming transverse reinforcement').
    A candidate stays a value parameter if one of its columns holds a value in some row
    that no other value parameter claims (merged-cell grids put labels and values in the
    same column for different row groups)."""
    candidates = set()
    for h in headers:
        for row in rows:
            cells = row.get("_cells") or []
            value_cols = {v["col"] for v in row["_values"]}
            conds = norm(" ".join(row["_conditions"]))
            if h.cols and all(c < len(cells) and not is_blank(cells[c]) and c not in value_cols
                              and norm(cells[c]) in conds for c in h.cols):
                candidates.add(h.id)
                break
    used_cols = {v["col"] for row in rows for v in row["_values"]}
    out = set()
    for h in headers:
        if h.id not in candidates:
            continue
        claimed = all(any(o.id != h.id and o.id not in candidates and c in o.cols for o in headers)
                      for c in h.cols if c in used_cols)
        if claimed:
            out.add(h.id)
    return out


def map_columns(value_cols: list[int], headers: list[Header],
                exclude: set[str] | frozenset = frozenset()) -> tuple[dict[int, Header], str | None]:
    """Each value column -> a header parameter, never one in ``exclude`` (condition
    parameters). By column first: a column shared by two parameters (merged-cell grids
    list a quantity once per column) goes to the one listed for several columns, then
    the first, each parameter used once per row. If that fails, align by order, only
    when the counts match. Returns (mapping, method)."""
    headers = [h for h in headers if h.id not in exclude]
    used: set[str] = set()
    mapping: dict[int, Header] = {}
    for col in sorted(value_cols):
        cands = [h for h in headers if col in h.cols and h.id not in used]
        if not cands:
            break
        best = sorted(cands, key=lambda h: (len(h.cols) == 1, h.cols[0], h.order))[0]
        mapping[col] = best
        used.add(best.id)
    else:
        return mapping, "column"
    if len(value_cols) == len(headers):
        ordered = sorted(headers, key=lambda h: (h.cols[0] if h.cols else 0, h.order))
        return dict(zip(sorted(value_cols), ordered)), ORDER
    return {}, None


def _row_index(row_id: str) -> int:
    return int(row_id.rsplit("#r", 1)[1])


def _load_table(graph, table_id: str):
    tables = _props(graph, "MATCH (t:Table {id: $id}) RETURN t", {"id": table_id})
    if not tables:
        return None, [], []
    rows = []
    for props in _props(graph, "MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row) WHERE r.values IS NOT NULL RETURN r",
                        {"id": table_id}):
        props["_conditions"] = json.loads(props.get("conditions") or "[]")
        props["_values"] = json.loads(props.get("values") or "[]")
        props["_cells"] = json.loads(props.get("cells") or "[]")
        rows.append(props)
    rows.sort(key=lambda r: _row_index(r["id"]))
    notes = _props(graph, "MATCH (:Table {id: $id})-[:HAS_FOOTNOTE]->(f:Footnote) RETURN f", {"id": table_id})
    return tables[0], rows, notes


def _rule_applies(note: dict, index: int) -> bool:
    rows = note.get("applies_to_rows") or []
    return not rows or index in rows


def _interpolation_partner(row: dict, evals: list[Evaluation], rows: list[dict], facts: dict[str, Fact],
                           notes: list[dict]):
    """For a row whose only false conditions are one threshold, the row on the other side
    of the fact's value with the same other conditions, if a footnote allows interpolation."""
    false = [e for e in evals if e.status == FALSE]
    if len(false) != 1 or false[0].lhs_value is None or false[0].rhs_value is None:
        return None
    pivot = false[0]
    others = sorted(c for c in row["_conditions"] if c != pivot.text)
    index = _row_index(row["id"])
    rule = next((n for n in notes if n.get("rule_kind") == "interpolation" and _rule_applies(n, index)), None)
    if rule is None:
        return None
    for other in rows:
        if other is row:
            continue
        o_evals = evaluate_all(other["_conditions"], facts)
        o_false = [e for e in o_evals if e.status == FALSE]
        if len(o_false) != 1 or o_false[0].key != pivot.key or o_false[0].rhs_value is None:
            continue
        if sorted(c for c in other["_conditions"] if c != o_false[0].text) != others:
            continue
        lo, hi = sorted([pivot.rhs_value, o_false[0].rhs_value])
        if lo < pivot.lhs_value < hi and _rule_applies(rule, _row_index(other["id"])):
            return other, pivot, o_false[0], rule
    return None


def _values(row: dict, headers: list[Header], exclude: set[str]) -> tuple[list[dict], str | None]:
    cols = [v["col"] for v in row["_values"]]
    mapping, method = map_columns(cols, headers, exclude)
    out = []
    for v in row["_values"]:
        h = mapping.get(v["col"])
        out.append({"param": h.id if h else None, "header": h.header_text if h else None, "value": v.get("value"),
                    "number": v.get("number"), "unit": v.get("unit") or (h.units if h else None), "col": v["col"],
                    "source": v.get("source")})
    return out, method


def compute_table(graph, table_id: str, store: FactStore, row_id: str | None = None, reason: str | None = None,
                  columns: list[str] | None = None, save_as_facts: bool = False) -> ToolResult:
    table, rows, notes = _load_table(graph, table_id)
    if table is None:
        return ToolResult(f"no table {table_id!r}", {"table": table_id}, error=True)
    facts = store.all()
    headers = table_headers(graph, table_id)
    conditions = condition_params(rows, headers)
    t = Touched()
    t.node("Table", table_id)
    labels = [x for x in [node_label(table, "Table")] if x]
    base = {"table": table_id, "caption": table.get("caption"), "pages": table.get("pages") or [table.get("page")],
            "header_parameters": [{"id": h.id, "header": h.header_text, "cols": h.cols, "units": h.units}
                                  for h in headers if h.id not in conditions],
            "condition_parameters": sorted(conditions)}
    evals = {r["id"]: evaluate_all(r["_conditions"], facts) for r in rows}

    if row_id is None:
        decided = [r for r in rows if evals[r["id"]] and all(e.status == TRUE for e in evals[r["id"]])]
        if len(decided) == 1:
            row_id, reason = decided[0]["id"], reason or "every condition of the row holds for the session facts"
        else:
            cands = [r for r in rows if not any(e.status == FALSE for e in evals[r["id"]])]
            for r in cands[:MAX_CANDIDATES]:
                t.node("Row", r["id"])
                t.edge(table_id, "HAS_ROW", r["id"])
            data = {**base, "candidates": [{"row": r["id"], "conditions": [e.as_dict() for e in evals[r["id"]]]}
                                           for r in cands[:MAX_CANDIDATES]], "more": max(0, len(cands) - MAX_CANDIDATES)}
            return ToolResult(f"{table_id}: {len(cands)} rows fit the facts; pick one with row_id and a reason "
                              f"(say which row and why), or save the facts that decide it", data, t, labels)

    row = next((r for r in rows if r["id"] == row_id), None)
    if row is None:
        return ToolResult(f"{table_id} has no row {row_id!r} with values", base, t, labels, error=True)
    row_evals = evals[row_id]
    t.node("Row", row_id)
    t.edge(table_id, "HAS_ROW", row_id)
    if node_label(row, "Row") and not labels:
        labels.append(node_label(row, "Row"))
    values, method = _values(row, headers, conditions)
    interpolation = None
    if any(e.status == FALSE for e in row_evals):
        found = _interpolation_partner(row, row_evals, rows, facts, notes)
        if found is None:
            bad = [e.as_dict() for e in row_evals if e.status == FALSE]
            return ToolResult(f"{row_id} contradicts the session facts: "
                              + "; ".join(f"{b['condition']} is false" for b in bad),
                              {**base, "row": row_id, "conditions": [e.as_dict() for e in row_evals]}, t, labels,
                              error=True)
        other, pivot, o_pivot, rule = found
        t.node("Row", other["id"])
        t.edge(table_id, "HAS_ROW", other["id"])
        values, method, interpolation = _interpolate(row, other, pivot, o_pivot, headers, conditions)
        interpolation["footnote"] = rule.get("marker")
        t.node("Footnote", rule.get("id"))
        t.edge(table_id, "HAS_FOOTNOTE", rule.get("id"))
    if any(e.status == UNKNOWN for e in row_evals) and not reason:
        return ToolResult(f"{row_id}: some conditions are descriptions; call again with a reason saying why the row "
                          f"matches the user's description", {**base, "row": row_id,
                                                             "conditions": [e.as_dict() for e in row_evals]}, t, labels,
                          error=True)
    if method is None:
        labels.append(Label(row_id, "unreviewed", "values could not be mapped to header parameters"))
    elif method == ORDER:
        labels.append(Label(row_id, ORDER, "values matched to header parameters by order, not by column"))
    if columns:
        values = [v for v in values if v["param"] in columns]
    for v in values:
        if v["param"]:
            t.node("Parameter", v["param"])
            t.edge(table_id, "GIVES", v["param"])
    applying = []
    for n in notes:
        if _rule_applies(n, _row_index(row_id)) and n.get("rule_kind"):
            applying.append({"marker": n.get("marker"), "kind": n.get("rule_kind"), "rule": n.get("rule")})
            t.node("Footnote", n.get("id"))
            t.edge(table_id, "HAS_FOOTNOTE", n.get("id"))
    blanks = json.loads(row.get("blank_cells") or "[]")
    if blanks:
        labels.append(Label(row_id, "unreviewed", "blank-cell interpretations are unreviewed: "
                            + "; ".join(b.get("interpretation", "")[:80] for b in blanks)))
    store.match_row(RowMatch(row_id, table_id, reason or ""))
    saved = []
    if save_as_facts:
        for v in values:
            if v["param"] and v["number"] is not None:
                store.set(Fact(v["param"], v["number"], v["unit"], "matched_row", label=v["header"]))
                saved.append(v["param"])
    data = {**base, "row": row_id, "page": row.get("page"), "conditions": [e.as_dict() for e in row_evals],
            "reason": reason, "values": values, "column_mapping": method, "footnotes": applying,
            "blank_cells": blanks, "interpolation": interpolation, "saved_as_facts": saved}
    shown = ", ".join(f"{v['param'] or 'col ' + str(v['col'])}={v['value']}" for v in values[:10])
    return ToolResult(f"{table_id} {row_id.rsplit('#', 1)[1]}{' (interpolated)' if interpolation else ''}: {shown}",
                      data, t, labels)


def _interpolate(row, other, pivot: Evaluation, o_pivot: Evaluation, headers, exclude):
    """Linear interpolation of each mapped value between two rows at the fact's value."""
    a_vals, method = _values(row, headers, exclude)
    b_vals, _ = _values(other, headers, exclude)
    by_param = {v["param"]: v for v in b_vals if v["param"]}
    x, xa, xb = pivot.lhs_value, pivot.rhs_value, o_pivot.rhs_value
    out = []
    for v in a_vals:
        w = by_param.get(v["param"])
        if v["number"] is None or w is None or w["number"] is None:
            out.append({**v, "interpolated": False})
            continue
        value = v["number"] + (x - xa) / (xb - xa) * (w["number"] - v["number"])
        out.append({**v, "value": fmt(value), "number": value, "interpolated": True,
                    "between": [v["number"], w["number"]]})
    detail = {"quantity": pivot.lhs, "value": x, "rows": [row["id"], other["id"]], "thresholds": [xa, xb]}
    return out, method, detail


def table_row_props(graph, row_id: str) -> dict:
    found = _props(graph, "MATCH (r:Row {id: $id}) RETURN r", {"id": row_id})
    return clean(found[0]) if found else {}


__all__ = ["compute_equation", "compute_table", "map_columns", "Header", "ORDER", "flag_reasons"]
