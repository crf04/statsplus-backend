"""GET /api/games/board on Postgres (crf04/statsplus#373): the pool, the
injury report and the books of a two-game slate, one repeatable-read scope."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql

from app.migrations import run_migrations
from app.providers.dfs import NBAMarketQuery
from app.providers.odds_api import BOOK_ALLOWLIST
from app.providers.registry import dfs_provider_names
from app.services.line_moves import book_histories
from app.services.projection_archive import LatestProjectionPlayerPoolReader, ProjectionArchiveReadScope
from tests.routes.test_games_board import (
    CURRY,
    CURRY_PTS,
    HAUSER,
    LATER,
    LATER_PTS,
    LEBRON_PTS,
    NOW,
    SEASON,
    SOURCES,
    TATUM,
    _games,
    _get,
    seed_board,
    test_a_book_is_judged_on_its_quotes_for_games_not_yet_started,
    test_a_started_games_closing_set_labels_its_unpriced_lines,
    test_a_started_games_own_pool_labels_its_unpriced_lines_from_a_set_or_a_replay,
    test_each_unpriced_line_is_labelled_line_only_alternate_or_unpriced,
)
from tests.routes.test_games_board_moves import PULLED, TATUM_MOVES, _rows, seed_moves

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def board(dependencies):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield seed_board(engine, dependencies)
    engine.dispose()


PTS_BOARD = {
    "date": "2026-01-15",
    "season": SEASON,
    "games": _games([CURRY_PTS, LEBRON_PTS], LATER_PTS),
    "sources": SOURCES,
    "players_total": 8,
    "omitted": {"book_only_unnamed": 2},
}


def test_the_pts_board_and_its_index_on_postgres(board, client):
    assert _get(client, "date=2026-01-15&market=PTS") == (200, PTS_BOARD)
    status, index = _get(client, "date=2026-01-15")
    assert status == 200
    assert index["markets"] == [
        {
            "market": "PTS",
            "players": 8,
            "book_only_unnamed": 2,
            "sources": ["dabble", "prizepicks", "draftkings", "fanduel"],
        },
        {"market": "3PM", "players": 1, "book_only_unnamed": 0, "sources": ["draftkings"]},
        {"market": "FG3A", "players": 2, "book_only_unnamed": 0, "sources": ["dabble"]},
    ]


def _tatum(body):
    (row,) = [row for game in body["games"] for row in game["players"] if row["name"] == "Jayson Tatum"]
    books = {book["book"]: book["line"] for book in row["book_lines"]["books"]}
    return row["lines"]["dabble"], books, row["injury_status"]


def _hauser(body):
    return [row["name"] for game in body["games"] for row in game["players"] if row["player_id"] == HAUSER[0]]


def _correct_tatum(engine):
    """Dabble and DraftKings move Tatum from 27.5 to 30.5, the report rules him Out and the
    catalog renames Hauser, in one commit."""

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
                "UPDATE sportsbook_prop_latest SET line = 30.5 "
                "WHERE book = 'draftkings' AND canonical_player_id = :player AND market_category = 'PTS'"
            ),
            {"player": TATUM[0]},
        )
        connection.execute(
            text(
                "UPDATE injury_snapshots "
                "SET normalized_entries = REPLACE(normalized_entries, '\"Questionable\"', '\"Out\"')"
            )
        )
        connection.execute(
            text("UPDATE athlete_catalog SET display_name = 'Samuel Hauser' WHERE player_id = :player"),
            {"player": HAUSER[0]},
        )


@pytest.mark.parametrize(
    ("owner", "method", "commits_first"),
    [
        # After the scope's first read, before the pool is read.
        ("event_catalog", "get_events_between", False),
        # After the pool is read, before the injury report and the books.
        ("injuries", "get_stored_injuries_many", True),
    ],
)
def test_a_correction_committed_mid_read_never_mixes_into_the_board(
    board, client, monkeypatch, owner, method, commits_first
):
    reads = getattr(board.service, owner)
    read = getattr(reads, method)

    def read_beside_a_correction(*arguments, connection=None, **keywords):
        if commits_first:
            _correct_tatum(board.engine)
        result = read(*arguments, connection=connection, **keywords)
        if not commits_first:
            _correct_tatum(board.engine)
        return result

    monkeypatch.setattr(reads, method, read_beside_a_correction)
    status, during = _get(client, "date=2026-01-15&market=PTS")
    monkeypatch.setattr(reads, method, read)
    _status, after = _get(client, "date=2026-01-15&market=PTS")

    assert status == 200
    assert _tatum(during) == (27.5, {"draftkings": 27.5, "fanduel": 28.5}, "Questionable")
    assert _tatum(after) == (30.5, {"draftkings": 30.5, "fanduel": 28.5}, "Out")
    assert (_hauser(during), _hauser(after)) == (["Sam Hauser"], ["Samuel Hauser"])


def test_a_pool_rebound_to_the_slates_season_reads_the_boards_snapshot(board, client, monkeypatch):
    """A pool reader configured for another season rebinds the whole slate to its season on
    the board's connection, as after the October rollover."""

    board.service.player_pool = LatestProjectionPlayerPoolReader(
        board.engine,
        tuple(
            ProjectionArchiveReadScope(provider=provider, query=NBAMarketQuery(season="2024-25"))
            for provider in dfs_provider_names()
        ),
        required_providers=("dabble", "prizepicks"),
        clock=lambda: NOW,
        event_reader=board.service.event_catalog,
    )
    reads = board.service.event_catalog
    read = reads.get_events_between

    def read_then_correct(*arguments, **keywords):
        result = read(*arguments, **keywords)
        _correct_tatum(board.engine)
        return result

    monkeypatch.setattr(reads, "get_events_between", read_then_correct)
    _status, during = _get(client, "date=2026-01-15&market=PTS")

    assert _tatum(during) == (27.5, {"draftkings": 27.5, "fanduel": 28.5}, "Questionable")


