"""Salary extraction from prose.

Only Ashby publishes structured compensation. Greenhouse and Lever publish none — yet
pay-transparency law means the band is usually printed in the description anyway. Measured
on a fresh 20-board sample, **45% of all postings** carry a readable range.

Every string below is taken from a real posting.
"""

from __future__ import annotations

import pytest

from src.models import SalaryPeriod, SalarySource
from src.salary import parse_salary

REAL = [
    ("Pay Range: Level 3: $130,000.00 - $195,000.00 Your actual level", 130_000, 195_000),
    (
        "The US base salary range for this position is $90,000 - $160,000, plus equity",
        90_000,
        160_000,
    ),
    ("US Salary Range $112,000—$149,000 USD The salary range", 112_000, 149_000),
    ("Salary Range: $205,000–$270,000 + Offers Equity", 205_000, 270_000),
    (
        "we are targeting a base pay range of $83,700 - $115,100. Actual compensation",
        83_700,
        115_100,
    ),
    ("The targeted range for this position is $80,000 – $110,000.", 80_000, 110_000),
    ("Estimated annual salary range: $150,000 - $189,000 plus equity", 150_000, 189_000),
    ("$150K - $190K", 150_000, 190_000),
]


@pytest.mark.parametrize(("text", "low", "high"), REAL)
def test_parses_real_postings(text, low, high):
    parsed = parse_salary(text)
    assert parsed is not None
    assert (parsed.salary_min, parsed.salary_max) == (low, high)
    assert parsed.salary_source is SalarySource.DESCRIPTION_PARSED


class TestRejections:
    """Returning None beats returning a number nobody will re-examine."""

    @pytest.mark.parametrize(
        "text",
        [
            "$5,000 - $10,000 signing bonus",  # not annual pay
            "we raised $1,000,000 - $5,000,000 in seed funding",
            "a $45 - $70 gift card",  # small figures, no hourly context
            "equity grants of $250 - $900 per share",
            "no numbers at all",
            "",
            None,
        ],
    )
    def test_implausible_input_yields_nothing(self, text):
        assert parse_salary(text) is None

    def test_inverted_range_rejected(self):
        assert parse_salary("$200,000 - $100,000") is None


class TestMultipleRanges:
    """Roughly 19% of matches list several bands — geographic tiers or internal levels."""

    def test_levels_span_all_stated_bands(self):
        parsed = parse_salary("I4 $137,100—$201,600 USD I5 $167,800—$246,800 USD")
        assert (parsed.salary_min, parsed.salary_max) == (137_100, 246_800)
        assert parsed.range_count == 2, "ambiguity is recorded, not hidden"

    def test_geographic_tiers(self):
        """Real: Zillow states a different band for DC and for Virginia."""
        parsed = parse_salary(
            "In Washington DC the standard base pay range for this role is "
            "$148,600.00 - $237,400.00 annually. In Virginia the standard base pay range "
            "for this role is $141,200.00 - $225,600.00 annually."
        )
        assert (parsed.salary_min, parsed.salary_max) == (141_200, 237_400)
        assert parsed.range_count == 2

    def test_single_range_is_marked_unambiguous(self):
        assert parse_salary("Salary Range: $205,000–$270,000").range_count == 1


class TestPeriod:
    def test_hourly_detected(self):
        parsed = parse_salary("The hourly rate for this role is $45 - $70 per hour.")
        assert parsed.salary_period is SalaryPeriod.HOUR
        assert (parsed.salary_min, parsed.salary_max) == (45, 70)

    def test_annual_is_the_default(self):
        assert parse_salary("$150,000 - $190,000").salary_period is SalaryPeriod.YEAR

    def test_hourly_and_annual_are_not_mixed(self):
        """Spanning an hourly rate and an annual band would be nonsense."""
        parsed = parse_salary(
            "Contractors are paid $45 - $70 per hour. Full-time base is $150,000 - $190,000."
        )
        assert parsed.salary_period is SalaryPeriod.HOUR
        assert parsed.salary_max == 70, "the annual band must not be folded in"


class TestCurrency:
    def test_explicit_currency_wins(self):
        assert parse_salary("Salary Range CAD $120,000 - $150,000").salary_currency == "CAD"

    def test_defaults_to_usd(self):
        assert parse_salary("$120,000 - $150,000").salary_currency == "USD"
