"""Build and persist the governed slate Player Pool from normalized DFS boards."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import logging
import time
from typing import Any, Callable, Iterable, Mapping, Protocol
from uuid import uuid4

from sqlalchemy.engine import Connection

from app.domain.freshness import (
    exact_age_seconds,
    exact_seconds,
    time_window_seconds,
    within_max_age,
)
from app.domain.statistics import MatchState, ScoringPeriod
from app.domain.utc import assume_utc, parse_utc_iso
from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    EventEvidence,
    MarketStatus,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
)
from app.services.athlete_mapping_errors import AthleteMappingPersistenceError
from app.services.dfs_board import DFSBoard, ProviderOutcome
from app.services.event_mapping_errors import EventMappingPersistenceError
from app.services.player_pool_snapshot_repository import (
    PlayerPoolRefreshResult,
    PlayerPoolSnapshotScope,
    StoredPlayerPoolSnapshot,
    StoredPlayerPoolSnapshotCandidate,
)
from app.services.publication_snapshot_calls import call_with_read_scope
from app.services.statistic_catalog import StatisticCatalog
from app.utils.telemetry import (
    BoundedPlayerPoolTelemetryRecorder,
    PlayerPoolTelemetryRecorder,
    PlayerPoolTelemetryEvent,
)


POOL_REUSE_MAX_AGE_SECONDS = time_window_seconds(
    15, unit_seconds=60, field="Player Pool reuse maximum age"
)
POOL_STALE_MAX_AGE_SECONDS = time_window_seconds(
    6, unit_seconds=3600, field="Player Pool stale-serve maximum age"
)
REFRESH_LEASE_SECONDS = time_window_seconds(
    60, field="Player Pool refresh lease"
)
FOLLOWER_WAIT_MAX_SECONDS = time_window_seconds(
    65, field="Player Pool follower wait maximum"
)
FOLLOWER_POLL_INITIAL_SECONDS = time_window_seconds(
    "0.05", field="Player Pool follower initial poll"
)
FOLLOWER_POLL_MAX_SECONDS = time_window_seconds(
    "0.5", field="Player Pool follower maximum poll"
)
POOL_CLOCK_SKEW_TOLERANCE_SECONDS = time_window_seconds(
    60, field="Player Pool clock-skew tolerance"
)
EXPECTED_COLLECTION_FAILURES = (
    ProviderUnavailableError,
    AthleteMappingPersistenceError,
    EventMappingPersistenceError,
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PoolPlayer:
    """One canonical targetable player and source-board provenance."""

    canonical_player_id: int
    name: str
    team_id: int
    market_categories: tuple[str, ...]
    provenance: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class PlayerPool:
    """The targetable union and the provider observations that produced it."""

    players: tuple[PoolPlayer, ...]
    team_counts: Mapping[int, int]
    freshness: Mapping[str, Any]
    game_states: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @staticmethod
    def unavailable_freshness() -> dict[str, Any]:
        return {"status": "unavailable", "retrieved_at": None, "providers": {}}

    @staticmethod
    def missing_projection_freshness() -> dict[str, Any]:
        """Extend unavailable freshness with the archive reader's public state."""

        freshness = PlayerPool.unavailable_freshness()
        freshness.update({"state": "missing", "observed_at": None})
        return freshness


@dataclass(slots=True)
class _PlayerContribution:
    team_id: int
    name: str
    providers: dict[str, set[str]]


@dataclass(frozen=True, slots=True)
class _MappedEvent:
    game_id: str
    team_ids: frozenset[int]


class DFSBoardReader(Protocol):
    def get_board(self, query: NBAMarketQuery) -> DFSBoard: ...


