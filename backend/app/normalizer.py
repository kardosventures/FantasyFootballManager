from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

EXPECTED_ROSTER_POSITIONS = [
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
]

EXPECTED_SCORING = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_2pt": 2.0,
    "pass_int": -1.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rush_2pt": 2.0,
    "rec": 0.5,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "rec_2pt": 2.0,
    "fgm_0_19": 3.0,
    "fgm_20_29": 3.0,
    "fgm_30_39": 3.0,
    "fgm_40_49": 4.0,
    "fgm_50_59": 5.0,
    "fgm_60p": 6.0,
    "xpm": 1.0,
    "fgmiss": -1.0,
    "xpmiss": -1.0,
    "def_td": 6.0,
    "pts_allow_0": 10.0,
    "pts_allow_1_6": 7.0,
    "pts_allow_7_13": 4.0,
    "pts_allow_14_20": 1.0,
    "pts_allow_21_27": 0.0,
    "pts_allow_28_34": -1.0,
    "pts_allow_35p": -4.0,
    "sack": 1.0,
    "int": 2.0,
    "fum_rec": 2.0,
    "safe": 2.0,
    "ff": 1.0,
    "blk_kick": 2.0,
    "st_td": 6.0,
    "st_ff": 1.0,
    "st_fum_rec": 1.0,
    "def_st_td": 6.0,
    "def_st_ff": 1.0,
    "def_st_fum_rec": 1.0,
    "fum_lost": -2.0,
    "fum_rec_td": 6.0,
}


@dataclass(frozen=True)
class Discrepancy:
    field: str
    expected: Any
    actual: Any
    severity: str
    message: str


def _draft_time(milliseconds: int | None) -> str | None:
    if not milliseconds:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def normalize_league(
    league: dict[str, Any],
    drafts: list[dict[str, Any]],
    *,
    waiver_semantics_confirmed: bool = False,
) -> dict[str, Any]:
    current_draft = drafts[0] if drafts else {}
    settings = league.get("settings") or {}
    draft_settings = current_draft.get("settings") or {}
    raw_draft_rounds = settings.get("draft_rounds")
    effective_draft_rounds = draft_settings.get("rounds") or raw_draft_rounds

    return {
        "league_id": str(league.get("league_id", "")),
        "name": league.get("name"),
        "season": str(league.get("season", "")),
        "status": league.get("status"),
        "total_rosters": league.get("total_rosters"),
        "roster_positions": league.get("roster_positions") or [],
        "scoring_settings": league.get("scoring_settings") or {},
        "playoff_settings": {
            key: settings.get(key)
            for key in ("playoff_teams", "playoff_week_start", "playoff_type", "playoff_seed_type")
        },
        "waiver_raw": {
            key: settings.get(key)
            for key in (
                "waiver_type",
                "waiver_day_of_week",
                "waiver_clear_days",
                "daily_waivers",
                "daily_waivers_days",
                "daily_waivers_hour",
                "waiver_budget",
            )
        },
        "waiver_semantics_confirmed": waiver_semantics_confirmed,
        "reserve_slots": settings.get("reserve_slots"),
        "reserve_eligibility_raw": {
            key: value for key, value in settings.items() if key.startswith("reserve_allow_")
        },
        "max_subs": settings.get("max_subs"),
        "positional_limits_raw": {
            key: value for key, value in settings.items() if key.startswith("position_limit_")
        },
        "league_draft_rounds_raw": raw_draft_rounds,
        "effective_draft_rounds": effective_draft_rounds,
        "draft": {
            "draft_id": current_draft.get("draft_id") or league.get("draft_id"),
            "status": current_draft.get("status"),
            "type": current_draft.get("type"),
            "start_time": _draft_time(current_draft.get("start_time")),
            "rounds": draft_settings.get("rounds"),
            "pick_timer": draft_settings.get("pick_timer"),
            "reversal_round_raw": draft_settings.get("reversal_round"),
            "draft_order": current_draft.get("draft_order"),
            "slot_to_roster_id": current_draft.get("slot_to_roster_id"),
        },
    }


def compare_expected(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    discrepancies: list[Discrepancy] = []

    def add(field: str, expected: Any, actual: Any, severity: str, message: str) -> None:
        discrepancies.append(Discrepancy(field, expected, actual, severity, message))

    if normalized["season"] != "2026":
        add("season", "2026", normalized["season"], "critical", "Unexpected league season")
    if normalized["total_rosters"] != 12:
        add("total_rosters", 12, normalized["total_rosters"], "critical", "Expected 12 teams")
    if normalized["roster_positions"] != EXPECTED_ROSTER_POSITIONS:
        add(
            "roster_positions",
            EXPECTED_ROSTER_POSITIONS,
            normalized["roster_positions"],
            "critical",
            "Roster positions differ from the expected league format",
        )
    for key, expected in EXPECTED_SCORING.items():
        actual = normalized["scoring_settings"].get(key)
        if actual != expected:
            add(f"scoring_settings.{key}", expected, actual, "critical", "Scoring discrepancy")
    if normalized["league_draft_rounds_raw"] != normalized["effective_draft_rounds"]:
        add(
            "draft_rounds",
            normalized["effective_draft_rounds"],
            normalized["league_draft_rounds_raw"],
            "warning",
            "League object differs from the operational draft object; draft object is effective",
        )
    if normalized["max_subs"] == 0:
        add("max_subs", ">0", 0, "critical", "Sleeper AutoSubs are disabled")
    return [asdict(item) for item in discrepancies]
