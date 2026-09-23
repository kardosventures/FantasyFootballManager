from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import PlayerWeekFeature, ProjectionSnapshot, SimulationRun, SourceDatasetHealth
from app.repositories import (
    intelligence_snapshot_totals,
    record_intelligence_snapshots,
    update_dataset_health,
)


async def _database():
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_dataset_health_tracks_failure_and_recovery() -> None:
    engine, factory = await _database()
    async with factory() as session:
        health = await update_dataset_health(
            session,
            "provider",
            "dataset",
            success=False,
            expected_records=12,
            observed_records=4,
            error="partial",
        )
        await session.commit()
        assert health.status == "unavailable"
        assert health.completeness == 4 / 12
        recovered = await update_dataset_health(
            session,
            "provider",
            "dataset",
            success=True,
            expected_records=12,
            observed_records=12,
        )
        await session.commit()
        assert recovered.status == "healthy"
        assert recovered.consecutive_failures == 0
        assert recovered.last_error is None
        preserved = await update_dataset_health(
            session,
            "provider",
            "dataset",
            success=True,
        )
        assert preserved.expected_records == 12
        assert preserved.observed_records == 12
        assert preserved.completeness == 1.0
        assert await session.scalar(select(func.count()).select_from(SourceDatasetHealth)) == 1
    await engine.dispose()


async def test_dataset_health_marks_an_expected_empty_result_complete() -> None:
    engine, factory = await _database()
    async with factory() as session:
        health = await update_dataset_health(
            session,
            "provider",
            "empty-queue",
            success=True,
            expected_records=0,
            observed_records=0,
        )
        assert health.status == "healthy"
        assert health.completeness == 1.0
    await engine.dispose()


async def test_dataset_health_treats_not_yet_published_data_as_pending() -> None:
    engine, factory = await _database()
    async with factory() as session:
        health = await update_dataset_health(
            session,
            "provider",
            "future-matchup",
            success=False,
            expected_absence=True,
            expected_records=22,
            observed_records=0,
            error="Week 3 is not published yet",
        )
        assert health.status == "pending"
        assert health.consecutive_failures == 0
        assert health.last_error == "Week 3 is not published yet"
    await engine.dispose()


async def test_intelligence_snapshots_are_idempotent() -> None:
    engine, factory = await _database()
    context = {
        "season": "2026",
        "week": 1,
        "league": {"league_id": "league"},
        "league_rosters": [
            {
                "roster_id": 1,
                "players": [
                    {
                        "player_id": "player",
                        "usage": {
                            "feature_version": "usage-v1",
                            "feature_hash": "feature-hash",
                            "provenance": {"source": "test"},
                        },
                        "projection_ensemble": {
                            "model_version": "projection-v1",
                            "input_hash": "projection-hash",
                            "mean": 12,
                            "sources": [],
                        },
                    }
                ],
            }
        ],
        "free_agent_candidates": [
            {
                "player_id": "free-agent",
                "usage": {
                    "feature_version": "usage-v1",
                    "feature_hash": "free-agent-feature-hash",
                    "provenance": {"source": "test"},
                },
                "projection_ensemble": {
                    "model_version": "projection-v1",
                    "input_hash": "free-agent-projection-hash",
                    "mean": 10,
                    "sources": [],
                },
            }
        ],
        "championship_outlook": {
            "status": "complete",
            "model_version": "simulation-v1",
            "input_hash": "simulation-hash",
            "seed": 7,
            "simulation_count": 500,
            "teams": {},
        },
    }
    async with factory() as session:
        assert await record_intelligence_snapshots(session, context) == {
            "usage": 2,
            "projections": 2,
            "simulations": 1,
        }
        await session.commit()
        assert await record_intelligence_snapshots(session, context) == {
            "usage": 0,
            "projections": 0,
            "simulations": 0,
        }
        assert await intelligence_snapshot_totals(session, season=2026, week=1) == {
            "usage": 2,
            "projections": 2,
            "simulations": 1,
        }
        assert await session.scalar(select(func.count()).select_from(PlayerWeekFeature)) == 2
        assert await session.scalar(select(func.count()).select_from(ProjectionSnapshot)) == 2
        assert await session.scalar(select(func.count()).select_from(SimulationRun)) == 1
    await engine.dispose()
