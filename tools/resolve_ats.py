"""Resolve a company name or careers URL to an ATS vendor and board token, and verify it.

Probes Greenhouse, Ashby and Lever. Workable and Workday are deliberately excluded - see
the Phase 2 reconnaissance notes in CLAUDE.md.

**Verification is the point of this tool, not resolution.** A guessed token that happens to
exist is a real board belonging to *somebody*; collecting it would attribute another
company's hiring to this one for the life of the panel. That already nearly happened three
times in Phase 1: `carbon` is Carbon Inc. rather than Carbon Health, `wise` an unrelated
field-sales board, `remote` General Assembly's.

Every vendor can be checked, though each in a different place:

- **Greenhouse** echoes `company_name` on every posting.
- **Ashby** and **Lever** echo nothing in their APIs, but their public board pages
  (`jobs.ashbyhq.com/{token}`, `jobs.lever.co/{token}`) carry the company's display name in
  the `<title>` and `og:title`.

A board is marked `verified` only on an **exact** normalized-name match. Anything else is
`unverified`: the board is recorded with the name it actually reports, and the collector
skips it until a human confirms. This is deliberately strict - "Chime" vs "Chime Financial,
Inc" is a legitimate legal name, and "Wise" vs "Wise Worksite Field Sales" is the wrong
company, and the two are structurally identical to a fuzzy matcher.

Usage:
    uv run python tools/resolve_ats.py --names "Stripe" "Plaid"
    uv run python tools/resolve_ats.py --file companies.txt --out config/watchlist.yaml
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
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.normalize import clean_text, company_slug, slugify

USER_AGENT = "job-market-panel/0.1 (+https://github.com/GabrielSiq/job-market-panel)"
BROWSER_UA = "Mozilla/5.0 (compatible; job-market-panel/0.1; +https://github.com/GabrielSiq)"


@dataclass(frozen=True)
class Vendor:
    name: str
    api: str  # job listing endpoint, {token} substituted
    board_page: str | None  # public board page carrying the company's display name

    def jobs(self, payload: Any) -> list[Any] | None:
        if isinstance(payload, list):  # Lever returns a bare array
            return payload
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        return jobs if isinstance(jobs, list) else None

    def echoed_name(self, payload: Any) -> str | None:
        """A company name the API itself reports, where one exists (Greenhouse only)."""
        jobs = self.jobs(payload) or []
        for job in jobs:
            if isinstance(job, dict) and job.get("company_name"):
                return str(job["company_name"])
        return None


VENDOR_BY_NAME: dict[str, Vendor] = {}

VENDORS = (
    Vendor(
        "greenhouse", "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false", None
    ),
    Vendor(
        "ashby",
        "https://api.ashbyhq.com/posting-api/job-board/{token}",
        "https://jobs.ashbyhq.com/{token}",
    ),
    Vendor(
        "lever",
        "https://api.lever.co/v0/postings/{token}?mode=json",
        "https://jobs.lever.co/{token}",
    ),
)
VENDOR_BY_NAME.update({v.name: v for v in VENDORS})

# Board tokens embedded in careers URLs - the reliable path when one is supplied, and the
# only way to find tokens no name-derived guess produces (Front is on Ashby as
# "frontcareers").
_URL_TOKEN_PATTERNS = (
    (
        "greenhouse",
        re.compile(
            r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I
        ),
    ),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
)

# --- careers-page fingerprinting ------------------------------------------------------
# Stage two, for names that guessing cannot resolve. A company's own careers page links to
# its board, which both FINDS tokens no name-derived guess produces (Front is on Ashby as
# "frontcareers") and is the strongest possible verification: the company itself is
# pointing at it.
#
# Vendors we cannot read are fingerprinted too, deliberately. Knowing that a company is on
# Workday is far more useful than "unresolved" - it turns an unexplained gap into a
# measured one, and tells us what a future adapter would buy.
READABLE = {"greenhouse", "ashby", "lever"}

CAREERS_FINGERPRINTS = (
    (
        "greenhouse",
        re.compile(
            r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I
        ),
    ),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", re.I)),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/([a-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/([a-z0-9_-]+)", re.I)),
    ("teamtailor", re.compile(r"([a-z0-9_-]+)\.teamtailor\.com", re.I)),
    ("bamboohr", re.compile(r"([a-z0-9_-]+)\.bamboohr\.com", re.I)),
    ("icims", re.compile(r"([a-z0-9_-]+)\.icims\.com", re.I)),
    ("successfactors", re.compile(r"([a-z0-9_-]+)\.successfactors\.com", re.I)),
    ("taleo", re.compile(r"([a-z0-9_-]+)\.taleo\.net", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([a-z0-9_-]+)", re.I)),
    ("pinpoint", re.compile(r"([a-z0-9_-]+)\.pinpointhq\.com", re.I)),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/[^\"\']*?/([a-z0-9_-]+)", re.I)),
)


def domain_candidates(name: str) -> list[str]:
    """Plausible careers URLs for a company name, cheapest first.

    Domain guessing is genuinely unreliable - "dbt Labs" lives at getdbt.com - so this is a
    best-effort second stage, not a guarantee. It costs a couple of requests and recovers
    boards that would otherwise be invisible forever.
    """
    slug = company_slug(name)
    flat = slug.replace("-", "")
    urls: list[str] = []
    for host in dict.fromkeys([flat, slug]):
        for tld in ("com", "io", "ai", "co"):
            urls.append(f"https://{host}.{tld}/careers")
        urls.append(f"https://careers.{host}.com")
        urls.append(f"https://{host}.com/jobs")
    return urls[:6]


async def fingerprint(client: httpx.AsyncClient, name: str) -> tuple[str, str | None] | None:
    """(vendor, token) read from the company's own careers page, or None."""
    for url in domain_candidates(name):
        try:
            response = await client.get(
                url, headers={"User-Agent": BROWSER_UA}, timeout=12, follow_redirects=True
            )
        except httpx.HTTPError:
            continue
        if response.status_code >= 400:
            continue
        body = response.text[:400_000]
        for vendor, pattern in CAREERS_FINGERPRINTS:
            if match := pattern.search(body):
                token = next((g for g in match.groups() if g), None)
                return vendor, (token.lower() if token else None)
    return None


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_OG_TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
# Board titles read "Plaid Jobs", "Wealthfront jobs", "Acme - Careers"; strip that suffix
# before comparing names. \u2013 is an en dash, spelled out to keep the literal unambiguous.
_TRAILING = re.compile("\\s*[-|\u2013]?\\s*(jobs|careers|job board|open roles|openings)\\s*$", re.I)


