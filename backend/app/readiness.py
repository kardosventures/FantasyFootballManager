from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.action_qualification import (
    action_is_qualified,
    multi_claim_is_qualified,
    waiver_selector_is_qualified,
    waiver_submission_is_qualified,
)
from app.config import Settings
from app.utils import canonical_json
from app.waivers import next_weekly_waiver


def _check(check_id: str, status: str, summary: str, evidence: Any = None) -> dict[str, Any]:
    return {"id": check_id, "status": status, "summary": summary, "evidence": evidence}


def build_readiness_report(
    *,
    settings: Settings,
    league: dict[str, Any],
    normalized: dict[str, Any],
    users: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    draft: dict[str, Any],
    discrepancies: list[dict[str, Any]],
) -> dict[str, Any]:
    owner = next(
        (item for item in users if str(item.get("user_id")) == settings.sleeper_owner_user_id), None
    )
    owner_roster = next(
        (item for item in rosters if str(item.get("owner_id")) == settings.sleeper_owner_user_id),
        None,
    )
    owner_team_name = (owner.get("metadata") or {}).get("team_name") if owner else None
    unclaimed = [item.get("roster_id") for item in rosters if not item.get("owner_id")]
    draft_order = draft.get("draft_order") or {}
    owner_slot = draft_order.get(settings.sleeper_owner_user_id)
    start_ms = draft.get("start_time")
    start_local = None
    if start_ms:
        start_local = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).astimezone(
            ZoneInfo(settings.app_timezone)
        )
    checks = [
        _check(
            "owner_identity",
            "pass"
            if owner
            and owner_team_name == "Jim.ai"
            and owner_roster
            and owner_roster.get("roster_id") == settings.sleeper_roster_id
            else "block",
            "Jim.ai resolves to roster 8"
            if owner_team_name == "Jim.ai"
            else "Configured owner/team identity did not match Jim.ai",
            {
                "user_id": settings.sleeper_owner_user_id,
                "display_name": owner.get("display_name") if owner else None,
                "team_name": owner_team_name,
                "roster_id": owner_roster.get("roster_id") if owner_roster else None,
            },
        ),
        _check(
            "dst_21_27",
            "pass"
            if normalized.get("scoring_settings", {}).get("pts_allow_21_27") == 0
            else "block",
            "D/ST points allowed 21–27 is 0",
            normalized.get("scoring_settings", {}).get("pts_allow_21_27"),
        ),
        _check(
            "draft_shape",
            "pass"
            if (draft.get("settings") or {}).get("rounds") == 16 and draft.get("type") == "snake"
            else "block",
            "16-round snake draft",
            {
                "starts_at": start_local.isoformat() if start_local else None,
                "rounds": (draft.get("settings") or {}).get("rounds"),
                "timer_seconds": (draft.get("settings") or {}).get("pick_timer"),
            },
        ),
        _check(
            "owner_draft_slot",
            "pass" if owner_slot else "block",
            f"Owner draft slot is {owner_slot}"
            if owner_slot
            else "Jim.ai draft slot is not assigned",
            owner_slot,
        ),
        _check(
            "claimed_rosters",
            "pass" if not unclaimed else "block",
            "All rosters are claimed"
            if not unclaimed
            else f"{len(unclaimed)} rosters are unclaimed",
            unclaimed,
        ),
        _check(
            "autosubs",
            "pass" if normalized.get("max_subs", 0) > 0 else "not_applicable",
            "AutoSubs enabled"
            if normalized.get("max_subs", 0) > 0
            else "AutoSubs are not available in this league; lineup automation remains independent",
            normalized.get("max_subs"),
        ),
        _check(
            "waiver_semantics",
            "pass" if normalized.get("waiver_semantics_confirmed") else "block",
            "Waiver timing semantics confirmed"
            if normalized.get("waiver_semantics_confirmed")
            else "Waiver enum/timing semantics require confirmation",
            normalized.get("waiver_raw"),
        ),
    ]
    blockers = [item for item in checks if item["status"] == "block"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league": {
            "league_id": league.get("league_id"),
            "name": league.get("name"),
            "season": league.get("season"),
            "status": league.get("status"),
        },
        "ready": not blockers,
        "checks": checks,
        "blocker_count": len(blockers),
        "discrepancies": discrepancies,
    }


