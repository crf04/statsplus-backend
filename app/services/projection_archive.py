"""Durable normalized projection evidence and its database-first live read model."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from app.domain.comparisons import market_reference
from app.domain.statistics import MatchState, ScoringPeriod
from app.domain.utc import assume_utc
from app.models.projection_archive import (
    LatestPlayerProjection,
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


LIVE_PROJECTION_MAX_AGE = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class ProjectionArchiveResult:
    """Observable result of accepting one normalized provider snapshot."""

    snapshot_id: str
    generation_id: str
    changed: bool
    observation_count: int


def _digest(prefix: str, *values: object) -> str:
    payload = "\x1f".join(str(value) for value in values).encode("utf-8")
    return f"{prefix}_{sha256(payload).hexdigest()}"


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
        ",".join(status.value for status in query.market_statuses),
        query.pregame_only,
    )


class ProjectionArchive:
    """Accept a complete normalized snapshot as one atomic archive generation."""

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
        source = _source_snapshot(snapshot)
        query_key = _query_key(query)
        document = serialize_provider_snapshot(source, query)
        checksum = sha256(f"{query_key}\x1f{document}".encode("utf-8")).hexdigest()
        snapshot_id = f"psn_{checksum}"
        generation_id = _digest("gen", snapshot_id)
        poll_id = _digest("poll", snapshot_id, accepted.isoformat())
        if accepted < assume_utc(snapshot.retrieved_at):
            raise ValueError("projection snapshot cannot be accepted before retrieval")
        snapshot_table = ProjectionProviderSnapshot.__table__

        with self.engine.begin() as connection:
            existing = connection.execute(
                select(snapshot_table.c.snapshot_id).where(
                    snapshot_table.c.checksum == checksum
                )
            ).scalar_one_or_none()
            if existing is not None:
                return ProjectionArchiveResult(
                    snapshot_id=str(existing),
                    generation_id=generation_id,
                    changed=False,
                    observation_count=len(snapshot.markets),
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
                )
            )
            self._advance_latest(
                connection,
                observation_rows,
                season=query.season,
                generation_id=generation_id,
            )
            connection.execute(
                insert(ProviderPoll.__table__).values(
                    poll_id=poll_id,
                    provider=snapshot.provider,
                    season=query.season,
                    query_key=query_key,
                    started_at=accepted,
                    completed_at=accepted,
                    outcome="changed",
                    snapshot_id=snapshot_id,
                    observation_count=len(observation_rows),
                )
            )

        return ProjectionArchiveResult(
            snapshot_id=snapshot_id,
            generation_id=generation_id,
            changed=True,
            observation_count=len(observation_rows),
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
        checksum = sha256(
            f"{row['query_key']}\x1f{document}".encode("utf-8")
        ).hexdigest()
        if checksum != row["checksum"]:
            raise ValueError("archived projection snapshot checksum is invalid")
        return deserialize_provider_snapshot(document)

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
            canonical_player_id = (
                None if market.athlete is None else market.athlete.canonical_id
            )
            canonical_game_id = (
                None if market.event is None else market.event.canonical_id
            )
            canonical_team_id = self._canonical_team_id(market)
            targetable = (
                market.status is MarketStatus.AVAILABLE
                and market.variant is MarketVariant.STANDARD
                and market.scoring_period is ScoringPeriod.FULL_GAME
                and canonical_player_id is not None
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
                    "canonical_player_name": (
                        None if market.athlete is None else market.athlete.name
                    ),
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
    def _canonical_team_id(market: PlayerProjectionMarket) -> int | None:
        if market.team is not None and market.team.canonical_id is not None:
            return market.team.canonical_id
        if market.athlete is not None and market.athlete.team is not None:
            return market.athlete.team.canonical_id
        return None

    @staticmethod
    def _advance_latest(
        connection: Any,
        observation_rows: list[dict[str, Any]],
        *,
        season: str,
        generation_id: str,
    ) -> None:
        table = LatestPlayerProjection.__table__
        for row in observation_rows:
            if not row["targetable"]:
                continue
            identity = (
                (table.c.provider == row["provider"])
                & (table.c.season == season)
                & (table.c.canonical_game_id == row["canonical_game_id"])
                & (table.c.canonical_player_id == row["canonical_player_id"])
                & (table.c.market_reference == row["market_reference"])
            )
            connection.execute(delete(table).where(identity))
            connection.execute(
                insert(table).values(
                    provider=row["provider"],
                    season=season,
                    canonical_game_id=row["canonical_game_id"],
                    canonical_player_id=row["canonical_player_id"],
                    market_reference=row["market_reference"],
                    observation_id=row["observation_id"],
                    generation_id=generation_id,
                    canonical_team_id=row["canonical_team_id"],
                    canonical_player_name=(
                        row["canonical_player_name"] or str(row["canonical_player_id"])
                    ),
                    canonical_statistic_id=row["canonical_statistic_id"],
                    market_category=row["market_category"],
                    observed_at=row["observed_at"],
                )
            )


class LatestProjectionPlayerPoolReader:
    """Read only fresh Latest Player Projections; never call a provider."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        maximum_age: timedelta = LIVE_PROJECTION_MAX_AGE,
    ) -> None:
        self.engine = engine
        self.clock = clock
        self.maximum_age = maximum_age

    def get_pool_for_game(self, *, season: str, game_id: str) -> PlayerPool:
        return self.get_pool(season=season, game_ids=(game_id,))

    def get_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool:
        requested_games = tuple(sorted({str(game_id) for game_id in game_ids}))
        if not requested_games:
            return PlayerPool((), {}, self._missing_freshness(), {})
        table = LatestPlayerProjection.__table__
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(table).where(
                        table.c.season == season,
                        table.c.canonical_game_id.in_(requested_games),
                    )
                )
                .mappings()
                .all()
            )

        now = assume_utc(self.clock())
        fresh_rows = [
            row
            for row in rows
            if timedelta(0) <= now - assume_utc(row["observed_at"]) <= self.maximum_age
        ]
        game_states = {
            game_id: self._state_for_rows(
                [row for row in fresh_rows if row["canonical_game_id"] == game_id]
            )
            for game_id in requested_games
        }
        if not fresh_rows:
            return PlayerPool((), {}, self._missing_freshness(), game_states)

        contributions: dict[int, dict[str, Any]] = {}
        for row in fresh_rows:
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
        observed_at = min(assume_utc(row["observed_at"]) for row in fresh_rows)
        providers = {
            str(row["provider"]): {
                "status": "fresh",
                "retrieved_at": assume_utc(row["observed_at"]).isoformat(),
            }
            for row in sorted(fresh_rows, key=lambda value: str(value["provider"]))
        }
        freshness = {
            "status": "fresh",
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

    @staticmethod
    def _missing_freshness() -> dict[str, Any]:
        return {
            "status": "unavailable",
            "state": "missing",
            "observed_at": None,
            "retrieved_at": None,
            "providers": {},
        }


__all__ = [
    "LatestProjectionPlayerPoolReader",
    "ProjectionArchive",
    "ProjectionArchiveResult",
]
