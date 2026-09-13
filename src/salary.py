"""Extract employer-disclosed pay ranges from job-description prose.

**Why this is worth doing.** Only Ashby publishes structured compensation; Greenhouse and
Lever expose none. But pay-transparency law means employers print the band in the
description anyway, and measurement showed that **392 of 1,192 stored descriptions (33%)
contain a range we were recording as "no salary"** - more than doubling the disclosed-band
coverage of the panel.

**Why it was deferred until measured.** A bad parse pollutes the compensation series
permanently and silently. Three properties keep that in check:

1. **Provenance is separate.** Parsed bands get `SalarySource.DESCRIPTION_PARSED` and are
   never pooled with `POSTING_DISCLOSED`. Both are employer-disclosed - the difference is
   that one was handed to us in a field and the other was extracted by this code, and only
   the first is above suspicion.
2. **Implausible results are discarded, not stored.** An inverted or absurd range yields
   nothing rather than a number nobody will re-examine.
3. **It is reprocessable.** Descriptions are retained for tracked families, so improving
   this parser and rebuilding reclassifies the whole history.

Structured fields always win; this only fills a gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.models import SalaryPeriod, SalarySource

# $150,000 | $150,000.00 | $150K | $150k
_MONEY = (
    r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?"  # $150,000 / $150,000.00
    r"|\$\s?\d{2,3}(?:\.\d+)?\s?[kK]\b"  # $150K
    r"|\$\s?\d{1,4}(?:\.\d{1,2})?\b"  # $45 - only survives with hourly context
)
#: Employers separate the two ends with a hyphen, en/em dash, or the word "to".
_RANGE = re.compile(rf"({_MONEY})\s*(?:-|\u2013|\u2014|to)\s*({_MONEY})", re.I)

#: Pay stated per hour rather than per year. Checked near the match, not document-wide:
#: a JD can mention an hourly contractor rate paragraphs away from the salaried band.
_HOURLY = re.compile(r"\b(per hour|/ ?hour|hourly|an hour)\b", re.I)
_MONTHLY = re.compile(r"\b(per month|/ ?month|monthly)\b", re.I)

#: Sanity bounds. Anything outside these is a parse failure, not a salary - a "$5,000 -
#: $10,000 signing bonus" or a "$1,000,000 - $5,000,000 in funding raised" sentence.
_MIN_ANNUAL, _MAX_ANNUAL = 10_000.0, 2_000_000.0
_MIN_HOURLY, _MAX_HOURLY = 5.0, 2_000.0

#: Currencies employers name explicitly next to a figure. Defaults to USD only when the
#: posting gives no signal, because the panel is US-focused; a wrong currency would be
#: far worse than a missing one.
_CURRENCY = re.compile(r"\b(USD|CAD|EUR|GBP|AUD|CHF|SGD|INR|BRL|MXN)\b")


@dataclass(frozen=True)
class ParsedSalary:
    salary_min: float
    salary_max: float
    salary_currency: str | None
    salary_period: SalaryPeriod
    salary_source: SalarySource
    #: How many distinct ranges the description contained. >1 means the employer listed
    #: several bands - usually geographic tiers ("in Washington DC... in Virginia...") or
    #: internal levels ("I4 ... I5 ... I6"). The stored band spans all of them, which is
    #: the honest reading but a wider one; analysis can restrict to `== 1` for tight
    #: estimates. Roughly 19% of matches are multi-range.
    range_count: int


def _to_number(text: str) -> float:
    cleaned = text.replace("$", "").replace(",", "").strip()
    if cleaned.lower().endswith("k"):
        return float(cleaned[:-1].strip()) * 1000
    return float(cleaned)


def parse_salary(description: str | None) -> ParsedSalary | None:
    """Best-effort pay band from prose. Returns None rather than guessing."""
    if not description:
        return None

    lows: list[float] = []
    highs: list[float] = []
    period = SalaryPeriod.YEAR
    saw_hourly = False

    for match in _RANGE.finditer(description):
        try:
            low, high = _to_number(match.group(1)), _to_number(match.group(2))
        except ValueError:
            continue
        if high < low:
            continue

        window = description[max(0, match.start() - 120) : match.end() + 120]
        if _HOURLY.search(window):
            local_period = SalaryPeriod.HOUR
        elif _MONTHLY.search(window):
            local_period = SalaryPeriod.MONTH
        else:
            local_period = SalaryPeriod.YEAR

        floor, ceiling = (
            (_MIN_HOURLY, _MAX_HOURLY)
            if local_period is SalaryPeriod.HOUR
            else (_MIN_ANNUAL, _MAX_ANNUAL)
        )
        if not (floor <= low <= ceiling and floor <= high <= ceiling):
            continue

        # Mixing an hourly rate and an annual band into one span would be nonsense, so the
        # first period seen wins and later mismatches are skipped.
        if lows and local_period is not period:
            continue
        period = local_period
        saw_hourly = saw_hourly or local_period is SalaryPeriod.HOUR
        lows.append(low)
        highs.append(high)

    if not lows:
        return None

    currency_match = _CURRENCY.search(description)
    return ParsedSalary(
        # Spanning every stated band is the honest reading when an employer lists several:
        # the role really is open across those tiers. `range_count` preserves the ambiguity.
        salary_min=min(lows),
        salary_max=max(highs),
        salary_currency=currency_match.group(1).upper() if currency_match else "USD",
        salary_period=period,
        salary_source=SalarySource.DESCRIPTION_PARSED,
        range_count=len(lows),
    )
