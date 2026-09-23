from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import app.weather as weather_module
from app.config import Settings
from app.weather import (
    NWSClient,
    WeatherIngestionError,
    compact_alerts,
    parse_stadium_locations,
    select_kickoff_forecast,
    sync_weather_context,
    weather_risk,
)


def test_stadium_parser_builds_valid_id_coordinate_crosswalk() -> None:
    locations = parse_stadium_locations(
        "stadium_id,stadium_name,lat,lon,city,state,country,tz,roof_type\n"
        "SEA00,Lumen Field,47.5952,-122.3316,Seattle,Washington,United States,"
        "America/Los_Angeles,Outdoors\n"
        "BAD00,Invalid,not-a-number,-122.0,Nowhere,NA,United States,UTC,Outdoors\n"
    )

    assert set(locations) == {"SEA00"}
    assert locations["SEA00"]["latitude"] == 47.5952
    assert locations["SEA00"]["roof_type"] == "Outdoors"


@pytest.mark.asyncio
async def test_nws_client_is_get_only_identified_and_host_restricted() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"properties": {}})

    async with NWSClient(
        Settings(nws_user_agent="JimAiFantasy-test/1.0"),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.point(47.5952, -122.3316)
        with pytest.raises(WeatherIngestionError, match="outside api.weather.gov"):
            await client.hourly("https://example.com/gridpoints/SEW/1,2/forecast/hourly")

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "api.weather.gov"
    assert requests[0].headers["user-agent"] == "JimAiFantasy-test/1.0"
    assert requests[0].headers["accept"] == "application/geo+json"


def test_kickoff_forecast_and_weather_risk_are_specific_to_game_time() -> None:
    kickoff = datetime(2026, 9, 10, 0, 20, tzinfo=timezone.utc)
    selected = select_kickoff_forecast(
        [
            {
                "startTime": "2026-09-09T23:00:00+00:00",
                "endTime": "2026-09-10T00:00:00+00:00",
                "temperature": 70,
                "temperatureUnit": "F",
                "windSpeed": "4 mph",
            },
            {
                "startTime": "2026-09-10T00:00:00+00:00",
                "endTime": "2026-09-10T01:00:00+00:00",
                "temperature": 55,
                "temperatureUnit": "F",
                "windSpeed": "20 to 28 mph",
                "probabilityOfPrecipitation": {"value": 85},
                "shortForecast": "Heavy Rain and Thunderstorms",
            },
        ],
        kickoff,
    )

    assert selected is not None
    assert selected["temperature"] == 55
    assert selected["max_wind_mph"] == 28
    risk = weather_risk(selected, [])
    assert risk["level"] == "high"
    assert any("wind" in factor for factor in risk["factors"])


def test_alerts_that_expire_before_kickoff_are_excluded() -> None:
    alerts = compact_alerts(
        {
            "features": [
                {
                    "properties": {
                        "event": "Heat Advisory",
                        "severity": "Moderate",
                        "onset": "2026-09-09T11:00:00-05:00",
                        "ends": "2026-09-09T20:00:00-05:00",
                    }
                },
                {
                    "properties": {
                        "event": "High Wind Warning",
                        "severity": "Severe",
                        "onset": "2026-09-13T10:00:00-04:00",
                        "ends": "2026-09-13T18:00:00-04:00",
                    }
                },
            ]
        },
        kickoff=datetime(2026, 9, 13, 17, tzinfo=timezone.utc),
    )

    assert [alert["event"] for alert in alerts] == ["High Wind Warning"]


def test_low_probability_storm_language_does_not_create_material_risk() -> None:
    risk = weather_risk(
        {
            "max_wind_mph": 8,
            "precipitation_probability_percent": 17,
            "temperature": 84,
            "temperature_unit": "F",
            "short_forecast": "Slight Chance Showers And Thunderstorms",
        },
        [],
    )

    assert risk == {"level": "low", "factors": []}


@pytest.mark.asyncio
async def test_weather_sync_skips_domes_and_writes_kickoff_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    (tmp_path / "nflverse-context.json").write_text(
        json.dumps(
            {
                "season": 2026,
                "week": 1,
                "week_schedule": [
                    {
                        "game_id": "2026_01_NE_SEA",
                        "gameday": "2026-09-09",
                        "gametime": "20:20",
                        "away_team": "NE",
                        "home_team": "SEA",
                        "stadium": "Lumen Field",
                        "stadium_id": "SEA00",
                        "roof": "outdoors",
                    },
                    {
                        "game_id": "2026_01_NO_DET",
                        "gameday": "2026-09-13",
                        "gametime": "13:00",
                        "away_team": "NO",
                        "home_team": "DET",
                        "stadium_id": "DET00",
                        "roof": "dome",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "stadium-locations.json").write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "locations": {
                    "SEA00": {
                        "stadium_id": "SEA00",
                        "stadium_name": "Lumen Field",
                        "latitude": 47.5952,
                        "longitude": -122.3316,
                        "city": "Seattle",
                        "state": "Washington",
                        "country": "United States",
                        "roof_type": "Outdoors",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path.startswith("/points/"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "forecastHourly": (
                            "https://api.weather.gov/gridpoints/SEW/124,67/forecast/hourly"
                        ),
                        "gridId": "SEW",
                        "gridX": 124,
                        "gridY": 67,
                    }
                },
            )
        if request.url.path.endswith("/forecast/hourly"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "periods": [
                            {
                                "startTime": "2026-09-09T17:00:00-07:00",
                                "endTime": "2026-09-09T18:00:00-07:00",
                                "temperature": 64,
                                "temperatureUnit": "F",
                                "windSpeed": "6 mph",
                                "windDirection": "W",
                                "probabilityOfPrecipitation": {"value": 10},
                                "relativeHumidity": {"value": 62},
                                "shortForecast": "Mostly Sunny",
                            }
                        ]
                    }
                },
            )
        if request.url.path == "/alerts/active":
            return httpx.Response(200, json={"features": []})
        raise AssertionError(f"Unexpected request: {request.url}")

    @asynccontextmanager
    async def fake_session_scope():
        yield object()

    async def noop(*_args, **_kwargs):
        return type("Health", (), {})()

    monkeypatch.setattr(weather_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(weather_module, "record_observation", noop)
    monkeypatch.setattr(weather_module, "update_source_health", noop)
    report = await sync_weather_context(
        Settings(reports_dir=tmp_path),
        now=now,
        nws_transport=httpx.MockTransport(handler),
    )

    assert report["coverage"]["eligible_game_count"] == 1
    assert report["coverage"]["forecast_count"] == 1
    assert report["coverage"]["skipped_indoor_game_count"] == 1
    assert report["games"][0]["forecast"]["temperature"] == 64
    assert report["games"][0]["risk"]["level"] == "low"
    assert (tmp_path / "weather-context.json").exists()
