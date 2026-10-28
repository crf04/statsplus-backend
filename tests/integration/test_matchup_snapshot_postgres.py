"""GET /api/games/matchup on Postgres: the pool, the injury report and the books of one
game read one repeatable-read scope, as the board's do (crf04/statsplus-backend#641)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.services.matchup_injuries import StoredMatchupInjuryReader
from tests.routes.test_games_board import BRUNSON, CURRY, HAUSER, LATER, LEBRON, STARTED, TATUM, seed_board
from tests.services.test_matchup_read_scope import matchup_over_board

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


def _correcting_after_the_first_read(monkeypatch, matchup):
    """Commit the correction once the scope's first read, the game's event, has run."""

    reads = matchup.service.event_catalog
    read = reads.get_event

    def read_then_correct(*arguments, **keywords):
        event = read(*arguments, **keywords)
        _correct_both_games(matchup.engine)
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
