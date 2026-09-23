from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

POLICY_VERSION = "2026.1"
ALLOWED_ACTIONS = {
    "DRAFT_PLAYER",
    "SET_LINEUP",
    "MOVE_TO_IR",
    "REMOVE_FROM_IR",
    "SET_AUTOSUB",
    "WAIVER_CLAIM",
    "ADD_FREE_AGENT",
    "DROP_PLAYER",
    "CANCEL_WAIVER_CLAIM",
    "REORDER_WAIVER_CLAIMS",
    "PROPOSE_TRADE",
    "ACCEPT_TRADE",
    "DECLINE_TRADE",
}
FORBIDDEN_ACTIONS = {"TRADE", "COMMISSIONER_SETTING", "CHANGE_LEAGUE_SETTING"}
OFFENSIVE_POSITIONS = {"QB", "RB", "WR", "TE"}


@dataclass(frozen=True)
class DropEvidence:
    player_id: str
    positions: tuple[str, ...]
    ros_ranks: dict[str, int | list[int] | None]
    ranking_fresh: bool
    ranking_disputed: bool = False
    manually_locked: bool = False
    protection_tier: str = "REVIEW"
    recently_acquired: bool = False


@dataclass(frozen=True)
class DropDecision:
    auto_eligible: bool
    approval_required: bool
    protection_tier: str
    reasons: tuple[str, ...]


def evaluate_drop(evidence: DropEvidence) -> DropDecision:
    """Fail closed: only fresh, undisputed CHURN_ELIGIBLE players can be auto-dropped."""
    reasons: list[str] = []
    eligible_positions = OFFENSIVE_POSITIONS.intersection(evidence.positions)
    ranks_by_position: dict[str, list[int]] = {}
    for position in eligible_positions:
        raw = evidence.ros_ranks.get(position)
        if isinstance(raw, int):
            ranks_by_position[position] = [raw]
        elif isinstance(raw, list):
            ranks_by_position[position] = [rank for rank in raw if isinstance(rank, int)]
    if any(rank <= 30 for ranks in ranks_by_position.values() for rank in ranks):
        reasons.append("top_30_ros")
    if eligible_positions and any(
        not ranks_by_position.get(position) for position in eligible_positions
    ):
        reasons.append("insufficiently_ranked")
    if eligible_positions and not evidence.ranking_fresh:
        reasons.append("stale_ranking")
    if evidence.ranking_disputed:
        reasons.append("disputed_ranking")
    if evidence.manually_locked:
        reasons.append("manually_locked")
    if evidence.protection_tier != "CHURN_ELIGIBLE":
        reasons.append("protected_or_review_tier")
    if evidence.recently_acquired:
        reasons.append("recently_acquired")
    return DropDecision(
        auto_eligible=not reasons,
        approval_required=bool(reasons),
        protection_tier=evidence.protection_tier,
        reasons=tuple(reasons),
    )


def _exact_player_ids(parameters: dict[str, Any], key: str) -> list[str]:
    raw = parameters.get(key)
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise ValueError(f"{key} must contain 1-20 player ids")
    ids = [str(player_id) for player_id in raw]
    if any(not player_id for player_id in ids) or len(set(ids)) != len(ids):
        raise ValueError(f"{key} must contain unique non-empty player ids")
    return ids


