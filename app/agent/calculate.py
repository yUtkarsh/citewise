"""``calculate``: arithmetic on values the agent selected, for when ``compute`` cannot map a
table's values itself (for example a column chosen by its header). Claude names the
operation and cites every input; this module checks each citation against the graph and
refuses on any mismatch, then does the arithmetic in SymPy. Claude never computes.

Inputs, each with a source:
- ``cell``: a table cell: table id, row id, column index, the value as printed, and the
  header text that justifies the column. The value must match the row's stored value at
  that column (or, without one, a number in the Pass 1 grid cell). The header's words and
  numbers must appear in the table's header (its header rows, the rows above its first
  values, its header parameters); where the grid has a header cell at that column, the
  header must sit in it, else the result says the column position could not be checked.
- ``fact``: a session fact, by id.
- ``number``: a constant written in the formula's source (for example the 2 of a mean) is
  not allowed: every number comes from a cited source.

Operations:
- ``formula``: a SymPy expression in the input names.
- ``interpolate``: linear interpolation of y at x between two cells; each cell's x is the
  number in its cited header. It needs a cited note, footnote or clause whose text permits
  interpolation, and x must lie between the two (no extrapolation).

Results are labelled "inputs selected by agent": the answer must state the choices.
"""
from __future__ import annotations

import json
import re

from app.agent.conditions import norm
from app.agent.facts import Fact
from app.agent.results import Label, ToolResult, Touched
from app.ingest.pass2.validate import numbers, parse_formula

SELECTED = "inputs selected by agent"
PERMIT = re.compile(r"interpolat", re.I)
TOL = 1e-9


class Refused(Exception):
    pass


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= TOL * max(1.0, abs(a), abs(b))


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]+", text or "")}


def header_region(graph, table_id: str) -> tuple[list[list[str]], list[str]]:
    """(header rows' cells by column, every header text of the table)."""
    rows = graph.ro_query("MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row) RETURN r.index, r.cells, r.is_header, r.values "
                          "ORDER BY r.index", {"id": table_id}).result_set
    first_values = next((i for i, (_, _, _, values) in enumerate(rows) if json.loads(values or "[]")), len(rows))
    head_rows = [json.loads(cells or "[]") for i, (_, cells, is_header, _) in enumerate(rows)
                 if is_header or i < first_values]
    gives = [t for (t,) in graph.ro_query("MATCH (:Table {id: $id})-[e:GIVES]->() "
                                          "WHERE coalesce(e.method, '') <> 'inferred' RETURN e.header_text",
                                          {"id": table_id}).result_set if t]
    return head_rows, [c for r in head_rows for c in r if c] + gives


def check_header(header: str, col: int, head_rows: list[list[str]], texts: list[str]) -> str:
    """'column' when a header cell at that column holds the header, 'table' when the header
    is in the table's header but the grid cannot place it; raises Refused otherwise."""
    if not header or not header.strip():
        raise Refused("every cell input needs the header text that justifies its column")
    pool = " ".join(texts)
    missing_words = _words(header) - _words(pool)
    pool_numbers = numbers(pool)
    missing_numbers = [n for n in numbers(header) if not any(_close(n, p) for p in pool_numbers)]
    if missing_words or missing_numbers:
        raise Refused(f"header {header!r} is not in the table's header (missing: "
                      f"{', '.join(sorted(missing_words) + [str(n) for n in missing_numbers])})")
    key = norm(header)
    if any(col < len(r) and r[col] and key in norm(r[col]) for r in head_rows):
        return "column"
    return "table"


def verify_cell(graph, item: dict, head_cache: dict, t: Touched) -> tuple[float, dict]:
    table_id, row_id, col = item.get("table"), item.get("row"), item.get("col")
    if not table_id or not row_id or col is None:
        raise Refused(f"input {item.get('name')!r}: a cell needs table, row and col")
    found = graph.ro_query("MATCH (:Table {id: $t})-[:HAS_ROW]->(r:Row {id: $r}) RETURN r.values, r.cells",
                           {"t": table_id, "r": row_id}).result_set
    if not found:
        raise Refused(f"input {item.get('name')!r}: {row_id} is not a row of {table_id}")
    values, cells = json.loads(found[0][0] or "[]"), json.loads(found[0][1] or "[]")
    stored = next((v for v in values if v.get("col") == col), None)
    candidates = ([stored["number"]] if stored and stored.get("number") is not None else []) or \
                 (numbers(cells[col]) if 0 <= col < len(cells) else [])
    cited = item.get("value")
    cited_numbers = numbers(str(cited)) if cited is not None else []
    if not cited_numbers:
        raise Refused(f"input {item.get('name')!r}: cite the value as printed")
    if not candidates or not any(_close(cited_numbers[0], c) for c in candidates):
        printed = stored.get("value") if stored else (cells[col] if 0 <= col < len(cells) else None)
        raise Refused(f"input {item.get('name')!r}: {row_id} col {col} holds {printed!r}, not {cited!r}")
    if table_id not in head_cache:
        head_cache[table_id] = header_region(graph, table_id)
    placed = check_header(item.get("header") or "", col, *head_cache[table_id])
    t.node("Table", table_id)
    t.node("Row", row_id)
    t.edge(table_id, "HAS_ROW", row_id)
    return cited_numbers[0], {"name": item.get("name"), "source": "cell", "table": table_id, "row": row_id,
                              "col": col, "value": cited, "header": item.get("header"), "header_check": placed}


