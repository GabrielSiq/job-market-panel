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


class TestRemoteFromDescription:
    """Last-resort inference from work-location prose.

    Measured on a hand-labelled sample of 100 ATS postings: this path raised the share of
    postings carrying any verdict from 55% to 80%, at 100% accuracy on the rows it newly
    decided. It MUST run at collection time - descriptions are stored only for tracked
    families, so for most postings the text is gone once the run ends and the inference
    becomes impossible rather than merely deferred.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "#LI-Hybrid",
            "#LI-Onsite #LI-LB1",
            "Fin has a hybrid working policy",
            "This role requires that you be on-site at our HQ in San Mateo, CA 5 days a week",
            "requiring a hybrid work schedule with 3 days of in-office work",
            "we expect all staff to be in one of our offices at least 25% of the time",
            "with four days a week in the office",
        ],
    )
    def test_onsite_and_hybrid_prose(self, classifier, text):
        finding = classifier.remote_from_description(text)
        assert finding.is_remote is False
        assert finding.remote_source is RemoteSource.DESCRIPTION_TEXT

    @pytest.mark.parametrize(
        "text",
        [
            "#LI-Remote",
            "#LI-REMOTE",
            "We are a 100% remote company with team members across 40+ countries",
            "Coinbase is a remote-first, but not remote-only company",
            "This is a remote position open to candidates residing in the US",
            "This position is US - Remote Eligible",
        ],
    )
    def test_remote_prose(self, classifier, text):
        finding = classifier.remote_from_description(text)
        assert finding.is_remote is True
        assert finding.remote_source is RemoteSource.DESCRIPTION_TEXT

    def test_benefits_boilerplate_is_not_a_remote_signal(self, classifier):
        """The trap this cost real accuracy on. Two strictly-onsite postings carried
        "Remote work, medical insurance, flexible time off..." in a benefits list; a bare
        "remote" match would have flipped both to remote."""
        text = (
            "Remote work, medical insurance, flexible time off, retirement savings plans, "
            "and modern family planning are just some of our benefits."
        )
        assert classifier.remote_from_description(text).is_remote is None

    def test_remote_sensing_is_not_a_remote_signal(self, classifier):
        """ "Remote sensing" is a real data-science domain and appears in JDs for strictly
        onsite roles."""
        assert (
            classifier.remote_from_description(
                "Build models over satellite and remote sensing imagery."
            ).is_remote
            is None
        )

    def test_hybrid_in_a_technical_sense_is_not_a_workplace_signal(self, classifier):
        """ "Hybrid search", "hybrid model" and "hybrid cloud" are ordinary DS vocabulary."""
        assert (
            classifier.remote_from_description(
                "Design hybrid search ranking combining lexical and vector retrieval."
            ).is_remote
            is None
        )

    def test_negative_beats_positive(self, classifier):
        assert (
            classifier.remote_from_description(
                "#LI-Remote. Note: this team works hybrid, three days a week in the office."
            ).is_remote
            is False
        )

    def test_empty_description_is_unknown(self, classifier):
        for text in (None, "", "   "):
            finding = classifier.remote_from_description(text)
            assert finding.is_remote is None
            assert finding.remote_source is RemoteSource.UNKNOWN
