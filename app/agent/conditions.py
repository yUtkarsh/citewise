"""Check a condition written in plain words (a table row's or a clause's) against the
session facts. Deterministic and conservative: only a comparison whose both sides
resolve to numbers is decided; everything else (categories, descriptions) is
``unknown`` and left to Claude's row match, which must give its reason.

A side resolves when it is a number, when it names a fact (by id or by the label the
fact was saved under, compared with spacing, case and prime marks ignored), or when it
parses as an expression whose names are all facts.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from app.agent.facts import Fact
from app.ingest.pass2.validate import numbers, parse_formula

COMPARATOR = re.compile(r"(<=|>=|≤|≥|<|>|=)")
OPS = {"<=": "≤", ">=": "≥"}
TRUE, FALSE, UNKNOWN = "true", "false", "unknown"


@dataclass
class Evaluation:
    text: str
    status: str  # true / false / unknown
    lhs: str = ""
    op: str = ""
    rhs: str = ""
    lhs_value: float | None = None
    rhs_value: float | None = None
    why: str = ""

    def as_dict(self) -> dict:
        out = {"condition": self.text, "status": self.status}
        if self.op:
            out |= {"lhs": self.lhs, "op": self.op, "rhs": self.rhs}
        if self.lhs_value is not None:
            out["lhs_value"] = self.lhs_value
        if self.why:
            out["why"] = self.why
        return out

    @property
    def key(self) -> str:
        """The compared quantity, normalised: rows that differ only in a threshold on the
        same quantity share it (used for interpolation)."""
        return norm(self.lhs)


def norm(text: str) -> str:
    text = (text or "").lower().replace("′", "'").replace("’", "'").replace("√", "sqrt")
    return re.sub(r"[\s()*·×]+", "", text)


def split(text: str) -> tuple[str, str, str] | None:
    parts = COMPARATOR.split(text, maxsplit=1)
    if len(parts) != 3:
        return None
    lhs, op, rhs = (p.strip() for p in parts)
    return lhs, OPS.get(op, op), rhs


def number_of(side: str) -> float | None:
    """The side's value when it is just a number (unit words allowed: '0.5 in.')."""
    found = numbers(side)
    letters = re.sub(r"[^A-Za-z]", "", side)
    return found[0] if len(found) == 1 and len(letters) <= 3 and not re.search(r"[*/^]", side) else None


def resolve(side: str, facts: dict[str, Fact]) -> tuple[float | None, str]:
    if not side:
        return None, "empty"
    value = number_of(side)
    if value is not None:
        return value, "number"
    key = norm(side)
    for fact in facts.values():
        if fact.number() is not None and key in {norm(fact.id), norm(fact.label or "")}:
            return fact.number(), f"fact {fact.id}"
    try:
        expr = parse_formula(side)
    except ValueError:
        return None, "not a number or a known fact"
    values = {}
    for sym in expr.free_symbols:
        fact = facts.get(sym.name)
        if fact is None or fact.number() is None:
            return None, f"no fact for {sym.name}"
        values[sym] = fact.number()
    try:
        return float(expr.subs(values)), "expression of facts"
    except (TypeError, ValueError):
        return None, "could not evaluate"


def evaluate(text: str, facts: dict[str, Fact]) -> Evaluation:
    parts = split(text)
    if parts is None:
        return Evaluation(text, UNKNOWN, why="a description, not a comparison: matched by Claude with a reason")
    lhs, op, rhs = parts
    left, left_why = resolve(lhs, facts)
    right, right_why = resolve(rhs, facts)
    ev = Evaluation(text, UNKNOWN, lhs, op, rhs, left, right)
    if left is None or right is None:
        ev.why = f"left side: {left_why}; right side: {right_why}"
        return ev
    ok = {"≤": left <= right, "≥": left >= right, "<": left < right, ">": left > right,
          "=": abs(left - right) <= 1e-9 * max(1.0, abs(right))}[op]
    ev.status = TRUE if ok else FALSE
    return ev


def evaluate_all(texts: list[str], facts: dict[str, Fact]) -> list[Evaluation]:
    return [evaluate(t, facts) for t in texts]
