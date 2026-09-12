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


def _c(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text


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

        # ID stability: after day one, appearances should be a trickle, not the whole
        # corpus. If every posting 'appears' again each day, job_id is not stable and the
        # entire survival dataset is fiction - while looking perfectly healthy.
        if len(files) >= 2:
            counts = [
                sum(1 for r in store.read_jsonl_gz(f) if r.get("event") == "appeared")
                for f in files[-2:]
            ]
            if counts[0] and counts[1] > counts[0] * 0.8:
                problems.append(
                    f"appearances are not decaying ({counts[0]} then {counts[1]}) - "
                    "job_id may not be stable across runs"
                )
            print(f"  appearances, last 2 days: {counts[0]} then {counts[1]}")

    pending = store.read_pending_misses()
    print(f"  postings pending a 2nd miss: {len(pending)}")

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
