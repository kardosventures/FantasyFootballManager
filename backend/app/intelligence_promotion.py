from __future__ import annotations

from typing import Any

from app.config import Settings


def _check(check_id: str, passed: bool, reason: str, observed: Any) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "pass" if passed else "blocked",
        "reason": reason,
        "observed": observed,
    }


def _feature_is_qualified(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, dict):
        return False
    return bool(value.get("implemented")) and value.get("qualification_status") == "qualified"


def build_intelligence_promotion(
    settings: Settings,
    context: dict[str, Any],
    backtest: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed until empirical and structural gates allow model authority."""

    ensemble = backtest.get("ensemble_calibration") or {}
    projection_qualified = ensemble.get("qualification_status") == "qualified"
    projection_blockers = list(ensemble.get("blockers") or [])
    if not settings.in_season_plan_v2_enabled:
        projection_blockers.append("IN_SEASON_PLAN_V2_ENABLED is false")
    projection_authoritative = bool(settings.in_season_plan_v2_enabled and projection_qualified)

    championship = context.get("championship_outlook") or {}
    our_team = championship.get("our_team") or {}
    simulation_count = int(championship.get("simulation_count") or 0)
    projection_coverage = float(championship.get("projection_coverage") or 0)
    structural_coverage = float(championship.get("structural_coverage") or 0)
    modeled_replacement_share = float(championship.get("modeled_replacement_share") or 0)
    standard_error = float(our_team.get("championship_standard_error") or 1)
    decision_features = championship.get("decision_features") or {}
    championship_checks = [
        _check(
            "projection_model_qualified",
            projection_qualified,
            "The projection ensemble must beat its strongest paired source first",
            ensemble.get("qualification_status") or "not_run",
        ),
        _check(
            "simulation_completed",
            championship.get("status") == "complete",
            "The championship simulation must complete without structural blockers",
            championship.get("status") or "unavailable",
        ),
        _check(
            "league_projection_coverage",
            projection_coverage >= 0.90 and structural_coverage >= 0.95,
            "At least 90% direct projection coverage and 95% complete legal team-week coverage are required",
            {
                "direct_projection_coverage": round(projection_coverage, 4),
                "structural_coverage": round(structural_coverage, 4),
            },
        ),
        _check(
            "replacement_reliance",
            modeled_replacement_share <= 0.10,
            "Modeled replacement starters may cover at most 10% of simulated slots",
            round(modeled_replacement_share, 4),
        ),
        _check(
            "simulation_precision",
            simulation_count >= 5_000 and standard_error <= 0.01,
            "At least 5,000 simulations and championship standard error at or below 1% are required",
            {"simulations": simulation_count, "standard_error": standard_error},
        ),
        _check(
            "decision_features",
            all(
                _feature_is_qualified(decision_features.get(feature))
                for feature in (
                    "live_score_and_remaining_players",
                    "injury_availability_scenarios",
                    "cross_player_correlations",
                    "future_opponent_effects",
                    "multi_week_projection_decay",
                )
            ),
            "Live remaining-player state, injuries, correlations, opponent effects, and projection decay must pass replay qualification",
            decision_features,
        ),
    ]
    championship_eligible = all(check["status"] == "pass" for check in championship_checks)
    championship_authoritative = bool(settings.in_season_plan_v2_enabled and championship_eligible)
    championship_blockers = [
        str(check["reason"]) for check in championship_checks if check["status"] != "pass"
    ]
    if not settings.in_season_plan_v2_enabled:
        championship_blockers.append("IN_SEASON_PLAN_V2_ENABLED is false")

    return {
        "mode": "authoritative" if projection_authoritative else "shadow",
        "master_switch_enabled": settings.in_season_plan_v2_enabled,
        "projection": {
            "qualification_status": ensemble.get("qualification_status") or "not_run",
            "eligible": projection_qualified,
            "authoritative": projection_authoritative,
            "completed_weeks": ensemble.get("completed_weeks") or [],
            "candidate": ensemble.get("candidate") or {},
            "strongest_paired_source": ensemble.get("strongest_paired_source"),
            "checks": ensemble.get("checks") or [],
            "blockers": projection_blockers,
            "calibration_hash": ensemble.get("input_hash"),
        },
        "championship": {
            "qualification_status": "qualified" if championship_eligible else "blocked",
            "eligible": championship_eligible,
            "authoritative": championship_authoritative,
            "checks": championship_checks,
            "blockers": championship_blockers,
        },
    }
