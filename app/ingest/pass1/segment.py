"""Step 3a.3: walk blocks in reading order and cut them into clauses.

Each heading block starts a clause. Text before a page's first heading continues
the previous clause. The parent comes from the number (drop the last component);
commentary links to the provision with the same number.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.ingest.pass1.layout import join_lines, normalize
from app.ingest.pass1.model import Block, Clause
from app.profile.schema import Profile

COVERAGE_WINDOW = 2  # text more than this many pages after its clause heading counts as uncovered
TEXT_KINDS = {"text", "where", "caption", "footnote"}


@dataclass
class Segmentation:
    clauses: list[Clause]
    owner: dict[int, str | None]  # id(block) -> clause id
    prose_chars: int = 0
    covered_chars: int = 0
    uncovered_by_page: dict[int, int] = field(default_factory=dict)
    prose_by_page: dict[int, int] = field(default_factory=dict)
    duplicates: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.covered_chars / self.prose_chars if self.prose_chars else 0.0

    def by_id(self) -> dict[str, Clause]:
        return {c.id: c for c in self.clauses}


def number_key(num: str, separator: str) -> tuple:
    """Sortable form of a heading number: numeric parts as ints, others as text."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in num.split(separator))


def parent_number(num: str, separator: str) -> str | None:
    parts = num.split(separator)
    return separator.join(parts[:-1]) if len(parts) > 1 else None


def segment(pages: dict[int, list[Block]], profile: Profile, vocab: set[str], body_style: str) -> Segmentation:
    streams = {s.name: s for s in profile.streams}
    seg = Segmentation(clauses=[], owner={})
    seen: dict[str, int] = defaultdict(int)
    lines_of: dict[str, list[str]] = defaultdict(list)
    current: Clause | None = None
    for page in sorted(pages):
        for block in pages[page]:
            if block.kind == "heading":
                stream = streams[block.stream]
                cid = stream.prefix + block.number
                seen[cid] += 1
                if seen[cid] > 1:
                    seg.duplicates.append(f"p{page}: heading {cid} repeated")
                    cid = f"{cid}~{seen[cid]}"
                current = Clause(
                    id=cid, number=block.number, stream=stream.name, kind=stream.kind,
                    title=normalize(block.title or ""), depth=len(block.number.split(stream.separator)),
                    page=page, bbox=block.bbox, page_end=page, order=len(seg.clauses),
                )
                if stream.commentary_of:
                    target = streams[stream.commentary_of]
                    current.commentary_on = target.prefix + block.number
                seg.clauses.append(current)
                seg.owner[id(block)] = cid
                continue
            seg.owner[id(block)] = current.id if current else None
            if current:
                current.page_end = page
                if block.kind in TEXT_KINDS:
                    lines_of[current.id].extend(normalize(ln.text) for ln in block.lines)
            if block.kind == "text" and block.style == body_style:
                n = len(block.text)
                seg.prose_chars += n
                seg.prose_by_page[page] = seg.prose_by_page.get(page, 0) + n
                if current and page - current.page <= COVERAGE_WINDOW:
                    seg.covered_chars += n
                else:
                    seg.uncovered_by_page[page] = seg.uncovered_by_page.get(page, 0) + n
    for clause in seg.clauses:
        clause.text = join_lines(lines_of[clause.id], vocab)
    assign_parents(seg.clauses, profile)
    return seg


def assign_parents(clauses: list[Clause], profile: Profile) -> None:
    """Parent = the nearest existing ancestor number in the clause's own stream.
    Commentary exists only where there is something to say, so a commentary clause
    without a commentary ancestor hangs under the provision ancestor instead."""
    streams = {s.name: s for s in profile.streams}
    ids = {c.id for c in clauses}
    for clause in clauses:
        stream = streams[clause.stream]
        fallback = streams.get(stream.commentary_of) if stream.commentary_of else None
        num = parent_number(clause.number, stream.separator)
        while num:
            if stream.prefix + num in ids:
                clause.parent_id = stream.prefix + num
                break
            if fallback and fallback.prefix + num in ids:
                clause.parent_id = fallback.prefix + num
                break
            num = parent_number(num, stream.separator)


def low_coverage_ranges(seg: Segmentation, threshold: float = 0.5) -> list[tuple[int, int]]:
    """Runs of consecutive pages where more than ``threshold`` of the prose is uncovered."""
    bad = sorted(p for p, total in seg.prose_by_page.items()
                 if total and seg.uncovered_by_page.get(p, 0) / total > threshold)
    ranges: list[tuple[int, int]] = []
    for p in bad:
        if ranges and p == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], p)
        else:
            ranges.append((p, p))
    return ranges
