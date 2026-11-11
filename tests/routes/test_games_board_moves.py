"""GET /api/games/board?moves=true: how each line moved on the slate's ET date (crf04/statsplus#491)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.providers.dfs import (
    CoverageEvidence,
    MarketStatus,
    PriceKind,
    PriceScope,
    Selection,
    SelectionDirection,
    SnapshotStatus,
)
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
    CURRY,
    GSW,
    HAUSER,
    LAL,
    LATER,
    LATER_TIP,
    LEBRON,
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

PRITCHARD = (1630202, "Payton Pritchard", BOS, LATER)
#: Two days before the slate: older than the history the moves read.
EARLY = datetime(2026, 1, 13, 20, 0, tzinfo=UTC)
#: 18:00 ET the day before: the slate's lines open.
T0 = datetime(2026, 1, 14, 23, 0, tzinfo=UTC)
#: 20:00 ET the day before: Dabble pulls Hauser before the slate's day begins.
T0B = datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
#: 10:00 ET: Pritchard is first posted; every other cell is re-posted unchanged.
T1 = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
#: An unchanged poll: it confirms T1's lines and writes no observation.
T1B = datetime(2026, 1, 15, 15, 5, tzinfo=UTC)
T2 = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
#: PrizePicks' first Partial snapshot, inside its live window at NOW.
T2B = datetime(2026, 1, 15, 23, 50, tzinfo=UTC)
#: A PrizePicks Partial that omits White: his line is carried, not seen.
T2C = datetime(2026, 1, 15, 23, 52, tzinfo=UTC)
#: After GSW @ LAL tipped: its closing set ignores this poll.
T3 = datetime(2026, 1, 15, 23, 55, tzinfo=UTC)
#: The books' night poll, 22:00 ET the day before.
P0 = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
P1 = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
P2 = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
P3 = datetime(2026, 1, 15, 23, 57, tzinfo=UTC)


def _line_only(catalog, player, threshold):
    """A Dabble line whose decimals are not near even: it states only Dabble's entry payout."""

    return replace(
        _market(catalog, "dabble", player, "points", threshold, (None, None)),
        selections=tuple(
            Selection(
                selection_id=direction.value,
                direction=direction,
                price_kind=PriceKind.MULTIPLIER,
                price_value=3,
                price_scope=PriceScope.ENTRY,
            )
            for direction in (SelectionDirection.HIGHER, SelectionDirection.LOWER)
        ),
    )


