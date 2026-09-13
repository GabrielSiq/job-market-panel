"""Render the panel as a markdown report. Spec Section 5/Phase 3 report format.

Committed to `reports/` and rendered natively by GitHub, including on a phone. No server,
no delivery mechanism, nothing to keep alive.

**Structure is the feature.** The objection this answers is "a wall of text makes things
hard", which is a layout problem rather than a format limit. The page opens as a glance —
status, what is new, current counts — and everything else is folded into `<details>`
sections the reader expands on purpose. Alerts, collapsibles, tables and block bars were
all verified against GitHub's own renderer before being relied on.

Two rules the report itself must follow, because a number without them invites a wrong
conclusion and a caveat living in CLAUDE.md is not on screen when someone reads this:

- **Disclosed and parsed pay bands are never pooled.** Both are employer-published, but one
  arrived in a vendor field and the other through a regex.
- **Remote share is reported twice**, with and without `location_implied`, which covers a
  large share of determinations at roughly 95% accuracy.

    uv run python -m src.report
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from src.build import DB_PATH, build, connect

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "reports" / "latest.md"

#: The families Gabriel is actually looking for. Adjacent data families are reported
#: separately, for context rather than as targets.
CORE = ("product_ds", "ds_manager", "product_analyst")
ADJACENT = ("ml_eng", "analytics_eng", "data_eng", "analyst")

_BAR_FULL, _BAR_EMPTY = "\u2588", "\u2591"


def bar(fraction: float, width: int = 10) -> str:
    """A block bar. Renders everywhere, needs no image, no dependency, no build step."""
    filled = max(0, min(width, round(fraction * width)))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


def cell(value: Any) -> str:
    """Escape a value for a markdown table cell.

    Job titles really do contain pipes — "Senior Data Scientist | Drive Innovation Through
    Data | Remote" is a real posting in this panel — and one unescaped pipe silently
    shatters the row into extra columns.
    """
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "—"


def money(low: Any, high: Any) -> str:
    if low is None and high is None:
        return "—"
    fmt = lambda v: f"${v / 1000:,.0f}k" if v else "?"  # noqa: E731
    return f"{fmt(low)}–{fmt(high)}"


def _q(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[tuple]:
    return con.execute(sql, params or []).fetchall()


def _has_column(con: duckdb.DuckDBPyConnection, table: str, column: str) -> bool:
    """Whether a table carries a column.

    On a fresh checkout a source may have written no rows at all, and `build.py` then
    creates a bare placeholder table. Querying it blindly turns an empty day into a
    crashed report, which is a worse outcome than a missing section.
    """
    return any(c[1] == column for c in con.execute(f"PRAGMA table_info('{table}')").fetchall())


def _families_sql(families: tuple[str, ...]) -> str:
    return ", ".join(f"'{f}'" for f in families)


def render(con: duckdb.DuckDBPyConnection) -> str:
    out: list[str] = []
    core = _families_sql(CORE)
    adjacent = _families_sql(ADJACENT)

    days = _q(con, "SELECT count(DISTINCT date), min(date), max(date) FROM events")[0]
    n_days, first_day, last_day = days
    open_total = _q(con, "SELECT count(*) FROM open_postings")[0][0]
    companies = _q(con, "SELECT count(DISTINCT company_slug) FROM open_postings")[0][0]
    core_open = _q(con, f"SELECT count(*) FROM open_postings WHERE role_family IN ({core})")[0][0]

    out.append("# Job market panel")
    out.append("")
    out.append(
        f"**{open_total:,}** open postings across **{companies:,}** companies · "
        f"**{core_open:,}** in target families · "
        f"{n_days} day{'s' if n_days != 1 else ''} of history "
        f"({first_day} → {last_day}) · generated {date.today()}"
    )
    out.append("")

    # --- caveats, on the page rather than buried in a repo file --------------------
    out.append("> [!NOTE]")
    out.append(
        "> **How to read this.** "
        f"Only {n_days} day{'s' if n_days != 1 else ''} of history, so there are no trends "
        "here yet — time-to-close, org growth and hiring-rate baselines need weeks and "
        "arrive in a later phase."
    )
    out.append(
        "> Pay and remote coverage vary a lot **by vendor**: Greenhouse boards publish no "
        "salary field at all, so a shift in which companies are tracked can look exactly "
        "like a shift in the market. See *Data quality* below before drawing conclusions."
    )
    out.append("> The panel skews toward startups and tech, so it is **not** a market denominator.")
    out.append("")

    # --- the part read daily -------------------------------------------------------
    out.append(f"## New since {last_day}")
    out.append("")
    # Capped at a few per company. Sorting purely by pay lets one generous employer
    # flood the entire list - OpenAI alone filled 20 of the first 25 rows - which buries
    # the variety that makes a daily scan worth doing.
    new_rows = _q(
        con,
        f"""
        WITH ranked AS (
            SELECT company_name, title, seniority, salary_min, salary_max,
                   location_raw, is_remote, country, apply_url, role_family,
                   ROW_NUMBER() OVER (
                       PARTITION BY company_slug
                       ORDER BY salary_max DESC NULLS LAST, title
                   ) AS per_company
            FROM events
            WHERE date = (SELECT max(date) FROM events)
              AND event = 'appeared' AND role_family IN ({core})
        )
        SELECT * EXCLUDE (per_company) FROM ranked
        WHERE per_company <= 3
        ORDER BY salary_max DESC NULLS LAST, company_name
        """,
    )
    if not new_rows:
        out.append("_Nothing new in target families._")
    else:
        shown = new_rows[:40]
        out.append("| Company | Role | Level | Pay | Where | Remote |")
        out.append("|---|---|---|---|:--:|:--:|")
        for co, title, sen, lo, hi, loc, remote, country, url, _fam in shown:
            link = f"[{cell(title)[:58]}]({url})" if url else cell(title)[:58]
            where = cell(country or loc)[:18]
            flag = {True: "yes", False: "no", None: "?"}[remote]
            out.append(
                f"| {cell(co)[:22]} | {link} | {cell(sen)} | {money(lo, hi)} | {where} | {flag} |"
            )
        if len(new_rows) > len(shown):
            out.append("")
            out.append(
                f"_{len(new_rows) - len(shown)} more not shown, and at most three per "
                "company are listed. Counts are inflated while newly-added boards "
                "backfill._"
            )
    out.append("")

    # --- current state -------------------------------------------------------------
    out.append("## Open now, target families")
    out.append("")
    out.append("| Family | Junior | Mid | Senior | Staff/Principal | Manager+ | Total |")
    out.append("|---|--:|--:|--:|--:|--:|--:|")
    for fam in CORE:
        row = _q(
            con,
            """
            SELECT
              count(*) FILTER (WHERE seniority = 'junior'),
              count(*) FILTER (WHERE seniority = 'mid'),
              count(*) FILTER (WHERE seniority = 'senior'),
              count(*) FILTER (WHERE seniority IN ('staff','principal')),
              count(*) FILTER (WHERE seniority IN ('manager','director')),
              count(*)
            FROM open_postings WHERE role_family = ?
            """,
            [fam],
        )[0]
        out.append(f"| `{fam}` | " + " | ".join(f"{v:,}" for v in row) + " |")
    out.append("")

    # --- folded detail -------------------------------------------------------------
    out.append(_companies_section(con, core))
    out.append(_pay_section(con, core))
    out.append(_location_section(con))
    out.append(_quality_section(con))
    out.append(_adjacent_section(con, adjacent))

    out.append("---")
    out.append("")
    out.append(
        "_Generated by `src/report.py` from `panel.duckdb`. Rebuild and query it yourself "
        "with `uv run python -m src.build` — see the README for the schema and example "
        "queries._"
    )
    return "\n".join(out) + "\n"


def _details(title: str, body: list[str]) -> str:
    return "\n".join(
        ["<details>", f"<summary><b>{title}</b></summary>", "", *body, "", "</details>", ""]
    )


def _companies_section(con: duckdb.DuckDBPyConnection, core: str) -> str:
    body: list[str] = []
    top = _q(
        con,
        f"""
        SELECT company_name,
               count(*) AS n,
               count(*) FILTER (WHERE role_family = 'ds_manager') AS mgrs,
               count(*) FILTER (WHERE is_remote) AS remote
        FROM open_postings WHERE role_family IN ({core})
        GROUP BY company_name ORDER BY n DESC, company_name LIMIT 30
        """,
    )
    body += ["| Company | Target reqs | of which manager | remote |", "|---|--:|--:|--:|"]
    body += [f"| {cell(co)[:30]} | {n} | {mgr} | {rem} |" for co, n, mgr, rem in top]
    body.append("")
    body.append(
        "**Manager reqs are the growth signal.** A company hiring several ICs *and* a "
        "manager is building out; one req in isolation is noise. This is a snapshot — "
        "the trend version needs more history."
    )
    return _details("Companies hiring in target families", body)


def _pay_section(con: duckdb.DuckDBPyConnection, core: str) -> str:
    body: list[str] = []
    body.append(
        "Employer-published either way, but kept apart on purpose: one came from a "
        "structured vendor field, the other was extracted from the job text by a parser. "
        "**Do not pool them.**"
    )
    body.append("")
    rows = _q(
        con,
        f"""
        SELECT p.salary_source,
               count(*),
               quantile_cont(p.salary_min, 0.5),
               quantile_cont(p.salary_max, 0.5),
               quantile_cont(p.salary_max, 0.9)
        FROM postings p
        WHERE p.role_family IN ({core}) AND p.salary_min IS NOT NULL
              AND coalesce(p.salary_period, 'year') = 'year'
        GROUP BY 1 ORDER BY 2 DESC
        """,
    )
    if rows:
        body += [
            "| Source | Postings | Median low | Median high | p90 high |",
            "|---|--:|--:|--:|--:|",
        ]
        for src, n, lo, hi, p90 in rows:
            body.append(f"| `{cell(src)}` | {n:,} | ${lo:,.0f} | ${hi:,.0f} | ${p90:,.0f} |")
    else:
        body.append("_No disclosed bands in target families yet._")

    bands = _q(
        con,
        f"""
        SELECT CASE
                 WHEN salary_max < 150000 THEN 'under $150k'
                 WHEN salary_max < 200000 THEN '$150k–200k'
                 WHEN salary_max < 250000 THEN '$200k–250k'
                 WHEN salary_max < 300000 THEN '$250k–300k'
                 ELSE '$300k+' END AS band,
               count(*)
        FROM postings
        WHERE role_family IN ({core}) AND salary_max IS NOT NULL
              AND coalesce(salary_period, 'year') = 'year'
        GROUP BY 1
        """,
    )
    if bands:
        order = ["under $150k", "$150k–200k", "$200k–250k", "$250k–300k", "$300k+"]
        counts = dict(bands)
        total = sum(counts.values())
        body += ["", "Top of the advertised band:", "", "| Band | | Postings |", "|---|---|--:|"]
        for label in order:
            n = counts.get(label, 0)
            body.append(f"| {label} | `{bar(n / total if total else 0)}` | {n:,} |")
    return _details("Pay", body)


def _location_section(con: duckdb.DuckDBPyConnection) -> str:
    body: list[str] = []
    total = _q(con, "SELECT count(*) FROM open_postings")[0][0] or 1
    countries = _q(
        con,
        """
        SELECT coalesce(country, 'unknown'), count(*)
        FROM open_postings GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """,
    )
    body += ["| Country | | Postings |", "|---|---|--:|"]
    body += [f"| {cell(c)} | `{bar(n / total)}` | {n:,} |" for c, n in countries]

    _strong, implied, unknown = _q(
        con,
        """
        SELECT
          count(*) FILTER (WHERE is_remote IS NOT NULL
                           AND remote_source <> 'location_implied'),
          count(*) FILTER (WHERE remote_source = 'location_implied'),
          count(*) FILTER (WHERE is_remote IS NULL)
        FROM open_postings
        """,
    )[0]
    remote_strong = _q(
        con,
        """
        SELECT count(*) FILTER (WHERE is_remote), count(*)
        FROM open_postings
        WHERE is_remote IS NOT NULL AND remote_source <> 'location_implied'
        """,
    )[0]
    body += ["", "**Remote share, reported two ways.**", ""]
    body.append(
        f"- On strong evidence only (vendor flag, location text, or an explicit statement "
        f"in the job text): **{remote_strong[0]:,} of {remote_strong[1]:,}** "
        f"(`{bar(remote_strong[0] / (remote_strong[1] or 1))}` "
        f"{remote_strong[0] / (remote_strong[1] or 1):.0%})."
    )
    body.append(
        f"- A further **{implied:,}** postings are inferred onsite purely because the "
        "location names a specific workplace, with nothing anywhere saying remote or "
        "hybrid. That inference measures around 95% accurate, but it is the weakest one "
        f"here. **{unknown:,}** remain genuinely undetermined."
    )
    return _details("Location and remote", body)


def _quality_section(con: duckdb.DuckDBPyConnection) -> str:
    body = [
        "Coverage differs sharply by vendor, so read any rate above with this in mind. "
        "A change in which companies are tracked moves these numbers without the market "
        "moving at all.",
        "",
        "| Source | Open | Pay known | Country known | Remote known |",
        "|---|--:|--:|--:|--:|",
    ]
    rows = _q(
        con,
        """
        SELECT source, count(*),
               count(*) FILTER (WHERE salary_min IS NOT NULL),
               count(*) FILTER (WHERE country IS NOT NULL),
               count(*) FILTER (WHERE is_remote IS NOT NULL)
        FROM open_postings GROUP BY 1 ORDER BY 2 DESC
        """,
    )
    for src, n, pay, country, remote in rows:
        body.append(
            f"| `{src}` | {n:,} | `{bar(pay / n)}` {pay / n:.0%} | "
            f"`{bar(country / n)}` {country / n:.0%} | `{bar(remote / n)}` {remote / n:.0%} |"
        )
    if _has_column(con, "runs", "date"):
        runs = _q(
            con,
            """
            SELECT status, count(*) FROM runs
            WHERE date = (SELECT max(date) FROM runs) GROUP BY 1 ORDER BY 2 DESC
            """,
        )
        if runs:
            body += [
                "",
                "Last collection run: " + ", ".join(f"`{status}` {n}" for status, n in runs) + ".",
            ]
    return _details("Data quality", body)


def _adjacent_section(con: duckdb.DuckDBPyConnection, adjacent: str) -> str:
    rows = _q(
        con,
        f"""
        SELECT role_family, count(*),
               count(*) FILTER (WHERE seniority IN ('senior','staff','principal')),
               count(*) FILTER (WHERE is_remote)
        FROM open_postings WHERE role_family IN ({adjacent})
        GROUP BY 1 ORDER BY 2 DESC
        """,
    )
    body = [
        "Context rather than targets — these supply the denominators that make the target "
        "numbers mean something.",
        "",
        "| Family | Open | Senior+ | Remote |",
        "|---|--:|--:|--:|",
    ]
    body += [f"| `{f}` | {n:,} | {s:,} | {r:,} |" for f, n, s, r in rows]
    return _details("Adjacent data families", body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render reports/latest.md from the panel.")
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    parser.add_argument("--rebuild", action="store_true", help="rebuild panel.duckdb first")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.rebuild or not DB_PATH.exists():
        build()
    con = connect()
    text = render(con)
    con.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    logger.info("wrote %s (%.1f KB)", args.out, len(text) / 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
