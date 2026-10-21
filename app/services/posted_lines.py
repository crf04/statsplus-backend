"""A player's posted lines for his team's game on today's slate, else his next game's.

Shared by the injury scenarios (one market at a time) and ``game_logs``
(``posted_line``, crf04/statsplus#238).  Reads stored data only: the event
catalog for the slate and the stored player pool for the lines.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.domain.nba_events import is_postponed_event
from app.domain.slate_time import slate_date_for_instant
from app.services.injury_listing import slate_events
from app.services.research_season import schedule_season

logger = logging.getLogger(__name__)

#: Posted-line providers in the order a line is taken from; any others follow
#: alphabetically. The game-log majority line breaks a tie in that order, then
#: by books in served order.
LINE_PROVIDER_PREFERENCE = ("prizepicks",)

#: Days after today searched for his next game's line when no line day is
#: named (books post an opener's lines days ahead).
LOOKAHEAD_DAYS = 7


def _provider_order(lines: Mapping[str, Mapping[str, float]]) -> list[str]:
    providers = [p for p in LINE_PROVIDER_PREFERENCE if p in lines]
    return providers + sorted(p for p in lines if p not in providers)


def posted_line(lines: Mapping[str, Mapping[str, float]], market: str) -> tuple[float, str] | None:
    """One market's ``(value, provider)``: PrizePicks first, then alphabetical."""

    for provider in _provider_order(lines):
        if market in lines[provider]:
            return float(lines[provider][market]), provider
    return None


def majority_line(
    lines: Mapping[str, Mapping[str, float]],
    market: str,
    book_lines: Mapping[str, Mapping[str, Any]],
) -> tuple[float, str] | None:
    """One market's ``(value, source)``: the line most providers and books
    post, named by the first source posting it; a tie goes to the tied line
    posted first."""

    sources = [(float(lines[p][market]), p) for p in _provider_order(lines) if market in lines[p]]
    sources += [(float(book["line"]), book["book"]) for book in book_lines.get(market, {}).get("books", [])]
    counts = Counter(line for line, _ in sources)
    if not counts:
        return None
    most = max(counts.values())
    return next(source for source in sources if counts[source[0]] == most)


def serialize_lines(lines: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, float]]:
    """A player's per-provider lines as served: providers and markets sorted."""

    return {provider: dict(sorted(markets.items())) for provider, markets in sorted(lines.items())}


