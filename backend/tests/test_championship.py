import math

import pytest

import app.championship as championship_module
from app.championship import (
    _best_lineup,
    _playoff_bracket_winner,
    _roster_move_counterfactuals,
    build_championship_outlook,
    compare_counterfactuals,
    simulate_championship,
)


def _team(roster_id: str, mean: float) -> dict:
    return {
        "roster_id": roster_id,
        "wins": 0,
        "ties": 0,
        "points_for": 0,
        "weekly": {
            "1": {"mean": mean, "standard_deviation": 0.1},
            "2": {"mean": mean, "standard_deviation": 0.1},
        },
    }


def test_simulator_is_reproducible_and_probabilities_sum_to_one() -> None:
    arguments = {
        "teams": [_team("1", 100), _team("2", 0)],
        "matchups_by_week": {
            "1": [
                {"roster_id": 1, "matchup_id": 1},
                {"roster_id": 2, "matchup_id": 1},
            ]
        },
        "current_week": 1,
        "playoff_start_week": 2,
        "championship_week": 2,
        "playoff_teams": 2,
        "simulations": 500,
        "seed": 7,
    }
    first = simulate_championship(**arguments)
    second = simulate_championship(**arguments)
    assert first == second
    assert first["teams"]["1"]["championship_probability"] == 1.0
    assert sum(row["championship_probability"] for row in first["teams"].values()) == 1.0


def test_counterfactual_comparison_uses_common_random_numbers() -> None:
    common = {
        "matchups_by_week": {
            "1": [
                {"roster_id": 1, "matchup_id": 1},
                {"roster_id": 2, "matchup_id": 1},
            ]
        },
        "current_week": 1,
        "playoff_start_week": 2,
        "championship_week": 2,
        "playoff_teams": 2,
        "simulations": 1000,
        "seed": 11,
    }
    baseline = simulate_championship(teams=[_team("1", 10), _team("2", 11)], **common)
    alternative = simulate_championship(teams=[_team("1", 14), _team("2", 11)], **common)
    comparison = compare_counterfactuals(baseline, alternative, roster_id="1")
    assert comparison["common_random_numbers"] is True
    assert comparison["championship_probability_delta"] > 0


def test_championship_lineup_honors_multi_position_eligibility() -> None:
    lineup = _best_lineup(
        [
            {
                "player_id": "hybrid",
                "position": "DB",
                "eligible_positions": ["WR"],
                "team": "JAX",
                "mean": 11,
            }
        ],
        ["WR"],
        bye_teams=set(),
    )
    assert lineup is not None
    assert lineup[0]["player_id"] == "hybrid"


def test_championship_lineup_optimizer_handles_duplicate_slots_and_flex() -> None:
    players = [
        {"player_id": "rb1", "position": "RB", "team": "A", "mean": 20},
        {"player_id": "rb2", "position": "RB", "team": "B", "mean": 15},
        {"player_id": "wr1", "position": "WR", "team": "C", "mean": 19},
        {"player_id": "wr2", "position": "WR", "team": "D", "mean": 14},
    ]
    lineup = _best_lineup(players, ["RB", "WR", "FLEX"], bye_teams=set())
    assert lineup is not None
    assert {player["player_id"] for player in lineup} == {"rb1", "rb2", "wr1"}


