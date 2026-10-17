"""Warning rules in app/ingest/text_checks.py, on synthetic spans and real fixture pages."""
from pathlib import Path

import pymupdf
import pytest

from app.ingest import text_checks as tc

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def span(text: str, size: float = 9.0, y: float = 100.0, flags: int = 0, font: str = "Times-Roman") -> dict:
    return {"chars": [{"c": c} for c in text], "size": size, "origin": (0.0, y), "flags": flags, "font": font}


def line(*spans: dict) -> dict:
    return {"spans": list(spans)}


def page_lines(name: str) -> list[dict]:
    with pymupdf.open(FIXTURES / name) as doc:
        raw = doc[0].get_text("rawdict")
    return [ln for block in raw["blocks"] for ln in block.get("lines", [])]


# Rule 1: characters, not fonts, make a warning

@pytest.mark.parametrize("ch", ["\x00", "\x01", "\x1f", "\t", "\n"])
def test_below_u0020_is_control(ch):
    assert tc.is_control(ch)
    assert tc.char_class(ch) == "control"


def test_char_classes():
    assert tc.char_class("") == "private_use"
    assert tc.char_class("") == "private_use"  # Symbol-font bracket piece
    assert tc.char_class("�") == "replacement"
    assert tc.char_class("θ") == "other"
    assert tc.char_class(" ") == "other"


def test_unmapped_glyph_null_makes_span_a_warning():
    assert tc.bad_chars(span("ab\x00c")) == ["\x00"]


def test_symbol_font_alone_is_not_a_warning():
    assert tc.bad_chars(span("θ ≤ ∑", font="Symbol")) == []


def test_bracket_fixture_flags_private_use_spans():
    bad = [c for ln in page_lines("equations_brackets.pdf") for s in ln["spans"] for c in tc.bad_chars(s)]
    assert bad and all(tc.char_class(c) == "private_use" for c in bad)


def test_clean_fixture_has_no_warning_spans():
    assert not any(tc.bad_chars(s) for ln in page_lines("text.pdf") for s in ln["spans"])


# Rule 2: superscripts explained by a footnote on the same page

def test_superscript_after_letter():
    assert tc.superscript_marker(span("Damage"), span("1", size=6.0, y=96.0)) == "1"
    assert tc.superscript_marker(span("Damage"), span("1", flags=tc.SUPERSCRIPT_FLAG)) == "1"
    assert tc.superscript_marker(span("Damage"), span("1")) is None  # same size, same baseline
    assert tc.superscript_marker(span("0.25"), span("1", size=6.0, y=96.0)) is None  # not after a letter


def test_footnote_line_needs_period_or_raised_marker():
    body = 9.0
    assert tc.footnote_marker(line(span("1.", size=7.9), span("Footnote text", size=7.9)), body) == "1"
    assert tc.footnote_marker(line(span("12. Values are", size=9.0)), body) == "12"  # table page: same size
    assert tc.footnote_marker(line(span("3", flags=tc.SUPERSCRIPT_FLAG), span("Text")), body) == "3"
    assert tc.footnote_marker(line(span("3", size=9.0)), body) is None  # table cell
    assert tc.footnote_marker(line(span("12", size=7.9), span("EI")), body) is None  # equation term
    assert tc.footnote_marker(line(span("1. Heading", size=12.0)), body) is None  # larger than body


def test_footnote_must_be_below_reference():
    footnotes = {"1": 600.0}
    assert tc.is_footnote_reference("1", span("1", y=300.0), footnotes)
    assert not tc.is_footnote_reference("1", span("1", y=700.0), footnotes)
    assert not tc.is_footnote_reference("2", span("1", y=300.0), footnotes)


def test_table_fixture_superscripts_are_explained():
    lines = page_lines("table.pdf")
    footnotes = tc.footnote_markers(lines)
    markers = [
        (m, s)
        for ln in lines
        for prev, s in zip(ln["spans"], ln["spans"][1:])
        if (m := tc.superscript_marker(prev, s))
    ]
    assert markers
    assert all(tc.is_footnote_reference(m, s, footnotes) for m, s in markers)
