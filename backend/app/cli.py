from __future__ import annotations

import argparse
import asyncio
import json

from app.bootstrap import bootstrap_live, probe_live, sync_rankings
from app.championship_backtest import write_championship_backtest
from app.config import get_settings
from app.draft_room import build_draft_room
from app.historical_sources import run_historical_backfill
from app.in_season_expert import build_in_season_plan
from app.in_season_sources import (
    sync_fantasypros_context,
    sync_nflverse_context,
    sync_sleeper_in_season,
    sync_sleeper_season_context,
    write_source_catalog,
)
from app.projection_backtest import write_projection_backtest
from app.readiness import build_next_14_days, read_report
from app.weather import sync_weather_context


async def run(command: str) -> dict:
    settings = get_settings()
    if command == "bootstrap":
        return await bootstrap_live(settings)
    if command == "probe":
        return await probe_live(settings)
    if command == "rankings":
        return await sync_rankings(settings)
    if command == "in-season":
        sleeper = await sync_sleeper_in_season(settings)
        season_context = await sync_sleeper_season_context(settings)
        nflverse = await sync_nflverse_context(settings)
        weather = await sync_weather_context(settings)
        fantasypros = await sync_fantasypros_context(settings)
        return {
            "sources": write_source_catalog(settings),
            "sleeper": sleeper,
            "season_context": season_context,
            "nflverse": nflverse,
            "weather": weather,
            "fantasypros": fantasypros,
        }
    if command == "manager":
        return await build_in_season_plan(settings)
    if command == "fantasypros":
        return await sync_fantasypros_context(settings)
    if command == "historical-backfill":
        return await run_historical_backfill(settings)
    if command == "projection-backtest":
        return write_projection_backtest(settings)
    if command == "championship-backtest":
        return write_championship_backtest(settings)
    if command in {"draft-room", "replay-draft"}:
        return await build_draft_room(settings)
    if command == "calendar":
        readiness = read_report(settings.reports_dir / "readiness.json", {})
        normalized = readiness.get("normalized", {})
        return build_next_14_days(
            normalized,
            settings.app_timezone,
            waiver_weekday=settings.waiver_process_weekday,
            waiver_hour=settings.waiver_process_hour,
            waiver_semantics_confirmed=settings.waiver_semantics_confirmed,
        )
    if command == "replay-events":
        return {
            "status": "Replay uses immutable source_observations; no external writes are permitted."
        }
    raise ValueError(f"Unknown command: {command}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "bootstrap",
            "probe",
            "rankings",
            "in-season",
            "manager",
            "fantasypros",
            "historical-backfill",
            "projection-backtest",
            "championship-backtest",
            "draft-room",
            "calendar",
            "replay-draft",
            "replay-events",
        ],
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.command)), indent=2, default=str))


if __name__ == "__main__":
    main()
