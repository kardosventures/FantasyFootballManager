import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.draft_expert import (
    EXPERT_SYSTEM_PROMPT,
    ExpertDraftUnavailable,
    _build_roster_trajectory,
    _practice_setting_conflicts,
    _sign_payload,
    apply_expert_decision,
    build_expert_context,
    choose_expert_draft_pick,
    validate_expert_decision,
)
from app.draft_room import (
    _apply_emergency_fallback,
    _autopick_configuration_blockers,
    _draft_clock_timing,
    _governing_draft_settings,
)


def candidate(player_id: str, name: str, position: str, rank: int) -> dict:
    return {
        "player_id": player_id,
        "player_name": name,
        "position": position,
        "team": "TST",
        "overall_rank": rank,
        "position_rank": rank,
        "draft_value_rank": float(rank),
        "score": 10 - rank,
        "confidence": 0.9,
        "survival_probability": 0.1,
        "reasons": ["programmatic reason"],
    }


def test_context_centers_complete_rosters_and_room_dynamics():
    preview = {
        "decision_pool": [
            candidate("wr2", "Second Receiver", "WR", 2),
            candidate("rb1", "Needed Runner", "RB", 4),
        ],
        "roster_summary": {"counts": {"WR": 1}, "starters": {"WR": 3, "RB": 2}},
    }
    players = {
        "wr1": {"full_name": "First Receiver", "position": "WR", "team": "ONE"},
        "qb1": {"full_name": "Other Quarterback", "position": "QB", "team": "TWO"},
    }
    picks = [
        {"pick_no": 1, "round": 1, "draft_slot": 1, "player_id": "qb1"},
        {
            "pick_no": 2,
            "round": 1,
            "draft_slot": 2,
            "roster_id": 22,
            "picked_by": "owner",
            "player_id": "wr1",
        },
    ]
    context = build_expert_context(
        league={
            "name": "Test League",
            "season": "2026",
            "roster_positions": ["QB", "RB", "RB", "WR", "WR", "FLEX", "BN"],
            "scoring_settings": {"rec": 0.5, "pass_td": 4},
        },
        draft={
            "draft_id": "draft-1",
            "type": "snake",
            "settings": {"teams": 2, "rounds": 7, "pick_timer": 120},
        },
        picks=picks,
        players=players,
        owner_slot=2,
        owner_roster_id=22,
        owner_user_id="owner",
        owner_pick_no=3,
        following_owner_pick=6,
        programmatic_recommendation=preview,
        candidate_pool_size=72,
        provenance={"source": "test"},
        candidate_turn_paths=[
            {
                "selected_player_id": "rb1",
                "next_two_turns": [{"player_name": "Future Receiver", "position": "WR"}],
            }
        ],
    )

    assert context["our_roster"][0]["player_name"] == "First Receiver"
    assert context["all_team_builds"][0]["position_counts"] == {"QB": 1}
    assert context["all_team_builds"][1]["position_counts"] == {"WR": 1}
    assert context["board_dynamics"]["positions_in_last_12_picks"] == {"QB": 1, "WR": 1}
    assert context["league"]["scoring_settings"]["rec"] == 0.5
    assert context["allowed_player_ids"] == ["wr2", "rb1"]
    assert "entire roster" in EXPERT_SYSTEM_PROMPT.lower()
    assert "next-two-turn" in EXPERT_SYSTEM_PROMPT.lower()
    assert "championship_win_probability" in EXPERT_SYSTEM_PROMPT
    assert "two percentage points" in EXPERT_SYSTEM_PROMPT.lower()
    assert "usable weekly roles" in EXPERT_SYSTEM_PROMPT.lower()
    assert "top-five qb" in EXPERT_SYSTEM_PROMPT.lower()
    assert context["roster_trajectory"]["strategy_state"] == "balanced_or_open"
    assert context["roster_trajectory"]["direct_starter_gaps"]["RB"] == 2
    assert context["candidate_specific_turn_paths"][0]["selected_player_id"] == "rb1"


