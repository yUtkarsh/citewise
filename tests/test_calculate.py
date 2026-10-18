"""``calculate``: cited inputs checked against the graph, arithmetic in SymPy; agent-selected
facts; warnings and one graph per session. A fake model, never the API."""
from __future__ import annotations

import json

import pytest

from app.agent import stream
from app.agent.calculate import SELECTED, calculate
from app.agent.facts import Fact, MemoryFacts
from app.agent.loop import Agent
from app.agent.tools import Toolbox
from app.graph import connection
from tests.agent_graph import GRAPH, code_graph  # noqa: F401  (fixture)
from tests.test_agent_loop import Script, text, tool

T = "Table 2-1"


@pytest.fixture
def graph(code_graph):
    """A table whose values sit under headers that name S, and a note that permits interpolation."""
    g = code_graph
    g.query("CREATE (:Table {id: $t, caption: 'Table 2-1 Site factors', page: 20, pages: [20], status: 'extracted'})",
            {"t": T})
    rows = [(0, ["Site", "S = 0.50", "S = 0.75"], True, None),
            (1, ["C", "1.2", "1.1"], False, [{"col": 1, "value": "1.2", "number": 1.2}, {"col": 2, "value": "1.1", "number": 1.1}]),
            (2, ["NOTE: Straight-line interpolation shall be used for intermediate values of S."], False, None),
            (3, ["D", "1.4", "1.2"], False, [{"col": 1, "value": "1.4", "number": 1.4}, {"col": 2, "value": "1.2", "number": 1.2}])]
    for i, cells, header, values in rows:
        g.query("MATCH (t:Table {id: $t}) CREATE (t)-[:HAS_ROW {index: $i}]->(:Row {id: $id, index: $i, cells: $c, "
                "is_header: $h, values: $v, conditions: $k, status: 'extracted'})",
                {"t": T, "i": i, "id": f"{T}#r{i}", "c": json.dumps(cells), "h": header,
                 "v": json.dumps(values) if values else None, "k": json.dumps([f"Site {cells[0]}"]) if values else None})
    yield g
    g.query("MATCH (t:Table {id: $t}) OPTIONAL MATCH (t)-[:HAS_ROW]->(r) DETACH DELETE t, r", {"t": T})


def cell(name, row, col, value, header):
    return {"name": name, "source": "cell", "table": T, "row": f"{T}#r{row}", "col": col, "value": value, "header": header}


def interp(lo=cell("y0", 1, 1, "1.2", "S = 0.50"), hi=cell("y1", 1, 2, "1.1", "S = 0.75"), permit=None):
    return {"operation": "interpolate", "result": "F_a",
            "inputs": [{"name": "x", "source": "fact", "fact": "S"}, lo, hi],
            "interpolate": {"x": "x", "lower": "y0", "upper": "y1"},
            "permitted_by": permit if permit is not None else {"kind": "note_row", "id": f"{T}#r2"}}


FACTS = {"S": Fact("S", 0.6)}


def test_interpolation_between_cited_cells_with_the_permitting_note(graph):
    r = calculate(graph, interp(), FACTS)
    assert not r.error and r.data["result"]["value"] == pytest.approx(1.16)
    assert r.data["permitted_by"]["kind"] == "note_row" and "interpolation" in r.data["permitted_by"]["text"]
    assert r.data["column_check"] == "header sits over the cited column"
    assert all(x.label == SELECTED for x in r.labels) and {"Table 2-1#r1", "Table 2-1#r2"} <= {n for _, n in r.touched.nodes}


def test_a_wrong_cell_value_or_header_is_refused(graph):
    r = calculate(graph, interp(lo=cell("y0", 1, 1, "1.3", "S = 0.50")), FACTS)
    assert r.error and "holds '1.2', not '1.3'" in r.summary
    r = calculate(graph, interp(lo=cell("y0", 1, 1, "1.2", "S = 1.00")), FACTS)
    assert r.error and "not in the table's header" in r.summary
    r = calculate(graph, interp(lo={**cell("y0", 1, 1, "1.2", "S = 0.50"), "row": "Table 1-1#r1"}), FACTS)
    assert r.error and "is not a row of Table 2-1" in r.summary


