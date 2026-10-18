"""Review-queue findings of the global pass (SPEC Step 3c).

A finding is a ``Finding`` node: one hit of a graph-wide check, or one proposal Claude
made with too little confidence to store (an inferred edge, a commentary link, a merge).
Its id is ``<kind>:<key>``, so a re-run MERGEs onto the same node; findings of a kind
that the new run no longer produces are removed unless a reviewer already decided them.

    (:Finding {id, kind, severity, reason, detail, subjects, subject_labels, proposal,
               page, source_quote, confidence, extraction_method, review_status, ...})
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from app.graph.schema import GRAPH_SCHEMA_VERSION, QUOTE_CHARS

GLOBAL_SCHEMA_VERSION = "global-v1"
KINDS = ("undefined_parameter", "missing_reference", "dependency_cycle",
         "commentary_link", "inferred_edge", "merge")


@dataclass
class Finding:
    kind: str
    key: str
    reason: str  # one line: why it is in the queue
    subjects: list[tuple[str, str]]  # (label, id) of the nodes it is about
    detail: str = ""
    page: int | None = None
    severity: str = "medium"
    proposal: dict | None = None  # what approving it would apply (an edge or a merge)
    confidence: float = 1.0
    method: str = "cypher"
    quote: str = ""

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.key}"

    def row(self) -> dict:
        return {"id": self.id, "kind": self.kind, "severity": self.severity, "reason": self.reason,
                "detail": self.detail, "subjects": [i for _, i in self.subjects],
                "subject_labels": [label for label, _ in self.subjects],
                "proposal": json.dumps(self.proposal, ensure_ascii=False) if self.proposal else None,
                "page": self.page, "bbox": None, "source_quote": (self.quote or self.reason)[:QUOTE_CHARS],
                "confidence": round(float(self.confidence), 3), "extraction_method": self.method,
                "schema_version": GRAPH_SCHEMA_VERSION, "global_schema": GLOBAL_SCHEMA_VERSION}


@dataclass
class FindingSet:
    """Findings of some kinds from one run; ``write`` replaces those kinds' open findings."""
    kinds: set[str]
    items: dict[str, Finding] = field(default_factory=dict)

    def add(self, f: Finding) -> None:
        assert f.kind in self.kinds, f.kind
        self.items[f.id] = f

    def count(self, kind: str) -> int:
        return sum(1 for f in self.items.values() if f.kind == kind)


def write_findings(graph, found: FindingSet) -> dict[str, int]:
    """MERGE every finding (a new one starts unreviewed; a decided one keeps its decision)
    and remove open findings of these kinds that this run did not produce."""
    rows = [f.row() for f in found.items.values()]
    if rows:
        graph.query("UNWIND $rows AS r MERGE (f:Finding {id: r.id}) "
                    "ON CREATE SET f.review_status = 'unreviewed', f.status = 'open' SET f += r", {"rows": rows})
    graph.query("MATCH (f:Finding) WHERE f.kind IN $kinds AND NOT f.id IN $ids "
                "AND coalesce(f.review_status, 'unreviewed') = 'unreviewed' DELETE f",
                {"kinds": sorted(found.kinds), "ids": list(found.items)})
    return {kind: found.count(kind) for kind in sorted(found.kinds)}


def open_findings(graph) -> list[dict]:
    rows = graph.ro_query("MATCH (f:Finding) RETURN properties(f)").result_set
    return [dict(r[0]) for r in rows]
