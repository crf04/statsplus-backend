"""Every posted line for one market across a Slate Date (crf04/statsplus#373).

The board reads the date's games, one Player Pool for all of them, their
stored injury report in one statement, their latest sportsbook rows in one
statement and the catalog rows of players the books quote outside the pool in
one statement, all on one repeatable-read connection, so a correction
committed between two of those reads never mixes into a response.  It composes
no Matchup: a row carries the Matchup player row's ``lines``, ``line_prices``
and ``book_lines`` for one market, and a player ruled Out stays in with his
status.  A started game's pool is its closing set, as every pool read serves it.
With ``moves`` it also reads the books' line history for the market
(``app.services.line_moves``) in one more statement on the same connection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.domain.nba_events import is_final_event, is_started_event
from app.domain.slate_time import slate_day_bounds_utc
from app.domain.utc import assume_utc, parse_utc_iso
from app.errors import InvalidInputError, ProviderUnavailableError
from app.providers.odds_api import BOOK_ALLOWLIST
from app.providers.registry import dfs_provider_names
from app.services.injury_listing import slate_events
from app.services.line_moves import book_histories, moves_of, pulled_value
from app.services.player_pool import PlayerPool
from app.services.posted_lines import line_labels
from app.services.publication_snapshot_calls import call_with_read_scope
from app.services.request_reads import request_read_scope
from app.services.research_season import schedule_season
from app.services.sportsbook_quotes import build_market_entry, is_stale_read, quote_updated_at

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
    line_labels: Mapping[str, str]
    book_rows: tuple[Any, ...]

    def row(self, *, now: datetime, started: bool) -> dict[str, Any]:
        entry = build_market_entry(self.book_rows, now=now, started=started)
        return {
            "player_id": self.player_id,
            "name": self.name,
            "team": self.team,
            "injury_status": self.injury_status,
            "lines": dict(self.lines),
            "line_prices": {provider: dict(prices) for provider, prices in self.line_prices.items()},
            "line_labels": dict(self.line_labels),
            "book_lines": (
                None
                if entry is None
                else {
                    "as_of": entry["as_of"],
                    "closing": entry["closing"],
                    "books": [
                        {key: book[key] for key in ("book", "line", "over", "under", "updated_at", "stale")}
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
        moves: bool = False,
    ) -> dict[str, Any]:
        """The index of the date's markets without ``market``, else that market's rows per game.

        ``moves`` adds each sportsbook cell's moves on the date and each book's pulled lines.
        """

        if market is not None and market not in self.markets:
            raise InvalidInputError(
                "The board market must be a catalog market such as PTS.",
                public_details={"parameters": ["market"]},
            )
        if moves and market is None:
            raise InvalidInputError(
                "Line moves need a market.",
                public_details={"parameters": ["moves"]},
            )
        # Without a reader the board shows no book lines, so it serves no book history either.
        history_books = sorted(
            book
            for book in BOOK_ALLOWLIST
            if self.sportsbook_reader is not None and (sources is None or book in sources)
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
            if moves:
                day_start = slate_day_bounds_utc(slate_date)[0]
                histories = book_histories(
                    connection,
                    game_ids=game_ids,
                    market=market,
                    books=history_books,
                    posted=(
                        (str(row.canonical_game_id), int(row.canonical_player_id), str(row.book))
                        for row in quotes
                        if row.market_category == market
                    ),
                )
                pulled = self._pulled(histories, events, pool, team, day_start, season, connection)
        now = assume_utc(self._clock())
        games = [self._game(event, now) for event in events]
        started = frozenset(game["game_id"] for game in games if game["status"] != "scheduled")
        board: dict[str, Any] = {
            "date": slate_date.isoformat(),
            "season": season,
            "games": games,
            "sources": self._sources(pool, quotes, sources, now=now, started=started),
        }
        if market is None:
            board["markets"] = [
                {
                    "market": name,
                    "players": len(found),
                    "book_only_unnamed": len(unnamed),
                    "sources": sorted({provider for posting in found for provider in posting.lines})
                    + sorted(
                        {row.book for posting in found for row in posting.book_rows}
                        | {row.book for books in unnamed for row in books}
                    ),
                }
                for name in self.markets
                for found, unnamed in (postings.of(name),)
                if found or unnamed
            ]
            return board
        rows_by_game: dict[str, list[dict[str, Any]]] = {game: [] for game in game_ids}
        found, unnamed = postings.of(market)
        found.sort(key=lambda posting: (posting.team, posting.name, posting.player_id))
        for posting in found:
            row = posting.row(now=now, started=posting.game_id in started)
            if moves:
                books = (
                    () if row["book_lines"] is None else (book["book"] for book in row["book_lines"]["books"])
                )
                row["moves"] = {
                    book: moves_of(histories.get((book, posting.game_id, posting.player_id)), day_start)
                    for book in sorted(books)
                }
            rows_by_game[posting.game_id].append(row)
        for game in board["games"]:
            game["players"] = rows_by_game[game["game_id"]]
        board["players_total"] = len(found)
        board["omitted"] = {"book_only_unnamed": len(unnamed)}
        if moves:
            board["moves_sources"] = history_books
            board["pulled"] = pulled
        return board

    def _pulled(
        self,
        histories: Mapping[tuple[str, str, int], Any],
        events: Sequence[Mapping[str, Any]],
        pool: PlayerPool,
        team: str | None,
        day_start: datetime,
        season: str,
        connection: Any,
    ) -> dict[str, list[dict[str, Any]]]:
        """Each book's lines posted on the date and no longer posted, named and in board order."""

        candidates = {
            key: value
            for key, history in histories.items()
            for value in (pulled_value(history, day_start),)
            if value is not None
        }
        tricodes = {
            int(event[f"{side}_team_id"]): str(event[f"{side}_team_tricode"])
            for event in events
            for side in ("away", "home")
        }
        names: dict[int, tuple[str, str | None]] = {
            int(player.canonical_player_id): (player.name, tricodes.get(player.team_id))
            for player in pool.players
        }
        unnamed = sorted({player_id for _book, _game, player_id in candidates} - set(names))
        for athlete in self._athletes(season, unnamed, connection):
            names[int(athlete["player_id"])] = (
                str(athlete["display_name"]),
                athlete.get("team_abbreviation"),
            )
        order = {str(event["nba_game_id"]): index for index, event in enumerate(events)}
        pulled: dict[str, list[dict[str, Any]]] = {}
        for (book, game_id, player_id), value in candidates.items():
            name, player_team = names.get(player_id, (None, None))
            if name is None or player_team is None or (team is not None and player_team != team):
                continue
            pulled.setdefault(book, []).append(
                {"game_id": game_id, "player_id": player_id, "name": name, "team": player_team, **value}
            )
        for entries in pulled.values():
            entries.sort(
                key=lambda entry: (order[entry["game_id"]], entry["team"], entry["name"], entry["player_id"])
            )
        return dict(sorted(pulled.items()))

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
        pool: PlayerPool,
        quotes: Iterable[Any],
        sources: frozenset[str] | None,
        *,
        now: datetime,
        started: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Each provider's pool freshness, then each quoting book's newest update.

        A book is judged on its quotes for the games that have not started:
        ``stale-served`` when the newest of them is stale, else ``fresh``.  A
        book whose every quoted game has started is ``closing``.
        """

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
        pending: dict[str, datetime] = {}
        for row in quotes:
            updated = quote_updated_at(row)
            newest[row.book] = max(newest.get(row.book, updated), updated)
            if str(row.canonical_game_id) not in started:
                pending[row.book] = max(pending.get(row.book, updated), updated)
        for book in sorted(newest):
            if book in pending:
                updated = pending[book]
                stale = is_stale_read(updated, now=now, started=False)
                status = "stale-served" if stale else "fresh"
            else:
                updated, status = newest[book], "closing"
            listed.append(
                {"source": book, "kind": "sportsbook", "status": status, "retrieved_at": _iso_z(updated)}
            )
        return listed


class _Postings:
    """The slate's postings, one market at a time, after the board's filters.

    A player a book quotes in a game whose pool has no row for him is posted
    with book columns only, named from the athlete catalog.  One the catalog
    lacks, or holds without a team, is counted instead of posted: without his
    team the board can neither apply ``team`` nor place him in the team, name,
    player id order.  A ``team`` filter keeps him in the count, since he is
    quoted in that team's game.
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

    def of(self, market: str) -> tuple[list[_Posting], list[tuple[Any, ...]]]:
        """The market's postings, and the book rows of each quoted player the catalog cannot name."""

        found = []
        unnamed = []
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
            labels = line_labels(player.lines, player.line_prices, player.priced_other_lines)
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
                    line_labels={
                        provider: labels[provider][market]
                        for provider in lines
                        if market in labels.get(provider, {})
                    },
                    books=books,
                )
            )
        for game_id, player_id in self._off_pool:
            books = tuple(self._books.get((game_id, player_id, market), ()))
            athlete = self._athletes.get(player_id, {})
            team = athlete.get("team_abbreviation")
            if not books:
                continue
            if team is None:
                unnamed.append(books)
                continue
            if self._team is not None and team != self._team:
                continue
            found.append(
                self._posting(
                    game_id,
                    player_id,
                    str(athlete["display_name"]),
                    str(team),
                    lines={},
                    line_prices={},
                    line_labels={},
                    books=books,
                )
            )
        return found, unnamed

    def _posting(
        self,
        game_id: str,
        player_id: int,
        name: str,
        team: str,
        *,
        lines: Mapping[str, float],
        line_prices: Mapping[str, Mapping[str, int | None]],
        line_labels: Mapping[str, str],
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
            line_labels=line_labels,
            book_rows=books,
        )


__all__ = ["BOARD_SOURCES", "SlateBoardService"]
