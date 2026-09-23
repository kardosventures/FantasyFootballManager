from datetime import datetime, timedelta, timezone

from app.freshness import destructive_confidence_allowed, freshness_status
from app.scheduler import (
    expert_refresh_seconds,
    expert_retry_seconds,
    fantasypros_refresh_seconds,
    nflverse_refresh_seconds,
    weather_refresh_seconds,
)
from app.scheduler_profiles import select_profile
from app.waivers import next_weekly_waiver


def test_freshness_fail_closed():
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    assert freshness_status(now - timedelta(minutes=5), timedelta(minutes=10), now=now) == "fresh"
    assert freshness_status(now - timedelta(minutes=15), timedelta(minutes=10), now=now) == "stale"
    assert not destructive_confidence_allowed(["fresh", "stale"])


def test_waiver_time_survives_dst_transition():
    now = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)
    waiver = next_weekly_waiver(now=now, weekday=2, hour=1)
    assert waiver.tzinfo is not None
    assert waiver.weekday() == 2
    assert waiver.hour == 1


def test_in_season_scheduler_accelerates_near_kickoff_and_playoffs():
    assert (
        select_profile(league_status="in_season", seconds_to_next_kickoff=90 * 60).name == "kickoff"
    )
    assert (
        select_profile(league_status="in_season", seconds_to_next_kickoff=12 * 60 * 60).name
        == "game_day"
    )
    assert (
        select_profile(league_status="in_season", current_week=15, playoff_start_week=15).name
        == "playoffs"
    )


def test_evidence_and_expert_refreshes_accelerate_in_critical_windows():
    assert expert_refresh_seconds(60, "normal") == 3600
    assert expert_refresh_seconds(60, "waiver_window") == 3600
    assert expert_refresh_seconds(60, "game_day") == 600
    assert expert_refresh_seconds(60, "kickoff") == 300
    assert expert_refresh_seconds(15, "kickoff") == 300


def test_in_season_scheduler_retries_local_worker_failure_in_five_minutes():
    assert (
        expert_retry_seconds(
            {
                "manager_state": "blocked",
                "blockers": ["The local Codex worker exceeded the decision timeout"],
            },
            3600,
        )
        == 300
    )
    assert expert_retry_seconds({"manager_state": "ready"}, 3600) == 3600
    assert (
        expert_retry_seconds(
            {"manager_state": "blocked", "blockers": ["Sleeper state is unavailable"]},
            3600,
        )
        == 3600
    )
    assert fantasypros_refresh_seconds(60, "normal") == 10800
    assert fantasypros_refresh_seconds(60, "game_day") == 7200
    assert fantasypros_refresh_seconds(60, "kickoff") == 3600
    assert nflverse_refresh_seconds(4, "kickoff") == 3600
    assert weather_refresh_seconds("normal") == 21600
    assert weather_refresh_seconds("game_day") == 900
    assert weather_refresh_seconds("kickoff") == 900
    assert weather_refresh_seconds("playoffs") == 3600
