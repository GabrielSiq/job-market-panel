"""Location parsing.

Every ATS row had `country = None` — only the aggregator ever set it — so "is this open to
US candidates" was unanswerable, despite US-eligibility being one of the few genuinely hard
filters in this search.

Every string below appears verbatim in the collected panel.
"""

from __future__ import annotations

import pytest

from src.location import parse_location


@pytest.mark.parametrize(
    ("raw", "country", "region"),
    [
        ("Costa Mesa, California, United States", "US", "CA"),
        ("Hawthorne, CA", "US", "CA"),
        ("Bastrop, TX", "US", "TX"),
        ("Starbase, TX", "US", "TX"),
        ("New York, NY", "US", "NY"),
        ("United States", "US", None),
        ("Remote - USA", "US", None),
        ("Remote - US", "US", None),
        ("London, United Kingdom", "GB", None),
        ("Toronto, Canada", "CA", None),
        ("Ireland", "IE", None),
        ("Anywhere", None, None),
        ("Remote", None, None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_country_and_region(raw, country, region):
    finding = parse_location(raw)
    assert finding.country == country
    assert finding.region == region


def test_parses_right_to_left():
    """Location strings are right-anchored: city, then region, then country.

    Reading left-to-right breaks on "Washington, District of Columbia" — the leading token
    is the *city* Washington, but a naive scan matches the *state* Washington and lands on
    the wrong side of the country.
    """
    finding = parse_location("Washington, District of Columbia, United States")
    assert (finding.country, finding.region) == ("US", "DC")
    assert finding.names_a_place


class TestNamesAPlace:
    """`names_a_place` is what licenses treating a posting as onsite, so the distinction
    between "a city" and "a country" has to be exact."""

    @pytest.mark.parametrize(
        "raw", ["Hawthorne, CA", "Starbase, TX", "San Francisco", "London, United Kingdom"]
    )
    def test_a_settlement_is_a_place(self, raw):
        assert parse_location(raw).names_a_place

    @pytest.mark.parametrize(
        "raw", ["United States", "Ireland", "Remote", "Anywhere", "Remote - USA", ""]
    )
    def test_a_country_or_a_placeless_word_is_not(self, raw):
        assert not parse_location(raw).names_a_place

    def test_multi_location_detected(self):
        finding = parse_location("Southaven, MS; Memphis, TN")
        assert finding.is_multi
        assert finding.country == "US"


class TestImpliedOnsite:
    def test_named_place_implies_onsite(self, classifier):
        finding = classifier.remote_implied_by_place("Hawthorne, CA")
        assert finding.is_remote is False
        assert finding.remote_source.value == "location_implied"

    def test_country_only_stays_unknown(self, classifier):
        """A bare "United States" says nothing about where the work happens, so guessing
        onsite there would be inventing data rather than inferring it."""
        finding = classifier.remote_implied_by_place("United States")
        assert finding.is_remote is None
        assert finding.remote_source.value == "unknown"

    def test_provenance_is_separable(self, classifier):
        """This is the weakest inference in the chain (~95%), so a remote-share metric
        must be able to exclude it. It never borrows LOCATION_STRING's provenance."""
        assert classifier.remote_implied_by_place("Austin, TX").remote_source.value == (
            "location_implied"
        )
        assert classifier.remote_from_location("Remote - USA").remote_source.value == (
            "location_string"
        )