def test_standalone_mock_reconstructs_team_builds_when_pick_roster_ids_are_null():
    context = build_expert_context(
        league={
            "name": "Test League",
            "season": "2026",
            "roster_positions": ["QB", "RB", "WR", "FLEX", "BN"],
            "scoring_settings": {"rec": 0.5},
        },
        draft={
            "draft_id": "mock-1",
            "league_id": None,
            "type": "snake",
            "slot_to_roster_id": {"1": 1, "2": 2},
            "settings": {"teams": 2, "rounds": 5, "pick_timer": 120},
        },
        picks=[
            {
                "pick_no": 1,
                "round": 1,
                "draft_slot": 1,
                "roster_id": None,
                "picked_by": "",
                "player_id": "qb1",
            },
            {
                "pick_no": 2,
                "round": 1,
                "draft_slot": 2,
                "roster_id": None,
                "picked_by": "owner",
                "player_id": "wr1",
            },
        ],
        players={
            "qb1": {"full_name": "Other Quarterback", "position": "QB"},
            "wr1": {"full_name": "Our Receiver", "position": "WR"},
        },
        owner_slot=2,
        owner_roster_id=2,
        owner_user_id="owner",
        owner_pick_no=3,
        following_owner_pick=6,
        programmatic_recommendation={
            "decision_pool": [candidate("rb1", "Available Runner", "RB", 3)],
            "roster_summary": {},
        },
        candidate_pool_size=72,
        provenance={"source": "test"},
    )

    assert context["all_team_builds"][0]["position_counts"] == {"QB": 1}
    assert context["all_team_builds"][1]["position_counts"] == {"WR": 1}
    assert context["all_team_builds"][0]["players"][0]["player_name"] == "Other Quarterback"


def test_trajectory_flags_zero_rb_and_measures_the_recovery_path():
    rb = candidate("rb1", "Needed Runner", "RB", 18)
    rb.update(survival_probability=0.72, depth_chart_order=1, injury_status=None)
    wr = candidate("wr4", "Fourth Receiver", "WR", 15)
    wr.update(survival_probability=0.65, depth_chart_order=1, injury_status=None)
    trajectory = _build_roster_trajectory(
        our_roster=[
            {"position": "WR", "player_name": "Receiver One"},
            {"position": "WR", "player_name": "Receiver Two"},
            {"position": "WR", "player_name": "Receiver Three"},
        ],
        league={
            "roster_positions": [
                "QB",
                "RB",
                "RB",
                "WR",
                "WR",
                "WR",
                "TE",
                "FLEX",
                "FLEX",
                "K",
                "DEF",
                "BN",
            ]
        },
        candidate_pool=[wr, rb],
    )

    assert trajectory["strategy_state"] == "zero_rb"
    assert trajectory["direct_starter_gaps"]["RB"] == 2
    assert trajectory["position_outlook_to_next_turn"]["RB"]["credible_next_turn_examples"] == [
        "Needed Runner"
    ]
    assert "No running back" in trajectory["structural_risks"][0]


def test_standalone_mock_reports_material_real_league_rule_conflicts():
    conflicts = _practice_setting_conflicts(
        {
            "total_rosters": 12,
            "roster_positions": [
                "QB",
                "RB",
                "RB",
                "WR",
                "WR",
                "WR",
                "TE",
                "FLEX",
                "FLEX",
                "K",
                "DEF",
                "BN",
                "BN",
                "BN",
                "BN",
                "BN",
            ],
            "scoring_settings": {"rec": 0.5},
        },
        {
            "league_id": None,
            "metadata": {"scoring_type": "std"},
            "settings": {
                "teams": 10,
                "rounds": 15,
                "slots_qb": 1,
                "slots_rb": 2,
                "slots_wr": 2,
                "slots_te": 1,
                "slots_flex": 2,
                "slots_k": 1,
                "slots_def": 1,
            },
        },
    )

    assert {item["field"] for item in conflicts} == {
        "starting_lineup",
        "teams",
        "rounds",
        "scoring",
    }


