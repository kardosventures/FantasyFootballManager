from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.scheduler as scheduler
from app.config import Settings
from app.database import Base
from app.evidence import (
    _required_datasets,
    apply_operational_evidence,
    evaluate_manager_evidence,
    operational_evidence_checks,
)
from app.models import OperationalIncident
from app.repositories import update_dataset_health


def _quality(now: datetime) -> dict:
    return {
        "sleeper_as_of": (now - timedelta(minutes=2)).isoformat(),
        "nflverse_as_of": (now - timedelta(hours=2)).isoformat(),
        "sleeper_ui_projection_as_of": (now - timedelta(seconds=30)).isoformat(),
        "expected_league_roster_count": 12,
        "observed_league_roster_count": 12,
        "schedule_game_count": 16,
        "starter_count": 11,
        "public_roster_player_ids": ["a", "b"],
        "browser_roster_player_ids": ["a", "b"],
        "sleeper_ui_projection_count": 16,
        "sleeper_ui_matchup_projection_count": 22,
        "weekly_projection_feed": "sleeper_ui_roster_only",
        "weather_status": "active",
    }


def test_evidence_gate_allows_complete_fresh_state() -> None:
    now = datetime(2026, 9, 9, 18, tzinfo=timezone.utc)
    gate = evaluate_manager_evidence(_quality(now), Settings(), now=now)
    assert gate["status"] == "ready"
    assert gate["allows_reasoning"]
    assert gate["allows_lineup_execution"]


def test_evidence_gate_blocks_stale_sleeper_and_roster_disagreement() -> None:
    now = datetime(2026, 9, 9, 18, tzinfo=timezone.utc)
    quality = _quality(now)
    quality["sleeper_as_of"] = (now - timedelta(hours=1)).isoformat()
    quality["browser_roster_player_ids"] = ["a", "c"]
    gate = evaluate_manager_evidence(quality, Settings(), now=now)
    assert gate["status"] == "blocked"
    assert not gate["allows_reasoning"]
    assert {row["id"] for row in gate["checks"] if row["status"] == "blocked"} == {
        "sleeper_state",
        "roster_cross_check",
    }


def test_evidence_gate_treats_weather_as_degraded_not_globally_blocking() -> None:
    now = datetime(2026, 9, 9, 18, tzinfo=timezone.utc)
    quality = _quality(now)
    quality["weather_status"] = "degraded"
    gate = evaluate_manager_evidence(quality, Settings(), now=now)
    assert gate["status"] == "degraded"
    assert gate["allows_lineup_execution"]
    assert gate["warnings"]


def test_missing_matchup_projection_panel_is_advisory_not_blocking() -> None:
    now = datetime(2026, 9, 9, 18, tzinfo=timezone.utc)
    quality = _quality(now)
    quality["sleeper_ui_matchup_projection_count"] = 0
    gate = evaluate_manager_evidence(quality, Settings(), now=now)

    assert gate["status"] == "degraded"
    assert gate["allows_reasoning"]
    assert gate["allows_lineup_execution"]
    assert any(row["id"] == "matchup_projection_availability" for row in gate["checks"])
    assert all(
        dataset != "matchup" or provider != "Sleeper authenticated UI matchup"
        for provider, dataset, _ in _required_datasets(Settings(), "reasoning")
    )


