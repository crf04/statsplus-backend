"""``closing_line`` on ``/api/games/game_logs`` rows (#433).

The route is called as the web app and the MCP call it, over the real
``GameService`` and the real closing-line table; only the box-score source is
a stub.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert, text

from app.migrations import run_migrations
from app.models.closing_lines import ClosingLine
from app.services.closing_lines import ClosingLineReader
from app.services.game_service import GameService

WHEN = datetime(2025, 11, 5, 0, 24, 41, tzinfo=UTC)
LEBRON = 2544


def _frame():
    columns = [
        "GAME_DATE",
        "GAME_ID",
        "PLAYER_ID",
        "PLAYER_NAME",
        "TEAM_ID",
        "TEAM_ABBREVIATION",
        "MATCHUP",
        "MIN",
        "PTS",
        "REB",
        "AST",
        "PRA",
        "PA",
        "PR",
        "RA",
        "STKS",
        "FD_PTS",
        "FGM",
        "FGA",
        "FG_PCT",
        "FG2M",
        "FG2A",
        "FG3M",
        "FG3A",
        "FTM",
        "FTA",
        "OREB",
        "DREB",
        "TOV",
        "STL",
        "BLK",
        "PF",
    ]

    def row(day, game_id, points):
        return [
            day,
            game_id,
            LEBRON,
            "LeBron James",
            1610612747,
            "LAL",
            "LAL @ DEN",
            35,
            points,
            8,
            7,
            points + 15,
            points + 7,
            points + 8,
            15,
            2,
            44.2,
            9,
            16,
            56.3,
            6,
            9,
            3,
            7,
            4,
            5,
            1,
            7,
            2,
            1,
            1,
            2,
        ]

    return pd.DataFrame(
        [
            row("2025-11-04", "0022500101", 27),
            row("2025-11-06", "0022500104", 22),
            row("2025-11-08", "0022500109", 31),
        ],
        columns=columns,
    )


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'logs.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(ClosingLine.__table__),
            [
                dict(
                    canonical_game_id=game,
                    canonical_player_id=player,
                    market_category=market,
                    book=book,
                    line=line,
                    snapshot_at=WHEN,
                    odds_event_id="e",
                    bought_at=WHEN,
                )
                for game, player, market, book, line in (
                    ("0022500101", LEBRON, "PTS", "draftkings", 24.5),
                    ("0022500101", LEBRON, "PTS", "fanduel", 25.5),
                    ("0022500104", LEBRON, "PTS", "betmgm", 23.5),
                    ("0022500109", LEBRON, "REB", "draftkings", 7.5),
                )
            ],
        )
    return engine


def _serve(dependencies, engine, *, reader):
    class Logs:
        def get_player_logs(self, player_id, season, **kwargs):
            assert player_id == LEBRON
            return _frame()

    class Catalog:
        def get_catalog(self, season, *, active_only=False):
            return [
                dict(
                    player_id=LEBRON,
                    display_name="LeBron James",
                    is_active_for_season=True,
                    team_id=1610612747,
                )
            ]

    dependencies.game_service = GameService(
        engine,
        redis_client=SimpleNamespace(),
        settings=dependencies.settings,
        athlete_catalog=Catalog(),
        game_logs_source=Logs(),
        closing_lines=reader,
    )


def test_each_row_carries_its_games_closing_points_line_or_null(dependencies, client, engine):
    _serve(dependencies, engine, reader=ClosingLineReader(engine))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert response.status_code == 200
    rows = response.get_json()["game_logs"]
    assert [(row["GAME_DATE"], row["PTS"], row["closing_line"]) for row in rows] == [
        # The better book wins when two stored a line; this is the contract's
        # `{value, book}` object, the book being the Odds API bookmaker key.
        ("2025-11-04", 27, {"value": 24.5, "book": "draftkings"}),
        ("2025-11-06", 22, {"value": 23.5, "book": "betmgm"}),
        # Only a rebounds line is stored for this game: no points line.
        ("2025-11-08", 31, None),
    ]


def test_closing_line_is_null_on_every_row_when_no_lines_are_stored(dependencies, client, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    run_migrations(engine)
    _serve(dependencies, engine, reader=ClosingLineReader(engine))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert [row["closing_line"] for row in response.get_json()["game_logs"]] == [None, None, None]


def test_a_deployment_without_the_reader_still_serves_rows_with_a_null_line(dependencies, client, engine):
    _serve(dependencies, engine, reader=None)

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert response.status_code == 200
    assert [row["closing_line"] for row in response.get_json()["game_logs"]] == [None, None, None]


def test_a_failing_line_store_does_not_fail_the_game_logs(dependencies, client, engine):
    _serve(dependencies, engine, reader=ClosingLineReader(engine))
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE closing_lines"))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert response.status_code == 200
    rows = response.get_json()["game_logs"]
    assert [row["PTS"] for row in rows] == [27, 22, 31]
    assert [row["closing_line"] for row in rows] == [None, None, None]


def test_the_line_follows_the_rows_a_filter_keeps(dependencies, client, engine):
    _serve(dependencies, engine, reader=ClosingLineReader(engine))

    response = client.get(
        "/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26" "&self_filters[PTS]=25,40"
    )

    rows = response.get_json()["game_logs"]
    assert [(row["PTS"], row["closing_line"]) for row in rows] == [
        (27, {"value": 24.5, "book": "draftkings"}),
        (31, None),
    ]


# -- closing_lines: every market, every stored season (crf04/statsplus#429) --

NEW_GAMES = ("0022600101", "0022600104", "0022600109")


def _frame_2026_27():
    frame = _frame()
    frame["GAME_ID"] = list(NEW_GAMES)
    frame["GAME_DATE"] = ["2026-11-01", "2026-11-03", "2026-11-05"]
    return frame


def _serve_2026_27(dependencies, engine, *, reader):
    _serve(dependencies, engine, reader=reader)
    dependencies.game_service.game_logs_source.get_player_logs = lambda player_id, season, **kwargs: (
        _frame_2026_27()
    )


def _poll(store, game_id, observed, quotes, *, is_closing):
    from app.services.sportsbook_quotes import ResolvedQuote

    store.record_poll(
        odds_event_id=f"event-{game_id}",
        game_id=game_id,
        started_at=observed,
        completed_at=observed,
        cost=5,
        remaining=1000,
        is_closing=is_closing,
        quotes=[
            ResolvedQuote(LEBRON, market, book, line, -110, -110, observed) for market, book, line in quotes
        ],
    )


@pytest.fixture
def live_engine(engine):
    """2026-27 quotes as the live odds collector stores them."""

    from app.services.sportsbook_quotes import SportsbookQuoteStore

    store = SportsbookQuoteStore(engine)
    first, second, third = NEW_GAMES
    tip = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    # An hourly poll at other lines, then the closing poll: only the close counts.
    _poll(
        store,
        first,
        tip - timedelta(hours=3),
        [("PTS", "draftkings", 22.5), ("REB", "fanduel", 6.5)],
        is_closing=False,
    )
    _poll(
        store,
        first,
        tip - timedelta(minutes=7),
        [
            ("PTS", "fanduel", 25.5),
            ("PTS", "draftkings", 24.5),
            ("REB", "betmgm", 8.5),
            ("REB", "fanduel", 7.5),
            ("AST", "betrivers", 6.5),
            ("3PM", "fanatics", 1.5),
            ("PRA", "williamhill_us", 39.5),
        ],
        is_closing=True,
    )
    # No closing poll: the last pregame quote is not a closing line.
    _poll(store, second, tip + timedelta(days=2, hours=-1), [("PTS", "draftkings", 23.5)], is_closing=False)
    # Postponed and closed again: the newer close wins, and REB, which it no
    # longer carried, is not read from the older one.
    _poll(
        store,
        third,
        tip + timedelta(days=3),
        [("PTS", "draftkings", 20.5), ("REB", "draftkings", 6.5)],
        is_closing=True,
    )
    _poll(store, third, tip + timedelta(days=4), [("PTS", "fanduel", 29.5)], is_closing=True)
    return engine


def test_a_2026_27_row_carries_each_markets_closing_quote_from_the_first_book(
    dependencies, client, live_engine
):
    _serve_2026_27(dependencies, live_engine, reader=ClosingLineReader(live_engine))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2026-27")

    assert response.status_code == 200
    rows = response.get_json()["game_logs"]
    assert [(row["GAME_DATE"], row["closing_lines"], row["closing_line"]) for row in rows] == [
        (
            "2026-11-01",
            {
                "PTS": {"value": 24.5, "book": "draftkings"},
                "REB": {"value": 7.5, "book": "fanduel"},
                "AST": {"value": 6.5, "book": "betrivers"},
                "3PM": {"value": 1.5, "book": "fanatics"},
                "PRA": {"value": 39.5, "book": "williamhill_us"},
            },
            {"value": 24.5, "book": "draftkings"},
        ),
        ("2026-11-03", {}, None),
        ("2026-11-05", {"PTS": {"value": 29.5, "book": "fanduel"}}, {"value": 29.5, "book": "fanduel"}),
    ]


def test_a_2025_26_row_carries_only_the_backfilled_lines_and_its_points_line_is_unchanged(
    dependencies, client, engine
):
    from app.services.sportsbook_quotes import SportsbookQuoteStore

    # A closing quote under a 2025-26 game id is not a 2025-26 source.
    _poll(
        SportsbookQuoteStore(engine),
        "0022500104",
        datetime(2025, 11, 6, 0, 0, tzinfo=UTC),
        [("PTS", "draftkings", 30.5), ("AST", "draftkings", 9.5)],
        is_closing=True,
    )
    _serve(dependencies, engine, reader=ClosingLineReader(engine))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    rows = response.get_json()["game_logs"]
    assert [(row["closing_lines"], row["closing_line"]) for row in rows] == [
        ({"PTS": {"value": 24.5, "book": "draftkings"}}, {"value": 24.5, "book": "draftkings"}),
        ({"PTS": {"value": 23.5, "book": "betmgm"}}, {"value": 23.5, "book": "betmgm"}),
        ({"REB": {"value": 7.5, "book": "draftkings"}}, None),
    ]


def test_a_failing_quote_store_reads_as_no_lines_and_does_not_fail_the_game_logs(
    dependencies, client, live_engine
):
    _serve_2026_27(dependencies, live_engine, reader=ClosingLineReader(live_engine))
    with live_engine.begin() as connection:
        connection.execute(text("DROP TABLE sportsbook_prop_quotes"))

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2026-27")

    assert response.status_code == 200
    rows = response.get_json()["game_logs"]
    assert [row["PTS"] for row in rows] == [27, 22, 31]
    assert [(row["closing_lines"], row["closing_line"]) for row in rows] == [({}, None)] * 3


def test_without_the_reader_every_row_carries_empty_closing_lines(dependencies, client, engine):
    _serve(dependencies, engine, reader=None)

    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2025-26")

    assert [row["closing_lines"] for row in response.get_json()["game_logs"]] == [{}, {}, {}]


def test_a_filter_keeps_each_rows_own_closing_lines(dependencies, client, live_engine):
    _serve_2026_27(dependencies, live_engine, reader=ClosingLineReader(live_engine))

    response = client.get(
        "/api/games/game_logs?player_name=LeBron+James&season_filter=2026-27&self_filters[PTS]=25,40"
    )

    rows = response.get_json()["game_logs"]
    assert [(row["PTS"], row["closing_lines"].get("PTS")) for row in rows] == [
        (27, {"value": 24.5, "book": "draftkings"}),
        (31, {"value": 29.5, "book": "fanduel"}),
    ]


# -- a re-closed game reads its current close only (review of #703) ----------

FIRST_TIP = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
#: The postponed game's new tip, two days later.
NEW_TIP = FIRST_TIP + timedelta(days=2)


def _catalog_tip(engine, game_id, tip):
    from app.models.event_catalog import EventCatalogEntry

    with engine.begin() as connection:
        connection.execute(
            insert(EventCatalogEntry.__table__).values(
                nba_game_id=game_id,
                season="2026-27",
                home_team_id=1610612743,
                home_team_name="Nuggets",
                home_team_tricode="DEN",
                away_team_id=1610612747,
                away_team_name="Lakers",
                away_team_tricode="LAL",
                scheduled_at=tip,
                status_text="Final",
                status_code=3,
                classification="Regular Season",
                first_seen_at=FIRST_TIP - timedelta(days=30),
                last_seen_at=tip,
            )
        )


def _close(engine, game_id, tip, quotes, *, empty=False):
    """A closing poll taken 7 minutes before ``tip``, as the live collector takes it."""

    from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteStore

    store = SportsbookQuoteStore(engine)
    observed = tip - timedelta(minutes=7)
    if empty:
        store.record_unstored_poll(
            odds_event_id=f"event-{game_id}",
            game_id=game_id,
            started_at=observed,
            completed_at=observed,
            outcome="empty",
            is_closing=True,
            cost=0,
            commence_time=tip,
        )
        return
    store.record_poll(
        odds_event_id=f"event-{game_id}",
        game_id=game_id,
        started_at=observed,
        completed_at=observed,
        cost=5,
        remaining=1000,
        is_closing=True,
        quotes=[
            ResolvedQuote(player, market, book, line, -110, -110, observed)
            for player, market, book, line in quotes
        ],
        commence_time=tip,
    )


def _first_row(client):
    response = client.get("/api/games/game_logs?player_name=LeBron+James&season_filter=2026-27")
    assert response.status_code == 200
    row = response.get_json()["game_logs"][0]
    return row["closing_lines"], row["closing_line"]


@pytest.fixture
def reclosed(dependencies, engine):
    """NEW_GAMES[0] was closed at its first tip with LeBron's lines, then postponed."""

    game = NEW_GAMES[0]
    _catalog_tip(engine, game, NEW_TIP)
    _close(engine, game, FIRST_TIP, [(LEBRON, "PTS", "draftkings", 24.5), (LEBRON, "REB", "fanduel", 7.5)])
    _serve_2026_27(dependencies, engine, reader=ClosingLineReader(engine))
    return engine


