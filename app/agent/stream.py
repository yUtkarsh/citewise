"""The chat page's agent hook (web/EVENTS.md, "Agent hook"): ``stream_chat`` yields
``{"event", "data"}`` dicts for ``POST /chat``.

One agent per chat session lives in this process. A session it does not hold (a new
tab, or the server restarted) is continued from the memory graph: its messages become
the history, its ASSUMES edges are the facts, and the last question's stored tool calls
are re-run so a what-if has a path to replay. Questions in one session run one at a time.

The agent loop is synchronous: each question runs in a worker thread that puts events on
a queue as tools start and finish; the generator yields them in order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import queue
import threading
from typing import Iterator

from app.agent.facts import Fact
from app.agent.guard import answer_labels
from app.agent.loop import Agent, ToolCall, Transport, Turn
from app.agent.results import ToolResult
from app.agent.tools import Toolbox
from app.graph.connection import get_db

DEFAULT_GRAPH = "code_fema356"
CHUNK = 160  # characters per answer_delta
TAGS = {"unreviewed", "inferred", "assumed"}


class SessionGraphError(RuntimeError):
    """A chat session is tied to one code graph; another graph needs a new chat."""

    def __init__(self, session_graph: str, asked: str):
        super().__init__(f"this chat uses {session_graph}; start a new chat to ask about {asked}")


@dataclass
class Chat:
    agent: Agent
    graph: str
    lock: threading.Lock = field(default_factory=threading.Lock)


_chats: dict[str, Chat] = {}
_chats_lock = threading.Lock()
_encoder = None
transport_for_tests: Transport | None = None  # tests replace the model; never set in the app


def _encode():
    global _encoder
    if _encoder is None:
        from app.agent.chat import LazyEncoder
        _encoder = LazyEncoder()
    return _encoder


def memory_graph_name() -> str:
    from app.memory.graph import MEMORY_GRAPH
    return os.getenv("MEMORY_GRAPH_NAME", MEMORY_GRAPH)


def max_cost() -> float:
    return float(os.getenv("CHAT_MAX_COST", "1.0"))


# ---- sessions ------------------------------------------------------------------------------

def resume(agent: Agent) -> None:
    """Rebuild an agent's history and last path from its memory session."""
    memory = agent.memory
    rows = memory.g.ro_query("MATCH (:Session {id: $s})-[:HAS_MESSAGE]->(m:Message) RETURN m.role, m.text "
                             "ORDER BY m.n", {"s": memory.session}).result_set
    memory._n = len(rows)
    agent.history = [{"role": role, "content": text} for role, text in rows if role in ("user", "assistant")]
    last = memory.last_question()
    if last:
        calls = [(c["tool"], c.get("args") or {}) for c in last["calls"]]
        turn = Turn(last["text"], answer=last.get("answer") or "", base_question=last["text"])
        turn.replayable = [(name, args, agent.toolbox._run(name, args)) for name, args in calls]
        agent.turns.append(turn)


def get_chat(session_id: str | None, graph: str | None) -> Chat:
    from app.agent.procedure import plan_procedure
    from app.memory.graph import MemoryGraph

    graph = graph or os.getenv("CODE_GRAPH") or DEFAULT_GRAPH
    with _chats_lock:
        chat = _chats.get(session_id) if session_id else None
        if chat is not None:
            if chat.graph != graph:
                raise SessionGraphError(chat.graph, graph)
            return chat
        db = get_db()
        if session_id and memory_graph_name() in db.list_graphs():
            stored = db.select_graph(memory_graph_name()).ro_query(
                "MATCH (s:Session {id: $s}) RETURN s.code", {"s": session_id}).result_set
            if stored and stored[0][0] and stored[0][0] != graph:
                raise SessionGraphError(stored[0][0], graph)
        code = db.select_graph(graph)
        docs = code.ro_query("MATCH (d:Document) RETURN d.title, d.edition LIMIT 1").result_set
        document = " / ".join(x for x in docs[0] if x) if docs else graph
        memory = MemoryGraph(db, graph, session_id=session_id, graph_name=memory_graph_name())
        toolbox = Toolbox(code, memory, _encode(), procedure=plan_procedure)
        agent = Agent(toolbox, transport=transport_for_tests, max_cost=max_cost(), document=document, memory=memory,
                      **({"model": "claude-sonnet-5-5"} if transport_for_tests else {}))
        resume(agent)
        chat = Chat(agent, graph)
        _chats[memory.session] = chat
        return chat


def forget_sessions() -> None:
    """Drop every in-process agent (as a server restart would)."""
    with _chats_lock:
        _chats.clear()


# ---- event payloads ------------------------------------------------------------------------------

def touched_event(call_id: str, result: ToolResult) -> dict:
    t = result.touched.as_dict()
    return {"tool_call_id": call_id, "summary": result.summary, "nodes": t["nodes"], "edges": t["edges"]}


def facts_event(agent: Agent) -> dict:
    facts = agent.toolbox.store.all()
    graph = agent.toolbox.graph
    params = {}
    if facts:
        rows = graph.ro_query("MATCH (p:Parameter) WHERE p.id IN $ids RETURN p.id, p.symbol, p.latex",
                              {"ids": list(facts)}).result_set
        params = {i: (sym, latex) for i, sym, latex in rows}
    out = []
    for f in facts.values():
        sym, latex = params.get(f.id, (None, None))
        item = {"param_id": f.id, "symbol": sym or f.label or f.id, "latex": latex, "value": f.value,
                "unit": f.unit, "source": f.source}
        if f.label and f.source == "matched_row":
            item["note"] = f.label
        out.append(item)
    return {"facts": out}


