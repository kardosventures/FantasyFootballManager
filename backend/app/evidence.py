from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.freshness import freshness_status
from app.models import OperationalIncident, SourceDatasetHealth
from app.utils import content_hash

UTC = timezone.utc
EVIDENCE_GATE_VERSION = "evidence-gate-2026.2"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _freshness_check(
    check_id: str,
    label: str,
    observed_at: Any,
    max_age: timedelta,
    *,
    now: datetime,
    required: bool,
) -> dict[str, Any]:
    parsed = _parse_time(observed_at)
    if parsed is None:
        return {
            "id": check_id,
            "label": label,
            "status": "blocked" if required else "degraded",
            "required": required,
            "observed_at": None,
            "max_age_seconds": int(max_age.total_seconds()),
            "reason": f"{label} has no valid observation timestamp",
        }
    freshness = freshness_status(parsed, max_age, now=now)
    acceptable = freshness == "fresh"
    return {
        "id": check_id,
        "label": label,
        "status": "pass" if acceptable else "blocked" if required else "degraded",
        "required": required,
        "freshness": freshness,
        "observed_at": parsed.isoformat(),
        "age_seconds": max(int((now - parsed).total_seconds()), 0),
        "max_age_seconds": int(max_age.total_seconds()),
        "reason": None if acceptable else f"{label} is {freshness}",
    }


def evaluate_manager_evidence(
    quality: dict[str, Any],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic, decision-specific gate; never infer missing evidence."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not settings.source_gating_v2_enabled:
        return {
            "version": EVIDENCE_GATE_VERSION,
            "status": "disabled",
            "allows_reasoning": True,
            "allows_lineup_execution": True,
            "allows_acquisition_execution": True,
            "checks": [],
            "blockers": [],
            "warnings": ["SOURCE_GATING_V2_ENABLED is false"],
        }

    checks = [
        _freshness_check(
            "sleeper_state",
            "Sleeper league state",
            quality.get("sleeper_as_of"),
            timedelta(minutes=settings.sleeper_state_max_age_minutes),
            now=current,
            required=True,
        ),
        _freshness_check(
            "nflverse_context",
            "NFL schedule and injury context",
            quality.get("nflverse_as_of"),
            timedelta(hours=settings.nflverse_context_max_age_hours),
            now=current,
            required=True,
        ),
        _freshness_check(
            "browser_lineup",
            "Authenticated Sleeper lineup",
            quality.get("sleeper_ui_projection_as_of"),
            timedelta(seconds=settings.browser_observation_max_age_seconds),
            now=current,
            required=True,
        ),
    ]

    def add_boolean(
        check_id: str, label: str, passed: bool, reason: str, *, required: bool = True
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "label": label,
                "status": "pass" if passed else "blocked" if required else "degraded",
                "required": required,
                "reason": None if passed else reason,
            }
        )

    expected_rosters = int(quality.get("expected_league_roster_count") or 0)
    observed_rosters = int(quality.get("observed_league_roster_count") or 0)
    add_boolean(
        "league_rosters_complete",
        "Complete league rosters",
        expected_rosters > 0 and observed_rosters == expected_rosters,
        f"Expected {expected_rosters or 'configured league size'} rosters, observed {observed_rosters}",
    )
    add_boolean(
        "weekly_schedule_present",
        "Current-week NFL schedule",
        int(quality.get("schedule_game_count") or 0) > 0,
        "No current-week NFL games were supplied",
    )
    public_ids = {str(item) for item in quality.get("public_roster_player_ids") or []}
    browser_ids = {str(item) for item in quality.get("browser_roster_player_ids") or []}
    add_boolean(
        "roster_cross_check",
        "Public/browser roster identity",
        bool(public_ids) and public_ids == browser_ids,
        "Authenticated Sleeper roster does not exactly match the public roster",
    )
    starter_count = int(quality.get("starter_count") or 0)
    add_boolean(
        "lineup_projection_coverage",
        "Roster projection coverage",
        starter_count > 0 and int(quality.get("sleeper_ui_projection_count") or 0) >= starter_count,
        "Not every starting slot has current authenticated projection evidence",
    )

    if quality.get("weekly_projection_feed") in {None, "not_configured"}:
        add_boolean(
            "weekly_projection_feed",
            "Weekly projection feed",
            False,
            "No current weekly projection feed is available",
        )
    if int(quality.get("sleeper_ui_matchup_projection_count") or 0) == 0:
        add_boolean(
            "matchup_projection_availability",
            "Authenticated matchup projections",
            False,
            (
                "The current matchup projection panel is unavailable; reason from the public "
                "opponent roster and player-level projections without using a matchup total or "
                "win probability"
            ),
            required=False,
        )
    if quality.get("weather_status") not in {"active", "healthy"}:
        add_boolean(
            "weather_coverage",
            "Kickoff-specific weather",
            False,
            "Weather is incomplete; affected outdoor players require conservative treatment",
            required=False,
        )

    blockers = [str(row["reason"]) for row in checks if row["status"] == "blocked"]
    warnings = [str(row["reason"]) for row in checks if row["status"] == "degraded"]
    return {
        "version": EVIDENCE_GATE_VERSION,
        "evaluated_at": current.isoformat(),
        "status": "blocked" if blockers else "degraded" if warnings else "ready",
        "allows_reasoning": not blockers,
        "allows_lineup_execution": not blockers,
        "allows_acquisition_execution": not blockers,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
    }


