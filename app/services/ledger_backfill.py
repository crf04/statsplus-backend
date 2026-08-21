"""Resumable, newest-first PBP backfill and correction scheduling."""

from __future__ import annotations

import threading
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.models.collection_control import CollectionManifest
from app.models.event_catalog import EventCatalogEntry

from app.domain.nba_events import (
    is_final_event,
    is_postponed_event,
    player_game_log_season_type,
)
from app.errors import ProviderUnavailableError
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LedgerBackfillProgress,
    LedgerValidationError,
    canonical_game_from_pbp,
)
from app.utils.telemetry import ProviderResponseError


class LedgerPBPProvider(Protocol):
    """Injected per-game PBP provider used by offline and Railway workers."""

    def fetch_game_stats(self, game_id: str, season: str, *, season_type: str = "Regular Season") -> dict[str, object]: ...


class LedgerAthleteCatalogReader(Protocol):
    def get_catalog(self, season: str, *, active_only: bool = False) -> list[dict[str, object]]: ...

    def get_freshness(self, season: str, *, now: datetime) -> Mapping[str, object]: ...


class LedgerParticipantCatalogReader(Protocol):
    def get_participants(self, observation_id: str, *, game_id: str) -> Mapping[int, Iterable[int]]: ...


class LedgerObservationRecorder(Protocol):
    def stage(
        self,
        observation: object,
        *,
        season: str,
        game_id: str,
        retrieved_at: datetime,
        manifest_id: str,
        manifest_scope: str,
        manifest_cutoff: datetime,
        schema_version: int,
    ) -> tuple[str, Mapping[str, object]]: ...

    def get_staged(self, observation_id: str) -> object: ...

    def consume(self, observation_id: str) -> None: ...

    def discard(self, observation_id: str) -> None: ...


