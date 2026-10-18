"""Session facts: the values the agent computes with. ``compute`` reads only these, so
every number in an answer traces back to a fact the user gave, a matched row, a value
stated outright in a clause's text (``from_clause``, with the clause id), or a typical
value the user accepted (SPEC Step 6).

``FactStore`` is the interface; ``MemoryFacts`` keeps facts in process (tests, and the
CLI before the memory graph exists); ``app.memory`` adds the FalkorDB-backed store.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

SOURCES = ("user", "matched_row", "assumed", "agent_selected", "from_clause")


@dataclass
class Fact:
    id: str  # a code-graph parameter id, or a descriptor (e.g. performance_level)
    value: float | str
    unit: str | None = None
    source: str = "user"
    label: str | None = None  # how the user or the code wrote it, e.g. "V/(bw d sqrt(f'c))"
    kind: str = "parameter"  # parameter (a node in the code graph) / descriptor
    clause: str | None = None  # from_clause: the clause whose text states the value

    @property
    def tag(self) -> str:
        if self.source == "from_clause":
            return f"stated in §{self.clause}"
        return {"user": "from you", "matched_row": "matched row", "assumed": "assumed",
                "agent_selected": "selected by agent"}.get(self.source, self.source)

    def display(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        return f"{self.id} = {self.value}{unit} ({self.tag})"

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


def resolve_facts(facts: dict[str, Fact], merged: dict[str, str]) -> dict[str, Fact]:
    """Facts keyed by the surviving id of any parameter the global pass merged (a fact
    saved under the old id still applies); a fact saved under the survivor wins."""
    out = {i: f for i, f in facts.items() if i not in merged}
    for i, f in facts.items():
        if i in merged and f.kind == "parameter":
            out.setdefault(merged[i], replace(f, id=merged[i]))
        elif i in merged:
            out[i] = f
    return out


@dataclass
class ResolvedStore:
    """A FactStore whose ``all`` resolves merged ids; writes go to the wrapped store."""
    store: FactStore
    merged: dict[str, str]

    def all(self) -> dict[str, Fact]:
        return resolve_facts(self.store.all(), self.merged)

    def set(self, fact: Fact) -> Fact | None:
        return self.store.set(fact)

    def match_row(self, match: RowMatch) -> None:
        self.store.match_row(match)

    def matches(self) -> list[RowMatch]:
        return self.store.matches()
