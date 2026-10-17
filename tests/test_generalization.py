"""Fixes found by running an unseen document (no fixture from it is needed here):
run-in headings, font-name weights, named links, letter footnote markers, rotated
margin text, unmapped symbol fonts, wrapped citations, coverage over table pages,
panel references, nested (quoted) streams and the font checks. All synthetic."""
import pymupdf
import pytest
from pydantic import ValidationError

from app.ingest import text_checks as tc
from app.ingest.pdf_lines import Line, is_bold, is_italic, read_lines
from app.ingest.pass1 import references as refs
from app.ingest.pass1.blocks import load_context
from app.ingest.pass1.layout import normalize_line
from app.ingest.pass1.skeleton import build_skeleton
from app.ingest.pass1.warnings import flag_lines
from app.profile.fonts import font_inventory
from app.profile.schema import FootnoteFormat, ProfileRules
from app.profile.validate import validate
from tests import synthetic as syn

BODY = ("Times-Roman", 10.0, False)


def doc_of(tmp_path, pages, name="doc.pdf"):
    return pymupdf.open(syn.make_pdf(tmp_path / name, pages))


# A1: run-in headings ----------------------------------------------------------

def test_run_in_heading_keeps_only_its_title(tmp_path):
    page = [("4.2.1", 54, 100, "bold", 11), ("Wall Anchors.", 85, 100, "bold", 11),
            ("Anchors shall resist the", 160, 100, "body", 10)] + syn.prose(113, lines=3)
    doc = doc_of(tmp_path, [page])
    skel = build_skeleton(doc, _with_body(syn.profile(1)))
    (clause,) = skel.seg.clauses
    assert clause.title == "Wall Anchors."
    assert clause.text.startswith("Anchors shall resist the")
    assert syn.PROSE in clause.text


def _with_body(profile):
    profile.body_style = BODY
    return profile


def test_heading_title_wrapped_in_heading_style_still_joins(tmp_path):
    page = syn.heading("4.3", "Requirements for Existing", 100) + [("Buildings", 76, 113, "bold", 11)] + syn.prose(130)
    clause = build_skeleton(doc_of(tmp_path, [page]), _with_body(syn.profile(1))).seg.clauses[0]
    assert clause.title == "Requirements for Existing Buildings"


# A2: weight and slant from font names -----------------------------------------

@pytest.mark.parametrize("font, bold, italic", [
    ("Helvetica-Bold", True, False), ("Times-Roman", False, False), ("Times-Italic", False, True),
    ("ABCDEF+Arial-BoldItalicMT", True, True), ("Foo-Semibold", True, False),
    ("XYZ123.B", True, False), ("XYZ123.BI", True, True), ("XYZ123.I+fb", False, True), ("XYZ123", False, False),
])
def test_weight_and_slant_from_font_names(font, bold, italic):
    span = {"font": font, "flags": 0}
    assert (is_bold(span), is_italic(span)) == (bold, italic)


# A3: named-destination links --------------------------------------------------

def test_named_links_resolve_through_the_name_tree():
    named = {"kind": pymupdf.LINK_NAMED, "nameddest": "sec-4.2"}
    assert refs.link_destination(named, {"sec-4.2": {"page": 1, "to": (54.0, 80.0)}}) == (2, 54.0, 80.0)
    assert refs.link_destination(named, {}) is None
    goto = {"kind": pymupdf.LINK_GOTO, "page": 0, "to": pymupdf.Point(10, 20)}
    assert refs.link_destination(goto, {}) == (1, 10.0, 20.0)


def test_unresolvable_named_links_are_counted(tmp_path):
    doc = doc_of(tmp_path, [syn.heading("1", "General", 80) + syn.prose(100)])
    doc[0].insert_link({"kind": pymupdf.LINK_NAMED, "from": pymupdf.Rect(54, 90, 150, 102), "nameddest": "nowhere"})
    skel = build_skeleton(doc, syn.profile(1))
    found, unresolved = refs.link_references(doc, [1], skel.blocks, skel.seg.owner, skel.ctx.page_lines)
    assert found == [] and unresolved == 1


# A4: footnote marker shape from the profile -----------------------------------

def sup(text, x=60.0):
    return {"text": text, "font": "Helvetica", "size": 6.0, "flags": tc.SUPERSCRIPT_FLAG,
            "origin": (x, 96.0), "bbox": (x, 90.0, x + 6, 97.0)}


