"""Greenhouse adapter — the CENSUS source, and the panel's original instrument.

Unlike an aggregator, this is a census over a defined universe: every open requisition at
every watchlist company. No sampling, no moderation queue, no expiry semantics. A posting
vanishes when the company removes it, which is precisely the event of interest. It also
carries non-remote and non-DS roles, which supply the denominators an aggregator
structurally cannot provide.

Failure isolation, status rules and concurrency live in `PerCompanyBoardSource`; this file
is only the Greenhouse field mapping.

Remote status is harder here than the spec anticipated. The "Workplace Type" metadata field
turned out to exist on roughly one board in six; the rest fall through to the location
string and then to the description.
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
from src.normalize import clean_text, strip_html
from src.salary import parse_salary
from src.sources.base import parse_iso
from src.sources.board import PerCompanyBoardSource

logger = logging.getLogger(__name__)


class GreenhouseSource(PerCompanyBoardSource):
    name = "greenhouse"
    source = Source.GREENHOUSE

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
        self,
        raw: dict[str, Any],
        company: dict[str, Any],
        today: date,
        fetched_at: datetime,
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
            # Greenhouse exposes no structured salary field, but pay-transparency law
            # means the band is usually printed in the description anyway. Measured on a
            # fresh 20-board sample: 45% of postings yield one. Marked
            # DESCRIPTION_PARSED so it is never pooled with a vendor-supplied field.
            **_salary_fields(parse_salary(description)),
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


def _salary_fields(parsed: object | None) -> dict[str, object]:
    """Map a parsed band onto JobPosting fields, or nothing at all.

    Returning an empty dict on failure is deliberate: the model's defaults leave the salary
    columns null, so a posting with no readable band is indistinguishable from one we never
    tried to read - which is the honest state.
    """
    if parsed is None:
        return {}
    return {
        "salary_min": parsed.salary_min,
        "salary_max": parsed.salary_max,
        "salary_currency": parsed.salary_currency,
        "salary_period": parsed.salary_period,
        "salary_source": parsed.salary_source,
    }