def test_championship_outlook_models_live_points_and_all_decision_features() -> None:
    player_one = {
        "player_id": "p1",
        "position": "QB",
        "eligible_positions": ["QB"],
        "team": "NE",
        "injury_status": "Active",
        "projection_ensemble": {"mean": 20, "standard_deviation": 5},
    }
    player_two = {
        "player_id": "p2",
        "position": "QB",
        "eligible_positions": ["QB"],
        "team": "SEA",
        "injury_status": "Questionable",
        "projection_ensemble": {"mean": 18, "standard_deviation": 5},
    }
    context = {
        "as_of": "2026-09-10T12:00:00+00:00",
        "week": 1,
        "league": {
            "starter_slots": ["QB"],
            "scoring_settings": {"pass_yd": 0.04, "pass_td": 4},
            "playoff_settings": {"playoff_week_start": 2, "playoff_teams": 2},
        },
        "our_team": {"roster_id": 1},
        "league_rosters": [
            {
                "roster_id": 1,
                "record": {},
                "starters": ["p1"],
                "players": [player_one],
            },
            {
                "roster_id": 2,
                "record": {},
                "starters": ["p2"],
                "players": [player_two],
            },
        ],
        "free_agent_candidates": [],
    }
    season_context = {
        "playoff_start_week": 2,
        "championship_week": 2,
        "matchups_by_week": {
            "1": [
                {
                    "roster_id": 1,
                    "matchup_id": 1,
                    "starters": ["p1"],
                    "players_points": {"p1": 17},
                },
                {
                    "roster_id": 2,
                    "matchup_id": 1,
                    "starters": ["p2"],
                    "players_points": {"p2": 10},
                },
            ],
            "2": [],
        },
    }
    schedule = [
        {
            "season": "2026",
            "week": str(week),
            "game_id": f"2026_0{week}_NE_SEA",
            "gameday": f"2026-09-{9 + 7 * (week - 1):02d}",
            "gametime": "20:20",
            "home_team": "SEA",
            "away_team": "NE",
            "home_score": "13" if week == 1 else "",
            "away_score": "10" if week == 1 else "",
        }
        for week in (1, 2)
    ]
    historical_stats = [
        {
            "season": "2025",
            "season_type": "REG",
            "week": "1",
            "game_id": "historic",
            "position": "QB",
            "opponent_team": "SEA",
            "passing_yards": "300",
            "passing_tds": "3",
        },
        {
            "season": "2025",
            "season_type": "REG",
            "week": "1",
            "game_id": "historic-2",
            "position": "QB",
            "opponent_team": "NE",
            "passing_yards": "150",
            "passing_tds": "1",
        },
    ]
    outlook = build_championship_outlook(
        context,
        season_context,
        {"season_schedule": schedule, "historical_player_stats": historical_stats},
        simulations=200,
        seed=19,
    )

    assert outlook["status"] == "complete"
    assert outlook["projection_coverage"] == 1.0
    assert outlook["structural_coverage"] == 1.0
    assert outlook["model_diagnostics"]["live_current_week"]["fixed_points"] == 27.0
    assert outlook["model_diagnostics"]["live_current_week"]["player_states"]["final"] == 2
    assert all(feature["implemented"] for feature in outlook["decision_features"].values())
    assert all(
        feature["qualification_status"] == "shadow_unvalidated"
        for feature in outlook["decision_features"].values()
    )


def test_availability_scenario_replaces_an_unavailable_player() -> None:
    unavailable_team = {
        "roster_id": "1",
        "wins": 0,
        "ties": 0,
        "points_for": 0,
        "weekly": {
            "1": {
                "mean": 20,
                "fixed_points": 0,
                "idiosyncratic_standard_deviation": 0,
                "factor_loadings": {},
                "availability_scenarios": [
                    {
                        "player_id": "out",
                        "availability_probability": 0,
                        "available_mean": 20,
                        "replacement_mean": 1,
                        "expected_mean": 20,
                    }
                ],
            },
        },
    }
    opponent = _team("2", 5)
    result = simulate_championship(
        teams=[unavailable_team, opponent],
        matchups_by_week={
            "1": [
                {"roster_id": 1, "matchup_id": 1},
                {"roster_id": 2, "matchup_id": 1},
            ]
        },
        current_week=1,
        playoff_start_week=1,
        championship_week=1,
        playoff_teams=2,
        simulations=200,
        seed=23,
    )
    assert result["teams"]["1"]["championship_probability"] == 0.0


def _multiweek_team(roster_id: str, mean: float) -> dict:
    return {
        "roster_id": roster_id,
        "wins": 0,
        "ties": 0,
        "points_for": 0,
        "weekly": {str(week): {"mean": mean, "standard_deviation": 0.01} for week in range(1, 6)},
    }


