"""Slate edges on PostgreSQL: each game of an omitted season composes on its own transaction.

A failed statement aborts a PostgreSQL transaction and a lost connection takes
its transaction with it, so neither may cost the games composed after it their
cells, as at 3a1a7bb9.  Skipped unless ``TEST_DATABASE_URL`` names a
disposable database.
"""

from __future__ import annotations

import os
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config.settings import RuntimeSettings
from app.services.matchup_reuse import PublicationSnapshotReuse
from app.services.matchup_snapshot import capture_matchup_snapshot
from app.services.request_reads import request_read_scope
from app.services.slate_edges import SlateEdgesService
from tests.test_slate_edges import GENERATION, FakeSlate, _blend_player, _game, _matchup

pytestmark = pytest.mark.integration

SETTINGS = RuntimeSettings(environment="testing", nba={"current_season": "2025-26"})


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


class Publications:
    def snapshot(self, stream_keys, *, season, **_):
        return SimpleNamespace(generation=GENERATION, retained_history=())


class ScopedComposer:
    """Reads each game on its session, then its connection; ``fail`` breaks ``G1``'s read.

    It owns its scope as ``MatchupService.get_matchup_from_snapshot`` does:
    the caller's, else a repeatable-read one of its own.
    """

    def __init__(self, engine, fail):
        self.engine = engine
        self.fail = fail

    def get_matchup_from_snapshot(self, *, game_id, publication_snapshot, injuries, read_scope=None, **_):
        scope = (
            nullcontext(read_scope)
            if read_scope is not None
            else request_read_scope(self.engine, repeatable_read=True)
        )
        with scope as (connection, session):
            session.execute(text("SELECT 1"))
            if game_id == "G1":
                self.fail(connection)
            value = float(connection.execute(text("SELECT 0.05")).scalar_one())
            return _matchup([_blend_player(2, "B", value)])


def _divide_by_zero(connection):
    connection.execute(text("SELECT 1 / 0"))


def _terminate_backend(connection):
    """Terminate the connection's own backend, then read on it: a genuine disconnect."""

    pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    with connection.engine.connect() as killer:
        killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    connection.execute(text("SELECT 1"))


@pytest.mark.parametrize("fail", [_divide_by_zero, _terminate_backend], ids=["statement", "disconnect"])
def test_a_game_whose_read_fails_leaves_the_later_games_cells(engine, fail):
    service = SlateEdgesService(
        slates=FakeSlate([_game("G1"), _game("G2")]),
        matchups=ScopedComposer(engine, fail),
        publication_reader=Publications(),
        injuries=object(),
        settings=SETTINGS,
        engine=engine,
    )

    body = service.get_edges("2026-04-10")

    assert [(game["game_id"], game["scores"]) for game in body["games"]] == [
        ("G1", []),
        ("G2", [{"canonical_id": 2, "value": 0.05}]),
    ]
    assert body["spotlight"]["best"]["canonical_id"] == 2


# --- the reused snapshot and the games' own reads ---------------------------


@pytest.fixture
def generation_table(engine):
    """A one-row table whose value names the live Publication generation."""

    table = f"edges_generation_{uuid4().hex[:12]}"
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE {table} (value int)"))
        connection.execute(text(f"INSERT INTO {table} VALUES (1)"))
    yield table
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE {table}"))


class ActivatingPublications:
    """Publications whose generation is the table's value: ``G<value>``.

    A read on ``session`` sees that session's transaction; any other read sees
    the database now.  With ``race`` set, the next capture without a session
    activates the next generation between its generation check and its read,
    as an activation can commit mid-capture.
    """

    captures_published_season = True

    def __init__(self, engine, table):
        self.engine = engine
        self.table = table
        self.race = False

    def value(self, session=None):
        query = text(f"SELECT value FROM {self.table}")
        if session is not None:
            return f"G{session.execute(query).scalar_one()}"
        with self.engine.connect() as connection:
            return f"G{connection.execute(query).scalar_one()}"

    def activate(self):
        with self.engine.begin() as connection:
            connection.execute(text(f"UPDATE {self.table} SET value = value + 1"))

    def generation(self, stream_keys, *, session=None):
        return (("player_game_logs", self.value(session), 1, 1),)

    def snapshot(self, stream_keys, *, season, session=None, **_):
        if self.race and session is None:
            self.race = False
            self.activate()
        return SimpleNamespace(
            generation=(("player_game_logs", self.value(session), 1, 1),),
            retained_history=(),
            season="2025-26",
        )


