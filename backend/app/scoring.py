from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# nflverse publishes descriptive player-week columns while Sleeper stores compact
# scoring keys on each league. Keep this translation explicit and auditable: an
# absent nflverse field scores zero and an unknown Sleeper rule is not invented.
NFLVERSE_PLAYER_STAT_MAP: dict[str, str] = {
    "pass_yd": "passing_yards",
    "pass_td": "passing_tds",
    "pass_int": "passing_interceptions",
    "pass_2pt": "passing_2pt_conversions",
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_tds",
    "rush_2pt": "rushing_2pt_conversions",
    "rec": "receptions",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_tds",
    "rec_2pt": "receiving_2pt_conversions",
    "fum_lost": "fumbles_lost_total",
    "fum_rec_td": "fumble_recovery_tds",
    "st_td": "special_teams_tds",
    "xpm": "pat_made",
    "xpmiss": "pat_missed",
    "fgm_0_19": "fg_made_0_19",
    "fgm_20_29": "fg_made_20_29",
    "fgm_30_39": "fg_made_30_39",
    "fgm_40_49": "fg_made_40_49",
    "fgm_50_59": "fg_made_50_59",
    "fgm_60p": "fg_made_60_",
    "fgmiss": "fg_missed",
}


def _number(value: Any) -> float:
    if value in {None, "", "NA", "NaN"}:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fantasy_points(stats: Mapping[str, float], scoring: Mapping[str, float]) -> float:
    """Apply Sleeper scoring keys without inventing values for absent statistics."""
    return round(sum(float(stats.get(key, 0)) * float(value) for key, value in scoring.items()), 3)


def nflverse_player_scoring_stats(row: Mapping[str, Any]) -> dict[str, float]:
    """Translate one nflverse player-week row into supported Sleeper scoring stats."""

    return {
        sleeper_key: _number(row.get(nflverse_key))
        for sleeper_key, nflverse_key in NFLVERSE_PLAYER_STAT_MAP.items()
    }


def score_nflverse_player_week(row: Mapping[str, Any], scoring: Mapping[str, float]) -> float:
    """Score an nflverse offense/kicker week with this league's Sleeper settings."""

    return fantasy_points(nflverse_player_scoring_stats(row), scoring)


def points_allowed_scoring_key(points_allowed: int) -> str:
    if points_allowed <= 0:
        return "pts_allow_0"
    if points_allowed <= 6:
        return "pts_allow_1_6"
    if points_allowed <= 13:
        return "pts_allow_7_13"
    if points_allowed <= 20:
        return "pts_allow_14_20"
    if points_allowed <= 27:
        return "pts_allow_21_27"
    if points_allowed <= 34:
        return "pts_allow_28_34"
    return "pts_allow_35p"


def nflverse_team_defense_scoring_stats(
    rows: list[Mapping[str, Any]], *, points_allowed: int
) -> dict[str, float]:
    """Aggregate supported nflverse team-week events into non-overlapping D/ST stats.

    The schedule's final opponent score is a conservative proxy for Sleeper points
    allowed because the open player-week table cannot remove scores charged to an
    offense (for example, a pick-six). Callers expose that limitation in provenance.
    """

    def total(field: str) -> float:
        return sum(_number(row.get(field)) for row in rows)

    stats = {
        "sack": total("def_sacks"),
        "int": total("def_interceptions"),
        "ff": total("def_fumbles_forced"),
        "fum_rec": total("def_fumbles"),
        "safe": total("def_safeties"),
        "blk_kick": total("def_fg_blocks") + total("def_pat_blocks") + total("def_punt_blocks"),
        "def_td": total("def_tds"),
        "st_td": total("special_teams_tds"),
        points_allowed_scoring_key(points_allowed): 1.0,
    }
    return {key: round(value, 3) for key, value in stats.items()}


def score_nflverse_team_defense_week(
    rows: list[Mapping[str, Any]], *, points_allowed: int, scoring: Mapping[str, float]
) -> float:
    return fantasy_points(
        nflverse_team_defense_scoring_stats(rows, points_allowed=points_allowed), scoring
    )


def value_over_replacement(projection: float, replacement_projection: float) -> float:
    return projection - replacement_projection


def blend_rankings(ranks: list[tuple[int, float]], *, minimum_weight: float = 0.01) -> float | None:
    """Weighted reciprocal-rank blend; lower returned values are better."""
    usable = [(rank, max(weight, minimum_weight)) for rank, weight in ranks if rank > 0]
    if not usable:
        return None
    reciprocal = sum(weight / rank for rank, weight in usable) / sum(weight for _, weight in usable)
    return 1 / reciprocal
