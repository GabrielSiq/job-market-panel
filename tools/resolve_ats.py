"""Resolve a company name or careers URL to an ATS vendor and board token.

Phase 1 probes Greenhouse only. Phase 2 extends this to Lever, Ashby and Workable, at
which point the ambiguity risk becomes real: a slug that exists on two vendors' boards
will match whichever is probed first, so every attempt is recorded rather than just the
winner, and ambiguous hits are flagged for manual review.

Usage:
    uv run python tools/resolve_ats.py --names "Stripe" "Figma"
    uv run python tools/resolve_ats.py --file companies.txt --out config/watchlist.yaml
    uv run python tools/resolve_ats.py --names "Acme" --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.normalize import company_slug, slugify

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
USER_AGENT = "job-market-panel/0.1 (+https://github.com/GabrielSiq/job-market-panel)"

# Board tokens embedded in careers URLs, which is the reliable path when it is available.
_URL_TOKEN_PATTERNS = (
    re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I),
    re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I),
    re.compile(r"greenhouse\.io/embed/job_board\?for=([a-z0-9_-]+)", re.I),
)


@dataclass
class Resolution:
    name: str
    ats: str = "unknown"
    token: str | None = None
    job_count: int = 0
    board_company: str | None = None
    tried: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    note: str = ""


def token_candidates(name: str) -> list[str]:
    """Plausible Greenhouse tokens for a company name, most likely first.

    Greenhouse tokens are usually the company name lowercased with separators removed
    ("Acme Corp" -> "acmecorp"), sometimes hyphenated, sometimes just the first word.
    """
    for pattern in _URL_TOKEN_PATTERNS:
        if match := pattern.search(name):
            return [match.group(1).lower()]

    slug = company_slug(name)  # legal suffixes already stripped
    raw = slugify(name)  # suffixes retained
    candidates = [
        slug.replace("-", ""),
        slug,
        raw.replace("-", ""),
        raw,
        slug.split("-")[0],
    ]
    seen: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


async def _probe(client: httpx.AsyncClient, token: str) -> tuple[int, str | None] | None:
    """Return (job_count, board_company_name), or None if there is no such board.

    A bad or empty token returns a clean 404, which makes this unambiguous. An empty
    board (0 jobs) is treated as *not a match*, since it is indistinguishable from a
    coincidental slug collision and would otherwise park a wrong token in the config.

    Greenhouse echoes `company_name` on each posting. That is the only available check
    that a guessed token belongs to the company we meant: "gemini" or "remote" are live
    boards regardless of whose they are, and a wrong one would quietly attribute another
    company's hiring to this one for the life of the panel.
    """
    try:
        response = await client.get(GREENHOUSE_URL.format(token=token))
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        jobs = response.json().get("jobs")
    except ValueError:
        return None
    if not isinstance(jobs, list) or not jobs:
        return None
    board_company = None
    for job in jobs:
        if isinstance(job, dict) and job.get("company_name"):
            board_company = str(job["company_name"])
            break
    return len(jobs), board_company


async def resolve_one(client: httpx.AsyncClient, name: str, sem: asyncio.Semaphore) -> Resolution:
    result = Resolution(name=name)
    for candidate in token_candidates(name):
        result.tried.append(candidate)
        async with sem:
            probed = await _probe(client, candidate)
        if probed:
            count, board_company = probed
            if result.token is None:
                result.ats, result.token, result.job_count = "greenhouse", candidate, count
                result.board_company = board_company
            else:
                # Two live boards for one name: record it rather than silently picking.
                result.ambiguous.append(candidate)
    if result.token is None:
        result.note = "no Greenhouse board found; park for Phase 2 vendor probing"
    elif result.board_company and company_slug(result.board_company) != company_slug(name):
        result.note = f"VERIFY: board self-reports as {result.board_company!r}, expected {name!r}"
    elif result.ambiguous:
        result.note = f"AMBIGUOUS: also live: {', '.join(result.ambiguous)} - verify by hand"
    return result


async def resolve_all(names: list[str], concurrency: int = 8) -> list[Resolution]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=20, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        return await asyncio.gather(*(resolve_one(client, n, sem) for n in names))


def to_watchlist_yaml(results: list[Resolution], source: str = "manual") -> str:
    """Emit config/watchlist.yaml. Spec Section 6.5."""
    today = date.today().isoformat()

    # One board must appear exactly once. Two spellings of a company resolving to the same
    # token would otherwise double-count its open reqs and corrupt its growth signal --
    # which is the headline metric this whole panel exists to produce.
    deduped: list[Resolution] = []
    claimed: dict[tuple[str, str], Resolution] = {}
    for r in results:
        if r.token is None:
            deduped.append(r)
            continue
        key = (r.ats, r.token)
        if (winner := claimed.get(key)) is None:
            claimed[key] = r
            deduped.append(r)
        elif len(r.name) < len(winner.name):
            winner.name = r.name  # keep the shorter, more canonical spelling
    results = deduped
    lines = [
        "# The census universe: every company whose board is fetched daily.",
        "#",
        "# Companies that resolved to a Greenhouse board are collected from day one.",
        "# Entries with `ats: unknown` are parked deliberately - they are picked up in",
        "# Phase 2 when Lever, Ashby and Workable adapters land. Do not delete them.",
        "#",
        "# Edit freely: this is config, not code. Adding a company takes effect on the",
        "# next run and requires no rebuild.",
        "",
        "companies:",
    ]
    for r in sorted(results, key=lambda r: (r.ats == "unknown", r.name.lower())):
        lines.append(f"  - name: {json.dumps(r.name)}")
        lines.append(f"    slug: {company_slug(r.name)}")
        lines.append(f"    ats: {r.ats}")
        lines.append(f"    token: {r.token if r.token else 'null'}")
        lines.append(f"    added: {today}")
        lines.append(f"    source: {source}")
        note = r.note or (f"{r.job_count} open reqs at resolution" if r.token else "")
        lines.append(f"    notes: {json.dumps(note)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--names", nargs="*", default=[])
    parser.add_argument("--file", type=Path, help="one company name or careers URL per line")
    parser.add_argument("--out", type=Path, help="write watchlist YAML here")
    parser.add_argument("--source", default="manual", help="provenance for watchlist entries")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    names = list(args.names)
    if args.file:
        names += [
            line.strip()
            for line in args.file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not names:
        parser.error("provide --names and/or --file")

    seen: dict[str, str] = {}
    for name in names:
        seen.setdefault(company_slug(name), name)
    names = list(seen.values())

    results = asyncio.run(resolve_all(names, concurrency=args.concurrency))

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        resolved = [r for r in results if r.token]
        print(f"{'company':<32}{'ats':<12}{'token':<26}{'reqs':>6}")
        print("-" * 78)
        for r in sorted(results, key=lambda r: (r.ats == "unknown", r.name.lower())):
            print(f"{r.name[:30]:<32}{r.ats:<12}{r.token or '-'!s:<26}{r.job_count or '-':>6}")
            if r.ambiguous:
                print(f"    ^ {r.note}")
        print(f"\nresolved {len(resolved)}/{len(results)} to a Greenhouse board")

    if args.out:
        args.out.write_text(to_watchlist_yaml(results, source=args.source), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