class PlayerPoolSnapshotReaderWriter(Protocol):
    def get(self, scope: PlayerPoolSnapshotScope) -> StoredPlayerPoolSnapshot | None: ...

    def try_acquire_refresh(
        self,
        scope: PlayerPoolSnapshotScope,
        *,
        owner: str,
        now: datetime,
        lease_seconds: object,
    ) -> bool: ...

    def get_refresh_result(
        self, scope: PlayerPoolSnapshotScope
    ) -> PlayerPoolRefreshResult: ...

    def replace_owned(
        self,
        scope: PlayerPoolSnapshotScope,
        *,
        owner: str,
        payload: Mapping[str, Any],
        retrieved_at: datetime,
        now: datetime,
    ) -> bool: ...

    def finish_failure_owned(
        self,
        scope: PlayerPoolSnapshotScope,
        *,
        owner: str,
        now: datetime,
    ) -> bool: ...

    def release_refresh(self, scope: PlayerPoolSnapshotScope, *, owner: str) -> None: ...


class PlayerPoolReader(Protocol):
    def get_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool: ...


class SingleGamePlayerPoolReader(Protocol):
    """Read one game's governed Player Pool without prescribing storage."""

    def get_pool_for_game(
        self,
        *,
        season: str,
        game_id: str,
        connection: Connection | None = None,
    ) -> PlayerPool | None: ...


class StoredPlayerPoolSnapshotReader(Protocol):
    def get(
        self,
        scope: PlayerPoolSnapshotScope,
        *,
        connection: Connection | None = None,
    ) -> StoredPlayerPoolSnapshot | None: ...

    def list_containing_game(
        self,
        season: str,
        game_id: str,
        *,
        connection: Connection | None = None,
    ) -> tuple[StoredPlayerPoolSnapshotCandidate, ...]: ...


