"""Parse a free-text location string into a country, a region, and a shape.

**Why this exists.** Every ATS row had `country = None`. Only the aggregator ever set it,
so the panel could not answer "how many of these are open to US candidates" - and
US-eligibility is one of the few genuinely hard filters in this search.

It also supplies the missing input for remote inference. 45% of ATS postings had no
determinable remote status, and their locations turned out to be overwhelmingly specific
places: "Costa Mesa, California, United States", "Hawthorne, CA", "Starbase, TX". A named
city is strong evidence of an onsite role, but *only* if you can tell a city from a
country - "United States" alone says nothing, while "Bastrop, TX" says a great deal.

Reference data, not tuning knobs, so it lives here rather than in taxonomy.yaml. The
derived values are stored alongside `location_raw`, which is always retained, so this can
be improved and the history rebuilt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.normalize import clean_text

US_STATES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
    "washington dc": "DC",
    "washington, d.c.": "DC",
    "puerto rico": "PR",
}
_STATE_CODES = set(US_STATES.values())

#: Country names and demonyms seen in real postings, mapped to ISO alpha-2. Not
#: exhaustive by design - an unrecognized country yields None rather than a wrong guess.
COUNTRIES = {
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "us": "US",
    "united states of america": "US",
    "america": "US",
    "canada": "CA",
    "united kingdom": "GB",
    "uk": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "great britain": "GB",
    "ireland": "IE",
    "germany": "DE",
    "france": "FR",
    "spain": "ES",
    "portugal": "PT",
    "italy": "IT",
    "netherlands": "NL",
    "holland": "NL",
    "belgium": "BE",
    "switzerland": "CH",
    "austria": "AT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "poland": "PL",
    "czech republic": "CZ",
    "czechia": "CZ",
    "romania": "RO",
    "bulgaria": "BG",
    "greece": "GR",
    "hungary": "HU",
    "ukraine": "UA",
    "israel": "IL",
    "india": "IN",
    "singapore": "SG",
    "japan": "JP",
    "china": "CN",
    "hong kong": "HK",
    "taiwan": "TW",
    "south korea": "KR",
    "korea": "KR",
    "australia": "AU",
    "new zealand": "NZ",
    "brazil": "BR",
    "mexico": "MX",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
    "peru": "PE",
    "uruguay": "UY",
    "south africa": "ZA",
    "nigeria": "NG",
    "kenya": "KE",
    "egypt": "EG",
    "united arab emirates": "AE",
    "uae": "AE",
    "dubai": "AE",
    "saudi arabia": "SA",
    "turkey": "TR",
    "philippines": "PH",
    "indonesia": "ID",
    "vietnam": "VN",
    "thailand": "TH",
    "malaysia": "MY",
    "pakistan": "PK",
    "bangladesh": "BD",
    "costa rica": "CR",
    "panama": "PA",
    "guatemala": "GT",
    "lithuania": "LT",
    "latvia": "LV",
    "estonia": "EE",
    "serbia": "RS",
    "croatia": "HR",
    "slovakia": "SK",
    "slovenia": "SI",
    "armenia": "AM",
    "georgia country": "GE",
    "cyprus": "CY",
    "malta": "MT",
}

#: Words that mean "no particular place", which is the opposite of a specific city.
_PLACELESS = re.compile(
    r"\b(remote|anywhere|worldwide|global|distributed|virtual|flexible|multiple locations|"
    r"various|home|wfh|work from home|us-based|nationwide|field|travel)\b",
    re.I,
)
_SPLIT = re.compile(r"\s*(?:;|\||/| or |,| and | - |\u2013)\s*", re.I)


@dataclass(frozen=True)
class LocationFinding:
    country: str | None
    region: str | None  # US state code where determinable
    #: True when the string names a settlement rather than only a country/region or a
    #: placeless word. This is what licenses treating a posting as onsite.
    names_a_place: bool
    #: True when several distinct locations are listed.
    is_multi: bool


def _normalize_token(token: str) -> str:
    return re.sub(r"[.\s]+", " ", token.strip().lower()).strip()


def parse_location(raw: str | None) -> LocationFinding:
    """Parse right-to-left, because location strings are right-anchored.

    "Costa Mesa, California, United States" runs city -> region -> country, so the country
    is the last token and the city the first. Reading left-to-right breaks on
    "Washington, District of Columbia": the leading token is the *city* Washington, but a
    naive scan matches it against the *state* Washington and lands in the wrong corner of
    the country.
    """
    text = clean_text(raw)
    if not text:
        return LocationFinding(None, None, False, False)

    tokens = [t for t in (_normalize_token(p) for p in _SPLIT.split(text)) if t]
    country: str | None = None
    region: str | None = None
    remaining = list(tokens)

    # Country first, from the right.
    while remaining:
        token = remaining[-1]
        if token in COUNTRIES:
            country = COUNTRIES[token]
            remaining.pop()
            break
        if token.upper() in _STATE_CODES and len(token) == 2:
            break  # a state code here means the country was simply omitted
        if _PLACELESS.fullmatch(token):
            remaining.pop()
            continue
        break

    # Then a US state, again from the right.
    while remaining:
        token = remaining[-1]
        upper = token.upper()
        if token in US_STATES:
            region, country = US_STATES[token], country or "US"
            remaining.pop()
            break
        if upper in _STATE_CODES and len(upper) == 2:
            region, country = upper, country or "US"
            remaining.pop()
            break
        if _PLACELESS.fullmatch(token):
            remaining.pop()
            continue
        break

    place_tokens = [t for t in remaining if not _PLACELESS.fullmatch(t)]
    names_a_place = bool(place_tokens)
    is_multi = bool(re.search(r"[;|]", text)) or len(place_tokens) > 1

    return LocationFinding(
        country=country, region=region, names_a_place=names_a_place, is_multi=is_multi
    )
