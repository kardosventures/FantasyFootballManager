from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.in_season_execution import (
    acquisition_execution_window,
    lineup_execution_window,
    synchronize_acquisition_actions,
    synchronize_lineup_actions,
)
from app.in_season_expert import (
    InSeasonExpertUnavailable,
    _fresh_authenticated_pending_waiver_claims,
    _fresh_fantasypros_index,
    _pending_waiver_claims,
    _recently_acquired_player_ids,
    build_decision_grounding_context,
    build_manager_context,
    compile_lineup_swaps,
    compile_waiver_actions,
    validate_decision,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _reports(tmp_path: Path) -> Settings:
    league = {
        "league_id": "league",
        "name": "Test league",
        "roster_positions": ["QB", "RB", "FLEX", "BN"],
        "scoring_settings": {"rec": 0.5},
        "settings": {"playoff_teams": 6, "playoff_week_start": 15, "waiver_budget": 100},
    }
    owner = {
        "roster_id": 8,
        "players": ["qb", "rb", "rb2", "bench"],
        "starters": ["qb", "rb", "rb2"],
        "settings": {"waiver_position": 3, "waiver_budget_used": 0},
    }
    opponent = {"roster_id": 2, "players": ["opp"], "starters": ["opp"], "settings": {}}
    _write(
        tmp_path / "in-season-context.json",
        {
            "season": "2026",
            "week": 1,
            "league_status": "in_season",
            "league": league,
            "owner_roster": owner,
            "owner_matchup": {"roster_id": 8, "matchup_id": 1, "points": 0},
            "opponent_matchup": {"roster_id": 2, "matchup_id": 1, "points": 0},
            "rosters": [owner, opponent],
            "matchups": [],
            "trending_adds": [
                {"player_id": "fake", "count": 200},
                {"player_id": "free", "count": 100},
            ],
        },
    )
    players = {
        "qb": {"full_name": "Quarter Back", "position": "QB", "team": "NE", "status": "Active"},
        "rb": {"full_name": "Running Back", "position": "RB", "team": "SEA", "status": "Active"},
        "rb2": {"full_name": "Other Runner", "position": "RB", "team": "SEA", "status": "Active"},
        "bench": {
            "full_name": "Bench Receiver",
            "position": "WR",
            "team": "DEN",
            "status": "Active",
        },
        "opp": {
            "full_name": "Other Quarterback",
            "position": "QB",
            "team": "DEN",
            "status": "Active",
        },
        "free": {"full_name": "Free Runner", "position": "RB", "team": "NE", "status": "Active"},
        "fake": {"full_name": "Free Runner", "position": "CB", "team": "NE", "status": "Active"},
    }
    rankings = [
        {
            "player_name": "Free Runner",
            "normalized_name": "freerunner",
            "overall_rank": 80,
            "position_rank": 29,
            "as_of": "2026-09-04",
            "position": "RB",
            "team": "NE",
        }
    ]
    _write(
        tmp_path / "draft-static-cache.json",
        {
            "players": players,
            "rankings": rankings,
            "ranking_digest": "rank-hash",
            "provenance": {"latest_source_date": "2026-09-04"},
        },
    )
    _write(
        tmp_path / "nflverse-context.json",
        {
            "week_schedule": [
                {
                    "game_id": "game",
                    "gameday": "2026-09-09",
                    "gametime": "20:20",
                    "away_team": "NE",
                    "home_team": "SEA",
                    "total_line": "44.5",
                }
            ],
            "week_injuries": [
                {
                    "gsis_id": "gsis-free",
                    "full_name": "Free Runner",
                    "report_status": "Questionable",
                    "practice_status": "Limited Participation in Practice",
                }
            ],
            "offensive_depth_chart": [
                {
                    "gsis_id": "gsis-free",
                    "player_name": "Free Runner",
                    "pos_abb": "RB",
                    "pos_rank": "2",
                }
            ],
            "player_ids": {"free": {"gsis_id": "gsis-free", "fantasypros_id": "123"}},
        },
    )
    _write(
        tmp_path / "in-season-sources.json",
        {"sources": [{"id": "fantasypros", "operational_state": "awaiting_key"}]},
    )
    _write(
        tmp_path / "weather-context.json",
        {
            "generated_at": "2026-09-09T11:55:00+00:00",
            "season": 2026,
            "week": 1,
            "weather_digest": "weather-hash",
            "coverage": {
                "eligible_game_count": 1,
                "forecast_count": 1,
                "skipped_indoor_game_count": 0,
                "risk_counts": {"low": 1, "moderate": 0, "high": 0, "unknown": 0},
            },
            "games": [
                {
                    "game_id": "game",
                    "coverage_status": "forecast_available",
                    "forecast": {
                        "temperature": 64,
                        "temperature_unit": "F",
                        "wind_speed": "6 mph",
                        "precipitation_probability_percent": 10,
                        "short_forecast": "Mostly Sunny",
                    },
                    "alerts": [],
                    "risk": {"level": "low", "factors": []},
                }
            ],
        },
    )
    _write(
        tmp_path / "browser-lineup.json",
        {
            "generated_at": "2026-09-09T11:59:30+00:00",
            "season": "2026",
            "week": 1,
            "view_type": "weekly_projection",
            "players": [{"player_id": "qb", "projected_points": 19.67}],
        },
    )
    _write(
        tmp_path / "browser-free-agents.json",
        {
            "generated_at": "2026-09-09T11:59:30+00:00",
            "season": "2026",
            "week": 1,
            "view_type": "weekly_projection",
            "players": [
                {
                    "display_name": "F. Runner",
                    "position": "RB",
                    "team": "NE",
                    "acquisition_type": "free_agent",
                    "projected_points": 12.5,
                },
                {
                    "display_name": "Unknown",
                    "position": "RB",
                    "team": "NE",
                    "acquisition_type": "waiver",
                    "projected_points": 9,
                },
            ],
        },
    )
    _write(
        tmp_path / "browser-matchup.json",
        {
            "generated_at": "2026-09-09T11:59:30+00:00",
            "season": "2026",
            "week": 1,
            "view_type": "weekly_projection",
            "our_team": {
                "username": "owner",
                "team_name": "Jim.ai",
                "projected_total": 110.25,
                "win_probability": 55,
                "players": [{"player_id": "qb", "projected_points": 19.5}],
            },
            "opponent": {
                "username": "other",
                "team_name": "Opponent",
                "projected_total": 105.75,
                "win_probability": 45,
                "players": [{"player_id": "opp", "projected_points": 21}],
            },
        },
    )
    return Settings(
        _env_file=None,
        reports_dir=tmp_path,
        sleeper_league_id="league",
        sleeper_roster_id=8,
    )


def _decision(context: dict) -> dict:
    lineup = [
        {
            "slot_index": index,
            "slot": row["slot"],
            "player_id": row["player_id"],
            "reason": "Best legal option",
        }
        for index, row in enumerate(context["our_team"]["current_lineup"])
    ]
    return {
        "decision_status": "needs_data",
        "week": context["week"],
        "confidence": 0.6,
        "team_assessment": {
            "championship_outlook": "Competitive",
            "weekly_strategy": "Preserve optionality",
            "strengths": [],
            "vulnerabilities": [],
        },
        "lineup_status": "hold_current",
        "lineup": lineup,
        "lineup_changes": [],
        "waiver_status": "watch",
        "waiver_strategy": "Wait for stronger evidence",
        "waiver_horizon": {
            "roster_thesis": "Preserve scarce bench upside while improving weak depth.",
            "rest_of_season_priorities": ["Add durable roles before weekly points"],
            "playoff_priorities": ["Protect lineup depth for Weeks 15-17"],
            "churn_policy": "Rentals must beat the exact drop's season-long option value.",
        },
        "waiver_claims": [
            {
                "priority": 1,
                "add_player_id": "free",
                "drop_player_id": "",
                "faab_percent": 0,
                "claim_type": "watch",
                "horizon": "rest_of_season",
                "expected_roster_role": "Upside bench watch",
                "immediate_case": "No immediate lineup role is established.",
                "season_case": "A durable workload could become useful depth.",
                "drop_cost": "No transaction while the role remains uncertain.",
                "exit_plan": "Remove from the watchlist if usage falls.",
                "reevaluate_after_week": context["week"] + 1,
                "contingency": "Role growth",
                "reason": "Monitor usage",
            }
        ],
        "pending_waiver_management": {
            "status": "keep",
            "desired_order": [],
            "reason": "No submitted claim needs a change.",
            "replacement_trigger": "Reassess when transaction state changes.",
        },
        "watchlist": [],
        "trade_status": "watch",
        "trade_strategy": "Monitor counterpart roster imbalances without forcing a deal.",
        "trade_targets": [],
        "urgent_alerts": [],
        "evidence_gaps": ["Weekly projection feed unavailable"],
        "research_evidence": [],
    }


def test_manager_context_joins_rosters_rankings_injuries_and_games(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    assert context["league"]["starter_slots"] == ["QB", "RB", "FLEX"]
    assert context["opponent"]["roster_id"] == 2
    assert context["opponent"]["projected_total"] == 105.75
    assert context["opponent"]["win_probability_percent"] == 45
    assert context["opponent"]["players"][0]["sleeper_weekly_projection"] == 21
    assert context["our_team"]["projected_total"] == 110.25
    assert context["our_team"]["win_probability_percent"] == 55
    free = context["free_agent_candidates"][0]
    assert free["player_id"] == "free"
    assert free["overall_rank"] == 80
    assert free["injury_status"] == "Questionable"
    assert free["depth_rank"] == "2"
    assert free["depth_group_rank"] == "2"
    assert free["ids"]["fantasypros_id"] == "123"
    assert free["game"]["game_id"] == "game"
    assert free["game"]["weather"]["forecast"]["temperature"] == 64
    assert free["sleeper_weekly_projection"] == 12.5
    assert free["sleeper_acquisition_type"] == "free_agent"
    assert free["sleeper_ui_display_name"] == "F. Runner"
    assert "fake" not in context["allowlists"]["free_agent_player_ids"]
    quarterback = context["our_team"]["all_players"][0]
    assert quarterback["sleeper_weekly_projection"] == 19.67
    assert (
        context["data_quality"]["weekly_projection_feed"]
        == "sleeper_ui_roster_free_agents_and_matchup"
    )
    assert context["data_quality"]["sleeper_ui_free_agent_projection_count"] == 1
    assert context["data_quality"]["sleeper_ui_free_agent_projection_join"] == {
        "observed": 2,
        "matched": 1,
        "stable_id_matches": 0,
        "ambiguous_or_unmatched": 1,
        "acquisition_typed": 1,
    }
    assert context["data_quality"]["sleeper_ui_matchup_projection_count"] == 2
    assert context["data_quality"]["weather_status"] == "active"
    assert context["data_quality"]["weather_coverage"]["forecast_count"] == 1
    assert context["season_horizon"]["objective"].startswith("Maximize championship")
    assert context["season_horizon"]["playoff_weeks"] == [15, 16, 17]
    assert context["season_horizon"]["roster_construction"]["open_roster_slots"] == 0


def test_manager_context_surfaces_open_reserve_slot_and_ir_stash_candidates(
    tmp_path: Path,
) -> None:
    settings = _reports(tmp_path)
    sleeper_path = tmp_path / "in-season-context.json"
    sleeper = json.loads(sleeper_path.read_text(encoding="utf-8"))
    sleeper["league"]["settings"].update(
        {
            "reserve_slots": 1,
            "reserve_allow_out": 0,
            "reserve_allow_doubtful": 0,
        }
    )
    _write(sleeper_path, sleeper)

    static_path = tmp_path / "draft-static-cache.json"
    static = json.loads(static_path.read_text(encoding="utf-8"))
    static["players"]["stash"] = {
        "full_name": "Injured Stash",
        "position": "WR",
        "fantasy_positions": ["WR"],
        "team": "NYJ",
        "status": "Inactive",
        "injury_status": "IR",
        "search_rank": 10,
    }
    static["rankings"].append(
        {
            "player_name": "Injured Stash",
            "normalized_name": "injuredstash",
            "overall_rank": 300,
            "position_rank": 100,
            "as_of": "2026-09-04",
            "position": "WR",
            "team": "NYJ",
        }
    )
    _write(static_path, static)

    browser_path = tmp_path / "browser-free-agents.json"
    browser = json.loads(browser_path.read_text(encoding="utf-8"))
    browser["players"].append(
        {
            "player_id": "stash",
            "display_name": "I. Stash",
            "position": "WR",
            "team": "NYJ",
            "injury_status": "IR",
            "acquisition_type": "waiver",
            "projected_points": 0,
        }
    )
    _write(browser_path, browser)

    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    reserve = context["season_horizon"]["reserve_management"]
    assert context["league"]["reserve"]["slots"] == 1
    assert context["league"]["reserve"]["eligible_statuses"] == ["IR", "NFI", "PUP"]
    assert reserve["open_reserve_slots"] == 1
    assert reserve["eligible_free_agent_player_ids"] == ["stash"]
    stash = next(
        row for row in context["free_agent_candidates"] if row["player_id"] == "stash"
    )
    assert stash["reserve_eligible"] is True
    assert stash["ir_stash_candidate"] is True
    assert stash["sleeper_acquisition_type"] == "waiver"


def test_decision_state_hash_ignores_refresh_only_changes(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    sleeper_path = tmp_path / "in-season-context.json"
    sleeper = json.loads(sleeper_path.read_text(encoding="utf-8"))
    sleeper["generated_at"] = "2026-09-09T11:58:00+00:00"
    _write(sleeper_path, sleeper)
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)

    first = build_manager_context(settings, now=now)
    sleeper["generated_at"] = "2026-09-09T11:59:00+00:00"
    sleeper["trending_adds"] = list(reversed(sleeper["trending_adds"]))
    _write(sleeper_path, sleeper)
    refreshed = build_manager_context(settings, now=now)

    assert refreshed["state_hash"] != first["state_hash"]
    assert refreshed["decision_state_hash"] == first["decision_state_hash"]

    nflverse_path = tmp_path / "nflverse-context.json"
    nflverse = json.loads(nflverse_path.read_text(encoding="utf-8"))
    nflverse["week_injuries"][0]["report_status"] = "Out"
    _write(nflverse_path, nflverse)
    changed = build_manager_context(settings, now=now)

    assert changed["decision_state_hash"] != first["decision_state_hash"]


def test_manager_context_rejects_completed_week_values_mislabeled_as_projections(
    tmp_path: Path,
) -> None:
    settings = _reports(tmp_path)
    for report_name in (
        "browser-lineup.json",
        "browser-free-agents.json",
        "browser-matchup.json",
    ):
        path = tmp_path / report_name
        report = json.loads(path.read_text(encoding="utf-8"))
        report["week"] = 0
        _write(path, report)

    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    quarterback = context["our_team"]["all_players"][0]
    free_agent = next(
        row for row in context["free_agent_candidates"] if row["player_id"] == "free"
    )
    assert quarterback["sleeper_weekly_projection"] is None
    assert free_agent["sleeper_weekly_projection"] is None
    assert context["our_team"]["projected_total"] is None
    assert context["opponent"]["projected_total"] is None
    assert context["data_quality"]["sleeper_ui_projection_count"] == 0
    assert context["data_quality"]["sleeper_ui_matchup_projection_count"] == 0
    assert context["data_quality"]["sleeper_ui_expected_projection_period"] == {
        "season": "2026",
        "week": 1,
    }


def test_manager_context_rejects_matchup_for_a_different_opponent(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    path = tmp_path / "browser-matchup.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["opponent"]["players"] = [{"player_id": "free", "projected_points": 21}]
    _write(path, report)

    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    assert context["opponent"]["projected_total"] is None
    assert context["opponent"]["win_probability_percent"] is None
    assert context["opponent"]["players"][0]["sleeper_weekly_projection"] is None
    assert context["data_quality"]["sleeper_ui_matchup_projection_count"] == 0


def test_targeted_browser_player_id_is_used_after_identity_validation(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    report_path = tmp_path / "browser-free-agents.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["players"][0]["player_id"] = "free"
    report["players"][0]["observed_at"] = "2026-09-09T11:59:45+00:00"
    _write(report_path, report)

    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    assert (
        context["data_quality"]["sleeper_ui_free_agent_projection_join"]["stable_id_matches"] == 1
    )
    free = next(row for row in context["free_agent_candidates"] if row["player_id"] == "free")
    assert free["sleeper_acquisition_type"] == "free_agent"


def test_grounding_keeps_observed_usage_while_hiding_unqualified_models() -> None:
    context = {
        "our_team": {
            "all_players": [
                {
                    "player_id": "p1",
                    "usage": {"status": "available", "latest": {"snap_share": 0.8}},
                    "projection_ensemble": {"median": 12},
                }
            ]
        },
        "free_agent_candidates": [
            {
                "player_id": "p2",
                "usage": {"status": "available", "latest": {"target_share": 0.2}},
                "projection_ensemble": {"median": 10},
            }
        ],
        "championship_outlook": {"status": "ready"},
    }
    promotion = {
        "projection": {"authoritative": False},
        "championship": {"authoritative": False},
    }

    grounded = build_decision_grounding_context(context, promotion)

    assert grounded["our_team"]["all_players"][0]["usage"]["latest"]["snap_share"] == 0.8
    assert grounded["free_agent_candidates"][0]["usage"]["latest"]["target_share"] == 0.2
    assert "projection_ensemble" not in grounded["our_team"]["all_players"][0]
    assert "championship_outlook" not in grounded
    assert "projection_ensemble" in context["our_team"]["all_players"][0]


def test_multi_position_player_uses_sleeper_fantasy_eligibility(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    sleeper_path = tmp_path / "in-season-context.json"
    sleeper = json.loads(sleeper_path.read_text(encoding="utf-8"))
    sleeper["owner_roster"]["players"].append("hybrid")
    sleeper["rosters"][0]["players"].append("hybrid")
    _write(sleeper_path, sleeper)
    static_path = tmp_path / "draft-static-cache.json"
    static = json.loads(static_path.read_text(encoding="utf-8"))
    static["players"]["hybrid"] = {
        "full_name": "Hybrid Player",
        "position": "DB",
        "fantasy_positions": ["DB", "WR"],
        "team": "JAX",
        "status": "Active",
    }
    _write(static_path, static)

    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    hybrid = next(row for row in context["our_team"]["all_players"] if row["player_id"] == "hybrid")
    assert hybrid["primary_position"] == "DB"
    assert hybrid["position"] == "WR"
    assert hybrid["eligible_positions"] == ["WR"]

    decision = _decision(context)
    decision["lineup"][2]["player_id"] = "hybrid"
    assert validate_decision(decision, context)["lineup"][2]["player_id"] == "hybrid"


def test_decision_validation_enforces_lineup_and_transaction_allowlists(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    validated = validate_decision(decision, context)
    assert validated["lineup"][2]["player_id"] == "rb2"

    decision["waiver_claims"][0]["add_player_id"] = "not-available"
    with pytest.raises(InSeasonExpertUnavailable, match="waiver allowlist"):
        validate_decision(decision, context)


def test_decision_validation_stages_claimed_player_until_rostered(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    decision["lineup"][2]["player_id"] = "free"
    decision["waiver_claims"][0].update(
        {
            "claim_type": "free_agent",
            "drop_player_id": "bench",
        }
    )

    validated = validate_decision(decision, context)

    assert validated["lineup"][2] == {
        "slot_index": 2,
        "slot": "FLEX",
        "player_id": "rb2",
        "reason": (
            "Acquisition target is not rostered yet; preserve the current starter until "
            "the transaction is reconciled."
        ),
    }
    assert validated["lineup_changes"] == []
    assert validated["waiver_claims"][0]["add_player_id"] == "free"


def test_full_roster_acquisition_requires_exact_drop_and_rental_exit(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    claim = decision["waiver_claims"][0]
    claim["claim_type"] = "free_agent"
    with pytest.raises(InSeasonExpertUnavailable, match="full roster requires an exact drop"):
        validate_decision(decision, context)

    claim["drop_player_id"] = "bench"
    claim["horizon"] = "one_week_rental"
    claim["reevaluate_after_week"] = 3
    with pytest.raises(InSeasonExpertUnavailable, match="following week"):
        validate_decision(decision, context)


def test_current_week_manual_drop_protection_is_grounded_and_enforced(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    _write(
        tmp_path / "in-season-overrides.json",
        {
            "season": "2026",
            "week": 1,
            "protected_player_ids": ["bench"],
            "protection_notes": {"bench": "Retain for Week 1"},
        },
    )
    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    assert context["manual_constraints"] == {
        "applies_to_current_week": True,
        "protected_player_ids": ["bench"],
        "protection_notes": {"bench": "Retain for Week 1"},
    }
    assert "bench" not in context["allowlists"]["drop_player_ids"]
    bench = next(
        player for player in context["our_team"]["all_players"] if player["player_id"] == "bench"
    )
    assert bench["drop_protected"] is True

    decision = _decision(context)
    decision["waiver_claims"][0]["drop_player_id"] = "bench"
    with pytest.raises(InSeasonExpertUnavailable, match="drop allowlist"):
        validate_decision(decision, context)


def test_manual_drop_protection_expires_outside_its_week(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    _write(
        tmp_path / "in-season-overrides.json",
        {
            "season": "2026",
            "week": 2,
            "protected_player_ids": ["bench"],
            "protection_notes": {"bench": "Retain for Week 2"},
        },
    )

    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    assert context["manual_constraints"]["applies_to_current_week"] is False
    assert context["manual_constraints"]["protected_player_ids"] == []
    assert "bench" in context["allowlists"]["drop_player_ids"]


def test_waiver_compiler_uses_observed_action_type_and_exact_roster(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    decision["waiver_status"] = "ready"
    decision["waiver_claims"] = [
        {
            "priority": 1,
            "add_player_id": "free",
            "drop_player_id": "bench",
            "faab_percent": 0,
            "horizon": "rest_of_season",
            "expected_roster_role": "Bench depth with flex upside",
            "immediate_case": "Adds usable depth immediately.",
            "season_case": "Has a path to durable touches.",
            "drop_cost": "Bench Receiver has less role upside.",
            "exit_plan": "Reassess if the workload does not materialize.",
            "reevaluate_after_week": 2,
            "contingency": "Role confirmed",
            "reason": "Upgrade the final bench position",
        }
    ]

    validated = validate_decision(decision, context)
    previews = compile_waiver_actions(validated, context)

    assert len(previews) == 1
    assert previews[0]["action_type"] == "ADD_FREE_AGENT"
    assert previews[0]["parameters"]["add_player_name"] == "F. Runner"
    assert previews[0]["parameters"]["add_player_search_name"] == "Free Runner"
    assert previews[0]["parameters"]["add_player_position"] == "RB"
    assert previews[0]["parameters"]["add_player_team"] == "NE"
    assert previews[0]["parameters"]["drop_player_name"] == "Bench Receiver"
    assert previews[0]["parameters"]["drop_player_search_name"] == "Bench Receiver"
    assert previews[0]["parameters"]["transaction_week"] == 1
    assert previews[0]["parameters"]["expected_roster_before"] == [
        "qb",
        "rb",
        "rb2",
        "bench",
    ]
    assert previews[0]["parameters"]["expected_roster_after"] == [
        "qb",
        "rb",
        "rb2",
        "free",
    ]
    assert previews[0]["parameters"]["drop_evidence"]["protection_tier"] == "REVIEW"
    assert validated["waiver_claims"][0]["claim_type"] == "free_agent"

    decision["waiver_claims"][0]["claim_type"] = "waiver"
    corrected = validate_decision(decision, context)
    assert corrected["waiver_claims"][0]["claim_type"] == "free_agent"
    assert compile_waiver_actions(corrected, context)[0]["action_type"] == "ADD_FREE_AGENT"

    target = next(
        row for row in context["free_agent_candidates"] if row["player_id"] == "free"
    )
    target["sleeper_acquisition_type"] = None
    with pytest.raises(InSeasonExpertUnavailable, match="authoritative acquisition type"):
        validate_decision(decision, context)


def test_free_agent_compiler_chains_each_exact_roster_mutation(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    second = {
        **next(
            row for row in context["free_agent_candidates"] if row["player_id"] == "free"
        ),
        "player_id": "free2",
        "name": "Second Receiver",
        "sleeper_ui_display_name": "S. Receiver",
        "position": "WR",
        "team": "MIA",
        "sleeper_acquisition_type": "free_agent",
    }
    context["free_agent_candidates"].append(second)
    context["allowlists"]["free_agent_player_ids"].append("free2")
    decision = _decision(context)
    decision["waiver_status"] = "ready"

    def claim(priority: int, add_id: str, drop_id: str) -> dict:
        return {
            "priority": priority,
            "add_player_id": add_id,
            "drop_player_id": drop_id,
            "faab_percent": 0,
            "claim_type": "free_agent",
            "horizon": "rest_of_season",
            "expected_roster_role": "Bench depth",
            "immediate_case": "Improves usable depth now.",
            "season_case": "Adds rest-of-season upside.",
            "drop_cost": "The outgoing player is replaceable.",
            "exit_plan": "Reassess after the next usage sample.",
            "reevaluate_after_week": 2,
            "contingency": "Role remains confirmed",
            "reason": f"Sequential roster upgrade {priority}",
        }

    decision["waiver_claims"] = [
        claim(1, "free", "bench"),
        claim(2, "free2", "rb"),
    ]

    actions = compile_waiver_actions(validate_decision(decision, context), context)

    assert actions[0]["parameters"]["expected_roster_before"] == [
        "qb",
        "rb",
        "rb2",
        "bench",
    ]
    assert actions[0]["parameters"]["expected_roster_after"] == [
        "qb",
        "rb",
        "rb2",
        "free",
    ]
    assert actions[1]["parameters"]["expected_roster_before"] == actions[0]["parameters"][
        "expected_roster_after"
    ]
    assert actions[1]["parameters"]["expected_roster_after"] == [
        "qb",
        "rb2",
        "free",
        "free2",
    ]


def test_waiver_fallback_compiler_keeps_each_claim_on_original_roster(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    first = next(row for row in context["free_agent_candidates"] if row["player_id"] == "free")
    first["sleeper_acquisition_type"] = "waiver"
    second = {
        **first,
        "player_id": "free2",
        "name": "Second Receiver",
        "sleeper_ui_display_name": "S. Receiver",
        "position": "WR",
        "team": "MIA",
    }
    context["free_agent_candidates"].append(second)
    context["allowlists"]["free_agent_player_ids"].append("free2")
    decision = _decision(context)
    decision["waiver_status"] = "ready"

    def claim(priority: int, add_id: str) -> dict:
        return {
            "priority": priority,
            "add_player_id": add_id,
            "drop_player_id": "bench",
            "faab_percent": 0,
            "claim_type": "waiver",
            "horizon": "rest_of_season",
            "expected_roster_role": "Bench depth",
            "immediate_case": "Improves usable depth now.",
            "season_case": "Adds rest-of-season upside.",
            "drop_cost": "The outgoing player is replaceable.",
            "exit_plan": "Reassess after the next usage sample.",
            "reevaluate_after_week": 2,
            "contingency": "Higher-priority claims may succeed first",
            "reason": f"Fallback claim {priority}",
        }

    decision["waiver_claims"] = [claim(1, "free"), claim(2, "free2")]

    # The model does not own transaction mechanics. Even a stale label or an
    # omitted label is deterministically replaced by Sleeper's observed state.
    decision["waiver_claims"][0]["claim_type"] = "free_agent"
    decision["waiver_claims"][1].pop("claim_type")

    validated = validate_decision(decision, context)
    assert [claim["claim_type"] for claim in validated["waiver_claims"]] == [
        "waiver",
        "waiver",
    ]
    actions = compile_waiver_actions(validated, context)

    original = ["qb", "rb", "rb2", "bench"]
    assert [action["parameters"]["expected_roster_before"] for action in actions] == [
        original,
        original,
    ]
    assert actions[0]["parameters"]["expected_roster_after"][-1] == "free"
    assert actions[1]["parameters"]["expected_roster_after"][-1] == "free2"


def test_ranked_unprotected_bench_player_is_explicitly_churn_eligible(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    static_path = tmp_path / "draft-static-cache.json"
    static = json.loads(static_path.read_text(encoding="utf-8"))
    static["rankings"].append(
        {
            "player_name": "Bench Receiver",
            "normalized_name": "benchreceiver",
            "overall_rank": 170,
            "position_rank": 55,
            "as_of": "2026-09-08",
            "position": "WR",
            "team": "DEN",
        }
    )
    _write(static_path, static)
    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    decision["waiver_status"] = "ready"
    decision["waiver_claims"] = [
        {
            "priority": 1,
            "add_player_id": "free",
            "drop_player_id": "bench",
            "faab_percent": 0,
            "claim_type": "free_agent",
            "horizon": "rest_of_season",
            "expected_roster_role": "Bench depth",
            "immediate_case": "Improves depth.",
            "season_case": "Adds upside.",
            "drop_cost": "The WR55 is replaceable.",
            "exit_plan": "Review after the next usage sample.",
            "reevaluate_after_week": 2,
            "contingency": "Role confirmed",
            "reason": "Improve the final bench position",
        }
    ]

    preview = compile_waiver_actions(validate_decision(decision, context), context)[0]

    assert preview["parameters"]["drop_evidence"]["protection_tier"] == "CHURN_ELIGIBLE"


def test_recent_acquisition_is_detected_from_exact_roster_mapping() -> None:
    now = datetime(2026, 9, 14, 18, tzinfo=timezone.utc)
    transactions = [
        {
            "status": "complete",
            "type": "waiver",
            "created": int(now.timestamp() * 1000),
            "adds": {"mine": 8, "theirs": 2},
        },
        {
            "status": "failed",
            "type": "waiver",
            "created": int(now.timestamp() * 1000),
            "adds": {"failed": 8},
        },
    ]

    assert _recently_acquired_player_ids(transactions, 8, now) == {"mine"}


def test_pending_waiver_context_requires_exact_owner_and_roster() -> None:
    transaction = {
        "transaction_id": "tx-1",
        "status": "pending",
        "type": "waiver",
        "creator": "owner",
        "created": 123,
        "settings": {"seq": 0},
        "adds": {"new": 8},
        "drops": {"old": 8},
        "roster_ids": [8],
    }

    pending = _pending_waiver_claims(
        [transaction, {**transaction, "transaction_id": "other", "creator": "other"}],
        roster_id=8,
        owner_user_id="owner",
        players={
            "new": {"full_name": "New Player"},
            "old": {"full_name": "Old Player"},
        },
    )

    assert pending == [
        {
            "transaction_id": "tx-1",
            "created": 123,
            "add_player_ids": ["new"],
            "add_player_names": ["New Player"],
            "drop_player_ids": ["old"],
            "drop_player_names": ["Old Player"],
            "sequence": 0,
            "waiver_bid": None,
        }
    ]


def test_authenticated_pending_waiver_resolves_exact_player_identities() -> None:
    now = datetime(2026, 9, 14, 20, 47, tzinfo=timezone.utc)
    report = {
        "generated_at": "2026-09-14T20:46:30+00:00",
        "league_id": "league",
        "roster_id": 8,
        "claims": [
            {
                "sequence": 1,
                "add_player_name": "C. Douglas",
                "add_player_position": "WR - MIA",
                "drop_player_name": "T. Spears",
                "drop_player_position": "RB - TEN",
                "processes_at": "Wed 1:05 am MDT",
            }
        ],
    }
    pending = _fresh_authenticated_pending_waiver_claims(
        report,
        {
            "13296": {
                "full_name": "Caleb Douglas",
                "position": "WR",
                "team": "MIA",
            },
            "9508": {
                "full_name": "Tyjae Spears",
                "position": "RB",
                "team": "TEN",
            },
        },
        now,
        league_id="league",
        roster_id=8,
        max_age_seconds=180,
    )

    assert pending[0]["add_player_ids"] == ["13296"]
    assert pending[0]["drop_player_ids"] == ["9508"]
    assert pending[0]["add_player_ui_display_name"] == "C. Douglas"
    assert pending[0]["drop_player_ui_display_name"] == "T. Spears"
    assert pending[0]["verification_channel"] == "authenticated_pending_ui"


def test_decision_validation_derives_slot_moves_from_exact_lineup(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    decision["lineup"][1]["player_id"] = "rb2"
    decision["lineup"][2]["player_id"] = "rb"

    validated = validate_decision(decision, context)

    assert validated["lineup_changes"] == [
        {
            "slot_index": 1,
            "slot": "RB",
            "out_player_id": "rb",
            "in_player_id": "rb2",
            "reason": "Best legal option",
        },
        {
            "slot_index": 2,
            "slot": "FLEX",
            "out_player_id": "rb2",
            "in_player_id": "rb",
            "reason": "Best legal option",
        },
    ]

    swaps = compile_lineup_swaps(validated, context)
    assert len(swaps) == 1
    assert swaps[0]["parameters"] == {
        "from_player_id": "rb",
        "from_player_name": "Running Back",
        "from_slot": "RB",
        "to_player_id": "rb2",
        "to_player_name": "Other Runner",
        "to_slot": "FLEX",
        "target_slot_index": 1,
        "expected_starters_before": ["qb", "rb", "rb2"],
        "expected_starters_after": ["qb", "rb2", "rb"],
    }


def test_lineup_compiler_sequences_bench_replacements(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    decision["lineup"][1]["player_id"] = "rb2"
    decision["lineup"][2]["player_id"] = "bench"
    validated = validate_decision(decision, context)

    swaps = compile_lineup_swaps(validated, context)

    assert len(swaps) == 2
    assert swaps[0]["parameters"]["expected_starters_after"] == ["qb", "rb2", "rb"]
    assert swaps[1]["parameters"]["expected_starters_before"] == ["qb", "rb2", "rb"]
    assert swaps[1]["parameters"]["expected_starters_after"] == ["qb", "rb2", "bench"]
    assert swaps[1]["parameters"]["to_slot"] == "BN"


@pytest.mark.asyncio
async def test_lineup_execution_is_disabled_by_default_and_has_a_safe_window(
    tmp_path: Path,
) -> None:
    settings = _reports(tmp_path)
    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    decision["decision_status"] = "ready"
    decision["lineup_status"] = "ready"
    decision["confidence"] = 0.9
    decision["lineup"][1]["player_id"] = "rb2"
    decision["lineup"][2]["player_id"] = "rb"
    validated = validate_decision(decision, context)
    swaps = compile_lineup_swaps(validated, context)

    not_before, expires_at = lineup_execution_window(
        settings,
        context,
        swaps,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    assert not_before.isoformat() == "2026-09-09T23:35:00+00:00"
    assert expires_at.isoformat() == "2026-09-10T00:15:00+00:00"
    result = await synchronize_lineup_actions(settings, context, validated, swaps)
    assert result == {"state": "analysis_only", "queued": [], "preview_count": 1}


@pytest.mark.asyncio
async def test_acquisition_execution_is_disabled_by_default_and_has_a_safe_window(
    tmp_path: Path,
) -> None:
    settings = _reports(tmp_path)
    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    decision["decision_status"] = "ready"
    decision["waiver_status"] = "ready"
    decision["confidence"] = 0.9
    decision["waiver_claims"] = [
        {
            "priority": 1,
            "add_player_id": "free",
            "drop_player_id": "rb",
            "faab_percent": 0,
            "claim_type": "free_agent",
            "horizon": "rest_of_season",
            "expected_roster_role": "Bench depth",
            "immediate_case": "Improves emergency depth.",
            "season_case": "Offers more rest-of-season upside.",
            "drop_cost": "The dropped player is replaceable.",
            "exit_plan": "Reassess after the next usage sample.",
            "reevaluate_after_week": 2,
            "contingency": "Role confirmed",
            "reason": "Upgrade the roster",
        }
    ]
    validated = validate_decision(decision, context)
    actions = compile_waiver_actions(validated, context)

    not_before, expires_at = acquisition_execution_window(
        settings,
        context,
        actions,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    assert not_before.isoformat() == "2026-09-09T12:00:00+00:00"
    assert expires_at.isoformat() == "2026-09-10T00:10:00+00:00"
    result = await synchronize_acquisition_actions(settings, context, validated, actions)
    assert result == {"state": "analysis_only", "queued": [], "preview_count": 1}


@pytest.mark.asyncio
async def test_exact_authenticated_pending_waiver_is_not_queued_again() -> None:
    settings = Settings(
        _env_file=None,
        sleeper_league_id="league",
        sleeper_roster_id=8,
        waiver_semantics_confirmed=True,
        in_season_acquisition_actions_enabled=True,
        execution_mode="browser",
        browser_live_actions="WAIVER_CLAIM",
    )
    context = {
        "season_horizon": {
            "pending_waiver_claims": [
                {
                    "add_player_ids": ["free"],
                    "drop_player_ids": ["rb"],
                    "verification_channel": "authenticated_pending_ui",
                }
            ]
        },
        "our_team": {"all_players": [{"player_id": "rb", "locked": False}]},
        "data_quality": {
            "sleeper_ui_free_agent_projection_join": {"acquisition_typed": 32}
        },
    }
    decision = {"waiver_status": "ready", "confidence": 0.9}
    actions = [
        {
            "action_type": "WAIVER_CLAIM",
            "parameters": {
                "add_player_id": "free",
                "drop_player_id": "rb",
                "observed_acquisition_type": "waiver",
            },
        }
    ]

    result = await synchronize_acquisition_actions(settings, context, decision, actions)

    assert result == {
        "state": "already_pending",
        "queued": [],
        "preview_count": 1,
        "reason": "Every exact waiver claim is already pending in Sleeper",
    }


def test_waiver_execution_window_uses_processing_time_instead_of_past_kickoffs() -> None:
    settings = Settings(
        _env_file=None,
        in_season_waiver_expire_minutes_before_processing=5,
    )
    now = datetime(2026, 9, 14, 18, tzinfo=timezone.utc)
    context = {
        "season_horizon": {"next_waiver_processing_at": "2026-09-16T01:00:00-06:00"},
        "our_team": {
            "all_players": [
                {"player_id": "old", "kickoff": "2026-09-13T17:00:00+00:00"}
            ]
        },
        "free_agent_candidates": [
            {"player_id": "new", "kickoff": "2026-09-13T17:00:00+00:00"}
        ],
    }
    actions = [
        {
            "action_type": "WAIVER_CLAIM",
            "parameters": {"add_player_id": "new", "drop_player_id": "old"},
        }
    ]

    not_before, expires_at = acquisition_execution_window(
        settings, context, actions, now=now
    )

    assert not_before == now
    assert expires_at.isoformat() == "2026-09-16T06:55:00+00:00"


def test_ir_player_is_excluded_from_lineup_allowlist(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    sleeper_path = tmp_path / "in-season-context.json"
    sleeper = json.loads(sleeper_path.read_text(encoding="utf-8"))
    sleeper["owner_roster"]["reserve"] = ["bench"]
    sleeper["rosters"][0]["reserve"] = ["bench"]
    _write(sleeper_path, sleeper)

    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    assert "bench" not in context["allowlists"]["lineup_player_ids"]
    assert context["our_team"]["bench"] == []
    assert context["our_team"]["reserve"][0]["player_id"] == "bench"


def test_fresh_fantasypros_payload_is_joined_by_stable_player_id(tmp_path: Path) -> None:
    settings = _reports(tmp_path)
    _write(
        tmp_path / "fantasypros-context.json",
        {
            "generated_at": "2026-09-09T11:59:30+00:00",
            "configured": True,
            "season": 2026,
            "week": 1,
            "datasets": {
                "projections": {
                    "content_hash": "projection-hash",
                    "payload": {
                        "players": [
                            {
                                "player_id": 123,
                                "player_name": "Free Runner",
                                "fpts": 13.4,
                                "stats": {"rush_yds": 68, "rec": 3.2},
                            }
                        ]
                    },
                },
                "weekly_rankings": {
                    "content_hash": "weekly-rank-hash",
                    "payload": {
                        "rankings": [{"player_id": "123", "rank_ecr": 41, "pos_rank": "RB24"}]
                    },
                },
            },
        },
    )

    context = build_manager_context(
        settings,
        now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    free = next(row for row in context["free_agent_candidates"] if row["player_id"] == "free")

    assert free["fantasypros"]["projections"]["fpts"] == 13.4
    assert free["fantasypros"]["weekly_rankings"]["rank_ecr"] == 41
    assert context["data_quality"]["fantasypros_player_count"] == 1
    assert context["data_quality"]["weekly_projection_feed"] == "active"


def test_fantasypros_defense_is_conservatively_joined_by_team() -> None:
    index, digest = _fresh_fantasypros_index(
        {
            "generated_at": "2026-09-09T11:59:30+00:00",
            "configured": True,
            "datasets": {
                "projections": {
                    "content_hash": "defense-projection-hash",
                    "payload": {
                        "players": [
                            {
                                "player_name": "Denver Broncos",
                                "position": "DST",
                                "team": "DEN",
                                "fpts": 7.4,
                            }
                        ]
                    },
                }
            },
        },
        datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )

    assert index["team:DEN"]["projections"]["fpts"] == 7.4
    assert digest


def test_limited_fantasypros_shape_joins_by_fpid_and_exact_name() -> None:
    index, _ = _fresh_fantasypros_index(
        {
            "generated_at": "2026-09-09T11:59:30+00:00",
            "configured": True,
            "datasets": {
                "projections": {
                    "content_hash": "limited-projection-hash",
                    "payload": {
                        "players": [
                            {
                                "fpid": 123,
                                "name": "Free Runner",
                                "position_id": "RB",
                                "team_id": "DEN",
                                "_observed_at": "2026-09-09T11:59:00+00:00",
                                "stats": {"points_half": 12.5},
                            }
                        ]
                    },
                }
            },
        },
        datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
    )
    assert index["123"]["projections"]["stats"]["points_half"] == 12.5
    assert index["name:freerunner:RB:DEN"]["projections"]["fpid"] == 123


def test_decision_validation_preserves_locked_slots(tmp_path: Path) -> None:
    context = build_manager_context(
        _reports(tmp_path),
        now=datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
    )
    decision = _decision(context)
    decision["lineup"][2]["player_id"] = "bench"

    with pytest.raises(InSeasonExpertUnavailable, match="locked player"):
        validate_decision(decision, context)
