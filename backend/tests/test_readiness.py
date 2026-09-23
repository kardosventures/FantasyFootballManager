import json
from datetime import datetime, timezone

from app.config import Settings
from app.normalizer import (
    EXPECTED_ROSTER_POSITIONS,
    EXPECTED_SCORING,
    compare_expected,
    normalize_league,
)
from app.readiness import (
    build_capability_readiness,
    build_manager_autopilot_status,
    build_next_14_days,
    build_readiness_report,
    read_report,
)


def live_shaped():
    league = {
        "league_id": "1395499060898586624",
        "name": "Nederlanders Fantasy League",
        "season": "2026",
        "status": "pre_draft",
        "total_rosters": 12,
        "roster_positions": EXPECTED_ROSTER_POSITIONS,
        "scoring_settings": EXPECTED_SCORING,
        "settings": {"draft_rounds": 3, "max_subs": 0, "waiver_type": 2},
    }
    draft = {
        "draft_id": "1395499061699706880",
        "status": "pre_draft",
        "type": "snake",
        "start_time": 1788571800000,
        "settings": {"rounds": 16, "pick_timer": 120},
        "draft_order": {"someone-else": 12},
    }
    users = [
        {
            "user_id": "1398337982238343168",
            "display_name": "jimkardos",
            "metadata": {"team_name": "Jim.ai"},
        }
    ]
    rosters = [{"roster_id": 8, "owner_id": "1398337982238343168"}] + [
        {"roster_id": i, "owner_id": None if i in {2, 3, 4, 5} else f"owner-{i}"}
        for i in range(1, 13)
        if i != 8
    ]
    return league, draft, users, rosters


def test_read_report_falls_back_on_transient_io_error():
    class UnreadableReport:
        def read_text(self, *, encoding: str) -> str:
            raise OSError(5, "Input/output error")

    assert read_report(UnreadableReport(), {"available": False}) == {"available": False}


def test_live_acceptance_readiness_preserves_known_blockers():
    league, draft, users, rosters = live_shaped()
    normalized = normalize_league(league, [draft])
    discrepancies = compare_expected(normalized)
    report = build_readiness_report(
        settings=Settings(),
        league=league,
        normalized=normalized,
        users=users,
        rosters=rosters,
        draft=draft,
        discrepancies=discrepancies,
    )
    assert not report["ready"]
    checks = {item["id"]: item for item in report["checks"]}
    assert checks["owner_identity"]["status"] == "pass"
    assert checks["dst_21_27"]["status"] == "pass"
    assert checks["draft_shape"]["evidence"]["rounds"] == 16
    assert checks["owner_draft_slot"]["status"] == "block"
    assert checks["claimed_rosters"]["evidence"] == [2, 3, 4, 5]
    assert any(item["field"] == "draft_rounds" for item in discrepancies)
    assert checks["autosubs"]["status"] == "not_applicable"


def test_calendar_includes_waiver_review_and_processing_windows() -> None:
    calendar = build_next_14_days(
        {"waiver_semantics_confirmed": True},
        "America/Denver",
        waiver_weekday=2,
        waiver_hour=1,
        now=datetime(2026, 9, 14, 18, tzinfo=timezone.utc),
    )

    assert [(event["type"], event["starts_at"]) for event in calendar["events"]] == [
        ("waiver_review", "2026-09-15T19:00:00-06:00"),
        ("waiver_processing", "2026-09-16T01:00:00-06:00"),
        ("waiver_review", "2026-09-22T19:00:00-06:00"),
        ("waiver_processing", "2026-09-23T01:00:00-06:00"),
    ]


def test_calendar_omits_waiver_events_until_semantics_are_confirmed() -> None:
    calendar = build_next_14_days(
        {"waiver_semantics_confirmed": False},
        "America/Denver",
        now=datetime(2026, 9, 14, 18, tzinfo=timezone.utc),
    )
    assert calendar["events"] == []


