import pytest

from app.ir_management import build_ir_plan
from app.pending_waivers import compile_pending_waiver_actions
from app.policy import validate_action


def player(player_id: str, name: str, status: str, *, locked: bool = False) -> dict:
    return {
        "player_id": player_id,
        "name": name,
        "sleeper_ui_display_name": name,
        "position": "WR",
        "team": "MIA",
        "injury_status": status,
        "locked": locked,
    }


def test_ir_plan_builds_exact_remove_then_move_sequence() -> None:
    active = player("active", "Active Player", "Active")
    injured = player("injured", "Injured Player", "IR")
    context = {
        "league": {"league_id": "league", "roster_positions": ["QB", "IR"]},
        "our_team": {
            "roster_id": 8,
            "all_players": [active, injured],
            "reserve": [active],
        },
    }

    plan = build_ir_plan(context)

    assert [row["action_type"] for row in plan["actions"]] == [
        "REMOVE_FROM_IR",
        "MOVE_TO_IR",
    ]
    assert plan["actions"][0]["parameters"]["expected_reserve_after"] == []
    assert plan["actions"][1]["parameters"]["expected_reserve_before"] == []
    assert plan["final_reserve_player_ids"] == ["injured"]
    for action in plan["actions"]:
        validate_action(action["action_type"], action["parameters"])


def test_ir_plan_never_moves_a_locked_player() -> None:
    context = {
        "league": {"roster_positions": ["IR"]},
        "our_team": {
            "all_players": [player("injured", "Locked Player", "IR", locked=True)],
            "reserve": [],
        },
    }

    plan = build_ir_plan(context)

    assert plan["actions"] == []
    assert plan["blocked"][0]["reason"] == "Eligible player is locked"


def test_ir_plan_reports_league_without_ir_as_not_applicable() -> None:
    plan = build_ir_plan(
        {
            "league": {"roster_positions": ["QB", "RB", "BN"]},
            "our_team": {
                "all_players": [player("injured", "Injured Player", "OUT")],
                "reserve": [],
            },
        }
    )

    assert plan["status"] == "not_applicable"
    assert plan["reason"] == "This league has no injured-reserve slots"
    assert plan["actions"] == []


def test_ir_plan_uses_sleeper_reserve_slots_when_roster_positions_omit_ir() -> None:
    context = {
        "league": {
            "league_id": "league",
            "roster_positions": ["QB", "RB", "BN"],
            "settings": {"reserve_slots": 1, "reserve_allow_out": 0},
        },
        "our_team": {
            "roster_id": 8,
            "all_players": [player("injured", "IR Player", "IR")],
            "reserve": [],
        },
    }

    plan = build_ir_plan(context)

    assert plan["reserve_capacity"] == 1
    assert plan["status"] == "ready"
    assert [row["action_type"] for row in plan["actions"]] == ["MOVE_TO_IR"]
    assert plan["final_reserve_player_ids"] == ["injured"]


def test_ir_plan_honors_sleeper_optional_out_eligibility() -> None:
    context = {
        "league": {
            "roster_positions": ["QB", "BN"],
            "settings": {"reserve_slots": 1, "reserve_allow_out": 0},
        },
        "our_team": {
            "all_players": [player("out", "Out Player", "OUT")],
            "reserve": [],
        },
    }

    assert build_ir_plan(context)["actions"] == []

    context["league"]["settings"]["reserve_allow_out"] = 1
    assert [row["action_type"] for row in build_ir_plan(context)["actions"]] == [
        "MOVE_TO_IR"
    ]


def claim(add_id: str, drop_id: str, sequence: int) -> dict:
    return {
        "sequence": sequence,
        "add_player_ids": [add_id],
        "add_player_names": [f"Add {add_id}"],
        "drop_player_ids": [drop_id],
        "drop_player_names": [f"Drop {drop_id}"],
    }


