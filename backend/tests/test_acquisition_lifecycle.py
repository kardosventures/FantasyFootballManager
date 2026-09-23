from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.acquisition_lifecycle import reconcile_acquisition_lifecycle
from app.config import Settings
from app.database import Base
from app.models import ActionItem, ExecutionCommand, NotificationDelivery


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_pending_waiver_reconciles_to_public_completion_and_notifies(
    session_factory, monkeypatch, tmp_path
) -> None:
    now = datetime(2026, 9, 14, 20, tzinfo=timezone.utc)
    settings = Settings(
        _env_file=None,
        reports_dir=tmp_path,
        sleeper_league_id="league",
        sleeper_owner_user_id="owner",
        sleeper_roster_id=8,
    )

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr(
        "app.acquisition_lifecycle.session_scope", test_session_scope
    )
    async with session_factory() as session, session.begin():
        action = ActionItem(
            action_type="WAIVER_CLAIM",
            title="Waiver Claim",
            status="verified",
            primary_reason="Season-long upside",
            confidence=0.9,
            dedupe_key="claim",
        )
        session.add(action)
        await session.flush()
        command = ExecutionCommand(
            action_id=action.id,
            action_type="WAIVER_CLAIM",
            league_id="league",
            roster_id=8,
            parameters={
                "add_player_id": "new",
                "add_player_search_name": "New Player",
                "drop_player_id": "old",
                "drop_player_search_name": "Old Player",
                "claim_priority": 1,
            },
            expected_state_hash="a" * 64,
            decision_policy_version="v1",
            evidence_hashes=[],
            idempotency_key="command",
            expires_at=now + timedelta(days=2),
            status="verified",
            created_at=now,
        )
        session.add(command)

    _write(
        tmp_path / "in-season-context.json",
        {"transactions": []},
    )
    _write(
        tmp_path / "in-season-plan.json",
        {
            "season_horizon": {
                "pending_waiver_claims": [
                    {"add_player_ids": ["new"], "drop_player_ids": ["old"]}
                ]
            }
        },
    )
    pending = await reconcile_acquisition_lifecycle(settings, now=now)
    assert pending["status"] == "pending"
    assert pending["claims"][0]["outcome_status"] == "pending"

    _write(tmp_path / "in-season-context.json", {"transactions": []})
    _write(tmp_path / "in-season-plan.json", {"season_horizon": {}})

    awaiting = await reconcile_acquisition_lifecycle(
        settings, now=now - timedelta(seconds=30)
    )
    assert awaiting["status"] == "pending"
    assert awaiting["active_count"] == 1
    assert awaiting["requires_manager_refresh"] is False
    assert awaiting["claims"][0]["outcome_status"] == "awaiting_reconciliation"

    _write(
        tmp_path / "in-season-context.json",
        {
            "transactions": [
                {
                    "transaction_id": "tx-1",
                    "type": "waiver",
                    "status": "complete",
                    "creator": "owner",
                    "created": int((now + timedelta(minutes=1)).timestamp() * 1000),
                    "roster_ids": [8],
                    "adds": {"new": 8},
                    "drops": {"old": 8},
                    "metadata": {"notes": "Processed successfully"},
                }
            ]
        },
    )
    _write(tmp_path / "in-season-plan.json", {"season_horizon": {}})
    completed = await reconcile_acquisition_lifecycle(
        settings, now=now + timedelta(minutes=2)
    )
    assert completed["status"] == "settled"
    assert completed["requires_manager_refresh"] is True
    assert completed["claims"][0]["outcome_status"] == "completed"
    assert completed["claims"][0]["transaction_id"] == "tx-1"

    again = await reconcile_acquisition_lifecycle(
        settings, now=now + timedelta(minutes=3)
    )
    assert again["requires_manager_refresh"] is False
    async with session_factory() as session:
        notices = list(
            await session.scalars(select(NotificationDelivery))
        )
    assert len(notices) == 1
    assert notices[0].channel == "dashboard"
    assert notices[0].status == "delivered"


