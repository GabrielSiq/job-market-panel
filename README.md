# job-market-panel

A longitudinal panel of the data-science labour market, collected daily from public job-posting APIs.

This is **a measurement instrument, not a job board**. It records when each posting appeared
and when it disappeared, so that questions like *"is this company's data-science org actually
growing?"* and *"what do remote senior DS roles really pay?"* can be answered from data rather
than guessed at. The value is entirely in the time series, which is why collection starts
before it is needed — it cannot be reconstructed retroactively.

Collection began **September 2026**.

## What it measures

- **Organizational growth signal** — per company, a rolling count of DS-family postings,
  whether DS *manager* roles appear, and DS share of total open requisitions.
- **Posting survival / time-to-close** — how long requisitions stay open, with right-censoring
  handled properly (jobs still open have not "lasted N days", they have lasted *at least* N days).
- **Compensation bands** from disclosed salary ranges, reported alongside the **disclosure rate**,
  which is the denominator that makes the distribution interpretable.
- **New-posting rate** — a 7-day rolling series, so a January hiring wave is a measurable
  deviation from a four-month baseline rather than a vibe.
- **Title taxonomy mix over time** — how Product DS, Analytics Engineer, Product Analyst and
  ML Engineer are being redefined relative to each other.

## Sources

Two sources doing different jobs. They are complements, **not** fallbacks for each other.

| | Himalayas (aggregator) | Greenhouse and other ATS boards (census) |
|---|---|---|
| Discovers unknown companies | **Yes — its whole job** | No, by construction |
| Same-day new postings | Partial — there is a moderation queue | **Yes, hour-zero** |
| Time-to-close / survival | **No** — listings auto-expire, so a disappearance is an expiry, not a fill | **Yes** |
| Remote share, DS share of total | **No** — remote-only, so there is no denominator | **Yes** |
| Org-growth signal | **No** | **Yes — this is the instrument** |

The ATS panel is a *census over a defined universe*: every open requisition at every watchlist
company, no sampling and no moderation queue. Its limitation is that it only sees companies
already on the watchlist — which is precisely what the aggregator is there to fix.

**Wiring a metric to a source that cannot support it produces a confident, wrong number, and
the failure is silent.** Each metric names its permitted source, and that binding is enforced.

## Design

- **Deterministic collection.** No LLM calls in the collection path, ever. A dataset gathered
  by something that re-decides its schema each morning is useless for statistics, because
  schema drift becomes indistinguishable from market movement.
- **Raw capture is immutable; everything derived is regenerable.** The event log is append-only
  and committed. `panel.duckdb` is gitignored and rebuilt from the log on every run. When the
  remote-inference heuristic improves, the entire history reclassifies and the series stays
  continuous — no discontinuity introduced by improving your own parser.
- **A source outage must never produce disappearance events.** A failed, rate-limited or
  truncated fetch is recorded as such and excluded from diffing. Silently marking a few hundred
  jobs "closed" because an endpoint timed out would permanently corrupt every survival metric.
- **Scheduling is GitHub Actions cron and nothing else.** No always-on machine, no VPS, no
  scheduler host.

## Layout

```
src/
  models.py       JobPosting, PostingEvent, SourceRun  — the stable contracts
  classify.py     role family + seniority + remote inference, driven by config
  collect.py      fetch -> normalize -> classify -> diff -> append
  sources/        one adapter per vendor, all conforming to one protocol
config/
  sources.yaml    source config, incl. the versioned query set
  taxonomy.yaml   classification rules as data, so reclassification is a config change
  watchlist.yaml  the census universe
tools/
  resolve_ats.py  company name or careers URL -> ATS vendor + board token
  healthcheck.py  event counts by source and day — the daily ten-second sanity check
data/
  events/         YYYY-MM-DD.jsonl.gz   appeared/disappeared, slim rows, every posting
  postings/       YYYY-MM-DD.jsonl.gz   full records incl. description, data-family roles only
  runs/           YYYY-MM-DD.jsonl      per-source health log; the diff step reads this
  latest/         new_postings.jsonl    stable path for downstream consumers
```

## Running it

```bash
uv sync                                  # Python is pinned in .python-version
uv run python -m src.collect             # one collection pass
uv run python -m src.report --rebuild    # rebuild panel.duckdb and render the report
uv run python tools/healthcheck.py       # counts by source and day
uv run pytest                            # adapter fixtures + correctness rules
```

The current state of the panel is always in **[`reports/latest.md`](reports/latest.md)**,
regenerated on every run.

The collector runs itself daily via `.github/workflows/collect.yml` and commits its output.

