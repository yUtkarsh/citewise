"""Phase 4 tools on a small invented code graph (tests/agent_graph.py). Skips without FalkorDB."""
from __future__ import annotations

import pytest

from app.agent import graph_tools as gt
from app.agent.compute import ORDER, Header, compute_equation, compute_table, map_columns
from app.agent.conditions import FALSE, TRUE, UNKNOWN, evaluate
from app.agent.facts import Fact, MemoryFacts
from app.agent.search import find_entry, ft_query
from app.agent.tools import Toolbox
from tests.agent_graph import code_graph  # noqa: F401  (fixture)


def store(**values) -> MemoryFacts:
    s = MemoryFacts()
    for k, v in values.items():
        value, unit = v if isinstance(v, tuple) else (v, None)
        s.set(Fact(k, value, unit))
    return s


def nodes(result) -> set[str]:
    return {n["id"] for n in result.touched.as_dict()["nodes"]}


# ---- pure functions --------------------------------------------------------------------------

def test_conditions_decide_comparisons_and_leave_descriptions_unknown():
    facts = {"R": Fact("R", 4.5), "s": Fact("s", 5.0), "d": Fact("d", 12.0),
             "ratio": Fact("ratio", 2.0, label="V/(b d)")}
    assert evaluate("R ≤ 3", facts).status == FALSE
    assert evaluate("R >= 3", facts).status == TRUE
    assert evaluate("s ≤ d/2", facts).status == TRUE  # an expression of facts
    assert evaluate("V/(b d) ≤ 3", facts).status == TRUE  # matched by the fact's label
    assert evaluate("Type A", facts).status == UNKNOWN
    assert evaluate("Q ≤ 3", facts).status == UNKNOWN  # no fact for Q


def test_columns_map_by_column_with_shared_columns_and_fall_back_to_order():
    headers = [Header("kind", [1], "kind", None, 0), Header("a", [1, 3], "a", None, 1),
               Header("b", [2, 4], "b", None, 2)]
    by_col, method = map_columns([1, 2], headers)
    assert method == "column" and by_col[1].id == "a" and by_col[2].id == "b"  # a, b: listed for several cols
    by_col, method = map_columns([3, 4], headers)
    assert method == "column" and [by_col[3].id, by_col[4].id] == ["a", "b"]
    two = [Header("x", [0], "x", None, 0), Header("y", [1], "y", None, 1)]
    by_col, method = map_columns([2, 3], two)
    assert method == ORDER and [by_col[2].id, by_col[3].id] == ["x", "y"]  # counts match: by order
    assert map_columns([2, 3, 4], two) == ({}, None)  # counts differ: no mapping


def test_fulltext_query_keeps_words_only():
    assert ft_query("What does Section 3.3.1.3 (a) reference?") == "Section|reference"


# ---- compute: equations -----------------------------------------------------------------------

def test_missing_inputs_are_listed_not_guessed(code_graph):
    r = compute_equation(code_graph, "Equation 1-1", {})
    assert not r.data["computed"] and {m["id"] for m in r.data["missing"]} == {"C", "W"}
    c = next(m for m in r.data["missing"] if m["id"] == "C")
    assert "Table 1-2" in c["where_text"]  # the where-item says where to look


def test_equation_is_evaluated_in_sympy_with_units_and_touched_ids(code_graph):
    r = compute_equation(code_graph, "Equation 1-1", store(C=0.25, W=(800, "kips")).all())
    assert r.data["result"]["value"] == pytest.approx(200.0) and r.data["result"]["unit"] == "kips"
    assert {"Equation 1-1", "1.2", "V", "C", "W"} <= nodes(r)
    assert "Equation 1-1|COMPUTES|V" in r.touched.edges and "Equation 1-1|USES|W" in r.touched.edges
    by_param = compute_equation(code_graph, "V", store(C=0.25, W=(800, "kips")).all())
    assert by_param.data["result"]["value"] == pytest.approx(200.0)


def test_solving_for_an_input_and_flagging_a_unit_the_code_does_not_use(code_graph):
    r = compute_equation(code_graph, "Equation 1-1", store(V=(200, "kips"), C=0.25).all(), solve_for="W")
    assert r.data["result"]["value"] == pytest.approx(800.0)
    r = compute_equation(code_graph, "Equation 1-1", store(C=0.25, W=(800, "lb")).all())
    assert any("no conversion" in w for w in r.data["warnings"])


def test_assumed_inputs_are_labelled(code_graph):
    s = store(W=(800, "kips"))
    s.set(Fact("C", 0.25, None, "assumed"))
    r = compute_equation(code_graph, "Equation 1-1", s.all())
    assert any(x.label == "assumed" and x.target == "C" for x in r.labels)


def test_an_equation_flagged_high_is_not_computed(code_graph):
    r = compute_equation(code_graph, "Equation 1-2", store(V=10, k=1).all())
    assert not r.data["computed"] and r.data["latex"] == "F = V k" and r.data["crop"]
    assert r.data["flags"][0]["reason"] == "equation_mismatch"
    assert any(x.label == "unreviewed" and x.target == "Equation 1-2" for x in r.labels)


# ---- compute: tables -----------------------------------------------------------------------------

def test_table_without_deciding_facts_lists_candidates(code_graph):
    r = compute_table(code_graph, "Table 1-1", MemoryFacts())
    assert len(r.data["candidates"]) == 3 and "pick one" in r.summary


