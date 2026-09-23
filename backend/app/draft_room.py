from __future__ import annotations

import asyncio
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import session_scope
from app.draft_engine import (
    build_candidate_turn_paths,
    build_slot_draft_plan,
    map_rankings_to_sleeper,
    recommend_draft_pick,
    simulate_all_draft_slots,
)
from app.draft_expert import (
    ExpertDraftUnavailable,
    apply_expert_decision,
    build_expert_context,
    choose_expert_draft_pick,
)
from app.execution import propose_execution
from app.models import ActionItem, ExecutionCommand, ShimHeartbeat
from app.rankings import RankingRow, fetch_open_rankings
from app.repositories import record_observation, update_source_health
from app.sleeper import SleeperClient
from app.utils import canonical_json, content_hash


@dataclass
class StaticDraftData:
    rankings: list[RankingRow]
    provenance: dict[str, Any]
    ranking_digest: str
    players: dict[str, dict[str, Any]]
    matched: list[tuple[str, RankingRow]]
    rejected: list[str]
    loaded_at: datetime
    players_endpoint: str


@dataclass
class LeagueDraftData:
    payload: dict[str, Any]
    loaded_at: datetime


@dataclass(frozen=True)
class DraftClockTiming:
    started_at: datetime
    deadline: datetime
    submit_at: datetime
    source: str

    def as_report(self, *, now: datetime) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "auto_submit_at": self.submit_at.isoformat(),
            "seconds_remaining": max(math.ceil((self.deadline - now).total_seconds()), 0),
            "source": self.source,
        }


_static_cache: StaticDraftData | None = None
_league_cache: LeagueDraftData | None = None


def _snake_slot(pick_no: int, teams: int) -> int:
    round_index, index = divmod(pick_no - 1, teams)
    return index + 1 if round_index % 2 == 0 else teams - index


def _next_owner_pick(pick_no: int, owner_slot: int | None, teams: int) -> int:
    if not owner_slot:
        return pick_no + teams
    for candidate in range(pick_no, pick_no + teams * 2 + 1):
        if _snake_slot(candidate, teams) == owner_slot:
            return candidate
    return pick_no + teams


def _position_counts_by_draft_slot(
    picks: list[dict[str, Any]], players: dict[str, dict[str, Any]], teams: int
) -> tuple[dict[int, dict[str, int]], dict[str, int]]:
    team_counts = {slot: {} for slot in range(1, teams + 1)}
    recent_counts: dict[str, int] = {}
    for index, pick in enumerate(picks):
        try:
            slot = int(pick.get("draft_slot") or 0)
        except (TypeError, ValueError):
            continue
        player = players.get(str(pick.get("player_id") or ""), {})
        metadata = pick.get("metadata") or {}
        position = str(player.get("position") or metadata.get("position") or "").upper()
        position = "DEF" if position == "DST" else position
        if slot in team_counts and position:
            counts = team_counts[slot]
            counts[position] = counts.get(position, 0) + 1
        if index >= len(picks) - 12 and position:
            recent_counts[position] = recent_counts.get(position, 0) + 1
    return team_counts, recent_counts


def _player_ids_by_draft_slot(picks: list[dict[str, Any]], teams: int) -> dict[int, set[str]]:
    rosters = {slot: set() for slot in range(1, teams + 1)}
    for pick in picks:
        try:
            slot = int(pick.get("draft_slot") or 0)
        except (TypeError, ValueError):
            continue
        player_id = str(pick.get("player_id") or "")
        if slot in rosters and player_id:
            rosters[slot].add(player_id)
    return rosters


def _governing_draft_settings(
    league: dict[str, Any], room_settings: dict[str, Any]
) -> dict[str, Any]:
    """Use configured league roster rules even when a standalone mock differs."""
    settings = dict(room_settings)
    roster_positions = [str(item or "").upper() for item in league.get("roster_positions") or []]
    if not roster_positions:
        return settings
    aliases = {"DST": "DEF", "WRT": "FLEX", "WRRB_FLEX": "FLEX"}
    normalized = [aliases.get(position, position) for position in roster_positions]
    for position, field in {
        "QB": "slots_qb",
        "RB": "slots_rb",
        "WR": "slots_wr",
        "TE": "slots_te",
        "K": "slots_k",
        "DEF": "slots_def",
        "FLEX": "slots_flex",
    }.items():
        settings[field] = normalized.count(position)
    draftable_positions = [position for position in normalized if position not in {"IR", "RESERVE"}]
    if draftable_positions:
        settings["rounds"] = len(draftable_positions)
    if league.get("total_rosters"):
        settings["teams"] = int(league["total_rosters"])
    return settings


def _governing_scoring_type(league: dict[str, Any], draft: dict[str, Any]) -> str:
    scoring = league.get("scoring_settings") or {}
    if scoring:
        receptions = float(scoring.get("rec") or 0)
        if receptions >= 0.75:
            return "ppr"
        if receptions > 0:
            return "half_ppr"
        return "standard"
    return str((draft.get("metadata") or {}).get("scoring_type") or "half_ppr").lower()


