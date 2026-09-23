from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from app.acquisition_lifecycle import reconcile_acquisition_lifecycle
from app.bootstrap import bootstrap_live, sync_draft_picks, sync_rankings
from app.championship_backtest import write_championship_backtest
from app.config import get_settings
from app.database import session_scope
from app.draft_room import build_draft_room
from app.in_season_expert import build_in_season_plan
from app.in_season_sources import (
    sync_fantasypros_context,
    sync_nflverse_context,
    sync_sleeper_in_season,
    sync_sleeper_season_context,
    write_source_catalog,
)
from app.projection_backtest import write_projection_backtest
from app.readiness import read_report
from app.repositories import (
    resolve_operational_incident,
    update_dataset_health,
    update_source_health,
    upsert_operational_incident,
)
from app.scheduler_profiles import select_profile
from app.trash_talk import evaluate_trash_talk
from app.waivers import next_weekly_waiver
from app.weather import sync_weather_context

logger = structlog.get_logger()
EASTERN = ZoneInfo("America/New_York")


def expert_refresh_seconds(configured_minutes: int, profile_name: str) -> int:
    normal = configured_minutes * 60
    critical = {
        # Waiver evidence still refreshes frequently, but a full model decision is
        # expensive and rarely changes every 15 minutes. Keep the configured
        # cadence during this window (hourly by default).
        "waiver_window": normal,
        "game_day": 10 * 60,
        "kickoff": 5 * 60,
        "playoffs": 30 * 60,
        "degraded": 15 * 60,
    }
    return min(normal, critical.get(profile_name, normal))


def expert_retry_seconds(result: object | None, normal_seconds: int) -> int:
    """Retry transient local-worker failures without creating a tight request loop."""
    if not isinstance(result, dict) or result.get("manager_state") != "blocked":
        return normal_seconds
    blockers = " ".join(str(item) for item in result.get("blockers") or []).lower()
    if "codex worker" in blockers or "codex response" in blockers:
        return min(normal_seconds, 5 * 60)
    return normal_seconds


def fantasypros_refresh_seconds(configured_minutes: int, profile_name: str) -> int:
    normal = max(configured_minutes * 60, 3 * 60 * 60)
    critical = {
        "waiver_window": 3 * 60 * 60,
        "game_day": 2 * 60 * 60,
        "kickoff": 60 * 60,
        "playoffs": 2 * 60 * 60,
        "degraded": 3 * 60 * 60,
    }
    return min(normal, critical.get(profile_name, normal))


def nflverse_refresh_seconds(configured_hours: int, profile_name: str) -> int:
    normal = configured_hours * 60 * 60
    critical = (
        5 * 60
        if profile_name == "degraded"
        else 60 * 60
        if profile_name in {"game_day", "kickoff", "playoffs"}
        else normal
    )
    return min(normal, critical)


def weather_refresh_seconds(profile_name: str) -> int:
    return {
        "game_day": 15 * 60,
        "kickoff": 15 * 60,
        "playoffs": 60 * 60,
    }.get(profile_name, 6 * 60 * 60)


def _next_kickoff_seconds(schedule: list[dict[str, Any]], now: datetime) -> float | None:
    upcoming: list[float] = []
    for game in schedule:
        gameday = str(game.get("gameday") or "")
        gametime = str(game.get("gametime") or "")
        if not gameday or not gametime:
            continue
        try:
            kickoff = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M").replace(
                tzinfo=EASTERN
            )
        except ValueError:
            continue
        seconds = (kickoff.astimezone(timezone.utc) - now).total_seconds()
        if seconds >= 0:
            upcoming.append(seconds)
    return min(upcoming, default=None)


