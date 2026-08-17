"""Atomic persistence for window-aware raw team matchup facts."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime
from math import isfinite
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Engine

from app.domain.utc import assume_utc, parse_utc_iso
from app.models.team_matchup import (
    TeamMatchupFactRow,
    TeamMatchupSurfaceObservationRow,
)
from app.models.collection_control import PublicationPointer, PublicationVersion
from app.services.team_matchup_publications import (
    NBA_PUBLICATION_BASES,
    PublicationLineage,
    publication_stream,
)
from app.utils.db import is_demo_database_url


EASTERN = ZoneInfo("America/New_York")


_PUBLICATION_CAPABILITY_TOKEN = object()


class PublicationWriteCapability:
    """Opaque authorization for one transactionally checked publication write."""

    __slots__ = ("engine", "_token")

    def __init__(self, engine, token) -> None:
        if token is not _PUBLICATION_CAPABILITY_TOKEN:
            raise TypeError("publication write capability is not constructible")
        self.engine = engine
        self._token = token

    def verify(self, connection, snapshots) -> None:
        """Verify every attached NBA lineage against active/candidate state."""

        for scope, facts, observations in snapshots:
            items = (*facts, *observations)
            window = "l15" if scope.window_games is not None else "season"
            for item in items:
                surface = item.base if hasattr(item, "base") else item.surface
                if surface not in NBA_PUBLICATION_BASES:
                    continue
                if hasattr(item, "status") and item.status != "available":
                    # Missing/unavailable surfaces carry diagnostic lineage,
                    # not facts authorized for a governed write.
                    continue
                publication = item.publication
                if publication is None or not publication.publication_id:
                    raise ValueError("publication_write_capability_required")
                stream_key = publication_stream(surface, window)
                version = connection.execute(
                    select(PublicationVersion.__table__).where(
                        PublicationVersion.publication_id == publication.publication_id,
                        PublicationVersion.stream_key == stream_key,
                    ).with_for_update()
                ).mappings().one_or_none()
                if version is None:
                    raise ValueError("publication_write_context_invalid")
                if version["season"] != scope.season:
                    raise ValueError("publication_write_context_invalid")
                if publication.version is not None and int(publication.version) != int(version["version"]):
                    raise ValueError("publication_write_context_invalid")
                if publication.cutoff is None:
                    raise ValueError("publication_write_context_invalid")
                try:
                    version_cutoff = assume_utc(version["cutoff"])
                    lineage_cutoff = parse_utc_iso(publication.cutoff)
                except (TypeError, ValueError, AttributeError, OverflowError):
                    raise ValueError("publication_write_context_invalid") from None
                if lineage_cutoff != version_cutoff:
                    raise ValueError("publication_write_context_invalid")
                if version_cutoff.date() > scope.as_of:
                    raise ValueError("publication_write_context_invalid")
                pointer = connection.execute(
                    select(PublicationPointer.__table__).where(
                        PublicationPointer.stream_key == stream_key,
                    ).with_for_update()
                ).mappings().one_or_none()
                status = version["status"]
                active = (
                    pointer is not None
                    and pointer["active_publication_id"] == publication.publication_id
                    and status in {"active", "rollback"}
                )
                if status != "candidate" and not active:
                    raise ValueError("publication_write_context_invalid")
        self._verify_payload_bindings(connection, snapshots)

    @staticmethod
    def _verify_payload_bindings(connection, snapshots) -> None:
        """Bind every governed fact to the exact immutable payload value."""

        from app.services.database_first_activation import (
            PublicationPayloadError,
            _reject_duplicate_json_keys,
            decode_team_window,
        )
        from app.services.team_matchup_publications import (
            publication_metric_identity,
            validate_publication_rows,
        )

        for scope, facts, observations in snapshots:
            window = "l15" if scope.window_games is not None else "season"
            for surface in NBA_PUBLICATION_BASES:
                surface_facts = tuple(fact for fact in facts if fact.base == surface)
                if not surface_facts:
                    continue
                publication_ids = {
                    fact.publication.publication_id
                    for fact in surface_facts
                    if fact.publication is not None
                }
                if len(publication_ids) != 1:
                    raise ValueError("publication_write_context_invalid")
                publication_id = next(iter(publication_ids))
                stream_key = publication_stream(surface, window)
                version = connection.execute(
                    select(PublicationVersion.__table__).where(
                        PublicationVersion.publication_id == publication_id,
                        PublicationVersion.stream_key == stream_key,
                    )
                ).mappings().one_or_none()
                try:
                    if hashlib.sha256(version["payload"].encode()).hexdigest() != version[
                        "checksum"
                    ]:
                        raise ValueError("publication checksum mismatch")
                    document = json.loads(
                        version["payload"],
                        object_pairs_hook=_reject_duplicate_json_keys,
                    )
                    rows = decode_team_window(document, stream_key=stream_key)
                    metric_keys = validate_publication_rows(surface, rows)
                except (
                    AttributeError,
                    KeyError,
                    TypeError,
                    ValueError,
                    PublicationPayloadError,
                ):
                    raise ValueError("publication_write_context_invalid") from None
                expected = {
                    (
                        row.team_id,
                        *publication_metric_identity(surface, metric_key),
                    ): (float(row.per48[metric_key]), frozenset(row.game_ids))
                    for row in rows
                    for metric_key in metric_keys
                }
                actual = {}
                for fact in surface_facts:
                    if fact.denominator_unit != "minutes" or fact.denominator_value <= 0:
                        raise ValueError("publication_write_context_invalid")
                    key = (fact.team_id, fact.slice_key, fact.stat_key)
                    if key in actual:
                        raise ValueError("publication_write_context_invalid")
                    actual[key] = (
                        float(fact.raw_value) / float(fact.denominator_value) * 48.0,
                        frozenset(fact.game_ids),
                    )
                if actual != expected:
                    raise ValueError("publication_write_context_invalid")
                available = tuple(
                    observation
                    for observation in observations
                    if observation.surface == surface
                    and observation.status == "available"
                )
                expected_game_ids = tuple(sorted({
                    game_id for row in rows for game_id in row.game_ids
                }))
                if len(available) != 1 or tuple(available[0].game_ids) != expected_game_ids:
                    raise ValueError("publication_write_context_invalid")


def create_publication_write_capability(engine) -> PublicationWriteCapability:
    """Create the only capability accepted by governed publication writes."""

    return PublicationWriteCapability(engine, _PUBLICATION_CAPABILITY_TOKEN)


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
    publication: PublicationLineage | None = None


@dataclass(frozen=True, slots=True)
class TeamMatchupObservation:
    surface: str
    status: str
    unavailable_reason: str | None = None
    game_ids: tuple[str, ...] = ()
    ledger_checksum: str | None = None
    publication: PublicationLineage | None = None


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

    def __init__(
        self,
        engine: Engine,
        *,
        write_fence=None,
        publication_write_capability: PublicationWriteCapability | None = None,
    ) -> None:
        if is_demo_database_url(str(engine.url)):
            raise ValueError("the demo database cannot store team matchup facts")
        self.engine = engine
        self._write_fence = write_fence
        self._publication_write_capability = publication_write_capability

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
    ) -> None:
        """Replace legacy/provider snapshots behind the normal write fence."""

        return self._replace_snapshots(snapshots, retrieved_at=retrieved_at)

    def replace_governed_publication_snapshots(
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
        capability: PublicationWriteCapability | None = None,
    ) -> None:
        """Replace snapshots after checking active/candidate publication state."""

        return self._replace_snapshots(
            snapshots,
            retrieved_at=retrieved_at,
            governed_publication=True,
            capability=capability,
        )

    def _replace_snapshots(
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
        governed_publication: bool = False,
        capability: PublicationWriteCapability | None = None,
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
        prepared = tuple(
            (scope, *self._prepare_surface_publication(facts, observations))
            for scope, facts, observations in received
        )
        fact_table = TeamMatchupFactRow.__table__
        observation_table = TeamMatchupSurfaceObservationRow.__table__
        with self.engine.begin() as connection:
            if governed_publication:
                capability = capability or self._publication_write_capability
                if capability is None:
                    raise ValueError("publication_write_capability_required")
                if not isinstance(capability, PublicationWriteCapability):
                    raise ValueError("publication_write_capability_required")
                if capability.engine is not self.engine:
                    raise ValueError("publication_write_capability_required")
                capability.verify(connection, received)
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
                changed_surfaces = {
                    observation.surface
                    for observation in observation_rows
                    if self._surface_changed(
                        observation,
                        facts_by_surface.get(observation.surface, ()),
                        existing_observations.get(observation.surface),
                        existing_facts.get(observation.surface, ()),
                        observed_at,
                        scope.as_of,
                    )
                }
                scoped_changes.append(
                    (scope, fact_rows, observation_rows, replace_surfaces, changed_surfaces)
                )
            checker = getattr(self._write_fence, "assert_writable", None)
            for scope, fact_rows, observation_rows, replace_surfaces, changed_surfaces in scoped_changes:
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
                    }
                    stream_by_surface.update({
                        surface: (
                            publication_stream(
                                surface,
                                "l15" if scope.window_games is not None else "season",
                            ),
                        )
                        for surface in NBA_PUBLICATION_BASES
                    })
                    # Lock/check only the stream(s) represented by this
                    # snapshot.  A season write must not fence L15, and a
                    # traditional-only write must not fence assist locations.
                    for observation in observation_rows:
                        if observation.surface not in changed_surfaces:
                            continue
                        # Only the distinct governed publication method may
                        # bypass the legacy fence, and it has already checked
                        # the active/candidate publication context above.
                        if (
                            governed_publication
                            and observation.surface in NBA_PUBLICATION_BASES
                            and observation.publication is not None
                        ):
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
                    connection.execute(
                        delete(fact_table).where(
                            *self._scope(fact_table, scope),
                            fact_table.c.base.in_(changed_replace_surfaces),
                        )
                    )
                if changed_surfaces:
                    connection.execute(
                        delete(observation_table).where(
                            *self._scope(observation_table, scope),
                            observation_table.c.surface.in_(changed_surfaces),
                        )
                    )
                changed_fact_rows = tuple(
                    fact for fact in fact_rows if fact.base in changed_replace_surfaces
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
                                **_publication_columns(fact.publication),
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
                                **_publication_columns(observation.publication),
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
            _publication_from_row(row),
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
            or _publication_from_row(existing_observation) != observation.publication
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
                    fact.publication,
                )
                for fact in facts
            )
        )
        return expected != existing_facts

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
                    publication=_publication_from_row(row),
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
                    publication=_publication_from_row(row),
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


def _publication_columns(
    publication: PublicationLineage | None,
) -> dict[str, object]:
    if publication is None:
        return {
            "publication_id": None,
            "publication_cutoff": None,
            "publication_freshness": None,
            "publication_version": None,
        }
    return {
        "publication_id": publication.publication_id,
        "publication_cutoff": publication.cutoff,
        "publication_freshness": publication.freshness,
        "publication_version": publication.version,
    }


def _publication_from_row(row) -> PublicationLineage | None:
    values = (
        row["publication_id"],
        row["publication_cutoff"],
        row["publication_freshness"],
        row["publication_version"],
    )
    return PublicationLineage(*values) if any(value is not None for value in values) else None


__all__ = [
    "PublicationWriteCapability",
    "StoredTeamMatchupFact",
    "StoredTeamMatchupObservation",
    "StoredTeamMatchupSnapshot",
    "TeamMatchupFact",
    "TeamMatchupObservation",
    "TeamMatchupRepository",
    "TeamMatchupSnapshotScope",
    "create_publication_write_capability",
]
