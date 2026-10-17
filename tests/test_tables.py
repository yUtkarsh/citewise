"""Steps 3a.1, 3a.4-3a.6 for tables: grids, captions, multi-page merge, footnotes, crops."""
import os

import pymupdf
import pytest

from app.ingest.pdf_lines import read_lines
from app.ingest.pass1 import tables as tbl
from app.ingest.pass1.blocks import load_context
from app.ingest.pass1.crops import DPI, PADDING, crop
from app.ingest.pass1.model import TableSegment
from app.ingest.pass1.run_pass1 import run_pass1
from app.profile.page_map import build_page_map
from tests.conftest import FIXTURES


def segments(pdf, profile, pages, tmp_path):
    doc = pymupdf.open(pdf)
    ctx = load_context(doc, profile, pages)
    segs = tbl.extract_segments(doc, pdf, "test", pages, ctx.page_lines, False, tmp_path)
    tbl.attach_captions(segs, ctx.page_lines, profile, ctx.body)
    tbl.add_borderless(segs, ctx.page_lines, profile, ctx.body, pages)
    return doc, ctx, segs


def test_continued_table_merges_into_one(combined_pdf, fema_profile, tmp_path):
    pdf = combined_pdf("table_multi_1.pdf", "table_multi_2.pdf")
    doc, ctx, segs = segments(pdf, fema_profile, [1, 2], tmp_path)
    ordered = [s for p in (1, 2) for s in segs[p]]
    tables, _ = tbl.merge_segments(ordered)
    assert len(tables) == 1
    table = tables[0]
    assert table.pages == [1, 2] and not table.merge_conflict
    assert table.segments[1].continued

    tbl.build_rows(table, ctx.page_lines)
    first = table.segments[0]
    headers = {tbl._norm(r) for r in first.rows[:first.header_rows]}
    assert first.header_rows > 0
    assert not any(r.is_header for r in table.rows if r.page == 2)
    assert not any(tbl._norm(r.cells) in headers for r in table.rows if r.page == 2)
    assert len(table.rows) < sum(len(s.rows) for s in table.segments)  # repeated header dropped
    assert [r.index for r in table.rows] == list(range(len(table.rows)))


def seg(page, caption_num=None, continued=False, cols=(0, 100, 200), top=100):
    return TableSegment(page=page, bbox=(50, top, 550, top + 200), rows=[["a"]], row_bboxes=[(50, top, 550, top + 10)],
                        header_rows=0, col_x=[50 + c for c in cols], method="test", caption_num=caption_num,
                        continued=continued)


def test_merge_rules():
    tables, skipped = tbl.merge_segments([seg(1, "1-1"), seg(2, None), seg(3, "1-2"), seg(4, None, cols=(0, 80))])
    assert [t.id for t in tables] == ["Table 1-1", "Table 1-2"]
    assert tables[0].pages == [1, 2]  # uncaptioned, next page, same columns: continuation
    assert len(skipped) == 1  # different columns, no caption: not a table


def test_conflicting_signals_are_flagged_not_guessed():
    tables, _ = tbl.merge_segments([seg(1, "1-1"), seg(2, "1-1", continued=True, cols=(0, 60, 130, 200))])
    assert len(tables) == 1 and tables[0].merge_conflict
    tables, _ = tbl.merge_segments([seg(1, "1-1"), seg(2, "1-1", continued=True, cols=(5, 105, 205))])
    assert not tables[0].merge_conflict  # a shifted page is not a conflict


def test_crops_are_200_dpi(tmp_path):
    doc = pymupdf.open(FIXTURES / "table.pdf")
    box = (100.0, 100.0, 300.0, 200.0)
    path = tmp_path / "t.png"
    crop(doc, 1, box, path)
    pix = pymupdf.Pixmap(str(path))
    assert abs(pix.width - (200 + 2 * PADDING) * DPI / 72) <= 2
    assert abs(pix.height - (100 + 2 * PADDING) * DPI / 72) <= 2


def test_table_page_end_to_end(fema_profile, tmp_path):
    doc = pymupdf.open(FIXTURES / "table.pdf")
    result = run_pass1(doc, FIXTURES / "table.pdf", "test", fema_profile, build_page_map(doc), tmp_path / "cache",
                       tmp_path / "crops", use_docling=False, pages=[1], log=lambda *_: None)
    assert len(result.tables) == 1
    table = result.tables[0]
    markers = {f.marker for f in table.footnotes}
    assert markers
    assert {m for r in table.rows for m in r.markers} <= markers  # every row marker has its footnote
    assert all(s.crop and (tmp_path / "crops").exists() for s in table.segments)
    assert table.header_crop
    assert not [f for f in result.flagged if any(r.startswith("unexplained") for r in f.reasons)]


@pytest.mark.skipif(not os.getenv("RUN_SLOW"), reason="Docling takes ~1 min to load; set RUN_SLOW=1")
def test_docling_grid_on_a_table_page():
    pdf = FIXTURES / "table.pdf"
    doc = pymupdf.open(pdf)
    found = tbl.docling_segments(pdf, [1], {1: doc[0].rect.height})[1]
    assert found and found[0].rows and found[0].method == "docling"
    assert tbl.agreement(found[0], read_lines(doc[0])) > 0.5
