"""Canonical Game Ledger domain and transactional repository.

This module owns the expensive seam between one PBP ``FullGame`` observation
and durable facts.  It intentionally does not register an HTTP route.  The
collector/control-plane writer or a scheduled backfill can prepare a
``CanonicalGame`` and hand it to :class:`CanonicalGameLedgerRepository`;
validation happens again at the repository boundary so direct callers cannot
bypass the complete-game invariant.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Connection, Engine

from app.domain.nba_events import REGULAR_SEASON_TYPE, is_final_event
from app.domain.utc import assume_utc
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
    LedgerBackfillState,
    LedgerPublication,
)
from app.providers.pbp_game_logs import PBP_GAME_LOG_COUNTING_COLUMNS, PBPGameLogAdapter
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.pbp_game_log_normalization import normalize_pbp_game_logs
from app.utils.db import is_demo_database_url

COUNT_FIELDS = (
    "points",
    "field_goals_made",
    "field_goals_attempted",
    "two_pointers_made",
    "two_pointers_attempted",
    "three_pointers_made",
    "three_pointers_attempted",
    "free_throws_made",
    "free_throws_attempted",
    "offensive_rebounds",
    "defensive_rebounds",
    "rebounds",
    "assists",
    "turnovers",
    "steals",
    "blocks",
    "personal_fouls",
)
ASSIST_LOCATION_FIELDS = (
    "two_point_assists",
    "three_point_assists",
    "arc3_assists",
    "corner3_assists",
    "at_rim_assists",
    "short_mid_range_assists",
    "long_mid_range_assists",
)


class LedgerValidationError(ValueError):
    """A candidate game cannot be published without losing governed facts."""


@dataclass(frozen=True, slots=True)
class PlayerGameFact:
    """Approved FullGame count primitives for one player."""

    player_id: int
    player_name: str
    team_id: int
    team_tricode: str
    minutes: float
    points: int
    field_goals_made: int
    field_goals_attempted: int
    two_pointers_made: int
    two_pointers_attempted: int
    three_pointers_made: int
    three_pointers_attempted: int
    free_throws_made: int
    free_throws_attempted: int
    offensive_rebounds: int
    defensive_rebounds: int
    rebounds: int
    assists: int
    turnovers: int
    steals: int
    blocks: int
    personal_fouls: int
    two_point_assists: int | None = None
    three_point_assists: int | None = None
    arc3_assists: int | None = None
    corner3_assists: int | None = None
    at_rim_assists: int | None = None
    short_mid_range_assists: int | None = None
    long_mid_range_assists: int | None = None
    possessions: float | None = None


@dataclass(frozen=True, slots=True)
class TeamGameFact:
    """Approved FullGame count primitives for one team."""

    team_id: int
    team_tricode: str
    opponent_team_id: int
    opponent_team_tricode: str
    is_home: bool
    points: int = 0
    field_goals_made: int = 0
    field_goals_attempted: int = 0
    two_pointers_made: int = 0
    two_pointers_attempted: int = 0
    three_pointers_made: int = 0
    three_pointers_attempted: int = 0
    free_throws_made: int = 0
    free_throws_attempted: int = 0
    offensive_rebounds: int = 0
    defensive_rebounds: int = 0
    rebounds: int = 0
    assists: int = 0
    turnovers: int = 0
    steals: int = 0
    blocks: int = 0
    personal_fouls: int = 0
    possessions: float | None = None
    team_minutes: float = 0.0


@dataclass(frozen=True, slots=True)
class CanonicalGame:
    """One complete, replacement-safe Regular Season game observation."""

    game_id: str
    season: str
    game_date: date
    home_team_id: int
    home_team_tricode: str
    away_team_id: int
    away_team_tricode: str
    team_facts: tuple[TeamGameFact, ...]
    player_facts: tuple[PlayerGameFact, ...]
    source_observation_id: str
    retrieved_at: datetime
    season_type: str = REGULAR_SEASON_TYPE
    status: str = "final"
    checksum: str | None = None

    def with_checksum(self) -> CanonicalGame:
        return replace(self, checksum=game_checksum(self))


@dataclass(frozen=True, slots=True)
class LedgerWriteResult:
    game_id: str
    checksum: str
    inserted: bool
    replaced: bool
    row_count: int


@dataclass(frozen=True, slots=True)
class LedgerGameSummary:
    game_id: str
    season: str
    game_date: date
    checksum: str
    retrieved_at: datetime
    player_count: int
    team_count: int


@dataclass(frozen=True, slots=True)
class LedgerBackfillProgress:
    season: str
    cutoff: datetime
    cursor_game_id: str | None
    completed_game_ids: frozenset[str]
    failed_game_ids: frozenset[str]
    status: str
    updated_at: datetime
    last_error: str | None = None


def _canonical_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as error:
        raise LedgerValidationError("game_date must be an ISO date") from error


def _required_text(value: Any, field_name: str) -> str:
    if value is None:
        raise LedgerValidationError(f"{field_name} is required")
    result = str(value).strip()
    if not result:
        raise LedgerValidationError(f"{field_name} is required")
    return result


def _integer(value: Any, field_name: str, *, nullable: bool = False) -> int | None:
    if (value is None or (nullable and isinstance(value, float) and math.isnan(value))) and nullable:
        return None
    # PBP's approved counting vocabulary is sparse: an omitted counting
    # field is an observed zero.  Identity fields still fail later when the
    # resulting zero cannot satisfy the complete-game invariant.
    if value is None:
        return 0
    if isinstance(value, bool):
        raise LedgerValidationError(f"{field_name} must be a non-negative integer")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise LedgerValidationError(f"{field_name} must be a non-negative integer") from error
    if not math.isfinite(number) or not number.is_integer() or number < 0:
        raise LedgerValidationError(f"{field_name} must be a non-negative integer")
    return int(number)


def _number(value: Any, field_name: str, *, nullable: bool = False) -> float | None:
    if (value is None or (nullable and isinstance(value, float) and math.isnan(value))) and nullable:
        return None
    if isinstance(value, bool):
        raise LedgerValidationError(f"{field_name} must be a finite non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise LedgerValidationError(f"{field_name} must be a finite non-negative number") from error
    if not math.isfinite(number) or number < 0:
        raise LedgerValidationError(f"{field_name} must be a finite non-negative number")
    return number


def _raw_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _wire_player_rows(observation: Mapping[str, Any]) -> dict[tuple[int, str], Mapping[str, Any]]:
    """Retain provider fields that the legacy adapter intentionally projects away.

    ``parse_game_stats`` has a deliberately closed public game-log vocabulary.
    The ledger's FullGame seam may additionally carry assist-location evidence,
    so durable normalization keeps the original row as a side channel keyed
    by the same player/team identity.  The adapter still owns wire-shape
    validation; this helper only preserves already-validated dictionaries.
    """

    stats = observation.get("stats")
    if not isinstance(stats, Mapping):
        return {}
    home_code = str(observation.get("home_team_abbreviation") or "")
    away_code = str(observation.get("away_team_abbreviation") or "")
    side_codes = {"Home": home_code, "Away": away_code}
    rows: dict[tuple[int, str], Mapping[str, Any]] = {}
    for side, team_code in side_codes.items():
        period = stats.get(side)
        period_rows = period.get("FullGame") if isinstance(period, Mapping) else None
        if not isinstance(period_rows, Sequence) or isinstance(period_rows, (str, bytes, bytearray)):
            continue
        for row in period_rows:
            if not isinstance(row, Mapping) or _raw_value(row, "EntityId", "PLAYER_ID", "player_id") in (None, "0", 0):
                continue
            player_id = _integer(_raw_value(row, "EntityId", "PLAYER_ID", "player_id"), "player_id")
            if player_id is not None:
                rows[(player_id, team_code)] = dict(row)
    return rows


def _validate_wire_game_identity(
    observation: Mapping[str, Any],
    *,
    event: Mapping[str, Any],
    canonical_season: str,
    expected_game_id: str,
) -> None:
    """Fence explicit FullGame envelope identity to the governed event."""

    identity_aliases = {
        "game_id": ("game_id", "gameId", "GameId", "nba_game_id"),
        "home_team_id": ("home_team_id", "homeTeamId", "HomeTeamId"),
        "away_team_id": ("away_team_id", "awayTeamId", "AwayTeamId"),
        "season": ("season", "Season"),
        "season_type": ("season_type", "seasonType", "SeasonType"),
    }
    raw_game_id = _raw_value(observation, *identity_aliases["game_id"])
    if raw_game_id is not None and str(raw_game_id) != expected_game_id:
        raise LedgerValidationError("PBP game identity contradicts the governed event")
    for field_name, expected in (
        ("home_team_id", event.get("home_team_id")),
        ("away_team_id", event.get("away_team_id")),
    ):
        raw_value = _raw_value(observation, *identity_aliases[field_name])
        if raw_value is not None and _integer(raw_value, field_name) != _integer(expected, field_name):
            raise LedgerValidationError("PBP team identity contradicts the governed event")
    raw_season = _raw_value(observation, *identity_aliases["season"])
    if raw_season is not None:
        try:
            raw_canonical_season = validate_canonical_season(str(raw_season))
        except (TypeError, ValueError) as error:
            raise LedgerValidationError("PBP season is not canonical") from error
        if raw_canonical_season != canonical_season:
            raise LedgerValidationError("PBP season contradicts the governed event")
    raw_season_type = _raw_value(observation, *identity_aliases["season_type"])
    if raw_season_type is not None and str(raw_season_type) != REGULAR_SEASON_TYPE:
        raise LedgerValidationError("PBP game is outside the governed Regular Season phase")


def _player_fact_from_row(row: Mapping[str, Any], *, team_id: int, team_tricode: str) -> PlayerGameFact:
    """Map a PBP row while retaining the stable primitive superset."""

    two_made = _integer(_raw_value(row, "FG2M", "two_pointers_made"), "FG2M") or 0
    two_att = _integer(_raw_value(row, "FG2A", "two_pointers_attempted"), "FG2A") or 0
    three_made = _integer(_raw_value(row, "FG3M", "three_pointers_made"), "FG3M") or 0
    three_att = _integer(_raw_value(row, "FG3A", "three_pointers_attempted"), "FG3A") or 0
    fgm = _integer(_raw_value(row, "FGM", "field_goals_made"), "FGM")
    fga = _integer(_raw_value(row, "FGA", "field_goals_attempted"), "FGA")
    fgm = two_made + three_made if fgm is None else fgm
    fga = two_att + three_att if fga is None else fga
    ftm = _integer(_raw_value(row, "FtPoints", "FTM", "free_throws_made"), "FTM") or 0
    fta = _integer(_raw_value(row, "FTA", "free_throws_attempted"), "FTA") or 0
    oreb = _integer(_raw_value(row, "OffRebounds", "OREB", "offensive_rebounds"), "OREB") or 0
    dreb = _integer(_raw_value(row, "DefRebounds", "DREB", "defensive_rebounds"), "DREB") or 0
    reb = _integer(_raw_value(row, "Rebounds", "REB", "rebounds"), "REB")
    if reb is None:
        reb = oreb + dreb
    return PlayerGameFact(
        player_id=_integer(_raw_value(row, "EntityId", "PLAYER_ID", "player_id"), "player_id") or 0,
        player_name=_required_text(_raw_value(row, "Name", "PLAYER_NAME", "player_name"), "player_name"),
        team_id=team_id,
        team_tricode=team_tricode,
        minutes=_number(_raw_value(row, "MIN", "Minutes", "minutes"), "minutes") or 0.0,
        points=_integer(_raw_value(row, "Points", "PTS", "points"), "points") or 0,
        field_goals_made=fgm,
        field_goals_attempted=fga,
        two_pointers_made=two_made,
        two_pointers_attempted=two_att,
        three_pointers_made=three_made,
        three_pointers_attempted=three_att,
        free_throws_made=ftm,
        free_throws_attempted=fta,
        offensive_rebounds=oreb,
        defensive_rebounds=dreb,
        rebounds=reb,
        assists=_integer(_raw_value(row, "Assists", "AST", "assists"), "assists") or 0,
        turnovers=_integer(_raw_value(row, "Turnovers", "TOV", "turnovers"), "turnovers") or 0,
        steals=_integer(_raw_value(row, "Steals", "STL", "steals"), "steals") or 0,
        blocks=_integer(_raw_value(row, "Blocks", "BLK", "blocks"), "blocks") or 0,
        personal_fouls=_integer(_raw_value(row, "Fouls", "PF", "personal_fouls"), "personal_fouls") or 0,
        two_point_assists=_integer(_raw_value(row, "TwoPtAssists", "two_point_assists"), "two_point_assists", nullable=True),
        three_point_assists=_integer(_raw_value(row, "ThreePtAssists", "three_point_assists"), "three_point_assists", nullable=True),
        arc3_assists=_integer(_raw_value(row, "Arc3Assists", "arc3_assists"), "arc3_assists", nullable=True),
        corner3_assists=_integer(_raw_value(row, "Corner3Assists", "corner3_assists"), "corner3_assists", nullable=True),
        at_rim_assists=_integer(_raw_value(row, "AtRimAssists", "at_rim_assists"), "at_rim_assists", nullable=True),
        short_mid_range_assists=_integer(_raw_value(row, "ShortMidRangeAssists", "short_mid_range_assists"), "short_mid_range_assists", nullable=True),
        long_mid_range_assists=_integer(_raw_value(row, "LongMidRangeAssists", "long_mid_range_assists"), "long_mid_range_assists", nullable=True),
        possessions=_number(_raw_value(row, "Possessions", "possessions"), "possessions", nullable=True),
    )


def _sum_team_facts(
    team_id: int,
    team_tricode: str,
    opponent_team_id: int,
    opponent_team_tricode: str,
    is_home: bool,
    players: Sequence[PlayerGameFact],
    provider_total: Mapping[str, Any] | None = None,
) -> TeamGameFact:
    total = provider_total or {}
    values = {
        field_name: sum(getattr(player, field_name) for player in players)
        for field_name in COUNT_FIELDS
        if field_name != "rebounds"
    }
    values["rebounds"] = sum(player.rebounds for player in players)
    # Team-results payloads are authoritative when available, but only for
    # fields they explicitly publish.  We never invent a total from a
    # provider's percentage or a derived rate.
    aliases = {
        "points": ("Points", "PTS", "points"),
        "field_goals_made": ("FGM", "field_goals_made"),
        "field_goals_attempted": ("FGA", "field_goals_attempted"),
        "two_pointers_made": ("FG2M", "two_pointers_made"),
        "two_pointers_attempted": ("FG2A", "two_pointers_attempted"),
        "three_pointers_made": ("FG3M", "three_pointers_made"),
        "three_pointers_attempted": ("FG3A", "three_pointers_attempted"),
        "free_throws_made": ("FtPoints", "FTM", "free_throws_made"),
        "free_throws_attempted": ("FTA", "free_throws_attempted"),
        "offensive_rebounds": ("OffRebounds", "OREB", "offensive_rebounds"),
        "defensive_rebounds": ("DefRebounds", "DREB", "defensive_rebounds"),
        "rebounds": ("Rebounds", "REB", "rebounds"),
        "assists": ("Assists", "AST", "assists"),
        "turnovers": ("Turnovers", "TOV", "turnovers"),
        "steals": ("Steals", "STL", "steals"),
        "blocks": ("Blocks", "BLK", "blocks"),
        "personal_fouls": ("Fouls", "PF", "personal_fouls"),
    }
    for field_name, names in aliases.items():
        raw = _raw_value(total, *names)
        if raw is not None:
            values[field_name] = _integer(raw, field_name) or 0
    return TeamGameFact(
        team_id=team_id,
        team_tricode=team_tricode,
        opponent_team_id=opponent_team_id,
        opponent_team_tricode=opponent_team_tricode,
        is_home=is_home,
        **values,
        # Five player slots make the effective team-game denominator.  This is
        # 48 for a regulation game and remains correct for overtime when the
        # retained player-minute total grows beyond 240.
        team_minutes=sum(player.minutes for player in players) / 5.0,
        possessions=_number(_raw_value(total, "Possessions", "possessions"), "possessions", nullable=True),
    )


def canonical_game_from_pbp(
    observation: Any,
    *,
    event: Mapping[str, Any],
    season: str | None = None,
    source_observation_id: str | None = None,
    retrieved_at: datetime | None = None,
) -> CanonicalGame:
    """Normalize a recorded/live PBP game-stats observation into a ledger game.

    ``observation`` may be the raw ``/get-game-stats`` JSON document, the
    adapter's normalized DataFrame, or a sequence of row mappings.  Supporting
    all three forms keeps the provider seam injectable and makes parity
    fixtures credential-free.
    """

    game_id = _required_text(event.get("nba_game_id"), "game_id")
    try:
        canonical_season = validate_canonical_season(season or str(event.get("season")))
    except (TypeError, ValueError) as error:
        raise LedgerValidationError("season must be a canonical YYYY-YY value") from error
    expected_phase = str(event.get("classification") or event.get("season_type") or REGULAR_SEASON_TYPE)
    if expected_phase != REGULAR_SEASON_TYPE:
        raise LedgerValidationError("only Regular Season games can enter the ledger")
    if ("status_code" in event or "status_text" in event) and not is_final_event(event):
        raise LedgerValidationError("only completed games can enter the ledger")
    if isinstance(observation, Mapping) and "stats" in observation:
        raw_observation = observation
        _validate_wire_game_identity(
            observation,
            event=event,
            canonical_season=canonical_season,
            expected_game_id=game_id,
        )
        frame = PBPGameLogAdapter.parse_game_stats(observation, game_id=game_id)
        team_results = observation.get("team_results")
    elif isinstance(observation, pd.DataFrame):
        raw_observation = None
        frame = observation
        team_results = None
    elif isinstance(observation, Sequence) and not isinstance(observation, (str, bytes, bytearray)):
        raw_observation = None
        frame = pd.DataFrame(list(observation))
        team_results = None
    else:
        raise LedgerValidationError("PBP observation must be a game-stats document or rows")

    event_shape = dict(event)
    event_shape.setdefault("classification", REGULAR_SEASON_TYPE)
    try:
        # The FullGame wire is sparse: omitted/null additive counters are
        # governed zeroes.  Identity and minutes columns intentionally remain
        # untouched so malformed evidence still fails closed.
        normalized_frame = frame.copy()
        for column in PBP_GAME_LOG_COUNTING_COLUMNS:
            if column in normalized_frame:
                normalized_frame[column] = normalized_frame[column].fillna(0)
        normalized, _counts = normalize_pbp_game_logs(
            normalized_frame,
            [event_shape],
            season_type=REGULAR_SEASON_TYPE,
            round_minutes=False,
        )
    except Exception as error:
        if isinstance(error, LedgerValidationError):
            raise
        raise LedgerValidationError("PBP game observation failed canonical normalization") from error
    if normalized.empty:
        raise LedgerValidationError("a completed game must contain player facts")
    expected_game_date = _canonical_date(event.get("scheduled_at") or (raw_observation or {}).get("date"))
    for normalized_row in normalized.to_dict(orient="records"):
        if _canonical_date(normalized_row.get("GAME_DATE")) != expected_game_date:
            raise LedgerValidationError("PBP player date contradicts the governed event")

    raw_by_player: dict[tuple[int, str], Mapping[str, Any]] = {}
    for row in normalized_frame.to_dict(orient="records"):
        player_id = _integer(_raw_value(row, "EntityId", "PLAYER_ID", "player_id"), "player_id")
        if player_id is not None:
            team_code = str(_raw_value(row, "Team", "TEAM_ABBREVIATION", "team_tricode") or "")
            raw_by_player[(player_id, team_code)] = row
    if raw_observation is not None:
        raw_by_player.update(_wire_player_rows(raw_observation))

    players: list[PlayerGameFact] = []
    for row in normalized.to_dict(orient="records"):
        player_id = int(row["PLAYER_ID"])
        team_code = str(row["TEAM_ABBREVIATION"])
        raw = {**raw_by_player.get((player_id, team_code), {}), **row}
        players.append(
            _player_fact_from_row(
                raw,
                team_id=int(row["TEAM_ID"]),
                team_tricode=str(row["TEAM_ABBREVIATION"]),
            )
        )

    home_id = _integer(event.get("home_team_id"), "home_team_id") or 0
    away_id = _integer(event.get("away_team_id"), "away_team_id") or 0
    home_code = _required_text(event.get("home_team_tricode"), "home_team_tricode")
    away_code = _required_text(event.get("away_team_tricode"), "away_team_tricode")
    home_players = tuple(player for player in players if player.team_id == home_id)
    away_players = tuple(player for player in players if player.team_id == away_id)
    if not home_players or not away_players:
        raise LedgerValidationError("a complete game must contain both event teams")

    team_result_map: dict[int, Mapping[str, Any]] = {}
    if isinstance(team_results, Mapping):
        for side, team_id in (("Home", home_id), ("Away", away_id)):
            value = team_results.get(side)
            if isinstance(value, Mapping) and isinstance(value.get("FullGame"), Mapping):
                result = value["FullGame"]
            elif isinstance(value, Mapping):
                result = value
            else:
                result = None
            if result is not None:
                result_team_id = _raw_value(result, "TeamId", "team_id", "TEAM_ID")
                if result_team_id is not None and _integer(result_team_id, "team_id") != team_id:
                    raise LedgerValidationError("PBP team totals contradict the governed event")
                team_result_map[team_id] = result
    teams = (
        _sum_team_facts(home_id, home_code, away_id, away_code, True, home_players, team_result_map.get(home_id)),
        _sum_team_facts(away_id, away_code, home_id, home_code, False, away_players, team_result_map.get(away_id)),
    )
    observation_id = _required_text(source_observation_id or game_id, "source_observation_id")
    retrieved = assume_utc(retrieved_at or datetime.now(timezone.utc))
    game_date = expected_game_date
    if raw_observation is not None and raw_observation.get("date") is not None:
        if _canonical_date(raw_observation["date"]) != game_date:
            raise LedgerValidationError("PBP game date contradicts the governed event")
    return CanonicalGame(
        game_id=game_id,
        season=canonical_season,
        game_date=game_date,
        home_team_id=home_id,
        home_team_tricode=home_code,
        away_team_id=away_id,
        away_team_tricode=away_code,
        team_facts=teams,
        player_facts=tuple(players),
        source_observation_id=observation_id,
        retrieved_at=retrieved,
    ).with_checksum()


def _fact_payload(value: Any) -> dict[str, Any]:
    result = asdict(value)
    for key, item in list(result.items()):
        if isinstance(item, (date, datetime)):
            result[key] = item.isoformat()
    return result


def game_checksum(game: CanonicalGame) -> str:
    """Hash all retained identity and primitive evidence, never provider JSON."""

    payload = {
        "game": {
            "game_id": game.game_id,
            "season": game.season,
            "game_date": game.game_date.isoformat(),
            "home_team_id": game.home_team_id,
            "home_team_tricode": game.home_team_tricode,
            "away_team_id": game.away_team_id,
            "away_team_tricode": game.away_team_tricode,
            "season_type": game.season_type,
        },
        "teams": [_fact_payload(value) for value in sorted(game.team_facts, key=lambda item: item.team_id)],
        "players": [_fact_payload(value) for value in sorted(game.player_facts, key=lambda item: (item.team_id, item.player_id))],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_count_primitives(value: Any, *, label: str) -> None:
    for field_name in COUNT_FIELDS:
        count = getattr(value, field_name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise LedgerValidationError(f"{label}.{field_name} must be a non-negative integer")
    for made, attempted in (
        ("field_goals_made", "field_goals_attempted"),
        ("two_pointers_made", "two_pointers_attempted"),
        ("three_pointers_made", "three_pointers_attempted"),
        ("free_throws_made", "free_throws_attempted"),
    ):
        if getattr(value, made) > getattr(value, attempted):
            raise LedgerValidationError(f"{label} has made counts above attempts")
    if value.rebounds != value.offensive_rebounds + value.defensive_rebounds:
        raise LedgerValidationError(f"{label}.rebounds must equal offensive plus defensive rebounds")
    if isinstance(value, PlayerGameFact):
        if not isinstance(value.minutes, (int, float)) or not math.isfinite(value.minutes) or value.minutes < 0:
            raise LedgerValidationError(f"{label}.minutes must be finite and non-negative")
        if (
            value.player_id <= 0
            or not isinstance(value.player_name, str)
            or not value.player_name.strip()
            or not isinstance(value.team_tricode, str)
            or not value.team_tricode.strip()
        ):
            raise LedgerValidationError(f"{label} has invalid identity evidence")
        for field_name in ASSIST_LOCATION_FIELDS:
            count = getattr(value, field_name)
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise LedgerValidationError(f"{label}.{field_name} must be null or a non-negative integer")
        if value.possessions is not None and (
            not isinstance(value.possessions, (int, float))
            or not math.isfinite(value.possessions)
            or value.possessions < 0
        ):
            raise LedgerValidationError(f"{label}.possessions must be finite and non-negative")
    elif isinstance(value, TeamGameFact):
        if not isinstance(value.team_minutes, (int, float)) or not math.isfinite(value.team_minutes) or value.team_minutes < 0:
            raise LedgerValidationError(f"{label}.team_minutes must be finite and non-negative")
        if value.possessions is not None and (
            not isinstance(value.possessions, (int, float))
            or not math.isfinite(value.possessions)
            or value.possessions < 0
        ):
            raise LedgerValidationError(f"{label}.possessions must be finite and non-negative")


def validate_complete_game(game: CanonicalGame, *, minimum_active_players_per_team: int = 5) -> CanonicalGame:
    """Validate the complete-game invariant at the domain boundary."""

    if not isinstance(game.season_type, str) or game.season_type != REGULAR_SEASON_TYPE:
        raise LedgerValidationError("only Regular Season games are governed by this ledger")
    try:
        canonical_season = validate_canonical_season(game.season)
    except (TypeError, ValueError) as error:
        raise LedgerValidationError("season must be canonical") from error
    if game.season != canonical_season or not isinstance(game.game_id, str) or not game.game_id.strip():
        raise LedgerValidationError("game identity is not canonical")
    if not isinstance(game.game_date, date):
        raise LedgerValidationError("game_date must be a date")
    if not isinstance(game.status, str) or game.status.strip().lower() not in {"final", "complete", "completed"}:
        raise LedgerValidationError("only completed games can enter the ledger")
    if (
        isinstance(game.home_team_id, bool)
        or isinstance(game.away_team_id, bool)
        or not isinstance(game.home_team_id, int)
        or not isinstance(game.away_team_id, int)
        or game.home_team_id == game.away_team_id
        or game.home_team_id <= 0
        or game.away_team_id <= 0
    ):
        raise LedgerValidationError("a game must have two distinct canonical teams")
    if (
        not isinstance(game.home_team_tricode, str)
        or not isinstance(game.away_team_tricode, str)
        or not game.home_team_tricode.strip()
        or not game.away_team_tricode.strip()
    ):
        raise LedgerValidationError("a game must have two canonical team tricodes")
    if (
        not isinstance(game.team_facts, Sequence)
        or len(game.team_facts) != 2
        or {fact.team_id for fact in game.team_facts} != {game.home_team_id, game.away_team_id}
    ):
        raise LedgerValidationError("a complete game must contain exactly two team fact sets")
    if not isinstance(game.player_facts, Sequence) or not game.player_facts:
        raise LedgerValidationError("a complete game must contain player facts")
    if isinstance(minimum_active_players_per_team, bool) or minimum_active_players_per_team < 1:
        raise LedgerValidationError("minimum_active_players_per_team must be positive")
    player_ids: set[tuple[int, int]] = set()
    global_player_ids: set[int] = set()
    team_identity = {
        game.home_team_id: (game.home_team_tricode, True),
        game.away_team_id: (game.away_team_tricode, False),
    }
    for fact in game.team_facts:
        _validate_count_primitives(fact, label="team_fact")
        if fact.team_id not in {game.home_team_id, game.away_team_id}:
            raise LedgerValidationError("team fact is outside the game identity")
        if fact.opponent_team_id not in {game.home_team_id, game.away_team_id} or fact.opponent_team_id == fact.team_id:
            raise LedgerValidationError("team fact has a contradictory opponent")
        expected_tricode, expected_home = team_identity[fact.team_id]
        opponent_tricode, _ = team_identity[fact.opponent_team_id]
        if fact.team_tricode != expected_tricode or fact.opponent_team_tricode != opponent_tricode:
            raise LedgerValidationError("team fact has a contradictory tricode")
        if fact.is_home != expected_home:
            raise LedgerValidationError("team fact has a contradictory home/away identity")
    active_by_team: dict[int, set[int]] = {game.home_team_id: set(), game.away_team_id: set()}
    for fact in game.player_facts:
        _validate_count_primitives(fact, label="player_fact")
        key = (fact.team_id, fact.player_id)
        if key in player_ids:
            raise LedgerValidationError("a player may appear once per team-game")
        if fact.player_id in global_player_ids:
            raise LedgerValidationError("a player may belong to only one team in a game")
        player_ids.add(key)
        global_player_ids.add(fact.player_id)
        if fact.team_id not in active_by_team:
            raise LedgerValidationError("player fact is outside the game identity")
        expected_tricode, _ = team_identity[fact.team_id]
        if fact.team_tricode != expected_tricode:
            raise LedgerValidationError("player fact has a contradictory team tricode")
        if fact.minutes > 0:
            active_by_team[fact.team_id].add(fact.player_id)
    if any(len(players) < minimum_active_players_per_team for players in active_by_team.values()):
        raise LedgerValidationError("a complete game must cover both teams' active participants")
    if not isinstance(game.source_observation_id, str) or not game.source_observation_id.strip():
        raise LedgerValidationError("source_observation_id is required")
    if not isinstance(game.retrieved_at, datetime):
        raise LedgerValidationError("retrieved_at must be a datetime")
    try:
        checksum = game_checksum(game)
    except (AttributeError, TypeError, ValueError) as error:
        raise LedgerValidationError("game facts cannot be serialized") from error
    if game.checksum is not None and game.checksum != checksum:
        raise LedgerValidationError("game checksum does not match normalized facts")
    return replace(game, checksum=checksum, retrieved_at=assume_utc(game.retrieved_at))


class CanonicalGameLedgerRepository:
    """Temporary-DB friendly atomic repository for complete games and progress."""

    def __init__(self, engine: Engine, *, minimum_active_players_per_team: int = 5) -> None:
        if is_demo_database_url(str(engine.url)):
            raise ValueError("the demo database cannot store ledger facts")
        self.engine = engine
        self.minimum_active_players_per_team = minimum_active_players_per_team
        self.ensure_schema()

    def ensure_schema(self) -> None:
        for model in (
            CanonicalGameLedgerGame,
            CanonicalGameLedgerTeamFact,
            CanonicalGameLedgerPlayerFact,
            LedgerBackfillState,
            LedgerPublication,
        ):
            model.__table__.create(self.engine, checkfirst=True)

    def replace_game(self, game: CanonicalGame) -> LedgerWriteResult:
        """Insert or atomically replace one complete game.

        The transaction deletes old team/player facts only after the new game
        has passed all validation.  Any SQLAlchemy error rolls back the whole
        game, including the checksum identity record.
        """

        candidate = validate_complete_game(
            game,
            minimum_active_players_per_team=self.minimum_active_players_per_team,
        )
        tables = self._tables()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(tables["game"]).where(tables["game"].c.game_id == candidate.game_id)
            ).mappings().one_or_none()
            if existing is not None:
                if self._identity_changed(existing, candidate):
                    raise LedgerValidationError("a correction cannot change a game's canonical identity")
                if existing["checksum"] == candidate.checksum:
                    return LedgerWriteResult(candidate.game_id, candidate.checksum or "", False, False, 0)
            self._delete_game(connection, candidate.game_id, tables)
            connection.execute(insert(tables["game"]).values(self._game_values(candidate)))
            connection.execute(insert(tables["team"]), [self._team_values(candidate.game_id, value) for value in candidate.team_facts])
            connection.execute(insert(tables["player"]), [self._player_values(candidate.game_id, value) for value in candidate.player_facts])
        return LedgerWriteResult(
            candidate.game_id,
            candidate.checksum or "",
            existing is None,
            existing is not None,
            len(candidate.player_facts),
        )

    def replace_games_atomic(self, games: Iterable[CanonicalGame]) -> tuple[LedgerWriteResult, ...]:
        """Validate every game before one transaction replaces all candidates."""

        candidates = tuple(
            validate_complete_game(game, minimum_active_players_per_team=self.minimum_active_players_per_team)
            for game in games
        )
        if not candidates:
            return ()
        if len({game.game_id for game in candidates}) != len(candidates):
            raise LedgerValidationError("one atomic batch cannot contain duplicate game identities")
        tables = self._tables()
        results: list[LedgerWriteResult] = []
        with self.engine.begin() as connection:
            for candidate in candidates:
                existing = connection.execute(
                    select(tables["game"]).where(tables["game"].c.game_id == candidate.game_id)
                ).mappings().one_or_none()
                if existing is not None and self._identity_changed(existing, candidate):
                    raise LedgerValidationError("a correction cannot change a game's canonical identity")
                if existing is not None and existing["checksum"] == candidate.checksum:
                    results.append(LedgerWriteResult(candidate.game_id, candidate.checksum or "", False, False, 0))
                    continue
                self._delete_game(connection, candidate.game_id, tables)
                connection.execute(insert(tables["game"]).values(self._game_values(candidate)))
                connection.execute(insert(tables["team"]), [self._team_values(candidate.game_id, value) for value in candidate.team_facts])
                connection.execute(insert(tables["player"]), [self._player_values(candidate.game_id, value) for value in candidate.player_facts])
                results.append(LedgerWriteResult(candidate.game_id, candidate.checksum or "", existing is None, existing is not None, len(candidate.player_facts)))
        return tuple(results)

    def get_game(self, game_id: str) -> CanonicalGame | None:
        tables = self._tables()
        with self.engine.connect() as connection:
            game_row = connection.execute(select(tables["game"]).where(tables["game"].c.game_id == game_id)).mappings().one_or_none()
            if game_row is None:
                return None
            team_rows = connection.execute(select(tables["team"]).where(tables["team"].c.game_id == game_id).order_by(tables["team"].c.team_id)).mappings().all()
            player_rows = connection.execute(select(tables["player"]).where(tables["player"].c.game_id == game_id).order_by(tables["player"].c.team_id, tables["player"].c.player_id)).mappings().all()
        return _game_from_rows(game_row, team_rows, player_rows)

    def list_games(self, season: str, *, through: date | datetime | None = None) -> tuple[LedgerGameSummary, ...]:
        canonical_season = validate_canonical_season(season)
        table = CanonicalGameLedgerGame.__table__
        statement = select(table).where(table.c.season == canonical_season).order_by(table.c.game_date.desc(), table.c.game_id.desc())
        if through is not None:
            statement = statement.where(table.c.game_date <= _canonical_date(through))
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            summaries = []
            player_table = CanonicalGameLedgerPlayerFact.__table__
            team_table = CanonicalGameLedgerTeamFact.__table__
            for row in rows:
                player_count = len(connection.execute(select(player_table.c.player_id).where(player_table.c.game_id == row["game_id"])).all())
                team_count = len(connection.execute(select(team_table.c.team_id).where(team_table.c.game_id == row["game_id"])).all())
                summaries.append(LedgerGameSummary(row["game_id"], row["season"], row["game_date"], row["checksum"], assume_utc(row["retrieved_at"]), player_count, team_count))
        return tuple(summaries)

    def game_checksums(self, season: str) -> dict[str, str]:
        canonical_season = validate_canonical_season(season)
        table = CanonicalGameLedgerGame.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(select(table.c.game_id, table.c.checksum).where(table.c.season == canonical_season)).all()
        return {str(game_id): str(checksum) for game_id, checksum in rows}

    def save_progress(self, progress: LedgerBackfillProgress) -> None:
        table = LedgerBackfillState.__table__
        values = {
            "season": validate_canonical_season(progress.season),
            "cutoff": assume_utc(progress.cutoff),
            "cursor_game_id": progress.cursor_game_id,
            "completed_game_ids": json.dumps(sorted(progress.completed_game_ids)),
            "failed_game_ids": json.dumps(sorted(progress.failed_game_ids)),
            "status": progress.status,
            "updated_at": assume_utc(progress.updated_at),
            "last_error": progress.last_error,
        }
        with self.engine.begin() as connection:
            connection.execute(delete(table).where(table.c.season == values["season"]))
            connection.execute(insert(table).values(values))

    def get_progress(self, season: str) -> LedgerBackfillProgress | None:
        canonical_season = validate_canonical_season(season)
        table = LedgerBackfillState.__table__
        with self.engine.connect() as connection:
            row = connection.execute(select(table).where(table.c.season == canonical_season)).mappings().one_or_none()
        if row is None:
            return None
        return LedgerBackfillProgress(
            season=canonical_season,
            cutoff=assume_utc(row["cutoff"]),
            cursor_game_id=row["cursor_game_id"],
            completed_game_ids=frozenset(json.loads(row["completed_game_ids"] or "[]")),
            failed_game_ids=frozenset(json.loads(row["failed_game_ids"] or "[]")),
            status=row["status"],
            updated_at=assume_utc(row["updated_at"]),
            last_error=row["last_error"],
        )

    def publish_metadata(self, publication: LedgerPublicationRecord) -> None:
        self.publish_metadata_batch((publication,))

    def publish_metadata_batch(self, publications: Iterable[LedgerPublicationRecord]) -> None:
        """Replace one materialization's metadata rows in one transaction."""

        records = tuple(publications)
        if not records:
            return
        table = LedgerPublication.__table__
        with self.engine.begin() as connection:
            for publication in records:
                values = asdict(publication)
                values["retrieved_at"] = assume_utc(publication.retrieved_at)
                connection.execute(delete(table).where(
                    table.c.stream_key == publication.stream_key,
                    table.c.season == publication.season,
                    table.c.window_kind == publication.window_kind,
                    table.c.window_games == publication.window_games,
                    table.c.as_of == publication.as_of,
                ))
                connection.execute(insert(table).values(values))

    def _tables(self) -> dict[str, Any]:
        return {
            "game": CanonicalGameLedgerGame.__table__,
            "team": CanonicalGameLedgerTeamFact.__table__,
            "player": CanonicalGameLedgerPlayerFact.__table__,
        }

    @staticmethod
    def _identity_changed(existing: Mapping[str, Any], candidate: CanonicalGame) -> bool:
        return any(
            existing[field_name] != expected
            for field_name, expected in (
                ("season", candidate.season),
                ("season_type", candidate.season_type),
                ("game_date", candidate.game_date),
                ("home_team_id", candidate.home_team_id),
                ("home_team_tricode", candidate.home_team_tricode),
                ("away_team_id", candidate.away_team_id),
                ("away_team_tricode", candidate.away_team_tricode),
            )
        )

    @staticmethod
    def _delete_game(connection: Connection, game_id: str, tables: Mapping[str, Any]) -> None:
        connection.execute(delete(tables["player"]).where(tables["player"].c.game_id == game_id))
        connection.execute(delete(tables["team"]).where(tables["team"].c.game_id == game_id))
        connection.execute(delete(tables["game"]).where(tables["game"].c.game_id == game_id))

    @staticmethod
    def _game_values(game: CanonicalGame) -> dict[str, Any]:
        return {
            "game_id": game.game_id,
            "season": game.season,
            "season_type": game.season_type,
            "game_date": game.game_date,
            "home_team_id": game.home_team_id,
            "home_team_tricode": game.home_team_tricode,
            "away_team_id": game.away_team_id,
            "away_team_tricode": game.away_team_tricode,
            "status": game.status,
            "source_observation_id": game.source_observation_id,
            "checksum": game.checksum or game_checksum(game),
            "retrieved_at": assume_utc(game.retrieved_at),
            "updated_at": assume_utc(game.retrieved_at),
        }

    @staticmethod
    def _team_values(game_id: str, fact: TeamGameFact) -> dict[str, Any]:
        return {"game_id": game_id, **asdict(fact)}

    @staticmethod
    def _player_values(game_id: str, fact: PlayerGameFact) -> dict[str, Any]:
        return {"game_id": game_id, **asdict(fact)}


