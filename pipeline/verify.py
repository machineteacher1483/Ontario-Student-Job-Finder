"""
Verification: decide whether each record may be published.

Four checks, each producing a flag rather than a silent drop:

  1. deadline      -- passed deadlines archive immediately (free, no network)
  2. domain trust  -- unrecognised domains go to review, not straight to public
  3. liveness      -- does the URL still resolve?
  4. content       -- does a 200 page actually say "this posting is closed"?

Design principle: NEVER silently delete. A record either publishes, archives
(kept for the historical employer-intake signal), or lands in pending review
for a human. Silent drops are how datasets quietly rot without anyone noticing.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urlparse

import requests

from .fetchers import USER_AGENT
from .schema import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING, Opportunity

log = logging.getLogger(__name__)

VERIFY_TIMEOUT = 15
MAX_WORKERS = 8  # keep concurrency modest; we are a guest on these servers

# Domains we trust to publish directly without human review
TRUSTED_DOMAIN_SUFFIXES = {
    "greenhouse.io", "job-boards.greenhouse.io", "boards.greenhouse.io",
    "lever.co", "jobs.lever.co",
    "ashbyhq.com", "jobs.ashbyhq.com",
    "myworkdayjobs.com", "wd1.myworkdayjobs.com", "wd3.myworkdayjobs.com",
    "smartrecruiters.com", "icims.com", "workable.com", "bamboohr.com",
    "adzuna.ca", "adzuna.com",
    "jobbank.gc.ca", "canada.ca", "ontario.ca",
}

# Phrases that mean "this posting is over" even on an HTTP 200 page
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
]


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().lstrip("www.")
    except ValueError:
        return ""


def is_trusted_domain(url: str) -> bool:
    domain = _domain_of(url)
    if not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in TRUSTED_DOMAIN_SUFFIXES)


def check_deadline(record: Opportunity, today: date | None = None) -> bool:
    """True if the deadline has passed."""
    if not record.deadline:
        return False
    today = today or date.today()
    try:
        return datetime.fromisoformat(record.deadline).date() < today
    except ValueError:
        return False


def check_liveness(url: str, session: requests.Session | None = None) -> tuple[bool, str, str]:
    """
    Returns (is_live, detail, body_text).

    HEAD first because it's cheap, but many ATS platforms don't implement HEAD
    properly (405, or a 200 that tells you nothing), so we fall back to GET --
    which we need anyway to read the body for expiry phrases.
    """
    sess = session or requests.Session()
    headers = {"User-Agent": USER_AGENT}

    try:
        head = sess.head(url, timeout=VERIFY_TIMEOUT, allow_redirects=True, headers=headers)
        if head.status_code == 404 or head.status_code == 410:
            return False, f"HTTP {head.status_code}", ""
    except requests.RequestException:
        pass  # fall through to GET

    try:
        resp = sess.get(url, timeout=VERIFY_TIMEOUT, allow_redirects=True, headers=headers)
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}", ""

    if resp.status_code in (404, 410):
        return False, f"HTTP {resp.status_code}", ""
    if resp.status_code >= 500:
        # Server-side problem is not evidence the job is gone
        return True, f"HTTP {resp.status_code} (treated as inconclusive)", ""
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}", ""

    return True, "ok", resp.text[:200_000]


def check_expiry_text(body: str) -> str | None:
    """Return the matched expiry phrase, or None."""
    if not body:
        return None
    text = re.sub(r"<[^>]+>", " ", body).lower()
    text = re.sub(r"\s+", " ", text)
    for phrase in EXPIRY_PHRASES:
        if phrase in text:
            return phrase
    return None


def verify_record(record: Opportunity, session: requests.Session | None = None,
                  today: date | None = None) -> Opportunity:
    """Run all checks against one record and set its status/flags."""
    today = today or date.today()
    record.last_verified = today.isoformat()

    # 1. Deadline -- cheapest check, no network
    if check_deadline(record, today):
        record.status = STATUS_ARCHIVED
        record.flags.append("deadline_passed")
        return record

    # 2. Completeness
    missing = [f for f in ("title", "employer", "source_url") if not getattr(record, f)]
    if missing:
        record.status = STATUS_PENDING
        record.flags.append(f"missing_fields:{','.join(missing)}")
        return record

    # 3. Liveness + content
    is_live, detail, body = check_liveness(record.source_url, session)
    if not is_live:
        record.status = STATUS_ARCHIVED
        record.flags.append(f"dead_link:{detail}")
        return record

    expired_phrase = check_expiry_text(body)
    if expired_phrase:
        record.status = STATUS_ARCHIVED
        record.flags.append(f"expired_text:{expired_phrase}")
        return record

    # 4. Domain trust -- live and open, but from somewhere we don't recognise
    if not is_trusted_domain(record.source_url):
        record.status = STATUS_PENDING
        record.flags.append(f"untrusted_domain:{_domain_of(record.source_url)}")
        return record

    record.status = STATUS_ACTIVE
    return record


def verify_all(records: list[Opportunity], today: date | None = None) -> list[Opportunity]:
    """Verify in parallel. Network-bound, so threads are the right tool here."""
    if not records:
        return []

    session = requests.Session()
    verified: list[Opportunity] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(verify_record, r, session, today): r for r in records}
        for future in as_completed(futures):
            original = futures[future]
            try:
                verified.append(future.result())
            except Exception as exc:  # noqa: BLE001
                log.warning("verification crashed for %s: %s", original.source_url, exc)
                original.status = STATUS_PENDING
                original.flags.append(f"verify_error:{type(exc).__name__}")
                verified.append(original)

    counts: dict[str, int] = {}
    for record in verified:
        counts[record.status] = counts.get(record.status, 0) + 1
    log.info("verification: %s", counts)

    return verified
