"""Shared machinery for per-company ATS board adapters.

Greenhouse, Ashby and Lever all work the same way: one board per company, fetched by
token, with each board's failures contained to that company. Only the field mapping
differs.

**This file exists so that logic lives once.** Per-company failure isolation is the most
safety-critical code in the project — spec correctness rule 1, "never infer disappearance
from a failed fetch" — and three hand-maintained copies of it is precisely where a silent
divergence would appear. A subclass supplies a URL, a way to find the jobs array, and a
field mapping; it cannot accidentally get the isolation rules wrong because it does not
implement them.

The contract a subclass must honour:

- `_to_posting` returns None for a record it cannot map. It is called inside a try/except
  that logs and skips, so a single malformed posting never costs a day of collection.
- `_extract_jobs` returns None to mean "this response is malformed" (→ `ERROR`), which is a
  different fact from returning `[]`, meaning "this board is genuinely empty" (→ `EMPTY`).
  Neither permits diffing; conflating them would lose the distinction in the health log.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any

import httpx

from src.classify import Classifier
from src.models import JobPosting, RunStatus, Source, SourceRun
from src.sources.base import FetchResult, utc_now

logger = logging.getLogger(__name__)


def select_companies(watchlist: list[dict[str, Any]], vendor: str) -> list[dict[str, Any]]:
    """Companies this vendor should collect from.

    Excludes `status: unverified` deliberately. Ashby and Lever echo no company name — only
    the token in a URL — so unlike Greenhouse there is no way to confirm that a *guessed*
    token belongs to the company we meant. An unverified board is a real board belonging to
    somebody; collecting it would attribute another company's hiring to this one for the
    life of the panel. Such entries stay in the watchlist awaiting confirmation rather than
    being silently collected or silently dropped.
    """
    return [
        entry
        for entry in watchlist
        if entry.get("ats") == vendor and entry.get("token") and entry.get("status") != "unverified"
    ]


class PerCompanyBoardSource:
    """Base for ATS adapters that fetch one board per company."""

    #: Key in config/sources.yaml, the watchlist `ats` value, and the log prefix.
    name: str = ""
    #: Value written to every posting's `source` field.
    source: Source

    def __init__(
        self,
        config: dict[str, Any],
        classifier: Classifier,
        watchlist: list[dict[str, Any]],
    ) -> None:
        self.cfg = config["sources"][self.name]
        self.classifier = classifier
        self.companies = select_companies(watchlist, self.name)
        self._base = self.cfg["base_url"].rstrip("/")
        self._template = self.cfg["path_template"]

    # ------------------------------------------------------- subclass responsibilities

    def _board_url(self, company: dict[str, Any]) -> str:
        return f"{self._base}{self._template.format(token=company['token'])}"

    async def _fetch_payload(
        self, client: httpx.AsyncClient, company: dict[str, Any]
    ) -> tuple[int, Any]:
        """Fetch one board, returning (status_code, parsed body).

        A hook rather than inline code because a vendor may need more than one request —
        Ashby's documented posting API is opt-in per organisation, so a board can exist
        and render publicly while that API 404s. Overriding this lets a subclass try a
        second endpoint **without reimplementing the failure-isolation rules**, which is
        the whole reason this base class exists.
        """
        response = await client.get(self._board_url(company))
        if response.status_code != 200:
            return response.status_code, None
        return 200, response.json()

    def _extract_jobs(self, payload: Any) -> list[Any] | None:
        """Find the postings in a vendor's response.

        Returns None for a malformed response. Overridden by Lever, which returns a **bare
        array** rather than a wrapper — the classic `payload.get("jobs")` bug yields zero
        rows there, silently.
        """
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        return jobs if isinstance(jobs, list) else None

    def _to_posting(
        self,
        raw: dict[str, Any],
        company: dict[str, Any],
        today: date,
        fetched_at: datetime,
    ) -> JobPosting | None:
        raise NotImplementedError

    # ------------------------------------------------------------------ shared fetching

    async def _fetch_company(
        self,
        client: httpx.AsyncClient,
        company: dict[str, Any],
        sem: asyncio.Semaphore,
        today: date,
        fetched_at: datetime,
    ) -> FetchResult:
        """Fetch one board. Every failure is contained to this company.

        Returning `status != ok` here removes only this company from diffing, leaving every
        other board to diff normally - which is the whole point of per-company isolation.
        """
        started = time.monotonic()
        postings: list[JobPosting] = []
        status = RunStatus.OK
        error: str | None = None

        try:
            async with sem:
                code, payload = await self._fetch_payload(client, company)
            if code == 404:
                # The board moved, was renamed, or the token is wrong. NOT evidence that
                # the company closed every requisition.
                status, error = RunStatus.ERROR, "404 - board not found"
            elif code != 200:
                status, error = RunStatus.ERROR, f"HTTP {code}"
            else:
                jobs = self._extract_jobs(payload)
                if jobs is None:
                    status, error = RunStatus.ERROR, "malformed response: no jobs array"
                else:
                    for raw in jobs:
                        if not isinstance(raw, dict):
                            continue
                        try:
                            posting = self._to_posting(raw, company, today, fetched_at)
                        except Exception as exc:
                            logger.warning(
                                "%s[%s]: invalid record skipped: %s",
                                self.name,
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
                "%s[%s]: status=%s %s", self.name, company["slug"], status.value, error or ""
            )

        run = SourceRun(
            date=today,
            source=self.source,
            status=status,
            company_slug=company["slug"],
            records_fetched=len(postings),
            duration_s=round(time.monotonic() - started, 2),
            error_message=error,
            # A board fetch is a census: every open req at that company, no sampling.
            is_census=self.cfg.get("role", "census") == "census",
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
            "%s: %s postings from %s/%s boards diffable (%s not ok)",
            self.name,
            len(combined.postings),
            ok,
            len(combined.runs),
            len(combined.runs) - ok,
        )
        return combined
