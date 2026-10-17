"""Turn governed mapping outcomes into canonical identity on market evidence.

The board resolves athletes and events to canonical rows and reports what
governance reached as outcomes.  Both the request-time player pool and the
projection collector read those outcomes through this one module, so the check
that a canonical row matches its outcome lives in one place.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from app.providers.dfs import (
    AthleteEvidence,
    EventEvidence,
    PlayerProjectionMarket,
    ProviderSnapshot,
    TeamEvidence,
)


@dataclass(frozen=True, slots=True)
class MappedEvent:
    game_id: str
    team_ids: frozenset[int]


def evidence_key(provider: str, evidence: AthleteEvidence | EventEvidence | None) -> tuple[str, str] | None:
    provider_id = None if evidence is None else evidence.provider_id
    if not isinstance(provider_id, str) or not provider_id.strip():
        return None
    return provider, provider_id.strip()


def athlete_mappings(
    outcomes: Iterable[Any],
) -> dict[tuple[str, str], tuple[int, int, str]]:
    """(provider, provider id) -> (canonical player id, team id, display name).

    Only an outcome whose governed player matches the canonical athlete it
    resolved to, and whose athlete names a team, is a mapping.
    """

    result = {}
    for outcome in outcomes:
        player_id = outcome.canonical_player_id
        resolution = outcome.resolution
        canonical = resolution.canonical_athlete
        key = evidence_key(resolution.provider, resolution.provider_evidence)
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


def event_mappings(outcomes: Iterable[Any]) -> dict[tuple[str, str], MappedEvent]:
    """(provider, provider id) -> the governed canonical game and its teams."""

    result = {}
    for outcome in outcomes:
        resolution = outcome.resolution
        game_id = outcome.canonical_event_id
        canonical = resolution.canonical_event
        key = evidence_key(resolution.provider, resolution.provider_evidence)
        if (
            key is not None
            and game_id is not None
            and canonical is not None
            and canonical.nba_game_id == str(game_id)
        ):
            team_ids = frozenset(
                team_id for team_id in (canonical.home_team_id, canonical.away_team_id) if team_id is not None
            )
            result[key] = MappedEvent(str(game_id), team_ids)
    return result


def apply_mapping_outcomes(
    snapshot: ProviderSnapshot,
    *,
    athletes: dict[tuple[str, str], tuple[int, int, str]],
    events: dict[tuple[str, str], MappedEvent],
) -> ProviderSnapshot:
    """The snapshot with governed canonical identity set on its markets.

    A market whose athlete or event has no governed mapping keeps the evidence
    it arrived with, so it stays unresolved.  A mapped athlete whose team is not
    one of the mapped game's teams gets no game.
    """

    return replace(
        snapshot,
        markets=tuple(
            _apply_to_market(snapshot.provider, market, athletes, events) for market in snapshot.markets
        ),
    )


def _apply_to_market(
    provider: str,
    market: PlayerProjectionMarket,
    athletes: dict[tuple[str, str], tuple[int, int, str]],
    events: dict[tuple[str, str], MappedEvent],
) -> PlayerProjectionMarket:
    changes: dict[str, Any] = {}
    athlete_key = evidence_key(provider, market.athlete)
    mapped_athlete = None if athlete_key is None else athletes.get(athlete_key)
    if mapped_athlete is not None and market.athlete is not None:
        player_id, team_id, name = mapped_athlete
        team = market.athlete.team or TeamEvidence()
        changes["athlete"] = replace(
            market.athlete,
            canonical_id=player_id,
            name=name,
            team=replace(team, canonical_id=team_id),
        )
    event_key = evidence_key(provider, market.event)
    mapped_event = None if event_key is None else events.get(event_key)
    if (
        mapped_event is not None
        and mapped_athlete is not None
        and mapped_athlete[1] not in mapped_event.team_ids
    ):
        # The player's team is not playing in the mapped game (a trade, or a
        # provider that omits team evidence), so one of the two mappings is
        # wrong.  Withhold the game: the line stays archived with its event
        # unresolved instead of becoming a targetable offering for a game the
        # player is not in.
        mapped_event = None
    if mapped_event is not None and market.event is not None:
        changes["event"] = replace(market.event, canonical_id=mapped_event.game_id)
    return replace(market, **changes) if changes else market
