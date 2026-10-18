"""Chained compute (compute with resolve_inputs) on a small invented graph, and through
the agent loop with a scripted fake model."""
from __future__ import annotations

import json

import pytest

from app.agent.chain import comparisons, compute_chained
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
    assert "2 equations" in missing["Q"] and "Equation 4-3" in missing["Q"]  # no conditions, values differ
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


RANGES = "code_test_chain_ranges"


@pytest.fixture
def ranges():
    """An invented response curve: A comes from one of four equations chosen by where the period P
    falls against P0 and PS, which are themselves computed (PS by two equations that agree when M = N)."""
    g = connection.get_db().select_graph(RANGES)
    try:
        g.delete()
    except Exception:
        pass
    g = connection.get_db().select_graph(RANGES)
    g.query("CREATE (:Clause {id: '5.1', title: 'Curve', page: 50}), (:Clause {id: '5.2', title: 'Other curve', page: 51})")
    params = [("Y", "Y", []), ("A", "A", []), ("K", "K", []), ("W", "W", []), ("G", "G", []), ("H", "H", []),
              ("M", "M", []), ("N", "N", []), ("P_period", "P", ["P"]), ("P_S", "P_S", ["PS"]), ("P_0", "P_0", ["P0"])]
    g.query("UNWIND $ps AS p CREATE (:Parameter {id: p[0], symbol: p[1], latex: p[1], aliases: p[2]})",
            {"ps": [[i, s, a] for i, s, a in params]})
    eqs = [("5.1", "Equation 5-1", "5-1", "A = 2*K*P_period/P_S", "A = 2 K P / P_S", "A", ["K", "P_period", "P_S"]),
           ("5.1", "Equation 5-2", "5-2", "A = K", r"A = K \text{ for } P_0 \leq P \leq P_S", "A", ["K"]),
           ("5.1", "Equation 5-3", "5-3", "A = K*P_S/P_period", r"A = K P_S/P \text{ for } P > P_S", "A",
            ["K", "P_S", "P_period"]),
           ("5.2", "Equation 5-4", "5-4", "A = K/P_period", "A = K/P", "A", ["K", "P_period"]),
           ("5.1", "Equation 5-5", "5-5", "P_S = G/K", "P_S = G/K", "P_S", ["G", "K"]),
           ("5.2", "Equation 5-6", "5-6", "P_S = G*M/(K*N)", "P_S = G M/(K N)", "P_S", ["G", "M", "K", "N"]),
           ("5.1", "Equation 5-7", "5-7", "P_0 = 0.2*P_S", "P_0 = 0.2 P_S", "P_0", ["P_S"]),
           ("5.1", "Equation 5-8", "5-8", "G = 3*H", "G = 3H", "G", ["H"]),
           ("5.2", "Equation 5-9", "5-9", "G >= 0.1", r"G \geq 0.1", "G", []),
           ("5.1", "Equation 5-10", "5-10", "Y = A*W", "Y = A W", "Y", ["A", "W"])]
    for clause, eid, number, sympy, latex, computes, uses in eqs:
        g.query("MATCH (c:Clause {id: $c}) CREATE (c)-[:CONTAINS]->(:Equation {id: $id, number: $n, sympy: $s, "
                "latex: $l, page: 50, status: 'extracted'})", {"c": clause, "id": eid, "n": number, "s": sympy, "l": latex})
        g.query("MATCH (e:Equation {id: $e}), (p:Parameter {id: $p}) CREATE (e)-[:COMPUTES]->(p)", {"e": eid, "p": computes})
        for u in uses:
            g.query("MATCH (e:Equation {id: $e}), (p:Parameter {id: $p}) CREATE (e)-[:USES]->(p)", {"e": eid, "p": u})
    conds = [("5.1", "5.1#c1", "Horizontal direction"),
             ("5.1", "5.1#c2", "Short period range (P < P0) for use of Equation (5-1)"),
             ("5.2", "5.2#c1", "Where a site-specific curve is developed")]
    for clause, cid, text in conds:
        g.query("MATCH (c:Clause {id: $c}) CREATE (c)-[:HAS_CONDITION]->(:Condition {id: $id, text: $t})",
                {"c": clause, "id": cid, "t": text})
    yield g
    g.delete()


def facts(**values) -> dict[str, Fact]:
    return {k: Fact(k, v) for k, v in values.items()}


def test_comparisons_split_chains_and_read_latex():
    assert comparisons("for 0 < T < T0, and") == [("0", "<", "T"), ("T", "<", "T0")]
    assert comparisons("C1 = 1.0 for T >= TS second.") == [("T", "≥", "TS")]
    assert comparisons("Horizontal direction") == []


@pytest.mark.parametrize("period, equation, value", [(0.45, "Equation 5-2", 1.0), (1.2, "Equation 5-3", 0.5),
                                                      (0.05, "Equation 5-1", 2 * 0.05 / 0.6)])
def test_the_equation_whose_range_holds_is_used(ranges, period, equation, value):
    r = compute_chained(ranges, "Equation 5-10", facts(K=1.0, H=0.2, M=1.0, N=1.0, W=10.0, P_period=period))
    assert r.data["computed"], r.data["still_missing"]
    assert r.data["result"]["value"] == pytest.approx(10 * value)
    steps = {s["id"]: s for s in r.data["chain"]}
    a = steps["A"]
    assert a["source"]["equation"] == equation and a["selected_by"]["rule"] == "its conditions hold"
    assert all(c["status"] == "true" for c in a["selected_by"]["conditions"])
    others = {o["equation"]: o["status"] for o in a["selected_by"]["other_equations"]}
    assert others.pop("Equation 5-4") == "unknown"  # no stated range: never chosen, never blocks
    assert set(others.values()) == {"false"} and len(others) == 2
    # P_S had two equations with no conditions; it is used because both give the same value
    assert steps["P_S"]["value"] == pytest.approx(0.6) and "same value" in steps["P_S"]["selected_by"]["rule"]
    assert steps["G"]["source"]["equation"] == "Equation 5-8"  # the inequality 5-9 is a limit, not a source


def test_conditions_that_cannot_be_decided_stop_the_branch(ranges):
    # M != N: the two P_S equations disagree, so the ranges that compare P with P_S are undecided
    r = compute_chained(ranges, "Equation 5-10", facts(K=1.0, H=0.2, M=2.0, N=1.0, W=10.0, P_period=0.45))
    assert not r.data["computed"]
    missing = {m["id"]: m["why"] for m in r.data["still_missing"]}
    assert "2 equations" in missing["P_S"]
    assert "select none" in missing["A"] and "Equation 5-2: unknown" in missing["A"]
