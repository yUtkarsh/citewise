"""Document-independent properties on a licensed excerpt that only exists locally.

Skips when private_fixtures/asce41_sample.pdf or its saved profile (built once by
`python -m app.ingest.run private_fixtures/asce41_sample.pdf`, kept in data/) is
missing. Never calls the API."""
import pymupdf
import pytest

from app.ingest.pass1.skeleton import build_skeleton
from app.ingest.pass1.warnings import flag_lines
from app.profile.build import file_hash, load_profile, profile_path

MAX_TITLE_WORDS = 30


@pytest.fixture
def excerpt(private_fixture):
    pdf = private_fixture("asce41_sample.pdf")
    path = profile_path(file_hash(pdf))
    if not path.is_file():
        pytest.skip(f"no saved profile for the excerpt ({path.name}); build it with app.ingest.run")
    doc = pymupdf.open(pdf)
    profile = load_profile(path)
    return doc, profile, build_skeleton(doc, profile)


def test_titles_are_titles(excerpt):
    _, _, skel = excerpt
    long = [(c.id, len(c.title.split())) for c in skel.seg.clauses if len(c.title.split()) > MAX_TITLE_WORDS]
    assert not long


def test_no_rotated_margin_text_in_the_text_flow(excerpt):
    _, _, skel = excerpt
    assert all(ln.horizontal for lines in skel.ctx.page_lines.values() for ln in lines)


def test_every_nested_clause_has_a_parent(excerpt):
    _, profile, skel = excerpt
    nested = {s.name for s in profile.streams if s.nested_in}
    assert all(c.parent_id for c in skel.seg.clauses if c.stream in nested)


def test_unmapped_fonts_are_flagged(excerpt):
    _, profile, skel = excerpt
    if not profile.unmapped_fonts:
        pytest.skip("the profile lists no unmapped fonts")
    flagged = flag_lines(skel.ctx.page_lines, [], profile)
    assert any("unmapped_font" in f.reasons for f in flagged)
