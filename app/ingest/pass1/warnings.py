"""Step 3a.8: line-level symbol warnings, recorded per page with the line bbox
(Pass 2 renders the line crop on the fly).

Uses the shared rules in ``app.ingest.text_checks``. A superscript's footnote
scope is its page, or every page of the merged table it sits in.
"""
from __future__ import annotations

from app.ingest import text_checks as tc
from app.ingest.pdf_lines import Line, Rect
from app.ingest.pass1.model import FlaggedLine, Table
from app.profile.schema import Profile


def _inside(line: Line, box: Rect) -> bool:
    cx, cy = (line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2
    return box[0] - 2 <= cx <= box[2] + 2 and box[1] - 2 <= cy <= box[3] + 2


def table_scopes(tables: list[Table], page_lines: dict[int, list[Line]], profile: Profile) -> list[tuple[int, Rect, set[str]]]:
    """(page, segment box, markers) for every table segment: the markers are every
    footnote of the whole merged table, on any of its pages."""
    out = []
    for table in tables:
        markers = {f.marker for f in table.footnotes}
        for seg in table.segments:
            below = [ln for ln in page_lines.get(seg.page, []) if ln.bbox[1] >= seg.bbox[3] - 2]
            markers |= tc.footnote_markers(below, profile.footnote)
        for seg in table.segments:
            box = seg.bbox
            if seg.caption_bbox:  # markers in the caption share the table's footnotes
                box = (min(box[0], seg.caption_bbox[0]), min(box[1], seg.caption_bbox[1]),
                       max(box[2], seg.caption_bbox[2]), max(box[3], seg.caption_bbox[3]))
            out.append((seg.page, box, markers))
    return out


def line_reasons(line: Line, scope: set[str], profile: Profile) -> list[str]:
    reasons = []
    classes = sorted({tc.char_class(c) for s in line.spans for c in tc.bad_chars(s)})
    reasons += classes
    if any(tc.ascii_from_symbol_font(s) for s in line.spans):
        reasons.append("symbol_font_ascii")
    if any(tc.text_from_unmapped_font(s, profile.unmapped_fonts) for s in line.spans):
        reasons.append("unmapped_font")
    reasons += [f"unexplained_superscript:{m}" for m, _ in tc.superscripts(line, profile.footnote.marker_regex)
                if not tc.is_footnote_reference(m, scope)]
    return reasons


def flag_lines(page_lines: dict[int, list[Line]], tables: list[Table], profile: Profile) -> list[FlaggedLine]:
    scopes = table_scopes(tables, page_lines, profile)
    flagged = []
    for page in sorted(page_lines):
        lines = page_lines[page]
        page_scope = tc.footnote_markers(lines, profile.footnote)
        page_tables = [(box, markers) for p, box, markers in scopes if p == page]
        for n, line in enumerate(lines):
            scope = set(page_scope)
            for box, markers in page_tables:
                if _inside(line, box):
                    scope |= markers
            reasons = line_reasons(line, scope, profile)
            if reasons:
                flagged.append(FlaggedLine(id=f"p{page}:l{n}", page=page, bbox=line.bbox,
                                           text=line.text.strip(), reasons=reasons))
    return flagged
