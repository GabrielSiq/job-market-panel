"""The rules that decide whether the resulting dataset is trustworthy.

Every failure guarded here is SILENT. None of them raises, none shows up in a log as an
error, and each produces numbers that look entirely plausible. They would be discovered in
December, when the baseline they corrupted is already unrecoverable.

Maps to the Phase 1 acceptance criteria:
  - re-running twice in one day is idempotent
  - a deliberately broken source produces a failed SourceRun and ZERO disappearances
  - a simulated 429 mid-query-set produces `partial` and ZERO disappearances
"""

from __future__ import annotations

import gzip
import json
import warnings
from datetime import UTC, date, datetime

import pytest

from src.collect import _enrich_disappearance_titles, diff, replay_open_postings
from src.models import (
    EventType,
    JobPosting,
    PostingEvent,
    RemoteSource,
    RoleFamily,
    RunStatus,
    Seniority,
    Source,
    SourceRun,
)
from src.storage import Storage

DAY1, DAY2, DAY3 = date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12)


def posting(job_id: str, source=Source.GREENHOUSE, company="acme", seen=DAY1) -> JobPosting:
    return JobPosting(
        source=source,
        source_job_id=job_id,
        company_name=company.title(),
        company_slug=company,
        title="Senior Data Scientist",
        role_family=RoleFamily.PRODUCT_DS,
        seniority=Seniority.SENIOR,
        is_remote=True,
        remote_source=RemoteSource.LOCATION_STRING,
        apply_url=f"https://example.com/{job_id}",
        first_seen=seen,
        last_seen=seen,
        fetched_at=datetime.now(UTC),
    )


def run_row(source=Source.GREENHOUSE, status=RunStatus.OK, company="acme", on=DAY1) -> SourceRun:
    return SourceRun(date=on, source=source, status=status, company_slug=company, records_fetched=1)


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(tmp_path)


class TestNeverInferDisappearanceFromFailure:
    """Spec correctness rule 1 - the single most damaging silent failure available.

    A source outage that quietly marks 400 jobs "closed" corrupts every time-to-close
    metric permanently, and nothing about the output looks wrong.
    """

    def test_broken_source_produces_zero_disappearances(self):
        open_now = {"greenhouse:1": _open("greenhouse:1")}
        outcome = diff(
            postings=[],
            runs=[run_row(status=RunStatus.ERROR)],  # 404 / timeout / DNS failure
            open_postings=open_now,
            pending_misses={},
            today=DAY2,
        )
        assert outcome.disappeared == 0
        assert [e for e in outcome.events if e.event is EventType.DISAPPEARED] == []
        assert outcome.skipped_scopes == 1

    @pytest.mark.parametrize("status", [RunStatus.ERROR, RunStatus.PARTIAL, RunStatus.EMPTY])
    def test_no_status_but_ok_permits_diffing(self, status):
        outcome = diff(
            [], [run_row(status=status)], {"greenhouse:1": _open("greenhouse:1")}, {}, DAY2
        )
        assert outcome.disappeared == 0

    def test_outage_does_not_advance_the_miss_counter(self):
        """Subtle and important: if a failed scope still incremented the miss counter, a
        two-day outage would mature every open req into a confirmed disappearance on the
        day the source came back."""
        open_now = {"greenhouse:1": _open("greenhouse:1")}
        pending = {}
        for _ in range(5):  # five consecutive days of outage
            outcome = diff([], [run_row(status=RunStatus.ERROR)], open_now, pending, DAY2)
            pending = outcome.pending_misses
            assert outcome.disappeared == 0
        assert pending == {}, "a failed scope must not accumulate misses"

    def test_one_dead_board_does_not_affect_others(self):
        """Per-company isolation: one 404 among 250 boards must not suppress diffing for
        the rest, nor mark its own jobs closed."""
        open_now = {
            "greenhouse:dead-1": _open("greenhouse:dead-1", company="dead-co"),
            "greenhouse:live-1": _open("greenhouse:live-1", company="live-co"),
        }
        outcome = diff(
            postings=[],
            runs=[
                run_row(status=RunStatus.ERROR, company="dead-co"),
                run_row(status=RunStatus.OK, company="live-co"),
            ],
            open_postings=open_now,
            # live-co's job was already missed yesterday, so it is eligible today
            pending_misses={"greenhouse:live-1": DAY1.isoformat()},
            today=DAY2,
        )
        gone = {e.job_id for e in outcome.events if e.event is EventType.DISAPPEARED}
        assert gone == {"greenhouse:live-1"}, "the healthy board still diffs"
        assert "greenhouse:dead-1" not in gone, "the dead board's jobs are untouched"


