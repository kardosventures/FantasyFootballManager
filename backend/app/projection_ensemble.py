from __future__ import annotations

import math
from typing import Any

from app.utils import content_hash

MODEL_VERSION = "projection-ensemble-2026.2-shadow"


def _number(value: Any) -> float | None:
    if value in {None, "", "NA", "NaN"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 80 else None


def extract_fantasypros_points(
    datasets: dict[str, Any] | None, scoring_settings: dict[str, Any] | None = None
) -> float | None:
    """Conservatively extract a weekly fantasy-point projection from compact FP data."""

    if not datasets:
        return None
    projection = datasets.get("projections")
    if not isinstance(projection, dict):
        return None
    containers = [projection]
    for key in ("stats", "projection", "player"):
        nested = projection.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    reception_points = float((scoring_settings or {}).get("rec") or 0)
    provider_point_keys = (
        ("points_ppr", "points_half", "points")
        if reception_points >= 0.75
        else ("points_half", "points_ppr", "points")
        if reception_points >= 0.25
        else ("points", "points_half", "points_ppr")
    )
    for container in containers:
        for key in (
            "fpts",
            "fantasy_points",
            "fantasyPoints",
            "projected_points",
            *provider_point_keys,
        ):
            value = _number(container.get(key))
            if value is not None:
                return value
    return None


def usage_baseline_points(
    usage: dict[str, Any] | None, scoring_settings: dict[str, Any]
) -> float | None:
    if not usage or usage.get("status") != "available" or usage.get("games_observed", 0) < 2:
        return None
    rolling = usage.get("rolling_3") or {}
    league_points = _number(rolling.get("league_fantasy_points"))
    if league_points is not None:
        return round(league_points, 3)
    standard = _number(rolling.get("fantasy_points"))
    receptions = _number(rolling.get("receptions"))
    if standard is None:
        return None
    reception_points = float(scoring_settings.get("rec") or 0)
    return round(standard + (receptions or 0) * reception_points, 3)


def build_projection_ensemble(
    *,
    sleeper_points: float | None,
    fantasypros: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    scoring_settings: dict[str, Any],
    position: str,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    sleeper = _number(sleeper_points)
    fantasypros_points = extract_fantasypros_points(fantasypros, scoring_settings)
    usage_points = usage_baseline_points(usage, scoring_settings)
    if sleeper is not None:
        sources.append({"source": "sleeper_ui", "points": sleeper, "base_weight": 0.60})
    if fantasypros_points is not None:
        sources.append({"source": "fantasypros", "points": fantasypros_points, "base_weight": 0.30})
    if usage_points is not None:
        sources.append({"source": "usage_baseline", "points": usage_points, "base_weight": 0.10})
    if not sources:
        return {
            "status": "unavailable",
            "model_version": MODEL_VERSION,
            "confidence": 0.0,
            "evidence_gap": "No valid weekly projection signal is available",
        }

    calibration_checks = {
        str(row.get("id")): str(row.get("status"))
        for row in (calibration or {}).get("checks") or []
        if isinstance(row, dict)
    }
    shadow_calibration_ready = (
        (calibration or {}).get("qualification_status") == "blocked"
        and calibration_checks.get("beats_strongest_source") == "blocked"
        and all(
            calibration_checks.get(check_id) == "pass"
            for check_id in (
                "completed_weeks",
                "sample_size",
                "position_coverage",
                "interval_calibration",
            )
        )
    )
    use_calibrated_weights = (
        (calibration or {}).get("qualification_status") == "qualified"
        or shadow_calibration_ready
    )
    calibrated_weights = (
        ((calibration or {}).get("recommended_weights_by_position") or {}).get(position) or {}
        if use_calibrated_weights
        else {}
    )
    weight_profile = (
        "qualified_position_calibration"
        if calibrated_weights and (calibration or {}).get("qualification_status") == "qualified"
        else "shadow_position_calibration"
        if calibrated_weights
        else "default_shadow"
    )
    if calibrated_weights:
        for row in sources:
            row["base_weight"] = float(calibrated_weights.get(row["source"]) or 0)
        if not any(float(row["base_weight"]) > 0 for row in sources):
            calibrated_weights = {}
            weight_profile = "default_shadow"
            default_weights = {"sleeper_ui": 0.60, "fantasypros": 0.30, "usage_baseline": 0.10}
            for row in sources:
                row["base_weight"] = default_weights[str(row["source"])]

    total_weight = sum(float(row["base_weight"]) for row in sources)
    for row in sources:
        row["weight"] = round(float(row.pop("base_weight")) / total_weight, 4)
    mean = sum(float(row["points"]) * float(row["weight"]) for row in sources)
    disagreement = math.sqrt(
        sum(float(row["weight"]) * (float(row["points"]) - mean) ** 2 for row in sources)
    )
    usage_volatility = _number((usage or {}).get("volatility"))
    position_spread = {
        "QB": 5.0,
        "RB": 5.5,
        "WR": 6.0,
        "TE": 4.5,
        "K": 3.5,
        "DEF": 4.5,
    }.get(position, 5.0)
    spread = max(position_spread, usage_volatility or 0, disagreement * 1.5)
    external_count = sum(row["source"] != "usage_baseline" for row in sources)
    completeness = min(len(sources) / 3, 1.0)
    confidence = min(
        (0.58 if external_count == 1 else 0.78 if external_count >= 2 else 0.35)
        + completeness * 0.08
        - min(disagreement / 25, 0.18),
        0.86,
    )
    projection = {
        "status": "shadow" if external_count < 2 else "active_candidate",
        "model_version": MODEL_VERSION,
        "mean": round(mean, 3),
        "floor_p20": round(max(mean - 0.8416 * spread, 0), 3),
        "median_p50": round(mean, 3),
        "ceiling_p80": round(mean + 0.8416 * spread, 3),
        "standard_deviation": round(spread, 3),
        "confidence": round(max(confidence, 0), 3),
        "source_count": len(sources),
        "external_source_count": external_count,
        "disagreement": round(disagreement, 3),
        "sources": sources,
        "weight_profile": weight_profile,
        "calibration_hash": (calibration or {}).get("input_hash"),
        "calibration_status": (calibration or {}).get("qualification_status") or "unavailable",
        "limitations": [
            "Intervals use position/usage residual heuristics until historical calibration passes"
        ],
    }
    projection["input_hash"] = content_hash(
        {
            "sources": sources,
            "usage_hash": (usage or {}).get("feature_hash"),
            "scoring": scoring_settings,
            "position": position,
            "calibration_hash": (calibration or {}).get("input_hash"),
            "weight_profile": weight_profile,
        }
    )
    return projection
