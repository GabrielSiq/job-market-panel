"""Build `panel.duckdb` from the raw event log. Spec 3.5.

**Derived and disposable.** Gitignored, rebuilt from raw on every run. Delete it at any
time; nothing is lost. That is the reprocessability principle in practice — the event log
is the truth and everything else is a convenience.

It serves two readers: `src/report.py`, and Gabriel querying the panel directly. The second
matters more. A report answers only the questions I thought to ask; a database answers his.

DuckDB reads the gzipped JSONL globs natively, which was worth checking rather than
assuming: the plan called for loading rows through `Storage.read_jsonl_gz` because the
documentation was inconclusive. Measured, the direct read does 26k rows in **0.14s against
53s** for row-by-row inserts, and infers the column types correctly. `union_by_name` keeps
it safe when a later day's file carries a field an earlier one did not.

    uv run python -m src.build
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb

from src.classify import load_classifier
from src.location import parse_location
from src.storage import Storage

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "panel.duckdb"

#: Current state, by replaying the log. This MUST mean the same thing as
#: `replay_open_postings` in src/collect.py, the collector's tested definition of
#: "currently open" — two implementations that disagreed would be worse than none, and
#: tests/test_build.py asserts they match.
#:
#: Ordering by `event DESC` breaks same-day ties toward 'disappeared' ('d' > 'a'), matching
#: the collector: a posting that appeared and closed on one day is closed.
OPEN_POSTINGS_SQL = """
CREATE OR REPLACE VIEW open_postings AS
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY job_id ORDER BY date DESC, event DESC
    ) AS rn
    FROM events
)
SELECT * EXCLUDE (rn) FROM ranked WHERE rn = 1 AND event = 'appeared';
"""

#: First and last observation of each posting — the input to survival analysis once there
#: are enough days for it to mean anything (Phase 3). Built now so the shape is settled.
JOB_SPANS_SQL = """
CREATE OR REPLACE VIEW job_spans AS
SELECT
    job_id,
    any_value(source)       AS source,
    any_value(company_slug) AS company_slug,
    any_value(company_name) AS company_name,
    any_value(title)        AS title,
    any_value(role_family)  AS role_family,
    any_value(seniority)    AS seniority,
    min(date)               AS first_seen,
    max(date) FILTER (WHERE event = 'appeared')    AS last_appeared,
    max(date) FILTER (WHERE event = 'disappeared') AS disappeared_on,
    bool_or(event = 'disappeared')                 AS is_closed
FROM events GROUP BY job_id;
"""


def _load(con: duckdb.DuckDBPyConnection, table: str, glob: str) -> int:
    """Create a table from a JSONL glob, or an empty placeholder if nothing matches."""
    try:
        con.execute(
            f"CREATE OR REPLACE TABLE {table} AS "
            f"SELECT * FROM read_json_auto(?, union_by_name=true)",
            [glob],
        )
    except duckdb.Error as exc:
        # No files yet, on a fresh checkout. An empty table beats a missing one: the
        # report then renders "no data" rather than crashing the workflow.
        logger.warning("no data for %s (%s): %s", table, glob, str(exc)[:120])
        con.execute(f"CREATE OR REPLACE TABLE {table} (job_id VARCHAR)")
        return 0
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _cast_json_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """Cast JSON-typed columns to VARCHAR.

    DuckDB infers a column's type from the values present. A column that happens to be
    **entirely null** across the files it read - `salary_period` on a day when nothing
    published one, say - is inferred as JSON, and every later `coalesce(col, 'year')`
    against it then fails with a conversion error.

    This is a real production risk, not just a test artifact: it depends on what the data
    looked like that day, so it would appear without warning and only on some rebuilds.
    No column here is legitimately JSON, so casting them all is safe.
    """
    json_columns = [
        name
        for name, dtype in (
            (c[1], c[2]) for c in con.execute(f"PRAGMA table_info('{table}')").fetchall()
        )
        if dtype.upper() == "JSON"
    ]
    for name in json_columns:
        con.execute(f'ALTER TABLE {table} ALTER "{name}" TYPE VARCHAR')
    return json_columns


def reclassify(con: duckdb.DuckDBPyConnection, table: str) -> int:
    """Recompute `role_family` and `seniority` from the title, for every row.

    **This is where the reprocessability principle actually pays off** (spec 3.5). The
    event log stores the classification made at collection time, so a taxonomy fix would
    otherwise only affect postings collected afterwards and the series would carry a
    silent discontinuity at the moment the rules changed - exactly the artifact the
    principle exists to prevent.

    The title is retained in the log precisely so this is possible. The value recorded at
    collection is kept alongside as `role_family_logged` / `seniority_logged`, so a
    reclassification can always be audited against what was originally decided.

    Classification runs over DISTINCT titles rather than rows: 26k postings share far
    fewer titles, and the regex work is the expensive part.
    """
    columns = {c[1] for c in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
    if "title" not in columns:
        return 0

    titles = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT title FROM {table} WHERE title IS NOT NULL"
        ).fetchall()
    ]
    if not titles:
        return 0

    classifier = load_classifier()
    mapping = [(t, classifier.role_family(t).value, classifier.seniority(t).value) for t in titles]
    con.execute("CREATE OR REPLACE TEMP TABLE _cls (title VARCHAR, fam VARCHAR, sen VARCHAR)")
    con.executemany("INSERT INTO _cls VALUES (?, ?, ?)", mapping)

    for field in ("role_family", "seniority"):
        if field in columns:
            con.execute(f'ALTER TABLE {table} RENAME "{field}" TO "{field}_logged"')
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT t.*, c.fam AS role_family, c.sen AS seniority
        FROM {table} t LEFT JOIN _cls c USING (title)
        """
    )
    changed = con.execute(
        f"SELECT count(*) FROM {table} WHERE role_family IS DISTINCT FROM role_family_logged"
    ).fetchone()[0]
    return changed


