from __future__ import annotations

from datetime import datetime, timedelta, timezone


def freshness_status(
    observed_at: datetime, max_age: timedelta, *, now: datetime | None = None
) -> str:
    current = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    age = current - observed_at
    if age <= max_age:
        return "fresh"
    if age <= max_age * 2:
        return "stale"
    return "expired"


def destructive_confidence_allowed(statuses: list[str]) -> bool:
    return bool(statuses) and all(status == "fresh" for status in statuses)
