import pytest

from app.normalizer import EXPECTED_SCORING
from app.rankings import parse_sleeper_adp
from app.scoring import (
    blend_rankings,
    fantasy_points,
    points_allowed_scoring_key,
    score_nflverse_player_week,
    score_nflverse_team_defense_week,
)


def test_half_ppr_scoring():
    points = fantasy_points({"rec": 5, "rec_yd": 100, "rec_td": 1}, EXPECTED_SCORING)
    assert points == 18.5


def test_scores_nflverse_player_week_with_league_rules() -> None:
    row = {
        "passing_yards": "250",
        "passing_tds": "2",
        "passing_interceptions": "1",
        "rushing_yards": "20",
        "fumbles_lost_total": "1",
    }
    scoring = {
        "pass_yd": 0.04,
        "pass_td": 4,
        "pass_int": -1,
        "rush_yd": 0.1,
        "fum_lost": -2,
    }
    assert score_nflverse_player_week(row, scoring) == 17.0


def test_scores_distance_kicking_and_misses() -> None:
    row = {
        "fg_made_20_29": "1",
        "fg_made_40_49": "1",
        "fg_made_50_59": "1",
        "fg_missed": "1",
        "pat_made": "2",
        "pat_missed": "1",
    }
    scoring = {
        "fgm_20_29": 3,
        "fgm_40_49": 4,
        "fgm_50_59": 5,
        "fgmiss": -1,
        "xpm": 1,
        "xpmiss": -1,
    }
    assert score_nflverse_player_week(row, scoring) == 12.0


def test_scores_team_defense_without_combined_key_double_counting() -> None:
    rows = [
        {
            "def_sacks": "2",
            "def_interceptions": "1",
            "def_fumbles_forced": "1",
            "def_fumbles": "1",
            "def_tds": "1",
            "special_teams_tds": "0",
        },
        {"def_sacks": "1", "special_teams_tds": "1"},
    ]
    scoring = {
        "sack": 1,
        "int": 2,
        "ff": 1,
        "fum_rec": 2,
        "def_td": 6,
        "st_td": 6,
        "def_st_td": 6,
        "pts_allow_14_20": 1,
    }
    assert score_nflverse_team_defense_week(rows, points_allowed=17, scoring=scoring) == 21
    assert points_allowed_scoring_key(0) == "pts_allow_0"
    assert points_allowed_scoring_key(35) == "pts_allow_35p"


def test_ranking_blend_is_deterministic_and_missing_safe():
    assert blend_rankings([]) is None
    assert blend_rankings([(10, 1), (20, 1)]) == pytest.approx(13.333333333333334)


def test_half_ppr_sleeper_adp_parser_reads_market_order_and_date():
    rows, as_of = parse_sleeper_adp(
        """
        <span><strong>Updated:</strong> 06 September 2026</span>
        <table><tr data-halfppr='7.3'><td>Amon-Ra St. Brown</td>
        <td><span>WR</span></td><td>DET</td><td></td><td>7.3</td><td>8</td>
        <td><span>WR4</span></td></tr></table>
        """
    )
    assert as_of == "2026-09-06"
    assert len(rows) == 1
    assert rows[0].player_name == "Amon-Ra St. Brown"
    assert rows[0].adp == 7.3
    assert rows[0].overall_rank == 8
