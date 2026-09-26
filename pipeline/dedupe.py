"""
Deduplication.

The same role legitimately appears on several sources: the employer's own
Greenhouse board, Adzuna, and possibly a school board. Students should see it
once, and the apply link should point at the employer -- aggregator redirect
links expire faster, sometimes add tracking hops, and occasionally land on a
"job no longer available" interstitial even while the real posting is live.

Precedence (highest wins): employer ATS > aggregator > manual submission.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from .schema import Opportunity

log = logging.getLogger(__name__)

# Higher number = preferred apply link
SOURCE_PRECEDENCE = {
    "greenhouse": 100,
    "lever": 100,
    "ashby": 100,
    "workday": 95,
    "manual": 50,      # verified SME submission -- trusted, but often no ATS
    "adzuna": 20,
    "jsearch": 20,
}

DEFAULT_PRECEDENCE = 10


def _rank(record: Opportunity) -> int:
    return SOURCE_PRECEDENCE.get(record.source_type, DEFAULT_PRECEDENCE)


def dedupe(records: list[Opportunity]) -> list[Opportunity]:
    """
    Collapse records sharing a dedupe id, keeping the highest-precedence one
    and merging the useful fields off the losers.
    """
    buckets: dict[str, list[Opportunity]] = defaultdict(list)
    for record in records:
        buckets[record.id].append(record)

    merged: list[Opportunity] = []
    collapsed = 0

    for _key, group in buckets.items():
        if len(group) == 1:
            merged.append(group[0])
            continue

        collapsed += len(group) - 1
        group.sort(key=_rank, reverse=True)
        winner, losers = group[0], group[1:]

        # Union the program tags -- one source may tag richer than another
        tags = list(winner.program_tags)
        for loser in losers:
            for tag in loser.program_tags:
                if tag not in tags:
                    tags.append(tag)
        winner.program_tags = tags

        # Backfill blanks from losers rather than discarding their data
        for field_name in ("industry", "term", "city", "province", "deadline", "posted_at"):
            if not getattr(winner, field_name):
                for loser in losers:
                    value = getattr(loser, field_name)
                    if value:
                        setattr(winner, field_name, value)
                        break

        others = sorted({l.source_name for l in losers})
        if others:
            winner.flags.append(f"also_listed_on:{','.join(others)}")

        merged.append(winner)

    log.info("dedupe: %d records -> %d unique (%d collapsed)",
             len(records), len(merged), collapsed)
    return merged