def _validate_acquisition(action_type: str, parameters: dict[str, Any]) -> None:
    required = {
        "add_player_id",
        "add_player_name",
        "add_player_search_name",
        "add_player_position",
        "add_player_team",
        "claim_priority",
        "faab_percent",
        "contingency",
        "observed_acquisition_type",
        "expected_roster_before",
        "expected_roster_after",
    }
    missing = sorted(
        key for key in required if parameters.get(key) is None or parameters.get(key) == ""
    )
    if missing:
        raise ValueError(f"{action_type} requires: {', '.join(missing)}")
    expected_type = "free_agent" if action_type == "ADD_FREE_AGENT" else "waiver"
    if parameters["observed_acquisition_type"] != expected_type:
        raise ValueError(f"{action_type} disagrees with the observed Sleeper action type")
    priority = parameters["claim_priority"]
    faab = parameters["faab_percent"]
    if not isinstance(priority, int) or isinstance(priority, bool) or priority < 1:
        raise ValueError(f"{action_type} requires a positive integer claim_priority")
    if not isinstance(faab, int) or isinstance(faab, bool) or not 0 <= faab <= 100:
        raise ValueError(f"{action_type} requires faab_percent between 0 and 100")
    if action_type == "ADD_FREE_AGENT" and faab != 0:
        raise ValueError("ADD_FREE_AGENT cannot carry a FAAB bid")
    if action_type == "WAIVER_CLAIM":
        transaction_week = parameters.get("transaction_week")
        if (
            not isinstance(transaction_week, int)
            or isinstance(transaction_week, bool)
            or transaction_week < 1
        ):
            raise ValueError("WAIVER_CLAIM requires a positive integer transaction_week")
        claim_group = parameters.get("claim_group")
        if claim_group is not None:
            if not isinstance(claim_group, list) or not 1 <= len(claim_group) <= 8:
                raise ValueError("WAIVER_CLAIM claim_group must contain 1-8 exact claims")
            keys: set[tuple[str, str]] = set()
            priorities: set[int] = set()
            for row in claim_group:
                if not isinstance(row, dict):
                    raise ValueError("WAIVER_CLAIM claim_group contains a malformed claim")
                group_add = str(row.get("add_player_id") or "")
                group_drop = str(row.get("drop_player_id") or "")
                group_priority = row.get("claim_priority")
                if (
                    not group_add
                    or not isinstance(group_priority, int)
                    or isinstance(group_priority, bool)
                    or group_priority < 1
                    or (group_add, group_drop) in keys
                    or group_priority in priorities
                ):
                    raise ValueError("WAIVER_CLAIM claim_group identities and priorities must be unique")
                keys.add((group_add, group_drop))
                priorities.add(group_priority)
            if (str(parameters["add_player_id"]), str(parameters.get("drop_player_id") or "")) not in keys:
                raise ValueError("WAIVER_CLAIM is not present in its exact claim_group")

    add_id = str(parameters["add_player_id"])
    drop_id = str(parameters.get("drop_player_id") or "")
    if drop_id:
        drop_required = (
            "drop_player_name",
            "drop_player_search_name",
            "drop_player_position",
            "drop_player_team",
        )
        if any(not str(parameters.get(key) or "").strip() for key in drop_required):
            raise ValueError(f"{action_type} requires exact drop-player UI identity")
    before = _exact_player_ids(parameters, "expected_roster_before")
    after = _exact_player_ids(parameters, "expected_roster_after")
    if add_id in before or add_id not in after:
        raise ValueError(f"{action_type} add player conflicts with the exact roster states")
    if drop_id and (drop_id not in before or drop_id in after):
        raise ValueError(f"{action_type} drop player conflicts with the exact roster states")
    transformed = [player_id for player_id in before if player_id != drop_id]
    transformed.append(add_id)
    if after != transformed:
        raise ValueError(f"{action_type} must encode exactly one roster acquisition")


def _validate_ir(action_type: str, parameters: dict[str, Any]) -> None:
    required = {
        "player_id",
        "player_name",
        "player_display_name",
        "player_position",
        "player_team",
        "observed_status",
        "expected_players",
        "expected_reserve_before",
        "expected_reserve_after",
    }
    missing = sorted(
        key for key in required if parameters.get(key) is None or parameters.get(key) == ""
    )
    if missing:
        raise ValueError(f"{action_type} requires: {', '.join(missing)}")
    player_id = str(parameters["player_id"])
    players = _exact_player_ids(parameters, "expected_players")

    def reserve_ids(key: str) -> list[str]:
        raw = parameters.get(key)
        if not isinstance(raw, list) or len(raw) > 20:
            raise ValueError(f"{key} must contain 0-20 unique player ids")
        values = [str(item) for item in raw]
        if any(not item for item in values) or len(set(values)) != len(values):
            raise ValueError(f"{key} must contain 0-20 unique player ids")
        if any(item not in players for item in values):
            raise ValueError(f"{key} contains a player outside expected_players")
        return values

    before = reserve_ids("expected_reserve_before")
    after = reserve_ids("expected_reserve_after")
    if player_id not in players:
        raise ValueError(f"{action_type} player must remain on the roster")
    if action_type == "MOVE_TO_IR":
        if player_id in before or after != [*before, player_id]:
            raise ValueError("MOVE_TO_IR must add exactly one player to reserve")
        if str(parameters["observed_status"]).upper() not in {"IR", "PUP", "NFI", "OUT"}:
            raise ValueError("MOVE_TO_IR requires an observed IR-eligible status")
    elif player_id not in before or after != [item for item in before if item != player_id]:
        raise ValueError("REMOVE_FROM_IR must remove exactly one player from reserve")


