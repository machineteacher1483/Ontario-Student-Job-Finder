"""
Normalization: raw source payload -> canonical Opportunity records.

One function per source type. Everything downstream is source-agnostic, so
adding a fifth ATS next semester means writing one function here and one
fetcher -- nothing else changes.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime
from typing import Any

from .timeutil import from_epoch_ms
from .classify import detect_opportunity_type, infer_term, is_ontario, parse_location, tag_programs
from .fetchers import FetchResult
from .schema import Opportunity

log = logging.getLogger(__name__)


def _strip_html(raw: str, limit: int = 4000) -> str:
    """Crude tag strip -- enough for keyword matching, not for display."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _iso(value: Any) -> str | None:
    """Coerce assorted timestamp formats to a plain ISO date string."""
    if not value:
        return None
    if isinstance(value, (int, float)):  # Lever uses epoch milliseconds
        try:
            return from_epoch_ms(value).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return None
    return None


# --- Per-source normalizers --------------------------------------------------
def normalize_greenhouse(raw: dict, source_name: str) -> Opportunity | None:
    title = raw.get("title", "").strip()
    location_raw = (raw.get("location") or {}).get("name", "")
    description = _strip_html(raw.get("content", ""))

    otype = detect_opportunity_type(title, description)
    if not otype or not is_ontario(location_raw):
        return None

    city, province = parse_location(location_raw)
    return Opportunity(
        title=title,
        employer=source_name,
        source_url=raw.get("absolute_url", ""),
        source_type="greenhouse",
        source_name=source_name,
        city=city,
        province=province or "ON",
        term=infer_term(title, description, date.today().year),
        opportunity_type=otype,
        program_tags=tag_programs(title, description),
        posted_at=_iso(raw.get("updated_at") or raw.get("first_published")),
    )


def normalize_lever(raw: dict, source_name: str) -> Opportunity | None:
    title = raw.get("text", "").strip()
    categories = raw.get("categories") or {}
    location_raw = categories.get("location", "")
    description = _strip_html(raw.get("descriptionPlain") or raw.get("description", ""))

    otype = detect_opportunity_type(title, description)
    if not otype or not is_ontario(location_raw):
        return None

    city, province = parse_location(location_raw)
    return Opportunity(
        title=title,
        employer=source_name,
        source_url=raw.get("hostedUrl") or raw.get("applyUrl", ""),
        source_type="lever",
        source_name=source_name,
        city=city,
        province=province or "ON",
        term=infer_term(title, description, date.today().year),
        opportunity_type=otype,
        program_tags=tag_programs(title, description),
        industry=categories.get("team", ""),
        posted_at=_iso(raw.get("createdAt")),
    )


def normalize_ashby(raw: dict, source_name: str) -> Opportunity | None:
    title = raw.get("title", "").strip()
    location_raw = raw.get("location", "")
    description = _strip_html(raw.get("descriptionPlain") or raw.get("descriptionHtml", ""))

    otype = detect_opportunity_type(title, description)
    if not otype or not is_ontario(location_raw):
        return None

    city, province = parse_location(location_raw)
    return Opportunity(
        title=title,
        employer=source_name,
        source_url=raw.get("jobUrl") or raw.get("applyUrl", ""),
        source_type="ashby",
        source_name=source_name,
        city=city,
        province=province or "ON",
        term=infer_term(title, description, date.today().year),
        opportunity_type=otype,
        program_tags=tag_programs(title, description),
        industry=raw.get("department", ""),
        posted_at=_iso(raw.get("publishedAt")),
    )


def normalize_adzuna(raw: dict, source_name: str) -> Opportunity | None:
    title = raw.get("title", "").strip()
    location_raw = (raw.get("location") or {}).get("display_name", "")
    description = _strip_html(raw.get("description", ""))
    employer = (raw.get("company") or {}).get("display_name", "").strip()

    otype = detect_opportunity_type(title, description)
    if not otype or not is_ontario(location_raw) or not employer:
        return None

    city, province = parse_location(location_raw)
    return Opportunity(
        title=title,
        employer=employer,
        source_url=raw.get("redirect_url", ""),
        source_type="adzuna",
        source_name=source_name,
        city=city,
        province=province or "ON",
        term=infer_term(title, description, date.today().year),
        opportunity_type=otype,
        program_tags=tag_programs(title, description),
        industry=(raw.get("category") or {}).get("label", ""),
        posted_at=_iso(raw.get("created")),
    )


NORMALIZERS = {
    "greenhouse": normalize_greenhouse,
    "lever": normalize_lever,
    "ashby": normalize_ashby,
    "adzuna": normalize_adzuna,
}


def normalize_result(result: FetchResult) -> list[Opportunity]:
    """
    Turn one FetchResult into canonical records, dropping anything that isn't
    an Ontario student role. A single malformed record is skipped, not fatal.
    """
    normalizer = NORMALIZERS.get(result.source_type)
    if normalizer is None:
        log.warning("no normalizer for source type %s", result.source_type)
        return []

    out: list[Opportunity] = []
    for raw in result.raw_records:
        try:
            record = normalizer(raw, result.source_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping malformed record from %s: %s", result.source_name, exc)
            continue
        if record and record.title and record.source_url:
            out.append(record)

    log.info("%s: %d raw -> %d Ontario student roles",
             result.source_name, len(result.raw_records), len(out))
    return out