def _tip_later_and_correct_curry_closing(engine):
    """BOS @ NYK tips and Curry's frozen closing PTS line is corrected to 28.5, in one commit."""

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE event_catalog SET status_code = 2, status_text = 'In Progress', "
                "first_observed_started_at = :tipped WHERE nba_game_id = :game"
            ),
            {"tipped": NOW, "game": LATER},
        )
        connection.execute(
            text(
                "UPDATE projection_observations SET line_value = 28.5 "
                "WHERE canonical_player_id = :player AND market_category = 'PTS' "
                "AND observation_id IN (SELECT observation_id FROM projection_closing_memberships)"
            ),
            {"player": CURRY[0]},
        )


def test_the_pools_start_fences_and_closing_sets_read_the_boards_snapshot(board, client, monkeypatch):
    """The pool decides which games have started and reads their closing sets after the
    schedule's first read; a correction committed in between stays out of the board."""

    reads = board.service.event_catalog
    read = reads.get_events_between

    def read_then_correct(*arguments, **keywords):
        result = read(*arguments, **keywords)
        _tip_later_and_correct_curry_closing(board.engine)
        return result

    monkeypatch.setattr(reads, "get_events_between", read_then_correct)
    during = _get(client, "date=2026-01-15&market=PTS")
    monkeypatch.setattr(reads, "get_events_between", read)
    status, after = _get(client, "date=2026-01-15&market=PTS")

    assert during == (200, PTS_BOARD)
    assert status == 200
    assert [game["status"] for game in after["games"]] == ["live", "live"]
    assert after["games"][0]["players"][0]["lines"] == {"dabble": 28.5}