def build_next_14_days(
    normalized: dict[str, Any],
    timezone_name: str,
    *,
    waiver_weekday: int = 2,
    waiver_hour: int = 1,
    waiver_semantics_confirmed: bool | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(days=14)
    events: list[dict[str, Any]] = []
    draft_start = normalized.get("draft", {}).get("start_time")
    if draft_start:
        starts_at = datetime.fromisoformat(draft_start)
        if now <= starts_at <= end:
            events.append(
                {
                    "type": "draft",
                    "title": "Sleeper league draft",
                    "starts_at": starts_at.astimezone(ZoneInfo(timezone_name)).isoformat(),
                    "priority": "P0",
                }
            )
    semantics_confirmed = (
        normalized.get("waiver_semantics_confirmed")
        if waiver_semantics_confirmed is None
        else waiver_semantics_confirmed
    )
    if semantics_confirmed:
        waiver_at = next_weekly_waiver(
            now=now,
            weekday=waiver_weekday,
            hour=waiver_hour,
            timezone_name=timezone_name,
        )
        while waiver_at.astimezone(timezone.utc) <= end:
            review_at = waiver_at - timedelta(hours=6)
            if review_at.astimezone(timezone.utc) >= now:
                events.append(
                    {
                        "type": "waiver_review",
                        "title": "Finalize season-long waiver claim tree",
                        "starts_at": review_at.isoformat(),
                        "priority": "P0",
                    }
                )
            events.append(
                {
                    "type": "waiver_processing",
                    "title": "Sleeper weekly waivers process",
                    "starts_at": waiver_at.isoformat(),
                    "priority": "P0",
                }
            )
            waiver_at += timedelta(days=7)
    events.sort(key=lambda event: event["starts_at"])
    return {"generated_at": now.isoformat(), "timezone": timezone_name, "events": events}


def write_reports(reports_dir: Path, readiness: dict[str, Any], calendar: dict[str, Any]) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "readiness.json").write_text(canonical_json(readiness) + "\n", encoding="utf-8")
    (reports_dir / "next-14-days.json").write_text(
        canonical_json(calendar) + "\n", encoding="utf-8"
    )
    lines = [
        "# League readiness",
        "",
        f"Generated: {readiness['generated_at']}",
        f"Overall: {'READY' if readiness['ready'] else 'BLOCKED'}",
        "",
    ]
    for item in readiness["checks"]:
        lines.append(f"- [{item['status'].upper()}] {item['summary']}")
    lines.extend(["", "## Discrepancies", ""])
    lines.extend(
        f"- [{item['severity'].upper()}] {item['field']}: {item['message']}"
        for item in readiness["discrepancies"]
    )
    (reports_dir / "readiness.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_report(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def build_manager_autopilot_status(manager_report: dict[str, Any]) -> dict[str, Any]:
    """Separate decision readiness from unattended execution readiness."""

    manager_state = str(manager_report.get("manager_state") or "unavailable")
    blockers = [
        str(item).strip()
        for item in manager_report.get("blockers") or []
        if str(item).strip()
    ]
    lifecycle = manager_report.get("acquisition_lifecycle") or {}
    unresolved = [
        row
        for row in lifecycle.get("claims") or []
        if row.get("outcome_status")
        in {"pending", "scheduled", "awaiting_reconciliation", "unverified"}
    ]
    if unresolved:
        blockers.append(
            f"{len(unresolved)} acquisition write(s) still require reconciliation"
        )
    execution_domains: dict[str, str] = {}
    blocking_states = {
        "approval_required",
        "blocked",
        "cancelled",
        "failed",
        "unverified",
    }
    for domain, key in (
        ("lineup", "lineup_execution"),
        ("acquisition", "acquisition_execution"),
        ("pending_waiver", "pending_waiver_execution"),
        ("ir", "ir_execution"),
        ("trade", "trade_execution"),
    ):
        execution = manager_report.get(key)
        if not isinstance(execution, dict):
            continue
        state = str(execution.get("state") or "unknown")
        execution_domains[domain] = state
        analysis_only_with_actions = (
            state == "analysis_only" and int(execution.get("preview_count") or 0) > 0
        )
        if state not in blocking_states and not analysis_only_with_actions:
            continue
        reason = str(execution.get("reason") or "").strip()
        blocker = reason or f"{domain.replace('_', ' ').title()} execution is {state}"
        if blocker not in blockers:
            blockers.append(blocker)

    if manager_state != "ready" and not blockers:
        blockers.append(f"Manager decision state is {manager_state}")
    ready = manager_state == "ready" and not blockers
    return {
        "ready": ready,
        "state": "ready" if ready else "blocked",
        "decision_state": manager_state,
        "blockers": blockers,
        "execution_domains": execution_domains,
    }


def build_capability_readiness(
    settings: Settings,
    league_report: dict[str, Any],
    manager_report: dict[str, Any],
) -> dict[str, Any]:
    gate = (manager_report.get("data_quality") or {}).get("evidence_gate") or {}
    league_checks = {row.get("id"): row for row in league_report.get("checks") or []}
    identity_ready = (league_checks.get("owner_identity") or {}).get("status") == "pass"
    waiver_ready = (league_checks.get("waiver_semantics") or {}).get("status") == "pass"
    data_ready = gate.get("status") in {"ready", "degraded"} and bool(gate.get("allows_reasoning"))
    intelligence = manager_report.get("intelligence") or {}
    promotion = intelligence.get("promotion") or {}
    snapshot_counts = intelligence.get("snapshots_persisted") or {}
    championship = manager_report.get("championship_outlook") or {}
    waiver_selector_qualified = waiver_selector_is_qualified(settings)
    waiver_submission_qualified = waiver_submission_is_qualified(settings)
    free_agent_submission_qualified = action_is_qualified(settings, "ADD_FREE_AGENT")
    ir_qualified_actions = [
        action_type
        for action_type in ("MOVE_TO_IR", "REMOVE_FROM_IR")
        if action_is_qualified(settings, action_type)
    ]
    ir_submission_qualified = len(ir_qualified_actions) == 2
    pending_waiver_qualified_actions = [
        action_type
        for action_type in ("CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS")
        if action_is_qualified(settings, action_type)
    ]
    pending_waiver_submission_qualified = len(pending_waiver_qualified_actions) == 2
    trade_qualified_actions = [
        action_type
        for action_type in ("PROPOSE_TRADE", "ACCEPT_TRADE", "DECLINE_TRADE")
        if action_is_qualified(settings, action_type)
    ]
    trade_submission_qualified = len(trade_qualified_actions) == 3
    waiver_fallback_qualified = multi_claim_is_qualified(settings)
    lifecycle = read_report(settings.reports_dir / "acquisition-lifecycle.json", {})
    acquisition_reconciliation_ready = (
        bool(lifecycle.get("generated_at"))
        and lifecycle.get("retry_policy") == "never_retry_uncertain_write"
    )
    ir_plan = manager_report.get("ir_plan") or {}
    pending_management = (manager_report.get("decision") or {}).get(
        "pending_waiver_management"
    ) or {}
    trade_status = str((manager_report.get("decision") or {}).get("trade_status") or "")
    browser_mode = settings.execution_mode == "browser"
    trust_then_verify = settings.in_season_transaction_trust_then_verify
    autopilot = build_manager_autopilot_status(manager_report)
    return {
        "platform_ready": bool(league_report.get("generated_at")),
        "data_ready": data_ready,
        "manager_decision_ready": manager_report.get("manager_state") == "ready",
        "autopilot_ready": autopilot["ready"],
        "autopilot_state": autopilot["state"],
        "autopilot_blockers": autopilot["blockers"],
        "execution_domains": autopilot["execution_domains"],
        "lineup_decision_ready": identity_ready and data_ready,
        "lineup_execution_ready": (
            identity_ready
            and data_ready
            and settings.execution_mode == "browser"
            and settings.in_season_lineup_actions_enabled
            and "SET_LINEUP" in settings.live_browser_actions
        ),
        "waiver_decision_ready": identity_ready and data_ready and waiver_ready,
        "waiver_selector_qualified": waiver_selector_qualified,
        "waiver_submission_qualified": waiver_submission_qualified,
        "waiver_execution_ready": (
            identity_ready
            and data_ready
            and waiver_ready
            and waiver_selector_qualified
            and waiver_submission_qualified
            and settings.execution_mode == "browser"
            and settings.in_season_acquisition_actions_enabled
            and "WAIVER_CLAIM" in settings.live_browser_actions
        ),
        "waiver_fallback_qualified": waiver_fallback_qualified,
        "waiver_fallback_execution_ready": (
            identity_ready
            and data_ready
            and waiver_ready
            and waiver_fallback_qualified
            and browser_mode
            and settings.in_season_acquisition_actions_enabled
            and settings.in_season_multi_claim_actions_enabled
            and "WAIVER_CLAIM" in settings.live_browser_actions
        ),
        "pending_waiver_decision_ready": (
            identity_ready and data_ready and bool(pending_management.get("status"))
        ),
        "pending_waiver_submission_qualified": pending_waiver_submission_qualified,
        "pending_waiver_qualified_actions": pending_waiver_qualified_actions,
        "pending_waiver_execution_ready": (
            identity_ready
            and data_ready
            and bool(pending_waiver_qualified_actions)
            and browser_mode
            and settings.in_season_pending_waiver_actions_enabled
            and bool(
                {"CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS"}
                & settings.live_browser_actions
            )
        ),
        "free_agent_decision_ready": identity_ready and data_ready,
        "free_agent_submission_qualified": free_agent_submission_qualified,
        "free_agent_execution_ready": (
            identity_ready
            and data_ready
            and free_agent_submission_qualified
            and browser_mode
            and settings.in_season_acquisition_actions_enabled
            and "ADD_FREE_AGENT" in settings.live_browser_actions
        ),
        "sequential_free_agent_execution_ready": (
            identity_ready
            and data_ready
            and free_agent_submission_qualified
            and browser_mode
            and settings.in_season_acquisition_actions_enabled
            and settings.in_season_sequential_free_agent_actions_enabled
            and "ADD_FREE_AGENT" in settings.live_browser_actions
        ),
        "ir_decision_ready": (
            identity_ready and data_ready and bool(ir_plan.get("status"))
        ),
        "ir_applicability": (
            "not_applicable"
            if ir_plan.get("status") == "not_applicable"
            else "available"
        ),
        "ir_submission_qualified": ir_submission_qualified,
        "ir_qualified_actions": ir_qualified_actions,
        "ir_execution_ready": (
            identity_ready
            and data_ready
            and bool(ir_qualified_actions)
            and browser_mode
            and settings.in_season_ir_actions_enabled
            and bool(
                {"MOVE_TO_IR", "REMOVE_FROM_IR"} & settings.live_browser_actions
            )
        ),
        "trade_intelligence_ready": (
            identity_ready
            and data_ready
            and trade_status in {"ready", "watch", "no_action"}
        ),
        "trade_submission_qualified": trade_submission_qualified,
        "trade_qualified_actions": trade_qualified_actions,
        "trade_execution_ready": (
            identity_ready
            and data_ready
            and browser_mode
            and settings.in_season_trade_actions_enabled
            and trade_submission_qualified
            and {
                "PROPOSE_TRADE",
                "ACCEPT_TRADE",
                "DECLINE_TRADE",
            }.issubset(settings.live_browser_actions)
        ),
        "transaction_guard_mode": (
            "trust_then_verify" if trust_then_verify else "qualified_only"
        ),
        "acquisition_reconciliation_ready": acquisition_reconciliation_ready,
        "dashboard_notifications_ready": bool(league_report.get("generated_at")),
        "slack_notifications_ready": settings.slack_notifications_configured,
        "usage_shadow_ready": int(snapshot_counts.get("usage") or 0) > 0,
        "projection_shadow_ready": int(snapshot_counts.get("projections") or 0) > 0,
        "championship_shadow_ready": championship.get("status") == "complete",
        "intelligence_authority": (
            "authoritative"
            if ((promotion.get("projection") or {}).get("authoritative"))
            else "shadow_locked"
        ),
        "draft_ready": league_report.get("league", {}).get("status") == "drafting"
        and not league_report.get("blocker_count"),
        "autosubs": "not_applicable"
        if (league_checks.get("autosubs") or {}).get("status") == "not_applicable"
        else "available",
    }
