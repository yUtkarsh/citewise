"""Warning rules in app/ingest/text_checks.py, on synthetic spans and real fixture pages."""
from pathlib import Path

import pymupdf
import pytest

from app.ingest import text_checks as tc
from app.profile.schema import FootnoteFormat

FOOTNOTE = FootnoteFormat(line_regex=r"^(?P<marker>\d{1,2})\.(\s|$)", marker_superscript=True)
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


# Rule 2: ASCII letters or digits from a symbol or math font

def test_ascii_from_symbol_font():
    assert tc.ascii_from_symbol_font(span("q", font="Symbol"))
    assert tc.ascii_from_symbol_font(span("7", font="ABCDEF+CambriaMath"))
    assert not tc.ascii_from_symbol_font(span("θ ≤", font="Symbol"))
    assert not tc.ascii_from_symbol_font(span("q", font="Times-Roman"))
    assert tc.ascii_from_symbol_font(span("x", font="CustomFont"), symbolic_fonts={"CustomFont"})


def test_dict_and_rawdict_spans_read_the_same():
    assert tc.span_text({"text": "ab"}) == tc.span_text({"chars": [{"c": "a"}, {"c": "b"}]})


# Rule 3: superscripts explained by a footnote in their scope

def test_superscript_after_letter():
    assert tc.superscript_marker(span("Damage"), span("1", size=6.0, y=96.0)) == "1"
    assert tc.superscript_marker(span("Damage"), span("1", flags=tc.SUPERSCRIPT_FLAG)) == "1"
    assert tc.superscript_marker(span("Damage"), span("1")) is None  # same size, same baseline
    assert tc.superscript_marker(span("0.25"), span("1", size=6.0, y=96.0)) is None  # not after a letter


def test_footnote_line_needs_period_or_raised_marker():
    body = 9.0
    fm = lambda ln: tc.footnote_marker(ln, body, FOOTNOTE)  # noqa: E731
    assert fm(line(span("1.", size=7.9), span(" Footnote text", size=7.9))) == "1"
    assert fm(line(span("12. Values are", size=9.0))) == "12"  # table page: same size
    assert fm(line(span("3", flags=tc.SUPERSCRIPT_FLAG), span("Text"))) == "3"
    assert fm(line(span("3", size=9.0))) is None  # table cell
    assert fm(line(span("12", size=7.9), span("EI"))) is None  # equation term
    assert fm(line(span("1. Heading", size=12.0))) is None  # larger than body


def test_footnote_format_comes_from_the_profile():
    lettered = FootnoteFormat(line_regex=r"^(?P<marker>[a-z])\)\s", marker_superscript=False)
    assert tc.footnote_marker(line(span("b) Applies to")), 9.0, lettered) == "b"
    assert tc.footnote_marker(line(span("1. Applies to")), 9.0, lettered) is None
    assert tc.footnote_marker(line(span("3", flags=tc.SUPERSCRIPT_FLAG), span("Text")), 9.0, lettered) is None


def test_footnote_reference_is_scope_membership():
    assert tc.is_footnote_reference("1", {"1", "2"})
    assert not tc.is_footnote_reference("3", {"1", "2"})


def test_table_fixture_superscripts_are_explained():
    lines = page_lines("table.pdf")
    footnotes = tc.footnote_markers(lines, FOOTNOTE)
    markers = [m for ln in lines for m, _ in tc.superscripts(ln)]
    assert markers
    assert all(tc.is_footnote_reference(m, footnotes) for m in markers)