def _draft_clock_timing(
    draft: dict[str, Any],
    *,
    submit_seconds_remaining: int,
    submit_delay_seconds: int | None = None,
) -> DraftClockTiming | None:
    if draft.get("status") != "drafting":
        return None
    timer_seconds = int((draft.get("settings") or {}).get("pick_timer") or 0)
    if timer_seconds <= 0:
        return None
    raw_started_at = draft.get("last_picked")
    source = "last_picked"
    if raw_started_at is None:
        raw_started_at = draft.get("start_time")
        source = "start_time"
    try:
        epoch_value = float(raw_started_at)
    except (TypeError, ValueError):
        return None
    if epoch_value <= 0:
        return None
    if epoch_value > 100_000_000_000:
        epoch_value /= 1000
    started_at = datetime.fromtimestamp(epoch_value, tz=timezone.utc)
    deadline = started_at + timedelta(seconds=timer_seconds)
    submit_at = (
        started_at + timedelta(seconds=submit_delay_seconds)
        if submit_delay_seconds is not None
        else deadline - timedelta(seconds=submit_seconds_remaining)
    )
    return DraftClockTiming(
        started_at=started_at,
        deadline=deadline,
        submit_at=submit_at,
        source=source,
    )


def _is_explicit_standalone_mock(cfg: Settings, draft: dict[str, Any]) -> bool:
    return bool(
        draft.get("league_id") is None
        and cfg.sleeper_draft_id
        and str(draft.get("draft_id") or "") == cfg.sleeper_draft_id
    )


def _select_draft(drafts: list[dict[str, Any]]) -> dict[str, Any]:
    if not drafts:
        raise ValueError("No Sleeper draft found")
    priority = {"drafting": 0, "pre_draft": 1, "paused": 2, "complete": 3}
    return sorted(
        drafts,
        key=lambda item: (
            priority.get(str(item.get("status")), 9),
            -int(item.get("created") or item.get("start_time") or 0),
        ),
    )[0]


async def _load_static_data(
    cfg: Settings, sleeper: SleeperClient, *, force: bool = False
) -> StaticDraftData:
    global _static_cache
    refresh_after = timedelta(minutes=cfg.draft_static_refresh_minutes)
    if (
        not force
        and _static_cache is not None
        and datetime.now(timezone.utc) - _static_cache.loaded_at < refresh_after
    ):
        return _static_cache
    cache_path = cfg.reports_dir / "draft-static-cache.json"
    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            loaded_at = datetime.fromisoformat(cached["loaded_at"])
            rankings = [RankingRow(**row) for row in cached["rankings"]]
            players = cached["players"]
            if (
                datetime.now(timezone.utc) - loaded_at < refresh_after
                and len(rankings) >= 100
                and isinstance(players, dict)
                and len(players) >= 1000
            ):
                if not all("bye_week" in row for row in cached["rankings"]) or not any(
                    row.get("market_adp") is not None for row in cached["rankings"]
                ):
                    rankings, provenance, ranking_digest = await fetch_open_rankings()
                    matched, rejected = map_rankings_to_sleeper(rankings, players)
                    _static_cache = StaticDraftData(
                        rankings=rankings,
                        provenance=provenance,
                        ranking_digest=ranking_digest,
                        players=players,
                        matched=matched,
                        rejected=rejected,
                        loaded_at=datetime.now(timezone.utc),
                        players_endpoint="players/nfl",
                    )
                    _write_report(
                        cache_path,
                        {
                            "loaded_at": _static_cache.loaded_at.isoformat(),
                            "rankings": [asdict(row) for row in rankings],
                            "provenance": provenance,
                            "ranking_digest": ranking_digest,
                            "players": players,
                        },
                    )
                    return _static_cache
                matched, rejected = map_rankings_to_sleeper(rankings, players)
                _static_cache = StaticDraftData(
                    rankings=rankings,
                    provenance=cached["provenance"],
                    ranking_digest=cached["ranking_digest"],
                    players=players,
                    matched=matched,
                    rejected=rejected,
                    loaded_at=loaded_at,
                    players_endpoint="players/nfl",
                )
                return _static_cache
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    ranking_result, players_response = await asyncio.gather(
        fetch_open_rankings(), sleeper.players()
    )
    rankings, provenance, ranking_digest = ranking_result
    if not isinstance(players_response.payload, dict) or len(players_response.payload) < 1000:
        raise ValueError("Sleeper player catalog is malformed or implausibly partial")
    matched, rejected = map_rankings_to_sleeper(rankings, players_response.payload)
    _static_cache = StaticDraftData(
        rankings=rankings,
        provenance=provenance,
        ranking_digest=ranking_digest,
        players=players_response.payload,
        matched=matched,
        rejected=rejected,
        loaded_at=datetime.now(timezone.utc),
        players_endpoint=players_response.endpoint,
    )
    _write_report(
        cache_path,
        {
            "loaded_at": _static_cache.loaded_at.isoformat(),
            "rankings": [asdict(row) for row in rankings],
            "provenance": provenance,
            "ranking_digest": ranking_digest,
            "players": players_response.payload,
        },
    )
    return _static_cache


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(canonical_json(report) + "\n", encoding="utf-8")
    temporary.replace(path)