def word(text="factors"):
    return {"text": text, "font": "Helvetica", "size": 9.0, "flags": 0, "origin": (20.0, 100.0), "bbox": (20, 91, 59, 101)}


def test_letter_markers_and_marker_lists():
    assert tc.superscript_marker(word(), sup("a,b"), r"[a-z]") == "a,b"
    assert [m for m, _ in tc.superscripts(Line(1, (0, 0, 1, 1), [word(), sup("a,b")]), r"[a-z]")] == ["a", "b"]
    assert tc.superscript_marker(word(), sup("a"), tc.DEFAULT_MARKER) is None  # digits by default
    assert tc.superscript_marker(word(), sup("2")) == "2"
    lettered = FootnoteFormat(line_regex=r"^(?P<marker>[a-z])\s", marker_superscript=True, marker_regex=r"[a-z]")
    line = Line(1, (0, 0, 1, 1), [sup("c"), word("Applies to walls")])
    assert tc.footnote_marker(line, 9.0, lettered) == "c"


# A5: rotated margin text --------------------------------------------------------

def test_rotated_margin_text_is_not_merged_or_kept(tmp_path):
    page = [("Stamp part one", 590, 400, "sans", 6), ("Stamp part two", 598, 400, "sans", 6)] + syn.prose(100)
    pdf = syn.make_pdf(tmp_path / "doc.pdf", [[]])
    doc = pymupdf.open(pdf)
    for text, x, y, kind, size in page:
        doc[0].insert_text((x, y), text, fontname=syn.FONTS[kind], fontsize=size, rotate=90 if kind == "sans" else 0)
    lines = read_lines(doc[0])
    rotated = [ln for ln in lines if not ln.horizontal]
    assert len(rotated) == 2  # two stamp lines, not merged into one
    ctx = load_context(doc, syn.profile(1), [1])
    assert all(ln.horizontal for ln in ctx.page_lines[1]) and len(ctx.page_lines[1]) == 3


# A6: unmapped symbol fonts ------------------------------------------------------

def test_unmapped_font_text_is_flagged_and_left_raw():
    radical = {"text": "pﬃﬃ", "font": "Qx9F12", "size": 10.0, "flags": 0, "origin": (80.0, 100.0), "bbox": (80, 90, 100, 101)}
    text = {"text": "the ﬁeld ", "font": "Times-Roman", "size": 10.0, "flags": 0, "origin": (40.0, 100.0), "bbox": (40, 90, 79, 101)}
    assert tc.text_from_unmapped_font(radical, ["^Qx9F"])
    assert not tc.text_from_unmapped_font(radical, [])
    assert not tc.text_from_unmapped_font({**radical, "text": "≤ ∑"}, ["^Qx9F"])  # symbols, no letters
    line = Line(1, (40, 90, 100, 101), [text, radical])
    assert normalize_line(line, ["^Qx9F"]) == "the field pﬃﬃ"  # ligature kept raw only in the unmapped font
    profile = syn.profile(1, unmapped_fonts=["^Qx9F"])
    assert flag_lines({1: [line]}, [], profile)[0].reasons == ["unmapped_font"]


# A7: a citation that wraps is not a heading -----------------------------------

def test_wrapped_citation_does_not_start_a_heading(tmp_path):
    page = (syn.heading("4.1", "Scope", 80)
            + [("These provisions extend Sections 4.1 through", 54, 100, "body", 10),
               ("4.3", 54, 113, "bold", 11), ("of the reference standard.", 74, 113, "bold", 11)]
            + syn.prose(126) + syn.heading("4.2", "Loads", 180) + syn.prose(200))
    clauses = build_skeleton(doc_of(tmp_path, [page]), _with_body(syn.profile(1))).seg.clauses
    assert [c.id for c in clauses] == ["4.1", "4.2"]


# A8: coverage counts prose pages, not pages -----------------------------------

def test_pages_with_only_a_table_do_not_break_coverage(tmp_path):
    table_only = [(f"{i}.25   {i}.50   {i}.75", 54, 100 + 12 * i, "sans", 8) for i in range(1, 8)]
    pages = [syn.heading("1", "General", 80) + syn.prose(100), table_only, table_only, table_only, syn.prose(100)]
    seg = build_skeleton(doc_of(tmp_path, pages), _with_body(syn.profile(5))).seg
    assert seg.coverage == 1.0


# A9: panel references --------------------------------------------------------

