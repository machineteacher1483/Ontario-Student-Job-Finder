# Ontario Internship Finder — 20-Day Refresh Pipeline

Automated collection pipeline for [oninternshipfinder.netlify.app](https://oninternshipfinder.netlify.app/).
Pulls Ontario student roles from employer ATS APIs and a job aggregator, normalizes
them into one schema, deduplicates across sources, verifies each listing is still
live, and publishes `data/opportunities.json` for the static site to read.

Implements **Task 1** (automated 20-day refresh) and **Task 2** (automated listing
validation and review queue).

---

## Quick start

```bash
pip install -r requirements.txt

# Offline demo — no API keys, no internet needed
python scripts/refresh.py --config config/sources.demo.json --no-verify --force

# Check that your real source tokens actually work
python scripts/check_sources.py

# Real run (gated: does nothing unless 20+ days have passed)
python scripts/refresh.py
```

Get free Adzuna credentials at <https://developer.adzuna.com/>, then:

```bash
cp .env.example .env      # local
# In CI: repo Settings → Secrets and variables → Actions → New repository secret
```

---

## Why APIs and not scraping

Every source here is an official public JSON endpoint — the same one the company's
own careers page calls. No HTML parsing, no login, no bot-detection arms race.

| Source | Endpoint | Auth |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs` | none |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | none |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{org}` | none |
| Adzuna | `api.adzuna.com/v1/api/jobs/ca/search/{page}` | free app_id + app_key |

The two-API split from the brief maps to: **ATS APIs** give high-confidence,
structured data from companies you've deliberately vetted (these are the large
employers with dedicated early-career recruiters); **Adzuna** gives breadth and
catches employers you'd never think to target directly. Precision plus recall.

---

## Architecture

```
config/sources.json          Source registry — add a company here, not in code
        │
        ▼
pipeline/fetchers.py         HTTP + retry/backoff, explicit failure typing
        │
        ▼
pipeline/normalize.py        Raw payload → canonical Opportunity records
pipeline/classify.py           ├─ Ontario filter
                               ├─ student-role detection
                               └─ program tagging (keyword rules)
        │
        ▼
pipeline/dedupe.py           Collapse cross-source dupes, employer link wins
        │
        ▼
pipeline/verify.py           Deadline → completeness → liveness → expiry text → domain trust
        │
        ▼
pipeline/store.py            20-day gate, merge with archive, publish + rollback
        │
        ▼
data/opportunities.json      What the website reads
```

---

## Three design decisions worth explaining in an interview

**1. An empty result is ambiguous, so it's never treated as "no jobs."**

`[]` from an ATS can mean the board genuinely has no student roles, *or* the board
token is wrong (404), *or* the ATS is down (5xx), *or* you got rate limited (429).
Collapse those and three weeks later someone asks why a company "stopped hiring
students" when really you had a typo the whole time. Every fetch returns a
`FetchResult` with an explicit outcome; only `OK` may overwrite existing data.

**2. Nothing is ever silently deleted.**

A record either publishes, archives (with the reason in `flags`), or goes to
`pending_review` for a human. Archived listings are kept ~400 days because they
show career centres which employers hire students and when the next intake opens.

**3. The pipeline refuses to publish a suspicious collapse.**

If active listings drop more than 60% in one cycle, the run aborts instead of
publishing. That pattern almost always means an API changed shape or verification
is throwing false positives — not that every employer stopped hiring at once.
`data/opportunities.prev.json` is the rollback path.

---

## Scheduling: why daily-with-a-gate, not a 20-day cron

Cron cannot express "every 20 days." `*/20` in the day-of-month field fires on the
1st and 21st and resets each month — that's not a 20-day cycle. So GitHub Actions
runs the script **daily**, and `Store.should_refresh()` decides whether to do work.
19 of 20 runs exit in about a second.

It also self-heals: if one day's run fails, tomorrow's picks it up instead of the
entire cycle being missed.

---

## Verification checks

| Check | Cost | Outcome on failure |
|---|---|---|
| Deadline passed | free | `archived` |
| Missing required fields | free | `pending_review` |
| Dead link (404/410) | 1 request | `archived` |
| Page says "no longer accepting applications" | same request | `archived` |
| Unrecognised domain | free | `pending_review` |

A 5xx during verification is treated as *inconclusive*, not as evidence the job is
gone — a server having a bad day shouldn't purge listings.

Verification runs in a thread pool (8 workers) since it's network-bound.

---

## CLI reference

```bash
python scripts/refresh.py                   # gated run
python scripts/refresh.py --force           # ignore the gate
python scripts/refresh.py --dry-run         # process, don't publish
python scripts/refresh.py --no-verify       # skip network checks (fast iteration)
python scripts/refresh.py --rollback        # restore previous dataset
python scripts/refresh.py --interval 14     # different cadence
python scripts/refresh.py -v                # debug logging

python scripts/check_sources.py             # validate the registry
python -m pytest tests/ -q                  # 48 tests, no network needed
```

---

## Adding a source

1. Find the token: Greenhouse `boards.greenhouse.io/<token>`, Lever `jobs.lever.co/<slug>`.
2. Add an entry to `config/sources.json` with `"enabled": false`.
3. Run `python scripts/check_sources.py --include-disabled`.
4. If it reports OK with a sensible count, flip `enabled` to `true`.

New ATS platform? Write one fetcher in `fetchers.py` and one normalizer in
`normalize.py`, register both in their dicts. Nothing else changes.

---

## Connecting to the website

