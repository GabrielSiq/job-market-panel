"""The first-week daily sanity check. Spec Section 11.3.

Silent failure is the main threat to this project: a collector that appears to run but
writes nothing, or writes garbage, and is only noticed in December when the baseline is
already lost. This prints what a person needs to see in ten seconds.

    uv run python tools/healthcheck.py
    uv run python tools/healthcheck.py --days 14

Exits non-zero when something looks wrong, so CI can fail loudly rather than quietly.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage import Storage

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

#: Above this many companies awaiting a verdict, something is wrong with a vendor rather
#: than with those companies. Discovery re-probes a batch per run, so a healthy backlog
#: drains within a day or two.
DEFERRED_LIMIT = 50


def _c(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text


def _deferred_companies() -> int:
    """Watchlist entries we could not get an answer about last time we asked."""
    path = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"
    if not path.exists():
        return 0
    import yaml

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return sum(1 for c in doc.get("companies") or [] if c.get("status") == "deferred")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--data", type=Path, default=None)
    args = parser.parse_args()

    store = Storage(args.data) if args.data else Storage()
    today = datetime.now(UTC).date()  # must match the collector, which stamps UTC
    window = [today - timedelta(days=i) for i in range(args.days - 1, -1, -1)]
    problems: list[str] = []

    print(
        f"\n{'date':<12}{'source':<13}{'appeared':>9}{'gone':>7}{'runs ok':>9}{'not ok':>8}  notes"
    )
    print("-" * 88)

    any_data = False
    for day in window:
        events_path = store.events / f"{day.isoformat()}.jsonl.gz"
        runs_path = store.runs / f"{day.isoformat()}.jsonl"
        if not events_path.exists() and not runs_path.exists():
            print(_c(f"{day.isoformat():<12}{'-- no collection run --':<40}", DIM))
            continue
        any_data = True

        appeared: Counter[str] = Counter()
        gone: Counter[str] = Counter()
        if events_path.exists():
            for row in store.read_jsonl_gz(events_path):
                (appeared if row.get("event") == "appeared" else gone)[row.get("source", "?")] += 1

        ok: Counter[str] = Counter()
        bad: defaultdict[str, Counter[str]] = defaultdict(Counter)
        if runs_path.exists():
            import json

            for line in runs_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                source, status = row.get("source", "?"), row.get("status", "?")
                if status == "ok":
                    ok[source] += 1
                else:
                    bad[source][status] += 1

        for source in sorted(set(appeared) | set(gone) | set(ok) | set(bad)):
            notes = (
                ", ".join(f"{s}={n}" for s, n in sorted(bad[source].items())) if bad[source] else ""
            )
            bad_total = sum(bad[source].values())
            line = (
                f"{day.isoformat():<12}{source:<13}{appeared[source]:>9}{gone[source]:>7}"
                f"{ok[source]:>9}{bad_total:>8}  {notes}"
            )
            print(_c(line, YELLOW) if bad_total else line)

    if not any_data:
        problems.append("no collection runs found at all")

    # --- the checks that actually catch silent corruption -------------------------
    print()
    files = store.event_files()
    if files:
        latest = files[-1].stem.removesuffix(".jsonl")
        age = (today - date.fromisoformat(latest)).days
        status = "ok" if age <= 1 else f"STALE by {age} days"
        print(f"  most recent event file : {latest}  ({status})")
        if age > 1:
            problems.append(f"no events written for {age} days - is the cron still firing?")

        # ID stability: after a company's first day, its postings should appear once and
        # then stay quiet. If they 'appear' again every run, job_id is not stable and the
        # entire survival dataset is fiction - while looking perfectly healthy.
        #
        # Which companies count as "established" comes from the RUNS log, not the events
        # log. Events only record changes, so a company with nothing new today is absent
        # from today's file entirely - using events to decide who was tracked therefore
        # self-selects for companies still producing appearances, which is precisely the
        # population that cannot show decay. The runs log records every company actually
        # collected, whether or not anything changed.
        #
        # Raw totals do not work either: adding 300 boards bulk-loads tens of thousands of
        # genuinely-new postings, indistinguishable from broken IDs by volume alone. A
        # healthcheck that fires on routine expansion gets ignored, and an ignored
        # healthcheck is worse than none.
        run_files = sorted(store.runs.glob("*.jsonl"))
        if len(files) >= 2 and len(run_files) >= 2:
            import json

            def collected(path: Path) -> set[str]:
                slugs = set()
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip() and (slug := json.loads(line).get("company_slug")):
                        slugs.add(slug)
                return slugs

            def appearances(path: Path, among: set[str]) -> int:
                return sum(
                    1
                    for r in store.read_jsonl_gz(path)
                    if r.get("event") == "appeared" and r.get("company_slug") in among
                )

            established = collected(run_files[-2]) & collected(run_files[-1])
            totals = [
                sum(1 for r in store.read_jsonl_gz(f) if r.get("event") == "appeared")
                for f in files[-2:]
            ]
            new_boards = len(collected(run_files[-1]) - collected(run_files[-2]))
            print(
                f"  appearances, last 2 days: {totals[0]:,} then {totals[1]:,}"
                + (f" ({new_boards} boards added today)" if new_boards else "")
            )

            if established:
                was = appearances(files[-2], established)
                now = appearances(files[-1], established)
                print(
                    f"  at the {len(established)} companies tracked on both days: "
                    f"{was:,} then {now:,} (this is the number that must decay)"
                )
                if was >= 50 and now > was * 0.8:
                    problems.append(
                        f"appearances are not decaying at established companies "
                        f"({was} then {now}) - job_id may not be stable across runs"
                    )

    pending = store.read_pending_misses()
    print(f"  postings pending a 2nd miss: {len(pending)}")

    # Companies whose boards we could not get an answer about. Normally zero. A set that
    # keeps growing means a vendor is throttling or down, which is exactly the condition
    # that used to be written into the watchlist as "this company has no board".
    if deferred := _deferred_companies():
        print(f"  companies awaiting a verdict: {deferred}")
        if deferred > DEFERRED_LIMIT:
            problems.append(
                f"{deferred} companies have gone unresolved-for-lack-of-answer - a vendor "
                "is likely throttling or down; discovery re-probes only a batch per run"
            )

    # The API changelog string is the cheapest available warning that a vendor changed
    # shape without telling anyone.
    notices = set()
    for day in window[-3:]:
        path = store.runs / f"{day.isoformat()}.jsonl"
        if path.exists():
            import json

            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and (notice := json.loads(line).get("api_notice")):
                    notices.add(notice[:120])
    if len(notices) > 1:
        problems.append("the source API changelog changed - check for a shape change")
    for notice in notices:
        print(
            f"  api notice             : {DIM}{notice}{RESET}"
            if sys.stdout.isatty()
            else f"  api notice             : {notice}"
        )

    total = sum(f.stat().st_size for f in store.root.rglob("*") if f.is_file())
    print(f"  data footprint         : {total / 1024 / 1024:.1f} MB")

    print()
    if problems:
        for problem in problems:
            print(_c(f"  PROBLEM: {problem}", RED))
        return 1
    print(_c("  all checks passed", GREEN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