def reparse_locations(con: duckdb.DuckDBPyConnection, table: str) -> int:
    """Recompute `country` and `region` from the stored `location_raw`.

    Same reasoning as `reclassify`, and the same principle (spec 3.5): the inputs are in
    the log, so the derived values should be recomputed rather than frozen at whatever the
    collector knew at the time.

    It matters concretely here. Location parsing did not exist when the first day was
    collected, so those rows carry `country = NULL` — a gap that looks exactly like "this
    posting has no determinable country" but is really "the parser had not been written
    yet". Left alone it would be a permanent discontinuity in the series at the date the
    feature shipped.

    `is_remote` is deliberately NOT recomputed: its strongest inputs (a vendor's workplace
    field, the description text) are not retained for most postings, so recomputing from
    the location alone would *downgrade* rows that were decided on better evidence.
    """
    columns = {c[1] for c in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
    if "location_raw" not in columns:
        return 0

    values = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT location_raw FROM {table} WHERE location_raw IS NOT NULL"
        ).fetchall()
    ]
    if not values:
        return 0

    mapping = []
    for raw in values:
        place = parse_location(raw)
        mapping.append((raw, place.country, place.region))
    con.execute("CREATE OR REPLACE TEMP TABLE _loc (location_raw VARCHAR, c VARCHAR, r VARCHAR)")
    con.executemany("INSERT INTO _loc VALUES (?, ?, ?)", mapping)

    for field in ("country", "region"):
        if field in columns:
            con.execute(f'ALTER TABLE {table} RENAME "{field}" TO "{field}_logged"')
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT t.*, l.c AS country, l.r AS region
        FROM {table} t LEFT JOIN _loc l USING (location_raw)
        """
    )
    return con.execute(
        f"SELECT count(*) FROM {table} WHERE country IS DISTINCT FROM country_logged"
    ).fetchone()[0]


def build(db_path: Path = DB_PATH, storage: Storage | None = None) -> dict[str, int]:
    store = storage or Storage()
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))

    counts = {
        "events": _load(con, "events", str(store.events / "*.jsonl.gz")),
        "postings": _load(con, "postings", str(store.postings / "*.jsonl.gz")),
        "runs": _load(con, "runs", str(store.runs / "*.jsonl")),
    }
    for table in ("events", "postings", "runs"):
        if cast := _cast_json_columns(con, table):
            logger.debug("%s: cast all-null columns to VARCHAR: %s", table, ", ".join(cast))
    # Reclassify BEFORE the views are defined, so everything downstream sees current rules.
    counts["reclassified_events"] = reclassify(con, "events")
    counts["reclassified_postings"] = reclassify(con, "postings")
    counts["relocated_events"] = reparse_locations(con, "events")
    counts["relocated_postings"] = reparse_locations(con, "postings")

    con.execute(OPEN_POSTINGS_SQL)
    con.execute(JOB_SPANS_SQL)
    counts["open_postings"] = con.execute("SELECT count(*) FROM open_postings").fetchone()[0]
    con.close()
    return counts


def connect(db_path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open the panel read-only. Builds it first if it is not there yet."""
    if not db_path.exists():
        build(db_path)
    return duckdb.connect(str(db_path), read_only=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild panel.duckdb from the event log.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    counts = build(args.db)
    logger.info(
        "built %s: %s events, %s postings, %s runs, %s currently open",
        args.db.name,
        counts["events"],
        counts["postings"],
        counts["runs"],
        counts["open_postings"],
    )
    if counts.get("relocated_events"):
        logger.info(
            "recomputed country/region on %s event rows from the stored location text "
            "(previous values kept as country_logged / region_logged)",
            counts["relocated_events"],
        )
    if counts.get("reclassified_events"):
        logger.info(
            "reclassified %s event rows and %s posting rows under the current taxonomy "
            "(values recorded at collection kept as role_family_logged / seniority_logged)",
            counts["reclassified_events"],
            counts["reclassified_postings"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
