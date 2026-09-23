from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActionAudit,
    ActionItem,
    DecisionManifest,
    Draft,
    DraftPick,
    ExecutionCommand,
    LeagueMember,
    LeagueRosterSnapshot,
    LeagueSettingsSnapshot,
    OperationalIncident,
    PlayerWeekFeature,
    ProjectionSnapshot,
    ShimHeartbeat,
    SimulationRun,
    SourceDatasetHealth,
    SourceHealth,
    SourceObservation,
)
from app.utils import content_hash


async def record_observation(
    session: AsyncSession,
    *,
    provider: str,
    endpoint: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any] | list[Any],
    normalized: dict[str, Any] | None = None,
    accepted: bool = True,
    rejection_reason: str | None = None,
    confidence: float = 1.0,
) -> SourceObservation:
    digest = content_hash(payload)
    existing = await session.scalar(
        select(SourceObservation).where(
            SourceObservation.provider == provider,
            SourceObservation.endpoint == endpoint,
            SourceObservation.content_hash == digest,
        )
    )
    if existing:
        return existing
    observation = SourceObservation(
        provider=provider,
        endpoint=endpoint,
        entity_type=entity_type,
        entity_id=entity_id,
        content_hash=digest,
        raw_payload=payload,
        normalized_payload=normalized,
        accepted=accepted,
        rejection_reason=rejection_reason,
        confidence=confidence,
        freshness_status="fresh" if accepted else "rejected",
    )
    try:
        async with session.begin_nested():
            session.add(observation)
            await session.flush()
        return observation
    except IntegrityError:
        # Pollers can legitimately observe the same immutable payload at the same time.
        # The uniqueness constraint is authoritative; recover the winning insert without
        # rolling back unrelated work in the outer transaction.
        concurrent = await session.scalar(
            select(SourceObservation).where(
                SourceObservation.provider == provider,
                SourceObservation.endpoint == endpoint,
                SourceObservation.content_hash == digest,
            )
        )
        if concurrent is None:
            raise
        return concurrent


async def update_source_health(
    session: AsyncSession,
    provider: str,
    *,
    success: bool,
    latency_ms: float | None = None,
    digest: str | None = None,
    error: str | None = None,
) -> SourceHealth:
    now = datetime.now(timezone.utc)
    health = await session.get(SourceHealth, provider)
    if health is None:
        health = SourceHealth(provider=provider)
        session.add(health)
    health.last_attempt_at = now
    health.latency_ms = latency_ms
    if success:
        if digest and health.last_content_hash != digest:
            health.last_changed_at = now
        health.status = "healthy"
        health.last_success_at = now
        health.last_content_hash = digest or health.last_content_hash
        health.consecutive_failures = 0
        health.last_error = None
    else:
        health.consecutive_failures = (health.consecutive_failures or 0) + 1
        health.status = "degraded" if health.last_success_at else "unavailable"
        health.last_error = error
    return health


async def update_dataset_health(
    session: AsyncSession,
    provider: str,
    dataset: str,
    *,
    success: bool,
    latency_ms: float | None = None,
    digest: str | None = None,
    error: str | None = None,
    source_updated_at: datetime | None = None,
    expected_records: int | None = None,
    observed_records: int | None = None,
    expected_absence: bool = False,
) -> SourceDatasetHealth:
    now = datetime.now(timezone.utc)
    health = await session.scalar(
        select(SourceDatasetHealth).where(
            SourceDatasetHealth.provider == provider,
            SourceDatasetHealth.dataset == dataset,
        )
    )
    if health is None:
        health = SourceDatasetHealth(provider=provider, dataset=dataset)
        session.add(health)
    health.last_attempt_at = now
    health.latency_ms = latency_ms
    if expected_records is not None:
        health.expected_records = expected_records
    if observed_records is not None:
        health.observed_records = observed_records
    effective_expected = health.expected_records
    effective_observed = health.observed_records
    if effective_expected is None or effective_observed is None:
        health.completeness = None
    elif effective_expected == 0:
        health.completeness = 1.0
    else:
        health.completeness = min(effective_observed / effective_expected, 1.0)
    if expected_absence:
        health.status = "pending"
        health.consecutive_failures = 0
        health.last_error = (error or "Dataset is not published yet")[:4000]
    elif success:
        if digest and health.last_content_hash != digest:
            health.last_changed_at = now
        health.status = "healthy"
        health.last_success_at = now
        health.source_updated_at = source_updated_at or health.source_updated_at
        health.last_content_hash = digest or health.last_content_hash
        health.consecutive_failures = 0
        health.last_error = None
    else:
        health.consecutive_failures = (health.consecutive_failures or 0) + 1
        health.status = "degraded" if health.last_success_at else "unavailable"
        health.last_error = (error or "unknown source failure")[:4000]
    return health


