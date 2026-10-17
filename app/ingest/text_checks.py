"""Deterministic text-layer warning rules, shared by scripts/font_check.py and
the Pass 1 warning detector (SPEC Step 3a.8).

Rules:
1. A font is never a warning by itself (Symbol is fine). A span is a warning when
   it yields private-use (U+E000-F8FF), control (< U+0020, Cc/Cf) or U+FFFD chars.
2. An ASCII letter or digit drawn in a symbol or math font is a warning: the glyph
   was probably not mapped to its real Unicode symbol.
3. A small raised digit after a letter is a footnote marker when a footnote with
   that marker exists in its scope: the page, or every page of a merged table for
   markers inside that table. Otherwise it is an unexplained superscript. What a
   footnote line looks like comes from the document profile (``FootnoteFormat``).

Spans are PyMuPDF ``dict`` spans (``text``) or ``rawdict`` spans (``chars``);
lines are PyMuPDF line dicts or ``app.ingest.pdf_lines.Line`` objects.
"""
from __future__ import annotations

from collections import Counter
import re
from typing import Iterable, Protocol
import unicodedata

SUPERSCRIPT_FLAG = 1  # PyMuPDF span flag bit 0
SMALL_SIZE_RATIO = 0.8  # superscript digit vs. preceding text
SIZE_TOLERANCE = 0.1  # points; footnote text may equal the body size on table pages
RAISED_START = re.compile(r"^\s*(\d{1,2})\s*$")
ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")
# Font families whose glyphs are symbols, not letters (generic, not document-specific).
SYMBOL_FONT = re.compile(r"symbol|math|mt ?extra|^cms[yl]|^cmmi|^cmex|^msam|^msbm|wingding|dingbat|webding",
                         re.IGNORECASE)

Span = dict


class FootnoteFormat(Protocol):
    line_regex: str
    marker_superscript: bool


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
    if "text" in span:
        return span["text"]
    return "".join(c["c"] for c in span["chars"])


def line_spans(line) -> list[Span]:
    return line["spans"] if isinstance(line, dict) else line.spans


def bad_chars(span: Span) -> list[str]:
    """Characters in this span that make it a warning (rule 1)."""
    return [c for c in span_text(span) if char_class(c) != "other"]


def is_symbol_font(font: str, symbolic_fonts: Iterable[str] = ()) -> bool:
    base = font.split("+")[-1]  # drop a subset prefix like ABCDEF+
    return bool(SYMBOL_FONT.search(base)) or font in set(symbolic_fonts)


def ascii_from_symbol_font(span: Span, symbolic_fonts: Iterable[str] = ()) -> bool:
    """Rule 2: an ASCII letter or digit in a symbol or math font."""
    return is_symbol_font(span["font"], symbolic_fonts) and bool(ASCII_ALNUM.search(span_text(span)))


def body_size(lines: list) -> float:
    """Most common character size on the page, i.e. the body text size."""
    sizes = Counter()
    for line in lines:
        for span in line_spans(line):
            sizes[round(span["size"], 1)] += len(span_text(span))
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


def superscripts(line) -> list[tuple[str, Span]]:
    """(marker, span) for every superscript digit after a letter in this line."""
    spans = line_spans(line)
    return [(m, s) for prev, s in zip(spans, spans[1:]) if (m := superscript_marker(prev, s))]


def footnote_marker(line, page_body_size: float, fmt: FootnoteFormat) -> str | None:
    """The marker if this line starts a footnote in the profile's format, in text
    no larger than the page body; or a raised number followed by more text."""
    spans = [s for s in line_spans(line) if span_text(s).strip()]
    if not spans:
        return None
    first = spans[0]
    if first["size"] > page_body_size + SIZE_TOLERANCE:
        return None
    # Line objects insert spaces at visible gaps between spans; raw dict lines do not
    text = "".join(span_text(s) for s in spans) if isinstance(line, dict) else line.text
    if match := re.match(fmt.line_regex, text.lstrip()):
        return match.group("marker")
    raised = bool(first["flags"] & SUPERSCRIPT_FLAG)
    if fmt.marker_superscript and raised and len(spans) > 1 and (match := RAISED_START.match(span_text(first))):
        return match.group(1)
    return None


def footnote_markers(lines: list, fmt: FootnoteFormat) -> set[str]:
    """Markers of every footnote that starts on these lines (one page or one scope)."""
    size = body_size(lines)
    return {m for line in lines if (m := footnote_marker(line, size, fmt))}


def is_footnote_reference(marker: str, scope_markers: set[str]) -> bool:
    """True if a footnote with this marker exists in the marker's scope."""
    return marker in scope_markers