def test_a_player_the_new_close_omits_has_no_closing_lines(reclosed, client):
    _close(reclosed, NEW_GAMES[0], NEW_TIP, [(203999, "PTS", "draftkings", 29.5)])

    assert _first_row(client) == ({}, None)


def test_an_empty_close_for_the_new_tip_does_not_bring_back_the_old_tips_lines(reclosed, client):
    _close(reclosed, NEW_GAMES[0], NEW_TIP, [], empty=True)

    assert _first_row(client) == ({}, None)


@pytest.fixture
def closed(dependencies, engine):
    """NEW_GAMES[0] closed at its (unmoved) tip with LeBron's lines."""

    game = NEW_GAMES[0]
    _catalog_tip(engine, game, FIRST_TIP)
    _close(engine, game, FIRST_TIP, [(LEBRON, "PTS", "draftkings", 24.5)])
    _serve_2026_27(dependencies, engine, reader=ClosingLineReader(engine))
    return engine


def _later_poll_for_the_same_tip(engine, *, outcome, quote_count=0):
    """A closing poll after the quoted one that stored no sportsbook quote."""

    from app.services.sportsbook_quotes import SportsbookQuoteStore

    observed = FIRST_TIP - timedelta(minutes=3)
    SportsbookQuoteStore(engine).record_unstored_poll(
        odds_event_id=f"event-{NEW_GAMES[0]}",
        game_id=NEW_GAMES[0],
        started_at=observed - timedelta(minutes=3),
        completed_at=observed,
        outcome=outcome,
        is_closing=True,
        cost=0,
        quote_count=quote_count,
        commence_time=FIRST_TIP,
    )


