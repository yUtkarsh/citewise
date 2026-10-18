"""Global pass, part 1: graph-wide checks in Cypher, written to the review queue.

A small invented graph (no text from any real code)."""
from __future__ import annotations

import pytest

from app.graph import connection
from app.ingest.global_pass.checks import _cycles, run_checks
from app.ingest.global_pass.findings import FindingSet, Finding, write_findings

GRAPH = "code_test_global_checks"


@pytest.fixture
def graph():
    g = connection.get_db().select_graph(GRAPH)
    try:
        g.delete()
    except Exception:
        pass
    g = connection.get_db().select_graph(GRAPH)
    g.query("CREATE (:Clause {id: '1', page: 3, unresolved_refs: []}), "
            "(:Clause {id: '1.1', page: 4, unresolved_refs: ['Section 1.9 -> 1.9', 'Section 1 -> 1']}), "
            "(:Clause {id: '1.2', page: 5, unresolved_refs: []})")
    g.query("UNWIND $rows AS r CREATE (p:Parameter) SET p = r", {"rows": [
        {"id": "A", "symbol": "A", "source": "notation"}, {"id": "B", "symbol": "B", "source": "clause"},
        {"id": "X", "symbol": "X", "source": "equation"}, {"id": "P", "symbol": "P", "source": "clause"},
        {"id": "Q", "symbol": "Q", "source": "clause"}, {"id": "T", "symbol": "T", "source": "table"}]})
    g.query("MATCH (c:Clause {id: '1.1'}), (b:Parameter {id: 'B'}), (p:Parameter {id: 'P'}), (q:Parameter {id: 'Q'}) "
            "CREATE (c)-[:DEFINES]->(b), (c)-[:DEFINES]->(p), (c)-[:DEFINES]->(q)")
    g.query("CREATE (:Table {id: 'Table 1-1'})")
    g.query("MATCH (t:Table {id: 'Table 1-1'}), (p:Parameter {id: 'T'}) CREATE (t)-[:GIVES]->(p)")
    # E1: B = A * X (X is defined nowhere); E2: P = Q + T; E3: Q = 2 P (a cycle P <-> Q)
    for eid, computes, uses in (("E1", "B", ["A", "X"]), ("E2", "P", ["Q", "T"]), ("E3", "Q", ["P"])):
        g.query("MATCH (c:Clause {id: '1.2'}) CREATE (c)-[:CONTAINS]->(:Equation {id: $id, page: 5})", {"id": eid})
        g.query("MATCH (e:Equation {id: $e}), (p:Parameter {id: $p}) CREATE (e)-[:COMPUTES]->(p)",
                {"e": eid, "p": computes})
        for u in uses:
            g.query("MATCH (e:Equation {id: $e}), (p:Parameter {id: $p}) CREATE (e)-[:USES]->(p)", {"e": eid, "p": u})
    yield g
    g.delete()


def test_each_check_finds_its_hits_and_nothing_else(graph):
    found = run_checks(graph)
    assert {f.id for f in found.items.values()} == {
        "undefined_parameter:X", "missing_reference:1.1|1.9", "dependency_cycle:P|Q"}
    assert all(f.severity == "medium" and f.reason for f in found.items.values())
    undefined = found.items["undefined_parameter:X"]
    assert ("Equation", "E1") in undefined.subjects and undefined.page == 5


def test_cycles_include_self_loops_and_skip_chains():
    assert _cycles({"a": {"b"}, "b": {"c"}}) == []
    assert _cycles({"a": {"a"}}) == [["a"]]
    assert _cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}) == [["a", "b", "c"]]


def test_findings_are_merged_on_rerun_and_stale_open_ones_removed(graph):
    write_findings(graph, run_checks(graph))
    write_findings(graph, run_checks(graph))
    assert graph.ro_query("MATCH (f:Finding) RETURN count(f)").result_set[0][0] == 3
    graph.query("MATCH (f:Finding {id: 'dependency_cycle:P|Q'}) SET f.review_status = 'approved'")
    graph.query("MATCH (:Equation {id: 'E3'})-[e:USES]->() DELETE e")  # cycle fixed
    graph.query("MATCH (:Equation {id: 'E1'})-[e:USES]->(:Parameter {id: 'X'}) DELETE e")  # no longer undefined
    write_findings(graph, run_checks(graph))
    left = {r[0] for r in graph.ro_query("MATCH (f:Finding) RETURN f.id").result_set}
    assert left == {"missing_reference:1.1|1.9", "dependency_cycle:P|Q"}  # the decided one stays


def test_findings_show_in_the_review_queue_and_can_be_approved_or_dismissed(graph):
    from app.api.review_routes import Paths, approve, item_detail, queue

    write_findings(graph, run_checks(graph))
    items = [i for i in queue(graph) if i["label"] == "Finding"]
    assert len(items) == 3 and all(i["severity"] == "medium" for i in items)
    detail = item_detail(graph, GRAPH, "Finding", "undefined_parameter:X")
    assert detail["finding"]["proposal"] is None
    assert {s["id"] for s in detail["finding"]["subjects"]} >= {"X", "E1"}
    assert approve(graph, "Finding", "undefined_parameter:X", None, Paths())["approved"]
    assert item_detail(graph, GRAPH, "Finding", "undefined_parameter:X")["review_status"] == "approved"


def test_approving_a_proposal_applies_its_edge(graph):
    from app.api.review_routes import Paths, approve
    from app.ingest.global_pass.edges import inferred_edge

    edge = inferred_edge(("Clause", "1.1"), "REFERENCES", ("Table", "Table 1-1"), 0.5, "uses its values", "fake",
                         type="uses_value_from")
    found = FindingSet({"inferred_edge"})
    found.add(Finding("inferred_edge", "Table 1-1", "low confidence", [("Table", "Table 1-1")],
                      proposal={"type": "edges", "edges": [edge]}, confidence=0.5, method="claude:fake"))
    write_findings(graph, found)
    assert approve(graph, "Finding", "inferred_edge:Table 1-1", None, Paths())["applied"]
    rows = graph.ro_query("MATCH (:Clause {id: '1.1'})-[e:REFERENCES]->(:Table) RETURN e.method, e.review_status, "
                          "e.type").result_set
    assert rows == [["inferred", "approved", "uses_value_from"]]