@dataclass
class Resolution:
    name: str
    ats: str = "unknown"
    token: str | None = None
    job_count: int = 0
    status: str = "unresolved"  # verified | unverified | unresolved
    verified_by: str | None = None  # name_echo | board_page | careers_url
    board_name: str | None = None
    tried: list[str] = field(default_factory=list)
    note: str = ""


def token_candidates(name: str) -> list[tuple[str | None, str]]:
    """(vendor_hint, token) pairs to try, most likely first.

    A careers URL pins both vendor and token exactly, so it short-circuits guessing.
    """
    for vendor, pattern in _URL_TOKEN_PATTERNS:
        if match := pattern.search(name):
            return [(vendor, match.group(1).lower())]

    slug = company_slug(name)  # legal suffixes stripped
    raw = slugify(name)  # suffixes retained
    flat = slug.replace("-", "")
    guesses = [flat, slug, raw.replace("-", ""), raw, slug.split("-")[0]]
    # Boards are routinely registered under a regional or boilerplate suffix. DoorDash is
    # on Greenhouse as "doordashusa" with 456 open reqs - invisible to every name-derived
    # guess above, and exactly the kind of major employer whose absence would quietly skew
    # the panel toward smaller companies.
    guesses += [
        f"{flat}{suffix}"
        for suffix in ("usa", "us", "inc", "global", "careers", "jobs", "hq", "corp", "team")
    ]
    seen: list[tuple[str | None, str]] = []
    for guess in guesses:
        if guess and (None, guess) not in seen:
            seen.append((None, guess))
    return seen


