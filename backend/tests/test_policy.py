import pytest

from app.execution import validate_execution_transition
from app.policy import (
    DropEvidence,
    approval_binding,
    approval_is_current,
    evaluate_drop,
    validate_action,
)


def evidence(**overrides):
    values = {
        "player_id": "p1",
        "positions": ("WR",),
        "ros_ranks": {"WR": 61},
        "ranking_fresh": True,
        "protection_tier": "CHURN_ELIGIBLE",
    }
    values.update(overrides)
    return DropEvidence(**values)


def test_only_explicit_churn_player_is_auto_eligible():
    decision = evaluate_drop(evidence())
    assert decision.auto_eligible
    assert not decision.approval_required


def test_explicit_kicker_or_defense_churn_does_not_require_ros_rankings():
    decision = evaluate_drop(evidence(positions=("DEF",), ros_ranks={}, ranking_fresh=False))
    assert decision.auto_eligible
    assert not decision.approval_required


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ros_ranks": {"WR": 30}}, "top_30_ros"),
        ({"ros_ranks": {"WR": [45, 29]}}, "top_30_ros"),
        ({"ros_ranks": {}}, "insufficiently_ranked"),
        ({"ranking_fresh": False}, "stale_ranking"),
        ({"ranking_disputed": True}, "disputed_ranking"),
        ({"manually_locked": True}, "manually_locked"),
        ({"recently_acquired": True}, "recently_acquired"),
        ({"protection_tier": "REVIEW"}, "protected_or_review_tier"),
    ],
)
def test_protected_drop_conditions_require_approval(overrides, reason):
    decision = evaluate_drop(evidence(**overrides))
    assert decision.approval_required
    assert reason in decision.reasons


def test_approval_binding_changes_with_exact_pair_or_evidence():
    first = approval_binding(
        action_type="WAIVER_CLAIM",
        parameters={"add_player_id": "a", "drop_player_id": "b"},
        evidence_hashes=["1"],
    )
    second = approval_binding(
        action_type="WAIVER_CLAIM",
        parameters={"add_player_id": "a", "drop_player_id": "c"},
        evidence_hashes=["1"],
    )
    assert not approval_is_current(first, second)


@pytest.mark.parametrize(
    "action", ["TRADE", "COMMISSIONER_SETTING", "CHANGE_LEAGUE_SETTING", "RAW_CLICK"]
)
def test_forbidden_actions_never_enter_outbox(action):
    with pytest.raises(ValueError):
        validate_action(action, {})


def test_draft_player_requires_an_exact_pick_target():
    with pytest.raises(ValueError, match="player_position"):
        validate_action(
            "DRAFT_PLAYER",
            {
                "draft_id": "123",
                "player_id": "5859",
                "player_name": "A.J. Brown",
                "expected_pick_no": 27,
                "owner_slot": 7,
            },
        )

    validate_action(
        "DRAFT_PLAYER",
        {
            "draft_id": "123",
            "player_id": "5859",
            "player_name": "A.J. Brown",
            "player_position": "WR",
            "expected_pick_no": 27,
            "owner_slot": 7,
        },
    )


def test_set_lineup_requires_one_exact_before_after_swap():
    parameters = {
        "from_player_id": "starter",
        "from_player_name": "Starter",
        "from_slot": "FLEX",
        "to_player_id": "bench",
        "to_player_name": "Bench",
        "to_slot": "BN",
        "target_slot_index": 1,
        "expected_starters_before": ["qb", "starter", "wr"],
        "expected_starters_after": ["qb", "bench", "wr"],
    }
    validate_action("SET_LINEUP", parameters)

    with pytest.raises(ValueError, match="one exact player swap"):
        validate_action(
            "SET_LINEUP",
            {
                **parameters,
                "expected_starters_after": ["other-qb", "bench", "wr"],
            },
        )


