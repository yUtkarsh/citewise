"""Session facts: the values the agent computes with. ``compute`` reads only these, so
every number in an answer traces back to a fact the user gave, a matched row, or a
typical value the user accepted (SPEC Step 6).

``FactStore`` is the interface; ``MemoryFacts`` keeps facts in process (tests, and the
CLI before the memory graph exists); ``app.memory`` adds the FalkorDB-backed store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

SOURCES = ("user", "matched_row", "assumed", "agent_selected")


@dataclass
class Fact:
    id: str  # a code-graph parameter id, or a descriptor (e.g. performance_level)
    value: float | str
    unit: str | None = None
    source: str = "user"
    label: str | None = None  # how the user or the code wrote it, e.g. "V/(bw d sqrt(f'c))"
    kind: str = "parameter"  # parameter (a node in the code graph) / descriptor

    def display(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        tag = {"user": "from you", "matched_row": "matched row", "assumed": "assumed",
               "agent_selected": "selected by agent"}.get(self.source, self.source)
        return f"{self.id} = {self.value}{unit} ({tag})"

    def number(self) -> float | None:
        try:
            return float(self.value)
        except (TypeError, ValueError):
            return None


@dataclass
class RowMatch:
    row_id: str
    table_id: str
    reason: str


class FactStore(Protocol):
    def all(self) -> dict[str, Fact]: ...
    def set(self, fact: Fact) -> Fact | None: ...  # returns the previous fact, if any
    def match_row(self, match: RowMatch) -> None: ...
    def matches(self) -> list[RowMatch]: ...


@dataclass
class MemoryFacts:
    facts: dict[str, Fact] = field(default_factory=dict)
    rows: dict[str, RowMatch] = field(default_factory=dict)

    def all(self) -> dict[str, Fact]:
        return dict(self.facts)

    def set(self, fact: Fact) -> Fact | None:
        previous = self.facts.get(fact.id)
        self.facts[fact.id] = fact
        return previous

    def match_row(self, match: RowMatch) -> None:
        self.rows[match.table_id] = match

    def matches(self) -> list[RowMatch]:
        return list(self.rows.values())


def facts_block(store: FactStore) -> str:
    """The "current project facts" block injected into every call."""
    facts = store.all()
    lines = [f.display() for f in facts.values()]
    lines += [f"{m.table_id}: matched {m.row_id} ({m.reason})" for m in store.matches()]
    body = "\n".join(f"- {line}" for line in lines) if lines else "- none yet"
    return "Current project facts (from the memory graph; use these values, ask for missing ones):\n" + body
