from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings, get_settings
from app.database import session_scope
from app.readiness import read_report
from app.repositories import record_observation, update_source_health
from app.utils import canonical_json, content_hash

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
STADIUMS_URL = "https://raw.githubusercontent.com/greerreNFL/Stadiums/main/data/stadiums.csv"
STADIUM_CACHE_TTL = timedelta(days=30)
POINT_CACHE_TTL = timedelta(days=7)
INDOOR_ROOFS = {"dome", "closed", "indoor", "indoors"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class WeatherIngestionError(RuntimeError):
    pass


@dataclass(frozen=True)
class WeatherResponse:
    endpoint: str
    payload: dict[str, Any]
    latency_ms: float


@dataclass(frozen=True)
class StadiumLocationResponse:
    endpoint: str
    locations: dict[str, dict[str, Any]]
    latency_ms: float
    last_modified: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_datetime(value: Any) -> datetime | None:
    try:
        observed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return observed.astimezone(UTC)


def _fresh(value: Any, *, now: datetime, ttl: timedelta) -> bool:
    observed = _as_datetime(value)
    return bool(observed and timedelta(0) <= now - observed <= ttl)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_stadium_locations(text: str) -> dict[str, dict[str, Any]]:
    """Parse the stadium-id coordinate crosswalk into a compact validated index."""
    locations: dict[str, dict[str, Any]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        stadium_id = str(row.get("stadium_id") or "").strip()
        try:
            latitude = float(row.get("lat") or "")
            longitude = float(row.get("lon") or "")
        except ValueError:
            continue
        if not stadium_id or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        locations[stadium_id] = {
            "stadium_id": stadium_id,
            "stadium_name": str(row.get("stadium_name") or "").strip() or None,
            "latitude": latitude,
            "longitude": longitude,
            "city": str(row.get("city") or "").strip() or None,
            "state": str(row.get("state") or "").strip() or None,
            "country": str(row.get("country") or "").strip() or None,
            "timezone": str(row.get("tz") or "").strip() or None,
            "roof_type": str(row.get("roof_type") or "").strip() or None,
        }
    if not locations:
        raise WeatherIngestionError("The stadium coordinate source contained no valid locations")
    return locations


async def _fetch_stadium_locations(
    transport: httpx.AsyncBaseTransport | None = None,
) -> StadiumLocationResponse:
    parsed = urlparse(STADIUMS_URL)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
        raise WeatherIngestionError("Stadium coordinates must use the allowlisted HTTPS source")
    started = time.perf_counter()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        follow_redirects=False,
        transport=transport,
        headers={"User-Agent": "JimAiFantasy/1.0"},
    ) as client:
        response = await client.get(STADIUMS_URL)
    latency_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return StadiumLocationResponse(
        endpoint=STADIUMS_URL,
        locations=parse_stadium_locations(response.text),
        latency_ms=latency_ms,
        last_modified=response.headers.get("last-modified"),
    )


async def _load_stadium_locations(
    reports_dir: Path,
    *,
    now: datetime,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], StadiumLocationResponse | None]:
    cache_path = reports_dir / "stadium-locations.json"
    cached = read_report(cache_path, {})
    cached_locations = cached.get("locations") or {}
    if (
        isinstance(cached_locations, dict)
        and cached_locations
        and _fresh(cached.get("generated_at"), now=now, ttl=STADIUM_CACHE_TTL)
    ):
        return (
            cached_locations,
            {
                "url": STADIUMS_URL,
                "retrieved_at": cached.get("generated_at"),
                "cache_status": "fresh",
                "location_count": len(cached_locations),
            },
            None,
        )
    try:
        response = await _fetch_stadium_locations(transport)
    except Exception as exc:
        if isinstance(cached_locations, dict) and cached_locations:
            return (
                cached_locations,
                {
                    "url": STADIUMS_URL,
                    "retrieved_at": cached.get("generated_at"),
                    "cache_status": "stale_fallback",
                    "location_count": len(cached_locations),
                    "refresh_error": str(exc)[:500],
                },
                None,
            )
        raise WeatherIngestionError(f"Stadium coordinate refresh failed: {exc}") from exc
    report = {
        "generated_at": now.isoformat(),
        "source": response.endpoint,
        "last_modified": response.last_modified,
        "locations": response.locations,
    }
    _write_json_atomic(cache_path, report)
    return (
        response.locations,
        {
            "url": STADIUMS_URL,
            "retrieved_at": now.isoformat(),
            "cache_status": "refreshed",
            "location_count": len(response.locations),
            "last_modified": response.last_modified,
        },
        response,
    )


