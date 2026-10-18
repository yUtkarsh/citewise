"""Step 3a.3: walk blocks in reading order and cut them into clauses.

Each heading block starts a clause. Text before a page's first heading continues
the previous clause. The parent comes from the number (drop the last component);
commentary links to the provision with the same number. A nested stream (sections
of another standard quoted inside this one) hangs under the clause that quotes it.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field

from app.ingest import text_checks as tc
from app.ingest.pass1.layout import join_lines, normalize, normalize_line
from app.ingest.pass1.model import Block, Clause
from app.ingest.pdf_lines import Line, Rect
from app.profile.schema import Profile, Stream

COVERAGE_WINDOW = 2  # prose further than this many prose-bearing pages from its heading counts as uncovered
TEXT_KINDS = {"text", "where", "caption", "footnote"}
REGION_KINDS = {"heading", "text", "where", "equation", "footnote"}  # tables and figures are cropped on their own


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


def clause_id(stream: Stream, num: str) -> str:
    return stream.id_prefix + stream.prefix + num


def is_prose(block: Block, body_style: str) -> bool:
    return block.kind == "text" and block.style == body_style


def segment(pages: dict[int, list[Block]], profile: Profile, vocab: set[str], body_style: str) -> Segmentation:
    streams = {s.name: s for s in profile.streams}
    seg = Segmentation(clauses=[], owner={})
    seen: dict[str, int] = defaultdict(int)
    lines_of: dict[str, list[str]] = defaultdict(list)
    regions: dict[str, dict[tuple[int, int], Rect]] = defaultdict(dict)
    last_of_stream: dict[str, str] = {}
    # Coverage counts distance in pages that carry prose, so pages holding only a table
    # or a figure do not make the text after them look orphaned.
    prose_pages = sorted(p for p, blocks in pages.items() if any(is_prose(b, body_style) for b in blocks))
    current: Clause | None = None
    for page in sorted(pages):
        for block in pages[page]:
            if block.kind == "heading":
                stream = streams[block.stream]
                cid = clause_id(stream, block.number)
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
                    current.commentary_on = clause_id(streams[stream.commentary_of], block.number)
                if stream.nested_in:
                    current.container_id = last_of_stream.get(stream.nested_in)
                last_of_stream[stream.name] = cid
                seg.clauses.append(current)
                seg.owner[id(block)] = cid
                _grow(regions[cid], page, block)
                continue
            seg.owner[id(block)] = current.id if current else None
            if current:
                current.page_end = page
                if block.kind in REGION_KINDS:
                    _grow(regions[current.id], page, block)
                if block.kind in TEXT_KINDS:
                    lines_of[current.id].extend(normalize_line(ln, profile.unmapped_fonts) for ln in block.lines)
                    current.suspect_spans.extend(suspect_spans(block.lines, profile.unmapped_fonts))
            if is_prose(block, body_style):
                n = len(block.text)
                seg.prose_chars += n
                seg.prose_by_page[page] = seg.prose_by_page.get(page, 0) + n
                distance = bisect_right(prose_pages, page) - bisect_right(prose_pages, current.page) if current else None
                if current and distance <= COVERAGE_WINDOW:
                    seg.covered_chars += n
                else:
                    seg.uncovered_by_page[page] = seg.uncovered_by_page.get(page, 0) + n
    for clause in seg.clauses:
        clause.text = join_lines(lines_of[clause.id], vocab)
        clause.regions = [(page, box) for (page, _), box in sorted(regions[clause.id].items())]
        clause.suspect_spans = list(dict.fromkeys(clause.suspect_spans))
    assign_parents(seg.clauses, profile)
    return seg


def _grow(regions: dict[tuple[int, int], Rect], page: int, block: Block) -> None:
    """Union the block into the clause's region for its page and column."""
    key = (page, max(block.col, 0))
    b = block.bbox
    if key in regions:
        r = regions[key]
        b = (min(r[0], b[0]), min(r[1], b[1]), max(r[2], b[2]), max(r[3], b[3]))
    regions[key] = tuple(round(float(v), 1) for v in b)


def suspect_spans(lines: list[Line], unmapped_fonts: list[str]) -> list[str]:
    """Text of spans whose characters may not be what is printed: the spans the Pass 1
    warning rules flag, or any span in an unmapped font. Unmapped spans stay raw, as in
    the clause text; others are normalized the same way."""
    out = []
    for line in lines:
        for span in line.spans:
            text = span["text"].strip()
            if not text:
                continue
            if tc.text_from_unmapped_font(span, unmapped_fonts) or tc.is_unmapped_font(span["font"], unmapped_fonts):
                out.append(text)
            elif tc.bad_chars(span) or tc.ascii_from_symbol_font(span):
                out.append(normalize(text))
    return out


def assign_parents(clauses: list[Clause], profile: Profile) -> None:
    """Parent = the nearest existing ancestor number in the clause's own stream.
    Commentary exists only where there is something to say, so a commentary clause
    without a commentary ancestor hangs under the provision ancestor instead. A nested
    clause without an ancestor hangs under the clause that quotes it."""
    streams = {s.name: s for s in profile.streams}
    ids = {c.id for c in clauses}
    for clause in clauses:
        stream = streams[clause.stream]
        fallback = streams.get(stream.commentary_of) if stream.commentary_of else None
        num = parent_number(clause.number, stream.separator)
        while num:
            if clause_id(stream, num) in ids:
                clause.parent_id = clause_id(stream, num)
                break
            if fallback and clause_id(fallback, num) in ids:
                clause.parent_id = clause_id(fallback, num)
                break
            num = parent_number(num, stream.separator)
        if clause.parent_id is None and clause.container_id:
            clause.parent_id = clause.container_id


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