## Querying the panel yourself

The report answers the questions I thought to ask. `panel.duckdb` answers yours.

```bash
uv run python -m src.build          # rebuild from the raw log (~1s)
uv run python -m src.report         # regenerate reports/latest.md
duckdb panel.duckdb                 # or query it directly
```

The database is **gitignored and disposable** — it is rebuilt from the event log every
run, so deleting it loses nothing. The log is the only thing that matters.

| Object | What it is |
|---|---|
| `events` | Every `appeared` / `disappeared` row, all days. The raw series. |
| `open_postings` | **Currently open**, by replaying the log: the latest event per `job_id`, kept when that event is `appeared`. Matches the collector's own definition exactly. |
| `job_spans` | One row per posting with `first_seen`, `disappeared_on`, `is_closed`. The input to survival analysis once there is enough history. |
| `postings` | Full records including description text, for data-family roles. |
| `runs` | Per-source, per-company collection health. |

**`role_family` and `seniority` are recomputed at build time** from the stored title, under
the current `config/taxonomy.yaml`. The values recorded when the posting was collected are
kept alongside as `role_family_logged` / `seniority_logged`. This is what lets a
classification fix apply to the whole history instead of creating a step change on the day
the rule changed.

Two things to know before trusting a number:

- **`salary_source` matters.** `posting_disclosed` came from a structured vendor field;
  `description_parsed` was extracted from the job text by a regex. Both are
  employer-published, but they are not equally reliable — do not pool them.
- **`remote_source` matters.** `location_implied` means "the location names a specific
  workplace and nothing said remote", which measures around 95% accurate but is the
  weakest inference here. Filter it out when you want only strong evidence.

### Worked examples

```sql
-- Senior+ product-DS roles with a disclosed band clearing $250k
SELECT company_name, title, salary_min, salary_max, country
FROM open_postings
WHERE role_family = 'product_ds'
  AND seniority IN ('senior', 'staff', 'principal')
  AND salary_max >= 250000
ORDER BY salary_max DESC;

-- Companies building out a DS org: several IC reqs AND a manager req
SELECT company_name,
       count(*) FILTER (WHERE role_family = 'product_ds')  AS ic_reqs,
       count(*) FILTER (WHERE role_family = 'ds_manager')  AS manager_reqs
FROM open_postings
WHERE role_family IN ('product_ds', 'ds_manager')
GROUP BY 1 HAVING manager_reqs > 0 AND ic_reqs >= 2
ORDER BY ic_reqs DESC;

-- Remote US roles, strong evidence only
SELECT company_name, title, seniority, salary_max
FROM open_postings
WHERE is_remote AND country = 'US'
  AND remote_source <> 'location_implied'
  AND role_family IN ('product_ds', 'ds_manager', 'product_analyst');

-- What closed, and how long it stayed open (needs more history to be meaningful)
SELECT company_name, title, first_seen, disappeared_on,
       disappeared_on - first_seen AS days_open
FROM job_spans
WHERE is_closed AND role_family = 'product_ds'
ORDER BY days_open;

-- Where a classification changed when the taxonomy was fixed
SELECT title, role_family_logged, role_family, count(*)
FROM events
WHERE role_family IS DISTINCT FROM role_family_logged
GROUP BY 1, 2, 3 ORDER BY 4 DESC;
```

## Measured findings

Values here are **measured, not assumed**. Several contradict the relevant API documentation.

### Himalayas: the published OpenAPI spec disagrees with the live API

`https://himalayas.app/docs/openapi.json` declares shapes the API does not return. Each of
these would corrupt data silently rather than raising:

| Field | OpenAPI declares | Live API returns |
|---|---|---|
| `locationRestrictions` | array of `{alpha2, name, slug}` objects | array of plain strings — `["United States"]` |
| `timezoneRestrictions` | array of strings — `"UTC-5"` | array of **integers** — `[-10, -9, …, 14]` |
| `pubDate` / `expiryDate` | Unix **milliseconds** | Unix **seconds** — parsing as ms yields year ~58000 |
| `guid` | a short slug | a full `himalayas.app` URL, identical to `applicationLink` |

Also: fields are `categories` / `timezoneRestrictions` (plural); `seniority` is an *array* of
labels; `salaryPeriod` is one of `hourly|weekly|fortnightly|monthly|annual` and salary figures
are **in that period, not annualized**; listings expire at **60 days** (not 30);
`applicationLink` points at a Himalayas page rather than the employer's ATS, so cross-source
matching cannot use URLs; and `parentCategories` is empty on roughly 58% of rows while
`categories` are per-job generated slugs rather than a controlled vocabulary — so
classification here is title-driven.