def test_panel_reference_resolves_to_the_figure():
    known = {"Figure 4-2", "Table 4-1"}
    assert refs.resolve_id("Figure 4-2b", known, "figure") == "Figure 4-2"
    assert refs.resolve_id("Figure 4-3b", known, "figure") is None
    assert refs.resolve_id("4.2b", {"4.2"}, "clause") is None  # clause numbers are not panels


# B2: nested (quoted) streams ---------------------------------------------------

def nested_rules():
    data = syn.rules()
    own = data["streams"][0]
    quoted = {**own, "name": "quoted", "id_prefix": "REF:", "nested_in": "provision",
              "levels": [{"depth": d, "style": {"font_regex": "^Helvetica-BoldOblique$", "size_min": 10.5,
                                                 "size_max": 11.5, "bold": True}} for d in (1, 2, 3)]}
    data["streams"] = [own, quoted]
    return data


def test_quoted_sections_hang_under_the_clause_that_quotes_them(tmp_path):
    page = (syn.heading("1", "General", 60) + syn.prose(75, 1) + syn.heading("1.1", "Amendments", 100)
            + [("Replace the following sections of the reference standard.", 54, 115, "body", 10),
               ("7.2", 54, 135, "boldit", 11), ("Quoted Section", 80, 135, "boldit", 11)] + syn.prose(150, 1)
            + [("7.2.1", 54, 175, "boldit", 11), ("Quoted Detail", 92, 175, "boldit", 11),
               ("See Section 7.2 for limits.", 54, 190, "body", 10)]
            + syn.heading("1.2", "Next", 215) + syn.prose(230, 1))
    profile = syn.profile(1, streams=nested_rules()["streams"])
    profile.body_style = BODY
    doc = doc_of(tmp_path, [page])
    report = validate(doc, profile)
    clauses = report.skeleton.seg.by_id()
    assert list(clauses) == ["1", "1.1", "REF:7.2", "REF:7.2.1", "1.2"]
    assert clauses["REF:7.2"].parent_id == "1.1" and clauses["REF:7.2.1"].parent_id == "REF:7.2"
    assert report.validation.order_violations == 0 and report.validation.hierarchy_violations == 0
    targets = refs.Targets(clause=list(clauses), table=[], figure=[], equation=[],
                           clause_depth={k: c.depth for k, c in clauses.items()},
                           clause_namespace={"REF:7.2.1": "REF:"})
    found = refs.regex_references("REF:7.2.1", "See Section 7.2 for limits.", 1, profile, targets)
    assert [r.target_id for r in found] == ["REF:7.2"]


def test_stream_schema_rules():
    data = nested_rules()
    data["streams"][1]["nested_in"] = "missing"
    with pytest.raises(ValidationError):
        ProfileRules.model_validate(data)
    data = nested_rules()
    data["streams"][1]["id_prefix"] = ""  # same ids as the provision stream
    with pytest.raises(ValidationError):
        ProfileRules.model_validate(data)


# B1: unmapped-font patterns are checked; the inventory shows what fonts emit ---

def test_unmapped_font_pattern_must_name_a_real_font(tmp_path):
    doc = doc_of(tmp_path, [syn.heading("1", "General", 80) + syn.prose(100)])
    report = validate(doc, syn.profile(1, unmapped_fonts=["^NoSuchFont$"]))
    assert report.validation.status == "warning"
    assert any("NoSuchFont" in issue for issue in report.validation.issues)
    with pytest.raises(ValidationError):
        ProfileRules.model_validate(syn.rules(unmapped_fonts=["(unclosed"]))


def test_font_inventory_lists_raised_characters(tmp_path):
    page = [("f", 54, 100, "body", 10), ("0", 59, 96, "body", 6), ("c", 63, 100, "body", 10)] + syn.prose(120, 1)
    inventory = font_inventory(doc_of(tmp_path, [page]), [1])
    assert "Times-Roman" in inventory.splitlines()[1]
    assert "[0]" in inventory


# Table notes printed inside the grid box ----------------------------------------

def line_at(text, x, y, size=8.0, font="Helvetica"):
    span = {"text": text, "font": font, "size": size, "flags": 0, "origin": (x, y), "bbox": (x, y - size, x + 6 * len(text), y)}
    return Line(1, span["bbox"], [span])


