#!/usr/bin/env python3
"""
Preflight check for the source registry.

Run this BEFORE enabling a new source, and any time listings look wrong.
It answers the question the main pipeline deliberately refuses to guess at:
is this board token actually correct?

    python scripts/check_sources.py
    python scripts/check_sources.py --include-disabled

Exit code 1 if any enabled source fails, so you can wire it into CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.fetchers import OK, fetch_source          # noqa: E402
from pipeline.normalize import normalize_result         # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the source registry")
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "sources.json"))
    parser.add_argument("--include-disabled", action="store_true")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text())
    sources = config.get("sources", config)
    if not args.include_disabled:
        sources = [s for s in sources if s.get("enabled", True)]

    print(f"\nChecking {len(sources)} sources from {args.config}\n")
    print(f"{'SOURCE':<18}{'TYPE':<12}{'RESULT':<16}{'RAW':>6}{'STUDENT':>9}  DETAIL")
    print("-" * 86)

    failures = 0
    warnings = 0

    for source in sources:
        name = source.get("name", "?")
        stype = source.get("type", "?")
        result = fetch_source(source)

        if result.outcome != OK:
            failures += 1
            print(f"{name:<18}{stype:<12}{RED}{'FAIL':<16}{RESET}{'-':>6}{'-':>9}  "
                  f"{result.outcome}: {result.detail}")
            continue

        raw = len(result.raw_records)
        matched = len(normalize_result(result))

        if raw == 0:
            warnings += 1
            status, colour = "EMPTY", YELLOW
            detail = "board reachable but returned 0 postings -- check the token"
        elif matched == 0:
            warnings += 1
            status, colour = "NO STUDENT", YELLOW
            detail = "postings found, but none are Ontario student roles"
        else:
            status, colour = "OK", GREEN
            detail = ""

        print(f"{name:<18}{stype:<12}{colour}{status:<16}{RESET}{raw:>6}{matched:>9}  {DIM}{detail}{RESET}")

    print("-" * 86)
    print(f"\n{failures} failed, {warnings} warnings, "
          f"{len(sources) - failures - warnings} healthy\n")

    if failures:
        print(f"{RED}Fix failing sources before running the pipeline.{RESET}")
        print(f"{DIM}A 404 almost always means a wrong board token, or the company "
              f"moved to a different ATS.{RESET}\n")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
