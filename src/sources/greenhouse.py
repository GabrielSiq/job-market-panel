"""Greenhouse adapter — the CENSUS source, and the panel's actual instrument.

Unlike an aggregator, this is a census over a defined universe: every open requisition at
every watchlist company. No sampling, no moderation queue, no expiry semantics. A posting
vanishes when the company removes it, which is precisely the event of interest. It also
carries non-remote and non-DS roles, which supply the denominators an aggregator
structurally cannot provide.

**Per-company isolation is the critical property here.** One dead board must log an error
for that company alone and return zero rows: it must not fail the run, and above all must
not mark that company's jobs as disappeared (spec correctness rule 1). A bad token returns
a clean 404, which makes this unambiguous.

Remote status is harder here than the spec anticipated. The "Workplace Type" metadata
field turned out to exist on roughly one board in six; the rest fall through to matching
against a messy location string. Hence `remote_source` on every row, and a tri-state
`is_remote` that records "unknown" rather than guessing.
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
    RemoteFinding,
    RunStatus,
    Source,
    SourceRun,
)
from src.normalize import clean_text, strip_html
from src.sources.base import FetchResult, parse_iso, utc_now

logger = logging.getLogger(__name__)


class GreenhouseSource:
    name = "greenhouse"

    def __init__(
        self,
        config: dict[str, Any],
        classifier: Classifier,
        watchlist: list[dict[str, Any]],
    ) -> None:
        self.cfg = config["sources"]["greenhouse"]
        self.classifier = classifier
        self.companies = [
            entry for entry in watchlist if entry.get("ats") == "greenhouse" and entry.get("token")
        ]
        self._base = self.cfg["base_url"].rstrip("/")
        self._template = self.cfg.get("path_template", "/v1/boards/{token}/jobs?content=true")

    # ------------------------------------------------------------------ normalization

    def _remote(
        self, raw: dict[str, Any], location: str | None, description: str | None
    ) -> RemoteFinding:
        """Determine remote status, preferring the strongest available evidence.

        Precedence, measured against a hand-labelled sample of 100 postings:

          1. the board's "Workplace Type" metadata field  - 100% accurate, but present on
             only about one board in six
          2. the location string                          - 97.5% accurate when decisive
          3. work-location prose in the description       - recovers most of the remainder
          4. otherwise NULL

        The location outranks the description because it is the more specific field. The
        single misclassification in the sample came from that ordering (a board whose
        location read "Remote, USA" while the description carried "#LI-Onsite"), which is
        an acceptable trade for the cases it gets right.

        Metadata field names are per-company custom on Greenhouse (real examples: "Quota
        Coverage Type", "Career Site Categories"), so the field is matched by name against
        a configured list rather than assumed to exist.
        """
        for item in raw.get("metadata") or []:
            if not isinstance(item, dict):
                continue
            if self.classifier.is_workplace_type_field(item.get("name")) and (
                finding := self.classifier.remote_from_workplace_type(item.get("value"))
            ):
                return finding
        by_location = self.classifier.remote_from_location(location)
        if by_location.is_remote is not None:
            return by_location
        return self.classifier.remote_from_description(description)

    def _to_posting(
        self, raw: dict[str, Any], company: dict[str, Any], today: Any, fetched_at: Any
    ) -> JobPosting | None:
        title = clean_text(raw.get("title"))
        job_id = raw.get("id")
        if not title or job_id is None:
            logger.warning("greenhouse[%s]: skipping record missing title/id", company["slug"])
            return None

        location = clean_text((raw.get("location") or {}).get("name"))
        role_family = self.classifier.role_family(title)

        # Parsed for every posting even though it is only STORED for tracked families:
        # the remote inference below needs it, and after this run the text is gone for
        # roughly 89% of postings. Deriving now is the difference between a deferred
        # improvement and a permanent hole in the data.
        description = strip_html(raw.get("content"))
        remote = self._remote(raw, location, description)

        departments = [
            clean_text(d.get("name"))
            for d in (raw.get("departments") or [])
            if isinstance(d, dict) and clean_text(d.get("name"))
        ]

        return JobPosting(
            source=Source.GREENHOUSE,
            source_job_id=str(job_id),
            # The watchlist name and slug are authoritative, not the board's echoed
            # company_name: the token was verified against that name at resolution time,
            # and a rebrand mid-panel must not split one company into two series.
            company_name=company["name"],
            company_slug=company["slug"],
            title=title,
            role_family=role_family,
            seniority=self.classifier.seniority(title),
            seniority_source=None,  # Greenhouse has no seniority concept
            location_raw=location,
            country=None,
            is_remote=remote.is_remote,
            remote_source=remote.remote_source,
            employment_type=None,
            department=departments[0] if departments else None,
            # Greenhouse exposes no structured salary; bands appear inside the description
            # where local law requires them. Parsing those is deliberately out of scope for
            # Phase 1 - a bad parse would pollute the compensation series, and the
            # description is retained so it can be parsed later from raw.
            salary_min=None,
            salary_max=None,
            salary_is_estimated=False,
            description_text=description if role_family in TRACKED_FAMILIES else None,
            apply_url=clean_text(raw.get("absolute_url")) or "",
            # `first_published` is the true posting date and is absent from the spec's
            # field list; `updated_at` merely reflects the last edit and would overstate
            # freshness for any req that was ever touched.
            posted_at=parse_iso(raw.get("first_published")) or parse_iso(raw.get("updated_at")),
            first_seen=today,
            last_seen=today,
            fetched_at=fetched_at,
        )

    # ------------------------------------------------------------------------- fetching

    async def _fetch_company(
        self,
        client: httpx.AsyncClient,
        company: dict[str, Any],
        sem: asyncio.Semaphore,
        today: Any,
        fetched_at: Any,
    ) -> FetchResult:
        """Fetch one board. Every failure is contained to this company.

        Returning `status != ok` here removes only this company from diffing, leaving the
        other boards to diff normally - which is the whole point of per-company isolation.
        """
        url = f"{self._base}{self._template.format(token=company['token'])}"
        started = time.monotonic()
        postings: list[JobPosting] = []
        status = RunStatus.OK
        error: str | None = None

        try:
            async with sem:
                response = await client.get(url)
            if response.status_code == 404:
                # The board moved, was renamed, or the token is wrong. NOT evidence that
                # the company closed every requisition.
                status, error = RunStatus.ERROR, "404 - board not found"
            else:
                response.raise_for_status()
                payload = response.json()
                jobs = payload.get("jobs") if isinstance(payload, dict) else None
                if not isinstance(jobs, list):
                    status, error = RunStatus.ERROR, "malformed response: no jobs array"
                else:
                    for raw in jobs:
                        if not isinstance(raw, dict):
                            continue
                        try:
                            posting = self._to_posting(raw, company, today, fetched_at)
                        except Exception as exc:
                            logger.warning(
                                "greenhouse[%s]: invalid record skipped: %s",
                                company["slug"],
                                exc,
                            )
                            continue
                        if posting is not None:
                            postings.append(posting)
                    if not postings:
                        # An empty board is ambiguous - a real hiring freeze looks exactly
                        # like a misconfigured token. Refusing to diff costs at most a
                        # day's disappearance events; guessing wrong corrupts the series.
                        status = RunStatus.EMPTY
        except httpx.HTTPError as exc:
            status, error = RunStatus.ERROR, str(exc)[:200]

        if status is not RunStatus.OK:
            logger.warning(
                "greenhouse[%s]: status=%s %s", company["slug"], status.value, error or ""
            )

        run = SourceRun(
            date=today,
            source=Source.GREENHOUSE,
            status=status,
            company_slug=company["slug"],
            records_fetched=len(postings),
            duration_s=round(time.monotonic() - started, 2),
            error_message=error,
            pages_fetched=1,
            requests_made=1,
        )
        return FetchResult(postings=postings, runs=[run])

    async def fetch(self, client: httpx.AsyncClient) -> FetchResult:
        today = utc_now().date()
        fetched_at = utc_now()
        sem = asyncio.Semaphore(int(self.cfg.get("concurrency", 8)))

        results = await asyncio.gather(
            *(
                self._fetch_company(client, company, sem, today, fetched_at)
                for company in self.companies
            )
        )

        combined = FetchResult()
        for result in results:
            combined.extend(result)

        ok = sum(1 for r in combined.runs if r.allows_diffing)
        logger.info(
            "greenhouse: %s postings from %s/%s boards diffable (%s not ok)",
            len(combined.postings),
            ok,
            len(combined.runs),
            len(combined.runs) - ok,
        )
        return combined
