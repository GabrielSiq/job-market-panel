# job-market-panel — working notes for Claude Code

**Read this file first. It is how context survives between sessions.**

> **Privacy boundary — this repo is PUBLIC.**
> Nothing personal goes here: no compensation targets, no search criteria, no resume
> content, no contact lists, no company shortlists framed as personal preference.
> This repo holds public job postings, a list of company names, and analysis code.
> The design spec (`JOB_SEARCH_SYSTEM_SPEC.md`) is **private** and lives in the
> `job-search-assets` repo. It is in `.gitignore` here as a second line of defence.
> If you need the spec, read it there.

---

## What this is

A **measurement instrument**, not a job board. A daily collector captures job postings and
records when each posting appeared and disappeared, accumulating a longitudinal dataset of
the data-science labour market from September 2026 onward.

The value is entirely in the time series, and the time series can only be built by starting
early — it cannot be bought, scraped retroactively, or reconstructed later. **Every day
without collection is a day of baseline permanently lost.** That fact sets the priorities:
speed to first data, and never breaking the series.

Highest-value output: an **organizational growth signal** per company — how many DS-family
roles it has posted over a rolling window, whether it has posted DS *manager* roles, and
how that is trending.

## Architecture in one screen

```
Himalayas API  ──►  collect.py ──► normalize ──► classify ──► diff vs. replayed state
Greenhouse ATS ──►       │                                          │
                         ▼                                          ▼
                   data/runs/*.jsonl                      data/events/*.jsonl.gz
                   (health log; gates diffing)            data/postings/*.jsonl.gz
                                                          data/latest/new_postings.jsonl
                         │
                         └──► build.py ──► panel.duckdb ──► reports/YYYY-WW.md
```

Two repos, by design:

| Repo | Visibility | Why |
|---|---|---|
| `job-market-panel` (this one) | Public | Actions minutes are free and unlimited on public repos; `schedule` reliably fires. Contains nothing personal. |
| `job-search-assets` | Private | Spec, criteria, accomplishment bank, resume pipeline. |

Cross-repo interface is one file over plain HTTPS: the private repo fetches this repo's
`data/latest/new_postings.jsonl` via `raw.githubusercontent.com`. No auth, no submodules,
no cross-repo tokens.

### The three principles that must not be violated

1. **No LLM calls in the collection path. Ever.** A longitudinal dataset collected by an
   agent that re-decides its schema each morning is useless for statistics — schema drift
   becomes indistinguishable from market movement. The collector is deterministic.
2. **Raw capture is immutable; everything derived is regenerable.** The event log is
   append-only and committed. `panel.duckdb` is gitignored and rebuilt from the log every
   run. When the remote-inference heuristic improves in November, the whole
   September–November history reclassifies and the series stays continuous.
3. **A source outage must never produce disappearance events.** This is the single most
   damaging silent failure available. See correctness rules below.

### Source fitness — binding

The two sources do different jobs and are **not** fallbacks for each other.

| | Himalayas (aggregator) | Greenhouse / ATS (census) |
|---|---|---|
| Discovery of unknown companies | **Yes — its whole job** | No, by construction |
| Watchlist seeding | **Yes** | No |
| Same-day new postings | Partial (moderation queue) | **Yes, hour-zero** |
| Time-to-close / survival | **No — listings auto-expire** | **Yes** |
| Remote share, DS share of total | **No — remote-only, no denominator** | **Yes** |
| Org-growth signal | **No** | **Yes — this is the instrument** |

Wiring a metric to the wrong source produces a confident, wrong number, and the failure is
silent. Never compute survival from Himalayas rows.

## Current status

**Phase 1 — in progress (started 2026-09-11).** Two-source collector: Himalayas (discovery)
+ Greenhouse (census). Goal is data flowing within days and the Section 6 contracts locked.

Phase 2+ (more ATS vendors, watchlist growth, analysis/reports, resume pipeline, daily brief)
are **out of scope** until Phase 1 has run unattended for at least a few days. Do not start
Phase N+1 in the session that finished Phase N — the observation period is part of the build.

## Correctness rules (get these wrong and the dataset is worthless)