def test_configured_league_lineup_governs_a_mismatched_mock_room():
    settings = _governing_draft_settings(
        {
            "total_rosters": 12,
            "roster_positions": [
                "QB",
                "RB",
                "RB",
                "WR",
                "WR",
                "WR",
                "TE",
                "FLEX",
                "FLEX",
                "K",
                "DEF",
                "BN",
                "BN",
                "BN",
                "BN",
                "BN",
            ],
        },
        {
            "teams": 12,
            "rounds": 16,
            "slots_qb": 1,
            "slots_rb": 2,
            "slots_wr": 2,
            "slots_te": 1,
            "slots_flex": 2,
            "slots_k": 1,
            "slots_def": 1,
        },
    )

    assert settings["slots_wr"] == 3
    assert settings["rounds"] == 16


@pytest.mark.asyncio
async def test_expert_can_override_top_wr_for_whole_roster_fit(tmp_path):
    preview = {
        "recommendation": candidate("wr2", "Second Receiver", "WR", 2),
        "fallbacks": [candidate("rb1", "Needed Runner", "RB", 4)],
        "decision_pool": [
            candidate("wr2", "Second Receiver", "WR", 2),
            candidate("rb1", "Needed Runner", "RB", 4),
        ],
        "roster_summary": {"counts": {"WR": 4, "RB": 0}},
        "engine_version": "programmatic",
    }
    context = {
        "allowed_candidates": preview["decision_pool"],
        "allowed_player_ids": ["wr2", "rb1"],
        "identity_check": {"draft_id": "draft-1", "owner_roster_id": "22"},
    }
    response_decision = {
        "selected_player_id": "rb1",
        "confidence": 0.84,
        "roster_strategy": "Add an anchor RB to a WR-heavy start.",
        "strategy_state": "zero_rb_pivot",
        "fit_summary": "The RB adds more marginal roster value than a fifth early WR.",
        "roster_risk": "Waiting would leave two starter gaps behind a falling RB tier.",
        "next_two_turn_plan": "Take the RB now, then compare WR and TE value next turn.",
        "key_tradeoffs": ["Passes on the highest-ranked remaining WR."],
        "alternatives": [{"player_id": "wr2", "reason": "Take him only if preserving pure value."}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.6-terra"
        assert body["reasoning"] == {"effort": "medium"}
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["tools"] == [{"type": "web_search"}]
        assert "complete roster" in body["instructions"]
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "status": "completed",
                "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(response_decision)}],
                    },
                ],
            },
        )

    settings = Settings(
        _env_file=None,
        draft_expert_provider="openai_api",
        openai_api_key="test-key",
        draft_expert_enabled=True,
        reports_dir=tmp_path,
    )
    result = await choose_expert_draft_pick(
        settings=settings,
        context=context,
        transport=httpx.MockTransport(handler),
    )
    recommendation = apply_expert_decision(preview, result)

    assert recommendation["recommendation"]["player_id"] == "rb1"
    assert recommendation["recommendation"]["confidence"] == 0.84
    assert recommendation["recommendation"]["decision_source"] == "expert_model"
    assert recommendation["fallbacks"][0]["player_id"] == "wr2"
    assert recommendation["expert_decision"]["used_web_search"] is True
    assert recommendation["expert_decision"]["provider"] == "openai_api"
    assert recommendation["expert_decision"]["strategy_state"] == "zero_rb_pivot"
    assert "Take the RB now" in recommendation["expert_decision"]["next_two_turn_plan"]