def test_autopilot_is_blocked_when_a_ready_decision_cannot_execute() -> None:
    status = build_manager_autopilot_status(
        {
            "manager_state": "ready",
            "blockers": ["Conditional multi-claim execution is not enabled"],
            "lineup_execution": {"state": "scheduled", "preview_count": 1},
            "acquisition_execution": {
                "state": "blocked",
                "reason": "Conditional multi-claim execution is not enabled",
            },
            "pending_waiver_execution": {"state": "no_action", "preview_count": 0},
        }
    )

    assert status == {
        "ready": False,
        "state": "blocked",
        "decision_state": "ready",
        "blockers": ["Conditional multi-claim execution is not enabled"],
        "execution_domains": {
            "lineup": "scheduled",
            "acquisition": "blocked",
            "pending_waiver": "no_action",
        },
    }


def test_autopilot_is_ready_when_decision_and_execution_domains_are_clear() -> None:
    status = build_manager_autopilot_status(
        {
            "manager_state": "ready",
            "blockers": [],
            "lineup_execution": {"state": "scheduled", "preview_count": 1},
            "acquisition_execution": {"state": "no_action", "preview_count": 0},
            "trade_execution": {"state": "no_action", "preview_count": 0},
        }
    )

    assert status["ready"] is True
    assert status["state"] == "ready"
    assert status["execution_domains"] == {
        "lineup": "scheduled",
        "acquisition": "no_action",
        "trade": "no_action",
    }


def test_waiver_execution_requires_matching_read_only_ui_qualification(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        reports_dir=tmp_path,
        execution_mode="browser",
        in_season_acquisition_actions_enabled=True,
        browser_live_actions="WAIVER_CLAIM",
        waiver_semantics_confirmed=True,
        sleeper_ui_contract_version="ui-v1",
    )
    league_report = {
        "generated_at": "2026-09-14T18:00:00+00:00",
        "checks": [
            {"id": "owner_identity", "status": "pass"},
            {"id": "waiver_semantics", "status": "pass"},
        ],
    }
    manager_report = {
        "data_quality": {
            "evidence_gate": {"status": "ready", "allows_reasoning": True}
        }
    }
    qualification = {
        "status": "selector_qualified",
        "ui_contract_version": "ui-v1",
        "league_id": settings.sleeper_league_id,
        "roster_id": settings.sleeper_roster_id,
        "action_type": "WAIVER_CLAIM",
        "write_attempted": False,
        "dialog_dismissed": True,
        "evidence": {
            "row_identity_confirmed": True,
            "dialog_identity_confirmed": True,
            "exact_drop_confirmed": True,
            "submit_control_enabled": True,
        },
    }
    (tmp_path / "waiver-ui-qualification.json").write_text(
        json.dumps(qualification), encoding="utf-8"
    )

    capabilities = build_capability_readiness(settings, league_report, manager_report)

    assert capabilities["waiver_selector_qualified"] is True
    assert capabilities["waiver_submission_qualified"] is False
    assert capabilities["waiver_execution_ready"] is False

    (tmp_path / "waiver-submission-qualification.json").write_text(
        json.dumps(
            {
                "status": "qualified",
                "ui_contract_version": "ui-v1",
                "league_id": settings.sleeper_league_id,
                "roster_id": settings.sleeper_roster_id,
                "action_type": "WAIVER_CLAIM",
                "public_transaction_verified": True,
                "transaction_id": "tx-1",
            }
        ),
        encoding="utf-8",
    )
    capabilities = build_capability_readiness(settings, league_report, manager_report)
    assert capabilities["waiver_submission_qualified"] is True
    assert capabilities["waiver_execution_ready"] is True

    (tmp_path / "waiver-submission-qualification.json").write_text(
        json.dumps(
            {
                "status": "qualified",
                "ui_contract_version": "ui-v1",
                "league_id": settings.sleeper_league_id,
                "roster_id": settings.sleeper_roster_id,
                "action_type": "WAIVER_CLAIM",
                "public_transaction_verified": False,
                "authenticated_pending_ui_verified": True,
            }
        ),
        encoding="utf-8",
    )
    capabilities = build_capability_readiness(settings, league_report, manager_report)
    assert capabilities["waiver_submission_qualified"] is True


