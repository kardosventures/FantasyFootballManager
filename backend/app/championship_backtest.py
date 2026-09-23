from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, median
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.championship import (
    SIMULATION_VERSION,
    _availability_probability,
    _correlation_weights,
    _number,
    _position,
)
from app.config import Settings, get_settings
from app.historical_sources import merge_nflverse_history, read_nflverse_history
from app.models import SimulationRun
from app.readiness import read_report
from app.scoring import score_nflverse_player_week
from app.utils import canonical_json, content_hash

BACKTEST_VERSION = "championship-backtest-2026.1"
POSITIONS = {"QB", "RB", "WR", "TE", "K"}


def _probability_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    if not observations:
        return {
            "samples": 0,
            "brier_score": None,
            "log_loss": None,
            "calibration_error": None,
            "reliability": [],
        }
    bins: dict[int, list[dict[str, Any]]] = defaultdict(list)
    brier = []
    log_losses = []
    for row in observations:
        predicted = min(max(float(row["predicted"]), 0.000001), 0.999999)
        actual = float(row["actual"])
        brier.append((predicted - actual) ** 2)
        log_losses.append(-(actual * math.log(predicted) + (1 - actual) * math.log(1 - predicted)))
        bins[min(int(predicted * 10), 9)].append(row)
    reliability = []
    for bucket, rows in sorted(bins.items()):
        predicted = fmean(float(row["predicted"]) for row in rows)
        actual = fmean(float(row["actual"]) for row in rows)
        reliability.append(
            {
                "range": [bucket / 10, (bucket + 1) / 10],
                "samples": len(rows),
                "mean_prediction": round(predicted, 4),
                "observed_rate": round(actual, 4),
                "absolute_gap": round(abs(predicted - actual), 4),
            }
        )
    calibration_error = sum(
        row["samples"] / len(observations) * row["absolute_gap"] for row in reliability
    )
    return {
        "samples": len(observations),
        "brier_score": round(fmean(brier), 5),
        "log_loss": round(fmean(log_losses), 5),
        "calibration_error": round(calibration_error, 5),
        "reliability": reliability,
    }


def _injury_calibration(report: dict[str, Any]) -> dict[str, Any]:
    active = {
        (str(row.get("player_id") or ""), int(row.get("season") or 0), int(row.get("week") or 0))
        for row in report.get("historical_player_stats") or []
        if _position(row.get("position")) in POSITIONS
        and str(row.get("season_type") or "REG").upper() == "REG"
        and str(row.get("week") or "").isdigit()
    }
    observations = []
    seen = set()
    role_not_established = 0
    for row in report.get("historical_injuries") or []:
        position = _position(row.get("position"))
        player_id = str(row.get("gsis_id") or "")
        try:
            season = int(row.get("season"))
            week = int(row.get("week"))
        except (TypeError, ValueError):
            continue
        key = (player_id, season, week)
        if not player_id or position not in POSITIONS or key in seen:
            continue
        seen.add(key)
        if not any(
            (player_id, season, prior_week) in active
            for prior_week in range(max(1, week - 3), week)
        ):
            role_not_established += 1
            continue
        observations.append(
            {
                "predicted": _availability_probability(
                    {
                        "position": position,
                        "injury_status": row.get("report_status"),
                        "practice_status": row.get("practice_status"),
                    },
                    horizon_weeks=0,
                ),
                "actual": 1.0 if key in active else 0.0,
            }
        )
    metrics = _probability_metrics(observations)
    actual_rate = fmean(row["actual"] for row in observations) if observations else None
    baseline = (
        _probability_metrics(
            [{"predicted": actual_rate, "actual": row["actual"]} for row in observations]
        )
        if actual_rate is not None
        else _probability_metrics([])
    )
    qualified = bool(
        metrics["samples"] >= 250
        and metrics["calibration_error"] is not None
        and metrics["calibration_error"] <= 0.08
        and metrics["brier_score"] is not None
        and baseline["brier_score"] is not None
        and metrics["brier_score"] <= baseline["brier_score"]
    )
    return {
        "qualification_status": "qualified" if qualified else "blocked",
        "samples": metrics["samples"],
        "model": metrics,
        "constant_base_rate_baseline": baseline,
        "observed_active_rate": round(actual_rate, 4) if actual_rate is not None else None,
        "checks": {
            "minimum_samples": metrics["samples"] >= 250,
            "maximum_calibration_error_0_08": (
                metrics["calibration_error"] is not None and metrics["calibration_error"] <= 0.08
            ),
            "brier_not_worse_than_base_rate": (
                metrics["brier_score"] is not None
                and baseline["brier_score"] is not None
                and metrics["brier_score"] <= baseline["brier_score"]
            ),
        },
        "excluded_without_prior_three_week_role": role_not_established,
        "limitation": (
            "Participation is observed from a matching nflverse skill-player stat row; the cohort "
            "requires a strictly prior three-week role so backup inactivity is not mislabeled as "
            "injury unavailability."
        ),
    }


