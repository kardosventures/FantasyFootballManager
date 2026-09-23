import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models import ExecutionCommand
from app.trades import compile_trade_actions, synchronize_trade_actions


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def context():
    return {
        "state_hash": "a" * 64,
        "manual_constraints": {"protected_player_ids": ["protected"]},
        "our_team": {
            "all_players": [
                {"player_id": "keep", "name": "Keep Player", "position": "RB", "team": "DEN"},
                {"player_id": "send", "name": "Send Player", "position": "WR", "team": "IND"},
                {"player_id": "protected", "name": "Protected Player", "position": "WR", "team": "MIN"},
            ]
        },
        "league_rosters": [
            {
                "roster_id": 10,
                "owner_display_name": "other-manager",
                "players": [
                    {"player_id": "receive", "name": "Receive Player", "position": "RB", "team": "JAX"},
                    {"player_id": "their-keep", "name": "Their Keep", "position": "WR", "team": "SEA"},
                ],
            }
        ],
        "trade_market": {"active_offers": []},
    }


def decision(*, confidence=0.94, offer_player="send", recommended_action="propose"):
    return {
        "trade_status": "ready",
        "trade_targets": [
            {
                "target_roster_id": 10,
                "target_player_ids": ["receive"],
                "offer_player_ids": [offer_player],
                "recommended_action": recommended_action,
                "upside_tier": "high",
                "evidence_status": "confirmed",
                "confidence": confidence,
                "reason": "Meaningful full-roster championship upgrade.",
                "championship_case": "Adds scarce upside without creating a lineup hole.",
            }
        ],
        "incoming_trade_decisions": [],
    }


def settings(**updates):
    return Settings(
        _env_file=None,
        sleeper_league_id="league-1",
        sleeper_roster_id=8,
        execution_mode="browser",
        in_season_trade_actions_enabled=True,
        browser_live_actions="PROPOSE_TRADE,ACCEPT_TRADE,DECLINE_TRADE",
        **updates,
    )


def test_trade_compiler_requires_confirmed_threshold_and_protects_outgoing_assets():
    assert len(compile_trade_actions(settings(), decision(), context())) == 1
    assert compile_trade_actions(settings(), decision(confidence=0.91), context()) == []
    assert compile_trade_actions(settings(), decision(offer_player="protected"), context()) == []
    assert compile_trade_actions(
        settings(), decision(recommended_action="watch"), context()
    ) == []


@pytest.mark.asyncio
async def test_trade_scheduler_queues_once_and_refuses_a_second_weekly_proposal(
    session_factory, monkeypatch, tmp_path
):
    @asynccontextmanager
    async def test_session_scope():
        async with session_factory() as session, session.begin():
            yield session

    monkeypatch.setattr("app.trades.session_scope", test_session_scope)
    qualified_settings = settings(
        reports_dir=tmp_path,
        sleeper_ui_contract_version="ui-v1",
    )
    (tmp_path / "trade-propose-submission-qualification.json").write_text(
        json.dumps(
            {
                "status": "qualified",
                "ui_contract_version": "ui-v1",
                "league_id": "league-1",
                "roster_id": 8,
                "action_type": "PROPOSE_TRADE",
                "write_attempted": True,
                "authenticated_ui_verified": True,
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 9, 15, 16, tzinfo=timezone.utc)
    trade_context = context()
    previews = compile_trade_actions(qualified_settings, decision(), trade_context)
    first = await synchronize_trade_actions(
        qualified_settings, trade_context, previews, now=now
    )
    assert first["state"] == "scheduled"
    assert len(first["queued"]) == 1

    async with session_factory() as session, session.begin():
        command = await session.scalar(select(ExecutionCommand))
        command.status = "verified"

    second = await synchronize_trade_actions(
        qualified_settings, trade_context, previews, now=now
    )
    assert second["state"] == "gated"
    assert second["queued"] == []
