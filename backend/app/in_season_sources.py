from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings, get_settings
from app.database import session_scope
from app.fantasypros import (
    FantasyProsClient,
    FantasyProsQuotaExceeded,
    FantasyProsResponse,
    fantasypros_quota_status,
)
from app.readiness import read_report
from app.repositories import (
    persist_members_and_rosters,
    record_observation,
    update_dataset_health,
    update_source_health,
)
from app.sleeper import SleeperClient, SleeperResponse
from app.source_catalog import build_source_catalog
from app.utils import canonical_json, content_hash

NFLVERSE_URLS = {
    "schedule": "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
    "injuries": "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv",
    "depth_charts": "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.csv",
    "player_stats": "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv",
    "snap_counts": "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{season}.csv",
    "player_ids": "https://github.com/dynastyprocess/data/raw/master/files/db_playerids.csv",
}


@dataclass(frozen=True)
class CsvResponse:
    endpoint: str
    rows: list[dict[str, str]]
    latency_ms: float
    last_modified: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _require_dict(response: SleeperResponse, label: str) -> dict[str, Any]:
    if not isinstance(response.payload, dict) or not response.payload:
        raise ValueError(f"{label} response is empty or malformed")
    return response.payload


def _require_list(response: SleeperResponse, label: str) -> list[dict[str, Any]]:
    if not isinstance(response.payload, list) or not all(
        isinstance(item, dict) for item in response.payload
    ):
        raise ValueError(f"{label} response is malformed")
    return response.payload


