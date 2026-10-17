"""Step 2.1 page map on generated pages and fixture pages."""
import pymupdf

from app.ingest.pdf_lines import read_lines
from app.profile.page_map import build_page_map, render_page_map, summarize_page
from tests.conftest import FIXTURES
from tests.synthetic import heading, make_pdf, prose


def test_summary_counts_numbers_headings_and_dot_leaders(tmp_path):
    toc = [(f"{i}.1 Topic . . . . . . . . . . . . . . {i}", 54, 100 + 14 * i, "body", 10) for i in range(1, 6)]
    body = heading("2.1", "Scope", 100) + prose(120) + heading("2.2", "Loads", 180) + prose(200)
    pdf = make_pdf(tmp_path / "doc.pdf", [toc, body])
    with pymupdf.open(pdf) as doc:
        first, second = build_page_map(doc)
    assert first.dot_leaders == 5 and first.numbered_lines == 5
    assert second.dot_leaders == 0 and second.numbered_lines == 2
    assert second.first_heading.startswith("2.1")
    assert any("Helvetica-Bold" in s for s in second.heading_styles)
    text = render_page_map([first, second])
    assert len(text.splitlines()) == 3 and "p2 |" in text


def test_running_header_and_footer_are_reported_separately(tmp_path):
    page = [("Running Title", 250, 30, "sans", 9), ("7", 300, 770, "sans", 9)] + prose(200)
    pdf = make_pdf(tmp_path / "doc.pdf", [page])
    with pymupdf.open(pdf) as doc:
        summary = summarize_page(doc[0])
    assert summary.header == "Running Title" and summary.footer == "7"
    assert summary.line_count == 3


def test_fixture_tables_are_flagged():
    for name, expected in (("table.pdf", True), ("table_multi_1.pdf", True), ("text.pdf", False)):
        with pymupdf.open(FIXTURES / name) as doc:
            assert summarize_page(doc[0]).has_table is expected, name


def test_two_column_fixture_is_detected():
    with pymupdf.open(FIXTURES / "commentary.pdf") as doc:
        assert summarize_page(doc[0]).columns == 2


def test_fragments_on_one_row_merge_with_a_space(tmp_path):
    big_number = [("1.", 54, 80, "bold", 40), ("Chapter Title", 130, 72, "bold", 18)]
    pdf = make_pdf(tmp_path / "doc.pdf", [big_number + heading("2.1", "Scope", 200)])
    with pymupdf.open(pdf) as doc:
        texts = [ln.text for ln in read_lines(doc[0])]
    assert "1. Chapter Title" in texts
    assert "2.1 Scope" in texts