async def _load_league_data(
    cfg: Settings, sleeper: SleeperClient, *, force: bool = False
) -> dict[str, Any]:
    global _league_cache
    refresh_after = timedelta(minutes=15)
    if (
        not force
        and _league_cache is not None
        and datetime.now(timezone.utc) - _league_cache.loaded_at < refresh_after
    ):
        return _league_cache.payload
    response = await sleeper.league(cfg.sleeper_league_id)
    if not isinstance(response.payload, dict):
        raise ValueError("Sleeper league response is malformed")
    _league_cache = LeagueDraftData(
        payload=response.payload,
        loaded_at=datetime.now(timezone.utc),
    )
    return _league_cache.payload


def _autopick_configuration_blockers(
    cfg: Settings,
    draft: dict[str, Any],
    *,
    owner_slot: int | None,
    recommendation: dict[str, Any] | None,
    on_clock: bool,
    expert_error: str | None,
) -> list[str]:
    blockers: list[str] = []
    if not cfg.draft_auto_pick_enabled:
        blockers.append("Automatic draft picks are not enabled")
    if not cfg.draft_expert_enabled:
        blockers.append("The whole-roster expert decision model is not enabled")
    if cfg.draft_expert_provider == "openai_api" and (
        cfg.openai_api_key is None or not cfg.openai_api_key.get_secret_value().strip()
    ):
        blockers.append("OPENAI_API_KEY is not configured for the expert draft manager")
    if expert_error:
        blockers.append(expert_error)
    if cfg.execution_mode != "browser":
        blockers.append("Browser execution mode is not enabled")
    if "DRAFT_PLAYER" not in cfg.live_browser_actions:
        blockers.append("DRAFT_PLAYER is not enabled for the browser agent")
    is_configured_league_draft = str(draft.get("league_id") or "") == cfg.sleeper_league_id
    is_explicit_standalone_mock = _is_explicit_standalone_mock(cfg, draft)
    if not is_configured_league_draft and not is_explicit_standalone_mock:
        blockers.append("Draft is not attached to the configured Sleeper league")
    if str(draft.get("type") or "snake") != "snake":
        blockers.append("Only snake drafts are qualified for automatic picks")
    if owner_slot is None:
        blockers.append("Your Sleeper draft slot is unassigned")
    if on_clock and recommendation is None:
        blockers.append("No verified expert-model decision is available for this live board")
    elif (
        recommendation is not None
        and float(recommendation.get("confidence") or 0) < cfg.draft_min_confidence
    ):
        blockers.append(f"Recommendation confidence is below {cfg.draft_min_confidence:.0%}")
    return blockers


def _apply_emergency_fallback(
    programmatic_recommendation: dict[str, Any], *, expert_failure: str
) -> dict[str, Any]:
    """Promote the roster-aware baseline only after the expert path has failed."""
    candidate = programmatic_recommendation.get("recommendation")
    if not isinstance(candidate, dict) or not candidate.get("player_id"):
        raise ExpertDraftUnavailable("No roster-aware emergency candidate is available")

    selected = dict(candidate)
    selected["decision_source"] = "emergency_fallback"
    selected_reasons = [str(item) for item in (selected.get("reasons") or []) if item]
    fit_summary = "; ".join(selected_reasons[:3]) or "best verified whole-roster fit"

    fallbacks: list[dict[str, Any]] = []
    for fallback in programmatic_recommendation.get("fallbacks") or []:
        if not isinstance(fallback, dict):
            continue
        item = dict(fallback)
        item["decision_source"] = "emergency_fallback_alternative"
        fallbacks.append(item)

    grounding_hash = content_hash(
        {
            "decision_source": "emergency_fallback",
            "expert_failure": expert_failure,
            "selected_player_id": str(selected["player_id"]),
            "selected_score": selected.get("score"),
            "roster_summary": programmatic_recommendation.get("roster_summary"),
        }
    )
    return {
        **programmatic_recommendation,
        "recommendation": selected,
        "fallbacks": fallbacks[:4],
        "decision_source": "emergency_fallback",
        "expert_decision": {
            "provider": "programmatic_emergency",
            "model": None,
            "response_id": None,
            "confidence": selected.get("confidence"),
            "roster_strategy": (
                "Preserve the roster construction plan with the strongest verified "
                "roster-aware option after the expert service failed"
            ),
            "strategy_state": "emergency_fallback",
            "fit_summary": fit_summary,
            "key_tradeoffs": selected_reasons[:2],
            "grounding_hash": grounding_hash,
            "latency_ms": None,
            "used_web_search": False,
            "cached": False,
            "prompt_version": None,
            "fallback_trigger": expert_failure,
        },
        "explanation": (
            "Codex did not return a usable decision before the live deadline. The "
            "roster-aware emergency engine selected from the same verified live allowlist "
            "after the backend confirmed that the draft board had not changed."
        ),
    }