async def record_decision_manifest(
    session: AsyncSession,
    *,
    decision_type: str,
    league_id: str,
    season: str,
    week: int,
    state_hash: str,
    evidence_hashes: list[str],
    source_gate: dict[str, Any],
    versions: dict[str, Any],
    result_hash: str | None = None,
) -> DecisionManifest:
    manifest = await session.scalar(
        select(DecisionManifest).where(
            DecisionManifest.decision_type == decision_type,
            DecisionManifest.state_hash == state_hash,
        )
    )
    if manifest is None:
        manifest = DecisionManifest(
            decision_type=decision_type,
            league_id=league_id,
            season=season,
            week=week,
            state_hash=state_hash,
            evidence_hashes=evidence_hashes,
            source_gate=source_gate,
            versions=versions,
            result_hash=result_hash,
        )
        session.add(manifest)
    elif result_hash and not manifest.result_hash:
        manifest.result_hash = result_hash
    return manifest


async def upsert_operational_incident(
    session: AsyncSession,
    *,
    category: str,
    severity: str,
    summary: str,
    dedupe_key: str,
    details: dict[str, Any] | None = None,
) -> OperationalIncident:
    now = datetime.now(timezone.utc)
    incident = await session.scalar(
        select(OperationalIncident).where(OperationalIncident.dedupe_key == dedupe_key)
    )
    if incident is None:
        incident = OperationalIncident(
            category=category,
            severity=severity,
            summary=summary,
            details=details or {},
            dedupe_key=dedupe_key,
            first_observed_at=now,
            last_observed_at=now,
        )
        session.add(incident)
    else:
        incident.severity = severity
        incident.status = "open"
        incident.summary = summary
        incident.details = details or {}
        incident.last_observed_at = now
        incident.resolved_at = None
    return incident


async def resolve_operational_incident(
    session: AsyncSession, *, dedupe_key: str
) -> OperationalIncident | None:
    incident = await session.scalar(
        select(OperationalIncident).where(OperationalIncident.dedupe_key == dedupe_key)
    )
    if incident and incident.status != "resolved":
        now = datetime.now(timezone.utc)
        incident.status = "resolved"
        incident.last_observed_at = now
        incident.resolved_at = now
    return incident


async def record_intelligence_snapshots(
    session: AsyncSession, context: dict[str, Any]
) -> dict[str, int]:
    season = int(context.get("season") or 0)
    week = int(context.get("week") or 0)
    counts = {"usage": 0, "projections": 0, "simulations": 0}
    players: dict[str, dict[str, Any]] = {}
    for roster in context.get("league_rosters") or []:
        for player in roster.get("players") or []:
            if player.get("player_id"):
                players[str(player["player_id"])] = player
    for collection in (
        (context.get("our_team") or {}).get("current_lineup") or [],
        (context.get("our_team") or {}).get("bench") or [],
        (context.get("our_team") or {}).get("reserve") or [],
        (context.get("opponent") or {}).get("players") or [],
        context.get("free_agent_candidates") or [],
    ):
        for player in collection:
            if player.get("player_id"):
                players[str(player["player_id"])] = player
    for player_id, player in players.items():
        usage = player.get("usage") or {}
        feature_hash = usage.get("feature_hash")
        if feature_hash:
            existing = await session.scalar(
                select(PlayerWeekFeature).where(
                    PlayerWeekFeature.player_id == player_id,
                    PlayerWeekFeature.season == season,
                    PlayerWeekFeature.week == week,
                    PlayerWeekFeature.feature_version == str(usage.get("feature_version")),
                    PlayerWeekFeature.content_hash == str(feature_hash),
                )
            )
            if existing is None:
                session.add(
                    PlayerWeekFeature(
                        player_id=player_id,
                        season=season,
                        week=week,
                        feature_version=str(usage.get("feature_version")),
                        content_hash=str(feature_hash),
                        features={
                            key: value for key, value in usage.items() if key != "provenance"
                        },
                        provenance=usage.get("provenance") or {},
                    )
                )
                counts["usage"] += 1
        projection = player.get("projection_ensemble") or {}
        input_hash = projection.get("input_hash")
        if input_hash:
            existing = await session.scalar(
                select(ProjectionSnapshot).where(
                    ProjectionSnapshot.player_id == player_id,
                    ProjectionSnapshot.season == season,
                    ProjectionSnapshot.week == week,
                    ProjectionSnapshot.model_version == str(projection.get("model_version")),
                    ProjectionSnapshot.input_hash == str(input_hash),
                )
            )
            if existing is None:
                session.add(
                    ProjectionSnapshot(
                        player_id=player_id,
                        season=season,
                        week=week,
                        model_version=str(projection.get("model_version")),
                        input_hash=str(input_hash),
                        projection=projection,
                        provenance={"sources": projection.get("sources") or []},
                    )
                )
                counts["projections"] += 1
    outlook = context.get("championship_outlook") or {}
    if outlook.get("status") == "complete" and outlook.get("input_hash"):
        existing = await session.scalar(
            select(SimulationRun).where(
                SimulationRun.input_hash == str(outlook["input_hash"]),
                SimulationRun.model_version == str(outlook.get("model_version")),
                SimulationRun.seed == int(outlook.get("seed") or 0),
            )
        )
        if existing is None:
            session.add(
                SimulationRun(
                    league_id=str((context.get("league") or {}).get("league_id") or ""),
                    season=season,
                    week=week,
                    model_version=str(outlook.get("model_version")),
                    input_hash=str(outlook["input_hash"]),
                    seed=int(outlook.get("seed") or 0),
                    simulation_count=int(outlook.get("simulation_count") or 0),
                    results=outlook,
                )
            )
            counts["simulations"] += 1
    return counts


