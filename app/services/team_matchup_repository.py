"""Atomic persistence for window-aware raw team matchup facts."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime
from math import isfinite
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc
from app.models.team_matchup import (
    TeamMatchupFactRow,
    TeamMatchupSurfaceObservationRow,
)
from app.utils.db import is_demo_database_url


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class TeamMatchupSnapshotScope:
    season: str
    as_of: date
    window_games: int | None = None

    def __post_init__(self) -> None:
        if self.window_games is not None and (
            not isinstance(self.window_games, int)
            or isinstance(self.window_games, bool)
            or self.window_games < 1
        ):
            raise ValueError("window_games must be a positive integer or None")

    @property
    def window_kind(self) -> str:
        return "season" if self.window_games is None else "rolling_games"

    @property
    def stored_window_games(self) -> int:
        return self.window_games or 0


@dataclass(frozen=True, slots=True)
class TeamMatchupFact:
    team_id: int
    base: str
    slice_key: str
    stat_key: str
    raw_value: float | None
    denominator_value: float | None
    denominator_unit: str | None
    provider: str
    window_start_date: date | None = None
    #: Exact governed game IDs this team's window aggregated and the
    #: deterministic ledger checksum of the selected game set.  Provider-
    #: collected legacy facts leave both empty.
    game_ids: tuple[str, ...] = ()
    ledger_checksum: str | None = None
    source_observation_ids: tuple[str, ...] = ()
    game_set_checksum: str | None = None
    cutoff: datetime | None = None
    recomposition_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TeamMatchupObservation:
    surface: str
    status: str
    unavailable_reason: str | None = None
    game_ids: tuple[str, ...] = ()
    ledger_checksum: str | None = None
    source_observation_ids: tuple[str, ...] = ()
    game_set_checksum: str | None = None
    cutoff: datetime | None = None
    recomposition_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StoredTeamMatchupFact(TeamMatchupFact):
    retrieved_at: datetime = datetime.min
    window_end_date: date = date.min


@dataclass(frozen=True, slots=True)
class StoredTeamMatchupObservation(TeamMatchupObservation):
    retrieved_at: datetime = datetime.min


@dataclass(frozen=True, slots=True)
class StoredTeamMatchupSnapshot:
    scope: TeamMatchupSnapshotScope
    facts: tuple[StoredTeamMatchupFact, ...]
    observations: tuple[StoredTeamMatchupObservation, ...]


class TeamMatchupRepository:
    """Replace or read one season/as-of/window snapshot transactionally."""

    def __init__(self, engine: Engine, *, write_fence=None) -> None:
        if is_demo_database_url(str(engine.url)):
            raise ValueError("the demo database cannot store team matchup facts")
        self.engine = engine
        self._write_fence = write_fence

    @staticmethod
    def _scope(table, scope: TeamMatchupSnapshotScope):
        return (
            table.c.season == scope.season,
            table.c.as_of_date == scope.as_of,
            table.c.window_kind == scope.window_kind,
            table.c.window_games == scope.stored_window_games,
        )

    def replace_snapshots(
        self,
        snapshots: Iterable[
            tuple[
                TeamMatchupSnapshotScope,
                Iterable[TeamMatchupFact],
                Iterable[TeamMatchupObservation],
            ]
        ],
        *,
        retrieved_at: datetime,
        affected_team_ids_by_scope: Mapping[TeamMatchupSnapshotScope, frozenset[int] | None] | None = None,
        affected_team_ids: frozenset[int] | None = None,
    ) -> None:
        """Replace related windows in one transaction after collection."""

        received = tuple(
            (scope, tuple(facts), tuple(observations))
            for scope, facts, observations in snapshots
        )
        if not received:
            raise ValueError("at least one team matchup snapshot is required")
        if any(not observations for _, _, observations in received):
            raise ValueError("a team matchup snapshot needs surface observations")
        observed_at = assume_utc(retrieved_at)
        current_date = observed_at.astimezone(EASTERN).date()
        if any(scope.as_of > current_date for scope, _, _ in received):
            raise ValueError("future as_of dates cannot be published")
        if affected_team_ids_by_scope is None and affected_team_ids is not None:
            affected_team_ids_by_scope = {
                scope: affected_team_ids for scope, _, _ in received
            }
        prepared = tuple(
            (scope, *self._prepare_surface_publication(facts, observations))
            for scope, facts, observations in received
        )
        fact_table = TeamMatchupFactRow.__table__
        observation_table = TeamMatchupSurfaceObservationRow.__table__
        with self.engine.begin() as connection:
            scoped_changes = []
            for scope, fact_rows, observation_rows, replace_surfaces in prepared:
                existing_observations = {
                    row["surface"]: row
                    for row in connection.execute(
                        select(observation_table).where(*self._scope(observation_table, scope))
                    ).mappings()
                }
                facts_by_surface: dict[str, list[TeamMatchupFact]] = defaultdict(list)
                for fact in fact_rows:
                    facts_by_surface[fact.base].append(fact)
                existing_facts: dict[str, tuple[tuple, ...]] = {}
                for surface in {observation.surface for observation in observation_rows}:
                    existing_facts[surface] = tuple(
                        sorted(
                            self._fact_signature(row)
                            for row in connection.execute(
                                select(fact_table).where(
                                    *self._scope(fact_table, scope),
                                    fact_table.c.base == surface,
                                )
                            ).mappings()
                        )
                    )
                if affected_team_ids_by_scope is None:
                    target_team_ids = None
                else:
                    selected_targets = affected_team_ids_by_scope.get(scope, frozenset())
                    target_team_ids = (
                        None
                        if selected_targets is None
                        else frozenset(selected_targets)
                    )
                    # A targeted correction cannot preserve a snapshot that
                    # does not exist yet. Build the complete scope on first
                    # materialization; subsequent corrections can narrow it.
                    if (
                        target_team_ids is not None
                        and any(
                            observation.surface not in existing_observations
                            for observation in observation_rows
                        )
                    ):
                        target_team_ids = None
                changed_surfaces = (
                    set()
                    if target_team_ids == frozenset()
                    else {
                        observation.surface
                        for observation in observation_rows
                        if self._surface_changed(
                            observation,
                            facts_by_surface.get(observation.surface, ()),
                            existing_observations.get(observation.surface),
                            existing_facts.get(observation.surface, ()),
                            observed_at,
                            scope.as_of,
                            target_team_ids,
                        )
                    }
                )
                scoped_changes.append(
                    (
                        scope,
                        fact_rows,
                        observation_rows,
                        replace_surfaces,
                        changed_surfaces,
                        target_team_ids,
                    )
                )
            checker = getattr(self._write_fence, "assert_writable", None)
            for (
                scope,
                fact_rows,
                observation_rows,
                replace_surfaces,
                changed_surfaces,
                target_team_ids,
            ) in scoped_changes:
                if callable(checker):
                    stream_by_surface = {
                        "traditional": (
                            "traditional_opponent",
                            "traditional_opponent_l15"
                            if scope.window_games is not None
                            else "traditional_opponent_season",
                        ),
                        "assist_locations": (
                            "assist_locations",
                            "assist_locations_l15"
                            if scope.window_games is not None
                            else "assist_locations_season",
                        ),
                        "play_types": (
                            "synergy_play_types_opponent_l15"
                            if scope.window_games is not None
                            else "synergy_play_types_opponent_season",
                        ),
                        "shot_types": (
                            "grouped_shot_types_opponent_l15"
                            if scope.window_games is not None
                            else "grouped_shot_types_opponent_season",
                        ),
                        "shot_zones": (
                            "exact_shot_zones_opponent_l15"
                            if scope.window_games is not None
                            else "exact_shot_zones_opponent_season",
                        ),
                    }
                    # Lock/check only the stream(s) represented by this
                    # snapshot.  A season write must not fence L15, and a
                    # traditional-only write must not fence assist locations.
                    for observation in observation_rows:
                        if observation.surface not in changed_surfaces:
                            continue
                        stream_keys = stream_by_surface.get(observation.surface, ())
                        for stream_key in stream_keys:
                            checker(stream_key, connection=connection)
                identity = {
                    "season": scope.season,
                    "as_of_date": scope.as_of,
                    "window_kind": scope.window_kind,
                    "window_games": scope.stored_window_games,
                }
                changed_replace_surfaces = set(replace_surfaces) & changed_surfaces
                if changed_replace_surfaces:
                    conditions = [
                        *self._scope(fact_table, scope),
                        fact_table.c.base.in_(changed_replace_surfaces),
                    ]
                    if target_team_ids is not None:
                        if target_team_ids:
                            conditions.append(fact_table.c.team_id.in_(target_team_ids))
                        else:
                            conditions.append(fact_table.c.team_id.in_((-1,)))
                    connection.execute(delete(fact_table).where(*conditions))
                if changed_surfaces:
                    connection.execute(
                        delete(observation_table).where(
                            *self._scope(observation_table, scope),
                            observation_table.c.surface.in_(changed_surfaces),
                        )
                    )
                changed_fact_rows = tuple(
                    fact for fact in fact_rows
                    if fact.base in changed_replace_surfaces
                    and (
                        target_team_ids is None
                        or fact.team_id in target_team_ids
                    )
                )
                if changed_fact_rows:
                    connection.execute(
                        insert(fact_table),
                        [
                            {
                                **identity,
                                "team_id": fact.team_id,
                                "base": fact.base,
                                "slice_key": fact.slice_key,
                                "stat_key": fact.stat_key,
                                "raw_value": fact.raw_value,
                                "denominator_value": fact.denominator_value,
                                "denominator_unit": fact.denominator_unit,
                                "provider": fact.provider,
                                "window_start_date": fact.window_start_date,
                                "window_end_date": scope.as_of,
                                "retrieved_at": observed_at,
                                "game_ids": _game_ids_json(fact.game_ids),
                                "ledger_checksum": fact.ledger_checksum,
                                "source_observation_ids": _source_observation_ids_json(
                                    fact.source_observation_ids
                                ),
                                "game_set_checksum": fact.game_set_checksum,
                                "cutoff": fact.cutoff,
                                "recomposition_reason": fact.recomposition_reason,
                            }
                            for fact in changed_fact_rows
                        ],
                    )
                changed_observations = tuple(
                    observation
                    for observation in observation_rows
                    if observation.surface in changed_surfaces
                )
                if changed_observations:
                    connection.execute(
                        insert(observation_table),
                        [
                            {
                                **identity,
                                "surface": observation.surface,
                                "status": observation.status,
                                "unavailable_reason": observation.unavailable_reason,
                                "retrieved_at": observed_at,
                                "game_ids": _game_ids_json(observation.game_ids),
                                "ledger_checksum": observation.ledger_checksum,
                                "source_observation_ids": _source_observation_ids_json(
                                    observation.source_observation_ids
                                ),
                                "game_set_checksum": observation.game_set_checksum,
                                "cutoff": observation.cutoff,
                                "recomposition_reason": observation.recomposition_reason,
                            }
                            for observation in changed_observations
                        ],
                    )

    @staticmethod
    def _fact_signature(row) -> tuple:
        return (
            row["team_id"],
            row["base"],
            row["slice_key"],
            row["stat_key"],
            row["raw_value"],
            row["denominator_value"],
            row["denominator_unit"],
            row["provider"],
            row["window_start_date"],
            row["window_end_date"],
            assume_utc(row["retrieved_at"]),
            _parse_game_ids(row["game_ids"]),
            row["ledger_checksum"],
            _parse_source_observation_ids(row["source_observation_ids"]),
            row["game_set_checksum"],
            _optional_aware(row["cutoff"]),
            row["recomposition_reason"],
        )

    @classmethod
    def _surface_changed(
        cls,
        observation: TeamMatchupObservation,
        facts: Iterable[TeamMatchupFact],
        existing_observation,
        existing_facts: tuple[tuple, ...],
        observed_at: datetime,
        window_end_date: date,
        affected_team_ids: frozenset[int] | None = None,
    ) -> bool:
        if existing_observation is None:
            return True
        if (
            existing_observation["status"] != observation.status
            or existing_observation["unavailable_reason"]
            != observation.unavailable_reason
            or assume_utc(existing_observation["retrieved_at"]) != observed_at
            or _parse_game_ids(existing_observation["game_ids"])
            != observation.game_ids
            or existing_observation["ledger_checksum"] != observation.ledger_checksum
            or _parse_source_observation_ids(existing_observation["source_observation_ids"])
            != observation.source_observation_ids
            or existing_observation["game_set_checksum"] != observation.game_set_checksum
            or _optional_aware(existing_observation["cutoff"]) != _optional_aware(observation.cutoff)
            or existing_observation["recomposition_reason"] != observation.recomposition_reason
        ):
            return True
        if observation.status != "available":
            return False
        expected = tuple(
            sorted(
                (
                    fact.team_id,
                    fact.base,
                    fact.slice_key,
                    fact.stat_key,
                    fact.raw_value,
                    fact.denominator_value,
                    fact.denominator_unit,
                    fact.provider,
                    fact.window_start_date,
                    window_end_date,
                    observed_at,
                    fact.game_ids,
                    fact.ledger_checksum,
                    fact.source_observation_ids,
                    fact.game_set_checksum,
                    _optional_aware(fact.cutoff),
                    fact.recomposition_reason,
                )
                for fact in facts
                if affected_team_ids is None or fact.team_id in affected_team_ids
            )
        )
        existing_expected = tuple(
            sorted(
                signature
                for signature in existing_facts
                if affected_team_ids is None or signature[0] in affected_team_ids
            )
        )
        return expected != existing_expected

    @staticmethod
    def _prepare_surface_publication(
        facts: tuple[TeamMatchupFact, ...],
        observations: tuple[TeamMatchupObservation, ...],
    ) -> tuple[
        tuple[TeamMatchupFact, ...],
        tuple[TeamMatchupObservation, ...],
        tuple[str, ...],
    ]:
        by_surface: dict[str, list[TeamMatchupFact]] = defaultdict(list)
        for fact in facts:
            by_surface[fact.base].append(fact)

        published_facts: list[TeamMatchupFact] = []
        published_observations = []
        replace_surfaces: set[str] = set()
        for observation in observations:
            surface_facts = tuple(by_surface[observation.surface])
            invalid_numeric = any(
                not TeamMatchupRepository._has_valid_numeric_values(fact)
                for fact in surface_facts
            )
            if observation.status == "available" and invalid_numeric:
                published_observations.append(
                    replace(
                        observation,
                        status="unavailable",
                        unavailable_reason="provider_invalid_numeric",
                    )
                )
                continue
            if (
                observation.status == "available"
                and not TeamMatchupRepository._has_complete_metrics(surface_facts)
            ):
                published_observations.append(
                    replace(
                        observation,
                        status="unavailable",
                        unavailable_reason="surface_incomplete",
                    )
                )
                continue
            published_observations.append(observation)
            if (
                observation.status == "available"
                and observation.surface not in replace_surfaces
            ):
                replace_surfaces.add(observation.surface)
                published_facts.extend(surface_facts)
        return (
            tuple(published_facts),
            tuple(published_observations),
            tuple(sorted(replace_surfaces)),
        )

    @staticmethod
    def _has_valid_numeric_values(fact: TeamMatchupFact) -> bool:
        if fact.denominator_unit not in {"minutes", "seconds"}:
            return False
        try:
            return (
                fact.raw_value is not None
                and isfinite(float(fact.raw_value))
                and fact.denominator_value is not None
                and isfinite(float(fact.denominator_value))
                and float(fact.denominator_value) > 0
            )
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _has_complete_metrics(facts: tuple[TeamMatchupFact, ...]) -> bool:
        if not facts:
            return False
        teams_by_metric: dict[tuple[str, str], set[int]] = defaultdict(set)
        counts_by_metric: dict[tuple[str, str], int] = defaultdict(int)
        for fact in facts:
            key = (fact.slice_key, fact.stat_key)
            teams_by_metric[key].add(fact.team_id)
            counts_by_metric[key] += 1
        team_sets = tuple(teams_by_metric.values())
        return len(set().union(*team_sets)) == 30 and all(
            len(teams_by_metric[key]) == 30 and counts_by_metric[key] == 30
            for key in teams_by_metric
        )

    def get_snapshot(
        self, scope: TeamMatchupSnapshotScope
    ) -> StoredTeamMatchupSnapshot:
        fact_table = TeamMatchupFactRow.__table__
        observation_table = TeamMatchupSurfaceObservationRow.__table__
        with self.engine.connect() as connection:
            fact_rows = (
                connection.execute(
                    select(fact_table)
                    .where(*self._scope(fact_table, scope))
                    .order_by(
                        fact_table.c.team_id,
                        fact_table.c.base,
                        fact_table.c.slice_key,
                        fact_table.c.stat_key,
                    )
                )
                .mappings()
                .all()
            )
            observation_rows = (
                connection.execute(
                    select(observation_table)
                    .where(*self._scope(observation_table, scope))
                    .order_by(observation_table.c.surface)
                )
                .mappings()
                .all()
            )
        return StoredTeamMatchupSnapshot(
            scope=scope,
            facts=tuple(
                StoredTeamMatchupFact(
                    team_id=row["team_id"],
                    base=row["base"],
                    slice_key=row["slice_key"],
                    stat_key=row["stat_key"],
                    raw_value=row["raw_value"],
                    denominator_value=row["denominator_value"],
                    denominator_unit=row["denominator_unit"],
                    provider=row["provider"],
                    window_start_date=row["window_start_date"],
                    retrieved_at=assume_utc(row["retrieved_at"]),
                    window_end_date=row["window_end_date"],
                    game_ids=_parse_game_ids(row["game_ids"]),
                    ledger_checksum=row["ledger_checksum"],
                    source_observation_ids=_parse_source_observation_ids(
                        row["source_observation_ids"]
                    ),
                    game_set_checksum=row["game_set_checksum"],
                    cutoff=_optional_aware(row["cutoff"]),
                    recomposition_reason=row["recomposition_reason"],
                )
                for row in fact_rows
            ),
            observations=tuple(
                StoredTeamMatchupObservation(
                    surface=row["surface"],
                    status=row["status"],
                    unavailable_reason=row["unavailable_reason"],
                    retrieved_at=assume_utc(row["retrieved_at"]),
                    game_ids=_parse_game_ids(row["game_ids"]),
                    ledger_checksum=row["ledger_checksum"],
                    source_observation_ids=_parse_source_observation_ids(
                        row["source_observation_ids"]
                    ),
                    game_set_checksum=row["game_set_checksum"],
                    cutoff=_optional_aware(row["cutoff"]),
                    recomposition_reason=row["recomposition_reason"],
                )
                for row in observation_rows
            ),
        )

    def get_latest_scope(
        self,
        season: str,
        *,
        window_games: int | None = None,
        as_of: date | None = None,
    ) -> TeamMatchupSnapshotScope | None:
        requested = TeamMatchupSnapshotScope(season, as_of or date.max, window_games)
        table = TeamMatchupSurfaceObservationRow.__table__
        conditions = (
            table.c.season == season,
            table.c.window_kind == requested.window_kind,
            table.c.window_games == requested.stored_window_games,
        )
        statement = select(func.max(table.c.as_of_date)).where(*conditions)
        if as_of is not None:
            statement = statement.where(table.c.as_of_date <= as_of)
        with self.engine.connect() as connection:
            latest_as_of = connection.execute(statement).scalar_one()
        if latest_as_of is None:
            return None
        return TeamMatchupSnapshotScope(season, latest_as_of, window_games)

    def get_latest_fact_scopes(
        self,
        season: str,
        *,
        window_games: int | None = None,
        as_of: date,
    ) -> dict[str, TeamMatchupSnapshotScope]:
        """Return each surface's newest fact-bearing scope through ``as_of``."""

        requested = TeamMatchupSnapshotScope(season, as_of, window_games)
        table = TeamMatchupFactRow.__table__
        statement = (
            select(table.c.base, func.max(table.c.as_of_date).label("as_of_date"))
            .where(
                table.c.season == season,
                table.c.window_kind == requested.window_kind,
                table.c.window_games == requested.stored_window_games,
                table.c.as_of_date <= as_of,
            )
            .group_by(table.c.base)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return {
            row["base"]: TeamMatchupSnapshotScope(
                season, row["as_of_date"], window_games
            )
            for row in rows
        }


def _game_ids_json(game_ids: tuple[str, ...]) -> str | None:
    """Serialize a game-id lineage tuple as deterministic JSON text."""

    if not game_ids:
        return None
    return json.dumps(sorted(game_ids), separators=(",", ":"))


def _parse_game_ids(value: str | None) -> tuple[str, ...]:
    """Parse a stored game-id lineage column back to a tuple."""

    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list) or any(
        not isinstance(game_id, str) for game_id in parsed
    ):
        return ()
    return tuple(parsed)


def _source_observation_ids_json(observation_ids: tuple[str, ...]) -> str | None:
    """Serialize immutable source-observation lineage deterministically."""

    if not observation_ids:
        return None
    return json.dumps(sorted(set(observation_ids)), separators=(",", ":"))


def _parse_source_observation_ids(value: str | None) -> tuple[str, ...]:
    """Parse source-observation lineage, failing closed for legacy rows."""

    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list) or any(
        not isinstance(observation_id, str) for observation_id in parsed
    ):
        return ()
    return tuple(parsed)


def _optional_aware(value: datetime | None) -> datetime | None:
    """Normalize nullable database timestamps for deterministic signatures."""

    return None if value is None else assume_utc(value)


__all__ = [
    "StoredTeamMatchupFact",
    "StoredTeamMatchupObservation",
    "StoredTeamMatchupSnapshot",
    "TeamMatchupFact",
    "TeamMatchupObservation",
    "TeamMatchupRepository",
    "TeamMatchupSnapshotScope",
]
