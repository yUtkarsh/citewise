"""Footnote markers printed raised on a number inside a table cell: found from the PDF's
spans, stripped from the cell, attached to it, and checked in Pass 2 answers. Invented
spans and rows (no text from any real code)."""
from __future__ import annotations

from app.ingest import text_checks as tc
from app.ingest.pass2.records import RowRec
from app.ingest.pass2.schemas import CellValue as TableValue
from app.ingest.pass2.validate import footnote_in_value


def span(text, size=9.0, flags=0, y=100.0):
    return {"text": text, "size": size, "flags": flags, "origin": (0.0, y), "font": "Body"}


def line(*spans):
    return {"spans": list(spans), "bbox": (0, 90, 100, 102)}


def test_a_raised_marker_after_a_digit_is_found_and_stripped_from_its_cell():
    found = tc.value_superscripts(line(span("400–9000"), span("3", 7.0, 1, 96.0)))
    assert found == [("3", "400–9000")]
    cells, markers = tc.strip_value_markers(["Late", "200–400", "400–90003"], found)
    assert cells == ["Late", "200–400", "400–9000"] and markers == {2: ["3"]}
    # thousands commas in the print and none in the grid (or the reverse) still match
    assert tc.strip_value_markers(["400–9,0003"], found)[0] == ["400–9,000"]


def test_ordinary_digits_are_not_markers():
    assert tc.value_superscripts(line(span("400–9000"), span("3"))) == []  # same size, same baseline
    assert tc.value_superscripts(line(span("Type"), span("3", 7.0, 1, 96.0))) == []  # after a letter
    assert tc.strip_value_markers(["12"], [("2", "3")]) == (["12"], {})


def row(cells, cell_markers):
    return RowRec(4, 1, (0, 0, 1, 1), cells, False, "docling", [], cell_markers)


def test_a_value_ending_in_a_raised_marker_is_flagged():
    r = row(["Late", "400–9000"], {1: ["3"]})
    bad = TableValue(col=1, value="400–90003", number=None, unit="psi", formula=None)
    assert footnote_in_value(bad, r).code == "footnote_in_value"
    good = TableValue(col=1, value="400–9000", number=None, unit="psi", formula=None)
    assert footnote_in_value(good, r) is None
    assert footnote_in_value(bad, row(["Late", "400–9000"], {})) is None  # no marker printed on that cell


def test_a_text_word_with_a_glued_marker_is_not_a_missing_number():
    from app.ingest.pass2.validate import _without_cell_marker

    r = row(["Silt", "2000", "200"], {1: ["7"]})
    assert _without_cell_marker("20007", r) == "2000" and _without_cell_marker("200", r) == "200"
    assert _without_cell_marker("20007", row(["Silt", "2000"], {})) == "20007"
