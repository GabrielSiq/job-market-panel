"""Himalayas adapter — the DISCOVERY source.

Its job is to surface companies that demonstrably post remote DS roles, which then seed
the watchlist. It is **not** a market denominator:

- It carries only remote roles, so there is no non-remote denominator.
- Its coverage is commercially determined (paid postings alongside crawled ones, with
  partnerships that change without notice and are not observable from outside), so a step
  change in its volume is at least as likely to be a syndication deal as a market move.
- Its listings auto-expire, so a disappearance records an expiry, not a filled req.
  **Never compute survival from Himalayas rows.**

The live API disagrees with its own published OpenAPI spec in four ways, each of which
would corrupt data silently rather than raise. Every one is handled below and pinned by
the fixture in tests/. See README "Measured findings".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from src.classify import Classifier
from src.models import (
    TRACKED_FAMILIES,
    JobPosting,
    RemoteSource,
    RunStatus,
    SalaryPeriod,
    SalarySource,
    Source,
    SourceRun,
)
from src.normalize import clean_text, company_slug, strip_html
from src.sources.base import FetchResult, parse_epoch, utc_now

logger = logging.getLogger(__name__)

#: The API's own vocabulary -> our normalized periods. Figures are left in their stated
#: period: Himalayas does NOT annualize, and converting at collection time would destroy
#: the raw value. Annualization is an analysis-layer concern.
_PERIOD_MAP = {
    "hourly": SalaryPeriod.HOUR,
    "weekly": SalaryPeriod.WEEK,
    "fortnightly": SalaryPeriod.FORTNIGHT,
    "monthly": SalaryPeriod.MONTH,
    "annual": SalaryPeriod.YEAR,
}


class HimalayasSource:
    name = "himalayas"

    def __init__(self, config: dict[str, Any], classifier: Classifier) -> None:
        self.cfg = config["sources"]["himalayas"]
        self.classifier = classifier
        self.query_set_version = str(self.cfg.get("query_set_version", "unversioned"))
        self._base = self.cfg["base_url"].rstrip("/")
        self._path = self.cfg.get("search_path", "/jobs/api/search")

    # ------------------------------------------------------------------ normalization

    @staticmethod
    def _locations(raw: Any) -> tuple[str | None, str | None]:
        """Return (location_raw, country).

        The OpenAPI spec declares an array of `{alpha2, name, slug}` objects. The live API
        returns an array of **plain country-name strings**. Both shapes are accepted
        because the documented one may yet appear, and indexing the wrong one raises
        TypeError mid-run.
        """
        if not isinstance(raw, list) or not raw:
            # An empty array is meaningful: no geographic restriction, i.e. worldwide.
            return "Worldwide", None
        names: list[str] = []
        first_country: str | None = None
        for item in raw:
            if isinstance(item, dict):
                name = clean_text(item.get("name") or item.get("slug"))
                code = clean_text(item.get("alpha2"))
            else:
                name, code = clean_text(str(item)), None
            if name:
                names.append(name)
            if first_country is None:
                first_country = code or name
        return (", ".join(names) or None), first_country

    def _to_posting(self, raw: dict[str, Any], today: Any, fetched_at: Any) -> JobPosting | None:
        title = clean_text(raw.get("title"))
        guid = clean_text(raw.get("guid"))
        company_name = clean_text(raw.get("companyName"))
        if not (title and guid and company_name):
            logger.warning("himalayas: skipping record missing title/guid/company: %r", guid)
            return None

        location_raw, country = self._locations(raw.get("locationRestrictions"))

        # `seniority` is an ARRAY of labels (the spec's filter docs imply a scalar), and a
        # job may carry several. Kept verbatim; the headline series uses our own uniform
        # title-derived value instead, because consistency across sources beats accuracy
        # on either one alone.
        seniority_raw = raw.get("seniority")
        seniority_source = (
            ", ".join(str(s) for s in seniority_raw)
            if isinstance(seniority_raw, list)
            else clean_text(str(seniority_raw))
            if seniority_raw
            else None
        )

        period_raw = clean_text(str(raw.get("salaryPeriod") or "")) or None
        role_family = self.classifier.role_family(title)

        return JobPosting(
            source=Source.HIMALAYAS,
            source_job_id=guid,
            company_name=company_name,
            # Prefer the API's own canonical slug; it is a better cross-source key than
            # anything derived from the display name.
            company_slug=clean_text(raw.get("companySlug")) or company_slug(company_name),
            title=title,
            role_family=role_family,
            seniority=self.classifier.seniority(title),
            seniority_source=seniority_source,
            location_raw=location_raw,
            country=country,
            # This board lists remote roles exclusively, so remote status is a property of
            # the source itself rather than an inference from text.
            is_remote=True,
            remote_source=RemoteSource.SOURCE_FIELD,
            employment_type=clean_text(raw.get("employmentType")),
            salary_min=raw.get("minSalary"),
            salary_max=raw.get("maxSalary"),
            salary_currency=raw.get("currency"),
            salary_period=_PERIOD_MAP.get((period_raw or "").lower()),
            salary_period_raw=period_raw,
            salary_is_estimated=False,
            salary_source=(
                SalarySource.POSTING_DISCLOSED
                if raw.get("minSalary") or raw.get("maxSalary")
                else None
            ),
            # Descriptions are stored for tracked families only (spec storage policy).
            description_text=(
                strip_html(raw.get("description")) if role_family in TRACKED_FAMILIES else None
            ),
            # NOTE: this is a himalayas.app page, not the employer's ATS URL, so it cannot
            # be used to match against an ATS observation of the same req.
            apply_url=clean_text(raw.get("applicationLink")) or guid,
            posted_at=parse_epoch(raw.get("pubDate")),
            first_seen=today,
            last_seen=today,
            fetched_at=fetched_at,
            query_set_version=self.query_set_version,
        )

    # ------------------------------------------------------------------------- fetching

    async def fetch(self, client: httpx.AsyncClient) -> FetchResult:
        from datetime import timedelta

        today = utc_now().date()
        fetched_at = utc_now()
        cutoff = fetched_at - timedelta(days=int(self.cfg.get("lookback_days", 4)))

        spacing = float(self.cfg.get("request_spacing_s", 0.8))
        max_pages = int(self.cfg.get("max_pages_per_query", 5))
        sort = self.cfg.get("sort", "recent")

        started = time.monotonic()
        postings: dict[str, JobPosting] = {}
        truncated = False
        requests_made = 0
        pages_fetched = 0
        errors: list[str] = []
        api_notice: str | None = None
        feed_updated_at = None

        for query in self.cfg["query_set"]:
            for page in range(1, max_pages + 1):
                params = {"q": query, "sort": sort, "page": page}
                try:
                    payload, used = await self._get(client, params)
                    requests_made += used
                except _RateLimited:
                    logger.error("himalayas: rate limited on %r page %s - truncating", query, page)
                    errors.append(f"429 on {query!r} page {page}")
                    truncated = True
                    break
                except httpx.HTTPError as exc:
                    logger.error("himalayas: %r page %s failed: %s", query, page, exc)
                    errors.append(f"{query!r} page {page}: {exc}")
                    truncated = True
                    break

                pages_fetched += 1
                api_notice = clean_text(payload.get("comments")) or api_notice
                feed_updated_at = parse_epoch(payload.get("updatedAt")) or feed_updated_at
                jobs = payload.get("jobs")
                if not isinstance(jobs, list) or not jobs:
                    # The ONLY safe termination signal. Page size cannot be used: /search
                    # ignores `limit` and returns 17-20 items while reporting `limit: 20`,
                    # so `len(page) == limit` is false on the very first page.
                    break

                newest = None
                for raw in jobs:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        posting = self._to_posting(raw, today, fetched_at)
                    except Exception as exc:
                        logger.warning("himalayas: invalid record skipped: %s", exc)
                        continue
                    if posting is None:
                        continue
                    # The same guid legitimately appears under several query terms.
                    # Deduping here is what keeps a re-run idempotent.
                    postings.setdefault(posting.job_id, posting)
                    if posting.posted_at and (newest is None or posting.posted_at > newest):
                        newest = posting.posted_at

                # `sort=recent` decays ~2-3 days per page, so once a whole page predates
                # the lookback window there is nothing new deeper in.
                if newest is not None and newest < cutoff:
                    break

                if spacing:
                    await asyncio.sleep(spacing)
            if truncated:
                break

        if truncated:
            status = RunStatus.PARTIAL
        elif not postings:
            status = RunStatus.EMPTY
        else:
            status = RunStatus.OK

        run = SourceRun(
            date=today,
            source=Source.HIMALAYAS,
            status=status,
            records_fetched=len(postings),
            duration_s=round(time.monotonic() - started, 2),
            error_message="; ".join(errors)[:500] or None,
            # Discovery, not census: the query set is sorted by recency and paged only a
            # few pages deep, so the window slides forward daily. Measured: closures
            # generated from this source clustered at exactly `lookback_days` after
            # posting - 84 at 4 days, 39 at 5 - which is the window moving, not reqs
            # closing. These listings live ~60 days.
            is_census=False,
            query_set_version=self.query_set_version,
            feed_updated_at=feed_updated_at,
            api_notice=api_notice,
            pages_fetched=pages_fetched,
            requests_made=requests_made,
        )
        logger.info(
            "himalayas: %s records, status=%s, %s requests over %s pages",
            len(postings),
            status.value,
            requests_made,
            pages_fetched,
        )
        return FetchResult(postings=list(postings.values()), runs=[run])

    async def _get(
        self, client: httpx.AsyncClient, params: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        """One search request, honouring the undocumented rate limit.

        The 429 body says "wait 60 seconds", which is the only published guidance on a
        threshold that is otherwise unmeasured. After the configured retries the caller
        truncates and the run is marked `partial` - it must NOT be treated as `ok` with
        fewer rows, or every unseen job becomes a false disappearance.
        """
        url = f"{self._base}{self._path}"
        sleep_s = float(self.cfg.get("rate_limit_sleep_s", 60))
        max_retries = int(self.cfg.get("rate_limit_max_retries", 2))
        used = 0
        for attempt in range(max_retries + 1):
            response = await client.get(url, params=params)
            used += 1
            if response.status_code == 429:
                if attempt == max_retries:
                    raise _RateLimited
                logger.warning("himalayas: 429, sleeping %ss (attempt %s)", sleep_s, attempt + 1)
                await asyncio.sleep(sleep_s)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise httpx.HTTPError(f"expected an object, got {type(payload).__name__}")
            return payload, used
        raise _RateLimited


class _RateLimited(Exception):
    """Rate limit survived every retry: the walk is truncated, not complete."""
