"""Adapter tests, driven by REAL captured responses.

Spec 4.1: record a fixture per vendor and test the adapter against it. This is ten
minutes of pytest that will catch a vendor changing shape in February, when Gabriel is
mid-search and not reading logs.

Several assertions here encode discrepancies between a vendor's documentation and its
actual behaviour. They are not paranoia - every one was observed live, and each would
corrupt data silently rather than raise.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import httpx

from src.models import RemoteSource, RunStatus, SalaryPeriod
from src.sources.base import build_client, parse_epoch
from src.sources.greenhouse import GreenhouseSource
from src.sources.himalayas import HimalayasSource
from tests.conftest import fixture

# pytest-httpx matches `url` as a str, re.Pattern or httpx.URL - query strings vary per
# request, so these are prefix patterns.
SEARCH_URL = re.compile(r"^https://himalayas\.app/jobs/api/search")
GH_ANY = re.compile(r"^https://boards-api\.greenhouse\.io/")
GH_AIRBNB = re.compile(r"^https://boards-api\.greenhouse\.io/v1/boards/airbnb/")
GH_DEAD = re.compile(r"^https://boards-api\.greenhouse\.io/v1/boards/deadtoken/")


def run(coro):
    return asyncio.run(coro)


async def _fetch(source, config):
    async with build_client(config) as client:
        return await source.fetch(client)


def _himalayas(config, classifier, **overrides):
    cfg = {
        **config,
        "sources": {**config["sources"], "himalayas": {**config["sources"]["himalayas"]}},
    }
    cfg["sources"]["himalayas"].update(
        {"query_set": ["data scientist"], "max_pages_per_query": 1, "request_spacing_s": 0}
    )
    cfg["sources"]["himalayas"].update(overrides)
    return HimalayasSource(cfg, classifier), cfg


class TestHimalayasShapes:
    """Every assertion below contradicts the published OpenAPI spec."""

    def test_pubdate_is_seconds_not_milliseconds(self):
        """The spec documents `pubDate` in milliseconds and gives a millisecond example.
        The live API returns seconds. Trusting the docs yields dates near the year 58000,
        which is a perfectly valid datetime and would poison every freshness metric."""
        raw = fixture("himalayas_search.json")["jobs"][0]["pubDate"]
        assert raw < 1e11, "fixture should carry a second-precision timestamp"
        parsed = parse_epoch(raw)
        assert 2020 < parsed.year < 2100

    def test_location_restrictions_are_strings_not_objects(self, config, classifier, httpx_mock):
        """Spec declares array[{alpha2,name,slug}]; live returns array[str]. Indexing the
        documented shape raises TypeError partway through a run."""
        payload = fixture("himalayas_search.json")
        assert isinstance(payload["jobs"][0]["locationRestrictions"][0], str)
        httpx_mock.add_response(url=SEARCH_URL, json=payload)
        source, cfg = _himalayas(config, classifier)
        result = run(_fetch(source, cfg))
        assert all(p.location_raw for p in result.postings)

    def test_handles_documented_object_shape_too(self, config, classifier, httpx_mock):
        """If the API ever matches its own docs, the adapter must not break."""
        payload = fixture("himalayas_search.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["locationRestrictions"] = [
            {"alpha2": "US", "name": "United States", "slug": "united-states"}
        ]
        httpx_mock.add_response(url=SEARCH_URL, json=payload)
        source, cfg = _himalayas(config, classifier)
        result = run(_fetch(source, cfg))
        assert result.postings[0].location_raw == "United States"
        assert result.postings[0].country == "US"

    def test_seniority_is_an_array(self, config, classifier, httpx_mock):
        payload = fixture("himalayas_search.json")
        assert isinstance(payload["jobs"][0]["seniority"], list)
        httpx_mock.add_response(url=SEARCH_URL, json=payload)
        source, cfg = _himalayas(config, classifier)
        result = run(_fetch(source, cfg))
        assert any(p.seniority_source for p in result.postings)

    def test_salary_period_kept_in_its_stated_period(self, config, classifier, httpx_mock):
        """Himalayas does NOT annualize. Converting at collection time would destroy the
        raw value; annualization belongs in the analysis layer."""
        payload = fixture("himalayas_search.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0].update(
            {"salaryPeriod": "fortnightly", "minSalary": 5000, "maxSalary": 7000}
        )
        httpx_mock.add_response(url=SEARCH_URL, json=payload)
        source, cfg = _himalayas(config, classifier)
        posting = run(_fetch(source, cfg)).postings[0]
        assert posting.salary_period is SalaryPeriod.FORTNIGHT
        assert posting.salary_period_raw == "fortnightly"
        assert posting.salary_min == 5000  # unchanged

    def test_empty_location_array_means_worldwide(self, config, classifier, httpx_mock):
        payload = fixture("himalayas_search.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["locationRestrictions"] = []
        httpx_mock.add_response(url=SEARCH_URL, json=payload)
        source, cfg = _himalayas(config, classifier)
        assert run(_fetch(source, cfg)).postings[0].location_raw == "Worldwide"

    def test_remote_status_comes_from_the_source_itself(self, config, classifier, httpx_mock):
        """The board lists remote roles exclusively, so this is a property of the source,
        not an inference from text."""
        httpx_mock.add_response(url=SEARCH_URL, json=fixture("himalayas_search.json"))
        source, cfg = _himalayas(config, classifier)
        for posting in run(_fetch(source, cfg)).postings:
            assert posting.is_remote is True
            assert posting.remote_source is RemoteSource.SOURCE_FIELD


class TestHimalayasPagination:
    def test_does_not_terminate_on_short_page(self, config, classifier, httpx_mock):
        """/search ignores `limit` and returns 17-20 items while reporting `limit: 20`.
        A `len(page) == limit` termination check exits on page one and collects almost
        nothing - silently, with status ok."""
        page1 = fixture("himalayas_search.json")
        page1["limit"] = 20
        assert len(page1["jobs"]) < 20
        page2 = fixture("himalayas_search.json")
        for i, job in enumerate(page2["jobs"]):
            job["guid"] = f"https://himalayas.app/second-page-{i}"
        httpx_mock.add_response(url=SEARCH_URL, json=page1)
        httpx_mock.add_response(url=SEARCH_URL, json=page2)
        source, cfg = _himalayas(config, classifier, max_pages_per_query=2, lookback_days=36500)
        result = run(_fetch(source, cfg))
        assert result.runs[0].pages_fetched == 2, "must page past a short page"
        assert len(result.postings) == len(page1["jobs"]) + len(page2["jobs"])

    def test_empty_page_terminates(self, config, classifier, httpx_mock):
        httpx_mock.add_response(url=SEARCH_URL, json=fixture("himalayas_search.json"))
        httpx_mock.add_response(url=SEARCH_URL, json={"jobs": [], "limit": 20})
        source, cfg = _himalayas(config, classifier, max_pages_per_query=5, lookback_days=36500)
        result = run(_fetch(source, cfg))
        assert result.runs[0].pages_fetched == 2
        assert result.runs[0].status is RunStatus.OK

    def test_duplicate_guids_across_queries_collapse(self, config, classifier, httpx_mock):
        """The same posting legitimately appears under several query terms. Deduping is
        what keeps a same-day re-run idempotent."""
        payload = fixture("himalayas_search.json")
        httpx_mock.add_response(url=SEARCH_URL, json=payload)
        httpx_mock.add_response(url=SEARCH_URL, json=payload)
        source, cfg = _himalayas(config, classifier)
        cfg["sources"]["himalayas"]["query_set"] = ["data scientist", "senior data scientist"]
        result = run(_fetch(source, cfg))
        assert len(result.postings) == len(payload["jobs"])
        assert len({p.job_id for p in result.postings}) == len(result.postings)

    def test_captures_api_changelog_for_change_detection(self, config, classifier, httpx_mock):
        httpx_mock.add_response(url=SEARCH_URL, json=fixture("himalayas_search.json"))
        source, cfg = _himalayas(config, classifier)
        assert run(_fetch(source, cfg)).runs[0].api_notice


class TestHimalayasFailureModes:
    """Spec correctness rules 1 and 2. A truncated walk must never look like a clean one."""

    def test_rate_limit_yields_partial_not_ok(self, config, classifier, httpx_mock):
        httpx_mock.add_response(
            url=SEARCH_URL, status_code=429, json={"ok": False}, is_reusable=True
        )
        source, cfg = _himalayas(config, classifier, rate_limit_sleep_s=0, rate_limit_max_retries=1)
        run_row = run(_fetch(source, cfg)).runs[0]
        assert run_row.status is RunStatus.PARTIAL
        assert run_row.allows_diffing is False

    def test_partial_midway_keeps_rows_but_forbids_diffing(self, config, classifier, httpx_mock):
        """Jobs not seen because the walk was truncated are not absent - they were never
        looked for. The rows collected stay; the permission to diff does not."""
        httpx_mock.add_response(url=SEARCH_URL, json=fixture("himalayas_search.json"))
        httpx_mock.add_response(url=SEARCH_URL, status_code=429, json={"ok": False})
        source, cfg = _himalayas(
            config,
            classifier,
            max_pages_per_query=3,
            lookback_days=36500,
            rate_limit_sleep_s=0,
            rate_limit_max_retries=0,
        )
        result = run(_fetch(source, cfg))
        assert result.postings, "rows already collected are still returned"
        assert result.runs[0].status is RunStatus.PARTIAL
        assert result.diffable_scopes == set()

    def test_server_error_yields_partial(self, config, classifier, httpx_mock):
        httpx_mock.add_response(url=SEARCH_URL, status_code=500)
        source, cfg = _himalayas(config, classifier)
        assert run(_fetch(source, cfg)).runs[0].status is RunStatus.PARTIAL

    def test_malformed_record_is_skipped_not_fatal(self, config, classifier, httpx_mock):
        """One bad posting must not cost a day of collection."""
        payload = fixture("himalayas_search.json")
        payload["jobs"].insert(0, {"title": None, "guid": None})
        payload["jobs"].insert(1, "not even an object")
        httpx_mock.add_response(url=SEARCH_URL, json=payload)
        source, cfg = _himalayas(config, classifier)
        result = run(_fetch(source, cfg))
        assert len(result.postings) == len(payload["jobs"]) - 2
        assert result.runs[0].status is RunStatus.OK


class TestGreenhouse:
    @staticmethod
    def _source(config, classifier, companies):
        return GreenhouseSource(config, classifier, companies)

    @staticmethod
    def _company(slug="airbnb", token="airbnb", name="Airbnb"):
        return {"name": name, "slug": slug, "ats": "greenhouse", "token": token}

    def test_uses_first_published_for_posted_at(self, config, classifier, httpx_mock):
        """`first_published` is absent from the spec's field list. `updated_at` merely
        reflects the last edit and overstates freshness for any req ever touched."""
        payload = fixture("greenhouse_board.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["first_published"] = "2026-01-15T10:00:00-05:00"
        payload["jobs"][0]["updated_at"] = "2026-09-09T04:35:19-04:00"
        httpx_mock.add_response(url=GH_ANY, json=payload)
        source = self._source(config, classifier, [self._company()])
        posting = run(_fetch(source, config)).postings[0]
        assert posting.posted_at == datetime(2026, 1, 15, 15, 0, tzinfo=UTC)

    def test_workplace_type_metadata_wins_over_location(self, config, classifier, httpx_mock):
        httpx_mock.add_response(url=GH_ANY, json=fixture("greenhouse_board.json"))
        source = self._source(config, classifier, [self._company()])
        postings = run(_fetch(source, config)).postings
        by_meta = [p for p in postings if p.remote_source is RemoteSource.METADATA_FIELD]
        assert by_meta, "the Airbnb fixture carries a Workplace Type field"
        assert {p.is_remote for p in by_meta} == {True, False}

    def test_board_without_metadata_falls_back_to_location(self, config, classifier, httpx_mock):
        """The common case: the Workplace Type field exists on roughly 1 board in 6."""
        httpx_mock.add_response(url=GH_ANY, json=fixture("greenhouse_board_no_metadata.json"))
        source = self._source(config, classifier, [self._company("figma", "figma", "Figma")])
        postings = run(_fetch(source, config)).postings
        assert postings
        assert all(p.remote_source is not RemoteSource.METADATA_FIELD for p in postings)

    def test_404_is_isolated_to_that_company(self, config, classifier, httpx_mock):
        """Spec correctness rule 1, per-company. One dead board must not fail the run and
        must not mark that company's jobs disappeared."""
        httpx_mock.add_response(url=GH_AIRBNB, json=fixture("greenhouse_board.json"))
        httpx_mock.add_response(url=GH_DEAD, status_code=404)
        source = self._source(
            config, classifier, [self._company(), self._company("dead-co", "deadtoken", "Dead Co")]
        )
        result = run(_fetch(source, config))
        by_slug = {r.company_slug: r for r in result.runs}
        assert by_slug["dead-co"].status is RunStatus.ERROR
        assert by_slug["dead-co"].records_fetched == 0
        assert by_slug["airbnb"].status is RunStatus.OK
        assert ("greenhouse", "airbnb") in result.diffable_scopes
        assert ("greenhouse", "dead-co") not in result.diffable_scopes

    def test_empty_board_is_not_diffable(self, config, classifier, httpx_mock):
        """A real hiring freeze looks exactly like a misconfigured token. Refusing to diff
        costs a day of disappearance events; guessing wrong corrupts the series."""
        httpx_mock.add_response(url=GH_ANY, json={"jobs": []})
        source = self._source(config, classifier, [self._company()])
        result = run(_fetch(source, config))
        assert result.runs[0].status is RunStatus.EMPTY
        assert result.diffable_scopes == set()

    def test_watchlist_identity_wins_over_board_echo(self, config, classifier, httpx_mock):
        """The board self-reports company_name, which can be a legal name or a post-rebrand
        name ('intercom' reports as 'Fin'). Using it would split one company's series in
        two mid-panel."""
        payload = fixture("greenhouse_board.json")
        for job in payload["jobs"]:
            job["company_name"] = "Some Rebranded Entity LLC"
        httpx_mock.add_response(url=GH_ANY, json=payload)
        source = self._source(config, classifier, [self._company()])
        postings = run(_fetch(source, config)).postings
        assert {p.company_slug for p in postings} == {"airbnb"}
        assert {p.company_name for p in postings} == {"Airbnb"}

    def test_descriptions_only_for_tracked_families(self, config, classifier, httpx_mock):
        httpx_mock.add_response(url=GH_ANY, json=fixture("greenhouse_board.json"))
        source = self._source(config, classifier, [self._company()])
        for posting in run(_fetch(source, config)).postings:
            assert bool(posting.description_text) == posting.is_tracked

    def test_location_outranks_description(self, config, classifier, httpx_mock):
        """The location is the more specific field. This ordering costs one known error
        (a board reading "Remote, USA" whose description carried "#LI-Onsite") and is
        worth it for the cases it decides correctly."""
        payload = fixture("greenhouse_board.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["metadata"] = []
        payload["jobs"][0]["location"] = {"name": "Remote - US"}
        payload["jobs"][0]["content"] = "<p>#LI-Hybrid, three days a week in the office.</p>"
        httpx_mock.add_response(url=GH_ANY, json=payload)
        source = self._source(config, classifier, [self._company()])
        posting = run(_fetch(source, config)).postings[0]
        assert posting.is_remote is True
        assert posting.remote_source is RemoteSource.LOCATION_STRING

    def test_description_decides_when_location_is_ambiguous(self, config, classifier, httpx_mock):
        payload = fixture("greenhouse_board.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["metadata"] = []
        payload["jobs"][0]["location"] = {"name": "Denver, CO"}
        payload["jobs"][0]["content"] = "<p>Great role. #LI-Hybrid</p>"
        httpx_mock.add_response(url=GH_ANY, json=payload)
        source = self._source(config, classifier, [self._company()])
        posting = run(_fetch(source, config)).postings[0]
        assert posting.is_remote is False
        assert posting.remote_source is RemoteSource.DESCRIPTION_TEXT

    def test_description_informs_remote_status_even_when_not_stored(
        self, config, classifier, httpx_mock
    ):
        """The crux of doing this at collection time: an untracked family keeps NO
        description, but must still carry the verdict derived from it. Otherwise the
        signal is lost permanently rather than deferred."""
        payload = fixture("greenhouse_board.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["metadata"] = []
        payload["jobs"][0]["title"] = "Account Executive, Enterprise"  # -> role_family other
        payload["jobs"][0]["location"] = {"name": "Denver, CO"}
        payload["jobs"][0]["content"] = "<p>This is a remote position. #LI-Remote</p>"
        httpx_mock.add_response(url=GH_ANY, json=payload)
        source = self._source(config, classifier, [self._company()])
        posting = run(_fetch(source, config)).postings[0]
        assert posting.is_tracked is False
        assert posting.description_text is None, "untracked families store no description"
        assert posting.is_remote is True, "but the verdict derived from it survives"
        assert posting.remote_source is RemoteSource.DESCRIPTION_TEXT

    def test_network_error_is_contained(self, config, classifier, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectTimeout("timed out"), url=GH_ANY)
        source = self._source(config, classifier, [self._company()])
        result = run(_fetch(source, config))
        assert result.runs[0].status is RunStatus.ERROR
        assert result.diffable_scopes == set()
