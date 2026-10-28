"""GET /api/games/matchup on Postgres: the pool, the injury report and the books of one
game read one repeatable-read scope, as the board's do (crf04/statsplus-backend#641)."""

from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.providers.dfs import NBAMarketQuery
from app.providers.registry import dfs_provider_names
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.matchup_injuries import StoredMatchupInjuryReader
from app.services.matchup_reuse import ComposedMatchupCache
from app.services.matchup_snapshot import SnapshotMatchups
from app.services.projection_archive import LatestProjectionPlayerPoolReader, ProjectionArchiveReadScope
from app.services.request_reads import request_read_scope
from app.services.stats_freshness_repository import StatsFreshnessRepository
from tests.routes.test_games_board import (
    BRUNSON,
    CURRY,
    HAUSER,
    LATER,
    LATER_TIP,
    LEBRON,
    NOW,
    SEASON,
    STARTED,
    TATUM,
    seed_board,
)
from tests.services.test_matchup_read_scope import matchup_over_board
from tests.test_matchup_reuse import DictRedis, _real_snapshot

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def matchup(dependencies):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    dependencies.matchup_service = matchup_over_board(seed_board(engine, dependencies).service, engine)
    yield SimpleNamespace(engine=engine, service=dependencies.matchup_service)
    engine.dispose()


def _correct_both_games(engine):
    """One commit: Dabble and DraftKings move Tatum from 27.5 to 30.5 and the report
    downgrades him to Doubtful; Curry's frozen closing PTS line is corrected to 28.5,
    DraftKings' closing line to 30.5, and LeBron is listed Probable."""

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE latest_player_projections SET line_value = 30.5 "
                "WHERE provider = 'dabble' AND canonical_player_id = :player AND market_category = 'PTS'"
            ),
            {"player": TATUM[0]},
        )
        connection.execute(
            text(
                "UPDATE projection_observations SET line_value = 28.5 "
                "WHERE canonical_player_id = :player AND market_category = 'PTS' "
                "AND observation_id IN (SELECT observation_id FROM projection_closing_memberships)"
            ),
            {"player": CURRY[0]},
        )
        for player in (TATUM, CURRY):
            connection.execute(
                text(
                    "UPDATE sportsbook_prop_latest SET line = 30.5 "
                    "WHERE book = 'draftkings' AND canonical_player_id = :player AND market_category = 'PTS'"
                ),
                {"player": player[0]},
            )
        connection.execute(
            text(
                "UPDATE injury_snapshots SET normalized_entries = "
                "REPLACE(REPLACE(normalized_entries, '\"Questionable\"', '\"Doubtful\"'), "
                "'\"Available\"', '\"Probable\"')"
            )
        )


def _state(body, player):
    """The player's Dabble PTS line, each book's PTS line, and the game's injury statuses."""

    (row,) = [row for row in body["players"] if row["canonical_id"] == player[0]]
    books = {book["book"]: book["line"] for book in row["book_lines"].get("PTS", {}).get("books", ())}
    statuses = {
        entry["canonical_player_id"]: entry["canonical_status"]
        for team in body["injuries"]["teams"]
        for entry in team["entries"]
    }
    return row["lines"]["dabble"]["PTS"], books, statuses


def _correcting_after_the_first_read(monkeypatch, matchup, correct=_correct_both_games):
    """Commit ``correct`` once the scope's first read, the game's event, has run."""

    reads = matchup.service.event_catalog
    read = reads.get_event

    def read_then_correct(*arguments, **keywords):
        event = read(*arguments, **keywords)
        correct(matchup.engine)
        return event

    monkeypatch.setattr(reads, "get_event", read_then_correct)
    return lambda: monkeypatch.setattr(reads, "get_event", read)


