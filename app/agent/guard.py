"""Checks on a finished answer, applied in code after Claude writes it:

- every label carried by the answer path (unreviewed, inferred, assumed, aligned by order)
  must appear in the answer; a missing one is added in a "Labels" footer;
- every number in the answer must appear in a tool result, a fact or the user's words;
  the others are returned as warnings (printed, never silently dropped).
"""
from __future__ import annotations

import re

from app.agent.results import Label, ToolResult
from app.ingest.pass2.validate import numbers

ANSWER_TOOLS = {"compute", "calculate", "get_clause", "trace", "applicable_clauses", "plan_procedure",
                "save_session_facts"}
ID_LIKE = re.compile(r"\b[A-Z]?\d+(?:[.-]\d+)+\b")  # clause, table and equation numbers


PATH_TOOLS = {"trace", "compute", "calculate", "save_session_facts"}  # once trace names the answer path


def answer_labels(calls: list[tuple[str, dict, ToolResult]]) -> list[Label]:
    """Labels the answer must carry: those of the answer path when trace was called
    (trace, compute and facts), else those of every tool that reads answer content."""
    tools = PATH_TOOLS if any(name == "trace" for name, _, _ in calls) else ANSWER_TOOLS
    seen: dict[tuple[str, str], Label] = {}
    for name, _, result in calls:
        if name in tools:
            for label in result.labels:
                seen.setdefault((label.target, label.label), label)
    return list(seen.values())


def _mentioned(target: str, answer: str) -> bool:
    candidates = {target, target.split("#")[0], target.split("|")[0]}
    if "#r" in target:
        table, index = target.rsplit("#r", 1)
        candidates |= {f"row {index}", table}
    return any(c and c in answer for c in candidates)


def missing_labels(answer: str, labels: list[Label]) -> list[Label]:
    low = answer.lower()
    return [x for x in labels if not (x.label.lower() in low and _mentioned(x.target, answer))]


def with_label_footer(answer: str, labels: list[Label]) -> tuple[str, list[Label]]:
    missing = missing_labels(answer, labels)
    if not missing:
        return answer, []
    lines = [f"- {x.target}: {x.label} ({x.reason})" for x in missing]
    return answer.rstrip() + "\n\nLabels (added by the checker):\n" + "\n".join(lines), missing


def unsupported_numbers(answer: str, sources: list[str]) -> list[str]:
    """Numbers in the answer that no tool result, fact or user message contains."""
    known = set()
    for text in sources:
        known |= {round(n, 9) for n in numbers(text)}
    out = []
    for token in NUMBER_TOKEN.findall(ID_LIKE.sub(" ", answer)):
        value = numbers(token)
        if value and round(value[0], 9) not in known and round(abs(value[0]), 9) not in known:
            out.append(token)
    return list(dict.fromkeys(out))


NUMBER_TOKEN = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")
