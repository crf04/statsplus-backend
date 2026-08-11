"""Season-only player Diet collection, atomic publication, and bulk reads."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

import pandas as pd
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc
from app.models.catalogs import PLAY_TYPES, SHOOTING_TYPES
from app.models.player_diet import (
    PlayerDietFactRow,
    PlayerDietSurfaceObservationRow,
)
from app.providers.nba_stats import validate_canonical_season
from app.utils.db import is_demo_database_url
from app.utils.telemetry import ProviderResponseError


PLAYER_DIET_BASES = (
    "assist_locations",
    "play_types",
    "shot_types",
    "shot_zones",
)
_SHOT_ZONE_SLICES = (
    "Restricted Area",
    "In The Paint (Non-RA)",
    "Mid-Range",
    "Corner 3",
    "Above the Break 3",
)
_ASSIST_SLICES = (
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
_VOLUME_UNITS = {
    "play_types": "possessions",
    "shot_types": "field_goal_attempts",
    "shot_zones": "field_goal_attempts",
    "assist_locations": "assists",
}
_PROVIDERS = {
    "play_types": "nba_synergy",
    "shot_types": "nba_stats",
    "shot_zones": "nba_stats",
    "assist_locations": "pbp_stats",
}


@dataclass(frozen=True, slots=True)
class PlayerDietFact:
    player_id: int
    base: str
    slice_key: str
    share: float
    volume: float
    games_played: int
    volume_unit: str
    provider: str


@dataclass(frozen=True, slots=True)
class StoredPlayerDietFact(PlayerDietFact):
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class PlayerDietObservation:
    base: str
    status: str
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StoredPlayerDietObservation(PlayerDietObservation):
    retrieved_at: datetime = datetime.min


@dataclass(frozen=True, slots=True)
class PlayerDietResult:
    season: str
    players: dict[int, tuple[StoredPlayerDietFact, ...]]
    observations: tuple[StoredPlayerDietObservation, ...]


class _AthleteJoinIncomplete(ValueError):
    pass


class _ProviderDependencyUnavailable(ValueError):
    pass


class PlayerDietRepository:
    """Persist available Bases while retaining prior facts for degraded Bases."""

    def __init__(self, engine: Engine) -> None:
        if is_demo_database_url(str(engine.url)):
            raise ValueError("the demo database cannot store player Diet facts")
        self.engine = engine

    def publish(
        self,
        season: str,
        facts: Iterable[PlayerDietFact],
        observations: Iterable[PlayerDietObservation],
        *,
        retrieved_at: datetime,
    ) -> None:
        season = validate_canonical_season(season)
        observed_at = assume_utc(retrieved_at)
        fact_rows = tuple(facts)
        observation_rows = tuple(observations)
        self._validate_publication(fact_rows, observation_rows)
        facts_by_base: dict[str, list[PlayerDietFact]] = defaultdict(list)
        for fact in fact_rows:
            facts_by_base[fact.base].append(fact)

        fact_table = PlayerDietFactRow.__table__
        observation_table = PlayerDietSurfaceObservationRow.__table__
        with self.engine.begin() as connection:
            for observation in observation_rows:
                if observation.status != "available":
                    continue
                connection.execute(
                    delete(fact_table).where(
                        fact_table.c.season == season,
                        fact_table.c.base == observation.base,
                    )
                )
                connection.execute(
                    insert(fact_table),
                    [
                        {
                            "season": season,
                            "player_id": fact.player_id,
                            "base": fact.base,
                            "slice_key": fact.slice_key,
                            "share": fact.share,
                            "volume": fact.volume,
                            "games_played": fact.games_played,
                            "volume_unit": fact.volume_unit,
                            "provider": fact.provider,
                            "retrieved_at": observed_at,
                        }
                        for fact in facts_by_base[observation.base]
                    ],
                )
            connection.execute(
                delete(observation_table).where(
                    observation_table.c.season == season
                )
            )
            connection.execute(
                insert(observation_table),
                [
                    {
                        "season": season,
                        "base": observation.base,
                        "status": observation.status,
                        "unavailable_reason": observation.unavailable_reason,
                        "retrieved_at": observed_at,
                    }
                    for observation in observation_rows
                ],
            )

    @staticmethod
    def _validate_publication(
        facts: tuple[PlayerDietFact, ...],
        observations: tuple[PlayerDietObservation, ...],
    ) -> None:
        if tuple(sorted(observation.base for observation in observations)) != (
            PLAYER_DIET_BASES
        ):
            raise ValueError("a player Diet publication needs each Base exactly once")
        facts_by_base: dict[str, list[PlayerDietFact]] = defaultdict(list)
        identities = set()
        for fact in facts:
            identity = (fact.player_id, fact.base, fact.slice_key)
            if identity in identities:
                raise ValueError("player Diet fact identities must be unique")
            identities.add(identity)
            facts_by_base[fact.base].append(fact)
            PlayerDietRepository._validate_fact(fact)
        for observation in observations:
            if observation.status not in {"available", "unavailable", "missing"}:
                raise ValueError("player Diet observation status is invalid")
            if (observation.status == "available") != (
                observation.unavailable_reason is None
            ):
                raise ValueError("player Diet observation reason is inconsistent")
            if observation.status == "available" and not facts_by_base[observation.base]:
                raise ValueError("an available player Diet Base needs facts")
            if observation.status != "available" and facts_by_base[observation.base]:
                raise ValueError("an unavailable player Diet Base cannot publish facts")

    @staticmethod
    def _validate_fact(fact: PlayerDietFact) -> None:
        if fact.base not in PLAYER_DIET_BASES or not fact.slice_key:
            raise ValueError("player Diet fact identity is invalid")
        if (
            isinstance(fact.player_id, bool)
            or not isinstance(fact.player_id, int)
            or fact.player_id < 1
        ):
            raise ValueError("player Diet fact player identity is invalid")
        if (
            isinstance(fact.games_played, bool)
            or not isinstance(fact.games_played, int)
            or fact.games_played < 1
        ):
            raise ValueError("player Diet games played must be positive")
        try:
            share = float(fact.share)
            volume = float(fact.volume)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("player Diet numeric values are invalid") from error
        if not isfinite(share) or not 0 <= share <= 1:
            raise ValueError("player Diet share must be finite and in [0,1]")
        if not isfinite(volume) or volume < 0:
            raise ValueError("player Diet volume must be finite and nonnegative")
        if fact.volume_unit != _VOLUME_UNITS[fact.base]:
            raise ValueError("player Diet volume unit is invalid")
        if fact.provider != _PROVIDERS[fact.base]:
            raise ValueError("player Diet provider is invalid")

    def get_for_players(
        self, season: str, player_ids: Sequence[int]
    ) -> PlayerDietResult:
        season = validate_canonical_season(season)
        requested = self._canonical_player_ids(player_ids)
        fact_table = PlayerDietFactRow.__table__
        observation_table = PlayerDietSurfaceObservationRow.__table__
        with self.engine.connect() as connection:
            fact_rows = []
            if requested:
                fact_rows = (
                    connection.execute(
                        select(fact_table)
                        .where(
                            fact_table.c.season == season,
                            fact_table.c.player_id.in_(requested),
                        )
                        .order_by(
                            fact_table.c.player_id,
                            fact_table.c.base,
                            fact_table.c.slice_key,
                        )
                    )
                    .mappings()
                    .all()
                )
            observation_rows = (
                connection.execute(
                    select(observation_table)
                    .where(observation_table.c.season == season)
                    .order_by(observation_table.c.base)
                )
                .mappings()
                .all()
            )
        players: dict[int, list[StoredPlayerDietFact]] = defaultdict(list)
        for row in fact_rows:
            players[row["player_id"]].append(
                StoredPlayerDietFact(
                    player_id=row["player_id"],
                    base=row["base"],
                    slice_key=row["slice_key"],
                    share=row["share"],
                    volume=row["volume"],
                    games_played=row["games_played"],
                    volume_unit=row["volume_unit"],
                    provider=row["provider"],
                    retrieved_at=assume_utc(row["retrieved_at"]),
                )
            )
        return PlayerDietResult(
            season=season,
            players={player_id: tuple(players[player_id]) for player_id in sorted(players)},
            observations=tuple(
                StoredPlayerDietObservation(
                    base=row["base"],
                    status=row["status"],
                    unavailable_reason=row["unavailable_reason"],
                    retrieved_at=assume_utc(row["retrieved_at"]),
                )
                for row in observation_rows
            ),
        )

    @staticmethod
    def _canonical_player_ids(player_ids: Sequence[int]) -> tuple[int, ...]:
        if isinstance(player_ids, (str, bytes)):
            raise ValueError("player_ids must be canonical positive integers")
        values = tuple(player_ids)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
            raise ValueError("player_ids must be canonical positive integers")
        return tuple(sorted(set(values)))


class PlayerDietService:
    """The small refresh plus bulk-query interface for Season player Diets."""

    def __init__(
        self,
        engine: Engine,
        *,
        athlete_catalog: Any,
        nba_stats_provider: Any,
        pbp_stats_provider: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = PlayerDietRepository(engine)
        self.athlete_catalog = athlete_catalog
        self.nba_stats = nba_stats_provider
        self.pbp_stats = pbp_stats_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def refresh(self, season: str) -> None:
        season = validate_canonical_season(season)
        retrieved_at = assume_utc(self._clock())
        freshness = self.athlete_catalog.get_freshness(season, now=retrieved_at)
        if not freshness.get("is_fresh") or not freshness.get("row_count"):
            raise ValueError("player Diet refresh requires a fresh Athlete Catalog")
        canonical_ids = {
            int(row["player_id"])
            for row in self.athlete_catalog.get_catalog(season, active_only=False)
        }
        if not canonical_ids:
            raise ValueError("player Diet refresh requires a nonempty Athlete Catalog")

        facts = []
        observations = []
        games_by_player: dict[int, int] = {}
        play_facts, play_observation = self._collect_base(
            "play_types",
            lambda: self._collect_play_types(season, canonical_ids),
        )
        facts.extend(play_facts)
        observations.append(play_observation)
        shot_type_facts, shot_type_observation = self._collect_base(
            "shot_types",
            lambda: self._collect_shot_types(
                season, canonical_ids, games_by_player=games_by_player
            ),
        )
        facts.extend(shot_type_facts)
        observations.append(shot_type_observation)
        shot_zone_facts, shot_zone_observation = self._collect_base(
            "shot_zones",
            lambda: self._collect_shot_zones(
                season,
                canonical_ids,
                games_by_player=games_by_player,
                games_played_evidence_available=(
                    shot_type_observation.status == "available"
                ),
            ),
        )
        facts.extend(shot_zone_facts)
        observations.append(shot_zone_observation)
        assist_facts, assist_observation = self._collect_base(
            "assist_locations",
            lambda: self._collect_assists(season, canonical_ids),
        )
        facts.extend(assist_facts)
        observations.append(assist_observation)
        self.repository.publish(
            season, facts, observations, retrieved_at=retrieved_at
        )

    def get_for_players(
        self, season: str, player_ids: Sequence[int]
    ) -> PlayerDietResult:
        return self.repository.get_for_players(season, player_ids)

    @staticmethod
    def _collect_base(
        base: str, collect: Callable[[], list[PlayerDietFact]]
    ) -> tuple[list[PlayerDietFact], PlayerDietObservation]:
        try:
            facts = collect()
            PlayerDietService._validate_collected_base(base, facts)
        except _AthleteJoinIncomplete:
            return [], PlayerDietObservation(
                base, "unavailable", "athlete_catalog_join_incomplete"
            )
        except _ProviderDependencyUnavailable:
            return [], PlayerDietObservation(
                base, "unavailable", "missing_games_played_evidence"
            )
        except ProviderResponseError:
            return [], PlayerDietObservation(
                base, "unavailable", "provider_malformed_response"
            )
        except ValueError:
            return [], PlayerDietObservation(
                base, "unavailable", "provider_invalid_response"
            )
        if not facts:
            return [], PlayerDietObservation(base, "missing", "provider_no_observation")
        return facts, PlayerDietObservation(base, "available")

    @staticmethod
    def _validate_collected_base(
        base: str, facts: Sequence[PlayerDietFact]
    ) -> None:
        identities = set()
        for fact in facts:
            if fact.base != base:
                raise ValueError("player Diet fact belongs to another Base")
            identity = (fact.player_id, fact.slice_key)
            if identity in identities:
                raise ValueError("provider returned duplicate player Diet facts")
            identities.add(identity)
            PlayerDietRepository._validate_fact(fact)

    def _collect_play_types(
        self, season: str, canonical_ids: set[int]
    ) -> list[PlayerDietFact]:
        facts = []
        for play_type in PLAY_TYPES:
            frame = self.nba_stats.fetch_synergy_play_types(
                play_type,
                player_or_team_abbreviation="P",
                type_grouping="Offensive",
                season=season,
                season_type="Regular Season",
                per_mode_simple="Totals",
            )
            if frame.empty:
                raise ValueError("one configured Synergy play type was not observed")
            for row in self._flat_frame(frame).to_dict(orient="records"):
                self._require_fields(
                    row, "PLAYER_ID", "PLAY_TYPE", "TYPE_GROUPING", "GP", "POSS_PCT", "POSS"
                )
                if row["PLAY_TYPE"] != play_type or row["TYPE_GROUPING"] != "Offensive":
                    raise ValueError("Synergy player play-type identity is inconsistent")
                facts.append(
                    self._fact(
                        row,
                        canonical_ids,
                        base="play_types",
                        slice_key=play_type,
                        share=row["POSS_PCT"],
                        volume=row["POSS"],
                        games_played=row["GP"],
                    )
                )
        return facts

    def _collect_shot_types(
        self,
        season: str,
        canonical_ids: set[int],
        *,
        games_by_player: dict[int, int],
    ) -> list[PlayerDietFact]:
        facts = []
        for shooting_type in SHOOTING_TYPES:
            frame = self.nba_stats.fetch_player_shot_type(
                shooting_type,
                season=season,
                season_type="Regular Season",
                per_mode_simple="Totals",
            )
            if frame.empty:
                raise ValueError("one configured player shot type was not observed")
            for row in self._flat_frame(frame).to_dict(orient="records"):
                self._require_fields(row, "PLAYER_ID", "GP", "FGA_FREQUENCY", "FGA")
                player_id = self._joined_player_id(row["PLAYER_ID"], canonical_ids)
                games = self._positive_int(row["GP"])
                previous_games = games_by_player.setdefault(player_id, games)
                if previous_games != games:
                    raise ValueError("player shot-type games played are inconsistent")
                facts.append(
                    self._fact(
                        row,
                        canonical_ids,
                        base="shot_types",
                        slice_key=shooting_type,
                        share=row["FGA_FREQUENCY"],
                        volume=row["FGA"],
                        games_played=games,
                    )
                )
        return facts

    def _collect_shot_zones(
        self,
        season: str,
        canonical_ids: set[int],
        *,
        games_by_player: Mapping[int, int],
        games_played_evidence_available: bool,
    ) -> list[PlayerDietFact]:
        frame = self.nba_stats.fetch_player_shooting_zone(
            season=season,
            season_type="Regular Season",
            per_mode_detailed="Totals",
        )
        if frame.empty:
            return []
        zone_rows = []
        for row in self._flat_frame(frame).to_dict(orient="records"):
            self._require_fields(row, "PLAYER_ID", *[f"{name}_FGA" for name in _SHOT_ZONE_SLICES])
            player_id = self._joined_player_id(row["PLAYER_ID"], canonical_ids)
            volumes = {name: self._number(row[f"{name}_FGA"]) for name in _SHOT_ZONE_SLICES}
            if any(volume < 0 for volume in volumes.values()):
                raise ValueError("shot-zone attempts must be nonnegative")
            total = sum(volumes.values())
            if not isfinite(total):
                raise ValueError("shot-zone total attempts must be finite")
            if total == 0:
                continue
            zone_rows.append((player_id, volumes, total))
        if not games_played_evidence_available:
            raise _ProviderDependencyUnavailable(
                "shot-type games played evidence is unavailable"
            )
        facts = []
        for player_id, volumes, total in zone_rows:
            games = games_by_player.get(player_id)
            if games is None:
                raise _ProviderDependencyUnavailable(
                    "shot-zone player has no games played evidence"
                )
            for slice_key, volume in volumes.items():
                facts.append(
                    PlayerDietFact(
                        player_id,
                        "shot_zones",
                        slice_key,
                        volume / total,
                        volume,
                        games,
                        _VOLUME_UNITS["shot_zones"],
                        _PROVIDERS["shot_zones"],
                    )
                )
        return facts

    def _collect_assists(
        self, season: str, canonical_ids: set[int]
    ) -> list[PlayerDietFact]:
        frame = self.pbp_stats.fetch_player_diet_totals(season=season)
        facts = []
        for row in self._flat_frame(frame).to_dict(orient="records"):
            self._require_fields(
                row, "EntityId", "GamesPlayed", "Assists", *_ASSIST_SLICES
            )
            assists = self._number(row["Assists"])
            if assists <= 0:
                continue
            for slice_key in _ASSIST_SLICES:
                if pd.isna(row[slice_key]):
                    continue
                volume = self._number(row[slice_key])
                facts.append(
                    self._fact(
                        {"PLAYER_ID": row["EntityId"]},
                        canonical_ids,
                        base="assist_locations",
                        slice_key=slice_key,
                        share=volume / assists,
                        volume=volume,
                        games_played=row["GamesPlayed"],
                    )
                )
        return facts

    @staticmethod
    def _flat_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            raise ProviderResponseError("player Diet provider response must be a frame")
        flattened = frame.copy()
        if isinstance(flattened.columns, pd.MultiIndex):
            flattened.columns = [
                "_".join(str(part) for part in column if part).strip()
                for column in flattened.columns
            ]
        else:
            flattened.columns = [str(column) for column in flattened.columns]
        if len(set(flattened.columns)) != len(flattened.columns):
            raise ProviderResponseError("player Diet provider columns are ambiguous")
        return flattened

    @staticmethod
    def _require_fields(row: Mapping[str, Any], *fields: str) -> None:
        if any(field not in row for field in fields):
            raise ProviderResponseError("player Diet provider schema is unsupported")

    @staticmethod
    def _joined_player_id(value: Any, canonical_ids: set[int]) -> int:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("provider player identity is invalid") from error
        if not isfinite(numeric) or not numeric.is_integer() or numeric <= 0:
            raise ValueError("provider player identity is invalid")
        player_id = int(numeric)
        if player_id not in canonical_ids:
            raise _AthleteJoinIncomplete("provider player is absent from Athlete Catalog")
        return player_id

    @staticmethod
    def _positive_int(value: Any) -> int:
        numeric = PlayerDietService._number(value)
        if not numeric.is_integer() or numeric <= 0:
            raise ValueError("games played must be a positive integer")
        return int(numeric)

    @staticmethod
    def _number(value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("provider numeric value is invalid") from error
        if not isfinite(numeric):
            raise ValueError("provider numeric value must be finite")
        return numeric

    @classmethod
    def _fact(
        cls,
        row: Mapping[str, Any],
        canonical_ids: set[int],
        *,
        base: str,
        slice_key: str,
        share: Any,
        volume: Any,
        games_played: Any,
    ) -> PlayerDietFact:
        return PlayerDietFact(
            player_id=cls._joined_player_id(row["PLAYER_ID"], canonical_ids),
            base=base,
            slice_key=slice_key,
            share=cls._number(share),
            volume=cls._number(volume),
            games_played=cls._positive_int(games_played),
            volume_unit=_VOLUME_UNITS[base],
            provider=_PROVIDERS[base],
        )


__all__ = [
    "PLAYER_DIET_BASES",
    "PlayerDietFact",
    "PlayerDietObservation",
    "PlayerDietResult",
    "PlayerDietService",
    "StoredPlayerDietFact",
    "StoredPlayerDietObservation",
]