The site already reads `/data/opportunities.json`. This pipeline publishes:

```json
{
  "generated_at": "2026-09-09T22:10:49",
  "refresh_interval_days": 20,
  "counts": { "total": 3, "active": 3, "archived": 0, "pending_review": 0 },
  "source_health": { "DemoCorp": { "last_outcome": "ok", "last_raw_count": 5 } },
  "opportunities": [ { "title": "...", "employer": "...", "status": "active", ... } ]
}
```

Filter the frontend on `status === "active"`. If your existing render function
expects different field names, the mapping lives in one place — `pipeline/schema.py`.

Netlify rebuilds automatically on push, so the Actions commit deploys the new data
with no extra wiring.

---

## Legal note

All sources are official public APIs intended for programmatic access. Adzuna's
terms require attribution and rate limiting — both are honoured. No source here
requires a login or prohibits automated access. If you add a source later, check
its terms first; the trust boundary is in `verify.py`'s `TRUSTED_DOMAIN_SUFFIXES`.

The pipeline identifies itself honestly via User-Agent, retries with backoff,
respects `Retry-After`, and caps concurrency at 8.


---

# Task 2 — Listing validation

```bash
# Static checks — instant, no network, no API keys
python scripts/validate_listings.py --input data/site_listings.json

# Also probe every URL live
python scripts/validate_listings.py --input data/site_listings.json --network

# Pull listings straight out of the site's HTML
python scripts/validate_listings.py --from-html ontario_internship_finder_updated.html
```

Opens `reports/review_queue.html` — a sortable dashboard, plus a CSV for Sheets
and a JSON report for the pipeline.

## Results on the current live dataset (53 records, static checks only)

| Verdict | Count | Meaning |
|---|---|---|
| Quarantine | 18 | Critical defect — must not be shown to students |
| Needs review | 23 | Publishable once a person confirms |
| Archive | 6 | Sound data, but not a current opening |
| Ready | 6 | Passed every automated check |

| Finding | Count |
|---|---|
| `never_verified` | 33 |
| `bot_protected_source` | 14 |
| `search_url_as_listing` | 13 |
| `aggregator_link` | 12 |
| `term_passed` | 9 |
| `vague_employer` | 3 |
| `hub_url_as_listing` | 2 |

## Why HTTP status checking isn't enough

`search_url_as_listing` is the finding that justifies this whole module. Thirteen
records are presented as specific job openings, but their URL is an Indeed or
Workday *search query*:

```
Uline — "Outside Sales Internship / Co-op"
  → https://ca.indeed.com/q-uline-jobs-l-ontario-jobs.html
```

That URL returns HTTP 200 forever. Liveness checking would mark it healthy
indefinitely while every student who clicks it lands on a search box. Only URL
*shape* analysis catches this, and it costs zero requests.

## The two tiers

**Static** (`pipeline/validate.py`) — no network, deterministic, free:

| Check | Severity on failure |
|---|---|
| Missing title / employer / URL | critical |
| URL is a search page but record claims to be an opening | critical |
| Employer is a category ("Automotive employer") not an organisation | critical |
| Work term already ended | critical |
| URL is a career hub but record claims to be an opening | warning |
| Source blocks automated checks (Indeed, LinkedIn) | warning |
| Status still "verify before applying" | warning |
| Links to an aggregator rather than the employer | info |

**Network** (`pipeline/netcheck.py`) — runs only on records that survived static:

| Signal | Interpretation |
|---|---|
| 404 / 410 | Dead. Archive it. |
| 403 / 429, or a 200 containing a CAPTCHA wall | **Blocked, not dead.** Never archive. |
| 5xx | Inconclusive. Re-check next cycle. |
| 200 with "no longer accepting applications" | Dead. Archive it. |
| Redirects to a generic `/jobs` page | Dead — the specific posting is gone. |
| Body under 400 characters | Inconclusive. |

**Blocked ≠ dead is the load-bearing distinction.** Fourteen records here live on
Indeed and LinkedIn, which return 403 to automated clients. A checker that reads
403 as "gone" would archive all fourteen overnight — a quarter of the site —
while every one of those jobs is still open.

## Scoring

Confidence starts at 100: −100 per critical, −15 per warning, −3 per note.

- any critical → **quarantine**
- clean, but status is archived/watchlist → **archive**
- confidence < 70, or any warning → **review**
- otherwise → **publish**

This replaces the hand-assigned High/Medium/Low `confidence` field with something
reproducible and explainable — every score comes with the findings that produced it.

## Integration with Task 1

`scripts/refresh.py` runs static validation on every record before publishing.
Records with a critical finding are never network-probed (no point spending a
request on a listing already known to be broken) and never published as active.

**Nothing is ever deleted.** Quarantine is a state. Every record keeps its
findings, so a fix can be verified against the same rule that flagged it.

## Fixing the flagged records

1. Open `reports/review_queue.html`, work top-down (worst confidence first).
2. For `search_url_as_listing`: find the direct posting URL, or reclassify the
   record as a monitored search source — the same URL is completely legitimate
   when labelled honestly (there's a test covering exactly this).
3. For `term_passed`: set status to archived. Keep the record.
4. For `vague_employer`: identify the real employer or drop the record.
5. Re-run the validator to confirm the finding clears.

## Also worth doing

The site currently hardcodes its data as `const OPPS = [...]` inside the HTML,
so updating listings means editing the page. Step 8 of your own plan calls for
reading `/data/opportunities.json` instead — worth doing, since it's what lets
the refresh pipeline update listings without touching the design.
