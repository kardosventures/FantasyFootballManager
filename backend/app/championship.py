from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from app.scoring import score_nflverse_player_week
from app.utils import content_hash

SIMULATION_VERSION = "championship-2026.3-shadow"
EASTERN = ZoneInfo("America/New_York")
TEAM_ALIASES = {"LA": "LAR", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR"}
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
REPLACEMENT_DEFAULTS = {
    "QB": (14.0, 5.0),
    "RB": (7.0, 4.5),
    "WR": (7.0, 4.5),
    "TE": (6.0, 3.5),
    "K": (7.0, 3.0),
    "DEF": (7.0, 4.0),
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _uniform(seed: int, simulation: int, week: int, key: str) -> float:
    digest = hashlib.sha256(f"{seed}:{simulation}:{week}:{key}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)


def _standard_normal_key(seed: int, simulation: int, week: int, key: str) -> float:
    digest = hashlib.sha256(f"{seed}:{simulation}:{week}:{key}".encode()).digest()
    first = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)
    second = (int.from_bytes(digest[8:16], "big") + 1) / (2**64 + 1)
    return math.sqrt(-2 * math.log(first)) * math.cos(2 * math.pi * second)


def _standard_normal(seed: int, simulation: int, week: int, roster_id: str) -> float:
    return _standard_normal_key(seed, simulation, week, roster_id)


def _forecast(team: dict[str, Any], week: int) -> tuple[float, float]:
    weekly = team.get("weekly") or {}
    row = weekly.get(str(week)) or weekly.get(week) or {}
    return _number(row.get("mean")), max(_number(row.get("standard_deviation"), 12.0), 0.1)


def _score(
    team: dict[str, Any],
    *,
    week: int,
    seed: int,
    simulation: int,
    latent_cache: dict[str, float] | None = None,
) -> float:
    weekly = team.get("weekly") or {}
    row = weekly.get(str(week)) or weekly.get(week) or {}
    factor_loadings = row.get("factor_loadings")
    if isinstance(factor_loadings, dict):
        score = _number(row.get("mean"))
        roster_id = str(team["roster_id"])
        score += _number(row.get("idiosyncratic_standard_deviation")) * _standard_normal_key(
            seed, simulation, week, f"roster:{roster_id}"
        )
        cache = latent_cache if latent_cache is not None else {}
        for factor, loading in factor_loadings.items():
            key = str(factor)
            cache_key = f"{week}:{key}"
            if cache_key not in cache:
                cache[cache_key] = _standard_normal_key(seed, simulation, week, key)
            score += _number(loading) * cache[cache_key]
        for scenario in row.get("availability_scenarios") or []:
            probability = min(max(_number(scenario.get("availability_probability"), 1.0), 0), 1)
            available = (
                _uniform(
                    seed,
                    simulation,
                    week,
                    f"availability:{roster_id}:{scenario.get('player_id')}",
                )
                <= probability
            )
            expected = _number(scenario.get("expected_mean"))
            observed = _number(
                scenario.get("available_mean") if available else scenario.get("replacement_mean")
            )
            score += observed - expected
        return max(score, _number(row.get("fixed_points")))

    mean, deviation = _forecast(team, week)
    return max(
        mean + deviation * _standard_normal(seed, simulation, week, str(team["roster_id"])),
        0.0,
    )


def _availability_adjustment(
    row: dict[str, Any],
    *,
    roster_id: str,
    week: int,
    seed: int,
    simulation: int,
) -> float:
    adjustment = 0.0
    for scenario in row.get("availability_scenarios") or []:
        probability = min(max(_number(scenario.get("availability_probability"), 1.0), 0), 1)
        available = (
            _uniform(
                seed,
                simulation,
                week,
                f"availability:{roster_id}:{scenario.get('player_id')}",
            )
            <= probability
        )
        expected = _number(scenario.get("expected_mean"))
        observed = _number(
            scenario.get("available_mean") if available else scenario.get("replacement_mean")
        )
        adjustment += observed - expected
    return adjustment


def _paired_scores(
    first: dict[str, Any],
    second: dict[str, Any],
    *,
    week: int,
    seed: int,
    simulation: int,
    correlation: float | None = None,
) -> tuple[float, float]:
    first_row = (first.get("weekly") or {}).get(str(week)) or {}
    second_row = (second.get("weekly") or {}).get(str(week)) or {}
    if not isinstance(first_row.get("factor_loadings"), dict) or not isinstance(
        second_row.get("factor_loadings"), dict
    ):
        return (
            _score(first, week=week, seed=seed, simulation=simulation),
            _score(second, week=week, seed=seed, simulation=simulation),
        )

    first_factors = first_row.get("factor_loadings") or {}
    second_factors = second_row.get("factor_loadings") or {}
    first_deviation = max(_number(first_row.get("simulation_standard_deviation"), 0.1), 0.1)
    second_deviation = max(_number(second_row.get("simulation_standard_deviation"), 0.1), 0.1)
    if correlation is None:
        covariance = sum(
            _number(loading) * _number(second_factors.get(factor))
            for factor, loading in first_factors.items()
        )
        correlation = min(
            max(covariance / max(first_deviation * second_deviation, 0.01), -0.75), 0.75
        )
    pair_key = ":".join(sorted((str(first["roster_id"]), str(second["roster_id"]))))
    first_normal = _standard_normal_key(seed, simulation, week, f"fantasy-matchup:{pair_key}:first")
    independent_normal = _standard_normal_key(
        seed, simulation, week, f"fantasy-matchup:{pair_key}:second"
    )
    second_normal = correlation * first_normal + math.sqrt(1 - correlation**2) * independent_normal
    first_score = (
        _number(first_row.get("mean"))
        + first_deviation * first_normal
        + _availability_adjustment(
            first_row,
            roster_id=str(first["roster_id"]),
            week=week,
            seed=seed,
            simulation=simulation,
        )
    )
    second_score = (
        _number(second_row.get("mean"))
        + second_deviation * second_normal
        + _availability_adjustment(
            second_row,
            roster_id=str(second["roster_id"]),
            week=week,
            seed=seed,
            simulation=simulation,
        )
    )
    return (
        max(first_score, _number(first_row.get("fixed_points"))),
        max(second_score, _number(second_row.get("fixed_points"))),
    )


def _week_pairs(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        roster_id = str(row.get("roster_id") or "")
        matchup_id = str(row.get("matchup_id") or "")
        if roster_id and matchup_id:
            grouped[matchup_id].append(roster_id)
    return [(rosters[0], rosters[1]) for rosters in grouped.values() if len(rosters) == 2]


def _playoff_match(
    first: str,
    second: str,
    teams: dict[str, dict[str, Any]],
    *,
    week: int,
    seed: int,
    simulation: int,
    pair_correlations: dict[tuple[int, str, str], float] | None = None,
) -> str:
    first_score, second_score = _paired_scores(
        teams[first],
        teams[second],
        week=week,
        seed=seed,
        simulation=simulation,
        correlation=(pair_correlations or {}).get((week, *sorted((str(first), str(second))))),
    )
    return first if first_score >= second_score else second


def _playoff_bracket_winner(
    entrants: list[str],
    teams: dict[str, dict[str, Any]],
    *,
    playoff_start_week: int,
    seed: int,
    simulation: int,
    pair_correlations: dict[tuple[int, str, str], float] | None = None,
) -> str:
    """Resolve Sleeper's documented fixed 2/4/6/8-team winners bracket."""

    def play(first: str, second: str, week_offset: int) -> str:
        return _playoff_match(
            first,
            second,
            teams,
            week=playoff_start_week + week_offset,
            seed=seed,
            simulation=simulation,
            pair_correlations=pair_correlations,
        )

    if len(entrants) == 2:
        return play(entrants[0], entrants[1], 0)
    if len(entrants) == 4:
        first = play(entrants[0], entrants[3], 0)
        second = play(entrants[1], entrants[2], 0)
        return play(first, second, 1)
    if len(entrants) == 6:
        three_six = play(entrants[2], entrants[5], 0)
        four_five = play(entrants[3], entrants[4], 0)
        first = play(entrants[0], three_six, 1)
        second = play(entrants[1], four_five, 1)
        return play(first, second, 2)
    if len(entrants) == 8:
        one_eight = play(entrants[0], entrants[7], 0)
        four_five = play(entrants[3], entrants[4], 0)
        two_seven = play(entrants[1], entrants[6], 0)
        three_six = play(entrants[2], entrants[5], 0)
        first = play(one_eight, four_five, 1)
        second = play(two_seven, three_six, 1)
        return play(first, second, 2)
    raise ValueError("Sleeper only documents 2, 4, 6, and 8-team winners brackets")


def simulate_championship(
    *,
    teams: list[dict[str, Any]],
    matchups_by_week: dict[str, list[dict[str, Any]]],
    current_week: int,
    playoff_start_week: int,
    championship_week: int,
    playoff_teams: int,
    simulations: int = 10_000,
    seed: int = 20260909,
) -> dict[str, Any]:
    if len(teams) < 2:
        raise ValueError("At least two teams are required")
    if simulations < 200:
        raise ValueError("At least 200 simulations are required")
    by_id = {str(team["roster_id"]): team for team in teams}
    if len(by_id) != len(teams):
        raise ValueError("Roster IDs must be unique")
    playoff_count = min(max(playoff_teams, 2), len(teams))
    if playoff_count not in {2, 4, 6, 8}:
        raise ValueError("Sleeper only documents 2, 4, 6, and 8-team winners brackets")
    playoff_rounds = math.ceil(math.log2(playoff_count))
    configured_playoff_weeks = championship_week - playoff_start_week + 1
    if configured_playoff_weeks < playoff_rounds:
        raise ValueError(
            "The configured playoff window cannot contain the required number of rounds"
        )
    champions = {roster_id: 0 for roster_id in by_id}
    playoff_counts = {roster_id: 0 for roster_id in by_id}
    bye_counts = {roster_id: 0 for roster_id in by_id}
    current_week_win_counts = {roster_id: 0.0 for roster_id in by_id}
    seed_counts = {
        roster_id: {str(position): 0 for position in range(1, len(teams) + 1)}
        for roster_id in by_id
    }
    pair_correlations: dict[tuple[int, str, str], float] = {}
    roster_ids = list(by_id)
    for week in range(current_week, championship_week + 1):
        for first_index, first_id in enumerate(roster_ids):
            first_row = (by_id[first_id].get("weekly") or {}).get(str(week)) or {}
            first_factors = first_row.get("factor_loadings") or {}
            first_deviation = max(_number(first_row.get("simulation_standard_deviation"), 0.1), 0.1)
            for second_id in roster_ids[first_index + 1 :]:
                second_row = (by_id[second_id].get("weekly") or {}).get(str(week)) or {}
                second_factors = second_row.get("factor_loadings") or {}
                second_deviation = max(
                    _number(second_row.get("simulation_standard_deviation"), 0.1), 0.1
                )
                covariance = sum(
                    _number(loading) * _number(second_factors.get(factor))
                    for factor, loading in first_factors.items()
                )
                pair_correlations[(week, *sorted((first_id, second_id)))] = min(
                    max(
                        covariance / max(first_deviation * second_deviation, 0.01),
                        -0.75,
                    ),
                    0.75,
                )

    for simulation in range(simulations):
        standings = {
            roster_id: {
                "wins": _number(team.get("wins")),
                "ties": _number(team.get("ties")),
                "points": _number(team.get("points_for")),
            }
            for roster_id, team in by_id.items()
        }
        for week in range(current_week, playoff_start_week):
            for first, second in _week_pairs(matchups_by_week.get(str(week), [])):
                if first not in by_id or second not in by_id:
                    continue
                first_score, second_score = _paired_scores(
                    by_id[first],
                    by_id[second],
                    week=week,
                    seed=seed,
                    simulation=simulation,
                    correlation=pair_correlations.get((week, *sorted((first, second)))),
                )
                standings[first]["points"] += first_score
                standings[second]["points"] += second_score
                if first_score == second_score:
                    standings[first]["ties"] += 1
                    standings[second]["ties"] += 1
                    if week == current_week:
                        current_week_win_counts[first] += 0.5
                        current_week_win_counts[second] += 0.5
                else:
                    winner = first if first_score > second_score else second
                    standings[winner]["wins"] += 1
                    if week == current_week:
                        current_week_win_counts[winner] += 1
        ordered = sorted(
            by_id,
            key=lambda roster_id: (
                standings[roster_id]["wins"] + 0.5 * standings[roster_id]["ties"],
                standings[roster_id]["points"],
                -int(roster_id) if roster_id.isdigit() else roster_id,
            ),
            reverse=True,
        )
        for position, roster_id in enumerate(ordered, start=1):
            seed_counts[roster_id][str(position)] += 1
        entrants = ordered[:playoff_count]
        for roster_id in entrants:
            playoff_counts[roster_id] += 1
        bracket_size = 2 ** math.ceil(math.log2(playoff_count))
        for roster_id in entrants[: bracket_size - playoff_count]:
            bye_counts[roster_id] += 1
        champion = _playoff_bracket_winner(
            entrants,
            by_id,
            playoff_start_week=playoff_start_week,
            seed=seed,
            simulation=simulation,
            pair_correlations=pair_correlations,
        )
        champions[champion] += 1

    results = {}
    for roster_id in by_id:
        probability = champions[roster_id] / simulations
        results[roster_id] = {
            "championship_probability": round(probability, 5),
            "championship_standard_error": round(
                math.sqrt(probability * (1 - probability) / simulations), 5
            ),
            "playoff_probability": round(playoff_counts[roster_id] / simulations, 5),
            "first_round_bye_probability": round(bye_counts[roster_id] / simulations, 5),
            "current_week_win_probability": (
                round(current_week_win_counts[roster_id] / simulations, 5)
                if current_week < playoff_start_week
                else None
            ),
            "seed_distribution": {
                position: round(count / simulations, 5)
                for position, count in seed_counts[roster_id].items()
            },
        }
    payload = {
        "status": "complete",
        "model_version": SIMULATION_VERSION,
        "seed": seed,
        "simulation_count": simulations,
        "current_week": current_week,
        "playoff_start_week": playoff_start_week,
        "championship_week": championship_week,
        "playoff_format": {
            "teams": playoff_count,
            "rounds": playoff_rounds,
            "first_round_byes": 2**playoff_rounds - playoff_count,
            "configured_weeks": configured_playoff_weeks,
            "seeding_tiebreak": "wins_then_points_for_then_roster_id",
            "round_policy": "Sleeper documented fixed winners bracket",
        },
        "teams": results,
        "limitations": [
            "Availability probabilities, correlations, opponent adjustments, and multi-week decay remain shadow heuristics until historical replay calibration passes",
            "In-progress remaining-player distributions rely on the latest Sleeper projection because a play-by-play clock feed is not configured",
        ],
    }
    payload["input_hash"] = content_hash(
        {
            "teams": teams,
            "matchups": matchups_by_week,
            "current_week": current_week,
            "playoff_start_week": playoff_start_week,
            "championship_week": championship_week,
            "playoff_teams": playoff_teams,
        }
    )
    return payload


def compare_counterfactuals(
    baseline: dict[str, Any], alternative: dict[str, Any], *, roster_id: str
) -> dict[str, Any]:
    baseline_team = (baseline.get("teams") or {}).get(str(roster_id)) or {}
    alternative_team = (alternative.get("teams") or {}).get(str(roster_id)) or {}
    championship_delta = _number(alternative_team.get("championship_probability")) - _number(
        baseline_team.get("championship_probability")
    )
    playoff_delta = _number(alternative_team.get("playoff_probability")) - _number(
        baseline_team.get("playoff_probability")
    )
    standard_error = math.sqrt(
        _number(baseline_team.get("championship_standard_error")) ** 2
        + _number(alternative_team.get("championship_standard_error")) ** 2
    )
    return {
        "championship_probability_delta": round(championship_delta, 5),
        "playoff_probability_delta": round(playoff_delta, 5),
        "approximate_95_percent_margin": round(1.96 * standard_error, 5),
        "strategically_equivalent": abs(championship_delta) <= 1.96 * standard_error,
        "common_random_numbers": baseline.get("seed") == alternative.get("seed"),
    }


def _eligible(positions: str | list[str], slot: str) -> bool:
    position_set = {positions} if isinstance(positions, str) else set(positions)
    if slot in {"FLEX", "WRT", "WRRB_FLEX"}:
        return bool(position_set & {"RB", "WR", "TE"})
    if slot in {"SUPER_FLEX", "SUPERFLEX"}:
        return bool(position_set & {"QB", "RB", "WR", "TE"})
    return slot in position_set


def _best_lineup(
    players: list[dict[str, Any]], slots: list[str], *, bye_teams: set[str]
) -> list[dict[str, Any]] | None:
    candidates = [
        player
        for player in players
        if player.get("mean") is not None and str(player.get("team") or "") not in bye_teams
    ]
    if not slots:
        return []
    empty_assignment = tuple(-1 for _ in slots)
    states: dict[int, tuple[float, tuple[int, ...]]] = {0: (0.0, empty_assignment)}
    for candidate_index, player in enumerate(candidates):
        next_states = dict(states)
        positions = player.get("eligible_positions") or str(player["position"])
        for mask, (score, assignment) in states.items():
            considered_slot_types: set[str] = set()
            for slot_index, slot in enumerate(slots):
                if mask & (1 << slot_index) or slot in considered_slot_types:
                    continue
                considered_slot_types.add(slot)
                if not _eligible(positions, slot):
                    continue
                next_mask = mask | (1 << slot_index)
                next_score = score + float(player["mean"])
                existing = next_states.get(next_mask)
                if existing is not None and existing[0] >= next_score:
                    continue
                next_assignment = list(assignment)
                next_assignment[slot_index] = candidate_index
                next_states[next_mask] = (next_score, tuple(next_assignment))
        states = next_states
    selected = states.get((1 << len(slots)) - 1)
    if selected is None:
        return None
    return [candidates[index] for index in selected[1]]


def _bye_teams_by_week(schedule: list[dict[str, Any]], weeks: range) -> dict[int, set[str]]:
    all_teams = {
        str(row.get(key) or "")
        for row in schedule
        for key in ("home_team", "away_team")
        if row.get(key)
    }
    result: dict[int, set[str]] = {}
    for week in weeks:
        playing = {
            str(row.get(key) or "")
            for row in schedule
            if str(row.get("week") or "") == str(week)
            for key in ("home_team", "away_team")
            if row.get(key)
        }
        result[week] = all_teams - playing if playing else set()
    return result


def _normalize_team(value: Any) -> str:
    team = str(value or "").upper()
    return TEAM_ALIASES.get(team, team)


def _position(value: Any) -> str:
    position = str(value or "").upper()
    return "DEF" if position in {"D/ST", "DST"} else position


def _projection(player: dict[str, Any]) -> tuple[float | None, float]:
    ensemble = player.get("projection_ensemble") or {}
    mean = ensemble.get("mean")
    if mean is None:
        mean = player.get("sleeper_weekly_projection")
    if mean is None:
        return None, 5.0
    return float(mean), max(_number(ensemble.get("standard_deviation"), 5.0), 0.5)


def _schedule_index(rows: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
    result: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        try:
            week = int(row.get("week"))
        except (TypeError, ValueError):
            continue
        home = _normalize_team(row.get("home_team"))
        away = _normalize_team(row.get("away_team"))
        if home:
            result[(week, home)] = row
        if away:
            result[(week, away)] = row
    return result


def _game_kickoff(row: dict[str, Any] | None) -> datetime | None:
    if not row:
        return None
    try:
        local = datetime.strptime(
            f"{row.get('gameday')} {row.get('gametime')}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=EASTERN)
    except (TypeError, ValueError):
        return None
    return local.astimezone(UTC)


def _game_is_final(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return row.get("home_score") not in {None, ""} and row.get("away_score") not in {None, ""}


def _game_metadata(row: dict[str, Any] | None, team: str) -> dict[str, Any]:
    if not row:
        return {"game_id": None, "opponent": None, "kickoff": None, "final": False}
    home = _normalize_team(row.get("home_team"))
    away = _normalize_team(row.get("away_team"))
    opponent = away if team == home else home if team == away else ""
    kickoff = _game_kickoff(row)
    return {
        "game_id": str(row.get("game_id") or "") or None,
        "opponent": opponent or None,
        "kickoff": kickoff.isoformat() if kickoff else None,
        "final": _game_is_final(row),
    }


def _availability_probability(player: dict[str, Any], *, horizon_weeks: int) -> float:
    status = str(player.get("injury_status") or player.get("status") or "").upper()
    practice = str(player.get("practice_status") or "").upper()
    position = _position(player.get("position"))
    healthy = {"QB": 0.975, "RB": 0.945, "WR": 0.955, "TE": 0.955, "K": 0.99, "DEF": 1.0}.get(
        position, 0.96
    )
    if status in {"OUT", "IR", "SUSPENDED", "PUP"}:
        current = 0.0
    elif "DOUBT" in status:
        current = 0.2
    elif status in {"QUESTIONABLE", "Q"}:
        current = 0.72
    elif status in {"D", "DTD", "DAY-TO-DAY"}:
        current = 0.82
    else:
        current = healthy
    if "DID NOT PARTICIPATE" in practice or practice == "DNP":
        current = min(current, 0.68)
    elif "LIMITED" in practice:
        current = min(current, 0.86)
    elif "FULL" in practice:
        current = max(current, 0.96)
    if horizon_weeks > 0 and current < healthy:
        current = healthy - (healthy - current) * math.exp(-0.65 * horizon_weeks)
    return round(min(max(current, 0.0), 1.0), 4)


def _has_explicit_availability_risk(player: dict[str, Any]) -> bool:
    status = str(player.get("injury_status") or player.get("status") or "").upper()
    practice = str(player.get("practice_status") or "").upper()
    return status in {
        "OUT",
        "IR",
        "SUSPENDED",
        "PUP",
        "DOUBTFUL",
        "D",
        "QUESTIONABLE",
        "Q",
        "DTD",
        "DAY-TO-DAY",
    } or any(marker in practice for marker in ("DID NOT PARTICIPATE", "DNP", "LIMITED"))


def _replacement_position(slot: str, baselines: dict[str, dict[str, Any]]) -> str:
    normalized = _position(slot)
    if normalized in {"FLEX", "WRT", "WRRB_FLEX"}:
        return max(("RB", "WR", "TE"), key=lambda item: _number(baselines[item]["mean"]))
    if normalized in {"SUPER_FLEX", "SUPERFLEX"}:
        return max(("QB", "RB", "WR", "TE"), key=lambda item: _number(baselines[item]["mean"]))
    return normalized


def _replacement_baselines(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    free_agent_values: dict[str, list[tuple[float, float]]] = defaultdict(list)
    roster_values: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for player in context.get("free_agent_candidates") or []:
        position = _position(player.get("position"))
        mean, deviation = _projection(player)
        if position in FANTASY_POSITIONS and mean is not None:
            free_agent_values[position].append((mean, deviation))
    for roster in context.get("league_rosters") or []:
        for player in roster.get("players") or []:
            position = _position(player.get("position"))
            mean, deviation = _projection(player)
            if position in FANTASY_POSITIONS and mean is not None:
                roster_values[position].append((mean, deviation))
    result: dict[str, dict[str, Any]] = {}
    for position, (fallback_mean, fallback_deviation) in REPLACEMENT_DEFAULTS.items():
        available = sorted(free_agent_values.get(position, []), reverse=True)[:5]
        if available:
            result[position] = {
                "mean": round(median(value for value, _ in available), 3),
                "standard_deviation": round(median(value for _, value in available), 3),
                "source": "projected_free_agent_top_five_median",
                "sample_count": len(available),
            }
            continue
        rostered = sorted(roster_values.get(position, []))
        if rostered:
            index = max(math.floor(0.2 * (len(rostered) - 1)), 0)
            result[position] = {
                "mean": round(rostered[index][0], 3),
                "standard_deviation": round(rostered[index][1], 3),
                "source": "rostered_position_lower_quintile_proxy",
                "sample_count": len(rostered),
            }
            continue
        result[position] = {
            "mean": fallback_mean,
            "standard_deviation": fallback_deviation,
            "source": "explicit_shadow_fallback",
            "sample_count": 0,
        }
    return result


def _position_anchors(
    context: dict[str, Any], baselines: dict[str, dict[str, Any]]
) -> dict[str, float]:
    values: dict[str, list[float]] = defaultdict(list)
    for roster in context.get("league_rosters") or []:
        for player in roster.get("players") or []:
            position = _position(player.get("position"))
            mean, _ = _projection(player)
            if position in FANTASY_POSITIONS and mean is not None:
                values[position].append(mean)
    return {
        position: round(median(points), 3)
        if (points := values.get(position))
        else _number(baselines[position]["mean"])
        for position in FANTASY_POSITIONS
    }


def _opponent_factors(
    nflverse: dict[str, Any], scoring_settings: dict[str, Any]
) -> tuple[dict[tuple[str, str], float], dict[str, Any]]:
    game_totals: dict[tuple[str, str, str], float] = defaultdict(float)
    for row in nflverse.get("historical_player_stats") or []:
        if str(row.get("season_type") or row.get("game_type") or "REG").upper() != "REG":
            continue
        position = _position(row.get("position"))
        opponent = _normalize_team(row.get("opponent_team"))
        game_id = str(row.get("game_id") or "")
        if position not in {"QB", "RB", "WR", "TE", "K"} or not opponent or not game_id:
            continue
        game_totals[(opponent, position, game_id)] += score_nflverse_player_week(
            row, scoring_settings
        )
    by_position: dict[str, list[float]] = defaultdict(list)
    by_defense: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (opponent, position, _), points in game_totals.items():
        by_position[position].append(points)
        by_defense[(opponent, position)].append(points)
    league_means = {
        position: sum(points) / len(points) for position, points in by_position.items() if points
    }
    factors: dict[tuple[str, str], float] = {}
    for key, points in by_defense.items():
        baseline = league_means.get(key[1])
        if not baseline or not points:
            continue
        raw = (sum(points) / len(points)) / baseline
        shrinkage = len(points) / (len(points) + 8)
        factors[key] = round(min(max(1 + (raw - 1) * shrinkage, 0.85), 1.15), 4)
    return factors, {
        "source": "prior_completed_nflverse_regular_season",
        "defense_position_pairs": len(factors),
        "game_position_observations": len(game_totals),
        "cap": [0.85, 1.15],
        "shrinkage_prior_games": 8,
    }


def _virtual_replacement(
    *, roster_id: str, week: int, slot: str, index: int, baselines: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    position = _replacement_position(slot, baselines)
    baseline = baselines.get(position) or {
        "mean": 0.0,
        "standard_deviation": 1.0,
        "source": "missing",
    }
    return {
        "player_id": f"replacement:{roster_id}:{week}:{index}:{position}",
        "position": position,
        "eligible_positions": [position],
        "team": "",
        "mean": _number(baseline.get("mean")),
        "conditional_mean": _number(baseline.get("mean")),
        "standard_deviation": max(_number(baseline.get("standard_deviation"), 3.0), 0.5),
        "availability_probability": 1.0,
        "modeled_replacement": True,
        "projection_source": baseline.get("source"),
        "game_state": "scheduled",
        "game_id": None,
        "opponent": None,
    }


def _complete_lineup(
    candidates: list[dict[str, Any]],
    slots: list[str],
    *,
    bye_teams: set[str],
    baselines: dict[str, dict[str, Any]],
    roster_id: str,
    week: int,
) -> tuple[list[dict[str, Any]] | None, int]:
    lineup = _best_lineup(candidates, slots, bye_teams=bye_teams)
    if lineup is not None:
        return lineup, 0
    augmented = list(candidates)
    for index, slot in enumerate(slots):
        augmented.append(
            _virtual_replacement(
                roster_id=roster_id,
                week=week,
                slot=slot,
                index=index,
                baselines=baselines,
            )
        )
    lineup = _best_lineup(augmented, slots, bye_teams=bye_teams)
    replacement_count = sum(bool(player.get("modeled_replacement")) for player in lineup or [])
    return lineup, replacement_count


def _correlation_weights(player: dict[str, Any]) -> dict[str, float]:
    position = _position(player.get("position"))
    team = _normalize_team(player.get("team"))
    game_id = str(player.get("game_id") or "")
    weights: dict[str, float] = {}
    if position == "DEF":
        if team:
            weights[f"nfl-defense:{team}"] = 0.28
        if game_id:
            weights[f"nfl-game:{game_id}"] = -0.12
        return weights
    if team:
        weights[f"nfl-offense:{team}"] = 0.18 if position != "K" else 0.15
    if game_id:
        weights[f"nfl-game:{game_id}"] = 0.10
    if team and position in {"QB", "WR", "TE"}:
        weights[f"passing-game:{team}"] = 0.22
    if team and position == "RB":
        weights[f"backfield:{team}"] = 0.15
    return weights


def _week_distribution(
    lineup: list[dict[str, Any]],
    slots: list[str],
    candidates: list[dict[str, Any]],
    *,
    baselines: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    selected_ids = {str(player.get("player_id")) for player in lineup}
    fixed_points = 0.0
    expected_points = 0.0
    idiosyncratic_variance = 0.0
    factor_loadings: dict[str, float] = defaultdict(float)
    availability_variance = 0.0
    scenarios: list[dict[str, Any]] = []
    states: dict[str, int] = defaultdict(int)
    modeled_replacements = 0
    inferred_projections = 0

    for slot, player in zip(slots, lineup, strict=True):
        state = str(player.get("game_state") or "scheduled")
        states[state] += 1
        modeled_replacements += int(bool(player.get("modeled_replacement")))
        inferred_projections += int(bool(player.get("inferred_projection")))
        actual = _number(player.get("actual_points"))
        conditional_mean = _number(player.get("conditional_mean"), _number(player.get("mean")))
        deviation = max(_number(player.get("standard_deviation"), 5.0), 0.1)
        if state == "final":
            fixed_points += actual
            expected_points += actual
            continue
        if state == "in_progress":
            fixed_points += actual
            expected_points += actual

        probability = min(max(_number(player.get("availability_probability"), 1.0), 0), 1)
        replacement_position = _replacement_position(slot, baselines)
        replacement = _number((baselines.get(replacement_position) or {}).get("mean"))
        bench = [
            _number(candidate.get("conditional_mean"), _number(candidate.get("mean")))
            for candidate in candidates
            if str(candidate.get("player_id")) not in selected_ids
            and not candidate.get("modeled_replacement")
            and _eligible(
                candidate.get("eligible_positions") or candidate.get("position") or "", slot
            )
        ]
        if bench:
            replacement = max(replacement, max(bench))
        replacement = min(replacement, conditional_mean)
        expected = probability * conditional_mean + (1 - probability) * replacement
        expected_points += expected
        if probability < 0.999 and state == "scheduled" and not player.get("modeled_replacement"):
            scenario_variance = (
                probability * (1 - probability) * (conditional_mean - replacement) ** 2
            )
            if player.get("explicit_availability_risk"):
                scenarios.append(
                    {
                        "player_id": str(player.get("player_id")),
                        "availability_probability": probability,
                        "available_mean": round(conditional_mean, 3),
                        "replacement_mean": round(replacement, 3),
                        "expected_mean": round(expected, 3),
                    }
                )
                availability_variance += scenario_variance
            else:
                idiosyncratic_variance += scenario_variance
        weights = _correlation_weights(player)
        weight_square_sum = sum(weight**2 for weight in weights.values())
        residual_weight = math.sqrt(max(1 - weight_square_sum, 0.25))
        idiosyncratic_variance += (deviation * residual_weight) ** 2
        for factor, weight in weights.items():
            factor_loadings[factor] += deviation * weight

    total_variance = (
        idiosyncratic_variance
        + sum(value**2 for value in factor_loadings.values())
        + availability_variance
    )
    return {
        "mean": round(expected_points, 3),
        "standard_deviation": round(math.sqrt(max(total_variance, 0.01)), 3),
        "simulation_standard_deviation": round(
            math.sqrt(
                max(
                    idiosyncratic_variance + sum(value**2 for value in factor_loadings.values()),
                    0.01,
                )
            ),
            3,
        ),
        "fixed_points": round(fixed_points, 3),
        "remaining_mean": round(max(expected_points - fixed_points, 0.0), 3),
        "idiosyncratic_standard_deviation": round(math.sqrt(max(idiosyncratic_variance, 0.01)), 3),
        "factor_loadings": {key: round(value, 4) for key, value in factor_loadings.items()},
        "availability_scenarios": scenarios,
        "starter_ids": [str(player.get("player_id")) for player in lineup],
        "game_states": dict(states),
        "modeled_replacement_count": modeled_replacements,
        "inferred_projection_count": inferred_projections,
    }


def build_championship_outlook(
    context: dict[str, Any],
    season_context: dict[str, Any],
    nflverse: dict[str, Any],
    *,
    simulations: int = 10_000,
    seed: int = 20260909,
    calibration: dict[str, Any] | None = None,
    include_counterfactuals: bool = True,
    counterfactual_simulations: int = 1_000,
) -> dict[str, Any]:
    """Create a fail-closed league simulation from versioned projection evidence."""

    playoff_settings = (context.get("league") or {}).get("playoff_settings") or {}
    unsupported_playoff_settings = {
        key: playoff_settings.get(key)
        for key in (
            "playoff_type",
            "playoff_round_type",
            "playoff_seed_type",
            "league_average_match",
        )
        if playoff_settings.get(key) not in {None, 0, "0"}
    }
    if unsupported_playoff_settings:
        return {
            "status": "blocked",
            "model_version": SIMULATION_VERSION,
            "blockers": [
                "The league uses playoff or median-game rules that this simulation does not "
                "yet model"
            ],
            "unsupported_playoff_settings": unsupported_playoff_settings,
        }
    current_week = int(context.get("week") or 0)
    playoff_start = int(
        season_context.get("playoff_start_week") or playoff_settings.get("playoff_week_start") or 0
    )
    championship_week = int(season_context.get("championship_week") or 0)
    matchup_schedule = season_context.get("matchups_by_week") or {}
    if current_week <= 0 or playoff_start <= current_week or championship_week < playoff_start:
        return {
            "status": "blocked",
            "model_version": SIMULATION_VERSION,
            "blockers": ["Complete current-week and playoff schedule metadata is unavailable"],
        }
    if not matchup_schedule:
        return {
            "status": "blocked",
            "model_version": SIMULATION_VERSION,
            "blockers": ["The complete Sleeper fantasy matchup schedule is unavailable"],
        }

    weeks = range(current_week, championship_week + 1)
    schedule_rows = nflverse.get("season_schedule") or nflverse.get("week_schedule") or []
    schedule = _schedule_index(schedule_rows)
    byes = _bye_teams_by_week(schedule_rows, weeks)
    slots = list((context.get("league") or {}).get("starter_slots") or [])
    if not slots:
        return {
            "status": "blocked",
            "model_version": SIMULATION_VERSION,
            "blockers": ["The league starter-slot configuration is unavailable"],
        }
    scoring_settings = (context.get("league") or {}).get("scoring_settings") or {}
    baselines = _replacement_baselines(context)
    anchors = _position_anchors(context, baselines)
    opponent_factors, opponent_factor_evidence = _opponent_factors(nflverse, scoring_settings)
    current_matchups = {
        str(row.get("roster_id")): row
        for row in matchup_schedule.get(str(current_week), [])
        if row.get("roster_id") is not None
    }
    try:
        as_of = datetime.fromisoformat(str(context.get("as_of") or ""))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        as_of = as_of.astimezone(UTC)
    except ValueError:
        as_of = datetime.now(UTC)

    missing: list[dict[str, Any]] = []
    teams: list[dict[str, Any]] = []
    total_slots = 0
    grounded_slots = 0
    modeled_replacement_slots = 0
    inferred_projection_slots = 0
    opponent_adjusted_slots = 0
    live_totals: dict[str, float] = defaultdict(float)
    live_states: dict[str, int] = defaultdict(int)
    live_by_roster: dict[str, dict[str, Any]] = {}
    availability_scenario_count = 0
    correlation_factor_count = 0
    for roster in context.get("league_rosters") or []:
        roster_id = str(roster.get("roster_id"))
        base_candidates = []
        for player in roster.get("players") or []:
            mean, deviation = _projection(player)
            position = _position(player.get("position"))
            inferred = mean is None
            if mean is None:
                mean = anchors.get(position, _number((baselines.get(position) or {}).get("mean")))
                mean *= 0.85
            base_candidates.append(
                {
                    "player_id": str(player.get("player_id") or ""),
                    "position": position,
                    "eligible_positions": list(
                        player.get("eligible_positions") or [player.get("position") or ""]
                    ),
                    "team": _normalize_team(player.get("team")),
                    "injury_status": player.get("injury_status"),
                    "practice_status": player.get("practice_status"),
                    "mean": float(mean),
                    "standard_deviation": float(deviation or 5.0),
                    "inferred_projection": inferred,
                    "projection_source": (
                        "position_anchor_shadow_fallback" if inferred else "projection_ensemble"
                    ),
                }
            )
        weekly = {}
        for week in weeks:
            horizon_weeks = max(week - current_week, 0)
            matchup = current_matchups.get(roster_id) if week == current_week else None
            points_by_player = {
                str(player_id): _number(points)
                for player_id, points in ((matchup or {}).get("players_points") or {}).items()
            }
            week_candidates: list[dict[str, Any]] = []
            for base in base_candidates:
                player = dict(base)
                team = str(player.get("team") or "")
                game_row = schedule.get((week, team))
                game = _game_metadata(game_row, team)
                position = _position(player.get("position"))
                base_mean = _number(player.get("mean"))
                anchor = anchors.get(position, base_mean)
                retained_signal = math.exp(-0.08 * horizon_weeks)
                decayed_mean = anchor + (base_mean - anchor) * retained_signal
                opponent_factor = opponent_factors.get(
                    (str(game.get("opponent") or ""), position), 1.0
                )
                conditional_mean = max(decayed_mean * opponent_factor, 0.0)
                state = "scheduled"
                actual_points = points_by_player.get(str(player.get("player_id")), 0.0)
                kickoff_raw = game.get("kickoff")
                kickoff = datetime.fromisoformat(str(kickoff_raw)) if kickoff_raw else None
                if not game_row:
                    state = "bye"
                elif week == current_week and game.get("final"):
                    state = "final"
                    conditional_mean = 0.0
                elif week == current_week and kickoff and kickoff <= as_of:
                    state = "in_progress"
                    conditional_mean = max(conditional_mean - actual_points, 0.0)
                    remaining_share = min(max(conditional_mean / max(decayed_mean, 0.1), 0.08), 1.0)
                    player["standard_deviation"] = max(
                        _number(player.get("standard_deviation")) * math.sqrt(remaining_share),
                        0.5,
                    )
                probability = (
                    1.0
                    if state in {"final", "in_progress"}
                    else _availability_probability(player, horizon_weeks=horizon_weeks)
                )
                replacement_position = position if position in baselines else "WR"
                replacement_mean = _number((baselines.get(replacement_position) or {}).get("mean"))
                selection_mean = (
                    actual_points
                    if state == "final"
                    else actual_points + conditional_mean
                    if state == "in_progress"
                    else probability * conditional_mean
                    + (1 - probability) * min(replacement_mean, conditional_mean)
                )
                player.update(
                    {
                        "mean": round(selection_mean, 3),
                        "conditional_mean": round(conditional_mean, 3),
                        "availability_probability": probability,
                        "explicit_availability_risk": _has_explicit_availability_risk(player),
                        "actual_points": round(actual_points, 3),
                        "game_state": state,
                        "game_id": game.get("game_id"),
                        "opponent": game.get("opponent"),
                        "kickoff": game.get("kickoff"),
                        "opponent_factor": opponent_factor,
                        "multi_week_retained_signal": round(retained_signal, 4),
                    }
                )
                week_candidates.append(player)

            lineup: list[dict[str, Any]] | None = None
            replacement_count = 0
            if week == current_week and matchup:
                by_player_id = {str(player.get("player_id")): player for player in week_candidates}
                starter_ids = [
                    str(item) for item in matchup.get("starters") or roster.get("starters") or []
                ]
                exact_lineup = [by_player_id.get(player_id) for player_id in starter_ids]
                if len(exact_lineup) == len(slots) and all(exact_lineup):
                    lineup = []
                    for index, player in enumerate(exact_lineup):
                        assert player is not None
                        if player.get("game_state") == "bye":
                            lineup.append(
                                _virtual_replacement(
                                    roster_id=roster_id,
                                    week=week,
                                    slot=slots[index],
                                    index=index,
                                    baselines=baselines,
                                )
                            )
                            replacement_count += 1
                        else:
                            lineup.append(player)
            if lineup is None:
                lineup, replacement_count = _complete_lineup(
                    week_candidates,
                    slots,
                    bye_teams=byes.get(week, set()),
                    baselines=baselines,
                    roster_id=roster_id,
                    week=week,
                )
            if lineup is None:
                missing.append(
                    {
                        "roster_id": roster.get("roster_id"),
                        "week": week,
                        "reason": "No complete legal lineup has projection evidence",
                    }
                )
                continue
            distribution = _week_distribution(lineup, slots, week_candidates, baselines=baselines)
            horizon_multiplier = math.sqrt(1 + 0.08 * horizon_weeks)
            distribution["standard_deviation"] = round(
                _number(distribution.get("standard_deviation")) * horizon_multiplier, 3
            )
            distribution["simulation_standard_deviation"] = round(
                _number(distribution.get("simulation_standard_deviation")) * horizon_multiplier,
                3,
            )
            distribution["idiosyncratic_standard_deviation"] = round(
                _number(distribution.get("idiosyncratic_standard_deviation")) * horizon_multiplier,
                3,
            )
            distribution["factor_loadings"] = {
                key: round(_number(value) * horizon_multiplier, 4)
                for key, value in distribution["factor_loadings"].items()
            }
            distribution["horizon_uncertainty_multiplier"] = round(horizon_multiplier, 4)
            weekly[str(week)] = distribution
            total_slots += len(slots)
            modeled_replacement_slots += int(distribution["modeled_replacement_count"])
            inferred_projection_slots += int(distribution["inferred_projection_count"])
            grounded_slots += len(slots) - int(distribution["inferred_projection_count"])
            opponent_adjusted_slots += sum(
                abs(_number(player.get("opponent_factor"), 1.0) - 1.0) > 0.0001 for player in lineup
            )
            availability_scenario_count += len(distribution["availability_scenarios"])
            correlation_factor_count += len(distribution["factor_loadings"])
            if week == current_week:
                live_totals["fixed_points"] += _number(distribution.get("fixed_points"))
                live_totals["remaining_mean"] += _number(distribution.get("remaining_mean"))
                live_by_roster[roster_id] = {
                    "fixed_points": _number(distribution.get("fixed_points")),
                    "remaining_mean": _number(distribution.get("remaining_mean")),
                    "player_states": distribution.get("game_states") or {},
                }
                for state, count in distribution.get("game_states", {}).items():
                    live_states[state] += int(count)
        record = roster.get("record") or {}
        teams.append(
            {
                "roster_id": roster_id,
                "wins": _number(record.get("wins")),
                "losses": _number(record.get("losses")),
                "ties": _number(record.get("ties")),
                "points_for": _number(record.get("fpts"))
                + _number(record.get("fpts_decimal")) / 100,
                "weekly": weekly,
            }
        )
    required_team_weeks = len(teams) * len(list(weeks))
    structural_coverage = (
        sum(len(team["weekly"]) for team in teams) / required_team_weeks
        if required_team_weeks
        else 0.0
    )
    if structural_coverage < 0.95:
        return {
            "status": "blocked",
            "model_version": SIMULATION_VERSION,
            "projection_coverage": round(grounded_slots / total_slots, 4) if total_slots else 0.0,
            "structural_coverage": round(structural_coverage, 4),
            "blockers": [
                "Fewer than 95% of league team-weeks have a complete projected legal lineup"
            ],
            "evidence_gaps": missing[:50],
            "input_hash": content_hash({"teams": teams, "matchups": matchup_schedule}),
        }
    try:
        result = simulate_championship(
            teams=teams,
            matchups_by_week=matchup_schedule,
            current_week=current_week,
            playoff_start_week=playoff_start,
            championship_week=championship_week,
            playoff_teams=int(playoff_settings.get("playoff_teams") or 6),
            simulations=simulations,
            seed=seed,
        )
    except ValueError as exc:
        return {
            "status": "blocked",
            "model_version": SIMULATION_VERSION,
            "blockers": [str(exc)],
        }
    result["projection_coverage"] = round(grounded_slots / total_slots, 4) if total_slots else 0.0
    result["structural_coverage"] = round(structural_coverage, 4)
    result["modeled_replacement_share"] = (
        round(modeled_replacement_slots / total_slots, 4) if total_slots else 0.0
    )
    result["inferred_projection_share"] = (
        round(inferred_projection_slots / total_slots, 4) if total_slots else 0.0
    )
    feature_qualification = (calibration or {}).get("feature_qualification") or {}

    def qualification(feature: str) -> str:
        row = feature_qualification.get(feature) or {}
        return (
            "qualified" if row.get("qualification_status") == "qualified" else "shadow_unvalidated"
        )

    result["decision_features"] = {
        "live_score_and_remaining_players": {
            "implemented": bool(current_matchups and schedule),
            "qualification_status": qualification("live_score_and_remaining_players"),
            "fixed_points": round(live_totals["fixed_points"], 3),
            "remaining_mean": round(live_totals["remaining_mean"], 3),
            "player_states": dict(live_states),
        },
        "injury_availability_scenarios": {
            "implemented": availability_scenario_count > 0,
            "qualification_status": qualification("injury_availability_scenarios"),
            "scenario_count": availability_scenario_count,
            "method": "status/practice-conditioned Bernoulli with roster/free-agent replacement",
        },
        "cross_player_correlations": {
            "implemented": correlation_factor_count > 0,
            "qualification_status": qualification("cross_player_correlations"),
            "factor_count": correlation_factor_count,
            "factors": [
                "NFL offense",
                "NFL defense",
                "game environment",
                "passing game",
                "backfield",
            ],
        },
        "future_opponent_effects": {
            "implemented": bool(opponent_factors and opponent_adjusted_slots),
            "qualification_status": qualification("future_opponent_effects"),
            "adjusted_starter_slots": opponent_adjusted_slots,
            **opponent_factor_evidence,
        },
        "multi_week_projection_decay": {
            "implemented": championship_week > current_week,
            "qualification_status": qualification("multi_week_projection_decay"),
            "weekly_retention_formula": "exp(-0.08 * horizon_weeks)",
            "position_anchors": anchors,
        },
    }
    our_roster_id = str((context.get("our_team") or {}).get("roster_id") or "")
    result["model_diagnostics"] = {
        "as_of": as_of.isoformat(),
        "live_current_week": {
            "fixed_points": round(live_totals["fixed_points"], 3),
            "remaining_mean": round(live_totals["remaining_mean"], 3),
            "player_states": dict(live_states),
            "league_fixed_points": round(live_totals["fixed_points"], 3),
            "league_remaining_mean": round(live_totals["remaining_mean"], 3),
            "league_player_states": dict(live_states),
            "our_team": live_by_roster.get(our_roster_id) or {},
        },
        "replacement_baselines": baselines,
        "modeled_replacement_slots": modeled_replacement_slots,
        "inferred_projection_slots": inferred_projection_slots,
        "opponent_adjusted_slots": opponent_adjusted_slots,
        "availability_scenarios": availability_scenario_count,
        "correlation_factors": correlation_factor_count,
        "evidence_gaps": missing[:50],
    }
    result["our_team"] = (result.get("teams") or {}).get(our_roster_id)
    if include_counterfactuals and our_roster_id and result.get("our_team"):
        result["roster_move_counterfactuals"] = _roster_move_counterfactuals(
            context,
            season_context,
            nflverse,
            roster_id=our_roster_id,
            seed=seed,
            simulations=max(counterfactual_simulations, 200),
            calibration=calibration,
        )
    return result


def _roster_move_counterfactuals(
    context: dict[str, Any],
    season_context: dict[str, Any],
    nflverse: dict[str, Any],
    *,
    roster_id: str,
    seed: int,
    simulations: int,
    calibration: dict[str, Any] | None,
) -> dict[str, Any]:
    """Screen legal add/drop pairs, then resimulate the best three with common randomness."""

    protected = {
        str(player_id)
        for player_id in (
            (context.get("manual_constraints") or {}).get("protected_player_ids") or []
        )
    }
    starters = {
        str(row.get("player_id"))
        for row in ((context.get("our_team") or {}).get("current_lineup") or [])
    }
    bench = [
        row
        for row in ((context.get("our_team") or {}).get("bench") or [])
        if str(row.get("player_id") or "") not in protected | starters
    ]
    free_agents = [
        row
        for row in (context.get("free_agent_candidates") or [])
        if row.get("sleeper_acquisition_type") in {"free_agent", "waiver"}
    ]
    if not bench or not free_agents:
        return {
            "status": "not_applicable",
            "reason": (
                "No unprotected bench and Sleeper-observed free-agent/waiver add-drop pairs "
                "are available"
            ),
            "simulation_count_per_scenario": simulations,
            "candidates": [],
        }

    def projected_mean(player: dict[str, Any]) -> float:
        mean, _ = _projection(player)
        return _number(mean, -100.0)

    pairs = []
    for add in free_agents:
        add_mean = projected_mean(add)
        if add_mean <= -99:
            continue
        for drop in bench:
            drop_mean = projected_mean(drop)
            if drop_mean <= -99 or add.get("player_id") == drop.get("player_id"):
                continue
            pairs.append((add_mean - drop_mean, add, drop))
    selected = []
    used_positions: set[str] = set()
    for proxy, add, drop in sorted(pairs, key=lambda item: item[0], reverse=True):
        position = _position(add.get("position"))
        if position in used_positions:
            continue
        selected.append((proxy, add, drop))
        used_positions.add(position)
        if len(selected) == 3:
            break
    if not selected:
        return {
            "status": "not_applicable",
            "reason": "No projected add/drop pairs could be scored",
            "simulation_count_per_scenario": simulations,
            "candidates": [],
        }

    baseline = build_championship_outlook(
        context,
        season_context,
        nflverse,
        simulations=simulations,
        seed=seed,
        calibration=calibration,
        include_counterfactuals=False,
    )
    candidates = []
    for proxy, add, drop in selected:
        add_id = str(add.get("player_id") or "")
        drop_id = str(drop.get("player_id") or "")
        alternative_context = deepcopy(context)
        for roster in alternative_context.get("league_rosters") or []:
            if str(roster.get("roster_id") or "") != roster_id:
                continue
            roster["players"] = [
                player
                for player in (roster.get("players") or [])
                if str(player.get("player_id") or "") != drop_id
            ] + [deepcopy(add)]
        our_team = alternative_context.get("our_team") or {}
        our_team["all_players"] = [
            player
            for player in (our_team.get("all_players") or [])
            if str(player.get("player_id") or "") != drop_id
        ] + [deepcopy(add)]
        our_team["bench"] = [
            player
            for player in (our_team.get("bench") or [])
            if str(player.get("player_id") or "") != drop_id
        ] + [deepcopy(add)]
        alternative = build_championship_outlook(
            alternative_context,
            season_context,
            nflverse,
            simulations=simulations,
            seed=seed,
            calibration=calibration,
            include_counterfactuals=False,
        )
        if baseline.get("status") != "complete" or alternative.get("status") != "complete":
            continue
        candidates.append(
            {
                "add_player_id": add_id,
                "add_player_name": add.get("name") or add.get("full_name") or add_id,
                "drop_player_id": drop_id,
                "drop_player_name": drop.get("name") or drop.get("full_name") or drop_id,
                "acquisition_type": add.get("sleeper_acquisition_type") or "unknown",
                "projection_proxy_delta": round(proxy, 3),
                **compare_counterfactuals(baseline, alternative, roster_id=roster_id),
            }
        )
    candidates.sort(key=lambda row: row["championship_probability_delta"], reverse=True)
    return {
        "status": "complete",
        "authority": "analysis_only",
        "simulation_count_per_scenario": simulations,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "note": "No transaction is executed; statistically equivalent scenarios require manager judgment.",
    }
