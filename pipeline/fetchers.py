"""
Source fetchers.

DESIGN NOTE -- the most important thing in this file:

An empty result list is ambiguous. `[]` can mean any of:
    1. the board exists and genuinely has no open student roles
    2. the board token is wrong / the company left this ATS  (404)
    3. the ATS is having an outage                            (5xx)
    4. we got rate limited                                    (429)

If you collapse all four into "no jobs", then three weeks later someone asks
why a company "stopped hiring students" and the real answer is that your board
token had a typo the whole time. So every fetch returns a FetchResult with an
explicit outcome, and only OK-with-zero-results is treated as "really empty".
Anything else keeps the previous cycle's data for that source and raises a
flag for human review.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "OntarioInternshipFinder/1.0 "
    "(+https://oninternshipfinder.netlify.app; student project; contact: admin@example.com)"
)

DEFAULT_TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_BASE = 2.0


# --- Fetch outcome typing ----------------------------------------------------
OK = "ok"                    # request succeeded, results are trustworthy
NOT_FOUND = "not_found"      # 404 -- board token likely wrong
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
NETWORK_ERROR = "network_error"
BAD_PAYLOAD = "bad_payload"  # 200 but the JSON wasn't the shape we expected


@dataclass
class FetchResult:
    source_name: str
    source_type: str
    outcome: str
    raw_records: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""

    @property
    def trustworthy(self) -> bool:
        """Only OK results may overwrite the previous cycle's data."""
        return self.outcome == OK


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def _get_json(
    url: str,
    params: dict[str, Any] | None = None,
    session: requests.Session | None = None,
) -> tuple[str, Any, str]:
    """
    GET with retry/backoff. Returns (outcome, payload, detail).

    Retries only on transient failures (5xx, 429, network). A 404 is a
    permanent config error -- retrying it just wastes time and hides the bug.
    """
    sess = session or _session()
    last_detail = ""

    for attempt in range(MAX_RETRIES):
        try:
            resp = sess.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
            log.warning("network error on %s (attempt %d): %s", url, attempt + 1, exc)
            time.sleep(BACKOFF_BASE ** attempt)
            continue

        if resp.status_code == 404:
            return NOT_FOUND, None, f"HTTP 404 for {url}"

        if resp.status_code == 429:
            last_detail = "HTTP 429 rate limited"
            retry_after = int(resp.headers.get("Retry-After", BACKOFF_BASE ** attempt))
            log.warning("rate limited on %s, sleeping %ss", url, retry_after)
            time.sleep(min(retry_after, 60))
            continue

        if resp.status_code >= 500:
            last_detail = f"HTTP {resp.status_code}"
            time.sleep(BACKOFF_BASE ** attempt)
            continue

        if resp.status_code != 200:
            return SERVER_ERROR, None, f"HTTP {resp.status_code}"

        try:
            return OK, resp.json(), ""
        except ValueError as exc:
            return BAD_PAYLOAD, None, f"invalid JSON: {exc}"

    if "429" in last_detail:
        return RATE_LIMITED, None, last_detail
    if last_detail.startswith("HTTP 5"):
        return SERVER_ERROR, None, last_detail
    return NETWORK_ERROR, None, last_detail or "exhausted retries"


