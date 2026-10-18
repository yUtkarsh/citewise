"""The agent's fact saves are checked: 'user' only for numbers the user wrote this
session, 'assumed' only after the user accepts offered typical values; anything else
becomes 'agent_selected', labelled. Saves the user makes directly (facts panel, /set)
keep their source. A scripted fake model on the invented code graph."""
from __future__ import annotations

from app.agent.facts import MemoryFacts
from app.agent.loop import Agent
from app.agent.tools import Toolbox
from tests.agent_graph import code_graph  # noqa: F401  (fixture)
from tests.test_agent_loop import Script, text, tool


def save(n, *facts):
    return [tool("save_session_facts", n, facts=[{"id": i, "value": v, "source": s} for i, v, s in facts])]


def agent(graph, script):
    return Agent(Toolbox(graph, MemoryFacts()), transport=script, model="claude-sonnet-5-5", log=lambda m: None)


def test_a_number_the_user_never_gave_is_not_saved_as_user(code_graph):
    script = Script(save(1, ("W", 2400, "user"), ("C", 1.0, "user")), [text("ok")])
    a = agent(code_graph, script)
    turn = a.ask("Weight W = 2,400 kips. What is V?")
    facts = a.toolbox.store.all()
    assert facts["W"].source == "user" and facts["C"].source == "agent_selected"
    result = turn.calls[0].result
    assert "C saved as agent_selected (the user did not give C = 1.0)" in result.summary
    assert any(x.target == "C" and x.label == "inputs selected by agent" for x in result.labels)


def test_numbers_from_earlier_messages_of_the_session_count(code_graph):
    script = Script([text("noted")], save(1, ("W", 800, "user")), [text("ok")])
    a = agent(code_graph, script)
    a.ask("My building weighs 800 kips.")
    a.ask("Now compute V.")
    assert a.toolbox.store.all()["W"].source == "user"


def test_assumed_needs_the_users_acceptance_of_offered_values(code_graph):
    script = Script(save(1, ("C", 0.25, "assumed")),
                    [text("C is missing; a typical value is 0.25 (assumed: engineering judgment). Accept it?")],
                    save(2, ("C", 0.25, "assumed")), [text("ok")])
    a = agent(code_graph, script)
    a.ask("What is V?")
    assert a.toolbox.store.all()["C"].source == "agent_selected"  # saved before anything was accepted
    a.ask("Yes, use the typical value.")
    assert a.toolbox.store.all()["C"].source == "assumed"


def test_direct_saves_by_the_user_keep_their_source(code_graph):
    box = Toolbox(code_graph, MemoryFacts(), user_texts=["What is V?"])
    box.save_facts([{"id": "C", "value": 0.3, "source": "user"}], trusted=True)  # the facts panel
    assert box.store.all()["C"].source == "user"
    box.save_facts([{"id": "W", "value": 900, "source": "user"}])
    assert box.store.all()["W"].source == "agent_selected"
    box.save_facts([{"id": "kind", "value": "frame", "source": "user"}])  # a choice in words is not checked
    assert box.store.all()["kind"].source == "user"
