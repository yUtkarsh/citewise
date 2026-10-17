"""Text lines from PyMuPDF, shared by the page map, Pass 1 and the warning rules.

Spans are PyMuPDF ``dict`` spans (keys: text, font, size, flags, origin, bbox), so
``app.ingest.text_checks`` works on them directly. PyMuPDF often splits one visual
line into several (a heading number and its title, for example); fragments on the
same baseline are merged unless the gap between them crosses the page centre, which
would join two columns.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pymupdf

BOLD_FLAG = 16
ITALIC_FLAG = 2
BASELINE_TOLERANCE = 1.5  # points
MERGE_GAP_EM = 3.0  # max horizontal gap, in font sizes, to merge fragments
ROW_OVERLAP = 0.6  # vertical overlap share that makes two fragments one row
SPACE_GAP_EM = 0.15  # a gap wider than this between spans reads as a space

Rect = tuple[float, float, float, float]


@dataclass
class Line:
    page: int  # 1-based
    bbox: Rect
    spans: list[dict] = field(default_factory=list)
    col: int = 0  # set by layout: 0 left/single, 1 right, -1 full width

    @property
    def text(self) -> str:
        """Span texts joined; a space is added where a visible gap separates two spans."""
        out = ""
        prev = None
        for span in self.spans:
            text = span["text"]
            if prev is not None and out and not out[-1].isspace() and not text[:1].isspace():
                if span["bbox"][0] - prev["bbox"][2] > SPACE_GAP_EM * span["size"]:
                    out += " "
            out += text
            prev = span
        return out

    @property
    def first_span(self) -> dict:
        return next((s for s in self.spans if s["text"].strip()), self.spans[0])

    @property
    def size(self) -> float:
        return round(self.first_span["size"], 1)

    @property
    def font(self) -> str:
        return self.first_span["font"]

    @property
    def bold(self) -> bool:
        return is_bold(self.first_span)

    @property
    def style(self) -> str:
        return style_label(self.font, self.size, self.bold)

    @property
    def baseline(self) -> float:
        return self.first_span["origin"][1]


def is_bold(span: dict) -> bool:
    return bool(span["flags"] & BOLD_FLAG) or "bold" in span["font"].lower()


def is_italic(span: dict) -> bool:
    name = span["font"].lower()
    return bool(span["flags"] & ITALIC_FLAG) or "italic" in name or "oblique" in name


def style_key(span: dict) -> tuple[str, float, bool]:
    return span["font"], round(span["size"], 1), is_bold(span)


def style_label(font: str, size: float, bold: bool) -> str:
    return f"{font} {round(size, 1)}{' bold' if bold else ''}"


def union(a: Rect, b: Rect) -> Rect:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def raw_lines(page: pymupdf.Page) -> list[Line]:
    """Every non-empty PyMuPDF line, unmerged, in content-stream order."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"]]
            if any(s["text"].strip() for s in spans):
                out.append(Line(page.number + 1, tuple(line["bbox"]), spans))
    return out


def _same_row(a: Line, b: Line) -> bool:
    """Same baseline, or one box mostly inside the other's height (a large chapter number)."""
    if abs(a.baseline - b.baseline) <= BASELINE_TOLERANCE:
        return True
    overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
    shorter = min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
    return shorter > 0 and overlap >= ROW_OVERLAP * shorter


def _mergeable(left: Line, right: Line, centre: float) -> bool:
    if not _same_row(left, right):
        return False
    gap = right.bbox[0] - left.bbox[2]
    if gap < -1 or gap > MERGE_GAP_EM * max(left.size, right.size):
        return False
    return not (left.bbox[2] < centre < right.bbox[0])


def read_lines(page: pymupdf.Page) -> list[Line]:
    """Visual lines of a page, fragments on one baseline merged, sorted top to bottom."""
    lines = sorted(raw_lines(page), key=lambda ln: (round(ln.baseline), ln.bbox[0]))
    centre = page.rect.width / 2
    merged: list[Line] = []
    for line in lines:
        target = next((m for m in reversed(merged[-6:]) if _mergeable(m, line, centre)), None)
        if target is not None:
            target.spans.extend(line.spans)
        else:  # a fragment printed to the left, e.g. a big chapter number beside its title
            target = next((m for m in reversed(merged[-6:]) if _mergeable(line, m, centre)), None)
            if target is None:
                merged.append(line)
                continue
            target.spans[:0] = line.spans
        target.bbox = union(target.bbox, line.bbox)
    merged.sort(key=lambda ln: (round(ln.baseline), ln.bbox[0]))
    return merged


def body_style(lines: list[Line]) -> tuple[str, float, bool]:
    """Most common (font, size, bold) by character count: the body text style."""
    counts: Counter = Counter()
    for line in lines:
        for span in line.spans:
            counts[style_key(span)] += len(span["text"].strip())
    return counts.most_common(1)[0][0] if counts else ("", 0.0, False)
