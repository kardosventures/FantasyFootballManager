from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SchedulerProfile:
    name: str
    league_seconds: int
    picks_seconds: int | None
    transactions_seconds: int
    matchups_seconds: int


PROFILES = {
    "pre_draft": SchedulerProfile("pre_draft", 900, 60, 900, 3600),
    "drafting": SchedulerProfile("drafting", 60, 2, 300, 3600),
    "normal": SchedulerProfile("normal", 900, None, 300, 300),
    "waiver_window": SchedulerProfile("waiver_window", 300, None, 60, 300),
    "game_day": SchedulerProfile("game_day", 300, None, 60, 30),
    "kickoff": SchedulerProfile("kickoff", 60, None, 30, 30),
    "playoffs": SchedulerProfile("playoffs", 300, None, 120, 60),
    "degraded": SchedulerProfile("degraded", 60, None, 60, 60),
}


def select_profile(
    *,
    league_status: str,
    source_degraded: bool = False,
    seconds_to_next_kickoff: float | None = None,
    waiver_window: bool = False,
    current_week: int | None = None,
    playoff_start_week: int | None = None,
) -> SchedulerProfile:
    if source_degraded:
        return PROFILES["degraded"]
    if league_status == "drafting":
        return PROFILES["drafting"]
    if league_status in {"pre_draft", "pre-draft"}:
        return PROFILES["pre_draft"]
    if league_status in {"complete", "post_season"}:
        return PROFILES["degraded"]
    if league_status == "in_season":
        if seconds_to_next_kickoff is not None:
            if 0 <= seconds_to_next_kickoff <= 2 * 60 * 60:
                return PROFILES["kickoff"]
            if 0 <= seconds_to_next_kickoff <= 24 * 60 * 60:
                return PROFILES["game_day"]
        if waiver_window:
            return PROFILES["waiver_window"]
        if current_week and playoff_start_week and current_week >= playoff_start_week:
            return PROFILES["playoffs"]
    return PROFILES["normal"]
