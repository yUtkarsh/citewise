"""Entity resolution (SPEC Step 3c.1), after the exact tier Pass 2 already applies (ids
and symbols matched against the notation dictionary at write time):

1. Similarity: pairs of Parameters whose embeddings (symbol, meaning, units, aliases) are
   very close, or close and sharing a symbol, alias or id spelling. Pairs with different
   units, or two notation entries scoped to different chapters, are never paired. Pairs
   are grouped into clusters of at most MAX_CLUSTER, closest first.
2. A cluster whose members all carry the same meaning word for word (and the same units)
   is merged without a call, unless two members' ids or symbols differ only by letter case
   (M_PCE / M_pCE, c_1 / C_1): case can carry meaning in a code's notation, so such a
   cluster goes to Claude with its meanings, units and notation scope.
3. Every other cluster gets one Claude call (``MODEL_GLOBAL``, default ``MODEL_REVIEW``):
   merge, keep separate, or flag. A merge with confidence >= 0.7 is applied (after the same
   guards); a low-confidence or flagged decision goes to the review queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

import numpy as np

from app.graph.merges import merge_parameter, units_conflict
from app.ingest.global_pass.calls import Ask, accepted
from app.ingest.global_pass.findings import Finding
from app.ingest.global_pass.schemas import CONFIDENCE_FLOOR, MergeDecision, check_merge
from app.ingest.global_pass.similarity import Vectors, load_vectors

HIGH = 0.95  # cosine: a pair on embeddings alone
SHARED = 0.85  # cosine: a pair that also shares a symbol, alias or id spelling
MAX_CLUSTER = 5
MIN_EXACT = 25  # characters of meaning for a word-for-word match to count


@dataclass
class Param:
    id: str
    symbol: str
    aliases: list[str]
    description: str
    units: str | None
    source: str | None
    scope: list
    page: int | None
    edges: int = 0
    used_in: list[str] = field(default_factory=list)

    def keys(self) -> set[str]:
        return {k for k in (_key(self.symbol), _key(self.id), *(_key(a) for a in self.aliases)) if k}

    def brief(self) -> str:
        parts = [f"id {self.id}", f"symbol {self.symbol}", f"meaning: {self.description or '(none)'}",
                 f"units: {self.units or '(none)'}", f"from: {self.source or '?'}"]
        if self.aliases:
            parts.append(f"aliases: {', '.join(self.aliases)}")
        if self.scope:
            parts.append(f"notation chapters: {', '.join(str(s) for s in self.scope)}")
        parts.append(f"used by {self.edges} edges" + (f" ({', '.join(self.used_in[:5])})" if self.used_in else ""))
        return "- " + " | ".join(parts)


def _key(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _meaning(text: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def load_params(graph) -> dict[str, Param]:
    rows = graph.ro_query("MATCH (p:Parameter) OPTIONAL MATCH (p)-[e]-(x) "
                          "RETURN p.id, p.symbol, p.aliases, p.description, p.units, p.source, p.scope_chapters, "
                          "p.page, count(e), collect(DISTINCT x.id)[0..6]").result_set
    return {r[0]: Param(r[0], r[1] or r[0], [a for a in (r[2] or []) if a], r[3] or "", r[4], r[5], r[6] or [], r[7],
                        r[8], [x for x in r[9] if x]) for r in rows}


def compatible(a: Param, b: Param) -> bool:
    """Never paired: different units; two notation entries scoped to different chapters;
    two table-header parameters (each is local to its table's columns)."""
    if units_conflict(a.units, b.units) or (a.source == "table" and b.source == "table"):
        return False
    if a.source == "notation" and b.source == "notation" and a.scope and b.scope and \
            not {str(s) for s in a.scope} & {str(s) for s in b.scope}:
        return False
    return True


def pairs(params: dict[str, Param], vectors: Vectors) -> list[tuple[float, str, str]]:
    ids = [i for i in vectors.ids if i in params]
    if len(ids) < 2:
        return []
    m = np.stack([vectors.vector(i) for i in ids])
    sims = m @ m.T
    out = []
    rows, cols = np.where(np.triu(sims, k=1) >= SHARED)
    for i, j in zip(rows.tolist(), cols.tolist()):
        a, b, s = params[ids[i]], params[ids[j]], float(sims[i, j])
        if s < HIGH and not (a.keys() & b.keys()):
            continue
        if compatible(a, b):
            out.append((s, a.id, b.id))
    return sorted(out, reverse=True)


def clusters(found: list[tuple[float, str, str]]) -> list[list[str]]:
    """Union-find over the pairs, closest first; a union that would pass MAX_CLUSTER is skipped."""
    parent: dict[str, str] = {}
    size: dict[str, int] = {}

    def root(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, a, b in found:
        ra, rb = root(a), root(b)
        if ra == rb or size.get(ra, 1) + size.get(rb, 1) > MAX_CLUSTER:
            continue
        parent[rb] = ra
        size[ra] = size.get(ra, 1) + size.get(rb, 1)
    groups: dict[str, list[str]] = {}
    for x in list(parent):
        groups.setdefault(root(x), []).append(x)
    return sorted(sorted(g) for g in groups.values() if len(g) > 1)


def survivor_of(ids: list[str], params: dict[str, Param]) -> str:
    return min(ids, key=lambda i: (params[i].source != "notation", -params[i].edges, len(i), i))


def _case_key(text: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text or "")


def differ_only_by_case(a: Param, b: Param) -> bool:
    """Ids or symbols equal apart from letter case (and punctuation), but not equal."""
    return any(_case_key(x) != _case_key(y) and _case_key(x).lower() == _case_key(y).lower()
               for x, y in ((a.id, b.id), (a.symbol, b.symbol)))


def is_exact(ids: list[str], params: dict[str, Param]) -> bool:
    """Merged without a call: one meaning word for word, compatible, and no two members
    told apart only by letter case."""
    meanings = {_meaning(params[i].description) for i in ids}
    members = [(params[a], params[b]) for a in ids for b in ids if a < b]
    return len(meanings) == 1 and len(next(iter(meanings))) >= MIN_EXACT and \
        all(compatible(x, y) and not differ_only_by_case(x, y) for x, y in members)


def prompt_text(ids: list[str], params: dict[str, Param]) -> str:
    return "Parameters that look alike:\n" + "\n".join(params[i].brief() for i in ids)


@dataclass
class Plan:
    clusters: list[list[str]]
    exact: list[list[str]]
    asks: list[Ask]
    params: dict[str, Param]


def plan(graph, model: str) -> Plan:
    params = load_params(graph)
    vectors = load_vectors(graph, "Parameter")
    groups = clusters(pairs(params, vectors))
    exact = [g for g in groups if is_exact(g, params)]
    asks = [Ask("|".join(g), "global_merge", prompt_text(g, params), MergeDecision,
                lambda out, ids=set(g): check_merge(out, ids), model)
            for g in groups if g not in exact]
    return Plan(groups, exact, asks, params)


def apply(graph, p: Plan, results: dict, model: str, dry_run: bool = False) -> tuple[list[dict], list[Finding]]:
    merges, findings = [], []

    def do(survivor: str, removed: list[str], reason: str, method: str, conf: float) -> None:
        for r in removed:
            merges.append({"survivor": survivor, "removed": r, "reason": reason, "method": method,
                           "confidence": conf} if dry_run else
                          {**merge_parameter(graph, survivor, r, reason, method, conf), "reason": reason,
                           "method": method, "confidence": conf})

    for g in p.exact:
        keep = survivor_of(g, p.params)
        do(keep, [i for i in g if i != keep], "same meaning word for word, same units", "exact_description", 1.0)
    for ask in p.asks:
        g = ask.key.split("|")
        page = min((p.params[i].page for i in g if p.params[i].page), default=None)
        subjects = [("Parameter", i) for i in g]
        out: MergeDecision | None = accepted(results.get(ask.key))
        if out is None:
            findings.append(Finding("merge", ask.key, f"possible duplicates {', '.join(g)}: no valid answer from Claude",
                                    subjects, page=page, confidence=0.0, method=f"claude:{model}"))
            continue
        keep = out.survivor
        removed = [i for i in out.merge_ids if out.decision == "merge" and compatible(p.params[keep], p.params[i])]
        dropped = [i for i in out.merge_ids if out.decision == "merge" and i not in removed]
        note = f"; kept apart by the unit/scope guard: {', '.join(dropped)}" if dropped else ""
        if out.decision == "merge" and removed and out.confidence >= CONFIDENCE_FLOOR:
            do(keep, removed, out.reason, "claude_adjudicated", out.confidence)
            if not dropped:
                continue
        if out.decision == "keep_separate" and out.confidence >= CONFIDENCE_FLOOR and not dropped:
            continue
        proposal = {"type": "merge", "survivor": keep, "removed": removed, "reason": out.reason,
                    "confidence": out.confidence} if out.decision == "merge" and removed \
            and out.confidence < CONFIDENCE_FLOOR else None
        findings.append(Finding(
            "merge", ask.key,
            f"possible duplicates {', '.join(g)}: Claude says {out.decision.replace('_', ' ')} "
            f"(confidence {out.confidence:.2f}){note}",
            subjects, detail=out.reason, page=page, proposal=proposal, confidence=out.confidence,
            method=f"claude:{model}"))
    return merges, findings
