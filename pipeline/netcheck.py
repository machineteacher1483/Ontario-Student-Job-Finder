"""
Task 2 -- network validation tier.

Runs only on records that survived the static tier, because there is no point
spending an HTTP request on a listing we already know is broken.

The single most important behaviour here: distinguishing BLOCKED from DEAD.

  403 / 429 from Indeed or LinkedIn  -> we were blocked. Says nothing about
                                        whether the job exists. Must NOT archive.
  404 / 410 from an employer ATS     -> the posting is genuinely gone. Archive.
  5xx from anywhere                  -> the server is unwell. Inconclusive.

Getting this wrong is how a validator mass-archives a third of a live dataset
overnight and nobody notices until students complain the site is empty.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .validate import (CRITICAL, INFO, WARNING, Finding, ValidationResult,
                       is_bot_protected)

log = logging.getLogger(__name__)

USER_AGENT = (
    "OntarioInternshipFinder/1.0 (+https://oninternshipfinder.netlify.app; "
    "student project; listing-validity checker)"
)

TIMEOUT = 20
MAX_WORKERS = 6
POLITE_DELAY = 0.3

# Phrases meaning "this posting is over" even on an HTTP 200
EXPIRY_PHRASES = [
    "no longer accepting applications",
    "no longer available",
    "this position has been filled",
    "position is closed",
    "posting has expired",
    "job has expired",
    "this job is no longer",
    "applications are closed",
    "we are no longer accepting",
    "job not found",
    "posting not found",
    "this opportunity has closed",
    "the job you are looking for",
    "this requisition is closed",
    "sorry, this job is not available",
]

# Phrases meaning "you got a bot wall", not a real page
BLOCK_PHRASES = [
    "verify you are human",
    "unusual traffic",
    "captcha",
    "access denied",
    "are you a robot",
    "enable javascript and cookies",
    "request unsuccessful",
    "cf-browser-verification",
]

# A redirect landing on one of these means the specific posting is gone and
# you have been bounced to a generic page.
GENERIC_LANDING_PATTERNS = [
    r"/jobs/?$", r"/careers/?$", r"/search/?$", r"/$",
    r"job-not-found", r"/error", r"/expired",
]

OUTCOME_LIVE = "live"
OUTCOME_DEAD = "dead"
OUTCOME_BLOCKED = "blocked"
OUTCOME_INCONCLUSIVE = "inconclusive"


@dataclass
class ProbeResult:
    outcome: str
    status_code: int | None = None
    final_url: str = ""
    detail: str = ""
    redirected: bool = False


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
    })
    return s


def probe(url: str, session: requests.Session | None = None) -> ProbeResult:
    """One HTTP probe, classified into live / dead / blocked / inconclusive."""
    sess = session or _session()

    try:
        resp = sess.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.Timeout:
        return ProbeResult(OUTCOME_INCONCLUSIVE, detail="timeout")
    except requests.TooManyRedirects:
        return ProbeResult(OUTCOME_DEAD, detail="redirect loop")
    except requests.RequestException as exc:
        return ProbeResult(OUTCOME_INCONCLUSIVE, detail=f"{type(exc).__name__}")

    code = resp.status_code
    final_url = resp.url
    redirected = final_url.rstrip("/") != url.rstrip("/")

    # --- blocked, not dead ---------------------------------------------------
    if code in (401, 403, 429):
        return ProbeResult(OUTCOME_BLOCKED, code, final_url,
                           f"HTTP {code} -- automated access refused", redirected)

    if code in (404, 410):
        return ProbeResult(OUTCOME_DEAD, code, final_url, f"HTTP {code}", redirected)

    if code >= 500:
        return ProbeResult(OUTCOME_INCONCLUSIVE, code, final_url,
                           f"HTTP {code} -- server error, not evidence of removal", redirected)

    if code != 200:
        return ProbeResult(OUTCOME_INCONCLUSIVE, code, final_url, f"HTTP {code}", redirected)

    body = resp.text[:300_000]
    text = re.sub(r"<[^>]+>", " ", body).lower()
    text = re.sub(r"\s+", " ", text)

    # A 200 that is actually a bot wall
    for phrase in BLOCK_PHRASES:
        if phrase in text:
            return ProbeResult(OUTCOME_BLOCKED, code, final_url,
                               f"HTTP 200 but page is a bot challenge ({phrase})", redirected)

    # A 200 that says the posting is closed
    for phrase in EXPIRY_PHRASES:
        if phrase in text:
            return ProbeResult(OUTCOME_DEAD, code, final_url,
                               f"page states: {phrase}", redirected)

    # Redirected away from a specific posting to a generic page
    if redirected:
        path = urlparse(final_url).path
        for pattern in GENERIC_LANDING_PATTERNS:
            if re.search(pattern, path, re.I):
                return ProbeResult(OUTCOME_DEAD, code, final_url,
                                   "redirected to a generic listing page", True)

    # Suspiciously small page
    if len(text) < 400:
        return ProbeResult(OUTCOME_INCONCLUSIVE, code, final_url,
                           f"page body only {len(text)} chars", redirected)

    return ProbeResult(OUTCOME_LIVE, code, final_url, "ok", redirected)


def validate_network(result: ValidationResult,
                     session: requests.Session | None = None) -> ValidationResult:
    """Add network findings to an already statically-validated result."""
    if not result.url:
        return result

    # Don't waste requests on records already quarantined by static checks
    if result.criticals:
        result.add(Finding("network_skipped", INFO,
                           "Network check skipped -- already quarantined", ""))
        return result

    probe_result = probe(result.url, session)
    result.checked_network = True

    if probe_result.outcome == OUTCOME_DEAD:
        result.add(Finding(
            "dead_link", CRITICAL,
            f"Listing is gone ({probe_result.detail})",
            "Archive it.",
        ))

    elif probe_result.outcome == OUTCOME_BLOCKED:
        severity = INFO if is_bot_protected(result.url) else WARNING
        result.add(Finding(
            "verification_blocked", severity,
            f"Could not verify -- {probe_result.detail}",
            "This is NOT evidence the job is gone. Needs a manual check, or "
            "swap in the employer's own posting URL.",
        ))

    elif probe_result.outcome == OUTCOME_INCONCLUSIVE:
        result.add(Finding(
            "verification_inconclusive", INFO,
            f"Inconclusive -- {probe_result.detail}",
            "Re-check next cycle before acting.",
        ))

    if probe_result.redirected and probe_result.outcome == OUTCOME_LIVE:
        result.add(Finding(
            "redirected", INFO,
            f"Redirects to {probe_result.final_url[:110]}",
            "Consider storing the final URL directly.",
        ))

    # Recompute verdict now that network findings are in
    return result.finalize()


def validate_network_batch(results: list[ValidationResult],
                           max_workers: int = MAX_WORKERS) -> list[ValidationResult]:
    """Probe many listings in parallel. Network-bound, so threads fit."""
    if not results:
        return []

    session = _session()
    out: list[ValidationResult] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(validate_network, r, session): r for r in results}
        for future in as_completed(futures):
            original = futures[future]
            try:
                out.append(future.result())
            except Exception as exc:  # noqa: BLE001
                log.warning("probe crashed for %s: %s", original.url, exc)
                original.add(Finding("probe_error", INFO,
                                     f"Checker crashed: {type(exc).__name__}", ""))
                out.append(original.finalize())
            time.sleep(POLITE_DELAY)

    return out
