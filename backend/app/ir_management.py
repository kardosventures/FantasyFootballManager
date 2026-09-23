from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.action_qualification import action_is_qualified
from app.config import Settings
from app.database import session_scope
from app.execution import propose_execution
from app.models import ActionItem, ExecutionCommand
from app.utils import content_hash

UTC = timezone.utc
BASE_RESERVE_ELIGIBLE_STATUSES = {"IR", "PUP", "NFI"}
OPTIONAL_RESERVE_STATUSES = {
    "reserve_allow_cov": {"COV", "COVID", "COVID-19"},
    "reserve_allow_dnr": {"DNR"},
    "reserve_allow_doubtful": {"DOUBTFUL"},
    "reserve_allow_na": {"NA"},
    "reserve_allow_out": {"OUT"},
    "reserve_allow_sus": {"SUS", "SUSP", "SUSPENDED"},
}


def reserve_status(player: dict[str, Any]) -> str:
    values = (player.get("injury_status"), player.get("status"))
    status = next((str(value).strip().upper() for value in values if value), "UNKNOWN")
    if status == "INJURED RESERVE" or status.startswith("IR-"):
        return "IR"
    if status.startswith("PUP-"):
        return "PUP"
    if status.startswith("NFI-"):
        return "NFI"
    return status


def reserve_capacity(league: dict[str, Any]) -> int:
    """Return Sleeper's authoritative reserve-slot count with legacy fallback."""

    reserve = league.get("reserve") or {}
    settings = league.get("settings") or {}
    for value in (reserve.get("slots"), settings.get("reserve_slots")):
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return sum(
        1
        for slot in league.get("roster_positions") or []
        if str(slot).upper() in {"IR", "RESERVE"}
    )


def reserve_eligible_statuses(league: dict[str, Any]) -> set[str]:
    """Translate Sleeper's reserve eligibility flags into normalized statuses."""

    reserve = league.get("reserve") or {}
    configured = reserve.get("eligible_statuses")
    if isinstance(configured, list):
        return {str(status).upper() for status in configured if status}

    raw_settings = reserve.get("raw_sleeper_settings") or league.get("settings") or {}
    statuses = set(BASE_RESERVE_ELIGIBLE_STATUSES)
    for setting, allowed_statuses in OPTIONAL_RESERVE_STATUSES.items():
        try:
            enabled = int(raw_settings.get(setting) or 0) == 1
        except (TypeError, ValueError):
            enabled = False
        if enabled:
            statuses.update(allowed_statuses)
    return statuses


def is_reserve_eligible(player: dict[str, Any], league: dict[str, Any]) -> bool:
    return reserve_status(player) in reserve_eligible_statuses(league)


