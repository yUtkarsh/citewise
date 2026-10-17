"""Steps 3a.4, 3a.7 and 3a.8: equations, references and line-level warnings."""
import re

import pymupdf

from app.ingest.pdf_lines import Line, raw_lines
from app.ingest.pass1 import references as refs
from app.ingest.pass1.model import Footnote, Table, TableSegment
from app.ingest.pass1.run_pass1 import run_pass1
from app.ingest.pass1.skeleton import build_skeleton
from app.ingest.pass1.warnings import flag_lines
from app.profile.page_map import build_page_map
from tests import synthetic as syn
from tests.conftest import FIXTURES


def pass1(name, profile, tmp_path):
    doc = pymupdf.open(FIXTURES / name)
    return run_pass1(doc, FIXTURES / name, "test", profile, build_page_map(doc), tmp_path / "cache",
                     tmp_path / "crops", use_docling=False, pages=[1], log=lambda *_: None)


# ---- equations ---------------------------------------------------------------

def test_every_equation_number_becomes_an_equation(fema_profile, tmp_path):
    with pymupdf.open(FIXTURES / "equations.pdf") as doc:
        numbers = {m.group("num") for ln in raw_lines(doc[0]) for s in ln.spans
                   if (m := re.fullmatch(fema_profile.equation_number, s["text"].strip()))}
    result = pass1("equations.pdf", fema_profile, tmp_path)
    assert numbers and {e.number for e in result.equations} == numbers
    for eq in result.equations:
        assert eq.clause_id
        assert any(f"({eq.number})" in s["text"] for s in eq.raw_spans)
        assert (tmp_path.parent / eq.crop).exists() or (tmp_path / "crops").exists()


def test_equation_box_covers_stacked_fractions(fema_profile, tmp_path):
    result = pass1("equations.pdf", fema_profile, tmp_path)
    tallest = max(result.equations, key=lambda e: e.bbox[3] - e.bbox[1])
    number_span = next(s for s in tallest.raw_spans if f"({tallest.number})" in s["text"])
    assert (tallest.bbox[3] - tallest.bbox[1]) > 1.5 * (number_span["bbox"][3] - number_span["bbox"][1])


def test_where_items_have_symbol_and_definition(fema_profile, tmp_path):
    result = pass1("equations.pdf", fema_profile, tmp_path)
    assert result.where_items
    assert all(w.symbol and w.definition and w.owner_id for w in result.where_items)


# ---- references --------------------------------------------------------------

def targets():
    return refs.Targets(clause=["1.1", "1.2", "1.2.1", "1.3", "2"], table=["Table 5-5", "Table 5-6"],
                        figure=[], equation=["Equation 3-1"],
                        clause_depth={"1.1": 2, "1.2": 2, "1.2.1": 3, "1.3": 2, "2": 1})


def test_lists_and_ranges_expand_to_known_ids():
    profile = syn.profile(1)
    text = "See Sections 1.1 through 1.3, Tables 5-5 and 5-6, Equation 3-1 and Section 9.9 of ACI 318-95."
    found = refs.regex_references("2", text, 1, profile, targets())
    resolved = {r.target_id for r in found if r.target_id}
    assert resolved == {"1.1", "1.2", "1.3", "Table 5-5", "Table 5-6", "Equation 3-1"}  # 1.2.1 is deeper
    assert [r for r in found if r.target_id is None and "9.9" in r.text]
    assert [r.target_id for r in refs.external_codes("2", text, 1, profile)] == ["ACI 318-95"]


def test_self_citation_is_not_an_external_code():
    profile = syn.profile(1, external_code_regex=r"\b(?P<code>SC\s?\d+)")
    assert refs.external_codes("1", "as required by SC 1", 1, profile) == []


def test_link_annotations_resolve_to_the_heading_they_point_at(tmp_path):
    pages = [syn.heading("1", "General", 80) + [("See Section 2 for loads", 54, 120, "body", 10)],
             syn.heading("2", "Loads", 80) + syn.prose(100)]
    pdf = syn.make_pdf(tmp_path / "doc.pdf", pages)
    doc = pymupdf.open(pdf)
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(54, 110, 160, 124), "page": 1,
                        "to": pymupdf.Point(54, 70)})
    doc.saveIncr()
    profile = syn.profile(2)
    skel = build_skeleton(doc, profile)
    links, _ = refs.link_references(doc, [1, 2], skel.blocks, skel.seg.owner, skel.ctx.page_lines)
    assert [(r.source_id, r.target_id, r.method) for r in links] == [("1", "2", "link")]
    assert "Section 2" in links[0].text


# ---- warnings ----------------------------------------------------------------

def test_bracket_fixture_flags_private_use_lines_with_crops(fema_profile, tmp_path):
    result = pass1("equations_brackets.pdf", fema_profile, tmp_path)
    flagged = [f for f in result.flagged if "private_use" in f.reasons]
    assert flagged
    assert all(f.crop for f in flagged)


def span(text, x, y=100.0, size=9.0, font="Helvetica"):
    return {"text": text, "font": font, "size": size, "flags": 0, "origin": (x, y), "bbox": (x, y - size, x + 30, y)}


def superscript_line(page, y):
    sup = span("3", 90, y - 3, size=6.0)
    return Line(page, (50, y - 9, 96, y), [span("Damage", 50, y), sup])


def test_superscript_scope_is_the_whole_merged_table(fema_profile):
    def table_seg(page):
        return TableSegment(page=page, bbox=(40, 50, 560, 400), rows=[], row_bboxes=[], header_rows=0,
                            col_x=[], method="test")

    table = Table(id="Table 1-1", number="1-1", caption="", segments=[table_seg(1), table_seg(2)],
                  footnotes=[Footnote("3", "3. Note.", 1, (40, 410, 300, 420))])
    lines = {1: [], 2: [superscript_line(2, 200), superscript_line(2, 600)]}  # inside, then outside the table
    flagged = flag_lines(lines, [table], fema_profile)
    assert [f.bbox[1] for f in flagged] == [591]
    assert flagged[0].reasons == ["unexplained_superscript:3"]


def test_ascii_from_symbol_font_line_is_flagged(fema_profile):
    line = Line(1, (50, 90, 80, 100), [span("q", 50, font="Symbol")])
    assert flag_lines({1: [line]}, [], fema_profile)[0].reasons == ["symbol_font_ascii"]
