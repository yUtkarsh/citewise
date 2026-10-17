"""Shared test fixtures."""
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
PRIVATE_FIXTURES = ROOT / "private_fixtures"


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
