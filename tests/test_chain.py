"""Chained compute (compute with resolve_inputs) on a small invented graph, and through
the agent loop with a scripted fake model."""
from __future__ import annotations

import json

import pytest

from app.agent.chain import compute_chained
from app.agent.facts import Fact, MemoryFacts
from app.agent.tools import Toolbox
from app.graph import connection

GRAPH = "code_test_chain"


@pytest.fixture
def graph():
    g = connection.get_db().select_graph(GRAPH)
    try:
        g.delete()
    except Exception:
        pass
    g = connection.get_db().select_graph(GRAPH)
    g.query("CREATE (:Clause {id: '4.1', title: 'Load', page: 40})")
    g.query("UNWIND $ids AS i CREATE (:Parameter {id: i, symbol: i})", {"ids": ["Y", "X", "Z", "U", "Q", "R", "S"]})
    eqs = [("Equation 4-1", "Y = X*Z*S", "Y", ["X", "Z", "S"]), ("Equation 4-2", "X = 2*U", "X", ["U"]),
           ("Equation 4-3", "Q = 3*U", "Q", ["U"]), ("Equation 4-4", "Q = 4*U", "Q", ["U"]),
           ("Equation 4-5", "R = Q + X", "R", ["Q", "X"])]
    for eid, sympy, computes, uses in eqs:
        g.query("MATCH (c:Clause {id: '4.1'}) CREATE (c)-[:CONTAINS]->(:Equation {id: $id, sympy: $s, page: 40, "
                "status: 'extracted'})", {"id": eid, "s": sympy})
        g.query("MATCH (e:Equation {id: $e}), (p:Parameter {id: $p}) CREATE (e)-[:COMPUTES]->(p)", {"e": eid, "p": computes})
        for u in uses:
            g.query("MATCH (e:Equation {id: $e}), (p:Parameter {id: $p}) CREATE (e)-[:USES]->(p)", {"e": eid, "p": u})
    # Table 4-1 gives Z by U (rows decided by the facts); Table 4-2's column s_col supplies S (inferred GIVES)
    g.query("CREATE (:Table {id: 'Table 4-1', caption: 'Z by U', page: 41}), "
            "(:Table {id: 'Table 4-2', caption: 'S values', page: 42}), (:Parameter {id: 's_col', symbol: 's_col', "
            "source: 'table'}), (:Parameter {id: 'kind', symbol: 'kind', source: 'table'})")
    g.query("MATCH (t:Table {id: 'Table 4-1'}), (z:Parameter {id: 'Z'}) CREATE (t)-[:GIVES {col: 1, cols: [1]}]->(z)")
    g.query("MATCH (t:Table {id: 'Table 4-2'}), (z:Parameter {id: 's_col'}) CREATE (t)-[:GIVES {col: 1, cols: [1]}]->(z)")
    g.query("MATCH (t:Table {id: 'Table 4-2'}), (s:Parameter {id: 'S'}) "
            "CREATE (t)-[:GIVES {method: 'inferred', via: 's_col', col: 1, confidence: 0.9}]->(s)")
    rows = [("Table 4-1", 1, ["U ≤ 10"], 3.0), ("Table 4-1", 2, ["U > 10"], 5.0), ("Table 4-2", 1, ["U ≤ 100"], 1.5)]
    for tid, n, conds, z in rows:
        g.query("MATCH (t:Table {id: $t}) CREATE (t)-[:HAS_ROW]->(:Row {id: $id, index: $n, page: 41, is_header: false, "
                "cells: $cells, conditions: $c, values: $v})",
                {"t": tid, "id": f"{tid}#r{n}", "n": n, "cells": json.dumps([conds[0], str(z)]), "c": json.dumps(conds),
                 "v": json.dumps([{"col": 1, "value": str(z), "number": z, "unit": None, "formula": None}])})
    yield g
    g.delete()


def test_missing_inputs_are_computed_through_equations_and_tables(graph):
    r = compute_chained(graph, "Equation 4-1", {"U": Fact("U", 4.0)})
    assert r.data["computed"] and r.data["result"]["value"] == pytest.approx(8 * 3 * 1.5)
    chain = {s["id"]: s for s in r.data["chain"]}
    assert chain["X"]["source"]["equation"] == "Equation 4-2" and chain["X"]["value"] == 8
    assert chain["Z"]["source"] == {"table": "Table 4-1", "row": "Table 4-1#r1", "column": "Z", "inferred_column": False,
                                    "interpolated": False, "page": 41}
    assert chain["S"]["source"]["inferred_column"] and chain["S"]["source"]["column"] == "s_col"
    assert any(x.label == "inferred" for x in r.labels)
    assert r.data["still_missing"] == []


def test_a_judgment_stops_the_branch_and_says_why(graph):
    r = compute_chained(graph, "Equation 4-5", {"U": Fact("U", 4.0)})
    assert not r.data["computed"]
    missing = {m["id"]: m["why"] for m in r.data["still_missing"]}
    assert "2 sources" in missing["Q"] and "Equation 4-3" in missing["Q"]
    assert [s["id"] for s in r.data["chain"]] == ["X"]  # the branch that needs no judgment is still resolved
    r = compute_chained(graph, "Equation 4-1", {})
    assert "no equation or table" in {m["id"]: m["why"] for m in r.data["still_missing"]}["U"]


def test_the_agent_tool_takes_resolve_inputs(graph):
    from app.agent.loop import Agent
    from tests.test_agent_loop import Script, text, tool

    box = Toolbox(graph, MemoryFacts())
    box.store.set(Fact("U", 4.0))
    script = Script([tool("compute", 1, target="Equation 4-1", resolve_inputs=True)], [text("Y = 36.")])
    turn = Agent(box, transport=script, model="claude-sonnet-5-5", log=lambda m: None).ask("Y?")
    assert turn.calls[0].result.data["result"]["value"] == pytest.approx(36)
    assert turn.answer.startswith("Y = 36.") and not turn.warnings
