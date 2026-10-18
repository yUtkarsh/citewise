"""Pass 2 inputs: notation slices, term links, clause crops per CLAUSE_CROPS, cache keys.
Never calls the API."""
import pymupdf
import pytest

from app.ingest.pass1.run_pass1 import run_pass1
from app.ingest.pass2 import images as img
from app.ingest.pass2.jobs import clause_job, is_trivial, table_jobs, table_parts
from app.ingest.pass2.notation import NotationDict, link_terms, term_pattern
from app.ingest.pass2.records import RowRec, TableRec, from_pass1
from app.ingest.pass2.schemas import NotationEntry, TermEntry
from app.profile.page_map import build_page_map
from tests.conftest import FIXTURES


def entry(id, display, aliases=(), scope=()):
    return NotationEntry(display=display, latex=display, id=id, description=id, units=None, defined_in=[],
                         aliases=list(aliases), scope_chapters=list(scope), source_quote=id)


def skeleton(name, profile, tmp_path):
    doc = pymupdf.open(FIXTURES / name)
    result = run_pass1(doc, FIXTURES / name, "hash", profile, build_page_map(doc), tmp_path / "cache",
                       tmp_path / "crops", use_docling=False, pages=[1], log=lambda *_: None)
    return doc, from_pass1(result, "test")


# ---- notation slice ------------------------------------------------------------------------

def test_slice_holds_only_symbols_in_the_text():
    d = NotationDict()
    d.add(1, [entry("V_d", "Vd", aliases=["V d"]), entry("Q_CE", "QCE"), entry("k", "k")])
    got = [e.id for e in d.slice("The shear V d is limited.", None)]
    assert got == ["V_d"]


def test_single_letter_symbols_need_equation_or_where_evidence():
    d = NotationDict()
    d.add(1, [entry("k", "k")])
    assert not d.slice("a k factor", None)
    assert [e.id for e in d.slice("a k factor", None, extra_symbols={"k"})] == ["k"]


def test_scoped_meanings_follow_the_chapter():
    d = NotationDict()
    d.add(1, [entry("C_1", "C1", scope=["3"]), entry("C_1_p9", "C1", scope=["9"])])
    assert [e.id for e in d.slice("uses C1 here", "9")] == ["C_1_p9"]


# ---- terms -------------------------------------------------------------------------------------

def term(t, aliases=()):
    return TermEntry(term=t, definition="d", aliases=list(aliases), source_quote="d")


def test_multi_word_terms_match_any_case_single_words_only_capitalized():
    assert term_pattern("lateral force").search("The LATERAL  Force acts")
    single = term_pattern("component")
    assert single.search("each Component shall") and not single.search("each component shall")


def test_term_links_are_capped_and_logged():
    logs = []
    clauses = [(f"c{i}", "applies to the Building") for i in range(60)]
    links = link_terms([term("building")], clauses, log=logs.append, cap=50)
    assert len(links["building"]) == 50 and links["building"][0] == "c0"
    assert logs and "60 clauses" in logs[0]


# ---- clause jobs and crops -----------------------------------------------------------------------

@pytest.mark.parametrize("name", ["text.pdf", "equations.pdf", "equations_brackets.pdf"])
def test_always_mode_crops_each_region_and_flagged_mode_only_flagged_lines(fema_profile, tmp_path, name):
    doc, skel = skeleton(name, fema_profile, tmp_path)
    before = set(tmp_path.rglob("*.png"))
    for c in skel.clauses:
        always = clause_job(c, skel, NotationDict(), "hash", "always")
        flagged = clause_job(c, skel, NotationDict(), "hash", "flagged")
        regions = [i for i in always.images if i.label.startswith("clause region")]
        assert len(regions) == len(c.regions)
        lines = [i for i in flagged.images if i.label.startswith("flagged line")]
        assert len(lines) == len(c.flagged)
        assert not [i for i in flagged.images if i.label.startswith("clause region")]
        for ref in regions + lines:
            assert ref.png(doc).startswith(b"\x89PNG")
        if regions != lines:
            assert always.cache_text() != flagged.cache_text()
    assert set(tmp_path.rglob("*.png")) == before  # rendering never writes a file


def test_bracket_page_flagged_lines_get_line_crops(fema_profile, tmp_path):
    doc, skel = skeleton("equations_brackets.pdf", fema_profile, tmp_path)
    flagged = [c for c in skel.clauses if c.flagged]
    assert flagged
    job = clause_job(flagged[0], skel, NotationDict(), "hash", "flagged")
    assert any(i.label.startswith("flagged line") for i in job.images)


def test_clause_job_text_has_every_input(fema_profile, tmp_path):
    import json
    _, skel = skeleton("equations.pdf", fema_profile, tmp_path)
    c = next(c for c in skel.clauses if c.equations)
    payload = json.loads(clause_job(c, skel, NotationDict(), "hash", "always").text)
    assert {"breadcrumb", "clause", "where_block", "notation", "references", "equations", "suspect_text",
            "flagged_lines"} <= payload.keys()
    assert [e["id"] for e in payload["equations"]] == [e.id for e in c.equations]


def test_cache_key_moves_with_the_region():
    a = img.region("hash", 3, (10, 10, 200, 300), "clause region, page 3")
    b = img.region("hash", 3, (10, 10, 200, 301), "clause region, page 3")
    assert a.key != b.key


def test_bad_crop_mode_is_refused(fema_profile, tmp_path):
    _, skel = skeleton("text.pdf", fema_profile, tmp_path)
    with pytest.raises(ValueError):
        img.clause_images(skel.clauses[0], "hash", "sometimes")


def test_trivial_clauses_need_no_call(fema_profile, tmp_path):
    _, skel = skeleton("text.pdf", fema_profile, tmp_path)
    assert all(not is_trivial(c) or (len(c.text) < 40 and not c.equations) for c in skel.clauses)


# ---- tables --------------------------------------------------------------------------------------

def test_large_tables_split_by_page_with_headers_repeated():
    rows = [RowRec(0, 1, (0, 0, 1, 1), ["h"], True, "docling")]
    rows += [RowRec(i, 1 + i // 50, (0, 0, 1, 1), [str(i)], False, "docling") for i in range(1, 101)]
    t = TableRec(id="T", number="1", caption="c", pages=[1, 2, 3], crops=[], header_crop=None, rows=rows,
                 footnotes=[], clause_id=None)
    parts = table_parts(t)
    assert len(parts) == 3 and all(p[0].is_header for p in parts)
    assert sorted(r.index for p in parts for r in p if not r.is_header) == list(range(1, 101))


def test_table_job_sends_grid_caption_footnotes_and_crops(fema_profile, tmp_path):
    import json
    _, skel = skeleton("table.pdf", fema_profile, tmp_path)
    if not skel.tables:
        pytest.skip("no table found on the fixture page")
    t = skel.tables[0]
    job = table_jobs(t, skel, NotationDict())[0]
    payload = json.loads(job.text)
    assert payload["table"]["caption"] == t.caption
    assert [g["row"] for g in payload["grid"]] == [r.index for r in t.rows]
    assert len([i for i in job.images if i.label.startswith("table crop")]) == len(t.crops)