@pytest.mark.parametrize("playoff_teams", (2, 4, 6, 8))
def test_playoff_formats_allocate_exact_byes_and_one_champion(playoff_teams: int) -> None:
    teams = [_multiweek_team(str(index), 200 - index) for index in range(1, 13)]
    rounds = math.ceil(math.log2(playoff_teams))
    result = simulate_championship(
        teams=teams,
        matchups_by_week={},
        current_week=1,
        playoff_start_week=1,
        championship_week=rounds,
        playoff_teams=playoff_teams,
        simulations=200,
        seed=31,
    )

    assert sum(team["championship_probability"] for team in result["teams"].values()) == 1
    assert sum(team["playoff_probability"] for team in result["teams"].values()) == playoff_teams
    assert sum(team["first_round_bye_probability"] for team in result["teams"].values()) == (
        2**rounds - playoff_teams
    )
    assert result["playoff_format"]["rounds"] == rounds


@pytest.mark.parametrize("playoff_teams", (3, 5, 7, 9, 10, 11, 12))
def test_undocumented_playoff_field_sizes_fail_closed(playoff_teams: int) -> None:
    with pytest.raises(ValueError, match="only documents"):
        simulate_championship(
            teams=[_multiweek_team(str(index), 100 - index) for index in range(1, 13)],
            matchups_by_week={},
            current_week=1,
            playoff_start_week=1,
            championship_week=5,
            playoff_teams=playoff_teams,
            simulations=200,
        )


def test_regular_season_tiebreak_and_current_week_probability_are_explicit() -> None:
    result = simulate_championship(
        teams=[_multiweek_team("1", 20), _multiweek_team("2", 0)],
        matchups_by_week={
            "1": [
                {"roster_id": 1, "matchup_id": 1},
                {"roster_id": 2, "matchup_id": 1},
            ]
        },
        current_week=1,
        playoff_start_week=2,
        championship_week=2,
        playoff_teams=2,
        simulations=200,
        seed=37,
    )

    assert result["teams"]["1"]["current_week_win_probability"] == 1
    assert result["teams"]["2"]["current_week_win_probability"] == 0
    assert result["playoff_format"]["seeding_tiebreak"] == ("wins_then_points_for_then_roster_id")


def test_playoff_window_fails_closed_when_it_cannot_fit_the_bracket() -> None:
    with pytest.raises(ValueError, match="cannot contain"):
        simulate_championship(
            teams=[_multiweek_team(str(index), 100 - index) for index in range(1, 9)],
            matchups_by_week={},
            current_week=1,
            playoff_start_week=1,
            championship_week=2,
            playoff_teams=8,
            simulations=200,
        )


def test_six_team_bracket_uses_sleepers_fixed_documented_paths(monkeypatch) -> None:
    observed = []

    def record_match(first, second, teams, **kwargs):
        observed.append((first, second, kwargs["week"]))
        return first

    monkeypatch.setattr(championship_module, "_playoff_match", record_match)
    winner = _playoff_bracket_winner(
        ["1", "2", "3", "4", "5", "6"],
        {str(index): _multiweek_team(str(index), 100 - index) for index in range(1, 7)},
        playoff_start_week=15,
        seed=1,
        simulation=0,
    )

    assert winner == "1"
    assert observed == [
        ("3", "6", 15),
        ("4", "5", 15),
        ("1", "3", 16),
        ("2", "4", 16),
        ("1", "2", 17),
    ]


def test_counterfactuals_reject_unobserved_acquisition_state() -> None:
    result = _roster_move_counterfactuals(
        {
            "our_team": {
                "bench": [
                    {
                        "player_id": "drop",
                        "position": "RB",
                        "projection_ensemble": {"mean": 5},
                    }
                ],
                "current_lineup": [],
            },
            "manual_constraints": {},
            "free_agent_candidates": [
                {
                    "player_id": "unknown",
                    "position": "WR",
                    "projection_ensemble": {"mean": 20},
                }
            ],
        },
        {},
        {},
        roster_id="1",
        seed=1,
        simulations=200,
        calibration=None,
    )

    assert result["status"] == "not_applicable"
    assert "Sleeper-observed" in result["reason"]