def _required_datasets(settings: Settings, purpose: str) -> list[tuple[str, str, timedelta]]:
    sleeper_age = timedelta(minutes=settings.sleeper_state_max_age_minutes)
    browser_age = timedelta(seconds=settings.browser_observation_max_age_seconds)
    nflverse_age = timedelta(hours=settings.nflverse_context_max_age_hours)
    requirements = [
        ("Sleeper public API", "in_season_bundle", sleeper_age),
        ("Sleeper public API", "league", sleeper_age),
        ("Sleeper public API", "rosters", sleeper_age),
        ("Sleeper public API", "matchups", sleeper_age),
        ("Sleeper public API", "nfl_state", sleeper_age),
        ("nflverse", "context_bundle", nflverse_age),
        ("Sleeper authenticated UI", "lineup", browser_age),
    ]
    if purpose in {"reasoning", "ADD_FREE_AGENT", "WAIVER_CLAIM", "DROP_PLAYER"}:
        requirements.extend(
            [
                ("Sleeper public API", "transactions", sleeper_age),
                ("Sleeper authenticated UI free agents", "free_agents", browser_age),
            ]
        )
    if purpose in {"reasoning", "WAIVER_CLAIM"}:
        requirements.append(
            ("Sleeper authenticated UI waivers", "pending_waivers", browser_age)
        )
    if purpose in {"PROPOSE_TRADE", "ACCEPT_TRADE", "DECLINE_TRADE"}:
        requirements.append(
            ("Sleeper authenticated UI trades", "trade_inbox", browser_age)
        )
    return requirements


def merge_evidence_checks(
    gate: dict[str, Any], checks: list[dict[str, Any]], *, now: datetime
) -> dict[str, Any]:
    """Merge persisted operational facts into a deterministic manager gate."""

    if gate.get("status") == "disabled":
        return gate
    combined = [*(gate.get("checks") or []), *checks]
    blockers = [str(row["reason"]) for row in combined if row.get("status") == "blocked"]
    warnings = [str(row["reason"]) for row in combined if row.get("status") == "degraded"]
    return {
        **gate,
        "version": EVIDENCE_GATE_VERSION,
        "evaluated_at": now.isoformat(),
        "status": "blocked" if blockers else "degraded" if warnings else "ready",
        "allows_reasoning": not blockers,
        "allows_lineup_execution": not blockers,
        "allows_acquisition_execution": not blockers,
        "checks": combined,
        "blockers": blockers,
        "warnings": warnings,
    }


