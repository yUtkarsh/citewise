"""The agent loop with a scripted fake model (never the API), on the invented code graph."""
from __future__ import annotations

import pytest

from app.agent.facts import MemoryFacts
from app.agent.loop import FINAL_NOTE, WRAP_UP_NOTE, Agent, AgentReply
from app.config import get_settings
from app.agent.results import ToolResult
from app.agent.tools import Toolbox
from tests.agent_graph import code_graph  # noqa: F401  (fixture)

USAGE = {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def tool(name: str, n: int, **args) -> dict:
    return {"type": "tool_use", "id": f"tu_{name}_{n}", "name": name, "input": args}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


DEFAULT_BUDGET = get_settings().agent_max_tool_calls_simple
PROCEDURE_BUDGET = get_settings().agent_max_tool_calls
THINKING_ONLY = [{"type": "thinking", "thinking": "", "signature": "sig-x"}]


class Script:
    """Replies in order (an exception in the list is raised); records every request."""

    def __init__(self, *replies: list[dict]):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def __call__(self, kwargs: dict) -> AgentReply:
        self.requests.append(kwargs)
        content = self.replies.pop(0) if self.replies else [text("done")]
        if isinstance(content, Exception):
            raise content
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return AgentReply(content, stop, dict(USAGE), "claude-sonnet-5-5")


def agent(graph, script, **kw) -> Agent:
    return Agent(Toolbox(graph, MemoryFacts()), transport=script, model="claude-sonnet-5-5", log=lambda m: None, **kw)


def test_a_calculation_turn_runs_tools_and_keeps_numbers_from_compute(code_graph):
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-1"}
    script = Script(
        [thinking, tool("find_entry", 1, query="base shear")],
        [tool("save_session_facts", 2, facts=[{"id": "C", "value": 0.25, "source": "user"},
                                               {"id": "W", "value": 800, "unit": "kips", "source": "user"}]),
         tool("compute", 3, target="Equation 1-1")],
        [tool("trace", 4, items=[{"id": "Equation 1-1"}, {"id": "1.2"}])],
        [text("V = 200 kips (Equation 1-1 in 1.2 Base Shear, p. 10).")])
    a = agent(code_graph, script)
    turn = a.ask("Base shear for C = 0.25 and W = 800 kips?")
    assert [c.name for c in turn.calls] == ["find_entry", "save_session_facts", "compute", "trace"]
    assert turn.answer.startswith("V = 200 kips") and not turn.warnings
    # the thinking block is passed back unchanged within the turn
    assert script.requests[1]["messages"][1]["content"][0] == thinking
    assert {"Equation 1-1", "1.2"} <= {n["id"] for n in turn.path()}


def test_system_prompt_and_tools_are_cached_and_facts_follow_the_breakpoint(code_graph):
    script = Script([text("ok")])
    a = agent(code_graph, script)
    a.ask("hello")
    req = script.requests[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Current project facts" in req["system"][1]["text"] and "cache_control" not in req["system"][1]
    assert req["cache_control"] == {"type": "ephemeral"} and req["tools"][-1]["name"] == "run_cypher"
    assert "tool_choice" not in req


def test_stored_history_has_one_line_tool_summaries_and_no_tool_blocks(code_graph):
    script = Script([tool("get_clause", 1, id="1.2")], [text("1.2 requires 1.3.")], [text("second")])
    a = agent(code_graph, script)
    a.ask("What does 1.2 require?")
    a.ask("And then?")
    second = script.requests[-1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert all(isinstance(m["content"], str) for m in second)
    assert "get_clause(" in second[1]["content"] and "-> 1.2 Base Shear" in second[1]["content"]


def test_the_tool_budget_ends_with_a_tool_free_answer(code_graph):
    script = Script(*[[tool("find_entry", i, query="x")] for i in range(DEFAULT_BUDGET + 3)])
    a = agent(code_graph, script)
    turn = a.ask("loop forever")
    assert len(turn.calls) == DEFAULT_BUDGET
    assert script.requests[-1]["tool_choice"] == {"type": "none"}


def test_a_procedure_question_gets_the_larger_budget(code_graph):
    replies = [[tool("plan_procedure", 0, goal="V")]] + [[tool("find_entry", i, query="x")]
                                                         for i in range(PROCEDURE_BUDGET + 2)]
    a = agent(code_graph, Script(*replies))
    a.toolbox.procedure = lambda g, goal, facts, encode: ToolResult("2 steps", {"steps": []})
    assert len(a.ask("What are the steps to get V?").calls) == PROCEDURE_BUDGET


def test_missing_labels_are_added_and_unsupported_numbers_warned(code_graph):
    script = Script([tool("compute", 1, target="Equation 1-2")], [text("F is about 999 (Equation 1-2).")])
    turn = agent(code_graph, script).ask("Story force?")
    assert "Labels (added by the checker)" in turn.answer and "unreviewed" in turn.answer
    assert turn.warnings and "999" in turn.warnings[0]


def test_the_cost_cap_stops_before_the_next_call(code_graph):
    script = Script([tool("find_entry", 1, query="x")], [text("never sent")])
    a = agent(code_graph, script, max_cost=0.032)
    turn = a.ask("anything")
    assert turn.stopped and "cost cap" in turn.stopped and len(script.requests) == 1
    assert a.spent == pytest.approx(0.003)


def test_with_trace_only_the_answer_paths_labels_are_required(code_graph):
    script = Script([tool("get_clause", 1, id="1.3")], [tool("trace", 2, items=[{"id": "1.2"}])],
                    [text("1.2 Base Shear, p. 10.")])
    turn = agent(code_graph, script).ask("What is 1.2?")
    assert "Labels (added by the checker)" not in turn.answer  # 1.3 is flagged but not on the path


def test_three_calls_before_the_budget_the_model_is_told_to_wrap_up(code_graph):
    script = Script(*[[tool("find_entry", i, query="x")] for i in range(DEFAULT_BUDGET)], [text("answer")])
    agent(code_graph, script).ask("many lookups")
    notes = [b["text"] for m in script.requests[-1]["messages"] if isinstance(m["content"], list)
             for b in m["content"] if b.get("type") == "text"]  # each request repeats the turn so far
    wrap = WRAP_UP_NOTE.format(left=3)
    assert notes.count(wrap) == 1
    first = next(i for i, r in enumerate(script.requests) if any(
        wrap in str(m["content"]) for m in r["messages"]))
    assert first == DEFAULT_BUDGET - 3  # the request after call budget-3 carries it


def test_a_reply_without_text_at_the_budget_gets_a_final_tool_free_answer(code_graph):
    script = Script(*[[tool("find_entry", i, query="x")] for i in range(DEFAULT_BUDGET)], THINKING_ONLY,
                    [text("Here is the procedure, from what the tools found.")])
    turn = agent(code_graph, script).ask("explain everything")
    assert turn.answer.startswith("Here is the procedure")
    final = script.requests[-1]
    assert final["tool_choice"] == {"type": "none"} and FINAL_NOTE in str(final["messages"][-1]["content"])
    assert final["messages"][-1]["role"] == "user" and final["messages"][-2]["role"] == "assistant"


def test_a_failed_final_call_gives_a_visible_error_never_an_empty_answer(code_graph):
    script = Script(*[[tool("find_entry", i, query="x")] for i in range(DEFAULT_BUDGET)], THINKING_ONLY,
                    RuntimeError("overloaded"))
    turn = agent(code_graph, script).ask("explain everything")
    assert turn.answer.startswith("(No answer: the final answer call failed (RuntimeError: overloaded)")
    script = Script(THINKING_ONLY, THINKING_ONLY)
    assert "returned no text" in agent(code_graph, script).ask("hello").answer
