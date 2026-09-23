from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import session_scope
from app.models import ActionItem, ExecutionCommand
from app.notifications import queue_dashboard_notice, queue_slack_alert
from app.readiness import read_report
from app.repositories import (
    resolve_operational_incident,
    transition_action,
    upsert_operational_incident,
)
from app.utils import canonical_json, content_hash

UTC = timezone.utc
ACQUISITION_TYPES = {"WAIVER_CLAIM", "ADD_FREE_AGENT"}
ACTIVE_COMMAND_STATES = {"ready", "leased", "preflight", "executing", "verifying"}
UNRESOLVED_OUTCOMES = {"pending", "scheduled", "awaiting_reconciliation", "unverified"}
RECONCILIATION_GRACE = timedelta(minutes=2)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _mapped_to_roster(mapping: Any, player_id: str, roster_id: int) -> bool:
    return isinstance(mapping, dict) and str(mapping.get(player_id)) == str(roster_id)


def _transaction_matches(
    transaction: dict[str, Any],
    *,
    action_type: str,
    add_player_id: str,
    drop_player_id: str,
    roster_id: int,
    owner_user_id: str,
    created_at: datetime,
) -> bool:
    expected_type = "waiver" if action_type == "WAIVER_CLAIM" else "free_agent"
    if str(transaction.get("type") or "") != expected_type:
        return False
    if str(transaction.get("creator") or "") != owner_user_id:
        return False
    if str(roster_id) not in {str(item) for item in transaction.get("roster_ids") or []}:
        return False
    if not _mapped_to_roster(transaction.get("adds"), add_player_id, roster_id):
        return False
    transaction_created = transaction.get("created")
    if isinstance(transaction_created, (int, float)):
        observed = datetime.fromtimestamp(float(transaction_created) / 1000, tz=UTC)
        if observed < _as_utc(created_at) - timedelta(seconds=5):
            return False
    drops = transaction.get("drops")
    if drop_player_id and isinstance(drops, dict) and drops:
        return _mapped_to_roster(drops, drop_player_id, roster_id)
    return not drop_player_id or str(transaction.get("status") or "") == "failed"


def _transaction_identity(transaction: dict[str, Any]) -> str:
    return str(transaction.get("transaction_id") or content_hash(transaction))


def _transaction_history(in_season: dict[str, Any]) -> list[dict[str, Any]]:
    history = in_season.get("transaction_history")
    if isinstance(history, list):
        rows = [row for row in history if isinstance(row, dict)]
    elif isinstance(in_season.get("transactions_by_week"), dict):
        rows = [
            row
            for week_rows in in_season["transactions_by_week"].values()
            if isinstance(week_rows, list)
            for row in week_rows
            if isinstance(row, dict)
        ]
    else:
        rows = [row for row in in_season.get("transactions") or [] if isinstance(row, dict)]
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique[_transaction_identity(row)] = row
    return sorted(unique.values(), key=lambda row: int(row.get("created") or 0), reverse=True)


