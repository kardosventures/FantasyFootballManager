from app.championship_backtest import (
    _probability_metrics,
    build_championship_backtest,
)


def test_probability_metrics_report_brier_log_loss_and_reliability() -> None:
    metrics = _probability_metrics(
        [
            {"predicted": 0.9, "actual": 1},
            {"predicted": 0.8, "actual": 1},
            {"predicted": 0.2, "actual": 0},
            {"predicted": 0.1, "actual": 0},
        ]
    )

    assert metrics["samples"] == 4
    assert metrics["brier_score"] == 0.025
    assert metrics["log_loss"] is not None
    assert sum(bucket["samples"] for bucket in metrics["reliability"]) == 4


def test_backtest_stays_blocked_without_empirical_evidence() -> None:
    report = build_championship_backtest(
        {},
        {"week": 1, "league": {"settings": {"num_teams": 12}}},
        {},
        {},
        [],
    )

    assert report["status"] == "complete"
    assert report["qualification_status"] == "blocked"
    assert report["qualification_blockers"]
    assert all(
        feature["qualification_status"] == "blocked"
        for feature in report["feature_qualification"].values()
    )
