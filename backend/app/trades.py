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
TRADE_ACTION_TYPES = {"PROPOSE_TRADE", "ACCEPT_TRADE", "DECLINE_TRADE"}


def _display_name(player: dict[str, Any]) -> str:
    position = str(player.get("position") or "").upper()
    if position == "DEF":
        return str(player.get("team") or player.get("name") or "")
    words = str(player.get("name") or "").split()
    while words and words[-1].lower().rstrip(".") in {"jr", "sr", "ii", "iii", "iv", "v"}:
        words.pop()
    if not words:
        return ""
    return f"{words[0][0]}. {words[-1]}"


def _asset(player: dict[str, Any]) -> dict[str, str]:
    return {
        "player_id": str(player.get("player_id") or ""),
        "player_name": str(player.get("name") or player.get("player_id") or ""),
        "player_display_name": _display_name(player),
        "position": str(player.get("position") or ""),
        "team": str(player.get("team") or ""),
    }


def compile_trade_actions(
    settings: Settings,
    decision: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compile only explicit, exact, high-conviction trade decisions."""

    ours = list((context.get("our_team") or {}).get("all_players") or [])
    our_by_id = {str(player.get("player_id")): player for player in ours}
    our_ids = list(our_by_id)
    protected = {
        str(player_id)
        for player_id in (context.get("manual_constraints") or {}).get(
            "protected_player_ids", []
        )
    }
    rosters = {
        int(roster.get("roster_id") or 0): roster
        for roster in context.get("league_rosters") or []
        if not roster.get("is_our_team")
    }
    actions: list[dict[str, Any]] = []

    if decision.get("trade_status") == "ready":
        for target in decision.get("trade_targets") or []:
            if (
                target.get("recommended_action") != "propose"
                or target.get("upside_tier") != "high"
                or target.get("evidence_status") != "confirmed"
                or float(target.get("confidence") or 0) < settings.in_season_trade_min_confidence
            ):
                continue
            roster_id = int(target.get("target_roster_id") or 0)
            counterpart = rosters.get(roster_id)
            if not counterpart or not counterpart.get("owner_display_name"):
                continue
            their_by_id = {
                str(player.get("player_id")): player
                for player in counterpart.get("players") or []
            }
            send_ids = [str(item) for item in target.get("offer_player_ids") or []]
            receive_ids = [str(item) for item in target.get("target_player_ids") or []]
            if (
                set(send_ids) & protected
                or not set(send_ids) <= set(our_by_id)
                or not set(receive_ids) <= set(their_by_id)
            ):
                continue
            parameters = {
                "counterparty_roster_id": roster_id,
                "counterparty_account_label": str(counterpart["owner_display_name"]),
                "send_assets": [_asset(our_by_id[player_id]) for player_id in send_ids],
                "receive_assets": [_asset(their_by_id[player_id]) for player_id in receive_ids],
                "expected_our_roster_before": our_ids,
                "expected_counterparty_roster_before": list(their_by_id),
                "decision_reason": str(target.get("reason") or ""),
                "championship_case": str(target.get("championship_case") or ""),
                "upside_tier": "high",
                "decision_confidence": float(target.get("confidence") or 0),
                "expiration_label": (
                    "1 Hour" if settings.in_season_trade_expiration_hours <= 1 else "24 Hours"
                ),
            }
            actions.append(
                {
                    "action_type": "PROPOSE_TRADE",
                    "parameters": parameters,
                    "reason": parameters["decision_reason"],
                    "confidence": parameters["decision_confidence"],
                    "expected_state_hash": content_hash(
                        {
                            "our_roster": our_ids,
                            "counterparty_roster": list(their_by_id),
                            "offer": parameters,
                        }
                    ),
                }
            )

    incoming = {
        str(offer.get("offer_fingerprint")): offer
        for offer in (context.get("trade_market") or {}).get("active_offers") or []
        if offer.get("direction") == "incoming"
    }
    for verdict in decision.get("incoming_trade_decisions") or []:
        action = str(verdict.get("action") or "watch")
        offer = incoming.get(str(verdict.get("offer_fingerprint") or ""))
        if action == "watch" or not offer:
            continue
        confidence = float(verdict.get("confidence") or 0)
        if confidence < settings.in_season_trade_min_confidence:
            continue
        if action == "accept" and (
            verdict.get("upside_tier") != "high"
            or verdict.get("evidence_status") != "confirmed"
        ):
            continue
        if action == "decline" and verdict.get("evidence_status") != "confirmed":
            continue
        roster_id = int(offer.get("counterparty_roster_id") or 0)
        counterpart = rosters.get(roster_id)
        if not counterpart:
            continue
        receive_assets = [_asset(player) for player in offer.get("receive_assets") or []]
        send_assets = [_asset(player) for player in offer.get("send_assets") or []]
        send_ids = [row["player_id"] for row in send_assets]
        receive_ids = [row["player_id"] for row in receive_assets]
        if action == "accept" and set(send_ids) & protected:
            continue
        expected_after = [player_id for player_id in our_ids if player_id not in set(send_ids)]
        expected_after.extend(receive_ids)
        parameters = {
            "counterparty_roster_id": roster_id,
            "counterparty_account_label": str(offer.get("counterparty_account_label") or ""),
            "send_assets": send_assets,
            "receive_assets": receive_assets,
            "expected_our_roster_before": our_ids,
            "expected_counterparty_roster_before": [
                str(player.get("player_id")) for player in counterpart.get("players") or []
            ],
            "expected_our_roster_after": expected_after,
            "offer_fingerprint": str(offer["offer_fingerprint"]),
            "decision_reason": str(verdict.get("reason") or ""),
            "championship_case": str(verdict.get("championship_case") or ""),
            "upside_tier": str(verdict.get("upside_tier") or "low"),
            "decision_confidence": confidence,
        }
        actions.append(
            {
                "action_type": "ACCEPT_TRADE" if action == "accept" else "DECLINE_TRADE",
                "parameters": parameters,
                "reason": parameters["decision_reason"],
                "confidence": confidence,
                "expected_state_hash": content_hash(
                    {"trade_market": context.get("trade_market"), "verdict": verdict}
                ),
            }
        )
    return actions


async def synchronize_trade_actions(
    settings: Settings,
    context: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not settings.in_season_trade_actions_enabled:
        return {"state": "analysis_only", "queued": [], "preview_count": len(actions)}
    if settings.execution_mode != "browser":
        return {"state": "blocked", "queued": [], "reason": "EXECUTION_MODE must be browser"}
    if any(action["action_type"] not in settings.live_browser_actions for action in actions):
        return {
            "state": "blocked",
            "queued": [],
            "reason": "Every trade action must be present in BROWSER_LIVE_ACTIONS",
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

    current = (now or datetime.now(UTC)).astimezone(UTC)
    week_start = current - timedelta(days=current.weekday(), hours=current.hour,
                                     minutes=current.minute, seconds=current.second,
                                     microseconds=current.microsecond)
    queued: list[dict[str, Any]] = []
    async with session_scope() as session:
        existing_rows = (
            await session.execute(
                select(ExecutionCommand, ActionItem)
                .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                .where(
                    ExecutionCommand.league_id == settings.sleeper_league_id,
                    ExecutionCommand.roster_id == settings.sleeper_roster_id,
                    ExecutionCommand.action_type.in_(sorted(TRADE_ACTION_TYPES)),
                )
                .order_by(ExecutionCommand.created_at.desc())
            )
        ).all()
        active = [
            (command, action)
            for command, action in existing_rows
            if command.status in {"ready", "leased", "preflight", "executing", "verifying"}
        ]
        if active:
            return {
                "state": "scheduled",
                "queued": [
                    {
                        "action_id": action.id,
                        "command_id": command.id,
                        "status": command.status,
                        "action_type": command.action_type,
                    }
                    for command, action in active
                ],
                "preview_count": len(actions),
            }

        proposed_this_week = sum(
            1
            for command, _ in existing_rows
            if command.action_type == "PROPOSE_TRADE"
            and command.created_at.replace(tzinfo=command.created_at.tzinfo or UTC) >= week_start
        )
        outgoing_offers = [
            offer
            for offer in (context.get("trade_market") or {}).get("active_offers") or []
            if offer.get("direction") == "outgoing"
        ]
        for preview in actions:
            parameters = preview["parameters"]
            if preview["action_type"] == "PROPOSE_TRADE":
                if outgoing_offers:
                    continue
                if proposed_this_week >= settings.in_season_trade_max_proposals_per_week:
                    continue
                cooldown = current - timedelta(days=settings.in_season_trade_cooldown_days)
                if any(
                    command.action_type == "PROPOSE_TRADE"
                    and int((command.parameters or {}).get("counterparty_roster_id") or 0)
                    == int(parameters["counterparty_roster_id"])
                    and command.created_at.replace(tzinfo=command.created_at.tzinfo or UTC) >= cooldown
                    for command, _ in existing_rows
                ):
                    continue
            dedupe_key = f"trade:{preview['action_type']}:{content_hash(parameters)}"
            expires_at = current + timedelta(minutes=30)
            action, command = await propose_execution(
                session,
                action_type=preview["action_type"],
                league_id=settings.sleeper_league_id,
                roster_id=settings.sleeper_roster_id,
                parameters=parameters,
                expected_state_hash=preview["expected_state_hash"],
                evidence_hashes=[str(context["state_hash"])],
                reason=preview["reason"],
                confidence=float(preview["confidence"]),
                approval_required=False,
                not_before=current,
                expires_at=expires_at,
                verification_plan={
                    "source": (
                        "Sleeper public roster API"
                        if preview["action_type"] == "ACCEPT_TRADE"
                        else "Sleeper authenticated Trades UI"
                    ),
                    "retry_after_uncertain_write": False,
                    "offer_fingerprint": parameters.get("offer_fingerprint"),
                },
                dedupe_key=dedupe_key,
                settings=settings,
            )
            if command:
                queued.append(
                    {
                        "action_id": action.id,
                        "command_id": command.id,
                        "status": command.status,
                        "action_type": command.action_type,
                        "expires_at": command.expires_at.isoformat(),
                    }
                )
                if preview["action_type"] == "PROPOSE_TRADE":
                    proposed_this_week += 1
        return {
            "state": "scheduled" if queued else "gated",
            "queued": queued,
            "preview_count": len(actions),
            "anti_spam": {
                "max_proposals_per_week": settings.in_season_trade_max_proposals_per_week,
                "cooldown_days": settings.in_season_trade_cooldown_days,
                "outgoing_active": len(outgoing_offers),
            },
        }
