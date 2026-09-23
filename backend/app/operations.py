from __future__ import annotations

from dataclasses import asdict, dataclass

from app.policy import DropDecision, DropEvidence, evaluate_drop

ENGINE_VERSION = "operations-2026.1"


@dataclass(frozen=True)
class RosterPlayer:
    player_id: str
    position: str
    weekly_points: float
    ros_points: float
    eligible_positions: tuple[str, ...] = ()
    status: str = "Active"
    bye_week: int | None = None
    locked: bool = False


@dataclass(frozen=True)
class WaiverCandidate:
    player_id: str
    position: str
    weekly_points: float
    ros_points: float
    acquisition_type: str


def ir_move(player: RosterPlayer, *, reserve_slots: int, occupied_slots: int) -> dict:
    eligible = player.status.upper() in {"IR", "PUP", "OUT"}
    can_move = eligible and occupied_slots < reserve_slots
    return {
        "engine_version": ENGINE_VERSION,
        "player_id": player.player_id,
        "eligible": eligible,
        "recommended": can_move,
        "reason": "eligible_open_slot" if can_move else "ineligible_or_no_open_slot",
    }


def bye_week_gaps(roster: list[RosterPlayer], starter_requirements: dict[str, int]) -> list[dict]:
    gaps: list[dict] = []
    weeks = sorted({player.bye_week for player in roster if player.bye_week})
    for week in weeks:
        available: dict[str, int] = {}
        for player in roster:
            if player.bye_week != week:
                for position in player.eligible_positions or (player.position,):
                    available[position] = available.get(position, 0) + 1
        for position, required in starter_requirements.items():
            if available.get(position, 0) < required:
                gaps.append(
                    {
                        "week": week,
                        "position": position,
                        "missing": required - available.get(position, 0),
                    }
                )
    return gaps


def streamer_choice(candidates: list[WaiverCandidate], position: str) -> WaiverCandidate | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.position in {position, "DST" if position == "DEF" else position}
    ]
    return max(
        eligible,
        key=lambda candidate: (candidate.weekly_points, candidate.ros_points, candidate.player_id),
        default=None,
    )


def waiver_plan(
    *,
    candidate: WaiverCandidate,
    drop_player: RosterPlayer,
    drop_evidence: DropEvidence,
    minimum_weekly_gain: float = 1.0,
) -> dict:
    drop: DropDecision = evaluate_drop(drop_evidence)
    gain = candidate.weekly_points - drop_player.weekly_points
    recommended = gain >= minimum_weekly_gain
    return {
        "engine_version": ENGINE_VERSION,
        "recommended": recommended,
        "approval_required": drop.approval_required,
        "drop_protection_tier": drop.protection_tier,
        "drop_reasons": list(drop.reasons),
        "weekly_gain": round(gain, 3),
        "action": {
            "add_player_id": candidate.player_id,
            "drop_player_id": drop_player.player_id,
            "acquisition_type": candidate.acquisition_type,
            "drop_evidence": asdict(drop_evidence),
        },
    }


def autosub_plan(
    *,
    max_subs: int,
    starter: RosterPlayer,
    substitute: RosterPlayer,
    starter_has_started: bool,
    substitute_has_started: bool,
) -> dict:
    if max_subs <= 0:
        return {"eligible": False, "reason": "league_autosubs_disabled"}
    if starter_has_started or substitute_has_started:
        return {"eligible": False, "reason": "relationship_locked_after_start"}
    starter_positions = set(starter.eligible_positions or (starter.position,))
    substitute_positions = set(substitute.eligible_positions or (substitute.position,))
    compatible = bool(starter_positions & substitute_positions) or bool(
        starter_positions & {"RB", "WR", "TE"} and substitute_positions & {"RB", "WR", "TE"}
    )
    return {
        "eligible": compatible,
        "reason": "eligible" if compatible else "position_ineligible",
    }