def serialize_line_prices(
    line_prices: Mapping[str, Mapping[str, Mapping[str, int | None]]],
) -> dict[str, dict[str, dict[str, int | None]]]:
    """A player's per-provider Over/Under prices as served (Matchup ``line_prices``)."""

    return {
        provider: {
            market: {"over": prices["over"], "under": prices["under"]}
            for market, prices in sorted(markets.items())
        }
        for provider, markets in sorted(line_prices.items())
    }


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
    """The line day's posted lines for one player's game (default: today, then
    his next game's within ``LOOKAHEAD_DAYS`` when he has none today).

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
        athlete_catalog: Any | None = None,
        sportsbook_reader: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.event_catalog = event_catalog
        self.player_pool = player_pool
        self.settings = settings
        self.closing_lines = closing_lines
        # His current team for the schedule's season (a summer trade moves it).
        self.athlete_catalog = athlete_catalog
        # Read-only sportsbook quotes, ``None`` when the book store is off.
        self.sportsbook_reader = sportsbook_reader
        self._clock = clock

    def for_player(
        self,
        player_id: int,
        line_date: date | None = None,
        team_by_game: Mapping[str, int] | None = None,
        *,
        team_id: int | None = None,
    ) -> dict[str, Any] | None:
        """The ``posted_line`` response object, or ``None``; never raises.

        ``team_id`` is his team in his latest logged game, used when the
        Athlete Catalog has no team for him in the schedule's season.
        """

        try:
            return self._for_player(int(player_id), line_date, team_by_game or {}, team_id)
        except Exception:
            logger.warning("posted line lookup failed", exc_info=True)
            return None

    def _for_player(
        self,
        player_id: int,
        line_date: date | None,
        team_by_game: Mapping[str, int],
        team_id: int | None,
    ) -> dict[str, Any] | None:
        if self.event_catalog is None:
            return None
        if line_date is not None:
            season = schedule_season(self.settings, self.event_catalog, line_date)
            events = slate_events(self.event_catalog, self.settings, line_date, season)
            return self._for_day(player_id, line_date, season, events, team_by_game)
        today = slate_date_for_instant(self._clock())
        teams: dict[str, int | None] = {}
        for offset in range(LOOKAHEAD_DAYS + 1):
            day = today + timedelta(days=offset)
            season = schedule_season(self.settings, self.event_catalog, day)
            # A postponed game is not his next game.
            events = [
                event
                for event in slate_events(self.event_catalog, self.settings, day, season)
                if not is_postponed_event(event)
            ]
            if offset == 0:
                # Today reads every game, as before, and needs no team.
                found = self._for_day(player_id, day, season, events, team_by_game)
                if found is not None:
                    return found
            # His team in this day's schedule season (a summer trade moves it).
            if season not in teams:
                teams[season] = self._catalog_team(player_id, season) or team_id
            team = teams[season]
            his = [
                event
                for event in events
                if team is not None and int(team) in (int(event["home_team_id"]), int(event["away_team_id"]))
            ]
            if offset > 0:
                # A later day reads only his team's game: one pool read.
                found = self._for_day(player_id, day, season, events if team is None else his, team_by_game)
                if found is not None:
                    return found
            if his:
                # His next game has no line yet; a later game's is not his next.
                return None
        return None

    def _catalog_team(self, player_id: int, season: str) -> int | None:
        """His team in the Athlete Catalog for ``season``, else ``None``."""

        if self.athlete_catalog is None:
            return None
        try:
            rows = self.athlete_catalog.get_catalog(season, active_only=False)
        except Exception:
            logger.warning("current team lookup failed", exc_info=True)
            return None
        for row in rows:
            if int(row["player_id"]) == player_id and row.get("team_id") is not None:
                return int(row["team_id"])
        return None

    def _for_day(
        self,
        player_id: int,
        slate_date: date,
        season: str,
        events: list[Mapping[str, Any]],
        team_by_game: Mapping[str, int],
    ) -> dict[str, Any] | None:
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
                "provider_lines": {},
                "line_prices": {},
                "book_lines": self._book_lines(player_id, game_id),
            }
        return None

    def _from_pool(
        self, player_id: int, season: str, event: Mapping[str, Any], slate_date: date
    ) -> dict[str, Any] | None:
        game_id = str(event["nba_game_id"])
        teams = (int(event["home_team_id"]), int(event["away_team_id"]))
        by_provider: dict[str, Mapping[str, float]] = {}
        prices_by_provider: dict[str, Mapping[str, Mapping[str, int | None]]] = {}
        team_id = None
        # The pool the reader serves (live, or a started game's frozen closing
        # set), then a started game's retained pre-tip archive.  Provider
        # freezes commit separately, so a provider the frozen set lacks is
        # supplemented from the archive; a provider that already gave him
        # lines keeps them and is not replayed.
        reads = (
            lambda: self.player_pool.get_pool_for_game(season=season, game_id=game_id),
            lambda: getattr(self.player_pool, "get_retained_pool_for_game", lambda **_: None)(
                season=season, game_id=game_id, player_id=player_id, skip_providers=tuple(by_provider)
            ),
        )
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
                if provider_lines and provider not in by_provider:
                    by_provider[provider] = provider_lines
                    # A provider's prices come from the pool its lines did.
                    if player.line_prices.get(provider):
                        prices_by_provider[provider] = player.line_prices[provider]
        if not by_provider:
            return None
        book_lines = self._book_lines(player_id, game_id)
        lines = {}
        for market in sorted({m for by_market in by_provider.values() for m in by_market}):
            found = majority_line(by_provider, market, book_lines)
            if found is not None:
                lines[market] = {"value": found[0], "provider": found[1]}
        return {
            "game_id": game_id,
            "game_date": slate_date.isoformat(),
            "matchup": _matchup(event, team_id),
            "lines": lines,
            "provider_lines": serialize_lines(by_provider),
            "line_prices": serialize_line_prices(prices_by_provider),
            "book_lines": book_lines,
        }

    def _book_lines(self, player_id: int, game_id: str) -> dict[str, Any]:
        """His sportsbook lines for the game; ``{}`` when off, empty or failing."""

        if self.sportsbook_reader is None:
            return {}
        try:
            return dict(self.sportsbook_reader.book_lines(game_id).get(player_id, {}))
        except Exception:
            logger.exception("Sportsbook book_lines read failed for game %s", game_id)
            return {}