def test_notes_inside_the_grid_box_become_footnotes():
    from app.ingest.pass1.model import TableSegment
    from app.ingest.pass1.tables import trim_notes
    def table(lines):
        seg = TableSegment(page=1, bbox=(45, 100, 300, 170), rows=[["Wall", "1.5"], ["Slab", "2.0"], ["aApplies"], ["bLinear"]],
                           row_bboxes=[(45, 100, 300, 112), (45, 115, 300, 127), (45, 140, 300, 152), (45, 154, 300, 164)],
                           header_rows=0, col_x=[], method="test")
        is_note = lambda ln: ln.text[0] if ln.text[0] in "ab" and ln.text[1].isupper() else None  # noqa: E731
        trim_notes({1: [seg]}, {1: lines}, is_note, r"[a-z]")
        return seg

    referenced = Line(1, (50, 101, 80, 112), [word("Wall"), sup("a", x=60.0)])
    rows = [line_at("1.5", 200, 110), line_at("Slab", 50, 125), line_at("2.0", 200, 125),
            line_at("aApplies to walls only", 50, 150), line_at("bLinear interpolation", 50, 162)]
    seg = table([referenced] + rows)
    assert seg.rows == [["Wall", "1.5"], ["Slab", "2.0"]]
    assert seg.bbox[3] < 150
    # same lines, but no row or caption refers to a or b: a cell that starts like a note stays
    assert len(table([line_at("Wall", 50, 110)] + rows).rows) == 4


def test_marker_with_a_fragment_is_not_a_footnote():
    lettered = FootnoteFormat(line_regex=r"^(?P<marker>[a-h])(?=[A-Z])", marker_superscript=False, marker_regex=r"[a-h]")
    assert tc.footnote_marker(line_at("cE", 50, 100, size=6.0), 9.0, lettered) is None  # a subscript fragment
    assert tc.footnote_marker(line_at("cConventional beams", 50, 100), 9.0, lettered) == "c"


def test_run_in_split_accepts_any_prose_style():
    from app.ingest.pass1.blocks import split_run_in
    bold = {"font": "Arial-BoldMT", "size": 9.5, "flags": 0}
    upright = {"font": "ArialMT", "size": 9.5, "flags": 0}
    spans = [{**bold, "text": "4.1", "origin": (50, 100), "bbox": (50, 91, 62, 101)},
             {**bold, "text": " Scope.", "origin": (62, 100), "bbox": (62, 91, 95, 101)},
             {**upright, "text": " Replace the section", "origin": (95, 100), "bbox": (95, 91, 190, 101)}]
    line = Line(1, (50, 91, 190, 101), spans)
    head, rest = split_run_in(line, {("ArialMT", 9.5, False)})
    assert head.text.strip() == "4.1 Scope." and rest.text.strip() == "Replace the section"
    assert split_run_in(line, {("Times-Italic", 9.5, False)}) == (line, None)  # not a prose style here


def test_wrapped_run_in_title_stops_at_the_body(tmp_path):
    page = [("4.2.1", 54, 100, "bold", 11), ("Anchors for Walls and", 90, 100, "bold", 11),
            ("Diaphragms.", 54, 113, "bold", 11), ("Anchors shall resist", 125, 113, "body", 10)] + syn.prose(126, lines=3)
    clause = build_skeleton(doc_of(tmp_path, [page]), _with_body(syn.profile(1))).seg.clauses[0]
    assert clause.title == "Anchors for Walls and Diaphragms."
    assert clause.text.startswith("Anchors shall resist") and syn.PROSE in clause.text


def test_each_table_keeps_the_notes_below_it():
    from app.ingest.pass1.model import Block, Table, TableSegment
    from app.ingest.pass1.tables import attach_footnotes

    def table(tid, top, bottom):
        seg = TableSegment(page=1, bbox=(50, top, 550, bottom), rows=[], row_bboxes=[], header_rows=0, col_x=[], method="t")
        return Table(id=tid, number=None, caption="", segments=[seg])

    upper, lower = table("Table 1", 100, 200), table("Table 2", 300, 400)
    notes = [Block("footnote", 1, (50, 210, 300, 220), 0, "a Upper note", marker="a"),
             Block("footnote", 1, (50, 410, 300, 420), 0, "a Lower note", marker="a")]
    attach_footnotes([upper, lower], {1: notes})
    assert [f.text for f in upper.footnotes] == ["a Upper note"]
    assert [f.text for f in lower.footnotes] == ["a Lower note"]
