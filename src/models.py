"""The stable data contracts. Spec Sections 3.4 and 6.

These are the interface that makes later phases additive instead of destructive.
Adding optional fields later is fine; **renaming or repurposing existing ones is not.**

Two rules that shape everything here:

1. `posted_at` and `first_seen` are different things and must never be conflated.
   `posted_at` is what a source *asserts* and is frequently wrong or absent;
   `first_seen` is a fact about our own observation. All survival and freshness
   metrics use `first_seen`.

2. Derived fields (`role_family`, `seniority`, `is_remote`) are stored for query speed,
   but the raw fields they were derived from are stored alongside them so the whole
   history can be reclassified when a heuristic improves. Never write a derived field
   back over its own input.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, computed_field, field_validator

from src.normalize import clean_text, normalize_title

# Descriptions are stored for analysis and resume tailoring, not archived verbatim.
# Long enough to hold any real job description; short enough that a pathological
# posting cannot bloat a day's file.
DESCRIPTION_MAX_CHARS = 20_000


class Source(StrEnum):
    HIMALAYAS = "himalayas"
    ADZUNA = "adzuna"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKABLE = "workable"


class RoleFamily(StrEnum):
    PRODUCT_DS = "product_ds"
    DS_MANAGER = "ds_manager"
    ML_ENG = "ml_eng"
    ANALYTICS_ENG = "analytics_eng"
    DATA_ENG = "data_eng"
    #: Product/growth/experimentation analytics - the strong end of the analyst market.
    #: Split out from ANALYST because the two are different jobs competing for different
    #: people, and lumping them made the good ones unfindable.
    PRODUCT_ANALYST = "product_analyst"
    ANALYST = "analyst"
    OTHER = "other"


#: Families whose full description text is captured (spec Phase 1 storage policy).
#: Deliberately wider than the roles Gabriel would apply to: this is the one
#: irreversible storage choice — an uncaptured description cannot be recovered later —
#: and the title-mix metric tracks DS work being absorbed into ML Engineer, which is
#: unmeasurable without ML Eng descriptions.
TRACKED_FAMILIES: frozenset[RoleFamily] = frozenset(
    {
        RoleFamily.PRODUCT_DS,
        RoleFamily.DS_MANAGER,
        RoleFamily.ML_ENG,
        RoleFamily.ANALYTICS_ENG,
        RoleFamily.DATA_ENG,
        RoleFamily.PRODUCT_ANALYST,
        RoleFamily.ANALYST,
    }
)


class Seniority(StrEnum):
    """IC and management levels, fine-grained enough to separate adjacent bands.

    `senior_staff` and `senior_manager` exist because collapsing them into `staff` and
    `manager` conflated levels that are a full step apart in scope and pay. That is a
    measurement error before it is a filtering one: it inflated the pay distribution for
    the two bands Gabriel is actually a candidate for.
    """

    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    SENIOR_STAFF = "senior_staff"
    PRINCIPAL = "principal"
    MANAGER = "manager"
    SENIOR_MANAGER = "senior_manager"
    DIRECTOR = "director"
    UNKNOWN = "unknown"


class RemoteSource(StrEnum):
    """How `is_remote` was determined. Spec correctness rule 5.

    Without this, a remote-share number that looks odd in December is undebuggable.
    Empirically this matters more than expected: the Greenhouse "Workplace Type"
    metadata field appears on only ~1 in 6 boards, so most ATS rows land on
    LOCATION_STRING and inherit its error rate.
    """

    SOURCE_FIELD = "source_field"  # the source has an explicit remote concept
    ATS_FLAG = "ats_flag"  # a first-class boolean (Ashby isRemote)
    METADATA_FIELD = "metadata_field"  # a board's custom "Workplace Type" field
    LOCATION_STRING = "location_string"  # inferred by matching the location text
    DESCRIPTION_TEXT = "description_text"  # inferred from work-location prose in the JD
    #: Last resort: the location names a specific workplace ("Hawthorne, CA",
    #: "Starbase, TX") and nothing anywhere said remote or hybrid. Measured ~95% correct,
    #: but weaker than the others, so it gets its own value and analysis can exclude it.
    LOCATION_IMPLIED = "location_implied"
    UNKNOWN = "unknown"


class RemoteFinding(BaseModel):
    """The outcome of a remote-status determination: the verdict plus how it was reached.

    Paired deliberately. `is_remote` alone is not enough — spec correctness rule 5
    requires knowing whether a value came from a first-class flag or from a regex over a
    messy location string, because those carry very different error rates.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    is_remote: bool | None
    remote_source: RemoteSource


