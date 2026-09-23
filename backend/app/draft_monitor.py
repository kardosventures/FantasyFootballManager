from __future__ import annotations

import argparse
import asyncio
import signal
from typing import Any

import structlog

from app.config import Settings, get_settings
from app.draft_room import build_draft_room

logger = structlog.get_logger()


async def run_draft_monitor(
    settings: Settings | None = None, *, draft_id: str | None = None, once: bool = False
) -> dict[str, Any]:
    cfg = settings or get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if not once:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    last_pick_count: int | None = None
    latest: dict[str, Any] = {}
    while not stop.is_set():
        try:
            latest = await build_draft_room(
                cfg,
                draft_id=draft_id,
                persist_observations=False,
                force_static_refresh=False,
            )
            pick_count = int(latest.get("pick_count") or 0)
            if pick_count != last_pick_count:
                candidate = (latest.get("recommendation") or {}).get("recommendation") or {}
                logger.info(
                    "draft_board_updated",
                    draft_id=latest.get("draft_id"),
                    pick_count=pick_count,
                    on_clock=latest.get("on_clock"),
                    next_owner_pick=latest.get("next_owner_pick"),
                    recommendation=candidate.get("player_name"),
                )
                last_pick_count = pick_count
        except Exception as exc:
            logger.exception("draft_monitor_poll_failed", error=str(exc))
        if once:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.draft_poll_seconds)
        except TimeoutError:
            pass
    return latest


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Sleeper draft monitor and auto-pick queue")
    parser.add_argument("--draft-id", help="Optional mock or explicit Sleeper draft ID")
    parser.add_argument("--once", action="store_true", help="Generate one live board and exit")
    args = parser.parse_args()
    asyncio.run(run_draft_monitor(draft_id=args.draft_id, once=args.once))


if __name__ == "__main__":
    main()
