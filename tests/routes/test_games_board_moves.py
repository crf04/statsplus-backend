"""GET /api/games/board?moves=true: how each sportsbook line moved on the slate's ET date (crf04/statsplus#491)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.providers.odds_api import BOOK_ALLOWLIST
from app.providers.registry import dfs_provider_names
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.event_catalog_service import EventCatalogService
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
)
from app.services.slate_board import SlateBoardService
from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteReader, SportsbookQuoteStore
from app.services.statistic_catalog import StatisticCatalog
from tests.routes.test_games_board import (
    BOS,
    BROWN,
    BRUNSON,
    GSW,
    HAUSER,
    LAL,
    LATER,
    LATER_TIP,
    NOW,
    NYK,
    QUERY,
    SEASON,
    STARTED,
    STARTED_TIP,
    TATUM,
    WHITE,
    _event,
    _get,
    _market,
    _NoProvider,
    _snapshot,
)

#: 22:00 ET the day before: the books' night poll opens the slate's lines.
P0 = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
#: 10:00 ET: every line re-posted unchanged except Brown's, which DraftKings pulls.
P1 = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
P2 = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
P3 = datetime(2026, 1, 15, 23, 57, tzinfo=UTC)


def _quote(player, book, line, over, under, at):
    return ResolvedQuote(player[0], "PTS", book, line, over, under, at)


def seed_moves(engine, dependencies):
    catalog = StatisticCatalog.load_default()
    settings = dependencies.settings.model_copy(
        update={"nba": dependencies.settings.nba.model_copy(update={"current_season": SEASON})}
    )
    schedule = EventCatalogService(
        engine, nba_stats_provider=_NoProvider(), settings=settings, clock=lambda: NOW
    )
    schedule.repository.publish(
        SEASON,
        pd.DataFrame([_event(STARTED, GSW, LAL, STARTED_TIP), _event(LATER, BOS, NYK, LATER_TIP)]),
        NOW - timedelta(days=2),
    )
    # Dabble posts Tatum too: a provider cell, which has no line history here.
    ProjectionArchive(engine, catalog).ingest_complete_snapshot(
        _snapshot("dabble", (_market(catalog, "dabble", TATUM, "points", "27.5", (-122, -104)),), NOW),
        query=QUERY,
        accepted_at=NOW,
    )
    polls = {
        P0: [
            _quote(TATUM, "draftkings", 27.5, -110, -110, P0),
            _quote(TATUM, "fanduel", 28.5, -105, -115, P0),
            _quote(WHITE, "draftkings", 15.5, -110, -110, P0),
            _quote(HAUSER, "draftkings", 9.5, -115, -105, P0),
            _quote(BROWN, "draftkings", 23.5, -115, -105, P0),
        ],
        P1: [
            _quote(TATUM, "draftkings", 27.5, -110, -110, P0),
            _quote(TATUM, "fanduel", 28.5, -105, -115, P0),
            _quote(WHITE, "draftkings", 15.5, -110, -110, P0),
            # Hauser's price moves before DraftKings pulls him.
            _quote(HAUSER, "draftkings", 9.5, -120, 100, P1),
        ],
        P2: [
            _quote(TATUM, "draftkings", 28.5, -115, -105, P2),
            _quote(TATUM, "fanduel", 28.5, -105, -115, P0),
            _quote(WHITE, "draftkings", 15.5, -125, 105, P2),
            _quote(BROWN, "draftkings", 23.5, -115, -105, P2),
            _quote(BRUNSON, "fanduel", 26.5, -120, -106, P2),
        ],
    }
    polls[P3] = [_quote(TATUM, "draftkings", 27.5, -110, -110, P3), *polls[P2][1:]]
    store = SportsbookQuoteStore(engine)
    for at, quotes in polls.items():
        store.record_poll(
            odds_event_id=f"odds-{LATER}",
            game_id=LATER,
            started_at=at,
            completed_at=at,
            cost=1,
            remaining=100,
            is_closing=False,
            quotes=quotes,
        )
    with engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__),
            [
                {
                    "season": SEASON,
                    "player_id": player_id,
                    "display_name": name,
                    "roster_status": "Active",
                    "is_active": True,
                    "is_active_for_season": True,
                    "team_id": team.canonical_id,
                    "team_name": team.abbreviation,
                    "team_abbreviation": team.abbreviation,
                    "published_at": NOW - timedelta(days=1),
                }
                for player_id, name, team, _game in (TATUM, BRUNSON, BROWN, WHITE, HAUSER)
            ],
        )
    dependencies.slate_board_service = SlateBoardService(
        event_catalog=schedule,
        player_pool=LatestProjectionPlayerPoolReader(
            engine,
            tuple(
                ProjectionArchiveReadScope(provider=provider, query=QUERY)
                for provider in dfs_provider_names()
            ),
            required_providers=("dabble",),
            clock=lambda: NOW,
            event_reader=schedule,
        ),
        injuries=None,
        sportsbook_reader=SportsbookQuoteReader(engine),
        statistic_catalog=catalog,
        settings=settings,
        athlete_catalog=AthleteCatalogReader(engine),
        engine=engine,
        clock=lambda: NOW,
    )
    return engine


@pytest.fixture
def moves_board(tmp_path, dependencies):
    engine = create_engine(f"sqlite:///{tmp_path / 'moves.sqlite3'}")
    run_migrations(engine)
    return seed_moves(engine, dependencies)


def _rows(body):
    return {row["name"]: row for game in body["games"] for row in game["players"]}


def _value(line, over, under, **at):
    return {"line": line, "over": over, "under": under, **at}


TATUM_MOVES = {
    "draftkings": {
        "open": _value(27.5, -110, -110, seen_at="2026-01-15T03:00:00Z"),
        "previous": _value(28.5, -115, -105, changed_at="2026-01-15T23:57:00Z"),
        "unchanged": False,
    },
    "fanduel": {
        "open": _value(28.5, -105, -115, seen_at="2026-01-15T03:00:00Z"),
        "previous": None,
        "unchanged": True,
    },
}
PULLED = {
    "draftkings": [
        {
            "game_id": LATER,
            "player_id": HAUSER[0],
            "name": "Sam Hauser",
            "team": "BOS",
            **_value(9.5, -120, 100, seen_at="2026-01-15T15:00:00Z"),
        }
    ]
}


def test_a_book_line_that_moved_and_moved_back_shows_its_open_and_previous_value(moves_board, client):
    status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    assert status == 200
    tatum = _rows(body)["Jayson Tatum"]
    # Dabble's cell has no line history, so it has no entry.
    assert tatum["lines"] == {"dabble": 27.5}
    assert tatum["moves"] == TATUM_MOVES


def test_the_board_names_the_sources_with_line_history(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")
    assert body["moves_sources"] == sorted(BOOK_ALLOWLIST)

    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true&sources=dabble")
    assert body["moves_sources"] == []
    assert _rows(body)["Jayson Tatum"]["moves"] == {}


def test_a_price_only_change_is_a_move(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    assert _rows(body)["Derrick White"]["moves"] == {
        "draftkings": {
            "open": _value(15.5, -110, -110, seen_at="2026-01-15T03:00:00Z"),
            "previous": _value(15.5, -110, -110, changed_at="2026-01-15T18:00:00Z"),
            "unchanged": False,
        }
    }


def test_a_same_value_repost_is_not_a_move(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    rows = _rows(body)
    # DraftKings pulled Brown at 10:00 ET and re-posted the same line at 13:00 ET.
    assert rows["Jaylen Brown"]["moves"] == {
        "draftkings": {
            "open": _value(23.5, -115, -105, seen_at="2026-01-15T03:00:00Z"),
            "previous": None,
            "unchanged": True,
        }
    }
    # First posted at 13:00 ET: its opening value is its first.
    assert rows["Jalen Brunson"]["moves"] == {
        "fanduel": {
            "open": _value(26.5, -120, -106, seen_at="2026-01-15T18:00:00Z"),
            "previous": None,
            "unchanged": True,
        }
    }


def test_each_book_lists_the_lines_it_pulled_today(moves_board, client):
    status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    assert status == 200
    assert body["pulled"] == PULLED
    assert "Sam Hauser" not in _rows(body)


def test_sources_and_team_narrow_moves_and_pulled(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true&sources=fanduel")
    assert _rows(body)["Jayson Tatum"]["moves"] == {"fanduel": TATUM_MOVES["fanduel"]}
    assert (body["moves_sources"], body["pulled"]) == (["fanduel"], {})

    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true&team=NYK")
    assert body["pulled"] == {}


def test_without_moves_the_answer_is_unchanged(moves_board, client):
    plain_status, plain = _get(client, "date=2026-01-15&market=PTS")
    false_status, explicit_false = _get(client, "date=2026-01-15&market=PTS&moves=false")
    _status, moved = _get(client, "date=2026-01-15&market=PTS&moves=true")

    assert (plain_status, false_status) == (200, 200)
    assert explicit_false == plain
    assert {"pulled", "moves_sources"}.isdisjoint(plain)
    assert all("moves" not in row for game in plain["games"] for row in game["players"])
    del moved["pulled"], moved["moves_sources"]
    for game in moved["games"]:
        for row in game["players"]:
            row.pop("moves")
    assert moved == plain


def test_book_history_is_served_for_any_date(moves_board, client):
    # Book history is never pruned, so an older date is not refused.
    status, body = _get(client, "date=2026-01-13&market=PTS&moves=true")

    assert (status, body["games"], body["pulled"]) == (200, [], {})


@pytest.mark.parametrize(
    "query",
    [
        "date=2026-01-15&market=PTS&moves=yes",
        "date=2026-01-15&market=PTS&moves=true&moves=true",
        # The index has no cells to carry moves.
        "date=2026-01-15&moves=true",
    ],
)
def test_an_unservable_moves_request_is_refused(moves_board, client, query):
    status, body = _get(client, query)

    assert status == 400
    assert body["error"]["details"]["parameters"] == ["moves"]
