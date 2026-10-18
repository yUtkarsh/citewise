"""Unmatched commentary (SPEC Step 3c.3): commentary sections that no provision of the
same number explains are linked to a provision by Claude, as inferred edges.

Candidates are found in code: the nearest provisions up the commentary's number (its
parents by number), the provisions it references, then the provisions most similar by
embedding; the top five go to one call (``MODEL_CLAUSES``) that picks one or "none".
A pick with confidence >= 0.7 is stored as ``COMMENTARY_ON {method: inferred}``; the
rest go to the review queue (medium).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ingest.global_pass.calls import Ask, accepted
from app.ingest.global_pass.edges import INFERRED, SOURCE, inferred_edge
from app.ingest.global_pass.findings import Finding
from app.ingest.global_pass.schemas import CONFIDENCE_FLOOR, CommentaryChoice, check_commentary
from app.ingest.global_pass.similarity import Vectors

TOP = 5
PARENTS = 2  # provisions up the number
TEXT_CHARS = 1500
CANDIDATE_CHARS = 350


@dataclass
class ClauseInfo:
    id: str
    number: str | None
    title: str
    text: str
    page: int | None
    stream: str | None

    def brief(self, chars: int = CANDIDATE_CHARS) -> str:
        return f"{self.id} {self.title}: {' '.join(self.text.split())[:chars]}".strip()


def load_clauses(graph) -> dict[str, ClauseInfo]:
    rows = graph.ro_query("MATCH (c:Clause) RETURN c.id, c.number, c.title, coalesce(c.text_corrected, c.text), "
                          "c.page, c.stream").result_set
    return {r[0]: ClauseInfo(r[0], r[1], r[2] or "", r[3] or "", r[4], r[5]) for r in rows}


def is_provision(c: ClauseInfo) -> bool:
    return c.stream != "commentary"


def unmatched_commentary(graph, clauses: dict[str, ClauseInfo]) -> list[ClauseInfo]:
    """Commentary with no COMMENTARY_ON edge, not counting this pass's own unapproved edges."""
    linked = {r[0] for r in graph.ro_query(
        "MATCH (c:Clause)-[e:COMMENTARY_ON]->() WHERE NOT (e.method = $m AND e.source = $s "
        "AND coalesce(e.review_status, '') <> 'approved') RETURN DISTINCT c.id", {"m": INFERRED, "s": SOURCE}).result_set}
    return sorted((c for c in clauses.values() if c.stream == "commentary" and c.id not in linked),
                  key=lambda c: (c.page or 0, c.id))


def candidates(c: ClauseInfo, clauses: dict[str, ClauseInfo], by_number: dict[str, str], refs: list[str],
               vectors: Vectors, provisions: set[str]) -> list[tuple[str, str]]:
    """Up to TOP (provision id, why) in order: parents by number, references, similar wording."""
    out: dict[str, str] = {}
    parts = (c.number or "").split(".")
    for n in range(len(parts), 0, -1):
        pid = by_number.get(".".join(parts[:n]))
        if pid and pid in provisions and len([w for w in out.values() if w.startswith("parent")]) < PARENTS:
            out.setdefault(pid, "same number" if n == len(parts) else "parent by number")
    for rid in refs:
        if rid in provisions and len(out) < TOP:
            out.setdefault(rid, "the commentary refers to it")
    vector = vectors.vector(c.id)
    if vector is not None:
        for pid, score in vectors.top(vector, TOP * 3, provisions):
            if len(out) >= TOP:
                break
            out.setdefault(pid, f"similar wording ({score:.2f})")
    return list(out.items())[:TOP]


def prompt_text(c: ClauseInfo, cands: list[tuple[str, str]], clauses: dict[str, ClauseInfo]) -> str:
    lines = [f"Commentary {c.id}: {c.title}", " ".join(c.text.split())[:TEXT_CHARS] or "(no text)", "",
             "Candidate provisions:"]
    lines += [f"- id: {pid} | why: {why} | {clauses[pid].brief()}" for pid, why in cands]
    return "\n".join(lines)


def build_asks(graph, clauses: dict[str, ClauseInfo], vectors: Vectors, model: str) -> tuple[list[Ask], dict]:
    provisions = {i for i, c in clauses.items() if is_provision(c)}
    by_number = {c.number: c.id for c in clauses.values() if c.number and is_provision(c)}
    refs: dict[str, list[str]] = {}
    for src, dst in graph.ro_query("MATCH (a:Clause)-[:REFERENCES]->(b:Clause) RETURN a.id, b.id").result_set:
        refs.setdefault(src, []).append(dst)
    asks, cands = [], {}
    for c in unmatched_commentary(graph, clauses):
        found = candidates(c, clauses, by_number, list(dict.fromkeys(refs.get(c.id, []))), vectors, provisions)
        cands[c.id] = found
        if not found:
            continue
        ids = {pid for pid, _ in found}
        asks.append(Ask(c.id, "global_commentary", prompt_text(c, found, clauses), CommentaryChoice,
                        lambda out, ids=ids: check_commentary(out, ids), model))
    return asks, cands


def decide(results: dict, cands: dict[str, list[tuple[str, str]]], clauses: dict[str, ClauseInfo],
           model: str) -> tuple[list[dict], list[Finding]]:
    edges, findings = [], []
    for cid, found in cands.items():
        c = clauses[cid]
        out: CommentaryChoice | None = accepted(results.get(cid)) if found else None
        if out is not None and out.choice != "none" and out.confidence >= CONFIDENCE_FLOOR:
            edges.append(inferred_edge(("Clause", cid), "COMMENTARY_ON", ("Clause", out.choice), out.confidence,
                                       out.reason, model))
            continue
        if not found:
            reason, proposal, conf = f"commentary {cid} has no candidate provision", None, 0.0
        elif out is None:
            reason, proposal, conf = f"commentary {cid}: no valid answer from Claude", None, 0.0
        elif out.choice == "none":
            reason, proposal, conf = f"commentary {cid}: Claude found no provision it explains ({out.reason})", None, \
                out.confidence
        else:
            edge = inferred_edge(("Clause", cid), "COMMENTARY_ON", ("Clause", out.choice), out.confidence,
                                 out.reason, model)
            reason, proposal, conf = (f"commentary {cid} probably explains {out.choice} "
                                      f"(confidence {out.confidence:.2f})"), {"type": "edges", "edges": [edge]}, \
                out.confidence
        findings.append(Finding("commentary_link", cid, reason, [("Clause", cid)] + [("Clause", p) for p, _ in found],
                                detail="candidates: " + "; ".join(f"{p} ({w})" for p, w in found),
                                page=c.page, proposal=proposal, confidence=conf, method=f"claude:{model}",
                                quote=c.title))
    return edges, findings
