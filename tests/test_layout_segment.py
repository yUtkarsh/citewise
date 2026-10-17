"""Steps 3a.2-3a.3 on fixture pages: running elements, reading order, clauses."""
import pymupdf
import pytest

from app.ingest.pass1.blocks import load_context, page_blocks
from app.ingest.pass1.layout import join_lines, normalize, zone
from app.ingest.pass1.segment import number_key
from app.ingest.pass1.skeleton import build_skeleton
from tests.conftest import FIXTURES


def context(name, profile):
    doc = pymupdf.open(FIXTURES / name)
    return doc, load_context(doc, profile, [1])


def test_running_elements_are_stripped(fema_profile):
    doc, ctx = context("commentary.pdf", fema_profile)
    height = doc[0].rect.height
    assert ctx.page_lines[1]
    assert all(zone(ln, height) is None for ln in ctx.page_lines[1])


def test_left_column_is_read_before_right_column(fema_profile):
    _, ctx = context("commentary.pdf", fema_profile)
    blocks = page_blocks(1, ctx)
    cols = [b.col for b in blocks if b.col in (0, 1)]
    assert 0 in cols and 1 in cols
    assert cols == sorted(cols)  # no full-width block on this page, so one band
    for col in (0, 1):
        ys = [b.bbox[1] for b in blocks if b.col == col]
        assert ys == sorted(ys)


def test_dehyphenation_keeps_unknown_compounds():
    vocab = {"requirement", "lowerbound"}
    assert join_lines(["the require-", "ment applies"], vocab) == "the requirement applies"
    assert join_lines(["lower-", "bound values"], vocab) == "lowerbound values"
    assert join_lines(["two-", "way slab"], vocab) == "two-way slab"


def test_normalize_ligatures_minus_and_primes():
    assert normalize("ﬁeld −3 f′c") == "field -3 f'c"


@pytest.mark.parametrize("name", ["commentary.pdf", "text.pdf", "equations.pdf"])
def test_clauses_follow_the_numbering(fema_profile, name):
    doc = pymupdf.open(FIXTURES / name)
    skel = build_skeleton(doc, fema_profile, pages=[1])
    clauses = skel.seg.clauses
    assert clauses
    streams = {s.name: s for s in fema_profile.streams}
    by_stream = {}
    for c in clauses:
        by_stream.setdefault(c.stream, []).append(number_key(c.number, streams[c.stream].separator))
        assert c.id == streams[c.stream].prefix + c.number
        if c.parent_id:  # the parent is an ancestor number, in this stream or its provision stream
            assert c.number.startswith(c.parent_id.removeprefix(streams[c.stream].prefix) + ".")
        if c.commentary_on:
            assert c.commentary_on == c.number
    for keys in by_stream.values():
        assert keys == sorted(keys)


def test_commentary_links_to_its_provision(fema_profile):
    doc = pymupdf.open(FIXTURES / "commentary.pdf")
    clauses = build_skeleton(doc, fema_profile, pages=[1]).seg.by_id()
    linked = [c for c in clauses.values() if c.commentary_on in clauses]
    assert linked
    for c in linked:
        assert clauses[c.commentary_on].kind == "provision" and c.kind == "commentary"


@pytest.mark.parametrize("name", ["commentary.pdf", "text.pdf"])
def test_prose_after_the_first_heading_belongs_to_a_clause(fema_profile, name):
    doc = pymupdf.open(FIXTURES / name)
    skel = build_skeleton(doc, fema_profile, pages=[1])
    blocks = skel.blocks[1]
    first = next(i for i, b in enumerate(blocks) if b.kind == "heading")
    assert all(skel.seg.owner[id(b)] is not None for b in blocks[first:])
    assert all(skel.seg.owner[id(b)] is None for b in blocks[:first])  # continues an earlier clause
    assert skel.seg.coverage > 0.5