class TimingOutGeneration(ActivatingPublications):
    """Publications whose cheap generation check exceeds its statement timeout."""

    def generation(self, stream_keys, *, session=None):
        statements = ("SET LOCAL statement_timeout = 10", "SELECT pg_sleep(0.05)")
        with nullcontext(session) if session is not None else Session(self.engine) as reader:
            for statement in statements:
                reader.execute(text(statement))
        raise AssertionError("the generation check was meant to time out")


class GenerationComposer:
    """Records, per game, the snapshot's generation and the one its own reads see."""

    def __init__(self, publications):
        self.publications = publications
        self.reads = []

    def get_matchup_from_snapshot(self, *, game_id, publication_snapshot, injuries, read_scope=None, **_):
        scope = (
            nullcontext(read_scope)
            if read_scope is not None
            else request_read_scope(self.publications.engine, repeatable_read=True)
        )
        with scope as (_connection, session):
            self.reads.append((publication_snapshot.generation[0][1], self.publications.value(session)))
        return _matchup([_blend_player(2, "B", 0.05)])


class ReadingSlate(FakeSlate):
    """Reads the table on the connection the edges give it, then runs ``then``."""

    def __init__(self, games, publications, then):
        super().__init__(games)
        self.publications = publications
        self.then = then

    def get_slate(self, requested_date=None, *, neighbours=True, connection=None):
        query = text(f"SELECT value FROM {self.publications.table}")
        if connection is None:
            with self.publications.engine.connect() as owned:
                owned.execute(query)
        else:
            connection.execute(query)
        self.then()
        return super().get_slate(requested_date)


def _edges(publications, reuse, then=lambda: None):
    composer = GenerationComposer(publications)
    body = SlateEdgesService(
        slates=ReadingSlate([_game("G1"), _game("G2")], publications, then),
        matchups=composer,
        publication_reader=publications,
        injuries=object(),
        settings=SETTINGS,
        snapshot_reuse=reuse,
        engine=publications.engine,
    ).get_edges("2026-04-10")
    return body, composer.reads


def _activate_after_the_slate(publications, reuse):
    """Round 1 on #674: the reuse holds G1, and G2 activates after the Slate's read."""

    capture_matchup_snapshot(SETTINGS, publications, reuse)
    return publications.activate


def _race_a_capture_after_the_slate(publications, reuse):
    """Round 2 on #674: after the Slate's read, a capture without a session (as
    Target resolution's) checks G1, G2 activates mid-capture, and the reuse
    holds G2 under G1."""

    def race():
        publications.race = True
        capture_matchup_snapshot(SETTINGS, publications, reuse)

    return race


@pytest.mark.parametrize(
    "arrange", [_activate_after_the_slate, _race_a_capture_after_the_slate], ids=["activation", "race"]
)
def test_every_game_reads_the_generation_its_snapshot_holds(engine, generation_table, arrange):
    """As 3a1a7bb9: the edges capture on no request transaction, so they
    compose the generation live after the Slate's read, each game's reads see
    the generation its snapshot holds, and the reuse keeps no older capture
    under the live generation."""

    publications = ActivatingPublications(engine, generation_table)
    reuse = PublicationSnapshotReuse()
    then = arrange(publications, reuse)

    body, reads = _edges(publications, reuse, then)

    assert reads == [("G2", "G2"), ("G2", "G2")]
    assert [game["game_id"] for game in body["games"] if game["scores"]] == ["G1", "G2"]
    # The reuse holds no older capture under the live generation.
    assert capture_matchup_snapshot(SETTINGS, publications, reuse)[1].generation[0][1] == "G2"


def test_a_generation_check_that_times_out_falls_back_to_a_full_capture(engine, generation_table):
    """As 3a1a7bb9: the cheap check runs on no request transaction, so its
    failure cannot abort the capture that replaces it."""

    publications = TimingOutGeneration(engine, generation_table)

    body, reads = _edges(publications, PublicationSnapshotReuse())

    assert reads == [("G1", "G1"), ("G1", "G1")]
    assert [game["game_id"] for game in body["games"] if game["scores"]] == ["G1", "G2"]
