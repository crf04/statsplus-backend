"""A player's posted lines for his team's game on today's slate.

Shared by the injury scenarios (one market at a time) and ``game_logs``
(``posted_line``, crf04/statsplus#238).  Reads stored data only: the event
catalog for the slate and the stored player pool for the lines.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from app.domain.slate_time import slate_date_for_instant
from app.services.injury_listing import slate_events
from app.services.research_season import schedule_season

logger = logging.getLogger(__name__)

#: Posted-line providers in the order a line is taken from; any others follow
#: alphabetically.
LINE_PROVIDER_PREFERENCE = ("prizepicks",)


def posted_line(lines: Mapping[str, Mapping[str, float]], market: str) -> tuple[float, str] | None:
    """One market's ``(value, provider)``: PrizePicks first, then alphabetical."""

    providers = [p for p in LINE_PROVIDER_PREFERENCE if p in lines]
    providers += sorted(p for p in lines if p not in providers)
    for provider in providers:
        if market in lines[provider]:
            return float(lines[provider][market]), provider
    return None


class StoredSchedule:
    """Read-only schedule access for the line day's games.

    The event catalog service owns a provider-backed refresh, so injecting it
    into the game-log service would leave that adapter reachable from the
    request path; this exposes only the two stored reads it needs.
    """

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def count_events(self, season: str) -> int:
        return self._repository.count_events(season)

    def get_events_between(self, season: str, starts_at: datetime, ends_at: datetime) -> list[dict[str, Any]]:
        return self._repository.list_events_between(season, starts_at, ends_at)

    def get_events_by_ids(self, season: str, game_ids: Any) -> list[dict[str, Any]]:
        return self._repository.list_events_by_ids(season, game_ids)


def _matchup(event: Mapping[str, Any], team_id: int | None) -> str:
    """The game from the player's side: ``LAL vs GSW`` home, ``LAL @ GSW`` away."""

    home, away = event["home_team_tricode"], event["away_team_tricode"]
    if team_id is not None and int(event["home_team_id"]) == int(team_id):
        return f"{home} vs {away}"
    if team_id is not None and int(event["away_team_id"]) == int(team_id):
        return f"{away} @ {home}"
    return f"{away} @ {home}"


class PostedLinesReader:
    """The line day's posted lines for one player's game (default: today).

    The posted pool line while a pool snapshot for that game is still served;
    otherwise the game's stored closing line (points only, its book as the
    provider).
    """

    def __init__(
        self,
        *,
        event_catalog: Any,
        player_pool: Any,
        settings: Any,
        closing_lines: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.event_catalog = event_catalog
        self.player_pool = player_pool
        self.settings = settings
        self.closing_lines = closing_lines
        self._clock = clock

    def for_player(
        self,
        player_id: int,
        line_date: date | None = None,
        team_by_game: Mapping[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """The ``posted_line`` response object, or ``None``; never raises."""

        try:
            return self._for_player(int(player_id), line_date, team_by_game or {})
        except Exception:
            logger.warning("posted line lookup failed", exc_info=True)
            return None

    def _for_player(
        self, player_id: int, line_date: date | None, team_by_game: Mapping[str, int]
    ) -> dict[str, Any] | None:
        if self.event_catalog is None:
            return None
        slate_date = line_date or slate_date_for_instant(self._clock())
        events = slate_events(self.event_catalog, self.settings, slate_date)
        season = schedule_season(self.settings, self.event_catalog, slate_date)
        if self.player_pool is not None:
            for event in events:
                found = self._from_pool(player_id, season, event, slate_date)
                if found is not None:
                    return found
        if self.closing_lines is None or not events:
            return None
        by_id = {str(event["nba_game_id"]): event for event in events}
        stored = self.closing_lines.for_player(player_id, list(by_id))
        for game_id, event in by_id.items():
            closing = stored.get(game_id)
            if closing is None:
                continue
            return {
                "game_id": game_id,
                "game_date": slate_date.isoformat(),
                "matchup": _matchup(event, team_by_game.get(game_id)),
                "lines": {"PTS": {"value": closing.value, "provider": closing.book}},
            }
        return None

    def _from_pool(
        self, player_id: int, season: str, event: Mapping[str, Any], slate_date: date
    ) -> dict[str, Any] | None:
        game_id = str(event["nba_game_id"])
        teams = (int(event["home_team_id"]), int(event["away_team_id"]))
        # The pool the reader serves (live, or a started game's frozen closing
        # set), then a started game's retained pre-tip archive.  Provider
        # freezes commit separately, so a provider the frozen set lacks is
        # supplemented from the archive; a frozen provider keeps its own lines.
        reads = (
            lambda: self.player_pool.get_pool_for_game(season=season, game_id=game_id),
            lambda: getattr(self.player_pool, "get_retained_pool_for_game", lambda **_: None)(
                season=season, game_id=game_id
            ),
        )
        by_provider: dict[str, Mapping[str, float]] = {}
        team_id = None
        for read in reads:
            pool = read()
            player = (
                None
                if pool is None
                else next(
                    (p for p in pool.players if int(p.canonical_player_id) == player_id),
                    None,
                )
            )
            if player is None or int(player.team_id) not in teams:
                continue
            team_id = player.team_id
            for provider, provider_lines in player.lines.items():
                if provider_lines:
                    by_provider.setdefault(provider, provider_lines)
        lines = {}
        for market in sorted({m for by_market in by_provider.values() for m in by_market}):
            found = posted_line(by_provider, market)
            if found is not None:
                lines[market] = {"value": found[0], "provider": found[1]}
        if not lines:
            return None
        return {
            "game_id": game_id,
            "game_date": slate_date.isoformat(),
            "matchup": _matchup(event, team_id),
            "lines": lines,
        }
