from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from app.config import Settings, get_settings
from app.database import session_scope
from app.normalizer import compare_expected, normalize_league
from app.rankings import fetch_open_rankings
from app.readiness import build_next_14_days, build_readiness_report, write_reports
from app.repositories import (
    persist_draft,
    persist_league_snapshot,
    persist_members_and_rosters,
    record_observation,
    update_source_health,
)
from app.sleeper import SleeperClient, SleeperResponse
from app.utils import canonical_json, content_hash


def _require_dict(response: SleeperResponse, name: str) -> dict[str, Any]:
    if not isinstance(response.payload, dict) or not response.payload:
        raise ValueError(f"{name} response is empty or malformed")
    return response.payload


def _require_list(response: SleeperResponse, name: str, minimum: int = 0) -> list[dict[str, Any]]:
    if not isinstance(response.payload, list) or len(response.payload) < minimum:
        raise ValueError(f"{name} response is malformed or implausibly partial")
    if not all(isinstance(item, dict) for item in response.payload):
        raise ValueError(f"{name} response contains malformed records")
    return response.payload


async def bootstrap_live(
    settings: Settings | None = None, *, include_players: bool = False
) -> dict[str, Any]:
    cfg = settings or get_settings()
    async with SleeperClient(cfg) as sleeper:
        league_r, users_r, rosters_r, drafts_r, state_r = await asyncio.gather(
            sleeper.league(cfg.sleeper_league_id),
            sleeper.users(cfg.sleeper_league_id),
            sleeper.rosters(cfg.sleeper_league_id),
            sleeper.drafts(cfg.sleeper_league_id),
            sleeper.nfl_state(),
        )
        league = _require_dict(league_r, "league")
        users = _require_list(users_r, "users", minimum=1)
        rosters = _require_list(rosters_r, "rosters", minimum=int(league.get("total_rosters") or 1))
        drafts = _require_list(drafts_r, "drafts", minimum=1)
        nfl_state = _require_dict(state_r, "NFL state")
        draft_r = await sleeper.draft(str(drafts[0]["draft_id"]))
        draft = _require_dict(draft_r, "draft")
        picks_r = await sleeper.draft_picks(str(draft["draft_id"]))
        picks = _require_list(picks_r, "draft picks", minimum=0)
        players_r = await sleeper.players() if include_players else None

    normalized = normalize_league(
        league, [draft], waiver_semantics_confirmed=cfg.waiver_semantics_confirmed
    )
    discrepancies = compare_expected(normalized)
    readiness = build_readiness_report(
        settings=cfg,
        league=league,
        normalized=normalized,
        users=users,
        rosters=rosters,
        draft=draft,
        discrepancies=discrepancies,
    )
    calendar = build_next_14_days(
        normalized,
        cfg.app_timezone,
        waiver_weekday=cfg.waiver_process_weekday,
        waiver_hour=cfg.waiver_process_hour,
        waiver_semantics_confirmed=cfg.waiver_semantics_confirmed,
    )

    responses = [league_r, users_r, rosters_r, drafts_r, state_r, draft_r, picks_r]
    async with session_scope() as session:
        for response in responses:
            await record_observation(
                session,
                provider="Sleeper public API",
                endpoint=response.endpoint,
                entity_type=response.endpoint.split("/")[0],
                entity_id=cfg.sleeper_league_id,
                payload=response.payload,
            )
        if players_r:
            await record_observation(
                session,
                provider="Sleeper public API",
                endpoint=players_r.endpoint,
                entity_type="players",
                entity_id="nfl",
                payload=players_r.payload,
            )
        await update_source_health(
            session,
            "Sleeper public API",
            success=True,
            latency_ms=max(response.latency_ms for response in responses),
            digest=content_hash([response.payload for response in responses]),
        )
        await persist_league_snapshot(
            session, league=league, normalized=normalized, discrepancies=discrepancies
        )
        await persist_members_and_rosters(
            session, league_id=cfg.sleeper_league_id, users=users, rosters=rosters
        )
        await persist_draft(session, league_id=cfg.sleeper_league_id, draft=draft, picks=picks)

    write_reports(cfg.reports_dir, readiness, calendar)
    return {"readiness": readiness, "calendar": calendar, "nfl_state": nfl_state}


