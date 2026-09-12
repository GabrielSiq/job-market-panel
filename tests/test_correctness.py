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

from datetime import UTC, date, datetime

import pytest

from src.collect import diff, replay_open_postings
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
