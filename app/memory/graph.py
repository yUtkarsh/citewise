"""The memory graph (SPEC Step 6): one FalkorDB graph, separate from every code graph.

    (:Session {id, code})-[:HAS_MESSAGE]->(:Message {id, n, role, text})
    (:Session)-[:ASSUMES {value, unit, source, label, kind}]->(:Parameter|:Descriptor {key, id, graph})
    (:Session)-[:MATCHED {reason}]->(:Row {key, id, graph, table})
    (:Session)-[:ASKED]->(:Question {id, n, text, answer, calls})-[:ANSWERED_VIA {order}]->(stub)

Stub nodes point to code-graph nodes by id (``key`` = graph + '|' + id); the code graph
itself is never written. ``MemoryGraph`` implements the agent's ``FactStore``.
"""
from __future__ import annotations

import json
import time
import uuid

from app.agent.facts import Fact, RowMatch

MEMORY_GRAPH = "memory"
STUB_LABELS = {"Parameter", "Descriptor", "Row", "Clause", "Equation", "Table", "Term", "Figure", "Footnote"}


def _stub_label(label: str) -> str:
    return label if label in STUB_LABELS else "Node"


class MemoryGraph:
    def __init__(self, db, code_graph: str, session_id: str | None = None, graph_name: str = MEMORY_GRAPH):
        self.g = db.select_graph(graph_name)
        self.code = code_graph
        self.session = session_id or f"s-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.g.query("MERGE (s:Session {id: $id}) ON CREATE SET s.code = $code, s.started = $t",
                     {"id": self.session, "code": code_graph, "t": time.time()})
        self._n = 0

    def _key(self, node_id: str) -> str:
        return f"{self.code}|{node_id}"

    # ---- FactStore ------------------------------------------------------------------------

    def all(self) -> dict[str, Fact]:
        rows = self.g.ro_query("MATCH (:Session {id: $s})-[a:ASSUMES]->(n) RETURN n.id, a.value, a.unit, a.source, "
                               "a.label, a.kind ORDER BY a.updated", {"s": self.session}).result_set
        return {i: Fact(i, v, u, src, lab, kind or "parameter") for i, v, u, src, lab, kind in rows}

    def set(self, fact: Fact) -> Fact | None:
        previous = self.all().get(fact.id)
        label = "Parameter" if fact.kind == "parameter" else "Descriptor"
        self.g.query(f"MATCH (s:Session {{id: $s}}) MERGE (n:{label} {{key: $key}}) "
                     "ON CREATE SET n.id = $id, n.graph = $graph "
                     "MERGE (s)-[a:ASSUMES]->(n) SET a.value = $value, a.unit = $unit, a.source = $source, "
                     "a.label = $label, a.kind = $kind, a.updated = $t",
                     {"s": self.session, "key": self._key(fact.id), "id": fact.id, "graph": self.code,
                      "value": fact.value, "unit": fact.unit, "source": fact.source, "label": fact.label,
                      "kind": fact.kind, "t": time.time()})
        return previous

    def match_row(self, match: RowMatch) -> None:
        self.g.query("MATCH (s:Session {id: $s})-[m:MATCHED]->(r:Row) WHERE r.table = $table DELETE m",
                     {"s": self.session, "table": match.table_id})
        self.g.query("MATCH (s:Session {id: $s}) MERGE (r:Row {key: $key}) "
                     "ON CREATE SET r.id = $id, r.graph = $graph, r.table = $table "
                     "MERGE (s)-[m:MATCHED]->(r) SET m.reason = $reason",
                     {"s": self.session, "key": self._key(match.row_id), "id": match.row_id, "graph": self.code,
                      "table": match.table_id, "reason": match.reason})

    def matches(self) -> list[RowMatch]:
        rows = self.g.ro_query("MATCH (:Session {id: $s})-[m:MATCHED]->(r:Row) RETURN r.id, r.table, m.reason",
                               {"s": self.session}).result_set
        return [RowMatch(i, t, reason or "") for i, t, reason in rows]

    # ---- history and answer traces ------------------------------------------------------------

    def add_message(self, role: str, text: str) -> None:
        self._n += 1
        self.g.query("MATCH (s:Session {id: $s}) CREATE (s)-[:HAS_MESSAGE]->(:Message {id: $id, n: $n, role: $role, "
                     "text: $text, time: $t})",
                     {"s": self.session, "id": f"{self.session}#m{self._n}", "n": self._n, "role": role,
                      "text": text, "t": time.time()})

    def record_question(self, question: str, answer: str, calls: list[dict], path: list[dict]) -> str:
        n = self.g.ro_query("MATCH (:Session {id: $s})-[:ASKED]->(q) RETURN count(q)",
                            {"s": self.session}).result_set[0][0] + 1
        qid = f"{self.session}#q{n}"
        self.g.query("MATCH (s:Session {id: $s}) CREATE (s)-[:ASKED]->(:Question {id: $id, n: $n, text: $q, "
                     "answer: $a, calls: $calls, time: $t})",
                     {"s": self.session, "id": qid, "n": n, "q": question, "a": answer,
                      "calls": json.dumps(calls, ensure_ascii=False, default=str), "t": time.time()})
        for order, node in enumerate(path):
            label = _stub_label(node["label"])
            self.g.query(f"MATCH (q:Question {{id: $q}}) MERGE (x:{label} {{key: $key}}) "
                         "ON CREATE SET x.id = $id, x.graph = $graph "
                         "CREATE (q)-[:ANSWERED_VIA {order: $order}]->(x)",
                         {"q": qid, "key": self._key(node["id"]), "id": node["id"], "graph": self.code,
                          "order": order})
        return qid

    def last_question(self) -> dict | None:
        rows = self.g.ro_query("MATCH (:Session {id: $s})-[:ASKED]->(q:Question) RETURN q.text, q.calls, q.answer "
                               "ORDER BY q.n DESC LIMIT 1", {"s": self.session}).result_set
        if not rows:
            return None
        text, calls, answer = rows[0]
        return {"text": text, "calls": json.loads(calls or "[]"), "answer": answer}

    def answer_path(self, question_id: str) -> list[str]:
        rows = self.g.ro_query("MATCH (:Question {id: $q})-[v:ANSWERED_VIA]->(x) RETURN x.id ORDER BY v.order",
                               {"q": question_id}).result_set
        return [r[0] for r in rows]