class CollectionObservationLedgerRecorder:
    """Stage provider retrievals for atomic acceptance with a valid ledger game."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._staged: dict[str, object] = {}
        self._peak_pending = 0
        self._lock = threading.Lock()

    def stage(
        self,
        observation: object,
        *,
        season: str,
        game_id: str,
        retrieved_at: datetime,
        manifest_id: str,
        manifest_scope: str,
        manifest_cutoff: datetime,
        schema_version: int,
    ) -> tuple[str, Mapping[str, object]]:
        if not manifest_id or manifest_scope != "canonical_game_ledger":
            raise LedgerValidationError("authorizing ledger manifest is required")
        document = (
            observation.to_dict(orient="records")
            if isinstance(observation, pd.DataFrame)
            else observation
        )
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
        observation_id = str(uuid4())
        values = {
            "observation_id": observation_id,
            "client_observation_id": f"ledger:{game_id}:{observation_id}",
            "collector_id": "railway-ledger",
            "manifest_id": manifest_id,
            "environment": "server",
            "provider": "pbp",
            "observation_type": "canonical_game_ledger",
            "scope": json.dumps(
                {"game_id": game_id, "surface": manifest_scope},
                sort_keys=True,
            ),
            "season": season,
            "cutoff": manifest_cutoff,
            "schema_version": schema_version,
            "checksum": hashlib.sha256(payload.encode()).hexdigest(),
            "payload": payload,
            "payload_bytes": len(payload.encode()),
            "retrieved_at": retrieved_at,
            "accepted_at": retrieved_at,
        }
        with self._lock:
            self._staged[observation_id] = document
            self._peak_pending = max(self._peak_pending, len(self._staged))
        return observation_id, values

    def get_staged(self, observation_id: str) -> object:
        with self._lock:
            if observation_id not in self._staged:
                raise LedgerValidationError("staged participant observation is required")
            return self._staged[observation_id]

    def consume(self, observation_id: str) -> None:
        """Release one staged document only after its DB transaction commits."""

        with self._lock:
            self._staged.pop(observation_id, None)

    def discard(self, observation_id: str) -> None:
        """Release one rejected or failed staged document without affecting peers."""

        with self._lock:
            self._staged.pop(observation_id, None)

    def pending_count(self) -> int:
        """Return bounded lifecycle diagnostics without exposing documents."""

        with self._lock:
            return len(self._staged)

    def peak_pending_count(self) -> int:
        """Return the highest retained-document count for worker diagnostics."""

        with self._lock:
            return self._peak_pending


class AcceptedObservationParticipantCatalog:
    """Read exact game participants from the accepted provider observation."""

    def __init__(self, engine: Engine, recorder: LedgerObservationRecorder) -> None:
        self.engine = engine
        self.recorder = recorder

    def get_participants(self, observation_id: str, *, game_id: str) -> Mapping[int, Iterable[int]]:
        document = self.recorder.get_staged(observation_id)
        if isinstance(document, Mapping):
            rows = _raw_document_rows(document)
        else:
            rows = document if isinstance(document, list) else []
        with self.engine.connect() as connection:
            event = connection.execute(select(
                EventCatalogEntry.home_team_id,
                EventCatalogEntry.home_team_tricode,
                EventCatalogEntry.away_team_id,
                EventCatalogEntry.away_team_tricode,
            ).where(EventCatalogEntry.nba_game_id == game_id)).mappings().one_or_none()
        team_ids_by_code = {} if event is None else {
            str(event["home_team_tricode"]): int(event["home_team_id"]),
            str(event["away_team_tricode"]): int(event["away_team_id"]),
        }
        participants: dict[int, list[int]] = {}
        for item in rows:
            team_id = int(item.get(
                "TEAM_ID",
                item.get("TeamId", item.get("team_id", team_ids_by_code.get(str(item.get("Team")), 0))),
            ))
            player_id = int(item.get("PLAYER_ID", item.get("EntityId", 0)))
            if team_id > 0 and player_id > 0:
                participants.setdefault(team_id, []).append(player_id)
        return participants


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
        repository: CanonicalGameLedgerRepository,
        athlete_catalog: LedgerAthleteCatalogReader,
        participant_catalog: LedgerParticipantCatalogReader,
        reconciliation_sink: Callable[[str, Mapping[str, object]], None],
        observation_recorder: LedgerObservationRecorder,
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
        self.repository = repository
        self.athlete_catalog = athlete_catalog
        self.participant_catalog = participant_catalog
        self.reconciliation_sink = reconciliation_sink
        self.observation_recorder = observation_recorder
        for collaborator, methods, label in (
            (athlete_catalog, ("get_catalog", "get_freshness"), "athlete_catalog"),
            (participant_catalog, ("get_participants",), "participant_catalog"),
            (
                observation_recorder,
                ("stage", "get_staged", "consume", "discard"),
                "observation_recorder",
            ),
        ):
            if any(not callable(getattr(collaborator, method, None)) for method in methods):
                raise TypeError(f"{label} does not satisfy the governed ledger contract")
        if not callable(reconciliation_sink):
            raise TypeError("reconciliation_sink must be callable")
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
        governed_events: Iterable[Mapping[str, object]] | None = None,
        manifest_id: str | None = None,
        manifest_scope: str = "canonical_game_ledger",
        collect_before: datetime | None = None,
        schema_version: int = 1,
        accepted_versions: frozenset[int] | None = None,
    ) -> BackfillResult:
        """Run one bounded pass and persist resumable progress.

        Missing games always outrank scheduled rechecks. Each valid target is
        accepted as its own transaction; a failed target leaves that game
        untouched without rolling back other valid progress from the pass.
        """

        if governed_events is None or cutoff is None or not manifest_id:
            raise LedgerValidationError("authorized ledger manifest identity is required")
        if collect_before is None:
            raise LedgerValidationError("authorized ledger collection deadline is required")
        if manifest_scope != "canonical_game_ledger":
            raise LedgerValidationError("canonical game ledger manifest scope is required")
        if accepted_versions is None or schema_version not in accepted_versions:
            raise LedgerValidationError("authorized ledger schema version is required")
        started_at = _aware(self.clock())
        through = _aware(cutoff)
        with self.repository.engine.connect() as connection:
            manifest = connection.execute(select(CollectionManifest).where(
                CollectionManifest.manifest_id == manifest_id,
                CollectionManifest.season == season,
                CollectionManifest.cutoff == through,
                CollectionManifest.status == "active",
            )).mappings().one_or_none()
        if (
            manifest is None
            or _aware(manifest["collect_before"]) != _aware(collect_before)
            or _aware(manifest["collect_before"]) <= started_at
            or manifest_scope not in set(json.loads(manifest["scopes"]))
            or schema_version not in set(json.loads(manifest["accepted_versions"]))
        ):
            raise LedgerValidationError("active authorized ledger manifest is required")
        events = self._authorized_events(governed_events, season=season, through=through)
        expected_ids = frozenset(str(event["nba_game_id"]) for event in events)
        checksums = self.repository.game_checksums(season)
        summaries = {
            summary.game_id: summary
            for summary in self.repository.list_games(season, through=through.date())
        }
        missing_raw_evidence = self.repository.game_ids_without_raw_evidence(
            season, through=through.date()
        )
        targets = self._targets(
            events,
            checksums=checksums,
            summaries=summaries,
            now=started_at,
            repair=historical_repair,
            accepted_manifest_game_ids=self.repository.game_ids_from_manifest(
                season, manifest_id
            ),
            missing_raw_evidence=missing_raw_evidence,
        )
        if max_games is not None:
            if isinstance(max_games, bool) or max_games < 1:
                raise ValueError("max_games must be a positive integer")
            selected = tuple(targets[:max_games])
            lower_priority_remaining = max(0, len(targets) - len(selected))
        else:
            selected = targets
            lower_priority_remaining = 0

        athlete_ids = self._athlete_ids(season, now=started_at)
        failures: list[str] = []
        writes = []
        processed = 0
        active_staged: set[str] = set()
        lifecycle_lock = threading.Lock()

        def fetch_and_commit(target: _Target):
            event = target.event
            game_id = str(event["nba_game_id"])
            observation_id: str | None = None
            committed = False
            validated = False
            try:
                observation = self.provider.fetch_game_stats(game_id, season, season_type="Regular Season")
                # Provenance is per retrieval: capture the time this response
                # was actually returned, not the batch start, so each staged
                # observation and archived row records its own retrieval time.
                retrieved_at = _aware(self.clock())
                observation_id, observation_values = self.observation_recorder.stage(
                    observation,
                    season=season,
                    game_id=game_id,
                    retrieved_at=retrieved_at,
                    manifest_id=manifest_id,
                    manifest_scope=manifest_scope,
                    manifest_cutoff=through,
                    schema_version=schema_version,
                )
                with lifecycle_lock:
                    active_staged.add(observation_id)
                participants = self.participant_catalog.get_participants(observation_id, game_id=game_id)
                game = canonical_game_from_pbp(
                    observation,
                    event=event,
                    season=season,
                    source_observation_id=observation_id,
                    retrieved_at=retrieved_at,
                    participant_ids_by_team=participants,
                )
                if athlete_ids is not None:
                    unknown = [player.player_id for player in game.player_facts if player.player_id not in athlete_ids]
                    if unknown:
                        self.reconciliation_sink(
                            game_id,
                            {
                                "kind": "athlete_identity",
                                "season": season,
                                "count": len(unknown),
                                "player_ids": tuple(sorted(unknown)[:25]),
                            },
                        )
                        raise LedgerValidationError("PBP game contains an unresolved Athlete Catalog identity")
                validated = True
                accepted_at = _aware(self.clock())
                if accepted_at >= _aware(collect_before):
                    raise LedgerValidationError("ledger manifest collection deadline elapsed")
                accepted_observation = dict(observation_values)
                accepted_observation["accepted_at"] = accepted_at
                write = self.repository.replace_games_atomic(
                    (game,),
                    accepted_observations={observation_id: accepted_observation},
                )[0]
                committed = True
                return game_id, write, validated, None
            except (
                KeyError,
                TypeError,
                ValueError,
                RuntimeError,
                OSError,
                ProviderResponseError,
                ProviderUnavailableError,
            ) as error:
                return game_id, None, validated, _safe_reason(error)
            finally:
                if observation_id is not None:
                    if committed:
                        self.observation_recorder.consume(observation_id)
                    else:
                        self.observation_recorder.discard(observation_id)
                    with lifecycle_lock:
                        active_staged.discard(observation_id)

        if selected:
            try:
                with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
                    futures = [executor.submit(fetch_and_commit, target) for target in selected]
                    for future in as_completed(futures):
                        game_id, write, validated, error = future.result()
                        processed += int(validated)
                        if error is not None:
                            failures.append(game_id)
                        elif write is not None:
                            writes.append(write)
            finally:
                with lifecycle_lock:
                    abandoned = tuple(active_staged)
                    active_staged.clear()
                for observation_id in abandoned:
                    self.observation_recorder.discard(observation_id)
        resulting_ids = {
            summary.game_id
            for summary in self.repository.list_games(season, through=through.date())
        }
        missing_raw_evidence = self.repository.game_ids_without_raw_evidence(
            season, through=through.date()
        )
        complete = expected_ids == resulting_ids and not missing_raw_evidence
        pending = tuple(sorted(expected_ids - resulting_ids))
        status = "complete" if complete else "unavailable"
        self._save_progress(
            season,
            cutoff=through,
            completed=resulting_ids,
            failed=failures,
            status=status,
            cursor=selected[-1].event.get("nba_game_id") if selected else None,
            last_error=None if complete else "one or more governed games are unavailable or identities differ",
        )
        return BackfillResult(
            season=season,
            cutoff=through,
            status=status,
            complete=complete,
            games_processed=processed,
            games_replaced=sum(1 for write in writes if write.replaced),
            games_skipped=sum(1 for write in writes if not write.inserted and not write.replaced) + max(0, len(checksums) - len(targets)),
            failed_game_ids=tuple(sorted(set(failures))),
            pending_game_ids=pending,
            lower_priority_remaining=lower_priority_remaining,
            reason=None if complete else "ledger identities do not exactly match governance",
        )

    @staticmethod
    def _authorized_events(
        governed_events: Iterable[Mapping[str, object]],
        *,
        season: str,
        through: datetime,
    ) -> tuple[dict[str, object], ...]:
        events = []
        for raw in governed_events:
            event = dict(raw)
            if (
                str(event.get("season")) != season
                or player_game_log_season_type(event) != "Regular Season"
                or not is_final_event(event)
                or is_postponed_event(event)
                or _event_datetime(event.get("scheduled_at")) > through
                or not event.get("nba_game_id")
            ):
                raise LedgerValidationError("authorized Event Catalog snapshot is invalid")
            events.append(event)
        if not events:
            raise LedgerValidationError("authorized Event Catalog snapshot is empty")
        return tuple(sorted(
            events,
            key=lambda event: (str(event.get("scheduled_at")), str(event.get("nba_game_id"))),
            reverse=True,
        ))

    def _targets(
        self,
        events: Iterable[Mapping[str, object]],
        *,
        checksums: Mapping[str, str],
        summaries: Mapping[str, object],
        now: datetime,
        repair: bool,
        accepted_manifest_game_ids: Iterable[str] = (),
        missing_raw_evidence: Iterable[str] = (),
    ) -> tuple[_Target, ...]:
        targets: list[_Target] = []
        accepted_manifest_game_ids = frozenset(accepted_manifest_game_ids)
        missing_raw_evidence = frozenset(missing_raw_evidence)
        for event in events:
            game_id = str(event["nba_game_id"])
            summary = summaries.get(game_id)
            # A stored game without complete raw PBP evidence (for example a
            # game accepted before migration 032) is as incomplete as a missing
            # game: re-fetch it regardless of age so it cannot permanently lack
            # both team-summary and player-row evidence.
            if game_id not in checksums or game_id in missing_raw_evidence:
                targets.append(_Target(event, 0))
                continue
            if repair:
                if game_id not in accepted_manifest_game_ids:
                    targets.append(_Target(event, 1))
                continue
            age_days = (now.date() - _event_datetime(event.get("scheduled_at")).date()).days
            if age_days <= self.daily_recheck_days and summary is not None:
                last_fetch = _aware(summary.retrieved_at)
                if now - last_fetch >= timedelta(hours=24):
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

    def _athlete_ids(self, season: str, *, now: datetime) -> frozenset[int]:
        freshness = self.athlete_catalog.get_freshness(season, now=now)
        if not isinstance(freshness, Mapping) or not freshness.get("is_fresh", freshness.get("fresh", False)):
            raise LedgerValidationError("Athlete Catalog must be fresh before backfill")
        rows = self.athlete_catalog.get_catalog(season, active_only=False)
        identities = frozenset(int(row["player_id"]) for row in rows if row.get("player_id") is not None)
        if not identities:
            raise LedgerValidationError("Athlete Catalog must contain governed identities")
        return identities

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


def _raw_document_rows(document: Mapping[str, object]) -> list[dict[str, object]]:
    """Flatten a raw ``/get-game-stats`` document into team-tagged row mappings.

    The raw document carries team-summary and player rows under
    ``stats.Home/Away.FullGame`` without per-row team identity, so each row is
    tagged with its side's abbreviation for the event-driven team join.
    """

    stats = document.get("stats")
    if not isinstance(stats, Mapping):
        return []
    side_codes = {
        "Home": str(document.get("home_team_abbreviation") or ""),
        "Away": str(document.get("away_team_abbreviation") or ""),
    }
    rows: list[dict[str, object]] = []
    for side, team_code in side_codes.items():
        period = stats.get(side)
        period_rows = period.get("FullGame") if isinstance(period, Mapping) else None
        if not isinstance(period_rows, list):
            continue
        for row in period_rows:
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            if "Team" not in item:
                item["Team"] = team_code
            rows.append(item)
    return rows


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
    "LedgerPBPProvider",
    "LedgerParticipantCatalogReader",
    "LedgerObservationRecorder",
    "CollectionObservationLedgerRecorder",
    "AcceptedObservationParticipantCatalog",
]
