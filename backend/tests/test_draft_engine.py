from dataclasses import replace
from datetime import date

from app.draft_engine import (
    DraftCandidate,
    estimate_candidate_championship_equity,
    map_rankings_to_sleeper,
    recommend_draft_pick,
    replacement_ranks_from_settings,
    roster_plan_from_settings,
)
from app.draft_room import _next_owner_pick
from app.rankings import RankingRow
from app.utils import normalize_player_name

MOCK_SETTINGS = {
    "rounds": 15,
    "teams": 10,
    "slots_qb": 1,
    "slots_rb": 2,
    "slots_wr": 2,
    "slots_te": 1,
    "slots_flex": 2,
    "slots_k": 1,
    "slots_def": 1,
}

LEAGUE_SETTINGS = {
    "rounds": 16,
    "teams": 12,
    "slots_qb": 1,
    "slots_rb": 2,
    "slots_wr": 3,
    "slots_te": 1,
    "slots_flex": 2,
    "slots_k": 1,
    "slots_def": 1,
}


def ranking(name: str, position: str, overall: int, positional: int) -> RankingRow:
    return RankingRow(
        source="test",
        source_url="https://example.test/rankings",
        player_name=name,
        normalized_name=normalize_player_name(name),
        team="TST",
        position=position,
        overall_rank=overall,
        position_rank=positional,
        rank_sd=2,
        as_of=date.today().isoformat(),
        confidence=0.9,
    )


def draft_candidate(player_id: str, row: RankingRow) -> DraftCandidate:
    return DraftCandidate(
        player_id=player_id,
        player_name=row.player_name,
        position=row.position,
        team=row.team,
        bye_week=None,
        overall_rank=row.overall_rank or 999,
        position_rank=row.position_rank,
        market_adp=None,
        draft_value_rank=float(row.overall_rank or 999),
        score=0,
        vor=0,
        tier_cliff=0,
        roster_fit=0,
        scarcity=0,
        available_at_pick_probability=1,
        survival_probability=0,
        confidence=0.9,
        injury_status=None,
        injury_detail=None,
        depth_chart_order=1,
        replacement_player_name=None,
        replacement_draft_value_rank=None,
        replacement_availability_probability=None,
        expected_position_demand_before_next_pick=0,
        cost_of_waiting=0,
        championship_ceiling=0,
        championship_win_probability=None,
        championship_equity_delta=None,
        championship_simulations=None,
        reasons=[],
        score_components={},
    )


def test_mock_roster_plan_uses_every_round():
    plan = roster_plan_from_settings(MOCK_SETTINGS)
    assert sum(plan.targets.values()) == 15
    assert plan.starters == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
    assert plan.targets["QB"] == 1
    assert plan.targets["TE"] == 1
    assert plan.targets["RB"] + plan.targets["WR"] == 11


def test_live_league_plan_concentrates_bench_depth_at_rb_and_wr():
    plan = roster_plan_from_settings(LEAGUE_SETTINGS)
    assert plan.targets == {"QB": 1, "RB": 5, "WR": 7, "TE": 1, "K": 1, "DEF": 1}


def test_replacement_levels_derive_from_actual_starters_and_flex_demand():
    replacements = replacement_ranks_from_settings(LEAGUE_SETTINGS, "half_ppr")

    assert replacements == {
        "QB": 13,
        "RB": 35,
        "WR": 50,
        "TE": 14,
        "K": 13,
        "DEF": 13,
        "DST": 13,
    }


def test_cost_of_waiting_uses_the_likely_next_turn_replacement():
    rows = [
        ("rb1", ranking("Current Runner", "RB", 10, 6)),
        ("rb2", ranking("Later Runner", "RB", 30, 18)),
        ("wr1", ranking("Current Receiver", "WR", 11, 6)),
        ("wr2", ranking("Later Receiver", "WR", 14, 9)),
    ]
    players = {
        player_id: {"full_name": row.player_name, "position": row.position, "depth_chart_order": 1}
        for player_id, row in rows
    }
    result = recommend_draft_pick(
        rankings=rows,
        drafted_player_ids=set(),
        roster_player_ids=set(),
        players=players,
        owner_pick_no=18,
        following_owner_pick=31,
        draft_settings=LEAGUE_SETTINGS,
        scoring_type="half_ppr",
        owner_slot=7,
    )
    by_id = {item["player_id"]: item for item in result["decision_pool"]}

    assert by_id["rb1"]["replacement_player_name"] == "Later Runner"
    assert by_id["rb1"]["cost_of_waiting"] > by_id["wr1"]["cost_of_waiting"]
    assert by_id["rb1"]["score_components"]["cost_of_waiting"] == by_id["rb1"]["cost_of_waiting"]