class NWSClient:
    """GET-only client restricted to the official National Weather Service API."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings or get_settings()
        if not self.settings.nws_user_agent.strip():
            raise WeatherIngestionError("NWS_USER_AGENT must identify the application")
        self._client = httpx.AsyncClient(
            base_url=self.settings.nws_base_url + "/",
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": self.settings.nws_user_agent,
                "Accept": "application/geo+json",
            },
        )

    async def __aenter__(self) -> NWSClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _validate_url(self, value: str) -> str:
        candidate = urljoin(self.settings.nws_base_url + "/", value)
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.hostname != "api.weather.gov":
            raise WeatherIngestionError("Blocked weather request outside api.weather.gov")
        if not parsed.path.startswith(("/points/", "/gridpoints/", "/alerts/")):
            raise WeatherIngestionError("Blocked request outside documented NWS API paths")
        return candidate

    async def _get(self, value: str, **params: Any) -> WeatherResponse:
        candidate = self._validate_url(value)
        started = time.perf_counter()
        response: httpx.Response | None = None
        for attempt in range(3):
            response = await self._client.get(candidate, params=params)
            if response.status_code not in RETRYABLE_STATUS_CODES or attempt == 2:
                break
            await asyncio.sleep(0.25 * (attempt + 1))
        assert response is not None
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise WeatherIngestionError("NWS returned an unexpected payload type")
        return WeatherResponse(endpoint=str(response.url), payload=payload, latency_ms=latency_ms)

    async def point(self, latitude: float, longitude: float) -> WeatherResponse:
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise WeatherIngestionError("Invalid stadium coordinates")
        return await self._get(f"points/{latitude:.4f},{longitude:.4f}")

    async def hourly(self, forecast_hourly_url: str) -> WeatherResponse:
        candidate = self._validate_url(forecast_hourly_url)
        if not urlparse(candidate).path.endswith("/forecast/hourly"):
            raise WeatherIngestionError("NWS hourly URL is outside the forecast/hourly route")
        return await self._get(candidate)

    async def alerts(self, latitude: float, longitude: float) -> WeatherResponse:
        return await self._get("alerts/active", point=f"{latitude:.4f},{longitude:.4f}")


def game_kickoff(row: dict[str, Any]) -> datetime | None:
    gameday = str(row.get("gameday") or "")
    gametime = str(row.get("gametime") or "")
    if not gameday or not gametime:
        return None
    try:
        local = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN)
    except ValueError:
        return None
    return local.astimezone(UTC)


def _metric_value(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get("value"))
    except (TypeError, ValueError):
        return None


def _max_wind_mph(value: Any) -> float | None:
    speeds = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", str(value or ""))]
    return max(speeds, default=None)


def compact_forecast(period: dict[str, Any]) -> dict[str, Any]:
    dewpoint = period.get("dewpoint") if isinstance(period.get("dewpoint"), dict) else {}
    return {
        "start_time": period.get("startTime"),
        "end_time": period.get("endTime"),
        "temperature": period.get("temperature"),
        "temperature_unit": period.get("temperatureUnit"),
        "precipitation_probability_percent": _metric_value(
            period.get("probabilityOfPrecipitation")
        ),
        "relative_humidity_percent": _metric_value(period.get("relativeHumidity")),
        "dewpoint": (round(value, 1) if (value := _metric_value(dewpoint)) is not None else None),
        "dewpoint_unit_code": dewpoint.get("unitCode"),
        "wind_speed": period.get("windSpeed"),
        "max_wind_mph": _max_wind_mph(period.get("windSpeed")),
        "wind_direction": period.get("windDirection"),
        "short_forecast": period.get("shortForecast"),
        "detailed_forecast": str(period.get("detailedForecast") or "")[:600] or None,
    }


def select_kickoff_forecast(
    periods: list[dict[str, Any]], kickoff: datetime
) -> dict[str, Any] | None:
    nearest: tuple[float, dict[str, Any]] | None = None
    for period in periods:
        start = _as_datetime(period.get("startTime"))
        end = _as_datetime(period.get("endTime"))
        if not start:
            continue
        if end and start <= kickoff < end:
            return compact_forecast(period)
        distance = abs((kickoff - start).total_seconds())
        if distance <= 3 * 60 * 60 and (nearest is None or distance < nearest[0]):
            nearest = (distance, period)
    return compact_forecast(nearest[1]) if nearest else None


def compact_alerts(
    payload: dict[str, Any], *, kickoff: datetime | None = None
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for feature in payload.get("features") or []:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            continue
        onset = _as_datetime(properties.get("onset") or properties.get("effective"))
        ends = _as_datetime(properties.get("ends") or properties.get("expires"))
        if kickoff and (
            (onset and onset > kickoff + timedelta(hours=4))
            or (ends and ends < kickoff - timedelta(hours=1))
        ):
            continue
        alerts.append(
            {
                "event": properties.get("event"),
                "severity": properties.get("severity"),
                "certainty": properties.get("certainty"),
                "urgency": properties.get("urgency"),
                "headline": properties.get("headline"),
                "onset": properties.get("onset") or properties.get("effective"),
                "ends": properties.get("ends") or properties.get("expires"),
                "description": str(properties.get("description") or "")[:800] or None,
                "instruction": str(properties.get("instruction") or "")[:800] or None,
            }
        )
        if len(alerts) == 10:
            break
    return alerts


def weather_risk(forecast: dict[str, Any] | None, alerts: list[dict[str, Any]]) -> dict[str, Any]:
    factors: list[str] = []
    level = "low"
    alert_severities = {str(alert.get("severity") or "").lower() for alert in alerts}
    if alert_severities & {"extreme", "severe"}:
        level = "high"
        factors.append("active severe or extreme NWS alert")
    elif "moderate" in alert_severities:
        level = "moderate"
        factors.append("active moderate NWS alert")
    if forecast:
        wind = forecast.get("max_wind_mph")
        precipitation = forecast.get("precipitation_probability_percent")
        temperature = forecast.get("temperature")
        text = " ".join(
            str(forecast.get(key) or "") for key in ("short_forecast", "detailed_forecast")
        ).lower()
        hazardous = any(
            phrase in text
            for phrase in ("thunderstorm", "snow", "freezing", "ice", "sleet", "heavy rain")
        )
        if isinstance(wind, (int, float)) and wind >= 25:
            level = "high"
            factors.append(f"wind up to {wind:g} mph")
        elif isinstance(wind, (int, float)) and wind >= 18:
            level = "moderate" if level == "low" else level
            factors.append(f"wind up to {wind:g} mph")
        if isinstance(precipitation, (int, float)) and precipitation >= 80 and hazardous:
            level = "high"
            factors.append(f"{precipitation:g}% precipitation with hazardous conditions")
        elif isinstance(precipitation, (int, float)) and precipitation >= 60:
            level = "moderate" if level == "low" else level
            factors.append(f"{precipitation:g}% precipitation probability")
        if (
            isinstance(temperature, (int, float))
            and str(forecast.get("temperature_unit") or "").upper() == "F"
        ):
            if temperature <= 10:
                level = "high"
                factors.append(f"extreme cold ({temperature:g} F)")
            elif temperature <= 25 or temperature >= 95:
                level = "moderate" if level == "low" else level
                factors.append(f"temperature {temperature:g} F")
        if hazardous and level == "low" and (precipitation is None or precipitation >= 30):
            level = "moderate"
            factors.append("potentially disruptive precipitation")
    return {"level": level, "factors": factors}


def _point_cache_entry_valid(entry: Any, location: dict[str, Any], now: datetime) -> bool:
    if not isinstance(entry, dict) or not _fresh(
        entry.get("refreshed_at"), now=now, ttl=POINT_CACHE_TTL
    ):
        return False
    try:
        coordinates_match = (
            abs(float(entry["latitude"]) - float(location["latitude"])) < 0.0001
            and abs(float(entry["longitude"]) - float(location["longitude"])) < 0.0001
        )
    except (KeyError, TypeError, ValueError):
        return False
    return coordinates_match and bool(entry.get("forecast_hourly"))


async def sync_weather_context(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    nws_transport: httpx.AsyncBaseTransport | None = None,
    stadium_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Attach kickoff-specific NWS weather and alerts to weather-exposed weekly games."""
    cfg = settings or get_settings()
    observed_at = (now or _utc_now()).astimezone(UTC)
    nflverse = read_report(cfg.reports_dir / "nflverse-context.json", {})
    schedule = nflverse.get("week_schedule") or []
    if not isinstance(schedule, list):
        raise WeatherIngestionError("The nflverse weekly schedule is unavailable")

    locations, location_source, stadium_response = await _load_stadium_locations(
        cfg.reports_dir, now=observed_at, transport=stadium_transport
    )
    point_cache_path = cfg.reports_dir / "nws-points-cache.json"
    point_cache_report = read_report(point_cache_path, {})
    point_cache = point_cache_report.get("entries") or {}
    if not isinstance(point_cache, dict):
        point_cache = {}

    indoor_games: list[dict[str, Any]] = []
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    uncovered: list[dict[str, Any]] = []
    for game in schedule:
        if not isinstance(game, dict):
            continue
        roof = str(game.get("roof") or "").strip().lower()
        if roof in INDOOR_ROOFS:
            indoor_games.append(
                {
                    "game_id": game.get("game_id"),
                    "stadium_id": game.get("stadium_id"),
                    "roof": game.get("roof"),
                }
            )
            continue
        stadium_id = str(game.get("stadium_id") or "")
        location = locations.get(stadium_id)
        kickoff = game_kickoff(game)
        if not location or not kickoff:
            uncovered.append(
                {
                    "game_id": game.get("game_id"),
                    "stadium_id": stadium_id or None,
                    "reason": "stadium coordinates unavailable"
                    if not location
                    else "invalid kickoff",
                }
            )
            continue
        if str(location.get("country") or "").lower() not in {
            "united states",
            "united states of america",
            "usa",
            "us",
        }:
            uncovered.append(
                {
                    "game_id": game.get("game_id"),
                    "stadium_id": stadium_id,
                    "reason": "stadium is outside NWS coverage",
                }
            )
            continue
        eligible.append((game, location))

    response_records: list[tuple[WeatherResponse, str]] = []
    errors: list[str] = []
    point_cache_refreshes = 0
    point_cache_reuses = 0
    semaphore = asyncio.Semaphore(4)

    async with NWSClient(cfg, transport=nws_transport) as nws:

        async def fetch_game(game: dict[str, Any], location: dict[str, Any]) -> dict[str, Any]:
            nonlocal point_cache_refreshes, point_cache_reuses
            game_id = str(game.get("game_id") or "unknown")
            stadium_id = str(game.get("stadium_id") or "")
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
            base = {
                "game_id": game_id,
                "kickoff": game_kickoff(game).isoformat() if game_kickoff(game) else None,
                "home_team": game.get("home_team"),
                "away_team": game.get("away_team"),
                "stadium": game.get("stadium") or location.get("stadium_name"),
                "stadium_id": stadium_id,
                "roof": game.get("roof"),
                "roof_uncertain": not bool(str(game.get("roof") or "").strip()),
                "stadium_roof_type": location.get("roof_type"),
                "location": {
                    "latitude": latitude,
                    "longitude": longitude,
                    "city": location.get("city"),
                    "state": location.get("state"),
                },
            }
            async with semaphore:
                try:
                    cached_point = point_cache.get(stadium_id)
                    if _point_cache_entry_valid(cached_point, location, observed_at):
                        point = cached_point
                        point_cache_reuses += 1
                    else:
                        point_response = await nws.point(latitude, longitude)
                        response_records.append((point_response, game_id))
                        properties = point_response.payload.get("properties") or {}
                        forecast_hourly = str(properties.get("forecastHourly") or "")
                        if not forecast_hourly:
                            raise WeatherIngestionError("NWS point response omitted forecastHourly")
                        point = {
                            "latitude": latitude,
                            "longitude": longitude,
                            "forecast_hourly": forecast_hourly,
                            "grid_id": properties.get("gridId"),
                            "grid_x": properties.get("gridX"),
                            "grid_y": properties.get("gridY"),
                            "refreshed_at": observed_at.isoformat(),
                        }
                        point_cache[stadium_id] = point
                        point_cache_refreshes += 1
                    hourly_response = await nws.hourly(str(point["forecast_hourly"]))
                    response_records.append((hourly_response, game_id))
                    periods = (hourly_response.payload.get("properties") or {}).get("periods") or []
                    kickoff = game_kickoff(game)
                    forecast = (
                        select_kickoff_forecast(periods, kickoff)
                        if kickoff and isinstance(periods, list)
                        else None
                    )
                    alert_error = None
                    alerts: list[dict[str, Any]] = []
                    try:
                        alerts_response = await nws.alerts(latitude, longitude)
                        response_records.append((alerts_response, game_id))
                        alerts = compact_alerts(alerts_response.payload, kickoff=kickoff)
                    except Exception as exc:
                        alert_error = str(exc)[:300]
                        errors.append(f"{game_id} alerts: {alert_error}")
                    if not forecast:
                        errors.append(f"{game_id}: no hourly forecast covers kickoff")
                    return {
                        **base,
                        "coverage_status": (
                            "forecast_available" if forecast else "kickoff_forecast_unavailable"
                        ),
                        "forecast": forecast,
                        "alerts": alerts,
                        "alerts_status": "active" if alert_error is None else "unavailable",
                        "alerts_error": alert_error,
                        "risk": weather_risk(forecast, alerts),
                    }
                except Exception as exc:
                    error = str(exc)[:500]
                    errors.append(f"{game_id}: {error}")
                    return {
                        **base,
                        "coverage_status": "unavailable",
                        "forecast": None,
                        "alerts": [],
                        "alerts_status": "unavailable",
                        "error": error,
                        "risk": {"level": "unknown", "factors": ["weather feed unavailable"]},
                    }

        games = await asyncio.gather(*(fetch_game(game, location) for game, location in eligible))

    _write_json_atomic(
        point_cache_path,
        {"generated_at": observed_at.isoformat(), "entries": point_cache},
    )
    forecast_count = sum(game.get("forecast") is not None for game in games)
    risk_counts = {
        level: sum((game.get("risk") or {}).get("level") == level for game in games)
        for level in ("high", "moderate", "low", "unknown")
    }
    report = {
        "generated_at": observed_at.isoformat(),
        "provider": "National Weather Service",
        "season": nflverse.get("season"),
        "week": nflverse.get("week"),
        "location_source": location_source,
        "coverage": {
            "scheduled_game_count": len(schedule),
            "eligible_game_count": len(eligible),
            "forecast_count": forecast_count,
            "uncovered_game_count": len(uncovered) + len(eligible) - forecast_count,
            "skipped_indoor_game_count": len(indoor_games),
            "point_cache_refreshes": point_cache_refreshes,
            "point_cache_reuses": point_cache_reuses,
            "risk_counts": risk_counts,
        },
        "games": games,
        "uncovered_games": uncovered,
        "skipped_indoor_games": indoor_games,
        "errors": errors,
        "weather_digest": content_hash(
            [
                {
                    "game_id": game.get("game_id"),
                    "forecast": game.get("forecast"),
                    "alerts": game.get("alerts"),
                    "risk": game.get("risk"),
                }
                for game in games
            ]
        ),
    }
    _write_json_atomic(cfg.reports_dir / "weather-context.json", report)

    latencies = [response.latency_ms for response, _ in response_records]
    fully_healthy = forecast_count == len(eligible) and not errors
    partial_success = forecast_count > 0 and not fully_healthy
    async with session_scope() as session:
        if stadium_response:
            await record_observation(
                session,
                provider="greerreNFL Stadiums",
                endpoint=stadium_response.endpoint,
                entity_type="stadium_locations",
                entity_id="nfl_stadiums",
                payload=stadium_response.locations,
                normalized={
                    "location_count": len(stadium_response.locations),
                    "retrieved_at": observed_at.isoformat(),
                    "last_modified": stadium_response.last_modified,
                },
                confidence=0.8,
            )
            await update_source_health(
                session,
                "greerreNFL Stadiums",
                success=True,
                latency_ms=stadium_response.latency_ms,
                digest=content_hash(stadium_response.locations),
            )
        for response, game_id in response_records:
            await record_observation(
                session,
                provider="National Weather Service",
                endpoint=response.endpoint,
                entity_type="game_weather",
                entity_id=game_id,
                payload=response.payload,
                normalized={"season": nflverse.get("season"), "week": nflverse.get("week")},
                confidence=0.95,
            )
        health = await update_source_health(
            session,
            "National Weather Service",
            success=fully_healthy,
            latency_ms=max(latencies, default=None),
            digest=report["weather_digest"] if forecast_count else None,
            error="; ".join(errors)[:1500] if errors else None,
        )
        if partial_success:
            health.status = "degraded"
            health.last_success_at = observed_at
            health.last_content_hash = report["weather_digest"]

    return report
