from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.historical_sources import merge_nflverse_history, read_nflverse_history
from app.models import ProjectionSnapshot
from app.readiness import read_report
from app.scoring import NFLVERSE_PLAYER_STAT_MAP, score_nflverse_player_week
from app.utils import canonical_json, content_hash

BACKTEST_VERSION = "projection-backtest-2026.2"
ELIGIBLE_POSITIONS = {"QB", "RB", "WR", "TE", "K"}
POSITION_SPREAD = {"QB": 5.0, "RB": 5.5, "WR": 6.0, "TE": 4.5, "K": 3.5}
PROMOTION_MIN_COMPLETED_WEEKS = 2
PROMOTION_MIN_SAMPLES = 40
PROMOTION_MIN_POSITION_SAMPLES = 5
PROMOTION_MIN_INTERVAL_COVERAGE = 0.50
PROMOTION_MAX_INTERVAL_COVERAGE = 0.75
PROMOTION_REQUIRED_MAE_IMPROVEMENT = 0.01


def _metric_rows(observations: list[dict[str, Any]], prediction_key: str) -> dict[str, Any]:
    if not observations:
        return {"samples": 0, "mae": None, "rmse": None, "bias": None}
    errors = [float(row[prediction_key]) - float(row["actual"]) for row in observations]
    return {
        "samples": len(errors),
        "mae": round(fmean(abs(error) for error in errors), 4),
        "rmse": round(math.sqrt(fmean(error * error for error in errors)), 4),
        "bias": round(fmean(errors), 4),
    }


def _candidate_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = _metric_rows(observations, "candidate")
    if not observations:
        return {**metrics, "p20_p80_coverage": None, "mean_interval_width": None}
    covered = sum(row["floor_p20"] <= row["actual"] <= row["ceiling_p80"] for row in observations)
    return {
        **metrics,
        "p20_p80_coverage": round(covered / len(observations), 4),
        "mean_interval_width": round(
            fmean(row["ceiling_p80"] - row["floor_p20"] for row in observations), 4
        ),
    }


def _projection_snapshot_rows(settings: Settings) -> list[dict[str, Any]]:
    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as session:
            snapshots = session.scalars(
                select(ProjectionSnapshot).order_by(
                    ProjectionSnapshot.season,
                    ProjectionSnapshot.week,
                    ProjectionSnapshot.player_id,
                    ProjectionSnapshot.created_at,
                )
            ).all()
            return [
                {
                    "player_id": row.player_id,
                    "season": row.season,
                    "week": row.week,
                    "created_at": row.created_at.isoformat(),
                    "projection": row.projection,
                    "model_version": row.model_version,
                    "input_hash": row.input_hash,
                }
                for row in snapshots
            ]
    finally:
        engine.dispose()


