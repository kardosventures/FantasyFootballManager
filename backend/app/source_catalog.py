from __future__ import annotations

from typing import Any

from app.config import Settings

CHAMPIONSHIP_OBJECTIVE = (
    "Maximize Jim.ai's probability of winning the league championship by managing the entire "
    "roster across weekly scoring, waivers, injuries, byes, opponent behavior, and the playoffs."
)


def build_source_catalog(settings: Settings) -> dict[str, Any]:
    """Describe the decision-grade source stack and how quickly each source must refresh."""
    fantasypros_configured = settings.fantasypros_api_key is not None
    return {
        "objective": CHAMPIONSHIP_OBJECTIVE,
        "strategy": "free_first_with_optional_decision_grade_projection_feed",
        "sources": [
            {
                "id": "sleeper",
                "provider": "Sleeper public API",
                "role": "authoritative_league_state",
                "required": True,
                "configured": True,
                "implemented": True,
                "operational_state": "active",
                "delivery": "adaptive_polling",
                "normal_cadence_seconds": 300,
                "critical_cadence_seconds": 30,
                "data": [
                    "league settings",
                    "all rosters and starters",
                    "matchups and scores",
                    "transactions and waiver results",
                    "player status metadata",
                    "trending adds and drops",
                ],
            },
            {
                "id": "fantasypros",
                "provider": "FantasyPros API",
                "role": "targeted_independent_weekly_projection_signal",
                "required": False,
                "configured": fantasypros_configured,
                "implemented": True,
                "operational_state": "active" if fantasypros_configured else "awaiting_key",
                "delivery": "adaptive_polling",
                "normal_cadence_seconds": max(settings.fantasypros_sync_minutes * 60, 10800),
                "critical_cadence_seconds": 3600,
                "data": [
                    "position-scoped weekly projections for our mapped roster",
                    "rotating current-opponent projection coverage",
                    "half-PPR points and underlying projected statistics",
                    "past-week access checks for diagnostic backtests when responses lack a "
                    "provider as-of timestamp",
                ],
                "configuration": "FANTASYPROS_API_KEY",
                "quota_policy": {
                    "rolling_24_hour_limit": settings.fantasypros_daily_request_limit,
                    "reserved_for_manual_emergencies": settings.fantasypros_request_reserve,
                    "requests_per_scheduled_sync": settings.fantasypros_requests_per_sync,
                    "scheduled_scope": (
                        "our mapped roster first, with opponent overflow rotated by position"
                    ),
                },
                "licensing_constraint": (
                    "The configured personal API tier is used only within its declared request "
                    "and response limits"
                ),
            },
            {
                "id": "sleeper_ui",
                "provider": "Sleeper authenticated UI",
                "role": "supplemental_roster_free_agent_and_matchup_projection_evidence",
                "required": False,
                "configured": True,
                "implemented": True,
                "operational_state": "active_read_only",
                "delivery": "browser_observation",
                "normal_cadence_seconds": 60,
                "critical_cadence_seconds": 30,
                "data": [
                    "current roster projections",
                    "league-filtered free-agent projections and projected stat lines",
                    "opponent projections, matchup totals, and live win percentages",
                    "start and ownership rates",
                    "visible injury designations",
                    "semantic lineup controls",
                ],
                "constraint": "supplemental evidence; public Sleeper API remains roster authority",
            },
            {
                "id": "nflverse",
                "provider": "nflverse",
                "role": "open_usage_and_performance_grounding",
                "required": True,
                "configured": True,
                "implemented": True,
                "operational_state": "active",
                "delivery": "batch_polling",
                "normal_cadence_seconds": settings.nflverse_sync_hours * 3600,
                "critical_cadence_seconds": 3600,
                "data": [
                    "NFL schedule and market lines",
                    "official injury-report fields",
                    "depth charts",
                    "snap counts",
                    "weekly player statistics",
                    "league-scored historical offense and kicker outcomes",
                    "estimated historical team D/ST outcomes",
                    "compressed completed-season backtest archive from 2021 onward",
                    "stable cross-provider player identifiers",
                ],
            },
            {
                "id": "nws",
                "provider": "National Weather Service",
                "role": "outdoor_game_weather",
                "required": False,
                "configured": bool(settings.nws_user_agent),
                "implemented": True,
                "operational_state": "active" if settings.nws_user_agent else "not_configured",
                "delivery": "adaptive_polling",
                "normal_cadence_seconds": 21600,
                "critical_cadence_seconds": 900,
                "data": ["forecast", "wind", "precipitation", "temperature", "alerts"],
                "coordinate_dependency": {
                    "provider": "greerreNFL Stadiums",
                    "delivery": "cached_monthly",
                    "role": "nflverse stadium_id to latitude/longitude crosswalk",
                },
            },
            {
                "id": "sportsdataio",
                "provider": "SportsDataIO",
                "role": "optional_low_latency_upgrade",
                "required": False,
                "configured": False,
                "implemented": False,
                "operational_state": "optional_upgrade",
                "delivery": "paid_polling",
                "normal_cadence_seconds": 3600,
                "critical_cadence_seconds": 900,
                "data": [
                    "15-minute projection updates",
                    "injuries and gameday inactives",
                    "depth charts",
                    "player news",
                ],
            },
            {
                "id": "sportradar",
                "provider": "Sportradar",
                "role": "optional_enterprise_realtime_upgrade",
                "required": False,
                "configured": False,
                "implemented": False,
                "operational_state": "optional_upgrade",
                "delivery": "paid_rest_and_push",
                "normal_cadence_seconds": 3600,
                "critical_cadence_seconds": 60,
                "data": ["live game events", "statistics", "injuries", "depth charts"],
            },
            {
                "id": "odds",
                "provider": "The Odds API",
                "role": "optional_market_expectation_signal",
                "required": False,
                "configured": False,
                "implemented": False,
                "operational_state": "optional_upgrade",
                "delivery": "paid_polling",
                "normal_cadence_seconds": 21600,
                "critical_cadence_seconds": 1800,
                "data": ["game totals", "spreads", "player props"],
            },
            {
                "id": "codex_web",
                "provider": "Codex web research",
                "role": "triggered_dispute_resolution",
                "required": True,
                "configured": True,
                "implemented": True,
                "operational_state": "active",
                "delivery": "event_triggered",
                "normal_cadence_seconds": None,
                "critical_cadence_seconds": None,
                "data": [
                    "late breaking context",
                    "conflicting injury reports",
                    "coach statements and role changes",
                ],
                "constraint": "contextual evidence only; never authoritative for roster state",
            },
        ],
    }