@pytest.mark.asyncio
async def test_verified_free_agent_move_forces_a_fresh_manager_plan(
    session_factory, monkeypatch, tmp_path
) -> None:
    now = datetime(2026, 9, 15, 18, tzinfo=timezone.utc)
    settings = Settings(
        _env_file=None,
        reports_dir=tmp_path,
        sleeper_league_id="league",
        sleeper_owner_user_id="owner",
        sleeper_roster_id=8,
    )

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.acquisition_lifecycle.session_scope", test_session_scope)
    async with session_factory() as session, session.begin():
        action = ActionItem(
            action_type="ADD_FREE_AGENT",
            title="Add Free Agent",
            status="verified",
            primary_reason="Whole-roster upgrade",
            confidence=0.9,
            dedupe_key="free-agent-step-one",
        )
        session.add(action)
        await session.flush()
        session.add(
            ExecutionCommand(
                action_id=action.id,
                action_type="ADD_FREE_AGENT",
                league_id="league",
                roster_id=8,
                parameters={
                    "add_player_id": "new",
                    "add_player_search_name": "New Player",
                    "drop_player_id": "old",
                    "drop_player_search_name": "Old Player",
                    "claim_priority": 1,
                    "sequence_mode": "verify_reconcile_replan",
                },
                expected_state_hash="a" * 64,
                decision_policy_version="v1",
                evidence_hashes=[],
                idempotency_key="free-agent-command",
                expires_at=now + timedelta(days=1),
                status="verified",
                created_at=now - timedelta(minutes=2),
                result={
                    "evidence": {
                        "write_attempted": True,
                        "ui_contract_version": settings.sleeper_ui_contract_version,
                    }
                },
            )
        )
    _write(
        tmp_path / "in-season-context.json",
        {
            "transactions": [
                {
                    "transaction_id": "tx-free-agent",
                    "type": "free_agent",
                    "status": "complete",
                    "creator": "owner",
                    "created": int((now - timedelta(minutes=1)).timestamp() * 1000),
                    "roster_ids": [8],
                    "adds": {"new": 8},
                    "drops": {"old": 8},
                }
            ]
        },
    )
    report = await reconcile_acquisition_lifecycle(settings, now=now)

    assert report["requires_manager_refresh"] is True
    assert report["claims"][0]["action_type"] == "ADD_FREE_AGENT"
    assert report["claims"][0]["outcome_status"] == "completed"
    assert report["claims"][0]["sequence_mode"] == "verify_reconcile_replan"
    qualification = json.loads(
        (tmp_path / "free-agent-submission-qualification.json").read_text(
            encoding="utf-8"
        )
    )
    assert qualification["transaction_id"] == "tx-free-agent"
    assert qualification["transaction_status"] == "complete"
    repeated = await reconcile_acquisition_lifecycle(
        settings, now=now + timedelta(minutes=1)
    )
    assert repeated["requires_manager_refresh"] is False


@pytest.mark.asyncio
async def test_failed_waiver_requests_manager_refresh(
    session_factory, monkeypatch, tmp_path
) -> None:
    now = datetime(2026, 9, 16, 7, tzinfo=timezone.utc)
    settings = Settings(
        _env_file=None,
        reports_dir=tmp_path,
        sleeper_league_id="league",
        sleeper_owner_user_id="owner",
        sleeper_roster_id=8,
    )

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr(
        "app.acquisition_lifecycle.session_scope", test_session_scope
    )
    async with session_factory() as session, session.begin():
        action = ActionItem(
            action_type="WAIVER_CLAIM",
            title="Waiver Claim",
            status="verified",
            primary_reason="Upside",
            confidence=0.9,
            dedupe_key="failed-claim",
        )
        session.add(action)
        await session.flush()
        session.add(
            ExecutionCommand(
                action_id=action.id,
                action_type="WAIVER_CLAIM",
                league_id="league",
                roster_id=8,
                parameters={
                    "add_player_id": "new",
                    "add_player_search_name": "New Player",
                    "drop_player_id": "old",
                    "drop_player_search_name": "Old Player",
                    "claim_priority": 1,
                },
                expected_state_hash="a" * 64,
                decision_policy_version="v1",
                evidence_hashes=[],
                idempotency_key="failed-command",
                expires_at=now + timedelta(days=1),
                status="verified",
                created_at=now - timedelta(minutes=5),
            )
        )
    _write(
        tmp_path / "in-season-context.json",
        {
            "transactions": [
                {
                    "transaction_id": "tx-failed",
                    "type": "waiver",
                    "status": "failed",
                    "creator": "owner",
                    "created": int(now.timestamp() * 1000),
                    "roster_ids": [8],
                    "adds": {"new": 8},
                    "drops": None,
                    "metadata": {"notes": "Player claimed by another owner"},
                }
            ]
        },
    )
    _write(tmp_path / "in-season-plan.json", {"season_horizon": {}})

    report = await reconcile_acquisition_lifecycle(settings, now=now)

    assert report["status"] == "settled_with_failures"
    assert report["failed_count"] == 1
    assert report["requires_manager_refresh"] is True
    async with session_factory() as session:
        notices = list(
            await session.scalars(
                select(NotificationDelivery).order_by(NotificationDelivery.channel)
            )
        )
    assert [notice.channel for notice in notices] == ["dashboard", "slack"]
    slack = notices[1]
    assert slack.status == "blocked"
    assert slack.subject == "SLEEPER AI AGENT: Acquisition failed: New Player"
    assert slack.recipient == "Sleeper AI alerts"
    assert "Issue type: ACQUISITION FAILED" in slack.payload["body"]
    assert "Command ID:" in slack.payload["body"]
    assert "Player claimed by another owner" in slack.payload["body"]
