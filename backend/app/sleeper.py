from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.config import Settings, get_settings


class SleeperClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class SleeperResponse:
    endpoint: str
    payload: dict[str, Any] | list[Any]
    latency_ms: float


class SleeperClient:
    """Typed, GET-only client for documented public Sleeper endpoints."""

    def __init__(
        self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.sleeper_base_url + "/",
            timeout=httpx.Timeout(20.0, connect=5.0),
            transport=transport,
            headers={"User-Agent": "fantasy-operations-monitor/0.1"},
            follow_redirects=False,
        )

    async def __aenter__(self) -> SleeperClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self, endpoint: str, *, request_timeout_seconds: float | None = None
    ) -> SleeperResponse:
        endpoint = endpoint.lstrip("/")
        candidate = urljoin(self.settings.sleeper_base_url + "/", endpoint)
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.hostname != "api.sleeper.app":
            raise SleeperClientError("Blocked request outside documented Sleeper public hostname")
        if not parsed.path.startswith("/v1/"):
            raise SleeperClientError("Blocked request outside documented Sleeper v1 path")

        last_error: Exception | None = None
        for attempt in range(4):
            started = time.perf_counter()
            try:
                response = await self._client.get(endpoint, timeout=request_timeout_seconds)
                latency_ms = (time.perf_counter() - started) * 1000
                if response.status_code == 429:
                    retry_after = min(float(response.headers.get("retry-after", "1")), 10.0)
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, (dict, list)):
                    raise SleeperClientError(f"Unexpected payload type for {endpoint}")
                return SleeperResponse(endpoint=endpoint, payload=payload, latency_ms=latency_ms)
            except (httpx.HTTPError, ValueError, SleeperClientError) as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(0.25 * (2**attempt))
        raise SleeperClientError(f"Sleeper GET failed for {endpoint}: {last_error}") from last_error

    async def league(self, league_id: str) -> SleeperResponse:
        return await self._get(f"league/{league_id}")

    async def users(self, league_id: str) -> SleeperResponse:
        return await self._get(f"league/{league_id}/users")

    async def rosters(self, league_id: str) -> SleeperResponse:
        return await self._get(f"league/{league_id}/rosters")

    async def drafts(self, league_id: str) -> SleeperResponse:
        return await self._get(f"league/{league_id}/drafts")

    async def draft(self, draft_id: str) -> SleeperResponse:
        return await self._get(f"draft/{draft_id}")

    async def draft_picks(self, draft_id: str) -> SleeperResponse:
        # Sleeper/CDN responses may otherwise lag behind the active browser room.
        return await self._get(f"draft/{draft_id}/picks?fresh={time.time_ns()}")

    async def nfl_state(self) -> SleeperResponse:
        return await self._get("state/nfl")

    async def matchups(self, league_id: str, week: int) -> SleeperResponse:
        return await self._get(f"league/{league_id}/matchups/{week}")

    async def transactions(self, league_id: str, week: int) -> SleeperResponse:
        return await self._get(f"league/{league_id}/transactions/{week}")

    async def winners_bracket(self, league_id: str) -> SleeperResponse:
        return await self._get(f"league/{league_id}/winners_bracket")

    async def losers_bracket(self, league_id: str) -> SleeperResponse:
        return await self._get(f"league/{league_id}/losers_bracket")

    async def players(self) -> SleeperResponse:
        # The public NFL catalog is large enough to exceed the normal live-poll timeout.
        return await self._get("players/nfl", request_timeout_seconds=120.0)

    async def trending(self, trend_type: str = "add", lookback_hours: int = 24) -> SleeperResponse:
        if trend_type not in {"add", "drop"}:
            raise ValueError("trend_type must be add or drop")
        return await self._get(
            f"players/nfl/trending/{trend_type}?lookback_hours={lookback_hours}&limit=100"
        )
