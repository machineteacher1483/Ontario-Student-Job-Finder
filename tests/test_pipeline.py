"""
Test suite. Runs with no network access -- every source response is a fixture.

This matters practically: you want to be able to test the pipeline logic on a
plane, in a lab with a locked-down network, or in CI without burning API quota.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.classify import (detect_opportunity_type, infer_term, is_ontario,
                               parse_location, tag_programs)
from pipeline.dedupe import dedupe
from pipeline.timeutil import utcnow
from pipeline.fetchers import BAD_PAYLOAD, NOT_FOUND, OK, FetchResult
from pipeline.normalize import normalize_result
from pipeline.schema import (STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING,
                             Opportunity)
from pipeline.store import Store, merge_with_existing, prune_archive
from pipeline.verify import check_deadline, check_expiry_text, is_trusted_domain, verify_record


# --- Fixtures ----------------------------------------------------------------
GREENHOUSE_PAYLOAD = [
    {
        "id": 1001,
        "title": "Software Engineering Intern (Summer 2027)",
        "location": {"name": "Toronto, ON"},
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1001",
        "updated_at": "2026-08-01T10:00:00-04:00",
        "content": "Join our cloud platform team. Python, AWS.",
    },
    {
        "id": 1002,
        "title": "Senior Staff Engineer",           # not a student role
        "location": {"name": "Toronto, ON"},
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1002",
        "content": "10+ years experience required.",
    },
    {
        "id": 1003,
        "title": "Marketing Intern",                # not Ontario
        "location": {"name": "Vancouver, BC"},
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1003",
        "content": "Social media and brand work.",
    },
]

LEVER_PAYLOAD = [
    {
        "text": "Data Analyst Co-op",
        "categories": {"location": "Ottawa, Ontario", "team": "Analytics"},
        "hostedUrl": "https://jobs.lever.co/beta/2001",
        "descriptionPlain": "SQL and dashboards for the winter 2027 term.",
        "createdAt": 1754006400000,
    },
]

ADZUNA_PAYLOAD = [
    {
        "id": "3001",
        "title": "Software Engineering Intern (Summer 2027)",   # dupe of GH 1001
        "location": {"display_name": "Toronto, Ontario"},
        "company": {"display_name": "Acme"},
        "redirect_url": "https://adzuna.ca/jobs/land/ad/3001",
        "description": "Cloud platform internship.",
        "created": "2026-08-02T09:00:00Z",
        "category": {"label": "IT Jobs"},
    },
]


# --- Classification ----------------------------------------------------------
class TestClassification:
    @pytest.mark.parametrize("raw,expected", [
        ("Toronto, ON", True),
        ("Ottawa, Ontario, Canada", True),
        ("Waterloo", True),
        ("Remote - Canada", True),
        ("Vancouver, BC", False),
        ("New York, NY", False),
        ("Remote - United States", False),
        ("", False),
    ])
    def test_is_ontario(self, raw, expected):
        assert is_ontario(raw) is expected

    def test_on_word_not_confused_with_province(self):
        # The English word "on" must not be read as the ON province code
        assert is_ontario("Hands on Laboratory, Denver") is False

    @pytest.mark.parametrize("raw,city,prov", [
        ("Toronto, ON", "Toronto", "ON"),
        ("Ottawa, Ontario, Canada", "Ottawa", "ON"),
        ("Kitchener", "Kitchener", ""),
    ])
    def test_parse_location(self, raw, city, prov):
        assert parse_location(raw) == (city, prov)

    @pytest.mark.parametrize("title,expected", [
        ("Software Engineering Intern", "internship"),
        ("Data Analyst Co-op", "co-op"),
        ("Summer Student - Finance", "summer"),
        ("New Grad Software Engineer", "new-grad"),
        ("Senior Engineering Manager", None),
        ("Director of Internal Audit", None),
    ])
    def test_detect_opportunity_type(self, title, expected):
        assert detect_opportunity_type(title) == expected

    def test_tag_programs_multi(self):
        tags = tag_programs("Supply Chain Data Analyst Intern")
        assert "Logistics/Business" in tags
        assert "CS/IT/Data" in tags

    def test_infer_term(self):
        assert infer_term("SWE Intern Summer 2027") == "Summer 2027"
        assert infer_term("Co-op (Winter 2027 term)") == "Winter 2027"
        assert infer_term("Generic Intern") == ""


# --- Normalization -----------------------------------------------------------
class TestNormalization:
    def test_greenhouse_filters_correctly(self):
        result = FetchResult("Acme", "greenhouse", OK, raw_records=GREENHOUSE_PAYLOAD)
        records = normalize_result(result)
        # 3 raw -> 1 kept (senior role dropped, BC role dropped)
        assert len(records) == 1
        assert records[0].title.startswith("Software Engineering Intern")
        assert records[0].city == "Toronto"
        assert records[0].province == "ON"
        assert records[0].opportunity_type == "internship"
        assert "CS/IT/Data" in records[0].program_tags

    def test_lever_bare_array_shape(self):
        result = FetchResult("Beta", "lever", OK, raw_records=LEVER_PAYLOAD)
        records = normalize_result(result)
        assert len(records) == 1
        assert records[0].opportunity_type == "co-op"
        assert records[0].city == "Ottawa"

    def test_malformed_record_skipped_not_fatal(self):
        payload = [{"garbage": True}, GREENHOUSE_PAYLOAD[0]]
        result = FetchResult("Acme", "greenhouse", OK, raw_records=payload)
        records = normalize_result(result)
        assert len(records) == 1  # bad one skipped, good one survives


# --- The ambiguous-empty-result trap ----------------------------------------
class TestFetchOutcomes:
    def test_404_is_not_trustworthy(self):
        assert FetchResult("X", "greenhouse", NOT_FOUND).trustworthy is False

    def test_ok_empty_is_trustworthy(self):
        # Genuinely zero jobs IS a valid answer -- unlike a 404
        assert FetchResult("X", "greenhouse", OK, raw_records=[]).trustworthy is True

    def test_bad_payload_not_trustworthy(self):
        assert FetchResult("X", "lever", BAD_PAYLOAD).trustworthy is False


# --- Dedupe ------------------------------------------------------------------
class TestDedupe:
    def test_cross_source_dupe_collapses_to_employer_link(self):
        gh = normalize_result(FetchResult("Acme", "greenhouse", OK,
                                          raw_records=[GREENHOUSE_PAYLOAD[0]]))
        adz = normalize_result(FetchResult("Adzuna", "adzuna", OK,
                                           raw_records=ADZUNA_PAYLOAD))
        assert len(gh) == 1 and len(adz) == 1
        assert gh[0].id == adz[0].id, "same role from two sources must hash equal"

        merged = dedupe(gh + adz)
        assert len(merged) == 1
        assert "greenhouse" in merged[0].source_url, "employer link must win"
        assert any(f.startswith("also_listed_on") for f in merged[0].flags)

    def test_distinct_roles_not_collapsed(self):
        a = Opportunity("SWE Intern", "Acme", "https://a.com", "greenhouse", "Acme", city="Toronto")
        b = Opportunity("SWE Intern", "Beta", "https://b.com", "greenhouse", "Beta", city="Toronto")
        assert len(dedupe([a, b])) == 2

    def test_dedupe_backfills_blank_fields(self):
        rich = Opportunity("SWE Intern", "Acme", "https://adzuna.ca/x", "adzuna", "Adzuna",
                           city="Toronto", industry="IT Jobs")
        sparse = Opportunity("SWE Intern", "Acme", "https://greenhouse.io/x", "greenhouse",
                             "Acme", city="Toronto")
        merged = dedupe([rich, sparse])
        assert len(merged) == 1
        assert merged[0].source_type == "greenhouse"      # employer wins
        assert merged[0].industry == "IT Jobs"            # but keeps adzuna's data


# --- Verification ------------------------------------------------------------
class TestVerification:
    def test_deadline_passed(self):
        past = (date.today() - timedelta(days=5)).isoformat()
        rec = Opportunity("X", "Y", "https://z.com", "greenhouse", "Y", deadline=past)
        assert check_deadline(rec) is True

    def test_deadline_future(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        rec = Opportunity("X", "Y", "https://z.com", "greenhouse", "Y", deadline=future)
        assert check_deadline(rec) is False

    def test_expired_page_text_detected(self):
        body = "<html><body><h1>We are no longer accepting applications</h1></body></html>"
        assert check_expiry_text(body) is not None

    def test_live_page_text_clean(self):
        body = "<html><body><h1>Apply now for our summer internship</h1></body></html>"
        assert check_expiry_text(body) is None

    @pytest.mark.parametrize("url,trusted", [
        ("https://job-boards.greenhouse.io/acme/jobs/1", True),
        ("https://jobs.lever.co/beta/2", True),
        ("https://acme.wd3.myworkdayjobs.com/careers", True),
        ("https://sketchy-jobs-xyz.ru/apply", False),
    ])
    def test_domain_trust(self, url, trusted):
        assert is_trusted_domain(url) is trusted

    def test_deadline_archives_before_any_network_call(self):
        past = (date.today() - timedelta(days=1)).isoformat()
        rec = Opportunity("X", "Y", "https://nonexistent.invalid", "greenhouse", "Y",
                          deadline=past)
        # No session passed -- if this tried the network it would hang/fail
        out = verify_record(rec)
        assert out.status == STATUS_ARCHIVED
        assert "deadline_passed" in out.flags

    def test_missing_fields_go_to_pending(self):
        rec = Opportunity("", "Y", "https://x.com", "greenhouse", "Y")
        rec.title = ""
        out = verify_record(rec)
        assert out.status == STATUS_PENDING


# --- The 20-day gate ---------------------------------------------------------
class TestRefreshGate:
    def test_first_run_always_refreshes(self, tmp_path):
        store = Store(tmp_path)
        should, reason = store.should_refresh({"last_successful_refresh": None})
        assert should is True
        assert "first run" in reason

    def test_gate_closed_at_19_days(self, tmp_path):
        store = Store(tmp_path)
        recent = (utcnow() - timedelta(days=19)).isoformat()
        should, _ = store.should_refresh({"last_successful_refresh": recent})
        assert should is False

    def test_gate_opens_at_20_days(self, tmp_path):
        store = Store(tmp_path)
        old = (utcnow() - timedelta(days=20)).isoformat()
        should, _ = store.should_refresh({"last_successful_refresh": old})
        assert should is True

    def test_corrupt_state_treated_as_first_run(self, tmp_path):
        store = Store(tmp_path)
        store.state_path.write_text("{ not valid json")
        state = store.load_state()
        assert state["last_successful_refresh"] is None


# --- Merge / archive ---------------------------------------------------------
class TestMerge:
    def test_disappeared_listing_archived_not_deleted(self):
        old = Opportunity("Gone Intern", "Acme", "https://a.com/1", "greenhouse", "Acme",
                          city="Toronto", status=STATUS_ACTIVE)
        fresh = [Opportunity("New Intern", "Acme", "https://a.com/2", "greenhouse", "Acme",
                             city="Toronto")]
        merged = merge_with_existing(fresh, [old])
        assert len(merged) == 2
        gone = next(r for r in merged if r.title == "Gone Intern")
        assert gone.status == STATUS_ARCHIVED
        assert "disappeared_from_source" in gone.flags

    def test_returning_listing_keeps_original_collection_date(self):
        old = Opportunity("SWE Intern", "Acme", "https://a.com/1", "greenhouse", "Acme",
                          city="Toronto", date_collected="2026-01-15")
        fresh = Opportunity("SWE Intern", "Acme", "https://a.com/1", "greenhouse", "Acme",
                            city="Toronto", date_collected="2026-09-09")
        merged = merge_with_existing([fresh], [old])
        assert len(merged) == 1
        assert merged[0].date_collected == "2026-01-15"

    def test_prune_drops_stale_archive_only(self):
        stale = Opportunity("Old", "A", "https://a.com", "greenhouse", "A",
                            date_collected="2020-01-01", status=STATUS_ARCHIVED)
        recent_archive = Opportunity("Recent", "A", "https://b.com", "greenhouse", "A",
                                     date_collected=date.today().isoformat(),
                                     status=STATUS_ARCHIVED)
        active_old = Opportunity("Active", "A", "https://c.com", "greenhouse", "A",
                                 date_collected="2020-01-01", status=STATUS_ACTIVE)
        out = prune_archive([stale, recent_archive, active_old])
        titles = {r.title for r in out}
        assert "Old" not in titles
        assert "Recent" in titles
        assert "Active" in titles  # active records are never pruned by age


# --- Publish / rollback ------------------------------------------------------
class TestStore:
    def test_publish_and_rollback(self, tmp_path):
        store = Store(tmp_path)
        v1 = [Opportunity("V1 Intern", "A", "https://a.com", "greenhouse", "A", city="Toronto")]
        store.publish(v1, {"sources": {}})

        v2 = [Opportunity("V2 Intern", "B", "https://b.com", "greenhouse", "B", city="Ottawa")]
        store.publish(v2, {"sources": {}})

        current = json.loads(store.dataset_path.read_text())
        assert current["opportunities"][0]["title"] == "V2 Intern"

        assert store.rollback() is True
        restored = json.loads(store.dataset_path.read_text())
        assert restored["opportunities"][0]["title"] == "V1 Intern"

    def test_published_payload_shape(self, tmp_path):
        store = Store(tmp_path)
        records = [
            Opportunity("A", "X", "https://a.com", "greenhouse", "X", status=STATUS_ACTIVE),
            Opportunity("B", "Y", "https://b.com", "greenhouse", "Y", status=STATUS_ARCHIVED),
        ]
        store.publish(records, {"sources": {"X": {"last_outcome": "ok"}}})
        payload = json.loads(store.dataset_path.read_text())
        assert payload["counts"]["active"] == 1
        assert payload["counts"]["archived"] == 1
        assert payload["refresh_interval_days"] == 20
        assert "generated_at" in payload
        assert payload["source_health"]["X"]["last_outcome"] == "ok"