def _record_count(payload: dict[str, Any] | list[Any]) -> int:
    if isinstance(payload, list):
        return len(payload)
    for key in ("players", "news", "injuries", "rankings", "projections"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return len(payload)


def latest_snapshot_rows(
    rows: list[dict[str, str]], *, timestamp_field: str
) -> list[dict[str, str]]:
    latest = max((row.get(timestamp_field) or "" for row in rows), default="")
    return [row for row in rows if row.get(timestamp_field) == latest]


async def sync_sleeper_in_season(settings: Settings | None = None) -> dict[str, Any]:
    """Capture the live weekly league state used by every in-season decision."""
    cfg = settings or get_settings()
    async with SleeperClient(cfg) as sleeper:
        state_response = await sleeper.nfl_state()
        state = _require_dict(state_response, "NFL state")
        week = int(state.get("week") or state.get("leg") or 1)
        gathered = await asyncio.gather(
            sleeper.league(cfg.sleeper_league_id),
            sleeper.users(cfg.sleeper_league_id),
            sleeper.rosters(cfg.sleeper_league_id),
            sleeper.matchups(cfg.sleeper_league_id, week),
            sleeper.trending("add", 24),
            sleeper.trending("drop", 24),
            *(
                sleeper.transactions(cfg.sleeper_league_id, transaction_week)
                for transaction_week in range(1, week + 1)
            ),
        )
        league_r, users_r, rosters_r, matchups_r, adds_r, drops_r = gathered[:6]
        transaction_responses = list(gathered[6:])

    league = _require_dict(league_r, "league")
    users = _require_list(users_r, "users")
    rosters = _require_list(rosters_r, "rosters")
    matchups = _require_list(matchups_r, "matchups")
    transactions_by_week = {
        str(transaction_week): _require_list(response, f"week {transaction_week} transactions")
        for transaction_week, response in enumerate(transaction_responses, start=1)
    }
    transaction_history = [
        transaction
        for transaction_week in range(1, week + 1)
        for transaction in transactions_by_week[str(transaction_week)]
    ]
    transactions = transactions_by_week.get(str(week), [])
    trending_adds = _require_list(adds_r, "trending adds")
    trending_drops = _require_list(drops_r, "trending drops")
    responses = (
        state_response,
        league_r,
        users_r,
        rosters_r,
        matchups_r,
        adds_r,
        drops_r,
        *transaction_responses,
    )

    async with session_scope() as session:
        for response in responses:
            await record_observation(
                session,
                provider="Sleeper public API",
                endpoint=response.endpoint,
                entity_type="weekly_league_state",
                entity_id=f"{cfg.sleeper_league_id}:week:{week}",
                payload=response.payload,
            )
        await persist_members_and_rosters(
            session,
            league_id=cfg.sleeper_league_id,
            users=users,
            rosters=rosters,
        )
        await update_source_health(
            session,
            "Sleeper public API",
            success=True,
            latency_ms=max(response.latency_ms for response in responses),
            digest=content_hash([response.payload for response in responses]),
        )
        sleeper_datasets = (
            ("nfl_state", state_response, 1),
            ("league", league_r, 1),
            ("users", users_r, len(rosters)),
            ("rosters", rosters_r, int(league.get("total_rosters") or len(rosters))),
            ("matchups", matchups_r, int(league.get("total_rosters") or len(matchups))),
            ("trending_adds", adds_r, None),
            ("trending_drops", drops_r, None),
        )
        for dataset, response, expected in sleeper_datasets:
            observed = _record_count(response.payload)
            expected_absence = dataset == "matchups" and observed == 0
            await update_dataset_health(
                session,
                "Sleeper public API",
                dataset,
                success=expected is None or observed >= expected,
                expected_absence=expected_absence,
                latency_ms=response.latency_ms,
                digest=content_hash(response.payload),
                expected_records=expected,
                observed_records=observed,
                error=(
                    None
                    if expected is None or observed >= expected
                    else f"Expected at least {expected} records, observed {observed}"
                ),
            )
        await update_dataset_health(
            session,
            "Sleeper public API",
            "transactions",
            success=True,
            latency_ms=max(
                (response.latency_ms for response in transaction_responses),
                default=0,
            ),
            digest=content_hash(transaction_history),
            observed_records=len(transaction_history),
        )

    owner_roster = next(
        (item for item in rosters if int(item.get("roster_id") or 0) == cfg.sleeper_roster_id),
        None,
    )
    owner_matchup = next(
        (item for item in matchups if int(item.get("roster_id") or 0) == cfg.sleeper_roster_id),
        None,
    )
    opponent_matchup = None
    if owner_matchup and owner_matchup.get("matchup_id") is not None:
        opponent_matchup = next(
            (
                item
                for item in matchups
                if item.get("matchup_id") == owner_matchup.get("matchup_id")
                and int(item.get("roster_id") or 0) != cfg.sleeper_roster_id
            ),
            None,
        )
    report = {
        "generated_at": _utc_now(),
        "nfl_state": state,
        "league": league,
        "season": str(state.get("season") or league.get("season") or ""),
        "week": week,
        "league_status": league.get("status"),
        "users": users,
        "rosters": rosters,
        "matchups": matchups,
        "owner_roster": owner_roster,
        "owner_matchup": owner_matchup,
        "opponent_matchup": opponent_matchup,
        "transaction_count": len(transactions),
        "transactions": transactions,
        "transactions_by_week": transactions_by_week,
        "transaction_history": transaction_history,
        "trending_adds": trending_adds[:50],
        "trending_drops": trending_drops[:50],
        "source_hash": content_hash(
            [state, league, users, rosters, matchups, transaction_history]
        ),
    }
    _write_report(cfg.reports_dir / "in-season-context.json", report)
    return report


async def sync_sleeper_season_context(settings: Settings | None = None) -> dict[str, Any]:
    """Capture the complete fantasy schedule and playoff bracket without any write access."""

    cfg = settings or get_settings()
    async with SleeperClient(cfg) as sleeper:
        league_r = await sleeper.league(cfg.sleeper_league_id)
        league = _require_dict(league_r, "league")
        playoff_start = int((league.get("settings") or {}).get("playoff_week_start") or 15)
        playoff_teams = int((league.get("settings") or {}).get("playoff_teams") or 6)
        playoff_rounds = max((playoff_teams - 1).bit_length(), 1)
        championship_week = min(playoff_start + playoff_rounds - 1, 18)
        responses = await asyncio.gather(
            *(
                sleeper.matchups(cfg.sleeper_league_id, week)
                for week in range(1, championship_week + 1)
            ),
            sleeper.winners_bracket(cfg.sleeper_league_id),
            sleeper.losers_bracket(cfg.sleeper_league_id),
        )
    matchup_responses = responses[:championship_week]
    winners_r = responses[-2]
    losers_r = responses[-1]
    matchups_by_week = {
        str(week): _require_list(response, f"week {week} matchups")
        for week, response in enumerate(matchup_responses, start=1)
    }
    winners = _require_list(winners_r, "winners bracket")
    losers = _require_list(losers_r, "losers bracket")
    report = {
        "generated_at": _utc_now(),
        "league_id": cfg.sleeper_league_id,
        "season": str(league.get("season") or ""),
        "playoff_start_week": playoff_start,
        "championship_week": championship_week,
        "matchups_by_week": matchups_by_week,
        "winners_bracket": winners,
        "losers_bracket": losers,
        "source_hash": content_hash([matchups_by_week, winners, losers]),
    }
    async with session_scope() as session:
        await record_observation(
            session,
            provider="Sleeper public API",
            endpoint=f"league/{cfg.sleeper_league_id}/season-context",
            entity_type="fantasy_season_schedule",
            entity_id=str(league.get("season") or ""),
            payload=report,
        )
        await update_dataset_health(
            session,
            "Sleeper public API",
            "season_schedule",
            success=sum(bool(rows) for rows in matchups_by_week.values()) >= playoff_start - 1,
            latency_ms=max(
                [response.latency_ms for response in matchup_responses]
                + [winners_r.latency_ms, losers_r.latency_ms]
            ),
            digest=report["source_hash"],
            expected_records=playoff_start - 1,
            observed_records=sum(bool(rows) for rows in matchups_by_week.values()),
        )
    _write_report(cfg.reports_dir / "sleeper-season-context.json", report)
    return report


async def _fetch_csv(
    client: httpx.AsyncClient, url: str, *, optional: bool = False
) -> CsvResponse | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com"}:
        raise ValueError("CSV source must be an allowlisted HTTPS GitHub endpoint")
    started = datetime.now(timezone.utc)
    response = await client.get(url)
    latency_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    if optional and response.status_code == 404:
        return None
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))
    if not rows:
        raise ValueError(f"CSV source is empty: {url}")
    return CsvResponse(
        endpoint=url,
        rows=rows,
        latency_ms=latency_ms,
        last_modified=response.headers.get("last-modified"),
    )


