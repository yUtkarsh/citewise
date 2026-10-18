"""Pydantic outputs of the global pass's Claude calls, and their deterministic checks."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.ingest.global_pass.calls import bad
from app.ingest.pass2.validate import Issue

MAX_REASON = 300
CONFIDENCE_FLOOR = 0.7  # stored at or above; below goes to the review queue


class CommentaryChoice(BaseModel):
    choice: str
    confidence: float = Field(ge=0, le=1)
    reason: str


class InferredLink(BaseModel):
    clause_id: str
    edge_type: Literal["uses_value_from", "requires"]
    confidence: float = Field(ge=0, le=1)
    reason: str


class InferredLinks(BaseModel):
    links: list[InferredLink]


class MergeDecision(BaseModel):
    decision: Literal["merge", "keep_separate", "flag"]
    survivor: str
    merge_ids: list[str]
    confidence: float = Field(ge=0, le=1)
    reason: str


def _reason(text: str) -> list[Issue]:
    if not text.strip():
        return [bad("reason is empty: give one short line")]
    if len(text) > MAX_REASON:
        return [bad(f"reason is longer than {MAX_REASON} characters: one short line")]
    return []


def check_commentary(out: CommentaryChoice, candidates: set[str]) -> list[Issue]:
    issues = _reason(out.reason)
    if out.choice != "none" and out.choice not in candidates:
        issues.append(bad(f"choice {out.choice!r} is not a candidate id; use one of {sorted(candidates)} or none"))
    return issues


def check_links(out: InferredLinks, candidates: set[str]) -> list[Issue]:
    issues = []
    if len(out.links) > 3:
        issues.append(bad("at most three links"))
    for link in out.links:
        if link.clause_id not in candidates:
            issues.append(bad(f"clause_id {link.clause_id!r} is not a candidate id"))
        issues += _reason(link.reason)
    if len({link.clause_id for link in out.links}) != len(out.links):
        issues.append(bad("each clause at most once"))
    return issues


def check_merge(out: MergeDecision, members: set[str]) -> list[Issue]:
    issues = _reason(out.reason)
    if out.decision == "merge":
        if out.survivor not in members:
            issues.append(bad(f"survivor {out.survivor!r} is not in the cluster"))
        unknown = [m for m in out.merge_ids if m not in members]
        if unknown:
            issues.append(bad(f"merge_ids not in the cluster: {unknown}"))
        if not out.merge_ids or out.survivor in out.merge_ids:
            issues.append(bad("merge_ids must list the other ids to merge, without the survivor"))
    return issues