def _kickoff(row: dict[str, Any]) -> datetime | None:
    gameday = str(row.get("gameday") or "")
    gametime = str(row.get("gametime") or "")
    if not gameday or not gametime:
        return None
    try:
        local = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("America/New_York")
        )
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ensemble_observations(
    report: dict[str, Any],
    scoring_settings: dict[str, Any],
    projection_snapshots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    identities = report.get("player_ids") or {}
    actuals: dict[tuple[str, int, int], dict[str, Any]] = {}
    for row in report.get("historical_player_stats") or []:
        try:
            key = (str(row.get("player_id") or ""), int(row["season"]), int(row["week"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key[0] and str(row.get("season_type") or "REG").upper() == "REG":
            actuals[key] = row

    kickoffs: dict[tuple[int, int, str], datetime] = {}
    schedule_by_week: defaultdict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    schedule_rows = [
        *(report.get("historical_schedule") or []),
        *(report.get("season_schedule") or []),
    ]
    for row in schedule_rows:
        observed = _kickoff(row)
        try:
            season = int(row["season"])
            week = int(row["week"])
        except (KeyError, TypeError, ValueError):
            continue
        if str(row.get("game_type") or "REG").upper() == "REG":
            schedule_by_week[(season, week)].append(row)
        if observed is None:
            continue
        for team_key in ("home_team", "away_team"):
            team = str(row.get(team_key) or "").upper()
            if team:
                kickoffs[(season, week, team)] = observed
    completed_weeks = {
        key
        for key, rows in schedule_by_week.items()
        if rows
        and all(
            row.get("home_score") not in {None, ""} and row.get("away_score") not in {None, ""}
            for row in rows
        )
    }

    selected: dict[tuple[str, int, int], dict[str, Any]] = {}
    exclusions: defaultdict[str, int] = defaultdict(int)
    for snapshot in projection_snapshots:
        sleeper_id = str(snapshot.get("player_id") or "")
        identity = identities.get(sleeper_id) or {}
        gsis_id = str(identity.get("gsis_id") or "")
        try:
            season = int(snapshot["season"])
            week = int(snapshot["week"])
            created_at = datetime.fromisoformat(str(snapshot["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            created_at = created_at.astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            exclusions["invalid_snapshot"] += 1
            continue
        if (season, week) not in completed_weeks:
            exclusions["week_not_complete"] += 1
            continue
        actual = actuals.get((gsis_id, season, week))
        if actual is None:
            exclusions["identity_or_outcome_unmatched"] += 1
            continue
        team = str(actual.get("team") or "").upper()
        kickoff = kickoffs.get((season, week, team))
        if kickoff is None:
            exclusions["kickoff_unavailable"] += 1
            continue
        if created_at >= kickoff:
            exclusions["at_or_after_kickoff"] += 1
            continue
        projection = snapshot.get("projection") or {}
        candidate = _float(projection.get("mean"))
        floor = _float(projection.get("floor_p20"))
        ceiling = _float(projection.get("ceiling_p80"))
        if candidate is None or floor is None or ceiling is None:
            exclusions["projection_incomplete"] += 1
            continue
        key = (sleeper_id, season, week)
        previous = selected.get(key)
        if previous is None or created_at.isoformat() > previous["created_at"]:
            selected[key] = {
                "player_id": sleeper_id,
                "position": str(actual.get("position") or identity.get("position") or "").upper(),
                "season": season,
                "week": week,
                "created_at": created_at.isoformat(),
                "kickoff": kickoff.isoformat(),
                "actual": score_nflverse_player_week(actual, scoring_settings),
                "candidate": candidate,
                "floor_p20": floor,
                "ceiling_p80": ceiling,
                "sources": {
                    str(source.get("source")): points
                    for source in projection.get("sources") or []
                    if (points := _float(source.get("points"))) is not None
                },
            }
    return list(selected.values()), dict(exclusions)


def _source_calibration(observations: list[dict[str, Any]]) -> dict[str, Any]:
    source_names = sorted({source for row in observations for source in (row.get("sources") or {})})
    result: dict[str, Any] = {}
    for source in source_names:
        paired = [row for row in observations if source in (row.get("sources") or {})]
        source_rows = [{**row, "source_prediction": row["sources"][source]} for row in paired]
        source_metrics = _metric_rows(source_rows, "source_prediction")
        candidate_metrics = _metric_rows(paired, "candidate")
        delta = (
            round(float(source_metrics["mae"]) - float(candidate_metrics["mae"]), 4)
            if source_metrics["mae"] is not None and candidate_metrics["mae"] is not None
            else None
        )
        result[source] = {
            **source_metrics,
            "paired_candidate_mae": candidate_metrics["mae"],
            "candidate_mae_improvement": delta,
        }
    return result


def _recommended_weights(observations: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    weights: dict[str, dict[str, float]] = {}
    for position in sorted(ELIGIBLE_POSITIONS):
        rows = [row for row in observations if row.get("position") == position]
        source_metrics = _source_calibration(rows)
        raw = {
            source: 1 / max(float(metrics["mae"]), 1.0) ** 2
            for source, metrics in source_metrics.items()
            if int(metrics.get("samples") or 0) >= PROMOTION_MIN_POSITION_SAMPLES
            and metrics.get("mae") is not None
        }
        total = sum(raw.values())
        if total:
            weights[position] = {source: round(value / total, 4) for source, value in raw.items()}
    return weights


def build_ensemble_calibration(
    report: dict[str, Any],
    scoring_settings: dict[str, Any],
    projection_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    observations, exclusions = _ensemble_observations(
        report, scoring_settings, projection_snapshots
    )
    completed_weeks = sorted({f"{row['season']}-W{row['week']:02d}" for row in observations})
    candidate = _candidate_metrics(observations)
    sources = _source_calibration(observations)
    qualified_sources = {
        source: metrics
        for source, metrics in sources.items()
        if int(metrics.get("samples") or 0) >= PROMOTION_MIN_SAMPLES
    }
    strongest_source, strongest = (
        min(qualified_sources.items(), key=lambda item: float(item[1]["mae"]))
        if qualified_sources
        else (None, None)
    )
    required_candidate_mae = (
        round(float(strongest["mae"]) * (1 - PROMOTION_REQUIRED_MAE_IMPROVEMENT), 4)
        if strongest
        else None
    )
    key_position_counts = {
        position: sum(row.get("position") == position for row in observations)
        for position in ("QB", "RB", "WR", "TE")
    }
    interval_coverage = candidate.get("p20_p80_coverage")
    checks = [
        {
            "id": "completed_weeks",
            "status": "pass"
            if len(completed_weeks) >= PROMOTION_MIN_COMPLETED_WEEKS
            else "blocked",
            "observed": len(completed_weeks),
            "required": PROMOTION_MIN_COMPLETED_WEEKS,
        },
        {
            "id": "sample_size",
            "status": "pass" if len(observations) >= PROMOTION_MIN_SAMPLES else "blocked",
            "observed": len(observations),
            "required": PROMOTION_MIN_SAMPLES,
        },
        {
            "id": "position_coverage",
            "status": "pass"
            if all(
                value >= PROMOTION_MIN_POSITION_SAMPLES for value in key_position_counts.values()
            )
            else "blocked",
            "observed": key_position_counts,
            "required_per_position": PROMOTION_MIN_POSITION_SAMPLES,
        },
        {
            "id": "beats_strongest_source",
            "status": "pass"
            if strongest
            and strongest.get("paired_candidate_mae") is not None
            and float(strongest["paired_candidate_mae"]) <= float(required_candidate_mae)
            else "blocked",
            "strongest_source": strongest_source,
            "source_mae": strongest.get("mae") if strongest else None,
            "candidate_mae": strongest.get("paired_candidate_mae") if strongest else None,
            "required_candidate_mae": required_candidate_mae,
        },
        {
            "id": "interval_calibration",
            "status": "pass"
            if interval_coverage is not None
            and PROMOTION_MIN_INTERVAL_COVERAGE
            <= float(interval_coverage)
            <= PROMOTION_MAX_INTERVAL_COVERAGE
            else "blocked",
            "observed": interval_coverage,
            "required_range": [
                PROMOTION_MIN_INTERVAL_COVERAGE,
                PROMOTION_MAX_INTERVAL_COVERAGE,
            ],
        },
    ]
    blockers = [
        {
            "completed_weeks": "Two completed shadow weeks are required",
            "sample_size": "At least 40 pregame player-week forecasts are required",
            "position_coverage": "QB, RB, WR, and TE each require at least five observations",
            "beats_strongest_source": "The ensemble must beat its strongest paired source by 1% MAE",
            "interval_calibration": "P20-P80 empirical coverage must be between 50% and 75%",
        }[check["id"]]
        for check in checks
        if check["status"] != "pass"
    ]
    qualification_status = "qualified" if not blockers else "blocked"
    return {
        "qualification_status": qualification_status,
        "authority": "eligible" if qualification_status == "qualified" else "shadow",
        "completed_weeks": completed_weeks,
        "candidate": candidate,
        "by_position": {
            position: _candidate_metrics(
                [row for row in observations if row.get("position") == position]
            )
            for position in sorted(ELIGIBLE_POSITIONS)
        },
        "sources": sources,
        "strongest_paired_source": strongest_source,
        "recommended_weights_by_position": _recommended_weights(observations),
        "checks": checks,
        "blockers": blockers,
        "snapshot_accounting": {
            "persisted": len(projection_snapshots),
            "eligible_pregame_player_weeks": len(observations),
            "excluded": exclusions,
        },
        "input_hash": content_hash(
            {
                "observations": observations,
                "thresholds": {
                    "weeks": PROMOTION_MIN_COMPLETED_WEEKS,
                    "samples": PROMOTION_MIN_SAMPLES,
                    "position_samples": PROMOTION_MIN_POSITION_SAMPLES,
                    "mae_improvement": PROMOTION_REQUIRED_MAE_IMPROVEMENT,
                    "interval": [
                        PROMOTION_MIN_INTERVAL_COVERAGE,
                        PROMOTION_MAX_INTERVAL_COVERAGE,
                    ],
                },
            }
        ),
    }


def _ordered_player_rows(report: dict[str, Any], scoring: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in report.get("historical_player_stats") or []:
        position = str(row.get("position") or "").upper()
        if position not in ELIGIBLE_POSITIONS:
            continue
        if str(row.get("season_type") or "REG").upper() != "REG":
            continue
        try:
            season = int(row.get("season"))
            week = int(row.get("week"))
        except (TypeError, ValueError):
            continue
        player_id = str(row.get("player_id") or "")
        if not player_id:
            continue
        result.append(
            {
                "player_id": player_id,
                "position": position,
                "season": season,
                "week": week,
                "actual": score_nflverse_player_week(row, scoring),
            }
        )
    return sorted(result, key=lambda item: (item["season"], item["week"], item["player_id"]))


def _walk_forward_observations(
    rows: list[dict[str, Any]], *, minimum_history: int = 2
) -> tuple[list[dict[str, Any]], int]:
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observations: list[dict[str, Any]] = []
    leakage_violations = 0
    for row in rows:
        history = histories[row["player_id"]]
        same_season = [item for item in history if item["season"] == row["season"]]
        if len(same_season) >= minimum_history:
            prior_three = same_season[-3:]
            prior_six = same_season[-6:]
            trailing_three = fmean(float(item["actual"]) for item in prior_three)
            trailing_six = fmean(float(item["actual"]) for item in prior_six)
            season_mean = fmean(float(item["actual"]) for item in same_season)
            candidate = 0.65 * trailing_three + 0.35 * trailing_six
            residual_spread = (
                pstdev(float(item["actual"]) for item in prior_six) if len(prior_six) >= 2 else 0.0
            )
            spread = max(POSITION_SPREAD[row["position"]], residual_spread)
            latest_training_key = max((item["season"], item["week"]) for item in same_season)
            target_key = (row["season"], row["week"])
            if latest_training_key >= target_key:
                leakage_violations += 1
            observations.append(
                {
                    **row,
                    "training_samples": len(same_season),
                    "latest_training_season": latest_training_key[0],
                    "latest_training_week": latest_training_key[1],
                    "trailing_3": round(trailing_three, 4),
                    "trailing_6": round(trailing_six, 4),
                    "season_mean": round(season_mean, 4),
                    "candidate": round(candidate, 4),
                    "floor_p20": round(max(candidate - 0.8416 * spread, 0), 4),
                    "ceiling_p80": round(candidate + 0.8416 * spread, 4),
                }
            )
        history.append(row)
    return observations, leakage_violations


def build_projection_backtest(
    report: dict[str, Any],
    scoring_settings: dict[str, Any],
    projection_snapshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run a strictly causal diagnostic over completed nflverse player weeks."""

    rows = _ordered_player_rows(report, scoring_settings)
    observations, leakage_violations = _walk_forward_observations(rows)
    seasons = sorted({int(row["season"]) for row in observations})
    weeks = sorted({f"{row['season']}-W{row['week']:02d}" for row in observations})
    by_position = {
        position: _candidate_metrics([row for row in observations if row["position"] == position])
        for position in sorted(ELIGIBLE_POSITIONS)
    }
    candidate = _candidate_metrics(observations)
    baselines = {
        "trailing_3": _metric_rows(observations, "trailing_3"),
        "trailing_6": _metric_rows(observations, "trailing_6"),
        "season_mean": _metric_rows(observations, "season_mean"),
    }
    comparable = [
        (name, float(metrics["mae"]))
        for name, metrics in baselines.items()
        if metrics["mae"] is not None
    ]
    strongest_name, strongest_mae = (
        min(comparable, key=lambda item: item[1])
        if comparable
        else (
            None,
            None,
        )
    )
    candidate_mae = candidate["mae"]
    delta = (
        round(float(strongest_mae) - float(candidate_mae), 4)
        if strongest_mae is not None and candidate_mae is not None
        else None
    )
    configured_nonzero = {
        str(key) for key, value in scoring_settings.items() if float(value or 0) != 0
    }
    supported_player_keys = configured_nonzero.intersection(NFLVERSE_PLAYER_STAT_MAP)
    ensemble = build_ensemble_calibration(report, scoring_settings, projection_snapshots or [])
    blockers = list(ensemble["blockers"])
    blockers.append("Historical injury-status calibration remains a separate future enhancement")
    if leakage_violations:
        blockers.insert(0, f"Leakage guard found {leakage_violations} invalid observations")
    if not observations:
        blockers.insert(0, "No eligible completed player weeks have enough prior observations")
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "diagnostic_complete" if observations and not leakage_violations else "blocked",
        "qualification_status": ensemble["qualification_status"],
        "backtest_version": BACKTEST_VERSION,
        "model": {
            "id": "causal_recency_blend",
            "formula": "0.65 * trailing_3 + 0.35 * trailing_6",
            "minimum_prior_same_season_games": 2,
            "training_policy": "only player games strictly earlier than the target week",
        },
        "coverage": {
            "seasons": seasons,
            "week_count": len(weeks),
            "first_week": weeks[0] if weeks else None,
            "last_week": weeks[-1] if weeks else None,
            "eligible_rows": len(rows),
            "evaluated_player_weeks": len(observations),
            "positions": sorted({row["position"] for row in observations}),
            "configured_nonzero_scoring_keys": len(configured_nonzero),
            "supported_player_scoring_keys": sorted(supported_player_keys),
            "note": "D/ST is scored for live usage separately; historical D/ST evaluation remains estimated",
        },
        "leakage_guard": {
            "status": "pass" if leakage_violations == 0 else "fail",
            "violations": leakage_violations,
            "rule": "max(training season/week) < target season/week",
        },
        "candidate": candidate,
        "baselines": baselines,
        "strongest_internal_baseline": {
            "name": strongest_name,
            "mae": strongest_mae,
            "candidate_mae_delta": delta,
            "candidate_materially_better": bool(
                strongest_mae is not None
                and candidate_mae is not None
                and float(candidate_mae) <= float(strongest_mae) * 0.99
            ),
        },
        "by_position": by_position,
        "calibration": {
            "nominal_p20_p80_coverage": 0.60,
            "observed_coverage": candidate.get("p20_p80_coverage"),
            "injury_status": "unavailable",
            "horizon": "one_week_ahead",
        },
        "ensemble_calibration": ensemble,
        "promotion_gate": {
            "status": ensemble["qualification_status"],
            "checks": ensemble["checks"],
            "blockers": ensemble["blockers"],
            "authority": ensemble["authority"],
        },
        "qualification_blockers": blockers,
        "input_hash": content_hash(
            {
                "rows": rows,
                "scoring_settings": scoring_settings,
                "backtest_version": BACKTEST_VERSION,
            }
        ),
    }
    return result


def write_projection_backtest(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    nflverse = read_report(cfg.reports_dir / "nflverse-context.json", {})
    nflverse = merge_nflverse_history(nflverse, read_nflverse_history(cfg.reports_dir))
    in_season = read_report(cfg.reports_dir / "in-season-context.json", {})
    scoring = (in_season.get("league") or {}).get("scoring_settings") or {}
    snapshots = _projection_snapshot_rows(cfg)
    result = build_projection_backtest(nflverse, scoring, snapshots)
    destination = Path(cfg.reports_dir) / "projection-backtest.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(canonical_json(result) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return result
