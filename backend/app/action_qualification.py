from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings

ACTION_QUALIFICATION_FILES = {
    "ADD_FREE_AGENT": "free-agent-submission-qualification.json",
    "CANCEL_WAIVER_CLAIM": "pending-waiver-cancel-submission-qualification.json",
    "REORDER_WAIVER_CLAIMS": "pending-waiver-reorder-submission-qualification.json",
    "MOVE_TO_IR": "move-to-ir-submission-qualification.json",
    "REMOVE_FROM_IR": "remove-from-ir-submission-qualification.json",
    "PROPOSE_TRADE": "trade-propose-submission-qualification.json",
    "ACCEPT_TRADE": "trade-accept-submission-qualification.json",
    "DECLINE_TRADE": "trade-decline-submission-qualification.json",
}

LEGACY_QUALIFICATION_FILES = {
    "CANCEL_WAIVER_CLAIM": "pending-waiver-submission-qualification.json",
    "REORDER_WAIVER_CLAIMS": "pending-waiver-submission-qualification.json",
    "MOVE_TO_IR": "ir-submission-qualification.json",
    "REMOVE_FROM_IR": "ir-submission-qualification.json",
    "PROPOSE_TRADE": "trade-submission-qualification.json",
    "ACCEPT_TRADE": "trade-submission-qualification.json",
    "DECLINE_TRADE": "trade-submission-qualification.json",
}


def _read_report(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def qualification_path(settings: Settings, action_type: str) -> Path | None:
    filename = ACTION_QUALIFICATION_FILES.get(action_type)
    return settings.reports_dir / filename if filename else None


def action_is_qualified(settings: Settings, action_type: str) -> bool:
    """Require proof scoped to one exact write type and the current UI contract."""

    filenames = [ACTION_QUALIFICATION_FILES.get(action_type)]
    legacy = LEGACY_QUALIFICATION_FILES.get(action_type)
    if legacy:
        filenames.append(legacy)
    for filename in filenames:
        if not filename:
            continue
        report = _read_report(settings.reports_dir / filename)
        action_specific_proof = True
        if action_type == "ADD_FREE_AGENT":
            action_specific_proof = (
                report.get("verification_channel") == "public_api"
                and bool(report.get("transaction_id"))
                and report.get("transaction_status") == "complete"
            )
        if (
            report.get("status") == "qualified"
            and report.get("ui_contract_version") == settings.sleeper_ui_contract_version
            and str(report.get("league_id")) == settings.sleeper_league_id
            and int(report.get("roster_id") or 0) == settings.sleeper_roster_id
            and report.get("action_type") == action_type
            and report.get("write_attempted") is True
            and (
                report.get("public_api_verified") is True
                or report.get("authenticated_ui_verified") is True
            )
            and action_specific_proof
        ):
            return True
    return False


def multi_claim_is_qualified(settings: Settings) -> bool:
    report = _read_report(settings.reports_dir / "waiver-multi-claim-qualification.json")
    return (
        report.get("status") == "qualified"
        and report.get("ui_contract_version") == settings.sleeper_ui_contract_version
        and str(report.get("league_id")) == settings.sleeper_league_id
        and int(report.get("roster_id") or 0) == settings.sleeper_roster_id
        and int(report.get("verified_claim_count") or 0) >= 2
    )


def waiver_selector_is_qualified(settings: Settings) -> bool:
    report = _read_report(settings.reports_dir / "waiver-ui-qualification.json")
    evidence = report.get("evidence") or {}
    return (
        report.get("status") == "selector_qualified"
        and report.get("ui_contract_version") == settings.sleeper_ui_contract_version
        and str(report.get("league_id")) == settings.sleeper_league_id
        and int(report.get("roster_id") or 0) == settings.sleeper_roster_id
        and report.get("action_type") == "WAIVER_CLAIM"
        and report.get("write_attempted") is False
        and report.get("dialog_dismissed") is True
        and all(
            evidence.get(key) is True
            for key in (
                "row_identity_confirmed",
                "dialog_identity_confirmed",
                "exact_drop_confirmed",
                "submit_control_enabled",
            )
        )
    )


def waiver_submission_is_qualified(settings: Settings) -> bool:
    report = _read_report(settings.reports_dir / "waiver-submission-qualification.json")
    return (
        report.get("status") == "qualified"
        and report.get("ui_contract_version") == settings.sleeper_ui_contract_version
        and str(report.get("league_id")) == settings.sleeper_league_id
        and int(report.get("roster_id") or 0) == settings.sleeper_roster_id
        and report.get("action_type") == "WAIVER_CLAIM"
        and (
            (
                report.get("public_transaction_verified") is True
                and bool(report.get("transaction_id"))
            )
            or report.get("authenticated_pending_ui_verified") is True
        )
    )
