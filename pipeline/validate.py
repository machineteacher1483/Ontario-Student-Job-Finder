"""
Task 2 -- automated listing validation.

The insight driving this module: HTTP 200 is a very weak signal of "this
listing is good." The real dataset has 14 records presented as specific job
openings whose URL is actually an Indeed *search query*. Those return 200
forever. A liveness-only checker would call them healthy indefinitely while
students click through to an empty search box.

So validation is split into two tiers:

  STATIC checks  -- no network. URL shape, term expiry, employer specificity,
                    entity-type coherence, field completeness. Free, fast,
                    deterministic, and they catch the failure modes that
                    network checks structurally cannot.

  NETWORK checks -- liveness, redirect tracking, expiry phrases in the page
                    body. Slower, rate-limited, and unreliable on
                    bot-protected domains.

Each check returns a Finding with a severity. Findings roll up into a verdict:

  PUBLISH    -- no critical findings, confidence >= threshold
  REVIEW     -- something needs a human eye before it goes public
  QUARANTINE -- critical problem, must not be shown to students

Nothing is deleted. QUARANTINE is a state, not a delete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import parse_qs, urlparse

# --- Severity ----------------------------------------------------------------
CRITICAL = "critical"   # must not be published
WARNING = "warning"     # publishable only after human review
INFO = "info"           # worth recording, not blocking

SEVERITY_WEIGHT = {CRITICAL: 100, WARNING: 15, INFO: 3}

# --- Verdicts ----------------------------------------------------------------
PUBLISH = "publish"      # clean, show it to students
ARCHIVE = "archive"      # no defects, but not a current opening
REVIEW = "review"        # needs a human before it goes public
QUARANTINE = "quarantine"  # critical defect, must not be shown


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity,
                "message": self.message, "hint": self.hint}


@dataclass
class ValidationResult:
    listing_id: str
    title: str
    organization: str
    url: str
    findings: list[Finding] = field(default_factory=list)
    verdict: str = PUBLISH
    confidence: int = 100
    entity_kind: str = "listing"   # listing | hub | search_source
    canonical_status: str = "unverified"
    checked_network: bool = False

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def criticals(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == CRITICAL]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    def finalize(self, review_threshold: int = 70) -> "ValidationResult":
        penalty = sum(SEVERITY_WEIGHT[f.severity] for f in self.findings)
        self.confidence = max(0, 100 - penalty)

        if self.criticals:
            self.verdict = QUARANTINE
        elif self.canonical_status in ("archived", "watchlist"):
            # No defects, but it is not a current opening. Distinguishing this
            # from PUBLISH matters: a clean archived record is correct data,
            # yet showing it as live would mislead a student.
            self.verdict = ARCHIVE
        elif self.confidence < review_threshold or self.warnings:
            self.verdict = REVIEW
        else:
            self.verdict = PUBLISH
        return self

    def to_dict(self) -> dict:
        return {
            "listing_id": self.listing_id,
            "title": self.title,
            "organization": self.organization,
            "url": self.url,
            "entity_kind": self.entity_kind,
            "canonical_status": self.canonical_status,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "checked_network": self.checked_network,
            "findings": [f.to_dict() for f in self.findings],
        }


# =============================================================================
# URL shape analysis -- the highest-value check in this module
# =============================================================================

# A URL that is a SEARCH QUERY, not a specific posting. Returns 200 forever.
SEARCH_URL_PATTERNS = [
    (r"/q-[^/]*-jobs\.html", "Indeed keyword search page"),
    (r"ca\.indeed\.com/m/jobs\?", "Indeed mobile search page"),
    (r"/jobsearch\?", "Job Bank search page"),
    (r"[?&]searchstring=", "search query parameter"),
    (r"[?&]jkw=", "Job Bank keyword parameter"),
    (r"[?&]sortColumn=", "sorted result listing, not a posting"),
    (r"workopolis\.com/search\?", "Workopolis search page"),
    (r"linkedin\.com/jobs/[a-z-]+-jobs-[a-z-]+/?$", "LinkedIn search page"),
    (r"/jobs\?.*[?&]q=", "generic search query"),
]

# A URL that IS a specific posting
POSTING_URL_PATTERNS = [
    r"indeed\.com/viewjob\?jk=",
    r"linkedin\.com/jobs/view/",
    r"eluta\.ca/spl/",
    r"myworkdayjobs\.com/.*/job/",
    r"greenhouse\.io/.*/jobs/\d+",
    r"lever\.co/[^/]+/[0-9a-f-]{8,}",
    r"ashbyhq\.com/[^/]+/[0-9a-f-]{8,}",
    r"/job/[^/]+/\d+",
    r"jobs\.arup\.com/jobs/[a-z0-9-]+-\d+",
    r"Preview\.aspx\?JobID=\d+",
]

# A URL that is a legitimate career HUB (fine as a monitored source, but must
# not be presented to a student as a specific opening)
HUB_URL_PATTERNS = [
    r"/go/[^/]+/\d+/?$",
    r"/students?[-/]",
    r"/student-",
    r"careers?-?student",
    r"/page/careers",
    r"/featuredopportunities/",
]

# Domains that block automated requests. A 403 from these means "we were
# blocked", NOT "the job is gone" -- conflating those would mass-archive
# perfectly live listings.
BOT_PROTECTED_DOMAINS = {
    "indeed.com", "ca.indeed.com", "emplois.ca.indeed.com",
    "linkedin.com", "ca.linkedin.com", "www.linkedin.com",
    "glassdoor.com", "ziprecruiter.com", "monster.ca",
}

# Aggregators: usable as leads, but the employer's own posting is preferred and
# the aggregator's terms generally restrict automated collection.
AGGREGATOR_DOMAINS = {
    "indeed.com", "ca.indeed.com", "emplois.ca.indeed.com",
    "linkedin.com", "ca.linkedin.com",
    "eluta.ca", "www.eluta.ca",
    "workopolis.com", "www.workopolis.com",
    "glassdoor.com", "ziprecruiter.com",
}

# Employer ATS + government: high trust
TRUSTED_DOMAIN_SUFFIXES = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "smartrecruiters.com", "icims.com", "workable.com", "bamboohr.com",
    "taleo.net", "successfactors.com", "avature.net",
    "jobbank.gc.ca", "canada.ca", "ontario.ca", "gojobs.gov.on.ca",
}


def domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def classify_url(url: str) -> tuple[str, str]:
    """
    Return (kind, detail) where kind is 'posting' | 'search' | 'hub' | 'unknown'.

    Order matters: a specific-posting pattern wins over a search pattern,
    because some posting URLs legitimately carry query parameters.
    """
    if not url:
        return "unknown", "empty URL"

    for pattern in POSTING_URL_PATTERNS:
        if re.search(pattern, url, re.I):
            return "posting", "matches a specific-posting URL pattern"

    for pattern, label in SEARCH_URL_PATTERNS:
        if re.search(pattern, url, re.I):
            return "search", label

    for pattern in HUB_URL_PATTERNS:
        if re.search(pattern, url, re.I):
            return "hub", "matches a career-hub URL pattern"

    return "unknown", "no recognised URL pattern"


def is_bot_protected(url: str) -> bool:
    d = domain_of(url)
    return any(d == b or d.endswith("." + b) for b in BOT_PROTECTED_DOMAINS)


def is_aggregator(url: str) -> bool:
    d = domain_of(url)
    return any(d == a or d.endswith("." + a) for a in AGGREGATOR_DOMAINS)


def is_trusted_domain(url: str) -> bool:
    d = domain_of(url)
    return any(d == t or d.endswith("." + t) for t in TRUSTED_DOMAIN_SUFFIXES)


# =============================================================================
# Term / recency
# =============================================================================
SEASON_END_MONTH = {"winter": 4, "spring": 5, "summer": 8, "fall": 12, "autumn": 12}


def parse_term(term: str) -> tuple[str | None, int | None]:
    """Extract (season, year) from free text like 'Fall 2026' or 'Summer 2026 / 4-12 months'."""
    if not term:
        return None, None
    low = term.lower()

    season = None
    for name in SEASON_END_MONTH:
        if name in low:
            season = "fall" if name == "autumn" else name
            break

    # Month-name ranges: "August-December 2026", "September 2026"
    if season is None:
        months = {
            "january": "winter", "february": "winter", "march": "winter", "april": "winter",
            "may": "summer", "june": "summer", "july": "summer", "august": "summer",
            "september": "fall", "october": "fall", "november": "fall", "december": "fall",
        }
        # Use the LAST month mentioned -- that's when the term ends
        found = [(low.rfind(m), s) for m, s in months.items() if m in low]
        if found:
            season = max(found)[1]

    year_match = re.search(r"\b(20\d{2})\b", low)
    year = int(year_match.group(1)) if year_match else None
    return season, year


def term_has_passed(term: str, today: date | None = None) -> bool | None:
    """
    True if the work term has ended, False if not, None if undeterminable.

    Returning None rather than guessing matters: 'Rolling' and 'Verify' are
    not expired, they're unknown, and treating unknown as expired would
    silently archive every evergreen source on the site.
    """
    today = today or date.today()
    season, year = parse_term(term)
    if season is None or year is None:
        return None
    end_month = SEASON_END_MONTH[season]
    end = date(year, end_month, 28)
    return end < today


# =============================================================================
# Employer specificity
# =============================================================================
PLACEHOLDER_EMPLOYER_PATTERNS = [
    r"^(an?|the)\s",
    r"employer$", r"^employer", r"\bemployers\b",
    r"^(accounting|automotive|technology|manufacturing) (firm|company|employer)",
    r"/ .*offices",
    r"\bvarious\b", r"\bmultiple\b", r"\bunknown\b", r"\bTBD\b",
    r"^\s*$",
    r"/ .*(employers|service)",
]


def employer_is_specific(name: str) -> bool:
    """
    False when the 'employer' is a category rather than an organisation.

    Real examples from the dataset: 'Automotive employer',
    'Accounting firm / Ontario offices', 'Safehaven / youth-service employers'.
    A student cannot research or apply to a category.
    """
    if not name or len(name.strip()) < 3:
        return False
    low = name.strip().lower()
    return not any(re.search(p, low, re.I) for p in PLACEHOLDER_EMPLOYER_PATTERNS)


# =============================================================================
# Status vocabulary normalisation
# =============================================================================
# The live site has 8 free-text status strings. Free text can't be filtered,
# sorted or reasoned about; map to a controlled vocabulary.
STATUS_VOCABULARY = {
    "active": ["live source", "open", "active", "accepting"],
    "unverified": ["verify before applying", "verify", "needs verification"],
    "archived": ["archived", "past term", "closed", "expired", "no longer"],
    "watchlist": ["watchlist", "watch next cycle", "watch fall", "no current",
                  "next cycle", "monitor"],
}


def normalize_status(raw: str) -> str:
    low = (raw or "").lower()
    # Order matters: 'Archived / watch next cycle' is archived first
    for canonical in ("archived", "watchlist", "unverified", "active"):
        if any(kw in low for kw in STATUS_VOCABULARY[canonical]):
            return canonical
    return "unverified"


# =============================================================================
# The static ruleset
# =============================================================================
def validate_static(record: dict, today: date | None = None) -> ValidationResult:
    """
    Run every no-network check against one record (site schema).

    Deliberately runs first and standalone: it costs nothing, it is
    deterministic, and roughly a third of the real dataset's problems are
    caught here without a single HTTP request.
    """
    today = today or date.today()

    title = (record.get("title") or "").strip()
    org = (record.get("organization") or "").strip()
    url = (record.get("url") or "").strip()
    rtype = (record.get("type") or "").strip()
    term = (record.get("term") or "").strip()
    status_raw = (record.get("status") or "").strip()

    result = ValidationResult(
        listing_id=record.get("id") or _fallback_id(record),
        title=title, organization=org, url=url,
    )

    # --- 1. Required fields --------------------------------------------------
    for fname, value in (("title", title), ("organization", org), ("url", url)):
        if not value:
            result.add(Finding(
                "missing_field", CRITICAL,
                f"Required field '{fname}' is empty",
                "A listing without this cannot be shown to students.",
            ))

    if not url:
        return result.finalize()

    # --- 2. URL is well-formed and https -------------------------------------
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        result.add(Finding("bad_url_scheme", CRITICAL,
                           f"URL scheme '{parsed.scheme}' is not http(s)", ""))
        return result.finalize()
    if parsed.scheme == "http":
        result.add(Finding("insecure_url", INFO,
                           "URL uses http rather than https",
                           "Try the https version of the same URL."))

    # --- 3. Entity kind: is this a listing, a hub, or a search page? ---------
    url_kind, url_detail = classify_url(url)
    declared_source = f"{rtype} {record.get('sourceType', '')}".lower()
    declared_is_source = any(w in declared_source for w in
                             ("source", "hub", "board", "program", "search"))

    result.entity_kind = "hub" if declared_is_source else "listing"

    if url_kind == "search" and not declared_is_source:
        # THE headline check. Presented as a specific opening; links to a query.
        result.add(Finding(
            "search_url_as_listing", CRITICAL,
            f"Presented as a specific opening, but the URL is a {url_detail}",
            "Replace with the direct posting URL, or reclassify this record as "
            "a monitored search source rather than an opening.",
        ))
    elif url_kind == "hub" and not declared_is_source:
        result.add(Finding(
            "hub_url_as_listing", WARNING,
            "Presented as a specific opening, but the URL is a career hub page",
            "Link the individual posting, or relabel as a hub source.",
        ))
    elif url_kind == "unknown" and not declared_is_source:
        result.add(Finding(
            "unrecognised_url_shape", INFO,
            f"URL shape not recognised ({url_detail})",
            "Confirm manually that this points at one specific posting.",
        ))

    # --- 4. Bot-protected source --------------------------------------------
    if is_bot_protected(url):
        result.add(Finding(
            "bot_protected_source", WARNING,
            f"{domain_of(url)} blocks automated checks, so this listing can "
            "never be auto-verified",
            "Find the employer's own posting URL. Until then this needs "
            "periodic manual review.",
        ))

    # --- 5. Aggregator when an employer link should exist --------------------
    if is_aggregator(url) and not is_bot_protected(url):
        result.add(Finding(
            "aggregator_link", INFO,
            f"Links to aggregator {domain_of(url)} rather than the employer",
            "Aggregator links expire sooner and add redirect hops.",
        ))

    # --- 6. Employer specificity --------------------------------------------
    if org and not employer_is_specific(org):
        result.add(Finding(
            "vague_employer", CRITICAL,
            f"'{org}' is a category, not a named organisation",
            "Students cannot research or apply to a category. Identify the "
            "actual employer or remove the record.",
        ))

    # --- 7. Work term already over ------------------------------------------
    expired = term_has_passed(term, today)
    if expired is True:
        result.add(Finding(
            "term_passed", CRITICAL,
            f"Work term '{term}' ended before {today.isoformat()}",
            "Archive it. Keep for next-cycle pattern data.",
        ))
    elif expired is None and term and not declared_is_source:
        result.add(Finding(
            "unparseable_term", INFO,
            f"Could not determine whether term '{term}' is current", "",
        ))

    # --- 8. Status coherence -------------------------------------------------
    canonical_status = normalize_status(status_raw)
    result.canonical_status = canonical_status
    if canonical_status == "unverified" and not declared_is_source:
        result.add(Finding(
            "never_verified", WARNING,
            "Status is 'verify before applying' -- this has never been confirmed",
            "Run the network verifier, or check manually.",
        ))
    if canonical_status == "active" and expired is True:
        result.add(Finding(
            "status_contradiction", WARNING,
            f"Marked '{status_raw}' but the term has already ended", "",
        ))

    # --- 9. Duplicate-prone URL ---------------------------------------------
    # Several distinct records sharing one URL is a strong signal that the URL
    # is a list page rather than a posting.
    if parse_qs(parsed.query).get("q") == [""]:
        result.add(Finding(
            "empty_query_param", INFO,
            "URL carries an empty search parameter", "",
        ))

    return result.finalize()


def _fallback_id(record: dict) -> str:
    import hashlib
    raw = f"{record.get('title','')}|{record.get('organization','')}|{record.get('city','')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def find_shared_urls(records: list[dict]) -> dict[str, list[str]]:
    """Map any URL used by 2+ records to the titles sharing it."""
    from collections import defaultdict
    buckets: dict[str, list[str]] = defaultdict(list)
    for r in records:
        if r.get("url"):
            buckets[r["url"]].append(r.get("title", "?"))
    return {u: t for u, t in buckets.items() if len(t) > 1}
