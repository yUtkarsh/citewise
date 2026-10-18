"""The chat page hook (web/EVENTS.md): event order and payloads, session continuation
from the memory graph, and facts-panel re-runs. A scripted fake model, never the API."""
from __future__ import annotations

import pytest

from app.agent import stream
from app.graph import connection
from tests.agent_graph import GRAPH, code_graph  # noqa: F401  (fixture)
from tests.test_agent_loop import Script, text, tool
from tests.test_memory import calc_turn, whatif_turn

MEMORY = "memory_stream_test"


@pytest.fixture
def hook(code_graph, monkeypatch):
    monkeypatch.setenv("MEMORY_GRAPH_NAME", MEMORY)
    monkeypatch.setattr(stream, "_encoder", lambda texts: [[0.0] * 8 for _ in texts])
    stream.forget_sessions()

    def run(script, question, session="s-test", **kw):
        monkeypatch.setattr(stream, "transport_for_tests", script)
        return list(stream.stream_chat(question, session_id=session, graph=GRAPH, facts=kw.get("facts"),
                                       rerun=kw.get("rerun", False)))
    yield run
    stream.forget_sessions()
    try:
        connection.get_db().select_graph(MEMORY).delete()
    except Exception:
        pass


def names(events) -> list[str]:
    return [e["event"] for e in events]


def answer(events) -> str:
    return "".join(e["data"]["text"] for e in events if e["event"] == "answer_delta")


def test_events_follow_the_contract(hook):
    events = hook(Script(*calc_turn(1)), "Base shear for C = 0.25 and W = 800 kips?")
    assert "error" not in names(events)
    calls = [e["data"] for e in events if e["event"] == "tool_call"]
    touched = [e["data"] for e in events if e["event"] == "touched"]
    assert [c["name"] for c in calls] == ["save_session_facts", "compute", "trace"]
    for c in calls:  # each tool_call comes before its touched event
        i = next(k for k, e in enumerate(events) if e["event"] == "tool_call" and e["data"]["id"] == c["id"])
        j = next(k for k, e in enumerate(events) if e["event"] == "touched" and e["data"]["tool_call_id"] == c["id"])
        assert i < j
    compute = next(t for t in touched if t["tool_call_id"] == calls[1]["id"])
    assert {"label": "Equation", "id": "Equation 1-1"} in compute["nodes"]
    assert "Equation 1-1|COMPUTES|V" in compute["edges"] and compute["summary"].startswith("Equation 1-1: V = 200")
    facts = [e["data"]["facts"] for e in events if e["event"] == "facts"][-1]
    assert {(f["param_id"], f["value"], f["source"]) for f in facts} == {("C", 0.25, "user"), ("W", 800, "user")}
    assert answer(events).startswith("V = 200 kips")
    done = events[-1]
    assert done["event"] == "answer_done"
    assert {"label": "Equation", "id": "Equation 1-1"} in done["data"]["path"]["nodes"]
    assert "1.2|CONTAINS|Equation 1-1" in done["data"]["path"]["edges"]
    assert [c["id"] for c in done["data"]["citations"]] == ["Equation 1-1", "1.2"]
    assert done["data"]["cost"]["usd"] > 0 and done["data"]["cost"]["tool_calls"] == 3


def test_a_session_is_continued_from_the_memory_graph(hook):
    script = Script(*calc_turn(1), *whatif_turn(10, 900))
    hook(script, "Base shear for C = 0.25 and W = 800 kips?", session="s-resume")
    stream.forget_sessions()  # as a server restart would
    events = hook(script, "What if W is 900 kips?", session="s-resume")
    assert answer(events).startswith("Changed: W 800 → 900 kips. V 200 → 225 kips; same path (Equation 1-1).")
    sent = script.requests[-1]["messages"]
    assert sent[0] == {"role": "user", "content": "Base shear for C = 0.25 and W = 800 kips?"}  # history restored
    assert "W = 800" in script.requests[-1]["system"][1]["text"]  # facts from the ASSUMES edges


def test_the_facts_panel_reruns_the_last_question(hook):
    script = Script(*calc_turn(1), [text("V is now 250 kips.")])
    hook(script, "Base shear for C = 0.25 and W = 800 kips?", session="s-panel")
    panel = [{"param_id": "C", "value": 0.25, "unit": None, "source": "user"},
             {"param_id": "W", "value": 1000, "unit": "kips", "source": "user"}]
    events = hook(script, "Base shear for C = 0.25 and W = 800 kips?", session="s-panel", facts=panel, rerun=True)
    assert [e["data"]["name"] for e in events if e["event"] == "tool_call"] == ["whatif_replay"]
    assert answer(events).startswith("Changed: W 800 → 1000 kips. V 200 → 250 kips")
    assert script.requests[-1]["tool_choice"] == {"type": "none"}
    assert {"label": "Equation", "id": "Equation 1-1"} in events[-1]["data"]["path"]["nodes"]
    messages = connection.get_db().select_graph(MEMORY).ro_query(
        "MATCH (:Session {id: 's-panel'})-[:HAS_MESSAGE]->(m) RETURN m.text ORDER BY m.n").result_set
    assert messages[2][0] == "(facts edited) Base shear for C = 0.25 and W = 800 kips?"


def test_a_failure_becomes_an_error_event(hook):
    def broken(kwargs):
        raise RuntimeError("model unavailable")
    events = hook(broken, "anything", session="s-error")
    assert names(events) == ["error"] and "model unavailable" in events[0]["data"]["message"]
