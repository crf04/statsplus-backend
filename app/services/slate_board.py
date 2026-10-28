"""Every posted line for one market across a Slate Date (crf04/statsplus#373).

The board reads the date's games, one Player Pool for all of them, their
stored injury report in one statement, their latest sportsbook rows in one
statement and the catalog rows of players the books quote outside the pool in
one statement, all on one repeatable-read connection, so a correction
committed between two of those reads never mixes into a response.  It composes
no Matchup: a row carries the Matchup player row's ``lines``, ``line_prices``
and ``book_lines`` for one market, and a player ruled Out stays in with his
status.  A started game's pool is its closing set, as every pool read serves it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.domain.nba_events import is_final_event, is_started_event
from app.domain.utc import assume_utc, parse_utc_iso
from app.errors import InvalidInputError, ProviderUnavailableError
from app.providers.odds_api import BOOK_ALLOWLIST
from app.providers.registry import dfs_provider_names
from app.services.injury_listing import slate_events
from app.services.player_pool import PlayerPool
from app.services.publication_snapshot_calls import call_with_read_scope
from app.services.request_reads import request_read_scope
from app.services.research_season import schedule_season
from app.services.sportsbook_quotes import build_market_entry

#: The report statuses a board row names; ``Available`` and no entry are ``null``.
BOARD_INJURY_STATUSES = frozenset({"Out", "Doubtful", "Questionable", "Probable"})
#: Every name ``sources`` accepts: the DFS and prediction-market providers and the served books.
BOARD_SOURCES = frozenset(dfs_provider_names()) | BOOK_ALLOWLIST


def _iso_z(value: Any) -> str:
    moment = assume_utc(value) if isinstance(value, datetime) else parse_utc_iso(str(value))
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label(event: Mapping[str, Any]) -> str:
    return f"{event['away_team_tricode']} @ {event['home_team_tricode']}"


@dataclass(frozen=True, slots=True)
class _Posting:
    """One player's lines in one market, from every source posting it."""

    game_id: str
    player_id: int
    name: str
    team: str
    injury_status: str | None
    lines: Mapping[str, float]
    line_prices: Mapping[str, Mapping[str, int | None]]
    book_rows: tuple[Any, ...]

    def row(self) -> dict[str, Any]:
        entry = build_market_entry(self.book_rows)
        return {
            "player_id": self.player_id,
            "name": self.name,
            "team": self.team,
            "injury_status": self.injury_status,
            "lines": dict(self.lines),
            "line_prices": {provider: dict(prices) for provider, prices in self.line_prices.items()},
            "book_lines": (
                None
                if entry is None
                else {
                    "as_of": entry["as_of"],
                    "closing": entry["closing"],
                    "books": [
                        {key: book[key] for key in ("book", "line", "over", "under", "updated_at")}
                        for book in entry["books"]
                    ],
                }
            ),
        }