def test_a_failed_injury_read_leaves_the_rest_of_the_board(board, client):
    """The stored report fails on the board's shared connection; the books read after it
    still answer, and no player carries a status."""

    with board.engine.begin() as connection:
        connection.execute(text("ALTER TABLE injury_snapshots RENAME TO injury_snapshots_gone"))

    status, body = _get(client, "date=2026-01-15&market=PTS")

    assert status == 200
    assert _tatum(body) == (27.5, {"draftkings": 27.5, "fanduel": 28.5}, None)
    assert {row["injury_status"] for game in body["games"] for row in game["players"]} == {None}


def test_unpriced_lines_are_labelled_from_their_poll_on_postgres(board, client):
    """The same-poll priced-line lookup (crf04/statsplus#495) on the live, closing and replay reads."""

    test_each_unpriced_line_is_labelled_line_only_alternate_or_unpriced(board, client)


def test_a_closing_set_labels_its_unpriced_lines_on_postgres(board, client):
    test_a_started_games_closing_set_labels_its_unpriced_lines(board, client)


@pytest.mark.parametrize("freeze", [True, False])
def test_a_started_games_own_pool_labels_its_unpriced_lines_on_postgres(board, freeze):
    test_a_started_games_own_pool_labels_its_unpriced_lines_from_a_set_or_a_replay(board, freeze)


def test_a_book_is_stale_served_on_its_pending_games_on_postgres(board, client):
    test_a_book_is_judged_on_its_quotes_for_games_not_yet_started(board, client)


def test_line_moves_on_postgres(dependencies, client):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    seed_moves(engine, dependencies)

    status, body = _get(client, "date=2026-01-15&market=PTS&moves=true")

    engine.dispose()
    assert status == 200
    assert _rows(body)["Jayson Tatum"]["moves"] == TATUM_MOVES
    assert body["pulled"] == PULLED


def test_the_book_history_read_scans_only_the_requested_games(dependencies):
    """However long the season's history, the read walks the series index for the slate's games."""

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    seed_moves(engine, dependencies)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sportsbook_prop_quotes (poll_id, canonical_game_id, canonical_player_id, "
                "market_category, book, line, over_price, under_price, observed_at, is_closing) "
                "SELECT 1, '00225' || lpad((n % 400)::text, 5, '0'), 1628369, 'PTS', 'draftkings', 20.5, "
                "-110, -110, now() - n * interval '1 hour', false FROM generate_series(1, 4000) AS n"
            )
        )
        connection.execute(text("ANALYZE sportsbook_prop_quotes"))
    statements = []

    class Capturing:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement):
            statements.append(statement)
            return self.connection.execute(statement)

    with engine.connect() as connection:
        book_histories(
            Capturing(connection),
            game_ids=[LATER],
            market="PTS",
            books=sorted(BOOK_ALLOWLIST),
            posted=(),
        )
        compiled = statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        connection.execute(text("SET enable_seqscan = off"))
        (plan,) = connection.execute(text(f"EXPLAIN (ANALYZE, FORMAT JSON) {compiled}")).scalar_one()
    engine.dispose()

    def scans(node):
        yield node
        for child in node.get("Plans", ()):
            yield from scans(child)

    read = [node for node in scans(plan["Plan"]) if node["Node Type"].endswith("Scan")]
    indexed = [node for node in read if "Index Name" in node]
    assert "Seq Scan" not in {node["Node Type"] for node in read}
    assert {node["Index Name"] for node in indexed} == {"ix_sportsbook_prop_quotes_series"}
    # The index itself narrows to the slate game and the market: no row is read and then discarded.
    assert all(
        "canonical_game_id" in node["Index Cond"] and "market_category" in node["Index Cond"]
        for node in indexed
    )
    assert (
        sum(
            node.get("Rows Removed by Filter", 0) + node.get("Rows Removed by Index Recheck", 0)
            for node in read
        )
        == 0
    )
    # The slate game's fifteen PTS rows (Tatum 4, White 4, Hauser 2, Brown 4, Brunson 1), none of
    # the 4,000 rows of other games.
    assert max(node["Actual Rows"] for node in read) == 15