def _pending_claims(parameters: dict[str, Any], key: str) -> list[dict[str, str]]:
    raw = parameters.get(key)
    if not isinstance(raw, list) or len(raw) > 8:
        raise ValueError(f"{key} must contain 0-8 exact pending claims")
    result: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError(f"{key} contains a malformed pending claim")
        identity = {
            field: str(row.get(field) or "")
            for field in (
                "add_player_id",
                "add_player_name",
                "add_player_position",
                "add_player_team",
                "drop_player_id",
                "drop_player_name",
                "drop_player_position",
                "drop_player_team",
            )
        }
        if not all(identity[field] for field in (
            "add_player_id",
            "add_player_name",
            "add_player_position",
            "add_player_team",
        )):
            raise ValueError(f"{key} requires exact add-player identity")
        if identity["drop_player_id"] and not all(
            identity[field]
            for field in ("drop_player_name", "drop_player_position", "drop_player_team")
        ):
            raise ValueError(f"{key} requires exact drop-player identity")
        claim_key = (identity["add_player_id"], identity["drop_player_id"])
        if claim_key in identities:
            raise ValueError(f"{key} contains duplicate pending claims")
        identities.add(claim_key)
        result.append(identity)
    return result


def _validate_pending_waiver_action(
    action_type: str, parameters: dict[str, Any]
) -> None:
    if not str(parameters.get("ui_contract_version") or "").strip():
        raise ValueError(f"{action_type} requires a pending-waiver UI contract version")
    if not str(parameters.get("management_reason") or "").strip():
        raise ValueError(f"{action_type} requires a management reason")
    before = _pending_claims(parameters, "expected_claims_before")
    after = _pending_claims(parameters, "expected_claims_after")
    before_keys = [(row["add_player_id"], row["drop_player_id"]) for row in before]
    after_keys = [(row["add_player_id"], row["drop_player_id"]) for row in after]
    if action_type == "CANCEL_WAIVER_CLAIM":
        claim = parameters.get("claim")
        if not isinstance(claim, dict):
            raise ValueError("CANCEL_WAIVER_CLAIM requires one exact claim")
        target = (str(claim.get("add_player_id") or ""), str(claim.get("drop_player_id") or ""))
        if target not in before_keys or after_keys != [key for key in before_keys if key != target]:
            raise ValueError("CANCEL_WAIVER_CLAIM must remove exactly one pending claim")
    elif (
        len(before_keys) < 2
        or set(before_keys) != set(after_keys)
        or before_keys == after_keys
    ):
        raise ValueError("REORDER_WAIVER_CLAIMS must reorder the same pending claim set")


def _trade_assets(parameters: dict[str, Any], key: str) -> list[dict[str, str]]:
    raw = parameters.get(key)
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise ValueError(f"{key} must contain 1-3 exact player assets")
    assets: list[dict[str, str]] = []
    ids: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError(f"{key} contains a malformed trade asset")
        asset = {
            field: str(row.get(field) or "").strip()
            for field in ("player_id", "player_name", "player_display_name", "position", "team")
        }
        if not all(asset.values()) or asset["player_id"] in ids:
            raise ValueError(f"{key} requires unique exact player identity")
        ids.add(asset["player_id"])
        assets.append(asset)
    return assets