def _dabble(catalog, at, lines):
    return _snapshot(
        "dabble",
        tuple(
            _line_only(catalog, player, threshold)
            if prices is None
            else _market(catalog, "dabble", player, "points", threshold, prices)
            for player, threshold, prices in lines
        ),
        at,
    )


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
    archive = ProjectionArchive(engine, catalog)
    opening = [
        (TATUM, "27.5", (-122, -104)),
        (BROWN, "23.5", None),
        (WHITE, "15.5", (-110, -110)),
        (BRUNSON, "26.5", (-120, -106)),
        (CURRY, "29.5", (-122, -104)),
        (LEBRON, "24.5", (-115, -111)),
    ]
    morning = [*opening, (PRITCHARD, "12.5", (-118, -108))]
    afternoon = [
        (TATUM, "28.5", (-115, -105)),
        (BROWN, "24.5", None),
        (WHITE, "15.5", (-125, 105)),
        (CURRY, "30.5", (-120, -106)),
        (LEBRON, "24.5", (-115, -111)),
        (PRITCHARD, "12.5", (-118, -108)),
    ]
    evening = [
        (TATUM, "27.5", (-122, -104)),
        (BROWN, "24.5", None),
        (WHITE, "15.5", (-125, 105)),
        (CURRY, "31.5", (-130, 100)),
        (LEBRON, "24.5", (-115, -111)),
        (PRITCHARD, "12.5", (-118, -108)),
    ]
    for at, lines in (
        (EARLY, [(LEBRON, "24.5", (-115, -111))]),
        (T0, [*opening, (HAUSER, "9.5", (-115, -105))]),
        (T0B, opening),
        (T1, morning),
        (T1B, morning),
        (T2, afternoon),
    ):
        archive.ingest_complete_snapshot(_dabble(catalog, at, lines), query=QUERY, accepted_at=at)
    archive.freeze_closing_projection_set(
        provider="dabble",
        query=QUERY,
        canonical_game_id=STARTED,
        started_at=STARTED_TIP,
        created_at=STARTED_TIP,
    )
    archive.ingest_complete_snapshot(_dabble(catalog, T3, evening), query=QUERY, accepted_at=T3)
    # PrizePicks writes Partial snapshots: an omitted line is carried, a suspended one withdrawn.
    # Tatum's 28.5 is posted under two market ids; suspending one leaves him posted.
    tatum = _market(catalog, "prizepicks", TATUM, "points", "28.5")
    tatum_again = replace(tatum, market_id="prizepicks-tatum-again")
    white = _market(catalog, "prizepicks", WHITE, "points", "15.5")
    # Pritchard's two ids are withdrawn one Partial apiece: the first withdrawal does not see him.
    pritchard = _market(catalog, "prizepicks", PRITCHARD, "points", "12.5")
    pritchard_again = replace(pritchard, market_id="prizepicks-pritchard-again")
    for at, markets in (
        (T2B, (tatum, tatum_again, white, pritchard, pritchard_again)),
        (T2C, (tatum, replace(pritchard, status=MarketStatus.SUSPENDED))),
        (
            T3,
            tuple(
                replace(market, status=MarketStatus.SUSPENDED) for market in (tatum, white, pritchard_again)
            ),
        ),
    ):
        archive.ingest_snapshot(
            replace(
                _snapshot("prizepicks", markets, at),
                status=SnapshotStatus.PARTIAL,
                coverage=CoverageEvidence(fanout_complete=False),
            ),
            query=QUERY,
            accepted_at=at,
        )

    store = SportsbookQuoteStore(engine)
    night = [
        ResolvedQuote(TATUM[0], "PTS", "draftkings", 27.5, -110, -110, P0),
        ResolvedQuote(TATUM[0], "PTS", "fanduel", 28.5, -105, -115, P0),
        ResolvedQuote(HAUSER[0], "PTS", "draftkings", 9.5, -115, -105, P0),
    ]
    later = [
        ResolvedQuote(TATUM[0], "PTS", "draftkings", 28.5, -115, -105, P2),
        ResolvedQuote(TATUM[0], "PTS", "fanduel", 28.5, -105, -115, P0),
    ]
    for at, quotes in ((P0, night), (P1, night), (P2, later), (P3, later)):
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
                for player_id, name, team, _game in (
                    CURRY,
                    LEBRON,
                    TATUM,
                    BRUNSON,
                    BROWN,
                    WHITE,
                    HAUSER,
                    PRITCHARD,
                )
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
            required_providers=("dabble", "prizepicks"),
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
    "prizepicks": {
        "open": _value(28.5, None, None, seen_at="2026-01-15T23:50:00Z"),
        "previous": None,
        "unchanged": True,
    },
    "dabble": {
        "open": _value(27.5, -122, -104, seen_at="2026-01-14T23:00:00Z"),
        "previous": _value(28.5, -115, -105, changed_at="2026-01-15T23:55:00Z"),
        "unchanged": False,
    },
    "draftkings": {
        "open": _value(27.5, -110, -110, seen_at="2026-01-15T03:00:00Z"),
        "previous": _value(27.5, -110, -110, changed_at="2026-01-15T18:00:00Z"),
        "unchanged": False,
    },
    "fanduel": {
        "open": _value(28.5, -105, -115, seen_at="2026-01-15T03:00:00Z"),
        "previous": None,
        "unchanged": True,
    },
}


def test_a_line_that_moved_and_moved_back_shows_its_open_and_previous_value(moves_board, client):
    status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    assert status == 200
    tatum = _rows(body)["Jayson Tatum"]
    assert tatum["lines"] == {"dabble": 27.5, "prizepicks": 28.5}
    assert tatum["moves"] == TATUM_MOVES