#: Suffixes a board may append to its own name without being a different company.
#: Deliberately a closed list: "DoorDash" vs "DoorDash USA" is the same employer, while
#: "Wise" vs "Wise Worksite Field Sales" is not, and only an explicit list separates them.
_NAME_SUFFIXES = ("usa", "us", "global", "careers", "jobs", "hq", "team", "teams", "group")


def _comparable(value: str) -> str:
    parts = company_slug(value).split("-")
    while len(parts) > 1 and parts[-1] in _NAME_SUFFIXES:
        parts.pop()
    return "-".join(parts)


def names_match(requested: str, reported: str | None) -> bool:
    """Exact normalized match only.

    Deliberately strict. A fuzzy matcher cannot separate "Chime" / "Chime Financial, Inc"
    (right company, legal name) from "Wise" / "Wise Worksite Field Sales" (wrong company
    entirely) - they are the same shape. Near-misses become `unverified` and carry the
    reported name so a human decides in one glance.
    """
    if not reported:
        return False
    return _comparable(requested) == _comparable(_TRAILING.sub("", clean_text(reported) or ""))


async def _board_display_name(client: httpx.AsyncClient, vendor: Vendor, token: str) -> str | None:
    """The company name a vendor's public board page advertises."""
    if not vendor.board_page:
        return None
    try:
        response = await client.get(
            vendor.board_page.format(token=token), headers={"User-Agent": BROWSER_UA}
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    body = response.text[:200_000]
    for pattern in (_OG_TITLE, _TITLE):
        if match := pattern.search(body):
            return clean_text(match.group(1))
    return None


async def _probe(
    client: httpx.AsyncClient, vendor: Vendor, token: str
) -> tuple[int, str | None] | None:
    """(job_count, api_echoed_name) if this board exists and has jobs, else None.

    An empty board counts as no match: it is indistinguishable from a slug collision, and
    would otherwise park a wrong token in the config.
    """
    try:
        response = await client.get(vendor.api.format(token=token))
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    jobs = vendor.jobs(payload)
    if not jobs:
        return None
    return len(jobs), vendor.echoed_name(payload)


async def resolve_one(client: httpx.AsyncClient, name: str, sem: asyncio.Semaphore) -> Resolution:
    result = Resolution(name=name)
    from_url = bool(_URL_TOKEN_PATTERNS) and any(p.search(name) for _, p in _URL_TOKEN_PATTERNS)

    for hint, token in token_candidates(name):
        for vendor in VENDORS:
            if hint and vendor.name != hint:
                continue
            result.tried.append(f"{vendor.name}:{token}")
            async with sem:
                probed = await _probe(client, vendor, token)
            if not probed:
                continue

            count, echoed = probed
            reported = echoed
            verified_by = "name_echo" if echoed else None
            if reported is None:
                async with sem:
                    reported = await _board_display_name(client, vendor, token)
                verified_by = "board_page" if reported else None

            result.ats, result.token, result.job_count = vendor.name, token, count
            result.board_name = reported

            if from_url:
                # The company's own careers page pointed here; that is stronger evidence
                # than any name comparison.
                result.status, result.verified_by = "verified", "careers_url"
            elif names_match(name, reported):
                result.status, result.verified_by = "verified", verified_by
            else:
                result.status = "unverified"
                result.note = (
                    f"board reports {reported!r}, expected {name!r} - confirm before collecting"
                    if reported
                    else "could not read a company name from the board - confirm before collecting"
                )
            return result

    # Stage two: ask the company's own site which ATS it uses.
    found = await fingerprint(client, name)
    if found is None:
        result.note = "no board found by guessing, and no careers page could be read"
        return result

    vendor, token = found
    if vendor in READABLE and token:
        async with sem:
            probed = await _probe(client, VENDOR_BY_NAME[vendor], token)
        if probed:
            count, _ = probed
            result.ats, result.token, result.job_count = vendor, token, count
            # The company's own careers page points here. That is stronger evidence than
            # any name comparison, so no further verification is needed.
            result.status, result.verified_by = "verified", "careers_page"
            result.note = f"{count} open reqs; found via careers-page fingerprint"
            return result

    # Reachable in principle, but not by an adapter we have. Recording the vendor turns an
    # unexplained gap into a measured one.
    result.ats = vendor
    result.note = (
        f"on {vendor}"
        + (f" (token {token})" if token else "")
        + " - no adapter for this vendor; see CLAUDE.md on vendor coverage"
    )
    return result


async def resolve_all(names: list[str], concurrency: int = 8) -> list[Resolution]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=25, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        return await asyncio.gather(*(resolve_one(client, n, sem) for n in names))


def render_watchlist(results: list[Resolution], source: str = "manual") -> str:
    """Emit config/watchlist.yaml. Spec Section 6.5, plus status/verified_by."""
    today = date.today().isoformat()

    # One board must appear exactly once. Two spellings resolving to the same token would
    # fetch it twice and double that company's open-req counts.
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
            winner.name = r.name

    lines = [
        "# The census universe: every company whose board is fetched daily.",
        "#",
        "# This is NOT a target list. It is the widest set of companies we can resolve and",
        "# verify; narrowing happens later, at scoring time, never at collection time.",
        "#",
        "# `status` governs collection:",
        "#   verified   - the board's own reported company name matched exactly, or the",
        "#                company's careers page links to it. Collected daily.",
        "#   unverified - a live board was found by guessing a token, but its reported name",
        "#                does not match. NOT collected. `notes` carries the name it reports",
        "#                so a human can confirm or correct it in one glance.",
        "#   unresolved - no board found on Greenhouse, Ashby or Lever. Kept deliberately:",
        "#                many are on Workday or a custom site. Do not delete.",
        "#",
        "# Verification is not paranoia. In Phase 1 it caught three wrong boards: 'carbon'",
        "# is Carbon Inc rather than Carbon Health, 'wise' an unrelated field-sales board,",
        "# and 'remote' General Assembly's. Each would have attributed another company's",
        "# hiring to the wrong employer for the life of the panel.",
        "",
        "companies:",
    ]
    order = {"verified": 0, "unverified": 1, "unresolved": 2}
    for r in sorted(deduped, key=lambda r: (order.get(r.status, 3), r.name.lower())):
        note = r.note or (f"{r.job_count} open reqs at resolution" if r.token else "")
        lines += [
            f"  - name: {json.dumps(r.name)}",
            f"    slug: {company_slug(r.name)}",
            f"    ats: {r.ats}",
            f"    token: {r.token if r.token else 'null'}",
            f"    status: {r.status}",
            f"    verified_by: {r.verified_by or 'null'}",
            f"    added: {today}",
            f"    source: {source}",
            f"    notes: {json.dumps(note)}",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--names", nargs="*", default=[])
    parser.add_argument("--file", type=Path, help="one company name or careers URL per line")
    parser.add_argument("--out", type=Path, help="write watchlist YAML here")
    parser.add_argument("--source", default="manual")
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
        print(f"{'company':<28}{'ats':<12}{'token':<24}{'status':<12}{'reqs':>6}")
        print("-" * 84)
        order = {"verified": 0, "unverified": 1, "unresolved": 2}
        for r in sorted(results, key=lambda r: (order.get(r.status, 3), r.name.lower())):
            print(
                f"{r.name[:26]:<28}{r.ats:<12}{r.token or '-'!s:<24}"
                f"{r.status:<12}{r.job_count or '-':>6}"
            )
            if r.status == "unverified":
                print(f"    ^ {r.note}")
        counts = {s: sum(1 for r in results if r.status == s) for s in order}
        print(
            f"\nverified {counts['verified']} | unverified {counts['unverified']} "
            f"| unresolved {counts['unresolved']} (of {len(results)})"
        )

    if args.out:
        args.out.write_text(render_watchlist(results, source=args.source), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
