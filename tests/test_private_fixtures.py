"""The private_fixture helper skips instead of failing when licensed pages are absent."""
import pytest

from scripts import font_check


def test_missing_private_fixture_skips(private_fixture):
    with pytest.raises(pytest.skip.Exception):
        private_fixture("does_not_exist.pdf")


@pytest.mark.parametrize("name", ["asce41_page.pdf"])
def test_private_page_font_check(private_fixture, name):
    report = font_check.scan_pdf(private_fixture(name))
    assert report.page_count >= 1
    assert report.fonts