async def sync_nflverse_context(
    settings: Settings | None = None,
    *,
    season: int | None = None,
    week: int | None = None,
) -> dict[str, Any]:
    """Capture open schedule, usage, injury, depth-chart, and ID mapping evidence."""
    cfg = settings or get_settings()
    if season is None or week is None:
        async with SleeperClient(cfg) as sleeper:
            state = _require_dict(await sleeper.nfl_state(), "NFL state")
        season = season or int(state.get("season") or 0)
        week = week or int(state.get("week") or state.get("leg") or 1)
    if season < 2000:
        raise ValueError("A valid NFL season is required")

    urls = {key: value.format(season=season) for key, value in NFLVERSE_URLS.items()}
    prior_injuries_url = NFLVERSE_URLS["injuries"].format(season=season - 1)
    prior_stats_url = NFLVERSE_URLS["player_stats"].format(season=season - 1)
    prior_snaps_url = NFLVERSE_URLS["snap_counts"].format(season=season - 1)
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        (
            schedule_r,
            injuries_r,
            depth_r,
            player_ids_r,
            stats_r,
            snaps_r,
            prior_injuries_r,
            prior_stats_r,
            prior_snaps_r,
        ) = await asyncio.gather(
            _fetch_csv(client, urls["schedule"]),
            _fetch_csv(client, urls["injuries"]),
            _fetch_csv(client, urls["depth_charts"]),
            _fetch_csv(client, urls["player_ids"]),
            _fetch_csv(client, urls["player_stats"], optional=True),
            _fetch_csv(client, urls["snap_counts"], optional=True),
            _fetch_csv(client, prior_injuries_url, optional=True),
            _fetch_csv(client, prior_stats_url, optional=True),
            _fetch_csv(client, prior_snaps_url, optional=True),
        )
    assert schedule_r and injuries_r and depth_r and player_ids_r

    schedule_rows = [row for row in schedule_r.rows if row.get("season") == str(season)]
    historical_schedule_rows = [
        row for row in schedule_r.rows if row.get("season") in {str(season - 1), str(season)}
    ]
    injury_rows = [
        row
        for row in injuries_r.rows
        if row.get("season") == str(season) and row.get("week") == str(week)
    ]
    depth_rows = latest_snapshot_rows(depth_r.rows, timestamp_field="dt")
    id_rows = [
        row
        for row in player_ids_r.rows
        if row.get("sleeper_id") not in {None, "", "NA"}
        and row.get("position") in {"QB", "RB", "WR", "TE", "K"}
    ]
    datasets: list[tuple[str, str, CsvResponse, list[dict[str, str]], float]] = [
        ("nflverse", "schedule", schedule_r, schedule_rows, 0.95),
        ("nflverse", "injuries", injuries_r, injury_rows, 0.85),
        ("nflverse", "depth_charts", depth_r, depth_rows, 0.85),
        ("DynastyProcess", "player_id_crosswalk", player_ids_r, id_rows, 0.95),
    ]
    if stats_r:
        datasets.append(("nflverse", "player_stats", stats_r, stats_r.rows, 0.95))
    if snaps_r:
        datasets.append(("nflverse", "snap_counts", snaps_r, snaps_r.rows, 0.9))
    if prior_injuries_r:
        datasets.append(
            ("nflverse", "injuries_prior_season", prior_injuries_r, prior_injuries_r.rows, 0.9)
        )
    if prior_stats_r:
        datasets.append(
            ("nflverse", "player_stats_prior_season", prior_stats_r, prior_stats_r.rows, 0.9)
        )
    if prior_snaps_r:
        datasets.append(
            ("nflverse", "snap_counts_prior_season", prior_snaps_r, prior_snaps_r.rows, 0.85)
        )

    async with session_scope() as session:
        for provider, entity_type, response, rows, confidence in datasets:
            await record_observation(
                session,
                provider=provider,
                endpoint=response.endpoint,
                entity_type=entity_type,
                entity_id=str(season),
                payload=rows,
                normalized={
                    "season": season,
                    "row_count": len(rows),
                    "retrieved_at": _utc_now(),
                    "last_modified": response.last_modified,
                },
                confidence=confidence,
            )
            await update_dataset_health(
                session,
                provider,
                entity_type,
                success=True,
                latency_ms=response.latency_ms,
                digest=content_hash(rows),
                observed_records=len(rows),
            )
        nflverse_responses = [
            response for response in (schedule_r, injuries_r, depth_r, stats_r, snaps_r) if response
        ]
        await update_source_health(
            session,
            "nflverse",
            success=True,
            latency_ms=max(response.latency_ms for response in nflverse_responses),
            digest=content_hash(
                [rows for provider, _, _, rows, _ in datasets if provider == "nflverse"]
            ),
        )
        await update_source_health(
            session,
            "DynastyProcess player IDs",
            success=True,
            latency_ms=player_ids_r.latency_ms,
            digest=content_hash(id_rows),
        )

    report = {
        "generated_at": _utc_now(),
        "season": season,
        "week": week,
        "datasets": {
            entity_type: {
                "provider": provider,
                "row_count": len(rows),
                "source": response.endpoint,
                "last_modified": response.last_modified,
            }
            for provider, entity_type, response, rows, _ in datasets
        },
        "week_schedule": [row for row in schedule_rows if row.get("week") == str(week)],
        "week_injuries": injury_rows,
        "offensive_depth_chart": [
            {
                key: row.get(key)
                for key in (
                    "dt",
                    "team",
                    "gsis_id",
                    "espn_id",
                    "player_name",
                    "pos_abb",
                    "pos_rank",
                )
            }
            for row in depth_rows
            if row.get("pos_abb") in {"QB", "RB", "FB", "WR", "TE", "K"}
        ],
        "player_ids": {
            str(row["sleeper_id"]): {
                key: row.get(key)
                for key in (
                    "name",
                    "team",
                    "position",
                    "gsis_id",
                    "espn_id",
                    "fantasypros_id",
                    "pfr_id",
                    "fantasy_data_id",
                    "sportradar_id",
                    "rotowire_id",
                    "yahoo_id",
                )
                if row.get(key) not in {None, "", "NA"}
            }
            for row in id_rows
        },
        "week_player_stats": [
            row for row in (stats_r.rows if stats_r else []) if row.get("week") == str(week)
        ],
        "week_snap_counts": [
            row for row in (snaps_r.rows if snaps_r else []) if row.get("week") == str(week)
        ],
        "season_schedule": schedule_rows,
        "historical_schedule": historical_schedule_rows,
        "historical_injuries": [
            *(prior_injuries_r.rows if prior_injuries_r else []),
            *[
                row
                for row in injuries_r.rows
                if row.get("season") == str(season)
                and str(row.get("week") or "").isdigit()
                and int(str(row["week"])) <= week
            ],
        ],
        "season_player_stats": [
            row
            for row in (stats_r.rows if stats_r else [])
            if row.get("season") == str(season)
            and str(row.get("week") or "").isdigit()
            and int(str(row["week"])) <= week
        ],
        "season_snap_counts": [
            row
            for row in (snaps_r.rows if snaps_r else [])
            if row.get("season") == str(season)
            and str(row.get("week") or "").isdigit()
            and int(str(row["week"])) <= week
        ],
        "historical_player_stats": [
            *(prior_stats_r.rows if prior_stats_r else []),
            *[
                row
                for row in (stats_r.rows if stats_r else [])
                if str(row.get("week") or "").isdigit() and int(str(row["week"])) <= week
            ],
        ],
        "historical_snap_counts": [
            *(prior_snaps_r.rows if prior_snaps_r else []),
            *[
                row
                for row in (snaps_r.rows if snaps_r else [])
                if str(row.get("week") or "").isdigit() and int(str(row["week"])) <= week
            ],
        ],
    }
    _write_report(cfg.reports_dir / "nflverse-context.json", report)
    return report