async def _safe(
    name: str,
    operation: Callable[[], Awaitable[object]],
    *,
    health_targets: tuple[tuple[str, str], ...] = (),
) -> object | None:
    try:
        result = await operation()
        if health_targets:
            async with session_scope() as session:
                for provider, dataset in health_targets:
                    await update_dataset_health(
                        session,
                        provider,
                        dataset,
                        success=True,
                    )
                    await resolve_operational_incident(
                        session,
                        dedupe_key=f"source:{provider}:{dataset}",
                    )
        logger.info("scheduled_operation_completed", operation=name)
        return result
    except Exception as exc:
        if health_targets:
            try:
                async with session_scope() as session:
                    for provider, dataset in health_targets:
                        await update_source_health(
                            session,
                            provider,
                            success=False,
                            error=f"{name}: {exc}",
                        )
                        await update_dataset_health(
                            session,
                            provider,
                            dataset,
                            success=False,
                            error=f"{name}: {exc}",
                        )
                        await upsert_operational_incident(
                            session,
                            category="source_failure",
                            severity="critical" if provider == "Sleeper public API" else "warning",
                            summary=f"{provider} {dataset} refresh failed",
                            dedupe_key=f"source:{provider}:{dataset}",
                            details={"operation": name, "error": str(exc)[:2000]},
                        )
            except Exception as health_exc:
                logger.exception(
                    "source_failure_recording_failed",
                    operation=name,
                    error=str(health_exc),
                )
        logger.exception("scheduled_operation_failed", operation=name, error=str(exc))
        return None


