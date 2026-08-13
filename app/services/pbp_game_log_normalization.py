"""Canonical PBP game-log normalization shared by live and durable paths.

PBP's wire rows are sparse, encode minutes as ``MM:SS``, and omit the player's
``TeamId`` (the request-time rows carry only ``Team``/``Opponent`` tricodes).
This module is the single owned mapping from PBP player-game observations to
the canonical game-log frame consumed by the game-log service, reused by
durable ingestion so the live and stored field mappings cannot drift.

Every row must join by canonical game ID to the governed Event Catalog to
recover team identity, opponent identity, home/away, and the existing
``TEAM vs. OPP`` / ``TEAM @ OPP`` Matchup notation.  The join is exact and
fail-closed: an unjoined game, a team tricode that matches neither event side,
a phase that disagrees with the requested season type, or any malformed
identity/minute evidence aborts the whole pass rather than serving or
publishing a partial set.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Mapping

import pandas as pd

from app.domain.nba_events import player_game_log_season_type
from app.errors import ProviderUnavailableError
from app.providers.pbp_game_logs import PBP_GAME_LOG_COUNTING_COLUMNS
from app.services.game_log_frame import GAME_LOG_FRAME_COLUMNS, derive_game_log_frame

#: The closed additive/counting fields for which PBP omits observed zeros.
#: Identity, game, date, team, opponent, and minutes evidence is never
#: zero-filled: a row missing one of those is malformed.
_PBP_ZERO_FILLED_FIELDS = frozenset(PBP_GAME_LOG_COUNTING_COLUMNS)

_MINUTES_PATTERN = re.compile(r"^(?P<minutes>[0-9]+):(?P<seconds>[0-5][0-9])$")

_REQUIRED_ROW_FIELDS = (
    "EntityId",
    "Name",
    "GameId",
    "Date",
    "Team",
    "Minutes",
)


@dataclass(frozen=True, slots=True)
class PBPJoinCounts:
    """Bounded identity-exclusion counters from one normalization pass.

    The join is fail-closed, so a successful pass always reports zero
    exclusions; the counters are retained so callers observe the input row
    count without re-reading the provider frame.
    """

    source_row_count: int
    unjoined_event_count: int = 0
    team_mismatch_count: int = 0
    unsupported_phase_count: int = 0


def parse_pbp_minutes(value: Any) -> float:
    """Parse one PBP ``MM:SS`` minutes value into the numeric minutes domain.

    Invalid, nonfinite, or missing minutes are malformed rather than zero.
    """

    if not isinstance(value, str):
        raise ProviderUnavailableError(
            "PBP Stats returned invalid minutes evidence."
        )
    match = _MINUTES_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ProviderUnavailableError(
            "PBP Stats returned invalid minutes evidence."
        )
    minutes = int(match.group("minutes")) + int(match.group("seconds")) / 60
    if not math.isfinite(minutes):
        raise ProviderUnavailableError(
            "PBP Stats returned invalid minutes evidence."
        )
    return minutes


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ProviderUnavailableError(
            "PBP Stats game-log rows are missing identity evidence."
        )
    return str(value).strip()


def _counting_value(row: Mapping[str, Any], field: str) -> int:
    return _integer_value(row, field, minimum=0)


def _integer_value(
    row: Mapping[str, Any], field: str, *, minimum: int | None
) -> int:
    value = row.get(field)
    if value is None:
        return 0
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ProviderUnavailableError(
            "PBP Stats returned an invalid box-score value."
        ) from error
    if (
        not math.isfinite(numeric)
        or not numeric.is_integer()
        or (minimum is not None and numeric < minimum)
    ):
        raise ProviderUnavailableError(
            "PBP Stats returned an invalid box-score value."
        )
    return int(numeric)


def _canonical_identity(row: Mapping[str, Any]) -> tuple[int, str, str]:
    entity_id = _integer_value(row, "EntityId", minimum=1)
    name = _required_text(row, "Name")
    game_id = _required_text(row, "GameId")
    return entity_id, name, game_id


def _reject_identity(message: str) -> None:
    raise ProviderUnavailableError(message)


def normalize_pbp_game_logs(
    frame: pd.DataFrame,
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    season_type: str = "Regular Season",
    round_minutes: bool = True,
) -> tuple[pd.DataFrame, PBPJoinCounts]:
    """Convert PBP observations into the canonical game-log frame.

    ``events`` is the governed Event Catalog for the requested season.  Team
    identity is derived exactly from each row's ``Team`` tricode against the
    event; any row that cannot join, contradicts its team, disagrees with the
    requested phase, or is missing identity/minutes evidence aborts the whole
    pass with ``ProviderUnavailableError`` so neither the live response nor a
    durable game publication ever tolerates a dropped row.
    """

    if not isinstance(frame, pd.DataFrame):
        raise ProviderUnavailableError(
            "PBP Stats returned an invalid game-log response."
        )
    event_map: dict[str, dict[str, Any]] = {}
    for event in events:
        game_id = event.get("nba_game_id")
        if game_id is not None:
            event_map[str(game_id)] = event

    rows = frame.to_dict(orient="records")
    canonical: list[dict[str, Any]] = []

    for row in rows:
        for field in _REQUIRED_ROW_FIELDS:
            if field not in row:
                raise ProviderUnavailableError(
                    "PBP Stats returned an invalid game-log schema."
                )
        entity_id, name, game_id = _canonical_identity(row)
        event = event_map.get(game_id)
        if event is None:
            _reject_identity(
                "PBP Stats returned a game log that cannot join the governed Event Catalog."
            )
        if player_game_log_season_type(event) != season_type:
            _reject_identity(
                "PBP Stats returned a game log outside the requested phase."
            )
        team_tricode = _required_text(row, "Team")
        if team_tricode == event["home_team_tricode"]:
            team_id = event["home_team_id"]
            team_tricode = event["home_team_tricode"]
            opponent_tricode = event["away_team_tricode"]
            matchup = f"{team_tricode} vs. {opponent_tricode}"
        elif team_tricode == event["away_team_tricode"]:
            team_id = event["away_team_id"]
            team_tricode = event["away_team_tricode"]
            opponent_tricode = event["home_team_tricode"]
            matchup = f"{team_tricode} @ {opponent_tricode}"
        else:
            _reject_identity(
                "PBP Stats returned a contradictory team identity."
            )

        two_pt_made = _counting_value(row, "FG2M")
        two_pt_attempted = _counting_value(row, "FG2A")
        three_pt_made = _counting_value(row, "FG3M")
        three_pt_attempted = _counting_value(row, "FG3A")
        # PBP reports free-throw points rather than made free throws; a made
        # free throw is exactly one point, so the two are semantically equal
        # for the endpoint's canonical FTM/FT_PCT presentation.
        free_throws_made = _counting_value(row, "FtPoints")
        free_throws_attempted = _counting_value(row, "FTA")
        field_goals_made = two_pt_made + three_pt_made
        field_goals_attempted = two_pt_attempted + three_pt_attempted
        if (
            field_goals_made > field_goals_attempted
            or three_pt_made > three_pt_attempted
            or free_throws_made > free_throws_attempted
        ):
            raise ProviderUnavailableError(
                "PBP Stats returned inconsistent shooting facts."
            )
        offensive_rebounds = _counting_value(row, "OffRebounds")
        defensive_rebounds = _counting_value(row, "DefRebounds")
        minutes = parse_pbp_minutes(row["Minutes"])
        canonical.append(
            {
                "PLAYER_ID": entity_id,
                "PLAYER_NAME": name,
                "GAME_ID": game_id,
                "GAME_DATE": _required_text(row, "Date"),
                "MATCHUP": matchup,
                "TEAM_ID": team_id,
                "TEAM_ABBREVIATION": team_tricode,
                "MIN": minutes,
                "PTS": _counting_value(row, "Points"),
                "REB": offensive_rebounds + defensive_rebounds,
                "AST": _counting_value(row, "Assists"),
                "FGM": field_goals_made,
                "FGA": field_goals_attempted,
                "FG3M": three_pt_made,
                "FG3A": three_pt_attempted,
                "FTM": free_throws_made,
                "FTA": free_throws_attempted,
                "OREB": offensive_rebounds,
                "DREB": defensive_rebounds,
                "TOV": _counting_value(row, "Turnovers"),
                "STL": _counting_value(row, "Steals"),
                "BLK": _counting_value(row, "Blocks"),
                "PF": _counting_value(row, "Fouls"),
            }
        )

    primitive = pd.DataFrame(
        canonical,
        columns=GAME_LOG_FRAME_COLUMNS,
    )
    return (
        derive_game_log_frame(primitive, round_minutes=round_minutes),
        PBPJoinCounts(source_row_count=len(rows)),
    )


__all__ = [
    "PBPJoinCounts",
    "normalize_pbp_game_logs",
    "parse_pbp_minutes",
]