@pytest.mark.parametrize(
    ("game", "player", "before", "after"),
    [
        # Not started: the Latest pool, the live books and a stale-served report.
        (
            LATER,
            TATUM,
            (
                27.5,
                {"draftkings": 27.5, "fanduel": 28.5},
                {TATUM[0]: "Questionable", HAUSER[0]: "Probable", BRUNSON[0]: "Out"},
            ),
            (
                30.5,
                {"draftkings": 30.5, "fanduel": 28.5},
                {TATUM[0]: "Doubtful", HAUSER[0]: "Probable", BRUNSON[0]: "Out"},
            ),
        ),
        # Started: the start fence, the closing set, the closing books and the pre-tip report.
        (
            STARTED,
            CURRY,
            (29.5, {"draftkings": 29.5}, {LEBRON[0]: "Available"}),
            (28.5, {"draftkings": 30.5}, {LEBRON[0]: "Probable"}),
        ),
    ],
    ids=("scheduled", "started"),
)
def test_a_correction_committed_mid_read_never_mixes_into_the_matchup(
    matchup, client, authenticate, monkeypatch, game, player, before, after
):
    headers = authenticate()
    restore = _correcting_after_the_first_read(monkeypatch, matchup)
    during = client.get(f"/api/games/matchup?game_id={game}", headers=headers)
    restore()
    following = client.get(f"/api/games/matchup?game_id={game}", headers=headers)

    assert (during.status_code, following.status_code) == (200, 200)
    assert _state(during.get_json(), player) == before
    assert _state(following.get_json(), player) == after


def test_a_snapshot_compose_without_a_caller_scope_reads_one_snapshot(matchup, monkeypatch):
    """A Target's Matchup composes through ``get_matchup_from_snapshot`` on a scope of its own."""

    injuries = StoredMatchupInjuryReader(matchup.service.injuries)

    def compose():
        return matchup.service.get_matchup_from_snapshot(
            game_id=LATER, publication_snapshot=None, injuries=injuries
        )

    restore = _correcting_after_the_first_read(monkeypatch, matchup)
    during = compose()
    restore()
    following = compose()

    statuses = {TATUM[0]: "Questionable", HAUSER[0]: "Probable", BRUNSON[0]: "Out"}
    assert _state(during, TATUM) == (27.5, {}, statuses)
    assert _state(following, TATUM) == (30.5, {}, {**statuses, TATUM[0]: "Doubtful"})


REPORT = {TATUM[0]: "Questionable", HAUSER[0]: "Probable", BRUNSON[0]: "Out"}
LATER_BOOKS = {"draftkings": 27.5, "fanduel": 28.5}


def _adopt_the_league_report(matchup):
    """The league report is newer than the game's stored one, so the Matchup reconciles it
    against the production catalog reader.  LeBron's ``Available`` is not a canonical
    status a stored report can be reconciled from, so it becomes ``Probable``."""

    matchup.service.injuries.athlete_catalog = AthleteCatalogReader(matchup.engine)
    with matchup.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE injury_source_snapshots SET retrieved_at = :at, "
                "normalized_entries = REPLACE(normalized_entries, '\"Available\"', '\"Probable\"')"
            ),
            {"at": NOW - timedelta(minutes=1)},
        )


@pytest.mark.parametrize(
    ("adopts", "unreadable", "status", "statuses"),
    [
        # The game's stored report fails; the league report is reconciled but cannot be stored.
        (False, "injury_snapshots", "unavailable", {}),
        # The league report fails; the game's stored report is stale-served.
        (False, "injury_source_snapshots", "stale", REPORT),
        # The catalog fails while the newer league report is reconciled; the stored one is served.
        (True, "athlete_catalog", "stale", REPORT),
    ],
    ids=("game-report", "league-report", "catalog"),
)
def test_a_failed_injury_read_leaves_the_rest_of_the_matchup(
    matchup, client, authenticate, adopts, unreadable, status, statuses
):
    if adopts:
        _adopt_the_league_report(matchup)
    with matchup.engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {unreadable} RENAME TO {unreadable}_gone"))

    response = client.get(f"/api/games/matchup?game_id={LATER}", headers=authenticate())

    assert response.status_code == 200
    body = response.get_json()
    assert body["injuries"]["status"] == status
    assert _state(body, TATUM) == (27.5, LATER_BOOKS, statuses)


def test_a_failed_stored_injury_read_leaves_the_next_game_on_the_scope(matchup):
    """The Slate composes every game's Matchup on one scope: a game whose stored report
    fails leaves the next game's reads working."""

    with matchup.engine.begin() as connection:
        connection.execute(text("ALTER TABLE injury_snapshots RENAME TO injury_snapshots_gone"))

    injuries = StoredMatchupInjuryReader(matchup.service.injuries)
    with request_read_scope(matchup.engine, repeatable_read=True) as read_scope:
        later, started = (
            matchup.service.get_matchup_from_snapshot(
                game_id=game, publication_snapshot=None, injuries=injuries, read_scope=read_scope
            )
            for game in (LATER, STARTED)
        )

    assert [later["injuries"]["unavailable_reason"], started["injuries"]["unavailable_reason"]] == [
        "fetch_failed",
        "fetch_failed",
    ]
    assert (_state(later, TATUM), _state(started, CURRY)) == ((27.5, {}, {}), (29.5, {}, {}))


