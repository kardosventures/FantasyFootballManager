from app.config import Settings
from app.intelligence_promotion import build_intelligence_promotion


def _qualified_backtest() -> dict:
    return {
        "ensemble_calibration": {
            "qualification_status": "qualified",
            "completed_weeks": ["2026-W01", "2026-W02"],
            "candidate": {"samples": 60, "mae": 4.2},
            "strongest_paired_source": "fantasypros",
            "checks": [{"id": "beats_strongest_source", "status": "pass"}],
            "blockers": [],
            "input_hash": "calibration",
        }
    }


def _complete_context() -> dict:
    return {
        "championship_outlook": {
            "status": "complete",
            "projection_coverage": 0.95,
            "structural_coverage": 1.0,
            "modeled_replacement_share": 0.04,
            "simulation_count": 10_000,
            "our_team": {"championship_standard_error": 0.004},
            "decision_features": {
                "live_score_and_remaining_players": True,
                "injury_availability_scenarios": True,
                "cross_player_correlations": True,
                "future_opponent_effects": True,
                "multi_week_projection_decay": True,
            },
        }
    }


def test_promotion_requires_the_manual_master_switch() -> None:
    promotion = build_intelligence_promotion(
        Settings(in_season_plan_v2_enabled=False), _complete_context(), _qualified_backtest()
    )
    assert promotion["projection"]["eligible"] is True
    assert promotion["projection"]["authoritative"] is False
    assert promotion["championship"]["authoritative"] is False
    assert promotion["mode"] == "shadow"


def test_promotion_requires_empirical_projection_and_simulation_gates() -> None:
    settings = Settings(in_season_plan_v2_enabled=True)
    promotion = build_intelligence_promotion(settings, _complete_context(), _qualified_backtest())
    assert promotion["projection"]["authoritative"] is True
    assert promotion["championship"]["authoritative"] is True
    assert promotion["mode"] == "authoritative"

    blocked = build_intelligence_promotion(
        settings,
        {"championship_outlook": {"status": "blocked", "projection_coverage": 0.2}},
        {"ensemble_calibration": {"qualification_status": "blocked", "blockers": ["bad"]}},
    )
    assert blocked["projection"]["authoritative"] is False
    assert blocked["championship"]["authoritative"] is False


def test_implemented_but_unvalidated_championship_features_remain_shadow() -> None:
    context = _complete_context()
    context["championship_outlook"]["decision_features"] = {
        feature: {"implemented": True, "qualification_status": "shadow_unvalidated"}
        for feature in (
            "live_score_and_remaining_players",
            "injury_availability_scenarios",
            "cross_player_correlations",
            "future_opponent_effects",
            "multi_week_projection_decay",
        )
    }
    promotion = build_intelligence_promotion(
        Settings(in_season_plan_v2_enabled=True), context, _qualified_backtest()
    )
    assert promotion["championship"]["authoritative"] is False
    assert promotion["championship"]["checks"][-1]["status"] == "blocked"
