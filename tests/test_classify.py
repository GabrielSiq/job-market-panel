"""Classification is asserted against REAL titles pulled from the live feeds.

This file exists because of a bug it would have caught. The ds_manager rules originally
matched `\\b(data scien|...)\\b`; that trailing word boundary can never match "data
science", because the next character is "c". "Data Science Manager" therefore classified
as product_ds, and the DS-manager count - the single highest-value output of the whole
panel - would have read zero forever while looking entirely plausible.

The lesson generalizes: a broken classifier does not raise, it produces a believable
number. Every taxonomy change must be checked against titles that actually occur.
"""

from __future__ import annotations

import pytest

from src.models import RemoteSource

# (title, expected role_family, expected seniority)
REAL_TITLES = [
    # The growth signal. These are the ones that must never regress.
    ("Data Science Manager", "ds_manager", "manager"),
    ("Manager, Data Science", "ds_manager", "manager"),
    ("Senior Manager, Data Science", "ds_manager", "manager"),
    ("Head of Data Science", "ds_manager", "director"),
    ("Director, Data Science", "ds_manager", "director"),
    ("Senior Director, Revenue Analytics", "ds_manager", "director"),
    (
        "Senior Director, Data Science: Small Commercial (SC) Product Design",
        "ds_manager",
        "director",
    ),
    # The target archetype.
    ("Senior Product Data Scientist", "product_ds", "senior"),
    ("Senior Data Scientist", "product_ds", "senior"),
    ("Data Scientist", "product_ds", "mid"),
    ("Principal Data Scientist", "product_ds", "principal"),
    ("Data Scientist, Finance", "product_ds", "mid"),
    ("Lead, Advanced Analytics, Acquisition", "product_ds", "senior"),
    ("Actuarial Data Scientist-Remote", "product_ds", "mid"),
    # Adjacent families, needed as denominators and for the title-mix metric.
    ("Staff Machine Learning Engineer", "ml_eng", "staff"),
    ("AI Applied Scientist", "ml_eng", "mid"),
    ("Senior ML Specialist", "ml_eng", "senior"),
    ("Analytics Engineer", "analytics_eng", "mid"),
    ("Senior Data Engineer", "data_eng", "senior"),
    ("Data Analyst", "analyst", "mid"),
    ("Senior Revenue Analytics Analyst", "analyst", "senior"),
    # Must NOT be counted as data roles. Each of these really occurs in the DS feed.
    ("Senior Backend Engineer, Analytics Instrumentation (Golang)", "other", "senior"),
    ("Epidemiologist, Internal Medicine, Inflammation & Immunology", "other", "mid"),
    ("Principal Scientist, Project Toxicologist", "other", "principal"),
    ("Sr. Manager, Clinical Science", "other", "manager"),
    ("Product Manager, Growth", "other", "manager"),
    ("Predictive Modeler / Pricing Modeler - Commercial Actuarial", "other", "mid"),
    # Junior detection, so entry-level reqs do not inflate the senior series.
    ("Intern - Data Scientist", "product_ds", "junior"),
    ("Data Scientist, Core Data -  PhD (2026)", "product_ds", "junior"),
]


@pytest.mark.parametrize(("title", "family", "seniority"), REAL_TITLES)
def test_classifies_real_titles(classifier, title, family, seniority):
    assert classifier.role_family(title).value == family
    assert classifier.seniority(title).value == seniority


def test_ds_manager_beats_product_ds(classifier):
    """Order is load-bearing: "Data Science Manager" contains "data scien", so a
    product_ds rule evaluated first would swallow every manager req."""
    assert classifier.role_family("Data Science Manager").value == "ds_manager"
    assert classifier.role_family("Senior Data Scientist").value == "product_ds"


def test_title_whitespace_variants_agree(classifier):
    """Greenhouse emits 'Senior Data Scientist ' and 'Senior Data Scientist' as separate
    values on the same board. They must not become two different rows."""
    assert classifier.role_family("Senior Data Scientist ") == classifier.role_family(
        "Senior Data Scientist"
    )


class TestRemoteInference:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            ("Remote - USA", True),
            ("US Remote", True),
            ("Anywhere", True),
            ("Remote (Hybrid - 3 days in office)", False),  # negative vetoes positive
            ("Hybrid - New York", False),
            ("Onsite - Austin, TX", False),
        ],
    )
    def test_decisive_locations(self, classifier, location, expected):
        assert classifier.remote_from_location(location).is_remote is expected

    @pytest.mark.parametrize(
        "location", ["United States", "United States ", "London, United Kingdom", "", None]
    )
    def test_ambiguous_location_is_none_not_false(self, classifier, location):
        """Tri-state on purpose. A bare "United States" says nothing about remote status,
        and guessing False would inflate the onsite share and corrupt the denominator."""
        finding = classifier.remote_from_location(location)
        assert finding.is_remote is None
        assert finding.remote_source is RemoteSource.UNKNOWN

    def test_negative_beats_positive(self, classifier):
        assert (
            classifier.remote_from_location("Remote", "3 days per week in office").is_remote
            is False
        )

    @pytest.mark.parametrize(
        ("value", "expected"), [("Remote", True), ("Hybrid", False), ("Onsite", False)]
    )
    def test_workplace_type_metadata(self, classifier, value, expected):
        finding = classifier.remote_from_workplace_type(value)
        assert finding is not None
        assert finding.is_remote is expected
        assert finding.remote_source is RemoteSource.METADATA_FIELD

    def test_unrecognized_workplace_value_defers(self, classifier):
        """An unexpected value must fall through to location inference rather than guess."""
        assert classifier.remote_from_workplace_type("Flexible") is None

    def test_only_configured_field_names_are_workplace_type(self, classifier):
        """Greenhouse metadata names are per-company custom; real boards carry fields like
        'Quota Coverage Type' that must not be read as a remote signal."""
        assert classifier.is_workplace_type_field("Workplace Type")
        assert not classifier.is_workplace_type_field("Quota Coverage Type")
