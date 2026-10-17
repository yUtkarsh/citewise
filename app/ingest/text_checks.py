"""Deterministic text-layer warning rules, shared by scripts/font_check.py and
the Pass 1 warning detector.

Rules:
1. A font is never a warning by itself (Symbol is fine). A span is a warning only
   when it yields private-use (U+E000-F8FF), control (< U+0020, Cc/Cf) or U+FFFD chars.
2. A small raised digit after a letter is a footnote marker when a footnote with
   that marker exists on the same page, below it; otherwise it is an unexplained
   superscript. A footnote is a line starting "N." (or a raised N) in text no
   larger than the page body; table cells and equation terms have no period.

Spans and lines are PyMuPDF ``rawdict`` structures.
"""
from __future__ import annotations

from collections import Counter
import re
import unicodedata

SUPERSCRIPT_FLAG = 1  # PyMuPDF span flag bit 0
SMALL_SIZE_RATIO = 0.8  # superscript digit vs. preceding text
SIZE_TOLERANCE = 0.1  # points; footnote text may equal the body size on table pages
FOOTNOTE_START = re.compile(r"^\s*(\d{1,2})\.(\s|$)")
RAISED_START = re.compile(r"^\s*(\d{1,2})\s*$")

Span = dict
Line = dict


def is_private_use(ch: str) -> bool:
    return unicodedata.category(ch) == "Co"


def is_control(ch: str) -> bool:
    """Anything below U+0020 (how unmapped glyphs often appear) plus Cc/Cf."""
    return ord(ch) < 0x20 or unicodedata.category(ch) in {"Cc", "Cf"}


def char_class(ch: str) -> str:
    """'private_use', 'control', 'replacement' (all warnings) or 'other'."""
    if is_private_use(ch):
        return "private_use"
    if is_control(ch):
        return "control"
    if ch == "�":
        return "replacement"
    return "other"


def char_name(ch: str) -> str:
    return unicodedata.name(ch, f"<unnamed {unicodedata.category(ch)}>")


def span_text(span: Span) -> str:
    return "".join(c["c"] for c in span["chars"])


def bad_chars(span: Span) -> list[str]:
    """Characters in this span that make it a warning (rule 1)."""
    return [c["c"] for c in span["chars"] if char_class(c["c"]) != "other"]


def body_size(lines: list[Line]) -> float:
    """Most common character size on the page, i.e. the body text size."""
    sizes = Counter()
    for line in lines:
        for span in line["spans"]:
            sizes[round(span["size"], 1)] += len(span["chars"])
    return sizes.most_common(1)[0][0] if sizes else 0.0


def superscript_marker(prev: Span, span: Span) -> str | None:
    """The digit marker if ``span`` is a small raised digit right after a letter."""
    text = span_text(span).strip()
    prev_text = span_text(prev).rstrip()
    if not text.isdigit() or not prev_text or not prev_text[-1].isalpha():
        return None
    flagged = bool(span["flags"] & SUPERSCRIPT_FLAG)
    smaller = span["size"] < prev["size"] * SMALL_SIZE_RATIO
    raised = span["origin"][1] < prev["origin"][1] - 0.5
    return text if flagged or (smaller and raised) else None


def footnote_marker(line: Line, page_body_size: float) -> str | None:
    """The marker if this line starts a footnote: "N." in text no larger than
    the page body, or a raised N followed by more text on the line."""
    spans = [s for s in line["spans"] if span_text(s).strip()]
    if not spans:
        return None
    first = spans[0]
    text = span_text(first)
    if first["size"] > page_body_size + SIZE_TOLERANCE:
        return None
    if match := FOOTNOTE_START.match(text):
        return match.group(1)
    raised = bool(first["flags"] & SUPERSCRIPT_FLAG)
    if raised and len(spans) > 1 and (match := RAISED_START.match(text)):
        return match.group(1)
    return None


def footnote_markers(lines: list[Line]) -> dict[str, float]:
    """Footnote marker -> lowest y (top-most baseline) where that footnote starts."""
    size = body_size(lines)
    found: dict[str, float] = {}
    for line in lines:
        if marker := footnote_marker(line, size):
            y = line["spans"][0]["origin"][1]
            found[marker] = min(y, found.get(marker, y))
    return found


def is_footnote_reference(marker: str, span: Span, footnotes: dict[str, float]) -> bool:
    """True if a footnote with this marker exists on the page below the marker."""
    return marker in footnotes and footnotes[marker] > span["origin"][1]
