from app.usage import build_usage_index, missing_usage


def test_usage_features_join_stats_and_snaps_without_future_leakage() -> None:
    report = {
        "player_ids": {
            "sleeper-1": {
                "name": "Example Runner",
                "team": "DEN",
                "position": "RB",
                "gsis_id": "00-1",
                "pfr_id": "RunnEx00",
            }
        },
        "season_player_stats": [
            {
                "player_id": "00-1",
                "player_display_name": "Example Runner",
                "position": "RB",
                "team": "DEN",
                "week": str(week),
                "carries": str(carries),
                "targets": str(targets),
                "receptions": str(targets - 1),
                "fantasy_points": str(points),
                "fantasy_points_ppr": str(points + targets - 1),
            }
            for week, carries, targets, points in (
                (1, 5, 2, 5),
                (2, 6, 2, 6),
                (3, 7, 3, 7),
                (4, 18, 7, 18),
                (5, 30, 10, 30),
            )
        ],
        "season_snap_counts": [
            {
                "pfr_player_id": "RunnEx00",
                "player": "Example Runner",
                "position": "RB",
                "team": "DEN",
                "week": str(week),
                "offense_snaps": str(snaps),
                "offense_pct": str(share),
            }
            for week, snaps, share in (
                (1, 20, 0.30),
                (2, 22, 0.32),
                (3, 24, 0.35),
                (4, 55, 0.80),
                (5, 60, 0.90),
            )
        ],
    }
    usage = build_usage_index(report, through_week=4)["sleeper-1"]
    assert usage["games_observed"] == 4
    assert usage["latest_week"] == 4
    assert usage["latest"]["snap_share"] == 0.8
    assert "snap_share_rising" in usage["trend"]["signals"]
    assert usage["data_quality"]["routes_available"] is False
    assert usage["provenance"]["through_week"] == 4


def test_missing_usage_is_explicit() -> None:
    assert missing_usage()["status"] == "unavailable"
    assert "evidence_gap" in missing_usage()


def test_usage_uses_prior_regular_season_but_excludes_postseason_and_future_rows() -> None:
    report = {
        "player_ids": {
            "sleeper-1": {
                "name": "Example Runner",
                "team": "DEN",
                "position": "RB",
                "gsis_id": "00-1",
            }
        },
        "historical_player_stats": [
            {
                "player_id": "00-1",
                "player_display_name": "Example Runner",
                "position": "RB",
                "team": "DEN",
                "season": "2025",
                "week": "18",
                "season_type": "REG",
                "carries": "12",
                "targets": "3",
            },
            {
                "player_id": "00-1",
                "player_display_name": "Example Runner",
                "position": "RB",
                "team": "DEN",
                "season": "2025",
                "week": "19",
                "season_type": "POST",
                "carries": "99",
                "targets": "99",
            },
            {
                "player_id": "00-1",
                "player_display_name": "Example Runner",
                "position": "RB",
                "team": "DEN",
                "season": "2026",
                "week": "2",
                "season_type": "REG",
                "carries": "88",
                "targets": "88",
            },
        ],
    }
    usage = build_usage_index(report, through_week=1, season=2026)["sleeper-1"]
    assert usage["games_observed"] == 1
    assert usage["latest_season"] == 2025
    assert usage["latest_week"] == 18
    assert usage["latest"]["carries"] == 12


def test_usage_scores_league_settings_and_resolves_unique_kicker_name() -> None:
    report = {
        "player_ids": {},
        "historical_player_stats": [
            {
                "player_id": "00-kicker",
                "player_display_name": "Example Kicker",
                "position": "K",
                "team": "DEN",
                "season": "2025",
                "week": str(week),
                "season_type": "REG",
                "fg_made_40_49": "1",
                "pat_made": str(week),
            }
            for week in (1, 2, 3)
        ],
    }
    players = {
        "sleeper-k": {
            "full_name": "Example Kicker",
            "position": "K",
            "team": "DEN",
        }
    }
    usage = build_usage_index(
        report,
        through_week=1,
        season=2026,
        scoring_settings={"fgm_40_49": 4, "xpm": 1},
        players=players,
    )["sleeper-k"]
    assert usage["games_observed"] == 3
    assert usage["rolling_3"]["league_fantasy_points"] == 6


def test_usage_builds_team_defense_history_from_schedule() -> None:
    report = {
        "player_ids": {},
        "historical_schedule": [
            {
                "season": "2025",
                "week": "1",
                "game_type": "REG",
                "home_team": "DEN",
                "away_team": "LV",
                "home_score": "20",
                "away_score": "10",
            }
        ],
        "historical_player_stats": [
            {
                "player_id": "defender",
                "position": "LB",
                "team": "DEN",
                "season": "2025",
                "week": "1",
                "season_type": "REG",
                "def_sacks": "2",
                "def_interceptions": "1",
            }
        ],
    }
    players = {"DEN": {"full_name": "Denver Broncos", "position": "DEF", "team": "DEN"}}
    usage = build_usage_index(
        report,
        through_week=1,
        season=2026,
        scoring_settings={"sack": 1, "int": 2, "pts_allow_7_13": 4},
        players=players,
    )["DEN"]
    assert usage["entity_type"] == "team_defense"
    assert usage["latest"]["league_fantasy_points"] == 8
    assert usage["data_quality"]["scoring_coverage"] == "estimated"


def test_usage_normalizes_nflverse_rams_team_alias() -> None:
    report = {
        "player_ids": {},
        "historical_schedule": [
            {
                "season": "2025",
                "week": "1",
                "home_team": "LA",
                "away_team": "SF",
                "home_score": "14",
                "away_score": "7",
            },
            {
                "season": "2025",
                "week": "19",
                "game_type": "POST",
                "home_team": "LA",
                "away_team": "SF",
                "home_score": "7",
                "away_score": "42",
            },
        ],
        "historical_player_stats": [
            {
                "player_id": "rams-defender",
                "position": "LB",
                "team": "LA",
                "season": "2025",
                "week": "1",
                "season_type": "REG",
                "def_sacks": "1",
            }
        ],
    }
    players = {"LAR": {"position": "DEF", "team": "LAR"}}
    usage = build_usage_index(
        report,
        through_week=1,
        season=2026,
        scoring_settings={"sack": 1, "pts_allow_7_13": 4},
        players=players,
    )
    assert usage["LAR"]["latest"]["league_fantasy_points"] == 5
    assert usage["LAR"]["games_observed"] == 1