def test_a_price_only_change_is_a_move(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    white = _rows(body)["Derrick White"]
    assert (white["lines"], white["line_prices"]) == (
        {"dabble": 15.5},
        {"dabble": {"over": -125, "under": 105}},
    )
    assert white["moves"] == {
        "dabble": {
            "open": _value(15.5, -110, -110, seen_at="2026-01-14T23:00:00Z"),
            "previous": _value(15.5, -110, -110, changed_at="2026-01-15T18:00:00Z"),
            "unchanged": False,
        }
    }


def test_a_line_only_dabble_cell_moves_with_null_prices(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    brown = _rows(body)["Jaylen Brown"]
    assert (brown["lines"], brown["line_prices"]) == ({"dabble": 24.5}, {})
    assert brown["moves"] == {
        "dabble": {
            "open": _value(23.5, None, None, seen_at="2026-01-14T23:00:00Z"),
            "previous": _value(23.5, None, None, changed_at="2026-01-15T18:00:00Z"),
            "unchanged": False,
        }
    }


def test_a_cell_that_never_moved_says_so_even_when_reposted(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    rows = _rows(body)
    # Dabble also posted LeBron's 24.5 two days before; history reaches one day before 00:00 ET.
    assert rows["LeBron James"]["moves"] == {
        "dabble": {
            "open": _value(24.5, -115, -111, seen_at="2026-01-14T23:00:00Z"),
            "previous": None,
            "unchanged": True,
        }
    }
    # First posted at 10:00 ET: its opening value is its first.
    assert rows["Payton Pritchard"]["moves"] == {
        "dabble": {
            "open": _value(12.5, -118, -108, seen_at="2026-01-15T15:00:00Z"),
            "previous": None,
            "unchanged": True,
        }
    }


def test_a_started_game_ends_its_history_at_the_tip(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    curry = _rows(body)["Stephen Curry"]
    assert curry["lines"] == {"dabble": 30.5}
    assert curry["moves"] == {
        "dabble": {
            "open": _value(29.5, -122, -104, seen_at="2026-01-14T23:00:00Z"),
            "previous": _value(29.5, -122, -104, changed_at="2026-01-15T18:00:00Z"),
            "unchanged": False,
        }
    }


PULLED = {
    "dabble": [
        {
            "game_id": LATER,
            "player_id": BRUNSON[0],
            "name": "Jalen Brunson",
            "team": "NYK",
            **_value(26.5, -120, -106),
            "last_seen_at": "2026-01-15T15:05:00Z",
        }
    ],
    "prizepicks": [
        {
            "game_id": LATER,
            "player_id": WHITE[0],
            "name": "Derrick White",
            "team": "BOS",
            **_value(15.5, None, None),
            "last_seen_at": "2026-01-15T23:50:00Z",
        },
        {
            "game_id": LATER,
            "player_id": PRITCHARD[0],
            "name": "Payton Pritchard",
            "team": "BOS",
            **_value(12.5, None, None),
            "last_seen_at": "2026-01-15T23:50:00Z",
        },
    ],
    "draftkings": [
        {
            "game_id": LATER,
            "player_id": HAUSER[0],
            "name": "Sam Hauser",
            "team": "BOS",
            **_value(9.5, -115, -105),
            "last_seen_at": None,
        }
    ],
}


def test_each_source_lists_the_lines_it_pulled_today(moves_board, client):
    status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    assert status == 200
    # Dabble's pull of Hauser came the evening before the slate's date, so it is not today's.
    # PrizePicks withdrew White in a Partial snapshot; Tatum, omitted from it, is still posted.
    assert body["pulled"] == PULLED
    assert "Jalen Brunson" not in _rows(body)


def test_sources_and_team_narrow_moves_and_pulled(moves_board, client):
    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true&sources=draftkings")
    assert _rows(body)["Jayson Tatum"]["moves"] == {"draftkings": TATUM_MOVES["draftkings"]}
    assert body["pulled"] == {"draftkings": PULLED["draftkings"]}

    _status, body = _get(client, "date=2026-01-15&market=PTS&moves=true&team=NYK")
    assert body["pulled"] == {"dabble": PULLED["dabble"]}


def test_without_moves_the_answer_is_unchanged(moves_board, client):
    plain_status, plain = _get(client, "date=2026-01-15&market=PTS")
    false_status, explicit_false = _get(client, "date=2026-01-15&market=PTS&moves=false")
    _status, moved = _get(client, "date=2026-01-15&market=PTS&moves=true")

    assert (plain_status, false_status) == (200, 200)
    assert explicit_false == plain
    assert "pulled" not in plain
    assert all("moves" not in row for game in plain["games"] for row in game["players"])
    moved.pop("pulled")
    for game in moved["games"]:
        for row in game["players"]:
            row.pop("moves")
    assert moved == plain


@pytest.mark.parametrize(
    "query",
    [
        "date=2026-01-15&market=PTS&moves=yes",
        "date=2026-01-15&market=PTS&moves=true&moves=true",
        # The index has no cells to carry moves.
        "date=2026-01-15&moves=true",
        # The archive keeps three days; the evening before this date is gone.
        "date=2026-01-13&market=PTS&moves=true",
    ],
)
def test_an_unservable_moves_request_is_refused(moves_board, client, query):
    status, body = _get(client, query)

    assert status == 400
    assert body["error"]["details"]["parameters"] == ["moves"]
