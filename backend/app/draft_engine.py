from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, replace
from statistics import median
from typing import Any

from app.rankings import RankingRow
from app.utils import normalize_player_name

FLEX_POSITIONS = {"RB", "WR", "TE"}
BASE_POSITION_SHARE = {
    "QB": 0.10,
    "RB": 0.34,
    "WR": 0.39,
    "TE": 0.10,
    "K": 0.035,
    "DEF": 0.035,
}
CHAMPIONSHIP_SIMULATION_VERSION = "title-equity-2026.1"
NFL_TEAM_ABBREVIATION_ALIASES = {
    # FantasyPros/DynastyProcess uses JAC; Sleeper's canonical defense player ID is JAX.
    "JAC": "JAX",
    # Retain the current Sleeper abbreviation if an upstream source uses WSH.
    "WSH": "WAS",
}


def _canonical_sleeper_player_id(player_id: str) -> str:
    normalized = str(player_id).upper()
    return NFL_TEAM_ABBREVIATION_ALIASES.get(normalized, normalized)


@dataclass(frozen=True)
class RosterPlan:
    rounds: int
    starters: dict[str, int]
    flex_slots: int
    targets: dict[str, int]


@dataclass(frozen=True)
class DraftCandidate:
    player_id: str
    player_name: str
    position: str
    team: str | None
    bye_week: int | None
    overall_rank: int
    position_rank: int | None
    market_adp: float | None
    draft_value_rank: float
    score: float
    vor: float
    tier_cliff: float
    roster_fit: float
    scarcity: float
    available_at_pick_probability: float
    survival_probability: float
    confidence: float
    injury_status: str | None
    injury_detail: str | None
    depth_chart_order: int | None
    replacement_player_name: str | None
    replacement_draft_value_rank: float | None
    replacement_availability_probability: float | None
    expected_position_demand_before_next_pick: float
    cost_of_waiting: float
    championship_ceiling: float
    championship_win_probability: float | None
    championship_equity_delta: float | None
    championship_simulations: int | None
    reasons: list[str]
    score_components: dict[str, float]


@dataclass(frozen=True)
class _TeamProjection:
    weekly_mean: float
    weekly_sd: float
    season_sd: float
    breakout_outcomes: tuple[tuple[float, float], ...]


def roster_plan_from_settings(settings: dict[str, Any] | None) -> RosterPlan:
    settings = settings or {}
    rounds = max(int(settings.get("rounds") or 16), 1)
    starters = {
        "QB": max(int(settings.get("slots_qb") or 1), 0),
        "RB": max(int(settings.get("slots_rb") or 2), 0),
        "WR": max(int(settings.get("slots_wr") or 3), 0),
        "TE": max(int(settings.get("slots_te") or 1), 0),
        "K": max(int(settings.get("slots_k") or 1), 0),
        "DEF": max(int(settings.get("slots_def") or 1), 0),
    }
    flex_slots = max(int(settings.get("slots_flex") or 0), 0)
    targets = dict(starters)
    bench_slots = max(rounds - sum(starters.values()) - flex_slots, 0)

    skill_total = starters["RB"] + starters["WR"] or 1
    wr_flex = round(flex_slots * starters["WR"] / skill_total)
    targets["RB"] += flex_slots - wr_flex
    targets["WR"] += wr_flex
    while bench_slots:
        rb_ratio = targets["RB"] / max(starters["RB"], 1)
        wr_ratio = targets["WR"] / max(starters["WR"], 1)
        targets["RB" if rb_ratio <= wr_ratio else "WR"] += 1
        bench_slots -= 1
    return RosterPlan(rounds=rounds, starters=starters, flex_slots=flex_slots, targets=targets)


def replacement_ranks_from_settings(
    settings: dict[str, Any] | None, scoring_type: str | None = None
) -> dict[str, int]:
    """Estimate the first player outside a league-wide starting lineup by position."""
    settings = settings or {}
    plan = roster_plan_from_settings(settings)
    teams = max(int(settings.get("teams") or 12), 1)
    scoring = (scoring_type or "half_ppr").lower()
    flex_weights = {
        "RB": float(plan.starters["RB"]),
        "WR": float(plan.starters["WR"]),
        "TE": float(plan.starters["TE"]) * 0.15,
    }
    if scoring in {"std", "standard"}:
        flex_weights["RB"] *= 1.08
        flex_weights["WR"] *= 0.92
    elif scoring in {"ppr", "full_ppr"}:
        flex_weights["RB"] *= 0.90
        flex_weights["WR"] *= 1.10
    else:
        flex_weights["RB"] *= 1.03
        flex_weights["WR"] *= 0.97
    total_weight = sum(flex_weights.values()) or 1.0
    flex_demand = teams * plan.flex_slots
    flex_allocations = {
        position: round(flex_demand * weight / total_weight)
        for position, weight in flex_weights.items()
    }
    allocation_delta = flex_demand - sum(flex_allocations.values())
    if allocation_delta:
        flex_allocations[max(flex_weights, key=flex_weights.get)] += allocation_delta

    replacements = {position: teams * count + 1 for position, count in plan.starters.items()}
    for position, allocation in flex_allocations.items():
        replacements[position] += allocation
    replacements["DST"] = replacements["DEF"]
    return replacements


def _snake_slot(pick_no: int, teams: int) -> int:
    round_index = (pick_no - 1) // teams
    index = (pick_no - 1) % teams
    return index + 1 if round_index % 2 == 0 else teams - index


def estimate_position_demand_before_next_pick(
    *,
    plan: RosterPlan,
    teams: int,
    owner_pick_no: int,
    following_owner_pick: int,
    owner_slot: int | None,
    team_roster_counts: dict[int, dict[str, int]] | None,
    recent_position_counts: dict[str, int] | None,
) -> dict[str, float]:
    """Estimate how many players at each position opponents will take before our next turn."""
    positions = tuple(BASE_POSITION_SHARE)
    demand = {position: 0.0 for position in positions}
    recent = recent_position_counts or {}
    recent_total = sum(recent.values())
    roster_counts = team_roster_counts or {}
    intervening = range(owner_pick_no + 1, following_owner_pick)
    for pick_no in intervening:
        slot = _snake_slot(pick_no, teams) if owner_slot is not None else 0
        counts = roster_counts.get(slot, {})
        weights: dict[str, float] = {}
        round_no = (pick_no - 1) // teams + 1
        for position in positions:
            base = BASE_POSITION_SHARE[position]
            target = plan.targets.get(position, 0)
            current = counts.get(position, 0)
            direct_gap = max(plan.starters.get(position, 0) - current, 0)
            target_gap = max(target - current, 0)
            weight = base * (1 + 0.85 * direct_gap + 0.30 * target_gap / max(target, 1))
            if current >= target:
                weight *= 0.12
            if position in {"K", "DEF"} and round_no < max(plan.rounds - 2, 10):
                weight *= 0.08
            if recent_total:
                recent_share = recent.get(position, 0) / recent_total
                expected_share = BASE_POSITION_SHARE[position]
                run_adjustment = max(min((recent_share - expected_share) * 0.45, 0.18), -0.12)
                weight *= 1 + run_adjustment
            weights[position] = max(weight, 0.0001)
        total = sum(weights.values()) or 1.0
        for position, weight in weights.items():
            demand[position] += weight / total
    return {position: round(value, 3) for position, value in demand.items()}


