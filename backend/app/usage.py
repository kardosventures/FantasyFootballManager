from __future__ import annotations

from collections import defaultdict
from statistics import fmean, pstdev
from typing import Any

from app.scoring import score_nflverse_player_week, score_nflverse_team_defense_week
from app.utils import content_hash, normalize_player_name

FEATURE_VERSION = "usage-2026.2"
TEAM_ALIASES = {"LA": "LAR", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def _normalize_team(value: Any) -> str:
    team = str(value or "").upper()
    return TEAM_ALIASES.get(team, team)


def _number(value: Any) -> float | None:
    if value in {None, "", "NA", "NaN"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _average(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [value for row in rows if (value := _number(row.get(key))) is not None]
    return round(fmean(values), 4) if values else None


def _unique_index(identities: dict[str, dict[str, Any]], field: str) -> dict[str, str]:
    candidates: dict[str, list[str]] = defaultdict(list)
    for sleeper_id, identity in identities.items():
        value = str(identity.get(field) or "")
        if value and value != "NA":
            candidates[value].append(str(sleeper_id))
    return {value: ids[0] for value, ids in candidates.items() if len(ids) == 1}


def _resolve_stats_player(
    row: dict[str, Any],
    *,
    gsis_index: dict[str, str],
    name_index: dict[tuple[str, str, str], str],
    name_position_index: dict[tuple[str, str], str],
) -> str | None:
    gsis_id = str(row.get("player_id") or "")
    if gsis_id in gsis_index:
        return gsis_index[gsis_id]
    key = (
        normalize_player_name(str(row.get("player_display_name") or row.get("player_name") or "")),
        str(row.get("position") or "").upper(),
        _normalize_team(row.get("team")),
    )
    return name_index.get(key) or name_position_index.get((key[0], key[1]))


def _identity_name_index(identities: dict[str, dict[str, Any]]) -> dict[tuple[str, str, str], str]:
    candidates: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for sleeper_id, identity in identities.items():
        key = (
            normalize_player_name(str(identity.get("name") or "")),
            str(identity.get("position") or "").upper(),
            _normalize_team(identity.get("team")),
        )
        if key[0] and key[1]:
            candidates[key].append(str(sleeper_id))
    return {key: ids[0] for key, ids in candidates.items() if len(ids) == 1}


def _identity_name_position_index(
    identities: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], str]:
    candidates: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sleeper_id, identity in identities.items():
        key = (
            normalize_player_name(str(identity.get("name") or "")),
            str(identity.get("position") or "").upper(),
        )
        if key[0] and key[1]:
            candidates[key].append(str(sleeper_id))
    return {key: ids[0] for key, ids in candidates.items() if len(ids) == 1}


def _sleeper_identities(players: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for player_id, player in (players or {}).items():
        position = str(player.get("position") or "").upper()
        if position not in {"QB", "RB", "WR", "TE", "K", "DEF"}:
            continue
        name = str(player.get("full_name") or "").strip()
        if not name:
            name = " ".join(
                part
                for part in (
                    str(player.get("first_name") or "").strip(),
                    str(player.get("last_name") or "").strip(),
                )
                if part
            )
        result[str(player_id)] = {
            "name": name,
            "team": _normalize_team(player.get("team")),
            "position": position,
        }
    return result


def build_usage_index(
    report: dict[str, Any],
    *,
    through_week: int,
    season: int | None = None,
    scoring_settings: dict[str, Any] | None = None,
    players: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build causal season-to-date workload features keyed by Sleeper player ID."""

    identities = report.get("player_ids") or {}
    if not isinstance(identities, dict):
        return {}
    identities = {**_sleeper_identities(players), **identities}
    gsis_index = _unique_index(identities, "gsis_id")
    pfr_index = _unique_index(identities, "pfr_id")
    name_index = _identity_name_index(identities)
    name_position_index = _identity_name_position_index(identities)
    scoring = scoring_settings or {}

    target_season = int(season or report.get("season") or 0)
    stats_by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    team_opportunities: dict[tuple[int, str, int], float] = defaultdict(float)
    for row in (
        report.get("historical_player_stats")
        or report.get("season_player_stats")
        or report.get("week_player_stats")
        or []
    ):
        try:
            week = int(row.get("week"))
            row_season = int(row.get("season") or target_season)
        except (TypeError, ValueError):
            continue
        season_type = str(row.get("season_type") or row.get("game_type") or "").upper()
        if season_type and season_type != "REG":
            continue
        if row_season > target_season or (row_season == target_season and week > through_week):
            continue
        sleeper_id = _resolve_stats_player(
            row,
            gsis_index=gsis_index,
            name_index=name_index,
            name_position_index=name_position_index,
        )
        if not sleeper_id:
            continue
        normalized = dict(row)
        normalized["week"] = week
        normalized["season"] = row_season
        stats_by_player[sleeper_id].append(normalized)
        team = _normalize_team(row.get("team"))
        team_opportunities[(row_season, team, week)] += (_number(row.get("carries")) or 0) + (
            _number(row.get("targets")) or 0
        )

    snaps_by_player_week: dict[tuple[str, int, int], dict[str, Any]] = {}
    for row in (
        report.get("historical_snap_counts")
        or report.get("season_snap_counts")
        or report.get("week_snap_counts")
        or []
    ):
        try:
            week = int(row.get("week"))
            row_season = int(row.get("season") or target_season)
        except (TypeError, ValueError):
            continue
        game_type = str(row.get("game_type") or row.get("season_type") or "").upper()
        if game_type and game_type != "REG":
            continue
        if row_season > target_season or (row_season == target_season and week > through_week):
            continue
        sleeper_id = pfr_index.get(str(row.get("pfr_player_id") or ""))
        if not sleeper_id:
            key = (
                normalize_player_name(str(row.get("player") or "")),
                str(row.get("position") or "").upper(),
                _normalize_team(row.get("team")),
            )
            sleeper_id = name_index.get(key)
        if sleeper_id:
            snaps_by_player_week[(sleeper_id, row_season, week)] = row

    result: dict[str, dict[str, Any]] = {}
    for player_id, rows in stats_by_player.items():
        rows.sort(key=lambda item: (int(item["season"]), int(item["week"])))
        games: list[dict[str, Any]] = []
        for row in rows:
            week = int(row["week"])
            row_season = int(row["season"])
            snap = snaps_by_player_week.get((player_id, row_season, week), {})
            carries = _number(row.get("carries")) or 0.0
            targets = _number(row.get("targets")) or 0.0
            team_total = team_opportunities.get(
                (row_season, _normalize_team(row.get("team")), week), 0
            )
            games.append(
                {
                    "season": row_season,
                    "week": week,
                    "team": row.get("team"),
                    "opponent": row.get("opponent_team"),
                    "offense_snaps": _number(snap.get("offense_snaps")),
                    "snap_share": _number(snap.get("offense_pct")),
                    "carries": carries,
                    "targets": targets,
                    "receptions": _number(row.get("receptions")) or 0.0,
                    "air_yards": _number(row.get("receiving_air_yards")),
                    "target_share": _number(row.get("target_share")),
                    "opportunity_share": round((carries + targets) / team_total, 4)
                    if team_total
                    else None,
                    "fantasy_points": _number(row.get("fantasy_points")),
                    "fantasy_points_ppr": _number(row.get("fantasy_points_ppr")),
                    "league_fantasy_points": score_nflverse_player_week(row, scoring)
                    if scoring
                    else None,
                }
            )

        latest = games[-1]
        previous = games[-4:-1]
        recent_three = games[-3:]
        recent_six = games[-6:]
        snap_delta = None
        opportunity_delta = None
        if previous:
            prior_snap = _average(previous, "snap_share")
            prior_opportunity = _average(previous, "opportunity_share")
            if latest["snap_share"] is not None and prior_snap is not None:
                snap_delta = round(float(latest["snap_share"]) - prior_snap, 4)
            if latest["opportunity_share"] is not None and prior_opportunity is not None:
                opportunity_delta = round(float(latest["opportunity_share"]) - prior_opportunity, 4)
        signals = []
        if snap_delta is not None and snap_delta >= 0.15:
            signals.append("snap_share_rising")
        if snap_delta is not None and snap_delta <= -0.15:
            signals.append("snap_share_falling")
        if opportunity_delta is not None and opportunity_delta >= 0.10:
            signals.append("opportunity_share_rising")
        if opportunity_delta is not None and opportunity_delta <= -0.10:
            signals.append("opportunity_share_falling")
        fantasy_value_key = "league_fantasy_points" if scoring else "fantasy_points_ppr"
        fantasy_values = [
            float(value)
            for game in recent_six
            if (value := game.get(fantasy_value_key)) is not None
        ]
        result[player_id] = {
            "status": "available",
            "feature_version": FEATURE_VERSION,
            "games_observed": len(games),
            "latest_week": latest["week"],
            "latest_season": latest["season"],
            "latest": latest,
            "rolling_3": {
                key: _average(recent_three, key)
                for key in (
                    "snap_share",
                    "carries",
                    "targets",
                    "target_share",
                    "opportunity_share",
                    "receptions",
                    "fantasy_points",
                    "fantasy_points_ppr",
                    "league_fantasy_points",
                )
            },
            "rolling_6": {
                key: _average(recent_six, key)
                for key in (
                    "snap_share",
                    "carries",
                    "targets",
                    "target_share",
                    "opportunity_share",
                    "receptions",
                    "fantasy_points",
                    "fantasy_points_ppr",
                    "league_fantasy_points",
                )
            },
            "trend": {
                "snap_share_delta_vs_prior_3": snap_delta,
                "opportunity_share_delta_vs_prior_3": opportunity_delta,
                "signals": signals,
            },
            "volatility": round(pstdev(fantasy_values), 4) if len(fantasy_values) >= 2 else None,
            "data_quality": {
                "stat_game_count": len(games),
                "snap_game_count": sum(game["snap_share"] is not None for game in games),
                "routes_available": False,
                "red_zone_opportunities_available": False,
            },
            "provenance": {
                "stats": "nflverse/stats_player",
                "snaps": "nflverse/snap_counts",
                "through_week": through_week,
                "through_season": target_season,
                "scoring": "league_scoring_settings" if scoring else "nflverse_precomputed",
            },
        }
        result[player_id]["feature_hash"] = content_hash(result[player_id])

    _add_team_defense_usage(
        result,
        report=report,
        identities=identities,
        target_season=target_season,
        through_week=through_week,
        scoring=scoring,
    )
    return result


def _add_team_defense_usage(
    result: dict[str, dict[str, Any]],
    *,
    report: dict[str, Any],
    identities: dict[str, dict[str, Any]],
    target_season: int,
    through_week: int,
    scoring: dict[str, Any],
) -> None:
    if not scoring:
        return
    defense_ids = {
        str(player_id): _normalize_team(identity.get("team") or player_id)
        for player_id, identity in identities.items()
        if str(identity.get("position") or "").upper() == "DEF"
    }
    if not defense_ids:
        return

    schedule = report.get("historical_schedule") or report.get("season_schedule") or []
    score_by_team_week: dict[tuple[int, str, int], int] = {}
    for game in schedule:
        if str(game.get("game_type") or "REG").upper() != "REG":
            continue
        try:
            row_season = int(game.get("season"))
            week = int(game.get("week"))
            home_score = int(game.get("home_score"))
            away_score = int(game.get("away_score"))
        except (TypeError, ValueError):
            continue
        if row_season > target_season or (row_season == target_season and week > through_week):
            continue
        home = _normalize_team(game.get("home_team"))
        away = _normalize_team(game.get("away_team"))
        score_by_team_week[(row_season, home, week)] = away_score
        score_by_team_week[(row_season, away, week)] = home_score

    rows_by_team_week: dict[tuple[int, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in report.get("historical_player_stats") or []:
        try:
            row_season = int(row.get("season"))
            week = int(row.get("week"))
        except (TypeError, ValueError):
            continue
        if row_season > target_season or (row_season == target_season and week > through_week):
            continue
        if str(row.get("season_type") or "REG").upper() != "REG":
            continue
        rows_by_team_week[(row_season, _normalize_team(row.get("team")), week)].append(row)

    for player_id, team in defense_ids.items():
        games: list[dict[str, Any]] = []
        for key, points_allowed in score_by_team_week.items():
            row_season, row_team, week = key
            if row_team != team:
                continue
            rows = rows_by_team_week.get(key, [])
            games.append(
                {
                    "season": row_season,
                    "week": week,
                    "team": team,
                    "opponent": None,
                    "points_allowed": points_allowed,
                    "league_fantasy_points": score_nflverse_team_defense_week(
                        rows, points_allowed=points_allowed, scoring=scoring
                    ),
                }
            )
        if not games:
            continue
        games.sort(key=lambda item: (int(item["season"]), int(item["week"])))
        recent_three = games[-3:]
        recent_six = games[-6:]
        values = [float(game["league_fantasy_points"]) for game in recent_six]
        entry: dict[str, Any] = {
            "status": "available",
            "feature_version": FEATURE_VERSION,
            "entity_type": "team_defense",
            "games_observed": len(games),
            "latest_week": games[-1]["week"],
            "latest_season": games[-1]["season"],
            "latest": games[-1],
            "rolling_3": {"league_fantasy_points": _average(recent_three, "league_fantasy_points")},
            "rolling_6": {"league_fantasy_points": _average(recent_six, "league_fantasy_points")},
            "trend": {"signals": []},
            "volatility": round(pstdev(values), 4) if len(values) >= 2 else None,
            "data_quality": {
                "stat_game_count": len(games),
                "scoring_coverage": "estimated",
                "points_allowed_method": "opponent_final_score_proxy",
            },
            "provenance": {
                "stats": "nflverse/stats_player team aggregation",
                "schedule": "nflverse/schedules",
                "through_week": through_week,
                "through_season": target_season,
                "limitation": "Opponent final score can include points not charged to D/ST",
            },
        }
        entry["feature_hash"] = content_hash(entry)
        result[player_id] = entry


def missing_usage() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "feature_version": FEATURE_VERSION,
        "games_observed": 0,
        "evidence_gap": "No completed, identity-matched player-week usage is available",
    }
