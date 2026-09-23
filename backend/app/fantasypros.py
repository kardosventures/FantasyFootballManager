from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.config import Settings, get_settings


class FantasyProsClientError(RuntimeError):
    pass


class FantasyProsQuotaExceeded(FantasyProsClientError):
    pass


@dataclass(frozen=True)
class FantasyProsResponse:
    endpoint: str
    payload: dict[str, Any] | list[Any]
    latency_ms: float
    request_params: dict[str, Any]
    quota: dict[str, Any]


def _quota_paths(settings: Settings) -> tuple[Path, Path]:
    ledger = Path(settings.reports_dir) / "fantasypros-quota.json"
    return ledger, ledger.with_suffix(".lock")


def _quota_payload(settings: Settings, timestamps: list[datetime], now: datetime) -> dict[str, Any]:
    usable_limit = max(
        settings.fantasypros_daily_request_limit - settings.fantasypros_request_reserve, 0
    )
    return {
        "updated_at": now.isoformat(),
        "window": "rolling_24_hours",
        "daily_limit": settings.fantasypros_daily_request_limit,
        "reserved_requests": settings.fantasypros_request_reserve,
        "usable_limit": usable_limit,
        "used": len(timestamps),
        "standard_remaining": max(usable_limit - len(timestamps), 0),
        "total_remaining": max(settings.fantasypros_daily_request_limit - len(timestamps), 0),
        "request_timestamps": [value.isoformat() for value in timestamps],
    }


def _read_timestamps(path: Path, now: datetime) -> list[datetime]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    cutoff = now - timedelta(hours=24)
    result = []
    for raw in payload.get("request_timestamps") or []:
        try:
            observed = datetime.fromisoformat(str(raw))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            observed = observed.astimezone(timezone.utc)
        except ValueError:
            continue
        if cutoff < observed <= now + timedelta(seconds=10):
            result.append(observed)
    return sorted(result)


def _write_quota(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def fantasypros_quota_status(settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ledger, lock_path = _quota_paths(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        timestamps = _read_timestamps(ledger, observed_at)
        payload = _quota_payload(settings, timestamps, observed_at)
        _write_quota(ledger, payload)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return {key: value for key, value in payload.items() if key != "request_timestamps"}


def _reserve_quota_request(
    settings: Settings, *, allow_reserve: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ledger, lock_path = _quota_paths(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        timestamps = _read_timestamps(ledger, observed_at)
        threshold = (
            settings.fantasypros_daily_request_limit
            if allow_reserve
            else max(
                settings.fantasypros_daily_request_limit - settings.fantasypros_request_reserve,
                0,
            )
        )
        if len(timestamps) >= threshold:
            payload = _quota_payload(settings, timestamps, observed_at)
            _write_quota(ledger, payload)
            fcntl.flock(lock, fcntl.LOCK_UN)
            raise FantasyProsQuotaExceeded(
                "FantasyPros rolling quota reserve is protected; scheduled request deferred"
            )
        timestamps.append(observed_at)
        payload = _quota_payload(settings, timestamps, observed_at)
        _write_quota(ledger, payload)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return {key: value for key, value in payload.items() if key != "request_timestamps"}


class FantasyProsClient:
    """GET-only client for the documented FantasyPros personal-use API."""

    def __init__(
        self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings or get_settings()
        if self.settings.fantasypros_api_key is None:
            raise FantasyProsClientError("FANTASYPROS_API_KEY is not configured")
        self._client = httpx.AsyncClient(
            base_url=self.settings.fantasypros_base_url + "/",
            timeout=httpx.Timeout(30.0, connect=5.0),
            transport=transport,
            headers={
                "User-Agent": "fantasy-operations-monitor/0.1",
                "x-api-key": self.settings.fantasypros_api_key.get_secret_value(),
            },
            follow_redirects=False,
        )

    async def __aenter__(self) -> FantasyProsClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self, endpoint: str, *, allow_reserve: bool = False, **params: Any
    ) -> FantasyProsResponse:
        endpoint = endpoint.lstrip("/")
        candidate = urljoin(self.settings.fantasypros_base_url + "/", endpoint)
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.hostname != "api.fantasypros.com":
            raise FantasyProsClientError("Blocked request outside FantasyPros")
        if not parsed.path.startswith("/public/v2/json/"):
            raise FantasyProsClientError("Blocked request outside documented FantasyPros API path")
        quota = _reserve_quota_request(self.settings, allow_reserve=allow_reserve)
        started = time.perf_counter()
        response = await self._client.get(endpoint, params=params)
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise FantasyProsClientError(f"Unexpected payload type for {endpoint}")
        return FantasyProsResponse(
            endpoint=endpoint,
            payload=payload,
            latency_ms=latency_ms,
            request_params=params,
            quota=quota,
        )

    async def players(self) -> FantasyProsResponse:
        return await self._get("nfl/players")

    async def news(self) -> FantasyProsResponse:
        return await self._get("nfl/news")

    async def injuries(self, *, season: int, week: int) -> FantasyProsResponse:
        return await self._get("nfl/injuries", season=season, week=week)

    async def projections(
        self,
        *,
        season: int,
        week: int,
        scoring: str = "HALF",
        player_ids: list[str] | None = None,
        position: str | None = None,
        allow_reserve: bool = False,
    ) -> FantasyProsResponse:
        if player_ids and len(player_ids) > 10:
            raise ValueError("The limited FantasyPros tier supports at most 10 targeted players")
        if player_ids and not position:
            raise ValueError(
                "position is required with player_ids because the limited API otherwise defaults to RB"
            )
        params: dict[str, Any] = {"week": week, "scoring": scoring}
        if player_ids:
            params["players"] = ":".join(str(player_id) for player_id in player_ids)
        params["position"] = position or "RB"
        return await self._get(
            f"nfl/{season}/projections",
            allow_reserve=allow_reserve,
            **params,
        )

    async def consensus_rankings(
        self, *, season: int, week: int, ranking_type: str
    ) -> FantasyProsResponse:
        if ranking_type not in {"weekly", "ros"}:
            raise ValueError("ranking_type must be weekly or ros")
        return await self._get(
            f"nfl/{season}/consensus-rankings",
            week=week,
            type=ranking_type,
            scoring="HALF",
        )
