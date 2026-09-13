"""The analytics layer: `panel.duckdb` and the markdown report.

Two properties matter more than the rest:

- **`open_postings` must mean exactly what the collector means by "currently open".**
  Two implementations of that definition drifting apart would be worse than having one.
- **The taxonomy is applied at build time, not trusted from the log.** Otherwise a rule
  change only affects postings collected afterwards, and the series carries a silent
  discontinuity at the moment the rules changed — the exact artifact spec 3.5 exists to
  prevent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.build import build, connect, reclassify
from src.collect import replay_open_postings
from src.models import (
    EventType,
    JobPosting,
    PostingEvent,
    RemoteSource,
    RoleFamily,
    Seniority,
    Source,
)
from src.report import bar, cell, money, render
from src.storage import Storage

DAY1, DAY2, DAY3 = date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12)


def posting(job_id: str, title: str = "Senior Data Scientist", company: str = "acme") -> JobPosting:
    return JobPosting(
        source=Source.GREENHOUSE,
        source_job_id=job_id,
        company_name=company.title(),
        company_slug=company,
        title=title,
        role_family=RoleFamily.PRODUCT_DS,
        seniority=Seniority.SENIOR,
        is_remote=True,
        remote_source=RemoteSource.LOCATION_STRING,
        country="US",
        region="CA",
        salary_min=200_000,
        salary_max=260_000,
        apply_url=f"https://example.com/{job_id}",
        first_seen=DAY1,
        last_seen=DAY1,
        fetched_at=datetime.now(UTC),
    )


@pytest.fixture
def panel(tmp_path):
    """A small panel on disk: two days, one closure."""
    store = Storage(tmp_path / "data")
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
    store.write_postings(DAY1, [posting("1"), posting("2")])
    store.write_runs(DAY1, [])
    db = tmp_path / "panel.duckdb"
    build(db, storage=store)
    return db, store


class TestOpenPostings:
    def test_matches_the_collectors_definition(self, panel):
        """The SQL view and `replay_open_postings` must agree exactly. This is the whole
        reason the view is defined the way it is."""
        db, store = panel
        con = connect(db)
        sql = {r[0] for r in con.execute("SELECT job_id FROM open_postings").fetchall()}
        con.close()
        python = set(replay_open_postings(store, before=DAY3 + timedelta(days=1)))
        assert sql == python == {"greenhouse:1", "greenhouse:3"}

    def test_a_closed_posting_is_excluded(self, panel):
        db, _ = panel
        con = connect(db)
        assert (
            con.execute(
                "SELECT count(*) FROM open_postings WHERE job_id = 'greenhouse:2'"
            ).fetchone()[0]
            == 0
        )
        con.close()

    def test_job_spans_records_closure(self, panel):
        db, _ = panel
        con = connect(db)
        closed = con.execute(
            "SELECT first_seen, disappeared_on, is_closed FROM job_spans "
            "WHERE job_id = 'greenhouse:2'"
        ).fetchone()
        con.close()
        assert closed == (DAY1, DAY2, True)


class TestReclassification:
    def test_current_taxonomy_is_applied_to_history(self, tmp_path):
        """A posting logged under an old rule is reclassified on rebuild.

        Real case: "Engineering Manager, Core Experimentation" was collected as
        `product_ds` before the taxonomy excluded platform-engineering titles. Without
        this, it would stay `product_ds` forever and the target-family count would carry
        a step change on the day the rule was fixed.
        """
        store = Storage(tmp_path / "data")
        stale = PostingEvent.from_posting(
            posting("9", title="Engineering Manager, Core Experimentation"),
            EventType.APPEARED,
            DAY1,
        )
        stale.role_family = RoleFamily.PRODUCT_DS  # what the old rules decided
        store.write_events(DAY1, [stale])
        db = tmp_path / "panel.duckdb"
        build(db, storage=store)

        con = connect(db)
        logged, current = con.execute(
            "SELECT role_family_logged, role_family FROM events WHERE job_id = 'greenhouse:9'"
        ).fetchone()
        con.close()
        assert logged == "product_ds", "what was recorded at collection is preserved"
        assert current == "other", "and the current rules are applied on top"

    def test_reclassify_is_a_noop_without_titles(self, panel):
        db, _ = panel
        con = connect(db)
        assert reclassify(con, "runs") == 0
        con.close()


class TestMarkdownHelpers:
    def test_cell_escapes_pipes(self):
        """Job titles really do contain pipes — "Senior Data Scientist | Drive Innovation
        Through Data | Remote" is a real posting here — and one unescaped pipe shatters
        the table row into extra columns."""
        assert cell("Senior DS | Remote") == "Senior DS \\| Remote"

    def test_cell_handles_missing(self):
        assert cell(None) == "—"
        assert cell("   ") == "—"

    def test_bar_is_fixed_width(self):
        assert len(bar(0.0)) == len(bar(0.5)) == len(bar(1.0)) == 10
        assert bar(1.0) == "█" * 10
        assert bar(0.0) == "░" * 10

    def test_money_formats_and_degrades(self):
        assert money(200_000, 260_000) == "$200k–$260k"
        assert money(None, None) == "—"


class TestReport:
    def test_renders_the_verified_markdown_features(self, panel):
        """Only features checked against GitHub's own renderer are used: alerts,
        collapsibles, tables and block bars."""
        db, _ = panel
        con = connect(db)
        text = render(con)
        con.close()
        assert "> [!NOTE]" in text
        assert "<details>" in text and "<summary>" in text
        assert text.count("<details>") == text.count("</details>")
        assert "█" in text or "░" in text

    def test_states_the_caveats_on_the_page(self, panel):
        """A caveat living in CLAUDE.md is not on screen when this is read on a phone."""
        db, _ = panel
        con = connect(db)
        text = render(con).lower()
        con.close()
        assert "no trends" in text
        assert "by vendor" in text
        assert "not** a market denominator" in text or "not a market denominator" in text

    def test_reports_remote_share_both_ways(self, panel):
        db, _ = panel
        con = connect(db)
        text = render(con)
        con.close()
        assert "strong evidence" in text
        assert "location names a specific workplace" in text

    def test_never_pools_disclosed_and_parsed_pay(self, panel):
        db, _ = panel
        con = connect(db)
        text = render(con)
        con.close()
        assert "Do not pool them." in text