def map_rankings_to_sleeper(
    rankings: list[RankingRow], players: dict[str, dict[str, Any]]
) -> tuple[list[tuple[str, RankingRow]], list[str]]:
    """Conservative name/team/position match; ambiguous rows are excluded."""
    index: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    for player_id, player in players.items():
        position = (player.get("position") or "").upper()
        name = normalize_player_name(player.get("full_name") or "")
        if name and position:
            index.setdefault((name, position), []).append((player_id, player))
    matched: list[tuple[str, RankingRow]] = []
    rejected: list[str] = []
    for row in rankings:
        if row.position in {"DST", "DEF"} and row.team:
            canonical_team = _canonical_sleeper_player_id(row.team)
            defense_matches = [
                str(player_id)
                for player_id, player in players.items()
                if (player.get("position") or "").upper() in {"DEF", "DST"}
                and (
                    str(player_id).upper() == canonical_team
                    or (player.get("team") or "").upper() == canonical_team
                    or normalize_player_name(
                        player.get("full_name")
                        or f"{player.get('first_name') or ''} {player.get('last_name') or ''}"
                    )
                    == row.normalized_name
                )
            ]
            defense_matches = list(dict.fromkeys(defense_matches))
            if len(defense_matches) != 1:
                rejected.append(row.player_name)
                continue
            matched.append((defense_matches[0], row))
            continue
        candidates = index.get((row.normalized_name, row.position), [])
        if len(candidates) != 1:
            rejected.append(row.player_name)
            continue
        player_id, player = candidates[0]
        row_team = (row.team or "").upper()
        sleeper_team = (player.get("team") or "").upper()
        if row_team and sleeper_team and row_team != sleeper_team:
            rejected.append(row.player_name)
            continue
        matched.append((player_id, row))
    return matched, rejected


def _flex_remaining(plan: RosterPlan, roster_counts: dict[str, int]) -> int:
    surplus = sum(
        max(roster_counts.get(position, 0) - plan.starters.get(position, 0), 0)
        for position in FLEX_POSITIONS
    )
    return max(plan.flex_slots - surplus, 0)


def _bye_week(player: dict[str, Any]) -> int | None:
    value = player.get("bye_week") or player.get("bye")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _draft_value_rank(row: RankingRow) -> float:
    expert_rank = float(row.overall_rank or 999)
    if row.market_adp is None:
        return expert_rank
    return expert_rank * 0.65 + row.market_adp * 0.35


def _availability_probability(row: RankingRow, pick_no: int) -> float:
    expected_pick = row.market_adp or float(row.overall_rank or 999)
    expert_spread = (row.rank_sd or 0) * 0.35
    scale = max(2.5, min(expert_spread, 12.0), pick_no * 0.04)
    exponent = max(min((pick_no - expected_pick) / scale, 20), -20)
    return 1 / (1 + math.exp(exponent))


def _replacement_outlook(
    *,
    player_id: str,
    row: RankingRow,
    available_rows: list[tuple[str, RankingRow]],
    players: dict[str, dict[str, Any]],
    following_owner_pick: int,
    expected_position_demand: float,
) -> dict[str, Any]:
    same_position = sorted(
        [
            (other_id, other)
            for other_id, other in available_rows
            if other_id != player_id and other.position == row.position
        ],
        key=lambda item: (
            _draft_value_rank(item[1]),
            item[1].overall_rank or 999,
            item[0],
        ),
    )
    if not same_position:
        return {
            "player_name": None,
            "draft_value_rank": None,
            "availability_probability": None,
            "rank_loss": 0.0,
            "cost": 0.0,
        }

    demand_index = max(math.ceil(expected_position_demand) - 1, 0)
    credible_index = next(
        (
            index
            for index, (other_id, other) in enumerate(same_position)
            if _availability_probability(other, following_owner_pick) >= 0.35
            and _health_context(players.get(other_id, {}))[0] > -1.0
        ),
        len(same_position) - 1,
    )
    replacement_id, replacement = same_position[
        min(max(demand_index, credible_index), len(same_position) - 1)
    ]
    replacement_probability = _availability_probability(replacement, following_owner_pick)
    rank_loss = max(_draft_value_rank(replacement) - _draft_value_rank(row), 0.0)
    gone_probability = 1 - _availability_probability(row, following_owner_pick)
    role_penalty = 0.0
    depth = players.get(replacement_id, {}).get("depth_chart_order")
    try:
        if depth is not None and int(depth) > 1:
            role_penalty = min((int(depth) - 1) * 0.12, 0.36)
    except (TypeError, ValueError):
        role_penalty = 0.25
    cost = min(gone_probability * rank_loss / 12 + role_penalty, 2.0)
    return {
        "player_name": replacement.player_name,
        "draft_value_rank": round(_draft_value_rank(replacement), 2),
        "availability_probability": round(replacement_probability, 5),
        "rank_loss": round(rank_loss, 2),
        "cost": round(cost, 5),
    }


def _health_context(player: dict[str, Any]) -> tuple[float, str | None, str | None]:
    injury_status = str(player.get("injury_status") or "").strip()
    normalized_status = injury_status.upper()
    roster_status = str(player.get("status") or "").upper()
    body_part = str(player.get("injury_body_part") or "").strip()
    notes = str(player.get("injury_notes") or "").strip()
    practice = str(player.get("practice_description") or "").strip()

    adjustment = 0.0
    if normalized_status in {"IR", "PUP", "NFI", "SUSP"} or roster_status == "INACTIVE":
        adjustment = -2.5
    elif normalized_status in {"OUT", "DOUBTFUL"}:
        adjustment = -1.2
    elif normalized_status in {"QUESTIONABLE", "Q"}:
        adjustment = -0.18
    if practice.upper() in {"DNP", "DID NOT PARTICIPATE"}:
        adjustment -= 0.45
    if normalized_status in {"QUESTIONABLE", "Q"} and any(
        term in body_part.upper() for term in ("ACL", "ACHILLES")
    ):
        adjustment -= 0.35

    detail_parts = [part for part in (body_part, notes, practice) if part]
    detail = "; ".join(detail_parts) or None
    return adjustment, injury_status or None, detail


def _normalized_position(position: str | None) -> str:
    normalized = str(position or "").upper()
    return "DEF" if normalized == "DST" else normalized


def _player_position(
    player_id: str,
    *,
    players: dict[str, dict[str, Any]],
    ranking_by_player: dict[str, RankingRow],
) -> str:
    ranking = ranking_by_player.get(player_id)
    return _normalized_position(
        players.get(player_id, {}).get("position") or (ranking.position if ranking else "")
    )