async def run_scheduler() -> None:
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    due = {
        "league": datetime.min.replace(tzinfo=timezone.utc),
        "picks": datetime.min.replace(tzinfo=timezone.utc),
        "rankings": datetime.min.replace(tzinfo=timezone.utc),
        "draft_room": datetime.min.replace(tzinfo=timezone.utc),
        "in_season": datetime.min.replace(tzinfo=timezone.utc),
        "season_context": datetime.min.replace(tzinfo=timezone.utc),
        "nflverse": datetime.min.replace(tzinfo=timezone.utc),
        "weather": datetime.min.replace(tzinfo=timezone.utc),
        "fantasypros": datetime.min.replace(tzinfo=timezone.utc),
        "source_catalog": datetime.min.replace(tzinfo=timezone.utc),
        "in_season_expert": datetime.min.replace(tzinfo=timezone.utc),
        "projection_backtest": datetime.min.replace(tzinfo=timezone.utc),
        "championship_backtest": datetime.min.replace(tzinfo=timezone.utc),
        "acquisition_lifecycle": datetime.min.replace(tzinfo=timezone.utc),
        "trash_talk": datetime.min.replace(tzinfo=timezone.utc),
    }
    while not stop.is_set():
        now = datetime.now(timezone.utc)
        readiness = read_report(settings.reports_dir / "readiness.json", {})
        league_status = (readiness.get("league") or {}).get("status") or "pre_draft"
        in_season_context = read_report(settings.reports_dir / "in-season-context.json", {})
        nflverse_context = read_report(settings.reports_dir / "nflverse-context.json", {})
        manager_plan = read_report(settings.reports_dir / "in-season-plan.json", {})
        source_degraded = ((manager_plan.get("data_quality") or {}).get("evidence_gate") or {}).get(
            "status"
        ) == "blocked"
        league_settings = (in_season_context.get("league") or {}).get("settings") or {}
        waiver_window = False
        if settings.waiver_semantics_confirmed:
            next_waiver = next_weekly_waiver(
                now=now,
                weekday=settings.waiver_process_weekday,
                hour=settings.waiver_process_hour,
                timezone_name=settings.app_timezone,
            )
            waiver_window = 0 <= (next_waiver - now).total_seconds() <= 6 * 60 * 60
        seconds_to_next_kickoff = _next_kickoff_seconds(
            nflverse_context.get("week_schedule") or [], now
        )
        profile = select_profile(
            league_status=league_status,
            seconds_to_next_kickoff=seconds_to_next_kickoff,
            current_week=int(in_season_context.get("week") or 0),
            playoff_start_week=int(league_settings.get("playoff_week_start") or 0),
            waiver_window=waiver_window,
            source_degraded=source_degraded,
        )
        expert_seconds = expert_refresh_seconds(settings.in_season_expert_minutes, profile.name)
        fantasypros_seconds = fantasypros_refresh_seconds(
            settings.fantasypros_sync_minutes, profile.name
        )
        nflverse_seconds = nflverse_refresh_seconds(settings.nflverse_sync_hours, profile.name)
        weather_seconds = weather_refresh_seconds(profile.name)
        for key, interval in (
            ("in_season_expert", expert_seconds),
            ("fantasypros", fantasypros_seconds),
            ("nflverse", nflverse_seconds),
            ("weather", weather_seconds),
        ):
            if due[key] > now + timedelta(seconds=interval):
                due[key] = now
        if now >= due["league"]:
            await _safe("bootstrap", lambda: bootstrap_live(settings))
            due["league"] = now + timedelta(seconds=profile.league_seconds)
        if profile.picks_seconds and now >= due["picks"]:
            await _safe("draft-picks", lambda: sync_draft_picks(settings))
            due["picks"] = now + timedelta(seconds=profile.picks_seconds)
        if now >= due["rankings"]:
            await _safe("rankings", lambda: sync_rankings(settings))
            due["rankings"] = now + timedelta(hours=24)
        if now >= due["draft_room"]:
            await _safe("draft-room", lambda: build_draft_room(settings))
            due["draft_room"] = now + timedelta(minutes=30 if league_status != "drafting" else 5)
        if now >= due["source_catalog"]:
            write_source_catalog(settings)
            due["source_catalog"] = now + timedelta(hours=24)
        if (
            settings.in_season_sync_enabled
            and league_status == "in_season"
            and now >= due["in_season"]
        ):
            await _safe(
                "sleeper-in-season",
                lambda: sync_sleeper_in_season(settings),
                health_targets=(("Sleeper public API", "in_season_bundle"),),
            )
            due["in_season"] = now + timedelta(
                seconds=min(profile.transactions_seconds, profile.matchups_seconds)
            )
        if league_status == "in_season" and now >= due["acquisition_lifecycle"]:
            lifecycle = await _safe(
                "acquisition-lifecycle",
                lambda: reconcile_acquisition_lifecycle(settings),
            )
            if isinstance(lifecycle, dict) and lifecycle.get("requires_manager_refresh"):
                due["in_season_expert"] = now
            due["acquisition_lifecycle"] = now + timedelta(minutes=1)
        if (
            settings.in_season_sync_enabled
            and league_status == "in_season"
            and now >= due["nflverse"]
        ):
            await _safe(
                "nflverse-context",
                lambda: sync_nflverse_context(settings),
                health_targets=(("nflverse", "context_bundle"),),
            )
            due["nflverse"] = now + timedelta(seconds=nflverse_seconds)
        if (
            settings.in_season_sync_enabled
            and league_status == "in_season"
            and now >= due["season_context"]
        ):
            await _safe(
                "sleeper-season-context",
                lambda: sync_sleeper_season_context(settings),
                health_targets=(("Sleeper public API", "season_schedule"),),
            )
            due["season_context"] = now + timedelta(hours=6)
        if (
            settings.in_season_sync_enabled
            and league_status == "in_season"
            and now >= due["projection_backtest"]
        ):
            await _safe(
                "projection-backtest",
                lambda: asyncio.to_thread(write_projection_backtest, settings),
            )
            due["projection_backtest"] = now + timedelta(hours=24)
        if (
            settings.in_season_sync_enabled
            and league_status == "in_season"
            and now >= due["championship_backtest"]
        ):
            await _safe(
                "championship-backtest",
                lambda: asyncio.to_thread(write_championship_backtest, settings),
            )
            due["championship_backtest"] = now + timedelta(hours=24)
        if (
            settings.in_season_sync_enabled
            and league_status == "in_season"
            and now >= due["weather"]
        ):
            await _safe(
                "weather-context",
                lambda: sync_weather_context(settings),
                health_targets=(("National Weather Service", "weather_context"),),
            )
            due["weather"] = now + timedelta(seconds=weather_seconds)
        if (
            settings.in_season_sync_enabled
            and settings.fantasypros_api_key is not None
            and league_status == "in_season"
            and now >= due["fantasypros"]
        ):
            await _safe(
                "fantasypros-context",
                lambda: sync_fantasypros_context(settings),
                health_targets=(("FantasyPros API", "context_bundle"),),
            )
            due["fantasypros"] = now + timedelta(seconds=fantasypros_seconds)
        if (
            settings.in_season_expert_enabled
            and league_status == "in_season"
            and now >= due["in_season_expert"]
        ):
            expert_result = await _safe(
                "in-season-expert", lambda: build_in_season_plan(settings)
            )
            due["in_season_expert"] = now + timedelta(
                seconds=expert_retry_seconds(expert_result, expert_seconds)
            )
        if (
            settings.trash_talk_enabled
            and league_status == "in_season"
            and now >= due["trash_talk"]
        ):
            await _safe("trash-talk", lambda: evaluate_trash_talk(settings))
            due["trash_talk"] = now + timedelta(
                minutes=5 if profile.name in {"game_day", "kickoff"} else 30
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(run_scheduler())