def _covered(engine):
    from app.services.closing_lines import games_with_closing_quotes

    return games_with_closing_quotes(engine, [NEW_GAMES[0]])


def test_a_later_empty_poll_for_the_same_tip_keeps_the_quoted_close(closed, client):
    _later_poll_for_the_same_tip(closed, outcome="empty")

    assert _first_row(client) == (
        {"PTS": {"value": 24.5, "book": "draftkings"}},
        {"value": 24.5, "book": "draftkings"},
    )
    assert _covered(closed) == {NEW_GAMES[0]}  # not a gap


def test_a_later_close_that_stored_no_quote_for_the_same_tip_keeps_the_quoted_close(closed, client):
    _close(closed, NEW_GAMES[0], FIRST_TIP + timedelta(minutes=4), [])

    assert _first_row(client)[0] == {"PTS": {"value": 24.5, "book": "draftkings"}}
    assert _covered(closed) == {NEW_GAMES[0]}


def test_a_newer_dfs_closing_poll_does_not_hide_the_sportsbook_close(closed, client):
    # The DFS feed records a complete closing poll whose quotes go to the
    # projection archive, not to sportsbook_prop_quotes.
    _later_poll_for_the_same_tip(closed, outcome="complete", quote_count=30)

    assert _first_row(client) == (
        {"PTS": {"value": 24.5, "book": "draftkings"}},
        {"value": 24.5, "book": "draftkings"},
    )
    assert _covered(closed) == {NEW_GAMES[0]}


def test_a_close_for_an_earlier_tip_is_not_the_games_close(reclosed, client):
    # The postponed game's new closing poll was missed: the old close does not stand in.
    assert _first_row(client) == ({}, None)


def test_the_new_close_is_read_when_it_carries_the_player(reclosed, client):
    _close(reclosed, NEW_GAMES[0], NEW_TIP, [(LEBRON, "PTS", "fanduel", 26.5)])

    assert _first_row(client) == (
        {"PTS": {"value": 26.5, "book": "fanduel"}},
        {"value": 26.5, "book": "fanduel"},
    )
