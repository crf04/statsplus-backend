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
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import case, delete, exists, insert, inspect, literal, or_, select, update
from sqlalchemy.engine import Connection, Engine

from app.domain.nba_events import REGULAR_SEASON_TYPE, is_final_event
from app.domain.utc import assume_utc
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
    LedgerBackfillState,
    LedgerGameRowEvidence,
    LedgerObservationEvidence,
    LedgerPublication,
)
from app.models.collection_control import CollectionManifest, CollectionObservation
from app.providers.pbp_game_logs import PBP_GAME_LOG_COUNTING_COLUMNS, PBPGameLogAdapter
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.pbp_game_log_normalization import normalize_pbp_game_logs, parse_pbp_minutes
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
#: The ``team_results`` ``FullGame`` diagnostic count vocabulary an accepted raw
#: observation must carry completely.  These are the provider wire spellings of
#: the governed count primitives (``COUNT_FIELDS`` without the derived
#: ``FGM``/``FGA`` composites).  ``Points`` is independently provable from
#: retained scoring components, but every other primitive has no in-row
#: arithmetic identity, so a sparse player omission is only a governed zero
#: when the complete team total proves it can be zero.  A raw acceptance
#: missing any of these fields -- or carrying a null or malformed value --
#: cannot prove a sparse omission and rejects the candidate atomically.
LEDGER_GOVERNED_DIAGNOSTIC_COUNTS = (
    "Points",
    "FG2M", "FG2A",
    "FG3M", "FG3A",
    "FtPoints", "FTA",
    "OffRebounds", "DefRebounds", "Rebounds",
    "Assists", "Turnovers", "Steals", "Blocks", "Fouls",
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
#: The PBP FullGame wire is sparse: the provider omits observed-zero additive
#: box-score counters on both player rows and the team-summary row.  Every
#: count primitive in ``COUNT_FIELDS`` is such an additive counter, so a missing
#: or null value is a governed zero (``_integer`` collapses it) rather than
#: missing evidence.  A governed zero is only accepted when independently
#: proven: an omitted ``Points`` must reconcile arithmetically with the retained
#: scoring components and every other omitted count must stay consistent with
#: the complete ``team_results`` diagnostic reconciliation, so a missing nonzero
#: count rejects the candidate atomically.  Because that proof depends on the
#: diagnostics, an accepted raw observation must carry the ``team_results``
#: Home and Away ``FullGame`` envelopes with every governed diagnostic count
#: (``LEDGER_GOVERNED_DIAGNOSTIC_COUNTS``) present, well-formed, and reconciling
#: with the declared team authority; a missing envelope, a missing/null/malformed
#: diagnostic field, or a reconciliation failure rejects the candidate atomically.
#: Identity, minutes, row presence, and malformed values stay strict: they are
#: never zero-filled and always reject the candidate game atomically.
#: ``FGM``/``FGA``/``Rebounds`` are derived from the two-pointer and three-pointer
#: components and from offensive plus defensive rebounds.

#: The typed extractor version that produced canonical facts and raw row
#: schema evidence.  Bump it only when the extraction vocabulary changes.
LEDGER_SCHEMA_VERSION = 1
#: The complete flat ``BoxscoreItem`` vocabulary documented by the PBP Stats
#: OpenAPI for ``/get-game-stats`` (``stats.Home/Away.FullGame``).  Every
#: provider row -- including the team-summary row -- is one flat BoxscoreItem,
#: so this is the authoritative accepted provider schema rather than just the
#: narrow typed aliases the extractor projects.  It covers shot context,
#: assisted scoring, rebound opportunities, turnover and foul types, blocks,
#: second-chance and penalty facts, pace and efficiency inputs, and derived
#: rates.  Keep it and ``LEDGER_SCHEMA_VERSION`` in lockstep: bump both
#: together whenever the provider vocabulary changes.
PBP_BOXSCORE_ITEM_FIELDS = frozenset({
    # identity / minutes
    "EntityId", "TeamId", "Name", "ShortName", "Season", "RowId",
    "TeamAbbreviation", "SecondsPlayed", "GamesPlayed", "Minutes",
    "MinutesMMSS",
    # possession context
    "PlusMinus", "OffPoss", "DefPoss", "PenaltyOffPoss", "PenaltyDefPoss",
    "SecondChanceOffPoss", "TotalPoss",
    # shot context (rim / short mid / long mid / corner / arc, opponent,
    # second-chance, penalty, blocked, heaves)
    "AtRimFGM", "AtRimFGA", "OpponentAtRimFGM", "OpponentAtRimFGA",
    "SecondChanceAtRimFGM", "SecondChanceAtRimFGA",
    "PenaltyAtRimFGM", "PenaltyAtRimFGA",
    "ShortMidRangeFGM", "ShortMidRangeFGA",
    "OpponentShortMidRangeFGM", "OpponentShortMidRangeFGA",
    "SecondChanceShortMidRangeFGM", "SecondChanceShortMidRangeFGA",
    "PenaltyShortMidRangeFGM", "PenaltyShortMidRangeFGA",
    "LongMidRangeFGM", "LongMidRangeFGA",
    "OpponentLongMidRangeFGM", "OpponentLongMidRangeFGA",
    "SecondChanceLongMidRangeFGM", "SecondChanceLongMidRangeFGA",
    "PenaltyLongMidRangeFGM", "PenaltyLongMidRangeFGA",
    "Corner3FGM", "Corner3FGA", "OpponentCorner3FGM", "OpponentCorner3FGA",
    "SecondChanceCorner3FGM", "SecondChanceCorner3FGA",
    "PenaltyCorner3FGM", "PenaltyCorner3FGA",
    "Arc3FGM", "Arc3FGA", "OpponentArc3FGM", "OpponentArc3FGA",
    "SecondChanceArc3FGM", "SecondChanceArc3FGA",
    "PenaltyArc3FGM", "PenaltyArc3FGA",
    # core shooting / points
    "FG2M", "FG2A", "FG3M", "FG3A", "FGM", "FGA",
    "OpponentFG2M", "OpponentFG2A", "OpponentFG3M", "OpponentFG3A",
    "OpponentFGM", "OpponentFGA", "FtPoints", "Points", "OpponentPoints",
    "SecondChanceFG2M", "SecondChanceFG2A", "SecondChanceFG3M",
    "SecondChanceFG3A", "SecondChanceFtPoints", "SecondChancePoints",
    "PenaltyFG2M", "PenaltyFG2A", "PenaltyFG3M", "PenaltyFG3A",
    "PenaltyFtPoints", "PenaltyPoints",
    # assisted scoring and shot types
    "PtsAssisted2s", "PtsUnassisted2s", "PtsAssisted3s", "PtsUnassisted3s",
    "PtsPutbacks", "HeaveMakes", "HeaveAttempts", "NonHeaveFg3a",
    "NonHeaveFg3m", "NonHeaveArc3FGA", "NonHeaveArc3FGM",
    "Fg2aBlocked", "Fg3aBlocked",
    # assists
    "TwoPtAssists", "ThreePtAssists", "Assists", "Arc3Assists",
    "Corner3Assists", "AtRimAssists", "ShortMidRangeAssists",
    "LongMidRangeAssists", "AssistPoints",
    # rebounding detail and opportunities
    "OffThreePtRebounds", "OffTwoPtRebounds", "FTOffRebounds",
    "DefThreePtRebounds", "DefTwoPtRebounds", "FTDefRebounds",
    "DefRebounds", "OffRebounds", "Rebounds",
    "OffThreePtReboundOpportunities", "OffTwoPtReboundOpportunities",
    "DefThreePtReboundOpportunities", "DefTwoPtReboundOpportunities",
    "DefReboundOpportunities", "OffReboundOpportunities", "SelfOReb",
    # steals, turnover types, and second-chance/penalty turnover context
    "Steals", "BadPassSteals", "LostBallSteals",
    "LiveBallTurnovers", "BadPassOutOfBoundsTurnovers", "BadPassTurnovers",
    "DeadBallTurnovers", "LostBallOutOfBoundsTurnovers", "LostBallTurnovers",
    "StepOutOfBoundsTurnovers", "Travels",
    "OpponentLiveBallTurnovers", "SecondChanceLiveBallTurnovers",
    "PenaltyLiveBallTurnovers", "Turnovers", "OpponentTurnovers",
    "SecondChanceTurnovers", "PenaltyTurnovers",
    # foul types, free-throw trips, and drawn fouls
    "ShootingFouls", "BlockingFouls", "Fouls", "Charge Fouls",
    "Clear Path Fouls", "Loose Ball Fouls", "Offensive Fouls",
    "Transition Take Fouls", "FoulsDrawn", "Charge Fouls Drawn",
    "Loose Ball Fouls Drawn", "Offensive Fouls Drawn",
    "Transition Take Fouls Drawn", "BlockingFoulsDrawn",
    "FTA", "2pt And 1 Free Throw Trips", "3pt And 1 Free Throw Trips",
    "Technical Free Throw Trips", "OpponentFTA", "TwoPtShootingFoulsDrawn",
    "ThreePtShootingFoulsDrawn", "NonShootingFoulsDrawn",
    # blocks and violations
    "Blocked2s", "Blocked3s", "BlockedArc3", "BlockedAtRim",
    "BlockedCorner3", "BlockedLongMidRange", "BlockedShortMidRange",
    "Blocks", "RecoveredBlocks", "DefensiveGoaltends", "OffensiveGoaltends",
    "3SecondViolations", "Defensive 3 Seconds Violations",
    # first-chance / penalty excluding take fouls
    "FirstChancePoints", "PenaltyPointsExcludingTakeFouls",
    "PenaltyOffPossExcludingTakeFouls", "NonShootingPenaltyNonTakeFouls",
    "NonShootingPenaltyNonTakeFoulsDrawn",
    # minutes by foul situation
    "Period1Fouls0Minutes", "Period1Fouls1Minutes", "Period1Fouls2Minutes",
    "Period1Fouls3Minutes", "Period1Fouls4Minutes", "Period1Fouls5Minutes",
    "Period2Fouls0Minutes", "Period2Fouls1Minutes", "Period2Fouls2Minutes",
    "Period2Fouls3Minutes", "Period2Fouls4Minutes", "Period2Fouls5Minutes",
    "Period3Fouls0Minutes", "Period3Fouls1Minutes", "Period3Fouls2Minutes",
    "Period3Fouls3Minutes", "Period3Fouls4Minutes", "Period3Fouls5Minutes",
    "Period4Fouls0Minutes", "Period4Fouls1Minutes", "Period4Fouls2Minutes",
    "Period4Fouls3Minutes", "Period4Fouls4Minutes", "Period4Fouls5Minutes",
    "PeriodOTFouls0Minutes", "PeriodOTFouls1Minutes", "PeriodOTFouls2Minutes",
    "PeriodOTFouls3Minutes", "PeriodOTFouls4Minutes", "PeriodOTFouls5Minutes",
    # efficiency and pace inputs
    "TrueShotAttempts", "PtsPer100Poss", "PtsPer100PossOpponent",
    "SecondsPerPoss", "FirstChancePtsPer100Poss", "SecondChancePtsPer100Poss",
    "PenaltyPtsPer100Poss", "PenaltyPtsPer100PossPenalty",
    "PenaltyOffPossPer100Poss", "AssistPointsPer100Poss", "FTAPer100Poss",
    "TurnoversPer100Poss", "AssistsPer100Poss", "OnOffRtg", "OnDefRtg",
    "OnNetRtg", "Assisted2sPct", "NonPutbacksAssisted2sPct", "Assisted3sPct",
    "Fg3Pct", "FTPct", "Fg3PctOpponent", "SecondChanceFg3Pct",
    "PenaltyFg3Pct", "NonHeaveFg3Pct", "Fg2Pct", "Fg2PctOpponent",
    "SecondChanceFg2Pct", "PenaltyFg2Pct", "EfgPct", "EfgPctOpponent",
    "SecondChanceEfgPct", "PenaltyEfgPct", "TsPct", "SecondChanceTsPct",
    "PenaltyTsPct", "FG3APct", "FG3APctOpponent", "FG3APctBlocked",
    "FG2APctBlocked", "AtRimPctBlocked", "ShortMidRangePctBlocked",
    "LongMidRangePctBlocked", "Corner3PctBlocked", "Arc3PctBlocked",
    "Usage", "LiveBallTurnoverPct", "OffReboundPct", "DefReboundPct",
    "DefFTReboundPct", "OffFTReboundPct", "DefTwoPtReboundPct",
    "OffTwoPtReboundPct", "DefThreePtReboundPct", "OffThreePtReboundPct",
    "DefFGReboundPct", "OffFGReboundPct", "OffAtRimReboundPct",
    "OffShortMidRangeReboundPct", "OffLongMidRangeReboundPct",
    "OffArc3ReboundPct", "OffCorner3ReboundPct", "DefAtRimReboundPct",
    "DefShortMidRangeReboundPct", "DefLongMidRangeReboundPct",
    "DefArc3ReboundPct", "DefCorner3ReboundPct", "SelfORebPct", "Pace",
    "BlocksRecoveredPct", "SecondsPerPossOff", "SecondsPerPossDef",
    "SecondsExcludingORebsPerPossOff", "SecondsExcludingORebsPerPossDef",
    "AtRimFrequency", "AtRimAccuracy", "UnblockedAtRimAccuracy",
    "AtRimPctAssisted", "ShortMidRangeFrequency", "ShortMidRangeAccuracy",
    "UnblockedShortMidRangeAccuracy", "ShortMidRangePctAssisted",
    "LongMidRangeFrequency", "LongMidRangeAccuracy",
    "UnblockedLongMidRangeAccuracy", "LongMidRangePctAssisted",
    "Corner3Frequency", "Corner3Accuracy", "UnblockedCorner3Accuracy",
    "Corner3PctAssisted", "Arc3Frequency", "Arc3Accuracy",
    "UnblockedArc3Accuracy", "Arc3PctAssisted", "AtRimFrequencyOpponent",
    "AtRimAccuracyOpponent", "ShortMidRangeFrequencyOpponent",
    "ShortMidRangeAccuracyOpponent", "LongMidRangeFrequencyOpponent",
    "LongMidRangeAccuracyOpponent", "Corner3FrequencyOpponent",
    "Corner3AccuracyOpponent", "Arc3FrequencyOpponent", "Arc3AccuracyOpponent",
    "SecondChanceAtRimFrequency", "SecondChanceAtRimAccuracy",
    "SecondChanceAtRimPctAssisted", "SecondChanceShortMidRangeFrequency",
    "SecondChanceShortMidRangeAccuracy", "SecondChanceShortMidRangePctAssisted",
    "SecondChanceLongMidRangeFrequency", "SecondChanceLongMidRangeAccuracy",
    "SecondChanceLongMidRangePctAssisted", "SecondChanceCorner3Frequency",
    "SecondChanceCorner3Accuracy", "SecondChanceCorner3PctAssisted",
    "SecondChanceArc3Frequency", "SecondChanceArc3Accuracy",
    "SecondChanceArc3PctAssisted", "PenaltyAtRimFrequency",
    "PenaltyAtRimAccuracy", "PenaltyShortMidRangeFrequency",
    "PenaltyShortMidRangeAccuracy", "PenaltyLongMidRangeFrequency",
    "PenaltyLongMidRangeAccuracy", "PenaltyCorner3Frequency",
    "PenaltyCorner3Accuracy", "PenaltyArc3Frequency", "PenaltyArc3Accuracy",
    "AtRimFG3AFrequency", "NonHeaveArc3Accuracy", "ShotQualityAvg",
    "OpponentShotQualityAvg", "SecondChanceShotQualityAvg",
    "PenaltyShotQualityAvg", "ShootingFoulsDrawnPct",
    "TwoPtShootingFoulsDrawnPct", "ThreePtShootingFoulsDrawnPct",
    "SecondChancePointsPct", "SecondChancePtsPer100PossSecondChance",
    "SecondChanceOffPossPer100Poss",
    "SecondChancePointsPer100PossSecondChance", "PenaltyPointsPct",
    "PenaltyPossessionsPct", "Avg2ptShotDistance", "Avg3ptShotDistance",
    "AtRimOffReboundedPct", "ShortMidRangeOffReboundedPct",
    "LongMidRangeOffReboundedPct", "ThreePtOffReboundedPct",
    "PenaltyEfficiencyExcludingTakeFouls", "PenaltyOffPossPct",
})
#: The governed FullGame wire-field baseline for ``LEDGER_SCHEMA_VERSION``:
#: every provider key the extractor understands and tolerates on an archived
#: raw row.  A first raw archive (a brand-new game, or a pre-032 game
#: receiving its row evidence for the first time) is judged against this
#: baseline: a field outside it is additive schema drift that must be recorded
#: and alerted, while a normal first observation inside the baseline stays
#: silent.  The baseline is the complete documented BoxscoreItem vocabulary
#: plus the normalized aliases the extractor tolerates, so a normal provider
#: row never alerts.  Keep this set and the schema version in lockstep: bump
#: both together whenever the extraction vocabulary changes.
LEDGER_GOVERNED_FULLGAME_FIELDS = PBP_BOXSCORE_ITEM_FIELDS | frozenset({
    # identity
    "PLAYER_ID", "player_id",
    "PLAYER_NAME", "player_name",
    # minutes
    "MIN", "minutes",
    # envelope/tolerated identity columns
    "GameId", "Date", "Team", "Opponent", "TeamId", "TEAM_ID",
    # NBA-style counting aliases the extractor tolerates
    "PTS", "FTM", "OREB", "DREB", "REB", "AST", "TOV", "STL", "BLK", "PF",
    # normalized count-primitive aliases
    "field_goals_made", "field_goals_attempted",
    "two_pointers_made", "two_pointers_attempted",
    "three_pointers_made", "three_pointers_attempted",
    "free_throws_made", "free_throws_attempted",
    "offensive_rebounds", "defensive_rebounds", "rebounds",
    "assists", "turnovers", "steals", "blocks", "personal_fouls", "points",
    # normalized expanded evidence
    "two_point_assists", "three_point_assists",
    "arc3_assists", "corner3_assists",
    "at_rim_assists", "short_mid_range_assists", "long_mid_range_assists",
    "Possessions", "possessions",
})
NBA_CALENDAR_TIMEZONE = ZoneInfo("America/New_York")


class LedgerValidationError(ValueError):
    """A candidate game cannot be published without losing governed facts."""


class LedgerSchemaUnavailable(RuntimeError):
    """Migration 032 has not provisioned the Canonical Game Ledger schema."""


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
class LedgerGameRow:
    """One complete archived PBP boxscore row (team summary or player)."""

    game_id: str
    row_type: str
    side: str
    row_index: int
    entity_id: int | None
    entity_name: str | None
    team_id: int
    payload: Mapping[str, Any]
    checksum: str
    observed_fields: tuple[str, ...]
    schema_version: int = LEDGER_SCHEMA_VERSION


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
    participant_ids_by_team: tuple[tuple[int, tuple[int, ...]], ...]
    season_type: str = REGULAR_SEASON_TYPE
    status: str = "final"
    checksum: str | None = None
    raw_rows: tuple[LedgerGameRow, ...] = ()
    raw_checksum: str | None = None

    def with_checksum(self) -> CanonicalGame:
        return replace(
            self,
            checksum=game_checksum(self),
            raw_checksum=raw_checksum(self.raw_rows),
        )


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


def _governed_event_dates(value: date | datetime | str) -> frozenset[date]:
    """Return the UTC and NBA-calendar dates represented by a tipoff."""

    parsed: date | datetime
    if isinstance(value, (date, datetime)):
        parsed = value
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = _canonical_date(value)
    if not isinstance(parsed, datetime):
        return frozenset((parsed,))
    instant = assume_utc(parsed)
    return frozenset((instant.date(), instant.astimezone(NBA_CALENDAR_TIMEZONE).date()))


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


def _count_or_none(row: Mapping[str, Any], *names: str) -> int | None:
    """Read an optional count, distinguishing an absent field from a zero.

    The derived composites (``FGM``/``FGA``/``Rebounds``) are not required
    evidence, so their absence must stay absent to let the documented
    derivation apply; ``_integer`` collapses a missing value to zero.
    """

    raw = _raw_value(row, *names)
    if raw is None:
        return None
    return _integer(raw, names[0])


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


def _minutes_value(value: Any, field_name: str) -> float:
    """Read minutes evidence that the provider writes as ``MM:SS`` or numeric.

    Minutes are never zero-filled: missing or malformed evidence is a
    complete-game failure rather than an observed zero.
    """

    if isinstance(value, str):
        try:
            return parse_pbp_minutes(value)
        except Exception as error:
            raise LedgerValidationError(
                f"{field_name} must be valid MM:SS minutes evidence"
            ) from error
    return _number(value, field_name)


def _minutes_string(value: float) -> str:
    """Format numeric minutes into the provider's ``MM:SS`` wire spelling."""

    total_seconds = int(round(float(value) * 60))
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def _raw_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _canonical_json(payload: Any) -> str:
    """Serialize arbitrary evidence deterministically for stable checksums."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _canonical_json_hash(payload: Any) -> str:
    """SHA-256 over canonical JSON evidence, stable across identical replays."""

    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def canonical_row_checksum(payload: Mapping[str, Any]) -> str:
    """Deterministic per-row SHA-256 over canonical JSON provider evidence."""

    return _canonical_json_hash(payload)


def _ledger_raw_rows(
    observation: Mapping[str, Any],
    *,
    game_id: str,
    home_team_id: int,
    away_team_id: int,
) -> tuple[LedgerGameRow, ...]:
    """Archive every Home/Away FullGame row, including the team summaries.

    Provider keys and values are preserved verbatim.  Only canonical identity
    (game, side, team, entity) is recorded as metadata beside the payload; a
    ``team`` row is the provider's aggregate (``EntityId == 0`` or ``Team``)
    and carries the team-only residuals (such as team rebounds) that
    participating player rows never carry.
    """

    stats = observation.get("stats")
    if not isinstance(stats, Mapping):
        raise LedgerValidationError("PBP game observation is missing a stats envelope")
    team_by_side = {"Home": home_team_id, "Away": away_team_id}
    output: list[LedgerGameRow] = []
    for side in ("Home", "Away"):
        period = stats.get(side)
        period_rows = period.get("FullGame") if isinstance(period, Mapping) else None
        if not isinstance(period_rows, Sequence) or isinstance(period_rows, (str, bytes, bytearray)):
            raise LedgerValidationError(f"PBP FullGame rows are required for {side} evidence")
        for index, row in enumerate(period_rows):
            if not isinstance(row, Mapping):
                raise LedgerValidationError("PBP FullGame contains a malformed raw row")
            raw_id = _raw_value(row, "EntityId", "PLAYER_ID", "player_id")
            raw_name = _raw_value(row, "Name", "PLAYER_NAME", "player_name")
            if raw_id in (None, "0", 0) or str(raw_name or "") == "Team":
                row_type = "team"
                entity_id = None
                entity_name = None
            else:
                row_type = "player"
                entity_id = _integer(raw_id, "entity_id") or 0
                entity_name = _required_text(raw_name, "entity_name")
            payload = dict(row)
            output.append(
                LedgerGameRow(
                    game_id=game_id,
                    row_type=row_type,
                    side=side,
                    row_index=index,
                    entity_id=entity_id,
                    entity_name=entity_name,
                    team_id=team_by_side[side],
                    payload=payload,
                    checksum=canonical_row_checksum(payload),
                    observed_fields=tuple(sorted(payload)),
                )
            )
    if not output:
        raise LedgerValidationError("PBP game observation contains no FullGame rows")
    team_rows_by_side = {"Home": 0, "Away": 0}
    for row in output:
        if row.row_type == "team":
            team_rows_by_side[row.side] += 1
    for side, count in team_rows_by_side.items():
        if count != 1:
            raise LedgerValidationError(
                f"PBP FullGame requires exactly one team-summary row for {side} evidence"
            )
    return tuple(output)


def _verify_observation_binding(
    candidate: CanonicalGame,
    observation: Mapping[str, Any],
) -> None:
    """Cryptographically bind one accepted observation to its candidate game.

    The observation's stored payload is the complete raw provider document,
    and the candidate's archived raw rows are derived deterministically from
    that document.  A caller must therefore prove that the exact payload being
    stamped as provenance reproduces the exact raw evidence being persisted:
    replaying an observation envelope over a different raw document would
    defeat the ledger's replay auditability.  The envelope's own checksum is
    verified against its payload as well, so a tampered checksum field cannot
    survive acceptance.  The retrieval time recorded on the candidate must
    also be exactly the retrieval time the observation declares, after UTC
    normalization, so a governed caller cannot stamp a correct payload while
    recording a false retrieval time.
    """

    payload_text = str(observation.get("payload") or "")
    if hashlib.sha256(payload_text.encode()).hexdigest() != str(observation.get("checksum") or ""):
        raise LedgerValidationError(
            "accepted ledger observation checksum does not match its payload"
        )
    if assume_utc(candidate.retrieved_at) != assume_utc(observation["retrieved_at"]):
        raise LedgerValidationError(
            "accepted ledger observation retrieval time does not match the candidate"
        )
    try:
        document = json.loads(payload_text)
    except (TypeError, ValueError) as error:
        raise LedgerValidationError("accepted ledger observation payload is malformed") from error
    if not isinstance(document, Mapping):
        raise LedgerValidationError(
            "accepted ledger observation payload is not a raw game document"
        )
    try:
        recomputed = _ledger_raw_rows(
            document,
            game_id=candidate.game_id,
            home_team_id=candidate.home_team_id,
            away_team_id=candidate.away_team_id,
        )
    except LedgerValidationError as error:
        raise LedgerValidationError(
            "accepted ledger observation payload cannot reproduce the candidate evidence"
        ) from error
    if raw_checksum(recomputed) != candidate.raw_checksum:
        raise LedgerValidationError(
            "accepted ledger observation is not bound to the candidate raw evidence"
        )


#: Explicit canonical raw-row side order shared by hashing, persistence, and
#: reload.  Home rows always precede Away rows regardless of lexicographic
#: ordering; within each side rows keep their provider array index order.
_RAW_ROW_SIDE_ORDER = {"Home": 0, "Away": 1}


def _raw_row_order(row: LedgerGameRow) -> tuple[int, int]:
    """Canonical raw-row sort key (side order first, then within-side index)."""

    return (_RAW_ROW_SIDE_ORDER.get(row.side, len(_RAW_ROW_SIDE_ORDER)), row.row_index)


def _side_order_expression(column: Any):
    """SQL expression mapping a raw-row side to its canonical priority.

    Derived from the same ``_RAW_ROW_SIDE_ORDER`` mapping used for hashing and
    Python-side reload sorting, so the database query and the checksum sort can
    never drift.  Unknown sides sort after every declared side, matching
    ``_raw_row_order``.
    """

    return case(
        *[
            (column == side, priority)
            for side, priority in sorted(
                _RAW_ROW_SIDE_ORDER.items(), key=lambda item: item[1]
            )
        ],
        else_=len(_RAW_ROW_SIDE_ORDER),
    )


def raw_checksum(raw_rows: Iterable[LedgerGameRow]) -> str | None:
    """Hash the complete archived raw evidence set for one game.

    Rows are hashed in the canonical order (Home side, then Away side, each by
    provider row index), so the checksum is stable across persistence, reload,
    and any provider re-fetch replay regardless of the order rows arrive in.
    """

    rows = sorted(tuple(raw_rows), key=_raw_row_order)
    if not rows:
        return None
    payload = [
        {
            "game_id": row.game_id,
            "row_type": row.row_type,
            "side": row.side,
            "row_index": row.row_index,
            "entity_id": row.entity_id,
            "entity_name": row.entity_name,
            "team_id": row.team_id,
            "observed_fields": row.observed_fields,
            "payload": row.payload,
        }
        for row in rows
    ]
    return _canonical_json_hash(payload)


_PLAYER_ASSIST_ROW_KEYS = (
    ("two_point_assists", "TwoPtAssists"),
    ("three_point_assists", "ThreePtAssists"),
    ("arc3_assists", "Arc3Assists"),
    ("corner3_assists", "Corner3Assists"),
    ("at_rim_assists", "AtRimAssists"),
    ("short_mid_range_assists", "ShortMidRangeAssists"),
    ("long_mid_range_assists", "LongMidRangeAssists"),
)


def _fact_count_payload(fact: Any) -> dict[str, Any]:
    """Map typed count primitives onto their provider FullGame wire spellings.

    Team-summary and player typed facts carry the same count vocabulary, so one
    helper owns the count-to-wire spelling; the row-type authority (which row
    drives which typed fact) stays in the callers.
    """

    return {
        "Points": fact.points,
        "FGM": fact.field_goals_made,
        "FGA": fact.field_goals_attempted,
        "FG2M": fact.two_pointers_made,
        "FG2A": fact.two_pointers_attempted,
        "FG3M": fact.three_pointers_made,
        "FG3A": fact.three_pointers_attempted,
        "FtPoints": fact.free_throws_made,
        "FTA": fact.free_throws_attempted,
        "OffRebounds": fact.offensive_rebounds,
        "DefRebounds": fact.defensive_rebounds,
        "Rebounds": fact.rebounds,
        "Assists": fact.assists,
        "Turnovers": fact.turnovers,
        "Steals": fact.steals,
        "Blocks": fact.blocks,
        "Fouls": fact.personal_fouls,
    }


def raw_rows_from_facts(game: CanonicalGame) -> tuple[LedgerGameRow, ...]:
    """Build coherent raw FullGame evidence from retained typed facts.

    Synthetic repaired games and test corrections reuse this helper so the
    archived raw rows always extract back to exactly the typed facts they
    describe; the repository boundary then accepts them like any accepted
    observation.  Optional fields are emitted only when the typed fact carries
    a value, so an absent assist-location observation stays absent.
    """

    rows: list[LedgerGameRow] = []
    team_facts_by_id = {fact.team_id: fact for fact in game.team_facts}
    players_by_side = {
        "Home": tuple(player for player in game.player_facts if player.team_id == game.home_team_id),
        "Away": tuple(player for player in game.player_facts if player.team_id == game.away_team_id),
    }
    for team_id, side in ((game.home_team_id, "Home"), (game.away_team_id, "Away")):
        team_fact = team_facts_by_id[team_id]
        side_players = players_by_side[side]
        payload: dict[str, Any] = {
            "EntityId": "0",
            "Name": "Team",
            "Minutes": "00:00",
        }
        # The archived team-summary row carries only the team-only residual for
        # each count primitive (the portion no player is credited with, such as
        # team rebounds), matching the sparse provider row.  Extraction re-adds
        # the participating-player sum, so the residual reproduces the typed
        # team fact exactly.
        player_sums = {
            field_name: sum(getattr(player, field_name) for player in side_players)
            for field_name in COUNT_FIELDS
            if field_name != "rebounds"
        }
        player_sums["rebounds"] = sum(player.rebounds for player in side_players)
        for field_name, names in _TEAM_ROW_ALIASES.items():
            residual = getattr(team_fact, field_name) - player_sums[field_name]
            if residual:
                payload[names[0]] = residual
        if team_fact.possessions is not None:
            payload["Possessions"] = team_fact.possessions
        rows.append(LedgerGameRow(
            game_id=game.game_id,
            row_type="team",
            side=side,
            row_index=0,
            entity_id=None,
            entity_name=None,
            team_id=team_id,
            payload=payload,
            checksum=canonical_row_checksum(payload),
            observed_fields=tuple(sorted(payload)),
        ))
        for index, player in enumerate(players_by_side[side], start=1):
            payload = {
                "EntityId": str(player.player_id),
                "Name": player.player_name,
                "Minutes": _minutes_string(player.minutes),
                **_fact_count_payload(player),
            }
            if player.possessions is not None:
                payload["Possessions"] = player.possessions
            for field_name, key in _PLAYER_ASSIST_ROW_KEYS:
                value = getattr(player, field_name)
                if value is not None:
                    payload[key] = value
            rows.append(LedgerGameRow(
                game_id=game.game_id,
                row_type="player",
                side=side,
                row_index=index,
                entity_id=player.player_id,
                entity_name=player.player_name,
                team_id=player.team_id,
                payload=payload,
                checksum=canonical_row_checksum(payload),
                observed_fields=tuple(sorted(payload)),
            ))
    return tuple(rows)


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


def _assert_missing_points_is_a_governed_zero(row: Mapping[str, Any]) -> None:
    """Reject a sparse Points omission that retained scoring evidence proves nonzero.

    The PBP FullGame wire omits observed-zero additive counters, so a player
    row that omits ``Points`` is normally a governed zero.  That is only valid
    when the retained scoring components (``FG2M``/``FG3M``/``FtPoints``)
    independently prove the total is zero: points must equal
    ``2 * FG2M + 3 * FG3M + FtPoints``.  A row that omits ``Points`` while its
    makes prove a nonzero total is corrupted evidence, not a sparse zero, and
    rejects the whole candidate atomically.  A counter that is independently
    proven to be zero stays accepted as a governed zero.
    """

    if _raw_value(row, "Points", "PTS", "points") is not None:
        return
    derived_points = (
        2 * _integer(_raw_value(row, "FG2M", "two_pointers_made"), "FG2M")
        + 3 * _integer(_raw_value(row, "FG3M", "three_pointers_made"), "FG3M")
        + _integer(_raw_value(row, "FtPoints", "FTM", "free_throws_made"), "FTM")
    )
    if derived_points != 0:
        raise LedgerValidationError(
            "player row missing nonzero points evidence contradicted by scoring components"
        )


def _player_fact_from_row(
    row: Mapping[str, Any],
    *,
    team_id: int,
    team_tricode: str,
) -> PlayerGameFact:
    """Map a PBP player row while retaining the stable primitive superset.

    The PBP FullGame wire is sparse: observed-zero additive box-score counters
    are omitted from player rows, so a missing or null count is a governed zero
    and the game still ingests.  Identity and minutes remain strict -- a row
    missing ``EntityId``/``Name``/``Minutes`` evidence, or carrying a malformed
    value, rejects the whole candidate atomically.  A missing count is only a
    governed zero when independent arithmetic (``Points`` against its scoring
    components) or the complete diagnostic ``team_results`` reconciliation
    proves it can be zero; a missing count that retained evidence proves is
    nonzero is corrupted evidence and rejects the candidate atomically.
    """

    _assert_missing_points_is_a_governed_zero(row)
    two_made = _integer(_raw_value(row, "FG2M", "two_pointers_made"), "FG2M")
    two_att = _integer(_raw_value(row, "FG2A", "two_pointers_attempted"), "FG2A")
    three_made = _integer(_raw_value(row, "FG3M", "three_pointers_made"), "FG3M")
    three_att = _integer(_raw_value(row, "FG3A", "three_pointers_attempted"), "FG3A")
    fgm = _count_or_none(row, "FGM", "field_goals_made")
    fga = _count_or_none(row, "FGA", "field_goals_attempted")
    fgm = two_made + three_made if fgm is None else fgm
    fga = two_att + three_att if fga is None else fga
    ftm = _integer(_raw_value(row, "FtPoints", "FTM", "free_throws_made"), "FTM")
    fta = _integer(_raw_value(row, "FTA", "free_throws_attempted"), "FTA")
    oreb = _integer(_raw_value(row, "OffRebounds", "OREB", "offensive_rebounds"), "OREB")
    dreb = _integer(_raw_value(row, "DefRebounds", "DREB", "defensive_rebounds"), "DREB")
    reb = _count_or_none(row, "Rebounds", "REB", "rebounds")
    if reb is None:
        reb = oreb + dreb
    return PlayerGameFact(
        player_id=_integer(_raw_value(row, "EntityId", "PLAYER_ID", "player_id"), "player_id") or 0,
        player_name=_required_text(_raw_value(row, "Name", "PLAYER_NAME", "player_name"), "player_name"),
        team_id=team_id,
        team_tricode=team_tricode,
        minutes=_minutes_value(_raw_value(row, "MIN", "Minutes", "minutes"), "minutes") or 0.0,
        points=_integer(_raw_value(row, "Points", "PTS", "points"), "points"),
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
        assists=_integer(_raw_value(row, "Assists", "AST", "assists"), "assists"),
        turnovers=_integer(_raw_value(row, "Turnovers", "TOV", "turnovers"), "turnovers"),
        steals=_integer(_raw_value(row, "Steals", "STL", "steals"), "steals"),
        blocks=_integer(_raw_value(row, "Blocks", "BLK", "blocks"), "blocks"),
        personal_fouls=_integer(_raw_value(row, "Fouls", "PF", "personal_fouls"), "personal_fouls"),
        two_point_assists=_integer(_raw_value(row, "TwoPtAssists", "two_point_assists"), "two_point_assists", nullable=True),
        three_point_assists=_integer(_raw_value(row, "ThreePtAssists", "three_point_assists"), "three_point_assists", nullable=True),
        arc3_assists=_integer(_raw_value(row, "Arc3Assists", "arc3_assists"), "arc3_assists", nullable=True),
        corner3_assists=_integer(_raw_value(row, "Corner3Assists", "corner3_assists"), "corner3_assists", nullable=True),
        at_rim_assists=_integer(_raw_value(row, "AtRimAssists", "at_rim_assists"), "at_rim_assists", nullable=True),
        short_mid_range_assists=_integer(_raw_value(row, "ShortMidRangeAssists", "short_mid_range_assists"), "short_mid_range_assists", nullable=True),
        long_mid_range_assists=_integer(_raw_value(row, "LongMidRangeAssists", "long_mid_range_assists"), "long_mid_range_assists", nullable=True),
        possessions=_number(_raw_value(row, "Possessions", "possessions"), "possessions", nullable=True),
    )


_TEAM_ROW_ALIASES = {
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


def _sum_team_facts(
    team_id: int,
    team_tricode: str,
    opponent_team_id: int,
    opponent_team_tricode: str,
    is_home: bool,
    players: Sequence[PlayerGameFact],
    team_row: Mapping[str, Any] | None = None,
    provider_total: Mapping[str, Any] | None = None,
    *,
    require_team_row: bool = False,
) -> TeamGameFact:
    """Build the team-game fact set from player rows plus team-only evidence.

    The real PBP ``/get-game-stats`` team-summary row (``EntityId == 0``) is
    sparse: it does **not** carry the complete traditional box score.  It
    carries the team-only residuals -- the rebound credits (and the occasional
    dead-ball/team turnover) that no player is credited with.  The complete
    team count for every primitive is therefore the sum of the participating
    player rows (the player rows are their own authority) plus that
    team-summary residual; both are sparse, so an omitted additive counter is
    an observed zero.  ``provider_total`` (the ``team_results`` envelope) is
    diagnostic and corruption-evidence only: an accepted raw observation
    requires the complete governed diagnostic count vocabulary present,
    well-formed, and reconciling with the declared team authority, and the
    diagnostics never populate a typed fact.  ``require_team_row`` is set for
    accepted raw observations: a complete game may never omit a side's
    team-summary row.
    """

    if require_team_row and team_row is None:
        raise LedgerValidationError(
            "accepted PBP evidence requires a team-summary row for every governed side"
        )
    total = provider_total or {}
    player_sums = {
        field_name: sum(getattr(player, field_name) for player in players)
        for field_name in COUNT_FIELDS
        if field_name != "rebounds"
    }
    player_sums["rebounds"] = sum(player.rebounds for player in players)
    if team_row is None:
        # Legacy projected-data seam (no team-summary row is available).
        values: dict[str, int] = dict(player_sums)
    else:
        values = {}
        for field_name, names in _TEAM_ROW_ALIASES.items():
            raw = _raw_value(team_row, *names)
            # A sparse team-summary omission is an observed zero residual, so
            # the complete team value is the participating-player sum.
            residual = 0 if raw is None else _integer(raw, field_name)
            values[field_name] = player_sums[field_name] + residual
    # Team-results payloads are diagnostic and corruption-evidence only: they
    # never populate a typed fact, and the complete team total is the declared
    # authority.  An accepted raw observation supplies the full diagnostic
    # envelope, so every governed diagnostic count must be present, well-formed,
    # and reconcile with that authority; a missing, null, or malformed field
    # cannot prove a sparse omission and rejects the candidate atomically.
    if provider_total is not None:
        for wire_name in LEDGER_GOVERNED_DIAGNOSTIC_COUNTS:
            if total.get(wire_name) is None:
                raise LedgerValidationError(
                    f"team_results diagnostics are missing the {wire_name} count"
                )
    # An explicitly published provider total must agree rather than overwriting
    # the declared authority.
    for field_name, names in _TEAM_ROW_ALIASES.items():
        raw = _raw_value(total, *names)
        if raw is not None:
            diagnostic = _integer(raw, field_name) or 0
            if diagnostic != values[field_name]:
                raise LedgerValidationError(
                    f"PBP team aggregate {field_name} does not reconcile with the declared authority"
                )
    # Possessions is an optional team-summary row value like any other typed
    # team fact: it is never sourced from the diagnostic team_results envelope,
    # so the persisted fact always equals extraction from the raw row.
    possession_value = (
        _raw_value(team_row, "Possessions", "possessions")
        if team_row is not None
        else None
    )
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
        possessions=_number(possession_value, "possessions", nullable=True),
    )


def _participant_evidence(
    observation: Mapping[str, Any] | None,
    *,
    participant_ids_by_team: Mapping[int, Iterable[int]] | None,
    home_team_id: int,
    away_team_id: int,
) -> dict[int, set[int]]:
    """Normalize the provider's complete game-participant roster.

    FullGame box-score rows cannot prove they were not truncated.  A separate
    roster/participant observation is therefore mandatory and must match the
    retained rows exactly, including zero-minute participants.
    """

    supplied = participant_ids_by_team
    if supplied is None and observation is not None:
        raw = observation.get("participant_ids_by_team")
        if isinstance(raw, Mapping):
            supplied = {
                int(team_id): tuple(values)
                for team_id, values in raw.items()
                if isinstance(values, Iterable) and not isinstance(values, (str, bytes, bytearray))
            }
    if supplied is None:
        raise LedgerValidationError("provider participant evidence is required for a complete game")
    expected_teams = {home_team_id, away_team_id}
    normalized: dict[int, set[int]] = {}
    for raw_team_id, raw_players in supplied.items():
        team_id = _integer(raw_team_id, "participant_team_id") or 0
        if team_id not in expected_teams:
            raise LedgerValidationError("participant evidence contains a team outside the game")
        player_ids = {
            _integer(player_id, "participant_player_id") or 0
            for player_id in raw_players
        }
        if not player_ids or 0 in player_ids:
            raise LedgerValidationError("participant evidence contains an invalid player identity")
        normalized[team_id] = player_ids
    if set(normalized) != expected_teams:
        raise LedgerValidationError("participant evidence must cover both exact game teams")
    return normalized


def canonical_game_from_pbp(
    observation: Any,
    *,
    event: Mapping[str, Any],
    season: str | None = None,
    source_observation_id: str | None = None,
    retrieved_at: datetime | None = None,
    participant_ids_by_team: Mapping[int, Iterable[int]] | None = None,
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
    governed_dates = _governed_event_dates(
        event.get("scheduled_at") or (raw_observation or {}).get("date")
    )
    observed_dates = {
        _canonical_date(normalized_row.get("GAME_DATE"))
        for normalized_row in normalized.to_dict(orient="records")
    }
    if len(observed_dates) != 1 or not observed_dates.issubset(governed_dates):
        raise LedgerValidationError("PBP player date contradicts the governed event")
    expected_game_date = next(iter(observed_dates))

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
        wire = raw_by_player.get((player_id, team_code), {})
        # The raw provider row preserves the sparse wire omissions (and any
        # retained optional fields such as assist locations); the normalized
        # row zero-fills the additive counting columns.  Both spellings agree
        # on a governed zero, so the merged row drives typed extraction.  The
        # sparse wire itself is checked first so a row omitting a count that
        # its retained components prove nonzero fails at intake rather than
        # being masked by the normalized zero-fill.
        if wire:
            _assert_missing_points_is_a_governed_zero(wire)
        raw = {**wire, **row}
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
    raw_rows: tuple[LedgerGameRow, ...] = ()
    team_row_by_team: dict[int, Mapping[str, Any]] = {}
    if raw_observation is not None:
        raw_rows = _ledger_raw_rows(
            raw_observation,
            game_id=game_id,
            home_team_id=home_id,
            away_team_id=away_id,
        )
        team_row_by_team = {
            row.team_id: row.payload
            for row in raw_rows
            if row.row_type == "team"
        }
    participant_evidence = _participant_evidence(
        raw_observation,
        participant_ids_by_team=participant_ids_by_team,
        home_team_id=home_id,
        away_team_id=away_id,
    )
    observed_participants = {
        home_id: {player.player_id for player in home_players},
        away_id: {player.player_id for player in away_players},
    }
    if participant_evidence != observed_participants:
        raise LedgerValidationError(
            "FullGame player facts must exactly match provider participant evidence"
        )

    team_result_map: dict[int, Mapping[str, Any]] = {}
    for side, team_id in (("Home", home_id), ("Away", away_id)):
        result = None
        if isinstance(team_results, Mapping):
            value = team_results.get(side)
            if isinstance(value, Mapping) and isinstance(value.get("FullGame"), Mapping):
                result = value["FullGame"]
        # Sparse zero acceptance depends on the complete diagnostics, so an
        # accepted raw observation must carry the team_results FullGame
        # envelope for every governed side.  A missing envelope leaves a sparse
        # omission unprovable and rejects the candidate atomically.
        if raw_observation is not None and not isinstance(result, Mapping):
            raise LedgerValidationError(
                "accepted PBP evidence requires the team_results diagnostic "
                f"envelope for {side}"
            )
        if result is not None:
            result_team_id = _raw_value(result, "TeamId", "team_id", "TEAM_ID")
            if result_team_id is not None and _integer(result_team_id, "team_id") != team_id:
                raise LedgerValidationError("PBP team totals contradict the governed event")
            team_result_map[team_id] = result
    teams = (
        _sum_team_facts(
            home_id, home_code, away_id, away_code, True,
            home_players, team_row_by_team.get(home_id), team_result_map.get(home_id),
            require_team_row=raw_observation is not None,
        ),
        _sum_team_facts(
            away_id, away_code, home_id, home_code, False,
            away_players, team_row_by_team.get(away_id), team_result_map.get(away_id),
            require_team_row=raw_observation is not None,
        ),
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
        participant_ids_by_team=tuple(
            (team_id, tuple(sorted(player_ids)))
            for team_id, player_ids in sorted(participant_evidence.items())
        ),
        raw_rows=raw_rows,
        raw_checksum=raw_checksum(raw_rows),
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
            "participant_ids_by_team": game.participant_ids_by_team,
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


def _reconcile_raw_and_typed_evidence(
    game: CanonicalGame,
    players_by_team: Mapping[int, Sequence[PlayerGameFact]],
) -> None:
    """Prove every stored typed fact equals extraction from its raw authority.

    The archived raw rows are the single source of truth: each typed player
    fact must equal extraction from its archived player row and each typed
    team fact must equal extraction from its team-summary row combined with
    the participating-player sums (the team-summary row supplies the team-only
    residual).  This also enforces the player/team identity, minutes, and
    presence evidence at the repository boundary, so a direct
    ``replace_game`` caller can never persist an incomplete or mixed raw/typed
    version.
    """

    tricode_by_team = {
        game.home_team_id: game.home_team_tricode,
        game.away_team_id: game.away_team_tricode,
    }
    raw_player_facts: dict[tuple[int, int], PlayerGameFact] = {}
    raw_team_rows: dict[int, LedgerGameRow] = {}
    for row in game.raw_rows:
        if row.row_type == "team":
            raw_team_rows[row.team_id] = row
            continue
        raw_player_facts[(row.team_id, row.entity_id or 0)] = _player_fact_from_row(
            row.payload,
            team_id=row.team_id,
            team_tricode=tricode_by_team[row.team_id],
        )
    typed_player_facts = {
        (fact.team_id, fact.player_id): fact for fact in game.player_facts
    }
    if set(raw_player_facts) != set(typed_player_facts):
        raise LedgerValidationError("typed player facts must match raw player evidence")
    for key, extracted in raw_player_facts.items():
        if extracted != typed_player_facts[key]:
            raise LedgerValidationError("typed player facts must match raw player evidence")
    team_facts_by_team = {fact.team_id: fact for fact in game.team_facts}
    for team_id, row in raw_team_rows.items():
        team_fact = team_facts_by_team[team_id]
        extracted = _sum_team_facts(
            team_id,
            team_fact.team_tricode,
            team_fact.opponent_team_id,
            team_fact.opponent_team_tricode,
            team_fact.is_home,
            players_by_team[team_id],
            row.payload,
            require_team_row=True,
        )
        if extracted != team_fact:
            raise LedgerValidationError(
                "typed team facts must match raw team-summary evidence"
            )


def _validate_team_summary_minutes(payload: Mapping[str, Any]) -> None:
    """Validate team-summary minutes evidence without treating it as additive.

    Minutes presence and accepted format are required on every archived row at
    the repository boundary, including the team-summary rows.  Team minutes are
    a provider team-summary field and are never compared against player
    minutes or player totals.
    """

    raw = _raw_value(payload, "Minutes", "MIN", "minutes")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise LedgerValidationError("team-summary row is missing minutes evidence")
    _minutes_value(raw, "team minutes")


def _validate_raw_row_identity(row: LedgerGameRow) -> None:
    """Reconcile archived row metadata against the provider payload identity.

    The provider's identity rules are the same ones ``_ledger_raw_rows`` uses:
    a row is a team summary when its payload ``EntityId`` is ``0``/``None`` or
    its ``Name`` is exactly ``Team``.  A team-summary row must therefore carry a
    team-identified payload and no player identity metadata, and a player row
    must carry a player-identified payload whose provider identity and name
    equal its archived metadata (the typed fact reconciliation then proves the
    retained player fact matches).
    """

    payload_id = _raw_value(row.payload, "EntityId", "PLAYER_ID", "player_id")
    payload_name = _raw_value(row.payload, "Name", "PLAYER_NAME", "player_name")
    is_team_payload = payload_id in (None, "0", 0) or str(payload_name or "") == "Team"
    if row.row_type == "team":
        if not is_team_payload or row.entity_id is not None or row.entity_name is not None:
            raise LedgerValidationError(
                "team-summary row metadata contradicts the provider payload identity"
            )
        return
    if is_team_payload:
        raise LedgerValidationError(
            "player row metadata contradicts the provider payload identity"
        )
    canonical_id = _integer(payload_id, "entity_id")
    canonical_name = _required_text(payload_name, "entity_name")
    if row.entity_id != canonical_id or row.entity_name != canonical_name:
        raise LedgerValidationError(
            "player row metadata must match the provider payload identity"
        )


def validate_complete_game(game: CanonicalGame) -> CanonicalGame:
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
    observed_by_team: dict[int, set[int]] = {game.home_team_id: set(), game.away_team_id: set()}
    for fact in game.player_facts:
        _validate_count_primitives(fact, label="player_fact")
        key = (fact.team_id, fact.player_id)
        if key in player_ids:
            raise LedgerValidationError("a player may appear once per team-game")
        if fact.player_id in global_player_ids:
            raise LedgerValidationError("a player may belong to only one team in a game")
        player_ids.add(key)
        global_player_ids.add(fact.player_id)
        if fact.team_id not in observed_by_team:
            raise LedgerValidationError("player fact is outside the game identity")
        expected_tricode, _ = team_identity[fact.team_id]
        if fact.team_tricode != expected_tricode:
            raise LedgerValidationError("player fact has a contradictory team tricode")
        observed_by_team[fact.team_id].add(fact.player_id)
    if (
        not isinstance(game.participant_ids_by_team, Sequence)
        or len(game.participant_ids_by_team) != 2
    ):
        raise LedgerValidationError("a complete game requires participant evidence for two teams")
    participant_evidence: dict[int, set[int]] = {}
    for team_id, participant_ids in game.participant_ids_by_team:
        ids = tuple(participant_ids)
        if team_id in participant_evidence or len(ids) != len(set(ids)):
            raise LedgerValidationError("participant evidence contains duplicate identities")
        if (
            isinstance(team_id, bool)
            or not isinstance(team_id, int)
            or any(isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0 for player_id in ids)
        ):
            raise LedgerValidationError("participant evidence contains invalid identities")
        participant_evidence[team_id] = set(ids)
    if participant_evidence != observed_by_team:
        raise LedgerValidationError(
            "player facts must exactly match provider participant evidence"
        )
    if (
        not isinstance(game.raw_rows, Sequence)
        or isinstance(game.raw_rows, (str, bytes, bytearray))
    ):
        raise LedgerValidationError("raw evidence must be a sequence of archived rows")
    if not game.raw_rows:
        raise LedgerValidationError("accepted raw evidence is required for a complete game")
    seen_raw_rows: set[tuple[object, ...]] = set()
    raw_player_rows: dict[int, int] = {}
    team_rows_by_side: dict[str, int] = {"Home": 0, "Away": 0}
    row_indices_by_side: dict[str, list[int]] = {"Home": [], "Away": []}
    for row in game.raw_rows:
        if not isinstance(row, LedgerGameRow):
            raise LedgerValidationError("raw evidence contains an invalid archived row")
        if (
            isinstance(row.row_index, bool)
            or not isinstance(row.row_index, int)
            or row.row_index < 0
        ):
            raise LedgerValidationError("raw evidence row_index must be a non-negative integer")
        # Provider FullGame array positions are unique per side regardless of
        # row type.  Enforcing one archived row per (side, row_index) keeps the
        # canonical raw-row order and raw_checksum deterministic.
        row_key = (row.game_id, row.side, row.row_index)
        if row_key in seen_raw_rows:
            raise LedgerValidationError(
                "raw evidence must contain exactly one archived row per side and provider index"
            )
        seen_raw_rows.add(row_key)
        if row.game_id != game.game_id:
            raise LedgerValidationError("raw evidence game identity contradicts the game")
        if row.row_type not in {"team", "player"} or row.side not in {"Home", "Away"}:
            raise LedgerValidationError("raw evidence has an invalid row type or side")
        row_indices_by_side[row.side].append(row.row_index)
        if row.team_id not in {game.home_team_id, game.away_team_id}:
            raise LedgerValidationError("raw evidence team is outside the game identity")
        expected_side = "Home" if row.team_id == game.home_team_id else "Away"
        if row.side != expected_side:
            raise LedgerValidationError("raw evidence side contradicts its team identity")
        if not isinstance(row.payload, Mapping):
            raise LedgerValidationError("raw evidence payload must be a mapping")
        if (
            not row.observed_fields
            or tuple(row.observed_fields) != tuple(sorted(row.payload))
        ):
            raise LedgerValidationError("raw evidence observed fields must match the payload")
        if row.checksum != canonical_row_checksum(row.payload):
            raise LedgerValidationError("raw evidence checksum does not match the payload")
        _validate_raw_row_identity(row)
        if row.row_type == "team":
            team_rows_by_side[row.side] += 1
            _validate_team_summary_minutes(row.payload)
        else:
            if row.entity_id is None or row.entity_id in raw_player_rows:
                raise LedgerValidationError("raw player evidence has an invalid entity identity")
            raw_player_rows[row.entity_id] = row.team_id
    if team_rows_by_side != {"Home": 1, "Away": 1}:
        raise LedgerValidationError(
            "raw evidence must contain exactly one team-summary row per side"
        )
    for side, indices in row_indices_by_side.items():
        if set(indices) != set(range(len(indices))):
            raise LedgerValidationError(
                f"raw evidence {side} rows must occupy contiguous provider indices 0..{len(indices) - 1}"
            )
    observed_raw_players = {
        team_id: {entity_id for entity_id, team in raw_player_rows.items() if team == team_id}
        for team_id in observed_by_team
    }
    if observed_raw_players != observed_by_team:
        raise LedgerValidationError(
            "raw player evidence must exactly match the retained player set"
        )
    if not isinstance(game.raw_checksum, str) or game.raw_checksum != raw_checksum(game.raw_rows):
        raise LedgerValidationError("raw evidence checksum does not match archived rows")
    players_by_team = {
        team_id: tuple(fact for fact in game.player_facts if fact.team_id == team_id)
        for team_id in observed_by_team
    }
    _reconcile_raw_and_typed_evidence(game, players_by_team)
    team_row_payload = {
        team_id: next(
            row.payload for row in game.raw_rows
            if row.row_type == "team" and row.team_id == team_id
        )
        for team_id in observed_by_team
    }
    for team_fact in game.team_facts:
        team_players = players_by_team[team_fact.team_id]
        # Every complete team count is the participating-player sum plus the
        # team-summary row's team-only residual (rebounds and the occasional
        # dead-ball/team turnover no player is credited with).  Both are
        # sparse: an omitted additive counter is an observed zero, so the raw
        # archived team row is the residual authority and never a fallback.
        for field_name in COUNT_FIELDS:
            raw = _raw_value(team_row_payload[team_fact.team_id], *_TEAM_ROW_ALIASES[field_name])
            residual = 0 if raw is None else _integer(raw, field_name)
            expected = sum(
                getattr(player, field_name) for player in team_players
            ) + residual
            if getattr(team_fact, field_name) != expected:
                raise LedgerValidationError(
                    f"team_fact.{field_name} must reconcile with player primitives and team-only evidence"
                )
        expected_minutes = sum(player.minutes for player in team_players) / 5.0
        if not math.isclose(team_fact.team_minutes, expected_minutes, abs_tol=1e-9):
            raise LedgerValidationError(
                "team_fact.team_minutes must reconcile with player minutes"
            )
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


def record_schema_drift(connection: Connection, drift: Mapping[str, object]) -> None:
    """Record one additive schema-drift correction as an operator alert.

    The reconciliation row is written inside the ledger correction transaction,
    so the drift alert commits atomically with the replacement evidence it
    describes and is surfaced through the control-plane reconciliation queue.
    """

    from app.models.collection_control import ReconciliationItem

    details = dict(drift.get("details") or {})
    game_id = str(drift.get("game_id") or "").strip()
    if game_id:
        details["game_id"] = game_id
    connection.execute(ReconciliationItem.__table__.insert().values(
        item_id=str(uuid.uuid4()),
        season=str(drift.get("season") or "")[:7],
        kind=str(drift.get("kind") or "schema_drift")[:64],
        reason=str(drift.get("reason") or "field_set_change")[:64],
        details=json.dumps(details, sort_keys=True, separators=(",", ":")),
        status="open",
        created_at=datetime.now(timezone.utc),
    ))


class CanonicalGameLedgerRepository:
    """Temporary-DB friendly atomic repository for complete games and progress."""

    def __init__(
        self,
        engine: Engine,
        *,
        correction_sink: Callable[[Connection, CanonicalGame], None] | None = None,
        schema_drift_sink: Callable[[Connection, Mapping[str, object]], None] | None = None,
    ) -> None:
        if is_demo_database_url(str(engine.url)):
            raise ValueError("the demo database cannot store ledger facts")
        self.engine = engine
        self.correction_sink = correction_sink
        self.schema_drift_sink = schema_drift_sink
        required = {
            CanonicalGameLedgerGame.__tablename__,
            CanonicalGameLedgerTeamFact.__tablename__,
            CanonicalGameLedgerPlayerFact.__tablename__,
            LedgerGameRowEvidence.__tablename__,
            LedgerBackfillState.__tablename__,
            LedgerPublication.__tablename__,
            LedgerObservationEvidence.__tablename__,
        }
        missing = sorted(required - set(inspect(engine).get_table_names()))
        if missing:
            raise LedgerSchemaUnavailable(
                "Canonical Game Ledger schema is unavailable; apply migration 032 "
                "and the latest migrations (including 033) before constructing the "
                "repository "
                f"(missing: {', '.join(missing)})"
            )

    def replace_game(self, game: CanonicalGame) -> LedgerWriteResult:
        """Insert or atomically replace one complete game.

        The transaction deletes old team/player facts only after the new game
        has passed all validation.  Any SQLAlchemy error rolls back the whole
        game, including the checksum identity record.
        """

        candidate = validate_complete_game(game)
        tables = self._tables()
        with self.engine.begin() as connection:
            return self._replace_candidate(connection, candidate, tables)

    def replace_games_atomic(
        self,
        games: Iterable[CanonicalGame],
        *,
        accepted_observations: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[LedgerWriteResult, ...]:
        """Validate every game before one transaction replaces all candidates."""

        candidates = tuple(
            validate_complete_game(game)
            for game in games
        )
        if not candidates:
            return ()
        if len({game.game_id for game in candidates}) != len(candidates):
            raise LedgerValidationError("one atomic batch cannot contain duplicate game identities")
        tables = self._tables()
        results: list[LedgerWriteResult] = []
        with self.engine.begin() as connection:
            if accepted_observations is not None:
                expected_observations = {game.source_observation_id for game in candidates}
                if set(accepted_observations) != expected_observations:
                    raise LedgerValidationError("accepted observations must exactly match candidate games")
                self._lock_manifest_authorization(
                    connection,
                    candidates,
                    accepted_observations,
                )
            for candidate in candidates:
                # An accepted candidate is ordered while its canonical game
                # row is locked.  The lock must cover the ordering decision,
                # observation insert, complete replacement, and correction
                # queue write; otherwise a late correction can insert an
                # observation and then be overwritten by an older arrival.
                if accepted_observations is not None:
                    existing = connection.execute(select(tables["game"]).where(
                        tables["game"].c.game_id == candidate.game_id,
                    ).with_for_update()).mappings().one_or_none()
                    if existing is not None:
                        if self._is_idempotent_replay(existing, candidate):
                            results.append(LedgerWriteResult(
                                candidate.game_id,
                                candidate.checksum or "",
                                False,
                                False,
                                0,
                            ))
                            continue
                        incoming_at = assume_utc(
                            accepted_observations[candidate.source_observation_id]["accepted_at"]
                        )
                        existing_at = connection.scalar(select(CollectionObservation.accepted_at).where(
                            CollectionObservation.observation_id == existing["source_observation_id"],
                        ))
                        if (
                            existing_at is not None
                            and incoming_at <= assume_utc(existing_at)
                        ):
                            # A stale or equal non-identical candidate is a
                            # durable no-op.  In particular, do not persist
                            # its accepted observation or enqueue a correction.
                            results.append(LedgerWriteResult(
                                candidate.game_id,
                                str(existing["checksum"] or ""),
                                False,
                                False,
                                0,
                            ))
                            continue
                if accepted_observations is not None:
                    observation = accepted_observations[candidate.source_observation_id]
                    stored_observation = connection.execute(select(
                        CollectionObservation.__table__,
                    ).where(
                        CollectionObservation.observation_id
                        == candidate.source_observation_id,
                    )).mappings().one_or_none()
                    if stored_observation is None:
                        connection.execute(
                            CollectionObservation.__table__.insert().values(
                                **dict(observation)
                            )
                        )
                    elif stored_observation["checksum"] != observation["checksum"]:
                        raise LedgerValidationError(
                            "accepted observation identity conflicts with stored evidence"
                        )
                results.append(self._replace_candidate(connection, candidate, tables))
        return tuple(results)

    @staticmethod
    def _lock_manifest_authorization(
        connection: Connection,
        candidates: Sequence[CanonicalGame],
        observations: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Revalidate the exact manifest inside the acceptance transaction.

        The no-op conditional UPDATE is deliberate: PostgreSQL and SQLite both
        serialize it with manifest supersession, and the write lock is retained
        through observation, ledger, and composition-job insertion.
        """

        rows = tuple(observations.values())
        manifest_ids = {str(row.get("manifest_id") or "") for row in rows}
        seasons = {str(row.get("season") or "") for row in rows}
        cutoffs = {assume_utc(row["cutoff"]) for row in rows if row.get("cutoff") is not None}
        accepted_at_values = tuple(
            assume_utc(row["accepted_at"])
            for row in rows
            if row.get("accepted_at") is not None
        )
        if (
            len(manifest_ids) != 1
            or "" in manifest_ids
            or len(seasons) != 1
            or len(cutoffs) != 1
            or len(accepted_at_values) != len(rows)
            or {candidate.season for candidate in candidates} != seasons
        ):
            raise LedgerValidationError("accepted ledger manifest evidence is inconsistent")
        manifest_id = next(iter(manifest_ids))
        season = next(iter(seasons))
        cutoff = next(iter(cutoffs))
        accepted_at = max(accepted_at_values)
        table = CollectionManifest.__table__
        lock = connection.execute(update(table).where(
            table.c.manifest_id == manifest_id,
            table.c.season == season,
            table.c.cutoff == cutoff,
            table.c.status == "active",
            table.c.collect_before > accepted_at,
        ).values(status=table.c.status))
        if lock.rowcount != 1:
            raise LedgerValidationError("ledger manifest authorization expired before acceptance")
        manifest = connection.execute(select(table).where(
            table.c.manifest_id == manifest_id,
        )).mappings().one()
        manifest_scopes = set(json.loads(manifest["scopes"]))
        accepted_versions = set(json.loads(manifest["accepted_versions"]))
        by_observation = {
            candidate.source_observation_id: candidate
            for candidate in candidates
        }
        for observation_id, row in observations.items():
            try:
                scope = json.loads(str(row.get("scope") or ""))
            except (TypeError, ValueError) as error:
                raise LedgerValidationError("accepted ledger scope is malformed") from error
            candidate = by_observation[observation_id]
            if (
                row.get("environment") != "server"
                or row.get("provider") != "pbp"
                or row.get("observation_type") != "canonical_game_ledger"
                or not isinstance(scope, Mapping)
                or scope.get("surface") != "canonical_game_ledger"
                or str(scope.get("game_id") or "") != candidate.game_id
                or "canonical_game_ledger" not in manifest_scopes
                or row.get("schema_version") not in accepted_versions
                or assume_utc(row["retrieved_at"]) >= assume_utc(manifest["collect_before"])
            ):
                raise LedgerValidationError("accepted ledger evidence is not manifest authorized")
            # Bind the observation's payload to the exact candidate evidence
            # being persisted so an envelope cannot stamp a foreign document.
            _verify_observation_binding(candidate, row)

    @staticmethod
    def _is_idempotent_replay(
        existing: Mapping[str, Any] | None,
        candidate: CanonicalGame,
    ) -> bool:
        """True when the stored game already matches this exact candidate.

        The raw checksum is compared separately from the typed checksum so a
        raw-only correction (a provider field that changes no typed primitive)
        is still recognized as a replacement rather than an idempotent replay.
        """
        return (
            existing is not None
            and existing["checksum"] == candidate.checksum
            and (existing["raw_checksum"] or "") == (candidate.raw_checksum or "")
        )

    def _schema_drift_payload(
        self,
        connection: Connection,
        candidate: CanonicalGame,
        existing: Mapping[str, Any] | None,
        tables: Mapping[str, Any],
    ) -> Mapping[str, object] | None:
        """Compare a candidate's observed field sets against governed evidence.

        A first raw archive (a brand-new game, or a pre-032 game receiving its
        row evidence for the first time) is judged against the governed
        baseline field set: an unknown additive field is schema drift that must
        be recorded and alerted, while a normal first observation inside the
        baseline stays silent.  A correction whose corresponding archived rows
        gain or lose provider fields is drift for the same reason.  Either
        alert is recorded without rejecting the otherwise valid replacement.
        """
        if not candidate.raw_rows:
            return None
        raw_rows = connection.execute(select(
            tables["raw"].c.side,
            tables["raw"].c.row_type,
            tables["raw"].c.row_index,
            tables["raw"].c.observed_fields,
        ).where(tables["raw"].c.game_id == candidate.game_id)).mappings().all()
        candidate_fields = {
            (row.side, row.row_type, row.row_index): frozenset(row.observed_fields)
            for row in candidate.raw_rows
        }
        if existing is None or not raw_rows:
            added: set[str] = set()
            for fields in candidate_fields.values():
                added |= set(fields) - LEDGER_GOVERNED_FULLGAME_FIELDS
            if not added:
                return None
            return {
                "season": candidate.season,
                "game_id": candidate.game_id,
                "kind": "schema_drift",
                "reason": "unknown_field",
                "details": {
                    "added_fields": tuple(sorted(added))[:25],
                    "removed_fields": (),
                    "schema_version": LEDGER_SCHEMA_VERSION,
                },
            }

        def row_key(row: Any) -> tuple[Any, Any, Any]:
            return (row["side"], row["row_type"], row["row_index"])

        existing_fields = {
            row_key(row): frozenset(json.loads(row["observed_fields"] or "[]"))
            for row in raw_rows
        }
        added: set[str] = set()
        removed: set[str] = set()
        for key in existing_fields.keys() & candidate_fields.keys():
            added |= candidate_fields[key] - existing_fields[key]
            removed |= existing_fields[key] - candidate_fields[key]
        if not added and not removed:
            return None
        return {
            "season": candidate.season,
            "game_id": candidate.game_id,
            "kind": "schema_drift",
            "reason": "field_set_change",
            "details": {
                "added_fields": tuple(sorted(added))[:25],
                "removed_fields": tuple(sorted(removed))[:25],
                "schema_version": LEDGER_SCHEMA_VERSION,
            },
        }

    def _replace_candidate(
        self,
        connection: Connection,
        candidate: CanonicalGame,
        tables: Mapping[str, Any],
    ) -> LedgerWriteResult:
        existing = connection.execute(
            select(tables["game"]).where(tables["game"].c.game_id == candidate.game_id)
        ).mappings().one_or_none()
        if existing is not None and self._identity_changed(existing, candidate):
            raise LedgerValidationError("a correction cannot change a game's canonical identity")
        if self._is_idempotent_replay(existing, candidate):
            return LedgerWriteResult(candidate.game_id, candidate.checksum or "", False, False, 0)
        drift = self._schema_drift_payload(connection, candidate, existing, tables)
        self._delete_game(connection, candidate.game_id, tables)
        connection.execute(insert(tables["game"]).values(self._game_values(candidate)))
        connection.execute(
            insert(tables["team"]),
            [self._team_values(candidate.game_id, value) for value in candidate.team_facts],
        )
        connection.execute(
            insert(tables["player"]),
            [self._player_values(candidate.game_id, value) for value in candidate.player_facts],
        )
        if candidate.raw_rows:
            connection.execute(
                insert(tables["raw"]),
                [
                    self._raw_row_values(
                        candidate.game_id,
                        row,
                        source_observation_id=candidate.source_observation_id,
                        retrieved_at=candidate.retrieved_at,
                    )
                    for row in candidate.raw_rows
                ],
            )
        if self.correction_sink is not None:
            # A sink keeps the public two-argument callback seam, while the
            # transaction-local flag lets correction propagation distinguish a
            # replacement from first acceptance without exposing old rows to a
            # second reader or widening the callback API.
            connection.info["canonical_game_ledger_replacement"] = existing is not None
            self.correction_sink(connection, candidate)
        # Durable reference for indefinite retention (#25): every observation
        # that supplies an accepted game, including superseded corrections, is
        # referenced here so generic observation GC never prunes it.  Only real
        # observations are referenced; the repair seam writes candidates
        # without a staged collection observation.
        connection.execute(
            insert(tables["observation_evidence"]).from_select(
                [
                    tables["observation_evidence"].c.observation_id,
                    tables["observation_evidence"].c.game_id,
                    tables["observation_evidence"].c.created_at,
                ],
                select(
                    CollectionObservation.__table__.c.observation_id,
                    literal(candidate.game_id),
                    literal(datetime.now(timezone.utc)),
                ).where(
                    CollectionObservation.__table__.c.observation_id
                    == candidate.source_observation_id,
                ),
            )
        )
        if drift is not None and self.schema_drift_sink is not None:
            self.schema_drift_sink(connection, drift)
        return LedgerWriteResult(
            candidate.game_id,
            candidate.checksum or "",
            existing is None,
            existing is not None,
            len(candidate.player_facts),
        )

    def get_game(
        self,
        game_id: str,
        *,
        connection: Connection | None = None,
    ) -> CanonicalGame | None:
        tables = self._tables()
        scope = self.engine.connect() if connection is None else nullcontext(connection)
        with scope as connection:
            game_row = connection.execute(select(tables["game"]).where(tables["game"].c.game_id == game_id)).mappings().one_or_none()
            if game_row is None:
                return None
            team_rows = connection.execute(select(tables["team"]).where(tables["team"].c.game_id == game_id).order_by(tables["team"].c.team_id)).mappings().all()
            player_rows = connection.execute(select(tables["player"]).where(tables["player"].c.game_id == game_id).order_by(tables["player"].c.team_id, tables["player"].c.player_id)).mappings().all()
            raw_rows = connection.execute(select(tables["raw"]).where(tables["raw"].c.game_id == game_id).order_by(
                _side_order_expression(tables["raw"].c.side),
                tables["raw"].c.row_index,
            )).mappings().all()
        return _game_from_rows(game_row, team_rows, player_rows, raw_rows)

    def list_games(
        self,
        season: str,
        *,
        through: date | datetime | None = None,
        connection: Connection | None = None,
    ) -> tuple[LedgerGameSummary, ...]:
        canonical_season = validate_canonical_season(season)
        table = CanonicalGameLedgerGame.__table__
        statement = select(table).where(table.c.season == canonical_season).order_by(table.c.game_date.desc(), table.c.game_id.desc())
        if through is not None:
            statement = statement.where(table.c.game_date <= _canonical_date(through))
        scope = self.engine.connect() if connection is None else nullcontext(connection)
        with scope as connection:
            rows = connection.execute(statement).mappings().all()
            summaries = []
            player_table = CanonicalGameLedgerPlayerFact.__table__
            team_table = CanonicalGameLedgerTeamFact.__table__
            for row in rows:
                player_count = len(connection.execute(select(player_table.c.player_id).where(player_table.c.game_id == row["game_id"])).all())
                team_count = len(connection.execute(select(team_table.c.team_id).where(team_table.c.game_id == row["game_id"])).all())
                summaries.append(LedgerGameSummary(row["game_id"], row["season"], row["game_date"], row["checksum"], assume_utc(row["retrieved_at"]), player_count, team_count))
        return tuple(summaries)

    def game_checksums(
        self,
        season: str,
        *,
        through: date | datetime | None = None,
        connection: Connection | None = None,
    ) -> dict[str, str]:
        canonical_season = validate_canonical_season(season)
        table = CanonicalGameLedgerGame.__table__
        statement = select(table.c.game_id, table.c.checksum).where(table.c.season == canonical_season)
        if through is not None:
            statement = statement.where(table.c.game_date <= _canonical_date(through))
        scope = self.engine.connect() if connection is None else nullcontext(connection)
        with scope as connection:
            rows = connection.execute(statement).all()
        return {str(game_id): str(checksum) for game_id, checksum in rows}

    def game_ids_from_manifest(
        self,
        season: str,
        manifest_id: str,
    ) -> frozenset[str]:
        """Return games whose current accepted observation belongs to a manifest."""

        canonical_season = validate_canonical_season(season)
        games = CanonicalGameLedgerGame.__table__
        observations = CollectionObservation.__table__
        statement = (
            select(games.c.game_id)
            .join(
                observations,
                observations.c.observation_id == games.c.source_observation_id,
            )
            .where(
                games.c.season == canonical_season,
                observations.c.manifest_id == manifest_id,
            )
        )
        with self.engine.connect() as connection:
            return frozenset(str(game_id) for game_id in connection.scalars(statement))

    def game_ids_without_raw_evidence(
        self,
        season: str,
        *,
        through: date | datetime | None = None,
    ) -> frozenset[str]:
        """Return stored game rows that lack the complete raw PBP row archive.

        Migration 032 creates the raw archive after earlier ledger games were
        already accepted, so those games carry ``raw_checksum`` NULL and no
        ``canonical_game_ledger_raw_rows``.  The backfill must re-fetch them and
        the season must not report complete until every governed game retains
        both team-summary and every player-row evidence.
        """
        canonical_season = validate_canonical_season(season)
        game_table = CanonicalGameLedgerGame.__table__
        raw_table = LedgerGameRowEvidence.__table__
        statement = (
            select(game_table.c.game_id)
            .where(game_table.c.season == canonical_season)
            .where(or_(
                game_table.c.raw_checksum.is_(None),
                ~exists(select(1).where(raw_table.c.game_id == game_table.c.game_id)),
            ))
        )
        if through is not None:
            statement = statement.where(game_table.c.game_date <= _canonical_date(through))
        with self.engine.connect() as connection:
            return frozenset(str(row[0]) for row in connection.execute(statement).all())

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

    def publish_metadata(
        self,
        publication: LedgerPublicationRecord,
        *,
        connection: Connection | None = None,
    ) -> None:
        self.publish_metadata_batch((publication,), connection=connection)

    def publish_metadata_batch(
        self,
        publications: Iterable[LedgerPublicationRecord],
        *,
        connection: Connection | None = None,
    ) -> None:
        """Replace one materialization's metadata rows in one transaction."""

        records = tuple(publications)
        if not records:
            return
        table = LedgerPublication.__table__
        with (
            nullcontext(connection)
            if connection is not None
            else self.engine.begin()
        ) as connection:
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
            return

    def get_publication(
        self,
        stream_key: str,
        *,
        season: str,
        window_kind: str,
        window_games: int,
        as_of: date,
    ) -> LedgerPublicationRecord | None:
        """Read one historical materialized payload without activating a route."""

        table = LedgerPublication.__table__
        with self.engine.connect() as connection:
            row = connection.execute(select(table).where(
                table.c.stream_key == stream_key,
                table.c.season == validate_canonical_season(season),
                table.c.window_kind == window_kind,
                table.c.window_games == window_games,
                table.c.as_of == as_of,
            )).mappings().one_or_none()
        if row is None:
            return None
        return LedgerPublicationRecord(**{
            field_name: row[field_name]
            for field_name in LedgerPublicationRecord.__dataclass_fields__
        })

    def _tables(self) -> dict[str, Any]:
        return {
            "game": CanonicalGameLedgerGame.__table__,
            "team": CanonicalGameLedgerTeamFact.__table__,
            "player": CanonicalGameLedgerPlayerFact.__table__,
            "raw": LedgerGameRowEvidence.__table__,
            "observation_evidence": LedgerObservationEvidence.__table__,
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
        connection.execute(delete(tables["raw"]).where(tables["raw"].c.game_id == game_id))
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
            "raw_checksum": game.raw_checksum or raw_checksum(game.raw_rows),
            "retrieved_at": assume_utc(game.retrieved_at),
            "updated_at": assume_utc(game.retrieved_at),
        }

    @staticmethod
    def _team_values(game_id: str, fact: TeamGameFact) -> dict[str, Any]:
        return {"game_id": game_id, **asdict(fact)}

    @staticmethod
    def _player_values(game_id: str, fact: PlayerGameFact) -> dict[str, Any]:
        return {"game_id": game_id, **asdict(fact)}

    @staticmethod
    def _raw_row_values(
        game_id: str,
        row: LedgerGameRow,
        *,
        source_observation_id: str,
        retrieved_at: datetime,
    ) -> dict[str, Any]:
        return {
            "game_id": game_id,
            "row_type": row.row_type,
            "side": row.side,
            "row_index": row.row_index,
            "entity_id": row.entity_id,
            "entity_name": row.entity_name,
            "team_id": row.team_id,
            "source_observation_id": source_observation_id,
            "retrieved_at": assume_utc(retrieved_at),
            "checksum": row.checksum,
            "schema_version": row.schema_version,
            "observed_fields": json.dumps(row.observed_fields, sort_keys=True, separators=(",", ":")),
            "payload": _canonical_json(row.payload),
        }


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
    payload: str = "{}"


def _game_from_rows(
    game_row: Mapping[str, Any],
    team_rows: Sequence[Mapping[str, Any]],
    player_rows: Sequence[Mapping[str, Any]],
    raw_rows: Sequence[Mapping[str, Any]] = (),
) -> CanonicalGame:
    teams = tuple(TeamGameFact(**{key: row[key] for key in TeamGameFact.__dataclass_fields__}) for row in team_rows)
    players = tuple(PlayerGameFact(**{key: row[key] for key in PlayerGameFact.__dataclass_fields__}) for row in player_rows)
    # Reload rows in the same canonical order used for hashing and
    # persistence, so an unchanged replace of a loaded game is idempotent.
    archived = tuple(
        sorted(
            (
                LedgerGameRow(
                    game_id=row["game_id"],
                    row_type=row["row_type"],
                    side=row["side"],
                    row_index=row["row_index"],
                    entity_id=row["entity_id"],
                    entity_name=row["entity_name"],
                    team_id=row["team_id"],
                    payload=json.loads(row["payload"]),
                    checksum=row["checksum"],
                    observed_fields=tuple(json.loads(row["observed_fields"] or "[]")),
                    schema_version=row["schema_version"],
                )
                for row in raw_rows
            ),
            key=_raw_row_order,
        )
    )
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
        participant_ids_by_team=tuple(
            (team_id, tuple(sorted(row["player_id"] for row in player_rows if row["team_id"] == team_id)))
            for team_id in sorted({row["team_id"] for row in team_rows})
        ),
        season_type=game_row["season_type"],
        status=game_row["status"],
        checksum=game_row["checksum"],
        raw_rows=archived,
        raw_checksum=game_row.get("raw_checksum"),
    )


__all__ = [
    "ASSIST_LOCATION_FIELDS",
    "COUNT_FIELDS",
    "LEDGER_GOVERNED_DIAGNOSTIC_COUNTS",
    "LEDGER_GOVERNED_FULLGAME_FIELDS",
    "LEDGER_SCHEMA_VERSION",
    "CanonicalGame",
    "CanonicalGameLedgerRepository",
    "LedgerBackfillProgress",
    "LedgerGameRow",
    "LedgerGameSummary",
    "LedgerPublicationRecord",
    "LedgerSchemaUnavailable",
    "LedgerValidationError",
    "LedgerWriteResult",
    "PlayerGameFact",
    "TeamGameFact",
    "canonical_game_from_pbp",
    "canonical_row_checksum",
    "game_checksum",
    "raw_checksum",
    "raw_rows_from_facts",
    "record_schema_drift",
    "validate_complete_game",
]