async def probe_live(settings: Settings | None = None) -> dict[str, Any]:
    """Generate live readiness reports without requiring PostgreSQL (host/setup diagnostic)."""
    cfg = settings or get_settings()
    async with SleeperClient(cfg) as sleeper:
        league_r, users_r, rosters_r, drafts_r = await asyncio.gather(
            sleeper.league(cfg.sleeper_league_id),
            sleeper.users(cfg.sleeper_league_id),
            sleeper.rosters(cfg.sleeper_league_id),
            sleeper.drafts(cfg.sleeper_league_id),
        )
        league = _require_dict(league_r, "league")
        users = _require_list(users_r, "users", minimum=1)
        rosters = _require_list(rosters_r, "rosters", minimum=int(league.get("total_rosters") or 1))
        drafts = _require_list(drafts_r, "drafts", minimum=1)
        draft_r = await sleeper.draft(str(drafts[0]["draft_id"]))
        draft = _require_dict(draft_r, "draft")
    normalized = normalize_league(
        league, [draft], waiver_semantics_confirmed=cfg.waiver_semantics_confirmed
    )
    discrepancies = compare_expected(normalized)
    readiness = build_readiness_report(
        settings=cfg,
        league=league,
        normalized=normalized,
        users=users,
        rosters=rosters,
        draft=draft,
        discrepancies=discrepancies,
    )
    calendar = build_next_14_days(
        normalized,
        cfg.app_timezone,
        waiver_weekday=cfg.waiver_process_weekday,
        waiver_hour=cfg.waiver_process_hour,
        waiver_semantics_confirmed=cfg.waiver_semantics_confirmed,
    )
    write_reports(cfg.reports_dir, readiness, calendar)
    return {"readiness": readiness, "calendar": calendar}


async def sync_rankings(settings: Settings | None = None) -> dict[str, Any]:
    _ = settings  # Kept for a uniform scheduler/CLI service signature.
    rankings, provenance, digest = await fetch_open_rankings()
    payload = [asdict(row) for row in rankings]
    async with session_scope() as session:
        await record_observation(
            session,
            provider="DynastyProcess",
            endpoint="db_fpecr_latest.csv",
            entity_type="rankings",
            entity_id="2026-redraft",
            payload=payload,
            normalized=provenance,
            confidence=0.8,
        )
        await update_source_health(session, "DynastyProcess", success=True, digest=digest)
    return provenance


async def sync_draft_picks(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    async with SleeperClient(cfg) as sleeper:
        drafts_response = await sleeper.drafts(cfg.sleeper_league_id)
        drafts = _require_list(drafts_response, "drafts", minimum=1)
        draft_response = await sleeper.draft(str(drafts[0]["draft_id"]))
        draft = _require_dict(draft_response, "draft")
        picks_response = await sleeper.draft_picks(str(draft["draft_id"]))
        picks = _require_list(picks_response, "draft picks", minimum=0)
    async with session_scope() as session:
        for response in (drafts_response, draft_response, picks_response):
            await record_observation(
                session,
                provider="Sleeper public API",
                endpoint=response.endpoint,
                entity_type="draft",
                entity_id=str(draft["draft_id"]),
                payload=response.payload,
            )
        await persist_draft(session, league_id=cfg.sleeper_league_id, draft=draft, picks=picks)
    report = {
        "draft_id": draft["draft_id"],
        "status": draft.get("status"),
        "pick_count": len(picks),
        "picks": picks,
    }
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    (cfg.reports_dir / "draft-picks.json").write_text(
        canonical_json(report) + "\n", encoding="utf-8"
    )
    return report
