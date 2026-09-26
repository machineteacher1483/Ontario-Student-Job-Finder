"""
Adapter between the canonical Opportunity schema (Task 1 pipeline) and the
site's existing record shape (Task 2 validator, and the current HTML).

Keeping the translation in one file means the validator can run against both
freshly-fetched API records and the site's legacy hand-curated data without
either side knowing about the other.
"""

from __future__ import annotations

from .schema import (STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING,
                     Opportunity)
from .validate import ARCHIVE, PUBLISH, QUARANTINE, REVIEW

# How a validation verdict maps onto a publishable status
VERDICT_TO_STATUS = {
    PUBLISH: STATUS_ACTIVE,
    ARCHIVE: STATUS_ARCHIVED,
    REVIEW: STATUS_PENDING,
    QUARANTINE: STATUS_PENDING,   # never deleted, always reviewable
}


def opportunity_to_record(opp: Opportunity) -> dict:
    """Canonical Opportunity -> the site's record shape, for validation."""
    return {
        "id": opp.id,
        "title": opp.title,
        "organization": opp.employer,
        "city": opp.city,
        "region": opp.province,
        "industry": opp.industry,
        "program": ", ".join(opp.program_tags),
        "type": opp.opportunity_type,
        "term": opp.term,
        "status": opp.status,
        "sourceType": opp.source_type,
        "notes": "",
        "url": opp.source_url,
    }


def record_to_opportunity(record: dict) -> Opportunity:
    """The site's record shape -> canonical Opportunity."""
    programs = [p.strip() for p in (record.get("program") or "").split(",") if p.strip()]
    return Opportunity(
        title=record.get("title", ""),
        employer=record.get("organization", ""),
        source_url=record.get("url", ""),
        source_type=record.get("sourceType", "manual"),
        source_name=record.get("organization", ""),
        city=record.get("city", ""),
        province=record.get("region", ""),
        term=record.get("term", ""),
        opportunity_type=record.get("type", ""),
        program_tags=programs,
        industry=record.get("industry", ""),
    )


def apply_validation(opp: Opportunity, result) -> Opportunity:
    """Write a ValidationResult's verdict and findings back onto an Opportunity."""
    opp.status = VERDICT_TO_STATUS.get(result.verdict, STATUS_PENDING)
    opp.last_verified = result.to_dict().get("checked_at")
    for finding in result.findings:
        if finding.severity != "info":
            tag = f"{finding.severity}:{finding.code}"
            if tag not in opp.flags:
                opp.flags.append(tag)
    opp.flags.append(f"verdict:{result.verdict}")
    opp.flags.append(f"confidence:{result.confidence}")
    return opp
