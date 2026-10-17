"""Map Odds API events and player names onto canonical identities (#377).

The payload carries no player IDs, so a player is resolved by name against the
two rosters of the event's own game, through the same ``AthleteResolver`` and
governed mapping tables the DFS board uses (provider ``oddsapi``).  A name
that does not resolve to exactly one rostered athlete produces no quote; it is
counted by the caller and never guessed.

The vendor does not always spell a name as the catalog does: it drops or adds a
generational suffix (``Jimmy Butler`` for ``Jimmy Butler III``) and posts a few
players by nickname (``Nicolas Claxton`` for ``Nic Claxton``).  Within the two
rosters of the game, a posted name with no exact normalized match is read as
the one roster player it can only be (see ``NAME_VARIANTS``); with two
candidates, or none, it stays unresolved (#437).  The roster's spelling only
finds the catalog row: the evidence and the provider identity stay what was
posted, so governance treats the name as it always did.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from app.providers.dfs import AthleteEvidence, EventEvidence, TeamEvidence
from app.providers.odds_api import OddsEvent, team_tricode
from app.services.athlete_mapping_errors import AthleteMappingPersistenceError
from app.services.athlete_resolver import (
    AthleteResolver,
    normalize_athlete_name,
    split_name_suffix,
)
from app.services.event_mapping_errors import EventMappingPersistenceError

logger = logging.getLogger(__name__)

PROVIDER = "oddsapi"

#: Names of one player (suffix aside) that the Odds API and the NBA catalog
#: spell differently and no rule can join, each group read in both directions
#: because either side may hold the short form.  The pairs are the ones seen in
#: the 2025-26 closing-line backfill (#437).  A name the vendor posts differently
#: for another player is added here, with a test from the real pair; ``python
#: scripts/backfill_closing_lines.py --re-resolve --dry-run`` lists the names that
#: still miss.  A group widens a posted name to a player of the game's own two
#: rosters only.
NAME_VARIANTS: tuple[tuple[str, ...], ...] = (
    ("Nic Claxton", "Nicolas Claxton"),
    ("Herb Jones", "Herbert Jones"),
    ("Ron Holland", "Ronald Holland"),
    ("Vince Williams", "Vincent Williams"),
    ("Bub Carrington", "Carlton Carrington"),
    ("Moe Wagner", "Moritz Wagner"),
)

_VARIANT_BASE = {
    split_name_suffix(spelling)[0]: split_name_suffix(group[0])[0]
    for group in NAME_VARIANTS
    for spelling in group
}


def athlete_provider_id(name: str) -> str:
    """The synthetic provider athlete ID for a player name."""

    return f"name:{normalize_athlete_name(name)}"


def _identity_key(name: str) -> tuple[str, str]:
    """``(name without its suffix, suffix)``, nicknames folded to one spelling."""

    base, suffix = split_name_suffix(name)
    return _VARIANT_BASE.get(base, base), suffix


class _RosterSpellings:
    """How the two rosters of one game spell their players' names."""

    def __init__(self, display_names: Iterable[str]) -> None:
        spellings = [name for name in display_names if name]
        self.exact = {normalize_athlete_name(name) for name in spellings}
        self._identities = [(name, _identity_key(name)) for name in spellings]

    def fits(self, posted: str) -> tuple[str, ...]:
        """The roster spellings ``posted`` could be, when it has no exact match.

        A roster player fits when his name is the same once nicknames are folded
        and suffixes are set aside, and the suffixes do not name two different
        men (``Jr.`` and ``Sr.``).  Active or not, every such player counts, so
        a name two players could carry is never settled for either of them.
        """

        base, suffix = _identity_key(posted)
        return tuple(
            name
            for name, (rostered_base, rostered_suffix) in self._identities
            if rostered_base == base
            and (not suffix or not rostered_suffix or suffix == rostered_suffix)
        )


class _RosterCatalog:
    """The athlete catalog restricted to the rows of the given teams."""

    def __init__(self, catalog: Any, team_ids: Collection[int]) -> None:
        self._catalog = catalog
        self._team_ids = frozenset(int(team_id) for team_id in team_ids)

    def get_catalog(self, season: str, *, active_only: bool = False):
        return [
            row
            for row in self._catalog.get_catalog(season, active_only=active_only)
            if row.get("team_id") is not None and int(row["team_id"]) in self._team_ids
        ]


@dataclass(frozen=True, slots=True)
class ResolvedAthlete:
    """A name resolved to one rostered athlete of the game."""

    player_id: int
    team_id: int
    display_name: str


