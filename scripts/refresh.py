#!/usr/bin/env python3
"""
Ontario Internship Finder -- 20-day refresh pipeline.

Run daily (via GitHub Actions). The 20-day gate inside decides whether to
actually do work.

    python scripts/refresh.py                 # normal gated run
    python scripts/refresh.py --force         # ignore the gate
    python scripts/refresh.py --dry-run       # fetch + process, don't publish
    python scripts/refresh.py --no-verify     # skip network verification
    python scripts/refresh.py --rollback      # restore previous dataset
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.timeutil import utcnow                                 # noqa: E402
from pipeline.dedupe import dedupe                                    # noqa: E402
from pipeline.fetchers import OK, fetch_source                        # noqa: E402
from pipeline.normalize import normalize_result                       # noqa: E402
from pipeline.schema import STATUS_ACTIVE, STATUS_PENDING             # noqa: E402
from pipeline.store import Store, merge_with_existing, prune_archive  # noqa: E402
from pipeline.verify import verify_all                                # noqa: E402
from pipeline.adapter import apply_validation, opportunity_to_record  # noqa: E402
from pipeline.validate import QUARANTINE, validate_static             # noqa: E402


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def load_sources(path: Path) -> list[dict]:
    config = json.loads(path.read_text())
    sources = config.get("sources", config)
    return [s for s in sources if s.get("enabled", True)]


def main() -> int:
    parser = argparse.ArgumentParser(description="20-day listing refresh")
    parser.add_argument("--force", action="store_true", help="ignore the 20-day gate")
    parser.add_argument("--dry-run", action="store_true", help="process but do not publish")
    parser.add_argument("--no-verify", action="store_true", help="skip network verification")
    parser.add_argument("--rollback", action="store_true", help="restore previous dataset")
    parser.add_argument("--interval", type=int, default=20, help="refresh interval in days")
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "sources.json"))
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("refresh")
    store = Store(args.data_dir)

    if args.rollback:
        ok = store.rollback()
        log.info("rollback %s", "succeeded" if ok else "failed -- no backup found")
        return 0 if ok else 1

    # --- the gate ------------------------------------------------------------
    state = store.load_state()
    should_run, reason = store.should_refresh(state, args.interval)

    if not should_run and not args.force:
        log.info("SKIP: %s", reason)
        return 0
    log.info("RUN: %s%s", reason, " [forced]" if args.force else "")

    # --- fetch, one source at a time, isolated -------------------------------
    sources = load_sources(Path(args.config))
    log.info("loaded %d enabled sources", len(sources))

    state.setdefault("sources", {})
    all_records = []
    failed_sources = []

    for source in sources:
        name = source["name"]
        result = fetch_source(source)

        health = state["sources"].setdefault(name, {})
        health["last_attempt"] = utcnow().isoformat(timespec="seconds")
        health["last_outcome"] = result.outcome

        if not result.trustworthy:
            # Critical: a failed source keeps its previous data (via the merge
            # step below) instead of silently vanishing from the site.
            log.warning("source %s FAILED (%s): %s", name, result.outcome, result.detail)
            health["needs_review"] = True
            health["last_error"] = result.detail
            failed_sources.append(name)
            continue

        records = normalize_result(result)
        health["needs_review"] = False
        health["last_success"] = health["last_attempt"]
        health["last_raw_count"] = len(result.raw_records)
        health["last_matched_count"] = len(records)
        health.pop("last_error", None)

        # An OK fetch that yields zero raw records is meaningfully different
        # from an OK fetch yielding 500 raw records and zero student roles.
        if result.outcome == OK and not result.raw_records:
            log.warning("source %s returned 0 raw postings -- verify the board token", name)
            health["needs_review"] = True
            health["last_error"] = "board returned zero postings"

        all_records.extend(records)

    if failed_sources and len(failed_sources) == len(sources):
        log.error("ALL %d sources failed -- aborting without publishing", len(sources))
        store.save_state(state)
        return 1

    log.info("collected %d student records across %d sources",
             len(all_records), len(sources) - len(failed_sources))

    # --- process -------------------------------------------------------------
    deduped = dedupe(all_records)

    # Task 2 static validation runs ALWAYS -- it costs no network and catches
    # the defects (search-page URLs, expired terms, category employers) that
    # liveness checking structurally cannot find.
    quarantined = 0
    for opp in deduped:
        result = validate_static(opportunity_to_record(opp))
        apply_validation(opp, result)
        if result.verdict == QUARANTINE:
            quarantined += 1
            log.warning("QUARANTINE %s -- %s", opp.title[:50],
                        "; ".join(f.code for f in result.criticals))
    log.info("static validation: %d quarantined of %d", quarantined, len(deduped))

    if args.no_verify:
        log.info("skipping network verification (--no-verify)")
        verified = deduped
    else:
        # Only probe records that passed static validation
        publishable = [o for o in deduped if not any(
            f.startswith("critical:") for f in o.flags)]
        skipped = len(deduped) - len(publishable)
        if skipped:
            log.info("skipping network checks for %d quarantined records", skipped)
        verify_all(publishable)
        verified = deduped

    existing = store.load_dataset()
    merged = merge_with_existing(verified, existing)
    merged = prune_archive(merged)

    active = sum(1 for r in merged if r.status == STATUS_ACTIVE)
    pending = sum(1 for r in merged if r.status == STATUS_PENDING)

    # --- sanity gate before publishing --------------------------------------
    prior_active = sum(1 for r in existing if r.status == STATUS_ACTIVE)
    if prior_active >= 10 and active < prior_active * 0.4:
        log.error(
            "REFUSING TO PUBLISH: active listings collapsed %d -> %d (>60%% drop). "
            "This usually means an API changed shape or verification is producing "
            "false positives. Inspect before re-running, or use --force after fixing.",
            prior_active, active,
        )
        state["last_aborted_refresh"] = utcnow().isoformat(timespec="seconds")
        store.save_state(state)
        return 1

    if args.dry_run:
        log.info("DRY RUN -- would publish %d records (%d active, %d pending)",
                 len(merged), active, pending)
        return 0

    # --- publish -------------------------------------------------------------
    state["last_successful_refresh"] = utcnow().isoformat(timespec="seconds")
    state.setdefault("history", []).append({
        "at": state["last_successful_refresh"],
        "active": active,
        "pending": pending,
        "total": len(merged),
        "failed_sources": failed_sources,
    })
    state["history"] = state["history"][-24:]  # keep ~1 year of cycles

    store.publish(merged, state)
    store.save_state(state)

    log.info("DONE: %d active, %d pending review, %d total", active, pending, len(merged))
    if failed_sources:
        log.warning("sources needing review: %s", ", ".join(failed_sources))
    return 0


if __name__ == "__main__":
    sys.exit(main())
