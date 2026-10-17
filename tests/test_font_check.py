"""font_check report on a generated page and on fixture pages."""
from pathlib import Path

import pymupdf
import pytest

from scripts import font_check

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Plain ASCII text", fontsize=11)
    page.insert_font(fontname="uni", fontbuffer=pymupdf.Font("cjk").buffer)
    page.insert_text((72, 130), "α ≤", fontsize=11, fontname="uni")
    path = tmp_path / "sample.pdf"
    doc.save(path)
    return path


def test_scan_reports_fonts_and_chars(sample_pdf: Path):
    report = font_check.scan_pdf(sample_pdf)
    assert report.page_count == 1
    assert report.fonts
    assert "α" in report.by_class("other")
    assert "≤" in report.by_class("other")
    assert sum(report.warning_spans.values()) == 0
    assert report.pages_without_text == []


def test_bracket_fixture_reports_warning_spans():
    report = font_check.scan_pdf(FIXTURES / "equations_brackets.pdf")
    assert report.warning_spans["Symbol"] > 0
    assert report.occurrences("private_use") > 0


def test_table_fixture_superscripts_are_footnote_markers():
    report = font_check.scan_pdf(FIXTURES / "table.pdf")
    assert report.footnote_markers
    assert report.unexplained == []


def test_find_pdfs_deduplicates(tmp_path: Path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")  # find_pdfs only looks at names
    assert font_check.find_pdfs([tmp_path, pdf]) == [pdf.resolve()]
