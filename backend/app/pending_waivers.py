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
from app.waivers import next_weekly_waiver

UTC = timezone.utc
PENDING_ACTION_TYPES = {"CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS"}
PENDING_WAIVER_COMMAND_VERSION = "pending-waiver-browser-2026.2"


def _claim_key(row: dict[str, Any]) -> tuple[str, str]:
    adds = row.get("add_player_ids") or []
    drops = row.get("drop_player_ids") or []
    return (str(adds[0]) if len(adds) == 1 else "", str(drops[0]) if len(drops) == 1 else "")


def _identity(
    row: dict[str, Any],
    roster_by_id: dict[str, dict[str, Any]],
    free_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    add_id, drop_id = _claim_key(row)
    add = free_by_id.get(add_id) or {}
    drop = roster_by_id.get(drop_id) or {}
    return {
        "add_player_id": add_id,
        "add_player_name": str(
            row.get("add_player_ui_display_name")
            or add.get("sleeper_ui_display_name")
            or (row.get("add_player_names") or [add_id])[0]
        ),
        "add_player_position": str(add.get("position") or ""),
        "add_player_team": str(add.get("team") or ""),
        "drop_player_id": drop_id,
        "drop_player_name": str(
            row.get("drop_player_ui_display_name")
            or drop.get("sleeper_ui_display_name")
            or (row.get("drop_player_names") or [drop_id or ""])[0]
        )
        if drop_id
        else "",
        "drop_player_position": str(drop.get("position") or "") if drop_id else "",
        "drop_player_team": str(drop.get("team") or "") if drop_id else "",
    }


def compile_pending_waiver_actions(
    decision: dict[str, Any], context: dict[str, Any]
) -> list[dict[str, Any]]:
    management = decision.get("pending_waiver_management") or {}
    status = str(management.get("status") or "needs_data")
    if status in {"keep", "needs_data"}:
        return []
    current_rows = list(
        (context.get("season_horizon") or {}).get("pending_waiver_claims") or []
    )
    current_rows.sort(key=lambda row: int(row.get("sequence") or 0))
    roster_by_id = {
        str(row.get("player_id")): row
        for row in (context.get("our_team") or {}).get("all_players") or []
    }
    free_by_id = {
        str(row.get("player_id")): row for row in context.get("free_agent_candidates") or []
    }
    working = [_identity(row, roster_by_id, free_by_id) for row in current_rows]
    desired_keys = [
        (str(row.get("add_player_id") or ""), str(row.get("drop_player_id") or ""))
        for row in management.get("desired_order") or []
    ]
    desired_set = set(desired_keys)
    actions: list[dict[str, Any]] = []

    for claim in reversed(list(working)):
        key = (claim["add_player_id"], claim["drop_player_id"])
        if key in desired_set:
            continue
        before = list(working)
        working = [
            row
            for row in working
            if (row["add_player_id"], row["drop_player_id"]) != key
        ]
        actions.append(
            {
                "sequence": len(actions) + 1,
                "action_type": "CANCEL_WAIVER_CLAIM",
                "parameters": {
                    "ui_contract_version": PENDING_WAIVER_COMMAND_VERSION,
                    "claim": claim,
                    "expected_claims_before": before,
                    "expected_claims_after": list(working),
                    "management_reason": str(management.get("reason") or ""),
                },
                "reason": str(management.get("reason") or "Cancel obsolete pending claim"),
                "expected_state_hash": content_hash(before),
            }
        )

    current_keys = [(row["add_player_id"], row["drop_player_id"]) for row in working]
    desired_existing = [key for key in desired_keys if key in set(current_keys)]
    if len(working) > 1 and current_keys != desired_existing and set(current_keys) == set(
        desired_existing
    ):
        ordered = [
            next(
                row
                for row in working
                if (row["add_player_id"], row["drop_player_id"]) == key
            )
            for key in desired_existing
        ]
        actions.append(
            {
                "sequence": len(actions) + 1,
                "action_type": "REORDER_WAIVER_CLAIMS",
                "parameters": {
                    "ui_contract_version": PENDING_WAIVER_COMMAND_VERSION,
                    "expected_claims_before": list(working),
                    "expected_claims_after": ordered,
                    "management_reason": str(management.get("reason") or ""),
                },
                "reason": str(management.get("reason") or "Reorder pending claims"),
                "expected_state_hash": content_hash(working),
            }
        )
    return actions


async def synchronize_pending_waiver_actions(
    settings: Settings,
    context: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not settings.in_season_pending_waiver_actions_enabled:
        return {"state": "analysis_only", "queued": [], "preview_count": len(actions)}
    if settings.execution_mode != "browser":
        return {"state": "blocked", "queued": [], "reason": "EXECUTION_MODE must be browser"}
    if any(action["action_type"] not in settings.live_browser_actions for action in actions):
        return {
            "state": "blocked",
            "queued": [],
            "reason": "Every pending-waiver action must be present in BROWSER_LIVE_ACTIONS",
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
    expires_at = next_weekly_waiver(
        now=current_time,
        weekday=settings.waiver_process_weekday,
        hour=settings.waiver_process_hour,
        timezone_name=settings.app_timezone,
    ) - timedelta(minutes=settings.in_season_waiver_expire_minutes_before_processing)
    if expires_at <= current_time:
        return {"state": "blocked", "queued": [], "reason": "Waiver edit window closed"}
    queued: list[dict[str, Any]] = []
    async with session_scope() as session:
        active = (
            await session.execute(
                select(ExecutionCommand, ActionItem)
                .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                .where(
                    ExecutionCommand.action_type.in_(sorted(PENDING_ACTION_TYPES)),
                    ExecutionCommand.league_id == settings.sleeper_league_id,
                    ExecutionCommand.roster_id == settings.sleeper_roster_id,
                    ExecutionCommand.status.in_(["ready", "leased"]),
                )
            )
        ).all()
        existing = {action.dedupe_key: (command, action) for command, action in active}
        planned_keys = {
            f"pending-waiver:{action['action_type']}:{content_hash(action['parameters'])}"
            for action in actions
        }
        for command, action in active:
            if action.dedupe_key in planned_keys:
                continue
            if command.status == "leased" and command.lease_until and command.lease_until > current_time:
                return {
                    "state": "blocked",
                    "queued": [],
                    "reason": "A pending-waiver change is already executing",
                }
            command.status = "blocked"
            command.completed_at = current_time
            action.status = "superseded"
        for index, preview in enumerate(actions):
            dedupe_key = (
                f"pending-waiver:{preview['action_type']}:{content_hash(preview['parameters'])}"
            )
            if dedupe_key in existing:
                command, action = existing[dedupe_key]
            else:
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
                    not_before=current_time + timedelta(seconds=index * 2),
                    expires_at=expires_at,
                    verification_plan={
                        "source": "Sleeper authenticated My Waivers UI",
                        "expected_claims": preview["parameters"]["expected_claims_after"],
                        "retry_after_uncertain_write": False,
                    },
                    dedupe_key=dedupe_key,
                    settings=settings,
                )
            if command is None:
                return {"state": "blocked", "queued": queued, "reason": "Command rejected"}
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
