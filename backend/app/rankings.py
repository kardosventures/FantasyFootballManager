from __future__ import annotations

import asyncio
import csv
import io
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any

import httpx

from app.utils import content_hash, normalize_player_name

DYNASTY_PROCESS_ECR_URL = (
    "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr_latest.csv"
)
SLEEPER_ADP_URL = "https://hashtagfootball.com/fantasy-football-adp-sleeper"


@dataclass(frozen=True)
class RankingRow:
    source: str
    source_url: str
    player_name: str
    normalized_name: str
    team: str | None
    position: str
    overall_rank: int | None
    position_rank: int | None
    rank_sd: float | None
    as_of: str
    confidence: float
    bye_week: int | None = None
    market_adp: float | None = None
    market_rank: int | None = None
    market_source: str | None = None
    market_as_of: str | None = None


@dataclass(frozen=True)
class MarketAdpRow:
    player_name: str
    normalized_name: str
    position: str
    team: str | None
    adp: float
    overall_rank: int
    position_rank: str | None


class _SleeperAdpParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._adp: float | None = None
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._cells: list[str] = []
        self.rows: list[MarketAdpRow] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr" and attributes.get("data-halfppr"):
            try:
                self._adp = float(attributes["data-halfppr"] or "")
            except ValueError:
                self._adp = None
            self._cells = []
        elif tag == "td" and self._adp is not None:
            self._in_cell = True
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._in_cell:
            self._cells.append(" ".join("".join(self._cell_parts).split()))
            self._in_cell = False
        elif tag == "tr" and self._adp is not None:
            if len(self._cells) >= 7:
                position = self._cells[1].upper().replace("D/ST", "DST")
                overall_rank = _integer(self._cells[5])
                if overall_rank is not None and position:
                    self.rows.append(
                        MarketAdpRow(
                            player_name=self._cells[0],
                            normalized_name=normalize_player_name(self._cells[0]),
                            position=position,
                            team=self._cells[2].upper() or None,
                            adp=self._adp,
                            overall_rank=overall_rank,
                            position_rank=self._cells[6] or None,
                        )
                    )
            self._adp = None
            self._cells = []


def _integer(value: str | None) -> int | None:
    try:
        return int(float(value or ""))
    except ValueError:
        return None


def _number(value: str | None) -> float | None:
    try:
        return float(value or "")
    except ValueError:
        return None


def parse_fantasypros_ecr(csv_text: str) -> list[RankingRow]:
    """Parse the open DynastyProcess mirror into one record per redraft player."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    overall: dict[tuple[str, str], dict[str, str]] = {}
    positional: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        position = (row.get("pos") or "").upper()
        key = (normalize_player_name(row.get("player") or ""), position)
        if row.get("page_type") == "redraft-overall":
            overall[key] = row
        elif row.get("page_type") == f"redraft-{position.lower()}":
            positional[key] = row

    parsed: list[RankingRow] = []
    for key, base in overall.items():
        position_row = positional.get(key, {})
        as_of = base.get("scrape_date") or date.today().isoformat()
        age = max((date.today() - date.fromisoformat(as_of)).days, 0)
        confidence = max(0.25, 0.9 - age * 0.05)
        parsed.append(
            RankingRow(
                source="FantasyPros ECR via DynastyProcess",
                source_url=DYNASTY_PROCESS_ECR_URL,
                player_name=base.get("player") or "",
                normalized_name=key[0],
                team=(base.get("team") or base.get("tm") or None),
                position=key[1],
                overall_rank=_integer(base.get("ecr")),
                position_rank=_integer(position_row.get("ecr")),
                rank_sd=_number(base.get("sd")),
                as_of=as_of,
                confidence=confidence,
                bye_week=_integer(base.get("bye")),
            )
        )
    return sorted(parsed, key=lambda row: row.overall_rank or 10000)


def parse_sleeper_adp(html_text: str) -> tuple[list[MarketAdpRow], str | None]:
    """Parse the public half-PPR Sleeper ADP table and its displayed update date."""
    parser = _SleeperAdpParser()
    parser.feed(html_text)
    match = re.search(r"Updated:</strong>\s*([^<]+)", html_text, flags=re.IGNORECASE)
    as_of = None
    if match:
        try:
            as_of = datetime.strptime(match.group(1).strip(), "%d %B %Y").date().isoformat()
        except ValueError:
            pass
    return parser.rows, as_of


def _merge_market_adp(
    rankings: list[RankingRow], adp_rows: list[MarketAdpRow], as_of: str | None
) -> tuple[list[RankingRow], int]:
    market: dict[tuple[str, str], MarketAdpRow] = {}
    for row in adp_rows:
        key = (row.normalized_name, row.position)
        if key not in market or row.adp < market[key].adp:
            market[key] = row

    merged: list[RankingRow] = []
    match_count = 0
    for row in rankings:
        adp = market.get((row.normalized_name, row.position))
        if adp and row.team and adp.team and row.team.upper() != adp.team.upper():
            adp = None
        if adp:
            match_count += 1
        merged.append(
            RankingRow(
                **{
                    **asdict(row),
                    "market_adp": adp.adp if adp else None,
                    "market_rank": adp.overall_rank if adp else None,
                    "market_source": "Sleeper ADP via Hashtag Football" if adp else None,
                    "market_as_of": as_of if adp else None,
                }
            )
        )
    return merged, match_count


async def fetch_open_rankings() -> tuple[list[RankingRow], dict[str, Any], str]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        ecr_response, adp_response = await asyncio.gather(
            client.get(DYNASTY_PROCESS_ECR_URL), client.get(SLEEPER_ADP_URL)
        )
        ecr_response.raise_for_status()
        adp_response.raise_for_status()
    rankings = parse_fantasypros_ecr(ecr_response.text)
    if len(rankings) < 100:
        raise ValueError(f"Ranking feed is implausibly partial ({len(rankings)} redraft players)")
    adp_rows, market_as_of = parse_sleeper_adp(adp_response.text)
    if len(adp_rows) < 100:
        raise ValueError(f"Sleeper ADP feed is implausibly partial ({len(adp_rows)} players)")
    rankings, market_match_count = _merge_market_adp(rankings, adp_rows, market_as_of)
    provenance = {
        "provider": "DynastyProcess",
        "underlying_source": "FantasyPros expert consensus rankings",
        "ranking_format": "PPR",
        "url": DYNASTY_PROCESS_ECR_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rankings),
        "latest_source_date": max(row.as_of for row in rankings),
        "market_provider": "Hashtag Football",
        "market_underlying_source": "Sleeper half-PPR ADP",
        "market_url": SLEEPER_ADP_URL,
        "market_retrieved_at": datetime.now(timezone.utc).isoformat(),
        "market_source_date": market_as_of,
        "market_row_count": len(adp_rows),
        "market_match_count": market_match_count,
        "license_note": "Open upstream mirror; retain source attribution and retrieval timestamp.",
    }
    payload = [asdict(row) for row in rankings]
    return rankings, provenance, content_hash(payload)
