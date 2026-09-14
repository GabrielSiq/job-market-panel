"""The collector: fetch -> normalize -> classify -> diff -> append.

Deterministic by design. **No LLM calls in this path, ever** (spec 3.1): a longitudinal
dataset gathered by something that re-decides its schema each morning is useless for
statistics, because schema drift becomes indistinguishable from market movement.

The diff step is where this project's data can be silently destroyed, so the rules it
enforces are worth stating plainly:

1. A source that errored, timed out, was rate-limited, truncated, or returned nothing is
   excluded from disappearance detection - **per company** for ATS boards. "We did not
   look" is not the same fact as "it was not there".
2. A posting must be missed on **two consecutive runs** before a disappearance is
   recorded. One absent day is more often a pagination hiccup than a closed req.
3. Current state is replayed from the event log rather than read from a mutable file.
4. State is replayed from days *before today*, so re-running on the same day recomputes
   an identical set of events instead of appending duplicates.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from src.classify import load_classifier
from src.models import (
    TRACKED_FAMILIES,
    EventType,
    JobPosting,
    PostingEvent,
    SourceRun,
)
from src.sources.ashby import AshbySource
from src.sources.base import FetchResult, build_client, utc_now
from src.sources.greenhouse import GreenhouseSource
from src.sources.himalayas import HimalayasSource
from src.sources.lever import LeverSource
from src.storage import Storage

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

ScopeKey = tuple[str, str | None]


@dataclass
class OpenPosting:
    """A posting believed open, reconstructed from the event log."""

    job_id: str
    source: str
    company_slug: str
    first_seen: date


@dataclass
class DiffOutcome:
    events: list[PostingEvent] = field(default_factory=list)
    pending_misses: dict[str, str] = field(default_factory=dict)
    appeared: int = 0
    disappeared: int = 0
    skipped_scopes: int = 0
    suppressed_by_two_miss_rule: int = 0


def scope_key(source: str, company_slug: str | None, per_company_sources: set[str]) -> ScopeKey:
    """The unit a run is entitled to diff.

    ATS sources are collected per company, so each board is its own scope: one dead board
    must not suppress diffing for the other 250, nor mark its own jobs closed. Aggregator
    sources are collected as a whole, so the scope is the source itself.
    """
    return (source, company_slug) if source in per_company_sources else (source, None)


def replay_open_postings(storage: Storage, before: date) -> dict[str, OpenPosting]:
    """Reconstruct which postings are currently open by replaying the event log.

    Deliberately excludes `before` (today) so that re-running the collector on the same
    day starts from the same prior state and recomputes identical events - the property
    that makes a same-day re-run idempotent.
    """
    open_postings: dict[str, OpenPosting] = {}
    for path in storage.event_files(before=before):
        for row in storage.read_jsonl_gz(path):
            job_id = row.get("job_id")
            if not job_id:
                continue
            if row.get("event") == EventType.APPEARED.value:
                open_postings.setdefault(
                    job_id,
                    OpenPosting(
                        job_id=job_id,
                        source=row.get("source", ""),
                        company_slug=row.get("company_slug", ""),
                        first_seen=date.fromisoformat(row["date"]),
                    ),
                )
            else:
                open_postings.pop(job_id, None)
    return open_postings


def diff(
    postings: list[JobPosting],
    runs: list[SourceRun],
    open_postings: dict[str, OpenPosting],
    pending_misses: dict[str, str],
    today: date,
) -> DiffOutcome:
    per_company_sources = {r.source.value for r in runs if r.company_slug is not None}
    # Two conditions, both meaning "we cannot conclude absence here":
    #   - the run did not complete cleanly (status != ok), or
    #   - the source samples a moving window rather than observing a census.
    # `FetchResult.diffable_scopes` applies the same rule; they must stay in step.
    diffable: set[ScopeKey] = {
        scope_key(r.source.value, r.company_slug, per_company_sources)
        for r in runs
        if r.allows_diffing and r.is_census
    }

    observed: dict[str, JobPosting] = {p.job_id: p for p in postings}
    outcome = DiffOutcome(pending_misses=dict(pending_misses))

    # --- appearances -------------------------------------------------------------
    for job_id, posting in observed.items():
        # A posting seen again clears any in-flight miss: it was a blip, not a closure.
        outcome.pending_misses.pop(job_id, None)
        if job_id not in open_postings:
            outcome.events.append(PostingEvent.from_posting(posting, EventType.APPEARED, today))
            outcome.appeared += 1

    # --- disappearances ----------------------------------------------------------
    for job_id, open_posting in open_postings.items():
        if job_id in observed:
            continue
        scope = scope_key(open_posting.source, open_posting.company_slug, per_company_sources)
        if scope not in diffable:
            # We did not look here today. Absence is not evidence of closure, and the
            # pending-miss counter must NOT advance either: a week-long source outage
            # would otherwise silently mature every open req into a disappearance.
            #
            # Any miss already queued for this job is dropped rather than left to linger.
            # For a source that is never diffable at all - an aggregator sampling a moving
            # window - a queued miss can never mature, so keeping it would grow the
            # pending file forever with entries that do nothing.
            outcome.pending_misses.pop(job_id, None)
            outcome.skipped_scopes += 1
            continue

        first_missed = outcome.pending_misses.get(job_id)
        if first_missed is None or first_missed >= today.isoformat():
            # First miss (or a re-run of the same day). Record the candidate and wait.
            outcome.pending_misses.setdefault(job_id, today.isoformat())
            outcome.suppressed_by_two_miss_rule += 1
            continue

        outcome.events.append(
            PostingEvent(
                date=today,
                event=EventType.DISAPPEARED,
                job_id=job_id,
                source=open_posting.source,
                company_slug=open_posting.company_slug,
                company_name=open_posting.company_slug,
                title="",
                apply_url="",
            )
        )
        outcome.disappeared += 1
        outcome.pending_misses.pop(job_id, None)

    return outcome


#: Per-company ATS adapters. All share PerCompanyBoardSource, so adding a vendor is a
#: field mapping plus an entry here - the failure-isolation rules cannot diverge between
#: them because no adapter implements them.
BOARD_SOURCES = (GreenhouseSource, AshbySource, LeverSource)


def build_sources(config: dict[str, Any], watchlist: list[dict[str, Any]], classifier):
    sources: list[Any] = []
    if config["sources"].get("himalayas", {}).get("enabled", True):
        sources.append(HimalayasSource(config, classifier))
    for cls in BOARD_SOURCES:
        if config["sources"].get(cls.name, {}).get("enabled", True):
            sources.append(cls(config, classifier, watchlist))
    return sources


async def fetch_all(config: dict[str, Any], sources: list[Any]) -> FetchResult:
    combined = FetchResult()
    async with build_client(config) as client:
        for source in sources:
            try:
                combined.extend(await source.fetch(client))
            except Exception:
                # A source blowing up entirely must not take the others down with it, and
                # must not be mistaken for "this source saw nothing".
                logger.exception("source %s failed outright", getattr(source, "name", source))
    return combined


def _enrich_disappearance_titles(events: list[PostingEvent], storage: Storage, today: date) -> None:
    """Backfill company_name/title on disappearance rows from the original appearance.

    The event log is slim but must stay self-describing: "which DS roles closed this week"
    should be answerable from the log alone, without a join back through history.
    """
    wanted = {e.job_id for e in events if e.event is EventType.DISAPPEARED}
    if not wanted:
        return
    found: dict[str, dict[str, Any]] = {}
    for path in reversed(storage.event_files(before=today)):
        for row in storage.read_jsonl_gz(path):
            if row.get("job_id") in wanted and row.get("event") == EventType.APPEARED.value:
                found.setdefault(row["job_id"], row)
        if len(found) == len(wanted):
            break
    for event in events:
        if event.event is EventType.DISAPPEARED and (row := found.get(event.job_id)):
            event.company_name = row.get("company_name") or event.company_name
            event.title = row.get("title") or ""
            event.role_family = row.get("role_family")
            event.seniority = row.get("seniority")
            event.is_remote = row.get("is_remote")
            event.location_raw = row.get("location_raw")
            event.apply_url = row.get("apply_url") or ""


def collect(
    config_dir: Path = CONFIG_DIR,
    storage: Storage | None = None,
    dry_run: bool = False,
    today: date | None = None,
) -> DiffOutcome:
    store = storage or Storage()
    config = yaml.safe_load((config_dir / "sources.yaml").read_text())
    watchlist = yaml.safe_load((config_dir / "watchlist.yaml").read_text())["companies"]
    classifier = load_classifier(config_dir / "taxonomy.yaml")
    run_date = today or utc_now().date()

    sources = build_sources(config, watchlist, classifier)
    result = asyncio.run(fetch_all(config, sources))

    open_postings = replay_open_postings(store, before=run_date)
    outcome = diff(
        result.postings, result.runs, open_postings, store.read_pending_misses(), run_date
    )
    _enrich_disappearance_titles(outcome.events, store, run_date)

    ok_scopes = sum(1 for r in result.runs if r.allows_diffing)
    logger.info(
        "collected %s postings | %s scopes diffable of %s | appeared=%s disappeared=%s "
        "(held back by two-miss rule: %s, scopes not looked at: %s)",
        len(result.postings),
        ok_scopes,
        len(result.runs),
        outcome.appeared,
        outcome.disappeared,
        outcome.suppressed_by_two_miss_rule,
        outcome.skipped_scopes,
    )

    if dry_run:
        logger.warning("dry run: nothing written")
        return outcome

    appeared_ids = {e.job_id for e in outcome.events if e.event is EventType.APPEARED}
    # Full records are captured on first appearance only. A description is rewritten
    # rarely, and Phase 4 scores a posting near its first sighting; re-storing every
    # description daily would multiply the storage budget for nearly no information.
    new_tracked = [
        p for p in result.postings if p.job_id in appeared_ids and p.role_family in TRACKED_FAMILIES
    ]

    store.write_events(run_date, outcome.events)
    store.write_postings(run_date, new_tracked)
    store.write_runs(run_date, result.runs)
    store.write_latest(new_tracked)
    store.write_pending_misses(outcome.pending_misses)
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect one day of the job-market panel.")
    parser.add_argument("--dry-run", action="store_true", help="fetch and diff, write nothing")
    parser.add_argument("--config", type=Path, default=CONFIG_DIR)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    collect(config_dir=args.config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