async def _queue_autopick(
    cfg: Settings,
    *,
    draft: dict[str, Any],
    picks: list[dict[str, Any]],
    owner_slot: int,
    recommendation: dict[str, Any],
    engine_version: str,
    ranking_digest: str,
    expert_decision: dict[str, Any],
    clock_timing: DraftClockTiming,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    decision_source = str(recommendation.get("decision_source") or "unknown")
    is_emergency_fallback = decision_source == "emergency_fallback"
    async with session_scope() as session:
        heartbeat = await session.scalar(
            select(ShimHeartbeat).where(ShimHeartbeat.agent_id == cfg.fantasy_agent_id)
        )
        if not _heartbeat_ready(cfg, heartbeat, now=now):
            return {
                "state": "blocked",
                "message": "The authenticated Playwright draft agent is not ready",
            }

        not_before = max(now, clock_timing.submit_at)
        expires_at = clock_timing.deadline - timedelta(seconds=2)
        if expires_at <= not_before:
            return {
                "state": "blocked",
                "message": "Too little time remains for a safely verified automatic pick",
            }
        pick_no = len(picks) + 1
        picks_hash = content_hash(picks)
        parameters = {
            "draft_id": str(draft["draft_id"]),
            "player_id": str(recommendation["player_id"]),
            "player_name": str(recommendation["player_name"]),
            "player_position": str(recommendation["position"]),
            "player_team": recommendation.get("team"),
            "expected_pick_no": pick_no,
            "owner_slot": owner_slot,
            "recommendation_score": recommendation.get("score"),
            "recommendation_engine_version": engine_version,
            "decision_source": decision_source,
            "decision_provider": expert_decision.get("provider"),
            "expert_provider": expert_decision.get("provider"),
            "expert_model": expert_decision.get("model"),
            "expert_response_id": expert_decision.get("response_id"),
            "expert_grounding_hash": expert_decision.get("grounding_hash"),
            "expert_roster_strategy": expert_decision.get("roster_strategy"),
            "expert_decision": expert_decision,
        }
        action, command = await propose_execution(
            session,
            action_type="DRAFT_PLAYER",
            league_id=cfg.sleeper_league_id,
            roster_id=cfg.sleeper_roster_id,
            parameters=parameters,
            expected_state_hash=picks_hash,
            evidence_hashes=[
                ranking_digest,
                picks_hash,
                str(expert_decision.get("grounding_hash") or ""),
            ],
            reason=(
                (
                    "Roster-aware emergency fallback selected "
                    if is_emergency_fallback
                    else "Whole-roster expert selected "
                )
                + f"{recommendation['player_name']} for pick #{pick_no}: "
                + str(expert_decision.get("fit_summary") or "best roster fit")
            ),
            confidence=float(recommendation.get("confidence") or 0),
            approval_required=False,
            not_before=not_before,
            expires_at=expires_at,
            verification_plan={
                "source": "Sleeper public API",
                "endpoint": f"draft/{draft['draft_id']}/picks",
                "expected_pick_no": pick_no,
                "expected_player_id": str(recommendation["player_id"]),
                "clock_deadline": clock_timing.deadline.isoformat(),
                "auto_submit_at": not_before.isoformat(),
            },
            dedupe_key=(
                f"draft:{draft['draft_id']}:pick:{pick_no}:"
                f"{picks_hash}:{recommendation['player_id']}"
            ),
            settings=cfg,
        )
        scheduled = command is not None and command.not_before > now
        mock_delay = (
            cfg.draft_mock_auto_submit_delay_seconds
            if _is_explicit_standalone_mock(cfg, draft)
            else None
        )
        return {
            "state": "scheduled" if scheduled else "queued",
            "message": (
                (
                    f"Planning {recommendation['player_name']}; mock release delay is "
                    f"{mock_delay} seconds"
                    if mock_delay is not None
                    else f"Planning {recommendation['player_name']}; releases to Playwright "
                    f"with {cfg.draft_auto_submit_seconds_remaining} seconds left"
                )
                if scheduled
                else f"Automatic pick released: {recommendation['player_name']}"
            ),
            "action_id": action.id,
            "command_id": command.id if command else None,
            "submit_at": command.not_before.isoformat() if command else None,
            "expires_at": command.expires_at.isoformat() if command else None,
        }


async def _existing_autopick_for_board(
    cfg: Settings,
    *,
    draft: dict[str, Any],
    picks: list[dict[str, Any]],
    owner_slot: int,
) -> dict[str, Any] | None:
    """Return the command already created for this exact live board, if any."""
    picks_hash = content_hash(picks)
    expected_pick_no = len(picks) + 1
    async with session_scope() as session:
        result = await session.execute(
            select(ExecutionCommand, ActionItem)
            .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
            .where(
                ExecutionCommand.action_type == "DRAFT_PLAYER",
                ExecutionCommand.league_id == cfg.sleeper_league_id,
                ExecutionCommand.roster_id == cfg.sleeper_roster_id,
                ExecutionCommand.expected_state_hash == picks_hash,
            )
            .order_by(ExecutionCommand.created_at.desc())
        )
        for command, action in result.all():
            parameters = dict(command.parameters or {})
            if (
                str(parameters.get("draft_id") or "") != str(draft.get("draft_id") or "")
                or int(parameters.get("expected_pick_no") or 0) != expected_pick_no
                or int(parameters.get("owner_slot") or 0) != owner_slot
            ):
                continue
            return {
                "action_id": action.id,
                "action_status": action.status,
                "action_confidence": action.confidence,
                "action_reason": action.primary_reason,
                "command_id": command.id,
                "command_status": command.status,
                "parameters": parameters,
                "not_before": command.not_before,
                "expires_at": command.expires_at,
            }
    return None


def _reuse_existing_autopick_recommendation(
    programmatic_recommendation: dict[str, Any], existing: dict[str, Any]
) -> dict[str, Any]:
    parameters = existing["parameters"]
    player_id = str(parameters["player_id"])
    candidate_pool = list(programmatic_recommendation.get("decision_pool") or [])
    selected = next(
        (dict(item) for item in candidate_pool if str(item.get("player_id") or "") == player_id),
        {
            "player_id": player_id,
            "player_name": str(parameters.get("player_name") or player_id),
            "position": str(parameters.get("player_position") or ""),
            "team": parameters.get("player_team"),
            "score": parameters.get("recommendation_score"),
            "reasons": [],
        },
    )
    decision_source = str(parameters.get("decision_source") or "expert_model")
    selected.update(
        player_name=str(parameters.get("player_name") or selected.get("player_name") or player_id),
        position=str(parameters.get("player_position") or selected.get("position") or ""),
        team=parameters.get("player_team") or selected.get("team"),
        confidence=float(existing.get("action_confidence") or 0),
        decision_source=decision_source,
    )
    expert_decision = dict(parameters.get("expert_decision") or {})
    expert_decision.setdefault("provider", parameters.get("expert_provider") or "codex_cli")
    expert_decision.setdefault("model", parameters.get("expert_model"))
    expert_decision.setdefault("response_id", parameters.get("expert_response_id"))
    expert_decision.setdefault("confidence", existing.get("action_confidence"))
    expert_decision.setdefault(
        "roster_strategy",
        parameters.get("expert_roster_strategy") or "Previously selected whole-roster plan",
    )
    expert_decision.setdefault("strategy_state", "selection_in_flight")
    expert_decision.setdefault("fit_summary", existing.get("action_reason") or "")
    expert_decision.setdefault("roster_risk", "")
    expert_decision.setdefault("next_two_turn_plan", "")
    expert_decision.setdefault("key_tradeoffs", [])
    expert_decision.setdefault("grounding_hash", parameters.get("expert_grounding_hash"))
    expert_decision.setdefault("latency_ms", 0)
    expert_decision.setdefault("used_web_search", False)
    expert_decision.setdefault("cached", False)
    expert_decision.setdefault("prompt_version", None)
    return {
        **programmatic_recommendation,
        "recommendation": selected,
        "decision_source": decision_source,
        "expert_decision": expert_decision,
        "explanation": (
            "The expert choice for this exact board is already in the execution outbox; "
            "the monitor will not request or queue a second decision."
        ),
    }


def _existing_autopick_status(existing: dict[str, Any]) -> dict[str, Any]:
    status = str(existing["command_status"])
    player_name = str(existing["parameters"].get("player_name") or "selected player")
    now = datetime.now(timezone.utc)
    if status in {"blocked", "failed", "unverified"}:
        state = "blocked"
        message = f"Existing automatic pick is {status}: {player_name}"
    elif status == "ready" and existing["not_before"] > now:
        state = "scheduled"
        message = f"Planning {player_name}; the existing command is awaiting its release time"
    elif status == "verified":
        state = "queued"
        message = f"Automatic pick verified: {player_name}; waiting for Sleeper's board update"
    else:
        state = "queued"
        message = f"Automatic pick already in progress: {player_name}"
    return {
        "state": state,
        "message": message,
        "action_id": existing["action_id"],
        "command_id": existing["command_id"],
        "submit_at": existing["not_before"].isoformat(),
        "expires_at": existing["expires_at"].isoformat(),
        "command_status": status,
    }


def _heartbeat_ready(
    cfg: Settings, heartbeat: ShimHeartbeat | None, *, now: datetime | None = None
) -> bool:
    checked_at = now or datetime.now(timezone.utc)
    return bool(
        heartbeat
        and heartbeat.observed_at
        >= checked_at - timedelta(seconds=max(cfg.draft_poll_seconds * 4, 10))
        and heartbeat.app_running
        and heartbeat.session_available
        and heartbeat.execution_mode == "browser"
        and "DRAFT_PLAYER" in (heartbeat.capabilities or [])
    )


async def _autopick_agent_ready(cfg: Settings) -> bool:
    async with session_scope() as session:
        heartbeat = await session.scalar(
            select(ShimHeartbeat).where(ShimHeartbeat.agent_id == cfg.fantasy_agent_id)
        )
        return _heartbeat_ready(cfg, heartbeat)


async def _refresh_unchanged_live_board(
    cfg: Settings,
    *,
    draft: dict[str, Any],
    picks: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Revalidate the exact draft snapshot before any delayed decision can execute."""
    async with SleeperClient(cfg) as live_sleeper:
        draft_response, picks_response = await asyncio.gather(
            live_sleeper.draft(str(draft["draft_id"])),
            live_sleeper.draft_picks(str(draft["draft_id"])),
        )
    refreshed_draft = draft_response.payload
    refreshed_picks = picks_response.payload
    if not isinstance(refreshed_draft, dict) or not isinstance(refreshed_picks, list):
        raise ExpertDraftUnavailable("Sleeper returned malformed refreshed draft state")
    if (
        str(refreshed_draft.get("draft_id") or "") != str(draft.get("draft_id") or "")
        or refreshed_draft.get("status") != "drafting"
        or content_hash(refreshed_picks) != content_hash(picks)
    ):
        raise ExpertDraftUnavailable("The live draft board changed while choosing a player")
    return refreshed_draft, refreshed_picks


async def build_draft_room(
    settings: Settings | None = None,
    *,
    draft_id: str | None = None,
    persist_observations: bool = True,
    force_static_refresh: bool = False,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    selected_draft_id = draft_id or cfg.sleeper_draft_id
    async with SleeperClient(cfg) as sleeper:
        if selected_draft_id:
            draft_response = await sleeper.draft(selected_draft_id)
        else:
            drafts_response = await sleeper.drafts(cfg.sleeper_league_id)
            drafts = drafts_response.payload if isinstance(drafts_response.payload, list) else []
            selected = _select_draft(drafts)
            draft_response = await sleeper.draft(str(selected["draft_id"]))
        if not isinstance(draft_response.payload, dict):
            raise ValueError("Sleeper draft response is malformed")
        draft = draft_response.payload
        picks_response, static, league = await asyncio.gather(
            sleeper.draft_picks(str(draft["draft_id"])),
            _load_static_data(cfg, sleeper, force=force_static_refresh),
            _load_league_data(cfg, sleeper, force=force_static_refresh),
        )

    picks = picks_response.payload if isinstance(picks_response.payload, list) else []
    settings_payload = draft.get("settings") or {}
    teams = max(int(settings_payload.get("teams") or 12), 1)
    rounds = max(int(settings_payload.get("rounds") or 16), 1)
    draft_order = draft.get("draft_order") or {}
    owner_slot_value = draft_order.get(cfg.sleeper_owner_user_id)
    owner_slot = int(owner_slot_value) if owner_slot_value is not None else None
    slot_to_roster = draft.get("slot_to_roster_id") or {}
    owner_roster_id = slot_to_roster.get(str(owner_slot)) if owner_slot else None
    if owner_roster_id is None:
        owner_roster_id = owner_slot or cfg.sleeper_roster_id

    roster_player_ids = {
        str(pick["player_id"])
        for pick in picks
        if pick.get("player_id")
        and (
            str(pick.get("picked_by") or "") == cfg.sleeper_owner_user_id
            or str(pick.get("roster_id") or "") == str(owner_roster_id)
        )
    }
    drafted = {str(pick["player_id"]) for pick in picks if pick.get("player_id")}
    next_pick_no = len(picks) + 1
    owner_pick_no = _next_owner_pick(next_pick_no, owner_slot, teams)
    following_owner_pick = _next_owner_pick(owner_pick_no + 1, owner_slot, teams)
    on_clock = bool(
        owner_slot and draft.get("status") == "drafting" and owner_pick_no == next_pick_no
    )
    team_roster_counts, recent_position_counts = _position_counts_by_draft_slot(
        picks, static.players, teams
    )
    team_roster_player_ids = _player_ids_by_draft_slot(picks, teams)
    governing_settings = _governing_draft_settings(league, settings_payload)
    governing_scoring_type = _governing_scoring_type(league, draft)
    explicit_standalone_mock = _is_explicit_standalone_mock(cfg, draft)
    mock_submit_delay = (
        cfg.draft_mock_auto_submit_delay_seconds if explicit_standalone_mock else None
    )
    clock_timing = _draft_clock_timing(
        draft,
        submit_seconds_remaining=cfg.draft_auto_submit_seconds_remaining,
        submit_delay_seconds=mock_submit_delay,
    )
    recommendation = recommend_draft_pick(
        rankings=static.matched,
        drafted_player_ids=drafted,
        roster_player_ids=roster_player_ids,
        players=static.players,
        owner_pick_no=owner_pick_no,
        following_owner_pick=following_owner_pick,
        draft_settings=governing_settings,
        scoring_type=governing_scoring_type,
        owner_slot=owner_slot,
        team_roster_counts=team_roster_counts,
        team_roster_player_ids=team_roster_player_ids,
        recent_position_counts=recent_position_counts,
        playoff_settings=league.get("settings") or {},
    )
    recommendation["decision_source"] = "programmatic_preview"
    recommendation["explanation"] = (
        "Programmatic grounding preview only. The whole-roster expert model makes the "
        "executable choice from the live board when your team is on the clock."
    )
    existing_autopick = (
        await _existing_autopick_for_board(
            cfg,
            draft=draft,
            picks=picks,
            owner_slot=owner_slot,
        )
        if on_clock and owner_slot is not None
        else None
    )
    slot_plan = build_slot_draft_plan(
        rankings=static.matched,
        players=static.players,
        owner_slot=owner_slot or cfg.sleeper_roster_id,
        draft_settings=governing_settings,
        scoring_type=governing_scoring_type,
        drafted_player_ids=drafted,
        roster_player_ids=roster_player_ids,
    )
    draft_complete = draft.get("status") == "complete" or len(picks) >= teams * rounds
    expert_error: str | None = None
    expert_status: dict[str, Any] = {
        "enabled": cfg.draft_expert_enabled,
        "provider": cfg.draft_expert_provider,
        "model": cfg.draft_expert_model,
        "emergency_fallback_enabled": cfg.draft_expert_emergency_fallback_enabled,
        "state": "waiting_for_clock",
        "objective": "Maximize the completed roster's championship probability",
        "uses_web_search": cfg.draft_expert_web_search_enabled,
    }
    if draft_complete:
        recommendation["recommendation"] = None
        recommendation["fallbacks"] = []
        expert_status["state"] = "complete"
    elif existing_autopick is not None:
        recommendation = _reuse_existing_autopick_recommendation(recommendation, existing_autopick)
        expert_status.update(
            state="selected",
            decision=recommendation.get("expert_decision"),
            reused_existing_command=True,
        )
    elif not cfg.draft_expert_enabled:
        expert_status["state"] = "disabled"
    elif cfg.draft_expert_provider == "openai_api" and (
        cfg.openai_api_key is None or not cfg.openai_api_key.get_secret_value().strip()
    ):
        expert_status["state"] = "not_configured"
    elif on_clock and owner_slot is not None:
        expert_status["state"] = "reasoning"
        candidate_turn_paths = build_candidate_turn_paths(
            rankings=static.matched,
            players=static.players,
            candidates=list(recommendation.get("decision_pool") or []),
            drafted_player_ids=drafted,
            roster_player_ids=roster_player_ids,
            owner_slot=owner_slot,
            owner_pick_no=owner_pick_no,
            following_owner_pick=following_owner_pick,
            draft_settings=governing_settings,
            scoring_type=governing_scoring_type,
        )
        expert_context = build_expert_context(
            league=league,
            draft=draft,
            picks=picks,
            players=static.players,
            owner_slot=owner_slot,
            owner_roster_id=owner_roster_id,
            owner_user_id=cfg.sleeper_owner_user_id,
            owner_pick_no=owner_pick_no,
            following_owner_pick=following_owner_pick,
            programmatic_recommendation=recommendation,
            candidate_pool_size=cfg.draft_expert_candidate_pool_size,
            provenance=static.provenance,
            slot_plan=slot_plan,
            candidate_turn_paths=candidate_turn_paths,
        )
        try:
            expert_result = await choose_expert_draft_pick(
                settings=cfg,
                context=expert_context,
            )
            recommendation = apply_expert_decision(recommendation, expert_result)
        except ExpertDraftUnavailable as exc:
            expert_failure = f"Expert decision unavailable: {exc}"
            if cfg.draft_expert_emergency_fallback_enabled:
                try:
                    refreshed_draft, refreshed_picks = await _refresh_unchanged_live_board(
                        cfg,
                        draft=draft,
                        picks=picks,
                    )
                    recommendation = _apply_emergency_fallback(
                        recommendation,
                        expert_failure=expert_failure,
                    )
                except ExpertDraftUnavailable as fallback_exc:
                    expert_error = f"{expert_failure}; emergency fallback blocked: {fallback_exc}"
                    expert_status.update(
                        state="blocked",
                        error=str(exc),
                        fallback_error=str(fallback_exc),
                    )
                else:
                    draft = refreshed_draft
                    picks = refreshed_picks
                    clock_timing = _draft_clock_timing(
                        draft,
                        submit_seconds_remaining=cfg.draft_auto_submit_seconds_remaining,
                        submit_delay_seconds=mock_submit_delay,
                    )
                    expert_status.update(
                        state="fallback_selected",
                        error=str(exc),
                        decision=recommendation.get("expert_decision"),
                    )
            else:
                expert_error = expert_failure
                expert_status.update(state="blocked", error=str(exc))
        except Exception as exc:
            expert_error = f"Expert decision unavailable: {type(exc).__name__}"
            expert_status.update(state="blocked", error=type(exc).__name__)
        else:
            try:
                # Model reasoning can take tens of seconds. Re-read both live
                # endpoints before scheduling so a manual pick invalidates the
                # decision and the release time uses Sleeper's newest clock.
                refreshed_draft, refreshed_picks = await _refresh_unchanged_live_board(
                    cfg,
                    draft=draft,
                    picks=picks,
                )
            except ExpertDraftUnavailable as exc:
                expert_error = f"Expert decision unavailable: {exc}"
                expert_status.update(state="blocked", error=str(exc))
            else:
                draft = refreshed_draft
                picks = refreshed_picks
                clock_timing = _draft_clock_timing(
                    draft,
                    submit_seconds_remaining=cfg.draft_auto_submit_seconds_remaining,
                    submit_delay_seconds=mock_submit_delay,
                )
                expert_status.update(
                    state="selected",
                    decision=recommendation.get("expert_decision"),
                )

    candidate = (
        recommendation.get("recommendation")
        if recommendation.get("decision_source") in {"expert_model", "emergency_fallback"}
        else None
    )
    autopick_blockers = _autopick_configuration_blockers(
        cfg,
        draft,
        owner_slot=owner_slot,
        recommendation=candidate,
        on_clock=on_clock,
        expert_error=expert_error,
    )
    autopick: dict[str, Any] = {
        "enabled": cfg.draft_auto_pick_enabled,
        "state": "blocked" if autopick_blockers else "armed",
        "message": autopick_blockers[0] if autopick_blockers else "Automatic picks are armed",
        "candidate": candidate.get("player_name") if candidate else None,
    }
    runtime_blockers: list[str] = []
    if draft_complete:
        autopick.update(state="complete", message="Draft complete")
    elif existing_autopick is not None and not autopick_blockers:
        existing_status = _existing_autopick_status(existing_autopick)
        autopick.update(existing_status)
        if existing_status["state"] == "blocked":
            runtime_blockers.append(str(existing_status["message"]))
    elif not autopick_blockers:
        try:
            if not await _autopick_agent_ready(cfg):
                runtime_blockers.append("The authenticated Playwright draft agent is not ready")
                autopick.update(state="blocked", message=runtime_blockers[-1])
            elif on_clock and candidate is not None and owner_slot is not None:
                if clock_timing is None:
                    runtime_blockers.append("The live Sleeper pick clock is unavailable")
                    autopick.update(state="blocked", message=runtime_blockers[-1])
                else:
                    autopick.update(
                        await _queue_autopick(
                            cfg,
                            draft=draft,
                            picks=picks,
                            owner_slot=owner_slot,
                            recommendation=candidate,
                            engine_version=str(recommendation.get("engine_version") or "unknown"),
                            ranking_digest=static.ranking_digest,
                            expert_decision=recommendation.get("expert_decision") or {},
                            clock_timing=clock_timing,
                        )
                    )
        except Exception as exc:
            runtime_blockers.append(f"Automatic pick queue is unavailable: {type(exc).__name__}")
            autopick.update(
                state="blocked",
                message=runtime_blockers[-1],
            )

    recent_picks = []
    for pick in picks[-8:]:
        player = static.players.get(str(pick.get("player_id") or ""), {})
        metadata = pick.get("metadata") or {}
        position = player.get("position") or metadata.get("position")
        fallback_name = f"{metadata.get('first_name', '')} {metadata.get('last_name', '')}".strip()
        if not fallback_name and position in {"DEF", "DST"}:
            fallback_name = f"{pick.get('player_id')} Defense"
        recent_picks.append(
            {
                "pick_no": pick.get("pick_no"),
                "round": pick.get("round"),
                "draft_slot": pick.get("draft_slot"),
                "player_id": pick.get("player_id"),
                "player_name": player.get("full_name") or fallback_name,
                "position": position,
                "team": player.get("team") or metadata.get("team") or pick.get("player_id"),
                "is_mine": (
                    str(pick.get("picked_by") or "") == cfg.sleeper_owner_user_id
                    or str(pick.get("roster_id") or "") == str(owner_roster_id)
                ),
            }
        )

    recommendation.pop("decision_pool", None)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "draft_id": str(draft["draft_id"]),
        "status": draft.get("status"),
        "draft_name": (draft.get("metadata") or {}).get("name") or "Sleeper draft",
        "format": {
            "type": draft.get("type") or "snake",
            "teams": teams,
            "rounds": rounds,
            "scoring": (draft.get("metadata") or {}).get("scoring_type"),
            "pick_timer_seconds": int(settings_payload.get("pick_timer") or 0),
        },
        "owner_slot": owner_slot,
        "owner_roster_id": owner_roster_id,
        "on_clock": on_clock,
        "pick_count": len(picks),
        "current_pick_no": next_pick_no,
        "next_owner_pick": None if draft_complete else owner_pick_no,
        "following_owner_pick": None if draft_complete else following_owner_pick,
        "picks_until_on_clock": None if draft_complete else max(owner_pick_no - next_pick_no, 0),
        "clock": {
            **(
                clock_timing.as_report(now=datetime.now(timezone.utc))
                if clock_timing is not None
                else {}
            ),
            "auto_submit_seconds_remaining": cfg.draft_auto_submit_seconds_remaining,
            "auto_submit_delay_seconds": mock_submit_delay,
            "submit_policy": (
                "mock_delay" if explicit_standalone_mock else "live_seconds_remaining"
            ),
        },
        "recommendation": recommendation,
        "expert_manager": expert_status,
        "slot_plan": slot_plan,
        "autopick": autopick,
        "recent_picks": recent_picks,
        "simulations": simulate_all_draft_slots(static.rankings, rounds=rounds, teams=teams),
        "provenance": static.provenance,
        "mapping": {
            "matched": len(static.matched),
            "rejected_ambiguous_or_mismatched": len(static.rejected),
        },
        "execution_blockers": autopick_blockers + runtime_blockers,
    }
    _write_report(cfg.reports_dir / "draft-room.json", report)

    if persist_observations:
        async with session_scope() as session:
            await record_observation(
                session,
                provider="DynastyProcess",
                endpoint="db_fpecr_latest.csv",
                entity_type="rankings",
                entity_id=f"{draft.get('season') or 'current'}-redraft",
                payload=[asdict(row) for row in static.rankings],
                normalized=static.provenance,
                confidence=0.8,
            )
            await update_source_health(
                session, "DynastyProcess", success=True, digest=static.ranking_digest
            )
            await record_observation(
                session,
                provider="Sleeper public API",
                endpoint=picks_response.endpoint,
                entity_type="draft-picks",
                entity_id=str(draft["draft_id"]),
                payload=picks,
                normalized={"pick_count": len(picks)},
            )
            await update_source_health(
                session,
                "Sleeper public API",
                success=True,
                latency_ms=picks_response.latency_ms,
                digest=content_hash(picks),
            )
    return report