class SlateBoardService:
    """Serve ``GET /api/games/board`` from stored reads only."""

    def __init__(
        self,
        *,
        event_catalog: Any | None,
        player_pool: Any | None,
        injuries: Any | None,
        sportsbook_reader: Any | None,
        statistic_catalog: Any,
        settings: Any,
        athlete_catalog: Any | None = None,
        engine: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.event_catalog = event_catalog
        self.player_pool = player_pool
        # Names and teams for the players the books quote outside the pool.
        self.athlete_catalog = athlete_catalog
        # Stored-only: ``get_stored_injuries_many`` never refreshes a report.
        self.injuries = injuries
        self.sportsbook_reader = sportsbook_reader
        self.settings = settings
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(UTC))
        self.markets = tuple(
            dict.fromkeys(
                statistic.market_category
                for statistic in statistic_catalog.statistics
                if statistic.market_category is not None
            )
        )

    def get_board(
        self,
        *,
        slate_date: date,
        season: str | None = None,
        market: str | None = None,
        game_id: str | None = None,
        team: str | None = None,
        sources: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """The index of the date's markets without ``market``, else that market's rows per game."""

        if market is not None and market not in self.markets:
            raise InvalidInputError(
                "The board market must be a catalog market such as PTS.",
                public_details={"parameters": ["market"]},
            )
        if self.event_catalog is None:
            raise ProviderUnavailableError("The NBA schedule is not available. Please try again later.")
        season = season or schedule_season(self.settings, self.event_catalog, slate_date)
        with request_read_scope(self._engine, repeatable_read=True) as (connection, _session):
            events = sorted(
                (
                    event
                    for event in slate_events(
                        self.event_catalog, self.settings, slate_date, season, connection=connection
                    )
                    if (game_id is None or str(event["nba_game_id"]) == game_id)
                    and (team is None or team in (event["away_team_tricode"], event["home_team_tricode"]))
                ),
                key=lambda event: (_iso_z(event["scheduled_at"]), _label(event)),
            )
            game_ids = [str(event["nba_game_id"]) for event in events]
            pool = self._pool(season, game_ids, connection)
            statuses = self._injury_statuses(events, season, pool, connection)
            quotes = [
                row
                for row in (
                    []
                    if self.sportsbook_reader is None or not game_ids
                    else self.sportsbook_reader.latest_quotes(game_ids, connection=connection)
                )
                if sources is None or row.book in sources
            ]
            postings = _Postings(
                events,
                pool,
                statuses,
                quotes,
                team=team,
                sources=sources,
                athletes=lambda player_ids: self._athletes(season, player_ids, connection),
            )
        now = assume_utc(self._clock())
        board: dict[str, Any] = {
            "date": slate_date.isoformat(),
            "season": season,
            "games": [self._game(event, now) for event in events],
            "sources": self._sources(pool, quotes, sources),
        }
        if market is None:
            board["markets"] = [
                {
                    "market": name,
                    "players": len(found),
                    "sources": sorted({provider for posting in found for provider in posting.lines})
                    + sorted({row.book for posting in found for row in posting.book_rows}),
                }
                for name in self.markets
                if (found := postings.of(name))
            ]
            return board
        rows_by_game: dict[str, list[dict[str, Any]]] = {game: [] for game in game_ids}
        found = sorted(
            postings.of(market), key=lambda posting: (posting.team, posting.name, posting.player_id)
        )
        for posting in found:
            rows_by_game[posting.game_id].append(posting.row())
        for game in board["games"]:
            game["players"] = rows_by_game[game["game_id"]]
        board["players_total"] = len(found)
        return board

    def _pool(self, season: str, game_ids: Sequence[str], connection: Any) -> PlayerPool:
        if self.player_pool is None:
            return PlayerPool((), {}, PlayerPool.unavailable_freshness())
        return call_with_read_scope(
            self.player_pool.get_pool, season=season, game_ids=game_ids, connection=connection
        )

    def _athletes(self, season: str, player_ids: Sequence[int], connection: Any) -> list[Mapping[str, Any]]:
        if self.athlete_catalog is None or not player_ids:
            return []
        return call_with_read_scope(
            self.athlete_catalog.get_catalog, season, player_ids=player_ids, connection=connection
        )

    def _injury_statuses(
        self, events: Sequence[Mapping[str, Any]], season: str, pool: PlayerPool, connection: Any
    ) -> dict[int, str]:
        """Each listed player's status on the stored report, the first entry naming him."""

        if self.injuries is None or not events:
            return {}
        teams = {
            str(event["nba_game_id"]): {int(event["away_team_id"]), int(event["home_team_id"])}
            for event in events
        }
        results = call_with_read_scope(
            self.injuries.get_stored_injuries_many,
            events=events,
            season=season,
            pool_players_by_game={
                game: tuple(player for player in pool.players if player.team_id in ids)
                for game, ids in teams.items()
            },
            connection=connection,
        )
        statuses: dict[int, str] = {}
        for result in results.values():
            for team in result.block["teams"]:
                for entry in team["entries"]:
                    if entry.get("canonical_player_id") is not None:
                        statuses.setdefault(int(entry["canonical_player_id"]), str(entry["canonical_status"]))
        return statuses

    @staticmethod
    def _game(event: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        if is_final_event(event):
            status = "final"
        elif is_started_event(event, now=now):
            status = "live"
        else:
            status = "scheduled"
        return {
            "game_id": str(event["nba_game_id"]),
            "label": _label(event),
            "tip": _iso_z(event["scheduled_at"]),
            "status": status,
        }

    @staticmethod
    def _sources(
        pool: PlayerPool, quotes: Iterable[Any], sources: frozenset[str] | None
    ) -> list[dict[str, Any]]:
        """Each provider's pool freshness, then each quoting book's newest update."""

        listed = [
            {
                "source": provider,
                "kind": "provider",
                "status": freshness["status"],
                "retrieved_at": (
                    None if freshness.get("retrieved_at") is None else _iso_z(freshness["retrieved_at"])
                ),
            }
            for provider, freshness in sorted(pool.freshness.get("providers", {}).items())
            if sources is None or provider in sources
        ]
        newest: dict[str, datetime] = {}
        for row in quotes:
            updated = assume_utc(row.market_last_update or row.observed_at)
            newest[row.book] = max(newest.get(row.book, updated), updated)
        listed.extend(
            {"source": book, "kind": "sportsbook", "status": "fresh", "retrieved_at": _iso_z(updated)}
            for book, updated in sorted(newest.items())
        )
        return listed


class _Postings:
    """The slate's postings, one market at a time, after the board's filters.

    A player a book quotes in a game whose pool has no row for him is posted
    with book columns only, named from the athlete catalog.  One the catalog
    lacks, or holds without a team, is left out: without his team the board can
    neither apply ``team`` nor place him in the team, name, player id order.
    """

    def __init__(
        self,
        events: Sequence[Mapping[str, Any]],
        pool: PlayerPool,
        statuses: Mapping[int, str],
        quotes: Iterable[Any],
        *,
        team: str | None,
        sources: frozenset[str] | None,
        athletes: Callable[[Sequence[int]], Iterable[Mapping[str, Any]]],
    ) -> None:
        self._team = team
        self._sources = sources
        self._statuses = statuses
        # A team plays once on a Slate Date, so its id names the player's game.
        games = {
            int(event[f"{side}_team_id"]): (str(event["nba_game_id"]), str(event[f"{side}_team_tricode"]))
            for event in events
            for side in ("away", "home")
        }
        self._games = {team_id: game for team_id, game in games.items() if team is None or game[1] == team}
        self._players = [player for player in pool.players if player.team_id in self._games]
        self._books: dict[tuple[str, int, str], list[Any]] = {}
        for row in quotes:
            key = (str(row.canonical_game_id), int(row.canonical_player_id), str(row.market_category))
            self._books.setdefault(key, []).append(row)
        pooled = {
            (games[player.team_id][0], int(player.canonical_player_id))
            for player in pool.players
            if player.team_id in games
        }
        self._off_pool = sorted({(game, player) for game, player, _ in self._books} - pooled)
        self._athletes = {
            int(row["player_id"]): row for row in athletes(sorted({player for _, player in self._off_pool}))
        }

    def of(self, market: str) -> list[_Posting]:
        found = []
        for player in self._players:
            game_id, team = self._games[player.team_id]
            lines = {
                provider: markets[market]
                for provider, markets in sorted(player.lines.items())
                if market in markets and (self._sources is None or provider in self._sources)
            }
            books = tuple(self._books.get((game_id, int(player.canonical_player_id), market), ()))
            if not lines and not books:
                continue
            found.append(
                self._posting(
                    game_id,
                    int(player.canonical_player_id),
                    player.name,
                    team,
                    lines=lines,
                    line_prices={
                        provider: {
                            "over": player.line_prices[provider][market]["over"],
                            "under": player.line_prices[provider][market]["under"],
                        }
                        for provider in lines
                        if market in player.line_prices.get(provider, {})
                    },
                    books=books,
                )
            )
        for game_id, player_id in self._off_pool:
            books = tuple(self._books.get((game_id, player_id, market), ()))
            athlete = self._athletes.get(player_id, {})
            team = athlete.get("team_abbreviation")
            if not books or team is None or (self._team is not None and team != self._team):
                continue
            found.append(
                self._posting(
                    game_id,
                    player_id,
                    str(athlete["display_name"]),
                    str(team),
                    lines={},
                    line_prices={},
                    books=books,
                )
            )
        return found

    def _posting(
        self,
        game_id: str,
        player_id: int,
        name: str,
        team: str,
        *,
        lines: Mapping[str, float],
        line_prices: Mapping[str, Mapping[str, int | None]],
        books: tuple[Any, ...],
    ) -> _Posting:
        status = self._statuses.get(player_id)
        return _Posting(
            game_id=game_id,
            player_id=player_id,
            name=name,
            team=team,
            injury_status=status if status in BOARD_INJURY_STATUSES else None,
            lines=lines,
            line_prices=line_prices,
            book_rows=books,
        )


__all__ = ["BOARD_SOURCES", "SlateBoardService"]
