"""Lever adapter.

Smaller coverage win than Ashby (5 watchlist companies against 24) but cheap, since
failure isolation and status rules come from `PerCompanyBoardSource`.

The one thing to get right is the response shape: **Lever returns a bare array**, not a
`{"jobs": [...]}` wrapper like every other vendor here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from src.models import (
    TRACKED_FAMILIES,
    JobPosting,
    RemoteFinding,
    Source,
)
from src.normalize import clean_text
from src.salary import parse_salary
from src.sources.base import parse_epoch
from src.sources.board import PerCompanyBoardSource
from src.sources.greenhouse import _salary_fields

logger = logging.getLogger(__name__)


class LeverSource(PerCompanyBoardSource):
    name = "lever"
    source = Source.LEVER

    def _extract_jobs(self, payload: Any) -> list[Any] | None:
        """Lever returns a **bare array**.

        The classic bug here is `payload.get("jobs") or []`, which yields zero rows without
        raising. Under this project's correctness rules that degrades safely - zero rows
        becomes `EMPTY`, which refuses to diff - so it would cost a day of data rather than
        a wave of false closures. It would still be silent, and it would stay silent.
        """
        if isinstance(payload, list):
            return payload
        return super()._extract_jobs(payload)

    # ------------------------------------------------------------------ normalization

    def _remote(self, raw: dict[str, Any], description: str | None) -> RemoteFinding:
        # `workplaceType` is lowercase here ("remote"/"hybrid"); the taxonomy's
        # workplace_type_map is keyed lowercase already, so it matches without special-casing.
        if finding := self.classifier.remote_from_workplace_type(raw.get("workplaceType")):
            return finding
        categories = raw.get("categories") or {}
        locations = [clean_text(categories.get("location"))] + [
            clean_text(entry) for entry in categories.get("allLocations") or []
        ]
        by_location = self.classifier.remote_from_location(*locations)
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
        # Lever calls the title `text`, not `title`.
        title = clean_text(raw.get("text"))
        job_id = clean_text(raw.get("id"))
        if not title or not job_id:
            logger.warning("lever[%s]: skipping record missing text/id", company["slug"])
            return None

        categories = raw.get("categories") or {}
        description = clean_text(raw.get("descriptionPlain"))
        role_family = self.classifier.role_family(title)
        remote = self._remote(raw, description)

        locations = [clean_text(categories.get("location"))] + [
            clean_text(entry) for entry in categories.get("allLocations") or []
        ]
        location_raw = ", ".join(dict.fromkeys(loc for loc in locations if loc)) or None

        return JobPosting(
            source=Source.LEVER,
            source_job_id=job_id,
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
            employment_type=clean_text(categories.get("commitment")),
            department=clean_text(categories.get("department"))
            or clean_text(categories.get("team")),
            # Lever keeps the pay band in `additionalPlain`, NOT in the description.
            # A fresh sample scored zero on Lever until that was noticed - the parser was
            # reading the wrong field entirely.
            **_salary_fields(parse_salary(clean_text(raw.get("additionalPlain")) or description)),
            salary_is_estimated=False,
            description_text=description if role_family in TRACKED_FAMILIES else None,
            apply_url=clean_text(raw.get("hostedUrl")) or clean_text(raw.get("applyUrl")) or "",
            # Epoch milliseconds; parse_epoch accepts seconds or milliseconds because
            # Himalayas forced that question.
            posted_at=parse_epoch(raw.get("createdAt")),
            first_seen=today,
            last_seen=today,
            fetched_at=fetched_at,
        )
