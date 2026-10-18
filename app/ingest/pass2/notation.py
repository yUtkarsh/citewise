"""The notation dictionary built from the notation pages, the per-clause notation
slice, and deterministic Term links (SPEC Step 3b, jobs 2 and 3)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import re

from app.ingest.pass2.schemas import NotationEntry, TermEntry

SLICE_LIMIT = 60
TERM_LINK_CAP = 50


@dataclass
class NotationDict:
    entries: list[tuple[NotationEntry, int]] = field(default_factory=list)  # (entry, page)

    def add(self, page: int, entries: list[NotationEntry]) -> None:
        self.entries.extend((e, page) for e in entries)

    def ids(self) -> set[str]:
        return {e.id for e, _ in self.entries}

    def latex(self) -> dict[str, str]:
        return {e.id: e.latex for e, _ in self.entries}

    def slice(self, text: str, chapter: str | None, extra_symbols: set[str] = frozenset()) -> list[NotationEntry]:
        """Entries whose symbol or an alias appears in ``text`` as a token, in scope for
        ``chapter``. A one-character symbol counts only when it also appears among
        ``extra_symbols`` (where-block or equation symbols), since lone letters occur in
        ordinary words."""
        out = []
        for entry, _ in self.entries:
            if entry.scope_chapters and chapter and chapter not in entry.scope_chapters:
                continue
            forms = {entry.display, *entry.aliases}
            if any(_present(f, text, extra_symbols) for f in forms if f.strip()):
                out.append(entry)
        return out[:SLICE_LIMIT]


def _primes(text: str) -> str:
    return re.sub("['’]", "′", text)  # ' and ’ match ′


def _present(form: str, text: str, extra: set[str]) -> bool:
    form, text = _primes(form.strip()), _primes(text)
    extra = {_primes(x) for x in extra}
    compact = form.replace(" ", "")
    if len(compact) == 1:
        return form in extra or compact in extra
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", text) is not None


def term_pattern(term: str) -> re.Pattern | None:
    """A multi-word term matches anywhere, case-insensitively; a one-word term matches
    only where it is capitalized (lower-case one-word matches are ordinary words)."""
    words = term.split()
    if not words:
        return None
    body = r"\s+".join(re.escape(w) for w in words)
    if len(words) > 1:
        return re.compile(rf"(?<![A-Za-z]){body}(?![A-Za-z])", re.IGNORECASE)
    word = words[0]
    if not word[:1].isalpha():
        return None
    head = re.escape(word[0].upper())
    return re.compile(rf"(?<![A-Za-z]){head}{re.escape(word[1:])}(?![A-Za-z])")


def link_terms(terms: list[TermEntry], clauses: list[tuple[str, str]], log=print,
               cap: int = TERM_LINK_CAP) -> dict[str, list[str]]:
    """term -> clause ids that use it (term or alias), in clause order, at most ``cap``.
    Terms that hit the cap are logged."""
    links: dict[str, list[str]] = defaultdict(list)
    for term in terms:
        patterns = [p for form in [term.term, *term.aliases] if (p := term_pattern(form))]
        hits = 0
        for cid, text in clauses:
            if any(p.search(text) for p in patterns):
                hits += 1
                if len(links[term.term]) < cap:
                    links[term.term].append(cid)
        if hits > cap:
            log(f"terms: '{term.term}' is used by {hits} clauses; linked the first {cap}")
    return dict(links)


def term_id(term: str) -> str:
    return "term:" + re.sub(r"[^a-z0-9]+", "_", term.lower()).strip("_")
