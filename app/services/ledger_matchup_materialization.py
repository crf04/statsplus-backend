"""High-level ledger-owned Season and exact L15 matchup materialization (#114).

This module owns the seam that turns stored Canonical Game Ledger evidence and
governed NBA Publications into the disposable ``team_matchup_facts`` read
model without any provider call.  One interface accepts a season, a shared
cutoff, and the runtime's authoritative governed window -- the expected game
IDs, expected L15 game IDs, and team IDs resolved from the active manifest and
Event Catalog.  It records the exact governed game IDs and deterministic
ledger checksum, aggregates every contracted PBP-owned non-shot opponent fact
(the traditional opponent counts and assist locations) exclusively from typed
ledger counts and denominators, and composes NBA-owned shot and play surfaces
independently from their immutable publication lineage.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from app.domain.nba_events import REGULAR_SEASON_TYPE
from app.domain.utc import assume_utc
from app.services.canonical_game_ledger import (
    CanonicalGame,
    CanonicalGameLedgerRepository,
    validate_canonical_season,
)
from app.services.ledger_derivations import (
    MATCHUP_ASSIST_KEYS,
    MATCHUP_TRADITIONAL_KEYS,
    AssistLocationWindowMaterialization,
    LedgerDerivationUnavailable,
    TeamWindowMaterialization,
    materialize_assist_location_window,
    materialize_team_window,
    window_ledger_checksum,
)
from app.services.database_first_activation import (
    PublicationPayloadError,
    PublicationRead,
    decode_team_window,
)
from app.services.team_matchup_publications import (
    NBA_PUBLICATION_STREAMS,
    NBA_PUBLICATION_WINDOWS,
    publication_cutoff_reason,
    publication_lineage,
    publication_metric_identity,
    publication_stream,
    validate_publication_rows,
)
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)


EASTERN = ZoneInfo("America/New_York")

LEDGER_SOURCE = "ledger"

ROSTER_INCOMPLETE_REASON = "governed_team_roster_incomplete"
INSUFFICIENT_GAMES_REASON = "insufficient_governed_games"
ASSIST_INCOMPLETE_REASON = "assist_location_evidence_incomplete"

NBA_PUBLICATION_SOURCE = "nba_publication"


@dataclass(frozen=True, slots=True)
class LedgerMatchupWindowSelection:
    """The exact governed games one matchup window selected."""

    scope: TeamMatchupSnapshotScope
    governed_game_ids: tuple[str, ...]
    game_ids_by_team: Mapping[int, tuple[str, ...]]
    game_checksums: Mapping[str, str]
    ledger_checksum: str
    complete: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class LedgerMatchupMaterialization:
    """One shared-cutoff Season + exact L15 matchup materialization."""

    season: str
    as_of: date
    season_selection: LedgerMatchupWindowSelection
    l15_selection: LedgerMatchupWindowSelection


class LedgerMatchupMaterializationService:
    """Materialize ledger-owned matchup facts at one season and shared cutoff.

    The service has no provider dependency: every contracted non-shot opponent
    fact is aggregated from typed ledger counts and denominators supplied by
    :class:`CanonicalGameLedgerRepository`, while NBA-owned surfaces are read
    from governed Publications and published independently into the same
    disposable read model.
    """

    def __init__(
        self,
        repository: CanonicalGameLedgerRepository,
        matchup_repository: TeamMatchupRepository,
        *,
        publication_reader=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(repository, CanonicalGameLedgerRepository):
            raise TypeError("repository must be a CanonicalGameLedgerRepository")
        if not isinstance(matchup_repository, TeamMatchupRepository):
            raise TypeError("matchup_repository must be a TeamMatchupRepository")
        self.repository = repository
        self.matchup_repository = matchup_repository
        self.publication_reader = publication_reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def materialize(
        self,
        season: str,
        *,
        as_of: date,
        expected_game_ids: frozenset[str],
        expected_l15_game_ids: Mapping[int, frozenset[str]],
        team_ids: frozenset[int],
    ) -> LedgerMatchupMaterialization:
        """Publish ledger-owned Season and exact L15 matchup facts at ``as_of``.

        The expected window is never derived from the ledger being validated:
        ``expected_game_ids``, ``expected_l15_game_ids``, and ``team_ids`` are
        the authoritative active-manifest and Event-Catalog governance resolved
        by the runtime.  A stored ledger that does not exactly equal the
        governed game set, or a team whose ledger L15 does not match its
        governed exact 15, rejects the materialization.  Before every governed
        team has 15 eligible games the league L15 is published explicitly
        unavailable rather than approximated, and a Season that is not league
        complete publishes no facts.
        """

        canonical_season = validate_canonical_season(season)
        retrieved_at = assume_utc(self._clock())
        current_date = retrieved_at.astimezone(EASTERN).date()
        if as_of > current_date:
            raise ValueError("future as_of dates cannot be published")
        games, checksums = self._load_games(canonical_season, as_of)
        self._reject_governance_mismatch(
            games,
            expected_game_ids=expected_game_ids,
            expected_l15_game_ids=expected_l15_game_ids,
        )
        season_scope = TeamMatchupSnapshotScope(canonical_season, as_of)
        l15_scope = TeamMatchupSnapshotScope(canonical_season, as_of, 15)
        season_window = materialize_team_window(
            games,
            season=canonical_season,
            as_of=as_of,
            expected_game_ids=expected_game_ids,
            team_ids=team_ids,
        )
        l15_window = materialize_team_window(
            games,
            season=canonical_season,
            as_of=as_of,
            window_games=15,
            expected_game_ids=expected_game_ids,
            expected_team_game_ids=expected_l15_game_ids,
            team_ids=team_ids,
        )
        assist_season = self._assist_window(
            games,
            season=canonical_season,
            as_of=as_of,
            expected_game_ids=expected_game_ids,
            expected_l15_game_ids=expected_l15_game_ids,
            team_ids=team_ids,
            window_games=None,
        )
        assist_l15 = self._assist_window(
            games,
            season=canonical_season,
            as_of=as_of,
            expected_game_ids=expected_game_ids,
            expected_l15_game_ids=expected_l15_game_ids,
            team_ids=team_ids,
            window_games=15,
        )
        games_by_id = {game.game_id: game for game in games}
        roster_incomplete = len(team_ids) != 30
        season_facts, season_observations = self._window_read_model(
            season_window,
            assist_season,
            checksums=checksums,
            games_by_id=games_by_id,
            roster_incomplete=roster_incomplete,
        )
        l15_facts, l15_observations = self._window_read_model(
            l15_window,
            assist_l15,
            checksums=checksums,
            games_by_id=games_by_id,
            roster_incomplete=roster_incomplete,
        )
        if self.publication_reader is not None:
            publication_reads = self._publication_reads(
                tuple(
                    publication_stream(base, window)
                    for window in NBA_PUBLICATION_WINDOWS
                    for base in NBA_PUBLICATION_STREAMS
                ),
                canonical_season,
            )
            season_publication_facts, season_publication_observations = (
                self._publication_read_model(
                    canonical_season,
                    as_of=as_of,
                    window="season",
                    reads=publication_reads,
                    expected_l15_game_ids=None,
                )
            )
            l15_publication_facts, l15_publication_observations = (
                self._publication_read_model(
                    canonical_season,
                    as_of=as_of,
                    window="l15",
                    reads=publication_reads,
                    expected_l15_game_ids=expected_l15_game_ids,
                )
            )
            season_facts = (*season_facts, *season_publication_facts)
            season_observations = (
                *season_observations,
                *season_publication_observations,
            )
            l15_facts = (*l15_facts, *l15_publication_facts)
            l15_observations = (
                *l15_observations,
                *l15_publication_observations,
            )
        self.matchup_repository.replace_snapshots(
            (
                (season_scope, season_facts, season_observations),
                (l15_scope, l15_facts, l15_observations),
            ),
            retrieved_at=retrieved_at,
        )
        return LedgerMatchupMaterialization(
            season=canonical_season,
            as_of=as_of,
            season_selection=self._selection(season_window, season_scope, checksums),
            l15_selection=self._selection(l15_window, l15_scope, checksums),
        )

    def _publication_read_model(
        self,
        season: str,
        *,
        as_of: date,
        window: str,
        reads: Mapping[str, object],
        expected_l15_game_ids: Mapping[int, frozenset[str]] | None,
    ) -> tuple[tuple[TeamMatchupFact, ...], tuple[TeamMatchupObservation, ...]]:
        """Project governed NBA team-window publications into raw facts.

        NBA-owned surfaces are read independently.  A missing, stale-invalid,
        or unsupported publication creates only its own unavailable/missing
        observation and never contributes facts from a ledger or legacy PBP
        surface.  The publication payload already contains per-48 values, so
        each fact stores an equivalent 48-minute numerator/denominator pair
        while retaining the immutable publication lineage beside it.
        """

        stream_by_base = {
            base: publication_stream(base, window)
            for base in NBA_PUBLICATION_STREAMS
        }
        facts: list[TeamMatchupFact] = []
        observations: list[TeamMatchupObservation] = []
        for base, stream_key in stream_by_base.items():
            read = reads.get(stream_key)
            if read is None:
                read = PublicationRead(
                    stream_key=stream_key,
                    publication_id=None,
                    season=season,
                    cutoff=None,
                    version=None,
                    status="missing",
                    freshness="missing",
                    age_seconds=None,
                    payload=None,
                )
            surface_facts, observation = self._publication_surface(
                base,
                stream_key,
                read,
                season=season,
                as_of=as_of,
                expected_l15_game_ids=expected_l15_game_ids,
            )
            facts.extend(surface_facts)
            observations.append(observation)
        return tuple(facts), tuple(observations)

    def _publication_reads(
        self,
        stream_keys: tuple[str, ...],
        season: str,
    ) -> dict[str, object]:
        """Capture one publication generation when the reader supports it."""

        snapshot = getattr(self.publication_reader, "snapshot", None)
        if callable(snapshot):
            captured = snapshot(stream_keys, season=season)
            reads = getattr(captured, "reads", None)
            if isinstance(reads, Mapping):
                return dict(reads)
            return {
                stream_key: captured.read(stream_key)
                for stream_key in stream_keys
            }
        read_many = getattr(self.publication_reader, "read_many", None)
        if callable(read_many):
            return dict(read_many(stream_keys, season=season))
        return {
            stream_key: self.publication_reader.read(stream_key, season=season)
            for stream_key in stream_keys
        }

    @staticmethod
    def _publication_surface(
        base: str,
        stream_key: str,
        read,
        *,
        season: str,
        as_of: date,
        expected_l15_game_ids: Mapping[int, frozenset[str]] | None,
    ) -> tuple[tuple[TeamMatchupFact, ...], TeamMatchupObservation]:
        """Decode one NBA publication without borrowing another surface."""

        lineage = publication_lineage(read)
        status = getattr(read, "status", "missing")
        available = bool(getattr(read, "available", False))
        if not available:
            observation_status = "missing" if status == "missing" else "unavailable"
            return (), TeamMatchupObservation(
                surface=base,
                status=observation_status,
                unavailable_reason=(
                    getattr(read, "unavailable_reason", None)
                    or f"publication_{status}"
                ),
                publication=lineage,
            )
        cutoff_reason = publication_cutoff_reason(read, as_of)
        if cutoff_reason is not None:
            return (), TeamMatchupObservation(
                surface=base,
                status="unavailable",
                unavailable_reason=cutoff_reason,
                publication=lineage,
            )
        try:
            rows = tuple(getattr(read, "decoded", None) or decode_team_window(
                read.payload,
                stream_key=stream_key,
            ))
        except (PublicationPayloadError, AttributeError):
            return (), TeamMatchupObservation(
                surface=base,
                status="unavailable",
                unavailable_reason="publication_payload_invalid",
                publication=lineage,
            )
        if len(rows) != 30:
            return (), TeamMatchupObservation(
                surface=base,
                status="unavailable",
                unavailable_reason="publication_surface_incomplete",
                publication=lineage,
            )
        try:
            metric_keys = validate_publication_rows(
                base, rows, expected_l15_game_ids=expected_l15_game_ids
            )
        except ValueError as exc:
            return (), TeamMatchupObservation(
                surface=base,
                status="unavailable",
                unavailable_reason=str(exc),
                publication=lineage,
            )
        game_ids = tuple(sorted({game_id for row in rows for game_id in row.game_ids}))
        facts = tuple(
            TeamMatchupFact(
                team_id=row.team_id,
                base=base,
                slice_key=metric_identity[0],
                stat_key=metric_identity[1],
                raw_value=float(row.per48[metric_key]),
                denominator_value=48.0,
                denominator_unit="minutes",
                provider=NBA_PUBLICATION_SOURCE,
                game_ids=tuple(row.game_ids),
                publication=lineage,
            )
            for row in rows
            for metric_key in metric_keys
            for metric_identity in (
                publication_metric_identity(base, metric_key),
            )
        )
        return facts, TeamMatchupObservation(
            surface=base,
            status="available",
            game_ids=game_ids,
            publication=lineage,
        )

    def _load_games(
        self, season: str, as_of: date
    ) -> tuple[tuple[CanonicalGame, ...], dict[str, str]]:
        """Load the governed Regular Season ledger games through ``as_of``."""

        summaries = self.repository.list_games(season, through=as_of)
        games = tuple(
            game
            for summary in summaries
            if (game := self.repository.get_game(summary.game_id)) is not None
            and game.season_type == REGULAR_SEASON_TYPE
        )
        checksums = {
            summary.game_id: summary.checksum
            for summary in summaries
            if summary.game_id in {game.game_id for game in games}
        }
        return games, checksums

    def _reject_governance_mismatch(
        self,
        games: tuple[CanonicalGame, ...],
        *,
        expected_game_ids: frozenset[str],
        expected_l15_game_ids: Mapping[int, frozenset[str]],
    ) -> None:
        """Reject a stored ledger that diverges from the governed window."""

        ledger_game_ids = frozenset(game.game_id for game in games)
        expected_ids = frozenset(expected_game_ids)
        if ledger_game_ids != expected_ids:
            missing = sorted(expected_ids - ledger_game_ids)
            extra = sorted(ledger_game_ids - expected_ids)
            message = "ledger games must exactly equal governed game IDs"
            if missing:
                message += f"; missing governed: {missing}"
            if extra:
                message += f"; extra ungoverned: {extra}"
            raise LedgerDerivationUnavailable(message)
        if not expected_l15_game_ids:
            return
        actual = self._actual_l15_game_ids(games)
        mismatched = {
            team_id
            for team_id, governed_ids in expected_l15_game_ids.items()
            if actual.get(team_id) != frozenset(governed_ids)
        }
        if mismatched:
            raise LedgerDerivationUnavailable(
                "ledger L15 game IDs do not match governed expectations "
                f"for teams {sorted(mismatched)}"
            )

    @staticmethod
    def _actual_l15_game_ids(
        games: tuple[CanonicalGame, ...],
    ) -> dict[int, frozenset[str]]:
        """Return each team's ledger exact 15 most recent game IDs.

        The derivation mirrors the completeness gate in
        ``materialize_team_window`` so the actual window and the governed
        window can never drift: games sort by date then game ID, newest first.
        Teams with fewer than 15 games yield the shorter set they have.
        """

        per_team: dict[int, list[CanonicalGame]] = defaultdict(list)
        for game in games:
            for fact in game.team_facts:
                per_team[fact.team_id].append(game)
        return {
            team_id: frozenset(
                game.game_id
                for game in sorted(
                    team_games,
                    key=lambda item: (item.game_date, item.game_id),
                    reverse=True,
                )[:15]
            )
            for team_id, team_games in per_team.items()
        }

    def _assist_window(
        self,
        games: tuple[CanonicalGame, ...],
        *,
        season: str,
        as_of: date,
        expected_game_ids: frozenset[str],
        expected_l15_game_ids: Mapping[int, frozenset[str]],
        team_ids: frozenset[int],
        window_games: int | None,
    ) -> AssistLocationWindowMaterialization | None:
        try:
            return materialize_assist_location_window(
                games,
                season=season,
                as_of=as_of,
                expected_game_ids=expected_game_ids,
                team_ids=team_ids,
                window_games=window_games,
                expected_team_game_ids=(
                    expected_l15_game_ids if window_games is not None else None
                ),
            )
        except LedgerDerivationUnavailable:
            return None

    def _window_read_model(
        self,
        window: TeamWindowMaterialization,
        assist_window: AssistLocationWindowMaterialization | None,
        *,
        checksums: Mapping[str, str],
        games_by_id: Mapping[str, CanonicalGame],
        roster_incomplete: bool,
    ) -> tuple[tuple[TeamMatchupFact, ...], tuple[TeamMatchupObservation, ...]]:
        """Build disposable facts and observations for one window.

        The observations always persist the truthful surface availability plus
        the ledger-owned lineage (the governed/selected game IDs and their
        deterministic ledger checksum), even when roster or window completeness
        blocks facts and ranks.
        """

        governed_game_ids = window.governed_game_ids
        ledger_checksum = window_ledger_checksum(governed_game_ids, checksums)
        if roster_incomplete:
            return (), self._missing_observations(
                ROSTER_INCOMPLETE_REASON,
                game_ids=governed_game_ids,
                ledger_checksum=ledger_checksum,
            )
        if not window.complete:
            return (), self._missing_observations(
                window.reason,
                game_ids=governed_game_ids,
                ledger_checksum=ledger_checksum,
            )
        start_dates = {
            team.team_id: min(
                (games_by_id[game_id].game_date for game_id in team.game_ids),
                default=None,
            )
            for team in window.teams
        }
        observations = [
            TeamMatchupObservation(
                surface="traditional",
                status="available",
                game_ids=governed_game_ids,
                ledger_checksum=ledger_checksum,
            )
        ]
        facts = list(
            self._traditional_facts(
                window, ledger_checksum=ledger_checksum, start_dates=start_dates
            )
        )
        if assist_window is not None and assist_window.complete:
            facts.extend(
                self._assist_facts(
                    assist_window,
                    ledger_checksum=ledger_checksum,
                    start_dates=start_dates,
                )
            )
            observations.append(
                TeamMatchupObservation(
                    surface="assist_locations",
                    status="available",
                    game_ids=governed_game_ids,
                    ledger_checksum=ledger_checksum,
                )
            )
        else:
            observations.append(
                TeamMatchupObservation(
                    surface="assist_locations",
                    status="unavailable",
                    unavailable_reason=ASSIST_INCOMPLETE_REASON,
                    game_ids=governed_game_ids,
                    ledger_checksum=ledger_checksum,
                )
            )
        return tuple(facts), tuple(observations)

    @staticmethod
    def _missing_observations(
        reason: str | None,
        *,
        game_ids: tuple[str, ...],
        ledger_checksum: str,
    ) -> tuple[TeamMatchupObservation, ...]:
        status, mapped_reason = (
            ("missing", INSUFFICIENT_GAMES_REASON)
            if reason and "15 eligible games" in reason
            else ("missing", ROSTER_INCOMPLETE_REASON)
            if reason and ("governed team roster" in reason or "League Complete" in reason)
            else ("missing", reason or "ledger_window_incomplete")
        )
        return (
            TeamMatchupObservation(
                "traditional",
                status,
                mapped_reason,
                game_ids=game_ids,
                ledger_checksum=ledger_checksum,
            ),
            TeamMatchupObservation(
                "assist_locations",
                status,
                mapped_reason,
                game_ids=game_ids,
                ledger_checksum=ledger_checksum,
            ),
        )

    @staticmethod
    def _traditional_facts(
        window: TeamWindowMaterialization,
        *,
        ledger_checksum: str,
        start_dates: Mapping[int, date | None],
    ) -> tuple[TeamMatchupFact, ...]:
        facts = []
        for team in window.teams:
            for stat_key, metric in MATCHUP_TRADITIONAL_KEYS.items():
                facts.append(
                    TeamMatchupFact(
                        team_id=team.team_id,
                        base="traditional",
                        slice_key=stat_key,
                        stat_key=stat_key,
                        raw_value=team.counts[metric],
                        denominator_value=team.team_minutes,
                        denominator_unit="minutes",
                        provider=LEDGER_SOURCE,
                        window_start_date=start_dates.get(team.team_id),
                        game_ids=team.game_ids,
                        ledger_checksum=ledger_checksum,
                    )
                )
        return tuple(facts)

    @staticmethod
    def _assist_facts(
        window: AssistLocationWindowMaterialization,
        *,
        ledger_checksum: str,
        start_dates: Mapping[int, date | None],
    ) -> tuple[TeamMatchupFact, ...]:
        facts = []
        for team in window.teams:
            for stat_key, metric in MATCHUP_ASSIST_KEYS.items():
                facts.append(
                    TeamMatchupFact(
                        team_id=team.team_id,
                        base="assist_locations",
                        slice_key=stat_key,
                        stat_key=stat_key,
                        raw_value=team.counts[metric],
                        denominator_value=team.team_minutes,
                        denominator_unit="minutes",
                        provider=LEDGER_SOURCE,
                        window_start_date=start_dates.get(team.team_id),
                        game_ids=team.game_ids,
                        ledger_checksum=ledger_checksum,
                    )
                )
        return tuple(facts)

    @staticmethod
    def _selection(
        window: TeamWindowMaterialization,
        scope: TeamMatchupSnapshotScope,
        checksums: Mapping[str, str],
    ) -> LedgerMatchupWindowSelection:
        return LedgerMatchupWindowSelection(
            scope=scope,
            governed_game_ids=window.governed_game_ids,
            game_ids_by_team={
                team.team_id: team.game_ids for team in window.teams
            },
            game_checksums={
                game_id: checksums[game_id]
                for game_id in window.governed_game_ids
            },
            ledger_checksum=window_ledger_checksum(
                window.governed_game_ids, checksums
            ),
            complete=window.complete,
            reason=window.reason,
        )


__all__ = [
    "LedgerMatchupMaterialization",
    "LedgerMatchupMaterializationService",
    "LedgerMatchupWindowSelection",
]