def test_a_reconciled_league_report_reads_the_catalog_of_the_matchups_snapshot(
    matchup, client, authenticate, monkeypatch
):
    """Brunson leaves the catalog in the commit that moves Tatum's line: the Matchup still
    matches his Out entry, so he is not targetable beside the pre-correction line."""

    _adopt_the_league_report(matchup)

    def drop_brunson(engine):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE latest_player_projections SET line_value = 30.5 "
                    "WHERE provider = 'dabble' AND canonical_player_id = :player AND market_category = 'PTS'"
                ),
                {"player": TATUM[0]},
            )
            connection.execute(
                text("UPDATE athlete_catalog SET is_active_for_season = false WHERE player_id = :player"),
                {"player": BRUNSON[0]},
            )

    _correcting_after_the_first_read(monkeypatch, matchup, drop_brunson)
    body = client.get(f"/api/games/matchup?game_id={LATER}", headers=authenticate()).get_json()

    brunson = [
        entry["canonical_player_id"]
        for team in body["injuries"]["teams"]
        for entry in team["entries"]
        if entry["source_player_name"] == BRUNSON[1]
    ]
    assert (body["injuries"]["status"], brunson) == ("fresh", [BRUNSON[0]])
    assert _state(body, TATUM)[0] == 27.5
    assert BRUNSON[0] not in {row["canonical_id"] for row in body["players"]}


def test_a_cached_matchup_reports_the_stats_freshness_of_its_read_scope(matchup, monkeypatch):
    """The Slate composes through ``SnapshotMatchups`` on its scope; the stats freshness the
    cache wrapper writes back is that scope's, not a later commit's."""

    service = matchup.service
    service.stats_freshness = StatsFreshnessRepository(matchup.engine)
    service.stats_freshness.record_success(NOW - timedelta(hours=1))

    def complete_stats(engine):
        with engine.begin() as connection:
            service.stats_freshness.record_success(NOW, connection=connection)
            connection.execute(
                text(
                    "UPDATE latest_player_projections SET line_value = 30.5 "
                    "WHERE provider = 'dabble' AND canonical_player_id = :player AND market_category = 'PTS'"
                ),
                {"player": TATUM[0]},
            )

    _correcting_after_the_first_read(monkeypatch, matchup, complete_stats)
    redis = DictRedis()
    with request_read_scope(matchup.engine, repeatable_read=True) as read_scope:
        document = SnapshotMatchups(
            service,
            _real_snapshot(age=0),
            StoredMatchupInjuryReader(service.injuries),
            season=SEASON,
            cache=ComposedMatchupCache(redis),
            settings=service.settings,
            read_scope=read_scope,
        ).get_matchup(game_id=LATER, game={"game_id": LATER, "scheduled_at": LATER_TIP.isoformat()})

    assert len(redis.values) == 1
    assert _state(document, TATUM)[0] == 27.5
    assert document["freshness"]["stats"]["retrieved_at"] == (NOW - timedelta(hours=1)).isoformat()


def test_a_pool_rebound_to_the_games_season_reads_the_matchups_snapshot(
    matchup, client, authenticate, monkeypatch
):
    """A pool reader configured for another season rebinds to the game's season on the same
    connection, as after the October rollover."""

    matchup.service.player_pool = LatestProjectionPlayerPoolReader(
        matchup.engine,
        tuple(
            ProjectionArchiveReadScope(provider=provider, query=NBAMarketQuery(season="2024-25"))
            for provider in dfs_provider_names()
        ),
        required_providers=("dabble", "prizepicks"),
        clock=lambda: NOW,
        event_reader=matchup.service.event_catalog,
    )
    restore = _correcting_after_the_first_read(monkeypatch, matchup)
    during = client.get(f"/api/games/matchup?game_id={LATER}", headers=authenticate())
    restore()
    following = client.get(f"/api/games/matchup?game_id={LATER}", headers=authenticate())

    assert _state(during.get_json(), TATUM) == (27.5, LATER_BOOKS, REPORT)
    assert _state(following.get_json(), TATUM)[0] == 30.5