def _championship_ceiling_score(
    *,
    row: RankingRow,
    player: dict[str, Any],
    position: str,
    final_discretionary_pick: bool,
) -> float:
    """Measure asymmetric upside without treating ordinary uncertainty as value."""
    if position not in FLEX_POSITIONS:
        return 0.0
    try:
        depth_order = int(player.get("depth_chart_order") or 1)
    except (TypeError, ValueError):
        depth_order = 99

    disagreement = min(max(float(row.rank_sd or 0) - 4, 0) / 24, 0.45)
    contingent_role = 0.0
    if position == "RB" and depth_order == 2:
        contingent_role = 0.62
    elif position == "RB" and depth_order == 3:
        contingent_role = 0.24
    elif position in {"WR", "TE"} and depth_order == 2:
        contingent_role = 0.22
    market_signal = 0.0
    if row.market_adp is not None and row.overall_rank is not None:
        market_signal = min(max((row.overall_rank - row.market_adp) / 30, 0), 0.30)
    health_adjustment, _, _ = _health_context(player)
    health_penalty = min(abs(health_adjustment) * 0.25, 0.55) if health_adjustment < 0 else 0.0
    ceiling = max(disagreement + contingent_role + market_signal - health_penalty, 0.0)
    return round(ceiling * (1.35 if final_discretionary_pick else 0.45), 5)


def _mandatory_roster_slots(plan: RosterPlan, counts: dict[str, int]) -> int:
    direct_missing = sum(
        max(plan.starters.get(position, 0) - counts.get(position, 0), 0)
        for position in plan.starters
    )
    return direct_missing + _flex_remaining(plan, counts)


def _completion_pick(
    *,
    market_order: list[tuple[str, RankingRow]],
    available: set[str],
    roster: set[str],
    players: dict[str, dict[str, Any]],
    ranking_by_player: dict[str, RankingRow],
    plan: RosterPlan,
) -> str | None:
    counts: dict[str, int] = {}
    for player_id in roster:
        position = _player_position(player_id, players=players, ranking_by_player=ranking_by_player)
        if position:
            counts[position] = counts.get(position, 0) + 1

    remaining = max(plan.rounds - len(roster), 1)
    mandatory_missing = _mandatory_roster_slots(plan, counts)
    flex_missing = _flex_remaining(plan, counts)
    direct_needed = {
        position
        for position, required in plan.starters.items()
        if counts.get(position, 0) < required
    }
    eligible_when_forced = set(direct_needed)
    if flex_missing:
        eligible_when_forced.update(FLEX_POSITIONS)

    best_id: str | None = None
    best_score = -10_000.0
    inspected = 0
    for player_id, row in market_order:
        if player_id not in available:
            continue
        position = _normalized_position(row.position)
        if position not in plan.targets:
            continue
        if remaining <= mandatory_missing and position not in eligible_when_forced:
            continue
        inspected += 1
        direct_gap = max(plan.starters.get(position, 0) - counts.get(position, 0), 0)
        target_gap = max(plan.targets.get(position, 0) - counts.get(position, 0), 0)
        score = -_draft_value_rank(row) + 18 * direct_gap + 5 * target_gap
        if position in {"K", "DEF"}:
            specialist_missing = sum(
                max(plan.starters.get(item, 0) - counts.get(item, 0), 0) for item in ("K", "DEF")
            )
            if remaining > specialist_missing:
                score -= 90
        if position in {"QB", "TE"} and counts.get(position, 0) >= plan.targets.get(position, 0):
            score -= 70
        if counts.get(position, 0) >= plan.targets.get(position, 0):
            score -= 14
        if score > best_score:
            best_id = player_id
            best_score = score
        if inspected >= 72:
            break
    return best_id


def _project_completed_rosters(
    *,
    rankings: list[tuple[str, RankingRow]],
    players: dict[str, dict[str, Any]],
    team_roster_player_ids: dict[int, set[str]],
    drafted_player_ids: set[str],
    candidate_id: str,
    owner_slot: int,
    owner_pick_no: int,
    plan: RosterPlan,
    teams: int,
) -> dict[int, set[str]]:
    rosters = {slot: set(team_roster_player_ids.get(slot, set())) for slot in range(1, teams + 1)}
    ranking_by_player = {player_id: row for player_id, row in rankings}
    market_order = sorted(
        rankings,
        key=lambda item: (
            item[1].market_adp or _draft_value_rank(item[1]),
            item[1].overall_rank or 999,
            item[0],
        ),
    )
    available = {
        player_id
        for player_id, _ in rankings
        if player_id not in drafted_player_ids and player_id != candidate_id
    }
    first_unfilled_pick = len(drafted_player_ids) + 1
    total_picks = teams * plan.rounds

    for pick_no in range(first_unfilled_pick, min(owner_pick_no, total_picks + 1)):
        slot = _snake_slot(pick_no, teams)
        selected = _completion_pick(
            market_order=market_order,
            available=available,
            roster=rosters[slot],
            players=players,
            ranking_by_player=ranking_by_player,
            plan=plan,
        )
        if selected is not None:
            rosters[slot].add(selected)
            available.discard(selected)

    rosters[owner_slot].add(candidate_id)
    for pick_no in range(owner_pick_no + 1, total_picks + 1):
        slot = _snake_slot(pick_no, teams)
        if len(rosters[slot]) >= plan.rounds:
            continue
        selected = _completion_pick(
            market_order=market_order,
            available=available,
            roster=rosters[slot],
            players=players,
            ranking_by_player=ranking_by_player,
            plan=plan,
        )
        if selected is not None:
            rosters[slot].add(selected)
            available.discard(selected)
    return rosters


def _player_projection(
    player_id: str,
    *,
    players: dict[str, dict[str, Any]],
    ranking_by_player: dict[str, RankingRow],
) -> tuple[str, float, float, tuple[float, float] | None]:
    row = ranking_by_player.get(player_id)
    position = _player_position(player_id, players=players, ranking_by_player=ranking_by_player)
    position_rank = max(int((row.position_rank if row else None) or 60), 1)
    rank_root = math.sqrt(max(position_rank - 1, 0))
    if position == "QB":
        mean = max(24.0 - 1.40 * rank_root, 14.5)
        weekly_sd = 5.4
    elif position == "RB":
        mean = max(20.0 - 1.65 * rank_root, 5.0)
        weekly_sd = 6.8
    elif position == "WR":
        mean = max(20.0 - 1.42 * rank_root, 5.5)
        weekly_sd = 7.1
    elif position == "TE":
        mean = max(17.0 - 1.75 * rank_root, 4.5)
        weekly_sd = 5.8
    elif position in {"K", "DEF"}:
        mean = max(9.2 - 0.16 * rank_root, 6.0)
        weekly_sd = 4.1
    else:
        return position, 0.0, 0.0, None

    player = players.get(player_id, {})
    health_adjustment, _, _ = _health_context(player)
    if health_adjustment <= -2:
        mean *= 0.55
    elif health_adjustment <= -1:
        mean *= 0.76
    elif health_adjustment < 0:
        mean *= 0.96
    weekly_sd += min(abs(health_adjustment), 2.0)

    try:
        depth_order = int(player.get("depth_chart_order") or 1)
    except (TypeError, ValueError):
        depth_order = 99
    disagreement = min(max(float(row.rank_sd or 0) - 4, 0) / 35, 0.28) if row else 0.0
    breakout: tuple[float, float] | None = None
    if position == "RB" and depth_order == 2:
        breakout = (min(0.17 + disagreement, 0.42), max(7.0 - position_rank * 0.04, 3.0))
    elif position == "RB" and depth_order == 3:
        breakout = (min(0.07 + disagreement, 0.24), max(5.0 - position_rank * 0.025, 2.0))
    elif position in {"WR", "TE"} and depth_order == 2:
        breakout = (min(0.10 + disagreement, 0.30), max(4.5 - position_rank * 0.025, 1.8))
    elif disagreement >= 0.10 and position in FLEX_POSITIONS:
        breakout = (min(0.06 + disagreement, 0.24), max(3.5 - position_rank * 0.015, 1.2))
    return position, mean, weekly_sd, breakout