def _validate_trade_action(action_type: str, parameters: dict[str, Any]) -> None:
    required = {
        "counterparty_roster_id",
        "counterparty_account_label",
        "send_assets",
        "receive_assets",
        "expected_our_roster_before",
        "expected_counterparty_roster_before",
        "decision_reason",
        "championship_case",
        "upside_tier",
        "decision_confidence",
    }
    missing = sorted(
        key for key in required if parameters.get(key) is None or parameters.get(key) == ""
    )
    if missing:
        raise ValueError(f"{action_type} requires: {', '.join(missing)}")
    counterpart = parameters["counterparty_roster_id"]
    if not isinstance(counterpart, int) or isinstance(counterpart, bool) or counterpart <= 0:
        raise ValueError(f"{action_type} requires a positive counterparty roster")
    confidence = parameters["decision_confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValueError(f"{action_type} requires decision_confidence between 0 and 1")
    if action_type in {"PROPOSE_TRADE", "ACCEPT_TRADE"} and confidence < 0.92:
        raise ValueError(f"{action_type} requires at least 0.92 decision confidence")
    if action_type in {"PROPOSE_TRADE", "ACCEPT_TRADE"} and parameters["upside_tier"] != "high":
        raise ValueError(f"{action_type} requires a high-upside classification")
    sends = _trade_assets(parameters, "send_assets")
    receives = _trade_assets(parameters, "receive_assets")
    send_ids = [row["player_id"] for row in sends]
    receive_ids = [row["player_id"] for row in receives]
    if set(send_ids) & set(receive_ids):
        raise ValueError(f"{action_type} cannot send and receive the same player")
    ours = _exact_player_ids(parameters, "expected_our_roster_before")
    theirs = _exact_player_ids(parameters, "expected_counterparty_roster_before")
    if not set(send_ids) <= set(ours) or not set(receive_ids) <= set(theirs):
        raise ValueError(f"{action_type} assets conflict with the exact before rosters")
    if action_type in {"ACCEPT_TRADE", "DECLINE_TRADE"} and not str(
        parameters.get("offer_fingerprint") or ""
    ).strip():
        raise ValueError(f"{action_type} requires an exact offer fingerprint")
    if action_type == "PROPOSE_TRADE":
        if parameters.get("expiration_label") not in {"1 Hour", "END OF TODAY", "24 Hours"}:
            raise ValueError("PROPOSE_TRADE requires a bounded Sleeper expiration")
    if action_type == "ACCEPT_TRADE":
        expected_after = _exact_player_ids(parameters, "expected_our_roster_after")
        transformed = [player_id for player_id in ours if player_id not in set(send_ids)]
        transformed.extend(receive_ids)
        if set(expected_after) != set(transformed) or len(expected_after) != len(transformed):
            raise ValueError("ACCEPT_TRADE expected roster does not match the exact exchange")


def validate_action(action_type: str, parameters: dict[str, Any]) -> None:
    if action_type in FORBIDDEN_ACTIONS or action_type not in ALLOWED_ACTIONS:
        raise ValueError(f"Action type {action_type!r} cannot enter the execution outbox")
    if action_type == "DRAFT_PLAYER":
        required = {
            "draft_id",
            "player_id",
            "player_name",
            "player_position",
            "expected_pick_no",
            "owner_slot",
        }
        missing = sorted(key for key in required if parameters.get(key) in {None, ""})
        if missing:
            raise ValueError(f"DRAFT_PLAYER requires: {', '.join(missing)}")
    if action_type == "SET_LINEUP":
        required = {
            "from_player_id",
            "from_player_name",
            "from_slot",
            "to_player_id",
            "to_player_name",
            "to_slot",
            "target_slot_index",
            "expected_starters_before",
            "expected_starters_after",
        }
        missing = sorted(key for key in required if parameters.get(key) is None)
        if missing:
            raise ValueError(f"SET_LINEUP requires: {', '.join(missing)}")
        before_raw = parameters["expected_starters_before"]
        after_raw = parameters["expected_starters_after"]
        target = parameters["target_slot_index"]
        if (
            not isinstance(before_raw, list)
            or not isinstance(after_raw, list)
            or not isinstance(target, int)
            or target < 0
        ):
            raise ValueError("SET_LINEUP requires exact valid before/after starter arrays")
        before = [str(player_id) for player_id in before_raw]
        after = [str(player_id) for player_id in after_raw]
        if (
            not before
            or len(before) > 20
            or len(before) != len(after)
            or target >= len(before)
            or any(not player_id for player_id in before + after)
            or len(set(before)) != len(before)
            or len(set(after)) != len(after)
            or before[target] != str(parameters["from_player_id"])
            or after[target] != str(parameters["to_player_id"])
        ):
            raise ValueError("SET_LINEUP requires exact valid before/after starter arrays")
        source = (
            before.index(str(parameters["to_player_id"]))
            if str(parameters["to_player_id"]) in before
            else None
        )
        changed = [index for index, player_id in enumerate(before) if after[index] != player_id]
        if source is None:
            valid_swap = changed == [target] and str(parameters["from_player_id"]) not in after
        else:
            valid_swap = sorted(changed) == sorted([target, source]) and after[source] == str(
                parameters["from_player_id"]
            )
        if not valid_swap:
            raise ValueError("SET_LINEUP may encode only one exact player swap")
    if action_type in {"MOVE_TO_IR", "REMOVE_FROM_IR"}:
        _validate_ir(action_type, parameters)
    if action_type in {"CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS"}:
        _validate_pending_waiver_action(action_type, parameters)
    if action_type in {"PROPOSE_TRADE", "ACCEPT_TRADE", "DECLINE_TRADE"}:
        _validate_trade_action(action_type, parameters)
    if action_type == "DROP_PLAYER" and not parameters.get("drop_player_id"):
        raise ValueError("DROP_PLAYER requires drop_player_id")
    if action_type in {"WAIVER_CLAIM", "ADD_FREE_AGENT"}:
        _validate_acquisition(action_type, parameters)


def approval_binding(
    *,
    action_type: str,
    parameters: dict[str, Any],
    evidence_hashes: list[str],
    policy_version: str = POLICY_VERSION,
) -> dict[str, Any]:
    """The exact material state that an approval authorizes."""
    return {
        "action_type": action_type,
        "add_player_id": parameters.get("add_player_id"),
        "drop_player_id": parameters.get("drop_player_id"),
        "decision_policy_version": policy_version,
        "evidence_hashes": sorted(evidence_hashes),
    }


def approval_is_current(approved_binding: dict[str, Any], current_binding: dict[str, Any]) -> bool:
    return approved_binding == current_binding


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