def test_a_described_row_needs_a_reason_and_is_recorded(code_graph):
    s = MemoryFacts()
    assert compute_table(code_graph, "Table 1-1", s, row_id="Table 1-1#r3").error
    r = compute_table(code_graph, "Table 1-1", s, row_id="Table 1-1#r3", reason="the member is type B")
    assert [(v["param"], v["number"]) for v in r.data["values"]] == [("a", 0.5), ("b", 0.6)]
    assert r.data["column_mapping"] == "column" and s.matches()[0].row_id == "Table 1-1#r3"
    assert {"Table 1-1", "Table 1-1#r3", "a", "b"} <= nodes(r)


def test_a_row_that_contradicts_the_facts_is_refused(code_graph):
    r = compute_table(code_graph, "Table 1-1", store(R=8.0), row_id="Table 1-1#r1", reason="type A")
    assert r.error and "R ≤ 3 is false" in r.summary


def test_rows_are_interpolated_when_a_footnote_allows_it(code_graph):
    r = compute_table(code_graph, "Table 1-1", store(R=4.5), row_id="Table 1-1#r1", reason="type A")
    got = {v["param"]: v["number"] for v in r.data["values"]}
    assert got == {"a": pytest.approx(0.015), "b": pytest.approx(0.03)}
    assert r.data["interpolation"]["rows"] == ["Table 1-1#r1", "Table 1-1#r2"]


def test_values_aligned_by_order_are_labelled(code_graph):
    r = compute_table(code_graph, "Table 1-2", MemoryFacts(), row_id="Table 1-2#r1", reason="all members")
    assert r.data["column_mapping"] == ORDER and any(x.label == ORDER for x in r.labels)


def test_row_values_can_become_facts_for_an_equation(code_graph):
    s = MemoryFacts()
    compute_table(code_graph, "Table 1-2", s, row_id="Table 1-2#r1", reason="all members", save_as_facts=True)
    assert s.all()["x"].source == "matched_row" and s.all()["x"].number() == 1.5


# ---- graph tools ------------------------------------------------------------------------------

def test_find_entry_ranks_exact_ids_and_symbols_first(code_graph):
    assert find_entry(code_graph, "Equation 1-1").data["candidates"][0]["id"] == "Equation 1-1"
    assert find_entry(code_graph, "Wt").data["candidates"][0]["id"] == "W"  # an alias
    assert find_entry(code_graph, "What does Section 1.2 say?").data["candidates"][0]["id"] == "1.2"
    hit = find_entry(code_graph, "story force")
    assert "1.3" in {c["id"] for c in hit.data["candidates"]}  # full-text on clause text


def test_dependencies_upstream_and_downstream(code_graph):
    up = gt.get_dependencies(code_graph, "V", "upstream")
    assert {"Equation 1-1", "C", "W", "1.2"} <= {n["id"] for n in up.data["nodes"]}
    down = gt.get_dependencies(code_graph, "V", "downstream")
    assert {"Equation 1-2", "F", "1.3"} <= {n["id"] for n in down.data["nodes"]}
    via_clause = gt.get_dependencies(code_graph, "1.3", "upstream")
    assert any(x.label == "inferred" for x in via_clause.labels)  # the inferred reference to Table 1-1


def test_get_clause_returns_text_references_equations_and_labels(code_graph):
    r = gt.get_clause(code_graph, "1.3")
    assert r.data["page"] == 11 and r.data["equations"][0]["crop"]
    assert {x["id"] for x in r.data["references"]} == {"Equation 1-2", "Table 1-1"}
    assert any(x.label == "unreviewed" for x in r.labels)


def test_applicable_clauses_checks_conditions_against_facts(code_graph):
    r = gt.applicable_clauses(code_graph, "1", {"h": Fact("h", 30.0)})
    status = {c["id"]: c["status"] for c in r.data["clauses"]}
    assert status["1.2"] == "does not apply" and status["1.3"] == "applies"


def test_trace_formats_citations_with_pages(code_graph):
    r = gt.trace(code_graph, [{"id": "Equation 1-1"}, {"id": "Table 1-1#r3"}, {"id": "1.3"}])
    lines = r.data["lines"]
    assert lines[0] == "Equation 1-1 (in 1.2 Base Shear), p. 10"
    assert lines[1] == "Table 1-1, row 3 (Type B), p. 12"
    assert any(x.label == "unreviewed" and x.target == "1.3" for x in r.labels)


def test_run_cypher_is_read_only_and_capped(code_graph):
    assert gt.run_cypher(code_graph, "CREATE (:X {id: 1})").error
    r = gt.run_cypher(code_graph, "UNWIND range(1, 250) AS i RETURN i")
    assert len(r.data["rows"]) == 200 and r.data["capped"]
    r = gt.run_cypher(code_graph, "MATCH (c:Clause {id: '1.2'}) RETURN c")
    assert "1.2" in nodes(r)


def test_toolbox_routes_compute_to_tables_rows_and_equations(code_graph):
    box = Toolbox(code_graph, store(C=0.25, W=(800, "kips")))
    assert box.run("compute", {"target": "Equation 1-1"}).data["result"]["value"] == pytest.approx(200.0)
    assert box.run("compute", {"target": "Table 1-1#r3", "reason": "type B"}).data["row"] == "Table 1-1#r3"
    saved = box.run("save_session_facts", {"facts": [{"id": "W", "value": 900, "unit": "kips", "source": "user"}]})
    assert saved.data["changed"] == ["W: 800 -> 900"]
    assert box.run("nope", {}).error
