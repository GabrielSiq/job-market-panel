"""Harvest ATS board tokens from public sources that publish them directly.

**Why this exists.** No ATS offers enumeration, so the panel can only see companies it can
name, and name-guessing has a real miss rate - DoorDash is on Greenhouse as `doordashusa`,
Front is on Ashby as `frontcareers`. Guessing will never produce `duck-duck-go` or
`category-labs` either.

The shortcut: find places where companies publish their own board links, and read the
tokens straight out. Hacker News' monthly "Ask HN: Who is hiring?" threads are exactly
that - companies posting their own ATS URLs - and the Algolia HN API is free, keyless and
documented. Six months of threads yielded 172 distinct board tokens.

**This inverts the usual direction.** Everywhere else we start from a company name and
guess a token. Here we start from a token the company itself published and ask the board
what company it belongs to, which is both easier and better evidence.

Known bias, and it is real: the HN audience skews to startups and developer-tools
companies. Useful as a jumpstart and a monthly top-up, never as a market denominator.

    uv run python tools/harvest_tokens.py --dry-run
    uv run python tools/harvest_tokens.py --months 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import date
from html import unescape
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.normalize import clean_text, company_slug
from tools.discover import load_watchlist
from tools.resolve_ats import (
    READABLE,
    USER_AGENT,
    VENDOR_BY_NAME,
    _board_display_name,
    _probe,
)

WATCHLIST = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"

# HN renders URLs with HTML entities (`https:&#x2F;&#x2F;`), so comment text must be
# unescaped before matching or nothing is found at all.
ATS_URL = re.compile(
    r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"
    r"|jobs\.lever\.co/([a-z0-9_-]+)"
    r"|jobs\.ashbyhq\.com/([a-z0-9_-]+)",
    re.I,
)
_NOT_TOKENS = {"embed", "job_board", "jobs", "careers"}


@dataclass
class Harvested:
    vendor: str
    token: str
    company: str | None = None
    job_count: int = 0


async def hn_threads(client: httpx.AsyncClient, months: int) -> list[str]:
    query = urllib.parse.urlencode(
        {"query": '"Ask HN: Who is hiring"', "tags": "story", "hitsPerPage": months * 2}
    )
    response = await client.get(f"{HN_SEARCH}?{query}")
    response.raise_for_status()
    return [
        hit["objectID"]
        for hit in response.json().get("hits", [])
        if "who is hiring?" in (hit.get("title") or "").lower()
    ][:months]


async def tokens_from_thread(client: httpx.AsyncClient, thread_id: str) -> set[tuple[str, str]]:
    try:
        response = await client.get(HN_ITEM.format(id=thread_id))
        response.raise_for_status()
    except httpx.HTTPError:
        return set()
    found: set[tuple[str, str]] = set()
    stack = [response.json()]
    while stack:
        node = stack.pop()
        for match in ATS_URL.finditer(unescape(node.get("text") or "")):
            vendor = "greenhouse" if match.group(1) else "lever" if match.group(2) else "ashby"
            token = (match.group(1) or match.group(2) or match.group(3)).lower()
            if token not in _NOT_TOKENS:
                found.add((vendor, token))
        stack.extend(node.get("children") or [])
    return found


async def identify(
    client: httpx.AsyncClient, vendor: str, token: str, sem: asyncio.Semaphore
) -> Harvested | None:
    """Ask the board which company it belongs to.

    The company name is not known up front here - the token came from a forum post. The
    board itself supplies it, which is why these entries are verified by construction:
    the token was published by the company and the board confirms whose it is.
    """
    if vendor not in READABLE:
        return None
    async with sem:
        probed = await _probe(client, VENDOR_BY_NAME[vendor], token)
    if not probed:
        return None
    count, echoed = probed
    name = echoed
    if not name:
        async with sem:
            name = await _board_display_name(client, VENDOR_BY_NAME[vendor], token)
    name = clean_text(re.sub(r"\s*(jobs|careers)\s*$", "", name or "", flags=re.I))
    if not name:
        return None
    return Harvested(vendor=vendor, token=token, company=name, job_count=count)


async def harvest(months: int, concurrency: int = 8) -> list[Harvested]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        threads = await hn_threads(client, months)
        print(f"mining {len(threads)} monthly 'Who is hiring' threads")
        pairs: set[tuple[str, str]] = set()
        for result in await asyncio.gather(*(tokens_from_thread(client, t) for t in threads)):
            pairs |= result
        print(f"found {len(pairs)} distinct board tokens; checking which are live")
        results = await asyncio.gather(*(identify(client, v, t, sem) for v, t in sorted(pairs)))
    return [r for r in results if r]


def append(harvested: list[Harvested], path: Path = WATCHLIST) -> int:
    known_slugs = {c["slug"] for c in load_watchlist(path)}
    known_boards = {(c["ats"], c["token"]) for c in load_watchlist(path) if c.get("token")}
    today = date.today().isoformat()
    blocks: list[str] = []
    added = 0
    for h in sorted(harvested, key=lambda h: -h.job_count):
        slug = company_slug(h.company or "")
        if not slug or slug in known_slugs or (h.vendor, h.token) in known_boards:
            continue
        known_slugs.add(slug)
        known_boards.add((h.vendor, h.token))
        added += 1
        blocks += [
            f"  - name: {json.dumps(h.company)}",
            f"    slug: {slug}",
            f"    ats: {h.vendor}",
            f"    token: {h.token}",
            "    status: verified",
            "    verified_by: published_token",
            f"    added: {today}",
            "    source: hn_whoishiring",
            f"    notes: {json.dumps(f'{h.job_count} open reqs; token published by the company on HN')}",
        ]
    if blocks:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(blocks) + "\n")
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    harvested = asyncio.run(harvest(args.months))
    known = {c["slug"] for c in load_watchlist()}
    fresh = [h for h in harvested if company_slug(h.company or "") not in known]
    print(f"\nlive boards: {len(harvested)} | not already on the watchlist: {len(fresh)}")
    for h in sorted(fresh, key=lambda h: -h.job_count)[:25]:
        print(f"  + {h.company[:30]:<32}{h.vendor:<12}{h.token[:22]:<24}{h.job_count:>5} reqs")

    if args.dry_run:
        print(f"\ndry run: {len(fresh)} would be added")
    else:
        print(f"\nadded {append(harvested)} companies to the watchlist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