async def sync_fantasypros_context(
    settings: Settings | None = None,
    *,
    season: int | None = None,
    week: int | None = None,
    force: bool = False,
    allow_reserve: bool = False,
) -> dict[str, Any]:
    """Capture targeted projections without exhausting the limited personal quota."""
    cfg = settings or get_settings()
    if cfg.fantasypros_api_key is None:
        return {"configured": False, "reason": "FANTASYPROS_API_KEY is not configured"}
    if season is None or week is None:
        async with SleeperClient(cfg) as sleeper:
            state = _require_dict(await sleeper.nfl_state(), "NFL state")
        season = season or int(state.get("season") or 0)
        week = week or int(state.get("week") or state.get("leg") or 1)
    previous = read_report(cfg.reports_dir / "fantasypros-context.json", {})
    if not force and (
        str(previous.get("season") or "") == str(season)
        and int(previous.get("week") or 0) == int(week)
    ):
        try:
            previous_at = datetime.fromisoformat(str(previous.get("generated_at") or ""))
            if previous_at.tzinfo is None:
                previous_at = previous_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - previous_at).total_seconds()
        except ValueError:
            age_seconds = 10**9
        if -10 <= age_seconds < 55 * 60:
            return {
                **previous,
                "status": "cached",
                "requests_this_sync": 0,
                "quota": fantasypros_quota_status(cfg),
            }
    sleeper = read_report(cfg.reports_dir / "in-season-context.json", {})
    nflverse = read_report(cfg.reports_dir / "nflverse-context.json", {})
    targets = _fantasypros_targets(sleeper, nflverse)
    target_ids = [row["fantasypros_id"] for row in targets]
    position_order = ("QB", "RB", "WR", "TE", "K", "DST")
    position_cursor = int(previous.get("position_cursor") or 0) % len(position_order)
    previous_offsets = previous.get("target_position_offsets") or {}
    quota_before = fantasypros_quota_status(cfg)
    request_allowance = min(
        cfg.fantasypros_requests_per_sync,
        int(quota_before.get("total_remaining" if allow_reserve else "standard_remaining") or 0),
    )
    request_specs: list[dict[str, Any]] = []
    next_offsets = {str(key): int(value or 0) for key, value in previous_offsets.items()}
    for position in position_order:
        owner_ids = [
            row["fantasypros_id"]
            for row in targets
            if row["position"] == position and row["scope"] == "our_team"
        ]
        opponent_ids = [
            row["fantasypros_id"]
            for row in targets
            if row["position"] == position and row["scope"] == "opponent"
        ]
        if not owner_ids and not opponent_ids:
            continue
        selected = owner_ids[:10]
        available = 10 - len(selected)
        offset = int(previous_offsets.get(position) or 0) % max(len(opponent_ids), 1)
        rotated_opponent_ids = opponent_ids[offset:] + opponent_ids[:offset]
        selected.extend(rotated_opponent_ids[:available])
        next_offset = (
            (offset + min(available, len(opponent_ids))) % len(opponent_ids) if opponent_ids else 0
        )
        request_specs.append(
            {
                "player_ids": selected,
                "position": position,
                "scope": "our_team_and_opponent",
                "next_target_offset": next_offset,
            }
        )
    request_specs = request_specs[:request_allowance]
    for spec in request_specs:
        next_offsets[str(spec["position"])] = int(spec.pop("next_target_offset"))
    position_calls = 0
    while len(request_specs) < request_allowance:
        position = position_order[(position_cursor + position_calls) % len(position_order)]
        request_specs.append({"position": position, "scope": f"top_{position.lower()}"})
        position_calls += 1

    responses: list[FantasyProsResponse] = []
    scopes: list[dict[str, Any]] = []
    async with FantasyProsClient(cfg) as fantasypros:
        for spec in request_specs:
            try:
                if spec.get("player_ids"):
                    response = await fantasypros.projections(
                        season=season,
                        week=week,
                        player_ids=list(spec["player_ids"]),
                        position=str(spec["position"]),
                        allow_reserve=allow_reserve,
                    )
                else:
                    response = await fantasypros.projections(
                        season=season,
                        week=week,
                        position=str(spec["position"]),
                        allow_reserve=allow_reserve,
                    )
            except FantasyProsQuotaExceeded:
                break
            responses.append(response)
            payload = response.payload if isinstance(response.payload, dict) else {}
            scopes.append(
                {
                    "scope": spec["scope"],
                    "position": spec.get("position"),
                    "requested_players": len(spec.get("player_ids") or []),
                    "returned_players": len(payload.get("players") or []),
                    "provider_count": payload.get("count"),
                    "provider_limit": payload.get("limit"),
                    "public_api_limited": payload.get("public_api_limited"),
                }
            )

    observed_at = _utc_now()
    previous_players = []
    if str(previous.get("season") or "") == str(season) and int(previous.get("week") or 0) == int(
        week
    ):
        previous_players = (
            ((previous.get("datasets") or {}).get("projections") or {}).get("payload") or {}
        ).get("players") or []
    merged = _merge_fantasypros_projection_players(previous_players, responses, observed_at)
    quota_after = fantasypros_quota_status(cfg)
    returned_target_ids = {
        str(row.get("fpid") or "")
        for row in merged
        if str(row.get("fpid") or "") in set(target_ids)
    }
    if responses:
        await _persist_fantasypros(
            cfg,
            responses,
            season=season,
            week=week,
            observed_players=len(merged),
            expected_players=max(len(target_ids), 1),
        )
    projection_payload = {
        "season": season,
        "week": week,
        "players": merged,
        "public_api_limited": any(scope.get("public_api_limited") for scope in scopes),
    }
    report = {
        "generated_at": observed_at,
        "configured": True,
        "status": "active" if responses else "quota_deferred",
        "mode": "limited_quota_targeted",
        "season": season,
        "week": week,
        "requests_this_sync": len(responses),
        "request_scopes": scopes,
        "quota": quota_after,
        "target_player_count": len(target_ids),
        "target_player_coverage": round(len(returned_target_ids) / len(target_ids), 4)
        if target_ids
        else 0.0,
        "position_cursor": (position_cursor + position_calls) % len(position_order),
        "target_position_offsets": next_offsets,
        "datasets": {
            "projections": {
                "endpoint": f"nfl/{season}/projections",
                "record_count": len(merged),
                "content_hash": content_hash(projection_payload),
                "payload": projection_payload,
            }
        },
        "limitations": [
            "The free API tier returns at most 10 players per request",
            "Frequent requests prioritize our roster and current opponent",
            "One rotating position sample uses otherwise available request capacity",
            "Sleeper and nflverse remain the injury/news sources to preserve this quota",
        ],
    }
    _write_report(cfg.reports_dir / "fantasypros-context.json", report)
    return report