1. **Never infer disappearance from a failed fetch.** If a source errors, times out, is
   rate-limited, or returns zero results, record a `SourceRun` with `status != ok` and skip
   diffing that source for the day. For the ATS panel this applies **per company**.
2. **A partial run is a failed run for diffing purposes.** Jobs not seen because the walk
   was truncated are not absent — they were never looked for.
3. **Require two consecutive misses before recording a disappearance.** One absent day is
   more often a pagination hiccup than a closed req.
4. **Derive current state by replaying the event log.** No mutable committed state file.
5. **Record how each inference was made** (`remote_source`). This is what makes December's
   odd numbers debuggable.
6. **One file per day, not one growing file.** A growing file means git stores a new blob
   of the whole thing on every commit.

## Conventions

- `uv` for everything: `uv run pytest`, `uv run python -m src.collect`. Python pinned to
  3.12 via `.python-version` so CI and local cannot drift.
- Commit directly to `main`. Solo project; PRs are ceremony with no reviewer.
- Validation failures on individual records are **logged and skipped, never raised**. One
  malformed posting must not kill a day's collection.
- `posted_at` (what the source claims, often wrong) and `first_seen` (our own observation)
  are different things and must never be conflated. All survival and freshness metrics use
  `first_seen`.

---

## Decision & research log

Newest last. Record what was found and why a call was made, not just what was done.

### 2026-09-11 — Phase 1 kickoff

**Repos created.** `job-market-panel` (public), `job-search-assets` (private). Spec moved
into the private repo. Toolchain: uv 0.12.13, Python pinned 3.12.14, pydantic 2.13.5,
httpx 0.28.1, tenacity 9.1.4, ruff + pytest. Versions resolved live by `uv add`, not typed
from memory.

**Research: the Himalayas OpenAPI spec contradicts its own live API.** Fetched
`https://himalayas.app/docs/openapi.json` and probed the live endpoints. Four discrepancies,
each of which would corrupt data silently:

| Field | OpenAPI declares | Live API actually returns |
|---|---|---|
| `locationRestrictions` | `array[{alpha2,name,slug}]` | `array[str]` — `["United States"]` |
| `timezoneRestrictions` | `array[str]` — `"UTC-5"` | `array[int]` — `[-10,-9,…,14]` |
| `pubDate` / `expiryDate` | Unix **milliseconds** | Unix **seconds** — naive ms parse gives year ~58000 |
| `guid` | short slug | full `himalayas.app` URL, identical to `applicationLink` |

Adapters must trust the live shapes and tolerate both. Fixtures pin this.

Further findings:
- Field names are `categories` / `timezoneRestrictions` (**plural**).
- `seniority` is an **array** of labels; a job may carry several.
- `salaryPeriod` is `hourly|weekly|fortnightly|monthly|annual` and salaries are **in that
  period, not annualized** (API changelog 2026-06-08).
- Listings expire at **60 days**, not 30 (`expiryDate - pubDate` = 5,184,000s exactly).
  The "never compute survival from Himalayas" rule stands; only the stated reason changes.
- `applicationLink` points at a himalayas.app page, **not** the employer's ATS. Cross-source
  dedupe cannot match on URL.
- `parentCategories` is empty on ~58% of rows, and `categories` are per-job generated slugs
  (`Predictive-Modeler`, `Risk-Analytics`), not a controlled vocabulary.
  **→ Classification must be title-driven. The taxonomy cannot lean on Himalayas categories.**
- Responses carry `comments` (a changelog string) and `updatedAt` (feed refresh time). Both
  are free silent-change detectors — logged per run, surfaced by `healthcheck.py`.
- Himalayas runs an MCP server at `himalayas.app/mcp`. Not usable from Actions runners;
  interactive-only.

**Research: two Himalayas pagination traps.**
1. `/jobs/api/search` **ignores `limit`** and returns **17–20 items per page** while
   reporting `limit: 20`. The idiomatic `while len(page) == limit` loop terminates on page 1.
   → Terminate on `n == 0`, a `pubDate` cutoff, or a fixed page budget. **Never on page size.**
