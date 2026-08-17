"""High-level ledger-owned Season and exact L15 matchup materialization (#114).

This module owns the seam that turns stored Canonical Game Ledger evidence into
the disposable ``team_matchup_facts`` read model without any provider call.
One interface accepts a season, a shared cutoff, and the runtime's
authoritative governed window -- the expected game IDs, expected L15 game IDs,
and team IDs resolved from the active manifest and Event Catalog.  It records
the exact governed game IDs and the deterministic ledger checksum, and
aggregates every contracted PBP-owned non-shot opponent fact (the traditional
opponent counts and assist locations) exclusively from typed ledger counts and
denominators.  NBA-owned shot and play surfaces are deliberately outside this
service: their independent refresh writes the same disposable read model and
can fail without preventing ledger-owned surfaces from materializing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

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
from app.services.ledger_lineage import LedgerLineage
from app.services.team_matchup_repository import (
    _LedgerRecompositionWriteCapability,
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
    :class:`CanonicalGameLedgerRepository`, and it publishes only the
    ``traditional`` and ``assist_locations`` surfaces to the disposable
    ``team_matchup_facts`` read model.
    """

    def __init__(
        self,
        repository: CanonicalGameLedgerRepository,
        matchup_repository: TeamMatchupRepository,
        *,
        ledger_write_capability: _LedgerRecompositionWriteCapability | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(repository, CanonicalGameLedgerRepository):
            raise TypeError("repository must be a CanonicalGameLedgerRepository")
        if not isinstance(matchup_repository, TeamMatchupRepository):
            raise TypeError("matchup_repository must be a TeamMatchupRepository")
        self.repository = repository
        self.matchup_repository = matchup_repository
        self._ledger_write_capability = ledger_write_capability
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def materialize(
        self,
        season: str,
        *,
        as_of: date,
        expected_game_ids: frozenset[str],
        expected_l15_game_ids: Mapping[int, frozenset[str]],
        team_ids: frozenset[int],
        cutoff: datetime | None = None,
        recomposition_reason: str | None = None,
        affected_team_ids: frozenset[int] | None = None,
        trigger_game_id: str | None = None,
        trigger_game_ids: frozenset[str] | None = None,
        session: Session | None = None,
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
        if cutoff is not None:
            cutoff = assume_utc(cutoff)
            if cutoff.date() != as_of:
                raise ValueError("cutoff must match the materialization date")
        current_date = retrieved_at.astimezone(EASTERN).date()
        if as_of > current_date:
            raise ValueError("future as_of dates cannot be published")
        games, checksums = self._load_games(
            canonical_season,
            as_of,
            connection=session.connection() if session is not None else None,
        )
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
            cutoff=cutoff,
            recomposition_reason=recomposition_reason,
        )
        l15_facts, l15_observations = self._window_read_model(
            l15_window,
            assist_l15,
            checksums=checksums,
            games_by_id=games_by_id,
            roster_incomplete=roster_incomplete,
            cutoff=cutoff,
            recomposition_reason=recomposition_reason,
        )
        snapshots = [(season_scope, season_facts, season_observations)]
        snapshot_kwargs = {"retrieved_at": retrieved_at}
        if affected_team_ids is not None:
            selected_trigger_game_ids = trigger_game_ids
            if selected_trigger_game_ids is None and trigger_game_id is not None:
                selected_trigger_game_ids = frozenset({trigger_game_id})
            l15_affected_team_ids = frozenset(
                team.team_id
                for team in l15_window.teams
                if team.team_id in affected_team_ids
                and (
                    selected_trigger_game_ids is None
                    or bool(set(team.game_ids) & selected_trigger_game_ids)
                )
            )
            # Season is one publication over the complete governed game set,
            # so a correction always rebuilds its complete fact envelope. An
            # exact-L15 correction outside every selected team is a no-op
            # for an existing scope; the repository also uses the empty target
            # to build a missing scope completely.
            snapshot_kwargs["affected_team_ids_by_scope"] = {season_scope: None}
            snapshots.append((l15_scope, l15_facts, l15_observations))
            snapshot_kwargs["affected_team_ids_by_scope"][l15_scope] = (
                l15_affected_team_ids
            )
        else:
            snapshots.append((l15_scope, l15_facts, l15_observations))
        if self._ledger_write_capability is not None:
            if session is None:
                raise PermissionError("ledger_recomposition_session_required")
            self.matchup_repository.replace_ledger_snapshots(
                snapshots,
                **snapshot_kwargs,
                capability=self._ledger_write_capability,
                session=session,
            )
        elif session is None:
            self.matchup_repository.replace_snapshots(snapshots, **snapshot_kwargs)
        else:
            self.matchup_repository.replace_snapshots(
                snapshots,
                **snapshot_kwargs,
                session=session,
            )
        return LedgerMatchupMaterialization(
            season=canonical_season,
            as_of=as_of,
            season_selection=self._selection(season_window, season_scope, checksums),
            l15_selection=self._selection(l15_window, l15_scope, checksums),
        )

    def _load_games(
        self,
        season: str,
        as_of: date,
        *,
        connection: Connection | None = None,
    ) -> tuple[tuple[CanonicalGame, ...], dict[str, str]]:
        """Load the governed Regular Season ledger games through ``as_of``."""

        summaries = self.repository.list_games(
            season,
            through=as_of,
            connection=connection,
        )
        games = tuple(
            game
            for summary in summaries
            if (game := self.repository.get_game(
                summary.game_id,
                connection=connection,
            )) is not None
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
        cutoff: datetime | None,
        recomposition_reason: str | None,
    ) -> tuple[tuple[TeamMatchupFact, ...], tuple[TeamMatchupObservation, ...]]:
        """Build disposable facts and observations for one window.

        The observations always persist the truthful surface availability plus
        the ledger-owned lineage (the governed/selected game IDs and their
        deterministic ledger checksum), even when roster or window completeness
        blocks facts and ranks.
        """

        governed_game_ids = window.governed_game_ids
        ledger_checksum = window_ledger_checksum(governed_game_ids, checksums)
        game_set_checksum = _game_set_checksum(governed_game_ids)
        source_observation_ids = _source_observation_ids(
            governed_game_ids, games_by_id
        )
        if roster_incomplete:
            return (), self._missing_observations(
                ROSTER_INCOMPLETE_REASON,
                game_ids=governed_game_ids,
                ledger_checksum=ledger_checksum,
                source_observation_ids=source_observation_ids,
                game_set_checksum=game_set_checksum,
                cutoff=cutoff,
                recomposition_reason=recomposition_reason,
            )
        if not window.complete:
            return (), self._missing_observations(
                window.reason,
                game_ids=governed_game_ids,
                ledger_checksum=ledger_checksum,
                source_observation_ids=source_observation_ids,
                game_set_checksum=game_set_checksum,
                cutoff=cutoff,
                recomposition_reason=recomposition_reason,
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
                source_observation_ids=source_observation_ids,
                game_set_checksum=game_set_checksum,
                cutoff=cutoff,
                recomposition_reason=recomposition_reason,
            )
        ]
        facts = list(
            self._traditional_facts(
                window,
                ledger_checksum=ledger_checksum,
                start_dates=start_dates,
                games_by_id=games_by_id,
                cutoff=cutoff,
                recomposition_reason=recomposition_reason,
            )
        )
        if assist_window is not None and assist_window.complete:
            facts.extend(
                self._assist_facts(
                    assist_window,
                    ledger_checksum=ledger_checksum,
                    start_dates=start_dates,
                    games_by_id=games_by_id,
                    cutoff=cutoff,
                    recomposition_reason=recomposition_reason,
                )
            )
            observations.append(
                TeamMatchupObservation(
                    surface="assist_locations",
                    status="available",
                    game_ids=governed_game_ids,
                    ledger_checksum=ledger_checksum,
                    source_observation_ids=source_observation_ids,
                    game_set_checksum=game_set_checksum,
                    cutoff=cutoff,
                    recomposition_reason=recomposition_reason,
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
                    source_observation_ids=source_observation_ids,
                    game_set_checksum=game_set_checksum,
                    cutoff=cutoff,
                    recomposition_reason=recomposition_reason,
                )
            )
        return tuple(facts), tuple(observations)

    @staticmethod
    def _missing_observations(
        reason: str | None,
        *,
        game_ids: tuple[str, ...],
        ledger_checksum: str,
        source_observation_ids: tuple[str, ...],
        game_set_checksum: str,
        cutoff: datetime | None,
        recomposition_reason: str | None,
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
                source_observation_ids=source_observation_ids,
                game_set_checksum=game_set_checksum,
                cutoff=cutoff,
                recomposition_reason=recomposition_reason,
            ),
            TeamMatchupObservation(
                "assist_locations",
                status,
                mapped_reason,
                game_ids=game_ids,
                ledger_checksum=ledger_checksum,
                source_observation_ids=source_observation_ids,
                game_set_checksum=game_set_checksum,
                cutoff=cutoff,
                recomposition_reason=recomposition_reason,
            ),
        )

    @staticmethod
    def _traditional_facts(
        window: TeamWindowMaterialization,
        *,
        ledger_checksum: str,
        start_dates: Mapping[int, date | None],
        games_by_id: Mapping[str, CanonicalGame],
        cutoff: datetime | None,
        recomposition_reason: str | None,
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
                        source_observation_ids=_source_observation_ids(
                            team.game_ids, games_by_id
                        ),
                        game_set_checksum=_game_set_checksum(team.game_ids),
                        cutoff=cutoff,
                        recomposition_reason=recomposition_reason,
                    )
                )
        return tuple(facts)

    @staticmethod
    def _assist_facts(
        window: AssistLocationWindowMaterialization,
        *,
        ledger_checksum: str,
        start_dates: Mapping[int, date | None],
        games_by_id: Mapping[str, CanonicalGame],
        cutoff: datetime | None,
        recomposition_reason: str | None,
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
                        source_observation_ids=_source_observation_ids(
                            team.game_ids, games_by_id
                        ),
                        game_set_checksum=_game_set_checksum(team.game_ids),
                        cutoff=cutoff,
                        recomposition_reason=recomposition_reason,
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


def _source_observation_ids(
    game_ids: tuple[str, ...],
    games_by_id: Mapping[str, CanonicalGame],
) -> tuple[str, ...]:
    """Return the accepted source observations for an exact game selection."""

    return tuple(sorted({
        games_by_id[game_id].source_observation_id
        for game_id in game_ids
        if game_id in games_by_id
    }))


def _game_set_checksum(game_ids: tuple[str, ...]) -> str:
    """Hash only the exact selected IDs, independently of their facts."""

    return LedgerLineage.for_game_ids(game_ids)


__all__ = [
    "LedgerMatchupMaterialization",
    "LedgerMatchupMaterializationService",
    "LedgerMatchupWindowSelection",
]
