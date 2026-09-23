from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
from asyncio import Lock, to_thread
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_qualification import (
    action_is_qualified,
    multi_claim_is_qualified,
    qualification_path,
    waiver_selector_is_qualified,
    waiver_submission_is_qualified,
)
from app.backup_health import verify_latest_backup
from app.config import get_settings
from app.database import get_session
from app.evidence import apply_operational_evidence
from app.execution import command_contract, confirm_action, validate_execution_transition
from app.fantasypros import fantasypros_quota_status
from app.in_season_expert import InSeasonExpertUnavailable, build_manager_context
from app.in_season_sources import sync_fantasypros_context
from app.models import (
    ActionItem,
    ExecutionCommand,
    ManualConfirmation,
    NotificationDelivery,
    OperationalIncident,
    SourceDatasetHealth,
    SourceHealth,
    TrashTalkControl,
    TrashTalkPost,
)
from app.notifications import queue_dashboard_notice, queue_slack_alert
from app.readiness import (
    build_capability_readiness,
    build_manager_autopilot_status,
    read_report,
)
from app.repositories import (
    lease_execution_command,
    list_actions,
    record_observation,
    transition_action,
    update_dataset_health,
    update_source_health,
    upsert_heartbeat,
)
from app.schemas import (
    CommandResultIn,
    ConfirmationIn,
    HeartbeatIn,
    TrashTalkControlIn,
    TrashTalkFeedbackIn,
    TrashTalkOptOutIn,
    WritePreflightIn,
)
from app.utils import canonical_json, content_hash

