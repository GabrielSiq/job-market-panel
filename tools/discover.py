"""Grow the watchlist automatically. This is the actual unlock of Phase 2.

**Why this exists.** No ATS offers enumeration - Greenhouse `/v1/boards` 404s, Ashby 401s,
Lever 404s - so a board is readable only if its token is already known. Every company we
cannot name is invisible, permanently, for that day. The watchlist is therefore a technical
requirement, not a target list, and hand-curating it was always going to be the bottleneck.

This inverts that: candidates are gathered daily, resolved, verified, and appended. Gabriel
prunes rather than researches.

Two candidate feeds, with opposite biases:

- **The event log's aggregator rows.** Himalayas surfaced 360 distinct companies on day
  one. But it is a remote-only board by construction, so every company it finds posts
  remote roles.
- **The Muse.** Keyless, ~412k jobs, a "Data and Analytics" category and a US filter, and
  crucially *not* remote-restricted - which is what corrects the bias above. Companies pay
  to be listed, so it skews toward large employers with branding budgets. Used only to
  generate names; never as a denominator, and its postings never enter the event log.

Candidates are filtered to companies posting a **tracked (data-family) role**, so the
watchlist grows with relevance rather than volume. Only `verified` boards are collected;
see `tools/resolve_ats.py` for what verification means and why it is strict.

    uv run python tools/discover.py --dry-run
    uv run python tools/discover.py --limit 80
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classify import load_classifier
from src.models import TRACKED_FAMILIES
from src.normalize import clean_text, company_slug
from src.storage import Storage
from tools.resolve_ats import USER_AGENT, Resolution, resolve_all

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "config" / "watchlist.yaml"

MUSE_URL = "https://www.themuse.com/api/public/jobs"
MUSE_CATEGORIES = ("Data and Analytics", "Science and Engineering")
MUSE_LOCATIONS = ("United States", "Flexible / Remote")


def existing_slugs(path: Path = WATCHLIST) -> set[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {c["slug"] for c in doc.get("companies") or []}


def candidates_from_event_log(days: int = 7) -> dict[str, str]:
    """Companies seen posting a tracked role in recent aggregator rows.

    Reads the committed event log rather than re-querying, so this costs nothing and
    reflects exactly what the panel already observed.
    """
    store = Storage()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    found: dict[str, str] = {}
    for path in store.event_files():
        if path.stem.removesuffix(".jsonl") < cutoff:
            continue
        for row in store.read_jsonl_gz(path):
            if row.get("source") != "himalayas" or row.get("event") != "appeared":
                continue
            if row.get("role_family") not in {f.value for f in TRACKED_FAMILIES}:
                continue
            if name := clean_text(row.get("company_name")):
                found.setdefault(company_slug(name), name)
    return found


async def candidates_from_muse(pages: int = 6) -> dict[str, str]:
    """Company names from The Muse, filtered locally to data-family titles.

    The Muse's own category is loose - a sample of "Data and Analytics" returned account
    directors and solutions architects - so titles are classified with the project's own
    taxonomy rather than trusted.
    """
    classifier = load_classifier()
    tracked = set(TRACKED_FAMILIES)
    found: dict[str, str] = {}
    async with httpx.AsyncClient(
        timeout=25, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        for category in MUSE_CATEGORIES:
            for location in MUSE_LOCATIONS:
                for page in range(pages):
                    query = urllib.parse.urlencode(
                        {"category": category, "location": location, "page": page}
                    )
                    try:
                        response = await client.get(f"{MUSE_URL}?{query}")
                        response.raise_for_status()
                        results = response.json().get("results") or []
                    except (httpx.HTTPError, ValueError):
                        break
                    if not results:
                        break
                    for job in results:
                        title = clean_text(job.get("name"))
                        name = clean_text((job.get("company") or {}).get("name"))
                        if not title or not name:
                            continue
                        if classifier.role_family(title) in tracked:
                            found.setdefault(company_slug(name), name)
                    await asyncio.sleep(0.3)
    return found


def append_to_watchlist(results: list[Resolution], path: Path = WATCHLIST) -> dict[str, int]:
    """Append newly-resolved companies, preserving everything already there.

    Unresolved and unverified entries are recorded too, deliberately: without them the
    same dead names would be re-probed every single day forever.
    """
    today = date.today().isoformat()
    blocks: list[str] = []
    counts = {"verified": 0, "unverified": 0, "unresolved": 0}
    for r in sorted(results, key=lambda r: r.name.lower()):
        counts[r.status] = counts.get(r.status, 0) + 1
        note = r.note or (f"{r.job_count} open reqs at discovery" if r.token else "")
        blocks += [
            f"  - name: {json.dumps(r.name)}",
            f"    slug: {company_slug(r.name)}",
            f"    ats: {r.ats}",
            f"    token: {r.token if r.token else 'null'}",
            f"    status: {r.status}",
            f"    verified_by: {r.verified_by or 'null'}",
            f"    added: {today}",
            "    source: auto_discovered",
            f"    notes: {json.dumps(note)}",
        ]
    if blocks:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(blocks) + "\n")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=80, help="max new companies to resolve")
    parser.add_argument("--days", type=int, default=7, help="event-log lookback")
    parser.add_argument("--muse-pages", type=int, default=6)
    parser.add_argument("--no-muse", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    known = existing_slugs()
    candidates = candidates_from_event_log(days=args.days)
    from_log = len(candidates)
    if not args.no_muse:
        candidates.update(asyncio.run(candidates_from_muse(pages=args.muse_pages)))

    new = {slug: name for slug, name in candidates.items() if slug not in known}
    print(f"watchlist: {len(known)} companies")
    print(f"candidates: {from_log} from the event log, {len(candidates)} with The Muse")
    print(f"not yet on the watchlist: {len(new)}")

    selected = sorted(new.values())[: args.limit]
    if not selected:
        print("nothing new to resolve")
        return 0
    print(f"resolving {len(selected)} (limit {args.limit})...")

    results = asyncio.run(resolve_all(selected))
    verified = [r for r in results if r.status == "verified"]
    for r in sorted(verified, key=lambda r: -r.job_count):
        print(f"  + {r.name[:30]:<32}{r.ats:<12}{r.token:<22}{r.job_count:>5} reqs")

    if args.dry_run:
        print("\ndry run: watchlist unchanged")
        counts = {
            s: sum(1 for r in results if r.status == s)
            for s in ("verified", "unverified", "unresolved")
        }
    else:
        counts = append_to_watchlist(results)
    print(
        f"\nverified {counts.get('verified', 0)} | unverified {counts.get('unverified', 0)} "
        f"| unresolved {counts.get('unresolved', 0)}"
    )
    print(
        "only `verified` boards are collected; the rest are recorded so they are not "
        "re-probed daily"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
