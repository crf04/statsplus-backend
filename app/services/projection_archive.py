"""Durable normalized projection evidence and its database-first live read model."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import threading
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.domain.comparisons import market_reference
from app.domain.statistics import MatchState, ScoringPeriod
from app.domain.utc import assume_utc
from app.models.projection_archive import (
    LatestPlayerProjection,
    ProjectionArchiveScopeLock,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProviderPoll,
)
from app.providers.dfs import (
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    SnapshotStatus,
)
from app.services.dfs_snapshot_cache import (
    deserialize_provider_snapshot,
    serialize_provider_snapshot,
)
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.statistic_catalog import StatisticCatalog


@dataclass(frozen=True, slots=True)
class ProjectionArchiveResult:
    """Observable result of accepting one normalized provider snapshot."""

    snapshot_id: str
    generation_id: str
    changed: bool
    observation_count: int
    materialization_outcome: str


@dataclass(frozen=True, slots=True)
class ProjectionArchiveReadScope:
    """One explicit provider/query identity selected for request-time reads."""

    provider: str
    query: NBAMarketQuery

    def __post_init__(self) -> None:
        provider = self.provider.strip().casefold()
        if not provider:
            raise ValueError("projection archive read provider is required")
        if not isinstance(self.query, NBAMarketQuery) or self.query.season is None:
            raise ValueError("projection archive read scope requires a season query")
        object.__setattr__(self, "provider", provider)

    @property
    def query_key(self) -> str:
        return _query_key(self.query)


def _digest(prefix: str, *values: object) -> str:
    payload = "\x1f".join(str(value) for value in values).encode("utf-8")
    return f"{prefix}_{sha256(payload).hexdigest()}"


def _snapshot_checksum(query_key: str, document: str) -> str:
    return sha256(f"{query_key}\x1f{document}".encode("utf-8")).hexdigest()


def _snapshot_content_checksum(query_key: str, document: str) -> str:
    payload = json.loads(document)
    del payload["retrieved_at"]
    canonical_content = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return _snapshot_checksum(query_key, canonical_content)


def _source_snapshot(snapshot: ProviderSnapshot) -> ProviderSnapshot:
    """Remove derived statistic matches from the immutable source document."""

    return replace(
        snapshot,
        markets=tuple(
            replace(market, statistic_match=None) for market in snapshot.markets
        ),
    )


def _query_key(query: NBAMarketQuery) -> str:
    return _digest(
        "qry",
        query.season,
        query.sport,
        query.league,
        ",".join(sorted(status.value for status in query.market_statuses)),
        query.pregame_only,
    )


class ProjectionArchive:
    """Accept a complete normalized snapshot as one atomic archive generation."""

    _scope_locks: dict[tuple[int, str, str, str], threading.RLock] = {}
    _scope_locks_guard = threading.Lock()

    def __init__(self, engine: Engine, statistic_catalog: StatisticCatalog) -> None:
        self.engine = engine
        self.market_categories = {
            statistic.id: statistic.market_category
            for statistic in statistic_catalog.statistics
            if statistic.market_category is not None
        }

    def ingest_complete_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None = None,
        poll_started_at: datetime | None = None,
    ) -> ProjectionArchiveResult:
        if not isinstance(snapshot, ProviderSnapshot):
            raise TypeError("snapshot must be ProviderSnapshot")
        if snapshot.status is not SnapshotStatus.COMPLETE:
            raise ValueError(
                "only complete provider snapshots may enter this archive path"
            )
        if not isinstance(query, NBAMarketQuery) or query.season is None:
            raise ValueError("projection archive queries require a canonical season")

        accepted = assume_utc(accepted_at or datetime.now(timezone.utc))
        poll_started = None if poll_started_at is None else assume_utc(poll_started_at)
        if poll_started is not None and poll_started > accepted:
            raise ValueError("projection poll cannot start after it completes")
        source = _source_snapshot(snapshot)
        query_key = _query_key(query)
        observation_count = len(snapshot.markets)
        document = serialize_provider_snapshot(source, query)
        checksum = _snapshot_checksum(query_key, document)
        content_checksum = _snapshot_content_checksum(query_key, document)
        snapshot_id = f"psn_{checksum}"
        generation_id = _digest("gen", snapshot_id)
        poll_id = _digest(
            "poll",
            snapshot_id,
            "" if poll_started is None else poll_started.isoformat(),
            accepted.isoformat(),
        )
        if accepted < assume_utc(snapshot.retrieved_at):
            raise ValueError("projection snapshot cannot be accepted before retrieval")
        snapshot_table = ProjectionProviderSnapshot.__table__

        with self._scope_transaction(
            snapshot.provider,
            query.season,
            query_key,
        ) as connection:
            replayed = self._replayed_result(connection, poll_id)
            if replayed is not None:
                return replayed
            current = self._current_materialization(
                connection,
                provider=snapshot.provider,
                season=query.season,
                query_key=query_key,
            )
            if current is not None and current["content_checksum"] == content_checksum:
                self._record_poll(
                    connection,
                    poll_id=poll_id,
                    snapshot=snapshot,
                    season=query.season,
                    query_key=query_key,
                    started_at=poll_started,
                    completed_at=accepted,
                    outcome="unchanged",
                    snapshot_id=str(current["snapshot_id"]),
                    observation_count=observation_count,
                )
                return ProjectionArchiveResult(
                    snapshot_id=str(current["snapshot_id"]),
                    generation_id=str(current["generation_id"]),
                    changed=False,
                    observation_count=observation_count,
                    materialization_outcome="unchanged",
                )

            existing = connection.execute(
                select(snapshot_table.c.snapshot_id).where(
                    snapshot_table.c.checksum == checksum
                )
            ).scalar_one_or_none()
            if existing is not None:
                self._record_poll(
                    connection,
                    poll_id=poll_id,
                    snapshot=snapshot,
                    season=query.season,
                    query_key=query_key,
                    started_at=poll_started,
                    completed_at=accepted,
                    outcome="unchanged",
                    snapshot_id=str(existing),
                    observation_count=observation_count,
                )
                return ProjectionArchiveResult(
                    snapshot_id=str(existing),
                    generation_id=generation_id,
                    changed=False,
                    observation_count=observation_count,
                    materialization_outcome="unchanged",
                )

            materialization_outcome = self._materialization_outcome(
                current,
                incoming_retrieved_at=snapshot.retrieved_at,
            )

            connection.execute(
                insert(snapshot_table).values(
                    snapshot_id=snapshot_id,
                    provider=snapshot.provider,
                    season=query.season,
                    query_key=query_key,
                    contract_version=snapshot.contract_version,
                    snapshot_status=snapshot.status.value,
                    retrieved_at=snapshot.retrieved_at,
                    accepted_at=accepted,
                    checksum=checksum,
                    content_checksum=content_checksum,
                    evidence_document=document,
                )
            )
            observation_rows = self._observation_rows(snapshot, snapshot_id)
            if observation_rows:
                connection.execute(
                    insert(ProjectionObservation.__table__), observation_rows
                )
            connection.execute(
                insert(ProjectionMaterializationGeneration.__table__).values(
                    generation_id=generation_id,
                    provider=snapshot.provider,
                    season=query.season,
                    query_key=query_key,
                    snapshot_id=snapshot_id,
                    created_at=accepted,
                    outcome=materialization_outcome,
                )
            )
            if materialization_outcome == "advanced":
                self._advance_latest(
                    connection,
                    observation_rows,
                    provider=snapshot.provider,
                    season=query.season,
                    query_key=query_key,
                    generation_id=generation_id,
                )
            self._record_poll(
                connection,
                poll_id=poll_id,
                snapshot=snapshot,
                season=query.season,
                query_key=query_key,
                started_at=poll_started,
                completed_at=accepted,
                outcome="changed",
                snapshot_id=snapshot_id,
                observation_count=observation_count,
            )

        return ProjectionArchiveResult(
            snapshot_id=snapshot_id,
            generation_id=generation_id,
            changed=True,
            observation_count=observation_count,
            materialization_outcome=materialization_outcome,
        )

    @contextmanager
    def _scope_transaction(
        self,
        provider: str,
        season: str,
        query_key: str,
    ) -> Any:
        key = (id(self.engine), provider, season, query_key)
        with self._scope_locks_guard:
            lock = self._scope_locks.setdefault(key, threading.RLock())
        table = ProjectionArchiveScopeLock.__table__
        if self.engine.dialect.name == "sqlite":
            with lock, self.engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        insert(table)
                        .values(provider=provider, season=season, query_key=query_key)
                        .prefix_with("OR IGNORE")
                    )
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            return
        with lock, self.engine.begin() as connection:
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(table).values(
                            provider=provider,
                            season=season,
                            query_key=query_key,
                        )
                    )
            except IntegrityError:
                pass
            connection.execute(
                select(table)
                .where(
                    table.c.provider == provider,
                    table.c.season == season,
                    table.c.query_key == query_key,
                )
                .with_for_update()
            ).one()
            yield connection

    @staticmethod
    def _replayed_result(
        connection: Any,
        poll_id: str,
    ) -> ProjectionArchiveResult | None:
        poll_table = ProviderPoll.__table__
        generation_table = ProjectionMaterializationGeneration.__table__
        poll = (
            connection.execute(
                select(
                    poll_table.c.snapshot_id,
                    poll_table.c.outcome,
                    poll_table.c.observation_count,
                ).where(poll_table.c.poll_id == poll_id)
            )
            .mappings()
            .one_or_none()
        )
        if poll is None:
            return None
        snapshot_id = str(poll["snapshot_id"])
        generation = (
            connection.execute(
                select(
                    generation_table.c.generation_id,
                    generation_table.c.outcome,
                ).where(generation_table.c.snapshot_id == snapshot_id)
            )
            .mappings()
            .one()
        )
        changed = poll["outcome"] == "changed"
        return ProjectionArchiveResult(
            snapshot_id=snapshot_id,
            generation_id=str(generation["generation_id"]),
            changed=changed,
            observation_count=int(poll["observation_count"]),
            materialization_outcome=(
                str(generation["outcome"]) if changed else "unchanged"
            ),
        )

    def load_source_snapshot(self, snapshot_id: str) -> ProviderSnapshot | None:
        """Read and checksum-verify one archived normalized source document."""

        table = ProjectionProviderSnapshot.__table__
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        table.c.evidence_document,
                        table.c.checksum,
                        table.c.query_key,
                    ).where(table.c.snapshot_id == snapshot_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        document = str(row["evidence_document"])
        checksum = _snapshot_checksum(str(row["query_key"]), document)
        if checksum != row["checksum"]:
            raise ValueError("archived projection snapshot checksum is invalid")
        return deserialize_provider_snapshot(document)

    @staticmethod
    def _current_materialization(
        connection: Any,
        *,
        provider: str,
        season: str,
        query_key: str,
    ) -> Any | None:
        generation_table = ProjectionMaterializationGeneration.__table__
        snapshot_table = ProjectionProviderSnapshot.__table__
        return (
            connection.execute(
                select(
                    generation_table.c.generation_id,
                    snapshot_table.c.snapshot_id,
                    snapshot_table.c.retrieved_at,
                    snapshot_table.c.content_checksum,
                )
                .select_from(
                    generation_table.join(
                        snapshot_table,
                        generation_table.c.snapshot_id == snapshot_table.c.snapshot_id,
                    )
                )
                .where(
                    generation_table.c.provider == provider,
                    generation_table.c.season == season,
                    generation_table.c.query_key == query_key,
                    generation_table.c.outcome == "advanced",
                )
                .order_by(snapshot_table.c.retrieved_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _materialization_outcome(
        current: Any | None,
        *,
        incoming_retrieved_at: datetime,
    ) -> str:
        if current is None:
            return "advanced"
        incoming = assume_utc(incoming_retrieved_at)
        current_retrieved_at = assume_utc(current["retrieved_at"])
        if incoming > current_retrieved_at:
            return "advanced"
        if incoming == current_retrieved_at:
            return "same_time_not_promoted"
        return "older_not_promoted"

    @staticmethod
    def _record_poll(
        connection: Any,
        *,
        poll_id: str,
        snapshot: ProviderSnapshot,
        season: str,
        query_key: str,
        started_at: datetime | None,
        completed_at: datetime,
        outcome: str,
        snapshot_id: str,
        observation_count: int,
    ) -> None:
        connection.execute(
            insert(ProviderPoll.__table__).values(
                poll_id=poll_id,
                provider=snapshot.provider,
                season=season,
                query_key=query_key,
                started_at=started_at,
                completed_at=completed_at,
                retrieved_at=snapshot.retrieved_at,
                outcome=outcome,
                snapshot_id=snapshot_id,
                observation_count=observation_count,
            )
        )

    def _observation_rows(
        self, snapshot: ProviderSnapshot, snapshot_id: str
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ordinal, market in enumerate(snapshot.markets):
            reference = market_reference(market)
            canonical_statistic_id = self._canonical_statistic_id(market)
            category = (
                None
                if canonical_statistic_id is None
                else self.market_categories.get(canonical_statistic_id)
            )
            canonical_player_id, canonical_player_name, athlete_team_id = (
                self._canonical_athlete_fields(market)
            )
            canonical_game_id = (
                None if market.event is None else market.event.canonical_id
            )
            canonical_team_id = (
                market.team.canonical_id
                if market.team is not None and market.team.canonical_id is not None
                else athlete_team_id
            )
            targetable = (
                market.status is MarketStatus.AVAILABLE
                and market.variant is MarketVariant.STANDARD
                and market.scoring_period is ScoringPeriod.FULL_GAME
                and canonical_player_id is not None
                and canonical_player_name is not None
                and canonical_game_id is not None
                and canonical_team_id is not None
                and category is not None
            )
            rows.append(
                {
                    "observation_id": _digest("obs", snapshot_id, ordinal, reference),
                    "snapshot_id": snapshot_id,
                    "ordinal": ordinal,
                    "provider": snapshot.provider,
                    "provider_market_id": market.market_id,
                    "market_reference": reference,
                    "canonical_game_id": canonical_game_id,
                    "canonical_player_id": canonical_player_id,
                    "canonical_team_id": canonical_team_id,
                    "canonical_statistic_id": canonical_statistic_id,
                    "market_category": category,
                    "market_status": market.status.value,
                    "market_variant": market.variant.value,
                    "scoring_period": market.scoring_period.value,
                    "targetable": targetable,
                    "observed_at": snapshot.retrieved_at,
                    "canonical_player_name": canonical_player_name,
                }
            )
        return rows

    @staticmethod
    def _canonical_statistic_id(market: PlayerProjectionMarket) -> str | None:
        match = market.statistic_match
        if match is None or match.state is not MatchState.CANONICAL:
            return None
        return match.canonical_id

    @staticmethod
    def _canonical_athlete_fields(
        market: PlayerProjectionMarket,
    ) -> tuple[int | None, str | None, int | None]:
        athlete = market.athlete
        if athlete is None:
            return None, None, None
        athlete_team_id = None if athlete.team is None else athlete.team.canonical_id
        return athlete.canonical_id, athlete.name, athlete_team_id

    @staticmethod
    def _advance_latest(
        connection: Any,
        observation_rows: list[dict[str, Any]],
        *,
        provider: str,
        season: str,
        query_key: str,
        generation_id: str,
    ) -> None:
        table = LatestPlayerProjection.__table__
        connection.execute(
            delete(table).where(
                table.c.provider == provider,
                table.c.season == season,
                table.c.query_key == query_key,
            )
        )
        materialized_references: set[str] = set()
        for row in observation_rows:
            if not row["targetable"]:
                continue
            reference = str(row["market_reference"])
            if reference in materialized_references:
                continue
            materialized_references.add(reference)
            connection.execute(
                insert(table).values(
                    provider=row["provider"],
                    season=season,
                    query_key=query_key,
                    canonical_game_id=row["canonical_game_id"],
                    canonical_player_id=row["canonical_player_id"],
                    market_reference=row["market_reference"],
                    observation_id=row["observation_id"],
                    generation_id=generation_id,
                    canonical_team_id=row["canonical_team_id"],
                    canonical_player_name=row["canonical_player_name"],
                    canonical_statistic_id=row["canonical_statistic_id"],
                    market_category=row["market_category"],
                    observed_at=row["observed_at"],
                )
            )


class LatestProjectionPlayerPoolReader:
    """Read the current Latest Player Projections; never call a provider."""

    def __init__(self, engine: Engine, scope: ProjectionArchiveReadScope) -> None:
        self.engine = engine
        self.scope = scope

    def get_pool_for_game(self, *, season: str, game_id: str) -> PlayerPool:
        return self.get_pool(season=season, game_ids=(game_id,))

    def get_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool:
        if season != self.scope.query.season:
            raise ValueError("projection archive read season is outside its scope")
        requested_games = tuple(sorted({str(game_id) for game_id in game_ids}))
        if not requested_games:
            return PlayerPool((), {}, PlayerPool.missing_projection_freshness(), {})
        table = LatestPlayerProjection.__table__
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(table).where(
                        table.c.season == season,
                        table.c.provider == self.scope.provider,
                        table.c.query_key == self.scope.query_key,
                        table.c.canonical_game_id.in_(requested_games),
                    )
                )
                .mappings()
                .all()
            )

        game_states = {
            game_id: self._state_for_rows(
                [row for row in rows if row["canonical_game_id"] == game_id]
            )
            for game_id in requested_games
        }
        if not rows:
            return PlayerPool(
                (),
                {},
                PlayerPool.missing_projection_freshness(),
                game_states,
            )

        contributions: dict[int, dict[str, Any]] = {}
        for row in rows:
            player_id = int(row["canonical_player_id"])
            entry = contributions.setdefault(
                player_id,
                {
                    "name": str(row["canonical_player_name"]),
                    "team_id": int(row["canonical_team_id"]),
                    "providers": {},
                },
            )
            entry["providers"].setdefault(str(row["provider"]), set()).add(
                str(row["market_category"])
            )
        players = tuple(
            PoolPlayer(
                canonical_player_id=player_id,
                name=entry["name"],
                team_id=entry["team_id"],
                market_categories=tuple(
                    sorted(
                        {
                            category
                            for categories in entry["providers"].values()
                            for category in categories
                        }
                    )
                ),
                provenance={
                    provider: tuple(sorted(categories))
                    for provider, categories in sorted(entry["providers"].items())
                },
            )
            for player_id, entry in sorted(contributions.items())
        )
        team_counts: dict[int, int] = {}
        for player in players:
            team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1
        observed_at = min(assume_utc(row["observed_at"]) for row in rows)
        provider_observed_at: dict[str, datetime] = {}
        for row in rows:
            provider = str(row["provider"])
            row_observed_at = assume_utc(row["observed_at"])
            provider_observed_at[provider] = min(
                provider_observed_at.get(provider, row_observed_at),
                row_observed_at,
            )
        providers = {
            provider: {
                "status": "fresh",
                "retrieved_at": provider_observed_at[provider].isoformat(),
            }
            for provider in sorted(provider_observed_at)
        }
        aggregate_status = (
            "partial"
            if any(state["state"] == "missing" for state in game_states.values())
            else "fresh"
        )
        freshness = {
            "status": aggregate_status,
            "state": "live",
            "observed_at": observed_at.isoformat(),
            "retrieved_at": observed_at.isoformat(),
            "providers": providers,
        }
        return PlayerPool(players, team_counts, freshness, game_states)

    @staticmethod
    def _state_for_rows(rows: list[Any]) -> dict[str, Any]:
        if not rows:
            return {"state": "missing", "observed_at": None}
        observed_at = min(assume_utc(row["observed_at"]) for row in rows)
        return {"state": "live", "observed_at": observed_at.isoformat()}


class ProjectionSelectionPlayerPoolReader:
    """Translate missing archive evidence to Selection's unavailable contract."""

    def __init__(self, reader: LatestProjectionPlayerPoolReader) -> None:
        self.reader = reader

    def get_pool_for_game(
        self,
        *,
        season: str,
        game_id: str,
    ) -> PlayerPool | None:
        pool = self.reader.get_pool_for_game(season=season, game_id=game_id)
        if pool.freshness.get("state") == "missing":
            return None
        return pool


class ProjectionRecordingService:
    """Application boundary for recording an already retrieved Complete snapshot."""

    def __init__(self, archive: ProjectionArchive) -> None:
        self.archive = archive

    def record_complete_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        query: NBAMarketQuery,
        accepted_at: datetime | None = None,
        poll_started_at: datetime | None = None,
    ) -> ProjectionArchiveResult:
        return self.archive.ingest_complete_snapshot(
            snapshot,
            query=query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
        )


__all__ = [
    "LatestProjectionPlayerPoolReader",
    "ProjectionArchive",
    "ProjectionArchiveReadScope",
    "ProjectionArchiveResult",
    "ProjectionRecordingService",
    "ProjectionSelectionPlayerPoolReader",
]