class RosterNameResolver:
    """Resolve player names within one game's two rosters."""

    def __init__(
        self,
        *,
        catalog: Any,
        mapping_repository: Any | None,
        season: str,
    ) -> None:
        self._catalog = catalog
        self._repository = mapping_repository
        self._season = season

    def resolve(
        self, names: Iterable[str], team_ids: Collection[int]
    ) -> dict[str, int]:
        """``{name as posted: canonical player id}``; unresolved names absent."""

        return {
            name: athlete.player_id
            for name, athlete in self.resolve_athletes(names, team_ids).items()
        }

    def resolve_athletes(
        self, names: Iterable[str], team_ids: Collection[int]
    ) -> dict[str, ResolvedAthlete]:
        """``{name as posted: athlete}``; unresolved names absent.

        Carries the roster row's team and display name as well, which the DFS
        projection pipeline needs to name a player the way every other
        provider's resolved markets do.
        """

        roster = _RosterCatalog(self._catalog, team_ids)
        roster_rows = {
            int(row["player_id"]): row for row in roster.get_catalog(self._season)
        }
        distinct = sorted({name.strip() for name in names if name and name.strip()})
        spellings = _RosterSpellings(
            str(row.get("display_name") or "") for row in roster_rows.values()
        )
        # A posted name with no exact match on the rosters is looked up under
        # the roster spellings it fits: one, and that player is the candidate;
        # two, and the resolver calls the name ambiguous.  The evidence stays
        # what was posted, so the identity, a rejection or approval of it, and
        # the recorded provider name are the posted name's, as for any name that
        # matched all along.
        catalog_names = {
            key: tuple(dict.fromkeys(normalize_athlete_name(fit) for fit in fits))
            for name in distinct
            if (key := normalize_athlete_name(name)) not in spellings.exact
            and (fits := spellings.fits(name))
        }
        resolver = AthleteResolver(
            roster, mapping_repository=self._repository, catalog_names=catalog_names
        )
        identities = [(PROVIDER, athlete_provider_id(name)) for name in distinct]
        board = resolver.for_board(self._season, identities)
        resolved: dict[str, ResolvedAthlete] = {}
        for name in distinct:
            evidence = AthleteEvidence(provider_id=athlete_provider_id(name), name=name)
            resolution = board.resolve(PROVIDER, evidence, self._season)
            player_id: int | None
            if self._repository is None:
                player_id = (
                    resolution.canonical_player_id
                    if resolution.is_auto_qualifying
                    else None
                )
            else:
                try:
                    outcome = self._repository.record_resolution(resolution).board_outcome(
                        resolution
                    )
                except AthleteMappingPersistenceError:
                    logger.warning("Could not record one Odds API athlete mapping")
                    continue
                player_id = outcome.canonical_player_id
            # An approved or overridden mapping names a canonical player
            # without consulting the roster-scoped catalog, so membership is
            # checked here: every resolved player is on one of the two rosters.
            row = None if player_id is None else roster_rows.get(int(player_id))
            if row is None or row.get("team_id") is None:
                continue
            resolved[name] = ResolvedAthlete(
                player_id=int(player_id),
                team_id=int(row["team_id"]),
                display_name=str(row.get("display_name") or name),
            )
        return resolved


@dataclass(frozen=True, slots=True)
class MappedEvent:
    event: OddsEvent
    game_id: str
    team_ids: tuple[int, int]
    scheduled_at: datetime | None


class OddsEventMapper:
    """Map Odds API events to canonical games by teams and start time."""

    def __init__(
        self, *, resolver: Any, mapping_repository: Any | None, season: str
    ) -> None:
        self._resolver = resolver
        self._repository = mapping_repository
        self._season = season

    def map(
        self, events: Sequence[OddsEvent]
    ) -> tuple[list[MappedEvent], list[OddsEvent]]:
        """Resolve every event; return ``(mapped, unresolved)``.

        Re-resolved every cycle, so a postponement that moves
        ``commence_time`` is picked up.  An event that does not resolve to
        exactly one scheduled game is skipped, never guessed.
        """

        mapped: list[MappedEvent] = []
        unresolved: list[OddsEvent] = []
        for event in events:
            evidence = EventEvidence(
                provider_id=event.event_id,
                label=f"{event.away_team} @ {event.home_team}",
                starts_at=event.commence_time,
                home_team=TeamEvidence(
                    name=event.home_team, abbreviation=team_tricode(event.home_team)
                ),
                away_team=TeamEvidence(
                    name=event.away_team, abbreviation=team_tricode(event.away_team)
                ),
            )
            resolution = self._resolver.resolve(PROVIDER, evidence, self._season)
            canonical = None
            if self._repository is None:
                canonical = resolution.canonical_event if resolution.is_auto_qualifying else None
                game_id = None if canonical is None else canonical.nba_game_id
            else:
                try:
                    outcome = self._repository.record_resolution(resolution).board_outcome(
                        resolution
                    )
                except EventMappingPersistenceError:
                    logger.warning("Could not record one Odds API event mapping")
                    unresolved.append(event)
                    continue
                game_id = outcome.canonical_event_id
                canonical = resolution.canonical_event
            if (
                game_id is None
                or canonical is None
                or game_id != canonical.nba_game_id
            ):
                unresolved.append(event)
                continue
            if canonical.home_team_id is None or canonical.away_team_id is None:
                unresolved.append(event)
                continue
            mapped.append(
                MappedEvent(
                    event=event,
                    game_id=game_id,
                    team_ids=(int(canonical.home_team_id), int(canonical.away_team_id)),
                    scheduled_at=canonical.scheduled_at,
                )
            )
        return mapped, unresolved


__all__ = [
    "MappedEvent",
    "NAME_VARIANTS",
    "OddsEventMapper",
    "PROVIDER",
    "ResolvedAthlete",
    "RosterNameResolver",
    "athlete_provider_id",
]
