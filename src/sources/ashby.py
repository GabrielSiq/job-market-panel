"""Ashby adapter — the largest single coverage win in Phase 2.

24 of the companies parked during Phase 1 are on Ashby, against 5 on Lever and 0 on
Workable, and they include the best-matched targets: Plaid, Ramp, Notion, Snowflake,
Airwallex, Modern Treasury, Column, Persona, Sardine.

Ashby is also the only ATS here that publishes **structured compensation**, which
Greenhouse does not expose at all. That lets the compensation metric draw disclosed bands
from the census itself rather than only from an aggregator.

Failure isolation and status rules come from `PerCompanyBoardSource`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from src.models import (
    TRACKED_FAMILIES,
    JobPosting,
    RemoteFinding,
    SalaryPeriod,
    SalarySource,
    Source,
)
from src.normalize import clean_text
from src.salary import parse_salary
from src.sources.base import parse_iso
from src.sources.board import PerCompanyBoardSource
from src.sources.greenhouse import _salary_fields

logger = logging.getLogger(__name__)

#: Ashby states a pay interval as e.g. "1 YEAR". Matched by keyword so an unseen variant
#: degrades to None rather than being silently mapped to the wrong period.
_INTERVAL_KEYWORDS = (
    ("YEAR", SalaryPeriod.YEAR),
    ("ANNUAL", SalaryPeriod.YEAR),
    ("MONTH", SalaryPeriod.MONTH),
    ("WEEK", SalaryPeriod.WEEK),
    ("HOUR", SalaryPeriod.HOUR),
)


class AshbySource(PerCompanyBoardSource):
    name = "ashby"
    source = Source.ASHBY

    # ------------------------------------------------------------------ normalization

    @staticmethod
    def _interval(value: Any) -> SalaryPeriod | None:
        text = str(value or "").upper()
        for keyword, period in _INTERVAL_KEYWORDS:
            if keyword in text:
                return period
        return None

    def _salary(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Extract the base-salary band, and **only** the base salary.

        A compensation tier carries several components - observed types include `Salary`,
        `EquityCashValue`, `Commission` and `Bonus`. Taking anything but `Salary` would
        fold equity and variable pay into what the panel reports as base compensation and
        silently inflate the entire series. Equity is deliberately discarded rather than
        stored: it is not comparable across companies or stages.
        """
        tiers = (raw.get("compensation") or {}).get("compensationTiers") or []
        for tier in tiers:
            for component in tier.get("components") or []:
                if not isinstance(component, dict):
                    continue
                if component.get("compensationType") != "Salary":
                    continue
                minimum, maximum = component.get("minValue"), component.get("maxValue")
                if minimum is None and maximum is None:
                    continue
                return {
                    "salary_min": minimum,
                    "salary_max": maximum,
                    "salary_currency": component.get("currencyCode"),
                    "salary_period": self._interval(component.get("interval")),
                    "salary_period_raw": clean_text(str(component.get("interval") or "")),
                    "salary_source": SalarySource.POSTING_DISCLOSED,
                }
        return {}

    def _remote(self, raw: dict[str, Any], description: str | None) -> RemoteFinding:
        """Remote status from `workplaceType`, then locations, then the description.

        **`isRemote` is deliberately ignored.** Measured across 422 live postings from four
        boards: 293 report `isRemote: true` while `workplaceType` says `Hybrid`, including
        a job whose location is "San Francisco HQ". It evidently means "some remote is
        permitted", not "this is a remote role". Trusting it would mark roughly a quarter
        of the panel remote when it is not - and would look entirely plausible doing so.
        """
        if finding := self.classifier.remote_from_workplace_type(raw.get("workplaceType")):
            return finding
        # secondaryLocations carry entries like "Remote (US)", which are real evidence.
        secondary = [
            clean_text(entry.get("location"))
            for entry in raw.get("secondaryLocations") or []
            if isinstance(entry, dict)
        ]
        by_location = self.classifier.remote_from_location(raw.get("location"), *secondary)
        if by_location.is_remote is not None:
            return by_location
        return self.classifier.remote_from_description(description)

    def _to_posting(
        self,
        raw: dict[str, Any],
        company: dict[str, Any],
        today: date,
        fetched_at: datetime,
    ) -> JobPosting | None:
        # An unlisted posting is not public; collecting it would record an appearance no
        # observer could have seen, and a disappearance when it is merely re-hidden.
        if raw.get("isListed") is False:
            return None

        title = clean_text(raw.get("title"))
        job_id = clean_text(raw.get("id"))
        if not title or not job_id:
            logger.warning("ashby[%s]: skipping record missing title/id", company["slug"])
            return None

        # Already plain text - unlike Greenhouse's `content`, no HTML stripping needed.
        description = clean_text(raw.get("descriptionPlain"))
        role_family = self.classifier.role_family(title)
        remote = self._remote(raw, description)

        locations = [clean_text(raw.get("location"))] + [
            clean_text(entry.get("location"))
            for entry in raw.get("secondaryLocations") or []
            if isinstance(entry, dict)
        ]
        location_raw = ", ".join(dict.fromkeys(loc for loc in locations if loc)) or None

        return JobPosting(
            source=Source.ASHBY,
            source_job_id=job_id,
            # Watchlist identity is authoritative; the board echoes no company name at all,
            # which is precisely why unverified tokens are not collected (see board.py).
            company_name=company["name"],
            company_slug=company["slug"],
            title=title,
            role_family=role_family,
            seniority=self.classifier.seniority(title),
            seniority_source=None,
            location_raw=location_raw,
            country=None,
            is_remote=remote.is_remote,
            remote_source=remote.remote_source,
            employment_type=clean_text(raw.get("employmentType")),
            department=clean_text(raw.get("department")) or clean_text(raw.get("team")),
            # Structured compensation always wins; prose is only a fallback for the ~40%
            # of Ashby postings that publish no tier.
            **(self._salary(raw) or _salary_fields(parse_salary(description))),
            salary_is_estimated=False,
            description_text=description if role_family in TRACKED_FAMILIES else None,
            apply_url=clean_text(raw.get("jobUrl")) or clean_text(raw.get("applyUrl")) or "",
            posted_at=parse_iso(raw.get("publishedAt")),
            first_seen=today,
            last_seen=today,
            fetched_at=fetched_at,
        )
