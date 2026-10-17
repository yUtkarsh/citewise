"""Self-consistency checks between a profile and its document, on synthetic text."""
import pymupdf

from app.profile.consistency import DEFINITION, id_mismatches, reference_share, where_share
from app.profile.validate import validate
from tests import synthetic as syn


def where_page():
    return (syn.heading("1", "General", 60) + syn.prose(75, 1)
            + [("where", 54, 110, "body", 10), ("Mu = factored moment", 54, 123, "body", 10),
               ("α2 = angle factor", 54, 136, "body", 10), ("δmax = largest drift", 54, 149, "body", 10),
               ("f'm = masonry strength", 54, 162, "body", 10)] + syn.prose(185, 2))


def test_where_item_pattern_must_accept_the_definition_lines(tmp_path):
    doc = pymupdf.open(syn.make_pdf(tmp_path / "w.pdf", [where_page()]))
    ascii_only = syn.rules()["where_block"] | {"item_regex": r"^(?P<symbol>[A-Za-z]+)\s*=\s*(?P<definition>.+)"}
    narrow = validate(doc, syn.profile(1, where_block=ascii_only))
    assert narrow.validation.status == "warning"
    assert narrow.validation.checks["where_items"] == 0.25
    assert any("rejected definition line" in i for i in narrow.validation.issues)
    broad = validate(doc, syn.profile(1))
    assert broad.validation.checks["where_items"] == 1.0


def test_definition_detector_is_generic():
    assert DEFINITION.match("Mu = factored moment") and DEFINITION.match("ln(x) = log of x")
    assert not DEFINITION.match("Note: A = area")  # a label, not a symbol
    assert not DEFINITION.match("= 1.0 for short periods")  # a continuation


def test_reference_patterns_must_cover_mentions():
    profile = syn.profile(1)  # cites "Section", "Table", "Figure", "Equation"
    text = ("Apply Clause 4.2.1 and Clause 4.3. See Clause 5.1 and Section 2.1. "
            "The factor of 1.5 applies; Clause 6.2.2 governs. Use Table 4-2 for loads. "
            "The weight of the roof, the area of the wall and the use of the building are inputs.")
    share, uncovered = reference_share(text, profile)
    assert share.total == 6 and share.matched == 2  # four Clause mentions are not covered
    assert all(u.startswith("Clause") for u in uncovered)  # "of 1.5" is not a mention
    profile.references[0].words += ["Clause"]
    assert reference_share(text, profile)[0].share == 1.0


def test_section_sign_counts_as_a_reference_word():
    profile = syn.profile(1)
    share, uncovered = reference_share("as required by § 4.2 and § 5.1.3", profile)
    assert share.total == 2 and share.matched == 0
    profile.references[0].words += ["§"]


def test_ids_must_agree_with_printed_numbers(tmp_path):
    doc = pymupdf.open(syn.make_pdf(tmp_path / "i.pdf", [where_page()]))
    good = validate(doc, syn.profile(1))
    assert good.validation.checks["ids"] == 1.0
    streams = syn.rules()["streams"]
    streams[0]["id_prefix"] = "P"  # a namespace that is not printed, on a stream that is not nested
    bad = validate(doc, syn.profile(1, streams=streams))
    assert bad.validation.checks["ids"] == 0.0 and bad.validation.status == "warning"
    share, examples = id_mismatches(bad.skeleton, bad.skeleton.ctx.profile)
    assert share.total == 1 and "not printed" in examples[0]


def two_level_page():
    return (syn.heading("1", "General", 60) + syn.prose(75, 1) + syn.heading("1.1", "Scope", 100) + syn.prose(115, 1)
            + syn.heading("1.1.1", "Detail", 140, size=10) + syn.prose(155, 1)
            + syn.heading("1.1.2", "More", 180, size=10) + syn.prose(195, 1))


def test_style_confirms_heading_and_number_gives_depth(tmp_path):
    rules = syn.rules()
    rules["streams"][0]["levels"] = [{"depth": 1, "style": {"font_regex": "^Helvetica-Bold$", "size_min": 9.5,
                                                            "size_max": 11.5, "bold": True}}]
    doc = pymupdf.open(syn.make_pdf(tmp_path / "l.pdf", [two_level_page()]))
    report = validate(doc, syn.profile(1, streams=rules["streams"]))
    clauses = report.skeleton.seg.clauses
    assert [(c.id, c.depth) for c in clauses] == [("1", 1), ("1.1", 2), ("1.1.1", 3), ("1.1.2", 3)]
    assert report.validation.checks["levels"] == 0.25  # depths 2 and 3 are not declared
    assert report.validation.status == "warning"
    assert any("add a level" in i for i in report.validation.issues)


def test_complete_levels_pass(tmp_path):
    doc = pymupdf.open(syn.make_pdf(tmp_path / "l.pdf", [two_level_page()]))
    rules = syn.rules()
    for level in rules["streams"][0]["levels"]:
        level["style"]["size_min"] = 9.5
    report = validate(doc, syn.profile(1, streams=rules["streams"]))
    assert report.validation.checks["levels"] == 1.0
    assert len(report.skeleton.seg.clauses) == 4


def test_declared_depth_keeps_its_own_style(tmp_path):
    """A bold numbered list item set like a deeper heading is not a heading at a depth
    whose own style differs."""
    page = (syn.heading("1", "General", 60, size=11) + syn.prose(75, 1)
            + [("2.", 54, 100, "bold", 10), ("Liquefaction.", 70, 100, "bold", 10)] + syn.prose(115, 1)
            + syn.heading("1.1", "Scope", 140, size=10) + syn.prose(155, 1))
    rules = syn.rules()
    rules["streams"][0]["levels"] = [
        {"depth": 1, "style": {"font_regex": "^Helvetica-Bold$", "size_min": 10.8, "size_max": 11.2, "bold": True}},
        {"depth": 2, "style": {"font_regex": "^Helvetica-Bold$", "size_min": 9.8, "size_max": 10.2, "bold": True}}]
    doc = pymupdf.open(syn.make_pdf(tmp_path / "d.pdf", [page]))
    clauses = validate(doc, syn.profile(1, streams=rules["streams"])).skeleton.seg.clauses
    assert [c.id for c in clauses] == ["1", "1.1"]
