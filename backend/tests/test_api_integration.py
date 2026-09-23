import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_session
from app.main import (
    _acquisition_state_blockers,
    _manager_execution_blocker,
    _overlay_current_execution_states,
    _projection_snapshot_period_error,
    _record_waiver_submission_qualification,
    app,
    settings,
)
from app.models import ActionAudit, ActionItem, ExecutionCommand
from app.utils import canonical_json


def test_projection_snapshot_period_must_be_explicit_and_current() -> None:
    valid = {
        "season": "2026",
        "week": 2,
        "view_type": "weekly_projection",
    }
    assert (
        _projection_snapshot_period_error(valid, expected_season="2026", expected_week=2)
        is None
    )
    assert "expected 2, observed 1" in _projection_snapshot_period_error(
        {**valid, "week": 1}, expected_season="2026", expected_week=2
    )
    assert "projection mode" in _projection_snapshot_period_error(
        {**valid, "view_type": "weekly_result"},
        expected_season="2026",
        expected_week=2,
    )


@pytest.mark.asyncio
async def test_health_dashboard_reads_and_agent_authentication():
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.get("/api/readiness")).status_code == 200
        assert (await client.get("/api/draft")).status_code == 200
        assert (await client.get("/api/actions")).json() == []
        assert (await client.get("/api/notifications?channel=dashboard&limit=5")).json() == []
        assert (await client.get("/api/notifications?channel=unknown")).status_code == 422
        assert (
            await client.post("/internal/agent/lease", json={"agent_id": "test"})
        ).status_code == 401
        heartbeat = await client.post(
            "/internal/agent/heartbeat",
            headers={"Authorization": f"Bearer {settings.shim_shared_secret}"},
            json={
                "agent_id": "test-agent",
                "app_version": "149.1",
                "app_running": True,
                "session_available": True,
                "execution_mode": "dry_run",
            },
        )
        assert heartbeat.status_code == 200
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_in_season_readiness_exposes_a_blocked_autopilot(
    monkeypatch, tmp_path
) -> None:
    configured = settings.model_copy(update={"reports_dir": tmp_path})
    (tmp_path / "readiness.json").write_text(
        canonical_json(
            {
                "generated_at": "2026-09-15T18:00:00+00:00",
                "ready": True,
                "blocker_count": 0,
                "league": {"status": "in_season"},
                "checks": [
                    {"id": "owner_identity", "status": "pass", "summary": "Owner"},
                    {
                        "id": "waiver_semantics",
                        "status": "pass",
                        "summary": "Waivers",
                    },
                ],
                "discrepancies": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "in-season-plan.json").write_text(
        canonical_json(
            {
                "manager_state": "ready",
                "execution_state": "scheduled",
                "blockers": ["Conditional multi-claim execution is not enabled"],
                "data_quality": {
                    "evidence_gate": {
                        "status": "ready",
                        "allows_reasoning": True,
                    }
                },
                "lineup_execution": {"state": "scheduled", "preview_count": 1},
                "acquisition_execution": {
                    "state": "blocked",
                    "reason": "Conditional multi-claim execution is not enabled",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.main.settings", configured)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        readiness = (await client.get("/api/readiness")).json()
        manager = (await client.get("/api/manager")).json()

    assert readiness["ready"] is False
    assert readiness["blocker_count"] == 1
    assert readiness["capabilities"]["manager_decision_ready"] is True
    assert readiness["capabilities"]["autopilot_ready"] is False
    assert readiness["checks"][-1] == {
        "id": "manager_autopilot",
        "status": "block",
        "summary": "Current manager plan has unattended execution blockers",
        "evidence": ["Conditional multi-claim execution is not enabled"],
    }
    assert manager["manager_state"] == "ready"
    assert manager["autopilot_state"] == "blocked"
    assert manager["autopilot_blockers"] == [
        "Conditional multi-claim execution is not enabled"
    ]


@pytest.mark.asyncio
async def test_agent_receives_prioritized_stable_free_agent_targets(monkeypatch, tmp_path):
    configured = settings.model_copy(update={"reports_dir": tmp_path})
    (tmp_path / "in-season-plan.json").write_text(
        canonical_json(
            {
                "decision": {
                    "waiver_claims": [{"add_player_id": "candidate-2"}],
                    "watchlist": [{"player_id": "candidate-1"}],
                },
                "free_agent_candidates": [
                    {
                        "player_id": "candidate-1",
                        "name": "First Player",
                        "position": "RB",
                        "team": "DEN",
                    },
                    {
                        "player_id": "candidate-2",
                        "name": "Second Player",
                        "position": "WR",
                        "team": "CLE",
                        "sleeper_acquisition_type": "waiver",
                    },
                    {
                        "player_id": "candidate-3",
                        "name": "IR Stash",
                        "position": "WR",
                        "team": "NYJ",
                        "ir_stash_candidate": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.main.settings", configured)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/internal/agent/free-agent-targets")).status_code == 401
        response = await client.get(
            "/internal/agent/free-agent-targets",
            headers={"Authorization": f"Bearer {configured.shim_shared_secret}"},
        )

    assert response.status_code == 200
    targets = response.json()["targets"]
    assert [target["player_id"] for target in targets] == [
        "candidate-2",
        "candidate-1",
        "candidate-3",
    ]
    assert targets[0]["reason"] == "current_claim"
    assert targets[1]["reason"] == "current_watchlist"
    assert targets[2]["reason"] == "ir_stash_candidate"


@pytest.mark.asyncio
async def test_emergency_fantasypros_refresh_requires_csrf_and_uses_reserve(monkeypatch, tmp_path):
    configured = settings.model_copy(
        update={
            "fantasypros_api_key": SecretStr("test-key"),
            "reports_dir": tmp_path,
            "fantasypros_emergency_refresh_cooldown_minutes": 15,
        }
    )
    (tmp_path / "fantasypros-context.json").write_text(
        canonical_json(
            {
                "generated_at": "2026-09-01T00:00:00+00:00",
                "status": "active",
                "target_player_count": 20,
                "target_player_coverage": 0.8,
            }
        ),
        encoding="utf-8",
    )
    called = {}

    async def fake_sync(*_args, **kwargs):
        called.update(kwargs)
        return {"status": "active", "requests_this_sync": 4}

    monkeypatch.setattr("app.main.settings", configured)
    monkeypatch.setattr("app.main.sync_fantasypros_context", fake_sync)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        rejected = await client.post("/api/fantasypros-refresh")
        assert rejected.status_code == 403
        token = (await client.get("/api/csrf")).json()["token"]
        refreshed = await client.post("/api/fantasypros-refresh", headers={"X-CSRF-Token": token})
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_status"] == "active"
    assert refreshed.json()["requests_this_refresh"] == 4
    assert called == {"force": True, "allow_reserve": True}


@pytest.mark.asyncio
async def test_owner_can_cancel_waiting_command_but_not_an_active_lease():
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_session():
        async with session_factory() as session:
            yield session

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        waiting = ActionItem(
            action_type="SET_LINEUP",
            title="Set Lineup",
            status="ready",
            exact_action={"from_player_id": "a", "to_player_id": "b"},
            primary_reason="Improve the flex slot",
            confidence=0.91,
            dedupe_key="cancel-test-waiting",
        )
        session.add(waiting)
        await session.flush()
        waiting_command = ExecutionCommand(
            action_id=waiting.id,
            action_type="SET_LINEUP",
            league_id="league",
            roster_id=1,
            parameters=waiting.exact_action,
            expected_state_hash="state",
            decision_policy_version="v1",
            evidence_hashes=["evidence"],
            idempotency_key="cancel-test-waiting-command",
            not_before=now + timedelta(minutes=5),
            expires_at=now + timedelta(hours=1),
            verification_plan={},
            status="ready",
        )
        active = ActionItem(
            action_type="SET_LINEUP",
            title="Set Lineup",
            status="leased",
            exact_action={"from_player_id": "c", "to_player_id": "d"},
            primary_reason="Improve the flex slot",
            confidence=0.92,
            dedupe_key="cancel-test-active",
        )
        session.add(active)
        await session.flush()
        active_command = ExecutionCommand(
            action_id=active.id,
            action_type="SET_LINEUP",
            league_id="league",
            roster_id=1,
            parameters=active.exact_action,
            expected_state_hash="state-2",
            decision_policy_version="v1",
            evidence_hashes=["evidence-2"],
            idempotency_key="cancel-test-active-command",
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            verification_plan={},
            status="leased",
            lease_owner="browser-agent",
            lease_until=now + timedelta(seconds=30),
        )
        session.add_all([waiting_command, active_command])
        await session.commit()
        waiting_id = waiting.id
        active_id = active.id

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        listed = (await client.get("/api/actions")).json()
        waiting_json = next(row for row in listed if row["id"] == waiting_id)
        active_json = next(row for row in listed if row["id"] == active_id)
        assert waiting_json["can_cancel"] is True
        assert waiting_json["execution"]["status"] == "ready"
        assert active_json["can_cancel"] is False

        token = (await client.get("/api/csrf")).json()["token"]
        cancelled = await client.post(
            f"/api/actions/{waiting_id}/cancel",
            headers={"X-CSRF-Token": token},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["action"]["status"] == "cancelled"
        assert cancelled.json()["action"]["execution"]["status"] == "cancelled"
        assert cancelled.json()["action"]["can_cancel"] is False

        refused = await client.post(
            f"/api/actions/{active_id}/cancel",
            headers={"X-CSRF-Token": token},
        )
        assert refused.status_code == 409
        assert "already executing" in refused.json()["detail"]

    async with session_factory() as session:
        waiting_after = await session.get(ActionItem, waiting_id)
        command_after = await session.scalar(
            select(ExecutionCommand).where(ExecutionCommand.action_id == waiting_id)
        )
        audit = await session.scalar(
            select(ActionAudit).where(
                ActionAudit.action_id == waiting_id,
                ActionAudit.to_status == "cancelled",
            )
        )
        assert waiting_after is not None and waiting_after.status == "cancelled"
        assert command_after is not None and command_after.status == "cancelled"
        assert command_after.lease_owner is None
        assert audit is not None and audit.actor == "owner"
        overlaid = await _overlay_current_execution_states(
            {
                "manager_state": "ready",
                "lineup_execution": {
                    "state": "scheduled",
                    "queued": [
                        {
                            "action_id": waiting_id,
                            "command_id": command_after.id,
                            "status": "ready",
                        }
                    ],
                },
            },
            session,
        )
        assert overlaid["lineup_execution"]["state"] == "cancelled"
        assert overlaid["lineup_execution"]["queued"][0]["status"] == "cancelled"
        assert "Cancelled by the owner" in overlaid["lineup_execution"]["reason"]

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_final_write_preflight_is_authenticated_and_fail_closed(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_session():
        async with session_factory() as session:
            yield session

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        action = ActionItem(
            action_type="SET_LINEUP",
            title="Set Lineup",
            status="preflight",
            exact_action={},
            primary_reason="Test the final evidence boundary",
            confidence=0.9,
            dedupe_key="final-preflight-test",
        )
        session.add(action)
        await session.flush()
        command = ExecutionCommand(
            action_id=action.id,
            action_type="SET_LINEUP",
            league_id=settings.sleeper_league_id,
            roster_id=settings.sleeper_roster_id,
            parameters={},
            expected_state_hash="a" * 64,
            decision_policy_version="v1",
            evidence_hashes=["evidence"],
            idempotency_key="final-preflight-test-command",
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            verification_plan={},
            status="preflight",
            lease_owner="test-agent",
            lease_until=now + timedelta(seconds=30),
        )
        session.add(command)
        await session.commit()
        command_id = command.id

    def fake_context(*_args, **_kwargs):
        return {"state_hash": "base", "data_quality": {"evidence_gate": {}}}

    async def blocked_gate(context, _session, _settings, **_kwargs):
        gate = {
            "status": "blocked",
            "allows_lineup_execution": False,
            "blockers": ["Roster dataset became partial"],
        }
        context["data_quality"]["evidence_gate"] = gate
        context["state_hash"] = "b" * 64
        return gate

    monkeypatch.setattr("app.main.build_manager_context", fake_context)
    monkeypatch.setattr("app.main.apply_operational_evidence", blocked_gate)
    monkeypatch.setattr(
        "app.main.read_report", lambda *_args, **_kwargs: {"manager_state": "ready"}
    )
    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        unauthenticated = await client.post(
            f"/internal/agent/commands/{command_id}/preflight",
            json={"agent_id": "test-agent"},
        )
        assert unauthenticated.status_code == 401
        result = await client.post(
            f"/internal/agent/commands/{command_id}/preflight",
            headers={"Authorization": f"Bearer {settings.shim_shared_secret}"},
            json={"agent_id": "test-agent"},
        )
        assert result.status_code == 200
        assert result.json()["allowed"] is False
        assert result.json()["gate"]["blockers"] == ["Roster dataset became partial"]

    app.dependency_overrides.clear()
    await engine.dispose()


def test_final_write_boundary_rejects_a_blocked_expert_plan() -> None:
    assert _manager_execution_blocker({"manager_state": "ready"}) is None
    assert _manager_execution_blocker(
        {
            "manager_state": "blocked",
            "blockers": ["The local Codex worker exceeded the decision timeout"],
        }
    ) == (
        "Current expert plan is not executable: "
        "The local Codex worker exceeded the decision timeout"
    )


def test_acquisition_write_boundary_rechecks_exact_roster_and_ui_action_type() -> None:
    command = ExecutionCommand(
        action_id="action",
        action_type="WAIVER_CLAIM",
        league_id=settings.sleeper_league_id,
        roster_id=settings.sleeper_roster_id,
        parameters={
            "add_player_id": "new",
            "add_player_name": "N Player",
            "add_player_position": "WR",
            "add_player_team": "MIA",
            "drop_player_id": "old",
            "drop_player_name": "O Player",
            "expected_roster_before": ["keep", "old"],
        },
        expected_state_hash="a" * 64,
        decision_policy_version="v1",
        evidence_hashes=[],
        idempotency_key="acquisition-write-boundary",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        verification_plan={},
    )
    context = {
        "our_team": {
            "all_players": [{"player_id": "keep"}, {"player_id": "old"}]
        },
        "free_agent_candidates": [
            {"player_id": "new", "sleeper_acquisition_type": "waiver"}
        ],
    }
    configured = settings.model_copy(update={"waiver_semantics_confirmed": True})

    assert _acquisition_state_blockers(command, context, configured) == []

    context["free_agent_candidates"][0]["sleeper_acquisition_type"] = None
    staged_ui = {
        "row_identity_confirmed": True,
        "dialog_identity_confirmed": True,
        "exact_drop_confirmed": True,
        "submit_control_enabled": True,
        "acquisition_type": "waiver",
        "add_player_name": "N Player",
        "add_player_position": "WR",
        "add_player_team": "MIA",
        "drop_player_name": "O Player",
    }
    assert _acquisition_state_blockers(command, context, configured, staged_ui) == []
    staged_ui["add_player_team"] = "NE"
    assert "Sleeper no longer exposes the exact target as a waiver" in (
        _acquisition_state_blockers(command, context, configured, staged_ui)
    )

    context["our_team"]["all_players"] = [
        {"player_id": "keep"},
        {"player_id": "changed"},
    ]
    context["free_agent_candidates"][0]["sleeper_acquisition_type"] = "free_agent"
    blockers = _acquisition_state_blockers(command, context, configured)
    assert "The public roster changed after the acquisition decision" in blockers
    assert "Sleeper no longer exposes the exact target as a waiver" in blockers
    assert "The exact drop player is no longer on the roster" in blockers


@pytest.mark.asyncio
async def test_controlled_waiver_retry_requires_a_prewrite_qualification_failure() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_session():
        async with session_factory() as session:
            yield session

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        action = ActionItem(
            action_type="WAIVER_CLAIM",
            title="Waiver Claim",
            status="blocked",
            exact_action={},
            primary_reason="Controlled qualification",
            confidence=0.9,
            dedupe_key="controlled-waiver-retry",
        )
        session.add(action)
        await session.flush()
        command = ExecutionCommand(
            action_id=action.id,
            action_type="WAIVER_CLAIM",
            league_id=settings.sleeper_league_id,
            roster_id=settings.sleeper_roster_id,
            parameters={},
            expected_state_hash="a" * 64,
            decision_policy_version="v1",
            evidence_hashes=[],
            idempotency_key="controlled-waiver-retry-command",
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            verification_plan={},
            status="blocked",
            lease_owner="test-agent",
            attempt_count=1,
            completed_at=now,
            result={
                "message": "Final gate blocked",
                "evidence": {
                    "qualification_run": True,
                    "write_attempted": False,
                },
            },
        )
        session.add(command)
        await session.commit()
        command_id = command.id
        action_id = action.id

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            f"/internal/agent/commands/{command_id}/qualification-retry",
            headers={"Authorization": f"Bearer {settings.shim_shared_secret}"},
            json={"agent_id": "test-agent"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}

    async with session_factory() as session:
        retried = await session.get(ExecutionCommand, command_id)
        retried_action = await session.get(ActionItem, action_id)
        assert retried is not None and retried.status == "ready"
        assert retried.lease_owner is None
        assert retried.completed_at is None
        assert retried_action is not None and retried_action.status == "ready"

    app.dependency_overrides.clear()
    await engine.dispose()


def test_verified_public_waiver_transaction_records_live_qualification(
    monkeypatch, tmp_path
) -> None:
    configured = settings.model_copy(update={"reports_dir": tmp_path})
    monkeypatch.setattr("app.main.settings", configured)
    command = ExecutionCommand(
        id="command-1",
        action_id="action",
        action_type="WAIVER_CLAIM",
        league_id=configured.sleeper_league_id,
        roster_id=configured.sleeper_roster_id,
        parameters={
            "add_player_id": "new",
            "drop_player_id": "old",
            "transaction_week": 2,
        },
        expected_state_hash="a" * 64,
        decision_policy_version="v1",
        evidence_hashes=[],
        idempotency_key="qualification-record",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        verification_plan={},
    )
    _record_waiver_submission_qualification(
        command,
        {
            "ui_contract_version": configured.sleeper_ui_contract_version,
            "write_attempted": True,
            "public_api_verified": True,
            "verification": {
                "verified": True,
                "transaction_id": "tx-1",
                "transaction_status": "pending",
            },
        },
    )

    report = json.loads(
        (tmp_path / "waiver-submission-qualification.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "qualified"
    assert report["public_transaction_verified"] is True
    assert report["transaction_id"] == "tx-1"
    assert report["add_player_id"] == "new"


def test_authenticated_pending_waiver_records_live_qualification(monkeypatch, tmp_path) -> None:
    configured = settings.model_copy(update={"reports_dir": tmp_path})
    monkeypatch.setattr("app.main.settings", configured)
    command = ExecutionCommand(
        id="command-ui",
        action_id="action",
        action_type="WAIVER_CLAIM",
        league_id=configured.sleeper_league_id,
        roster_id=configured.sleeper_roster_id,
        parameters={
            "add_player_id": "new",
            "drop_player_id": "old",
            "transaction_week": 2,
        },
        expected_state_hash="a" * 64,
        decision_policy_version="v1",
        evidence_hashes=[],
        idempotency_key="qualification-ui-record",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        verification_plan={},
    )
    _record_waiver_submission_qualification(
        command,
        {
            "ui_contract_version": configured.sleeper_ui_contract_version,
            "write_attempted": True,
            "public_api_verified": False,
            "authenticated_ui_verified": True,
            "verification": {
                "verified": True,
                "verification_channel": "authenticated_pending_ui",
                "transaction_status": "pending",
            },
        },
    )
    report = json.loads(
        (tmp_path / "waiver-submission-qualification.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "qualified"
    assert report["authenticated_pending_ui_verified"] is True
    assert report["public_transaction_verified"] is False
    assert report["transaction_id"] is None