def build_ir_plan(context: dict[str, Any]) -> dict[str, Any]:
    """Build exact reserve-array changes from authoritative roster and injury state."""
    team = context.get("our_team") or {}
    league = context.get("league") or {}
    roster = list(team.get("all_players") or [])
    roster_by_id = {str(row.get("player_id")): row for row in roster}
    reserve_ids = [str(row.get("player_id")) for row in team.get("reserve") or []]
    capacity = reserve_capacity(league)
    if capacity <= 0:
        season_horizon = context.get("season_horizon") or {}
        capacity = int(
            (season_horizon.get("reserve_management") or {}).get("reserve_capacity")
            or (season_horizon.get("roster_construction") or {}).get("reserve_capacity")
            or 0
        )
    if capacity <= 0:
        return {
            "status": "not_applicable",
            "reason": "This league has no injured-reserve slots",
            "reserve_capacity": 0,
            "reserve_occupied": len(reserve_ids),
            "actions": [],
            "blocked": [],
            "final_reserve_player_ids": list(reserve_ids),
        }
    actions: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    working = list(reserve_ids)

    for player_id in list(working):
        player = roster_by_id.get(player_id) or {}
        if is_reserve_eligible(player, league):
            continue
        if player.get("locked"):
            blocked.append(
                {
                    "player_id": player_id,
                    "player_name": player.get("name") or player_id,
                    "action_type": "REMOVE_FROM_IR",
                    "reason": "Player is no longer IR-eligible but is locked",
                }
            )
            continue
        before = list(working)
        working.remove(player_id)
        actions.append(_ir_action("REMOVE_FROM_IR", player, before, working, roster, context))

    available_slots = max(0, capacity - len(working))
    candidates = sorted(
        (
            row
            for row in roster
            if str(row.get("player_id")) not in working
            and is_reserve_eligible(row, league)
        ),
        key=lambda row: (
            reserve_status(row) != "IR",
            reserve_status(row) != "PUP",
            int(row.get("overall_rank") or 10_000),
            str(row.get("player_id")),
        ),
    )
    for player in candidates:
        player_id = str(player.get("player_id"))
        if available_slots <= 0:
            blocked.append(
                {
                    "player_id": player_id,
                    "player_name": player.get("name") or player_id,
                    "action_type": "MOVE_TO_IR",
                    "reason": "No open injured-reserve slot",
                }
            )
            continue
        if player.get("locked"):
            blocked.append(
                {
                    "player_id": player_id,
                    "player_name": player.get("name") or player_id,
                    "action_type": "MOVE_TO_IR",
                    "reason": "Eligible player is locked",
                }
            )
            continue
        before = list(working)
        working.append(player_id)
        actions.append(_ir_action("MOVE_TO_IR", player, before, working, roster, context))
        available_slots -= 1

    for sequence, action in enumerate(actions, start=1):
        action["sequence"] = sequence

    return {
        "status": "ready" if actions else "blocked" if blocked else "no_action",
        "reserve_capacity": capacity,
        "reserve_occupied": len(reserve_ids),
        "actions": actions,
        "blocked": blocked,
        "final_reserve_player_ids": working,
    }


