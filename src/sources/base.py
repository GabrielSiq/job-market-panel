"""The contract every source adapter conforms to.

Four vendors, four vocabularies. Greenhouse returns `{"jobs": [...]}` with `title` and
`absolute_url`; Lever returns a **bare array** with the title under `text`; Ashby wraps in
`jobs` and exposes `isRemote`. This mismatch is the most likely source of silent data
corruption in the project, so per-vendor adapters map into one `JobPosting` and **nothing
downstream ever touches raw vendor JSON**.

Two rules every adapter must honour:

1. **A record that fails validation is logged and skipped, never raised.** One malformed
   posting must not cost a day of collection for every other company.
2. **An adapter reports what it actually looked at.** Returning fewer rows than expected is
   only safe if the `SourceRun` says so; the differ refuses to diff anything not marked
   `ok`. Returning a short list with `status: ok` is how a source outage turns into
   hundreds of false "closed" events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from src.models import CollectionScope, JobPosting, SourceRun

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """What one source produced in one run.

    `runs` is the health log and the diffing gate: one row per source, or per company for
    ATS boards. `postings` may span several scopes; the differ pairs them up by
    (source, company_slug).
    """

    postings: list[JobPosting] = field(default_factory=list)
    runs: list[SourceRun] = field(default_factory=list)

    @property
    def diffable_scopes(self) -> set[tuple[str, str | None]]:
        """Scopes this run is entitled to diff — i.e. where we genuinely looked, over a
        universe that does not move.

        Two ways to fail that test, and both mean "we did not look" rather than "it was
        not there" (spec correctness rules 1 and 2):

        - the run did not complete cleanly (`status != ok`), or
        - the source observes a moving sample rather than a census (`is_census` false).
        """
        return {
            CollectionScope(source=r.source, company_slug=r.company_slug).key()
            for r in self.runs
            if r.allows_diffing and r.is_census
        }

    def extend(self, other: FetchResult) -> None:
        self.postings.extend(other.postings)
        self.runs.extend(other.runs)


class Source(Protocol):
    """Implemented by every adapter."""

    name: str

    async def fetch(self, client: httpx.AsyncClient) -> FetchResult: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_epoch(value: Any) -> datetime | None:
    """Parse a Unix timestamp that may be in seconds *or* milliseconds.

    Not defensive programming for its own sake. The Himalayas OpenAPI spec documents
    `pubDate` as milliseconds and gives a millisecond example, but the live API returns
    **seconds**. Trusting the documentation yields dates around the year 58000, which
    would sail through as a valid datetime and quietly poison every freshness metric.
    The threshold below sits far above any plausible second-precision timestamp and far
    below any plausible millisecond one.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 1e11:  # anything this large must be milliseconds
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_client(config: dict[str, Any]) -> httpx.AsyncClient:
    http = config.get("http", {})
    return httpx.AsyncClient(
        timeout=http.get("timeout_s", 30),
        headers={"User-Agent": http.get("user_agent", "job-market-panel/0.1")},
        follow_redirects=True,
    )
