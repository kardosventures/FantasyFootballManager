from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.fantasypros import (
    FantasyProsClient,
    FantasyProsClientError,
    FantasyProsQuotaExceeded,
    fantasypros_quota_status,
)
from app.in_season_sources import (
    NFLVERSE_URLS,
    _fantasypros_priority_ids,
    _fetch_csv,
    latest_snapshot_rows,
)
from app.models import SourceObservation
from app.repositories import record_observation
from app.source_catalog import CHAMPIONSHIP_OBJECTIVE, build_source_catalog


def test_source_catalog_is_championship_first_and_free_first():
    catalog = build_source_catalog(Settings(_env_file=None))
    assert "winning the league championship" in CHAMPIONSHIP_OBJECTIVE
    assert catalog["strategy"] == "free_first_with_optional_decision_grade_projection_feed"
    sources = {source["id"]: source for source in catalog["sources"]}
    assert sources["sleeper"]["required"]
    assert sources["sleeper"]["operational_state"] == "active"
    assert sources["nflverse"]["required"]
    assert not sources["fantasypros"]["configured"]
    assert sources["fantasypros"]["operational_state"] == "awaiting_key"
    assert sources["nws"]["implemented"]
    assert sources["nws"]["operational_state"] == "active"
    assert sources["sportradar"]["delivery"] == "paid_rest_and_push"


def test_nflverse_player_stats_uses_current_official_release_asset():
    assert NFLVERSE_URLS["player_stats"].endswith("/stats_player/stats_player_week_{season}.csv")


def test_only_latest_depth_chart_snapshot_is_retained():
    rows = [
        {"dt": "2026-09-08T12:00:00Z", "player": "old"},
        {"dt": "2026-09-09T12:00:00Z", "player": "one"},
        {"dt": "2026-09-09T12:00:00Z", "player": "two"},
    ]
    assert latest_snapshot_rows(rows, timestamp_field="dt") == rows[1:]


def test_fantasypros_client_requires_an_explicit_key():
    with pytest.raises(FantasyProsClientError, match="FANTASYPROS_API_KEY"):
        FantasyProsClient(Settings(_env_file=None))


@pytest.mark.asyncio
async def test_fantasypros_client_uses_get_and_documented_host(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "api.fantasypros.com"
        assert request.url.path == "/public/v2/json/nfl/2026/projections"
        assert request.url.params["week"] == "1"
        assert request.url.params["position"] == "RB"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json={"players": []})

    settings = Settings(fantasypros_api_key=SecretStr("test-key"), reports_dir=tmp_path)
    client = FantasyProsClient(settings, transport=httpx.MockTransport(handler))
    try:
        response = await client.projections(season=2026, week=1)
        assert response.payload == {"players": []}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fantasypros_targets_at_most_ten_players_and_preserves_reserve(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["players"] == "11:22"
        assert request.url.params["position"] == "WR"
        return httpx.Response(200, json={"players": []})

    settings = Settings(
        fantasypros_api_key=SecretStr("test-key"),
        reports_dir=tmp_path,
        fantasypros_daily_request_limit=10,
        fantasypros_request_reserve=9,
    )
    client = FantasyProsClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError, match="position is required"):
            await client.projections(season=2026, week=1, player_ids=["11", "22"])
        await client.projections(season=2026, week=1, player_ids=["11", "22"], position="WR")
        with pytest.raises(FantasyProsQuotaExceeded, match="reserve is protected"):
            await client.projections(season=2026, week=1, player_ids=["11", "22"], position="WR")
    finally:
        await client.aclose()
    assert len(requests) == 1
    quota = fantasypros_quota_status(settings)
    assert quota["used"] == 1
    assert quota["standard_remaining"] == 0
    assert quota["total_remaining"] == 9


def test_fantasypros_priority_ids_put_our_roster_before_opponent() -> None:
    sleeper = {
        "owner_roster": {"players": ["ours", "shared", "missing"]},
        "opponent_matchup": {"players": ["theirs", "shared"]},
    }
    nflverse = {
        "player_ids": {
            "ours": {"fantasypros_id": "1", "position": "QB"},
            "shared": {"fantasypros_id": "2", "position": "WR"},
            "theirs": {"fantasypros_id": "3", "position": "RB"},
        }
    }
    assert _fantasypros_priority_ids(sleeper, nflverse) == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_nflverse_csv_loader_rejects_unapproved_hosts():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as client:
        with pytest.raises(ValueError, match="allowlisted"):
            await _fetch_csv(client, "https://example.com/players.csv")


@pytest.mark.asyncio
async def test_nflverse_optional_dataset_can_be_unpublished():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await _fetch_csv(
            client,
            "https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats_2026.csv",
            optional=True,
        )
    assert response is None


@pytest.mark.asyncio
async def test_immutable_observation_insert_is_idempotent():
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        first = await record_observation(
            session,
            provider="test",
            endpoint="https://example.invalid/context",
            entity_type="context",
            entity_id="one",
            payload={"value": 1},
        )
        second = await record_observation(
            session,
            provider="test",
            endpoint="https://example.invalid/context",
            entity_type="context",
            entity_id="one",
            payload={"value": 1},
        )
        await session.commit()
        assert first.id == second.id
        assert await session.scalar(select(func.count()).select_from(SourceObservation)) == 1
    await engine.dispose()