2. `totalCount` on `/search` is a **relevance-candidate count, not a result count**.
   `q=analytics` → `totalCount: 5000` with **zero jobs**. It cannot size pagination.
   (On the `/jobs/api` browse endpoint it is real: 101,855 active listings — confirming a
   full-corpus crawl is infeasible.)

Measured `q=data scientist&sort=recent` pages 1–12: **229 distinct** guids, page size 17–20,
recency decaying ~2–3 days per page (p1 = one day only; p12 reached 2026-08-30). So ~5 pages
per query covers a daily delta with enough overlap that a missed run self-heals.

Search relevance is loose — `q=data scientist` returned actuaries, toxicologists,
epidemiologists and backend engineers. **Over-fetch broadly, classify locally**, expect to
discard most rows.

**Research: Greenhouse's remote signal is weaker than assumed.** Probed 6 boards
(stripe 631 jobs, databricks 886, gitlab 226, airbnb 165, figma 153, vercel 86).
- The spec expects a **"Workplace Type" metadata field**. It exists on **1 of 6** boards
  (Airbnb: Remote 139 / Hybrid 24 / Onsite 2). Others expose only company-custom fields
  (`Quota Coverage Type`, `Career Page Posting Category`, `Career Site Categories`).
  **→ Remote inference is location-string matching plus a per-company metadata override
  where one happens to exist.** This raises the stakes on `remote_source` and on measuring
  the error rate (Phase 1 deliverable, see below).
- Location strings are dirty: `"United States"` and `"United States "` (trailing space) are
  *separate* values; also `"Remote - USA"`. Titles too (`"Senior Data Scientist "`).
  Normalize before any grouping.
- Naive location matching yields wildly different remote shares: gitlab 86%, stripe 17%,
  airbnb 12%, figma **0% of 153**. Figma is the failure mode to watch — a company that never
  writes "remote" in the location reads as a fully onsite org.
- **`first_published` exists** and the spec did not know about it. Better `posted_at` than
  `updated_at`, and on day one it is the only way to know a req's true age.
- `departments[]` carries a real department name — useful as a DS-share denominator and as
  a cross-check on title classification.
- Bad/empty token → clean **HTTP 404**. Per-company isolation is trivial.
- DS-family titles are 1–17% of a board, confirming the two-tier storage split.

**Contract deltas locked in Phase 1** (spec Section 6 allows additive changes; renaming is
forbidden). Each is additive or resolves a contradiction in the spec:

1. **`SourceRun.status` = `ok | partial | empty | error`.** Spec Section 6.3 declares only
   `ok|error|empty`, but the Phase 1 correctness rules and Section 8 both *require* a
   `partial` status for a truncated run. The spec contradicted itself; this resolves it.
   **Only `ok` permits diffing.**
2. **`SourceRun` gains optional `company_slug`.** Correctness rule 1 mandates per-company
   ATS failure isolation, which is unenforceable with one row per source per run.
3. **`salary_period` widens to `year|month|week|fortnight|hour`**, with `salary_period_raw`
   holding the source's own token. Himalayas emits `fortnightly`/`weekly` and does not
   annualize. **Annualization happens in the analysis layer, never at collection.**
4. **`posted_at` from Greenhouse `first_published`**, falling back to `updated_at`.
5. **`SourceRun` gains optional `feed_updated_at` and `api_notice`** (Himalayas `updatedAt`
   and `comments`) for silent-change detection.

**Judgement calls made** (spec Phase 1 "Left to your judgement"):
- **Himalayas query set:** fixed, versioned, ~8–10 `q` terms × `sort=recent` × 5 pages, no
  `country`/`seniority` filter — filter locally. `query_set_version` stored on every row so
  a change to the query set is visible in the data rather than silently shifting the series.
- **429 handling:** docs say "wait 60 seconds". Serial requests ~0.7s apart; on 429 sleep 60s,
  retry twice, then mark `partial` and **stop**. Real ceiling to be measured in week one.
