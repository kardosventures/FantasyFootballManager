from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.historical_sources import (
    HISTORICAL_BACKFILL_VERSION,
    _read_gzip_json,
    _write_gzip_json,
    merge_nflverse_history,
    probe_fantasypros_archive,
    sync_nflverse_history,
)
from app.in_season_sources import CsvResponse


def test_compressed_history_round_trip_and_merge_prefers_live_season(tmp_path: Path) -> None:
    path = tmp_path / "history.json.gz"
    archive = {
        "version": HISTORICAL_BACKFILL_VERSION,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "seasons": [2024, 2025],
        "historical_player_stats": [
            {"season": "2024", "player_id": "old"},
            {"season": "2025", "player_id": "archive-overlap"},
        ],
    }
    _write_gzip_json(path, archive)
    restored = _read_gzip_json(path)

    merged = merge_nflverse_history(
        {
            "historical_player_stats": [
                {"season": "2025", "player_id": "live-overlap"},
                {"season": "2026", "player_id": "current"},
            ]
        },
        restored,
    )

    assert [row["player_id"] for row in merged["historical_player_stats"]] == [
        "old",
        "live-overlap",
        "current",
    ]
    assert merged["historical_archive"]["seasons"] == [2024, 2025]


@pytest.mark.asyncio
async def test_nflverse_history_backfill_is_compressed_and_resumable(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    async def fake_fetch(_client, url: str, *, optional: bool = False):
        calls.append(url)
        if url.endswith("games.csv"):
            rows = [
                {"season": "2023", "week": "1", "game_id": "old"},
                {"season": "2024", "week": "1", "game_id": "new"},
            ]
        else:
            season = url.rsplit("_", 1)[-1].split(".", 1)[0]
            rows = [{"season": season, "week": "1", "player_id": f"p-{season}"}]
        return CsvResponse(url, rows, 1.0, "test")

    monkeypatch.setattr("app.historical_sources._fetch_csv", fake_fetch)
    settings = Settings(_env_file=None, reports_dir=tmp_path)

    first = await sync_nflverse_history(
        settings, current_season=2025, start_season=2023, end_season=2024
    )
    second = await sync_nflverse_history(
        settings, current_season=2025, start_season=2023, end_season=2024
    )

    assert first["available_seasons"] == [2023, 2024]
    assert first["fetched_seasons"] == [2023, 2024]
    assert first["row_counts"]["historical_player_stats"] == 2
    assert first["compressed_bytes"] > 0
    assert second["fetched_seasons"] == []
    assert second["cached_seasons"] == [2023, 2024]
    assert len(calls) == 8


def test_historical_sleeper_ids_are_deduplicated_and_exclude_current() -> None:
    settings = Settings(
        _env_file=None,
        sleeper_league_id="current",
        sleeper_historical_league_ids="old-one,current,old-two,old-one",
    )

    assert settings.historical_sleeper_league_ids == ("old-one", "old-two")


@pytest.mark.asyncio
async def test_fantasypros_archive_without_provider_timestamp_is_diagnostic_only(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeFantasyProsClient:
        def __init__(self, _settings: Settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def projections(self, **_params):
            return SimpleNamespace(
                payload={"count": "1", "players": [{"player_id": "one"}]},
                quota={"standard_remaining": 2},
            )

    monkeypatch.setattr("app.historical_sources.FantasyProsClient", FakeFantasyProsClient)
    monkeypatch.setattr(
        "app.historical_sources.fantasypros_quota_status",
        lambda _settings: {"standard_remaining": 3},
    )
    settings = Settings(
        _env_file=None,
        reports_dir=tmp_path,
        fantasypros_api_key="test-key",
    )

    result = await probe_fantasypros_archive(settings, season=2025, week=17, position="RB")
    saved = json.loads((tmp_path / "fantasypros-archive-probe.json").read_text())

    assert result["status"] == "access_confirmed"
    assert result["provider_timestamp_fields"] == []
    assert result["causal_promotion_eligible"] is False
    assert saved["causal_promotion_eligible"] is False