def _historical_scored_rows(
    report: dict[str, Any], scoring: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = []
    for raw in report.get("historical_player_stats") or []:
        position = _position(raw.get("position"))
        try:
            season = int(raw.get("season"))
            week = int(raw.get("week"))
        except (TypeError, ValueError):
            continue
        player_id = str(raw.get("player_id") or "")
        game_id = str(raw.get("game_id") or "")
        team = str(raw.get("team") or "").upper()
        opponent = str(raw.get("opponent_team") or "").upper()
        if (
            position not in POSITIONS
            or not player_id
            or not game_id
            or not team
            or not opponent
            or str(raw.get("season_type") or "REG").upper() != "REG"
        ):
            continue
        rows.append(
            {
                "player_id": player_id,
                "position": position,
                "season": season,
                "week": week,
                "game_id": game_id,
                "team": team,
                "opponent": opponent,
                "points": score_nflverse_player_week(raw, scoring),
            }
        )
    return sorted(rows, key=lambda row: (row["season"], row["week"], row["game_id"]))


def _opponent_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    game_totals: dict[tuple[int, int, str, str, str], float] = defaultdict(float)
    for row in rows:
        game_totals[
            (row["season"], row["week"], row["game_id"], row["opponent"], row["position"])
        ] += row["points"]
    histories: dict[str, list[float]] = defaultdict(list)
    defense_histories: dict[tuple[str, str], list[float]] = defaultdict(list)
    observations = []
    for (season, week, game_id, defense, position), actual in sorted(game_totals.items()):
        prior = histories[position]
        defense_prior = defense_histories[(defense, position)]
        if len(prior) >= 32 and len(defense_prior) >= 3:
            baseline = fmean(prior)
            raw_factor = fmean(defense_prior) / max(baseline, 0.01)
            shrinkage = len(defense_prior) / (len(defense_prior) + 8)
            factor = min(max(1 + (raw_factor - 1) * shrinkage, 0.85), 1.15)
            observations.append(
                {
                    "season": season,
                    "week": week,
                    "game_id": game_id,
                    "actual": actual,
                    "baseline": baseline,
                    "adjusted": baseline * factor,
                }
            )
        prior.append(actual)
        defense_prior.append(actual)
    baseline_mae = (
        fmean(abs(row["baseline"] - row["actual"]) for row in observations)
        if observations
        else None
    )
    adjusted_mae = (
        fmean(abs(row["adjusted"] - row["actual"]) for row in observations)
        if observations
        else None
    )
    improvement = (
        (baseline_mae - adjusted_mae) / baseline_mae
        if baseline_mae and adjusted_mae is not None
        else None
    )
    qualified = bool(len(observations) >= 500 and improvement is not None and improvement >= 0)
    return {
        "qualification_status": "qualified" if qualified else "blocked",
        "samples": len(observations),
        "walk_forward": True,
        "baseline_mae": round(baseline_mae, 4) if baseline_mae is not None else None,
        "adjusted_mae": round(adjusted_mae, 4) if adjusted_mae is not None else None,
        "relative_mae_improvement": round(improvement, 5) if improvement is not None else None,
        "checks": {
            "minimum_samples": len(observations) >= 500,
            "not_worse_than_unadjusted": improvement is not None and improvement >= 0,
        },
    }


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left_mean = fmean(left for left, _ in pairs)
    right_mean = fmean(right for _, right in pairs)
    covariance = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_variance = sum((left - left_mean) ** 2 for left, _ in pairs)
    right_variance = sum((right - right_mean) ** 2 for _, right in pairs)
    if left_variance <= 0 or right_variance <= 0:
        return None
    return covariance / math.sqrt(left_variance * right_variance)


def _pair_class(first: dict[str, Any], second: dict[str, Any]) -> str:
    if first["team"] != second["team"]:
        return "opposing_teams"
    positions = {first["position"], second["position"]}
    if "QB" in positions and bool(positions & {"WR", "TE"}):
        return "passing_stack"
    if first["position"] == second["position"] == "RB":
        return "same_backfield"
    return "same_offense"


def _correlation_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    histories: dict[str, list[float]] = defaultdict(list)
    game_rows: dict[tuple[int, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        history = histories[row["player_id"]]
        if len(history) >= 3:
            game_rows[(row["season"], row["week"], row["game_id"])].append(
                {**row, "residual": row["points"] - fmean(history[-6:])}
            )
        history.append(row["points"])
    pairs_by_class: dict[str, list[tuple[float, float]]] = defaultdict(list)
    implied_by_class: dict[str, list[float]] = defaultdict(list)
    for players in game_rows.values():
        for first_index, first in enumerate(players):
            for second in players[first_index + 1 :]:
                pair_class = _pair_class(first, second)
                pairs_by_class[pair_class].append((first["residual"], second["residual"]))
                first_weights = _correlation_weights(first)
                second_weights = _correlation_weights(second)
                implied_by_class[pair_class].append(
                    sum(
                        weight * _number(second_weights.get(factor))
                        for factor, weight in first_weights.items()
                    )
                )
    results = {}
    squared_errors = []
    sample_total = 0
    for pair_class, pairs in sorted(pairs_by_class.items()):
        empirical = _pearson(pairs)
        implied = fmean(implied_by_class[pair_class]) if implied_by_class[pair_class] else None
        if empirical is not None and implied is not None:
            squared_errors.append((empirical - implied) ** 2)
        sample_total += len(pairs)
        results[pair_class] = {
            "samples": len(pairs),
            "empirical_residual_correlation": round(empirical, 4)
            if empirical is not None
            else None,
            "model_implied_correlation": round(implied, 4) if implied is not None else None,
        }
    rmse = math.sqrt(fmean(squared_errors)) if squared_errors else None
    qualified = bool(sample_total >= 1_000 and rmse is not None and rmse <= 0.12)
    return {
        "qualification_status": "qualified" if qualified else "blocked",
        "samples": sample_total,
        "pair_classes": results,
        "correlation_rmse": round(rmse, 5) if rmse is not None else None,
        "checks": {
            "minimum_samples": sample_total >= 1_000,
            "maximum_rmse_0_12": rmse is not None and rmse <= 0.12,
        },
    }


def _decay_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_position_time: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for row in rows:
        by_player[row["player_id"]].append(row)
        by_position_time[(row["position"], row["season"], row["week"])].append(row["points"])
    anchors: dict[tuple[str, int, int], float] = {}
    for position in POSITIONS:
        cumulative: list[float] = []
        times = sorted(
            (season, week)
            for observed_position, season, week in by_position_time
            if observed_position == position
        )
        for season, week in times:
            if cumulative:
                anchors[(position, season, week)] = median(cumulative)
            cumulative.extend(by_position_time[(position, season, week)])
    observations = []
    for player_rows in by_player.values():
        ordered = sorted(player_rows, key=lambda row: (row["season"], row["week"]))
        for index in range(3, len(ordered)):
            history = [row for row in ordered[:index] if row["season"] == ordered[index]["season"]]
            if len(history) < 3:
                continue
            player_level = fmean(row["points"] for row in history[-3:])
            anchor = anchors.get(
                (ordered[index]["position"], ordered[index]["season"], ordered[index]["week"])
            )
            if anchor is None:
                continue
            horizon = max(ordered[index]["week"] - history[-1]["week"], 1)
            observations.append(
                {
                    "actual": ordered[index]["points"],
                    "no_decay": player_level,
                    "decay": anchor + (player_level - anchor) * math.exp(-0.08 * horizon),
                    "horizon": horizon,
                }
            )
    no_decay_mae = (
        fmean(abs(row["no_decay"] - row["actual"]) for row in observations)
        if observations
        else None
    )
    decay_mae = (
        fmean(abs(row["decay"] - row["actual"]) for row in observations) if observations else None
    )
    relative = (
        (no_decay_mae - decay_mae) / no_decay_mae
        if no_decay_mae and decay_mae is not None
        else None
    )
    qualified = bool(len(observations) >= 500 and relative is not None and relative >= -0.01)
    return {
        "qualification_status": "qualified" if qualified else "blocked",
        "samples": len(observations),
        "no_decay_mae": round(no_decay_mae, 4) if no_decay_mae is not None else None,
        "decay_mae": round(decay_mae, 4) if decay_mae is not None else None,
        "relative_mae_improvement": round(relative, 5) if relative is not None else None,
        "formula": "anchor + (trailing_3 - anchor) * exp(-0.08 * horizon_weeks)",
        "checks": {
            "minimum_samples": len(observations) >= 500,
            "no_more_than_one_percent_worse": relative is not None and relative >= -0.01,
        },
    }


def _replacement_calibration(
    rows: list[dict[str, Any]], manager_plan: dict[str, Any], league: dict[str, Any]
) -> dict[str, Any]:
    modeled = ((manager_plan.get("championship_outlook") or {}).get("model_diagnostics") or {}).get(
        "replacement_baselines"
    ) or {}
    team_count = int((league.get("settings") or {}).get("num_teams") or 12)
    ranks = {
        "QB": team_count + 1,
        "RB": team_count * 3,
        "WR": team_count * 4,
        "TE": team_count + 3,
        "K": team_count + 1,
    }
    weekly: dict[tuple[int, int, str], list[float]] = defaultdict(list)
    for row in rows:
        weekly[(row["season"], row["week"], row["position"])].append(row["points"])
    empirical: dict[str, list[float]] = defaultdict(list)
    for (_, _, position), points in weekly.items():
        ordered = sorted(points, reverse=True)
        rank = ranks[position]
        if len(ordered) >= rank:
            empirical[position].append(ordered[rank - 1])
    by_position = {}
    relative_errors = []
    samples = 0
    for position in sorted(POSITIONS):
        values = empirical.get(position) or []
        modeled_mean = _number((modeled.get(position) or {}).get("mean"), math.nan)
        empirical_mean = median(values) if values else None
        relative_error = (
            abs(modeled_mean - empirical_mean) / max(abs(empirical_mean), 1.0)
            if empirical_mean is not None and math.isfinite(modeled_mean)
            else None
        )
        if relative_error is not None:
            relative_errors.append(relative_error)
        samples += len(values)
        by_position[position] = {
            "weekly_samples": len(values),
            "replacement_rank": ranks[position],
            "historical_median": round(empirical_mean, 3) if empirical_mean is not None else None,
            "modeled_mean": round(modeled_mean, 3) if math.isfinite(modeled_mean) else None,
            "absolute_relative_error": round(relative_error, 4)
            if relative_error is not None
            else None,
        }
    mean_relative_error = fmean(relative_errors) if relative_errors else None
    qualified = bool(
        samples >= 50
        and len(relative_errors) == len(POSITIONS)
        and mean_relative_error is not None
        and mean_relative_error <= 0.25
    )
    return {
        "qualification_status": "qualified" if qualified else "blocked",
        "samples": samples,
        "mean_absolute_relative_error": (
            round(mean_relative_error, 4) if mean_relative_error is not None else None
        ),
        "by_position": by_position,
        "checks": {
            "minimum_samples": samples >= 50,
            "all_positions_observed": len(relative_errors) == len(POSITIONS),
            "maximum_mean_relative_error_0_25": (
                mean_relative_error is not None and mean_relative_error <= 0.25
            ),
        },
    }


def _simulation_rows(settings: Settings, league_id: str) -> list[dict[str, Any]]:
    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as session:
            runs = session.scalars(
                select(SimulationRun)
                .where(SimulationRun.league_id == league_id)
                .order_by(SimulationRun.season, SimulationRun.week, SimulationRun.created_at)
            ).all()
            return [
                {
                    "season": row.season,
                    "week": row.week,
                    "created_at": row.created_at.isoformat(),
                    "results": row.results,
                }
                for row in runs
            ]
    except Exception:
        return []
    finally:
        engine.dispose()


def _season_probability_calibration(
    runs: list[dict[str, Any]], season_context: dict[str, Any], current_week: int
) -> dict[str, Any]:
    latest_by_week: dict[tuple[int, int], dict[str, Any]] = {}
    for run in runs:
        key = (int(run["season"]), int(run["week"]))
        latest_by_week[key] = run
    observations = []
    for (_, week), run in latest_by_week.items():
        if week >= current_week:
            continue
        matchup_rows = (season_context.get("matchups_by_week") or {}).get(str(week)) or []
        points = {str(row.get("roster_id")): _number(row.get("points")) for row in matchup_rows}
        for first, second in _matchup_pairs(matchup_rows):
            if first not in points or second not in points:
                continue
            first_actual = (
                1.0
                if points[first] > points[second]
                else 0.5
                if points[first] == points[second]
                else 0.0
            )
            for roster_id, actual in ((first, first_actual), (second, 1 - first_actual)):
                predicted = (
                    ((run.get("results") or {}).get("teams") or {}).get(roster_id) or {}
                ).get("current_week_win_probability")
                if predicted is not None:
                    observations.append({"predicted": predicted, "actual": actual})
    metrics = _probability_metrics(observations)
    qualified = bool(
        metrics["samples"] >= 40
        and metrics["calibration_error"] is not None
        and metrics["calibration_error"] <= 0.10
        and metrics["brier_score"] is not None
        and metrics["brier_score"] <= 0.25
    )
    return {
        "qualification_status": "qualified" if qualified else "blocked",
        "weekly_win_probability": metrics,
        "championship_probability": {
            "samples": 0,
            "status": "pending_until_season_complete",
        },
        "checks": {
            "minimum_weekly_team_outcomes": metrics["samples"] >= 40,
            "maximum_calibration_error_0_10": (
                metrics["calibration_error"] is not None and metrics["calibration_error"] <= 0.10
            ),
            "brier_better_than_coin_flip": (
                metrics["brier_score"] is not None and metrics["brier_score"] <= 0.25
            ),
        },
    }


def _matchup_pairs(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    by_matchup: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        matchup_id = str(row.get("matchup_id") or "")
        roster_id = str(row.get("roster_id") or "")
        if matchup_id and roster_id:
            by_matchup[matchup_id].append(roster_id)
    return [(rosters[0], rosters[1]) for rosters in by_matchup.values() if len(rosters) == 2]


def build_championship_backtest(
    nflverse: dict[str, Any],
    in_season: dict[str, Any],
    season_context: dict[str, Any],
    manager_plan: dict[str, Any],
    simulation_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    scoring = (in_season.get("league") or {}).get("scoring_settings") or {}
    rows = _historical_scored_rows(nflverse, scoring)
    injury = _injury_calibration(nflverse)
    opponent = _opponent_calibration(rows)
    correlation = _correlation_calibration(rows)
    decay = _decay_calibration(rows)
    replacement = _replacement_calibration(rows, manager_plan, in_season.get("league") or {})
    probabilities = _season_probability_calibration(
        simulation_runs, season_context, int(in_season.get("week") or 0)
    )
    feature_qualification = {
        "live_score_and_remaining_players": {
            "qualification_status": probabilities["qualification_status"],
            "evidence": "season_probability_calibration.weekly_win_probability",
        },
        "injury_availability_scenarios": {
            "qualification_status": injury["qualification_status"],
            "evidence": "component_calibration.injury_availability",
        },
        "cross_player_correlations": {
            "qualification_status": correlation["qualification_status"],
            "evidence": "component_calibration.cross_player_correlations",
        },
        "future_opponent_effects": {
            "qualification_status": opponent["qualification_status"],
            "evidence": "component_calibration.future_opponent_effects",
        },
        "multi_week_projection_decay": {
            "qualification_status": decay["qualification_status"],
            "evidence": "component_calibration.multi_week_projection_decay",
        },
    }
    blockers = [
        feature.replace("_", " ")
        for feature, result in feature_qualification.items()
        if result["qualification_status"] != "qualified"
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "backtest_version": BACKTEST_VERSION,
        "simulation_model_version": SIMULATION_VERSION,
        "qualification_status": "qualified" if not blockers else "blocked",
        "qualification_blockers": blockers,
        "feature_qualification": feature_qualification,
        "component_calibration": {
            "injury_availability": injury,
            "cross_player_correlations": correlation,
            "future_opponent_effects": opponent,
            "multi_week_projection_decay": decay,
            "replacement_values": replacement,
        },
        "season_probability_calibration": probabilities,
        "coverage": {
            "historical_scored_player_weeks": len(rows),
            "simulation_snapshots": len(simulation_runs),
            "previous_league_available": bool(
                (in_season.get("league") or {}).get("previous_league_id")
            ),
        },
        "input_hash": content_hash(
            {
                "historical_rows": rows,
                "historical_injuries": nflverse.get("historical_injuries") or [],
                "simulation_runs": simulation_runs,
                "version": BACKTEST_VERSION,
            }
        ),
    }


def write_championship_backtest(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    nflverse = read_report(cfg.reports_dir / "nflverse-context.json", {})
    nflverse = merge_nflverse_history(nflverse, read_nflverse_history(cfg.reports_dir))
    in_season = read_report(cfg.reports_dir / "in-season-context.json", {})
    season_context = read_report(cfg.reports_dir / "sleeper-season-context.json", {})
    manager_plan = read_report(cfg.reports_dir / "in-season-plan.json", {})
    league_id = str((in_season.get("league") or {}).get("league_id") or "")
    simulation_runs = _simulation_rows(cfg, league_id) if league_id else []
    result = build_championship_backtest(
        nflverse, in_season, season_context, manager_plan, simulation_runs
    )
    destination = Path(cfg.reports_dir) / "championship-backtest.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(canonical_json(result) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return result
