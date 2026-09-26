"""
State + dataset persistence.

Two files:
  data/state.json         -- last_successful_refresh, per-source health
  data/opportunities.json -- the published dataset the website reads

The 20-day gate lives here. Note the design: GitHub Actions runs this DAILY,
and the gate decides whether to actually do work. Cron cannot express "every
20 days" (it has no notion of an interval from an arbitrary start date -- only
fixed calendar points), and a monthly cron would drift off the 20-day cycle.
Daily-run-plus-gate also self-heals: if one day's run fails, tomorrow's picks
it up instead of the whole cycle being missed.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import date, datetime, timedelta

from .timeutil import utcnow
from pathlib import Path
from typing import Any

from .schema import STATUS_ACTIVE, STATUS_ARCHIVED, Opportunity

log = logging.getLogger(__name__)

REFRESH_INTERVAL_DAYS = 20


class Store:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.dataset_path = self.data_dir / "opportunities.json"
        self.backup_path = self.data_dir / "opportunities.prev.json"

    # --- state ---------------------------------------------------------------
    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"last_successful_refresh": None, "sources": {}, "history": []}
        try:
            return json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.error("state.json unreadable (%s) -- treating as first run", exc)
            return {"last_successful_refresh": None, "sources": {}, "history": []}

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=2, sort_keys=True))

    def days_since_refresh(self, state: dict[str, Any] | None = None) -> int | None:
        state = state if state is not None else self.load_state()
        last = state.get("last_successful_refresh")
        if not last:
            return None
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return None
        return (utcnow() - last_dt).days

    def should_refresh(self, state: dict[str, Any] | None = None,
                       interval_days: int = REFRESH_INTERVAL_DAYS) -> tuple[bool, str]:
        """The gate. Returns (should_run, human-readable reason)."""
        days = self.days_since_refresh(state)
        if days is None:
            return True, "no previous successful refresh recorded (first run)"
        if days >= interval_days:
            return True, f"{days} days since last refresh (>= {interval_days})"
        return False, f"only {days} days since last refresh (< {interval_days})"

    # --- dataset -------------------------------------------------------------
    def load_dataset(self) -> list[Opportunity]:
        if not self.dataset_path.exists():
            return []
        try:
            payload = json.loads(self.dataset_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.error("opportunities.json unreadable: %s", exc)
            return []
        records = payload.get("opportunities", payload) if isinstance(payload, dict) else payload
        return [Opportunity.from_dict(r) for r in records]

    def publish(self, records: list[Opportunity], state: dict[str, Any]) -> None:
        """
        Write the dataset, backing up the previous version first.

        The backup is the rollback path: if a refresh publishes garbage (bad
        API response shape, mass false-positive archiving), you restore
        opportunities.prev.json rather than digging through git history under
        pressure.
        """
        if self.dataset_path.exists():
            shutil.copy2(self.dataset_path, self.backup_path)

        active = [r for r in records if r.status == STATUS_ACTIVE]
        payload = {
            "generated_at": utcnow().isoformat(timespec="seconds"),
            "refresh_interval_days": REFRESH_INTERVAL_DAYS,
            "counts": {
                "total": len(records),
                "active": len(active),
                "archived": sum(1 for r in records if r.status == STATUS_ARCHIVED),
                "pending_review": sum(1 for r in records
                                      if r.status not in (STATUS_ACTIVE, STATUS_ARCHIVED)),
            },
            "source_health": state.get("sources", {}),
            "opportunities": [r.to_dict() for r in records],
        }
        self.dataset_path.write_text(json.dumps(payload, indent=2, sort_keys=False))
        log.info("published %d records (%d active) to %s",
                 len(records), len(active), self.dataset_path)

    def rollback(self) -> bool:
        """Restore the previous dataset. Returns False if no backup exists."""
        if not self.backup_path.exists():
            return False
        shutil.copy2(self.backup_path, self.dataset_path)
        log.warning("rolled back dataset from %s", self.backup_path)
        return True


def merge_with_existing(fresh: list[Opportunity],
                        existing: list[Opportunity]) -> list[Opportunity]:
    """
    Combine this cycle's records with what we already had.

    Rules:
      - a record found again this cycle takes the fresh version, but inherits
        its original date_collected (so "posted 3 months ago" stays true)
      - a record NOT found this cycle is archived, not deleted -- per plan
        step 10, expired leads reveal which employers hire students and when
        the next intake opens
    """
    fresh_by_id = {r.id: r for r in fresh}
    merged: list[Opportunity] = []

    for record in fresh:
        prior = next((e for e in existing if e.id == record.id), None)
        if prior:
            record.date_collected = prior.date_collected or record.date_collected
        merged.append(record)

    for old in existing:
        if old.id in fresh_by_id:
            continue
        if old.status != STATUS_ARCHIVED:
            old.status = STATUS_ARCHIVED
            if "disappeared_from_source" not in old.flags:
                old.flags.append("disappeared_from_source")
        merged.append(old)

    return merged


def prune_archive(records: list[Opportunity], keep_days: int = 400) -> list[Opportunity]:
    """
    Drop archived records older than keep_days. 400 days keeps a full annual
    hiring cycle plus a month of margin, so career centres can still see last
    year's intake timing.
    """
    cutoff = date.today() - timedelta(days=keep_days)
    out = []
    for record in records:
        if record.status != STATUS_ARCHIVED:
            out.append(record)
            continue
        try:
            collected = datetime.fromisoformat(record.date_collected).date()
        except (ValueError, TypeError):
            out.append(record)
            continue
        if collected >= cutoff:
            out.append(record)
    return out