def test_add_free_agent_requires_one_exact_observed_roster_mutation():
    parameters = {
        "add_player_id": "LV",
        "add_player_name": "Las Vegas. Raiders",
        "add_player_search_name": "Las Vegas",
        "add_player_position": "DEF",
        "add_player_team": "LV",
        "drop_player_id": "MIN",
        "drop_player_name": "Minnesota Vikings",
        "drop_player_search_name": "Minnesota Vikings",
        "drop_player_position": "DEF",
        "drop_player_team": "MIN",
        "claim_priority": 1,
        "faab_percent": 0,
        "contingency": "Only while Sleeper shows an immediate add",
        "observed_acquisition_type": "free_agent",
        "expected_roster_before": ["p1", "MIN"],
        "expected_roster_after": ["p1", "LV"],
    }
    validate_action("ADD_FREE_AGENT", parameters)

    with pytest.raises(ValueError, match="observed Sleeper action type"):
        validate_action(
            "ADD_FREE_AGENT",
            {**parameters, "observed_acquisition_type": "waiver"},
        )
    with pytest.raises(ValueError, match="exactly one roster acquisition"):
        validate_action(
            "ADD_FREE_AGENT",
            {**parameters, "expected_roster_after": ["p1", "other", "LV"]},
        )


def test_waiver_claim_requires_an_exact_transaction_week():
    parameters = {
        "add_player_id": "new",
        "add_player_name": "New Player",
        "add_player_search_name": "New Player",
        "add_player_position": "WR",
        "add_player_team": "MIA",
        "drop_player_id": "old",
        "drop_player_name": "Old Player",
        "drop_player_search_name": "Old Player",
        "drop_player_position": "RB",
        "drop_player_team": "TEN",
        "claim_priority": 1,
        "faab_percent": 0,
        "contingency": "Role remains confirmed",
        "observed_acquisition_type": "waiver",
        "expected_roster_before": ["old"],
        "expected_roster_after": ["new"],
    }
    with pytest.raises(ValueError, match="transaction_week"):
        validate_action("WAIVER_CLAIM", parameters)
    validate_action("WAIVER_CLAIM", {**parameters, "transaction_week": 2})


def test_uncertain_or_verified_commands_are_terminal():
    validate_execution_transition("executing", "unverified")
    validate_execution_transition("unverified", "verified")
    with pytest.raises(ValueError):
        validate_execution_transition("unverified", "executing")
    with pytest.raises(ValueError):
        validate_execution_transition("verified", "executing")


def trade_parameters(**overrides):
    values = {
        "counterparty_roster_id": 10,
        "counterparty_account_label": "other-manager",
        "send_assets": [
            {
                "player_id": "send",
                "player_name": "Send Player",
                "player_display_name": "S. Player",
                "position": "WR",
                "team": "IND",
            }
        ],
        "receive_assets": [
            {
                "player_id": "receive",
                "player_name": "Receive Player",
                "player_display_name": "R. Player",
                "position": "RB",
                "team": "JAX",
            }
        ],
        "expected_our_roster_before": ["keep", "send"],
        "expected_counterparty_roster_before": ["receive", "their-keep"],
        "decision_reason": "Improves the full roster's championship ceiling.",
        "championship_case": "Adds scarce upside without creating a starter hole.",
        "upside_tier": "high",
        "decision_confidence": 0.94,
        "expiration_label": "24 Hours",
    }
    values.update(overrides)
    return values


def test_trade_policy_requires_exact_high_confidence_assets():
    parameters = trade_parameters()
    validate_action("PROPOSE_TRADE", parameters)

    with pytest.raises(ValueError, match="0.92 decision confidence"):
        validate_action(
            "PROPOSE_TRADE", trade_parameters(decision_confidence=0.91)
        )
    with pytest.raises(ValueError, match="exact before rosters"):
        validate_action(
            "PROPOSE_TRADE",
            trade_parameters(
                receive_assets=[
                    {
                        "player_id": "wrong",
                        "player_name": "Wrong Player",
                        "player_display_name": "W. Player",
                        "position": "RB",
                        "team": "JAX",
                    }
                ]
            ),
        )


def test_trade_acceptance_requires_exact_fingerprint_and_roster_transform():
    parameters = trade_parameters(
        offer_fingerprint="other-manager:receive:send",
        expected_our_roster_after=["keep", "receive"],
    )
    parameters.pop("expiration_label")
    validate_action("ACCEPT_TRADE", parameters)

    with pytest.raises(ValueError, match="exact exchange"):
        validate_action(
            "ACCEPT_TRADE", {**parameters, "expected_our_roster_after": ["keep"]}
        )