def _team_projection(
    roster: set[str],
    *,
    players: dict[str, dict[str, Any]],
    ranking_by_player: dict[str, RankingRow],
    plan: RosterPlan,
) -> _TeamProjection:
    projections: dict[str, list[tuple[str, float, float, tuple[float, float] | None]]] = {}
    for player_id in roster:
        position, mean, weekly_sd, breakout = _player_projection(
            player_id, players=players, ranking_by_player=ranking_by_player
        )
        if position:
            projections.setdefault(position, []).append((player_id, mean, weekly_sd, breakout))
    for rows in projections.values():
        rows.sort(key=lambda item: (-item[1], item[0]))

    starters: list[tuple[str, float, float, tuple[float, float] | None]] = []
    used: set[str] = set()
    for position, required in plan.starters.items():
        for projection in projections.get(position, [])[:required]:
            starters.append(projection)
            used.add(projection[0])
    flex_options = sorted(
        [
            item
            for position in FLEX_POSITIONS
            for item in projections.get(position, [])
            if item[0] not in used
        ],
        key=lambda item: (-item[1], item[0]),
    )
    for projection in flex_options[: plan.flex_slots]:
        starters.append(projection)
        used.add(projection[0])

    missing_starters = max(sum(plan.starters.values()) + plan.flex_slots - len(starters), 0)
    weekly_mean = sum(item[1] for item in starters) + missing_starters * 5.0
    weekly_sd = math.sqrt(sum(item[2] ** 2 for item in starters) + missing_starters * 25)
    bench = [
        item
        for position in FLEX_POSITIONS
        for item in projections.get(position, [])
        if item[0] not in used
    ]
    bench.sort(key=lambda item: (-item[1], item[0]))
    # Depth has real replacement value, but only a small fraction converts into weekly points.
    weekly_mean += sum(item[1] for item in bench[:2]) * 0.035
    breakouts = tuple(item[3] for item in [*starters, *bench] if item[3] is not None)
    season_sd = max(weekly_sd * 0.48 + len(breakouts) * 0.35, 7.0)
    return _TeamProjection(
        weekly_mean=weekly_mean,
        weekly_sd=max(weekly_sd, 10.0),
        season_sd=season_sd,
        breakout_outcomes=breakouts,
    )