async def intelligence_snapshot_totals(
    session: AsyncSession, *, season: int, week: int
) -> dict[str, int]:
    """Return persisted intelligence totals for one decision week."""

    usage = await session.scalar(
        select(func.count())
        .select_from(PlayerWeekFeature)
        .where(
            PlayerWeekFeature.season == season,
            PlayerWeekFeature.week == week,
        )
    )
    projections = await session.scalar(
        select(func.count())
        .select_from(ProjectionSnapshot)
        .where(
            ProjectionSnapshot.season == season,
            ProjectionSnapshot.week == week,
        )
    )
    simulations = await session.scalar(
        select(func.count())
        .select_from(SimulationRun)
        .where(
            SimulationRun.season == season,
            SimulationRun.week == week,
        )
    )
    return {
        "usage": int(usage or 0),
        "projections": int(projections or 0),
        "simulations": int(simulations or 0),
    }


async def persist_league_snapshot(
    session: AsyncSession,
    *,
    league: dict[str, Any],
    normalized: dict[str, Any],
    discrepancies: list[dict[str, Any]],
) -> LeagueSettingsSnapshot:
    digest = content_hash(league)
    existing = await session.scalar(
        select(LeagueSettingsSnapshot).where(LeagueSettingsSnapshot.content_hash == digest)
    )
    if existing:
        return existing
    snapshot = LeagueSettingsSnapshot(
        league_id=str(league["league_id"]),
        content_hash=digest,
        raw_payload=league,
        normalized_payload=normalized,
        discrepancy_report={"items": discrepancies},
    )
    session.add(snapshot)
    return snapshot


async def persist_members_and_rosters(
    session: AsyncSession,
    *,
    league_id: str,
    users: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
) -> None:
    roster_by_owner = {str(item.get("owner_id")): item for item in rosters if item.get("owner_id")}
    for user in users:
        user_id = str(user.get("user_id"))
        member = await session.scalar(
            select(LeagueMember).where(
                LeagueMember.league_id == league_id, LeagueMember.user_id == user_id
            )
        )
        roster = roster_by_owner.get(user_id)
        if member is None:
            member = LeagueMember(league_id=league_id, user_id=user_id, raw_payload=user)
            session.add(member)
        member.roster_id = roster.get("roster_id") if roster else None
        member.display_name = user.get("display_name")
        member.team_name = (user.get("metadata") or {}).get("team_name")
        member.raw_payload = user

    for roster in rosters:
        digest = content_hash(roster)
        existing = await session.scalar(
            select(LeagueRosterSnapshot).where(
                LeagueRosterSnapshot.league_id == league_id,
                LeagueRosterSnapshot.roster_id == int(roster["roster_id"]),
                LeagueRosterSnapshot.content_hash == digest,
            )
        )
        if existing:
            continue
        session.add(
            LeagueRosterSnapshot(
                league_id=league_id,
                roster_id=int(roster["roster_id"]),
                owner_id=roster.get("owner_id"),
                content_hash=digest,
                players=roster.get("players") or [],
                starters=roster.get("starters") or [],
                reserve=roster.get("reserve") or [],
                settings=roster.get("settings") or {},
                raw_payload=roster,
            )
        )