class TestPartialRunIsAFailedRun:
    """Spec correctness rule 2. Jobs not seen because the walk was truncated are not
    absent - they were never looked for."""

    def test_rate_limited_partial_yields_zero_disappearances(self):
        open_now = {
            f"himalayas:{i}": _open(f"himalayas:{i}", source="himalayas") for i in range(50)
        }
        outcome = diff(
            postings=[posting("kept", source=Source.HIMALAYAS)],
            runs=[
                SourceRun(
                    date=DAY2,
                    source=Source.HIMALAYAS,
                    status=RunStatus.PARTIAL,
                    records_fetched=1,
                    error_message="429 on 'data scientist' page 3",
                )
            ],
            open_postings=open_now,
            pending_misses={},
            today=DAY2,
        )
        assert outcome.disappeared == 0
        assert outcome.skipped_scopes == 50
        assert outcome.appeared == 1, "rows actually collected are still recorded"


class TestTwoConsecutiveMisses:
    """Spec correctness rule 3. One absent day is more often a pagination hiccup than a
    closed requisition."""

    def test_first_miss_is_held_back(self):
        outcome = diff([], [run_row()], {"greenhouse:1": _open("greenhouse:1")}, {}, DAY2)
        assert outcome.disappeared == 0
        assert outcome.suppressed_by_two_miss_rule == 1
        assert outcome.pending_misses == {"greenhouse:1": DAY2.isoformat()}

    def test_second_consecutive_miss_confirms(self):
        outcome = diff(
            [],
            [run_row()],
            {"greenhouse:1": _open("greenhouse:1")},
            {"greenhouse:1": DAY2.isoformat()},
            DAY3,
        )
        assert outcome.disappeared == 1
        assert outcome.pending_misses == {}

    def test_reappearing_clears_the_miss(self):
        """A job that comes back was a blip. It must not be one miss away from a false
        closure for the rest of its life."""
        outcome = diff(
            [posting("1")],
            [run_row()],
            {"greenhouse:1": _open("greenhouse:1")},
            {"greenhouse:1": DAY2.isoformat()},
            DAY3,
        )
        assert outcome.disappeared == 0
        assert outcome.pending_misses == {}


class TestIdempotency:
    """Re-running the collector twice in one day must not duplicate events.

    Achieved structurally: state is replayed from days *before* today, so the second run
    recomputes an identical event set rather than appending to it.
    """

    def test_same_day_rerun_produces_identical_events(self, store):
        day1 = [
            PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1),
            PostingEvent.from_posting(posting("2"), EventType.APPEARED, DAY1),
        ]
        store.write_events(DAY1, day1)

        observed = [posting("2", seen=DAY2), posting("3", seen=DAY2)]
        runs = [run_row(on=DAY2)]

        first = diff(observed, runs, replay_open_postings(store, DAY2), {}, DAY2)
        store.write_events(DAY2, first.events)
        store.write_pending_misses(first.pending_misses)

        second = diff(
            observed, runs, replay_open_postings(store, DAY2), store.read_pending_misses(), DAY2
        )

        assert [e.model_dump() for e in first.events] == [e.model_dump() for e in second.events]
        assert first.appeared == second.appeared == 1  # job 3 is new
        assert first.disappeared == second.disappeared == 0  # job 1 is only on its first miss

    def test_rerun_does_not_mature_a_first_miss_into_a_disappearance(self, store):
        """The dangerous version of the idempotency bug: run twice on the same day and the
        second run sees a pending miss dated 'yesterday or earlier' and confirms it."""
        store.write_events(
            DAY1, [PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1)]
        )
        open_now = replay_open_postings(store, DAY2)

        first = diff([], [run_row(on=DAY2)], open_now, {}, DAY2)
        second = diff([], [run_row(on=DAY2)], open_now, first.pending_misses, DAY2)

        assert first.disappeared == 0
        assert second.disappeared == 0, "a same-day re-run must not confirm its own first miss"

    def test_written_files_are_byte_identical_on_rerun(self, store):
        events = [PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1)]
        first = store.write_events(DAY1, events).read_bytes()
        second = store.write_events(DAY1, events).read_bytes()
        assert first == second, "an unchanged re-run must produce no git diff"