def verify_fact(item: dict, facts: dict[str, Fact]) -> tuple[float, dict]:
    fact = facts.get(item.get("fact") or "")
    if fact is None or fact.number() is None:
        raise Refused(f"input {item.get('name')!r}: no numeric session fact {item.get('fact')!r}")
    return fact.number(), {"name": item.get("name"), "source": "fact", "fact": fact.id, "value": fact.value,
                           "unit": fact.unit, "fact_source": fact.source}


def verify_permission(graph, permit: dict | None, t: Touched) -> dict:
    """The note, footnote or clause that permits interpolation: it must exist and say so."""
    if not permit:
        raise Refused("interpolation needs a cited note, footnote or clause that permits it")
    kind, ident = permit.get("kind"), permit.get("id")
    queries = {"footnote": ("Footnote", "MATCH (f:Footnote {id: $id}) RETURN coalesce(f.text, '') + ' ' + coalesce(f.rule, '')"),
               "note_row": ("Row", "MATCH (r:Row {id: $id}) RETURN r.cells"),
               "clause": ("Clause", "MATCH (c:Clause {id: $id}) RETURN coalesce(c.text_corrected, c.text)")}
    if kind not in queries:
        raise Refused("permitted_by.kind must be footnote, note_row or clause")
    label, query = queries[kind]
    found = graph.ro_query(query, {"id": ident}).result_set
    if not found:
        raise Refused(f"no {kind} {ident!r} in the graph")
    text = found[0][0] or ""
    if not PERMIT.search(text):
        raise Refused(f"{kind} {ident!r} does not mention interpolation")
    t.node(label, ident)
    snippet = text[max(0, PERMIT.search(text).start() - 60):PERMIT.search(text).start() + 80]
    return {"kind": kind, "id": ident, "text": " ".join(snippet.split())}


def calculate(graph, args: dict, facts: dict[str, Fact]) -> ToolResult:
    import sympy

    t = Touched()
    head_cache: dict = {}
    try:
        values, cited = {}, []
        for item in args.get("inputs") or []:
            name = item.get("name")
            if not name or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name):
                raise Refused(f"input name {name!r} must be an identifier")
            if item.get("source") == "cell":
                value, info = verify_cell(graph, item, head_cache, t)
            elif item.get("source") == "fact":
                value, info = verify_fact(item, facts)
            else:
                raise Refused(f"input {name!r}: source must be cell or fact")
            values[name] = value
            cited.append(info)
        op = args.get("operation")
        permitted = None
        if op == "interpolate":
            spec = args.get("interpolate") or {}
            lower, upper, at = (spec.get(k) for k in ("lower", "upper", "x"))
            by_name = {c["name"]: c for c in cited}
            if lower not in by_name or upper not in by_name or at not in values:
                raise Refused("interpolate needs x, lower and upper naming cited inputs")
            if by_name[lower]["source"] != "cell" or by_name[upper]["source"] != "cell":
                raise Refused("lower and upper must be table cells")
            xs = [numbers(by_name[n]["header"]) for n in (lower, upper)]
            if any(len(x) != 1 for x in xs):
                raise Refused("each interpolation cell's header must hold exactly one number (its x)")
            x0, x1, x = xs[0][0], xs[1][0], values[at]
            if not min(x0, x1) <= x <= max(x0, x1) or _close(x0, x1):
                raise Refused(f"x = {x:g} is not between the two columns' {x0:g} and {x1:g} (no extrapolation)")
            permitted = verify_permission(graph, args.get("permitted_by"), t)
            y0, y1 = sympy.Float(values[lower]), sympy.Float(values[upper])
            expr = y0 + (sympy.Float(x) - x0) * (y1 - y0) / (x1 - x0)
            formula = f"{lower} + ({at} - {x0:g}) * ({upper} - {lower}) / ({x1:g} - {x0:g})"
        elif op == "formula":
            formula = args.get("formula") or ""
            try:
                parsed = parse_formula(formula)
            except ValueError as exc:
                raise Refused(f"formula: {exc}")
            if parsed.atoms(sympy.Number) - {sympy.Integer(1), sympy.Integer(-1)}:
                raise Refused("the formula may not contain numbers: cite every value as an input")
            unknown = {s.name for s in parsed.free_symbols} - set(values)
            if unknown:
                raise Refused(f"formula names without a cited input: {', '.join(sorted(unknown))}")
            expr = parsed.subs({sympy.Symbol(n): sympy.Float(v) for n, v in values.items()})
        else:
            raise Refused("operation must be interpolate or formula")
        result = float(expr)
    except Refused as exc:
        return ToolResult(f"refused: {exc}", {"refused": str(exc), "args": args}, t, error=True)
    name = args.get("result") or "result"
    weak = [c for c in cited if c.get("header_check") == "table"]
    note = "; ".join(f"{c['row']} col {c['col']} under {c['header']!r}" for c in cited if c["source"] == "cell")
    labels = [Label(c["row"], SELECTED, f"the agent chose {c['row']} col {c['col']} for header {c['header']!r}")
              for c in cited if c["source"] == "cell"]
    data = {"result": {"name": name, "value": result, "display": f"{result:.6g}"}, "operation": op,
            "formula": formula, "inputs": cited, "permitted_by": permitted,
            "column_check": "header found in the table's header; its column could not be checked on the grid"
                            if weak else "header sits over the cited column",
            "state_in_answer": f"inputs selected by agent: {note}" if note else "inputs selected by agent"}
    return ToolResult(f"{name} = {result:.6g} ({op}; {SELECTED})", data, t, labels)
