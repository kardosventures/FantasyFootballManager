from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.fantasypros import (
    FantasyProsClient,
    FantasyProsQuotaExceeded,
    fantasypros_quota_status,
)
from app.in_season_sources import NFLVERSE_URLS, _fetch_csv, _require_dict
from app.scoring import NFLVERSE_PLAYER_STAT_MAP
from app.sleeper import SleeperClient
from app.utils import canonical_json, content_hash

NFLVERSE_HISTORY_FILE = "nflverse-history.json.gz"
SLEEPER_HISTORY_FILE = "sleeper-history.json.gz"
FANTASYPROS_ARCHIVE_PROBE_FILE = "fantasypros-archive-probe.json"
HISTORICAL_BACKFILL_VERSION = "historical-backfill-2026.1"
HISTORY_FIELDS = (
    "historical_schedule",
    "historical_injuries",
    "historical_player_stats",
    "historical_snap_counts",
)
PLAYER_STAT_FIELDS = {
    "player_id",
    "player_display_name",
    "player_name",
    "position",
    "season",
    "week",
    "season_type",
    "game_id",
    "team",
    "opponent_team",
    "carries",
    "targets",
    "receptions",
    "receiving_air_yards",
    "target_share",
    "fantasy_points",
    "fantasy_points_ppr",
    *NFLVERSE_PLAYER_STAT_MAP.values(),
    "def_sacks",
    "def_interceptions",
    "def_fumbles_forced",
    "def_fumbles",
    "def_safeties",
    "def_fg_blocks",
    "def_pat_blocks",
    "def_punt_blocks",
    "def_tds",
    "special_teams_tds",
}
INJURY_FIELDS = {
    "season",
    "week",
    "season_type",
    "gsis_id",
    "full_name",
    "position",
    "team",
    "report_status",
    "practice_status",
    "report_primary_injury",
}
SNAP_FIELDS = {
    "season",
    "week",
    "game_type",
    "pfr_player_id",
    "player",
    "position",
    "team",
    "offense_snaps",
    "offense_pct",
}
SCHEDULE_FIELDS = {
    "season",
    "week",
    "game_type",
    "game_id",
    "gameday",
    "gametime",
    "home_team",
    "away_team",
    "home_score",
    "away_score",
    "spread_line",
    "total_line",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_gzip_json(path: Path) -> dict[str, Any]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            payload = json.load(source)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_gzip_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}.gz")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as destination:
        json.dump(payload, destination, sort_keys=True, separators=(",", ":"))
        destination.write("\n")
    temporary.replace(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    temporary.replace(path)


def _compact_rows(
    rows: list[dict[str, Any]], fields: set[str], *, regular_season_only: bool = False
) -> list[dict[str, Any]]:
    compact = []
    for row in rows:
        season_type = str(row.get("season_type") or row.get("game_type") or "REG").upper()
        if regular_season_only and season_type != "REG":
            continue
        compact.append(
            {
                key: row[key]
                for key in fields
                if key in row and row[key] is not None and row[key] != ""
            }
        )
    return compact


def _history_content_hash(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for field in HISTORY_FIELDS:
        digest.update(field.encode("utf-8"))
        for row in payload.get(field) or []:
            digest.update(canonical_json(row).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def read_nflverse_history(reports_dir: Path) -> dict[str, Any]:
    return _read_gzip_json(Path(reports_dir) / NFLVERSE_HISTORY_FILE)


def merge_nflverse_history(current: dict[str, Any], archive: dict[str, Any]) -> dict[str, Any]:
    """Add completed historical seasons while preferring live rows for overlapping seasons."""

    if not archive:
        return current
    merged = dict(current)
    for field in HISTORY_FIELDS:
        current_rows = list(current.get(field) or [])
        current_seasons = {
            str(row.get("season") or "") for row in current_rows if isinstance(row, dict)
        }
        archived_rows = [
            row
            for row in archive.get(field) or []
            if isinstance(row, dict) and str(row.get("season") or "") not in current_seasons
        ]
        merged[field] = [*archived_rows, *current_rows]
    merged["historical_archive"] = {
        "version": archive.get("version"),
        "generated_at": archive.get("generated_at"),
        "seasons": archive.get("seasons") or [],
        "content_hash": archive.get("content_hash"),
    }
    return merged


def _replace_season(
    rows: list[dict[str, Any]], season: int, fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        row for row in rows if str(row.get("season") or "") != str(season)
    ] + fresh


async def sync_nflverse_history(
    settings: Settings | None = None,
    *,
    current_season: int | None = None,
    start_season: int | None = None,
    end_season: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Backfill completed nflverse seasons into one compressed, resumable archive."""

    cfg = settings or get_settings()
    if current_season is None:
        async with SleeperClient(cfg) as sleeper:
            state = _require_dict(await sleeper.nfl_state(), "NFL state")
        current_season = int(state.get("season") or 0)
    if current_season < 2000:
        raise ValueError("A valid current NFL season is required")
    start = int(start_season or cfg.nflverse_history_start_season)
    end = min(int(end_season or current_season - 1), current_season - 1)
    if start > end:
        raise ValueError("Historical start season must not exceed the last completed season")

    destination = Path(cfg.reports_dir) / NFLVERSE_HISTORY_FILE
    archive = read_nflverse_history(cfg.reports_dir)
    if archive.get("version") != HISTORICAL_BACKFILL_VERSION:
        archive = {}
    existing_seasons = {int(value) for value in archive.get("seasons") or []}
    schedule_rows = list(archive.get("historical_schedule") or [])
    injury_rows = list(archive.get("historical_injuries") or [])
    stat_rows = list(archive.get("historical_player_stats") or [])
    snap_rows = list(archive.get("historical_snap_counts") or [])
    datasets = dict(archive.get("datasets") or {})
    requested = list(range(start, end + 1))
    fetched: list[int] = []

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        schedule_response = await _fetch_csv(client, NFLVERSE_URLS["schedule"])
        assert schedule_response is not None
        schedule_rows = _compact_rows(
            [
                row
                for row in schedule_response.rows
                if str(row.get("season") or "").isdigit()
                and start <= int(str(row["season"])) <= end
            ],
            SCHEDULE_FIELDS,
            regular_season_only=True,
        )
        for season in requested:
            if season in existing_seasons and not force:
                continue
            stats_response, injuries_response, snaps_response = await asyncio.gather(
                _fetch_csv(
                    client, NFLVERSE_URLS["player_stats"].format(season=season), optional=True
                ),
                _fetch_csv(
                    client, NFLVERSE_URLS["injuries"].format(season=season), optional=True
                ),
                _fetch_csv(
                    client, NFLVERSE_URLS["snap_counts"].format(season=season), optional=True
                ),
            )
            fresh_stats = _compact_rows(
                list(stats_response.rows if stats_response else []),
                PLAYER_STAT_FIELDS,
                regular_season_only=True,
            )
            fresh_injuries = _compact_rows(
                list(injuries_response.rows if injuries_response else []),
                INJURY_FIELDS,
                regular_season_only=True,
            )
            fresh_snaps = _compact_rows(
                list(snaps_response.rows if snaps_response else []),
                SNAP_FIELDS,
                regular_season_only=True,
            )
            stat_rows = _replace_season(stat_rows, season, fresh_stats)
            injury_rows = _replace_season(injury_rows, season, fresh_injuries)
            snap_rows = _replace_season(snap_rows, season, fresh_snaps)
            datasets[str(season)] = {
                "player_stats": {
                    "source": stats_response.endpoint if stats_response else None,
                    "last_modified": stats_response.last_modified if stats_response else None,
                    "row_count": len(fresh_stats),
                },
                "injuries": {
                    "source": injuries_response.endpoint if injuries_response else None,
                    "last_modified": injuries_response.last_modified if injuries_response else None,
                    "row_count": len(fresh_injuries),
                },
                "snap_counts": {
                    "source": snaps_response.endpoint if snaps_response else None,
                    "last_modified": snaps_response.last_modified if snaps_response else None,
                    "row_count": len(fresh_snaps),
                },
            }
            existing_seasons.add(season)
            fetched.append(season)
            partial = {
                "version": HISTORICAL_BACKFILL_VERSION,
                "generated_at": _utc_now(),
                "seasons": sorted(existing_seasons),
                "datasets": datasets,
                "historical_schedule": schedule_rows,
                "historical_injuries": injury_rows,
                "historical_player_stats": stat_rows,
                "historical_snap_counts": snap_rows,
            }
            partial["content_hash"] = _history_content_hash(partial)
            _write_gzip_json(destination, partial)

        final_payload = {
            "version": HISTORICAL_BACKFILL_VERSION,
            "generated_at": _utc_now(),
            "seasons": sorted(existing_seasons),
            "datasets": datasets,
            "historical_schedule": schedule_rows,
            "historical_injuries": injury_rows,
            "historical_player_stats": stat_rows,
            "historical_snap_counts": snap_rows,
        }
        final_payload["content_hash"] = _history_content_hash(final_payload)
        _write_gzip_json(destination, final_payload)

    result = read_nflverse_history(cfg.reports_dir)
    return {
        "status": "complete",
        "version": result.get("version"),
        "generated_at": result.get("generated_at"),
        "requested_seasons": requested,
        "available_seasons": result.get("seasons") or [],
        "fetched_seasons": fetched,
        "cached_seasons": sorted(set(requested) - set(fetched)),
        "row_counts": {
            field: len(result.get(field) or []) for field in HISTORY_FIELDS
        },
        "compressed_bytes": destination.stat().st_size if destination.exists() else 0,
        "content_hash": result.get("content_hash"),
    }


def _configured_sleeper_history_ids(settings: Settings) -> list[str]:
    return list(settings.historical_sleeper_league_ids)


async def sync_sleeper_history(settings: Settings | None = None) -> dict[str, Any]:
    """Archive explicitly configured or linked prior Sleeper leagues."""

    cfg = settings or get_settings()
    configured = _configured_sleeper_history_ids(cfg)
    discovered: list[str] = []
    async with SleeperClient(cfg) as sleeper:
        current = _require_dict(await sleeper.league(cfg.sleeper_league_id), "current league")
        previous_id = str(current.get("previous_league_id") or "")
        seen = {cfg.sleeper_league_id}
        while previous_id and previous_id not in seen and len(discovered) < 20:
            seen.add(previous_id)
            discovered.append(previous_id)
            prior = _require_dict(await sleeper.league(previous_id), "historical league")
            previous_id = str(prior.get("previous_league_id") or "")
        league_ids = list(dict.fromkeys([*configured, *discovered]))
        if not league_ids:
            return {
                "status": "not_configured",
                "league_ids": [],
                "reason": (
                    "The current league has no previous_league_id and no explicit historical "
                    "Sleeper league IDs are configured"
                ),
            }

        leagues: dict[str, Any] = {}
        for league_id in league_ids:
            league_r, users_r, rosters_r, drafts_r, winners_r, losers_r = await asyncio.gather(
                sleeper.league(league_id),
                sleeper.users(league_id),
                sleeper.rosters(league_id),
                sleeper.drafts(league_id),
                sleeper.winners_bracket(league_id),
                sleeper.losers_bracket(league_id),
            )
            weekly = await asyncio.gather(
                *(
                    request
                    for week in range(1, 19)
                    for request in (
                        sleeper.matchups(league_id, week),
                        sleeper.transactions(league_id, week),
                    )
                )
            )
            leagues[league_id] = {
                "league": league_r.payload,
                "users": users_r.payload,
                "rosters": rosters_r.payload,
                "drafts": drafts_r.payload,
                "winners_bracket": winners_r.payload,
                "losers_bracket": losers_r.payload,
                "matchups_by_week": {
                    str(week): weekly[(week - 1) * 2].payload for week in range(1, 19)
                },
                "transactions_by_week": {
                    str(week): weekly[(week - 1) * 2 + 1].payload for week in range(1, 19)
                },
            }

    payload = {
        "version": HISTORICAL_BACKFILL_VERSION,
        "generated_at": _utc_now(),
        "league_ids": league_ids,
        "leagues": leagues,
    }
    payload["content_hash"] = content_hash(leagues)
    destination = Path(cfg.reports_dir) / SLEEPER_HISTORY_FILE
    _write_gzip_json(destination, payload)
    return {
        "status": "complete",
        "league_ids": league_ids,
        "configured_ids": configured,
        "discovered_ids": discovered,
        "compressed_bytes": destination.stat().st_size,
        "content_hash": payload["content_hash"],
    }


async def probe_fantasypros_archive(
    settings: Settings | None = None,
    *,
    season: int | None = None,
    week: int = 17,
    position: str = "RB",
    force: bool = False,
) -> dict[str, Any]:
    """Verify past-week access without treating an undated response as causal evidence."""

    cfg = settings or get_settings()
    destination = Path(cfg.reports_dir) / FANTASYPROS_ARCHIVE_PROBE_FILE
    if destination.exists() and not force:
        try:
            cached = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        if cached.get("status") == "access_confirmed":
            return {**cached, "cached": True}
    if cfg.fantasypros_api_key is None:
        return {"status": "not_configured"}
    if int(fantasypros_quota_status(cfg).get("standard_remaining") or 0) < 1:
        return {"status": "quota_deferred"}
    if season is None:
        async with SleeperClient(cfg) as sleeper:
            state = _require_dict(await sleeper.nfl_state(), "NFL state")
        season = int(state.get("season") or 0) - 1
    try:
        async with FantasyProsClient(cfg) as client:
            response = await client.projections(
                season=season,
                week=week,
                position=position,
            )
    except FantasyProsQuotaExceeded:
        return {"status": "quota_deferred"}
    payload = response.payload if isinstance(response.payload, dict) else {}
    players = list(payload.get("players") or [])
    provider_timestamp_fields = [
        key
        for key in payload
        if any(token in str(key).lower() for token in ("observed", "updated", "as_of", "timestamp"))
    ]
    result = {
        "status": "access_confirmed" if players else "empty",
        "generated_at": _utc_now(),
        "season": season,
        "week": week,
        "position": position,
        "returned_players": len(players),
        "provider_count": payload.get("count"),
        "provider_timestamp_fields": provider_timestamp_fields,
        "causal_promotion_eligible": bool(provider_timestamp_fields),
        "causal_limitation": (
            None
            if provider_timestamp_fields
            else "The response has no provider-side as-of timestamp and is diagnostic only"
        ),
        "content_hash": content_hash(payload),
        "payload": payload,
        "quota": response.quota,
    }
    _write_json(destination, result)
    return result


async def run_historical_backfill(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    nflverse = await sync_nflverse_history(cfg)
    sleeper = await sync_sleeper_history(cfg)
    fantasypros = await probe_fantasypros_archive(cfg)
    report = {
        "generated_at": _utc_now(),
        "version": HISTORICAL_BACKFILL_VERSION,
        "nflverse": nflverse,
        "sleeper": sleeper,
        "fantasypros_archive_probe": {
            key: value for key, value in fantasypros.items() if key != "payload"
        },
    }
    _write_json(Path(cfg.reports_dir) / "historical-backfill.json", report)
    return report
