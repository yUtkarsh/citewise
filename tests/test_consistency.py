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
