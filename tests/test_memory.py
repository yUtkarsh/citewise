"""Memory graph and what-if answers (SPEC Step 6), with a scripted fake model."""
from __future__ import annotations

import pytest

from app.agent.facts import Fact, MemoryFacts, RowMatch, facts_block
from app.agent.loop import Agent
from app.agent.tools import Toolbox
from app.graph import connection
from app.memory.graph import MemoryGraph
from tests.agent_graph import code_graph  # noqa: F401  (fixture)
from tests.test_agent_loop import Script, text, tool

MEMORY_TEST = "memory_test"


@pytest.fixture
def memory(code_graph):
    db = connection.get_db()
    m = MemoryGraph(db, "code_test_agent", graph_name=MEMORY_TEST)
    yield m
    try:
        db.select_graph(MEMORY_TEST).delete()
    except Exception:
        pass


def calc_turn(n: int, c: float = 0.25, w: float = 800) -> list[list[dict]]:
    return [[tool("save_session_facts", n, facts=[{"id": "C", "value": c, "source": "user"},
                                                   {"id": "W", "value": w, "unit": "kips", "source": "user"}]),
             tool("compute", n + 1, target="Equation 1-1")],
            [tool("trace", n + 2, items=[{"id": "Equation 1-1"}, {"id": "1.2"}])],
            [text("V = 200 kips (Equation 1-1, 1.2, p. 10).")]]


def whatif_turn(n: int, w: float) -> list[list[dict]]:
    return [[tool("save_session_facts", n, facts=[{"id": "W", "value": w, "unit": "kips", "source": "user"}])],
            [text("The base shear is updated as above.")]]


def test_memory_graph_stores_facts_rows_and_answer_paths(code_graph, memory):
    before = code_graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
    assert memory.set(Fact("W", 800.0, "kips")) is None
    assert memory.set(Fact("W", 900.0, "kips")).value == 800.0
    memory.set(Fact("performance_level", "Life Safety", None, "user", kind="descriptor"))
    memory.match_row(RowMatch("Table 1-1#r1", "Table 1-1", "type A"))
    memory.match_row(RowMatch("Table 1-1#r3", "Table 1-1", "type B"))  # one match per table
    facts = memory.all()
    assert facts["W"].value == 900.0 and facts["performance_level"].kind == "descriptor"
    assert [m.row_id for m in memory.matches()] == ["Table 1-1#r3"]
    qid = memory.record_question("q?", "a.", [{"tool": "compute"}],
                                 [{"label": "Equation", "id": "Equation 1-1"}, {"label": "Clause", "id": "1.2"}])
    assert memory.answer_path(qid) == ["Equation 1-1", "1.2"]
    assert memory.last_question()["calls"] == [{"tool": "compute"}]
    assert "W = 900.0 kips (from you)" in facts_block(memory) and "matched Table 1-1#r3" in facts_block(memory)
    assert code_graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0] == before  # code graph untouched


def test_a_what_if_reruns_the_path_and_leads_with_one_change_line(code_graph, memory):
    script = Script(*calc_turn(1), *whatif_turn(10, 900), *whatif_turn(20, 1000))
    a = Agent(Toolbox(code_graph, memory), transport=script, model="claude-sonnet-5-5", memory=memory)
    first = a.ask("Base shear for C = 0.25 and W = 800 kips?")
    assert first.change_line is None
    second = a.ask("What if W is 900 kips?")
    line = "Changed: W 800 → 900 kips. V 200 → 225 kips; same path (Equation 1-1)."
    assert second.change_line == line and second.answer.startswith(line)
    assert second.base_question == first.question
    third = a.ask("And 1000 kips?")  # a second what-if re-runs the re-run path
    assert third.answer.startswith("Changed: W 900 → 1000 kips. V 225 → 250 kips; same path (Equation 1-1).")
    assert memory.last_question()["text"] == first.question


def test_editing_a_fact_outside_the_chat_reanswers_with_no_tools(code_graph):
    script = Script(*calc_turn(1), [text("V is now 275 kips.")])
    a = Agent(Toolbox(code_graph, MemoryFacts()), transport=script, model="claude-sonnet-5-5")
    a.ask("Base shear for C = 0.25 and W = 800 kips?")
    result = a.toolbox.save_facts([{"id": "W", "value": 1100, "unit": "kips", "source": "user"}])
    assert "replay" in result.data
    turn = a.answer_replay(a.last_replay)
    assert script.requests[-1]["tool_choice"] == {"type": "none"}
    assert turn.answer.startswith("Changed: W 800 → 1100 kips. V 200 → 275 kips")
    assert not turn.warnings  # 275 is in the re-run results


def test_a_changed_row_shows_in_the_change_line(code_graph):
    script = Script(
        [tool("save_session_facts", 1, facts=[{"id": "R", "value": 2, "source": "user"}]),
         tool("compute", 2, target="Table 1-1", row_id="Table 1-1#r1", reason="the member is type A")],
        [text("a = 0.02 (Table 1-1, row 1).")],
        [tool("save_session_facts", 3, facts=[{"id": "R", "value": 4.5, "source": "user"}])],
        [text("Updated.")])
    a = Agent(Toolbox(code_graph, MemoryFacts()), transport=script, model="claude-sonnet-5-5")
    a.ask("Rotation limit a for a type A member with R = 2?")
    turn = a.ask("What if R is 4.5?")
    assert "a 0.02 → 0.015" in turn.change_line and "path changed: now uses Table 1-1#r2" in turn.change_line


def test_a_value_corrected_within_one_turn_is_not_a_what_if(code_graph):
    script = Script(*calc_turn(1),
                    [tool("save_session_facts", 5, facts=[{"id": "k", "value": 1, "source": "user"}]),
                     tool("save_session_facts", 6, facts=[{"id": "k", "value": 2, "source": "user"}])],
                    [text("k is 2.")])
    a = Agent(Toolbox(code_graph, MemoryFacts()), transport=script, model="claude-sonnet-5-5")
    a.ask("Base shear?")
    assert a.ask("k is 1, no, 2.").change_line is None


def test_a_what_if_reruns_the_latest_answer_the_change_affects(code_graph):
    script = Script(*calc_turn(1),
                    [tool("get_clause", 5, id="1.3")], [text("1.3 covers the story force.")],
                    *whatif_turn(10, 900))
    a = Agent(Toolbox(code_graph, MemoryFacts()), transport=script, model="claude-sonnet-5-5")
    first = a.ask("Base shear for C = 0.25 and W = 800 kips?")
    a.ask("What does 1.3 cover?")  # no replayable path that uses W
    turn = a.ask("What if W is 900 kips?")
    assert turn.change_line.startswith("Changed: W 800 → 900 kips. V 200 → 225 kips")
    assert turn.base_question == first.question