def _ir_action(
    action_type: str,
    player: dict[str, Any],
    before: list[str],
    after: list[str],
    roster: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    player_id = str(player.get("player_id"))
    parameters = {
        "player_id": player_id,
        "player_name": str(player.get("name") or player_id),
        "player_display_name": str(
            player.get("sleeper_ui_display_name") or player.get("name") or player_id
        ),
        "player_position": str(player.get("position") or ""),
        "player_team": str(player.get("team") or ""),
        "observed_status": reserve_status(player),
        "expected_players": [str(row.get("player_id")) for row in roster],
        "expected_reserve_before": list(before),
        "expected_reserve_after": list(after),
    }
    return {
        "sequence": 0,
        "action_type": action_type,
        "parameters": parameters,
        "expected_state_hash": content_hash(
            {
                "league_id": (context.get("league") or {}).get("league_id"),
                "roster_id": (context.get("our_team") or {}).get("roster_id"),
                "players": parameters["expected_players"],
                "reserve": before,
            }
        ),
        "reason": (
            f"Move {parameters['player_name']} into the open injured-reserve slot"
            if action_type == "MOVE_TO_IR"
            else f"Remove active {parameters['player_name']} from injured reserve"
        ),
    }


def _execution_window(
    settings: Settings,
    context: dict[str, Any],
    action: dict[str, Any],
    now: datetime,
) -> tuple[datetime, datetime]:
    player_id = str(action["parameters"]["player_id"])
    player = next(
        (
            row
            for row in (context.get("our_team") or {}).get("all_players") or []
            if str(row.get("player_id")) == player_id
        ),
        {},
    )
    kickoff_raw = player.get("kickoff")
    expires_at = now + timedelta(hours=6)
    if kickoff_raw:
        kickoff = datetime.fromisoformat(str(kickoff_raw).replace("Z", "+00:00")).astimezone(UTC)
        expires_at = min(
            expires_at,
            kickoff - timedelta(minutes=settings.in_season_lineup_expire_minutes_before_kickoff),
        )
    if expires_at <= now:
        raise ValueError("The safe IR execution window has closed")
    return now, expires_at


async def synchronize_ir_actions(
    settings: Settings,
    context: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not settings.in_season_ir_actions_enabled:
        return {"state": "analysis_only", "queued": [], "preview_count": len(actions)}
    if settings.execution_mode != "browser":
        return {"state": "blocked", "queued": [], "reason": "EXECUTION_MODE must be browser"}
    if any(action["action_type"] not in settings.live_browser_actions for action in actions):
        return {
            "state": "blocked",
            "queued": [],
            "reason": "Every IR action must be present in BROWSER_LIVE_ACTIONS",
        }
    if not actions:
        return {"state": "no_action", "queued": [], "preview_count": 0}
    unqualified = sorted(
        {
            str(action["action_type"])
            for action in actions
            if not action_is_qualified(settings, str(action["action_type"]))
        }
    )
    if unqualified:
        return {
            "state": "blocked",
            "queued": [],
            "preview_count": len(actions),
            "reason": f"Live qualification is missing for: {', '.join(unqualified)}",
        }
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    queued: list[dict[str, Any]] = []
    async with session_scope() as session:
        active = (
            await session.execute(
                select(ExecutionCommand, ActionItem)
                .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                .where(
                    ExecutionCommand.action_type.in_(["MOVE_TO_IR", "REMOVE_FROM_IR"]),
                    ExecutionCommand.league_id == settings.sleeper_league_id,
                    ExecutionCommand.roster_id == settings.sleeper_roster_id,
                    ExecutionCommand.status.in_(["ready", "leased"]),
                )
            )
        ).all()
        if any(
            command.status == "leased"
            and command.lease_until is not None
            and command.lease_until > current_time
            for command, _ in active
        ):
            return {"state": "blocked", "queued": [], "reason": "An IR action is executing"}
        desired_keys = {
            f"in-season-ir:{action['action_type']}:{content_hash(action['parameters'])}"
            for action in actions
        }
        existing = {action.dedupe_key: (command, action) for command, action in active}
        for command, action in active:
            if action.dedupe_key in desired_keys:
                continue
            command.status = "blocked"
            command.completed_at = current_time
            command.result = {"reason": "Superseded by newer IR state"}
            action.status = "superseded"
        for index, preview in enumerate(actions):
            preview["sequence"] = index + 1
            dedupe_key = (
                f"in-season-ir:{preview['action_type']}:{content_hash(preview['parameters'])}"
            )
            if dedupe_key in existing:
                command, action = existing[dedupe_key]
            else:
                try:
                    not_before, expires_at = _execution_window(
                        settings, context, preview, current_time + timedelta(seconds=index * 2)
                    )
                except ValueError as exc:
                    return {"state": "blocked", "queued": queued, "reason": str(exc)}
                action, command = await propose_execution(
                    session,
                    action_type=preview["action_type"],
                    league_id=settings.sleeper_league_id,
                    roster_id=settings.sleeper_roster_id,
                    parameters=preview["parameters"],
                    expected_state_hash=preview["expected_state_hash"],
                    evidence_hashes=[str(context["state_hash"])],
                    reason=preview["reason"],
                    confidence=1.0,
                    approval_required=False,
                    not_before=not_before,
                    expires_at=expires_at,
                    verification_plan={
                        "source": "Sleeper public roster API",
                        "expected_reserve": preview["parameters"]["expected_reserve_after"],
                        "retry_after_uncertain_write": False,
                    },
                    dedupe_key=dedupe_key,
                    settings=settings,
                )
            if command is None:
                return {"state": "blocked", "queued": queued, "reason": "IR command rejected"}
            queued.append(
                {
                    "sequence": index + 1,
                    "action_id": action.id,
                    "command_id": command.id,
                    "status": command.status,
                    "not_before": command.not_before.isoformat(),
                    "expires_at": command.expires_at.isoformat(),
                }
            )
    return {"state": "scheduled", "queued": queued, "preview_count": len(actions)}