@dataclass(frozen=True, slots=True)
class LedgerPublicationRecord:
    stream_key: str
    season: str
    window_kind: str
    window_games: int
    as_of: date
    status: str
    checksum: str
    game_count: int
    team_count: int
    retrieved_at: datetime
    reason: str | None = None


def _game_from_rows(game_row: Mapping[str, Any], team_rows: Sequence[Mapping[str, Any]], player_rows: Sequence[Mapping[str, Any]]) -> CanonicalGame:
    teams = tuple(TeamGameFact(**{key: row[key] for key in TeamGameFact.__dataclass_fields__}) for row in team_rows)
    players = tuple(PlayerGameFact(**{key: row[key] for key in PlayerGameFact.__dataclass_fields__}) for row in player_rows)
    return CanonicalGame(
        game_id=game_row["game_id"],
        season=game_row["season"],
        game_date=game_row["game_date"],
        home_team_id=game_row["home_team_id"],
        home_team_tricode=game_row["home_team_tricode"],
        away_team_id=game_row["away_team_id"],
        away_team_tricode=game_row["away_team_tricode"],
        team_facts=teams,
        player_facts=players,
        source_observation_id=game_row["source_observation_id"],
        retrieved_at=assume_utc(game_row["retrieved_at"]),
        season_type=game_row["season_type"],
        status=game_row["status"],
        checksum=game_row["checksum"],
    )


__all__ = [
    "ASSIST_LOCATION_FIELDS",
    "COUNT_FIELDS",
    "CanonicalGame",
    "CanonicalGameLedgerRepository",
    "LedgerBackfillProgress",
    "LedgerGameSummary",
    "LedgerPublicationRecord",
    "LedgerValidationError",
    "LedgerWriteResult",
    "PlayerGameFact",
    "TeamGameFact",
    "canonical_game_from_pbp",
    "game_checksum",
    "validate_complete_game",
]
