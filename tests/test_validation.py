"""
Task 2 validation tests.

Several cases are drawn verbatim from the live site's data, so they act as
regression tests: if someone loosens a rule later, the real-world defect it
was written to catch fails the build.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.netcheck import (BLOCK_PHRASES, EXPIRY_PHRASES, OUTCOME_BLOCKED,
                               OUTCOME_DEAD, OUTCOME_LIVE)
from pipeline.validate import (ARCHIVE, CRITICAL, PUBLISH, QUARANTINE, REVIEW,
                               WARNING, classify_url, employer_is_specific,
                               find_shared_urls, is_aggregator, is_bot_protected,
                               normalize_status, parse_term, term_has_passed,
                               validate_static)

TODAY = date(2026, 9, 12)


# --- URL classification: the headline check ----------------------------------
class TestUrlClassification:
    @pytest.mark.parametrize("url", [
        "https://ca.indeed.com/q-uline-jobs-l-ontario-jobs.html",
        "https://ca.indeed.com/m/jobs?l=Toronto%2C+ON&q=Winter+Co+Op",
        "https://emplois.ca.indeed.com/q-winter-co-op-l-greater-toronto-area%2C-on-jobs.html",
        "https://www.jobbank.gc.ca/jobsearch/jobsearch?searchstring=internship",
        "https://www.workopolis.com/search?l=ontario&q=co+op",
        "https://ca.linkedin.com/jobs/internship-jobs-kitchener-on",
    ])
    def test_search_pages_detected(self, url):
        kind, _ = classify_url(url)
        assert kind == "search", f"{url} should be recognised as a search page"

    @pytest.mark.parametrize("url", [
        "https://ca.indeed.com/viewjob?jk=2877129e0d8b6e2f",
        "https://ca.linkedin.com/jobs/view/it-technical-advisor-4408693641",
        "https://www.eluta.ca/spl/logistics-co-op-549eed8b812b4ea6dfada94880c5ba2c",
        "https://td.wd3.myworkdayjobs.com/en-US/TD_Bank_Careers/job/Toronto-Ontario/Fund-My-Business-Intern---Co-Op_R_1480660",
        "https://jobs.arup.com/jobs/fire-safety-intern-august-december-2026-32744",
        "https://www.gojobs.gov.on.ca/Preview.aspx?JobID=201105&Language=English",
    ])
    def test_real_postings_detected(self, url):
        kind, _ = classify_url(url)
        assert kind == "posting", f"{url} should be recognised as a real posting"

    def test_posting_pattern_beats_search_pattern(self):
        # Carries a query string but is still a specific posting
        url = "https://ca.indeed.com/viewjob?jk=abc123&from=serp&q=intern"
        assert classify_url(url)[0] == "posting"


class TestDomainClassification:
    @pytest.mark.parametrize("url,blocked", [
        ("https://ca.indeed.com/viewjob?jk=x", True),
        ("https://ca.linkedin.com/jobs/view/1", True),
        ("https://www.eluta.ca/spl/x", False),
        ("https://jobs.arup.com/jobs/x-1", False),
    ])
    def test_bot_protection(self, url, blocked):
        assert is_bot_protected(url) is blocked

    def test_eluta_is_aggregator_but_not_bot_protected(self):
        url = "https://www.eluta.ca/spl/logistics-co-op-549eed"
        assert is_aggregator(url) is True
        assert is_bot_protected(url) is False


# --- Term parsing ------------------------------------------------------------
class TestTermParsing:
    @pytest.mark.parametrize("term,season,year", [
        ("Fall 2026", "fall", 2026),
        ("Summer 2026 / 4–12 months", "summer", 2026),
        ("Winter 2027", "winter", 2027),
        ("August–December 2026", "fall", 2026),
        ("September 2026 / 12 months", "fall", 2026),
        ("January 2027", "winter", 2027),
    ])
    def test_parse(self, term, season, year):
        assert parse_term(term) == (season, year)

    @pytest.mark.parametrize("term,passed", [
        ("Summer 2026", True),
        ("Winter 2026", True),
        ("Fall 2026", False),
        ("Winter 2027", False),
        ("Summer 2027 next cycle", False),
    ])
    def test_expiry(self, term, passed):
        assert term_has_passed(term, TODAY) is passed

    @pytest.mark.parametrize("term", ["Rolling", "Verify", "Rolling / verify", ""])
    def test_unknown_terms_return_none_not_expired(self, term):
        # Critical: returning True here would archive every evergreen source
        assert term_has_passed(term, TODAY) is None


# --- Employer specificity ----------------------------------------------------
class TestEmployerSpecificity:
    @pytest.mark.parametrize("name", [
        "Automotive employer",
        "Accounting firm / Ontario offices",
        "Safehaven / youth-service employers",
        "",
    ])
    def test_vague_rejected(self, name):
        assert employer_is_specific(name) is False

    @pytest.mark.parametrize("name", [
        "RBC", "Scotiabank", "TD Bank", "Ontario Power Generation",
        "IKO Industries Ltd.", "General Dynamics Land Systems",
        "Industrial and Commercial Bank of China (Canada)",
    ])
    def test_real_employers_accepted(self, name):
        assert employer_is_specific(name) is True


# --- Status vocabulary -------------------------------------------------------
class TestStatusNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("Live source", "active"),
        ("Verify before applying", "unverified"),
        ("Archived / watch next cycle", "archived"),
        ("Archived / past term", "archived"),
        ("Closed for 2026 / watch fall-winter", "archived"),
        ("2026 closed / watch next cycle", "archived"),
        ("Watchlist source", "watchlist"),
        ("No current student category openings in scan", "watchlist"),
    ])
    def test_all_eight_site_statuses_map(self, raw, expected):
        assert normalize_status(raw) == expected


# --- End-to-end static validation, on real records ---------------------------
class TestStaticValidation:
    def test_indeed_search_url_quarantined(self):
        record = {
            "title": "Logistics Co-op", "organization": "Uline",
            "url": "https://ca.indeed.com/q-uline-jobs-l-ontario-jobs.html",
            "type": "Internship / Co-op", "sourceType": "Indeed listing",
            "term": "Fall 2026", "status": "Verify before applying",
        }
        result = validate_static(record, TODAY)
        assert result.verdict == QUARANTINE
        assert any(f.code == "search_url_as_listing" for f in result.criticals)

    def test_same_url_as_declared_source_is_fine(self):
        # Identical URL, but honestly labelled as a search source, not an opening
        record = {
            "title": "Indeed — Internship Ontario",
            "organization": "Indeed",
            "url": "https://ca.indeed.com/q-internship-l-ontario-jobs.html",
            "type": "Public job board source", "sourceType": "Job board",
            "term": "Rolling", "status": "Live source",
        }
        result = validate_static(record, TODAY)
        assert result.verdict != QUARANTINE
        assert not any(f.code == "search_url_as_listing" for f in result.findings)

    def test_expired_term_quarantined(self):
        record = {
            "title": "Environmental Stewardship Co-op",
            "organization": "Niagara Parks Commission",
            "url": "https://www.eluta.ca/spl/environmental-stewardship-co-op-e66b7a",
            "type": "Summer Student / Co-op", "sourceType": "Eluta listing",
            "term": "Summer 2026", "status": "Archived / watch next cycle",
        }
        result = validate_static(record, TODAY)
        assert result.verdict == QUARANTINE
        assert any(f.code == "term_passed" for f in result.criticals)

    def test_vague_employer_quarantined(self):
        record = {
            "title": "Co-op / Intern — Master Planning & Logistics",
            "organization": "Automotive employer",
            "url": "https://ca.indeed.com/viewjob?jk=abc",
            "type": "Co-op / Intern", "sourceType": "Indeed listing",
            "term": "Fall 2026", "status": "Verify before applying",
        }
        result = validate_static(record, TODAY)
        assert result.verdict == QUARANTINE
        assert any(f.code == "vague_employer" for f in result.criticals)

    def test_clean_employer_posting_publishes(self):
        record = {
            "title": "Fire Safety Intern", "organization": "Arup",
            "url": "https://jobs.arup.com/jobs/fire-safety-intern-august-december-2026-32744",
            "type": "Internship", "sourceType": "Employer career posting",
            "term": "August–December 2026", "status": "Live source",
        }
        result = validate_static(record, TODAY)
        assert result.verdict == PUBLISH
        assert result.confidence == 100

    def test_archived_clean_record_gets_archive_not_publish(self):
        record = {
            "title": "GBM Investment Banking (Mining) Internship/Co-op",
            "organization": "Scotiabank",
            "url": "https://jobs.scotiabank.com/job/Toronto-GBM/604279917/",
            "type": "Internship / Co-op", "sourceType": "Employer career posting",
            "term": "Winter 2027", "status": "Archived / watch next cycle",
        }
        result = validate_static(record, TODAY)
        assert result.verdict == ARCHIVE, "archived data is sound but not a live opening"
        assert not result.criticals

    def test_bot_protected_flagged_as_unverifiable(self):
        record = {
            "title": "IT Technical Advisor I", "organization": "Intact",
            "url": "https://ca.linkedin.com/jobs/view/it-technical-advisor-4408693641",
            "type": "4-month Internship / Co-op", "sourceType": "LinkedIn listing",
            "term": "Fall 2026", "status": "Verify before applying",
        }
        result = validate_static(record, TODAY)
        assert any(f.code == "bot_protected_source" for f in result.findings)

    def test_missing_url_quarantined(self):
        result = validate_static({"title": "X", "organization": "Y", "url": ""}, TODAY)
        assert result.verdict == QUARANTINE

    def test_confidence_is_monotonic(self):
        clean = validate_static({
            "title": "Fire Safety Intern", "organization": "Arup",
            "url": "https://jobs.arup.com/jobs/fire-safety-intern-32744",
            "type": "Internship", "sourceType": "Employer career posting",
            "term": "Fall 2026", "status": "Live source"}, TODAY)
        messy = validate_static({
            "title": "Thing", "organization": "Automotive employer",
            "url": "https://ca.indeed.com/q-co-op-l-ontario-jobs.html",
            "type": "Co-op", "sourceType": "Indeed listing",
            "term": "Summer 2026", "status": "Verify before applying"}, TODAY)
        assert clean.confidence > messy.confidence


# --- Shared URL detection ----------------------------------------------------
class TestSharedUrls:
    def test_detects_two_records_one_url(self):
        records = [
            {"title": "A", "url": "https://jobs.rbc.com/featuredopportunities/x"},
            {"title": "B", "url": "https://jobs.rbc.com/featuredopportunities/x"},
            {"title": "C", "url": "https://jobs.rbc.com/other"},
        ]
        shared = find_shared_urls(records)
        assert len(shared) == 1
        assert set(shared["https://jobs.rbc.com/featuredopportunities/x"]) == {"A", "B"}


# --- Network tier constants --------------------------------------------------
class TestNetworkTier:
    def test_block_and_expiry_phrase_sets_are_disjoint(self):
        # An overlap would make a bot wall read as an expired posting, which is
        # exactly the confusion this module exists to prevent
        assert not (set(BLOCK_PHRASES) & set(EXPIRY_PHRASES))

    def test_outcome_vocabulary_distinguishes_blocked_from_dead(self):
        assert OUTCOME_BLOCKED != OUTCOME_DEAD != OUTCOME_LIVE


# --- Regression guard against the whole real dataset -------------------------
class TestRealDatasetRegression:
    @pytest.fixture
    def real_records(self):
        import json
        path = Path(__file__).resolve().parent.parent / "data" / "site_listings.json"
        if not path.exists():
            pytest.skip("site_listings.json not present")
        return json.loads(path.read_text())

    def test_every_record_gets_a_verdict(self, real_records):
        for record in real_records:
            result = validate_static(record, TODAY)
            assert result.verdict in (PUBLISH, ARCHIVE, REVIEW, QUARANTINE)
            assert 0 <= result.confidence <= 100

    def test_validator_finds_the_known_search_url_defects(self, real_records):
        results = [validate_static(r, TODAY) for r in real_records]
        hits = sum(1 for r in results
                   for f in r.findings if f.code == "search_url_as_listing")
        assert hits >= 10, "known search-URL defects in the real data must be caught"

    def test_no_record_crashes_the_validator(self, real_records):
        for record in real_records:
            validate_static(record, TODAY)  # must not raise
