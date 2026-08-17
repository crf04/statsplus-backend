"""Collect exact team-window matchup facts for durable publication."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import json
from typing import Any, Literal, Protocol, cast, runtime_checkable
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select

from app.domain.nba_events import (
    NBAGameStatus,
    is_all_star_kind,
    is_postponed_event,
    is_preseason_kind,
    resolve_stored_event_classification,
)
from app.domain.publication_integrity import publication_payload_matches_checksum
from app.domain.utc import assume_utc, parse_utc_iso
from app.models.catalogs import PLAY_TYPES, SHOOTING_TYPES
from app.models.collection_control import CollectionManifest, PublicationPointer
from app.providers.nba_stats import validate_canonical_season
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)
from app.utils.telemetry import ProviderResponseError


EASTERN = ZoneInfo("America/New_York")


def governed_season_type(event: Mapping[str, Any]) -> str | None:
    """Return the provider season type for a canonical governed NBA event."""

    game_id = str(event.get("nba_game_id") or "")
    classification = resolve_stored_event_classification(
        game_id, str(event.get("classification") or "")
    )
    if (
        is_all_star_kind(classification.kind)
        or is_all_star_kind(classification.display)
        or is_preseason_kind(classification.kind)
        or is_preseason_kind(classification.display)
    ):
        return None
    if classification.kind in {"Regular Season", "Playoffs"}:
        return classification.kind
    return None


def is_governed_event(event: Mapping[str, Any]) -> bool:
    """Whether an event belongs to the canonical regular/postseason schedule."""

    return governed_season_type(event) is not None


@dataclass(frozen=True, slots=True)
class TeamWindowBoundary:
    """The exact governed games and provider date bounds for one team."""

    team_id: int
    from_date: date
    to_date: date
    game_ids: tuple[str, ...]
    season_type: str | None


@dataclass(frozen=True, slots=True)
class TeamMatchupProvenance:
    """Exact authority supplied by the collection cycle to the legacy writer."""

    cutoff: datetime
    manifest_id: str
    event_catalog_publication_id: str
    event_catalog_checksum: str
    manifest_checksum: str | None = None
    collect_before: datetime | None = None
    canonical_ledger_pointer_fence: int | None = None
    canonical_ledger_pointer_publication_id: str | None = None


class TeamWindowBoundaryResolver:
    """Resolve rolling windows from the canonical governed schedule."""

    def last_n(
        self,
        events: list[dict[str, Any]],
        *,
        as_of: date,
        window_games: int,
    ) -> dict[int, TeamWindowBoundary]:
        if (
            not isinstance(window_games, int)
            or isinstance(window_games, bool)
            or window_games < 1
        ):
            raise ValueError("window_games must be a positive integer")

        games_by_team: dict[int, list[tuple[datetime, str, str]]] = defaultdict(list)
        for event in events:
            season_type = governed_season_type(event)
            if season_type is None or not self._is_completed_by(event, as_of=as_of):
                continue
            scheduled_at = self._scheduled_at(event)
            game_id = str(event["nba_game_id"])
            for field in ("home_team_id", "away_team_id"):
                games_by_team[int(event[field])].append(
                    (scheduled_at, game_id, season_type)
                )

        boundaries: dict[int, TeamWindowBoundary] = {}
        for team_id, games in games_by_team.items():
            selected = sorted(games, reverse=True)[:window_games]
            if len(selected) != window_games:
                continue
            boundaries[team_id] = TeamWindowBoundary(
                team_id=team_id,
                from_date=selected[-1][0].astimezone(EASTERN).date(),
                to_date=as_of,
                game_ids=tuple(game_id for _, game_id, _ in selected),
                season_type=(
                    selected[0][2]
                    if len({season_type for _, _, season_type in selected}) == 1
                    else None
                ),
            )
        return boundaries

    @staticmethod
    def _scheduled_at(event: dict[str, Any]) -> datetime:
        value = event["scheduled_at"]
        if isinstance(value, datetime):
            return assume_utc(value)
        return parse_utc_iso(str(value))

    @classmethod
    def _is_completed_by(cls, event: dict[str, Any], *, as_of: date) -> bool:
        if is_postponed_event(event):
            return False
        is_final = event.get("status_code") == NBAGameStatus.FINAL or str(
            event.get("status_text") or ""
        ).casefold().startswith("final")
        if not is_final:
            return False
        return cls._scheduled_at(event).astimezone(EASTERN).date() <= as_of


_TRADITIONAL_STATS = ("OPP_REB", "OPP_TOV", "OPP_STL", "OPP_BLK")
_SHOT_STATS = ("FG2M", "FG2A", "FG3M", "FG3A")
_ASSIST_STATS = (
    "Assists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
MATCHUP_SURFACES = (
    "assist_locations",
    "play_types",
    "shot_types",
    "shot_zones",
    "traditional",
)


class _ProviderWindowUnverified(ValueError):
    """An aggregate response cannot prove it represents the governed window."""


class _ProviderRosterMismatch(ValueError):
    """A Season aggregate does not contain the governed NBA team roster."""


@runtime_checkable
class TeamMatchupEventCatalog(Protocol):
    def get_events(self, season: str) -> list[dict[str, Any]]: ...


@runtime_checkable
class TeamMatchupNBAStatsProvider(Protocol):
    def fetch_opponent_team_stats(
        self,
        date_from: str | None,
        *,
        date_to: str | None = None,
        season: str | None = None,
        season_type: str | None = None,
        team_id: int | None = None,
        last_n_games: int | None = None,
        per_mode_detailed: str = "Per48",
    ) -> pd.DataFrame | None: ...

    def fetch_opponent_shot_chart(
        self,
        general_range: str,
        date_from: str | None,
        *,
        date_to: str | None = None,
        season: str | None = None,
        season_type: str | None = None,
        team_id: int | None = None,
        last_n_games: int | None = None,
        per_mode_simple: str = "PerGame",
    ) -> pd.DataFrame: ...

    def fetch_opponent_shooting_zone(
        self,
        date_from: str | None,
        *,
        date_to: str | None = None,
        season: str | None = None,
        season_type: str | None = None,
        team_id: int | None = None,
        last_n_games: int | None = None,
        per_mode_detailed: str = "PerGame",
    ) -> pd.DataFrame: ...

    def fetch_synergy_play_types(
        self,
        play_type: str,
        *,
        player_or_team_abbreviation: str,
        type_grouping: str,
        season: str | None = None,
        per_mode_simple: str | None = None,
    ) -> pd.DataFrame: ...


@runtime_checkable
class TeamMatchupPBPStatsProvider(Protocol):
    def fetch_totals_frame(
        self,
        data_type: Literal["player", "opponent"] = "player",
        *,
        season: str | None = None,
        season_type: str = "Regular+Season",
        team_id: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> pd.DataFrame: ...


class TeamMatchupRefreshService:
    """Collect Season and exact team Last-15 facts, then publish together."""

    def __init__(
        self,
        *,
        repository: TeamMatchupRepository,
        event_catalog: TeamMatchupEventCatalog,
        nba_stats_provider: TeamMatchupNBAStatsProvider,
        pbp_stats_provider: TeamMatchupPBPStatsProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(repository, TeamMatchupRepository):
            raise TypeError("repository must be a TeamMatchupRepository")
        if not isinstance(event_catalog, TeamMatchupEventCatalog):
            raise TypeError("event_catalog must provide get_events")
        if not isinstance(nba_stats_provider, TeamMatchupNBAStatsProvider):
            raise TypeError("nba_stats_provider does not satisfy the matchup contract")
        if not isinstance(pbp_stats_provider, TeamMatchupPBPStatsProvider):
            raise TypeError("pbp_stats_provider does not satisfy the matchup contract")
        self.repository = repository
        self.event_catalog = event_catalog
        self.nba_stats = nba_stats_provider
        self.pbp_stats = pbp_stats_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def refresh(
        self,
        season: str,
        *,
        as_of: date | None = None,
        provenance: TeamMatchupProvenance | None = None,
    ) -> None:
        canonical_season = validate_canonical_season(season)
        retrieved_at = assume_utc(self._clock())
        current_date = retrieved_at.astimezone(EASTERN).date()
        snapshot_date = as_of or current_date
        if snapshot_date > current_date:
            raise ValueError("future as_of dates cannot be published")
        provenance = provenance or self._provenance_for_snapshot(
            canonical_season, snapshot_date
        )
        if provenance is not None:
            self._revalidate_provenance(provenance, season=canonical_season)
        events = (
            self._immutable_catalog_events(canonical_season, provenance)
            if provenance is not None
            else self.event_catalog.get_events(canonical_season)
        )
        team_ids = self._team_ids(events)
        season_scope = TeamMatchupSnapshotScope(canonical_season, snapshot_date)
        rolling_scope = TeamMatchupSnapshotScope(
            canonical_season, snapshot_date, window_games=15
        )
        if len(team_ids) != 30:
            observations = self._surface_observations(
                default_status="missing",
                default_reason="governed_team_roster_incomplete",
            )
            snapshots = (
                (season_scope, (), observations),
                (rolling_scope, (), observations),
            )
            if provenance is None:
                self.repository.replace_snapshots(snapshots, retrieved_at=retrieved_at)
            else:
                with self.repository.engine.begin() as connection:
                    self._revalidate_provenance(
                        provenance,
                        season=canonical_season,
                        connection=connection,
                    )
                    self.repository.replace_snapshots(
                        snapshots,
                        retrieved_at=retrieved_at,
                        connection=connection,
                    )
            return
        boundaries = TeamWindowBoundaryResolver().last_n(
            events, as_of=snapshot_date, window_games=15
        )
        season_game_ids_by_team = self._season_game_ids_by_team(
            events, as_of=snapshot_date, team_ids=team_ids
        )
        season_play_types_are_bounded = (
            snapshot_date == retrieved_at.astimezone(EASTERN).date()
        )
        season_facts, season_failures, season_provider_game_ids = self._collect_season(
            canonical_season,
            snapshot_date=snapshot_date,
            include_play_types=season_play_types_are_bounded,
            team_ids=team_ids,
            expected_game_counts={
                team_id: len(game_ids)
                for team_id, game_ids in season_game_ids_by_team.items()
            },
            expected_game_ids_by_team=season_game_ids_by_team,
            verify_window=provenance is not None,
        )
        season_window_identity = None
        if season_provider_game_ids is not None:
            season_window_identity = self._provider_window_identity(
                window="season",
                game_ids_by_team=season_game_ids_by_team,
                provider_game_ids_by_team=season_provider_game_ids,
                expected_counts={
                    team_id: len(game_ids)
                    for team_id, game_ids in season_game_ids_by_team.items()
                },
                collect_before=provenance.collect_before if provenance else None,
            )
        season_facts = self._bind_legacy_window_evidence(
            season_facts, season_provider_game_ids or {}, season_window_identity,
            provenance=provenance, cutoff=retrieved_at,
        )
        season_observations = self._surface_observations(
            overrides={
                **(
                    {}
                    if season_play_types_are_bounded
                    else {
                        "play_types": (
                            "unavailable",
                            "provider_unbounded_as_of",
                        )
                    }
                ),
                **season_failures,
            }
        )
        season_observations = self._bind_observation_evidence(
            season_observations, season_window_identity,
            provenance=provenance, cutoff=retrieved_at,
        )
        if set(boundaries) != set(team_ids):
            rolling_facts = []
            rolling_observations = self._surface_observations(
                default_status="missing",
                default_reason="insufficient_governed_games",
            )
        elif any(boundary.season_type is None for boundary in boundaries.values()):
            rolling_facts = []
            rolling_observations = self._surface_observations(
                default_status="unavailable",
                default_reason="provider_window_unrepresentable",
                overrides={"play_types": ("unavailable", "provider_window_unsupported")},
            )
        else:
            rolling_facts, window_overrides, rolling_provider_game_ids = self._collect_last_15(
                canonical_season,
                snapshot_date=snapshot_date,
                team_ids=team_ids,
                boundaries=boundaries,
                verify_window=provenance is not None,
            )
            rolling_game_ids_by_team = {
                team_id: boundary.game_ids
                for team_id, boundary in boundaries.items()
            }
            rolling_window_identity = None
            if rolling_provider_game_ids is not None:
                rolling_window_identity = self._provider_window_identity(
                    window="l15",
                    game_ids_by_team=rolling_game_ids_by_team,
                    provider_game_ids_by_team=rolling_provider_game_ids,
                    expected_counts={
                        team_id: 15 for team_id in rolling_game_ids_by_team
                    },
                    collect_before=provenance.collect_before if provenance else None,
                )
            rolling_facts = self._bind_legacy_window_evidence(
                rolling_facts,
                rolling_provider_game_ids or {}, rolling_window_identity,
                provenance=provenance, cutoff=retrieved_at,
            )
            rolling_observations = self._surface_observations(
                overrides={
                    "play_types": ("unavailable", "provider_window_unsupported"),
                    **window_overrides,
                }
            )
            rolling_observations = self._bind_observation_evidence(
                rolling_observations, rolling_window_identity,
                provenance=provenance, cutoff=retrieved_at,
            )
        snapshots = (
            (season_scope, season_facts, season_observations),
            (rolling_scope, rolling_facts, rolling_observations),
        )
        if provenance is None:
            self.repository.replace_snapshots(snapshots, retrieved_at=retrieved_at)
        else:
            # Revalidation and the legacy replacement share one transaction.
            # The provider calls above may be slow, but once they finish there
            # is no unlocked authority-to-write gap left for a manifest or
            # canonical pointer mutation to race through.
            with self.repository.engine.begin() as connection:
                self._revalidate_provenance(
                    provenance,
                    season=canonical_season,
                    connection=connection,
                )
                self.repository.replace_snapshots(
                    snapshots,
                    retrieved_at=retrieved_at,
                    connection=connection,
                )

    def _provenance_for_snapshot(
        self, season: str, snapshot_date: date
    ) -> TeamMatchupProvenance | None:
        """Resolve one exact active manifest; never choose by slate date alone."""
        now = assume_utc(self._clock())
        with self.repository.engine.connect() as connection:
            rows = connection.execute(select(CollectionManifest.__table__).where(
                CollectionManifest.season == season,
            ).with_for_update()).mappings().all()
            pointer = connection.execute(select(PublicationPointer.__table__).where(
                PublicationPointer.stream_key == "canonical_game_ledger",
            ).with_for_update()).mappings().one_or_none()
        same_slate = [
            row for row in rows
            if assume_utc(row["cutoff"]).astimezone(EASTERN).date() == snapshot_date
        ]
        # Multiple manifests for one slate date are ambiguous even when one
        # is active: choosing by date would silently relabel a different
        # governed cutoff or Event Catalog publication.
        if len(same_slate) != 1:
            return None
        row = same_slate[0]
        if row["status"] != "active" or not row["checksum"]:
            return None
        try:
            scopes = set(json.loads(row["scopes"]))
            accepted_versions = {
                int(value) for value in json.loads(row["accepted_versions"])
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            "canonical_game_ledger" not in scopes
            or 1 not in accepted_versions
            or not row["event_catalog_publication_id"]
            or not row["event_catalog_checksum"]
            or assume_utc(row["collect_before"]) <= now
        ):
            return None
        pointer_row = pointer
        return TeamMatchupProvenance(
            cutoff=assume_utc(row["cutoff"]),
            manifest_id=row["manifest_id"],
            event_catalog_publication_id=row["event_catalog_publication_id"],
            event_catalog_checksum=row["event_catalog_checksum"],
            manifest_checksum=row.get("checksum"),
            collect_before=assume_utc(row["collect_before"]),
            canonical_ledger_pointer_fence=(
                int(pointer_row["fence"]) if pointer_row is not None else None
            ),
            canonical_ledger_pointer_publication_id=(
                pointer_row["active_publication_id"] if pointer_row is not None else None
            ),
        )

    def _revalidate_provenance(
        self, provenance: TeamMatchupProvenance, *, season: str,
        connection=None,
    ) -> None:
        """Re-read governance after provider I/O and reject authority drift."""

        if connection is None:
            with self.repository.engine.connect() as owned_connection:
                return self._revalidate_provenance(
                    provenance, season=season, connection=owned_connection
                )

        now = assume_utc(self._clock())
        manifest = connection.execute(select(CollectionManifest.__table__).where(
            CollectionManifest.manifest_id == provenance.manifest_id,
        ).with_for_update()).mappings().one_or_none()
        pointer = connection.execute(select(PublicationPointer.__table__).where(
            PublicationPointer.stream_key == "canonical_game_ledger",
        ).with_for_update()).mappings().one_or_none()
        if manifest is None or manifest["status"] != "active":
            raise _ProviderWindowUnverified("governance changed during provider collection")
        try:
            scopes = set(json.loads(manifest["scopes"]))
            accepted_versions = {
                int(value) for value in json.loads(manifest["accepted_versions"])
            }
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise _ProviderWindowUnverified("collection governance is invalid") from error
        if (
            manifest["season"] != season
            or assume_utc(manifest["cutoff"]) != provenance.cutoff
            or manifest.get("checksum") != provenance.manifest_checksum
            or provenance.collect_before is None
            or assume_utc(manifest["collect_before"]) != assume_utc(
                provenance.collect_before
            )
            or assume_utc(manifest["collect_before"]) <= now
            or "canonical_game_ledger" not in scopes
            or 1 not in accepted_versions
            or manifest["event_catalog_publication_id"]
            != provenance.event_catalog_publication_id
            or manifest["event_catalog_checksum"]
            != provenance.event_catalog_checksum
            or (pointer is None)
            != (provenance.canonical_ledger_pointer_fence is None)
            or (
                pointer is not None
                and (
                    int(pointer["fence"]) != provenance.canonical_ledger_pointer_fence
                    or pointer["active_publication_id"]
                    != provenance.canonical_ledger_pointer_publication_id
                )
            )
        ):
            raise _ProviderWindowUnverified("governance changed during provider collection")
        # This verifies the immutable payload/checksum and its exact identity,
        # rather than trusting manifest columns alone.
        self._immutable_catalog_events(season, provenance, connection=connection)

    def _immutable_catalog_events(
        self, season: str, provenance: TeamMatchupProvenance, connection=None,
    ) -> list[dict[str, Any]]:
        """Read the exact manifest-bound catalog, never mutable event rows."""
        from app.models.collection_control import CatalogPublication

        if connection is None:
            with self.repository.engine.connect() as owned_connection:
                return self._immutable_catalog_events(
                    season, provenance, connection=owned_connection
                )

        row = connection.execute(select(CatalogPublication.__table__).where(
            CatalogPublication.publication_id
            == provenance.event_catalog_publication_id,
        ).with_for_update()).mappings().one_or_none()
        if (
            row is None
            or row["season"] != season
            or row["catalog_type"] != "event"
            or row["complete"] is not True
            or assume_utc(row["cutoff"]) != assume_utc(provenance.cutoff)
            or row["checksum"] != provenance.event_catalog_checksum
            or not publication_payload_matches_checksum(
                row["payload"], row["checksum"]
            )
        ):
            raise _ProviderWindowUnverified("immutable Event Catalog authority unavailable")
        try:
            payload = json.loads(row["payload"])
            source_events = payload["events"]
            if not isinstance(source_events, list):
                raise TypeError
            return [
                {
                    **event,
                    "classification": event.get(
                        "classification", event.get("phase")
                    ),
                    "status_text": event.get("status_text", event.get("status")),
                    "postponed_status": event.get("postponed_status"),
                }
                for event in source_events
                if isinstance(event, dict)
            ]
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise _ProviderWindowUnverified(
                "immutable Event Catalog payload is invalid"
            ) from error

    @staticmethod
    def _surface_observations(
        *,
        default_status: str = "available",
        default_reason: str | None = None,
        overrides: Mapping[str, tuple[str, str | None]] | None = None,
    ) -> tuple[TeamMatchupObservation, ...]:
        resolved = overrides or {}
        observations = []
        for surface in MATCHUP_SURFACES:
            status, reason = resolved.get(surface, (default_status, default_reason))
            observations.append(
                TeamMatchupObservation(
                    surface=surface,
                    status=status,
                    unavailable_reason=reason,
                )
            )
        return tuple(observations)

    @staticmethod
    def _provider_failure(
        error: ProviderResponseError | ValueError,
    ) -> tuple[str, str]:
        if isinstance(error, _ProviderWindowUnverified):
            return "unavailable", "provider_window_unverified"
        if isinstance(error, _ProviderRosterMismatch):
            return "unavailable", "provider_roster_mismatch"
        if isinstance(error, ProviderResponseError):
            return "unavailable", "provider_malformed_response"
        return "unavailable", "provider_invalid_response"

    @staticmethod
    def _team_ids(events: list[dict[str, Any]]) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    int(event[field])
                    for event in events
                    if is_governed_event(event)
                    for field in ("home_team_id", "away_team_id")
                }
            )
        )

    @staticmethod
    def _season_game_ids_by_team(
        events: list[dict[str, Any]],
        *,
        as_of: date,
        team_ids: tuple[int, ...],
    ) -> dict[int, tuple[str, ...]]:
        """Resolve the actual provider window identity from the event catalog."""

        selected: dict[int, list[tuple[datetime, str]]] = {
            team_id: [] for team_id in team_ids
        }
        for event in events:
            if governed_season_type(event) != "Regular Season" or not TeamWindowBoundaryResolver._is_completed_by(
                event, as_of=as_of
            ):
                continue
            scheduled_at = TeamWindowBoundaryResolver._scheduled_at(event)
            game_id = str(event["nba_game_id"])
            for field in ("home_team_id", "away_team_id"):
                team_id = int(event[field])
                if team_id in selected:
                    selected[team_id].append((scheduled_at, game_id))
        return {
            team_id: tuple(
                game_id
                for _, game_id in sorted(values, key=lambda item: (item[0], item[1]))
            )
            for team_id, values in selected.items()
        }

    @staticmethod
    def _provider_window_identity(
        *, window: str, game_ids_by_team: Mapping[int, tuple[str, ...]],
        provider_game_ids_by_team: Mapping[int, tuple[str, ...]],
        expected_counts: Mapping[int, int],
        collect_before: datetime | None = None,
    ) -> str:
        """Record provider request evidence only after count verification.

        The Event Catalog IDs are not appended as a post-hoc label.  The
        collector first obtains exact IDs from the independent TeamGameLog
        detail response, verifies each aggregate's returned count against the
        immutable set, then stores this canonical evidence.
        """
        if set(game_ids_by_team) != set(expected_counts):
            raise _ProviderWindowUnverified("provider window team set is incomplete")
        if any(len(game_ids_by_team[team_id]) != expected_counts[team_id]
               for team_id in expected_counts):
            raise _ProviderWindowUnverified("provider window game count is unverified")
        if set(provider_game_ids_by_team) != set(game_ids_by_team):
            raise _ProviderWindowUnverified("provider game membership is incomplete")
        if any(
            frozenset(provider_game_ids_by_team[team_id])
            != frozenset(game_ids_by_team[team_id])
            for team_id in game_ids_by_team
        ):
            raise _ProviderWindowUnverified("provider game IDs do not match authority")
        if collect_before is None:
            raise _ProviderWindowUnverified("provider collection fence is unavailable")
        return json.dumps({
            "window": window,
            "provider_source": "nba_stats.team_game_log",
            "collect_before": assume_utc(collect_before).isoformat(),
            "teams": {
                str(team_id): {
                    "expected_games": expected_counts[team_id],
                    "authority_game_ids": sorted(game_ids_by_team[team_id]),
                    "provider_game_ids": sorted(provider_game_ids_by_team[team_id]),
                }
                for team_id in sorted(game_ids_by_team)
            },
        }, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _bind_legacy_window_evidence(
        facts: list[TeamMatchupFact],
        game_ids_by_team: Mapping[int, tuple[str, ...]],
        provider_window_identity: str | None,
        *, provenance: TeamMatchupProvenance | None,
        cutoff: datetime,
    ) -> list[TeamMatchupFact]:
        bound = []
        for fact in facts:
            if not (
                provider_window_identity
                and fact.base in {"traditional", "assist_locations"}
            ):
                bound.append(fact)
                continue
            bound.append(replace(
                fact,
                game_ids=tuple(game_ids_by_team.get(fact.team_id, ())),
                cutoff=provenance.cutoff if provenance else None,
                manifest_id=provenance.manifest_id if provenance else None,
                event_catalog_publication_id=(
                    provenance.event_catalog_publication_id if provenance else None
                ),
                event_catalog_checksum=(
                    provenance.event_catalog_checksum if provenance else None
                ),
                provider_window_identity=provider_window_identity,
            ))
        return bound

    @staticmethod
    def _bind_observation_evidence(
        observations: tuple[TeamMatchupObservation, ...],
        provider_window_identity: str | None,
        *, provenance: TeamMatchupProvenance | None,
        cutoff: datetime,
    ) -> tuple[TeamMatchupObservation, ...]:
        return tuple(
            replace(
                observation,
                game_ids=(),
                cutoff=provenance.cutoff if provenance else None,
                manifest_id=provenance.manifest_id if provenance else None,
                event_catalog_publication_id=(
                    provenance.event_catalog_publication_id if provenance else None
                ),
                event_catalog_checksum=(
                    provenance.event_catalog_checksum if provenance else None
                ),
                provider_window_identity=provider_window_identity,
            )
            if provider_window_identity
            and observation.surface in {"traditional", "assist_locations"}
            else observation
            for observation in observations
        )

    @staticmethod
    def _require_governed_roster(
        facts: list[TeamMatchupFact],
        team_ids: tuple[int, ...],
    ) -> list[TeamMatchupFact]:
        if facts and {fact.team_id for fact in facts} != set(team_ids):
            raise _ProviderRosterMismatch(
                "provider response does not match the governed NBA team roster"
            )
        return facts

    def _collect_season(
        self,
        season: str,
        *,
        snapshot_date: date,
        include_play_types: bool,
        team_ids: tuple[int, ...],
        expected_game_counts: Mapping[int, int],
        expected_game_ids_by_team: Mapping[int, Iterable[str]],
        verify_window: bool,
    ) -> tuple[
        list[TeamMatchupFact],
        dict[str, tuple[str, str | None]],
        dict[int, tuple[str, ...]] | None,
    ]:
        date_to = self._nba_date(snapshot_date)
        common = {
            "season": season,
            "season_type": "Regular Season",
            "team_id": None,
            "last_n_games": 0,
            "date_to": date_to,
        }
        facts_by_surface: dict[str, list[TeamMatchupFact]] = {
            surface: [] for surface in MATCHUP_SURFACES
        }
        failures: dict[str, tuple[str, str | None]] = {}
        provider_ids_by_surface: dict[str, dict[int, tuple[str, ...]]] = {}
        try:
            if verify_window:
                provider_ids_by_surface["traditional"] = (
                    self._independent_provider_game_ids(
                        season=season,
                        season_type="Regular Season",
                        team_ids=team_ids,
                        date_from=None,
                        date_to=date_to,
                        expected_game_ids_by_team=expected_game_ids_by_team,
                    )
                )
            traditional_frame = self.nba_stats.fetch_opponent_team_stats(
                None, per_mode_detailed="Totals", **common
            )
            if verify_window:
                self._verify_aggregate_window(
                    traditional_frame,
                    expected_game_counts=expected_game_counts,
                    expected_game_ids_by_team=expected_game_ids_by_team,
                    # LeagueDashTeamStats is an aggregate endpoint and does
                    # not expose membership.  Exact IDs come only from the
                    # independent TeamGameLog response above.
                    require_game_ids=False,
                )
            minutes_by_team = self._minutes_by_team(traditional_frame)
        except (ProviderResponseError, ValueError) as error:
            minutes_by_team = None
            dependent_surfaces = ["traditional", "shot_types", "shot_zones"]
            if include_play_types:
                dependent_surfaces.append("play_types")
            for surface in dependent_surfaces:
                failures[surface] = self._provider_failure(error)
        if minutes_by_team is not None:
            try:
                facts_by_surface["traditional"] = self._require_governed_roster(
                    self._traditional_facts(traditional_frame), team_ids
                )
            except (ProviderResponseError, ValueError) as error:
                failures["traditional"] = self._provider_failure(error)
            try:
                for shooting_type in SHOOTING_TYPES:
                    facts_by_surface["shot_types"].extend(
                        self._shot_type_facts(
                            self.nba_stats.fetch_opponent_shot_chart(
                                shooting_type,
                                None,
                                per_mode_simple="Totals",
                                **common,
                            ),
                            shooting_type,
                            minutes_by_team=minutes_by_team,
                        )
                    )
                self._require_governed_roster(
                    facts_by_surface["shot_types"], team_ids
                )
            except (ProviderResponseError, ValueError) as error:
                failures["shot_types"] = self._provider_failure(error)
            try:
                facts_by_surface["shot_zones"] = self._require_governed_roster(
                    self._shot_zone_facts(
                        self.nba_stats.fetch_opponent_shooting_zone(
                            None, per_mode_detailed="Totals", **common
                        ),
                        minutes_by_team=minutes_by_team,
                    ),
                    team_ids,
                )
            except (ProviderResponseError, ValueError) as error:
                failures["shot_zones"] = self._provider_failure(error)
            if include_play_types:
                try:
                    for play_type in PLAY_TYPES:
                        facts_by_surface["play_types"].extend(
                            self._play_type_facts(
                                self.nba_stats.fetch_synergy_play_types(
                                    play_type,
                                    player_or_team_abbreviation="T",
                                    type_grouping="Defensive",
                                    season=season,
                                    per_mode_simple="Totals",
                                ),
                                play_type,
                                minutes_by_team=minutes_by_team,
                            )
                        )
                    self._require_governed_roster(
                        facts_by_surface["play_types"], team_ids
                    )
                except (ProviderResponseError, ValueError) as error:
                    failures["play_types"] = self._provider_failure(error)
        try:
            if verify_window:
                provider_ids = provider_ids_by_surface.get("traditional")
                if provider_ids is None:
                    raise _ProviderWindowUnverified(
                        "independent provider game membership is unavailable"
                    )
            assist_frame = self.pbp_stats.fetch_totals_frame(
                        "opponent",
                        season=season,
                        season_type="Regular Season",
                        team_id=None,
                        from_date=None,
                        to_date=snapshot_date.isoformat(),
                    )
            if verify_window:
                self._verify_aggregate_window(
                    assist_frame,
                    expected_game_counts=expected_game_counts,
                    expected_game_ids_by_team=expected_game_ids_by_team,
                    require_game_ids=False,
                )
                provider_ids_by_surface["assist_locations"] = (
                    provider_ids_by_surface["traditional"]
                )
            facts_by_surface["assist_locations"] = self._require_governed_roster(
                self._assist_facts(assist_frame),
                team_ids,
            )
        except (ProviderResponseError, ValueError) as error:
            failures["assist_locations"] = self._provider_failure(error)
        provider_game_ids = None
        traditional_ids = provider_ids_by_surface.get("traditional")
        assist_ids = provider_ids_by_surface.get("assist_locations")
        if traditional_ids is not None and assist_ids == traditional_ids:
            provider_game_ids = traditional_ids
        return (
            [
                fact
                for surface, surface_facts in facts_by_surface.items()
                if surface not in failures
                for fact in surface_facts
            ],
            failures,
            provider_game_ids,
        )

    def _collect_last_15(
        self,
        season: str,
        *,
        snapshot_date: date,
        team_ids: tuple[int, ...],
        boundaries: Mapping[int, TeamWindowBoundary],
        verify_window: bool,
    ) -> tuple[
        list[TeamMatchupFact],
        dict[str, tuple[str, str | None]],
        dict[int, tuple[str, ...]] | None,
    ]:
        facts_by_surface: dict[str, list[TeamMatchupFact]] = {
            surface: [] for surface in MATCHUP_SURFACES
        }
        failures: dict[str, tuple[str, str | None]] = {}
        provider_ids_by_surface: dict[str, dict[int, tuple[str, ...]]] = {
            "traditional": {},
            "assist_locations": {},
        }
        date_to = self._nba_date(snapshot_date)
        for team_id in team_ids:
            boundary = boundaries[team_id]
            season_type = cast(str, boundary.season_type)
            common = {
                "season": season,
                "season_type": season_type,
                "team_id": team_id,
                "last_n_games": 15,
                "date_to": date_to,
            }
            minutes_by_team = None
            if any(
                surface not in failures
                for surface in ("traditional", "shot_types", "shot_zones")
            ):
                try:
                    if verify_window:
                        provider_ids = self._independent_provider_game_ids(
                            season=season,
                            season_type=season_type,
                            team_ids=(team_id,),
                            date_from=self._nba_date(boundary.from_date),
                            date_to=date_to,
                            expected_game_ids_by_team={
                                team_id: boundary.game_ids,
                            },
                        )
                        provider_ids_by_surface["traditional"][team_id] = (
                            provider_ids[team_id]
                        )
                    traditional_frame = self.nba_stats.fetch_opponent_team_stats(
                        self._nba_date(boundary.from_date),
                        per_mode_detailed="Totals",
                        **common,
                    )
                    self._verify_team_window(
                        traditional_frame,
                        team_id=team_id,
                        expected_games=len(boundary.game_ids),
                        expected_game_ids=boundary.game_ids,
                        require_game_count=True,
                        # LeagueDashTeamStats is aggregate-only; membership is
                        # independently evidenced by TeamGameLog above.
                        require_game_ids=False,
                    )
                    minutes_by_team = self._minutes_by_team(traditional_frame)
                    if "traditional" not in failures:
                        facts_by_surface["traditional"].extend(
                            self._with_start(
                                self._traditional_facts(traditional_frame),
                                boundary.from_date,
                            )
                        )
                except (ProviderResponseError, ValueError) as error:
                    failure = self._provider_failure(error)
                    failures.setdefault("traditional", failure)
                    if minutes_by_team is None:
                        failures.setdefault("shot_types", failure)
                        failures.setdefault("shot_zones", failure)

            if minutes_by_team is not None and "shot_types" not in failures:
                try:
                    team_shot_type_facts = []
                    for shooting_type in SHOOTING_TYPES:
                        frame = self.nba_stats.fetch_opponent_shot_chart(
                            shooting_type,
                            self._nba_date(boundary.from_date),
                            per_mode_simple="Totals",
                            **common,
                        )
                        self._verify_team_window(
                            frame,
                            team_id=team_id,
                            expected_games=len(boundary.game_ids),
                            expected_game_ids=boundary.game_ids,
                            require_game_count=True,
                            require_game_ids=False,
                        )
                        team_shot_type_facts.extend(
                            self._shot_type_facts(
                                frame,
                                shooting_type,
                                minutes_by_team=minutes_by_team,
                            )
                        )
                    facts_by_surface["shot_types"].extend(
                        self._with_start(team_shot_type_facts, boundary.from_date)
                    )
                except (ProviderResponseError, ValueError) as error:
                    failures.setdefault("shot_types", self._provider_failure(error))

            if minutes_by_team is not None and "shot_zones" not in failures:
                try:
                    frame = self.nba_stats.fetch_opponent_shooting_zone(
                        self._nba_date(boundary.from_date),
                        per_mode_detailed="Totals",
                        **common,
                    )
                    self._verify_team_window(
                        frame,
                        team_id=team_id,
                        expected_games=len(boundary.game_ids),
                        expected_game_ids=boundary.game_ids,
                        require_game_count=False,
                        require_game_ids=False,
                    )
                    facts_by_surface["shot_zones"].extend(
                        self._with_start(
                            self._shot_zone_facts(
                                frame,
                                minutes_by_team=minutes_by_team,
                            ),
                            boundary.from_date,
                        )
                    )
                except (ProviderResponseError, ValueError) as error:
                    failures.setdefault("shot_zones", self._provider_failure(error))

            if "assist_locations" not in failures:
                try:
                    if verify_window:
                        provider_ids = provider_ids_by_surface["traditional"].get(team_id)
                        if provider_ids is None:
                            raise _ProviderWindowUnverified(
                                "independent provider game membership is unavailable"
                            )
                    frame = self.pbp_stats.fetch_totals_frame(
                        "opponent",
                        season=season,
                        season_type=season_type,
                        team_id=team_id,
                        from_date=boundary.from_date.isoformat(),
                        to_date=boundary.to_date.isoformat(),
                    )
                    self._verify_team_window(
                        frame,
                        team_id=team_id,
                        expected_games=len(boundary.game_ids),
                        expected_game_ids=boundary.game_ids,
                        require_game_count=True,
                        require_game_ids=False,
                        required_columns=(
                            "TeamId",
                            "SecondsPlayed",
                            "GamesPlayed",
                        ),
                    )
                    if verify_window:
                        provider_ids_by_surface["assist_locations"][team_id] = (
                            provider_ids_by_surface["traditional"][team_id]
                        )
                    facts_by_surface["assist_locations"].extend(
                        self._with_start(
                            self._assist_facts(frame),
                            boundary.from_date,
                        )
                    )
                except (ProviderResponseError, ValueError) as error:
                    failures.setdefault(
                        "assist_locations", self._provider_failure(error)
                    )
        facts = [
            fact
            for surface in MATCHUP_SURFACES
            if surface not in failures
            for fact in facts_by_surface[surface]
        ]
        provider_game_ids = None
        if (
            provider_ids_by_surface["traditional"]
            and provider_ids_by_surface["assist_locations"]
            == provider_ids_by_surface["traditional"]
        ):
            provider_game_ids = provider_ids_by_surface["traditional"]
        return facts, failures, provider_game_ids

    @staticmethod
    def _nba_date(value: date) -> str:
        return value.strftime("%m/%d/%Y")

    def _independent_provider_game_ids(
        self,
        *,
        season: str,
        season_type: str,
        team_ids: Iterable[int],
        date_from: str | None,
        date_to: str | None,
        expected_game_ids_by_team: Mapping[int, Iterable[str]],
    ) -> dict[int, tuple[str, ...]]:
        """Collect membership from a provider detail endpoint.

        Aggregate rows are intentionally never treated as game identity
        evidence.  Production NBAStatsAdapter exposes TeamGameLog; injected
        providers must expose the same optional seam in governed runs or the
        window is unavailable rather than being relabeled from the catalog.
        """
        fetcher = getattr(self.nba_stats, "fetch_team_game_ids", None)
        if not callable(fetcher):
            raise _ProviderWindowUnverified(
                "provider detail endpoint cannot prove exact game membership"
            )
        result: dict[int, tuple[str, ...]] = {}
        for team_id in team_ids:
            values = fetcher(
                team_id=int(team_id),
                season=season,
                season_type=season_type,
                date_from=date_from,
                date_to=date_to,
            )
            if isinstance(values, pd.DataFrame):
                if "GAME_ID" not in values.columns:
                    raise _ProviderWindowUnverified(
                        "provider detail response is missing GAME_ID"
                    )
                values = values["GAME_ID"].tolist()
            if isinstance(values, str) or not isinstance(values, Iterable):
                raise _ProviderWindowUnverified(
                    "provider detail response has invalid game membership"
                )
            ids = tuple(sorted(str(value).strip() for value in values))
            if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
                raise _ProviderWindowUnverified(
                    "provider detail response has invalid game membership"
                )
            expected_ids = tuple(sorted(
                str(value).strip()
                for value in expected_game_ids_by_team.get(int(team_id), ())
            ))
            if ids != expected_ids:
                raise _ProviderWindowUnverified(
                    "provider detail response does not match governed game membership"
                )
            result[int(team_id)] = ids
        return result

    @classmethod
    def _verify_team_window(
        cls,
        frame: pd.DataFrame,
        *,
        team_id: int,
        expected_games: int,
        expected_game_ids: Iterable[str] | None = None,
        require_game_count: bool,
        require_game_ids: bool = False,
        required_columns: tuple[str, ...] = (),
    ) -> tuple[str, ...] | None:
        records = cls._flat_frame(frame).to_dict(orient="records")
        if len(records) != 1:
            raise _ProviderWindowUnverified(
                f"provider response does not identify only team {team_id}"
            )
        row = records[0]
        missing = [
            column
            for column in required_columns
            if column not in row or pd.isna(row[column])
        ]
        if missing:
            raise _ProviderWindowUnverified(
                "provider response is missing rolling-window evidence"
            )
        try:
            identified_team = cls._team_id(row)
        except ValueError as error:
            raise _ProviderWindowUnverified(str(error)) from error
        if identified_team != team_id:
            raise _ProviderWindowUnverified(
                f"provider response does not identify only team {team_id}"
            )
        game_counts = [
            row[column]
            for column in ("GP", "G", "GamesPlayed", "Games")
            if column in row and not pd.isna(row[column])
        ]
        if require_game_count and not game_counts:
            raise _ProviderWindowUnverified(
                "provider response does not expose its aggregate game count"
            )
        if game_counts:
            try:
                matches = all(float(value) == expected_games for value in game_counts)
            except (TypeError, ValueError) as error:
                raise _ProviderWindowUnverified(
                    "provider response has an invalid aggregate game count"
                ) from error
            if not matches:
                raise _ProviderWindowUnverified(
                    f"provider response does not contain exactly {expected_games} games"
                )
        return cls._verify_provider_game_ids(
            row, expected_game_ids, require_game_ids=require_game_ids
        )

    @staticmethod
    def _verify_provider_game_ids(
        row: Mapping[str, Any], expected_game_ids: Iterable[str] | None, *,
        require_game_ids: bool = False,
    ) -> tuple[str, ...] | None:
        """Verify exact provider membership, when the surface requires it."""
        if expected_game_ids is None:
            return None
        value = None
        for column in ("GAME_IDS", "GameIds", "GAME_IDs"):
            if column not in row:
                continue
            candidate = row[column]
            if candidate is None:
                continue
            missing = pd.isna(candidate)
            if not hasattr(missing, "__len__") and bool(missing):
                continue
            value = candidate
            break
        if value is None:
            if require_game_ids:
                raise _ProviderWindowUnverified(
                    "provider aggregate does not expose exact game IDs"
                )
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = [value]
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise _ProviderWindowUnverified("provider game IDs are malformed")
        actual = tuple(sorted(str(game_id) for game_id in value))
        if len(actual) != len(set(actual)):
            raise _ProviderWindowUnverified("provider game IDs are duplicated")
        if frozenset(actual) != frozenset(str(game_id) for game_id in expected_game_ids):
            raise _ProviderWindowUnverified("provider game IDs do not match authority")
        return actual

    @classmethod
    def _verify_aggregate_window(
        cls,
        frame: pd.DataFrame,
        *,
        expected_game_counts: Mapping[int, int],
        expected_game_ids_by_team: Mapping[int, Iterable[str]],
        require_game_ids: bool = False,
    ) -> dict[int, tuple[str, ...]]:
        records = cls._flat_frame(frame).to_dict(orient="records")
        if len(records) != len(expected_game_counts):
            raise _ProviderWindowUnverified("provider aggregate roster is incomplete")
        seen: set[int] = set()
        provider_ids: dict[int, tuple[str, ...]] = {}
        for row in records:
            team_id = cls._team_id(row)
            if team_id in seen or team_id not in expected_game_counts:
                raise _ProviderWindowUnverified("provider aggregate roster is invalid")
            seen.add(team_id)
            game_counts = [
                row[column]
                for column in ("GP", "G", "GamesPlayed", "Games")
                if column in row and not pd.isna(row[column])
            ]
            if not game_counts:
                raise _ProviderWindowUnverified("provider aggregate lacks game count")
            if any(float(value) != expected_game_counts[team_id] for value in game_counts):
                raise _ProviderWindowUnverified("provider aggregate game count mismatch")
            provider_ids[team_id] = cls._verify_provider_game_ids(
                row,
                expected_game_ids_by_team.get(team_id),
                require_game_ids=require_game_ids,
            )
        if seen != set(expected_game_counts):
            raise _ProviderWindowUnverified("provider aggregate roster is incomplete")
        return {
            team_id: ids
            for team_id, ids in provider_ids.items()
            if ids is not None
        }

    @staticmethod
    def _flat_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            raise ValueError("team matchup providers must return DataFrames")
        result = frame.copy()
        if isinstance(result.columns, pd.MultiIndex):
            result.columns = [
                "_".join(str(part) for part in column if str(part)).strip("_")
                for column in result.columns
            ]
        return result

    @staticmethod
    def _team_id(row: Mapping[str, Any]) -> int:
        value = row.get("TEAM_ID", row.get("TeamId"))
        if value is None or pd.isna(value):
            raise ValueError("team matchup provider row is missing TEAM_ID")
        return int(value)

    @staticmethod
    def _denominator(row: Mapping[str, Any]) -> tuple[float, str]:
        for column, unit in (
            ("MIN", "minutes"),
            ("SecondsPlayed", "seconds"),
        ):
            value = row.get(column)
            if value is not None and not pd.isna(value) and float(value) > 0:
                return float(value), unit
        raise ValueError("team matchup provider row is missing a positive denominator")

    @classmethod
    def _minutes_by_team(cls, frame: pd.DataFrame) -> dict[int, float]:
        minutes_by_team = {}
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            value = row.get("MIN")
            if value is None or pd.isna(value) or float(value) <= 0:
                raise ValueError(
                    "traditional provider row is missing positive team minutes"
                )
            minutes_by_team[cls._team_id(row)] = float(value)
        return minutes_by_team

    @classmethod
    def _denominator_for_team(
        cls,
        row: Mapping[str, Any],
        minutes_by_team: Mapping[int, float],
    ) -> tuple[float, str]:
        team_id = cls._team_id(row)
        try:
            return minutes_by_team[team_id], "minutes"
        except KeyError as exc:
            raise ValueError(
                f"traditional provider response is missing team {team_id}"
            ) from exc

    @classmethod
    def _traditional_facts(cls, frame: pd.DataFrame) -> list[TeamMatchupFact]:
        facts = []
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator(row)
            for stat_key in _TRADITIONAL_STATS:
                if stat_key not in row:
                    raise ValueError(f"traditional provider row is missing {stat_key}")
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "traditional",
                        stat_key,
                        stat_key,
                        float(row[stat_key]),
                        denominator,
                        unit,
                        "nba_stats",
                    )
                )
        return facts

    @classmethod
    def _shot_type_facts(
        cls,
        frame: pd.DataFrame,
        shooting_type: str,
        *,
        minutes_by_team: Mapping[int, float],
    ) -> list[TeamMatchupFact]:
        facts = []
        slice_key = shooting_type.replace(" ", "_").casefold()
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator_for_team(row, minutes_by_team)
            for stat_key in _SHOT_STATS:
                if stat_key not in row:
                    raise ValueError(f"shot-type provider row is missing {stat_key}")
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "shot_types",
                        slice_key,
                        stat_key,
                        float(row[stat_key]),
                        denominator,
                        unit,
                        "nba_stats",
                    )
                )
        return facts

    @classmethod
    def _shot_zone_facts(
        cls,
        frame: pd.DataFrame,
        *,
        minutes_by_team: Mapping[int, float],
    ) -> list[TeamMatchupFact]:
        facts = []
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator_for_team(row, minutes_by_team)
            stat_columns = [
                column
                for column in row
                if str(column).endswith(("_OPP_FGM", "_OPP_FGA"))
                and "Backcourt" not in str(column)
            ]
            if not stat_columns:
                raise ValueError(
                    "shot-zone provider row has no raw opponent makes/attempts"
                )
            for column in stat_columns:
                slice_key, stat_key = str(column).rsplit("_OPP_", 1)
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "shot_zones",
                        slice_key,
                        stat_key,
                        float(row[column]),
                        denominator,
                        unit,
                        "nba_stats",
                    )
                )
        return facts

    @classmethod
    def _play_type_facts(
        cls,
        frame: pd.DataFrame,
        play_type: str,
        *,
        minutes_by_team: Mapping[int, float],
    ) -> list[TeamMatchupFact]:
        facts = []
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator_for_team(row, minutes_by_team)
            for stat_key in ("PTS", "POSS"):
                if stat_key not in row:
                    raise ValueError(f"play-type provider row is missing {stat_key}")
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "play_types",
                        play_type,
                        stat_key,
                        float(row[stat_key]),
                        denominator,
                        unit,
                        "nba_synergy",
                    )
                )
        return facts

    @classmethod
    def _assist_facts(cls, frame: pd.DataFrame) -> list[TeamMatchupFact]:
        facts = []
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator(row)
            for stat_key in _ASSIST_STATS:
                if stat_key not in row:
                    raise ValueError(f"PBP opponent row is missing {stat_key}")
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "assist_locations",
                        stat_key,
                        stat_key,
                        float(row[stat_key]),
                        denominator,
                        unit,
                        "pbp_stats",
                    )
                )
        return facts

    @staticmethod
    def _with_start(facts: list[TeamMatchupFact], start: date) -> list[TeamMatchupFact]:
        return [replace(fact, window_start_date=start) for fact in facts]


__all__ = [
    "TeamMatchupEventCatalog",
    "TeamMatchupNBAStatsProvider",
    "TeamMatchupPBPStatsProvider",
    "TeamMatchupRefreshService",
    "TeamMatchupProvenance",
    "TeamWindowBoundary",
    "TeamWindowBoundaryResolver",
    "governed_season_type",
    "is_governed_event",
]
