"""Text normalization shared by adapters, the classifier and the analysis layer.

Kept in one place because the panel joins across sources on normalized company names,
and any drift between how two adapters normalize would silently split one company into
two rows in the org-growth signal — the headline metric.

Real data motivating this: Greenhouse returns "United States" and "United States "
(trailing space) as *separate* location values, and titles like "Senior Data Scientist "
alongside "Senior Data Scientist".
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")

# Job titles use en dash, em dash and minus sign interchangeably with hyphen;
# fold them all so "Senior DS \u2013 Payments" and "Senior DS - Payments" match.
_DASHES = str.maketrans({0x2013: "-", 0x2014: "-", 0x2212: "-"})

# Suffixes stripped when deriving a company slug, so "Acme, Inc." and "Acme" join.
_COMPANY_SUFFIXES = (
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "gmbh",
    "bv",
    "nv",
    "plc",
    "sa",
    "ag",
    "ab",
    "oy",
    "as",
    "pte",
)


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace and strip. Returns None for empty/None input."""
    if value is None:
        return None
    cleaned = _WS.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    return cleaned or None


def normalize_title(title: str) -> str:
    """Lowercased, whitespace-collapsed title for pattern matching and grouping.

    Deliberately preserves word content: the taxonomy matches on words like
    'senior' and 'manager', so nothing may be dropped here.
    """
    cleaned = clean_text(title) or ""
    return cleaned.translate(_DASHES).lower()


def slugify(value: str) -> str:
    """Lowercase alphanumeric slug, hyphen-separated."""
    cleaned = unicodedata.normalize("NFKD", clean_text(value) or "")
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", cleaned.lower()).strip("-")


def company_slug(name: str) -> str:
    """Canonical cross-source company key.

    Strips a trailing legal suffix so 'Acme, Inc.' and 'Acme' resolve together. Where a
    source supplies its own canonical slug (Himalayas `companySlug`, an ATS board token),
    prefer that and use this only as the fallback.
    """
    slug = slugify(name)
    parts = slug.split("-")
    while len(parts) > 1 and parts[-1] in _COMPANY_SUFFIXES:
        parts.pop()
    return "-".join(parts) or slug


def content_hash(value: str | None) -> str | None:
    """Short stable hash, used to detect a description changing between runs."""
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
