from app.projection_ensemble import (
    build_projection_ensemble,
    extract_fantasypros_points,
    usage_baseline_points,
)


def _usage() -> dict:
    return {
        "status": "available",
        "games_observed": 3,
        "rolling_3": {"fantasy_points": 10.0, "receptions": 4.0},
        "volatility": 5.0,
        "feature_hash": "usage",
    }


def test_extracts_only_projection_dataset_points() -> None:
    assert extract_fantasypros_points({"projections": {"stats": {"fpts": 14.5}}}) == 14.5
    assert extract_fantasypros_points({"weekly_rankings": {"points": 99}}) is None


def test_extracts_half_ppr_points_from_limited_api_shape() -> None:
    datasets = {
        "projections": {
            "fpid": 123,
            "stats": {"points": 10, "points_half": 12.5, "points_ppr": 15},
        }
    }
    assert extract_fantasypros_points(datasets, {"rec": 0.5}) == 12.5
    assert extract_fantasypros_points(datasets, {"rec": 0}) == 10


def test_usage_baseline_applies_league_reception_scoring() -> None:
    assert usage_baseline_points(_usage(), {"rec": 0.5}) == 12.0


def test_usage_baseline_prefers_precomputed_league_points() -> None:
    usage = _usage()
    usage["rolling_3"]["league_fantasy_points"] = 14.25
    assert usage_baseline_points(usage, {"rec": 9}) == 14.25


def test_projection_ensemble_is_transparent_and_ordered() -> None:
    projection = build_projection_ensemble(
        sleeper_points=14,
        fantasypros={"projections": {"fpts": 16}},
        usage=_usage(),
        scoring_settings={"rec": 0.5},
        position="RB",
    )
    assert projection["external_source_count"] == 2
    assert projection["status"] == "active_candidate"
    assert projection["floor_p20"] <= projection["median_p50"] <= projection["ceiling_p80"]
    assert round(sum(row["weight"] for row in projection["sources"]), 3) == 1.0
    assert projection["limitations"]


def test_single_source_is_confidence_capped_and_labeled_shadow() -> None:
    projection = build_projection_ensemble(
        sleeper_points=12,
        fantasypros=None,
        usage=None,
        scoring_settings={"rec": 0.5},
        position="WR",
    )
    assert projection["status"] == "shadow"
    assert projection["confidence"] < 0.75


def test_qualified_position_calibration_replaces_default_weights() -> None:
    projection = build_projection_ensemble(
        sleeper_points=14,
        fantasypros={"projections": {"fpts": 16}},
        usage=_usage(),
        scoring_settings={"rec": 0.5},
        position="RB",
        calibration={
            "qualification_status": "qualified",
            "input_hash": "calibration-hash",
            "recommended_weights_by_position": {
                "RB": {"sleeper_ui": 0.2, "fantasypros": 0.7, "usage_baseline": 0.1}
            },
        },
    )
    weights = {row["source"]: row["weight"] for row in projection["sources"]}
    assert weights == {"sleeper_ui": 0.2, "fantasypros": 0.7, "usage_baseline": 0.1}
    assert projection["weight_profile"] == "qualified_position_calibration"
    assert projection["calibration_hash"] == "calibration-hash"


def test_blocked_calibration_weights_are_evaluated_in_shadow_when_other_checks_pass() -> None:
    projection = build_projection_ensemble(
        sleeper_points=14,
        fantasypros={"projections": {"fpts": 16}},
        usage=_usage(),
        scoring_settings={"rec": 0.5},
        position="RB",
        calibration={
            "qualification_status": "blocked",
            "input_hash": "shadow-calibration",
            "checks": [
                {"id": "completed_weeks", "status": "pass"},
                {"id": "sample_size", "status": "pass"},
                {"id": "position_coverage", "status": "pass"},
                {"id": "beats_strongest_source", "status": "blocked"},
                {"id": "interval_calibration", "status": "pass"},
            ],
            "recommended_weights_by_position": {
                "RB": {"sleeper_ui": 0.2, "fantasypros": 0.7, "usage_baseline": 0.1}
            },
        },
    )
    weights = {row["source"]: row["weight"] for row in projection["sources"]}
    assert weights == {"sleeper_ui": 0.2, "fantasypros": 0.7, "usage_baseline": 0.1}
    assert projection["weight_profile"] == "shadow_position_calibration"
    assert projection["calibration_status"] == "blocked"