def _fantasypros_priority_ids(sleeper: dict[str, Any], nflverse: dict[str, Any]) -> list[str]:
    return [row["fantasypros_id"] for row in _fantasypros_targets(sleeper, nflverse)]


def _fantasypros_targets(sleeper: dict[str, Any], nflverse: dict[str, Any]) -> list[dict[str, str]]:
    ordered_sleeper_ids = [
        *(
            ("our_team", str(value))
            for value in (sleeper.get("owner_roster") or {}).get("players") or []
        ),
        *(
            ("opponent", str(value))
            for value in (sleeper.get("opponent_matchup") or {}).get("players") or []
        ),
    ]
    identities = nflverse.get("player_ids") or {}
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for scope, sleeper_id in ordered_sleeper_ids:
        identity = identities.get(sleeper_id) or {}
        fantasypros_id = str(identity.get("fantasypros_id") or "")
        position = str(identity.get("position") or "").upper()
        if position in {"DEF", "D/ST"}:
            position = "DST"
        if fantasypros_id and position and fantasypros_id not in seen:
            result.append(
                {
                    "fantasypros_id": fantasypros_id,
                    "position": position,
                    "scope": scope,
                }
            )
            seen.add(fantasypros_id)
    return result


def _merge_fantasypros_projection_players(
    previous_players: list[dict[str, Any]],
    responses: list[FantasyProsResponse],
    observed_at: str,
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in previous_players:
        if isinstance(row, dict) and row.get("fpid") not in {None, ""}:
            by_id[str(row["fpid"])] = row
    for response in responses:
        payload = response.payload if isinstance(response.payload, dict) else {}
        for row in payload.get("players") or []:
            if not isinstance(row, dict) or row.get("fpid") in {None, ""}:
                continue
            by_id[str(row["fpid"])] = {
                **row,
                "_observed_at": observed_at,
                "_request_scope": response.request_params,
            }
    return sorted(
        by_id.values(),
        key=lambda row: (str(row.get("position_id") or ""), str(row["fpid"])),
    )


async def _persist_fantasypros(
    settings: Settings,
    responses: list[FantasyProsResponse],
    *,
    season: int,
    week: int,
    observed_players: int,
    expected_players: int,
) -> None:
    async with session_scope() as session:
        for response in responses:
            await record_observation(
                session,
                provider="FantasyPros API",
                endpoint=response.endpoint,
                entity_type="fantasy_decision_context",
                entity_id=f"{season}:week:{week}",
                payload=response.payload,
                normalized={
                    "season": season,
                    "week": week,
                    "record_count": _record_count(response.payload),
                    "request_params": response.request_params,
                    "retrieved_at": _utc_now(),
                },
                confidence=0.8,
            )
        await update_dataset_health(
            session,
            "FantasyPros API",
            "projections",
            success=True,
            latency_ms=max(response.latency_ms for response in responses),
            digest=content_hash([response.payload for response in responses]),
            expected_records=expected_players,
            observed_records=observed_players,
        )
        await update_source_health(
            session,
            "FantasyPros API",
            success=True,
            latency_ms=max(response.latency_ms for response in responses),
            digest=content_hash([response.payload for response in responses]),
        )


def write_source_catalog(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    catalog = {"generated_at": _utc_now(), **build_source_catalog(cfg)}
    _write_report(cfg.reports_dir / "in-season-sources.json", catalog)
    return catalog