settings = get_settings()
app = FastAPI(title="Sleeper Fantasy Operations Manager", version="0.1.0")
REQUESTS = Counter("fantasy_http_requests_total", "HTTP requests", ["method", "path", "status"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
FANTASYPROS_REFRESH_LOCK = Lock()


@app.middleware("http")
async def count_requests(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    REQUESTS.labels(request.method, request.url.path, response.status_code).inc()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _command_can_be_cancelled(command: ExecutionCommand, now: datetime) -> bool:
    if command.status == "ready":
        return True
    return (
        command.status == "leased"
        and command.lease_until is not None
        and _as_utc(command.lease_until) <= now
    )


def _jsonable_action(
    item: ActionItem,
    command: ExecutionCommand | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = now or datetime.now(timezone.utc)
    return {
        "id": item.id,
        "action_type": item.action_type,
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "exact_action": item.exact_action,
        "primary_reason": item.primary_reason,
        "confidence": item.confidence,
        "approval_required": item.approval_required,
        "drop_protection_tier": item.drop_protection_tier,
        "decision_policy_version": item.decision_policy_version,
        "evidence_hashes": item.evidence_hashes,
        "recommended_complete_by": item.recommended_complete_by,
        "created_at": item.created_at,
        "execution": (
            {
                "id": command.id,
                "status": command.status,
                "not_before": command.not_before,
                "expires_at": command.expires_at,
                "attempt_count": command.attempt_count,
                "result": command.result,
            }
            if command is not None
            else None
        ),
        "can_cancel": item.status == "approval_required"
        or (command is not None and _command_can_be_cancelled(command, observed_at)),
    }


def _acquisition_state_blockers(
    command: ExecutionCommand,
    context: dict[str, Any],
    configured_settings: Any,
    ui_observation: dict[str, Any] | None = None,
) -> list[str]:
    """Bind an acquisition command to the exact fresh roster and UI action state."""

    parameters = command.parameters or {}
    current_players = [
        str(row.get("player_id"))
        for row in (context.get("our_team") or {}).get("all_players") or []
        if row.get("player_id") is not None
    ]
    expected_before = [str(player_id) for player_id in parameters.get("expected_roster_before") or []]
    blockers: list[str] = []
    if len(current_players) != len(expected_before) or set(current_players) != set(expected_before):
        blockers.append("The public roster changed after the acquisition decision")

    add_id = str(parameters.get("add_player_id") or "")
    candidate = next(
        (
            row
            for row in context.get("free_agent_candidates") or []
            if str(row.get("player_id")) == add_id
        ),
        None,
    )
    expected_type = "waiver" if command.action_type == "WAIVER_CLAIM" else "free_agent"
    observation = ui_observation or {}
    staged_ui_matches = all(
        (
            observation.get("row_identity_confirmed") is True,
            observation.get("dialog_identity_confirmed") is True,
            observation.get("exact_drop_confirmed") is True,
            observation.get("submit_control_enabled") is True,
            observation.get("acquisition_type") == expected_type,
            observation.get("add_player_name") == parameters.get("add_player_name"),
            observation.get("add_player_position") == parameters.get("add_player_position"),
            observation.get("add_player_team") == parameters.get("add_player_team"),
            observation.get("drop_player_name") == parameters.get("drop_player_name"),
        )
    )
    if candidate is None:
        blockers.append("The exact acquisition target is no longer available")
    elif candidate.get("sleeper_acquisition_type") not in {None, expected_type}:
        blockers.append(f"Sleeper no longer exposes the exact target as a {expected_type}")
    elif candidate.get("sleeper_acquisition_type") is None and not staged_ui_matches:
        blockers.append(f"Sleeper no longer exposes the exact target as a {expected_type}")

    drop_id = str(parameters.get("drop_player_id") or "")
    drop = next(
        (
            row
            for row in (context.get("our_team") or {}).get("all_players") or []
            if str(row.get("player_id")) == drop_id
        ),
        None,
    )
    if drop_id and drop is None:
        blockers.append("The exact drop player is no longer on the roster")
    if command.action_type == "ADD_FREE_AGENT" and drop is not None and drop.get("locked"):
        blockers.append("The exact immediate drop player is locked")
    if (
        command.action_type == "WAIVER_CLAIM"
        and not configured_settings.waiver_semantics_confirmed
    ):
        blockers.append("Waiver processing semantics are not confirmed")
    if command.action_type == "WAIVER_CLAIM":
        observed_pending = {
            (
                str((row.get("add_player_ids") or [""])[0]),
                str((row.get("drop_player_ids") or [""])[0])
                if row.get("drop_player_ids")
                else "",
            )
            for row in (context.get("season_horizon") or {}).get(
                "pending_waiver_claims"
            )
            or []
            if len(row.get("add_player_ids") or []) == 1
            and len(row.get("drop_player_ids") or []) <= 1
        }
        exact = (add_id, drop_id)
        allowed_group = {
            (
                str(row.get("add_player_id") or ""),
                str(row.get("drop_player_id") or ""),
            )
            for row in parameters.get("claim_group") or []
            if isinstance(row, dict)
        } or {exact}
        if exact in observed_pending:
            blockers.append("The exact waiver claim is already pending in Sleeper")
        if observed_pending - allowed_group:
            blockers.append("Sleeper has a pending waiver outside the exact approved claim tree")
    return blockers


def _record_waiver_submission_qualification(
    command: ExecutionCommand, evidence: dict[str, Any]
) -> None:
    verification = evidence.get("verification") or {}
    public_verified = evidence.get("public_api_verified") is True
    pending_ui_verified = (
        evidence.get("authenticated_ui_verified") is True
        and verification.get("verification_channel") == "authenticated_pending_ui"
    )
    if (
        command.action_type != "WAIVER_CLAIM"
        or evidence.get("write_attempted") is not True
        or not (public_verified or pending_ui_verified)
        or verification.get("verified") is not True
        or verification.get("transaction_status") not in {"pending", "complete"}
        or (public_verified and not verification.get("transaction_id"))
    ):
        return
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "qualified",
        "ui_contract_version": str(evidence.get("ui_contract_version") or ""),
        "league_id": command.league_id,
        "roster_id": command.roster_id,
        "action_type": command.action_type,
        "add_player_id": str(command.parameters.get("add_player_id") or ""),
        "drop_player_id": str(command.parameters.get("drop_player_id") or "") or None,
        "transaction_week": command.parameters.get("transaction_week"),
        "transaction_id": (
            str(verification["transaction_id"])
            if verification.get("transaction_id")
            else None
        ),
        "transaction_status": str(verification["transaction_status"]),
        "verification_channel": str(verification.get("verification_channel") or "public_api"),
        "public_transaction_verified": public_verified,
        "authenticated_pending_ui_verified": pending_ui_verified,
        "command_id": command.id,
    }
    target = settings.reports_dir / "waiver-submission-qualification.json"
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(canonical_json(report) + "\n", encoding="utf-8")
    temporary.replace(target)


def _record_in_season_action_qualification(
    command: ExecutionCommand, evidence: dict[str, Any]
) -> None:
    """Persist proof only after a real write and action-specific post-state verification."""

    target = qualification_path(settings, command.action_type)
    verification = evidence.get("verification") or {}
    authenticated = evidence.get("authenticated_ui_verified") is True
    public = evidence.get("public_api_verified") is True
    if (
        target is None
        or evidence.get("write_attempted") is not True
        or verification.get("verified") is not True
    ):
        return
    if command.action_type in {"CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS"}:
        if not authenticated or verification.get("verification_channel") != "authenticated_pending_ui":
            return
    elif command.action_type in {"PROPOSE_TRADE", "DECLINE_TRADE"}:
        if not authenticated or verification.get("verification_channel") != "authenticated_trade_ui":
            return
    elif not public:
        return
    if command.action_type == "ADD_FREE_AGENT" and (
        verification.get("verification_channel") != "public_api"
        or not verification.get("transaction_id")
        or verification.get("transaction_status") != "complete"
    ):
        return
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "qualified",
        "ui_contract_version": str(evidence.get("ui_contract_version") or ""),
        "league_id": command.league_id,
        "roster_id": command.roster_id,
        "action_type": command.action_type,
        "write_attempted": True,
        "public_api_verified": public,
        "authenticated_ui_verified": authenticated,
        "verification_channel": str(verification.get("verification_channel") or "public_api"),
        "transaction_id": (
            str(verification["transaction_id"])
            if verification.get("transaction_id")
            else None
        ),
        "transaction_status": verification.get("transaction_status"),
        "transaction_created": verification.get("transaction_created"),
        "command_id": command.id,
    }
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(canonical_json(report) + "\n", encoding="utf-8")
    temporary.replace(target)


def _ir_state_blockers(
    command: ExecutionCommand, context: dict[str, Any]
) -> list[str]:
    parameters = command.parameters or {}
    players = [
        str(row.get("player_id"))
        for row in (context.get("our_team") or {}).get("all_players") or []
    ]
    reserve = [
        str(row.get("player_id"))
        for row in (context.get("our_team") or {}).get("reserve") or []
    ]
    expected_players = [str(item) for item in parameters.get("expected_players") or []]
    expected_reserve = [
        str(item) for item in parameters.get("expected_reserve_before") or []
    ]
    blockers: list[str] = []
    if len(players) != len(expected_players) or set(players) != set(expected_players):
        blockers.append("The public roster changed after the IR decision")
    if len(reserve) != len(expected_reserve) or set(reserve) != set(expected_reserve):
        blockers.append("The public reserve changed after the IR decision")
    player_id = str(parameters.get("player_id") or "")
    player = next(
        (
            row
            for row in (context.get("our_team") or {}).get("all_players") or []
            if str(row.get("player_id")) == player_id
        ),
        None,
    )
    if player is None:
        blockers.append("The exact IR player is no longer rostered")
    elif player.get("locked"):
        blockers.append("The exact IR player is locked")
    elif command.action_type == "MOVE_TO_IR":
        observed = str(player.get("injury_status") or player.get("status") or "").upper()
        if observed not in {"IR", "PUP", "NFI", "OUT"}:
            blockers.append("The exact player is no longer IR-eligible")
    return blockers


def _pending_waiver_state_blockers(
    command: ExecutionCommand, context: dict[str, Any]
) -> list[str]:
    """Require the private queue to match the exact ordered pre-write state."""

    rows = list(
        (context.get("season_horizon") or {}).get("pending_waiver_claims") or []
    )
    rows.sort(key=lambda row: int(row.get("sequence") or 0))
    observed = [
        (
            str((row.get("add_player_ids") or [""])[0]),
            str((row.get("drop_player_ids") or [""])[0])
            if row.get("drop_player_ids")
            else "",
        )
        for row in rows
        if len(row.get("add_player_ids") or []) == 1
        and len(row.get("drop_player_ids") or []) <= 1
    ]
    expected = [
        (
            str(row.get("add_player_id") or ""),
            str(row.get("drop_player_id") or ""),
        )
        for row in (command.parameters or {}).get("expected_claims_before") or []
    ]
    if observed != expected:
        return ["The authenticated pending-waiver queue changed after the decision"]
    return []


def _trade_state_blockers(
    command: ExecutionCommand,
    context: dict[str, Any],
    ui_observation: dict[str, Any] | None = None,
) -> list[str]:
    """Rebind a trade to exact rosters, offer identity, and staged UI state."""

    parameters = command.parameters or {}
    ours = {
        str(row.get("player_id"))
        for row in (context.get("our_team") or {}).get("all_players") or []
    }
    expected_ours = {str(item) for item in parameters.get("expected_our_roster_before") or []}
    roster_id = int(parameters.get("counterparty_roster_id") or 0)
    counterpart = next(
        (
            roster
            for roster in context.get("league_rosters") or []
            if int(roster.get("roster_id") or 0) == roster_id
        ),
        None,
    )
    theirs = {
        str(row.get("player_id")) for row in (counterpart or {}).get("players") or []
    }
    expected_theirs = {
        str(item) for item in parameters.get("expected_counterparty_roster_before") or []
    }
    blockers: list[str] = []
    if ours != expected_ours:
        blockers.append("Our roster changed after the trade decision")
    if counterpart is None or theirs != expected_theirs:
        blockers.append("The counterparty roster changed after the trade decision")
    protected = {
        str(item)
        for item in (context.get("manual_constraints") or {}).get("protected_player_ids") or []
    }
    outgoing = {
        str(row.get("player_id")) for row in parameters.get("send_assets") or []
    }
    if outgoing & protected:
        blockers.append("The trade includes a manually protected player")
    observation = ui_observation or {}
    if not all(
        observation.get(key) is True
        for key in ("exact_partner_confirmed", "exact_assets_confirmed")
    ):
        blockers.append("The staged Sleeper trade UI did not confirm exact partner and assets")
    if command.action_type == "PROPOSE_TRADE":
        if observation.get("bounded_expiration_confirmed") is not True:
            blockers.append("The staged trade proposal has no bounded expiration")
        if float(parameters.get("decision_confidence") or 0) < settings.in_season_trade_min_confidence:
            blockers.append("The trade confidence is below the high-upside threshold")
        if parameters.get("upside_tier") != "high":
            blockers.append("The trade is not classified as high upside")
        if any(
            offer.get("direction") == "outgoing"
            for offer in (context.get("trade_market") or {}).get("active_offers") or []
        ):
            blockers.append("A Sleeper trade proposal is already active")
    else:
        fingerprint = str(parameters.get("offer_fingerprint") or "")
        exact_offer = next(
            (
                offer
                for offer in (context.get("trade_market") or {}).get("active_offers") or []
                if offer.get("direction") == "incoming"
                and str(offer.get("offer_fingerprint") or "") == fingerprint
            ),
            None,
        )
        if exact_offer is None:
            blockers.append("The exact incoming trade offer is no longer active")
        if observation.get("offer_fingerprint") != fingerprint:
            blockers.append("The staged incoming trade does not match the decision fingerprint")
        if command.action_type == "ACCEPT_TRADE" and (
            parameters.get("upside_tier") != "high"
            or float(parameters.get("decision_confidence") or 0)
            < settings.in_season_trade_min_confidence
        ):
            blockers.append("The incoming trade does not clear the high-upside acceptance gate")
    return blockers


def _csrf_signature(value: str) -> str:
    return hmac.new(settings.app_secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def _require_csrf(header: str | None, cookie: str | None) -> None:
    if not header or not cookie or not hmac.compare_digest(header, cookie):
        raise HTTPException(status_code=403, detail="Valid CSRF token required")
    try:
        value, signature = cookie.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Malformed CSRF token") from exc
    if not hmac.compare_digest(_csrf_signature(value), signature):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _require_lan(request: Request) -> None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    host = forwarded or (request.client.host if request.client else "")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="Approval requires a private LAN client"
        ) from exc
    if not (address.is_private or address.is_loopback):
        raise HTTPException(status_code=403, detail="Approval links only work on the private LAN")


def _require_agent(authorization: str | None) -> None:
    expected = f"Bearer {settings.shim_shared_secret}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid agent credential")


def _write_browser_evidence(snapshot: dict[str, Any], report_name: str) -> None:
    report = {
        **snapshot,
        "generated_at": snapshot.get("observed_at") or datetime.now(timezone.utc).isoformat(),
        "provider": "Sleeper authenticated UI",
    }
    path = settings.reports_dir / report_name
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(canonical_json(report) + "\n", encoding="utf-8")
    temporary.replace(path)


def _projection_snapshot_period_error(
    snapshot: dict[str, Any], *, expected_season: str, expected_week: int
) -> str | None:
    if str(snapshot.get("view_type") or "") != "weekly_projection":
        return "Authenticated UI snapshot did not prove it was in projection mode"
    if str(snapshot.get("season") or "") != expected_season:
        return (
            "Authenticated UI snapshot season did not match Sleeper's current season: "
            f"expected {expected_season}, observed {snapshot.get('season') or 'unknown'}"
        )
    try:
        observed_week = int(snapshot.get("week") or 0)
    except (TypeError, ValueError):
        observed_week = 0
    if observed_week != expected_week:
        return (
            "Authenticated UI snapshot week did not match Sleeper's current week: "
            f"expected {expected_week}, observed {observed_week or 'unknown'}"
        )
    return None


def _manager_execution_blocker(report: dict[str, Any]) -> str | None:
    state = str(report.get("manager_state") or "unavailable")
    if state == "ready":
        return None
    blockers = [str(item).strip() for item in report.get("blockers") or [] if str(item).strip()]
    detail = blockers[0] if blockers else f"Manager state is {state}"
    return f"Current expert plan is not executable: {detail}"


async def _overlay_current_execution_states(
    report: dict[str, Any], session: AsyncSession
) -> dict[str, Any]:
    """Keep file-backed plans honest when an operator changes a queued DB command."""

    execution_keys = (
        "lineup_execution",
        "acquisition_execution",
        "pending_waiver_execution",
        "ir_execution",
        "trade_execution",
    )
    command_ids = {
        str(item.get("command_id"))
        for key in execution_keys
        for item in ((report.get(key) or {}).get("queued") or [])
        if item.get("command_id")
    }
    if not command_ids:
        return report
    commands = list(
        await session.scalars(
            select(ExecutionCommand).where(ExecutionCommand.id.in_(command_ids))
        )
    )
    by_id = {str(command.id): command for command in commands}
    for key in execution_keys:
        execution = report.get(key)
        if not isinstance(execution, dict) or not execution.get("queued"):
            continue
        queued: list[dict[str, Any]] = []
        terminal: list[tuple[str, str]] = []
        for item in execution["queued"]:
            command = by_id.get(str(item.get("command_id") or ""))
            if command is None:
                queued.append(item)
                terminal.append(("blocked", "The recorded execution command is missing"))
                continue
            queued.append({**item, "status": command.status})
            if command.status in {"cancelled", "blocked", "failed", "unverified"}:
                result = command.result or {}
                terminal.append(
                    (
                        command.status,
                        str(result.get("message") or result.get("reason") or "").strip()
                        or f"The execution command is {command.status}",
                    )
                )
        execution["queued"] = queued
        if terminal:
            execution["state"], execution["reason"] = terminal[0]
    return report


def _free_agent_observation_targets(limit: int = 32) -> list[dict[str, str]]:
    """Prioritize exact candidates that need stable authenticated UI evidence."""

    report = read_report(settings.reports_dir / "in-season-plan.json", {})
    candidates = report.get("free_agent_candidates") or []
    candidate_by_id = {
        str(row.get("player_id")): row
        for row in candidates
        if isinstance(row, dict) and row.get("player_id")
    }
    decision = report.get("decision") or {}
    priority_ids: list[tuple[str, str]] = []
    for claim in decision.get("waiver_claims") or []:
        priority_ids.append((str(claim.get("add_player_id") or ""), "current_claim"))
    for watch in decision.get("watchlist") or []:
        priority_ids.append((str(watch.get("player_id") or ""), "current_watchlist"))
    for candidate in candidates:
        if candidate.get("ir_stash_candidate"):
            priority_ids.append((str(candidate.get("player_id") or ""), "ir_stash_candidate"))
    for candidate in candidates:
        reason = (
            "missing_authenticated_acquisition_state"
            if candidate.get("sleeper_acquisition_type") not in {"free_agent", "waiver"}
            else "waiver_shortlist"
        )
        priority_ids.append((str(candidate.get("player_id") or ""), reason))

    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for player_id, reason in priority_ids:
        candidate = candidate_by_id.get(player_id) or {}
        if not player_id or player_id in seen or not candidate:
            continue
        name = str(candidate.get("name") or "").strip()
        position = str(candidate.get("position") or "").strip().upper()
        team = str(candidate.get("team") or "").strip().upper()
        if not name or not position or not team:
            continue
        seen.add(player_id)
        targets.append(
            {
                "player_id": player_id,
                "name": name,
                "position": position,
                "team": team,
                "reason": reason,
            }
        )
        if len(targets) >= limit:
            break
    return targets


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/internal/agent/free-agent-targets")
async def free_agent_observation_targets(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_agent(authorization)
    targets = _free_agent_observation_targets()
    return {
        "league_id": settings.sleeper_league_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "targets": targets,
    }


@app.get("/health/ready")
async def ready(session: SessionDep) -> JSONResponse:
    checks: dict[str, Any] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "pass"
    except Exception as exc:
        checks["database"] = f"fail: {exc}"
    try:
        address = ipaddress.ip_address(settings.dashboard_bind_address)
        checks["dashboard_binding"] = (
            "pass" if (address.is_private or address.is_loopback) else "fail"
        )
    except ValueError:
        checks["dashboard_binding"] = "fail"
    report = read_report(settings.reports_dir / "readiness.json", {"ready": False})
    checks["league_report"] = "pass" if report.get("generated_at") else "fail"
    is_ready = all(value == "pass" for value in checks.values())
    return JSONResponse({"ready": is_ready, "checks": checks}, status_code=200 if is_ready else 503)


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/csrf")
async def csrf(response: Response) -> dict[str, str]:
    value = secrets.token_urlsafe(24)
    token = f"{value}.{_csrf_signature(value)}"
    response.set_cookie("fantasy_csrf", token, httponly=False, samesite="strict", secure=False)
    return {"token": token}


@app.get("/api/readiness")
async def readiness_report(session: SessionDep) -> dict[str, Any]:
    report = read_report(
        settings.reports_dir / "readiness.json", {"ready": False, "checks": [], "discrepancies": []}
    )
    manager = await _overlay_current_execution_states(
        read_report(settings.reports_dir / "in-season-plan.json", {}), session
    )
    capabilities = build_capability_readiness(settings, report, manager)
    in_season = (report.get("league") or {}).get("status") == "in_season"
    autopilot_ready = bool(capabilities["autopilot_ready"])
    checks = list(report.get("checks") or [])
    if in_season:
        checks.append(
            {
                "id": "manager_autopilot",
                "status": "pass" if autopilot_ready else "block",
                "summary": (
                    "Current manager plan is fully executable"
                    if autopilot_ready
                    else "Current manager plan has unattended execution blockers"
                ),
                "evidence": capabilities["autopilot_blockers"],
            }
        )
    return {
        **report,
        "ready": bool(report.get("ready")) and (autopilot_ready if in_season else True),
        "blocker_count": int(report.get("blocker_count") or 0)
        + int(in_season and not autopilot_ready),
        "checks": checks,
        "capabilities": capabilities,
    }


@app.get("/api/calendar")
async def calendar_report() -> dict[str, Any]:
    return read_report(settings.reports_dir / "next-14-days.json", {"events": []})


@app.get("/api/draft")
async def draft_report() -> dict[str, Any]:
    room = read_report(
        settings.reports_dir / "draft-room.json",
        {
            "recommendation": None,
            "simulations": [],
            "execution_blockers": ["Draft room has not been generated"],
        },
    )
    room["live"] = {
        "status": room.get("status"),
        "pick_count": room.get("pick_count", 0),
        "picks": room.get("recent_picks", []),
    }
    return room


@app.get("/api/in-season")
async def in_season_report() -> dict[str, Any]:
    return read_report(
        settings.reports_dir / "in-season-context.json",
        {"generated_at": None, "owner_roster": None, "owner_matchup": None},
    )


@app.get("/api/source-catalog")
async def source_catalog_report() -> dict[str, Any]:
    return read_report(
        settings.reports_dir / "in-season-sources.json",
        {"generated_at": None, "objective": None, "sources": []},
    )


@app.get("/api/projection-backtest")
async def projection_backtest_report() -> dict[str, Any]:
    return read_report(
        settings.reports_dir / "projection-backtest.json",
        {
            "generated_at": None,
            "status": "not_run",
            "qualification_status": "blocked",
            "qualification_blockers": ["Projection backtest has not run"],
        },
    )


@app.get("/api/championship-backtest")
async def championship_backtest_report() -> dict[str, Any]:
    return read_report(
        settings.reports_dir / "championship-backtest.json",
        {
            "generated_at": None,
            "status": "not_run",
            "qualification_status": "blocked",
            "qualification_blockers": ["Championship backtest has not run"],
        },
    )


def _fantasypros_dashboard_status() -> dict[str, Any]:
    if settings.fantasypros_api_key is None:
        return {"configured": False, "status": "awaiting_key"}
    quota = fantasypros_quota_status(settings)
    report = read_report(settings.reports_dir / "fantasypros-context.json", {})
    now = datetime.now(timezone.utc)
    last_refresh = None
    cooldown_remaining = 0
    try:
        last_refresh = datetime.fromisoformat(str(report.get("generated_at") or ""))
        if last_refresh.tzinfo is None:
            last_refresh = last_refresh.replace(tzinfo=timezone.utc)
        next_refresh = last_refresh.astimezone(timezone.utc) + timedelta(
            minutes=settings.fantasypros_emergency_refresh_cooldown_minutes
        )
        cooldown_remaining = max(int((next_refresh - now).total_seconds()), 0)
    except ValueError:
        pass
    return {
        "configured": True,
        "status": report.get("status") or "active",
        **quota,
        "last_refresh_at": last_refresh.isoformat() if last_refresh else None,
        "target_player_count": int(report.get("target_player_count") or 0),
        "target_player_coverage": float(report.get("target_player_coverage") or 0),
        "last_refresh_requests": int(report.get("requests_this_sync") or 0),
        "cooldown_remaining_seconds": cooldown_remaining,
        "emergency_refresh_available": bool(
            int(quota.get("total_remaining") or 0) > 0 and cooldown_remaining == 0
        ),
    }


@app.get("/api/fantasypros-quota")
async def fantasypros_quota_report() -> dict[str, Any]:
    return _fantasypros_dashboard_status()


@app.post("/api/fantasypros-refresh")
async def refresh_fantasypros(
    request: Request,
    x_csrf_token: str | None = Header(default=None),
    fantasy_csrf: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _require_lan(request)
    _require_csrf(x_csrf_token, fantasy_csrf)
    if settings.fantasypros_api_key is None:
        raise HTTPException(status_code=409, detail="FANTASYPROS_API_KEY is not configured")
    async with FANTASYPROS_REFRESH_LOCK:
        before = _fantasypros_dashboard_status()
        if int(before.get("cooldown_remaining_seconds") or 0) > 0:
            return {**before, "refresh_status": "cooldown"}
        report = await sync_fantasypros_context(
            settings,
            force=True,
            allow_reserve=True,
        )
        return {
            **_fantasypros_dashboard_status(),
            "refresh_status": report.get("status") or "complete",
            "requests_this_refresh": int(report.get("requests_this_sync") or 0),
        }


@app.get("/api/manager")
async def manager_report(session: SessionDep) -> dict[str, Any]:
    report = await _overlay_current_execution_states(
        read_report(
            settings.reports_dir / "in-season-plan.json",
            {
                "generated_at": None,
                "manager_state": "waiting_for_first_analysis",
                "execution_state": "analysis_only",
                "decision": None,
                "blockers": ["The first in-season analysis has not completed"],
            },
        ),
        session,
    )
    autopilot = build_manager_autopilot_status(report)
    return {
        **report,
        "autopilot_state": autopilot["state"],
        "autopilot_ready": autopilot["ready"],
        "autopilot_blockers": autopilot["blockers"],
        "execution_domains": autopilot["execution_domains"],
    }


@app.get("/api/source-health")
async def source_health(session: SessionDep) -> list[dict[str, Any]]:
    items = list(await session.scalars(select(SourceHealth).order_by(SourceHealth.provider)))
    return [
        {
            "provider": item.provider,
            "status": item.status,
            "last_success_at": item.last_success_at,
            "last_changed_at": item.last_changed_at,
            "consecutive_failures": item.consecutive_failures,
            "latency_ms": item.latency_ms,
            "last_error": item.last_error,
        }
        for item in items
    ]


@app.get("/api/source-dataset-health")
async def source_dataset_health(session: SessionDep) -> list[dict[str, Any]]:
    items = list(
        await session.scalars(
            select(SourceDatasetHealth).order_by(
                SourceDatasetHealth.provider, SourceDatasetHealth.dataset
            )
        )
    )
    return [
        {
            "provider": item.provider,
            "dataset": item.dataset,
            "status": item.status,
            "last_attempt_at": item.last_attempt_at,
            "last_success_at": item.last_success_at,
            "source_updated_at": item.source_updated_at,
            "consecutive_failures": item.consecutive_failures,
            "latency_ms": item.latency_ms,
            "expected_records": item.expected_records,
            "observed_records": item.observed_records,
            "completeness": item.completeness,
            "last_error": item.last_error,
        }
        for item in items
    ]


@app.get("/api/operational-incidents")
async def operational_incidents(session: SessionDep) -> list[dict[str, Any]]:
    items = list(
        await session.scalars(
            select(OperationalIncident)
            .where(OperationalIncident.status == "open")
            .order_by(OperationalIncident.severity, OperationalIncident.last_observed_at.desc())
        )
    )
    return [
        {
            "id": item.id,
            "category": item.category,
            "severity": item.severity,
            "status": item.status,
            "summary": item.summary,
            "details": item.details,
            "first_observed_at": item.first_observed_at,
            "last_observed_at": item.last_observed_at,
        }
        for item in items
    ]


@app.get("/api/host-health")
async def host_health() -> dict[str, Any]:
    report = read_report(
        settings.host_health_path,
        {
            "generated_at": None,
            "status": "unavailable",
            "checks": {},
        },
    )
    backup_check = await to_thread(verify_latest_backup, settings.backup_health_dir)
    checks = dict(report.get("checks") or {})
    checks["backup"] = backup_check
    checks["codex_worker"] = read_report(
        settings.codex_watchdog_path,
        {
            "generated_at": None,
            "status": "unavailable",
            "reason": "Codex worker watchdog has not reported yet",
        },
    )
    statuses = {str(row.get("status")) for row in checks.values() if isinstance(row, dict)}
    return {
        **report,
        "status": "healthy" if statuses <= {"healthy"} else "degraded",
        "checks": checks,
    }


@app.get("/api/actions")
async def actions(session: SessionDep) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    result: list[dict[str, Any]] = []
    for item in await list_actions(session):
        command = await session.scalar(
            select(ExecutionCommand)
            .where(ExecutionCommand.action_id == item.id)
            .order_by(ExecutionCommand.created_at.desc())
            .limit(1)
        )
        result.append(_jsonable_action(item, command, now=now))
    return result


@app.get("/api/acquisition-lifecycle")
async def acquisition_lifecycle() -> dict[str, Any]:
    return read_report(
        settings.reports_dir / "acquisition-lifecycle.json",
        {
            "generated_at": None,
            "status": "not_run",
            "claims": [],
            "active_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "requires_manager_refresh": False,
        },
    )


@app.get("/api/notifications")
async def notifications(
    session: SessionDep, limit: int = 50, channel: str | None = None
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    if channel not in {None, "dashboard", "email", "slack"}:
        raise HTTPException(status_code=422, detail="Unsupported notification channel")
    query = select(NotificationDelivery)
    if channel is not None:
        query = query.where(NotificationDelivery.channel == channel)
    deliveries = list(
        await session.scalars(
            query.order_by(NotificationDelivery.created_at.desc()).limit(bounded_limit)
        )
    )
    return [
        {
            "id": item.id,
            "action_id": item.action_id,
            "channel": item.channel,
            "kind": item.kind,
            "subject": item.subject,
            "body": str((item.payload or {}).get("body") or ""),
            "severity": str((item.payload or {}).get("severity") or "info"),
            "status": item.status,
            "attempt_count": item.attempt_count,
            "created_at": item.created_at,
            "delivered_at": item.delivered_at,
            "last_error": item.last_error,
        }
        for item in deliveries
    ]


@app.get("/api/trash-talk")
async def trash_talk_status(session: SessionDep) -> dict[str, Any]:
    control = await session.get(TrashTalkControl, "global")
    posts = list(
        await session.scalars(
            select(TrashTalkPost).order_by(TrashTalkPost.created_at.desc()).limit(50)
        )
    )
    current = datetime.now(timezone.utc)
    local_now = current.astimezone(ZoneInfo(settings.app_timezone))
    week_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=local_now.weekday()
    )
    counted = [
        post
        for post in posts
        if post.status in {"pending", "delivered"}
        and _as_utc(post.created_at) >= week_start.astimezone(timezone.utc)
    ]
    return {
        "configured": settings.trash_talk_enabled,
        "enabled": bool(settings.trash_talk_enabled and (control is None or control.enabled)),
        "paused_reason": control.paused_reason if control else None,
        "opted_out_roster_ids": control.opted_out_roster_ids if control else [],
        "weekly_target": settings.trash_talk_weekly_target,
        "weekly_limit": settings.trash_talk_weekly_limit,
        "weekly_count": len(counted),
        "image_upload_configured": settings.slack_image_upload_configured,
        "posts": [
            {
                "id": post.id,
                "trigger_type": post.trigger_type,
                "target_label": post.target_label,
                "message": post.message,
                "format": post.format,
                "status": post.status,
                "quality_score": post.quality_score,
                "safety": post.safety,
                "suppression_reason": post.suppression_reason,
                "evidence": post.evidence,
                "created_at": post.created_at,
                "delivered_at": post.delivered_at,
            }
            for post in posts
        ],
    }


@app.post("/api/trash-talk/pause")
async def pause_trash_talk(
    body: TrashTalkControlIn,
    session: SessionDep,
    x_csrf_token: str | None = Header(default=None),
    fantasy_csrf: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _require_csrf(x_csrf_token, fantasy_csrf)
    control = await session.get(TrashTalkControl, "global")
    if control is None:
        control = TrashTalkControl(id="global")
        session.add(control)
    control.enabled = False
    control.paused_reason = body.reason or "Paused by owner"
    await session.commit()
    return {"enabled": False, "paused_reason": control.paused_reason}


@app.post("/api/trash-talk/resume")
async def resume_trash_talk(
    body: TrashTalkControlIn,
    session: SessionDep,
    x_csrf_token: str | None = Header(default=None),
    fantasy_csrf: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _require_csrf(x_csrf_token, fantasy_csrf)
    control = await session.get(TrashTalkControl, "global")
    if control is None:
        control = TrashTalkControl(id="global")
        session.add(control)
    control.enabled = True
    control.paused_reason = None
    await session.commit()
    return {"enabled": bool(settings.trash_talk_enabled), "paused_reason": None}


@app.post("/api/trash-talk/feedback")
async def trash_talk_feedback(
    body: TrashTalkFeedbackIn,
    session: SessionDep,
    x_csrf_token: str | None = Header(default=None),
    fantasy_csrf: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _require_csrf(x_csrf_token, fantasy_csrf)
    post = await session.get(TrashTalkPost, body.post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Trash-talk post not found")
    post.safety = {**(post.safety or {}), "owner_feedback": body.rating}
    if body.rating == "crossed_line":
        control = await session.get(TrashTalkControl, "global")
        if control is None:
            control = TrashTalkControl(id="global")
            session.add(control)
        control.enabled = False
        control.paused_reason = f"Owner marked post {post.id} as crossed_line"
    await session.commit()
    return {"status": "recorded", "rating": body.rating}


@app.post("/api/trash-talk/opt-out")
async def trash_talk_opt_out(
    body: TrashTalkOptOutIn,
    session: SessionDep,
    x_csrf_token: str | None = Header(default=None),
    fantasy_csrf: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _require_csrf(x_csrf_token, fantasy_csrf)
    control = await session.get(TrashTalkControl, "global")
    if control is None:
        control = TrashTalkControl(id="global")
        session.add(control)
    roster_ids = set(control.opted_out_roster_ids or [])
    if body.opted_out:
        roster_ids.add(body.roster_id)
    else:
        roster_ids.discard(body.roster_id)
    control.opted_out_roster_ids = sorted(roster_ids)
    await session.commit()
    return {"roster_id": body.roster_id, "opted_out": body.opted_out}


@app.post("/api/actions/{action_id}/approve")
async def approve_action(
    action_id: str,
    body: ConfirmationIn,
    request: Request,
    session: SessionDep,
    x_csrf_token: str | None = Header(default=None),
    fantasy_csrf: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _require_lan(request)
    _require_csrf(x_csrf_token, fantasy_csrf)
    action = await session.scalar(
        select(ActionItem).where(ActionItem.id == action_id).with_for_update()
    )
    if not action or action.status != "approval_required":
        raise HTTPException(status_code=404, detail="Approval request not found")
    try:
        command = await confirm_action(session, action, body.nonce, settings)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"action": _jsonable_action(action), "command_id": command.id}


@app.post("/api/actions/{action_id}/cancel")
async def cancel_action(
    action_id: str,
    request: Request,
    session: SessionDep,
    x_csrf_token: str | None = Header(default=None),
    fantasy_csrf: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _require_lan(request)
    _require_csrf(x_csrf_token, fantasy_csrf)
    action = await session.scalar(
        select(ActionItem).where(ActionItem.id == action_id).with_for_update()
    )
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")

    command = await session.scalar(
        select(ExecutionCommand)
        .where(ExecutionCommand.action_id == action.id)
        .order_by(ExecutionCommand.created_at.desc())
        .with_for_update()
        .limit(1)
    )
    now = datetime.now(timezone.utc)
    approval_waiting = action.status == "approval_required" and command is None
    if not approval_waiting and (command is None or not _command_can_be_cancelled(command, now)):
        if (
            command is not None
            and command.status == "leased"
            and command.lease_until is not None
            and _as_utc(command.lease_until) > now
        ):
            detail = "The browser agent is already executing this action"
        else:
            detail = f"Action cannot be cancelled from status {action.status}"
        raise HTTPException(status_code=409, detail=detail)

    if command is not None:
        command.status = "cancelled"
        command.completed_at = now
        command.lease_owner = None
        command.lease_until = None
        command.result = {
            "message": "Cancelled by the owner before the browser write boundary",
            "evidence": {"cancelled_at": now.isoformat()},
        }
    confirmations = list(
        await session.scalars(
            select(ManualConfirmation).where(
                ManualConfirmation.action_id == action.id,
                ManualConfirmation.confirmed_at.is_(None),
                ManualConfirmation.invalidated_at.is_(None),
            )
        )
    )
    for confirmation in confirmations:
        confirmation.invalidated_at = now
    await transition_action(
        session,
        action,
        "cancelled",
        actor="owner",
        details={"reason": "Owner cancelled before browser write boundary"},
    )
    await session.commit()
    return {"action": _jsonable_action(action, command, now=now)}


@app.post("/internal/agent/heartbeat")
@app.post("/internal/shim/heartbeat", include_in_schema=False)
async def agent_heartbeat(
    body: HeartbeatIn,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _require_agent(authorization)
    await upsert_heartbeat(session, body.model_dump())
    in_season_context = read_report(settings.reports_dir / "in-season-context.json", {})
    expected_season = str(in_season_context.get("season") or "")
    try:
        expected_week = int(in_season_context.get("week") or 0)
    except (TypeError, ValueError):
        expected_week = 0
    lineup_snapshot = body.details.get("lineup_snapshot")
    lineup_period_error = (
        _projection_snapshot_period_error(
            lineup_snapshot,
            expected_season=expected_season,
            expected_week=expected_week,
        )
        if isinstance(lineup_snapshot, dict) and expected_season and expected_week
        else "Current Sleeper season/week is unavailable"
    )
    if (
        isinstance(lineup_snapshot, dict)
        and lineup_period_error is None
        and str(lineup_snapshot.get("league_id")) == settings.sleeper_league_id
        and str(lineup_snapshot.get("roster_id")) == str(settings.sleeper_roster_id)
        and isinstance(lineup_snapshot.get("players"), list)
        and len(lineup_snapshot["players"]) >= 11
    ):
        evidence = {
            "league_id": lineup_snapshot["league_id"],
            "roster_id": lineup_snapshot["roster_id"],
            "season": lineup_snapshot["season"],
            "week": lineup_snapshot["week"],
            "view_type": lineup_snapshot["view_type"],
            "players": lineup_snapshot["players"],
        }
        await record_observation(
            session,
            provider="Sleeper authenticated UI",
            endpoint="/team lineup projection snapshot",
            entity_type="weekly_roster_projections",
            entity_id=f"{settings.sleeper_league_id}:{settings.sleeper_roster_id}",
            payload=evidence,
            confidence=0.8,
        )
        await update_source_health(
            session,
            "Sleeper authenticated UI",
            success=True,
            digest=content_hash(evidence),
        )
        await update_dataset_health(
            session,
            "Sleeper authenticated UI",
            "lineup",
            success=True,
            digest=content_hash(evidence),
            expected_records=11,
            observed_records=len(lineup_snapshot["players"]),
        )
        _write_browser_evidence(lineup_snapshot, "browser-lineup.json")
    elif isinstance(lineup_snapshot, dict):
        players = lineup_snapshot.get("players")
        await update_dataset_health(
            session,
            "Sleeper authenticated UI",
            "lineup",
            success=False,
            expected_records=11,
            observed_records=len(players) if isinstance(players, list) else 0,
            error=lineup_period_error
            or "Lineup heartbeat was partial or identified the wrong league/roster",
        )
    free_agent_snapshot = body.details.get("free_agent_snapshot")
    free_agent_period_error = (
        _projection_snapshot_period_error(
            free_agent_snapshot,
            expected_season=expected_season,
            expected_week=expected_week,
        )
        if isinstance(free_agent_snapshot, dict) and expected_season and expected_week
        else "Current Sleeper season/week is unavailable"
    )
    if (
        isinstance(free_agent_snapshot, dict)
        and free_agent_period_error is None
        and str(free_agent_snapshot.get("league_id")) == settings.sleeper_league_id
        and isinstance(free_agent_snapshot.get("players"), list)
        and len(free_agent_snapshot["players"]) >= 20
    ):
        evidence = {
            "league_id": free_agent_snapshot["league_id"],
            "season": free_agent_snapshot["season"],
            "week": free_agent_snapshot["week"],
            "view_type": free_agent_snapshot["view_type"],
            "players": free_agent_snapshot["players"],
        }
        await record_observation(
            session,
            provider="Sleeper authenticated UI free agents",
            endpoint="/players free-agent projection snapshot",
            entity_type="weekly_free_agent_projections",
            entity_id=settings.sleeper_league_id,
            payload=evidence,
            confidence=0.75,
        )
        await update_source_health(
            session,
            "Sleeper authenticated UI free agents",
            success=True,
            digest=content_hash(evidence),
        )
        await update_dataset_health(
            session,
            "Sleeper authenticated UI free agents",
            "free_agents",
            success=True,
            digest=content_hash(evidence),
            expected_records=20,
            observed_records=len(free_agent_snapshot["players"]),
        )
        _write_browser_evidence(free_agent_snapshot, "browser-free-agents.json")
    elif isinstance(free_agent_snapshot, dict):
        players = free_agent_snapshot.get("players")
        await update_dataset_health(
            session,
            "Sleeper authenticated UI free agents",
            "free_agents",
            success=False,
            expected_records=20,
            observed_records=len(players) if isinstance(players, list) else 0,
            error=free_agent_period_error
            or "Free-agent heartbeat was partial or identified the wrong league",
        )
    pending_waiver_snapshot = body.details.get("pending_waiver_snapshot")
    if (
        isinstance(pending_waiver_snapshot, dict)
        and str(pending_waiver_snapshot.get("league_id")) == settings.sleeper_league_id
        and str(pending_waiver_snapshot.get("roster_id"))
        == str(settings.sleeper_roster_id)
        and isinstance(pending_waiver_snapshot.get("claims"), list)
    ):
        evidence = {
            "league_id": pending_waiver_snapshot["league_id"],
            "roster_id": pending_waiver_snapshot["roster_id"],
            "claims": pending_waiver_snapshot["claims"],
        }
        await record_observation(
            session,
            provider="Sleeper authenticated UI waivers",
            endpoint="/team My Waivers snapshot",
            entity_type="pending_waiver_claims",
            entity_id=(
                f"{settings.sleeper_league_id}:{settings.sleeper_roster_id}"
            ),
            payload=evidence,
            confidence=0.95,
        )
        await update_dataset_health(
            session,
            "Sleeper authenticated UI waivers",
            "pending_waivers",
            success=True,
            digest=content_hash(evidence),
            expected_records=len(pending_waiver_snapshot["claims"]),
            observed_records=len(pending_waiver_snapshot["claims"]),
        )
        _write_browser_evidence(
            pending_waiver_snapshot,
            "browser-pending-waivers.json",
        )
    trade_snapshot = body.details.get("trade_snapshot")
    trade_offers = trade_snapshot.get("offers") if isinstance(trade_snapshot, dict) else None
    valid_trade_offers = isinstance(trade_offers, list) and all(
        isinstance(offer, dict)
        and offer.get("direction") in {"incoming", "outgoing", "unknown"}
        and isinstance(offer.get("receive_player_ids"), list)
        and isinstance(offer.get("send_player_ids"), list)
        and isinstance(offer.get("offer_fingerprint"), str)
        for offer in (trade_offers or [])
    )
    if (
        isinstance(trade_snapshot, dict)
        and str(trade_snapshot.get("league_id")) == settings.sleeper_league_id
        and str(trade_snapshot.get("roster_id")) == str(settings.sleeper_roster_id)
        and trade_snapshot.get("proposal_builder_available") is True
        and valid_trade_offers
    ):
        evidence = {
            "league_id": trade_snapshot["league_id"],
            "roster_id": trade_snapshot["roster_id"],
            "interface_title": trade_snapshot.get("interface_title"),
            "offers": trade_offers,
        }
        await record_observation(
            session,
            provider="Sleeper authenticated UI trades",
            endpoint="/team Trades snapshot",
            entity_type="active_trade_offers",
            entity_id=f"{settings.sleeper_league_id}:{settings.sleeper_roster_id}",
            payload=evidence,
            confidence=0.85,
        )
        await update_dataset_health(
            session,
            "Sleeper authenticated UI trades",
            "trade_inbox",
            success=True,
            digest=content_hash(evidence),
            expected_records=None,
            observed_records=len(trade_offers),
        )
        _write_browser_evidence(trade_snapshot, "browser-trades.json")
    matchup_snapshot = body.details.get("matchup_snapshot")
    matchup_period_error = (
        _projection_snapshot_period_error(
            matchup_snapshot,
            expected_season=expected_season,
            expected_week=expected_week,
        )
        if isinstance(matchup_snapshot, dict) and expected_season and expected_week
        else "Current Sleeper season/week is unavailable"
    )
    matchup_sides = (
        (matchup_snapshot.get("our_team"), matchup_snapshot.get("opponent"))
        if isinstance(matchup_snapshot, dict)
        else (None, None)
    )
    if (
        isinstance(matchup_snapshot, dict)
        and matchup_period_error is None
        and str(matchup_snapshot.get("league_id")) == settings.sleeper_league_id
        and str(matchup_snapshot.get("roster_id")) == str(settings.sleeper_roster_id)
        and all(isinstance(side, dict) for side in matchup_sides)
        and all(
            isinstance(side.get("players"), list) and len(side["players"]) >= 11
            for side in matchup_sides
        )
    ):
        evidence = {
            "league_id": matchup_snapshot["league_id"],
            "roster_id": matchup_snapshot["roster_id"],
            "season": matchup_snapshot["season"],
            "week": matchup_snapshot["week"],
            "view_type": matchup_snapshot["view_type"],
            "our_team": matchup_snapshot["our_team"],
            "opponent": matchup_snapshot["opponent"],
        }
        await record_observation(
            session,
            provider="Sleeper authenticated UI matchup",
            endpoint="/matchup projection snapshot",
            entity_type="weekly_matchup_projections",
            entity_id=f"{settings.sleeper_league_id}:{settings.sleeper_roster_id}",
            payload=evidence,
            confidence=0.8,
        )
        await update_source_health(
            session,
            "Sleeper authenticated UI matchup",
            success=True,
            digest=content_hash(evidence),
        )
        await update_dataset_health(
            session,
            "Sleeper authenticated UI matchup",
            "matchup",
            success=True,
            digest=content_hash(evidence),
            expected_records=22,
            observed_records=sum(len(side["players"]) for side in matchup_sides),
        )
        _write_browser_evidence(matchup_snapshot, "browser-matchup.json")
    elif isinstance(matchup_snapshot, dict):
        observed = sum(
            len(side.get("players") or []) if isinstance(side, dict) else 0
            for side in matchup_sides
        )
        unavailable_reason = str(matchup_snapshot.get("unavailable_reason") or "")
        expected_absence = (
            matchup_period_error is None
            and str(matchup_snapshot.get("league_id")) == settings.sleeper_league_id
            and str(matchup_snapshot.get("roster_id")) == str(settings.sleeper_roster_id)
            and unavailable_reason.startswith("Sleeper has not populated the Week ")
        )
        await update_dataset_health(
            session,
            "Sleeper authenticated UI matchup",
            "matchup",
            success=False,
            expected_records=22,
            observed_records=observed,
            expected_absence=expected_absence,
            error=matchup_period_error
            or unavailable_reason
            or "Matchup heartbeat was partial or identified the wrong league/roster",
        )
    await session.commit()
    return {"status": "recorded"}


@app.post("/internal/agent/lease")
@app.post("/internal/shim/lease", include_in_schema=False)
async def agent_lease(
    body: dict[str, Any],
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> Response:
    _require_agent(authorization)
    agent_id = body.get("agent_id")
    if not agent_id:
        raise HTTPException(status_code=422, detail="agent_id required")
    action_types = body.get("action_types")
    if action_types is not None and (
        not isinstance(action_types, list)
        or not action_types
        or any(not isinstance(action_type, str) for action_type in action_types)
    ):
        raise HTTPException(status_code=422, detail="action_types must be a non-empty string list")
    command_id = body.get("command_id")
    if command_id is not None and not isinstance(command_id, str):
        raise HTTPException(status_code=422, detail="command_id must be a string")
    command = await lease_execution_command(
        session,
        agent_id=str(agent_id),
        command_id=command_id,
        action_types=action_types,
    )
    await session.commit()
    if command is None:
        return Response(status_code=204)
    return JSONResponse(command_contract(command))


@app.post("/internal/agent/commands/{command_id}/preflight")
async def agent_write_preflight(
    command_id: str,
    body: WritePreflightIn,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Rebuild all action-specific evidence at the final browser write boundary."""

    _require_agent(authorization)
    command = await session.scalar(
        select(ExecutionCommand).where(ExecutionCommand.id == command_id).with_for_update()
    )
    if command is None or command.lease_owner != body.agent_id:
        raise HTTPException(status_code=404, detail="Leased command not found")
    if command.status not in {"leased", "preflight"}:
        raise HTTPException(status_code=409, detail=f"Command is already {command.status}")
    now = datetime.now(timezone.utc)
    expires_at = _as_utc(command.expires_at)
    if expires_at is None or expires_at <= now:
        return {
            "allowed": False,
            "evaluated_at": now,
            "reason": "Command expired at the final write boundary",
            "gate": {"status": "blocked", "blockers": ["Command expired"]},
        }
    if command.action_type == "DRAFT_PLAYER":
        return {
            "allowed": True,
            "evaluated_at": now,
            "reason": "Draft board state is verified by the action-specific public API preflight",
            "gate": {"status": "action_specific", "blockers": []},
        }
    transaction_blocker = None
    if command.action_type in {"ADD_FREE_AGENT", "WAIVER_CLAIM"}:
        if not settings.in_season_acquisition_actions_enabled:
            transaction_blocker = "Acquisition execution is disabled by configuration"
        elif command.action_type == "ADD_FREE_AGENT" and not action_is_qualified(
            settings, "ADD_FREE_AGENT"
        ):
            transaction_blocker = (
                "Free-agent execution lacks transaction-backed live qualification"
            )
        elif command.action_type == "WAIVER_CLAIM" and (
            not waiver_selector_is_qualified(settings)
            or not waiver_submission_is_qualified(settings)
        ):
            transaction_blocker = "Waiver execution lacks current live qualification"
        elif (
            command.action_type == "WAIVER_CLAIM"
            and len((command.parameters or {}).get("claim_group") or []) > 1
            and (
                not settings.in_season_multi_claim_actions_enabled
                or not multi_claim_is_qualified(settings)
            )
        ):
            transaction_blocker = "Conditional multi-claim execution is not qualified"
    elif command.action_type in {"MOVE_TO_IR", "REMOVE_FROM_IR"}:
        if not settings.in_season_ir_actions_enabled:
            transaction_blocker = "IR execution is disabled by configuration"
        elif not action_is_qualified(settings, command.action_type):
            transaction_blocker = f"{command.action_type} lacks live qualification"
    elif command.action_type in {"CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS"}:
        if not settings.in_season_pending_waiver_actions_enabled:
            transaction_blocker = "Pending-waiver execution is disabled by configuration"
        elif not action_is_qualified(settings, command.action_type):
            transaction_blocker = f"{command.action_type} lacks live qualification"
    elif command.action_type in {"PROPOSE_TRADE", "ACCEPT_TRADE", "DECLINE_TRADE"}:
        if not settings.in_season_trade_actions_enabled:
            transaction_blocker = "Trade execution is disabled by configuration"
        elif not action_is_qualified(settings, command.action_type):
            transaction_blocker = f"{command.action_type} lacks live qualification"
    if transaction_blocker:
        return {
            "allowed": False,
            "evaluated_at": now,
            "reason": transaction_blocker,
            "gate": {"status": "blocked", "blockers": [transaction_blocker]},
        }
    manager_blocker = _manager_execution_blocker(
        read_report(settings.reports_dir / "in-season-plan.json", {})
    )
    if manager_blocker:
        return {
            "allowed": False,
            "evaluated_at": now,
            "reason": manager_blocker,
            "gate": {"status": "blocked", "blockers": [manager_blocker]},
        }
    try:
        preflight_settings = settings.model_copy(update={"championship_simulation_enabled": False})
        context = build_manager_context(preflight_settings, now=now)
        gate = await apply_operational_evidence(
            context,
            session,
            preflight_settings,
            purpose=command.action_type,
            now=now,
        )
    except (InSeasonExpertUnavailable, OSError, ValueError) as exc:
        return {
            "allowed": False,
            "evaluated_at": now,
            "reason": f"Manager evidence could not be rebuilt: {exc}",
            "gate": {"status": "blocked", "blockers": [str(exc)]},
        }
    if command.action_type in {"ADD_FREE_AGENT", "WAIVER_CLAIM"}:
        state_blockers = _acquisition_state_blockers(
            command,
            context,
            settings,
            body.ui_observation,
        )
        if state_blockers:
            gate = {
                **gate,
                "status": "blocked",
                "allows_acquisition_execution": False,
                "blockers": [*(gate.get("blockers") or []), *state_blockers],
            }
        allowed = bool(gate.get("allows_acquisition_execution"))
    elif command.action_type in {"PROPOSE_TRADE", "ACCEPT_TRADE", "DECLINE_TRADE"}:
        state_blockers = _trade_state_blockers(command, context, body.ui_observation)
        if state_blockers:
            gate = {
                **gate,
                "status": "blocked",
                "allows_acquisition_execution": False,
                "blockers": [*(gate.get("blockers") or []), *state_blockers],
            }
        allowed = bool(gate.get("allows_acquisition_execution"))
    elif command.action_type in {"MOVE_TO_IR", "REMOVE_FROM_IR"}:
        state_blockers = _ir_state_blockers(command, context)
        if state_blockers:
            gate = {
                **gate,
                "status": "blocked",
                "allows_lineup_execution": False,
                "blockers": [*(gate.get("blockers") or []), *state_blockers],
            }
        allowed = bool(gate.get("allows_lineup_execution"))
    elif command.action_type in {"CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS"}:
        state_blockers = _pending_waiver_state_blockers(command, context)
        if state_blockers:
            gate = {
                **gate,
                "status": "blocked",
                "allows_acquisition_execution": False,
                "blockers": [*(gate.get("blockers") or []), *state_blockers],
            }
        allowed = bool(gate.get("allows_acquisition_execution"))
    else:
        allowed = bool(gate.get("allows_lineup_execution"))
    return {
        "allowed": allowed,
        "evaluated_at": now,
        "reason": None if allowed else "Required evidence failed at the final write boundary",
        "state_hash": context["state_hash"],
        "evidence_hash": content_hash(gate),
        "gate": gate,
    }


@app.post("/internal/agent/commands/{command_id}/qualification-retry")
async def retry_prewrite_qualification(
    command_id: str,
    body: WritePreflightIn,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    """Requeue only a controlled waiver qualification that failed before clicking."""

    _require_agent(authorization)
    command = await session.scalar(
        select(ExecutionCommand).where(ExecutionCommand.id == command_id).with_for_update()
    )
    if command is None or command.action_type != "WAIVER_CLAIM":
        raise HTTPException(status_code=404, detail="Controlled waiver command not found")
    if command.status == "ready" and command.lease_owner is None:
        return {"status": "ready"}
    evidence = (command.result or {}).get("evidence") or {}
    if (
        command.status != "blocked"
        or evidence.get("qualification_run") is not True
        or evidence.get("write_attempted") is not False
    ):
        raise HTTPException(
            status_code=409,
            detail="Only a pre-write controlled qualification failure can be retried",
        )
    if command.attempt_count >= 3:
        raise HTTPException(status_code=409, detail="Controlled qualification retry limit reached")
    now = datetime.now(timezone.utc)
    if (_as_utc(command.expires_at) or now) <= now:
        raise HTTPException(status_code=409, detail="Controlled waiver command expired")
    action = await session.get(ActionItem, command.action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Controlled waiver action not found")
    await transition_action(
        session,
        action,
        "ready",
        actor="playwright-agent",
        details={
            "reason": "Retrying a controlled qualification that made no Sleeper write",
            "agent_id": body.agent_id,
            "previous_attempt_count": command.attempt_count,
        },
    )
    command.status = "ready"
    command.lease_owner = None
    command.lease_until = None
    command.completed_at = None
    command.last_error = None
    await session.commit()
    return {"status": "ready"}


@app.post("/internal/agent/commands/{command_id}/prewrite-retry")
async def retry_proven_prewrite_failure(
    command_id: str,
    body: WritePreflightIn,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    """Explicitly requeue one command only when evidence proves no click occurred."""

    _require_agent(authorization)
    retryable = {
        "WAIVER_CLAIM",
        "ADD_FREE_AGENT",
        "CANCEL_WAIVER_CLAIM",
        "REORDER_WAIVER_CLAIMS",
        "MOVE_TO_IR",
        "REMOVE_FROM_IR",
        "PROPOSE_TRADE",
        "ACCEPT_TRADE",
        "DECLINE_TRADE",
    }
    command = await session.scalar(
        select(ExecutionCommand).where(ExecutionCommand.id == command_id).with_for_update()
    )
    if command is None or command.action_type not in retryable:
        raise HTTPException(status_code=404, detail="Retryable transaction command not found")
    evidence = (command.result or {}).get("evidence") or {}
    if command.status != "blocked" or evidence.get("write_attempted") is not False:
        raise HTTPException(
            status_code=409,
            detail="Only an explicitly reviewed failure with write_attempted=false can be retried",
        )
    if command.attempt_count >= 3:
        raise HTTPException(status_code=409, detail="Pre-write retry limit reached")
    now = datetime.now(timezone.utc)
    if (_as_utc(command.expires_at) or now) <= now:
        raise HTTPException(status_code=409, detail="Transaction command expired")
    action = await session.get(ActionItem, command.action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Transaction action not found")
    await transition_action(
        session,
        action,
        "ready",
        actor="owner-reviewed-prewrite-retry",
        details={
            "reason": "Explicit retry after evidence proved no Sleeper write occurred",
            "agent_id": body.agent_id,
            "previous_attempt_count": command.attempt_count,
        },
    )
    command.status = "ready"
    command.lease_owner = None
    command.lease_until = None
    command.completed_at = None
    command.last_error = None
    await session.commit()
    return {"status": "ready"}


@app.post("/internal/agent/commands/{command_id}/result")
@app.post("/internal/shim/commands/{command_id}/result", include_in_schema=False)
async def agent_result(
    command_id: str,
    body: CommandResultIn,
    session: SessionDep,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _require_agent(authorization)
    command = await session.get(ExecutionCommand, command_id)
    if not command or command.lease_owner != body.agent_id:
        raise HTTPException(status_code=404, detail="Leased command not found")
    effective_status = body.status
    effective_message = body.message
    if command.action_type == "ADD_FREE_AGENT" and body.status == "verified":
        verification = body.evidence.get("verification") or {}
        transaction_backed = (
            body.evidence.get("public_api_verified") is True
            and verification.get("verified") is True
            and verification.get("verification_channel") == "public_api"
            and bool(verification.get("transaction_id"))
            and verification.get("transaction_status") == "complete"
        )
        if not transaction_backed:
            effective_status = "unverified"
            effective_message = (
                "Free-agent result lacked an exact completed Sleeper transaction; "
                "the write is uncertain and automatic retry is disabled"
            )
    try:
        validate_execution_transition(command.status, effective_status)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    previous_status = command.status
    previous_result = command.result if previous_status == "unverified" else None
    command.status = effective_status
    result_evidence = dict(body.evidence)
    if previous_result is not None and effective_status == "verified":
        result_evidence["prior_unverified_result"] = previous_result
    command.result = {"message": effective_message, "evidence": result_evidence}
    command.lease_until = (
        None
        if effective_status in {"verified", "unverified", "failed", "blocked"}
        else command.lease_until
    )
    if effective_status in {"verified", "unverified", "failed", "blocked"}:
        command.completed_at = datetime.now(timezone.utc)
    action = await session.get(ActionItem, command.action_id)
    if action:
        await transition_action(
            session,
            action,
            effective_status,
            actor="playwright-agent",
            details={
                **body.model_dump(),
                "status": effective_status,
                "message": effective_message,
                "evidence": result_evidence,
            },
        )
        if effective_status in {"unverified", "failed", "blocked"}:
            screenshot = result_evidence.get("failure_screenshot")
            attachments = [screenshot] if isinstance(screenshot, dict) else []
            printable_evidence = {
                key: value
                for key, value in result_evidence.items()
                if key != "failure_screenshot"
            }
            issue_type = f"{effective_status.upper()} {action.action_type.replace('_', ' ')}"
            detail = (
                f"Issue type: {issue_type}\n"
                f"Observed at: {datetime.now(timezone.utc).isoformat()}\n"
                f"Message: {effective_message}\n"
                f"Sleeper league: {command.league_id}\n"
                f"Roster: {command.roster_id}\n"
                f"Action ID: {action.id}\n"
                f"Command ID: {command.id}\n"
                f"Write attempted: {bool(result_evidence.get('write_attempted'))}\n"
                f"Automatic retry: disabled\n\n"
                f"Requested action:\n{json.dumps(action.exact_action, indent=2, default=str)}\n\n"
                f"Verification evidence:\n"
                f"{json.dumps(printable_evidence, indent=2, default=str)}"
            )
            if attachments:
                detail += "\n\nA browser screenshot from the failure state is attached."
            await queue_dashboard_notice(
                session,
                subject=f"Sleeper action {effective_status}: {action.title}",
                body=detail,
                dedupe_key=f"dashboard:execution:{command.id}:{effective_status}",
                action_id=action.id,
                severity="warning",
            )
            await queue_slack_alert(
                session,
                settings,
                subject=issue_type,
                body=detail,
                dedupe_key=f"execution:{command.id}:{effective_status}",
                action_id=action.id,
                attachments=attachments,
            )
    if effective_status == "verified":
        _record_waiver_submission_qualification(command, result_evidence)
        _record_in_season_action_qualification(command, result_evidence)
    await session.commit()
    return {"status": effective_status}