def test_remaining_in_season_actions_require_independent_qualification(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        reports_dir=tmp_path,
        execution_mode="browser",
        in_season_acquisition_actions_enabled=True,
        in_season_sequential_free_agent_actions_enabled=True,
        in_season_multi_claim_actions_enabled=True,
        in_season_pending_waiver_actions_enabled=True,
        in_season_ir_actions_enabled=True,
        browser_live_actions=(
            "WAIVER_CLAIM,ADD_FREE_AGENT,CANCEL_WAIVER_CLAIM,"
            "REORDER_WAIVER_CLAIMS,MOVE_TO_IR,REMOVE_FROM_IR"
        ),
        sleeper_ui_contract_version="ui-v2",
    )
    league_report = {
        "generated_at": "2026-09-14T18:00:00+00:00",
        "checks": [
            {"id": "owner_identity", "status": "pass"},
            {"id": "waiver_semantics", "status": "pass"},
        ],
    }
    manager_report = {
        "data_quality": {
            "evidence_gate": {"status": "ready", "allows_reasoning": True}
        },
        "ir_plan": {"status": "no_action"},
        "decision": {
            "pending_waiver_management": {"status": "keep"},
            "trade_status": "watch",
        },
    }

    initial = build_capability_readiness(settings, league_report, manager_report)

    assert initial["free_agent_execution_ready"] is False
    assert initial["sequential_free_agent_execution_ready"] is False
    assert initial["ir_execution_ready"] is False
    assert initial["pending_waiver_execution_ready"] is False
    assert initial["waiver_fallback_execution_ready"] is False
    assert initial["trade_intelligence_ready"] is True

    base = {
        "status": "qualified",
        "ui_contract_version": "ui-v2",
        "league_id": settings.sleeper_league_id,
        "roster_id": settings.sleeper_roster_id,
        "write_attempted": True,
    }
    reports = {
        "free-agent-submission-qualification.json": {
            **base,
            "action_type": "ADD_FREE_AGENT",
            "public_api_verified": True,
            "verification_channel": "public_api",
            "transaction_id": "free-agent-tx",
            "transaction_status": "complete",
        },
        "ir-submission-qualification.json": {
            **base,
            "action_type": "MOVE_TO_IR",
            "public_api_verified": True,
        },
        "pending-waiver-submission-qualification.json": {
            **base,
            "action_type": "CANCEL_WAIVER_CLAIM",
            "authenticated_ui_verified": True,
        },
        "waiver-multi-claim-qualification.json": {
            "status": "qualified",
            "ui_contract_version": "ui-v2",
            "league_id": settings.sleeper_league_id,
            "roster_id": settings.sleeper_roster_id,
            "verified_claim_count": 2,
        },
        "acquisition-lifecycle.json": {
            "generated_at": "2026-09-14T18:01:00+00:00",
            "retry_policy": "never_retry_uncertain_write",
        },
    }
    for filename, payload in reports.items():
        (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")

    qualified = build_capability_readiness(settings, league_report, manager_report)

    assert qualified["free_agent_execution_ready"] is True
    assert qualified["sequential_free_agent_execution_ready"] is True
    assert qualified["ir_execution_ready"] is True
    assert qualified["pending_waiver_execution_ready"] is True
    assert qualified["waiver_fallback_execution_ready"] is True
    assert qualified["acquisition_reconciliation_ready"] is True


def test_trust_then_verify_does_not_bypass_trade_qualification(tmp_path) -> None:
    league_report = {
        "generated_at": "2026-09-15T18:00:00+00:00",
        "checks": [
            {"id": "owner_identity", "status": "pass"},
            {"id": "waiver_semantics", "status": "pass"},
        ],
    }
    manager_report = {
        "data_quality": {
            "evidence_gate": {"status": "ready", "allows_reasoning": True}
        },
        "decision": {
            "trade_status": "watch",
            "pending_waiver_management": {"status": "keep"},
        },
    }
    base = dict(
        _env_file=None,
        reports_dir=tmp_path,
        execution_mode="browser",
        in_season_transaction_trust_then_verify=True,
        in_season_trade_actions_enabled=True,
    )
    incomplete = Settings(
        **base,
        browser_live_actions="PROPOSE_TRADE,ACCEPT_TRADE",
    )
    complete = Settings(
        **base,
        browser_live_actions="PROPOSE_TRADE,ACCEPT_TRADE,DECLINE_TRADE",
    )

    assert build_capability_readiness(
        incomplete, league_report, manager_report
    )["trade_execution_ready"] is False
    capabilities = build_capability_readiness(
        complete, league_report, manager_report
    )
    assert capabilities["trade_execution_ready"] is False
    assert capabilities["transaction_guard_mode"] == "trust_then_verify"