class SalaryPeriod(StrEnum):
    """Widened from the spec's `year|month|hour`.

    Required by reality: Himalayas emits `hourly|weekly|fortnightly|monthly|annual`
    and does **not** normalize figures to annual. Annualization is an analysis-layer
    concern; storing a converted figure here would destroy the raw value.
    """

    YEAR = "year"
    MONTH = "month"
    WEEK = "week"
    FORTNIGHT = "fortnight"
    HOUR = "hour"


class SalarySource(StrEnum):
    POSTING_DISCLOSED = "posting_disclosed"
    #: Employer-disclosed, but printed in the description rather than a structured field
    #: and extracted by src/salary.py. Genuinely disclosed - the employer published it -
    #: but it passed through a parser, so it is kept separable from the fields a vendor
    #: handed us. Report the two as distinct series.
    DESCRIPTION_PARSED = "description_parsed"
    SOURCE_ESTIMATE = "source_estimate"  # Adzuna predictions — never pool with disclosed


class RunStatus(StrEnum):
    """Spec Section 6.3 declares only ok|error|empty, but the Phase 1 correctness rules
    and Section 8 both require a `partial` status for a truncated run. The spec
    contradicted itself; this resolves it in favour of the correctness rules.

    **Only `OK` permits diffing.** Everything else means "we did not look", which is a
    different thing from "it was not there".
    """

    OK = "ok"
    PARTIAL = "partial"  # truncated mid-walk, e.g. a 429 partway through the query set
    EMPTY = "empty"
    ERROR = "error"

    @property
    def allows_diffing(self) -> bool:
        return self is RunStatus.OK


class EventType(StrEnum):
    APPEARED = "appeared"
    DISAPPEARED = "disappeared"


