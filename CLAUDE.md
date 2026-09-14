# job-market-panel — working notes for Claude Code

**Read this file first. It is how context survives between sessions.**

> **This file is the plan of record.** Everything decided, measured or ruled out lives
> here or in the private repo's `CLAUDE.md` — not in a chat transcript, not in a scratch
> plan file, not in anyone's head. It is committed and pushed, so it survives compaction,
> a new session, or a new machine.
>
> **If a decision is made in conversation, it is not real until it is written here.**
> Append to the dated log at the bottom, then update "Current status" near the top so the
> next session reads the *conclusion* without having to replay the whole log.

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
 Himalayas  (discovery, remote-only)  ─┐
 Greenhouse ─┐                         │
 Ashby      ─┼─ census, 382 boards ────┼──►  collect.py
 Lever      ─┘                         │      │  normalize → classify → parse pay/location
                                       │      │  → diff vs. state replayed from the log
 The Muse ──► tools/discover.py ───────┘      │
 HN threads ► tools/harvest_tokens.py         ▼
   (both grow config/watchlist.yaml)     data/events/*.jsonl.gz     ← append-only truth
                                         data/postings/*.jsonl.gz   ← full records
                                         data/runs/*.jsonl          ← health; gates diffing
                                         data/latest/new_postings.jsonl ← Phase 5 reads this
                                              │
                     build.py ────────────────┘   reclassifies + reparses from raw
                        │
                        ▼
                  panel.duckdb  ──► report.py ──► reports/latest.md
                  (derived, gitignored,          (committed; the daily glance)
                   rebuilt in ~1s)
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

**Phases 1, 2 and 2.5 are built and collecting** (last updated 2026-09-13). Four sources,
a census over **382 verified boards**, discovery that runs itself, and an analytics layer.
**229 tests passing**, workflow green in CI.

| | |
|---|---|
| Boards collected daily | **382** (148 Greenhouse, 189 Ashby, 45 Lever) |
| Watchlist entries | 591 (382 verified, 18 quarantined, 191 no readable board) |
| Open postings tracked | ~26,000, collected in **under a minute** |
| In-band target roles open | ~400 |
| Pay band known | 36.5% overall (57% on days collected since the parser shipped) |
| Country known | 66.5% |
| Remote, strong evidence | 58.6% (86% including `location_implied`) |
| Days of history | **2** |

**All Phase 1 and Phase 2 acceptance criteria are met** except one that can only pass with
time: seven consecutive unattended daily commits. `job_id` stability was confirmed — at the
61 companies tracked on two consecutive days, appearances went 9,261 → 7.

> [!NOTE]
> **Day one predates several features.** Salary parsing, location parsing and the
> `product_analyst` / `senior_staff` / `senior_manager` levels all shipped after the first
> collection. `build.py` recomputes everything whose inputs survive in the log — role
> family, seniority, country, region — so those are continuous. **Parsed pay is not
> recoverable for day one**, because descriptions are only retained for tracked families.
> Expect a visible step up in pay coverage at 2026-09-13; it is an artifact, not a market
> move.

### What to do next

**Phase 3 is gated on calendar, not code.** Survival and time-to-close with censoring,
per-company growth trends, the new-posting-rate baseline, title-mix over time, and Adzuna
macro context all need weeks of history to say anything true. `job_spans` already has the
shape survival needs. Building them now would produce charts of noise.

Three conventions Phase 2.5 established that Phase 3 must preserve — they are already
honoured in `reports/latest.md`:

1. **Never pool `posting_disclosed` with `description_parsed` pay.** Both are
   employer-published, but one arrived in a vendor field and the other through a regex.
2. **Report remote share both ways** — with and without `location_implied`, which covers a
   large share of determinations at roughly 95% accuracy.
3. **Report rates per vendor.** Greenhouse boards carry no salary field, so a change in
   vendor mix looks exactly like a change in the market.

And one Phase 2.5 added:

4. **Compute pay in-band only.** Director, Senior Manager, Principal and Senior Staff reqs
   sit well above the band being searched and skew the distribution upward. The band is
   `seniority.target_band` in `config/taxonomy.yaml`.

**Worth doing whenever, roughly in value order:**

- **A Workday adapter.** The last real coverage hole — Etsy, Canva, Deel and others are
  invisible. Reassessed as viable (see the log): Etsy's endpoint carries `remoteType`, so
  Workday supports 5 of the 6 metrics; only compensation is missing. Needs a
  hand-maintained tenant list, so it works best when Gabriel names the companies.
- **Re-run `tools/harvest_tokens.py` monthly** as new "Who is hiring" threads appear.
  Deliberately not in the daily workflow.
- **Extract the healthcheck's ID-stability logic** out of `main()` if it is touched again;
  it is the project's main defence against silent failure and is currently untested.

**Waiting on Gabriel, neither urgent:**

- A free **Adzuna API key** — the only feed that sees Workday companies without per-company
  setup.
- The **December repo-visibility decision**, which now has two reasons behind it: Actions
  minutes do not justify the public/private split at this scale, and the public repo
  republishes Himalayas description text.
- **Pruning the watchlist** whenever he likes. 382 boards, largely seeded automatically.

**Phase 4 (private repo) needs him directly** — the `bank.yaml` evidence fields are his to
write, and the three resume corrections must happen before the bank is built from it.

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
- **`git pull` before doing anything local.** Both repos commit to themselves daily — this
  one from the collector, the private one from the schedule canary — so a local clone goes
  stale on its own, without anybody touching it. This is not housekeeping: pushing from a
  stale clone while a run was in flight is exactly what surfaced the detached-HEAD bug in
  the workflow's commit step (2026-09-13 below).
- **Never re-run the collector mid-day without reason.** Re-runs are idempotent for
  *events* — state replays from days before today — but not for the pending-miss queue. A
  re-run forgets that a posting was already missed once, delaying its closure by a day.
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

### 2026-09-11 — source config and watchlist (61 boards)

**Himalayas query terms must be measured, never assumed.** Each candidate term was run
against the live search endpoint and scored by how many *classified, tracked* rows it
yielded. Several obvious terms are effectively dead: `q=analytics` returns **0 jobs**
(while reporting `totalCount: 5000`), `q=data analyst` returns **0**, and `q=data engineer`
returns **1**. Longer phrases work fine. A plausible-looking term can contribute nothing at
all, so `config/sources.yaml` carries only terms verified to return results. Final set: 12
terms, `query_set_version: 2026-09-11.1`.

**Pagination budget.** `max_pages_per_query: 5` with `lookback_days: 4`: paging stops once
a whole page predates the lookback window. Since `sort=recent` decays ~2–3 days per page,
steady state is ~2 pages/query (~24 requests/day) and the 5-page budget only gets spent
catching up after a missed run. Worst case 60 requests/day.

**`tools/resolve_ats.py` verifies board identity, and that caught real errors.** Greenhouse
echoes `company_name` on every posting, which is the only available check that a *guessed*
token belongs to the company intended. Comparing it against the requested name flagged five
mismatches, three of which were genuinely the wrong board:

| Requested | Token guessed | Board actually belongs to | Outcome |
|---|---|---|---|
| Carbon Health | `carbon` | Carbon, Inc. (3D printing) | not on Greenhouse — parked |
| Wise | `wise` | an unrelated field-sales board | not on Greenhouse — parked |
| Remote.com | `remote` | General Assembly | corrected to `remotecom` (175 reqs) |
| Chime | `chime` | "Chime Financial, Inc" | correct — legal name |
| Intercom | `intercom` | "Fin" | correct — company rebrand |

Without that check, three companies' hiring would have been silently attributed to the
wrong employer **for the life of the panel**, and the org-growth signal for each would have
been fiction. Any future watchlist addition must clear the same check.

**Token-level dedupe added.** "Amplitude" and "Amplitude Analytics" both resolved to token
`amplitude`, which would have fetched that board twice and doubled its req counts. The
watchlist writer now guarantees one entry per `(ats, token)`.

**Result: 61 Greenhouse boards resolved, 60 parked as `ats: unknown`** for Phase 2's Lever /
Ashby / Workable adapters. Acceptance criterion (≥40) met. The parked entries are not
failures and must not be deleted — many are simply on another vendor.

### 2026-09-11 — adapters and fixtures

`src/sources/{base,himalayas,greenhouse}.py`, real captured fixtures, 71 tests passing.

**`parse_epoch` accepts seconds or milliseconds.** Not defensive habit: the Himalayas spec
documents millisecond timestamps and the live API sends seconds. Parsing as documented
gives dates around the year 58000 — a perfectly valid `datetime` that would sail through
validation and quietly poison every freshness metric.

**Himalayas `is_remote` is `SOURCE_FIELD`, not an inference.** The board lists remote roles
exclusively, so remote status is a property of the source. This is also exactly why it
cannot supply a remote *share*: there is no non-remote denominator anywhere in it.

**Greenhouse uses the watchlist's name and slug, never the board's echoed `company_name`.**
The token was verified against the watchlist name at resolution time, and boards echo legal
names or post-rebrand names ("intercom" reports as "Fin"). Trusting the echo would split
one company's series in two mid-panel.

**Greenhouse salary is deliberately not parsed.** No structured salary field exists; bands
appear inside description prose where local law requires them. A bad regex would pollute
the compensation series permanently, and the description is retained, so this can be
parsed later from raw with no loss. Phase 3 decides.

**An empty Greenhouse board is `EMPTY`, not `OK`.** A genuine hiring freeze is
indistinguishable from a misconfigured token. Refusing to diff costs at most one day of
disappearance events; guessing wrong corrupts the survival series permanently.

**Measured: remote-status provenance across 1,316 live postings from 7 boards.**

| `remote_source` | share | note |
|---|---|---|
| `unknown` | 59% | ambiguous location, recorded honestly as NULL |
| `location_string` | 28% | inferred by pattern — carries real error |
| `metadata_field` | 13% | all from a single board (Airbnb) |

That 59% is the headline caveat for any remote-share metric built on the ATS panel, and it
is the reason the remote denominator must be reported alongside the numerator rather than
assumed. Item 22 (~100 hand-labelled postings) decides whether a "named city implies
onsite" rule is worth adding; it is deliberately NOT assumed now, because reclassification
from raw is free by design and a wrong guess baked in today would not be.

**Live per-company isolation confirmed:** a deliberately broken token among 7 real boards
produced `status=error`, zero rows, and exclusion from diffing, while the other 7 boards
diffed normally.

### 2026-09-12 (UTC) — collector live, first data collected

`src/storage.py`, `src/collect.py`, `tools/healthcheck.py`. **First real collection:
9,819 postings across 62 scopes in 72 seconds, 62/62 diffable.**

| | count |
|---|---|
| Greenhouse (census, 61 boards) | 9,234 |
| Himalayas (discovery, 44 requests) | 585 |
| `product_ds` | 193 |
| **`ds_manager` — the growth signal** | **58** |
| `ml_eng` / `analyst` / `data_eng` / `analytics_eng` | 482 / 311 / 59 / 14 |
| full records stored (tracked families) | 1,117 |
| with a disclosed salary band | 263 (all Himalayas; Greenhouse has no salary field) |

**Deliberate departure from correctness rule 4: `data/state/pending_misses.json`.** The
two-miss rule must know whether a job was *also* absent on the previous run, and the event
log records only transitions — never "seen today" — so that fact is genuinely not
reconstructible from it. The rule's stated concern is git bloat from rewriting full state
daily; this file holds only in-flight candidates (a few hundred rows against ~10k), so it
is a few KB. Storing the full daily observed set instead would cost roughly 55 MB/year and
blow the storage budget alone. Losing the file costs exactly one extra day of latency
before a closure is recorded — the kind of failure spec principle 2 says to accept.

**A failed scope must not advance the miss counter.** Subtle and nearly invisible: if it
did, a two-day source outage would mature every open req into a confirmed disappearance
the moment the source came back. Covered by a test that simulates five consecutive
outage days and asserts the pending set stays empty.

**Idempotency is structural, not defensive.** State is replayed from days *before* today,
so a same-day re-run recomputes an identical event set instead of appending. Verified live:
second run produced zero duplicate job_ids and zero disappearances. The files did change,
for two legitimate reasons worth knowing: a company genuinely removed a req between the two
runs (9,234 → 9,233), and `fetched_at` records *when we looked*, which is a fact about the
observation and must not be frozen to make a checksum stable.

**Consequence of that design, accepted:** a posting that appears and vanishes between two
same-day runs leaves no trace at all, because the second run overwrites the day's file and
never had it in prior state. That is the correct reading — we did not observe it at the end
of the day — and it is the price of same-day idempotency.

**Gzip output is content-deterministic** (`mtime=0`, empty filename in the header).
Otherwise every re-run would rewrite the header and look like a change to git.

**`healthcheck.py` uses the UTC date, matching the collector.** Using the local date made
it report phantom gaps for part of every day — it claimed "no collection runs found" while
the day's file sat on disk.

**Descriptions are captured on first appearance only**, not re-stored when they change.
Phase 4 scores a posting near its first sighting, and re-storing ~7 KB of description per
posting per day would multiply the storage budget for almost no information. Revisit in
Phase 3, where DuckDB makes a change-detection lookup cheap.

**Discovery is already working:** Himalayas surfaced DS-manager reqs at companies absent
from the watchlist (Liberty Mutual, Humana, Westinghouse). Those are Phase 2 watchlist
candidates — exactly the job this source exists to do.

### 2026-09-12 (UTC) — remote inference measured, then improved

**Item 22 done: ~100 postings hand-labelled, error rate measured.** Ground truth assigned
by reading each posting's location field together with the work-location sentences pulled
from its description.

| Evidence | n | accuracy |
|---|---|---|
| `metadata_field` ("Workplace Type") | 15 | **100%** |
| `location_string`, where decisive | 40 | **97.5%** |
| `unknown` (no prediction made) | 45 | — |

**The finding that forced a change: the `unknown` bucket was not unknowable.** Of its 45
postings, **41 (91%) were actually onsite or hybrid**, 2 were genuinely remote, and only 2
were truly indeterminate. Nearly all of them stated their arrangement outright in the
description — `#LI-Hybrid`, "four days a week in the office", "on-site at our HQ 5 days a
week". 59% of the ATS panel was landing in that bucket.

**Why this could not wait for Phase 3.** Descriptions are stored only for tracked families,
so for ~89% of postings the text is discarded at the end of the run. Deferring the
inference would not have deferred it — it would have made it **permanently impossible**.
That is the opposite of the reprocessability principle, which holds only where the raw
input survives. So the inference now runs at collection time and the *derived verdict*
persists even where the description does not. There is a test asserting exactly that.

**Result after adding the description pass** (taxonomy `2026-09-12.1`):

| | before | after |
|---|---|---|
| sample accuracy | 97.5% / 100% | **98.8%** overall |
| sample coverage (a verdict at all) | 55/100 | **80/100** |
| full panel `remote_source: unknown` | 59% | **35%** |
| full panel carrying a verdict | 41% | **65%** |

Precedence is metadata → location → description → NULL. Location outranks description
deliberately: it is the more specific field. That ordering causes the sample's single
error (a board reading "Remote, USA" whose description carried `#LI-Onsite`) and is worth
it for the cases it decides correctly.

**Two false-positive traps, both real, both now tested:** bare "remote" appears in benefits
boilerplate ("Remote work, medical insurance, flexible time off…") on strictly onsite
postings, and "remote sensing" is an actual data-science domain. Likewise "hybrid search"
and "hybrid model" are ordinary DS vocabulary. Every pattern requires a qualifier binding
the word to a work arrangement, and only the first 6,000 characters are examined — benefits
and EEO boilerplate cluster at the end.

**A test caught a pattern the live data did not.** `\bin our offices? at least\b` never
matched the real phrasing, "in **one of** our offices at least 25% of the time". It scored
fine in the live sample only because those postings had a metadata field that won first.
Fixed.

### 2026-09-12 — Phase 2 vendor reconnaissance (research only, nothing built)

Probed every remaining vendor against live endpoints to size the porting work before
opening Phase 2. **No adapters written; Phase 1 is still in its observation week.**

**Coverage available today**, probing the 60 parked companies plus careers-page fingerprints:

| Vendor | Companies reachable | Open reqs | Verdict |
|---|---|---|---|
| **Ashby** | 24 | ~2,600 | **Build first — 92% of the win** |
| Lever | 5 | 214 | Build second |
| Workable | **0** | 0 | **Skip entirely** |
| Workday | ~up to 30 | many | Defer; document the bias |

This **reverses the spec's implied ordering** (Greenhouse → Lever → Ashby → Workable).
Ashby is the single highest-value adapter by a wide margin and brings the best targets:
Plaid, Ramp, Notion, Snowflake, Airwallex, Modern Treasury, Column, Persona, Sardine.

**Trap: Ashby's `isRemote` is NOT a remote flag — do not use it.** Across 422 live postings
from 4 boards:

| `isRemote` | `workplaceType` | n |
|---|---|---|
| `true` | **Hybrid** | **293** |
| `true` | Remote | 65 |
| `false` | OnSite | 20 |
| `null` | `null` | 44 |

69% claim `isRemote: true` while saying `Hybrid`, including a job located at "San Francisco
HQ". It evidently means "some remote permitted", not "this is a remote role". **Use
`workplaceType`; ignore `isRemote`.** `RemoteSource.ATS_FLAG` exists in the contract
precisely for this field on the spec's guidance — leave it unused, or the remote share for
a quarter of the panel becomes fiction.

**Bonus: Ashby has structured compensation on 58% of postings** — Greenhouse has none.
`{compensationType: "Salary", interval: "1 YEAR", currencyCode: "USD", minValue, maxValue}`.
This lets Phase 3's compensation metric draw disclosed bands from the *census*, not only
from Himalayas. **Take only the `Salary` component** — tiers also carry `EquityCashValue`,
`Commission` and `Bonus`, and pooling those into base pay would silently inflate the series.

**Lever:** returns a **bare array** (the spec's warning is accurate). Title is `text`,
location is `categories.location`, `workplaceType` is lowercase (already matches the
taxonomy map), `createdAt` is epoch **milliseconds** — already handled by `parse_epoch`.
No structured salary, only prose (`"Estimated annual salary range: $150,000 - $189,000"`);
do not parse it, for the same reason Greenhouse salary is not parsed.

**Workable: skip.** Zero of 60 target companies had a live board with jobs. It is the
undocumented widget endpoint. It 404s on a clearly bogus token, but short tokens collide
with unrelated accounts (`gong` returns an account named "GONG!" with zero jobs) and there
is no company-identity field to verify against when a board is empty — so the verification
trick that saved the Greenhouse watchlist does not work here.

**Workday: technically reachable, but degraded and high-maintenance.** The unofficial CXS
endpoint works — `POST {tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`
returned 28 reqs for Turo. But the payload carries only
`title, externalPath, locationsText, postedOn, bulletFields`:

- **no description** — so remote inference loses the description pass and falls back to
  location text alone, which is the weakest signal we measured
- **no department, no salary**
- `postedOn` is a *relative human string* ("Posted Yesterday", "Posted 30+ Days Ago").
  Less damaging than it looks, since all survival metrics use `first_seen` by design —
  but `posted_at` would be unusable
- each company needs **three config values discovered by hand** (tenant, `wd` number, site
  name) from a mostly JS-rendered careers page

Keeping it out of scope remains right. Document the bias instead.

**Watchlist hygiene found along the way — three entries are no longer independent
companies:** Census now redirects to Fivetran (already tracked), Loom to Atlassian, and
Segment to Twilio. Remove or merge them in Phase 2.

**Also: token-guessing has a real miss rate.** Careers-page fingerprinting found Front on
Ashby under `frontcareers`, which no name-derived guess would produce. Worth running the
fingerprint pass as a second stage in `resolve_ats.py` for anything that fails to resolve.

### 2026-09-12 — Phase 2 REPLANNED: connectors first, watchlist becomes a byproduct

Gabriel corrected the framing, and it changes Phase 2's shape. Recorded here because the
spec's Section 2 does not say this.

**The watchlist was built wrong — as a target list.** It was assembled from an inferred
profile (200–1,000 headcount, remote-friendly, fintech/marketplace). That was my instinct,
not a requirement. Gabriel wants **the widest net that is still relevant**, and intends to
build his own shortlist later, from the data. So the watchlist must become **the universe**,
not a filter, and he curates it by *deleting*, never by having to add.

**The mechanical constraint that makes this non-obvious.** Verified against live endpoints:

| Enumeration attempt | Result |
|---|---|
| `boards-api.greenhouse.io/v1/boards` | **404** |
| `api.ashbyhq.com/posting-api/job-board` | **401** |
| `api.lever.co/v0/postings` | **404** |

**There is no "get everything" endpoint on any ATS.** A board is readable only if you
already know its token. The watchlist is therefore a *technical* requirement, not a
preference — which is exactly why the spec pairs a discovery source with the census. Every
company we cannot name is invisible, permanently, for that day.

**What is NOT a constraint — nothing is being filtered out at collection.** Confirmed
against the live census (9,245 postings, 61 boards): all role families, all seniorities
(820 staff, 190 principal, 430 director), and all remote statuses are already captured.
`capture_all_families: true`. The panel is already as broad as Gabriel wants it. **The only
thing narrowing it is which companies we can see.**

**Discovery is therefore the whole game, and today it is remote-biased.** Himalayas is a
*remote-only* board by construction, so every company discovered through it is a company
that posts remote roles. That is a real selection bias against Gabriel's stated intent.
Measured for scale: Himalayas surfaced **360 distinct companies in one day**, 67 of them
posting DS-family roles, of which only 5 were on the watchlist.

#### Revised Phase 2 — build in this order

1. **Ashby adapter.** 24 companies, ~2,600 reqs, and structured compensation on 58% of
   postings. 92% of the available coverage win. **Do not use `isRemote`** — see the
   reconnaissance note above.
2. **Lever adapter.** 5 companies, 214 reqs. Bare array; title is `text`.
3. **Auto-resolution pipeline — the actual unlock.** Run daily: take every company name
   seen in any discovery feed that is not yet on the watchlist, resolve it against all
   three vendors, and auto-append whatever resolves with `source: auto_discovered`. This
   inverts the bottleneck: the watchlist grows by itself and Gabriel prunes rather than
   researches. Add careers-page fingerprinting as a second stage — token-guessing misses
   real boards (Front is on Ashby as `frontcareers`, which no name-derived guess produces).
4. **A non-remote discovery feed**, to correct the Himalayas bias. Candidates measured:
   - **Adzuna `top_companies`** — already planned for Phase 3, free tier ~1,000 calls/mo,
     US, not remote-restricted, and the endpoint literally answers "which employers
     advertise most for this query". Needs a free API key from Gabriel. **Best fit.**
   - **The Muse** — keyless, 412k jobs indexed, a "Data and Analytics" category, US
     filter, not remote-restricted. But it is an employer-branding site companies *pay* to
     appear on, so it skews to large enterprises with branding budgets (TELUS Digital,
     Kyndryl, GE Vernova dominated the sample). Same commercially-determined-coverage
     caveat as Himalayas, different direction. Useful as a second feed, never a denominator.
5. **Watchlist hygiene:** drop Census (now Fivetran), Loom (Atlassian), Segment (Twilio).

#### Taxonomy changes to follow (Gabriel's stated preferences, superseding spec Section 2)

Spec Section 2 is a **placeholder** and should not be treated as binding. Actual intent:

- **Staff is in scope**, not a dealbreaker. Spec Section 2.4 lists "Titled Staff/Principal
  IC" as a score-zero dealbreaker — that is wrong and must not be carried into Phase 4.
- **Strong product analytics roles are in scope** where the company is growing and comp is
  there. The current `analyst` family lumps a strong product-analytics req together with a
  junior reporting analyst. **Worth splitting** so the good ones are findable.
- **Manager roles: keep measuring, not a personal target.** Unchanged.
- **Do not restrict to remote.** Already true at collection; make sure it stays true, and
  do not let the 35%-unknown remote field become an implicit filter downstream.

### 2026-09-12 — operational findings not previously written down

Recorded because they were established in conversation and would otherwise be lost.

**Actions cron ran 4h19m late on its first scheduled run.** Scheduled `17 7 * * *` UTC
(03:17 ET), actually started 11:36 UTC (07:36 ET). Succeeded, committed normally. This is
the spec Section 8 warning ("Actions cron is not punctual") measured rather than assumed.

Harmless for the collector — nothing depends on the minute and re-runs are idempotent.
**But it is a real constraint on Phase 5.** The brief is meant to arrive before Gabriel
wakes, and the spec imagines "runs at 06:30, read at 08:00". A delay of this size breaks
that. **When building the brief, set its cron several hours earlier than the time the brief
is actually wanted**, and treat any "runs at X" reasoning in the spec as a floor, not a
schedule.

**Spec Section 9 Q4 is provisionally answered: `schedule` DOES fire on the private repo.**
The canary fired on 2026-09-12 at 11:39 UTC (see `schedule_canary.log` in the private repo).
One data point; it keeps accumulating until December. If it holds, no GitHub Pro is needed.

**Actions minutes are NOT a reason to keep the panel public.** Measured from real runs:
collector 79s/run → 2 billed min/day. Projected total if *everything* were private —
collector 60 + weekly report 12 + weekday brief 66 + canary 30 = **168 min/month against
the 2,000 free private allowance, about 8%**. Even at Phase 2's ~250 companies it stays
near 12%. Spec Section 3.2's first rationale for the public/private split does not bind at
this scale.

**What DOES still argue for the current split:** going private breaks the cross-repo
interface. The Phase 5 brief fetches `data/latest/new_postings.jsonl` from
`raw.githubusercontent.com` with no authentication (spec 3.2 chose it precisely to avoid
tokens). Making the panel private 404s that URL, so it is a design change — add a
cross-repo PAT, or collapse into one private repo — not a visibility toggle.

**A concern the spec did not anticipate: the public repo republishes source content.**
Spec Section 5/Phase 1 says Himalayas' terms ask for attribution if you republish and ask
their listings not be submitted to third-party job sites, then concludes *"neither applies
to a private analysis panel, but do not build anything public-facing on this data without
honouring both."* **It was built public.** ~276 Himalayas postings with full ~6,500-char
descriptions are committed publicly each day. Attribution is in the README and a git repo
is not a job board, so it is defensible — but the spec's reasoning assumed private.
Two cheap fixes when this is revisited: stop committing Himalayas `description_text`, or
go private (which also resolves it). **Decision deferred to December**, together with the
visibility decision, at Gabriel's direction.

**Repo size is the real GitHub-side risk, not blocking.** Request volume is ~105/day
across all sources — negligible, and a block degrades safely (a 429 or 403 yields
`status != ok`, hence zero disappearance events). But the repo was 8.5 MB with 15 MB of git
history after ONE day, inflated by the day-1 bulk load; `data/latest/new_postings.jsonl`
alone was 8.3 MB and is rewritten every run. GitHub gets uncomfortable around 5 GB.
**Measure the steady-state daily growth once a few days exist**, and if the trajectory is
bad, stop committing the uncompressed `new_postings.jsonl` or prune its history.

**On ToS generally:** three of the four ATS endpoints are documented public APIs (Himalayas
publishes an OpenAPI spec). No HTML scraping, no auth bypass, no robots.txt issue. Workable's
is the undocumented widget endpoint and is being skipped anyway. LinkedIn is nowhere in the
system and must stay that way.

### 2026-09-13 — Phase 2 shipped: three ATS vendors, 135 boards, automated discovery

Built in one session at Gabriel's direction, with Phase 1's observation week running in
parallel (nothing here touches the differ or the event-log format).

**`PerCompanyBoardSource` (`src/sources/board.py`) — the refactor that came first.**
Greenhouse held ~60 lines of failure-isolation logic that is entirely vendor-independent.
Three hand-maintained copies of correctness rule 1 is precisely where a silent divergence
appears, so it now lives once: a subclass supplies a URL, a jobs extractor and a field
mapping, and **cannot get the isolation rules wrong because it does not implement them**.
Greenhouse was refactored onto it with its 23 tests passing unchanged.

One distinction the base preserves: `_extract_jobs` returning `None` means *malformed*
(→ `ERROR`); returning `[]` means *genuinely empty* (→ `EMPTY`). Neither permits diffing,
but conflating them would lose the difference in the health log.

**Ashby — the large win, and the phase's worst trap.**
30 boards, ~2,900 reqs. **64% of its postings carry a structured salary band**, where
Greenhouse exposes none at all. Across the whole ATS census this took salary disclosure
from **0% to 39%**.

Two rules, both tested:
- **`isRemote` is ignored.** 293 of 422 sampled postings report `isRemote: true` while
  `workplaceType` says `Hybrid`, one located at "San Francisco HQ". It means "some remote
  permitted", not "this is a remote role". `RemoteSource.ATS_FLAG` stays unused.
- **Only the `Salary` component of a compensation tier is read.** Tiers also carry
  `EquityCashValue`, `Commission` and `Bonus`; pooling any of them into base pay would
  silently inflate the entire compensation series.

`secondaryLocations` ("Remote (US)") feeds remote inference. Overall **remote-known rose
from 65% to 80%**.

**Lever** — 16 boards. Returns a **bare array**; a test guards the `payload.get("jobs")`
bug. Salary prose in `additionalPlain` is deliberately unparsed.

**Verification became universal, and that changed the auto-add design.**
Ashby and Lever echo no company name in their APIs — but their **public board pages carry
it in the `<title>`/`og:title`** (`jobs.ashbyhq.com/frontcareers` → "Front"). So every
vendor can be identity-checked, at one extra request.

Verification demands an **exact** normalized match. This is deliberate: a fuzzy matcher
cannot separate "Chime" / "Chime Financial, Inc" (right company, legal name) from "Wise" /
"Wise Worksite Field Sales" (wrong company entirely) — they are structurally identical.
Near misses become `status: unverified`, are **not collected**, and record the name the
board actually reports so a human decides in one glance. Four were confirmed by hand
(Chime, Intercom→"Fin", Remote.com, Rover→"Rover.com"); Carbon Health and Wise were
confirmed **wrong** and demoted.

**`tools/discover.py` — the actual unlock.** Candidates come from the event log's
aggregator rows plus The Muse, are filtered to companies posting a *tracked* role, then
resolved and verified. Only `verified` boards are collected; unresolved ones are recorded
too, so the same dead names are not re-probed every day forever. Runs daily in CI before
collection (`continue-on-error` — discovery is an enhancement, collection is the critical
path). Seeded immediately: **+45 companies**, including Spotify, Binance, TELUS Digital,
Typeform, Life360, PathAI.

The Muse saturates fast — 25 pages yielded only ~35 more candidates than 6 — so it is a
supplement, not an engine. Its bias is the opposite of Himalayas' (paid employer branding,
large enterprises) which is exactly why both are used, and why **neither is ever a
denominator**.

**Contract additions (additive):**
- `RoleFamily.PRODUCT_ANALYST` — strong product/growth/experimentation analytics, split
  from `analyst`. Gabriel wants these surfaced; one bucket made them unfindable. Evaluated
  *after* `product_ds` deliberately, so that at companies where "Advanced Analytics" *is*
  the product-DS function those reqs stay `product_ds`.
- `dedupe_key` (`company_slug:title_normalized`) on `JobPosting` and `PostingEvent`.
  A grouping hint for the analysis layer, **never an identity** — it merges genuinely
  distinct same-titled reqs (common for multi-location postings; 347 such groups on day
  one). Both observations are always kept; nothing is dropped at collection.

**A Phase 1 inconsistency fixed while splitting the taxonomy.** `ds_manager` matched a bare
"lead", so "Analytics Lead" became a manager req while "Lead, Advanced Analytics" did not —
the same job classified two ways depending on word order. "Lead" is an IC seniority marker
and the seniority rules already read it as senior. Removed. Taxonomy `2026-09-13.1`.

**Live:** 14,724 postings, 136/136 scopes diffable, **43 seconds**.

**Still open:** ≥150 verified boards (at 135, climbing daily); Adzuna awaiting Gabriel's
free API key; the `unknown` remote bucket is now ~20% of ATS rows.

### 2026-09-13 — token harvesting: 143 → 382 boards

Gabriel asked whether a public list of board tokens exists to borrow. Investigated, and
the answer reshaped how the watchlist grows.

**What does NOT exist:** no ATS publishes an index. `job-boards.greenhouse.io/sitemap.xml`
and `jobs.lever.co/sitemap.xml` both 404; Ashby's returns its SPA shell. GitHub has no
maintained open list — only Apify actors selling a ~4,600-board directory commercially,
which the near-zero-cost constraint rules out.

**What does: companies publish their own tokens in public forums.** Hacker News' monthly
"Ask HN: Who is hiring?" threads are exactly that, and the Algolia HN API is free, keyless
and documented. Twelve threads yielded **316 distinct board tokens, 253 live, 239 new**.

`tools/harvest_tokens.py` does this, and it **inverts the usual direction**: everywhere else
we start from a company name and guess a token; here the token is known and the *board*
supplies the company name. That is both easier and better evidence — the company published
the link and the board confirms whose it is (`verified_by: published_token`). It also
reaches tokens no guesser would ever produce: `duck-duck-go`, `category-labs`, `runway-ml`,
`andurilindustries`, `addepar1`, `archer56`.

One gotcha that silently returned zero on the first attempt: **HN entity-encodes URLs**
(`https:&#x2F;&#x2F;`), so comment text must be `html.unescape`d before matching.

**Bias, and it is real:** the HN audience skews to startups, developer tools, and
space/defense — the harvest brought in SpaceX (2,412 reqs), Anduril (2,293), Shield AI,
Zoox, Relativity. Excellent for coverage, and **never a denominator**. Worth re-running
monthly as new threads appear; it is not in the daily workflow.

**Result:** 382 verified boards (148 Greenhouse, 189 Ashby, 45 Lever), **26,120 postings in
47 seconds**, 383/383 scopes diffable. The spec's ≥150 criterion is comfortably met.

**Watch the dilution:** across today's bulk load, disclosed salary fell to 19.6% and
remote-known to 55.1%, down from 39% and 80%. That is composition, not regression — the
harvest added many small Greenhouse boards, which carry no salary field and weaker remote
signal. Report these rates per vendor rather than pooled.

**Also fixed here:** `resolve_ats` now fetches careers pages (the stage Phase 2 shipped
incomplete), and token guessing tries regional/boilerplate suffixes after **DoorDash turned
out to be on Greenhouse as `doordashusa` with 456 open reqs** — invisible to every previous
guess, and precisely the kind of major employer whose absence skews the panel small.
`discover.py --retry-unresolved` replays resolver improvements over the whole backlog.

**Workday reassessed — I was too quick to rule it out.** Etsy's endpoint returns a
`remoteType` field (`"Partially Remote"` / `"Open to Remote"`) that Turo's did not. That
makes Workday support **5 of the 6 spec metrics** — only compensation bands are missing,
and the relative `"Posted Yesterday"` string barely matters since every survival metric
uses `first_seen` by design. The remaining cost is three hand-discovered config values per
company. **Recommendation: build it next, driven by a hand-maintained tenant list** for
companies Gabriel names, rather than automatic discovery.

### 2026-09-13 — salary parsed from description prose: 19.6% → 57.1% coverage

Gabriel pushed back on the low salary coverage, correctly: employers print the band in the
job text even when the ATS exposes no field for it. Measured before building anything.

**The measurement.** Of 1,192 stored descriptions, **392 (33%) contained a pay range we
were recording as "no salary"**. A fresh 20-board sample across *all* role families (3,594
postings, not just the tracked ones we retain text for) put the recoverable rate at
**45.3%**. Median band $137k–$192k; **zero implausible or inverted values** in the whole
set, which is what made parsing defensible rather than reckless.

**Result after wiring `src/salary.py` into all three adapters:**

| Vendor | Postings with a band |
|---|---|
| Greenhouse | **62.5%** (was 0% — no salary field exists) |
| Ashby | **64.9%** (structured tiers plus prose fallback) |
| Lever | 7.5% |
| **ATS census overall** | **57.1%**, up from 19.6% |

Core target families (`product_ds` + `ds_manager` + `product_analyst`) now carry a band on
57.4% of postings, median **$180,000–$250,000**.

**Why this was deferred until now, and what makes it safe.** A bad parse pollutes the
compensation series permanently and silently. Three properties:

1. **`SalarySource.DESCRIPTION_PARSED`** — never pooled with `POSTING_DISCLOSED`. Both are
   employer-disclosed; the difference is that one arrived in a vendor field and the other
   through a regex, and only the first is above suspicion. **Report them as separate
   series.** Note it is NOT `salary_is_estimated` — the employer published this figure; it
   is not a prediction like Adzuna's.
2. **Implausible results are discarded, not stored.** Bounds reject signing bonuses,
   funding amounts, per-share equity and inverted ranges. Returning nothing beats returning
   a number nobody will re-examine.
3. **Reprocessable** — descriptions are retained for tracked families, so improving the
   parser and rebuilding reclassifies the history.

**Multi-range postings (~19% of matches).** Employers list several bands: geographic tiers
(Zillow states different bands for DC and Virginia) or internal levels (DoorDash I4/I5/I6).
The stored band spans all of them — the honest reading, since the role genuinely is open
across those tiers — and `range_count` records the ambiguity so analysis can restrict to
`== 1` for tight estimates.

**A bug the fresh sample caught, which the stored data could not.** Lever scored **zero**
until I noticed its pay band lives in `additionalPlain`, not the description. The parser
had been pointed at the wrong field entirely, and would have quietly reported Lever as
publishing no compensation at all. It is still only 7.5% — most Lever boards genuinely do
not publish — but that is now a fact rather than an artifact.

**Structured always wins.** Ashby's compensation tiers take precedence over its own prose;
parsing only fills a gap.

### 2026-09-13 — location parsing and implied-onsite: country 0% → 63.5%

Same question as salary, applied to location. Two gaps, and the second mattered more.

**Gap 1 — `country` was NEVER populated for any ATS row.** Only the aggregator set it. So
"how many of these are open to US candidates" was unanswerable, despite US-eligibility
being one of the few genuinely hard filters in this search. `src/location.py` now parses
`location_raw` into `country` and a US `region` (state).

It parses **right-to-left**, because location strings are right-anchored (city → region →
country). Reading left-to-right breaks on "Washington, District of Columbia": the leading
token is the *city* Washington, but a naive scan matches the *state* Washington and lands
on the wrong side of the country.

| | before | after |
|---|---|---|
| `country` populated | **0%** | **63.5%** |
| US state where applicable | 0% | 51.3% |
| **US-based ATS postings** | unanswerable | **8,968 (54.8%)** |

**Gap 2 — 45% of postings had no determinable remote status, and their locations were
overwhelmingly specific places**: "Costa Mesa, California", "Hawthorne, CA", "Bastrop, TX",
"Starbase, TX". A rocket facility is not a remote job. `remote_implied_by_place` treats a
named settlement, with no remote or hybrid signal anywhere, as onsite.

**Measured twice before shipping.** The Phase 1 hand-labelled sample found 91% of
undetermined rows were in fact onsite. A second check on the current corpus searched the
descriptions of named-place unknowns for any remote-ish word: a third contained one, but
nearly all were false alarms —

- "distributed systems" (an engineering term, not a work arrangement)
- "remote and underserved areas" (Starlink describing its *product*)
- "remote work options are not available" (agreeing with the rule)
- anti-recruitment-scam boilerplate warning about fake "remote interviews"

Roughly 1 in 8 was a genuine signal, putting the rule near 95%.

**It gets its own `RemoteSource.LOCATION_IMPLIED` provenance, and that matters when
reading the headline number:**

| | share |
|---|---|
| remote status determinable | **98.7%** (was 55.1%) |
| **determinable on strong evidence only** (excluding `location_implied`) | **55.1%** |

So 98.7% is real but leans on the weakest inference for 7,131 of 16,376 rows. **Any
remote-share metric should report both, or exclude `location_implied` and say so.** It is
separable precisely so that choice stays available.

**Bug worth remembering:** the new classifier method was appended to the end of
`classify.py` and silently became a *nested function inside another function* rather than a
method — valid Python, ruff-clean, and every posting then failed to parse. The adapter
tests caught it because log-and-skip turned it into an `EMPTY` board rather than a crash.
That is the failure mode this project is built around, working as intended.

### 2026-09-13 — Phase 2.5: the panel became readable

Gabriel wanted something to glance at daily, explicitly not a notification system. Two
outputs: `panel.duckdb` (queryable, gitignored, rebuilt in **0.64s**) and
`reports/latest.md` (regenerated and committed on every run).

**Format: markdown, and the reasoning is worth keeping.** Gabriel pushed for HTML —
reasonably, since his objection was "a wall of text makes things hard". Two facts settled
it: **GitHub does not render `.html` in the repo view** (it shows source), and Pages
**requires a public repo on the Free plan** while a Pages site is **public even when its
source repo is private**. That would have made this a publicly-accessible job-search
dashboard, colliding with the December visibility decision.

The wall-of-text problem is a **layout** problem, and markdown can solve it. Every technique
used was verified against GitHub's own renderer via `POST /markdown` before being relied
on — alerts, `<details>` collapsibles, tables and Unicode block bars all survive
sanitisation. Mermaid survives as a `highlight-source-mermaid` block but GitHub draws it
client-side, so the API cannot confirm the diagram paints; it is therefore **not used**.
The page opens as a glance and folds five detail sections.

**DuckDB reads the gzipped JSONL globs natively — measured, not assumed.** The plan said to
load rows through `Storage.read_jsonl_gz` because the docs were inconclusive. Once duckdb
was installed the question was settleable: **0.14s vs 53s** for row-by-row inserts, with
correct type inference. `build.py` uses the direct read.

**`role_family` and `seniority` are recomputed at build time, not trusted from the log.**
This is the correction that made Phase 2.5 worth more than a report. The event log stores
the classification made at collection, so a taxonomy fix would only ever affect postings
collected afterwards and the series would carry a **silent step change on the day the rules
changed** — exactly the artifact spec 3.5 exists to prevent. Titles are retained precisely
so this is possible; the original values survive as `role_family_logged` /
`seniority_logged` so any reclassification is auditable. It immediately corrected 64 event
rows and 62 posting rows.

**A classifier bug the report surfaced, which no test would have.** Reading actual output
showed "Software Engineer, Full-Stack - Core Experimentation" and "Engineering Manager,
Core Experimentation" sitting in the target-family list. `ml_eng` excluded
platform-engineering titles; `product_ds` did not, so engineers who *build* the
experimentation platform were being counted as product data scientists. Fixed in taxonomy
`2026-09-13.2` — and because of the reclassification above, the fix reached the existing
history rather than only future collections.

**Two smaller traps, both now guarded:**

- A column that is **entirely null** in the files DuckDB reads is inferred as JSON, and
  every later `coalesce(col, 'year')` against it fails. This depends on what the data
  looked like that day, so it would appear without warning on some rebuilds. `build.py`
  casts JSON columns to VARCHAR.
- Job titles contain pipes — "Senior Data Scientist | Drive Innovation Through Data |
  Remote" is a real posting here — and one unescaped pipe shatters a markdown table row.
  `cell()` escapes them; a test asserts it.

**The report carries its own caveats**, because a caveat living in this file is not on
screen when the report is read on a phone: no trends yet; rates must be read per vendor;
the panel is not a market denominator; disclosed and parsed pay bands are never pooled; and
remote share is stated both with and without `location_implied`.

**Self-service is documented in the README** — schema, the two provenance fields that
decide whether a number can be trusted, and five worked queries.

### 2026-09-13 — healthcheck was crying wolf; fixed

The first CI run after Phase 2.5 went red on `PROBLEM: appearances are not decaying
(9816 then 16399) - job_id may not be stable`. **A false positive**, and worth recording
because the failure mode is instructive.

The check compared raw daily appearance totals. That works in steady state, but the
watchlist grew from 61 boards to 382 the same day, and 321 new boards bulk-loading tens of
thousands of genuinely-new postings is **indistinguishable from broken IDs by volume
alone**. Data committed fine — the healthcheck deliberately runs after the commit — but a
check that fires on routine expansion gets ignored, and an ignored healthcheck is worse
than none. Silent failure is the exact threat it exists to catch (spec 11.3).

**First fix attempt was also wrong, in an interesting way.** Restricting the comparison to
companies with appearances on *both* days sounds right, but the event log only records
*changes* — a company with nothing new today is absent from today's file entirely. So that
population self-selects for companies still producing appearances, which is precisely the
group that cannot show decay. It still failed.

**The runs log is the right source.** It records every company actually collected, whether
or not anything changed. Restricting to companies with a run row on both days gives the
honest number:

```
appearances, last 2 days: 9,816 then 16,396 (321 boards added today)
at the 61 companies tracked on both days: 9,261 then 7   <- the number that must decay
```

The general lesson: **"what changed" logs cannot answer "what was observed"**. That
distinction is the same one behind correctness rule 1 — absence of an event is not evidence
of absence — and it bit again here in a different disguise.

Not unit-tested: the logic lives inside `main()`. Worth extracting if it is touched again.

### 2026-09-13 — the workflow's commit step: merge, never rebase

Second failure of the same afternoon, different cause, and a latent bug rather than a
one-off: `git pull --rebase` in the commit step left the runner on a **detached HEAD** when
two runs touched the same UTC day, and a `|| true` swallowed the real error so it surfaced
as a confusing push rejection instead.

**Rebase was the wrong strategy from the start.** Every artifact this workflow commits — a
day's event file, `reports/latest.md`, `config/watchlist.yaml` — is **regenerated wholesale
on each run, never edited incrementally**. There is therefore no meaningful merge between
two versions of one: the newer run's output simply supersedes the older. Rebase instead
replays one run's output on top of another's and conflicts on every shared file, every time.

Now: `git merge origin/main -X ours --no-edit`, which keeps this run's freshly generated
files while still accepting anything the other side added, with up to three push attempts.
Also `set -euo pipefail` and no `|| true`, so a broken commit step fails loudly rather than
quietly leaving a day's collection uncommitted.

**Why it had not bitten before:** in normal daily operation there is one run and no race.
It only appeared because a session was pushing by hand while a run was in flight — which
means it would otherwise have sat dormant until some future session did the same thing, and
then silently cost a day.

Both failures landed in steps *after* collection, which is where failures should land: the
data was written and committed in every case.

Verified green end to end afterwards, all steps including Commit and Healthcheck.

### 2026-09-13 — seniority bands: Gabriel's levels, and a measurement error behind them

Gabriel read the first report and flagged roles that should not be in it: Director, Senior
Director, VP, Senior Manager, Senior Staff and Principal. His words: *"these inflate pay"*.

**He was reporting a filtering problem, but the cause was a measurement error.** The
taxonomy could not express the distinction at all:

| Title | Was classified | Should be |
|---|---|---|
| `Senior Staff Data Scientist` | **`staff`** | `senior_staff` |
| `Senior Manager, Product Data Science` | **`manager`** | `senior_manager` |

So a level above the band was being counted *inside* the two bands he is actually a
candidate for, and dragging their pay statistics up with it. No amount of report filtering
could have fixed that — the information was not in the data.

**`Seniority` gains `SENIOR_STAFF` and `SENIOR_MANAGER`**, with rules ordered so
`senior_manager` is tested before `manager` and `senior_staff` before `staff`. Ordering is
load-bearing here exactly as it is for role families: the more specific rule must run first
or it can never match. Taxonomy `2026-09-13.3`. The reclassification step applies this to
all existing history.

**The band is config, not code** — `seniority.target_band` in `config/taxonomy.yaml`:

```yaml
target_band: [mid, senior, staff, manager, unknown]
```

Out: `junior`, `senior_staff`, `principal`, `senior_manager`, `director` and above. Gabriel
can widen or narrow it in one line, and it re-reads on the next build.

**Out of band means out of the report, not out of the panel.** Everything is still
collected and still in `panel.duckdb` — that is his standing instruction, and these levels
are real market signal. What changed is that the headline list, the company rankings and
**every pay figure** are computed in-band only, and a folded section shows what was excluded
alongside the gap that justifies excluding it:

| Level | Open | Median top of band |
|---|--:|--:|
| senior staff | 10 | $320,000 |
| senior manager | 19 | $270,000 |
| director | 30 | $243,800 |
| **in band** | — | **$211,500** |

**Manager stays in band deliberately.** Gabriel wants to move into a first-line manager role
before long, so those reqs are useful visibility — but only the first-line ones, which is
precisely the distinction the taxonomy previously could not make.

**Two old tests encoded the bug** (`Senior Manager, Data Science` asserted as `manager`) and
were updated. Worth noting as a pattern: a test can pin wrong behaviour just as firmly as
right behaviour, so a failing test after a deliberate fix deserves reading rather than
reflexively re-greening.

### 2026-09-13 — `country` and `region` now recomputed at build time

Found while writing accurate numbers into this file, which is a good argument for writing
them down: `country` coverage read 40.6%, well below the 63.5% measured when location
parsing shipped. The cause was that **day one was collected before the parser existed**, so
those 7,536 rows carried `country = NULL` — a gap indistinguishable from "this posting has
no determinable country", and a permanent discontinuity at the date the feature landed.

`build.py` now recomputes `country` and `region` from the stored `location_raw`, the same
way it already reclassified `role_family` and `seniority`. Coverage went **40.6% → 66.5%**.
Previous values are preserved as `country_logged` / `region_logged`.

**`is_remote` is deliberately NOT recomputed.** Its strongest inputs — a vendor's workplace
field, the description text — are not retained for most postings, so recomputing from the
location alone would *downgrade* rows that were decided on better evidence. The rule is:
recompute a derived field only when **all** of its inputs survive in the log.

**Parsed pay is the one thing day one cannot recover**, because descriptions are kept only
for tracked families. Expect a visible step up in pay coverage at 2026-09-13. It is an
artifact of the feature shipping, not a market movement, and it is flagged in the status
section above so nobody reads it as signal later.

### 2026-09-13 — the report's Remote column was laundering a weak inference

Gabriel asked where the boolean `Remote` column in the first table comes from. Tracing it
turned up a flaw I had introduced: it rendered `is_remote` as a flat `yes` / `no` / `?`
**with no provenance at all**.

| Column showed | Actual provenance | Rows |
|---|---|---|
| `no` | `metadata_field` | 39 |
| `yes` | `metadata_field` | 25 |
| **`no`** | **`location_implied`** | **21** |
| `yes` | `location_string` | 19 |
| `yes` / `no` | `description_text` | 10 |

So **21 of 120 rows showed a confident "no" that was really `location_implied`** — inferred
purely because the location names a specific workplace and nothing anywhere said remote,
roughly 95% accurate. `RemoteSource.LOCATION_IMPLIED` was added *specifically* so that
inference would stay separable, and the report's own caveats say remote must be reported
both ways — and then the headline table collapsed it into something indistinguishable from
a vendor's explicit workplace flag, in the table read most often.

Now rendered as **`likely no`**, with a one-line note under the table. `remote_flag()` is
unit-tested per provenance value.

**The general lesson, which applies well beyond this column:** a derived value and its
confidence have to travel together all the way to the surface. Aggregate sections had the
caveat; the row-level view silently dropped it, and row-level is where a decision actually
gets made. Worth checking any future surface — the Phase 5 brief especially — for the same
mistake.

### 2026-09-14 — the first closures were mostly fake. Only a census may close a posting.

Day three produced the panel's first disappearance events: **223 closures, 217 of them from
Himalayas.** That split was the tell, and it was worth checking rather than celebrating.

**The ages gave it away.** Grouping the "closed" aggregator rows by how long after posting
they vanished:

| Posted N days before vanishing | Count |
|---|---|
| 3 | 8 |
| **4** | **84** |
| **5** | **39** |
| 6–9 | 74 |

`lookback_days` is **4**. Those listings did not close — they **aged out of our sampling
window**. The aggregator is queried with a fixed query set sorted by recency and paged only
a few pages deep, so the window slides forward every day, and a posting that falls behind it
simply stops being looked at. Himalayas listings live about **60 days**; essentially all 217
were still open.

Left alone, this manufactures a closure on a **fixed delay after posting, for every
aggregator row, forever** — and time-to-close would have converged on "4 days" with
beautiful consistency.

**The rule this establishes, which is broader than the spec's version.** Spec Section 5
says "never compute survival from Himalayas rows", which addresses the *metrics* layer. The
real problem is upstream: **a source whose observation window moves must not produce
disappearance events at all.** That is correctness rule 1 generalized —

> "We did not look there" is not evidence of absence, whether the reason is an outage, a
> truncated run, **or a window that moved.**

`SourceRun` now carries `is_census`, set from the `role:` field that was already in
`config/sources.yaml` but never wired to anything. A scope is diffable only when the run
completed cleanly **and** the source observes a census. Appearances are unaffected — those
come from observation, not from diffing.

**Two implementations of the same rule had drifted, and a test caught it.**
`FetchResult.diffable_scopes` and `diff()` each built their own diffable set; patching one
left the other wrong, and the new test failed on exactly that. The fix is applied in both
with a comment pointing at the other. Worth remembering as a pattern: this project already
learned the lesson once with `open_postings` vs `replay_open_postings`, where the
duplication was deliberate and tested. Here it was accidental.

**Cost of regenerating today's file:** the 6 legitimate Greenhouse closures were queued
again rather than emitted, because the earlier run had already consumed their pending-miss
entries. They close tomorrow instead. Same-day re-runs are idempotent for *events* but not
for the pending-miss queue — a wart worth knowing before re-running mid-day.

**Also fixed:** a miss queued against a never-diffable scope is now dropped rather than
left in `pending_misses` forever, where it could never mature and would grow the file
without bound.

### 2026-09-14 — Gabriel's target company list (30), checked against the panel

Gabriel asked for an **unbiased** target list rather than trusting my original seeding, which
was built from an inferred profile he had already corrected once. A clean-context agent —
given his profile but **no knowledge of the existing 399 boards** — produced 30 companies
from ~35 web searches. The full research, with reasoning and caveats per company, is
`TARGET_COMPANIES.md` in the **private** repo (it is search targeting, not panel data).

**Diff against the panel:**

| | Count |
|---|---|
| Already collected | 12 |
| On the watchlist but not collected | 5 |
| Not in the panel at all | 13 |

Of the 18 not being collected, **7 resolved and verified** and are now in: FanDuel (94 reqs),
dLocal (56), Nium (28), Kalshi (28), Parafin (22), DailyPay (19), Trustly (19). Five of those
came via the **careers-page board-title check**, which is the stage that did not exist when
they last failed.

**`target_list: true`** now tags 24 watchlist entries. These are the companies whose coverage
actually matters, as against ~380 auto-discovered boards, and the tag is there so the report
can prioritise them later.

**Ten remain unreachable**, and this is now the strongest case yet for an adapter beyond the
three we have: **Whatnot, Flywire, Checkout.com, Deel, Navan, Revolut, Bilt, Rippling,
Hostaway, Mews**. Four of those are in the agent's top ten, and Whatnot was its pick for the
clearest 12–18 month management path. None could be fingerprinted — their careers pages are
JS-rendered, so the ATS link is not in the served HTML.

**Also worth recording:** the agent independently chose 12 companies already in the panel,
including ranking Airwallex #1 — some convergence with the original seeding. But it surfaced
13 the panel had never heard of, which is the argument for having asked it at all.

**Market checks it ran that change the target set:** Block cut 40% of staff, PayPal is cutting
20%, BILL cut 30%, Etsy cut 12%, Brex reset from $12.3B to $5.2B. Several obvious payments
targets are now bad targets. Re-verify in December — 9,700+ fintech cuts so far in 2026.
