"""plan_procedure on the invented code graph: order, inputs, decisions, labels."""
from __future__ import annotations

from app.agent.facts import Fact
from app.agent.procedure import plan_procedure
from tests.agent_graph import code_graph  # noqa: F401  (fixture)


def steps(result) -> list[str]:
    return [s["id"] for s in result.data["steps"]]


def test_dependencies_come_first_and_inputs_show_what_the_facts_give(code_graph):
    r = plan_procedure(code_graph, "F", {"W": Fact("W", 800.0, "kips")})
    order = steps(r)
    assert order.index("Equation 1-1") < order.index("Equation 1-2")  # V is computed before it is used
    eq2 = next(s for s in r.data["steps"] if s["id"] == "Equation 1-2")
    v = next(i for i in eq2["inputs"] if i["id"] == "V")
    assert v["from_steps"] == [order.index("Equation 1-1") + 1] and not v["known"]
    eq1 = next(s for s in r.data["steps"] if s["id"] == "Equation 1-1")
    assert next(i for i in eq1["inputs"] if i["id"] == "W")["known"]
    assert set(r.data["inputs_needed"]) == {"C", "k"}
    assert any(x.label == "unreviewed" and x.target == "Equation 1-2" for x in r.labels)


def test_a_goal_clause_lists_its_sub_clauses_after_it_and_tables_after_their_clause(code_graph):
    r = plan_procedure(code_graph, "1", {})
    order = steps(r)
    assert order.index("1") < order.index("1.2") and order.index("1") < order.index("1.3")
    r = plan_procedure(code_graph, "1.3", {})
    assert "Table 1-1" in steps(r)  # through the inferred reference
    assert any(x.label == "inferred" for x in r.labels)


def test_decisions_are_found_in_the_graph(code_graph):
    r = plan_procedure(code_graph, "1.2", {})
    kinds = {(d["kind"], d["at"]) for d in r.data["decisions"]}
    assert ("alternative_to", "1.2") in kinds  # 1.2 is an alternative to 1.3
    assert ("row", "Table 1-1") in kinds and ("column", "Table 1-1") in kinds


def test_a_phrase_goal_is_resolved_with_find_entry(code_graph):
    r = plan_procedure(code_graph, "story force", {})
    assert r.data["goal"]["id"] in {"1.3", "F", "Equation 1-2"}


def test_equation_inputs_bring_the_clauses_that_define_them(code_graph):
    code_graph.query("MATCH (p:Parameter {id: 'k'}), (c:Clause {id: '1'}) MERGE (p)-[:DEFINED_IN]->(c)")
    try:
        r = plan_procedure(code_graph, "Equation 1-2", {})
        order = steps(r)
        assert "1" in order and order.index("1") < order.index("Equation 1-2")
        eq2 = next(s for s in r.data["steps"] if s["id"] == "Equation 1-2")
        k = next(i for i in eq2["inputs"] if i["id"] == "k")
        assert k["defined_in_steps"] == [order.index("1") + 1] and not k["from_steps"]
        assert next(s for s in r.data["steps"] if s["id"] == "1")["defines"] == ["k"]
    finally:
        code_graph.query("MATCH (:Parameter {id: 'k'})-[e:DEFINED_IN]->() DELETE e")


def test_an_untyped_reference_to_an_equation_is_a_dependency(code_graph):
    code_graph.query("MATCH (c:Clause {id: '1'}), (e:Equation {id: 'Equation 1-1'}) "
                     "CREATE (c)-[:REFERENCES {text: 'Equation (1-1)'}]->(e)")
    try:
        r = plan_procedure(code_graph, "1", {})
        assert "Equation 1-1" in steps(r)  # and through it, its inputs
        eq = next(s for s in r.data["steps"] if s["id"] == "Equation 1-1")
        assert {i["id"] for i in eq["inputs"]} == {"C", "W"}
    finally:
        code_graph.query("MATCH (:Clause {id: '1'})-[e:REFERENCES]->(:Equation {id: 'Equation 1-1'}) DELETE e")


def test_a_parameter_with_many_sources_keeps_the_nearest_and_counts_the_rest(code_graph, monkeypatch):
    import app.agent.procedure as pr
    monkeypatch.setattr(pr, "MAX_SOURCES", 1)
    code_graph.query("MATCH (p:Parameter {id: 'V'}) CREATE (:Equation {id: 'Equation 9-1', page: 90, "
                     "latex: 'V = 2', sympy: 'V = 2'})-[:COMPUTES]->(p)")
    try:
        r = plan_procedure(code_graph, "V", {})
        assert "Equation 9-1" not in steps(r) and "Equation 1-1" in steps(r)  # page 10 is nearer than 90
        d = next(d for d in r.data["decisions"] if d["kind"] == "source" and d["at"] == "V")
        assert "2 sources" in d["why"]
    finally:
        code_graph.query("MATCH (e:Equation {id: 'Equation 9-1'}) DETACH DELETE e")
