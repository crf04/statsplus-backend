"""Resumable, newest-first PBP backfill and correction scheduling."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.domain.nba_events import (
    is_final_event,
    is_postponed_event,
    player_game_log_season_type,
)
from app.errors import ProviderUnavailableError
from app.services.canonical_game_ledger import (
    CanonicalGame,
    CanonicalGameLedgerRepository,
    LedgerBackfillProgress,
    LedgerValidationError,
    canonical_game_from_pbp,
)
from app.utils.telemetry import ProviderResponseError


class LedgerPBPProvider(Protocol):
    """Injected per-game PBP provider used by offline and Railway workers."""

    def fetch_game_player_logs(self, game_id: str, season: str, *, season_type: str = "Regular Season") -> object: ...


class LedgerEventCatalogReader(Protocol):
    def get_events(self, season: str) -> list[dict[str, object]]: ...

    def get_freshness(self, season: str, *, now: datetime) -> Mapping[str, object]: ...


class LedgerAthleteCatalogReader(Protocol):
    def get_catalog(self, season: str, *, active_only: bool = False) -> list[dict[str, object]]: ...

    def get_freshness(self, season: str, *, now: datetime) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class BackfillResult:
    season: str
    cutoff: datetime
    status: str
    complete: bool
    games_processed: int
    games_replaced: int
    games_skipped: int
    failed_game_ids: tuple[str, ...]
    pending_game_ids: tuple[str, ...]
    lower_priority_remaining: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Target:
    event: Mapping[str, object]
    priority: int


class LedgerBackfillService:
    """Fetch missing games and bounded recent corrections without partial publish."""

    def __init__(
        self,
        *,
        provider: LedgerPBPProvider,
        event_catalog: LedgerEventCatalogReader,
        repository: CanonicalGameLedgerRepository,
        athlete_catalog: LedgerAthleteCatalogReader | None = None,
        reconciliation_sink: Callable[[str, Mapping[str, object]], None] | None = None,
        max_concurrency: int = 3,
        daily_recheck_days: int = 7,
        weekly_recheck_days: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_concurrency, bool) or max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if daily_recheck_days < 0 or weekly_recheck_days < daily_recheck_days:
            raise ValueError("backfill recheck windows are invalid")
        self.provider = provider
        self.event_catalog = event_catalog
        self.repository = repository
        self.athlete_catalog = athlete_catalog
        self.reconciliation_sink = reconciliation_sink
        self.max_concurrency = max_concurrency
        self.daily_recheck_days = daily_recheck_days
        self.weekly_recheck_days = weekly_recheck_days
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def refresh(
        self,
        season: str,
        *,
        cutoff: datetime | None = None,
        max_games: int | None = None,
        historical_repair: bool = False,
    ) -> BackfillResult:
        """Run one bounded pass and persist resumable progress.

        Missing games always outrank scheduled rechecks.  A failed target
        causes no candidate game to be written in this pass; this is what
        preserves the previous valid publication when a correction batch is
        incomplete.
        """

        now = _aware(cutoff or self.clock())
        events = self._governed_events(season, through=now)
        expected_ids = frozenset(str(event["nba_game_id"]) for event in events)
        checksums = self.repository.game_checksums(season)
        summaries = {summary.game_id: summary for summary in self.repository.list_games(season, through=now.date())}
        targets = self._targets(events, checksums=checksums, summaries=summaries, now=now, repair=historical_repair)
        if max_games is not None:
            if isinstance(max_games, bool) or max_games < 1:
                raise ValueError("max_games must be a positive integer")
            selected = tuple(targets[:max_games])
            lower_priority_remaining = max(0, len(targets) - len(selected))
        else:
            selected = targets
            lower_priority_remaining = 0

        athlete_ids = self._athlete_ids(season, now=now)
        staged: list[CanonicalGame] = []
        failures: list[str] = []
        lock = threading.Lock()

        def fetch(target: _Target) -> tuple[str, CanonicalGame | None, str | None]:
            event = target.event
            game_id = str(event["nba_game_id"])
            try:
                observation = self.provider.fetch_game_player_logs(game_id, season, season_type="Regular Season")
                game = canonical_game_from_pbp(
                    observation,
                    event=event,
                    season=season,
                    source_observation_id=f"pbp:{game_id}:{now.isoformat()}",
                    retrieved_at=now,
                )
                if athlete_ids is not None:
                    unknown = [player.player_id for player in game.player_facts if player.player_id not in athlete_ids]
                    if unknown:
                        if self.reconciliation_sink is not None:
                            self.reconciliation_sink(game_id, {"kind": "athlete_identity", "count": len(unknown)})
                        raise LedgerValidationError("PBP game contains an unresolved Athlete Catalog identity")
                return game_id, game, None
            except (
                KeyError,
                TypeError,
                ValueError,
                RuntimeError,
                OSError,
                ProviderResponseError,
                ProviderUnavailableError,
            ) as error:
                return game_id, None, _safe_reason(error)

        if selected:
            with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
                futures = [executor.submit(fetch, target) for target in selected]
                for future in as_completed(futures):
                    game_id, game, error = future.result()
                    if error is not None:
                        with lock:
                            failures.append(game_id)
                        continue
                    if game is not None:
                        with lock:
                            staged.append(game)

        if failures:
            self._save_progress(
                season,
                cutoff=now,
                completed=checksums.keys(),
                failed=failures,
                status="incomplete",
                cursor=selected[-1].event.get("nba_game_id") if selected else None,
                last_error="one or more PBP game observations failed",
            )
            missing = tuple(sorted(expected_ids - set(checksums)))
            return BackfillResult(
                season=season,
                cutoff=now,
                status="unavailable",
                complete=False,
                games_processed=len(staged),
                games_replaced=0,
                games_skipped=max(0, len(checksums) - len(targets)),
                failed_game_ids=tuple(sorted(failures)),
                pending_game_ids=missing,
                lower_priority_remaining=lower_priority_remaining,
                reason="incomplete PBP evidence; prior publication retained",
            )

        writes = self.repository.replace_games_atomic(staged) if staged else ()
        resulting_ids = set(self.repository.game_checksums(season))
        complete = expected_ids.issubset(resulting_ids)
        pending = tuple(sorted(expected_ids - resulting_ids))
        status = "complete" if complete else "unavailable"
        self._save_progress(
            season,
            cutoff=now,
            completed=resulting_ids,
            failed=(),
            status=status,
            cursor=selected[-1].event.get("nba_game_id") if selected else None,
            last_error=None if complete else "ledger is missing governed completed games",
        )
        return BackfillResult(
            season=season,
            cutoff=now,
            status=status,
            complete=complete,
            games_processed=len(staged),
            games_replaced=sum(1 for write in writes if write.replaced),
            games_skipped=sum(1 for write in writes if not write.inserted and not write.replaced) + max(0, len(checksums) - len(targets)),
            failed_game_ids=(),
            pending_game_ids=pending,
            lower_priority_remaining=lower_priority_remaining,
            reason=None if complete else "incomplete ledger publication",
        )

    def _governed_events(self, season: str, *, through: datetime) -> tuple[dict[str, object], ...]:
        raw_events = self.event_catalog.get_events(season)
        freshness_getter = getattr(self.event_catalog, "get_freshness", None)
        if callable(freshness_getter):
            try:
                freshness = freshness_getter(season, now=through)
            except TypeError:
                freshness = freshness_getter(season)
            if not isinstance(freshness, Mapping):
                raise LedgerValidationError("Event Catalog freshness evidence is malformed")
            fresh = freshness.get("fresh", freshness.get("is_fresh", False))
            event_count = freshness.get("event_count")
            if not fresh or (event_count is not None and int(event_count) != len(raw_events)):
                raise LedgerValidationError("Event Catalog must be fresh and complete before backfill")
        events = []
        for raw in raw_events:
            event = dict(raw)
            if player_game_log_season_type(event) != "Regular Season":
                continue
            if not is_final_event(event) or is_postponed_event(event):
                continue
            scheduled = _event_datetime(event.get("scheduled_at"))
            if scheduled > through:
                continue
            if not event.get("nba_game_id"):
                continue
            events.append(event)
        return tuple(sorted(events, key=lambda event: (str(event.get("scheduled_at")), str(event.get("nba_game_id"))), reverse=True))

    def _targets(
        self,
        events: Iterable[Mapping[str, object]],
        *,
        checksums: Mapping[str, str],
        summaries: Mapping[str, object],
        now: datetime,
        repair: bool,
    ) -> tuple[_Target, ...]:
        targets: list[_Target] = []
        for event in events:
            game_id = str(event["nba_game_id"])
            summary = summaries.get(game_id)
            if game_id not in checksums:
                targets.append(_Target(event, 0))
                continue
            if repair:
                targets.append(_Target(event, 1))
                continue
            age_days = (now.date() - _event_datetime(event.get("scheduled_at")).date()).days
            if age_days <= self.daily_recheck_days:
                targets.append(_Target(event, 1))
            elif age_days <= self.weekly_recheck_days and summary is not None:
                last_fetch = _aware(summary.retrieved_at)
                if now - last_fetch >= timedelta(days=7):
                    targets.append(_Target(event, 2))
        return tuple(
            sorted(
                targets,
                key=lambda target: (
                    target.priority,
                    -_event_datetime(target.event.get("scheduled_at")).timestamp(),
                    str(target.event.get("nba_game_id")),
                ),
            )
        )

    def _athlete_ids(self, season: str, *, now: datetime) -> frozenset[int] | None:
        if self.athlete_catalog is None:
            return None
        freshness_getter = getattr(self.athlete_catalog, "get_freshness", None)
        if callable(freshness_getter):
            try:
                freshness = freshness_getter(season, now=now)
            except TypeError:
                freshness = freshness_getter(season)
            if not isinstance(freshness, Mapping) or not freshness.get("is_fresh", freshness.get("fresh", False)):
                raise LedgerValidationError("Athlete Catalog must be fresh before backfill")
        rows = self.athlete_catalog.get_catalog(season, active_only=False)
        return frozenset(int(row["player_id"]) for row in rows if row.get("player_id") is not None)

    def _save_progress(
        self,
        season: str,
        *,
        cutoff: datetime,
        completed: Iterable[str],
        failed: Iterable[str],
        status: str,
        cursor: object,
        last_error: str | None,
    ) -> None:
        self.repository.save_progress(
            LedgerBackfillProgress(
                season=season,
                cutoff=cutoff,
                cursor_game_id=str(cursor) if cursor is not None else None,
                completed_game_ids=frozenset(str(value) for value in completed),
                failed_game_ids=frozenset(str(value) for value in failed),
                status=status,
                updated_at=cutoff,
                last_error=last_error,
            )
        )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _event_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError) as error:
        raise LedgerValidationError("governed event is missing scheduled_at") from error


def _safe_reason(error: Exception) -> str:
    del error
    # Provider text can contain credentials, IDs, and unbounded upstream
    # details; progress stores one closed reason only.
    return "provider_unavailable"


__all__ = [
    "BackfillResult",
    "LedgerAthleteCatalogReader",
    "LedgerBackfillService",
    "LedgerEventCatalogReader",
    "LedgerPBPProvider",
]
