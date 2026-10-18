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


def test_a_condition_parameter_is_never_mapped_as_a_value():
    # 'kind' comes first and is listed for one column, like 'a': without the exclusion it would win column 1
    headers = [Header("kind", [1], "kind", None, 0), Header("a", [1], "a", None, 1), Header("b", [2], "b", None, 2)]
    by_col, _ = map_columns([1, 2], headers)
    assert by_col[1].id == "kind"
    by_col, method = map_columns([1, 2], headers, exclude={"kind"})
    assert method == "column" and [by_col[1].id, by_col[2].id] == ["a", "b"]
    by_col, method = map_columns([1, 2, 3], headers, exclude={"kind"})
    assert method is None  # three values, two value parameters: no order alignment either


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


def test_condition_columns_are_found_from_the_grid_and_never_returned(code_graph):
    r = compute_table(code_graph, "Table 1-1", MemoryFacts(), row_id="Table 1-1#r3", reason="the member is type B")
    assert r.data["condition_parameters"] == ["kind"]  # column 1 holds 'A', part of 'Type A', in rows 1 and 2
    assert "kind" not in {v["param"] for v in r.data["values"]}
    assert "kind" not in {h["id"] for h in r.data["header_parameters"]}
    for row in ("Table 1-1#r1", "Table 1-1#r2"):
        values = compute_table(code_graph, "Table 1-1", MemoryFacts(), row_id=row, reason="type A").data["values"]
        assert [v["param"] for v in values] == ["a", "b"]


def test_a_label_column_that_holds_values_in_other_rows_stays_a_value_column():
    from app.agent.compute import condition_params

    headers = [Header("d", [0], "d", None, 0), Header("e", [1], "e", None, 1)]
    rows = [{"_cells": ["0.75", "2.0"], "_values": [{"col": 0}, {"col": 1}], "_conditions": ["Walls"]},
            {"_cells": ["Beams", "Diagonal", "0.02"], "_values": [{"col": 2}], "_conditions": ["Beams", "Diagonal"]}]
    assert condition_params(rows, headers) == set()  # columns 0 and 1 hold the walls' values


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
    refs = {x["id"]: x for x in r.data["references"]}
    assert set(refs) == {"Equation 1-2", "Table 1-1", "1"}
    assert refs["1"]["kind"] == "pdf link" and "kind" not in refs["Table 1-1"]  # PDF links kept, marked
    assert any(x.label == "unreviewed" for x in r.labels)
    assert r.data["flagged"] is True and "review_status" not in r.data and "status" not in r.data


def test_a_pdf_link_to_a_target_the_text_names_is_not_listed_twice(code_graph):
    refs = gt.get_clause(code_graph, "1.2").data["references"]
    assert sorted(x["type"] for x in refs if x["id"] == "1.3") == ["alternative_to", "requires"]
    assert all(x.get("kind") != "pdf link" for x in refs)  # the link to 1.3 adds nothing
    assert "flagged" not in gt.get_clause(code_graph, "1.2").data  # not flagged: no status at all


def test_get_clause_on_a_table_gives_columns_and_conditions_but_no_values(code_graph):
    r = gt.get_clause(code_graph, "Table 1-1")
    assert [c["id"] for c in r.data["value_columns"]] == ["a", "b"] and r.data["condition_columns"] == ["kind"]
    assert r.data["rows"][0] == {"row": "Table 1-1#r1", "conditions": ["Type A", "R ≤ 3"]}
    assert r.data["footnotes"][0]["kind"] == "interpolation"
    assert "0.02" not in r.for_model() and "compute" in r.data["values"]
    assert gt.get_clause(code_graph, "Table 1-1#r3").data["id"] == "Table 1-1"  # a row id gives its table


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


def test_run_cypher_hides_table_row_values(code_graph):
    for query in ("MATCH (r:Row {id: 'Table 1-1#r3'}) RETURN r",
                  "MATCH (r:Row {id: 'Table 1-1#r3'}) RETURN properties(r)",
                  "MATCH (r:Row {id: 'Table 1-1#r3'}) RETURN r.values, r.cells, r.conditions"):
        text = gt.run_cypher(code_graph, query).for_model()
        assert "0.5" not in text and "hidden" in text and "Type B" in text, query


def test_tool_results_carry_no_review_status():
    from app.agent.results import clean
    assert clean({"id": "x", "status": "extracted", "review_status": "unreviewed"}) == {"id": "x"}
    assert clean({"id": "y", "status": "flagged", "review_status": "unreviewed"}) == {"id": "y", "flagged": True}


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