def test_candidate_equity_directly_simulates_first_place_against_peer_rosters():
    positions = ["QB", "RB", "WR", "TE", "RB", "WR", "WR", "RB", "WR", "TE"]
    position_ranks: dict[str, int] = {}
    rows: list[tuple[str, RankingRow]] = []
    for overall in range(1, 241):
        position = positions[(overall - 1) % len(positions)]
        position_ranks[position] = position_ranks.get(position, 0) + 1
        rows.append(
            (
                f"p{overall}",
                ranking(
                    f"Player {overall}",
                    position,
                    overall,
                    position_ranks[position],
                ),
            )
        )
    row_by_id = dict(rows)
    players = {
        player_id: {
            "full_name": row.player_name,
            "position": row.position,
            "depth_chart_order": 1,
        }
        for player_id, row in rows
    }
    equities = estimate_candidate_championship_equity(
        rankings=rows,
        players=players,
        candidates=[
            draft_candidate("p1", row_by_id["p1"]),
            draft_candidate("p231", row_by_id["p231"]),
        ],
        drafted_player_ids=set(),
        team_roster_player_ids={slot: set() for slot in range(1, 13)},
        owner_slot=1,
        owner_pick_no=1,
        draft_settings=LEAGUE_SETTINGS,
        playoff_settings={"playoff_teams": 6},
        simulations=300,
    )

    assert equities["p1"] > equities["p231"]
    assert all(0 <= probability <= 1 for probability in equities.values())


def test_elite_qb_trigger_is_explicit_when_top_five_qb_falls():
    rows = [
        ("qb", ranking("Falling Quarterback", "QB", 42, 3)),
        ("wr", ranking("Nearby Receiver", "WR", 40, 18)),
    ]
    players = {
        player_id: {"full_name": row.player_name, "position": row.position}
        for player_id, row in rows
    }
    result = recommend_draft_pick(
        rankings=rows,
        drafted_player_ids=set(),
        roster_player_ids=set(),
        players=players,
        owner_pick_no=60,
        following_owner_pick=73,
        draft_settings=LEAGUE_SETTINGS,
    )
    quarterback = next(item for item in result["decision_pool"] if item["player_id"] == "qb")

    assert quarterback["score_components"]["elite_qb_ceiling"] >= 0.35
    assert any("Elite-QB ceiling trigger" in reason for reason in quarterback["reasons"])


def test_final_discretionary_pick_keeps_contingent_upside_in_the_pool():
    roster = {
        "qb1": {"position": "QB"},
        "te1": {"position": "TE"},
        **{f"rb{i}": {"position": "RB"} for i in range(5)},
        **{f"wr{i}": {"position": "WR"} for i in range(6)},
    }
    rows = [
        ("upside", ranking("Contingent Runner", "RB", 150, 48)),
        ("floor", ranking("Reserve Receiver", "WR", 149, 62)),
        ("k", ranking("Kicker", "K", 170, 1)),
        ("dst", ranking("Defense", "DST", 171, 1)),
    ]
    players = {
        **roster,
        "upside": {"position": "RB", "depth_chart_order": 2},
        "floor": {"position": "WR", "depth_chart_order": 1},
        "k": {"position": "K"},
        "dst": {"position": "DEF"},
    }
    result = recommend_draft_pick(
        rankings=rows,
        drafted_player_ids=set(),
        roster_player_ids=set(roster),
        players=players,
        owner_pick_no=162,
        following_owner_pick=175,
        draft_settings=LEAGUE_SETTINGS,
    )
    by_id = {item["player_id"]: item for item in result["decision_pool"]}

    assert "upside" in by_id
    assert by_id["upside"]["championship_ceiling"] > by_id["floor"]["championship_ceiling"]


def test_roster_construction_downgrades_second_qb_early():
    rows = [
        ("qb2", ranking("Quarterback Two", "QB", 40, 2)),
        ("rb", ranking("Running Back", "RB", 42, 15)),
        ("wr", ranking("Wide Receiver", "WR", 43, 18)),
    ]
    players = {
        "qb1": {"full_name": "Quarterback One", "position": "QB"},
        "wr1": {"full_name": "Roster WR", "position": "WR"},
        "qb2": {"full_name": "Quarterback Two", "position": "QB"},
        "rb": {"full_name": "Running Back", "position": "RB"},
        "wr": {"full_name": "Wide Receiver", "position": "WR"},
    }
    result = recommend_draft_pick(
        rankings=rows,
        drafted_player_ids=set(),
        roster_player_ids={"qb1", "wr1"},
        players=players,
        owner_pick_no=45,
        following_owner_pick=56,
        draft_settings=MOCK_SETTINGS,
    )
    assert result["recommendation"]["position"] in {"RB", "WR"}
    assert result["roster_summary"]["counts"] == {"QB": 1, "WR": 1}
    assert {item["player_id"] for item in result["decision_pool"]} == {"qb2", "rb", "wr"}