def _playoff_winner(
    seeds: list[int],
    *,
    profiles: dict[int, _TeamProjection],
    season_form: dict[int, float],
    rng: random.Random,
) -> int:
    remaining = list(seeds)
    if len(remaining) == 6:
        advanced = remaining[:2]
        for high_index, low_index in ((2, 5), (3, 4)):
            high_seed = remaining[high_index]
            low_seed = remaining[low_index]
            high = profiles[high_seed]
            low = profiles[low_seed]
            high_score = (
                high.weekly_mean + 0.18 * season_form[high_seed] + rng.gauss(0, high.weekly_sd)
            )
            low_score = low.weekly_mean + 0.18 * season_form[low_seed] + rng.gauss(0, low.weekly_sd)
            advanced.append(high_seed if high_score >= low_score else low_seed)
        seed_index = {team: index for index, team in enumerate(seeds)}
        remaining = sorted(advanced, key=seed_index.get)
    while len(remaining) > 1:
        bye_count = 1 if len(remaining) % 2 else 0
        advanced = remaining[:bye_count]
        playing = remaining[bye_count:]
        for index in range(len(playing) // 2):
            high_seed = playing[index]
            low_seed = playing[-index - 1]
            high = profiles[high_seed]
            low = profiles[low_seed]
            high_score = (
                high.weekly_mean + 0.18 * season_form[high_seed] + rng.gauss(0, high.weekly_sd)
            )
            low_score = low.weekly_mean + 0.18 * season_form[low_seed] + rng.gauss(0, low.weekly_sd)
            advanced.append(high_seed if high_score >= low_score else low_seed)
        seed_index = {team: index for index, team in enumerate(seeds)}
        remaining = sorted(advanced, key=seed_index.get)
    return remaining[0]


def estimate_candidate_championship_equity(
    *,
    rankings: list[tuple[str, RankingRow]],
    players: dict[str, dict[str, Any]],
    candidates: list[DraftCandidate],
    drafted_player_ids: set[str],
    team_roster_player_ids: dict[int, set[str]],
    owner_slot: int,
    owner_pick_no: int,
    draft_settings: dict[str, Any] | None,
    playoff_settings: dict[str, Any] | None = None,
    simulations: int = 1200,
) -> dict[str, float]:
    """Condition on each candidate, finish the draft, then simulate first-place outcomes."""
    settings = draft_settings or {}
    teams = max(int(settings.get("teams") or 12), 2)
    plan = roster_plan_from_settings(settings)
    playoff_teams = int((playoff_settings or {}).get("playoff_teams") or min(6, teams))
    playoff_teams = max(min(playoff_teams, teams), 2)
    simulation_count = max(int(simulations), 200)
    ranking_by_player = {player_id: row for player_id, row in rankings}
    results: dict[str, float] = {}

    for candidate in candidates:
        rosters = _project_completed_rosters(
            rankings=rankings,
            players=players,
            team_roster_player_ids=team_roster_player_ids,
            drafted_player_ids=drafted_player_ids,
            candidate_id=candidate.player_id,
            owner_slot=owner_slot,
            owner_pick_no=owner_pick_no,
            plan=plan,
            teams=teams,
        )
        profiles = {
            slot: _team_projection(
                roster,
                players=players,
                ranking_by_player=ranking_by_player,
                plan=plan,
            )
            for slot, roster in rosters.items()
        }
        # Reuse the same random stream for every candidate to reduce comparison noise.
        rng = random.Random(20260908 + owner_pick_no * 101 + len(drafted_player_ids) * 17)
        championships = 0
        for _ in range(simulation_count):
            season_form: dict[int, float] = {}
            season_scores: dict[int, float] = {}
            for slot, profile in profiles.items():
                breakout = sum(
                    impact
                    for probability, impact in profile.breakout_outcomes
                    if rng.random() < probability
                )
                form = rng.gauss(0, profile.season_sd) + breakout
                season_form[slot] = form
                season_scores[slot] = profile.weekly_mean + form
            seeds = sorted(
                profiles,
                key=lambda slot: (-season_scores[slot], slot),
            )[:playoff_teams]
            if owner_slot not in seeds:
                continue
            winner = _playoff_winner(
                seeds,
                profiles=profiles,
                season_form=season_form,
                rng=rng,
            )
            championships += winner == owner_slot
        results[candidate.player_id] = round(championships / simulation_count, 5)
    return results


def _candidate_score(
    *,
    row: RankingRow,
    player: dict[str, Any],
    roster_counts: dict[str, int],
    roster_byes: dict[str, list[int]],
    plan: RosterPlan,
    owner_pick_no: int,
    following_owner_pick: int,
    scoring_type: str,
    replacement_ranks: dict[str, int],
    replacement_outlook: dict[str, Any],
) -> tuple[float, dict[str, float], float, list[str]]:
    position = "DEF" if row.position == "DST" else row.position
    overall = row.overall_rank or 999
    positional = row.position_rank or 99
    replacement = replacement_ranks.get(position, 13)
    vor = max(replacement - positional, 0) / replacement
    scarcity = max((replacement - positional) / replacement, -1.0)
    current_count = roster_counts.get(position, 0)
    target = plan.targets.get(position, 0)
    target_gap = max(target - current_count, 0)
    direct_need = max(plan.starters.get(position, 0) - current_count, 0)
    flex_need = _flex_remaining(plan, roster_counts) if position in FLEX_POSITIONS else 0
    starter_need = direct_need + (min(flex_need, 1) if position in FLEX_POSITIONS else 0)
    remaining_picks = max(plan.rounds - sum(roster_counts.values()), 1)
    mandatory_missing = _flex_remaining(plan, roster_counts) + sum(
        max(plan.starters.get(pos, 0) - roster_counts.get(pos, 0), 0) for pos in plan.starters
    )
    final_discretionary_pick = remaining_picks <= 4 and remaining_picks - mandatory_missing == 1

    if target <= 0:
        roster_fit = -1.0
    elif current_count >= target:
        roster_fit = -0.9
    else:
        roster_fit = min(0.25 + target_gap / target + 0.35 * starter_need, 2.3)

    phase = sum(roster_counts.values()) / plan.rounds
    phase_adjustment = 0.0
    if position in {"K", "DEF"}:
        specialists_missing = sum(
            max(plan.starters.get(pos, 0) - roster_counts.get(pos, 0), 0) for pos in ("K", "DEF")
        )
        phase_adjustment = -3.5 if remaining_picks > specialists_missing else 2.2
    elif position == "QB" and current_count >= plan.starters.get(position, 0):
        phase_adjustment = -2.0 if phase < 0.78 else -0.15
        roster_fit = min(roster_fit, 0.45)
    elif position == "TE" and current_count >= plan.starters.get(position, 0):
        phase_adjustment = -1.3 if phase < 0.72 else -0.15
        roster_fit = min(roster_fit, 0.55)
    if starter_need and remaining_picks <= mandatory_missing:
        phase_adjustment += 1.8

    health_adjustment, injury_status, _ = _health_context(player)

    depth_order_value = player.get("depth_chart_order")
    try:
        depth_order = int(depth_order_value) if depth_order_value is not None else None
    except (TypeError, ValueError):
        depth_order = None
    role_adjustment = 0.0
    if position in FLEX_POSITIONS and depth_order and depth_order > 1:
        role_adjustment = -min(0.08 * (depth_order - 1), 0.24)

    bye = _bye_week(player)
    bye_overlap = 0.0
    if bye is not None and bye in roster_byes.get(position, []):
        bye_overlap = -0.18 * roster_byes[position].count(bye)

    value_rank = _draft_value_rank(row)
    value = 1.2 / math.sqrt(max(value_rank, 1) / 12)
    market_value = max(min((owner_pick_no - overall) / 24, 1.5), -1.5)
    survival = _availability_probability(row, following_owner_pick)
    uncertainty = min((row.rank_sd or 0) / 40, 0.45)
    championship_ceiling = _championship_ceiling_score(
        row=row,
        player=player,
        position=position,
        final_discretionary_pick=final_discretionary_pick,
    )
    elite_qb_ceiling = 0.0
    if position == "QB" and current_count == 0 and positional <= 5:
        value_slip = max(owner_pick_no - value_rank, 0)
        elite_qb_ceiling = min((6 - positional) * 0.12 + value_slip / 24, 1.15)
    scoring_adjustment = 0.0
    if scoring_type in {"std", "standard"}:
        scoring_adjustment = (
            0.25 if position == "RB" else -0.25 if position in {"WR", "TE"} else 0.0
        )
    elif scoring_type in {"half_ppr", "half-ppr", "0.5_ppr"}:
        scoring_adjustment = (
            0.18 if position == "RB" else -0.08 if position in {"WR", "TE"} else 0.0
        )
    components = {
        "consensus_value": value + market_value,
        "replacement_value": vor * 1.35,
        "scarcity": scarcity * 0.55,
        "roster_fit": roster_fit * 1.25,
        "next_turn_urgency": (1 - survival) * 0.45,
        "cost_of_waiting": float(replacement_outlook.get("cost") or 0),
        "draft_phase": phase_adjustment,
        "bye_overlap": bye_overlap,
        "health": health_adjustment,
        "role": role_adjustment,
        "uncertainty": uncertainty * 0.25 if final_discretionary_pick else -uncertainty,
        "championship_ceiling": championship_ceiling,
        "elite_qb_ceiling": elite_qb_ceiling,
        "scoring_format": scoring_adjustment,
    }
    reasons: list[str] = []
    if starter_need:
        reasons.append(f"Fills an {'open starter' if direct_need else 'open FLEX'} slot")
    elif target_gap:
        reasons.append(f"Adds needed {position} depth ({current_count}/{target} target)")
    if market_value >= 0.35:
        reasons.append(f"Value: ECR {overall} at pick {owner_pick_no}")
    if survival <= 0.35:
        reasons.append(f"Only {survival:.0%} chance to reach your following pick")
    if float(replacement_outlook.get("rank_loss") or 0) >= 6:
        reasons.append(
            f"Waiting projects {replacement_outlook.get('player_name') or 'a lower tier'} "
            f"({replacement_outlook['rank_loss']:.0f} value-rank drop)"
        )
    if phase_adjustment < -0.5:
        reasons.append("Roster timing makes this position a lower priority")
    if health_adjustment < 0:
        reasons.append(f"Availability risk: {injury_status or 'inactive'}")
    if role_adjustment < 0:
        reasons.append(f"Listed No. {depth_order} at his depth-chart position")
    if elite_qb_ceiling >= 0.35:
        reasons.insert(0, "Elite-QB ceiling trigger: top-five option at a favorable cost")
    if championship_ceiling >= 0.45:
        reasons.insert(0, "Asymmetric final-bench upside increases first-place equity")
    if bye_overlap < 0:
        reasons.append(f"Bye-week overlap at {position}")
    if not reasons:
        reasons.append("Best blend of value and roster construction")
    return sum(components.values()), components, survival, reasons[:3]


def recommend_draft_pick(
    *,
    rankings: list[tuple[str, RankingRow]],
    drafted_player_ids: set[str],
    roster_player_ids: set[str],
    players: dict[str, dict[str, Any]],
    owner_pick_no: int,
    following_owner_pick: int,
    draft_settings: dict[str, Any] | None = None,
    scoring_type: str | None = None,
    owner_slot: int | None = None,
    team_roster_counts: dict[int, dict[str, int]] | None = None,
    team_roster_player_ids: dict[int, set[str]] | None = None,
    recent_position_counts: dict[str, int] | None = None,
    playoff_settings: dict[str, Any] | None = None,
    championship_simulations: int = 1200,
) -> dict[str, Any]:
    plan = roster_plan_from_settings(draft_settings)
    normalized_scoring = (scoring_type or "half_ppr").lower()
    settings = draft_settings or {}
    teams = max(int(settings.get("teams") or 12), 1)
    replacement_ranks = replacement_ranks_from_settings(settings, normalized_scoring)
    expected_position_demand = estimate_position_demand_before_next_pick(
        plan=plan,
        teams=teams,
        owner_pick_no=owner_pick_no,
        following_owner_pick=following_owner_pick,
        owner_slot=owner_slot,
        team_roster_counts=team_roster_counts,
        recent_position_counts=recent_position_counts,
    )
    ranking_by_player = {player_id: row for player_id, row in rankings}
    roster_counts: dict[str, int] = {}
    roster_byes: dict[str, list[int]] = {}
    for player_id in roster_player_ids:
        player = players.get(player_id, {})
        ranking = ranking_by_player.get(player_id)
        position = (player.get("position") or (ranking.position if ranking else "")).upper()
        position = "DEF" if position == "DST" else position
        roster_counts[position] = roster_counts.get(position, 0) + 1
        bye = _bye_week(player) or (ranking.bye_week if ranking else None)
        if bye is not None:
            roster_byes.setdefault(position, []).append(bye)

    candidates: list[DraftCandidate] = []
    canonical_drafted_player_ids = {
        _canonical_sleeper_player_id(player_id) for player_id in drafted_player_ids
    }
    available_rows = [
        (player_id, row)
        for player_id, row in rankings
        if _canonical_sleeper_player_id(player_id) not in canonical_drafted_player_ids
    ]
    for index, (player_id, row) in enumerate(available_rows):
        if row.overall_rank is None:
            continue
        position = "DEF" if row.position == "DST" else row.position
        replacement_outlook = _replacement_outlook(
            player_id=player_id,
            row=row,
            available_rows=available_rows,
            players=players,
            following_owner_pick=following_owner_pick,
            expected_position_demand=expected_position_demand.get(position, 0.0),
        )
        score, components, survival, reasons = _candidate_score(
            row=row,
            player=players.get(player_id, {}),
            roster_counts=roster_counts,
            roster_byes=roster_byes,
            plan=plan,
            owner_pick_no=owner_pick_no,
            following_owner_pick=following_owner_pick,
            scoring_type=normalized_scoring,
            replacement_ranks=replacement_ranks,
            replacement_outlook=replacement_outlook,
        )
        better_same_position_available = any(
            other.position == row.position for _, other in available_rows[:index]
        )
        next_same_position = next(
            (other for _, other in available_rows[index + 1 :] if other.position == row.position),
            None,
        )
        cliff = (
            0.0
            if better_same_position_available
            else float(
                (next_same_position.overall_rank if next_same_position else 300) - row.overall_rank
            )
        )
        cliff_value = min(max(cliff / 18, 0), 0.5)
        score += cliff_value
        components["tier_cliff"] = cliff_value
        if cliff >= 7:
            reasons.insert(0, f"{int(cliff)}-pick tier drop behind him")
        replacement = replacement_ranks.get(position, 13)
        player = players.get(player_id, {})
        _, injury_status, injury_detail = _health_context(player)
        depth_order_value = player.get("depth_chart_order")
        try:
            depth_order = int(depth_order_value) if depth_order_value is not None else None
        except (TypeError, ValueError):
            depth_order = None
        candidates.append(
            DraftCandidate(
                player_id=player_id,
                player_name=row.player_name,
                position=position,
                team=row.team,
                bye_week=_bye_week(players.get(player_id, {})) or row.bye_week,
                overall_rank=row.overall_rank,
                position_rank=row.position_rank,
                market_adp=row.market_adp,
                draft_value_rank=round(_draft_value_rank(row), 2),
                score=round(score, 5),
                vor=round(max(replacement - (row.position_rank or 99), 0) / replacement, 5),
                tier_cliff=cliff,
                roster_fit=round(components["roster_fit"] / 1.25, 5),
                scarcity=round(components["scarcity"] / 0.55, 5),
                available_at_pick_probability=round(
                    _availability_probability(row, owner_pick_no), 5
                ),
                survival_probability=round(survival, 5),
                confidence=row.confidence,
                injury_status=injury_status,
                injury_detail=injury_detail,
                depth_chart_order=depth_order,
                replacement_player_name=replacement_outlook.get("player_name"),
                replacement_draft_value_rank=replacement_outlook.get("draft_value_rank"),
                replacement_availability_probability=replacement_outlook.get(
                    "availability_probability"
                ),
                expected_position_demand_before_next_pick=expected_position_demand.get(
                    position, 0.0
                ),
                cost_of_waiting=float(replacement_outlook.get("cost") or 0),
                championship_ceiling=float(components.get("championship_ceiling") or 0),
                championship_win_probability=None,
                championship_equity_delta=None,
                championship_simulations=None,
                reasons=reasons[:3],
                score_components={key: round(value, 5) for key, value in components.items()},
            )
        )
    ordered = sorted(candidates, key=lambda item: (-item.score, item.overall_rank, item.player_id))
    # The programmatic score is an evidence signal, not the final decision-maker.
    # Give the expert model both roster-aware leaders and pure consensus values so
    # it can deliberately override the score without being trapped in a narrow list.
    consensus_ordered = sorted(
        candidates,
        key=lambda item: (item.draft_value_rank, item.overall_rank, item.player_id),
    )
    provisional_pool: list[DraftCandidate] = []
    seen_player_ids: set[str] = set()
    for candidate in [*ordered[:64], *consensus_ordered[:48]]:
        if candidate.player_id in seen_player_ids:
            continue
        provisional_pool.append(candidate)
        seen_player_ids.add(candidate.player_id)
        if len(provisional_pool) >= 96:
            break

    simulation_count: int | None = None
    if owner_slot is not None and team_roster_player_ids is not None and provisional_pool:
        simulation_count = max(int(championship_simulations), 200)
        equities = estimate_candidate_championship_equity(
            rankings=rankings,
            players=players,
            candidates=provisional_pool,
            drafted_player_ids=drafted_player_ids,
            team_roster_player_ids=team_roster_player_ids,
            owner_slot=owner_slot,
            owner_pick_no=owner_pick_no,
            draft_settings=settings,
            playoff_settings=playoff_settings,
            simulations=simulation_count,
        )
        equity_median = median(equities.values()) if equities else 0.0
        revised: list[DraftCandidate] = []
        for candidate in candidates:
            probability = equities.get(candidate.player_id)
            if probability is None:
                revised.append(candidate)
                continue
            delta = probability - equity_median
            equity_component = delta * 35
            components = {
                **candidate.score_components,
                "championship_equity": round(equity_component, 5),
            }
            reasons = list(candidate.reasons)
            if delta >= 0.01:
                reasons.insert(
                    0,
                    f"Modeled title equity is {delta * 100:+.1f} points above candidate median",
                )
            revised.append(
                replace(
                    candidate,
                    score=round(candidate.score + equity_component, 5),
                    championship_win_probability=probability,
                    championship_equity_delta=round(delta, 5),
                    championship_simulations=simulation_count,
                    reasons=reasons[:3],
                    score_components=components,
                )
            )
        candidates = revised
        ordered = sorted(
            candidates, key=lambda item: (-item.score, item.overall_rank, item.player_id)
        )
        consensus_ordered = sorted(
            candidates,
            key=lambda item: (item.draft_value_rank, item.overall_rank, item.player_id),
        )

    decision_pool: list[DraftCandidate] = []
    seen_player_ids = set()
    for candidate in [*ordered[:64], *consensus_ordered[:48]]:
        if candidate.player_id in seen_player_ids:
            continue
        decision_pool.append(candidate)
        seen_player_ids.add(candidate.player_id)
        if len(decision_pool) >= 96:
            break
    return {
        "recommendation": asdict(ordered[0]) if ordered else None,
        "fallbacks": [asdict(item) for item in ordered[1:5]],
        "decision_pool": [asdict(item) for item in decision_pool],
        "candidate_count": len(ordered),
        "engine_version": "draft-2026.5",
        "roster_summary": {
            "counts": roster_counts,
            "targets": plan.targets,
            "starters": plan.starters,
            "flex_slots": plan.flex_slots,
            "rounds": plan.rounds,
            "remaining_picks": max(plan.rounds - len(roster_player_ids), 0),
            "dynamic_replacement_ranks": replacement_ranks,
            "expected_position_demand_before_next_pick": expected_position_demand,
            "championship_model": {
                "version": CHAMPIONSHIP_SIMULATION_VERSION,
                "simulations_per_candidate": simulation_count,
                "objective": "first_place_probability",
            },
        },
        "explanation": "Roster-aware blend of consensus/ADP, dynamic replacement value, opponent demand, cost of waiting, exact lineup requirements, health/depth-chart context, next-turn survival, and simulated first-place probability against peer rosters.",
    }


def _plan_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    availability = float(candidate.get("available_at_pick_probability") or 0)
    if availability >= 0.65:
        availability_band = "likely"
    elif availability >= 0.35:
        availability_band = "coin_flip"
    elif availability >= 0.12:
        availability_band = "long_shot"
    else:
        availability_band = "unlikely"
    return {
        key: candidate.get(key)
        for key in (
            "player_id",
            "player_name",
            "position",
            "team",
            "bye_week",
            "overall_rank",
            "position_rank",
            "market_adp",
            "draft_value_rank",
            "score",
            "roster_fit",
            "available_at_pick_probability",
            "survival_probability",
            "injury_status",
            "injury_detail",
            "depth_chart_order",
            "replacement_player_name",
            "replacement_draft_value_rank",
            "replacement_availability_probability",
            "expected_position_demand_before_next_pick",
            "cost_of_waiting",
            "championship_ceiling",
            "championship_win_probability",
            "championship_equity_delta",
            "championship_simulations",
            "reasons",
        )
    } | {"availability_band": availability_band}


def _next_pick_for_slot(start_pick: int, owner_slot: int, teams: int) -> int:
    for pick_no in range(start_pick, start_pick + teams * 2 + 1):
        if _snake_slot(pick_no, teams) == owner_slot:
            return pick_no
    return start_pick + teams


def _consume_market_pick(
    market_order: list[tuple[str, RankingRow]],
    simulated_drafted: set[str],
    simulated_roster: set[str],
    market_index: int,
) -> int:
    while market_index < len(market_order):
        market_player_id, _ = market_order[market_index]
        market_index += 1
        if market_player_id not in simulated_drafted and market_player_id not in simulated_roster:
            simulated_drafted.add(market_player_id)
            return market_index
    return market_index


def build_candidate_turn_paths(
    *,
    rankings: list[tuple[str, RankingRow]],
    players: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
    drafted_player_ids: set[str],
    roster_player_ids: set[str],
    owner_slot: int,
    owner_pick_no: int,
    following_owner_pick: int,
    draft_settings: dict[str, Any] | None = None,
    scoring_type: str | None = None,
    max_candidates: int = 12,
) -> list[dict[str, Any]]:
    """Project two market turns after each realistic current selection."""
    settings = draft_settings or {}
    teams = max(int(settings.get("teams") or 12), 1)
    plan = roster_plan_from_settings(settings)
    market_order = sorted(
        rankings,
        key=lambda item: (
            item[1].market_adp or _draft_value_rank(item[1]),
            item[1].overall_rank or 999,
            item[0],
        ),
    )
    ranking_by_player = {player_id: row for player_id, row in rankings}
    results: list[dict[str, Any]] = []

    for selected in candidates[:max_candidates]:
        selected_id = str(selected.get("player_id") or "")
        if not selected_id or selected_id in drafted_player_ids:
            continue
        simulated_drafted = set(drafted_player_ids) | {selected_id}
        simulated_roster = set(roster_player_ids) | {selected_id}
        market_index = 0
        current_pick = owner_pick_no + 1
        turn_pick = following_owner_pick
        turns: list[dict[str, Any]] = []

        for _ in range(2):
            while current_pick < turn_pick:
                market_index = _consume_market_pick(
                    market_order, simulated_drafted, simulated_roster, market_index
                )
                current_pick += 1
            next_owner_pick = _next_pick_for_slot(turn_pick + 1, owner_slot, teams)
            recommendation = recommend_draft_pick(
                rankings=rankings,
                drafted_player_ids=simulated_drafted,
                roster_player_ids=simulated_roster,
                players=players,
                owner_pick_no=turn_pick,
                following_owner_pick=next_owner_pick,
                draft_settings=settings,
                scoring_type=scoring_type,
                owner_slot=owner_slot,
            )
            expected = recommendation.get("recommendation")
            if expected is None:
                break
            turns.append(
                {
                    "pick": turn_pick,
                    "player_id": expected.get("player_id"),
                    "player_name": expected.get("player_name"),
                    "position": expected.get("position"),
                    "draft_value_rank": expected.get("draft_value_rank"),
                    "injury_status": expected.get("injury_status"),
                    "depth_chart_order": expected.get("depth_chart_order"),
                }
            )
            expected_id = str(expected["player_id"])
            simulated_roster.add(expected_id)
            simulated_drafted.add(expected_id)
            current_pick = turn_pick + 1
            turn_pick = next_owner_pick

        counts: dict[str, int] = {}
        for player_id in simulated_roster:
            ranking = ranking_by_player.get(player_id)
            position = str(
                players.get(player_id, {}).get("position") or (ranking.position if ranking else "")
            ).upper()
            position = "DEF" if position == "DST" else position
            if position:
                counts[position] = counts.get(position, 0) + 1
        direct_gaps = {
            position: max(required - counts.get(position, 0), 0)
            for position, required in plan.starters.items()
        }
        target_gaps = {
            position: max(required - counts.get(position, 0), 0)
            for position, required in plan.targets.items()
        }
        results.append(
            {
                "selected_player_id": selected_id,
                "selected_player_name": selected.get("player_name"),
                "selected_position": selected.get("position"),
                "next_two_turns": turns,
                "position_counts_after_path": counts,
                "direct_starter_gaps_after_path": direct_gaps,
                "target_gaps_after_path": target_gaps,
                "path_warning": (
                    "Core starter gaps remain after the projected two-turn path."
                    if any(direct_gaps.get(position, 0) for position in ("QB", "RB", "WR", "TE"))
                    else "Projected path fills every core direct starter slot."
                ),
            }
        )
    return results


def build_slot_draft_plan(
    *,
    rankings: list[tuple[str, RankingRow]],
    players: dict[str, dict[str, Any]],
    owner_slot: int,
    draft_settings: dict[str, Any] | None = None,
    scoring_type: str | None = None,
    drafted_player_ids: set[str] | None = None,
    roster_player_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Build a market-base roster and queues for plausible slides from one snake slot."""
    settings = draft_settings or {}
    teams = max(int(settings.get("teams") or 12), 1)
    plan = roster_plan_from_settings(settings)
    actual_drafted = set(drafted_player_ids or set())
    owner_roster = set(roster_player_ids or set())
    simulated_drafted = set(actual_drafted)
    market_order = sorted(
        rankings,
        key=lambda item: (
            item[1].market_adp or _draft_value_rank(item[1]),
            item[1].overall_rank or 999,
            item[0],
        ),
    )
    market_index = 0
    current_pick = len(actual_drafted) + 1
    round_rows: list[dict[str, Any]] = []

    def draft_next_market_player() -> None:
        nonlocal market_index
        while market_index < len(market_order):
            player_id, _ = market_order[market_index]
            market_index += 1
            if player_id not in simulated_drafted and player_id not in owner_roster:
                simulated_drafted.add(player_id)
                return

    first_remaining_round = len(owner_roster) + 1
    for round_no in range(first_remaining_round, plan.rounds + 1):
        owner_pick = (round_no - 1) * teams + (
            owner_slot if round_no % 2 else teams + 1 - owner_slot
        )
        if owner_pick < current_pick:
            continue
        while current_pick < owner_pick:
            draft_next_market_player()
            current_pick += 1

        following_pick = (
            round_no * teams + (owner_slot if (round_no + 1) % 2 else teams + 1 - owner_slot)
            if round_no < plan.rounds
            else owner_pick + teams
        )
        base_result = recommend_draft_pick(
            rankings=rankings,
            drafted_player_ids=simulated_drafted,
            roster_player_ids=owner_roster,
            players=players,
            owner_pick_no=owner_pick,
            following_owner_pick=following_pick,
            draft_settings=settings,
            scoring_type=scoring_type,
            owner_slot=owner_slot,
        )
        expected = base_result.get("recommendation")
        if expected is None:
            break

        locked_gone = {
            player_id
            for player_id, row in rankings
            if _availability_probability(row, owner_pick) < 0.08
        }
        plausible_result = recommend_draft_pick(
            rankings=rankings,
            drafted_player_ids=locked_gone | owner_roster,
            roster_player_ids=owner_roster,
            players=players,
            owner_pick_no=owner_pick,
            following_owner_pick=following_pick,
            draft_settings=settings,
            scoring_type=scoring_type,
            owner_slot=owner_slot,
        )
        priority_candidates = [
            item
            for item in [
                plausible_result.get("recommendation"),
                *(plausible_result.get("fallbacks") or []),
            ]
            if item
        ]
        expected_drafted_before_pick = set(simulated_drafted)
        round_rows.append(
            {
                "round": round_no,
                "pick": owner_pick,
                "following_pick": following_pick if round_no < plan.rounds else None,
                "expected_pick": _plan_candidate(expected),
                "base_fallbacks": [
                    _plan_candidate(item) for item in base_result.get("fallbacks", [])[:3]
                ],
                "priority_queue": [_plan_candidate(item) for item in priority_candidates],
                "slide_watch": [
                    _plan_candidate(item)
                    for item in priority_candidates
                    if item["player_id"] in expected_drafted_before_pick
                ][:3],
            }
        )
        selected_id = str(expected["player_id"])
        owner_roster.add(selected_id)
        simulated_drafted.add(selected_id)
        current_pick = owner_pick + 1

    ranking_by_player = {player_id: row for player_id, row in rankings}
    roster_rows = []
    counts: dict[str, int] = {}
    for item in round_rows:
        candidate = item["expected_pick"]
        position = str(candidate["position"])
        counts[position] = counts.get(position, 0) + 1
        roster_rows.append(
            {
                "round": item["round"],
                "pick": item["pick"],
                **candidate,
            }
        )
    for player_id in roster_player_ids or set():
        row = ranking_by_player.get(player_id)
        player = players.get(player_id, {})
        position = str(player.get("position") or (row.position if row else "")).upper()
        position = "DEF" if position == "DST" else position
        if position:
            counts[position] = counts.get(position, 0) + 1

    return {
        "owner_slot": owner_slot,
        "teams": teams,
        "rounds": plan.rounds,
        "pick_numbers": [row["pick"] for row in round_rows],
        "target_counts": plan.targets,
        "projected_counts": counts,
        "target_complete": all(counts.get(pos, 0) >= count for pos, count in plan.targets.items()),
        "projected_roster": roster_rows,
        "round_plan": round_rows,
        "strategy": (
            "Fill one QB, one TE, one kicker, and one defense; use the remaining roster "
            "capacity on five RBs and seven WRs, with QB/TE depth only for a material value slide."
        ),
        "availability_note": (
            "Probabilities estimate whether a player reaches the pick from half-PPR Sleeper ADP, "
            "tempered by expert-rank dispersion. The base roster assumes market-order selections; "
            "the priority queue keeps plausible slides visible."
        ),
    }


def simulate_all_draft_slots(
    rankings: list[RankingRow], rounds: int = 16, teams: int = 12
) -> list[dict[str, Any]]:
    player_pool = [row for row in rankings if row.overall_rank]
    simulations: list[dict[str, Any]] = []
    for slot in range(1, teams + 1):
        picks: list[dict[str, Any]] = []
        for round_no in range(1, rounds + 1):
            overall_pick = (round_no - 1) * teams + (slot if round_no % 2 else teams + 1 - slot)
            if overall_pick <= len(player_pool):
                player = player_pool[overall_pick - 1]
                picks.append(
                    {
                        "round": round_no,
                        "pick": overall_pick,
                        "player": player.player_name,
                        "position": player.position,
                    }
                )
        simulations.append({"slot": slot, "picks": picks})
    return simulations