class TestReplay:
    def test_state_comes_from_replaying_the_log(self, store):
        store.write_events(
            DAY1,
            [
                PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1),
                PostingEvent.from_posting(posting("2"), EventType.APPEARED, DAY1),
            ],
        )
        store.write_events(
            DAY2,
            [
                PostingEvent.from_posting(posting("2"), EventType.DISAPPEARED, DAY2),
                PostingEvent.from_posting(posting("3"), EventType.APPEARED, DAY2),
            ],
        )
        assert set(replay_open_postings(store, DAY3)) == {"greenhouse:1", "greenhouse:3"}

    def test_replay_excludes_today(self, store):
        """The mechanism behind idempotency: today's own events must not be treated as
        prior state, or a re-run would see its own appearances as already-open."""
        store.write_events(
            DAY1, [PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1)]
        )
        store.write_events(
            DAY2, [PostingEvent.from_posting(posting("2"), EventType.APPEARED, DAY2)]
        )
        assert set(replay_open_postings(store, DAY2)) == {"greenhouse:1"}


def _open(job_id: str, source: str = "greenhouse", company: str = "acme"):
    from src.collect import OpenPosting

    return OpenPosting(job_id=job_id, source=source, company_slug=company, first_seen=DAY1)


class TestDedupeKey:
    """Cross-source duplicates are grouped for analysis, never dropped at collection.

    A job seen on both an aggregator and its company's own ATS board is two genuine
    observations of our own looking. Raw capture stays immutable; the key lets the analysis
    layer collapse them.
    """

    def test_same_req_across_sources_shares_a_key(self):
        ats = posting("gh-1", source=Source.GREENHOUSE, company="acme")
        aggregator = posting("him-1", source=Source.HIMALAYAS, company="acme")
        assert ats.job_id != aggregator.job_id, "identity stays per-source"
        assert ats.dedupe_key == aggregator.dedupe_key == "acme:senior data scientist"

    def test_key_survives_dirty_titles(self):
        """Greenhouse emits 'Senior Data Scientist ' and 'Senior Data Scientist' as
        separate values on one board; they must not become two groups."""
        a = posting("1")
        b = posting("2")
        b.title = "Senior Data Scientist "
        assert a.dedupe_key == posting("2").dedupe_key

    def test_key_reaches_the_event_log(self):
        event = PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1)
        assert event.dedupe_key == "acme:senior data scientist"

    def test_key_is_a_grouping_hint_not_an_identity(self):
        """Two genuinely distinct reqs with the same title at one company share a key -
        common for multi-location postings. job_id remains the only identity."""
        a, b = posting("1"), posting("2")
        assert a.dedupe_key == b.dedupe_key
        assert a.job_id != b.job_id


class TestOnlyACensusMayCloseAPosting:
    """Correctness rule 1, generalized beyond outages.

    An aggregator is queried with a fixed query set sorted by recency and paged only a few
    pages deep, so its observation window slides forward every day. A posting that falls
    out of that window has not closed — it aged past where we look.

    Measured on real data before this was fixed: 217 of 223 closures came from the
    aggregator, and their ages clustered at exactly `lookback_days` after posting (84 at
    four days, 39 at five). Those listings live about 60 days. The panel was manufacturing
    a closure on a fixed delay after posting, for every row, forever.
    """

    @staticmethod
    def _run(source: Source, is_census: bool, company: str | None = None) -> SourceRun:
        return SourceRun(
            date=DAY2,
            source=source,
            status=RunStatus.OK,
            company_slug=company,
            is_census=is_census,
            records_fetched=1,
        )

    def test_a_moving_window_never_closes_anything(self):
        open_now = {"himalayas:1": _open("himalayas:1", source="himalayas", company="acme")}
        outcome = diff(
            postings=[],
            runs=[self._run(Source.HIMALAYAS, is_census=False)],
            open_postings=open_now,
            pending_misses={"himalayas:1": DAY1.isoformat()},  # already missed once
            today=DAY2,
        )
        assert outcome.disappeared == 0, "an aggregator may never record a closure"
        assert outcome.skipped_scopes == 1

    def test_a_census_still_closes_normally(self):
        open_now = {"greenhouse:1": _open("greenhouse:1")}
        outcome = diff(
            postings=[],
            runs=[self._run(Source.GREENHOUSE, is_census=True, company="acme")],
            open_postings=open_now,
            pending_misses={"greenhouse:1": DAY1.isoformat()},
            today=DAY2,
        )
        assert outcome.disappeared == 1

    def test_queued_misses_are_dropped_not_left_to_linger(self):
        """A miss queued against a never-diffable source can never mature, so keeping it
        would grow the pending file forever with entries that do nothing."""
        outcome = diff(
            postings=[],
            runs=[self._run(Source.HIMALAYAS, is_census=False)],
            open_postings={"himalayas:1": _open("himalayas:1", source="himalayas")},
            pending_misses={"himalayas:1": DAY1.isoformat()},
            today=DAY2,
        )
        assert outcome.pending_misses == {}


