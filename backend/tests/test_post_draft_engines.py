from app.operations import (
    RosterPlayer,
    WaiverCandidate,
    autosub_plan,
    bye_week_gaps,
    ir_move,
    streamer_choice,
    waiver_plan,
)
from app.policy import DropEvidence
from app.projections import PositionProjectionModel, TrainingRow, blend_open_projection


def test_position_model_is_versioned_and_missing_inputs_lower_confidence():
    rows = [
        TrainingRow("WR", {"historical_fppg": 10, "opportunity_share": 0.2}, 11),
        TrainingRow("WR", {"historical_fppg": 15, "opportunity_share": 0.3}, 16),
        TrainingRow("WR", {"historical_fppg": 20, "opportunity_share": 0.4}, 21),
    ]
    model = PositionProjectionModel("WR").fit(rows, iterations=100)
    projection = model.predict(
        {"historical_fppg": 16, "opportunity_share": 0.3},
        games_remaining=10,
        source_fresh=True,
    )
    stale = model.predict(
        {"historical_fppg": 16, "opportunity_share": 0.3},
        games_remaining=10,
        source_fresh=False,
    )
    assert projection.model_version == "projection-2026.1"
    assert stale.confidence < projection.confidence
    assert (
        blend_open_projection(projection, None, open_source_fresh=False).confidence
        < projection.confidence
    )


def test_ir_bye_streamer_and_autosub_rules():
    assert ir_move(RosterPlayer("p", "WR", 0, 0, status="OUT"), reserve_slots=2, occupied_slots=1)[
        "recommended"
    ]
    roster = [RosterPlayer("qb", "QB", 10, 100, bye_week=7)]
    assert bye_week_gaps(roster, {"QB": 1}) == [{"week": 7, "position": "QB", "missing": 1}]
    best = streamer_choice(
        [
            WaiverCandidate("k1", "K", 8, 80, "free_agent"),
            WaiverCandidate("k2", "K", 7, 90, "free_agent"),
        ],
        "K",
    )
    assert best.player_id == "k1"
    disabled = autosub_plan(
        max_subs=0,
        starter=RosterPlayer("a", "WR", 1, 1),
        substitute=RosterPlayer("b", "RB", 1, 1),
        starter_has_started=False,
        substitute_has_started=False,
    )
    assert disabled == {"eligible": False, "reason": "league_autosubs_disabled"}


def test_waiver_engine_cannot_bypass_top_30_drop_approval():
    plan = waiver_plan(
        candidate=WaiverCandidate("add", "WR", 12, 120, "waiver"),
        drop_player=RosterPlayer("drop", "WR", 8, 80),
        drop_evidence=DropEvidence(
            player_id="drop",
            positions=("WR",),
            ros_ranks={"WR": 25},
            ranking_fresh=True,
            protection_tier="CHURN_ELIGIBLE",
        ),
    )
    assert plan["recommended"]
    assert plan["approval_required"]