async def operational_evidence_checks(
    session: AsyncSession,
    settings: Settings,
    *,
    purpose: str = "reasoning",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read durable dataset health and incidents for a reasoning or write boundary."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    datasets = list(await session.scalars(select(SourceDatasetHealth)))
    by_key = {(row.provider, row.dataset): row for row in datasets}
    checks: list[dict[str, Any]] = []
    for provider, dataset, max_age in _required_datasets(settings, purpose):
        row = by_key.get((provider, dataset))
        label = f"{provider} {dataset}"
        if row is None:
            checks.append(
                {
                    "id": f"dataset:{provider}:{dataset}",
                    "label": label,
                    "status": "blocked",
                    "required": True,
                    "reason": f"{label} has no persisted health record",
                }
            )
            continue
        last_success = _parse_time(row.last_success_at)
        freshness = (
            freshness_status(last_success, max_age, now=current) if last_success else "missing"
        )
        expected = row.expected_records
        observed = row.observed_records
        # An expected empty result is a complete dataset, not a missing one.
        # This matters for authenticated queues such as My Waivers and trades,
        # where zero records is the normal healthy state.
        complete = expected is None or (
            expected >= 0 and observed is not None and observed >= expected
        )
        healthy = row.status == "healthy"
        passed = healthy and freshness == "fresh" and complete
        reasons = []
        if not healthy:
            reasons.append(f"status is {row.status}")
        if freshness != "fresh":
            reasons.append(f"freshness is {freshness}")
        if not complete:
            reasons.append(f"observed {observed or 0} of {expected} expected records")
        checks.append(
            {
                "id": f"dataset:{provider}:{dataset}",
                "label": label,
                "status": "pass" if passed else "blocked",
                "required": True,
                "provider": provider,
                "dataset": dataset,
                "health_status": row.status,
                "observed_at": last_success.isoformat() if last_success else None,
                "age_seconds": (
                    max(int((current - last_success).total_seconds()), 0) if last_success else None
                ),
                "max_age_seconds": int(max_age.total_seconds()),
                "expected_records": expected,
                "observed_records": observed,
                "completeness": row.completeness,
                "reason": None if passed else f"{label}: {', '.join(reasons)}",
            }
        )

    incidents = list(
        await session.scalars(
            select(OperationalIncident).where(OperationalIncident.status == "open")
        )
    )
    critical = [row for row in incidents if row.severity == "critical"]
    warnings = [row for row in incidents if row.severity != "critical"]
    incident_status = "blocked" if critical else "degraded" if warnings else "pass"
    incident_rows = [
        {
            "id": row.id,
            "category": row.category,
            "severity": row.severity,
            "summary": row.summary,
            "last_observed_at": _parse_time(row.last_observed_at).isoformat()
            if _parse_time(row.last_observed_at)
            else None,
        }
        for row in incidents
    ]
    checks.append(
        {
            "id": "operational_incidents",
            "label": "Unresolved operational incidents",
            "status": incident_status,
            "required": True,
            "incidents": incident_rows,
            "reason": (
                f"{len(critical)} critical operational incident(s) are open"
                if critical
                else f"{len(warnings)} non-critical operational incident(s) are open"
                if warnings
                else None
            ),
        }
    )
    return checks


async def apply_operational_evidence(
    context: dict[str, Any],
    session: AsyncSession,
    settings: Settings,
    *,
    purpose: str = "reasoning",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind current durable operational evidence to a manager context and its hash."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    quality = context.setdefault("data_quality", {})
    operational_checks = await operational_evidence_checks(
        session,
        settings,
        purpose=purpose,
        now=current,
    )
    gate = merge_evidence_checks(
        quality.get("evidence_gate") or {},
        operational_checks,
        now=current,
    )
    quality["evidence_gate"] = gate
    stable_operational_evidence = [
        {
            key: row.get(key)
            for key in (
                "id",
                "status",
                "health_status",
                "observed_at",
                "expected_records",
                "observed_records",
                "completeness",
                "reason",
                "incidents",
            )
        }
        for row in operational_checks
    ]
    context["state_hash"] = content_hash(
        {
            "base_state_hash": context.get("state_hash"),
            "operational_evidence": stable_operational_evidence,
            "purpose": purpose,
        }
    )
    return gate