def _pending_keys(manager: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in (manager.get("season_horizon") or {}).get("pending_waiver_claims") or []:
        adds = [str(item) for item in row.get("add_player_ids") or []]
        drops = [str(item) for item in row.get("drop_player_ids") or []]
        if len(adds) == 1 and len(drops) <= 1:
            keys.add((adds[0], drops[0] if drops else ""))
    return keys


def _cancelled_keys(
    cancellations: list[ExecutionCommand],
) -> list[tuple[tuple[str, str], datetime]]:
    result: list[tuple[tuple[str, str], datetime]] = []
    for command in cancellations:
        if command.status != "verified":
            continue
        claim = (command.parameters or {}).get("claim") or {}
        result.append(
            (
                (
                    str(claim.get("add_player_id") or ""),
                    str(claim.get("drop_player_id") or ""),
                ),
                _as_utc(command.created_at),
            )
        )
    return result


def _roster_state(in_season: dict[str, Any], parameters: dict[str, Any]) -> str:
    observed = {
        str(player_id)
        for player_id in (in_season.get("owner_roster") or {}).get("players") or []
    }
    before = {str(player_id) for player_id in parameters.get("expected_roster_before") or []}
    after = {str(player_id) for player_id in parameters.get("expected_roster_after") or []}
    if observed and after and observed == after:
        return "after"
    if observed and before and observed == before:
        return "before"
    return "other"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    temporary.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    temporary.replace(path)


async def reconcile_acquisition_lifecycle(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile every acquisition against history without attempting a Sleeper write."""

    cfg = settings or get_settings()
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    in_season = read_report(cfg.reports_dir / "in-season-context.json", {})
    manager = read_report(cfg.reports_dir / "in-season-plan.json", {})
    transactions = _transaction_history(in_season)
    pending = _pending_keys(manager)
    previous = read_report(cfg.reports_dir / "acquisition-lifecycle.json", {})
    prior_states = {
        str(row.get("command_id")): str(row.get("outcome_status"))
        for row in previous.get("claims") or []
    }
    claims: list[dict[str, Any]] = []
    claimed_transactions: set[str] = set()
    free_agent_qualification: dict[str, Any] | None = None
    transitioned = False
    incident_key = (
        f"acquisition-lifecycle-unresolved:{cfg.sleeper_league_id}:{cfg.sleeper_roster_id}"
    )

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ExecutionCommand, ActionItem)
                .join(ActionItem, ActionItem.id == ExecutionCommand.action_id)
                .where(
                    ExecutionCommand.league_id == cfg.sleeper_league_id,
                    ExecutionCommand.roster_id == cfg.sleeper_roster_id,
                    ExecutionCommand.action_type.in_(sorted(ACQUISITION_TYPES)),
                )
                .order_by(ExecutionCommand.created_at.desc())
                .limit(100)
            )
        ).all()
        cancellation_commands = list(
            await session.scalars(
                select(ExecutionCommand).where(
                    ExecutionCommand.league_id == cfg.sleeper_league_id,
                    ExecutionCommand.roster_id == cfg.sleeper_roster_id,
                    ExecutionCommand.action_type == "CANCEL_WAIVER_CLAIM",
                )
            )
        )
        cancelled = _cancelled_keys(cancellation_commands)

        for command, action in rows:
            parameters = command.parameters or {}
            add_id = str(parameters.get("add_player_id") or "")
            drop_id = str(parameters.get("drop_player_id") or "")
            matching = next(
                (
                    transaction
                    for transaction in transactions
                    if _transaction_identity(transaction) not in claimed_transactions
                    and _transaction_matches(
                        transaction,
                        action_type=command.action_type,
                        add_player_id=add_id,
                        drop_player_id=drop_id,
                        roster_id=cfg.sleeper_roster_id,
                        owner_user_id=cfg.sleeper_owner_user_id,
                        created_at=command.created_at,
                    )
                ),
                None,
            )
            if matching is not None:
                claimed_transactions.add(_transaction_identity(matching))
                outcome = str(matching.get("status") or "unknown")
                state = (
                    "completed"
                    if outcome == "complete"
                    else "failed"
                    if outcome == "failed"
                    else outcome
                )
                result_evidence = (command.result or {}).get("evidence") or {}
                if (
                    free_agent_qualification is None
                    and command.action_type == "ADD_FREE_AGENT"
                    and outcome == "complete"
                    and command.status == "verified"
                    and result_evidence.get("write_attempted") is True
                    and result_evidence.get("ui_contract_version")
                    == cfg.sleeper_ui_contract_version
                ):
                    free_agent_qualification = {
                        "generated_at": observed_at.isoformat(),
                        "status": "qualified",
                        "ui_contract_version": cfg.sleeper_ui_contract_version,
                        "league_id": command.league_id,
                        "roster_id": command.roster_id,
                        "action_type": "ADD_FREE_AGENT",
                        "write_attempted": True,
                        "public_api_verified": True,
                        "authenticated_ui_verified": False,
                        "verification_channel": "public_api",
                        "transaction_id": str(matching.get("transaction_id")),
                        "transaction_status": "complete",
                        "transaction_created": matching.get("created"),
                        "command_id": command.id,
                        "reconciled_from_transaction_history": True,
                    }
            elif command.action_type == "WAIVER_CLAIM" and any(
                key == (add_id, drop_id) and cancelled_at >= _as_utc(command.created_at)
                for key, cancelled_at in cancelled
            ):
                state = "cancelled"
            elif command.action_type == "WAIVER_CLAIM" and (add_id, drop_id) in pending:
                state = "pending"
            elif command.status in ACTIVE_COMMAND_STATES:
                state = "scheduled"
            elif command.status in {"failed", "blocked", "cancelled"}:
                state = str(command.status)
            else:
                age = observed_at - _as_utc(command.created_at)
                roster_state = _roster_state(in_season, parameters)
                stale_uncertain = command.status == "unverified" and age >= RECONCILIATION_GRACE
                false_verified_add = (
                    command.action_type == "ADD_FREE_AGENT"
                    and command.status == "verified"
                    and age >= RECONCILIATION_GRACE
                    and roster_state != "after"
                )
                if stale_uncertain or false_verified_add:
                    state = "failed"
                    reason = (
                        "No exact Sleeper transaction was observed after the browser write; "
                        f"public roster state is {roster_state}. Automatic retry is disabled."
                    )
                    command.status = "failed"
                    command.completed_at = observed_at
                    command.result = {
                        "message": "Public verification conflict",
                        "evidence": {
                            "reconciled_at": observed_at.isoformat(),
                            "transaction_observed": False,
                            "public_roster_state": roster_state,
                            "write_attempted": True,
                            "automatic_retry": False,
                            "reason": reason,
                        },
                    }
                    if action.status != "failed":
                        await transition_action(
                            session,
                            action,
                            "failed",
                            actor="acquisition-reconciler",
                            details={"reason": reason},
                        )
                    transitioned = True
                elif command.status in {"verified", "unverified"}:
                    state = (
                        "awaiting_reconciliation"
                        if command.status == "verified"
                        else "unverified"
                    )
                else:
                    state = str(command.status)

            if state == "failed" and matching is not None and command.status != "failed":
                command.status = "failed"
                command.completed_at = observed_at
                if action.status != "failed":
                    await transition_action(
                        session,
                        action,
                        "failed",
                        actor="acquisition-reconciler",
                        details={
                            "reason": "Sleeper reported the acquisition transaction failed",
                            "transaction_id": matching.get("transaction_id"),
                        },
                    )
                transitioned = True

            claim = {
                "action_id": action.id,
                "command_id": command.id,
                "action_type": command.action_type,
                "submission_status": command.status,
                "outcome_status": state,
                "add_player_id": add_id,
                "add_player_name": parameters.get("add_player_search_name")
                or parameters.get("add_player_name"),
                "drop_player_id": drop_id or None,
                "drop_player_name": parameters.get("drop_player_search_name")
                or parameters.get("drop_player_name")
                or None,
                "claim_priority": parameters.get("claim_priority"),
                "sequence_mode": parameters.get("sequence_mode"),
                "sequence_step": parameters.get("sequence_step"),
                "sequence_plan_size": parameters.get("sequence_plan_size"),
                "transaction_id": str(matching.get("transaction_id")) if matching else None,
                "transaction_notes": ((matching or {}).get("metadata") or {}).get("notes"),
                "submitted_at": command.created_at.isoformat(),
            }
            claims.append(claim)

        unresolved = [row for row in claims if row["outcome_status"] in UNRESOLVED_OUTCOMES]
        failed = [row for row in claims if row["outcome_status"] == "failed"]
        if unresolved:
            await upsert_operational_incident(
                session,
                category="acquisition_reconciliation",
                severity="critical",
                summary=f"{len(unresolved)} acquisition write(s) remain unresolved",
                dedupe_key=incident_key,
                details={
                    "unresolved_command_ids": [row["command_id"] for row in unresolved],
                    "failed_command_ids": [row["command_id"] for row in failed],
                    "automatic_retry": False,
                },
            )
        else:
            await resolve_operational_incident(session, dedupe_key=incident_key)

        for claim in claims:
            state = str(claim["outcome_status"])
            if state not in {"completed", "failed"}:
                continue
            if prior_states.get(str(claim["command_id"])) == state:
                continue
            subject = (
                f"Acquisition completed: {claim['add_player_name']}"
                if state == "completed"
                else f"Acquisition failed: {claim['add_player_name']}"
            )
            body = (
                f"Add {claim['add_player_name']}"
                + (f"; drop {claim['drop_player_name']}" if claim["drop_player_name"] else "")
                + f". Sleeper outcome: {state}."
                + (f" {claim['transaction_notes']}" if claim["transaction_notes"] else "")
            )
            dedupe = f"acquisition-outcome:{claim['command_id']}:{state}"
            await queue_dashboard_notice(
                session,
                subject=subject,
                body=body,
                dedupe_key=f"dashboard:{dedupe}",
                action_id=str(claim["action_id"]),
                severity="success" if state == "completed" else "warning",
            )
            if state == "failed":
                await queue_slack_alert(
                    session,
                    cfg,
                    subject=subject,
                    body=(
                        "Issue type: ACQUISITION FAILED\n"
                        f"Observed at: {observed_at.isoformat()}\n"
                        f"Action ID: {claim['action_id']}\n"
                        f"Command ID: {claim['command_id']}\n"
                        f"Add player: {claim['add_player_name']} ({claim['add_player_id']})\n"
                        f"Drop player: {claim['drop_player_name'] or 'none'} "
                        f"({claim['drop_player_id'] or 'none'})\n"
                        f"Sleeper outcome: {state}\n"
                        f"Sleeper transaction ID: {claim['transaction_id'] or 'not observed'}\n"
                        f"Details: {claim['transaction_notes'] or body}\n"
                        "Required response: inspect the dashboard and Sleeper before attempting "
                        "any manual correction."
                    ),
                    dedupe_key=f"slack:{dedupe}",
                    action_id=str(claim["action_id"]),
                )

    active = [row for row in claims if row["outcome_status"] in UNRESOLVED_OUTCOMES]
    failed = [row for row in claims if row["outcome_status"] == "failed"]
    completed = [row for row in claims if row["outcome_status"] == "completed"]
    material = [{key: value for key, value in row.items() if key != "submitted_at"} for row in claims]
    changed_terminal = any(
        prior_states.get(str(row["command_id"])) != str(row["outcome_status"])
        and row["outcome_status"] in {"completed", "failed", "cancelled"}
        for row in claims
    )
    report = {
        "generated_at": observed_at.isoformat(),
        "status": (
            "attention"
            if any(row["outcome_status"] == "unverified" for row in active)
            else "pending"
            if active
            else "settled_with_failures"
            if failed
            else "settled"
            if completed
            else "idle"
        ),
        "claims": claims,
        "active_count": len(active),
        "unresolved_count": len(active),
        "completed_count": len(completed),
        "failed_count": len(failed),
        "requires_manager_refresh": transitioned or changed_terminal,
        "retry_policy": "never_retry_uncertain_write",
        "transaction_history_count": len(transactions),
        "state_hash": content_hash(material),
    }
    _write_json_atomic(cfg.reports_dir / "acquisition-lifecycle.json", report)
    if free_agent_qualification is not None:
        _write_json_atomic(
            cfg.reports_dir / "free-agent-submission-qualification.json",
            free_agent_qualification,
        )
    return report
