"""Recent chats, read from the memory graph (SPEC Step 6): list a code's sessions and load
one session's messages and facts so the chat page can reopen it. Read-only."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from app.graph.connection import get_db

RECENT = 5
router = APIRouter()


def memory_graph_name() -> str:
    from app.memory.graph import MEMORY_GRAPH
    return os.getenv("MEMORY_GRAPH_NAME", MEMORY_GRAPH)


def _memory():
    """The memory graph, or None before any chat has created it."""
    db, name = get_db(), memory_graph_name()
    return db.select_graph(name) if name in db.list_graphs() else None


def _rows(graph, query: str, params: dict) -> list:
    return graph.ro_query(query, params, timeout=5000).result_set


@router.get("/codes/{code}/sessions")
def recent_sessions(code: str, limit: int = RECENT) -> dict:
    g = _memory()
    if g is None:
        return {"sessions": []}
    rows = _rows(g, (
        "MATCH (s:Session {code: $code}) "
        "OPTIONAL MATCH (s)-[:ASKED]->(q:Question) "
        "WITH s, count(q) AS questions, max(q.time) AS last "
        "OPTIONAL MATCH (s)-[:ASKED]->(f:Question {n: 1}) "
        "RETURN s.id, s.started, coalesce(last, s.started) AS t, questions, f.text "
        "ORDER BY t DESC LIMIT $limit"), {"code": code, "limit": max(1, min(limit, 50))})
    return {"sessions": [{"id": sid, "started": started, "last": last, "questions": n, "first_question": first}
                         for sid, started, last, n, first in rows if n]}


@router.get("/sessions/{session_id}")
def session(session_id: str) -> dict:
    g = _memory()
    found = _rows(g, "MATCH (s:Session {id: $s}) RETURN s.code", {"s": session_id}) if g is not None else []
    if not found:
        raise HTTPException(404, "no such chat session")
    messages = [{"n": n, "role": role, "text": text} for n, role, text in _rows(
        g, "MATCH (:Session {id: $s})-[:HAS_MESSAGE]->(m:Message) RETURN m.n, m.role, m.text ORDER BY m.n",
        {"s": session_id})]
    if not messages:  # sessions that only stored question traces
        for text, answer in _rows(g, "MATCH (:Session {id: $s})-[:ASKED]->(q:Question) RETURN q.text, q.answer "
                                     "ORDER BY q.n", {"s": session_id}):
            messages += [{"role": "user", "text": text}, {"role": "assistant", "text": answer or ""}]
    facts = [{"param_id": pid, "symbol": label or pid, "value": value, "unit": unit, "source": source or "user"}
             for pid, value, unit, source, label in _rows(
                 g, "MATCH (:Session {id: $s})-[a:ASSUMES]->(n) RETURN n.id, a.value, a.unit, a.source, a.label "
                    "ORDER BY a.updated", {"s": session_id})]
    matches = {r: reason for r, reason in _rows(
        g, "MATCH (:Session {id: $s})-[m:MATCHED]->(r:Row) RETURN r.id, m.reason", {"s": session_id})}
    last = next((m["text"] for m in reversed(messages) if m["role"] == "user"), None)
    return {"id": session_id, "graph": found[0][0], "messages": messages, "facts": facts,
            "matched_rows": [{"row": r, "reason": why} for r, why in matches.items()], "last_question": last}