@pytest.mark.asyncio
async def test_codex_subscription_bridge_accepts_only_a_signed_live_decision(tmp_path):
    pool = [
        candidate("wr2", "Second Receiver", "WR", 2),
        candidate("rb1", "Needed Runner", "RB", 4),
    ]
    context = {
        "allowed_candidates": pool,
        "allowed_player_ids": ["wr2", "rb1"],
        "identity_check": {"draft_id": "draft-codex", "owner_roster_id": "22"},
    }
    secret = "codex-bridge-test-secret-is-long-enough"
    settings = Settings(
        _env_file=None,
        draft_expert_provider="codex_cli",
        draft_expert_enabled=True,
        draft_expert_timeout_seconds=10,
        shim_shared_secret=secret,
        codex_decisions_dir=tmp_path / "spool",
        reports_dir=tmp_path / "reports",
    )

    task = asyncio.create_task(choose_expert_draft_pick(settings=settings, context=context))
    requests_dir = settings.codex_decisions_dir / "requests"
    for _ in range(40):
        requests = list(requests_dir.glob("*.json")) if requests_dir.exists() else []
        if requests:
            break
        await asyncio.sleep(0.025)
    assert len(requests) == 1
    request_envelope = json.loads(requests[0].read_text(encoding="utf-8"))
    request_payload = request_envelope["payload"]
    assert request_envelope["signature"] == _sign_payload(secret, request_payload)
    assert request_payload["model"] == "gpt-5.6-terra"
    assert "entire roster" in request_payload["prompt"].lower()

    decision = {
        "selected_player_id": "rb1",
        "confidence": 0.88,
        "roster_strategy": "Anchor a WR-heavy start with scarce running-back volume.",
        "strategy_state": "zero_rb_pivot",
        "fit_summary": "This improves the complete build more than another receiver.",
        "roster_risk": "Another receiver would compound an empty RB room.",
        "next_two_turn_plan": "Add an RB now and reassess FLEX value at the next turn.",
        "key_tradeoffs": ["Passes on the highest-ranked receiver."],
        "alternatives": [{"player_id": "wr2", "reason": "Best pure-value alternative."}],
    }
    response_payload = {
        "request_id": request_payload["request_id"],
        "completed_at": request_payload["created_at"] + 0.2,
        "model": "gpt-5.6-terra",
        "latency_ms": 200,
        "used_web_search": True,
        "decision": decision,
    }
    response_path = (
        settings.codex_decisions_dir / "responses" / f"{request_payload['request_id']}.json"
    )
    response_path.parent.mkdir(parents=True)
    response_path.write_text(
        json.dumps(
            {"payload": response_payload, "signature": _sign_payload(secret, response_payload)}
        ),
        encoding="utf-8",
    )

    result = await task
    assert result.provider == "codex_cli"
    assert result.decision["selected_player_id"] == "rb1"
    assert result.response_id == request_payload["request_id"]


def test_expert_selection_must_stay_inside_verified_allowlist():
    with pytest.raises(ExpertDraftUnavailable, match="outside the live allowlist"):
        validate_expert_decision(
            {
                "selected_player_id": "not-available",
                "confidence": 0.9,
                "roster_strategy": "Test",
                "fit_summary": "Test",
                "key_tradeoffs": [],
                "alternatives": [],
            },
            [candidate("allowed", "Allowed Player", "RB", 1)],
        )


def test_autopick_fails_closed_without_expert_api_key():
    settings = Settings(
        _env_file=None,
        sleeper_league_id="league-1",
        draft_auto_pick_enabled=True,
        draft_expert_enabled=True,
        draft_expert_provider="openai_api",
        execution_mode="browser",
        browser_live_actions="DRAFT_PLAYER",
    )
    blockers = _autopick_configuration_blockers(
        settings,
        {"league_id": "league-1", "type": "snake"},
        owner_slot=7,
        recommendation=None,
        on_clock=True,
        expert_error=None,
    )

    assert "OPENAI_API_KEY is not configured for the expert draft manager" in blockers
    assert "No verified expert-model decision is available for this live board" in blockers


def test_codex_subscription_provider_does_not_require_an_api_key():
    settings = Settings(
        _env_file=None,
        sleeper_league_id="league-1",
        draft_auto_pick_enabled=True,
        draft_expert_enabled=True,
        draft_expert_provider="codex_cli",
        execution_mode="browser",
        browser_live_actions="DRAFT_PLAYER",
    )
    recommendation = {"confidence": 0.9}
    blockers = _autopick_configuration_blockers(
        settings,
        {"league_id": "league-1", "type": "snake"},
        owner_slot=7,
        recommendation=recommendation,
        on_clock=True,
        expert_error=None,
    )

    assert not any("OPENAI_API_KEY" in blocker for blocker in blockers)


