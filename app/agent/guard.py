"""Checks on a finished answer, applied in code after Claude writes it:

- every label carried by the answer path (unreviewed, inferred, assumed, aligned by order)
  must appear in the answer; a missing one is added in a "Labels" footer;
- every number in the answer must appear in a tool result, a fact or the user's words,
  exactly or rounded to the precision shown when that keeps at least three significant
  digits; the others are returned as warnings (printed, never silently dropped).
"""
from __future__ import annotations

import re

from app.agent.results import Label, ToolResult
from app.ingest.pass2.validate import numbers

ANSWER_TOOLS = {"compute", "calculate", "get_clause", "trace", "applicable_clauses", "plan_procedure",
                "save_session_facts"}
# Clause, table, equation and figure numbers, not values: after a word that names one (or
# '[[Clause:1.6]]', '§1.6'), with two or more dots (3.3.1), with a hyphen (3-10), or a page.
ID_LIKE = re.compile(r"(?:\b(?:Sections?|Clauses?|Tables?|Equations?|Eqs?\.|Figures?|Chapters?|Rows?)\s*|[§:])"
                     r"\(?[A-Z]?\d+(?:[.-]\d+)*\)?(?:\s*(?:,|and|or|through|to)\s*[A-Z]?\d+(?:[.-]\d+)*)*"
                     r"|\b[A-Z]?\d+(?:\.\d+){2,}\b|\b[A-Z]?\d+-\d+(?:\.\d+)?\b|\bp\.\s*\d+")


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
    """Numbers in the answer that no tool result, fact or user message contains, either
    exactly or rounded to the precision the answer shows (see ``rounded_from``)."""
    known = set()
    for text in sources:
        known |= {round(n, 9) for n in numbers(text)}
    out = []
    for token in NUMBER_TOKEN.findall(ID_LIKE.sub(" ", answer)):
        value = numbers(token)
        if not value or round(value[0], 9) in known or round(abs(value[0]), 9) in known:
            continue
        if not rounded_from(token, value[0], known):
            out.append(token)
    return list(dict.fromkeys(out))


MIN_ROUNDED_DIGITS = 3  # a rounded number must keep this many significant digits


def significant_digits(token: str) -> int:
    """Significant digits as written: '1,967' -> 4, '1.15' -> 3, '1.2' -> 2, '2,000' -> 1."""
    digits = token.replace(",", "").lstrip("-")
    if "." in digits:
        whole, frac = digits.split(".", 1)
        return len((whole + frac).lstrip("0"))
    return len(digits.strip("0"))


def rounded_from(token: str, value: float, known: set[float]) -> bool:
    """True when ``value`` is a known number rounded to the decimals the token shows, and
    the token keeps at least MIN_ROUNDED_DIGITS significant digits: '1,967' from 1967.02
    passes; '1.2' from 1.16 does not (rounding to two digits can hide a wrong value)."""
    if significant_digits(token) < MIN_ROUNDED_DIGITS:
        return False
    decimals = len(token.split(".", 1)[1]) if "." in token else 0
    tolerance = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(abs(k) - abs(value)) <= tolerance for k in known)


NUMBER_TOKEN = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")
