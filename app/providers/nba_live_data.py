"""NBA LiveData box-score fallback for governed ledger collection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from app.config.settings import RuntimeSettings
from app.utils.nba_api_config import get_shared_nba_session
from app.utils.telemetry import (
    CACHE_DISABLED,
    PROVIDER_NBA_LIVE_DATA,
    ProviderResponseError,
    provider_call,
)

NBA_LIVE_DATA_BOXSCORE_URL = (
    "https://nba-prod-us-east-1-mediaops-stats.s3.amazonaws.com/"
    "NBA/liveData/boxscore/boxscore_{game_id}.json"
)

_PLAYER_FIELDS = {
    "FGM": "fieldGoalsMade",
    "FGA": "fieldGoalsAttempted",
    "FG2M": "twoPointersMade",
    "FG2A": "twoPointersAttempted",
    "FG3M": "threePointersMade",
    "FG3A": "threePointersAttempted",
    "FtPoints": "freeThrowsMade",
    "FTA": "freeThrowsAttempted",
    "OffRebounds": "reboundsOffensive",
    "DefRebounds": "reboundsDefensive",
    "Rebounds": "reboundsTotal",
    "Assists": "assists",
    "Turnovers": "turnovers",
    "Steals": "steals",
    "Blocks": "blocks",
    "Fouls": "foulsPersonal",
    "Points": "points",
}


class NBALiveDataBoxscoreAdapter:
    """Fetch one complete NBA-hosted LiveData box score."""

    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        session: requests.Session | Any | None = None,
    ) -> None:
        self.session = session or get_shared_nba_session(settings)
        provider_settings = (settings or RuntimeSettings()).providers
        self.connect_timeout = provider_settings.pbp_connect_timeout_seconds
        self.read_timeout = provider_settings.pbp_read_timeout_seconds

    def fetch_game_stats(
        self,
        game_id: str,
        season: str,
        *,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        del season, season_type
        if not re.fullmatch(r"\d{10}", str(game_id)):
            raise ValueError("NBA game_id must contain exactly 10 digits")
        url = NBA_LIVE_DATA_BOXSCORE_URL.format(game_id=game_id)
        with provider_call(
            PROVIDER_NBA_LIVE_DATA,
            "game_boxscore",
            cache_status=CACHE_DISABLED,
        ) as tracker:
            response = self.session.get(
                url,
                timeout=(self.connect_timeout, self.read_timeout),
            )
            tracker.status_code = response.status_code
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as error:
                raise ProviderResponseError(
                    "NBA LiveData returned a response that was not valid JSON."
                ) from error
        game = payload.get("game") if isinstance(payload, Mapping) else None
        if (
            not isinstance(game, Mapping)
            or str(game.get("gameId")) != str(game_id)
            or game.get("gameStatus") != 3
            or not isinstance(game.get("homeTeam"), Mapping)
            or not isinstance(game.get("awayTeam"), Mapping)
        ):
            raise ProviderResponseError(
                "NBA LiveData box score is missing final governed game evidence."
            )
        return dict(payload)


def compose_pbp_live_data_observation(
    pbp_observation: Mapping[str, Any] | None,
    live_observation: Mapping[str, Any],
    *,
    event: Mapping[str, object],
) -> dict[str, Any]:
    """Preserve PBP evidence and fill only its missing fields from LiveData."""

    game = live_observation.get("game")
    if not isinstance(game, Mapping) or game.get("gameStatus") != 3:
        raise ProviderResponseError("NBA LiveData box score is not final.")
    if str(game.get("gameId")) != str(event.get("nba_game_id")):
        raise ProviderResponseError("NBA LiveData game identity contradicts governance.")

    expected = {
        "Home": (int(event["home_team_id"]), str(event["home_team_tricode"])),
        "Away": (int(event["away_team_id"]), str(event["away_team_tricode"])),
    }
    live_teams = {"Home": game.get("homeTeam"), "Away": game.get("awayTeam")}
    pbp_rows = _pbp_player_rows(pbp_observation)
    stats: dict[str, dict[str, list[dict[str, Any]]]] = {}
    team_results: dict[str, dict[str, dict[str, Any]]] = {}
    participant_ids_by_team: dict[str, list[int]] = {}
    for side, team in live_teams.items():
        if not isinstance(team, Mapping):
            raise ProviderResponseError("NBA LiveData must contain both exact game teams.")
        team_id, tricode = expected[side]
        if int(team.get("teamId") or 0) != team_id or str(team.get("teamTricode")) != tricode:
            raise ProviderResponseError("NBA LiveData team identity contradicts governance.")
        players = team.get("players")
        totals = team.get("statistics")
        if not isinstance(players, list) or not isinstance(totals, Mapping):
            raise ProviderResponseError("NBA LiveData team evidence is incomplete.")
        player_rows = [
            _player_row(player, pbp_rows.get(int(player.get("personId") or 0)))
            for player in players
            if isinstance(player, Mapping) and str(player.get("played")) == "1"
        ]
        if not player_rows:
            raise ProviderResponseError("NBA LiveData has no participating players.")
        participant_ids_by_team[str(team_id)] = [
            int(row["EntityId"]) for row in player_rows
        ]
        live_totals = _normalized_team_totals(totals)
        pbp_summary = _pbp_team_summary(pbp_observation, side)
        summary = dict(pbp_summary or {})
        summary.update({"EntityId": "0", "Name": "Team"})
        summary.setdefault("Minutes", "00:00")
        pbp_team_total = _pbp_team_total(pbp_observation, side)
        merged_totals = dict(pbp_team_total or {})
        merged_totals.update({
            "TeamId": team_id,
            "Name": str(team.get("teamName") or tricode),
            "ShortName": tricode,
        })
        merged_totals.setdefault("Minutes", str(totals.get("minutes") or "00:00"))
        for wire_name, live_name in _PLAYER_FIELDS.items():
            if merged_totals.get(wire_name) is None:
                merged_totals[wire_name] = live_totals[wire_name]
            total = _count(merged_totals, wire_name)
            player_sum = sum(_count(row, wire_name) for row in player_rows)
            residual = total - player_sum
            if residual < 0:
                raise ProviderResponseError(
                    f"NBA LiveData {live_name} total does not reconcile with players."
                )
            if summary.get(wire_name) is None and residual:
                summary[wire_name] = residual
        stats[side] = {"FullGame": [summary, *player_rows]}
        team_results[side] = {"FullGame": merged_totals}

    pbp = dict(pbp_observation or {})
    provider = "pbp+nba_live_data" if pbp_observation is not None else "nba_live_data"
    return {
        "stats": stats,
        "team_results": team_results,
        "home_team_abbreviation": expected["Home"][1],
        "away_team_abbreviation": expected["Away"][1],
        "home_team_id": str(expected["Home"][0]),
        "away_team_id": str(expected["Away"][0]),
        "date": str(pbp.get("date") or game.get("gameTimeUTC")),
        "league": str(pbp.get("league") or "NBA"),
        "season": str(event.get("season")),
        "season_type": str(pbp.get("season_type") or "Regular Season"),
        "participant_ids_by_team": participant_ids_by_team,
        "_ledger_provenance": {
            "provider": provider,
            "source_documents": {
                "pbp": deepcopy(pbp_observation),
                "nba_live_data": deepcopy(live_observation),
            },
        },
    }


def _normalized_team_totals(totals: Mapping[str, Any]) -> dict[str, int]:
    """Include LiveData's separately reported team rebounds in OREB/DREB."""

    normalized = {
        wire_name: _count(totals, live_name)
        for wire_name, live_name in _PLAYER_FIELDS.items()
    }
    normalized["OffRebounds"] += _optional_count(
        totals, "reboundsTeamOffensive"
    )
    normalized["DefRebounds"] += _optional_count(
        totals, "reboundsTeamDefensive"
    )
    if normalized["Rebounds"] != (
        normalized["OffRebounds"] + normalized["DefRebounds"]
    ):
        raise ProviderResponseError(
            "NBA LiveData team rebounds do not reconcile with the reported total."
        )
    return normalized