### Himalayas: two pagination traps

1. `/jobs/api/search` **ignores `limit`** and returns **17–20 items per page** while reporting
   `limit: 20`. The idiomatic `while len(page) == limit` loop therefore terminates on page one.
   Terminate on an empty page, a date cutoff, or a fixed page budget — never on page size.
2. `totalCount` on `/search` is a **relevance-candidate count, not a result count**:
   `q=analytics` returns `totalCount: 5000` with **zero jobs**. It cannot be used to size
   pagination. On the `/jobs/api` browse endpoint it is real (101,855 active listings), which
   confirms that a full-corpus crawl at 20 records per request is infeasible.

Sampling `q=data scientist&sort=recent` over pages 1–12 gave **229 distinct** postings with
recency decaying roughly 2–3 days per page. Search relevance is loose — that query also returned
actuaries, toxicologists and backend engineers — so the collector over-fetches broadly and
classifies locally.

**Observed load:** the 12-term query set completes in 44 requests per run with the 4-day lookback
cutoff, against a 60-request worst case. No 429 has been seen at that rate with requests spaced
0.8s apart. *The practical ceiling remains unmeasured — the collector stays well beneath it rather
than probing for it.*

### Greenhouse: remote status is mostly inferred, and that inference has real error

Across 6 sampled boards (631, 886, 226, 165, 153 and 86 postings), the **"Workplace Type"
metadata field exists on only one of them**. The others expose only company-specific custom
fields. Remote status is therefore inferred from location strings in most cases, and every
record carries a `remote_source` field recording *how* the determination was made.

Location strings are inconsistent in ways that matter: `"United States"` and `"United States "`
(trailing space) occur as separate values, alongside `"Remote - USA"`. Naive matching produces
remote shares ranging from 86% on one board to **0% of 153 postings** on another — the latter
being a company that simply never writes "remote" in a location field.

**Measured on a hand-labelled sample of 100 postings** (ground truth assigned by reading each
posting's location field together with the work-location sentences in its description):

| Evidence used | Share of sample | Accuracy |
|---|---|---|
| Board "Workplace Type" metadata field | 15% | **100%** |
| Location string, where decisive | 40% | **97.5%** |
| Work-location prose in the description | 25% | **100%** |
| No determinable evidence — recorded as NULL | 20% | — |

Overall **98.8%** accurate on the 80 postings it decides. The single error was a board whose
location read `"Remote, USA"` while its description carried `#LI-Onsite`; the location is treated
as the more specific field, and that ordering is worth one error for the cases it gets right.

The description pass was added because of what the sample showed: **91% of postings with no
determinable location were in fact onsite or hybrid, and said so plainly in their own text**.
Leaving them NULL was honest but discarded recoverable signal on a majority of the panel. It runs
at collection time by necessity — descriptions are stored only for tracked families, so for most
postings the text is gone once the run ends, making the inference impossible rather than merely
deferred. Across the full panel it cut NULLs from 59% to 35%.

Bare `"remote"` is deliberately never treated as a signal. It appears in benefits boilerplate
("Remote work, medical insurance, flexible time off…") on strictly onsite postings, and in
"remote sensing", a real data-science domain. Every pattern requires a qualifier.

Greenhouse does expose `first_published`, which is a better posting date than `updated_at`, and
`departments`, which gives a usable denominator for DS share of hiring.

### Known biases

- The ATS panel skews toward **Greenhouse/Ashby/Lever**, i.e. startups and scale-ups.
  **Workday is absent** — its endpoints are unofficial and break.
- Aggregator coverage is **commercially determined** (paid postings alongside crawled ones,
  with partnerships that change without notice and are not observable from outside). Its volume
  is treated as a discovery feed, never as a market denominator.
- Remote roles have systematically **higher salary-disclosure rates** than onsite ones, because
  a role open to several US states must comply with the strictest applicable pay-transparency
  law. Remote is therefore the best-measured segment, and disclosure rates are reported
  alongside every compensation figure.

## Attribution and use

Job data is collected from public, keyless JSON APIs used as intended, at modest request rates.
Remote listings are sourced in part from **[Himalayas](https://himalayas.app)**. This repository
is an analysis panel: it does not republish listings and does not submit them to third-party job
sites.

No site that prohibits automated access is scraped. In particular there is **no LinkedIn
scraping or automation of any kind**.

## License

MIT for the code. The collected data is derived from public postings and is provided as-is.