class TestDisappearanceBackfill:
    """Disappearance rows are built empty and filled in from a previous day's raw JSON.

    That is the one place in the collector where a model is mutated after construction,
    and it silently produced rows whose enum fields held plain strings - equal to the
    enum (these are StrEnums) but not identical, while this codebase compares enums
    with `is`.
    """

    def _closed(self, store, prior: PostingEvent) -> PostingEvent:
        store.write_events(DAY1, [prior])
        event = PostingEvent(
            date=DAY2,
            event=EventType.DISAPPEARED,
            job_id=prior.job_id,
            source=prior.source,
            company_slug=prior.company_slug,
            company_name=prior.company_slug,
            title="",
            apply_url="",
        )
        _enrich_disappearance_titles([event], store, DAY2)
        return event

    def test_backfilled_enums_are_enums_not_strings(self, store):
        event = self._closed(
            store, PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1)
        )
        assert event.role_family is RoleFamily.PRODUCT_DS
        assert event.seniority is Seniority.SENIOR

    def test_backfill_serializes_without_warnings(self, store):
        """A pydantic serializer warning is only cosmetic until it hides a real one."""
        event = self._closed(
            store, PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1)
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            event.model_dump_json()

    def test_remote_provenance_travels_with_the_verdict(self, store):
        """Copying `is_remote` while leaving `remote_source` at its UNKNOWN default
        publishes a confident remote status resting on no stated evidence."""
        event = self._closed(
            store, PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1)
        )
        assert event.is_remote is True
        assert event.remote_source is RemoteSource.LOCATION_STRING

    def test_a_retired_taxonomy_value_does_not_kill_the_run(self, store):
        """Renaming enum members is forbidden, so this should never fire - but one bad
        historical row must not cost a whole day's collection, and the fields either
        side of it must still fill in."""
        prior = PostingEvent.from_posting(posting("1"), EventType.APPEARED, DAY1)
        # A row as an earlier taxonomy might have written it. Storage serializes plain
        # dicts as readily as models, which is what lets this be written at all.
        raw = prior.model_dump(mode="json") | {"role_family": "a_family_we_retired"}
        store.write_events(DAY1, [raw])

        event = PostingEvent(
            date=DAY2,
            event=EventType.DISAPPEARED,
            job_id=prior.job_id,
            source=prior.source,
            company_slug=prior.company_slug,
            company_name=prior.company_slug,
            title="",
            apply_url="",
        )
        _enrich_disappearance_titles([event], store, DAY2)

        assert event.role_family is None
        assert event.seniority is Seniority.SENIOR
        assert event.title == prior.title


class TestCrossRepoInterface:
    """`data/latest/new_postings.jsonl.gz` is the one file another repo reads (spec 3.2).

    Compressed since 2026-09-20: it is rewritten wholesale every run, so git stored a new
    blob of the whole thing daily and had accumulated 105.7 MB across 22 versions - all of
    it duplicating `data/postings/<date>.jsonl.gz` uncompressed. Compression must not cost
    a single field, and description text is the part that would hurt to lose, so both are
    asserted rather than assumed.
    """

    def _posting_with_description(self, job_id: str) -> JobPosting:
        p = posting(job_id)
        return p.model_copy(update={"description_text": "Lead experimentation. " * 200})

    def test_the_interface_is_gzipped(self, store):
        path = store.write_latest([self._posting_with_description("1")])
        assert path.name == "new_postings.jsonl.gz"
        assert gzip.decompress(path.read_bytes())

    def test_compression_loses_no_field_and_no_description(self, store):
        original = [self._posting_with_description(str(i)) for i in range(5)]
        path = store.write_latest(original)

        rows = list(store.read_jsonl_gz(path))
        assert len(rows) == len(original)
        assert [r["job_id"] for r in rows] == [p.job_id for p in original]
        # Every serialized field survives, computed ones included.
        assert set(rows[0]) == set(json.loads(original[0].model_dump_json()))
        assert all(r["description_text"] == original[0].description_text for r in rows)

    def test_bytes_match_what_an_uncompressed_write_would_have_produced(self, store):
        """The guarantee that makes this safe: decompressing returns the previous format
        exactly, so a consumer written against either sees identical content."""
        original = [self._posting_with_description(str(i)) for i in range(3)]
        expected = "".join(p.model_dump_json() + "\n" for p in original).encode()

        path = store.write_latest(original)
        assert gzip.decompress(path.read_bytes()) == expected
