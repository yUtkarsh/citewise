"""Shared test fixtures."""
from pathlib import Path
from typing import Callable

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
PRIVATE_FIXTURES = ROOT / "private_fixtures"


@pytest.fixture(scope="session")
def fema_profile():
    """FEMA 356's profile, generated once from the full PDF by Claude and committed.
    Fixture pages are single pages, so they cannot produce a profile themselves."""
    from app.profile.schema import Profile

    return Profile.model_validate_json((FIXTURES / "fema356_profile.json").read_text(encoding="utf-8"))


@pytest.fixture
def combined_pdf(tmp_path: Path) -> Callable[..., Path]:
    """Join fixture pages into one temporary PDF (page order as given)."""

    def make(*names: str) -> Path:
        out = pymupdf.open()
        for name in names:
            with pymupdf.open(FIXTURES / name) as src:
                out.insert_pdf(src)
        path = tmp_path / ("_".join(Path(n).stem for n in names) + ".pdf")
        out.save(path)
        out.close()
        return path

    return make


@pytest.fixture
def private_fixture() -> Callable[[str], Path]:
    """Return a path in private_fixtures/, skipping the test if the file is missing.

    Licensed pages are git-ignored, so they exist only on machines that have them:
        def test_asce_page(private_fixture):
            pdf = private_fixture("asce41_page.pdf")
    """

    def get(name: str) -> Path:
        path = PRIVATE_FIXTURES / name
        if not path.is_file():
            pytest.skip(f"private fixture not available: private_fixtures/{name}")
        return path

    return get
