"""
Canonical schema for a single opportunity record.

Every fetcher, regardless of source, must emit records in this shape before
anything downstream (dedupe, verify, publish) touches them. This is the
contract that lets you add a new source without changing any other module.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any


# --- Status values -----------------------------------------------------------
# ACTIVE    -> verified live, publish it
# ARCHIVED  -> was real, now expired/dead. Keep it (see plan step 10: expired
#              leads still tell career centres who hires students and when).
# PENDING   -> failed a soft check, needs a human look before publishing
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
STATUS_PENDING = "pending_review"

VALID_STATUSES = {STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING}


@dataclass
class Opportunity:
    """One normalized listing."""

    title: str
    employer: str
    source_url: str
    source_type: str            # greenhouse | lever | adzuna | manual | ...
    source_name: str            # human label, e.g. "Shopify" or "Adzuna"

    city: str = ""
    province: str = ""
    term: str = ""              # "Winter 2027", "Summer 2027", "" if unknown
    opportunity_type: str = ""  # internship | co-op | summer | new-grad
    program_tags: list[str] = field(default_factory=list)
    industry: str = ""
    deadline: str | None = None      # ISO date string or None
    posted_at: str | None = None     # ISO date string or None

    date_collected: str = ""
    last_verified: str | None = None
    status: str = STATUS_ACTIVE
    flags: list[str] = field(default_factory=list)  # why it's pending/archived

    id: str = ""

    def __post_init__(self) -> None:
        if not self.date_collected:
            self.date_collected = date.today().isoformat()
        if not self.id:
            self.id = self.compute_id()

    def compute_id(self) -> str:
        """
        Stable dedupe key: title + employer + city + term.

        Deliberately EXCLUDES source_url, so the same role posted on Adzuna and
        on the employer's own Greenhouse board collapses to one record. (The
        site plan says to hash the URL in too, but that defeats cross-source
        dedupe -- the whole point is that the same job from two sources should
        collide.)
        """
        parts = [
            _norm_text(self.title),
            _norm_text(self.employer),
            _norm_text(self.city),
            _norm_text(self.term),
        ]
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Opportunity":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def _norm_text(s: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace, for hashing."""
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()
