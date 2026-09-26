"""Timezone-aware UTC helpers. datetime.utcnow() is deprecated as of 3.12."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC datetime, for consistent ISO strings across the codebase."""
    return datetime.now(UTC).replace(tzinfo=None)


def from_epoch_ms(ms: float) -> datetime:
    """Lever and some ATS platforms emit epoch milliseconds."""
    return datetime.fromtimestamp(ms / 1000, UTC).replace(tzinfo=None)