def test_kicker_and_defense_wait_until_required():
    rows = [
        ("rb", ranking("Depth Runner", "RB", 65, 20)),
        ("k", ranking("Early Kicker", "K", 60, 1)),
        ("dst", ranking("Early Defense", "DST", 61, 1)),
    ]
    players = {
        "starter": {"full_name": "Starter", "position": "QB"},
        "rb": {"full_name": "Depth Runner", "position": "RB"},
        "k": {"full_name": "Early Kicker", "position": "K"},
        "dst": {"full_name": "Early Defense", "position": "DEF"},
    }
    early = recommend_draft_pick(
        rankings=rows,
        drafted_player_ids=set(),
        roster_player_ids={"starter"},
        players=players,
        owner_pick_no=60,
        following_owner_pick=63,
        draft_settings=MOCK_SETTINGS,
    )
    assert early["recommendation"]["position"] == "RB"

    late_roster = {
        **{f"qb{i}": {"position": "QB"} for i in range(2)},
        **{f"rb{i}": {"position": "RB"} for i in range(5)},
        **{f"wr{i}": {"position": "WR"} for i in range(4)},
        **{f"te{i}": {"position": "TE"} for i in range(2)},
        **players,
    }
    late = recommend_draft_pick(
        rankings=rows,
        drafted_player_ids=set(),
        roster_player_ids={key for key in late_roster if key not in players},
        players=late_roster,
        owner_pick_no=149,
        following_owner_pick=152,
        draft_settings=MOCK_SETTINGS,
    )
    assert late["recommendation"]["position"] in {"K", "DEF"}


def test_ten_team_slot_nine_snake_turns_are_exact():
    assert _next_owner_pick(81, 9, 10) == 89
    assert _next_owner_pick(90, 9, 10) == 92


def test_sleeper_defense_ids_resolve_through_the_verified_player_catalog():
    defense = replace(ranking("Jacksonville Jaguars", "DST", 120, 1), team="JAC")
    players = {
        "JAX": {
            "player_id": "JAX",
            "first_name": "Jacksonville",
            "last_name": "Jaguars",
            "position": "DEF",
            "team": "JAX",
        }
    }

    matched, rejected = map_rankings_to_sleeper([defense], players)

    assert matched == [("JAX", defense)]
    assert rejected == []


def test_a_drafted_jax_defense_cannot_reappear_as_the_jac_rankings_alias():
    jags = replace(ranking("Jacksonville Jaguars", "DST", 120, 9), team="JAC")
    packers = replace(ranking("Green Bay Packers", "DST", 130, 10), team="GB")
    players = {
        "JAX": {
            "first_name": "Jacksonville",
            "last_name": "Jaguars",
            "position": "DEF",
            "team": "JAX",
        },
        "GB": {
            "first_name": "Green Bay",
            "last_name": "Packers",
            "position": "DEF",
            "team": "GB",
        },
        "qb": {"position": "QB"},
        "te": {"position": "TE"},
        "k": {"position": "K"},
        **{f"rb{i}": {"position": "RB"} for i in range(5)},
        **{f"wr{i}": {"position": "WR"} for i in range(7)},
    }
    mapped, rejected = map_rankings_to_sleeper([jags, packers], players)

    result = recommend_draft_pick(
        rankings=mapped,
        drafted_player_ids={"JAX"},
        roster_player_ids={
            "qb",
            "te",
            "k",
            *{f"rb{i}" for i in range(5)},
            *{f"wr{i}" for i in range(7)},
        },
        players=players,
        owner_pick_no=186,
        following_owner_pick=199,
        draft_settings=LEAGUE_SETTINGS,
    )

    assert rejected == []
    assert result["recommendation"]["player_id"] == "GB"
    assert all(item["player_id"] != "JAX" for item in result["decision_pool"])

    stale_alias_result = recommend_draft_pick(
        rankings=[("JAC", jags), ("GB", packers)],
        drafted_player_ids={"JAX"},
        roster_player_ids={
            "qb",
            "te",
            "k",
            *{f"rb{i}" for i in range(5)},
            *{f"wr{i}" for i in range(7)},
        },
        players=players,
        owner_pick_no=186,
        following_owner_pick=199,
        draft_settings=LEAGUE_SETTINGS,
    )
    assert stale_alias_result["recommendation"]["player_id"] == "GB"
