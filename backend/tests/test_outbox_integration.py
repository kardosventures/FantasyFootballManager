import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.draft_room import _existing_autopick_for_board
from app.execution import propose_execution
from app.in_season_execution import synchronize_acquisition_actions, synchronize_lineup_actions
from app.models import ActionItem, ExecutionCommand
from app.queue import enqueue_event, lease_event
from app.repositories import lease_execution_command
from app.utils import content_hash


def _qualify_action(tmp_path, settings: Settings, action_type: str) -> None:
    filenames = {
        "ADD_FREE_AGENT": "free-agent-submission-qualification.json",
    }
    (tmp_path / filenames[action_type]).write_text(
        json.dumps(
            {
                "status": "qualified",
                "ui_contract_version": settings.sleeper_ui_contract_version,
                "league_id": settings.sleeper_league_id,
                "roster_id": settings.sleeper_roster_id,
                "action_type": action_type,
                "write_attempted": True,
                "public_api_verified": True,
                "verification_channel": "public_api",
                "transaction_id": "qualification-transaction",
                "transaction_status": "complete",
            }
        ),
        encoding="utf-8",
    )


def _qualify_waiver(tmp_path, settings: Settings) -> None:
    (tmp_path / "waiver-ui-qualification.json").write_text(
        json.dumps(
            {
                "status": "selector_qualified",
                "ui_contract_version": settings.sleeper_ui_contract_version,
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
        ),
        encoding="utf-8",
    )
    (tmp_path / "waiver-submission-qualification.json").write_text(
        json.dumps(
            {
                "status": "qualified",
                "ui_contract_version": settings.sleeper_ui_contract_version,
                "league_id": settings.sleeper_league_id,
                "roster_id": settings.sleeper_roster_id,
                "action_type": "WAIVER_CLAIM",
                "public_transaction_verified": True,
                "transaction_id": "qualification-waiver-transaction",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_is_idempotent_and_retains_verification_plan(session_factory):
    not_before = datetime.now(timezone.utc) + timedelta(seconds=60)
    async with session_factory() as session, session.begin():
        kwargs = {
            "action_type": "DRAFT_PLAYER",
            "league_id": "l1",
            "roster_id": 8,
            "parameters": {
                "draft_id": "d1",
                "player_id": "p1",
                "player_name": "Player One",
                "player_position": "WR",
                "expected_pick_no": 1,
                "owner_slot": 1,
            },
            "expected_state_hash": "state1",
            "evidence_hashes": ["e1"],
            "reason": "on clock",
            "confidence": 0.9,
            "approval_required": False,
            "not_before": not_before,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=2),
            "verification_plan": {"endpoint": "draft/d1/picks"},
            "dedupe_key": "draft:d1:pick:1",
        }
        first_action, first_command = await propose_execution(session, **kwargs)
        await session.flush()
        second_action, second_command = await propose_execution(session, **kwargs)
        assert second_action.id == first_action.id
        assert second_command.id == first_command.id
        assert first_command.expected_state_hash == "state1"
        assert first_command.not_before == not_before
        assert first_action.verification_plan == {"endpoint": "draft/d1/picks"}


@pytest.mark.asyncio
async def test_existing_autopick_is_found_only_for_the_exact_live_board(
    session_factory, monkeypatch
):
    picks = [{"pick_no": 1, "draft_slot": 1, "player_id": "already-picked"}]
    settings = Settings(
        _env_file=None,
        sleeper_league_id="l1",
        sleeper_roster_id=8,
    )

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.draft_room.session_scope", test_session_scope)
    async with session_factory() as session, session.begin():
        await propose_execution(
            session,
            action_type="DRAFT_PLAYER",
            league_id="l1",
            roster_id=8,
            parameters={
                "draft_id": "d1",
                "player_id": "p1",
                "player_name": "Player One",
                "player_position": "WR",
                "expected_pick_no": 2,
                "owner_slot": 2,
                "decision_source": "expert_model",
            },
            expected_state_hash=content_hash(picks),
            evidence_hashes=["e1"],
            reason="whole-roster choice",
            confidence=0.8,
            approval_required=False,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            verification_plan={"endpoint": "draft/d1/picks"},
            dedupe_key="draft:d1:pick:2",
        )

    existing = await _existing_autopick_for_board(
        settings,
        draft={"draft_id": "d1"},
        picks=picks,
        owner_slot=2,
    )
    wrong_draft = await _existing_autopick_for_board(
        settings,
        draft={"draft_id": "another-draft"},
        picks=picks,
        owner_slot=2,
    )
    changed_board = await _existing_autopick_for_board(
        settings,
        draft={"draft_id": "d1"},
        picks=[*picks, {"pick_no": 2, "draft_slot": 2, "player_id": "p1"}],
        owner_slot=2,
    )

    assert existing is not None
    assert existing["parameters"]["player_id"] == "p1"
    assert existing["command_status"] == "ready"
    assert wrong_draft is None
    assert changed_board is None


@pytest.mark.asyncio
async def test_outbox_reapplies_drop_gate_even_if_caller_claims_auto_eligible(session_factory):
    async with session_factory() as session, session.begin():
        action, command = await propose_execution(
            session,
            action_type="WAIVER_CLAIM",
            league_id="l1",
            roster_id=8,
            parameters={
                "add_player_id": "new",
                "add_player_name": "New Player",
                "add_player_search_name": "New Player",
                "add_player_position": "WR",
                "add_player_team": "NE",
                "drop_player_id": "top30",
                "drop_player_name": "Protected Player",
                "drop_player_search_name": "Protected Player",
                "drop_player_position": "WR",
                "drop_player_team": "SEA",
                "claim_priority": 1,
                "faab_percent": 0,
                "contingency": "Role upgrade remains confirmed",
                "observed_acquisition_type": "waiver",
                "transaction_week": 1,
                "expected_roster_before": ["top30"],
                "expected_roster_after": ["new"],
                "drop_evidence": {
                    "positions": ["WR"],
                    "ros_ranks": {"WR": 30},
                    "ranking_fresh": True,
                    "protection_tier": "CHURN_ELIGIBLE",
                },
            },
            expected_state_hash="state1",
            evidence_hashes=["e1"],
            reason="waiver upgrade",
            confidence=0.9,
            approval_required=False,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            verification_plan={"endpoint": "league/l1/transactions/1"},
            dedupe_key="waiver:new:drop:top30",
        )
        assert action.status == "approval_required"
        assert action.approval_required
        assert command is None


@pytest.mark.asyncio
async def test_exact_waiver_claim_enters_outbox_with_transaction_verifier(
    session_factory, monkeypatch, tmp_path
):
    now = datetime.now(timezone.utc)
    settings = Settings(
        _env_file=None,
        sleeper_league_id="l1",
        sleeper_roster_id=8,
        waiver_semantics_confirmed=True,
        in_season_acquisition_actions_enabled=True,
        execution_mode="browser",
        browser_live_actions="WAIVER_CLAIM",
        reports_dir=tmp_path,
    )
    _qualify_waiver(tmp_path, settings)

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.in_season_execution.session_scope", test_session_scope)
    parameters = {
        "add_player_id": "new",
        "add_player_name": "N Player",
        "add_player_search_name": "New Player",
        "add_player_position": "WR",
        "add_player_team": "MIA",
        "drop_player_id": "old",
        "drop_player_name": "O Player",
        "drop_player_search_name": "Old Player",
        "drop_player_position": "RB",
        "drop_player_team": "TEN",
        "claim_priority": 1,
        "faab_percent": 0,
        "contingency": "Roles remain unchanged",
        "observed_acquisition_type": "waiver",
        "transaction_week": 2,
        "expected_roster_before": ["keep", "old"],
        "expected_roster_after": ["keep", "new"],
        "drop_evidence": {
            "positions": ["RB"],
            "ros_ranks": {"RB": 51},
            "ranking_fresh": True,
            "protection_tier": "CHURN_ELIGIBLE",
        },
    }
    context = {
        "season": "2026",
        "week": 2,
        "state_hash": "a" * 64,
        "season_horizon": {
            "next_waiver_processing_at": (now + timedelta(days=1)).isoformat()
        },
        "our_team": {
            "all_players": [
                {"player_id": "keep", "locked": False},
                {"player_id": "old", "locked": True},
            ]
        },
        "data_quality": {
            "sleeper_ui_free_agent_projection_join": {"acquisition_typed": 32}
        },
    }
    decision = {"waiver_status": "ready", "confidence": 0.91}
    actions = [
        {
            "action_type": "WAIVER_CLAIM",
            "parameters": parameters,
            "expected_state_hash": "b" * 64,
            "reason": "Season-long roster upgrade",
        }
    ]

    result = await synchronize_acquisition_actions(
        settings, context, decision, actions, now=now
    )

    assert result["state"] == "scheduled"
    async with session_factory() as session:
        command = await session.scalar(select(ExecutionCommand))
        assert command is not None
        assert command.action_type == "WAIVER_CLAIM"
        assert command.verification_plan["expected_statuses"] == ["pending", "complete"]
        assert command.verification_plan["endpoint"] == "league/l1/transactions/2"

    retired = await synchronize_acquisition_actions(
        settings, context, {"waiver_status": "no_action", "confidence": 0.91}, [], now=now
    )
    assert retired == {"state": "no_action", "queued": [], "preview_count": 0}
    async with session_factory() as session:
        command = await session.scalar(select(ExecutionCommand))
        action = await session.scalar(select(ActionItem))
        assert command is not None and command.status == "blocked"
        assert action is not None and action.status == "superseded"


@pytest.mark.asyncio
async def test_ordered_fallback_claims_share_exact_group_and_release_in_order(
    session_factory, monkeypatch, tmp_path
):
    now = datetime.now(timezone.utc)
    settings = Settings(
        _env_file=None,
        sleeper_league_id="l1",
        sleeper_roster_id=8,
        waiver_semantics_confirmed=True,
        in_season_acquisition_actions_enabled=True,
        in_season_multi_claim_actions_enabled=True,
        execution_mode="browser",
        browser_live_actions="WAIVER_CLAIM",
        reports_dir=tmp_path,
    )
    (tmp_path / "waiver-multi-claim-qualification.json").write_text(
        json.dumps(
            {
                "status": "qualified",
                "ui_contract_version": settings.sleeper_ui_contract_version,
                "league_id": "l1",
                "roster_id": 8,
                "verified_claim_count": 2,
            }
        ),
        encoding="utf-8",
    )
    _qualify_waiver(tmp_path, settings)

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.in_season_execution.session_scope", test_session_scope)

    def parameters(add_id: str, priority: int) -> dict:
        return {
            "add_player_id": add_id,
            "add_player_name": f"Add {add_id}",
            "add_player_search_name": f"Add {add_id}",
            "add_player_position": "WR",
            "add_player_team": "MIA",
            "drop_player_id": "old",
            "drop_player_name": "Old Player",
            "drop_player_search_name": "Old Player",
            "drop_player_position": "RB",
            "drop_player_team": "TEN",
            "claim_priority": priority,
            "faab_percent": 0,
            "contingency": "Role remains confirmed",
            "observed_acquisition_type": "waiver",
            "transaction_week": 2,
            "expected_roster_before": ["keep", "old"],
            "expected_roster_after": ["keep", add_id],
            "drop_evidence": {
                "positions": ["RB"],
                "ros_ranks": {"RB": 51},
                "ranking_fresh": True,
                "protection_tier": "CHURN_ELIGIBLE",
            },
        }

    actions = [
        {
            "sequence": priority,
            "action_type": "WAIVER_CLAIM",
            "parameters": parameters(add_id, priority),
            "expected_state_hash": chr(96 + priority) * 64,
            "reason": f"Fallback priority {priority}",
        }
        for priority, add_id in enumerate(("first", "second"), start=1)
    ]
    context = {
        "season": "2026",
        "week": 2,
        "state_hash": "c" * 64,
        "season_horizon": {
            "next_waiver_processing_at": (now + timedelta(days=1)).isoformat()
        },
        "our_team": {"all_players": [{"player_id": "old", "locked": False}]},
        "data_quality": {
            "sleeper_ui_free_agent_projection_join": {"acquisition_typed": 32}
        },
    }

    result = await synchronize_acquisition_actions(
        settings,
        context,
        {"waiver_status": "ready", "confidence": 0.91},
        actions,
        now=now,
        not_before_offset_seconds=4,
    )

    assert result["state"] == "scheduled"
    assert [row["sequence"] for row in result["queued"]] == [1, 2]
    async with session_factory() as session:
        commands = list(
            await session.scalars(
                select(ExecutionCommand).order_by(ExecutionCommand.not_before)
            )
        )
    assert len(commands) == 2
    expected_group = [
        {"add_player_id": "first", "drop_player_id": "old", "claim_priority": 1},
        {"add_player_id": "second", "drop_player_id": "old", "claim_priority": 2},
    ]
    assert all(command.parameters["claim_group"] == expected_group for command in commands)
    assert (commands[1].not_before - commands[0].not_before).total_seconds() == 2
    assert (commands[0].not_before.replace(tzinfo=timezone.utc) - now).total_seconds() == 4


@pytest.mark.asyncio
async def test_free_agent_plan_releases_only_one_move_then_requires_fresh_replan(
    session_factory, monkeypatch, tmp_path
):
    now = datetime.now(timezone.utc)
    settings = Settings(
        _env_file=None,
        sleeper_league_id="l1",
        sleeper_roster_id=8,
        in_season_acquisition_actions_enabled=True,
        in_season_sequential_free_agent_actions_enabled=True,
        execution_mode="browser",
        browser_live_actions="ADD_FREE_AGENT",
        reports_dir=tmp_path,
    )
    _qualify_action(tmp_path, settings, "ADD_FREE_AGENT")

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.in_season_execution.session_scope", test_session_scope)

    def parameters(
        add_id: str,
        drop_id: str,
        priority: int,
        before: list[str],
        after: list[str],
    ) -> dict:
        return {
            "add_player_id": add_id,
            "add_player_name": f"Add {add_id}",
            "add_player_search_name": f"Add {add_id}",
            "add_player_position": "WR",
            "add_player_team": "MIA",
            "drop_player_id": drop_id,
            "drop_player_name": f"Drop {drop_id}",
            "drop_player_search_name": f"Drop {drop_id}",
            "drop_player_position": "RB",
            "drop_player_team": "TEN",
            "claim_priority": priority,
            "faab_percent": 0,
            "contingency": "The role remains confirmed",
            "observed_acquisition_type": "free_agent",
            "transaction_week": 2,
            "expected_roster_before": before,
            "expected_roster_after": after,
            "drop_evidence": {
                "positions": ["RB"],
                "ros_ranks": {"RB": 51},
                "ranking_fresh": True,
                "protection_tier": "CHURN_ELIGIBLE",
            },
        }

    actions = [
        {
            "sequence": 1,
            "action_type": "ADD_FREE_AGENT",
            "parameters": parameters(
                "new-one", "old-one", 1, ["old-one", "old-two"], ["old-two", "new-one"]
            ),
            "expected_state_hash": "d" * 64,
            "reason": "First exact roster upgrade",
        },
        {
            "sequence": 2,
            "action_type": "ADD_FREE_AGENT",
            "parameters": parameters(
                "new-two", "old-two", 2, ["old-two", "new-one"], ["new-one", "new-two"]
            ),
            "expected_state_hash": "e" * 64,
            "reason": "Second exact roster upgrade",
        },
    ]
    kickoff = (now + timedelta(days=2)).isoformat()
    context = {
        "season": "2026",
        "week": 2,
        "state_hash": "f" * 64,
        "season_horizon": {},
        "our_team": {
            "all_players": [
                {"player_id": "old-one", "locked": False, "kickoff": kickoff},
                {"player_id": "old-two", "locked": False, "kickoff": kickoff},
            ]
        },
        "free_agent_candidates": [
            {"player_id": "new-one", "kickoff": kickoff},
            {"player_id": "new-two", "kickoff": kickoff},
        ],
        "data_quality": {
            "sleeper_ui_free_agent_projection_join": {"acquisition_typed": 32}
        },
    }
    decision = {"waiver_status": "ready", "confidence": 0.91}

    first = await synchronize_acquisition_actions(
        settings, context, decision, actions, now=now
    )

    assert first["state"] == "scheduled_sequential"
    assert len(first["queued"]) == 1
    assert first["deferred_count"] == 1
    assert first["deferred"][0]["sequence"] == 2
    assert first["continuation_policy"] == "verify_reconcile_replan"
    repeated = await synchronize_acquisition_actions(
        settings, context, decision, actions, now=now
    )
    assert repeated["queued"][0]["command_id"] == first["queued"][0]["command_id"]

    async with session_factory() as session, session.begin():
        commands = list(await session.scalars(select(ExecutionCommand)))
        action_items = list(await session.scalars(select(ActionItem)))
        assert len(commands) == 1
        assert len(action_items) == 1
        command = commands[0]
        assert command.parameters["sequence_mode"] == "verify_reconcile_replan"
        assert command.parameters["sequence_plan_size"] == 2
        assert command.verification_plan["continuation_policy"] == "verify_reconcile_replan"
        command.status = "verified"
        action_items[0].status = "verified"

    refreshed_context = {
        **context,
        "state_hash": "1" * 64,
        "our_team": {
            "all_players": [
                {"player_id": "old-two", "locked": False, "kickoff": kickoff},
                {"player_id": "new-one", "locked": False, "kickoff": kickoff},
            ]
        },
    }
    second = await synchronize_acquisition_actions(
        settings, refreshed_context, decision, [actions[1]], now=now
    )
    assert second["state"] == "scheduled"
    assert len(second["queued"]) == 1
    async with session_factory() as session:
        commands = list(await session.scalars(select(ExecutionCommand)))
    assert len(commands) == 2
    assert sum(command.status == "ready" for command in commands) == 1


@pytest.mark.asyncio
async def test_multiple_free_agent_moves_require_the_sequential_release_switch() -> None:
    settings = Settings(
        _env_file=None,
        in_season_acquisition_actions_enabled=True,
        execution_mode="browser",
        browser_live_actions="ADD_FREE_AGENT",
    )
    context = {
        "our_team": {"all_players": [{"player_id": "old", "locked": False}]},
        "data_quality": {
            "sleeper_ui_free_agent_projection_join": {"acquisition_typed": 32}
        },
    }
    actions = [
        {
            "action_type": "ADD_FREE_AGENT",
            "parameters": {
                "add_player_id": "new-one",
                "drop_player_id": "old",
                "observed_acquisition_type": "free_agent",
                "expected_roster_before": ["old"],
                "expected_roster_after": ["new-one"],
            },
        },
        {
            "action_type": "ADD_FREE_AGENT",
            "parameters": {
                "add_player_id": "new-two",
                "drop_player_id": "new-one",
                "observed_acquisition_type": "free_agent",
                "expected_roster_before": ["new-one"],
                "expected_roster_after": ["new-two"],
            },
        },
    ]

    result = await synchronize_acquisition_actions(
        settings,
        context,
        {"waiver_status": "ready", "confidence": 0.91},
        actions,
    )

    assert result == {
        "state": "blocked",
        "queued": [],
        "reason": "Sequential free-agent execution is not enabled",
    }


@pytest.mark.asyncio
async def test_cancelled_acquisition_identity_survives_a_reworded_replan(
    session_factory, monkeypatch, tmp_path
) -> None:
    now = datetime.now(timezone.utc)
    settings = Settings(
        _env_file=None,
        sleeper_league_id="l1",
        sleeper_roster_id=8,
        in_season_acquisition_actions_enabled=True,
        execution_mode="browser",
        browser_live_actions="ADD_FREE_AGENT",
        reports_dir=tmp_path,
    )
    _qualify_action(tmp_path, settings, "ADD_FREE_AGENT")

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.in_season_execution.session_scope", test_session_scope)
    kickoff = (now + timedelta(days=2)).isoformat()
    parameters = {
        "add_player_id": "new",
        "add_player_name": "New Player",
        "add_player_search_name": "New Player",
        "add_player_position": "WR",
        "add_player_team": "MIA",
        "drop_player_id": "old",
        "drop_player_name": "Old Player",
        "drop_player_search_name": "Old Player",
        "drop_player_position": "RB",
        "drop_player_team": "TEN",
        "claim_priority": 1,
        "faab_percent": 0,
        "contingency": "The role remains confirmed",
        "observed_acquisition_type": "free_agent",
        "transaction_week": 2,
        "expected_roster_before": ["old"],
        "expected_roster_after": ["new"],
        "drop_evidence": {
            "positions": ["RB"],
            "ros_ranks": {"RB": 51},
            "ranking_fresh": True,
            "protection_tier": "CHURN_ELIGIBLE",
        },
    }
    context = {
        "season": "2026",
        "week": 2,
        "state_hash": "2" * 64,
        "season_horizon": {},
        "our_team": {
            "all_players": [{"player_id": "old", "locked": False, "kickoff": kickoff}]
        },
        "free_agent_candidates": [{"player_id": "new", "kickoff": kickoff}],
        "data_quality": {
            "sleeper_ui_free_agent_projection_join": {"acquisition_typed": 32}
        },
    }
    decision = {"waiver_status": "ready", "confidence": 0.91}
    initial = {
        "sequence": 1,
        "action_type": "ADD_FREE_AGENT",
        "parameters": parameters,
        "expected_state_hash": "3" * 64,
        "reason": "Initial wording",
    }
    scheduled = await synchronize_acquisition_actions(
        settings, context, decision, [initial], now=now
    )
    assert scheduled["state"] == "scheduled"

    async with session_factory() as session, session.begin():
        command = await session.scalar(select(ExecutionCommand))
        action = await session.scalar(select(ActionItem))
        assert command is not None and action is not None
        command.status = "cancelled"
        action.status = "cancelled"

    reworded = {
        **initial,
        "parameters": {
            **parameters,
            "contingency": "The refreshed evidence still supports the role",
        },
        "reason": "Materially different narrative wording",
    }
    blocked = await synchronize_acquisition_actions(
        settings, context, decision, [reworded], now=now
    )

    assert blocked["state"] == "cancelled"
    assert "add/drop identity" in blocked["reason"]
    async with session_factory() as session:
        commands = list(await session.scalars(select(ExecutionCommand)))
    assert len(commands) == 1


@pytest.mark.asyncio
async def test_failed_acquisition_identity_is_not_automatically_retried(
    session_factory, monkeypatch, tmp_path
) -> None:
    now = datetime.now(timezone.utc)
    settings = Settings(
        _env_file=None,
        sleeper_league_id="l1",
        sleeper_roster_id=8,
        in_season_acquisition_actions_enabled=True,
        execution_mode="browser",
        browser_live_actions="ADD_FREE_AGENT",
        reports_dir=tmp_path,
    )
    _qualify_action(tmp_path, settings, "ADD_FREE_AGENT")

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.in_season_execution.session_scope", test_session_scope)
    kickoff = (now + timedelta(days=2)).isoformat()
    parameters = {
        "add_player_id": "new",
        "add_player_name": "New Player",
        "add_player_search_name": "New Player",
        "add_player_position": "WR",
        "add_player_team": "MIA",
        "drop_player_id": "old",
        "drop_player_name": "Old Player",
        "drop_player_search_name": "Old Player",
        "drop_player_position": "RB",
        "drop_player_team": "TEN",
        "claim_priority": 1,
        "faab_percent": 0,
        "contingency": "The role remains confirmed",
        "transaction_week": 2,
        "observed_acquisition_type": "free_agent",
        "expected_roster_before": ["old"],
        "expected_roster_after": ["new"],
        "drop_evidence": {
            "positions": ["RB"],
            "ros_ranks": {"RB": 51},
            "ranking_fresh": True,
            "protection_tier": "CHURN_ELIGIBLE",
        },
    }
    context = {
        "season": "2026",
        "week": 2,
        "state_hash": "2" * 64,
        "season_horizon": {},
        "our_team": {
            "all_players": [{"player_id": "old", "locked": False, "kickoff": kickoff}]
        },
        "free_agent_candidates": [{"player_id": "new", "kickoff": kickoff}],
        "data_quality": {
            "sleeper_ui_free_agent_projection_join": {"acquisition_typed": 32}
        },
    }
    decision = {"waiver_status": "ready", "confidence": 0.91}
    action_preview = {
        "sequence": 1,
        "action_type": "ADD_FREE_AGENT",
        "parameters": parameters,
        "expected_state_hash": "3" * 64,
        "reason": "Grounded acquisition",
    }
    scheduled = await synchronize_acquisition_actions(
        settings, context, decision, [action_preview], now=now
    )
    assert scheduled["state"] == "scheduled"

    async with session_factory() as session, session.begin():
        command = await session.scalar(select(ExecutionCommand))
        action = await session.scalar(select(ActionItem))
        assert command is not None and action is not None
        command.status = "failed"
        action.status = "failed"

    blocked = await synchronize_acquisition_actions(
        settings,
        context,
        decision,
        [{**action_preview, "reason": "Reworded acquisition"}],
        now=now,
    )

    assert blocked["state"] == "blocked"
    assert "prior failed attempt" in blocked["reason"]
    async with session_factory() as session:
        commands = list(await session.scalars(select(ExecutionCommand)))
    assert len(commands) == 1


@pytest.mark.asyncio
async def test_execution_command_cannot_be_leased_before_scheduled_release(session_factory):
    async with session_factory() as session, session.begin():
        _, command = await propose_execution(
            session,
            action_type="DRAFT_PLAYER",
            league_id="l1",
            roster_id=8,
            parameters={
                "draft_id": "1",
                "player_id": "p1",
                "player_name": "Player One",
                "player_position": "WR",
                "expected_pick_no": 1,
                "owner_slot": 1,
            },
            expected_state_hash="state1",
            evidence_hashes=["e1"],
            reason="scheduled draft pick",
            confidence=0.9,
            approval_required=False,
            not_before=datetime.now(timezone.utc) + timedelta(seconds=60),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
            verification_plan={"endpoint": "draft/1/picks"},
            dedupe_key="scheduled-draft-pick",
        )
        assert command is not None

    async with session_factory() as session, session.begin():
        assert await lease_execution_command(session, agent_id="test-agent") is None


@pytest.mark.asyncio
async def test_constrained_lease_claims_only_the_exact_allowed_command(session_factory):
    now = datetime.now(timezone.utc)
    async with session_factory() as session, session.begin():
        _, unrelated = await propose_execution(
            session,
            action_type="DRAFT_PLAYER",
            league_id="l1",
            roster_id=8,
            parameters={
                "draft_id": "d1",
                "player_id": "p1",
                "player_name": "Player One",
                "player_position": "WR",
                "expected_pick_no": 1,
                "owner_slot": 1,
            },
            expected_state_hash="state1",
            evidence_hashes=["e1"],
            reason="unrelated ready command",
            confidence=0.9,
            approval_required=False,
            expires_at=now + timedelta(minutes=5),
            verification_plan={"endpoint": "draft/d1/picks"},
            dedupe_key="unrelated-draft-command",
        )
        _, waiver = await propose_execution(
            session,
            action_type="WAIVER_CLAIM",
            league_id="l1",
            roster_id=8,
            parameters={
                "add_player_id": "new",
                "add_player_name": "New Player",
                "add_player_search_name": "New Player",
                "add_player_position": "WR",
                "add_player_team": "MIA",
                "drop_player_id": "old",
                "drop_player_name": "Old Player",
                "drop_player_search_name": "Old Player",
                "drop_player_position": "RB",
                "drop_player_team": "TEN",
                "claim_priority": 1,
                "faab_percent": 0,
                "contingency": "Roles remain unchanged",
                "observed_acquisition_type": "waiver",
                "transaction_week": 1,
                "expected_roster_before": ["old"],
                "expected_roster_after": ["new"],
                "drop_evidence": {
                    "positions": ["RB"],
                    "ros_ranks": {"RB": 51},
                    "ranking_fresh": True,
                    "protection_tier": "CHURN_ELIGIBLE",
                },
            },
            expected_state_hash="state2",
            evidence_hashes=["e2"],
            reason="exact controlled waiver command",
            confidence=0.9,
            approval_required=False,
            expires_at=now + timedelta(minutes=5),
            verification_plan={"endpoint": "league/l1/transactions/1"},
            dedupe_key="exact-waiver-command",
        )
        assert unrelated is not None and waiver is not None
        await session.flush()
        waiver_id = waiver.id
        unrelated_id = unrelated.id

    async with session_factory() as session, session.begin():
        leased = await lease_execution_command(
            session,
            agent_id="controlled-qualifier",
            command_id=waiver_id,
            action_types=["WAIVER_CLAIM"],
        )
        assert leased is not None
        assert leased.id == waiver_id
        assert leased.action_type == "WAIVER_CLAIM"

    async with session_factory() as session:
        untouched = await session.get(ExecutionCommand, unrelated_id)
        assert untouched is not None
        assert untouched.status == "ready"
        assert untouched.lease_owner is None


@pytest.mark.asyncio
async def test_hold_current_is_no_action_and_retires_an_unleased_lineup_command(
    session_factory, monkeypatch
):
    settings = Settings(
        _env_file=None,
        sleeper_league_id="l1",
        sleeper_roster_id=8,
        in_season_lineup_actions_enabled=True,
        execution_mode="browser",
        browser_live_actions="SET_LINEUP",
    )

    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.in_season_execution.session_scope", test_session_scope)
    async with session_factory() as session, session.begin():
        action, command = await propose_execution(
            session,
            action_type="SET_LINEUP",
            league_id="l1",
            roster_id=8,
            parameters={
                "from_player_id": "a",
                "from_player_name": "Player A",
                "from_slot": "RB",
                "to_player_id": "b",
                "to_player_name": "Player B",
                "to_slot": "FLEX",
                "target_slot_index": 1,
                "expected_starters_before": ["qb", "a", "b"],
                "expected_starters_after": ["qb", "b", "a"],
            },
            expected_state_hash="state1",
            evidence_hashes=["e1"],
            reason="Previous plan",
            confidence=0.9,
            approval_required=False,
            not_before=datetime.now(timezone.utc) + timedelta(minutes=5),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            verification_plan={},
            dedupe_key="previous-lineup-plan",
        )
        assert command is not None
        action_id = action.id

    result = await synchronize_lineup_actions(settings, {}, {"lineup_status": "hold_current"}, [])

    assert result == {"state": "no_action", "queued": [], "preview_count": 0}
    async with session_factory() as session:
        action_after = await session.get(ActionItem, action_id)
        command_after = await session.scalar(
            select(ExecutionCommand).where(ExecutionCommand.action_id == action_id)
        )
        assert action_after is not None and action_after.status == "superseded"
        assert command_after is not None and command_after.status == "blocked"


@pytest.mark.asyncio
async def test_row_lease_prevents_same_event_from_being_claimed_twice(session_factory):
    async with session_factory() as session, session.begin():
        await enqueue_event(
            session,
            event_type="SYNC_SLEEPER",
            entity_type="league",
            entity_id="l1",
            dedupe_key="sync:l1:1",
        )
    async with session_factory() as session, session.begin():
        first = await lease_event(session, worker_id="w1")
        assert first is not None
    async with session_factory() as session, session.begin():
        second = await lease_event(session, worker_id="w2")
        assert second is None
