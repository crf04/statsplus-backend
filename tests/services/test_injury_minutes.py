"""Minutes on each injury row: the report date's season and the one before (crf04/statsplus#488).

Game logs are real Publications in a migrated SQLite database, read through the
real reader and game-log repository.  ``world`` is production on 2026-11-10:
2025-26 is the Published Season and 2026-27's game logs are a held candidate.
``cut_over`` is the same games after the Season Cutover.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, update

from app.models.player_game_log import PublicationPlayerGameLog
from app.services.injury_listing import StoredInjuryListing
from app.services.injury_minutes import InjuryMinutesService
from tests import test_target_backtest as bt
from tests.routes.test_injuries import Events, _event
from tests.services.test_backtest_seasons import LAST, NEW, World

VETERAN, PRIOR_ONLY, NO_GAMES, TRADED, ROOKIE = 101, 103, 104, 105, 106
REPORT_DATE = "2026-11-10"
REPORT_AFTERNOON = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
GAMES = [_event("0022600150", (bt.LAL, "LAL"), (bt.BOS, "BOS"), "2026-11-11T00:30:00+00:00")]


def _games(player_id, season, minutes, *, team=(bt.LAL, "LAL"), first=0):
    start = date(2026, 1, 1) if season == LAST else date(2026, 10, 22)
    prefix = "00225" if season == LAST else "00226"
    rows = []
    for offset, played in enumerate(minutes, start=first):
        record = bt._row(
            player_id,
            name=f"Player {player_id}",
            game_id=f"{prefix}{player_id % 100:02d}{offset:03d}",
            game_date=start.replace(day=start.day + offset),
            team_id=team[0],
            team_tricode=team[1],
            minutes=played,
        )
        rows.append({**asdict(record), "season": season, "game_date": record.game_date.isoformat()})
    return rows


def _build(tmp_path, *, hold):
    built = World(create_engine(f"sqlite:///{tmp_path / 'minutes.sqlite3'}"))
    built.compose(
        "player_game_logs",
        LAST,
        {"rows": _games(VETERAN, LAST, [28, 30]) + _games(PRIOR_ONLY, LAST, [15, 25])},
    )
    built.compose(
        "player_game_logs",
        NEW,
        {
            "rows": _games(VETERAN, NEW, [32, 34])
            + _games(ROOKIE, NEW, [10])
            + _games(TRADED, NEW, [30, 30, 30], team=(bt.LAL, "LAL"))
            + _games(TRADED, NEW, [20, 20], team=(bt.BOS, "BOS"), first=3)
        },
        hold=hold,
    )
    return built


@pytest.fixture
def world(tmp_path):
    built = _build(tmp_path, hold=True)
    yield built
    built.engine.dispose()


@pytest.fixture
def cut_over(tmp_path):
    built = _build(tmp_path, hold=False)
    yield built
    built.engine.dispose()


def _service(world):
    return InjuryMinutesService(
        player_logs=world.logs, settings=world.settings, publication_reader=world.reader
    )


def _triples(found):
    return {player_id: (m.average_minutes, m.games_played, m.season) for player_id, m in found.items()}


def test_the_report_dates_season_is_read_before_its_cutover_beside_the_season_before(world):
    found = _service(world).minutes([VETERAN, PRIOR_ONLY, NO_GAMES, TRADED, ROOKIE], NEW)

    assert (found.season, found.published_season) == (NEW, LAST)
    assert _triples(found.current) == {
        VETERAN: (33.0, 2, NEW),
        TRADED: (26.0, 5, NEW),
        ROOKIE: (10.0, 1, NEW),
    }
    assert _triples(found.prior) == {VETERAN: (29.0, 2, LAST), PRIOR_ONLY: (20.0, 2, LAST)}


def test_after_the_cutover_the_two_seasons_are_still_the_report_dates_and_the_one_before(cut_over):
    found = _service(cut_over).minutes([VETERAN, PRIOR_ONLY, NO_GAMES, TRADED, ROOKIE], NEW)

    assert (found.season, found.published_season) == (NEW, NEW)
    assert _triples(found.current) == {
        VETERAN: (33.0, 2, NEW),
        TRADED: (26.0, 5, NEW),
        ROOKIE: (10.0, 1, NEW),
    }
    assert _triples(found.prior) == {VETERAN: (29.0, 2, LAST), PRIOR_ONLY: (20.0, 2, LAST)}


def test_an_unreadable_prior_season_leaves_the_current_minutes(cut_over):
    cut_over.revoke("player_game_logs", LAST)

    found = _service(cut_over).minutes([VETERAN, PRIOR_ONLY], NEW)

    assert _triples(found.current) == {VETERAN: (33.0, 2, NEW)}
    assert found.prior == {}


PLAYERS = [
    ("No Id", None, "LAL", "Out"),
    ("Prior Only", PRIOR_ONLY, "LAL", "Out"),
    ("No Games", NO_GAMES, "LAL", "Out"),
    ("Rookie", ROOKIE, "LAL", "Out"),
    ("Traded", TRADED, "LAL", "Out"),
    ("Veteran", VETERAN, "LAL", "Out"),
]


def _listing(world, minutes, *, ids=None, players=PLAYERS):
    entries = [
        {
            "source_player_name": name,
            "canonical_player_id": player_id,
            "tricode": tricode,
            "canonical_status": status,
            "reason": "",
            "report_time": None,
        }
        for name, player_id, tricode, status in players
        for player_id in [ids(player_id) if ids and player_id is not None else player_id]
    ]
    snapshot = SimpleNamespace(normalized_entries=entries, retrieved_at=REPORT_AFTERNOON)

    class Snapshots:
        def get_many(self, scopes):
            return {scopes[0]: snapshot}

    return StoredInjuryListing(
        event_catalog=Events(GAMES),
        snapshot_repository=Snapshots(),
        settings=world.settings,
        enabled=True,
        clock=lambda: REPORT_AFTERNOON,
        minutes=minutes,
    )


def _minutes_columns(body):
    return {
        row["player"]: (
            (row["average_minutes"], row["games_played"], row["minutes_season"]),
            (row["prior_average_minutes"], row["prior_games_played"], row["prior_minutes_season"]),
        )
        for row in body["injuries"]
    }


NONE = (None, 0, None)


def test_every_injury_row_carries_both_seasons(world):
    body = _listing(world, _service(world)).list_injuries(REPORT_DATE, None)

    assert (body["minutes_season"], body["minutes_published_season"]) == (NEW, LAST)
    assert _minutes_columns(body) == {
        "Veteran": ((33.0, 2, NEW), (29.0, 2, LAST)),
        "Traded": ((26.0, 5, NEW), NONE),
        "Rookie": ((10.0, 1, NEW), NONE),
        "Prior Only": (NONE, (20.0, 2, LAST)),
        "No Games": (NONE, NONE),
        "No Id": (NONE, NONE),
    }


def test_minutes_leave_the_order_by_team_then_status_then_player(world):
    players = [
        ("Veteran", VETERAN, "LAL", "Questionable"),
        ("Traded", TRADED, "BOS", "Out"),
        ("Rookie", ROOKIE, "LAL", "Out"),
        ("Prior Only", PRIOR_ONLY, "BOS", "Doubtful"),
        ("No Games", NO_GAMES, "LAL", "Out"),
        ("No Id", None, "BOS", "Out"),
    ]

    body = _listing(world, _service(world), players=players).list_injuries(REPORT_DATE, None)

    assert [(row["team"], row["status"], row["player"]) for row in body["injuries"]] == [
        ("BOS", "Out", "No Id"),
        ("BOS", "Out", "Traded"),
        ("BOS", "Doubtful", "Prior Only"),
        ("LAL", "Out", "No Games"),
        ("LAL", "Out", "Rookie"),
        ("LAL", "Questionable", "Veteran"),
    ]


def test_after_the_cutover_no_row_names_one_season_twice(cut_over):
    body = _listing(cut_over, _service(cut_over)).list_injuries(REPORT_DATE, None)

    assert (body["minutes_season"], body["minutes_published_season"]) == (NEW, NEW)
    assert _minutes_columns(body)["Veteran"] == ((33.0, 2, NEW), (29.0, 2, LAST))
    assert {row["minutes_season"] for row in body["injuries"]} == {NEW, None}
    assert {row["prior_minutes_season"] for row in body["injuries"]} == {LAST, None}


def test_an_unreadable_prior_season_does_not_fail_the_list(cut_over):
    cut_over.revoke("player_game_logs", LAST)

    body = _listing(cut_over, _service(cut_over)).list_injuries(REPORT_DATE, None)

    assert _minutes_columns(body)["Veteran"] == ((33.0, 2, NEW), NONE)
    assert _minutes_columns(body)["Prior Only"] == (NONE, NONE)


def test_a_player_with_no_current_games_listed_alone_keeps_empty_current_minutes(world):
    listing = _listing(world, _service(world), players=[("Prior Only", PRIOR_ONLY, "LAL", "Out")])

    body = listing.list_injuries(REPORT_DATE, None)

    assert _minutes_columns(body) == {"Prior Only": (NONE, (20.0, 2, LAST))}


def test_opening_night_before_any_game_log_keeps_last_seasons_minutes(tmp_path):
    opening = World(create_engine(f"sqlite:///{tmp_path / 'opening.sqlite3'}"))
    opening.compose("player_game_logs", LAST, {"rows": _games(VETERAN, LAST, [28, 30])})

    body = _listing(opening, _service(opening)).list_injuries(REPORT_DATE, None)
    opening.engine.dispose()

    assert body["minutes_season"] == NEW
    assert _minutes_columns(body)["Veteran"] == (NONE, (29.0, 2, LAST))


def test_a_refused_current_season_does_not_fail_the_list(world):
    world.checks.refused["player_game_logs"] = "candidate_not_readable"

    body = _listing(world, _service(world)).list_injuries(REPORT_DATE, None)

    assert _minutes_columns(body)["Veteran"] == (NONE, (29.0, 2, LAST))


def test_the_list_reads_each_season_once_however_many_players_it_has(cut_over):
    reads = []

    @event.listens_for(cut_over.engine, "before_cursor_execute")
    def _count(connection, cursor, statement, *_rest):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "publication_player_game_logs.row_payload" in statement.split(" FROM ")[0]
        ):
            reads.append(statement)

    _listing(cut_over, _service(cut_over)).list_injuries(REPORT_DATE, None)

    assert len(reads) == 2


def test_seasons_are_null_with_no_minutes_service(world):
    body = _listing(world, None).list_injuries(REPORT_DATE, None)

    assert (body["minutes_season"], body["minutes_published_season"]) == (None, None)
    assert _minutes_columns(body)["Veteran"] == (NONE, NONE)


def test_a_player_id_stored_as_a_string_still_matches(world):
    body = _listing(world, _service(world), ids=str).list_injuries(REPORT_DATE, None)

    assert _minutes_columns(body)["Veteran"] == ((33.0, 2, NEW), (29.0, 2, LAST))
    assert _minutes_columns(body)["Traded"] == ((26.0, 5, NEW), NONE)


def _corrupt(world, season_publication_season, player_id):
    capture = world.reader.snapshot if season_publication_season == NEW else world.reader.retained_snapshot
    snapshot = capture(
        ("player_game_logs",),
        season=season_publication_season,
        projection_only_keys=frozenset({"player_game_logs"}),
    )
    publication_id = snapshot.read("player_game_logs").publication_id
    with world.engine.begin() as connection:
        connection.execute(
            update(PublicationPlayerGameLog)
            .where(
                PublicationPlayerGameLog.publication_id == publication_id,
                PublicationPlayerGameLog.player_id == player_id,
            )
            .values(row_payload="bad json")
        )


def test_corrupt_current_rows_give_that_player_no_current_minutes_and_spare_the_others(cut_over):
    _corrupt(cut_over, NEW, VETERAN)

    found = _service(cut_over).minutes([VETERAN, TRADED], NEW)

    assert _triples(found.current) == {TRADED: (26.0, 5, NEW)}
    assert _triples(found.prior) == {VETERAN: (29.0, 2, LAST)}


def test_corrupt_prior_rows_give_that_player_no_prior_minutes(cut_over):
    _corrupt(cut_over, LAST, PRIOR_ONLY)

    found = _service(cut_over).minutes([PRIOR_ONLY, VETERAN], NEW)

    assert _triples(found.prior) == {VETERAN: (29.0, 2, LAST)}


def test_postseason_games_do_not_count(world, monkeypatch):
    """The legacy table may hold Playoffs rows; they are not regular-season minutes."""

    real = world.logs.list_players_rows

    def with_a_playoff_game(season, player_ids, **kwargs):
        rows = real(season, player_ids, **kwargs)
        if season == NEW and ROOKIE in rows:
            extra = replace(rows[ROOKIE][0], season_type="Playoffs", game_id="0042600001", minutes=40)
            rows = {**rows, ROOKIE: rows[ROOKIE] + (extra,)}
        return rows

    monkeypatch.setattr(world.logs, "list_players_rows", with_a_playoff_game)

    assert _triples(_service(world).minutes([ROOKIE], NEW).current) == {ROOKIE: (10.0, 1, NEW)}