def test_interpolation_needs_a_note_that_permits_it_and_x_between_the_columns(graph):
    r = calculate(graph, interp(permit={}), FACTS)
    assert r.error and "permits it" in r.summary
    r = calculate(graph, interp(permit={"kind": "note_row", "id": f"{T}#r1"}), FACTS)
    assert r.error and "does not mention interpolation" in r.summary
    r = calculate(graph, interp(), {"S": Fact("S", 0.9)})
    assert r.error and "no extrapolation" in r.summary


def test_a_formula_uses_only_cited_inputs(graph):
    args = {"operation": "formula", "formula": "y * s", "result": "S_XS",
            "inputs": [cell("y", 3, 2, "1.2", "S = 0.75"), {"name": "s", "source": "fact", "fact": "S"}]}
    assert calculate(graph, args, FACTS).data["result"]["value"] == pytest.approx(0.72)
    assert "may not contain numbers" in calculate(graph, {**args, "formula": "y * 0.75"}, FACTS).summary
    assert "without a cited input" in calculate(graph, {**args, "formula": "y * q"}, FACTS).summary


def test_a_matched_row_fact_is_kept_only_for_values_compute_mapped(code_graph):
    box = Toolbox(code_graph, MemoryFacts())
    box.run("compute", {"target": "Table 1-1", "row_id": "Table 1-1#r3", "reason": "type B"})  # maps a = 0.5
    r = box.run("save_session_facts", {"facts": [{"id": "a", "value": 0.5, "source": "matched_row"},
                                                 {"id": "b", "value": 0.9, "source": "matched_row"}]})
    facts = box.store.all()
    assert facts["a"].source == "matched_row" and facts["b"].source == "agent_selected"
    assert "b saved as agent_selected" in r.summary and any(x.label == SELECTED for x in r.labels)


def test_the_loop_reports_a_refused_calculate_and_labels_selected_inputs(graph):
    bad = interp(lo=cell("y0", 1, 1, "1.3", "S = 0.50"))
    script = Script([tool("save_session_facts", 1, facts=[{"id": "S", "value": 0.6, "source": "user"}]),
                     tool("calculate", 2, **bad)],
                    [tool("calculate", 3, **interp())],
                    [text("F_a = 1.16 for site C at S = 0.6.")])
    a = Agent(Toolbox(graph, MemoryFacts()), transport=script, model="claude-sonnet-5-5")
    turn = a.ask("F_a for site C at S = 0.6?")
    refused = [c for c in turn.calls if c.name == "calculate" and c.result.error]
    assert len(refused) == 1 and '"is_error": true' in json.dumps(script.requests[1]["messages"][-1]["content"][1])
    assert "inputs selected by agent" in turn.answer  # the checker adds the label the answer left out
    assert not turn.warnings  # 1.16 comes from calculate


@pytest.fixture
def hook(code_graph, monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_NAME", "memory_calc_test")
    monkeypatch.setattr(stream, "_encoder", lambda texts: [[0.0] * 8 for _ in texts])
    stream.forget_sessions()

    def run(script, question, session, graph_name=GRAPH):
        monkeypatch.setattr(stream, "transport_for_tests", script)
        return list(stream.stream_chat(question, session_id=session, graph=graph_name, facts=None, rerun=False))
    yield run
    stream.forget_sessions()
    try:
        connection.get_db().select_graph("memory_calc_test").delete()
    except Exception:
        pass


def test_warnings_reach_the_page_and_the_question_and_a_session_keeps_its_graph(hook):
    events = hook(Script([tool("get_clause", 1, id="1.2")], [text("The answer is 999.")]), "Anything?", "s-warn")
    done = events[-1]["data"]
    assert done["warnings"] and "999" in done["warnings"][0]
    stored = connection.get_db().select_graph("memory_calc_test").ro_query(
        "MATCH (:Session {id: 's-warn'})-[:ASKED]->(q) RETURN q.warnings, q.tools").result_set[0]
    assert "999" in stored[0][0] and json.loads(stored[1])[0]["tool"] == "get_clause"
    refused = hook(Script(), "Anything?", "s-warn", graph_name="code_other")
    assert refused[0]["event"] == "error" and refused[0]["data"]["code"] == "session_graph"
    stream.forget_sessions()  # a restarted server refuses it too, from the memory graph
    refused = hook(Script(), "Anything?", "s-warn", graph_name="code_other")
    assert refused[0]["data"]["code"] == "session_graph"
