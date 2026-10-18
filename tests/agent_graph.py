"""A small invented code graph for the agent tests (no text from any real code).

Clauses 1, 1.2 and 1.3; Equation 1-1 (V = C*W, clean) and Equation 1-2 (flagged high);
Table 1-1 whose grid puts a and b in different columns for different row groups, with
an interpolation footnote; Table 1-2 whose values only align with its headers by order;
an inferred reference edge.
"""
from __future__ import annotations

import json

import pytest

from app.graph import connection

GRAPH = "code_test_agent"


def _q(g, cypher: str, params: dict | None = None):
    return g.query(cypher, params or {})


def build(g) -> None:
    try:
        g.delete()
    except Exception:
        pass
    g = connection.get_db().select_graph(GRAPH)
    clause = lambda i, title, page, text, status="extracted", flags=None: {  # noqa: E731
        "id": i, "title": title, "page": page, "text": text, "status": status, "review_status": "unreviewed",
        "flags": flags or []}
    flag = json.dumps({"reason": "equation_mismatch", "severity": "high",
                       "detail": "equation Equation 1-2: the LaTeX and the SymPy form give different values"})
    _q(g, "UNWIND $rows AS r CREATE (c:Clause) SET c = r", {"rows": [
        clause("1", "General Rules", 9, "Rules for the example structure."),
        clause("1.2", "Base Shear", 10, "The base shear V shall be computed with Equation 1-1. See Section 1.3."),
        clause("1.3", "Story Force", 11, "The story force F shall be computed with Equation 1-2.", "flagged", [flag]),
    ]})
    _q(g, "MATCH (a:Clause {id: '1.2'}), (b:Clause {id: '1'}) CREATE (a)-[:PART_OF]->(b)")
    _q(g, "MATCH (a:Clause {id: '1.3'}), (b:Clause {id: '1'}) CREATE (a)-[:PART_OF]->(b)")
    _q(g, "MATCH (a:Clause {id: '1.2'}) CREATE (a)-[:HAS_CONDITION]->(:Condition {id: '1.2#cond1', text: 'h ≤ 20'})")
    _q(g, "UNWIND $rows AS r CREATE (p:Parameter) SET p = r", {"rows": [
        {"id": "V", "symbol": "V", "description": "Base shear", "units": "kips (kN)", "status": "extracted"},
        {"id": "C", "symbol": "C", "description": "Response coefficient", "status": "extracted"},
        {"id": "W", "symbol": "W", "description": "Effective weight", "units": "kips (kN)", "status": "extracted",
         "aliases": ["Wt"]},
        {"id": "F", "symbol": "F", "description": "Story force", "status": "flagged"},
        {"id": "k", "symbol": "k", "description": "Distribution exponent", "status": "extracted"},
        {"id": "a", "symbol": "a", "description": "Plastic rotation a", "units": "radians"},
        {"id": "b", "symbol": "b", "description": "Plastic rotation b", "units": "radians"},
        {"id": "kind", "symbol": "kind", "description": "Member kind"},
        {"id": "x", "symbol": "x", "description": "Coefficient x"},
        {"id": "y", "symbol": "y", "description": "Coefficient y"},
    ]})
    _q(g, "UNWIND $rows AS r CREATE (e:Equation) SET e = r", {"rows": [
        {"id": "Equation 1-1", "number": "1-1", "latex": "V = C W", "sympy": "V = C*W", "page": 10,
         "crop": "data/crops/eq_1-1.png", "status": "extracted", "review_status": "unreviewed"},
        {"id": "Equation 1-2", "number": "1-2", "latex": "F = V k", "sympy": "F = V*k*2", "page": 11,
         "crop": "data/crops/eq_1-2.png", "status": "flagged", "review_status": "unreviewed"},
    ]})
    for clause_id, eq in (("1.2", "Equation 1-1"), ("1.3", "Equation 1-2")):
        _q(g, "MATCH (c:Clause {id: $c}), (e:Equation {id: $e}) CREATE (c)-[:CONTAINS]->(e)", {"c": clause_id, "e": eq})
    for eq, kind, pid in (("Equation 1-1", "COMPUTES", "V"), ("Equation 1-1", "USES", "C"),
                          ("Equation 1-1", "USES", "W"), ("Equation 1-2", "COMPUTES", "F"),
                          ("Equation 1-2", "USES", "V"), ("Equation 1-2", "USES", "k")):
        _q(g, f"MATCH (e:Equation {{id: $e}}), (p:Parameter {{id: $p}}) CREATE (e)-[:{kind}]->(p)", {"e": eq, "p": pid})
    _q(g, "MATCH (e:Equation {id: 'Equation 1-1'}) CREATE (e)-[:WHERE]->(:WhereItem {id: 'Equation 1-1#w1', "
          "symbol: 'C', definition: 'Response coefficient from Table 1-2'})")
    _q(g, "MATCH (a:Clause {id: '1.2'}), (b:Clause {id: '1.3'}) CREATE (a)-[:REFERENCES {text: 'Section 1.3', "
          "type: 'requires'}]->(b)")
    _q(g, "MATCH (a:Clause {id: '1.2'}), (b:Clause {id: '1.3'}) CREATE (a)-[:REFERENCES {text: 'Section 1.3', "
          "type: 'alternative_to'}]->(b)")
    _q(g, "MATCH (a:Clause {id: '1.3'}), (e:Equation {id: 'Equation 1-2'}) CREATE (a)-[:REFERENCES "
          "{text: 'Equation 1-2', type: 'requires'}]->(e)")

    _q(g, "CREATE (:Table {id: 'Table 1-1', number: '1-1', caption: 'Table 1-1 Rotation Limits', page: 12, "
          "pages: [12], status: 'extracted', review_status: 'unreviewed'})")
    _q(g, "CREATE (:Table {id: 'Table 1-2', number: '1-2', caption: 'Table 1-2 Coefficients', page: 13, "
          "pages: [13], status: 'extracted', review_status: 'unreviewed'})")
    _q(g, "MATCH (c:Clause {id: '1.3'}), (t:Table {id: 'Table 1-1'}) "
          "CREATE (c)-[:REFERENCES {text: 'Table 1-1', type: 'requires', method: 'inferred'}]->(t)")
    gives = [("Table 1-1", "kind", [1]), ("Table 1-1", "a", [1, 3]), ("Table 1-1", "b", [2, 4]),
             ("Table 1-2", "x", [0]), ("Table 1-2", "y", [1])]
    for tid, pid, cols in gives:
        _q(g, "MATCH (t:Table {id: $t}), (p:Parameter {id: $p}) CREATE (t)-[:GIVES {col: $c0, cols: $cols, "
              "header_text: $p, units: null}]->(p)", {"t": tid, "p": pid, "c0": cols[0], "cols": cols})
    value = lambda col, n: {"col": col, "value": str(n), "number": n, "unit": "radians", "formula": None}  # noqa
    rows = [  # (table, index, conditions, values, Pass 1 grid cells)
        ("Table 1-1", 1, ["Type A", "R ≤ 3"], [value(3, 0.02), value(4, 0.04)], ["Type A", "A", "≤ 3", "0.02", "0.04"]),
        ("Table 1-1", 2, ["Type A", "R ≥ 6"], [value(3, 0.01), value(4, 0.02)], ["", "A", "≥ 6", "0.01", "0.02"]),
        ("Table 1-1", 3, ["Type B"], [value(1, 0.5), value(2, 0.6)], ["Type B", "0.5", "0.6"]),
        ("Table 1-2", 1, ["All members"], [value(2, 1.5), value(3, 2.5)], ["", "", "1.5", "2.5"]),
    ]
    for tid, index, conds, values, cells in rows:
        rid = f"{tid}#r{index}"
        _q(g, "MATCH (t:Table {id: $t}) CREATE (t)-[:HAS_ROW]->(:Row {id: $id, index: $i, page: $page, "
              "conditions: $c, values: $v, cells: $cells, blank_cells: '[]', status: 'extracted'})",
           {"t": tid, "id": rid, "i": index, "page": 12 if tid == "Table 1-1" else 13,
            "c": json.dumps(conds, ensure_ascii=False), "v": json.dumps(values),
            "cells": json.dumps(cells, ensure_ascii=False)})
    _q(g, "MATCH (t:Table {id: 'Table 1-1'}) CREATE (t)-[:HAS_FOOTNOTE]->(:Footnote {id: 'Table 1-1#fn1', "
          "marker: '1', text: '1. Interpolation is permitted.', rule_kind: 'interpolation', "
          "rule: 'Linear interpolation between rows is permitted.', applies_to_rows: [1, 2], applies_to_cols: []})")
    _q(g, "CREATE (:Document {id: $id, title: 'Example Code'})", {"id": GRAPH})
    g.query("CALL db.idx.fulltext.createNodeIndex('Clause', 'text', 'title')")


@pytest.fixture(scope="module")
def code_graph():
    try:
        connection.ping()
    except Exception:
        pytest.skip("FalkorDB is not running (docker compose up -d)")
    g = connection.get_db().select_graph(GRAPH)
    build(g)
    g = connection.get_db().select_graph(GRAPH)
    yield g
    try:
        g.delete()
    except Exception:
        pass