# --- Greenhouse --------------------------------------------------------------
def fetch_greenhouse(source: dict[str, Any], session=None) -> FetchResult:
    """
    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

    No auth required for reads. `content=true` includes the HTML description,
    which we need for program tagging and term inference.
    """
    token = source["board_token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    outcome, payload, detail = _get_json(url, {"content": "true"}, session)

    if outcome != OK:
        return FetchResult(source["name"], "greenhouse", outcome, detail=detail)

    if not isinstance(payload, dict) or "jobs" not in payload:
        return FetchResult(
            source["name"], "greenhouse", BAD_PAYLOAD,
            detail="response had no 'jobs' key -- is this actually a Greenhouse board?",
        )

    return FetchResult(source["name"], "greenhouse", OK, raw_records=payload["jobs"])


# --- Lever -------------------------------------------------------------------
def fetch_lever(source: dict[str, Any], session=None) -> FetchResult:
    """
    GET https://api.lever.co/v0/postings/{company}?mode=json

    Note Lever returns a BARE ARRAY, not an object with a key. Code written for
    Greenhouse that does payload["jobs"] will silently produce [] forever here.
    """
    company = source["company_slug"]
    url = f"https://api.lever.co/v0/postings/{company}"
    outcome, payload, detail = _get_json(url, {"mode": "json"}, session)

    if outcome != OK:
        return FetchResult(source["name"], "lever", outcome, detail=detail)

    if not isinstance(payload, list):
        return FetchResult(
            source["name"], "lever", BAD_PAYLOAD,
            detail="expected a JSON array -- is this actually a Lever board?",
        )

    return FetchResult(source["name"], "lever", OK, raw_records=payload)


# --- Ashby -------------------------------------------------------------------
def fetch_ashby(source: dict[str, Any], session=None) -> FetchResult:
    """GET https://api.ashbyhq.com/posting-api/job-board/{org}"""
    org = source["org_slug"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
    outcome, payload, detail = _get_json(url, None, session)

    if outcome != OK:
        return FetchResult(source["name"], "ashby", outcome, detail=detail)

    if not isinstance(payload, dict) or "jobs" not in payload:
        return FetchResult(source["name"], "ashby", BAD_PAYLOAD, detail="no 'jobs' key")

    return FetchResult(source["name"], "ashby", OK, raw_records=payload["jobs"])


# --- Adzuna ------------------------------------------------------------------
def fetch_adzuna(source: dict[str, Any], session=None) -> FetchResult:
    """
    GET https://api.adzuna.com/v1/api/jobs/ca/search/{page}

    Requires app_id + app_key (free developer tier). Paginates; we walk pages
    until we run out of results or hit max_pages.
    """
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")

    if not app_id or not app_key:
        return FetchResult(
            source["name"], "adzuna", NETWORK_ERROR,
            detail="ADZUNA_APP_ID / ADZUNA_APP_KEY not set in environment",
        )

    all_results: list[dict[str, Any]] = []
    max_pages = source.get("max_pages", 5)
    per_page = source.get("results_per_page", 50)
    sess = session or _session()

    for page in range(1, max_pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/ca/search/{page}"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": per_page,
            "what": source.get("query", "internship co-op student"),
            "where": source.get("where", "Ontario"),
            "max_days_old": source.get("max_days_old", 30),
            "content-type": "application/json",
        }
        outcome, payload, detail = _get_json(url, params, sess)

        if outcome != OK:
            # Partial success still counts if we already have pages banked
            if all_results:
                log.warning("adzuna page %d failed (%s), keeping %d earlier results",
                            page, outcome, len(all_results))
                break
            return FetchResult(source["name"], "adzuna", outcome, detail=detail)

        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not results:
            break
        all_results.extend(results)

        if len(results) < per_page:
            break
        time.sleep(0.5)  # be polite to the API

    return FetchResult(source["name"], "adzuna", OK, raw_records=all_results)



# --- Local fixture (offline demo / testing) ----------------------------------
def fetch_fixture(source: dict[str, Any], session=None) -> FetchResult:
    """
    Read raw records from a local JSON file instead of the network.

    Lets you demo and debug the full pipeline with no API keys and no internet
    -- useful when showing the project to someone, or developing offline.
    Set "type": "fixture" and "path": "tests/fixtures/x.json" in sources.json.
    """
    import json as _json
    from pathlib import Path as _Path

    path = _Path(source["path"])
    if not path.exists():
        return FetchResult(source["name"], "fixture", NOT_FOUND,
                           detail=f"fixture file not found: {path}")
    try:
        payload = _json.loads(path.read_text())
    except ValueError as exc:
        return FetchResult(source["name"], "fixture", BAD_PAYLOAD, detail=str(exc))

    records = payload if isinstance(payload, list) else payload.get("jobs", [])
    result = FetchResult(source["name"], source.get("emulates", "greenhouse"),
                         OK, raw_records=records)
    return result


# --- Registry ----------------------------------------------------------------
FETCHERS: dict[str, Callable[..., FetchResult]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "adzuna": fetch_adzuna,
    "fixture": fetch_fixture,
}


def fetch_source(source: dict[str, Any], session=None) -> FetchResult:
    """Dispatch one source config to its fetcher, never raising."""
    stype = source.get("type")
    fetcher = FETCHERS.get(stype)

    if fetcher is None:
        return FetchResult(
            source.get("name", "?"), stype or "?", BAD_PAYLOAD,
            detail=f"no fetcher registered for type '{stype}'",
        )

    try:
        return fetcher(source, session)
    except Exception as exc:  # noqa: BLE001 -- one bad source must not kill the run
        log.exception("unhandled error fetching %s", source.get("name"))
        return FetchResult(
            source.get("name", "?"), stype, NETWORK_ERROR,
            detail=f"unhandled {type(exc).__name__}: {exc}",
        )
