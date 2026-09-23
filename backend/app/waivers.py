from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def next_weekly_waiver(
    *,
    now: datetime,
    weekday: int,
    hour: int,
    timezone_name: str = "America/Denver",
) -> datetime:
    """Return the next local weekly waiver time; weekday follows Python Monday=0."""
    if not 0 <= weekday <= 6 or not 0 <= hour <= 23:
        raise ValueError("Invalid waiver weekday/hour")
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    days = (weekday - local_now.weekday()) % 7
    candidate = datetime.combine(local_now.date() + timedelta(days=days), time(hour), tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate


def require_confirmed_semantics(confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Sleeper waiver enum/timing semantics are unresolved")