class JobPosting(BaseModel):
    """Spec Section 6.1. Every source adapter maps into this; nothing downstream ever
    touches raw vendor JSON."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    source: Source
    source_job_id: str
    company_name: str
    company_slug: str
    title: str

    role_family: RoleFamily | None = None
    seniority: Seniority | None = None
    #: The source's own seniority label, kept verbatim. Himalayas supplies one; ATS
    #: boards do not. The headline series uses our uniform `seniority` instead, because
    #: a consistent definition across sources matters more than accuracy on either one.
    seniority_source: str | None = None

    location_raw: str | None = None
    country: str | None = None
    #: US state code where determinable. Derived from `location_raw`, which is always
    #: retained, so both can be recomputed.
    region: str | None = None
    is_remote: bool | None = None
    remote_source: RemoteSource = RemoteSource.UNKNOWN

    employment_type: str | None = None
    department: str | None = None

    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: SalaryPeriod | None = None
    #: The source's own period token before normalization, so a mapping bug is recoverable.
    salary_period_raw: str | None = None
    salary_is_estimated: bool = False
    salary_source: SalarySource | None = None

    description_text: str | None = None

    apply_url: str
    posted_at: datetime | None = None  # what the source claims; unreliable, nullable
    first_seen: date  # our observation — the trustworthy one
    last_seen: date
    fetched_at: datetime

    #: Version of the query set that surfaced this row. Changing the query set changes
    #: the series, so it must be visible in the data rather than silently shifting it.
    query_set_version: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def job_id(self) -> str:
        """Stable identity. Must survive the source re-issuing the same posting.

        Known limitation: the Himalayas `guid` is a title-slug URL, so a *retitled*
        posting yields a new id and therefore a false disappear+appear pair. Phase 2's
        `dedupe_key` is the mitigation; it is not corrected at collection time because
        raw capture stays immutable.
        """
        return f"{self.source.value}:{self.source_job_id}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def title_normalized(self) -> str:
        return normalize_title(self.title)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dedupe_key(self) -> str:
        """Groups observations of what is probably one requisition, across sources.

        A job can be seen on both an aggregator and its company's own ATS board. Both
        observations are kept - raw capture is immutable, and they are genuinely two
        different facts about our own looking. This key lets the analysis layer collapse
        them without anything being dropped at collection time.

        Deliberately coarse: company plus normalized title. It will merge two genuinely
        distinct reqs with identical titles at one company (real, and common for
        multi-location postings), so it is a grouping hint for analysis, never an identity.
        `job_id` remains the only identity.
        """
        return f"{self.company_slug}:{self.title_normalized}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_tracked(self) -> bool:
        """Whether the full record (with description) is stored. Spec storage policy."""
        return self.role_family in TRACKED_FAMILIES

    @field_validator("title", "company_name", mode="before")
    @classmethod
    def _require_clean_text(cls, v: object) -> object:
        """Collapse whitespace on the fields used as join keys and match inputs.

        Not cosmetic: Greenhouse emits 'Senior Data Scientist ' and
        'Senior Data Scientist' as distinct values on the same board.
        """
        return clean_text(v) if isinstance(v, str) else v

    @field_validator("location_raw", "department", "employment_type", mode="before")
    @classmethod
    def _clean_optional(cls, v: object) -> object:
        return clean_text(v) if isinstance(v, str) else v

    @field_validator("description_text", mode="before")
    @classmethod
    def _truncate_description(cls, v: object) -> object:
        if isinstance(v, str) and len(v) > DESCRIPTION_MAX_CHARS:
            return v[:DESCRIPTION_MAX_CHARS]
        return v

    @field_validator("salary_currency", mode="before")
    @classmethod
    def _upper_currency(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) and v.strip() else None


class PostingEvent(BaseModel):
    """Spec Section 6.2 — the event log. One gzipped JSONL file per day.

    Slim by design: every posting from every source, so non-DS roles still contribute
    the denominators the growth signal needs, but without description text. Append-only
    and immutable; everything downstream reads this.
    """

    model_config = ConfigDict(extra="forbid")

    date: date
    event: EventType
    job_id: str
    source: Source
    company_slug: str
    company_name: str
    title: str
    role_family: RoleFamily | None = None
    seniority: Seniority | None = None
    is_remote: bool | None = None
    remote_source: RemoteSource = RemoteSource.UNKNOWN
    location_raw: str | None = None
    country: str | None = None
    region: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: SalaryPeriod | None = None
    salary_is_estimated: bool = False
    posted_at: datetime | None = None
    apply_url: str
    #: Analysis-layer grouping hint for cross-source duplicates; see JobPosting.dedupe_key.
    dedupe_key: str | None = None

    @classmethod
    def from_posting(cls, posting: JobPosting, event: EventType, on: date) -> PostingEvent:
        return cls(
            date=on,
            event=event,
            job_id=posting.job_id,
            source=posting.source,
            company_slug=posting.company_slug,
            company_name=posting.company_name,
            title=posting.title,
            role_family=posting.role_family,
            seniority=posting.seniority,
            is_remote=posting.is_remote,
            remote_source=posting.remote_source,
            location_raw=posting.location_raw,
            country=posting.country,
            region=posting.region,
            salary_min=posting.salary_min,
            salary_max=posting.salary_max,
            salary_period=posting.salary_period,
            salary_is_estimated=posting.salary_is_estimated,
            posted_at=posting.posted_at,
            apply_url=posting.apply_url,
            dedupe_key=posting.dedupe_key,
        )


class SourceRun(BaseModel):
    """Spec Section 6.3 — the health log. One row per source (per company, for ATS
    boards) per run, at data/runs/YYYY-MM-DD.jsonl.

    **The diff step reads this.** A source whose status is not `ok` is excluded from
    disappearance detection for that day. This file is what makes "never infer
    disappearance from a failed fetch" enforceable rather than aspirational.
    """

    model_config = ConfigDict(extra="forbid")

    date: date
    source: Source
    status: RunStatus
    records_fetched: int = 0
    duration_s: float = 0.0
    error_message: str | None = None

    #: Set for ATS boards. Correctness rule 1 isolates failures *per company*, which is
    #: unenforceable with a single row per source per run: one dead board must not
    #: suppress diffing for the other 249, nor mark its own jobs closed.
    company_slug: str | None = None

    #: Which query set produced this run, so a change is visible in the data.
    query_set_version: str | None = None

    #: Free silent-change detection. Himalayas returns `updatedAt` (feed refresh time)
    #: and `comments` (an API changelog string); healthcheck flags when the notice
    #: changes, which is the cheapest available warning that a vendor altered its shape.
    feed_updated_at: datetime | None = None
    api_notice: str | None = None

    pages_fetched: int = 0
    requests_made: int = 0

    @property
    def allows_diffing(self) -> bool:
        return self.status.allows_diffing


class CollectionScope(BaseModel):
    """What a run actually looked at, so the differ knows what 'absent' means.

    A posting is only a candidate for disappearance if this run genuinely looked in the
    place it would have been. Without this the differ cannot distinguish "the company
    removed the req" from "we skipped that board today", which is the single most
    damaging silent failure available.
    """

    model_config = ConfigDict(extra="forbid")

    source: Source
    company_slug: str | None = None

    def key(self) -> tuple[str, str | None]:
        return (self.source.value, self.company_slug)
