"""Approving a global-pass finding: a proposal Claude was not confident enough to store
(an inferred edge, a commentary link, a merge) is applied once a reviewer approves it.
A check's hit has no proposal; approving it only records the decision."""
from __future__ import annotations

import json

from app.graph.merges import merge_parameter
from app.ingest.global_pass.edges import write_edges


def apply_proposal(graph, finding: dict) -> dict:
    proposal = finding.get("proposal")
    proposal = json.loads(proposal) if isinstance(proposal, str) else proposal
    if not proposal:
        return {"applied": False, "why": "nothing to apply: the finding is a check's hit"}
    if proposal["type"] == "edges":
        edges = [{**e, "props": {**e["props"], "review_status": "approved"}} for e in proposal["edges"]]
        return {"applied": True, "edges": write_edges(graph, edges)}
    if proposal["type"] == "merge":
        results = [merge_parameter(graph, proposal["survivor"], removed, proposal["reason"], "reviewed",
                                   proposal.get("confidence", 0.0), finding.get("id"))
                   for removed in proposal["removed"]]
        return {"applied": any(r["merged"] for r in results), "merges": results}
    return {"applied": False, "why": f"unknown proposal type {proposal['type']!r}"}