def _pbp_player_rows(
    observation: Mapping[str, Any] | None,
) -> dict[int, Mapping[str, Any]]:
    indexed: dict[int, Mapping[str, Any]] = {}
    stats = observation.get("stats") if isinstance(observation, Mapping) else None
    if not isinstance(stats, Mapping):
        return indexed
    for side in ("Home", "Away"):
        side_value = stats.get(side)
        rows = side_value.get("FullGame") if isinstance(side_value, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            player_id = int(row.get("EntityId") or 0)
            if player_id > 0:
                indexed[player_id] = row
    return indexed


def _pbp_team_summary(
    observation: Mapping[str, Any] | None, side: str
) -> Mapping[str, Any] | None:
    stats = observation.get("stats") if isinstance(observation, Mapping) else None
    side_value = stats.get(side) if isinstance(stats, Mapping) else None
    rows = side_value.get("FullGame") if isinstance(side_value, Mapping) else None
    if not isinstance(rows, list):
        return None
    return next(
        (row for row in rows if isinstance(row, Mapping) and str(row.get("EntityId")) == "0"),
        None,
    )


def _pbp_team_total(
    observation: Mapping[str, Any] | None, side: str
) -> Mapping[str, Any] | None:
    results = observation.get("team_results") if isinstance(observation, Mapping) else None
    side_value = results.get(side) if isinstance(results, Mapping) else None
    total = side_value.get("FullGame") if isinstance(side_value, Mapping) else None
    return total if isinstance(total, Mapping) else None


def _player_row(
    player: Mapping[str, Any],
    pbp_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    player_id = int(player.get("personId") or 0)
    statistics = player.get("statistics")
    if player_id <= 0 or not isinstance(statistics, Mapping):
        raise ProviderResponseError("NBA LiveData player evidence is malformed.")
    row = dict(pbp_row or {})
    row.update({
        "EntityId": str(player_id),
        "Name": str(player.get("name") or (
            f"{player.get('firstName', '')} {player.get('familyName', '')}".strip()
        )),
    })
    row.setdefault("Minutes", _pbp_minutes(statistics.get("minutes")))
    for wire_name, live_name in _PLAYER_FIELDS.items():
        if row.get(wire_name) is None:
            row[wire_name] = _count(statistics, live_name)
    return row


def _count(values: Mapping[str, Any], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or value is None:
        raise ProviderResponseError(f"NBA LiveData is missing the {key} count.")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProviderResponseError(f"NBA LiveData has a malformed {key} count.") from error
    if not numeric.is_finite() or numeric < 0 or numeric != numeric.to_integral_value():
        raise ProviderResponseError(f"NBA LiveData has a malformed {key} count.")
    return int(numeric)


def _optional_count(values: Mapping[str, Any], key: str) -> int:
    if key not in values:
        return 0
    return _count(values, key)


def _pbp_minutes(value: object) -> str:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", str(value))
    if match is None:
        raise ProviderResponseError("NBA LiveData has malformed player minutes.")
    hours, minutes, seconds = match.groups()
    seconds_value = float(seconds or 0)
    if seconds_value < 0 or seconds_value >= 60:
        raise ProviderResponseError("NBA LiveData has malformed player minutes.")
    total_minutes = int(hours or 0) * 60 + int(minutes or 0)
    return f"{total_minutes:02d}:{int(seconds_value):02d}"


__all__ = [
    "NBA_LIVE_DATA_BOXSCORE_URL",
    "NBALiveDataBoxscoreAdapter",
    "compose_pbp_live_data_observation",
]