@pytest.mark.asyncio
async def test_operational_gate_blocks_partial_dataset_and_critical_incident() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(_env_file=None)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        for provider, dataset, _ in _required_datasets(settings, "SET_LINEUP"):
            await update_dataset_health(
                session,
                provider,
                dataset,
                success=True,
                expected_records=12,
                observed_records=12,
            )
        partial = await update_dataset_health(
            session,
            "Sleeper public API",
            "rosters",
            success=True,
            expected_records=12,
            observed_records=11,
        )
        session.add(
            OperationalIncident(
                category="source_failure",
                severity="critical",
                summary="Roster feed failed",
                dedupe_key="test-critical",
            )
        )
        await session.flush()
        context = {
            "state_hash": "base",
            "data_quality": {
                "evidence_gate": evaluate_manager_evidence(_quality(now), settings, now=now)
            },
        }
        gate = await apply_operational_evidence(
            context,
            session,
            settings,
            purpose="SET_LINEUP",
            now=now,
        )

    assert partial.completeness == 11 / 12
    assert gate["status"] == "blocked"
    blocked_ids = {row["id"] for row in gate["checks"] if row["status"] == "blocked"}
    assert "dataset:Sleeper public API:rosters" in blocked_ids
    assert "operational_incidents" in blocked_ids
    assert context["state_hash"] != "base"
    await engine.dispose()


@pytest.mark.asyncio
async def test_operational_gate_accepts_a_healthy_expected_empty_queue() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(_env_file=None)
    async with factory() as session:
        for provider, dataset, _ in _required_datasets(settings, "WAIVER_CLAIM"):
            await update_dataset_health(
                session,
                provider,
                dataset,
                success=True,
                expected_records=0,
                observed_records=0,
            )
        checks = await operational_evidence_checks(
            session,
            settings,
            purpose="WAIVER_CLAIM",
        )

    pending = next(row for row in checks if row["dataset"] == "pending_waivers")
    assert pending["status"] == "pass"
    assert pending["completeness"] == 1.0
    await engine.dispose()


@pytest.mark.asyncio
async def test_operational_gate_fails_closed_when_required_health_is_missing() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(_env_file=None)
    now = datetime.now(timezone.utc)
    context = {
        "state_hash": "base",
        "data_quality": {
            "evidence_gate": evaluate_manager_evidence(_quality(now), settings, now=now)
        },
    }
    async with factory() as session:
        gate = await apply_operational_evidence(
            context,
            session,
            settings,
            purpose="SET_LINEUP",
            now=now,
        )
    assert not gate["allows_lineup_execution"]
    assert any("no persisted health record" in reason for reason in gate["blockers"])
    await engine.dispose()


@pytest.mark.asyncio
async def test_operational_gate_hash_is_stable_while_evidence_is_unchanged() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(_env_file=None)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        for provider, dataset, _ in _required_datasets(settings, "SET_LINEUP"):
            await update_dataset_health(session, provider, dataset, success=True)
        first = {
            "state_hash": "base",
            "data_quality": {
                "evidence_gate": evaluate_manager_evidence(_quality(now), settings, now=now)
            },
        }
        second = {
            "state_hash": "base",
            "data_quality": {
                "evidence_gate": evaluate_manager_evidence(
                    _quality(now), settings, now=now + timedelta(seconds=1)
                )
            },
        }
        await apply_operational_evidence(first, session, settings, purpose="SET_LINEUP", now=now)
        await apply_operational_evidence(
            second,
            session,
            settings,
            purpose="SET_LINEUP",
            now=now + timedelta(seconds=1),
        )
    assert first["state_hash"] == second["state_hash"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_failure_records_provider_and_dataset_health(monkeypatch) -> None:
    calls = []

    @asynccontextmanager
    async def fake_scope():
        yield object()

    async def source_health(_session, provider, **values):
        calls.append(("provider", provider, values))

    async def dataset_health(_session, provider, dataset, **values):
        calls.append(("dataset", provider, dataset, values))

    async def failure():
        raise RuntimeError("offline")

    monkeypatch.setattr(scheduler, "session_scope", fake_scope)
    monkeypatch.setattr(scheduler, "update_source_health", source_health)
    monkeypatch.setattr(scheduler, "update_dataset_health", dataset_health)
    await scheduler._safe(
        "test-source",
        failure,
        health_targets=(("provider-a", "dataset-a"),),
    )

    assert calls[0][0:2] == ("provider", "provider-a")
    assert calls[0][2]["success"] is False
    assert calls[1][0:3] == ("dataset", "provider-a", "dataset-a")
    assert "offline" in calls[1][3]["error"]
