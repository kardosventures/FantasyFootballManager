from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.action_qualification import (
    action_is_qualified,
    multi_claim_is_qualified,
    waiver_selector_is_qualified,
    waiver_submission_is_qualified,
)
from app.config import Settings
from app.database import session_scope
from app.execution import propose_execution
from app.models import ActionItem, ExecutionCommand
from app.policy import DropEvidence, evaluate_drop
from app.readiness import read_report
from app.utils import content_hash

UTC = timezone.utc


def _kickoff_by_player(context: dict[str, Any]) -> dict[str, datetime]:
    result: dict[str, datetime] = {}
    for player in (context.get("our_team") or {}).get("all_players") or []:
        raw = player.get("kickoff")
        if not raw:
            continue
        try:
            kickoff = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        result[str(player["player_id"])] = kickoff.astimezone(UTC)
    return result


def lineup_execution_window(
    settings: Settings,
    context: dict[str, Any],
    swaps: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    kickoffs = _kickoff_by_player(context)
    involved_ids = {
        str(player_id)
        for swap in swaps
        for player_id in (
            swap["parameters"]["from_player_id"],
            swap["parameters"]["to_player_id"],
        )
    }
    involved_kickoffs = [kickoffs[player_id] for player_id in involved_ids if player_id in kickoffs]
    if len(involved_kickoffs) != len(involved_ids):
        raise ValueError("Every lineup-swap player requires a verified kickoff")
    first_lock = min(involved_kickoffs)
    not_before = max(
        current_time,
        first_lock - timedelta(minutes=settings.in_season_lineup_submit_minutes_before_kickoff),
    )
    expires_at = first_lock - timedelta(
        minutes=settings.in_season_lineup_expire_minutes_before_kickoff
    )
    if not_before >= expires_at:
        raise ValueError("The safe lineup execution window has already closed")
    return not_before, expires_at


def _lineup_prerequisite_error(
    settings: Settings, context: dict[str, Any], decision: dict[str, Any]
) -> str | None:
    if settings.execution_mode != "browser":
        return "EXECUTION_MODE must be browser"
    if "SET_LINEUP" not in settings.live_browser_actions:
        return "SET_LINEUP is not present in BROWSER_LIVE_ACTIONS"
    if decision.get("lineup_status") != "ready":
        return "The expert lineup decision is not ready"
    if float(decision.get("confidence") or 0) < settings.in_season_lineup_min_confidence:
        return "The expert lineup confidence is below the execution threshold"
    quality = context.get("data_quality") or {}
    evidence_gate = quality.get("evidence_gate") or {}
    if evidence_gate and not evidence_gate.get("allows_lineup_execution", False):
        return "Required evidence failed the lineup execution gate"
    if int(quality.get("sleeper_ui_projection_count") or 0) < 11:
        return "The authenticated Sleeper lineup observation is incomplete"
    return None


async def synchronize_lineup_actions(
    settings: Settings,
    context: dict[str, Any],
    decision: dict[str, Any],
    swaps: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Synchronize one exact plan with the outbox without activating it by default."""
    if not settings.in_season_lineup_actions_enabled:
        return {"state": "analysis_only", "queued": [], "preview_count": len(swaps)}
    if not swaps:
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        async with session_scope() as session:
            active_rows = (
                await session.execute(
                    select(ExecutionCommand, ActionItem)
                    .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                    .where(
                        ExecutionCommand.action_type == "SET_LINEUP",
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
                for command, _ in active_rows
            ):
                return {
                    "state": "blocked",
                    "queued": [],
                    "reason": "A lineup command is already being executed",
                }
            for command, action in active_rows:
                command.status = "blocked"
                command.completed_at = current_time
                command.result = {"reason": "Fresh expert plan holds the current lineup"}
                action.status = "superseded"
        return {"state": "no_action", "queued": [], "preview_count": 0}
    prerequisite = _lineup_prerequisite_error(settings, context, decision)
    if prerequisite:
        return {"state": "blocked", "queued": [], "reason": prerequisite}

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    desired_dedupe = {
        f"in-season-lineup:{context.get('season')}:{context.get('week')}:"
        f"{content_hash(swap['parameters'])}": swap
        for swap in swaps
    }
    queued: list[dict[str, Any]] = []
    async with session_scope() as session:
        cancelled_action = await session.scalar(
            select(ActionItem).where(
                ActionItem.dedupe_key.in_(list(desired_dedupe)),
                ActionItem.status == "cancelled",
            )
        )
        if cancelled_action is not None:
            return {
                "state": "cancelled",
                "queued": [],
                "reason": "The owner cancelled this exact grounded lineup plan",
                "action_id": cancelled_action.id,
            }
        active_rows = (
            await session.execute(
                select(ExecutionCommand, ActionItem)
                .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                .where(
                    ExecutionCommand.action_type == "SET_LINEUP",
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
            for command, _ in active_rows
        ):
            return {
                "state": "blocked",
                "queued": [],
                "reason": "A lineup command is already being executed",
            }

        retained = set()
        for command, action in active_rows:
            if action.dedupe_key in desired_dedupe:
                retained.add(action.dedupe_key)
                queued.append(
                    {
                        "action_id": action.id,
                        "command_id": command.id,
                        "sequence": desired_dedupe[action.dedupe_key]["sequence"],
                        "not_before": command.not_before.isoformat(),
                        "expires_at": command.expires_at.isoformat(),
                        "status": command.status,
                    }
                )
                continue
            command.status = "blocked"
            command.completed_at = current_time
            command.result = {"reason": "Superseded by a newer grounded lineup plan"}
            action.status = "superseded"

        try:
            not_before, expires_at = lineup_execution_window(
                settings, context, swaps, now=current_time
            )
        except ValueError as exc:
            return {"state": "blocked", "queued": [], "reason": str(exc)}

        evidence_hashes = [
            str(context["state_hash"]),
            content_hash(decision),
        ]
        for dedupe_key, swap in desired_dedupe.items():
            if dedupe_key in retained:
                continue
            action, command = await propose_execution(
                session,
                action_type="SET_LINEUP",
                league_id=settings.sleeper_league_id,
                roster_id=settings.sleeper_roster_id,
                parameters=swap["parameters"],
                expected_state_hash=swap["expected_state_hash"],
                evidence_hashes=evidence_hashes,
                reason=swap["reason"],
                confidence=float(decision["confidence"]),
                approval_required=False,
                not_before=not_before,
                expires_at=expires_at,
                verification_plan={
                    "source": "Sleeper public roster API",
                    "expected_starters": swap["parameters"]["expected_starters_after"],
                    "retry_after_uncertain_write": False,
                },
                dedupe_key=dedupe_key,
                settings=settings,
            )
            if command is None:
                raise RuntimeError("An automatic lineup swap unexpectedly required approval")
            if command.status not in {"ready", "leased"}:
                return {
                    "state": "blocked",
                    "queued": [],
                    "reason": f"The exact lineup action is already {command.status}",
                    "action_id": action.id,
                }
            queued.append(
                {
                    "action_id": action.id,
                    "command_id": command.id,
                    "sequence": swap["sequence"],
                    "not_before": command.not_before.isoformat(),
                    "expires_at": command.expires_at.isoformat(),
                    "status": command.status,
                }
            )
    return {
        "state": "scheduled",
        "queued": sorted(queued, key=lambda row: row["sequence"]),
        "preview_count": len(swaps),
    }


def acquisition_execution_window(
    settings: Settings,
    context: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    action_types = {str(action.get("action_type") or "") for action in actions}
    if action_types == {"WAIVER_CLAIM"}:
        raw_processing = (context.get("season_horizon") or {}).get(
            "next_waiver_processing_at"
        )
        try:
            processing_at = datetime.fromisoformat(
                str(raw_processing).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("The next waiver processing time is not verified") from exc
        expires_at = processing_at - timedelta(
            minutes=settings.in_season_waiver_expire_minutes_before_processing
        )
        if current_time >= expires_at:
            raise ValueError("The safe waiver submission window has already closed")
        return current_time, expires_at
    players = [
        *((context.get("our_team") or {}).get("all_players") or []),
        *(context.get("free_agent_candidates") or []),
    ]
    kickoffs = _kickoff_by_player({"our_team": {"all_players": players}})
    involved_ids = {
        str(player_id)
        for action in actions
        for player_id in (
            action["parameters"].get("add_player_id"),
            action["parameters"].get("drop_player_id"),
        )
        if player_id
    }
    involved_kickoffs = [kickoffs[player_id] for player_id in involved_ids if player_id in kickoffs]
    if len(involved_kickoffs) != len(involved_ids):
        raise ValueError("Every acquisition player requires a verified kickoff")
    first_lock = min(involved_kickoffs)
    expires_at = first_lock - timedelta(
        minutes=settings.in_season_acquisition_expire_minutes_before_kickoff
    )
    if current_time >= expires_at:
        raise ValueError("The safe acquisition window has already closed")
    return current_time, expires_at


def _acquisition_prerequisite_error(
    settings: Settings,
    context: dict[str, Any],
    decision: dict[str, Any],
    actions: list[dict[str, Any]],
) -> str | None:
    if settings.execution_mode != "browser":
        return "EXECUTION_MODE must be browser"
    if not actions or len(actions) > 8:
        return "One to eight exact acquisition actions are required"
    action_types = {str(action.get("action_type") or "") for action in actions}
    if not action_types <= {"ADD_FREE_AGENT", "WAIVER_CLAIM"} or len(action_types) != 1:
        return "An acquisition plan must contain one consistent exact action type"
    action_type = next(iter(action_types))
    if len(actions) > 1:
        if action_type == "WAIVER_CLAIM":
            if not settings.in_season_multi_claim_actions_enabled:
                return "Conditional multi-claim execution is not enabled"
            if not multi_claim_is_qualified(settings):
                return "Conditional multi-claim execution has not passed live qualification"
        if (
            action_type == "ADD_FREE_AGENT"
            and not settings.in_season_sequential_free_agent_actions_enabled
        ):
            return "Sequential free-agent execution is not enabled"
    if action_type == "ADD_FREE_AGENT":
        if not action_is_qualified(settings, "ADD_FREE_AGENT"):
            return "Free-agent execution has not passed transaction-backed live qualification"
        sequence_error = _sequential_free_agent_plan_error(context, actions)
        if sequence_error:
            return sequence_error
    if action_type not in settings.live_browser_actions:
        return f"{action_type} is not present in BROWSER_LIVE_ACTIONS"
    if decision.get("waiver_status") != "ready":
        return "The expert acquisition decision is not ready"
    if float(decision.get("confidence") or 0) < settings.in_season_acquisition_min_confidence:
        return "The expert acquisition confidence is below the execution threshold"
    expected_acquisition_type = "free_agent" if action_type == "ADD_FREE_AGENT" else "waiver"
    for action in actions:
        parameters = action.get("parameters") or {}
        if parameters.get("observed_acquisition_type") != expected_acquisition_type:
            return f"Sleeper no longer exposes the target as a {expected_acquisition_type}"
    if action_type == "WAIVER_CLAIM" and not settings.waiver_semantics_confirmed:
        return "Waiver processing semantics are not confirmed"
    if action_type == "WAIVER_CLAIM":
        if not waiver_selector_is_qualified(settings):
            return "Waiver selector execution has not passed read-only live qualification"
        if not waiver_submission_is_qualified(settings):
            return "Waiver submission has not passed live qualification"
        pending = (context.get("season_horizon") or {}).get("pending_waiver_claims") or []
        desired = {
            _waiver_key(action.get("parameters") or {}) for action in actions
        }
        observed = {
            (
                str((row.get("add_player_ids") or [""])[0]),
                str((row.get("drop_player_ids") or [""])[0])
                if row.get("drop_player_ids")
                else "",
            )
            for row in pending
            if len(row.get("add_player_ids") or []) == 1
            and len(row.get("drop_player_ids") or []) <= 1
        }
        if observed - desired:
            return "An existing pending Sleeper waiver differs from the current exact plan"
    for action in actions:
        parameters = action.get("parameters") or {}
        drop_id = str(parameters.get("drop_player_id") or "")
        drop = next(
            (
                row
                for row in (context.get("our_team") or {}).get("all_players") or []
                if str(row.get("player_id")) == drop_id
            ),
            {},
        )
        if action_type == "ADD_FREE_AGENT" and drop_id and drop.get("locked"):
            return "The exact immediate drop player is already locked"
    quality = context.get("data_quality") or {}
    join = quality.get("sleeper_ui_free_agent_projection_join") or {}
    if int(join.get("acquisition_typed") or 0) < 20:
        return "The authenticated Sleeper acquisition-state observation is incomplete"
    return None


def _sequential_free_agent_plan_error(
    context: dict[str, Any], actions: list[dict[str, Any]]
) -> str | None:
    """Prove that every preview is the exact successor of the prior roster state."""

    current = [
        str(row.get("player_id"))
        for row in (context.get("our_team") or {}).get("all_players") or []
        if row.get("player_id") is not None
    ]
    seen_adds: set[str] = set()
    seen_drops: set[str] = set()
    for index, action in enumerate(actions, start=1):
        parameters = action.get("parameters") or {}
        before = [str(player_id) for player_id in parameters.get("expected_roster_before") or []]
        after = [str(player_id) for player_id in parameters.get("expected_roster_after") or []]
        if len(before) != len(current) or set(before) != set(current):
            return f"Free-agent step {index} does not start from the verified prior roster"
        add_id = str(parameters.get("add_player_id") or "")
        drop_id = str(parameters.get("drop_player_id") or "")
        if not add_id or add_id in current or add_id in seen_adds:
            return f"Free-agent step {index} has a duplicate or invalid add player"
        if drop_id and (drop_id not in current or drop_id in seen_drops):
            return f"Free-agent step {index} has a duplicate or invalid drop player"
        expected_after = [player_id for player_id in current if player_id != drop_id]
        expected_after.append(add_id)
        if len(after) != len(expected_after) or set(after) != set(expected_after):
            return f"Free-agent step {index} does not describe one exact roster mutation"
        seen_adds.add(add_id)
        if drop_id:
            seen_drops.add(drop_id)
        current = expected_after
    return None


def _waiver_key(parameters: dict[str, Any]) -> tuple[str, str]:
    return (
        str(parameters.get("add_player_id") or ""),
        str(parameters.get("drop_player_id") or ""),
    )


def _has_exact_pending_waiver(
    context: dict[str, Any], parameters: dict[str, Any]
) -> bool:
    pending = (context.get("season_horizon") or {}).get("pending_waiver_claims") or []
    add_id, drop_id = _waiver_key(parameters)
    return any(
        row.get("add_player_ids") == [add_id]
        and row.get("drop_player_ids") == ([drop_id] if drop_id else [])
        for row in pending
    )


def _automatic_drop_is_safe(parameters: dict[str, Any]) -> bool:
    drop_id = str(parameters.get("drop_player_id") or "")
    if not drop_id:
        return True
    raw = parameters.get("drop_evidence") or {}
    decision = evaluate_drop(
        DropEvidence(
            player_id=drop_id,
            positions=tuple(raw.get("positions") or ()),
            ros_ranks=raw.get("ros_ranks") or {},
            ranking_fresh=bool(raw.get("ranking_fresh", False)),
            ranking_disputed=bool(raw.get("ranking_disputed", False)),
            manually_locked=bool(raw.get("manually_locked", False)),
            protection_tier=raw.get("protection_tier") or "REVIEW",
            recently_acquired=bool(raw.get("recently_acquired", False)),
        )
    )
    return decision.auto_eligible


async def synchronize_acquisition_actions(
    settings: Settings,
    context: dict[str, Any],
    decision: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    not_before_offset_seconds: int = 0,
) -> dict[str, Any]:
    """Synchronize one exact free-agent or waiver plan behind explicit live gates."""
    lifecycle = read_report(settings.reports_dir / "acquisition-lifecycle.json", {})
    unresolved = [
        row
        for row in lifecycle.get("claims") or []
        if row.get("outcome_status")
        in {"pending", "scheduled", "awaiting_reconciliation", "unverified"}
    ]
    if unresolved:
        return {
            "state": "blocked",
            "queued": [],
            "preview_count": len(actions),
            "reason": (
                f"{len(unresolved)} prior acquisition write(s) require reconciliation before "
                "another acquisition can be queued"
            ),
        }
    if not settings.in_season_acquisition_actions_enabled:
        return {"state": "analysis_only", "queued": [], "preview_count": len(actions)}
    if not actions:
        if (context.get("season_horizon") or {}).get("pending_waiver_claims"):
            return {
                "state": "blocked",
                "queued": [],
                "preview_count": 0,
                "reason": "Sleeper has a pending waiver that cannot be retired as a local command",
            }
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        async with session_scope() as session:
            active_rows = (
                await session.execute(
                    select(ExecutionCommand, ActionItem)
                    .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                    .where(
                        ExecutionCommand.action_type.in_(["ADD_FREE_AGENT", "WAIVER_CLAIM"]),
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
                for command, _ in active_rows
            ):
                return {
                    "state": "blocked",
                    "queued": [],
                    "reason": "An acquisition command is already being executed",
                }
            for command, action in active_rows:
                command.status = "blocked"
                command.completed_at = current_time
                command.result = {"reason": "The current expert decision is no acquisition"}
                action.status = "superseded"
        return {"state": "no_action", "queued": [], "preview_count": 0}
    if all(
        action.get("action_type") == "WAIVER_CLAIM"
        and _has_exact_pending_waiver(context, action.get("parameters") or {})
        for action in actions
    ):
        return {
            "state": "already_pending",
            "queued": [],
            "preview_count": len(actions),
            "reason": "Every exact waiver claim is already pending in Sleeper",
        }
    prerequisite = _acquisition_prerequisite_error(settings, context, decision, actions)
    if prerequisite:
        return {"state": "blocked", "queued": [], "reason": prerequisite}

    remaining_actions = [
        action
        for action in actions
        if action["action_type"] != "WAIVER_CLAIM"
        or not _has_exact_pending_waiver(context, action["parameters"])
    ]
    if not remaining_actions:
        return {
            "state": "already_pending",
            "queued": [],
            "preview_count": len(actions),
            "reason": "Every exact waiver claim is already pending in Sleeper",
        }
    if any(not _automatic_drop_is_safe(action["parameters"]) for action in remaining_actions):
        return {
            "state": "approval_required",
            "queued": [],
            "preview_count": len(actions),
            "reason": "At least one fallback claim fails automatic drop protection",
        }

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    sequential_free_agent_plan = (
        len(remaining_actions) > 1
        and all(action["action_type"] == "ADD_FREE_AGENT" for action in remaining_actions)
    )
    queue_actions = remaining_actions[:1] if sequential_free_agent_plan else remaining_actions
    deferred_actions = remaining_actions[1:] if sequential_free_agent_plan else []
    planned = [
        (
            action,
            (
                f"in-season-acquisition:{action['action_type']}:"
                f"{context.get('season')}:{context.get('week')}:"
                f"{content_hash(action['parameters'])}"
            ),
        )
        for action in queue_actions
    ]
    planned_keys = {dedupe_key for _, dedupe_key in planned}
    queued_action_types = sorted({action["action_type"] for action in queue_actions})
    async with session_scope() as session:
        cancelled_rows = (
            await session.execute(
                select(ExecutionCommand, ActionItem)
                .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                .where(
                    ExecutionCommand.action_type.in_(queued_action_types),
                    ExecutionCommand.league_id == settings.sleeper_league_id,
                    ExecutionCommand.roster_id == settings.sleeper_roster_id,
                    ActionItem.status == "cancelled",
                )
            )
        ).all()
        desired_transaction_identities = {
            (
                action["action_type"],
                str(action["parameters"].get("add_player_id") or ""),
                str(action["parameters"].get("drop_player_id") or ""),
                int(action["parameters"].get("transaction_week") or context.get("week") or 0),
            )
            for action in queue_actions
        }
        matching_cancellations = [
            action
            for command, action in cancelled_rows
            if (
                command.action_type,
                str((command.parameters or {}).get("add_player_id") or ""),
                str((command.parameters or {}).get("drop_player_id") or ""),
                int((command.parameters or {}).get("transaction_week") or 0),
            )
            in desired_transaction_identities
        ]
        if matching_cancellations:
            return {
                "state": "cancelled",
                "queued": [],
                "preview_count": len(actions),
                "reason": (
                    "The owner cancelled this add/drop identity for the current transaction week"
                ),
                "action_ids": [action.id for action in matching_cancellations],
            }
        failed_rows = (
            await session.execute(
                select(ExecutionCommand, ActionItem)
                .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                .where(
                    ExecutionCommand.action_type.in_(queued_action_types),
                    ExecutionCommand.league_id == settings.sleeper_league_id,
                    ExecutionCommand.roster_id == settings.sleeper_roster_id,
                    ExecutionCommand.status == "failed",
                    ActionItem.status == "failed",
                )
            )
        ).all()
        matching_failures = [
            action
            for command, action in failed_rows
            if (
                command.action_type,
                str((command.parameters or {}).get("add_player_id") or ""),
                str((command.parameters or {}).get("drop_player_id") or ""),
                int((command.parameters or {}).get("transaction_week") or 0),
            )
            in desired_transaction_identities
        ]
        if matching_failures:
            return {
                "state": "blocked",
                "queued": [],
                "preview_count": len(actions),
                "reason": (
                    "A prior failed attempt exists for this add/drop identity in the current "
                    "transaction week; manual review is required before retrying"
                ),
                "action_ids": [action.id for action in matching_failures],
            }
        active_rows = (
            await session.execute(
                select(ExecutionCommand, ActionItem)
                .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                .where(
                    ExecutionCommand.action_type.in_(["ADD_FREE_AGENT", "WAIVER_CLAIM"]),
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
            for command, _ in active_rows
        ):
            return {
                "state": "blocked",
                "queued": [],
                "reason": "An acquisition command is already being executed",
            }
        existing_by_key: dict[str, tuple[ExecutionCommand, ActionItem]] = {}
        for command, action in active_rows:
            if action.dedupe_key in planned_keys:
                existing_by_key[action.dedupe_key] = (command, action)
                continue
            command.status = "blocked"
            command.completed_at = current_time
            command.result = {"reason": "Superseded by a newer grounded acquisition plan"}
            action.status = "superseded"

        try:
            not_before, expires_at = acquisition_execution_window(
                settings, context, queue_actions, now=current_time
            )
        except ValueError as exc:
            return {"state": "blocked", "queued": [], "reason": str(exc)}
        queued: list[dict[str, Any]] = []
        claim_group = [
            {
                "add_player_id": str(action["parameters"].get("add_player_id") or ""),
                "drop_player_id": str(action["parameters"].get("drop_player_id") or ""),
                "claim_priority": int(action["parameters"].get("claim_priority") or 0),
            }
            for action in actions
            if action["action_type"] == "WAIVER_CLAIM"
        ]
        for index, (action_preview, dedupe_key) in enumerate(planned):
            existing = existing_by_key.get(dedupe_key)
            if existing is not None:
                command, action = existing
            else:
                parameters = dict(action_preview["parameters"])
                if action_preview["action_type"] == "WAIVER_CLAIM":
                    parameters["claim_group"] = claim_group
                elif sequential_free_agent_plan:
                    parameters.update(
                        {
                            "sequence_mode": "verify_reconcile_replan",
                            "sequence_step": 1,
                            "sequence_plan_size": len(remaining_actions),
                            "deferred_action_hashes": [
                                content_hash(action["parameters"])
                                for action in deferred_actions
                            ],
                        }
                    )
                action, command = await propose_execution(
                    session,
                    action_type=action_preview["action_type"],
                    league_id=settings.sleeper_league_id,
                    roster_id=settings.sleeper_roster_id,
                    parameters=parameters,
                    expected_state_hash=action_preview["expected_state_hash"],
                    evidence_hashes=[str(context["state_hash"]), content_hash(decision)],
                    reason=action_preview["reason"],
                    confidence=float(decision["confidence"]),
                    approval_required=False,
                    not_before=not_before
                    + timedelta(seconds=not_before_offset_seconds + index * 2),
                    expires_at=expires_at,
                    verification_plan=(
                        {
                            "source": (
                                "Sleeper authenticated pending-waiver UI and public processed "
                                "transaction API"
                            ),
                            "endpoint": (
                                f"league/{settings.sleeper_league_id}/transactions/"
                                f"{parameters['transaction_week']}"
                            ),
                            "expected_statuses": ["pending", "complete"],
                            "expected_add_player_id": parameters["add_player_id"],
                            "expected_drop_player_id": parameters.get("drop_player_id") or None,
                            "claim_group": claim_group,
                            "retry_after_uncertain_write": False,
                        }
                        if action_preview["action_type"] == "WAIVER_CLAIM"
                        else {
                            "source": "Sleeper public roster API",
                            "expected_players": parameters["expected_roster_after"],
                            "retry_after_uncertain_write": False,
                            "continuation_policy": (
                                "verify_reconcile_replan"
                                if sequential_free_agent_plan
                                else "none"
                            ),
                            "release_condition": (
                                "Public roster completion must be reconciled, then a fresh "
                                "expert plan must be compiled from the new roster"
                                if sequential_free_agent_plan
                                else None
                            ),
                        }
                    ),
                    dedupe_key=dedupe_key,
                    settings=settings,
                )
                action.priority = int(parameters.get("claim_priority") or 1)
                if command is not None:
                    await session.flush()
            if command is None:
                return {
                    "state": "approval_required",
                    "queued": queued,
                    "action_id": action.id,
                    "reason": "The exact drop failed the automatic drop-protection gate",
                }
            if command.status not in {"ready", "leased"}:
                return {
                    "state": "blocked",
                    "queued": queued,
                    "reason": f"An exact acquisition action is already {command.status}",
                    "action_id": action.id,
                }
            queued.append(
                {
                    "sequence": int(action_preview.get("sequence") or index + 1),
                    "action_id": action.id,
                    "command_id": command.id,
                    "not_before": command.not_before.isoformat(),
                    "expires_at": command.expires_at.isoformat(),
                    "status": command.status,
                }
            )
        return {
            "state": "scheduled_sequential" if sequential_free_agent_plan else "scheduled",
            "queued": queued,
            "preview_count": len(actions),
            "already_pending_count": len(actions) - len(remaining_actions),
            "deferred_count": len(deferred_actions),
            "deferred": [
                {
                    "sequence": int(action.get("sequence") or index + 2),
                    "action_type": action["action_type"],
                    "add_player_id": str(action["parameters"].get("add_player_id") or ""),
                    "drop_player_id": str(action["parameters"].get("drop_player_id") or ""),
                    "reason": "Awaiting verification, reconciliation, and a fresh expert re-plan",
                }
                for index, action in enumerate(deferred_actions)
            ],
            "continuation_policy": (
                "verify_reconcile_replan" if sequential_free_agent_plan else "none"
            ),
        }
