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

from src.models import RemoteSource, RunStatus, SalaryPeriod, SalarySource
from src.sources.ashby import AshbySource
from src.sources.base import build_client, parse_epoch
from src.sources.greenhouse import GreenhouseSource
from src.sources.himalayas import HimalayasSource
from src.sources.lever import LeverSource
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


ASHBY_ANY = re.compile(r"^https://api\.ashbyhq\.com/")
#: Ashby's documented posting API is opt-in per organisation, so the adapter falls back to
#: the endpoint its own board page uses. Tests that 404 the documented API must therefore
#: also answer the fallback, or the request goes unmocked.
ASHBY_GRAPHQL = re.compile(r"^https://jobs\.ashbyhq\.com/api/non-user-graphql")
LEVER_ANY = re.compile(r"^https://api\.lever\.co/")


def _company(slug="ramp", token="ramp", name="Ramp"):
    return {"name": name, "slug": slug, "ats": "ashby", "token": token, "status": "verified"}


class TestAshby:
    """Ashby is the largest Phase 2 coverage win, and carries the phase's worst trap."""

    @staticmethod
    def _source(config, classifier, companies):
        return AshbySource(config, classifier, companies)

    def test_isremote_is_ignored_in_favour_of_workplacetype(self, config, classifier, httpx_mock):
        """**The trap.** Measured across 422 live postings: 293 report `isRemote: true`
        while `workplaceType` says `Hybrid`, one of them located at "San Francisco HQ".
        `isRemote` evidently means "some remote permitted", not "this is a remote role".
        Trusting it marks roughly a quarter of the panel remote when it is not - and the
        resulting number looks entirely plausible."""
        payload = fixture("ashby_board.json")
        contradictory = [
            j
            for j in payload["jobs"]
            if j.get("isRemote") is True and j.get("workplaceType") == "Hybrid"
        ]
        assert contradictory, "fixture must contain the contradiction this test guards"
        payload["jobs"] = contradictory
        httpx_mock.add_response(url=ASHBY_ANY, json=payload)
        source = self._source(config, classifier, [_company()])
        for posting in run(_fetch(source, config)).postings:
            assert posting.is_remote is False, "workplaceType Hybrid must win over isRemote"
            assert posting.remote_source is RemoteSource.METADATA_FIELD

    def test_structured_salary_takes_only_the_salary_component(
        self, config, classifier, httpx_mock
    ):
        """Tiers also carry EquityCashValue, Commission and Bonus. Pooling any of those
        into base pay would silently inflate the compensation series."""
        payload = fixture("ashby_board.json")
        payload["jobs"] = [
            j for j in payload["jobs"] if (j.get("compensation") or {}).get("compensationTiers")
        ][:1]
        job = payload["jobs"][0]
        job["compensation"]["compensationTiers"] = [
            {
                "components": [
                    {
                        "compensationType": "EquityCashValue",
                        "minValue": 999_999,
                        "maxValue": 999_999,
                        "currencyCode": "USD",
                        "interval": "1 YEAR",
                    },
                    {
                        "compensationType": "Salary",
                        "minValue": 211_400,
                        "maxValue": 290_600,
                        "currencyCode": "USD",
                        "interval": "1 YEAR",
                    },
                    {
                        "compensationType": "Bonus",
                        "minValue": 50_000,
                        "maxValue": 50_000,
                        "currencyCode": "USD",
                        "interval": "1 YEAR",
                    },
                ]
            }
        ]
        httpx_mock.add_response(url=ASHBY_ANY, json=payload)
        source = self._source(config, classifier, [_company()])
        posting = run(_fetch(source, config)).postings[0]
        assert (posting.salary_min, posting.salary_max) == (211_400, 290_600)
        assert posting.salary_period is SalaryPeriod.YEAR
        assert posting.salary_period_raw == "1 YEAR"
        assert posting.salary_currency == "USD"

    def test_unknown_salary_interval_degrades_to_none(self, config, classifier, httpx_mock):
        payload = fixture("ashby_board.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["compensation"] = {
            "compensationTiers": [
                {
                    "components": [
                        {
                            "compensationType": "Salary",
                            "minValue": 1,
                            "maxValue": 2,
                            "currencyCode": "USD",
                            "interval": "PER SPRINT",
                        }
                    ]
                }
            ]
        }
        httpx_mock.add_response(url=ASHBY_ANY, json=payload)
        source = self._source(config, classifier, [_company()])
        posting = run(_fetch(source, config)).postings[0]
        assert posting.salary_period is None, "an unseen interval must not be guessed"
        assert posting.salary_period_raw == "PER SPRINT", "but the raw token is kept"

    def test_unlisted_postings_are_skipped(self, config, classifier, httpx_mock):
        """An unlisted posting is not public. Collecting it would record an appearance no
        observer could have seen, and a disappearance when it is merely re-hidden."""
        payload = fixture("ashby_board.json")
        for job in payload["jobs"]:
            job["isListed"] = False
        httpx_mock.add_response(url=ASHBY_ANY, json=payload)
        source = self._source(config, classifier, [_company()])
        result = run(_fetch(source, config))
        assert result.postings == []
        assert result.runs[0].status is RunStatus.EMPTY
        assert result.diffable_scopes == set()

    def test_secondary_locations_inform_remote(self, config, classifier, httpx_mock):
        payload = fixture("ashby_board.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0].update(
            workplaceType=None,
            isRemote=None,
            location="New York",
            secondaryLocations=[{"location": "Remote (US)"}],
            descriptionPlain="",
        )
        httpx_mock.add_response(url=ASHBY_ANY, json=payload)
        source = self._source(config, classifier, [_company()])
        posting = run(_fetch(source, config)).postings[0]
        assert posting.is_remote is True
        assert "Remote (US)" in posting.location_raw

    def test_uses_published_at(self, config, classifier, httpx_mock):
        payload = fixture("ashby_board.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["publishedAt"] = "2026-06-03T18:24:47.526+00:00"
        httpx_mock.add_response(url=ASHBY_ANY, json=payload)
        source = self._source(config, classifier, [_company()])
        assert run(_fetch(source, config)).postings[0].posted_at == datetime(
            2026, 6, 3, 18, 24, 47, 526000, tzinfo=UTC
        )


class TestLever:
    @staticmethod
    def _company(slug="wealthfront", token="wealthfront", name="Wealthfront"):
        return {"name": name, "slug": slug, "ats": "lever", "token": token, "status": "verified"}

    def test_bare_array_yields_rows(self, config, classifier, httpx_mock):
        """Lever returns a bare array, not {"jobs": [...]}. The classic
        `payload.get("jobs") or []` bug yields zero rows without raising - which under this
        project's rules degrades to EMPTY and costs a day of data, silently, forever."""
        payload = fixture("lever_board.json")
        assert isinstance(payload, list), "fixture must stay a bare array"
        httpx_mock.add_response(url=LEVER_ANY, json=payload)
        source = LeverSource(config, classifier, [self._company()])
        result = run(_fetch(source, config))
        assert len(result.postings) == len(payload)
        assert result.runs[0].status is RunStatus.OK

    def test_title_comes_from_text_field(self, config, classifier, httpx_mock):
        payload = fixture("lever_board.json")
        httpx_mock.add_response(url=LEVER_ANY, json=payload)
        source = LeverSource(config, classifier, [self._company()])
        titles = {p.title for p in run(_fetch(source, config)).postings}
        assert titles == {p["text"] for p in payload}

    def test_created_at_milliseconds(self, config, classifier, httpx_mock):
        payload = fixture("lever_board.json")[:1]
        payload[0]["createdAt"] = 1694463796009
        httpx_mock.add_response(url=LEVER_ANY, json=payload)
        source = LeverSource(config, classifier, [self._company()])
        posted = run(_fetch(source, config)).postings[0].posted_at
        assert posted is not None and 2020 < posted.year < 2100

    def test_lowercase_workplace_type(self, config, classifier, httpx_mock):
        payload = fixture("lever_board.json")[:1]
        payload[0]["workplaceType"] = "remote"
        httpx_mock.add_response(url=LEVER_ANY, json=payload)
        source = LeverSource(config, classifier, [self._company()])
        posting = run(_fetch(source, config)).postings[0]
        assert posting.is_remote is True
        assert posting.remote_source is RemoteSource.METADATA_FIELD

    def test_salary_is_read_from_additional_plain(self, config, classifier, httpx_mock):
        """Lever keeps the band in `additionalPlain`, NOT the description.

        A fresh 20-board sample scored **zero** on Lever until this was noticed - the
        parser was pointed at the wrong field entirely, and would have quietly reported
        Lever as publishing no compensation at all.
        """
        payload = fixture("lever_board.json")[:1]
        payload[0]["additionalPlain"] = "Estimated annual salary range: $150,000 - $189,000"
        httpx_mock.add_response(url=LEVER_ANY, json=payload)
        source = LeverSource(config, classifier, [self._company()])
        posting = run(_fetch(source, config)).postings[0]
        assert (posting.salary_min, posting.salary_max) == (150_000, 189_000)
        assert posting.salary_source is SalarySource.DESCRIPTION_PARSED


class TestUnverifiedBoardsAreNotCollected:
    """Ashby and Lever echo no company name in their APIs, so a guessed token that happens
    to exist cannot be checked there. An unverified board is a real board belonging to
    somebody; collecting it attributes their hiring to the wrong company permanently."""

    def test_unverified_company_is_excluded(self, config, classifier):
        watchlist = [
            {
                "name": "Good Co",
                "slug": "good-co",
                "ats": "ashby",
                "token": "goodco",
                "status": "verified",
            },
            {
                "name": "Maybe Co",
                "slug": "maybe-co",
                "ats": "ashby",
                "token": "maybeco",
                "status": "unverified",
            },
            {
                "name": "Old Co",
                "slug": "old-co",
                "ats": "ashby",
                "token": "oldco",
            },  # no status = legacy
        ]
        source = AshbySource(config, classifier, watchlist)
        assert {c["slug"] for c in source.companies} == {"good-co", "old-co"}

    def test_each_vendor_only_claims_its_own(self, config, classifier):
        watchlist = [
            {"name": "A", "slug": "a", "ats": "ashby", "token": "a", "status": "verified"},
            {"name": "L", "slug": "l", "ats": "lever", "token": "l", "status": "verified"},
            {"name": "G", "slug": "g", "ats": "greenhouse", "token": "g", "status": "verified"},
        ]
        assert [c["slug"] for c in AshbySource(config, classifier, watchlist).companies] == ["a"]
        assert [c["slug"] for c in LeverSource(config, classifier, watchlist).companies] == ["l"]


class TestCrossVendorIsolation:
    """Correctness rule 1 now spans three vendors sharing one implementation.

    The isolation logic lives once, in PerCompanyBoardSource, precisely so it cannot
    diverge between adapters. This asserts the shared path still contains a failure to the
    single company that suffered it.
    """

    def test_dead_board_in_one_vendor_does_not_affect_another(self, config, classifier, httpx_mock):
        httpx_mock.add_response(url=ASHBY_ANY, status_code=404)
        httpx_mock.add_response(
            url=ASHBY_GRAPHQL, json={"data": {"jobBoard": None}}, is_reusable=True
        )
        httpx_mock.add_response(url=GH_ANY, json=fixture("greenhouse_board.json"))

        gh_watch = [
            {
                "name": "Airbnb",
                "slug": "airbnb",
                "ats": "greenhouse",
                "token": "airbnb",
                "status": "verified",
            }
        ]
        ashby_watch = [
            {"name": "Ramp", "slug": "ramp", "ats": "ashby", "token": "ramp", "status": "verified"}
        ]

        ashby = run(_fetch(AshbySource(config, classifier, ashby_watch), config))
        greenhouse = run(_fetch(GreenhouseSource(config, classifier, gh_watch), config))

        assert ashby.runs[0].status is RunStatus.ERROR
        assert ashby.diffable_scopes == set(), "the dead board must not be diffed"
        assert greenhouse.runs[0].status is RunStatus.OK
        assert ("greenhouse", "airbnb") in greenhouse.diffable_scopes

    def test_one_dead_ashby_board_does_not_affect_its_siblings(
        self, config, classifier, httpx_mock
    ):
        httpx_mock.add_response(
            url=re.compile(r"^https://api\.ashbyhq\.com/posting-api/job-board/deadco"),
            status_code=404,
        )
        # A genuinely dead board is dead on both routes.
        httpx_mock.add_response(
            url=ASHBY_GRAPHQL, json={"data": {"jobBoard": None}}, is_reusable=True
        )
        httpx_mock.add_response(url=ASHBY_ANY, json=fixture("ashby_board.json"))
        watchlist = [
            {
                "name": "Dead Co",
                "slug": "dead-co",
                "ats": "ashby",
                "token": "deadco",
                "status": "verified",
            },
            {"name": "Ramp", "slug": "ramp", "ats": "ashby", "token": "ramp", "status": "verified"},
        ]
        result = run(_fetch(AshbySource(config, classifier, watchlist), config))
        by_slug = {r.company_slug: r for r in result.runs}
        assert by_slug["dead-co"].status is RunStatus.ERROR
        assert by_slug["dead-co"].records_fetched == 0
        assert by_slug["ramp"].status is RunStatus.OK
        assert ("ashby", "ramp") in result.diffable_scopes
        assert ("ashby", "dead-co") not in result.diffable_scopes


class TestParsedSalaryNeverMasqueradesAsStructured:
    """Both kinds are employer-disclosed, but one came from a vendor field and the other
    through a regex. Pooling them would hide the difference in reliability."""

    def test_ashby_structured_wins_over_prose(self, config, classifier, httpx_mock):
        payload = fixture("ashby_board.json")
        payload["jobs"] = [
            j for j in payload["jobs"] if (j.get("compensation") or {}).get("compensationTiers")
        ][:1]
        payload["jobs"][0]["compensation"]["compensationTiers"] = [
            {
                "components": [
                    {
                        "compensationType": "Salary",
                        "minValue": 211_400,
                        "maxValue": 290_600,
                        "currencyCode": "USD",
                        "interval": "1 YEAR",
                    }
                ]
            }
        ]
        payload["jobs"][0]["descriptionPlain"] = "Salary Range: $10,000 - $20,000"
        httpx_mock.add_response(url=ASHBY_ANY, json=payload)
        posting = run(_fetch(AshbySource(config, classifier, [_company()]), config)).postings[0]
        assert (posting.salary_min, posting.salary_max) == (211_400, 290_600)
        assert posting.salary_source is SalarySource.POSTING_DISCLOSED

    def test_greenhouse_prose_is_labelled_parsed(self, config, classifier, httpx_mock):
        payload = fixture("greenhouse_board.json")
        payload["jobs"] = payload["jobs"][:1]
        payload["jobs"][0]["content"] = "<p>US Salary Range $112,000&#8212;$149,000 USD</p>"
        httpx_mock.add_response(url=GH_ANY, json=payload)
        watch = [
            {
                "name": "Airbnb",
                "slug": "airbnb",
                "ats": "greenhouse",
                "token": "airbnb",
                "status": "verified",
            }
        ]
        posting = run(_fetch(GreenhouseSource(config, classifier, watch), config)).postings[0]
        assert (posting.salary_min, posting.salary_max) == (112_000, 149_000)
        assert posting.salary_source is SalarySource.DESCRIPTION_PARSED
        assert posting.salary_is_estimated is False, "disclosed by the employer, not predicted"


class TestAshbyBoardEndpointFallback:
    """Ashby's documented posting API is **opt-in per organisation**.

    A company can have a fully public board rendering at jobs.ashbyhq.com/<token> while the
    documented API 404s. Whatnot is exactly that — 144 live postings the documented route
    cannot see. Treating that 404 as "no board exists" is how it got recorded as unreachable.
    """

    def test_falls_back_when_the_documented_api_404s(self, config, classifier, httpx_mock):
        httpx_mock.add_response(url=ASHBY_ANY, status_code=404)
        httpx_mock.add_response(
            url=ASHBY_GRAPHQL,
            json={
                "data": {
                    "jobBoard": {
                        "jobPostings": [
                            {
                                "id": "abc",
                                "title": "Senior Data Scientist",
                                "locationName": "Remote - US",
                                "employmentType": "FullTime",
                                "compensationTierSummary": "$200K - $260K",
                                "secondaryLocations": [],
                            }
                        ]
                    }
                }
            },
        )
        source = AshbySource(
            config,
            classifier,
            [
                {
                    "name": "Whatnot",
                    "slug": "whatnot",
                    "ats": "ashby",
                    "token": "whatnot",
                    "status": "verified",
                }
            ],
        )
        result = run(_fetch(source, config))
        assert len(result.postings) == 1
        posting = result.postings[0]
        assert posting.title == "Senior Data Scientist"
        assert posting.apply_url == "https://jobs.ashbyhq.com/whatnot/abc"
        assert (posting.salary_min, posting.salary_max) == (200_000, 260_000)
        assert posting.salary_source is SalarySource.DESCRIPTION_PARSED, (
            "a display string parsed by us is not a vendor-supplied field"
        )

    def test_documented_api_is_preferred_when_available(self, config, classifier, httpx_mock):
        """The fallback is poorer — no descriptions, compensation as a string — so a board
        with the API enabled must never touch it."""
        httpx_mock.add_response(url=ASHBY_ANY, json=fixture("ashby_board.json"))
        source = AshbySource(config, classifier, [_company()])
        result = run(_fetch(source, config))
        assert result.postings, "the documented API answered, so no fallback was needed"

    def test_dead_on_both_routes_is_still_an_error(self, config, classifier, httpx_mock):
        httpx_mock.add_response(url=ASHBY_ANY, status_code=404)
        httpx_mock.add_response(url=ASHBY_GRAPHQL, json={"data": {"jobBoard": None}})
        source = AshbySource(config, classifier, [_company("dead", "dead", "Dead Co")])
        result = run(_fetch(source, config))
        assert result.runs[0].status is RunStatus.ERROR
        assert result.diffable_scopes == set()
