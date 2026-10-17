"""Step 2.4 validation and the build loop (one revision, then a warning), no API."""
import json

import pymupdf
import pytest

from app.profile import build
from app.profile.page_map import build_page_map
from app.profile.schema import RangeLabels
from app.profile.validate import validate
from tests import synthetic as syn


def five_page_doc(tmp_path, numbers=("1", "1.1", "1.2", "1.3", "2")):
    pages = []
    for num in numbers:
        pages.append(syn.heading(num, f"Topic {num}", 80) + syn.prose(100, lines=20))
    return syn.make_pdf(tmp_path / "doc.pdf", pages)


def test_clean_document_passes(tmp_path):
    pdf = five_page_doc(tmp_path)
    with pymupdf.open(pdf) as doc:
        report = validate(doc, syn.profile(5))
    v = report.validation
    assert v.status == "pass" and v.heading_count == 5 and v.coverage == 1.0
    assert report.failures == ""


def test_order_and_hierarchy_problems_are_found(tmp_path):
    pdf = five_page_doc(tmp_path, numbers=("1", "1.3", "1.2", "3.1.1", "3.1.2"))
    with pymupdf.open(pdf) as doc:
        v = validate(doc, syn.profile(5)).validation
    assert v.order_violations == 1  # 1.2 after 1.3
    assert v.hierarchy_violations == 2  # 3.1.1 and 3.1.2 have no 3.1
    assert any("1.2" in issue for issue in v.issues)


def test_missed_headings_lower_coverage_and_are_reported(tmp_path):
    pdf = five_page_doc(tmp_path)
    narrow = syn.rules()
    for level in narrow["streams"][0]["levels"]:
        level["style"]["size_min"], level["style"]["size_max"] = 13.0, 14.0  # wrong size: nothing matches
    with pymupdf.open(pdf) as doc:
        report = validate(doc, syn.profile(5, streams=narrow["streams"]))
    assert report.validation.status == "warning" and report.validation.coverage == 0.0
    assert report.validation.low_coverage_ranges == [(1, 5)]
    assert "[Helvetica-Bold 11.0 bold] 1 Topic 1" in report.failures


class FakeClaude:
    """Answers the three profile prompts; the first rule proposal misses every heading."""

    def __init__(self, revised_ok: bool):
        self.revised_ok = revised_ok
        self.calls = []

    def __call__(self, model, system, user_text, output):
        self.calls.append(system.splitlines()[0])
        if output is RangeLabels:
            return json.dumps({"ranges": [{"start": 1, "end": 5, "kind": "body"}],
                               "sample_pages": [1, 2, 3, 4], "title_pages": [1], "notes": ""})
        data = syn.rules()
        good = "revise" in system and self.revised_ok
        if not good:
            for level in data["streams"][0]["levels"]:
                level["style"]["size_min"], level["style"]["size_max"] = 13.0, 14.0
        return json.dumps(data)


@pytest.mark.parametrize("revised_ok, status", [(True, "pass"), (False, "warning")])
def test_build_revises_once_then_proceeds(tmp_path, monkeypatch, revised_ok, status):
    monkeypatch.setattr(build, "require_model", lambda name: "fake-model")
    pdf = five_page_doc(tmp_path)
    fake = FakeClaude(revised_ok)
    with pymupdf.open(pdf) as doc:
        profile, _ = build.build_profile(doc, build_page_map(doc), transport=fake,
                                         cache_dir=tmp_path / "cache", log=lambda *_: None)
    assert len(fake.calls) == 3  # ranges, rules, one revision
    assert profile.validation.status == status
    assert profile.validation.revised is revised_ok
    assert profile.ranges[0].kind == "body"
