from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations


@dataclass(frozen=True)
class PlayerOption:
    player_id: str
    position: str
    projection: float
    eligible_positions: tuple[str, ...] = ()
    locked: bool = False
    locked_slot: int | None = None
    inactive: bool = False
    kickoff_epoch: int = 0


def eligible(position: str, slot: str) -> bool:
    if slot == "FLEX":
        return position in {"RB", "WR", "TE"}
    return position == slot


def optimize_lineup(players: list[PlayerOption], slots: list[str]) -> list[str]:
    """Exact small-roster optimizer with two FLEX support and inactive avoidance."""
    if len(players) < len(slots):
        raise ValueError("Not enough players to fill every lineup slot")
    best: tuple[float, tuple[str, ...]] | None = None
    active = [player for player in players if not player.inactive]
    for candidate in permutations(active, len(slots)):
        if not all(
            any(
                eligible(position, slot)
                for position in (player.eligible_positions or (player.position,))
            )
            for player, slot in zip(candidate, slots, strict=True)
        ):
            continue
        if any(player.locked and player not in candidate for player in players):
            continue
        if any(
            player.locked_slot is not None and candidate[player.locked_slot] != player
            for player in players
            if player.locked_slot is not None
        ):
            continue
        # Preserve later players in FLEX when projections tie.
        score = sum(player.projection for player in candidate)
        flex_bonus = sum(
            player.kickoff_epoch / 10**12
            for player, slot in zip(candidate, slots, strict=True)
            if slot == "FLEX"
        )
        key = (score + flex_bonus, tuple(player.player_id for player in candidate))
        if best is None or key > best:
            best = key
    if best is None:
        raise ValueError("No legal all-active lineup exists")
    return list(best[1])
