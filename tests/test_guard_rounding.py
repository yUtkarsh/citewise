"""The number guard accepts a tool-result number rounded to the precision the answer
shows, but only when the rounded number keeps at least three significant digits."""
from __future__ import annotations

from app.agent.guard import significant_digits, unsupported_numbers

SOURCES = ['{"summary": "Equation 3-10: V = 1967.02 kips"}', '{"C_1": 1.14982, "F_a": 1.16, "x": 1995}']


def test_a_rounded_tool_number_is_accepted():
    assert unsupported_numbers("V ≈ 1,967 kips with C1 = 1.15.", SOURCES) == []
    assert unsupported_numbers("V = 1967.0 kips", SOURCES) == []


def test_rounding_that_hides_digits_or_changes_the_value_still_fails():
    assert unsupported_numbers("Fa is 1.2 here", SOURCES) == ["1.2"]  # 1.16 shown with 2 digits
    assert unsupported_numbers("V = 1,968 kips", SOURCES) == ["1,968"]  # not a rounding of 1967.02
    assert unsupported_numbers("about 2,000", SOURCES) == ["2,000"]  # 1995 rounded to one digit


def test_significant_digits_as_written():
    assert [significant_digits(t) for t in ("1,967", "1.15", "1.2", "2,000", "0.0450", "-12.5")] == [4, 3, 2, 1, 3, 3]
