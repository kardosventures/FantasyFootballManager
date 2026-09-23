from app.projection_backtest import build_projection_backtest


def _row(player_id: str, position: str, week: int, points: float) -> dict:
    if position == "K":
        return {
            "player_id": player_id,
            "position": position,
            "season": "2025",
            "week": str(week),
            "season_type": "REG",
            "fg_made_40_49": str(points / 4),
        }
    return {
        "player_id": player_id,
        "position": position,
        "season": "2025",
        "week": str(week),
        "season_type": "REG",
        "rushing_yards": str(points * 10),
    }


def test_backtest_is_walk_forward_and_includes_kickers() -> None:
    report = {
        "historical_player_stats": [
            *[_row("runner", "RB", week, points) for week, points in enumerate((5, 7, 9, 11), 1)],
            *[_row("kicker", "K", week, points) for week, points in enumerate((4, 8, 12), 1)],
        ]
    }
    result = build_projection_backtest(report, {"rush_yd": 0.1, "fgm_40_49": 4})
    assert result["status"] == "diagnostic_complete"
    assert result["qualification_status"] == "blocked"
    assert result["leakage_guard"] == {
        "status": "pass",
        "violations": 0,
        "rule": "max(training season/week) < target season/week",
    }
    assert result["candidate"]["samples"] == 3
    assert result["by_position"]["K"]["samples"] == 1
    assert result["coverage"]["first_week"] == "2025-W03"
    assert result["qualification_blockers"]


def test_backtest_blocks_when_history_is_insufficient() -> None:
    report = {"historical_player_stats": [_row("runner", "RB", 1, 5)]}
    result = build_projection_backtest(report, {"rush_yd": 0.1})
    assert result["status"] == "blocked"
    assert result["candidate"]["samples"] == 0


def test_ensemble_promotion_requires_and_scores_pregame_paired_forecasts() -> None:
    positions = ["QB", "RB", "WR", "TE"]
    report = {
        "player_ids": {
            f"sleeper-{position}-{index}": {
                "gsis_id": f"gsis-{position}-{index}",
                "position": position,
            }
            for position in positions
            for index in range(5)
        },
        "historical_schedule": [
            {
                "season": "2025",
                "week": str(week),
                "gameday": f"2025-09-{week + 6:02d}",
                "gametime": "13:00",
                "home_team": "DEN",
                "away_team": "LV",
                "home_score": "24",
                "away_score": "17",
            }
            for week in (1, 2)
        ],
        "historical_player_stats": [
            {
                "player_id": f"gsis-{position}-{index}",
                "position": position,
                "team": "DEN",
                "season": "2025",
                "week": str(week),
                "season_type": "REG",
                "rushing_yards": "100",
            }
            for week in (1, 2)
            for position in positions
            for index in range(5)
        ],
    }
    snapshots = []
    observation_number = 0
    for week in (1, 2):
        for position in positions:
            for index in range(5):
                observation_number += 1
                covered = observation_number <= 24
                candidate = 10.0 if covered else 13.0
                snapshots.append(
                    {
                        "player_id": f"sleeper-{position}-{index}",
                        "season": 2025,
                        "week": week,
                        "created_at": f"2025-09-{week + 6:02d}T12:00:00+00:00",
                        "projection": {
                            "mean": candidate,
                            "floor_p20": candidate - 1,
                            "ceiling_p80": candidate + 1,
                            "sources": [
                                {"source": "sleeper_ui", "points": 15.0},
                                {"source": "fantasypros", "points": 14.0},
                            ],
                        },
                    }
                )

    result = build_projection_backtest(report, {"rush_yd": 0.1}, snapshots)
    calibration = result["ensemble_calibration"]

    assert calibration["qualification_status"] == "qualified"
    assert calibration["candidate"]["samples"] == 40
    assert calibration["candidate"]["p20_p80_coverage"] == 0.6
    assert calibration["strongest_paired_source"] == "fantasypros"
    assert calibration["recommended_weights_by_position"]["QB"]
    assert result["promotion_gate"]["status"] == "qualified"


def test_ensemble_calibration_excludes_snapshots_at_or_after_kickoff() -> None:
    report = {
        "player_ids": {"sleeper": {"gsis_id": "gsis", "position": "RB"}},
        "historical_schedule": [
            {
                "season": "2025",
                "week": "1",
                "gameday": "2025-09-07",
                "gametime": "13:00",
                "home_team": "DEN",
                "away_team": "LV",
                "home_score": "24",
                "away_score": "17",
            }
        ],
        "historical_player_stats": [
            {
                "player_id": "gsis",
                "position": "RB",
                "team": "DEN",
                "season": "2025",
                "week": "1",
                "season_type": "REG",
                "rushing_yards": "100",
            }
        ],
    }
    result = build_projection_backtest(
        report,
        {"rush_yd": 0.1},
        [
            {
                "player_id": "sleeper",
                "season": 2025,
                "week": 1,
                "created_at": "2025-09-07T17:00:00+00:00",
                "projection": {
                    "mean": 10,
                    "floor_p20": 5,
                    "ceiling_p80": 15,
                    "sources": [{"source": "sleeper_ui", "points": 10}],
                },
            }
        ],
    )
    accounting = result["ensemble_calibration"]["snapshot_accounting"]
    assert accounting["eligible_pregame_player_weeks"] == 0
    assert accounting["excluded"]["at_or_after_kickoff"] == 1