- **Seniority:** store **both** — `seniority_source` (Himalayas' own labels, null for ATS)
  and `seniority` (uniform title-derived, used for all headline series). Consistency across
  sources beats accuracy on either one alone.
- **`job_id`:** `f"{source}:{source_job_id}"`. Greenhouse → `id`. Himalayas → `guid`.
  **Known limitation:** the Himalayas guid is a title-slug URL, so a *retitled* posting
  produces a false disappear+appear pair. Mitigation is Phase 2's `dedupe_key`.
- **Cross-source dedupe:** deferred to Phase 2 as an analysis-layer `dedupe_key`, preserving
  raw immutability. Phase 1 keeps both observations.
- **Description tier:** full records captured for `product_ds, ds_manager, ml_eng,
  analytics_eng, data_eng, analyst` — all data-family roles, not just target roles. Gabriel's
  call. Rationale: this is the one irreversible choice here (an uncaptured description cannot
  be recovered), and the title-mix metric explicitly tracks DS work being absorbed into ML
  Engineer, which is unmeasurable without ML Eng descriptions. Written on `first_seen` or on
  description-hash change only, never re-stored daily.
- **Cron:** `17 7 * * *` UTC (03:17 ET) — off the hour to dodge Actions' top-of-hour
  congestion, after the Himalayas daily refresh.
- **Remote-inference validation pulled into Phase 1** (the spec listed it only as a gotcha)
  because the Workplace Type field turned out to be near-absent. ~100 hand-labelled postings,
  measured error rate written to the README.

**Open, to be answered by data in week one** (record answers in README.md):
- Himalayas' practical 429 ceiling.
- Actual DS-family postings/day after classification. Measured ~19 *raw* hits/day for one
  query with loose relevance, which is near the spec's "under ~20/day → the adapter may not
  justify itself" threshold. Flagged to Gabriel; he chose to keep it, since its real job is
  watchlist seeding, which Greenhouse structurally cannot do.
- Day-1 vs steady-state event-log gzip footprint.

### 2026-09-11 — contracts and classifier landed

`src/normalize.py`, `src/models.py`, `src/classify.py`, `config/taxonomy.yaml`.

**`RemoteFinding` added to the contracts.** A small frozen model pairing `is_remote` with
`remote_source`, so the verdict and its provenance cannot be separated by accident.
Correctness rule 5 is unenforceable if a function can return a bare bool.

**`CollectionScope` added to the contracts.** Records what a run actually *looked at*
(source, and company for ATS boards). A posting can only be a disappearance candidate if
the run genuinely looked where it would have been. Without this the differ cannot tell
"the company removed the req" from "we skipped that board today" — the most damaging
silent failure available.

**Remote inference is deliberately tri-state.** An ambiguous location yields `None`, never
`False`. A bare `"United States"` says nothing about remote status, and guessing `False`
would quietly inflate the onsite share and corrupt the remote denominator. Verified against
real strings: `"Remote - USA"` → True, `"Remote (Hybrid - 3 days in office)"` → **False**
(the negative veto beats the positive match, which is the point), `"United States"` → None.

**Bug found and fixed in the taxonomy — it had silently broken the panel's headline signal.**
The `ds_manager` rules matched on `\b(data scien|…)\b`. That trailing `\b` can *never* match
"data science": after "scien" comes "c", a word character, so there is no boundary there.
Result: **"Data Science Manager" classified as `product_ds`** — the DS-manager count, which
is the single highest-value output of the whole panel, would have read **zero forever**, and
looked entirely plausible while doing so. Fixed to `data scien\w*`. Two lesser fixes
alongside: the actuarial veto was discarding "Actuarial Data Scientist" (a data scientist),
and "Senior ML Specialist" fell through to `other`. Taxonomy now `2026-09-11.2`.

**The lesson worth keeping:** this class of bug produces a *plausible* number, not an error.
It was caught only by asserting expected classifications against real titles pulled from the
live feeds. Every taxonomy change needs that same check — see `tests/test_classify.py`.

**Design note — managers of adjacent families.** `ds_manager` is scoped to data
science/analytics/insights leadership. An ML or data-engineering manager is classified into
its own IC family with `seniority=manager` instead (e.g. "Director of Machine Learning
Engineering" → `ml_eng` + `director`). This keeps the DS-manager headline a clean read on DS
org growth, while "manager-level reqs in family X" stays answerable by querying seniority.