class StoredPlayerPoolReader:
    """Read a governed stored slate scope without leases or provider access."""

    def __init__(
        self,
        snapshot_repository: StoredPlayerPoolSnapshotReader,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.snapshot_repository = snapshot_repository
        self._clock = clock

    def get_pool_for_game(
        self,
        *,
        season: str,
        game_id: str,
        connection: Connection | None = None,
    ) -> PlayerPool | None:
        try:
            candidates = call_with_read_scope(
                self.snapshot_repository.list_containing_game,
                season,
                game_id,
                connection=connection,
            )
        except (KeyError, TypeError, ValueError):
            return None
        now = self._clock()
        unusable_scopes: set[PlayerPoolSnapshotScope] = set()
        for maximum_age, serve_stale in (
            (POOL_REUSE_MAX_AGE_SECONDS, False),
            (POOL_STALE_MAX_AGE_SECONDS, True),
        ):
            for candidate in candidates:
                if candidate.scope in unusable_scopes:
                    continue
                try:
                    if not PlayerPoolService._within_age(
                        candidate, now, maximum_age
                    ):
                        continue
                    stored = call_with_read_scope(
                        self.snapshot_repository.get,
                        candidate.scope,
                        connection=connection,
                    )
                    if stored is None or not PlayerPoolService._within_age(
                        stored, now, maximum_age
                    ):
                        continue
                    pool = (
                        PlayerPoolService._stale_pool(stored.payload, {})
                        if serve_stale
                        else PlayerPoolService._decode_pool(stored.payload)
                    )
                except (KeyError, TypeError, ValueError):
                    unusable_scopes.add(candidate.scope)
                    continue
                return pool
        return None


class PlayerPoolService:
    """Collect, persist, and reuse qualifying joined Player Pool markets."""

    def __init__(
        self,
        board_service: DFSBoardReader,
        statistic_catalog: StatisticCatalog,
        *,
        telemetry_recorder: PlayerPoolTelemetryRecorder | None = None,
        snapshot_repository: PlayerPoolSnapshotReaderWriter | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.board_service = board_service
        self.market_categories = {
            statistic.id: statistic.market_category
            for statistic in statistic_catalog.statistics
            if statistic.market_category is not None
        }
        self.telemetry_recorder = (
            telemetry_recorder or BoundedPlayerPoolTelemetryRecorder()
        )
        self.snapshot_repository = snapshot_repository
        self.clock = clock
        self.sleeper = sleeper
        self.monotonic = monotonic

    def get_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool:
        scope = PlayerPoolSnapshotScope.create(season, game_ids)
        repository = self.snapshot_repository
        if repository is None:
            return self._collect_pool(season=scope.season, game_ids=scope.game_ids)

        baseline = repository.get_refresh_result(scope).version
        waiting = False
        wait_deadline = self.monotonic() + float(FOLLOWER_WAIT_MAX_SECONDS)
        poll_seconds = float(FOLLOWER_POLL_INITIAL_SECONDS)
        while True:
            now = assume_utc(self.clock())
            stored = repository.get(scope)
            if self._within_age(stored, now, POOL_REUSE_MAX_AGE_SECONDS):
                return self._decode_pool(stored.payload)

            completed = repository.get_refresh_result(scope)
            if waiting:
                if completed.version > baseline:
                    completed_stored = repository.get(scope)
                    if completed.outcome == "success" and completed_stored is not None:
                        return self._decode_pool(completed_stored.payload)
                    if completed.outcome == "failure":
                        return self._fallback_pool(completed_stored, now, {})
                    baseline = completed.version

            lease_expires_at = completed.lease_expires_at
            if lease_expires_at is not None and assume_utc(lease_expires_at) > now:
                waiting = True
                if self.monotonic() >= wait_deadline:
                    return self._fallback_pool(stored, now, {})
                self.sleeper(poll_seconds)
                poll_seconds = min(
                    poll_seconds * 2, float(FOLLOWER_POLL_MAX_SECONDS)
                )
                continue

            owner = uuid4().hex
            if repository.try_acquire_refresh(
                scope,
                owner=owner,
                now=now,
                lease_seconds=REFRESH_LEASE_SECONDS,
            ):
                return self._refresh_owned(
                    scope, owner=owner, prior=stored, baseline_version=baseline
                )
            waiting = True
            if self.monotonic() >= wait_deadline:
                return self._fallback_pool(stored, now, {})
            self.sleeper(poll_seconds)
            poll_seconds = min(poll_seconds * 2, float(FOLLOWER_POLL_MAX_SECONDS))

    def _refresh_owned(
        self,
        scope: PlayerPoolSnapshotScope,
        *,
        owner: str,
        prior: StoredPlayerPoolSnapshot | None,
        baseline_version: int,
    ) -> PlayerPool:
        repository = self.snapshot_repository
        if repository is None:  # narrowed by the caller; retained for protocol safety
            raise RuntimeError("snapshot repository is required for an owned refresh")
        try:
            now = assume_utc(self.clock())
            completed = repository.get_refresh_result(scope)
            stored = repository.get(scope)
            if self._within_age(stored, now, POOL_REUSE_MAX_AGE_SECONDS):
                return self._decode_pool(stored.payload)
            if completed.version > baseline_version:
                completed_stored = repository.get(scope)
                if completed.outcome == "success" and completed_stored is not None:
                    return self._decode_pool(completed_stored.payload)
                if completed.outcome == "failure":
                    return self._fallback_pool(completed_stored or prior, now, {})
            fallback = stored or prior
            try:
                refreshed = self._collect_pool(
                    season=scope.season, game_ids=scope.game_ids
                )
            except EXPECTED_COLLECTION_FAILURES:
                finished_at = assume_utc(self.clock())
                recorded = repository.finish_failure_owned(
                    scope, owner=owner, now=finished_at
                )
                if not recorded:
                    logger.warning("player_pool_refresh outcome=fence_lost")
                return self._fallback_pool(fallback, finished_at, {})

            providers = refreshed.freshness["providers"]
            finished_at = assume_utc(self.clock())
            total_failure = not providers or all(
                provider["status"] == "missing" for provider in providers.values()
            )
            if total_failure:
                recorded = repository.finish_failure_owned(
                    scope, owner=owner, now=finished_at
                )
                if not recorded:
                    logger.warning("player_pool_refresh outcome=fence_lost")
                return self._fallback_pool(fallback, finished_at, providers)

            retrieved_at = refreshed.freshness.get("retrieved_at")
            if retrieved_at is not None:
                published = repository.replace_owned(
                    scope,
                    owner=owner,
                    payload=self._encode_pool(refreshed),
                    retrieved_at=parse_utc_iso(retrieved_at),
                    now=finished_at,
                )
                if not published:
                    logger.warning("player_pool_refresh outcome=fence_lost")
                    winner = repository.get_refresh_result(scope)
                    if winner.version > baseline_version and winner.outcome == "success":
                        winner_snapshot = repository.get(scope)
                        if winner_snapshot is not None:
                            return self._decode_pool(winner_snapshot.payload)
            return refreshed
        finally:
            repository.release_refresh(scope, owner=owner)

    @staticmethod
    def _within_age(
        stored: StoredPlayerPoolSnapshot | StoredPlayerPoolSnapshotCandidate | None,
        now: datetime,
        maximum_age: Any,
    ) -> bool:
        if stored is None:
            return False
        elapsed = exact_seconds(assume_utc(now) - assume_utc(stored.retrieved_at))
        if elapsed < 0:
            future_skew = exact_age_seconds(-elapsed, field="Player Pool future skew")
            if not within_max_age(future_skew, POOL_CLOCK_SKEW_TOLERANCE_SECONDS):
                raise ValueError("Player Pool snapshot timestamp is too far in the future")
            elapsed = Decimal(0)
        age = exact_age_seconds(elapsed, field="Player Pool snapshot age")
        return within_max_age(age, maximum_age)

    def _fallback_pool(
        self,
        stored: StoredPlayerPoolSnapshot | None,
        now: datetime,
        failed_providers: Mapping[str, Any],
    ) -> PlayerPool:
        if self._within_age(stored, now, POOL_STALE_MAX_AGE_SECONDS):
            return self._stale_pool(stored.payload, failed_providers)
        return PlayerPool((), {}, self._unavailable_freshness(failed_providers))

    def _collect_pool(self, *, season: str, game_ids: Iterable[str]) -> PlayerPool:
        board = self.board_service.get_board(
            NBAMarketQuery(
                season=season,
                market_statuses=(MarketStatus.AVAILABLE,),
            )
        )
        slate_games = frozenset(str(game_id) for game_id in game_ids)
        athlete_mappings = self._athlete_mappings(board)
        event_mappings = self._event_mappings(board)
        contributions: dict[int, _PlayerContribution] = {}
        unknown_label_count = 0
        unjoined_athlete_count = 0
        unjoined_event_count = 0
        team_mismatch_count = 0

        for provider_outcome in board.provider_outcomes:
            if not provider_outcome.usable or provider_outcome.snapshot is None:
                continue
            provider = provider_outcome.provider
            for market in provider_outcome.snapshot.markets:
                if not self._qualifying_shape(market):
                    continue
                event_key = self._evidence_key(provider, market.event)
                if event_key is None or event_key not in event_mappings:
                    unjoined_event_count += 1
                    continue
                mapped_event = event_mappings[event_key]
                if mapped_event.game_id not in slate_games:
                    continue
                category = self._market_category(market)
                if category is None:
                    unknown_label_count += 1
                    continue
                athlete_key = self._evidence_key(provider, market.athlete)
                canonical = (
                    None if athlete_key is None else athlete_mappings.get(athlete_key)
                )
                if canonical is None:
                    unjoined_athlete_count += 1
                    continue
                player_id, team_id, name = canonical
                if team_id not in mapped_event.team_ids:
                    team_mismatch_count += 1
                    continue
                entry = contributions.setdefault(
                    player_id,
                    _PlayerContribution(team_id=team_id, name=name, providers={}),
                )
                entry.providers.setdefault(provider, set()).add(category)

        players = tuple(
            PoolPlayer(
                canonical_player_id=player_id,
                name=entry.name,
                team_id=entry.team_id,
                market_categories=tuple(
                    sorted(
                        {
                            category
                            for categories in entry.providers.values()
                            for category in categories
                        }
                    )
                ),
                provenance={
                    provider: tuple(sorted(categories))
                    for provider, categories in sorted(entry.providers.items())
                },
            )
            for player_id, entry in sorted(contributions.items())
        )
        team_counts: dict[int, int] = {}
        for player in players:
            team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1

        self.telemetry_recorder.record(
            PlayerPoolTelemetryEvent(
                unknown_stat_label_count=unknown_label_count,
                unjoined_athlete_count=unjoined_athlete_count,
                unjoined_event_count=unjoined_event_count,
                team_mismatch_count=team_mismatch_count,
            )
        )
        return PlayerPool(
            players=players,
            team_counts=team_counts,
            freshness=self._freshness(board.provider_outcomes),
        )

    @staticmethod
    def _encode_pool(pool: PlayerPool) -> dict[str, Any]:
        return {
            "players": [
                {
                    "canonical_player_id": player.canonical_player_id,
                    "name": player.name,
                    "team_id": player.team_id,
                    "market_categories": list(player.market_categories),
                    "provenance": {
                        provider: list(categories)
                        for provider, categories in player.provenance.items()
                    },
                }
                for player in pool.players
            ],
            "team_counts": {
                str(team_id): count for team_id, count in pool.team_counts.items()
            },
            "freshness": dict(pool.freshness),
        }

    @staticmethod
    def _decode_pool(payload: Mapping[str, Any]) -> PlayerPool:
        players = tuple(
            PoolPlayer(
                canonical_player_id=int(player["canonical_player_id"]),
                name=str(player["name"]),
                team_id=int(player["team_id"]),
                market_categories=tuple(player["market_categories"]),
                provenance={
                    str(provider): tuple(categories)
                    for provider, categories in player["provenance"].items()
                },
            )
            for player in payload["players"]
        )
        return PlayerPool(
            players=players,
            team_counts={
                int(team_id): int(count)
                for team_id, count in payload["team_counts"].items()
            },
            freshness=PlayerPoolService._normalize_freshness(payload["freshness"]),
        )

    @staticmethod
    def _normalize_freshness(value: Mapping[str, Any]) -> dict[str, Any]:
        providers = {
            str(provider): {
                "status": state["status"],
                "retrieved_at": (
                    parse_utc_iso(state["retrieved_at"]).isoformat()
                    if state.get("retrieved_at") is not None
                    else None
                ),
            }
            for provider, state in value["providers"].items()
        }
        normalized = {
            "retrieved_at": (
                parse_utc_iso(value["retrieved_at"]).isoformat()
                if value.get("retrieved_at") is not None
                else None
            ),
            "providers": providers,
        }
        if "status" in value:
            normalized["status"] = value["status"]
        return normalized

    @classmethod
    def _stale_pool(
        cls,
        payload: Mapping[str, Any],
        failed_providers: Mapping[str, Any],
    ) -> PlayerPool:
        pool = cls._decode_pool(payload)
        providers = {
            provider: {
                "status": (
                    "stale-served" if state["retrieved_at"] is not None else "missing"
                ),
                "retrieved_at": state["retrieved_at"],
            }
            for provider, state in pool.freshness["providers"].items()
        }
        for provider in failed_providers:
            providers.setdefault(
                provider, {"status": "missing", "retrieved_at": None}
            )
        return PlayerPool(
            players=pool.players,
            team_counts=pool.team_counts,
            freshness={
                "status": "stale-served",
                "retrieved_at": pool.freshness["retrieved_at"],
                "providers": providers,
            },
        )

    @staticmethod
    def _unavailable_freshness(
        failed_providers: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "retrieved_at": None,
            "providers": {
                provider: {"status": "missing", "retrieved_at": None}
                for provider in sorted(failed_providers)
            },
        }

    @staticmethod
    def _qualifying_shape(market: PlayerProjectionMarket) -> bool:
        return (
            market.status is MarketStatus.AVAILABLE
            and market.variant is MarketVariant.STANDARD
            and market.scoring_period is ScoringPeriod.FULL_GAME
        )

    def _market_category(self, market: PlayerProjectionMarket) -> str | None:
        match = market.statistic_match
        if match is None or match.state is not MatchState.CANONICAL:
            return None
        return self.market_categories.get(match.canonical_id)

    @staticmethod
    def _evidence_key(
        provider: str, evidence: AthleteEvidence | EventEvidence | None
    ) -> tuple[str, str] | None:
        provider_id = None if evidence is None else evidence.provider_id
        if not isinstance(provider_id, str) or not provider_id.strip():
            return None
        return provider, provider_id.strip()

    @classmethod
    def _athlete_mappings(
        cls, board: DFSBoard
    ) -> dict[tuple[str, str], tuple[int, int, str]]:
        result = {}
        for outcome in board.mapping_outcomes:
            player_id = outcome.canonical_player_id
            resolution = outcome.resolution
            canonical = resolution.canonical_athlete
            key = cls._evidence_key(resolution.provider, resolution.provider_evidence)
            if (
                key is None
                or player_id is None
                or canonical is None
                or canonical.team_id is None
                or canonical.player_id != player_id
            ):
                continue
            result[key] = (
                int(player_id),
                int(canonical.team_id),
                str(canonical.display_name),
            )
        return result

    @staticmethod
    def _event_mappings(board: DFSBoard) -> dict[tuple[str, str], _MappedEvent]:
        result = {}
        for outcome in board.event_mapping_outcomes:
            resolution = outcome.resolution
            game_id = outcome.canonical_event_id
            canonical = resolution.canonical_event
            key = PlayerPoolService._evidence_key(
                resolution.provider, resolution.provider_evidence
            )
            if (
                key is not None
                and game_id is not None
                and canonical is not None
                and canonical.nba_game_id == str(game_id)
            ):
                team_ids = frozenset(
                    team_id
                    for team_id in (canonical.home_team_id, canonical.away_team_id)
                    if team_id is not None
                )
                result[key] = _MappedEvent(str(game_id), team_ids)
        return result

    @staticmethod
    def _freshness(outcomes: Iterable[ProviderOutcome]) -> dict[str, Any]:
        providers = {}
        retrieved: list[datetime] = []
        for outcome in sorted(outcomes, key=lambda value: value.provider):
            if outcome.usable and outcome.snapshot is not None:
                observed_at = assume_utc(outcome.snapshot.retrieved_at)
                retrieved.append(observed_at)
                providers[outcome.provider] = {
                    "status": (
                        "stale-served" if outcome.cache_status == "stale" else "fresh"
                    ),
                    "retrieved_at": observed_at.isoformat(),
                }
            else:
                providers[outcome.provider] = {
                    "status": "missing",
                    "retrieved_at": None,
                }
        freshness = {
            "retrieved_at": min(retrieved).isoformat() if retrieved else None,
            "providers": providers,
        }
        statuses = {provider["status"] for provider in providers.values()}
        if not retrieved:
            freshness["status"] = "unavailable"
        elif statuses == {"fresh"}:
            freshness["status"] = "fresh"
        elif statuses == {"stale-served"}:
            freshness["status"] = "stale-served"
        return freshness


__all__ = [
    "DFSBoardReader",
    "PlayerPool",
    "PlayerPoolReader",
    "SingleGamePlayerPoolReader",
    "PlayerPoolService",
    "PoolPlayer",
    "StoredPlayerPoolReader",
]