async def persist_draft(
    session: AsyncSession, *, league_id: str, draft: dict[str, Any], picks: list[dict[str, Any]]
) -> Draft:
    draft_id = str(draft["draft_id"])
    model = await session.get(Draft, draft_id)
    if model is None:
        model = Draft(
            draft_id=draft_id,
            league_id=league_id,
            status="pre_draft",
            draft_type="snake",
            raw_payload=draft,
        )
        session.add(model)
    settings = draft.get("settings") or {}
    start_ms = draft.get("start_time")
    model.status = draft.get("status") or "unknown"
    model.draft_type = draft.get("type") or "unknown"
    model.start_time = (
        datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) if start_ms else None
    )
    model.rounds = settings.get("rounds")
    model.pick_timer = settings.get("pick_timer")
    model.draft_order = draft.get("draft_order")
    model.raw_payload = draft

    for pick in picks:
        pick_no = int(pick.get("pick_no") or 0)
        if pick_no <= 0 or not pick.get("player_id"):
            continue
        existing = await session.scalar(
            select(DraftPick).where(DraftPick.draft_id == draft_id, DraftPick.pick_no == pick_no)
        )
        if existing is None:
            session.add(
                DraftPick(
                    draft_id=draft_id,
                    pick_no=pick_no,
                    round=int(pick.get("round") or 0),
                    roster_id=pick.get("roster_id"),
                    player_id=str(pick["player_id"]),
                    picked_by=pick.get("picked_by"),
                    raw_payload=pick,
                )
            )
    return model


async def latest_league_snapshot(
    session: AsyncSession, league_id: str
) -> LeagueSettingsSnapshot | None:
    return await session.scalar(
        select(LeagueSettingsSnapshot)
        .where(LeagueSettingsSnapshot.league_id == league_id)
        .order_by(LeagueSettingsSnapshot.observed_at.desc())
        .limit(1)
    )


async def list_actions(session: AsyncSession, limit: int = 100) -> list[ActionItem]:
    result = await session.scalars(
        select(ActionItem).order_by(ActionItem.priority, ActionItem.created_at.desc()).limit(limit)
    )
    return list(result)


async def transition_action(
    session: AsyncSession,
    action: ActionItem,
    to_status: str,
    *,
    actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    previous = action.status
    action.status = to_status
    session.add(
        ActionAudit(
            action_id=action.id,
            actor=actor,
            from_status=previous,
            to_status=to_status,
            details=details or {},
        )
    )


async def lease_execution_command(
    session: AsyncSession,
    *,
    agent_id: str,
    lease_seconds: int = 30,
    command_id: str | None = None,
    action_types: list[str] | None = None,
) -> ExecutionCommand | None:
    now = datetime.now(timezone.utc)
    query = (
        select(ExecutionCommand)
        .where(
            ExecutionCommand.status.in_(["ready", "leased"]),
            ExecutionCommand.not_before <= now,
            ExecutionCommand.expires_at > now,
            (ExecutionCommand.lease_until.is_(None) | (ExecutionCommand.lease_until < now)),
        )
    )
    if command_id:
        query = query.where(ExecutionCommand.id == command_id)
    if action_types:
        query = query.where(ExecutionCommand.action_type.in_(action_types))
    query = (
        query.order_by(ExecutionCommand.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    command = await session.scalar(query)
    if command is None:
        return None
    command.status = "leased"
    command.lease_owner = agent_id
    command.lease_until = now + timedelta(seconds=lease_seconds)
    command.attempt_count += 1
    return command


async def upsert_heartbeat(session: AsyncSession, payload: dict[str, Any]) -> ShimHeartbeat:
    agent_id = str(payload["agent_id"])
    heartbeat = await session.get(ShimHeartbeat, agent_id)
    if heartbeat is None:
        heartbeat = ShimHeartbeat(agent_id=agent_id)
        session.add(heartbeat)
    heartbeat.observed_at = datetime.now(timezone.utc)
    heartbeat.app_version = payload.get("app_version")
    heartbeat.app_running = bool(payload.get("app_running"))
    heartbeat.session_available = bool(payload.get("session_available"))
    heartbeat.execution_mode = payload.get("execution_mode") or "dry_run"
    heartbeat.capabilities = payload.get("capabilities") or []
    heartbeat.details = payload.get("details") or {}
    return heartbeat