def _label_of(node_id: str, known: dict[str, str], graph) -> str:
    if node_id in known:
        return known[node_id]
    from app.agent.graph_tools import find_node
    found = find_node(graph, node_id)
    return found[0] if found else "Parameter"


def done_event(agent: Agent, turn: Turn) -> dict:
    graph = agent.toolbox.graph
    path = turn.path()
    path_ids = {n["id"] for n in path}
    results = [c.result for c in turn.calls if c.name in ("trace", "compute")]
    if turn.change_line:  # a what-if: the re-run path is the answer's path
        results += [r for _, _, r in turn.replayable]
    known = {}
    for r in [c.result for c in turn.calls] + [r for _, _, r in turn.replayable]:
        for label, nid in r.touched.nodes:
            known[nid] = label
    edges = list(dict.fromkeys(e for r in results for e in r.touched.edges
                               if e.split("|")[0] in path_ids and e.split("|")[2] in path_ids))
    calls = [(c.name, c.args, c.result) for c in turn.calls]
    labels = []
    for x in answer_labels(calls) + ([y for _, _, r in turn.replayable for y in r.labels] if turn.change_line else []):
        tag = x.label if x.label in TAGS else "flagged"
        if "|" in x.target:
            labels.append({"edge": x.target, "tag": tag, "note": x.reason})
        else:
            labels.append({"node": {"label": _label_of(x.target, known, graph), "id": x.target}, "tag": tag,
                           "note": x.reason})
    labels = list({(str(x.get("node") or x.get("edge")), x["tag"]): x for x in labels}.values())
    traced = [c.result for c in turn.calls if c.name == "trace"]
    citations = []
    for c in (traced[-1].data.get("citations", []) if traced else []):
        if c.get("label"):
            short = c["id"].replace("#r", " row ") if c["label"] == "Row" else c["id"]
            citations.append({"label": c["label"], "id": c["id"], "text": short, "page": c.get("page")})
    usage = {k: sum(u.get(k, 0) for u in turn.usages) for k in
             ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}
    cost = {"usd": round(turn.cost, 4),
            "input_tokens": usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"],
            "output_tokens": usage["output_tokens"], "tool_calls": len(turn.calls), "model": agent.model}
    return {"path": {"nodes": path, "edges": edges}, "labels": labels, "citations": citations, "cost": cost,
            "warnings": list(turn.warnings)}


def chunks(text: str) -> Iterator[str]:
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK)
        if end < len(text):
            space = text.rfind(" ", start, end)
            end = space + 1 if space > start else end
        yield text[start:end]
        start = end


# ---- the hook ------------------------------------------------------------------------------------

def _panel_facts(agent: Agent, facts: list[dict]) -> ToolResult:
    """The facts panel's full list, written as ASSUMES edges (labels kept)."""
    current = agent.toolbox.store.all()
    items = []
    for f in facts:
        old = current.get(f["param_id"])
        items.append({"id": f["param_id"], "value": f.get("value"), "unit": f.get("unit"),
                      "source": f.get("source") or "user", "label": old.label if old else None})
    return agent.toolbox.save_facts(items, trusted=True)


def _run(chat: Chat, question: str, facts: list[dict] | None, rerun: bool, emit) -> None:
    agent = chat.agent
    agent.on_tool_start = lambda call_id, name, args: emit("tool_call", {"id": call_id, "name": name, "args": args})

    def after(call: ToolCall) -> None:
        emit("touched", touched_event(call.id, call.result))
        if call.name in ("save_session_facts", "compute"):
            emit("facts", facts_event(agent))
    agent.on_tool = after
    turn = None
    if facts is not None:
        saved = _panel_facts(agent, facts)
        emit("facts", facts_event(agent))
        if rerun and "replay" in saved.data and agent.last_replay is not None:
            emit("tool_call", {"id": "whatif", "name": "whatif_replay", "args": {"changed": saved.data["changed"]}})
            emit("touched", touched_event("whatif", agent.last_replay))
            turn = agent.answer_replay(agent.last_replay)
    if turn is None:
        turn = agent.ask(question)
    if not turn.answer.strip():  # the loop never returns one; the page must never show a blank answer
        emit("error", {"message": "the agent returned no answer text", "code": "empty_answer"})
        return
    for piece in chunks(turn.answer):
        emit("answer_delta", {"text": piece})
    emit("facts", facts_event(agent))
    emit("answer_done", done_event(agent, turn))


def stream_chat(question: str, *, session_id: str | None, graph: str | None,
                facts: list[dict] | None, rerun: bool) -> Iterator[dict]:
    events: queue.Queue = queue.Queue()
    end = object()

    def emit(event: str, data: dict) -> None:
        events.put({"event": event, "data": data})

    def work() -> None:
        try:
            chat = get_chat(session_id, graph)
            with chat.lock:  # one question at a time per session
                _run(chat, question, facts, rerun, emit)
        except SessionGraphError as exc:
            emit("error", {"message": str(exc), "code": "session_graph"})
        except Exception as exc:  # the page shows it and stops
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            events.put(end)

    threading.Thread(target=work, daemon=True).start()
    while (item := events.get()) is not end:
        yield item