def pending_context() -> dict:
    return {
        "season_horizon": {
            "pending_waiver_claims": [claim("a", "x", 1), claim("b", "y", 2)]
        },
        "our_team": {
            "all_players": [
                {"player_id": "x", "name": "Drop x", "position": "RB", "team": "TEN"},
                {"player_id": "y", "name": "Drop y", "position": "WR", "team": "NYJ"},
            ]
        },
        "free_agent_candidates": [
            {"player_id": "a", "name": "Add a", "position": "WR", "team": "MIA"},
            {"player_id": "b", "name": "Add b", "position": "RB", "team": "DEN"},
        ],
    }


def test_pending_waiver_compiler_cancels_bottom_up_and_validates_contract() -> None:
    decision = {
        "pending_waiver_management": {
            "status": "cancel",
            "desired_order": [{"add_player_id": "a", "drop_player_id": "x"}],
            "reason": "The fallback no longer improves the season-long roster.",
        }
    }

    actions = compile_pending_waiver_actions(decision, pending_context())

    assert len(actions) == 1
    assert actions[0]["action_type"] == "CANCEL_WAIVER_CLAIM"
    assert actions[0]["parameters"]["claim"]["add_player_id"] == "b"
    validate_action(actions[0]["action_type"], actions[0]["parameters"])


def test_pending_waiver_compiler_uses_exact_authenticated_ui_names() -> None:
    context = pending_context()
    context["season_horizon"]["pending_waiver_claims"] = [
        {
            **claim("a", "x", 1),
            "add_player_ui_display_name": "C. Douglas",
            "drop_player_ui_display_name": "T. Spears",
        }
    ]
    context["our_team"]["all_players"][0].update(
        {"name": "Tyjae Spears", "sleeper_ui_display_name": "Tyjae Spears"}
    )
    context["free_agent_candidates"][0].update(
        {"name": "Caleb Douglas", "sleeper_ui_display_name": "Caleb Douglas"}
    )
    decision = {
        "pending_waiver_management": {
            "status": "cancel",
            "desired_order": [],
            "reason": "Replace the existing claim with the stronger plan.",
        }
    }

    actions = compile_pending_waiver_actions(decision, context)

    exact_claim = actions[0]["parameters"]["claim"]
    assert exact_claim["add_player_id"] == "a"
    assert exact_claim["add_player_name"] == "C. Douglas"
    assert exact_claim["drop_player_id"] == "x"
    assert exact_claim["drop_player_name"] == "T. Spears"
    validate_action(actions[0]["action_type"], actions[0]["parameters"])


def test_pending_waiver_compiler_emits_one_exact_reorder() -> None:
    decision = {
        "pending_waiver_management": {
            "status": "reorder",
            "desired_order": [
                {"add_player_id": "b", "drop_player_id": "y"},
                {"add_player_id": "a", "drop_player_id": "x"},
            ],
            "reason": "The second claim now has higher championship value.",
        }
    }

    actions = compile_pending_waiver_actions(decision, pending_context())

    assert len(actions) == 1
    assert actions[0]["action_type"] == "REORDER_WAIVER_CLAIMS"
    assert [
        row["add_player_id"]
        for row in actions[0]["parameters"]["expected_claims_after"]
    ] == ["b", "a"]
    validate_action(actions[0]["action_type"], actions[0]["parameters"])


def test_pending_waiver_contract_rejects_a_non_mutating_reorder() -> None:
    action = compile_pending_waiver_actions(
        {
            "pending_waiver_management": {
                "status": "reorder",
                "desired_order": [
                    {"add_player_id": "b", "drop_player_id": "y"},
                    {"add_player_id": "a", "drop_player_id": "x"},
                ],
                "reason": "Material evidence changed.",
            }
        },
        pending_context(),
    )[0]
    parameters = {
        **action["parameters"],
        "expected_claims_after": action["parameters"]["expected_claims_before"],
    }
    with pytest.raises(ValueError, match="reorder"):
        validate_action("REORDER_WAIVER_CLAIMS", parameters)
