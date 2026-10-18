"""The chat agent's tool loop (SPEC Step 5) on ``MODEL_AGENT``.

Per question: the cached system prompt (answer rules) and tool definitions, then the
current project facts (frozen for the question, so the prefix never changes inside a
turn), then the history. Claude calls tools until it answers; at most
AGENT_MAX_TOOL_CALLS_SIMPLE calls (default 10), or AGENT_MAX_TOOL_CALLS (default 30) once
it calls ``plan_procedure``. With WRAP_UP_AT calls left the tool result tells it to start
wrapping up; when the budget is used up, the next call has no tools. A turn never ends
with an empty answer: a reply without text gets one more call without tools, and if that
fails too the answer is a visible error saying why.
Past turns are stored as text only: the question, a one-line summary of each tool
call and the answer. No thinking block is ever replayed across questions, so
trimming history never edits a turn whose reasoning is sent back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Callable

from app.agent.guard import answer_labels, unsupported_numbers, with_label_footer
from app.agent.results import Label, ToolResult
from app.agent.tools import PROCEDURE_TOOL, TOOLS, Toolbox, args_text
from app.agent.facts import facts_block
from app.agent.whatif import REPLAYABLE, outcome, replay
from app.config import get_settings, require_model
from app.ingest.pass2.runner import cost_of
from app.prompts import load

DEFAULT_BUDGET, PROCEDURE_BUDGET = 10, 30  # defaults of AGENT_MAX_TOOL_CALLS_SIMPLE / AGENT_MAX_TOOL_CALLS
WRAP_UP_AT = 3
WRAP_UP_NOTE = "{left} tool calls left for this question: start wrapping up. Use them only for what the answer " \
               "cannot do without (a missing value, trace), then write the answer."
FINAL_NOTE = "Write your answer to the question now, in full, from the tool results above. Tools are not available."
MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
BUDGET_NOTE = "Tool budget for this question is used up. Answer now with what the tool results show, and say " \
              "what is missing."
MIN_CALL_ESTIMATE = 0.03  # USD: assumed cost of the next call before any call has been measured


@dataclass
class AgentReply:
    content: list[dict]
    stop_reason: str
    usage: dict
    model: str


Transport = Callable[[dict], AgentReply]


def anthropic_transport(fallbacks: bool) -> Transport:
    import anthropic

    client = anthropic.Anthropic()

    def send(kwargs: dict) -> AgentReply:
        if fallbacks:
            message = client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            message = client.messages.create(**kwargs)
        content = [block.model_dump(mode="json", exclude_none=True) for block in message.content]
        u = message.usage
        usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
        return AgentReply(content, message.stop_reason or "", usage, message.model)

    return send


@dataclass
class ToolCall:
    name: str
    args: dict
    result: ToolResult
    id: str = ""  # the tool-use id

    def line(self) -> str:
        return f"{self.name}({args_text(self.args)}) -> {self.result.summary}"


@dataclass
class Turn:
    question: str
    answer: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usages: list[dict] = field(default_factory=list)
    cost: float = 0.0
    warnings: list[str] = field(default_factory=list)
    labels_added: list[Label] = field(default_factory=list)
    stopped: str | None = None  # why the loop ended early (cost cap, refusal, max_tokens)
    base_question: str = ""  # the question a what-if re-answers (else the question itself)
    user_text: str | None = None  # what history and memory store as the user's message, if not the question
    change_line: str | None = None
    replayable: list[tuple[str, dict, ToolResult]] = field(default_factory=list)  # the path a what-if re-runs

    def path(self) -> list[dict]:
        """Nodes cited in the answer: trace's items, else every node compute touched,
        else (a what-if) the re-run path."""
        traced = [c.result for c in self.calls if c.name == "trace"]
        computed = [c.result for c in self.calls if c.name == "compute"]
        source = traced[-1:] or computed or [r for n, _, r in self.replayable if n in ("trace", "compute")]
        out = []
        for result in source:
            out += result.touched.as_dict()["nodes"]
        return list({(n["label"], n["id"]): n for n in out}.values())


class Agent:
    def __init__(self, toolbox: Toolbox, transport: Transport | None = None, model: str | None = None,
                 max_cost: float | None = None, log: Callable[[str], None] = print,
                 on_tool: Callable[[ToolCall], None] | None = None, document: str = "", memory=None,
                 on_tool_start: Callable[[str, str, dict], None] | None = None):
        settings = get_settings()
        self.toolbox = toolbox
        self.model = model or require_model("model_agent")
        self.effort = settings.agent_effort
        self.transport = transport or anthropic_transport(settings.server_fallback and "haiku" not in self.model)
        self.max_cost = max_cost
        self.log = log
        self.on_tool = on_tool  # after a tool ran
        self.on_tool_start = on_tool_start  # before it runs: (tool-use id, name, args)
        self.static = load("agent_system").text + (f"\n\nThe code: {document}" if document else "")
        self.history: list[dict] = []
        self.turns: list[Turn] = []
        self.spent = 0.0
        self.memory = memory  # app.memory.graph.MemoryGraph, or None
        self._replayed: ToolResult | None = None
        self._replayed_turn: Turn | None = None  # the earlier answer a what-if re-ran
        self._facts_at_start: dict | None = None  # facts when the current turn began (None outside a turn)
        if toolbox.on_facts_changed is None:
            toolbox.on_facts_changed = self.replay_last

    # ---- what-if ------------------------------------------------------------------------

    def replay_last(self, changes) -> ToolResult | None:
        """Re-run the last answer's path with the changed facts (None if there is none)."""
        if self._facts_at_start is not None:
            changes = [(old, new) for old, new in changes
                       if old is not None and old.id in self._facts_at_start
                       and self._facts_at_start[old.id].value == old.value]
        candidates = [t for t in reversed(self.turns) if t.replayable]
        if not changes or not candidates:
            return None
        # the most recent answer whose results the change affects; else the last one ("no result changed")
        first = None
        for turn in candidates:
            result = replay(self.toolbox, turn.replayable, changes)
            first = first or (turn, result)
            if result.data["results_changed"]:
                first = (turn, result)
                break
        self._replayed_turn, self._replayed = first
        return self._replayed

    @property
    def last_replay(self) -> ToolResult | None:
        return self._replayed

    def new_session(self, store=None, memory=None) -> None:
        """Start over: no history, no turns; a new fact store and memory session if given."""
        self.history.clear()
        self.turns.clear()
        self._replayed = None
        if store is not None:
            self.toolbox.store = store
        self.memory = memory if memory is not None else self.memory

    def answer_replay(self, result: ToolResult) -> Turn:
        """Answer again after a fact was edited outside the chat (the facts panel, /set):
        one model call with the re-run results and no tools."""
        base = (self._replayed_turn or self.turns[-1]).base_question if self.turns else ""
        line = result.data["change_line"]
        message = ("[The user edited a project fact. The path of your last answer was re-run with the new facts.]\n"
                   f"Re-run results: {result.for_model()}\n\n"
                   f"Answer the question again in full, from these results only: {base}\n"
                   f"Start your answer with this line exactly: {line}")
        return self.ask(message, final_first=True, base_question=base, replayed=result,
                        user_text=f"(facts edited) {base}")

    # ---- request ------------------------------------------------------------------------

    def request(self, messages: list[dict], facts_text: str, final: bool) -> dict:
        kwargs = {"model": self.model, "max_tokens": MAX_TOKENS,
                  "system": [{"type": "text", "text": self.static, "cache_control": {"type": "ephemeral"}},
                             {"type": "text", "text": facts_text}],
                  "tools": TOOLS, "messages": list(messages), "cache_control": {"type": "ephemeral"}}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if final:
            kwargs["tool_choice"] = {"type": "none"}
        return kwargs

    def _estimate(self, turn: Turn) -> float:
        costs = [cost_of({"model": self.model, **u}) or 0.0 for u in turn.usages[-2:]]
        return max(costs + [MIN_CALL_ESTIMATE])

    # ---- the loop -----------------------------------------------------------------------

    def ask(self, question: str, final_first: bool = False, base_question: str | None = None,
            replayed: ToolResult | None = None, user_text: str | None = None) -> Turn:
        turn = Turn(question, base_question=base_question or question, user_text=user_text)
        self._replayed = replayed  # set again by replay_last when a fact changes during the turn
        self._facts_at_start = self.toolbox.store.all()
        facts_text = facts_block(self.toolbox.store)
        messages = list(self.history)
        messages.append({"role": "user", "content": question})
        settings = get_settings()
        simple, procedure = settings.agent_max_tool_calls_simple, settings.agent_max_tool_calls
        budget, used, final, warned = simple, 0, final_first, False
        reply = None
        while True:
            if self.max_cost is not None and self.spent + self._estimate(turn) > self.max_cost:
                turn.stopped = f"cost cap ${self.max_cost:.2f} reached (spent ${self.spent:.3f})"
                break
            reply = self.transport(self.request(messages, facts_text, final))
            usage = {"model": reply.model, **reply.usage}
            turn.usages.append(usage)
            cost = cost_of(usage) or 0.0
            turn.cost += cost
            self.spent += cost
            messages.append({"role": "assistant", "content": reply.content})
            uses = [b for b in reply.content if b.get("type") == "tool_use"]
            if reply.stop_reason != "tool_use" or not uses:
                if reply.stop_reason in ("refusal", "max_tokens"):
                    turn.stopped = reply.stop_reason
                break
            results = []
            for block in uses:
                name, args = block.get("name"), block.get("input") or {}
                if used >= budget:
                    result = ToolResult(BUDGET_NOTE, {}, error=True)
                    final = True
                else:
                    used += 1
                    if name == PROCEDURE_TOOL and budget != procedure:
                        budget, warned = procedure, False
                    if self.on_tool_start:
                        self.on_tool_start(block["id"], name, args)
                    result = self.toolbox.run(name, args)
                    call = ToolCall(name, args, result, block["id"])
                    turn.calls.append(call)
                    if self.on_tool:
                        self.on_tool(call)
                results.append({"type": "tool_result", "tool_use_id": block["id"], "content": result.for_model(),
                                **({"is_error": True} if result.error else {})})
            if used >= budget:
                final = True
                results.append({"type": "text", "text": BUDGET_NOTE})
            elif budget - used <= WRAP_UP_AT and not warned:
                warned = True
                results.append({"type": "text", "text": WRAP_UP_NOTE.format(left=budget - used)})
            messages.append({"role": "user", "content": results})

        text = _text(reply)
        if not text and reply is not None and turn.stopped in (None, "max_tokens"):
            text = self.final_answer(messages, facts_text, turn)
        if not text:
            text = (f"(No answer: {turn.stopped or 'the model returned no text'}. The tools used are listed in "
                    f"the trace; ask again, or raise AGENT_MAX_TOOL_CALLS for longer questions.)")
        calls = [(c.name, c.args, c.result) for c in turn.calls]
        replayed = self._replayed
        if replayed is not None:  # a what-if: the change line leads; the re-run path is this turn's path
            turn.change_line = replayed.data["change_line"]
            if not text.startswith(turn.change_line):
                text = turn.change_line + "\n\n" + text
            calls.append(("compute", {}, replayed))  # its labels count as the answer path's labels
            if self.turns and not any(c.name in REPLAYABLE for c in turn.calls):
                turn.base_question = (self._replayed_turn or self.turns[-1]).base_question
                turn.replayable = list(replayed.reruns)
        if not turn.replayable:
            turn.replayable = [(c.name, c.args, c.result) for c in turn.calls if c.name in REPLAYABLE]
        turn.answer, turn.labels_added = with_label_footer(text, answer_labels(calls))
        sources = [question] + [json.dumps(c.result.data, default=str) for c in turn.calls] + \
                  [m["content"] for m in self.history if isinstance(m.get("content"), str)] + \
                  [f.display() for f in self.toolbox.store.all().values()]
        if replayed is not None:
            sources.append(json.dumps(replayed.data, default=str))
        if bad := unsupported_numbers(text, sources):
            turn.warnings.append("numbers not found in any tool result, fact or message: " + ", ".join(bad[:12]))
        self._facts_at_start = None
        self.remember(turn)
        return turn

    def final_answer(self, messages: list[dict], facts_text: str, turn: Turn) -> str:
        """One more call without tools when a turn ended with no answer text (a reply of
        only thinking, or nothing). Returns its text, or '' with ``turn.stopped`` set."""
        if messages and messages[-1]["role"] == "assistant" and not _has_output(messages[-1]["content"]):
            messages.pop()  # an empty assistant turn cannot be sent back
        if messages[-1]["role"] == "assistant":
            messages.append({"role": "user", "content": FINAL_NOTE})
        else:
            last = messages[-1]["content"]
            blocks = last if isinstance(last, list) else [{"type": "text", "text": last}]
            messages[-1] = {"role": "user", "content": blocks + [{"type": "text", "text": FINAL_NOTE}]}
        try:
            reply = self.transport(self.request(messages, facts_text, final=True))
        except Exception as exc:  # the answer says so; a turn is never left empty
            turn.stopped = f"the final answer call failed ({type(exc).__name__}: {str(exc)[:200]})"
            return ""
        usage = {"model": reply.model, **reply.usage}
        turn.usages.append(usage)
        cost = cost_of(usage) or 0.0
        turn.cost += cost
        self.spent += cost
        text = _text(reply)
        if not text:
            turn.stopped = f"the final answer call returned no text (stop reason {reply.stop_reason})"
        return text

    def remember(self, turn: Turn) -> None:
        """Store the turn as text: tool results become one-line summaries. With a memory
        graph, also the messages and the question's answer path."""
        tools = "\n".join(f"- {c.line()}" for c in turn.calls)
        self.history.append({"role": "user", "content": turn.user_text or turn.question})
        self.history.append({"role": "assistant",
                             "content": (f"[Tools used for this answer:\n{tools}]\n\n" if tools else "") + turn.answer})
        self.turns.append(turn)
        if self.memory is not None:
            self.memory.add_message("user", turn.user_text or turn.question)
            self.memory.add_message("assistant", turn.answer)
            calls = [{"tool": n, "args": a, "summary": r.summary, "outcome": outcome(n, r)}
                     for n, a, r in turn.replayable]
            tools = [{"tool": c.name, "args": c.args, "summary": c.result.summary, "error": c.result.error}
                     for c in turn.calls]
            self.memory.record_question(turn.base_question, turn.answer, calls, turn.path(), turn.warnings, tools)


def _text(reply: AgentReply | None) -> str:
    return "\n".join(b.get("text", "") for b in (reply.content if reply else []) if b.get("type") == "text").strip()


def _has_output(content) -> bool:
    return any(b.get("type") == "tool_use" or (b.get("type") == "text" and (b.get("text") or "").strip())
               for b in content or [])