def test_emergency_fallback_promotes_the_roster_aware_verified_candidate():
    selected = candidate("rb1", "Needed Runner", "RB", 4)
    baseline = {
        "recommendation": selected,
        "fallbacks": [candidate("wr2", "Second Receiver", "WR", 6)],
        "decision_pool": [selected],
        "engine_version": "draft-test",
        "decision_source": "programmatic_preview",
        "roster_summary": {
            "counts": {"WR": 6, "RB": 4},
            "targets": {"WR": 6, "RB": 6},
        },
    }

    result = _apply_emergency_fallback(
        baseline,
        expert_failure="Expert decision unavailable: Codex timed out",
    )

    assert result["decision_source"] == "emergency_fallback"
    assert result["recommendation"]["player_id"] == "rb1"
    assert result["recommendation"]["decision_source"] == "emergency_fallback"
    assert result["expert_decision"]["provider"] == "programmatic_emergency"
    assert result["expert_decision"]["grounding_hash"]
    assert "Codex timed out" in result["expert_decision"]["fallback_trigger"]
    assert baseline["recommendation"].get("decision_source") is None


def test_emergency_fallback_requires_a_verified_candidate():
    with pytest.raises(ExpertDraftUnavailable, match="emergency candidate"):
        _apply_emergency_fallback(
            {"recommendation": None},
            expert_failure="Expert decision unavailable: Codex timed out",
        )


def test_only_explicitly_configured_standalone_mock_bypasses_league_attachment():
    settings = Settings(
        _env_file=None,
        sleeper_league_id="league-1",
        sleeper_draft_id="mock-123",
        draft_auto_pick_enabled=True,
        draft_expert_enabled=True,
        draft_expert_provider="codex_cli",
        execution_mode="browser",
        browser_live_actions="DRAFT_PLAYER",
    )
    recommendation = {"confidence": 0.9}

    allowed = _autopick_configuration_blockers(
        settings,
        {"draft_id": "mock-123", "league_id": None, "type": "snake"},
        owner_slot=7,
        recommendation=recommendation,
        on_clock=True,
        expert_error=None,
    )
    rejected = _autopick_configuration_blockers(
        settings,
        {"draft_id": "mock-456", "league_id": None, "type": "snake"},
        owner_slot=7,
        recommendation=recommendation,
        on_clock=True,
        expert_error=None,
    )

    assert "Draft is not attached to the configured Sleeper league" not in allowed
    assert "Draft is not attached to the configured Sleeper league" in rejected


def test_draft_clock_releases_the_pick_with_fifteen_seconds_remaining():
    started_at = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)
    timing = _draft_clock_timing(
        {
            "status": "drafting",
            "last_picked": int(started_at.timestamp() * 1000),
            "settings": {"pick_timer": 120},
        },
        submit_seconds_remaining=15,
    )

    assert timing is not None
    assert timing.source == "last_picked"
    assert timing.deadline == started_at + timedelta(seconds=120)
    assert timing.submit_at == started_at + timedelta(seconds=105)
    assert timing.as_report(now=started_at + timedelta(seconds=30))["seconds_remaining"] == 90


def test_mock_draft_clock_can_release_immediately():
    started_at = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)
    timing = _draft_clock_timing(
        {
            "status": "drafting",
            "last_picked": int(started_at.timestamp() * 1000),
            "settings": {"pick_timer": 120},
        },
        submit_seconds_remaining=15,
        submit_delay_seconds=0,
    )

    assert timing is not None
    assert timing.submit_at == started_at
    assert timing.deadline == started_at + timedelta(seconds=120)


def test_draft_clock_is_unavailable_without_an_active_timer():
    assert (
        _draft_clock_timing(
            {"status": "drafting", "last_picked": 1_800_000_000_000, "settings": {}},
            submit_seconds_remaining=15,
        )
        is None
    )
