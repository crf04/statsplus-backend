"""Minutes on each injury row come from the season rule of crf04/statsplus#230.

Game logs are real Publications in a migrated SQLite database, read through the
real reader and game-log repository; 2025-26 is published first and 2026-27
activates after it, so the prior season is always read from retained history.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event

from app.services.injury_listing import StoredInjuryListing
from app.services.injury_minutes import InjuryMinutesService
from tests import test_target_backtest as bt
from tests.routes.test_injuries import AFTERNOON, Events
from tests.services.test_backtest_seasons import LAST, NEW, World

ALWAYS_FIVE, FOUR_PLUS_PRIOR, PRIOR_ONLY, NO_GAMES, TRADED = 101, 102, 103, 104, 105


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


def _publish(world, season, rows):
    world.compose("player_game_logs", season, {"rows": rows})


@pytest.fixture
def world(tmp_path):
    built = World(create_engine(f"sqlite:///{tmp_path / 'minutes.sqlite3'}"))
    _publish(
        built,
        LAST,
        _games(FOUR_PLUS_PRIOR, LAST, [10, 20, 30])
        + _games(PRIOR_ONLY, LAST, [15, 25])
        + _games(ALWAYS_FIVE, LAST, [5]),
    )
    _publish(
        built,
        NEW,
        _games(ALWAYS_FIVE, NEW, [30, 32, 34, 36, 38])
        + _games(FOUR_PLUS_PRIOR, NEW, [20, 20, 20, 20])
        + _games(TRADED, NEW, [30, 30, 30], team=(bt.LAL, "LAL"))
        + _games(TRADED, NEW, [20, 20], team=(bt.BOS, "BOS"), first=3),
    )
    yield built
    built.engine.dispose()


def _service(world):
    return InjuryMinutesService(
        player_logs=world.logs, settings=world.settings, publication_reader=world.reader
    )


def test_each_branch_of_the_season_rule(world):
    found = _service(world).minutes(
        [ALWAYS_FIVE, FOUR_PLUS_PRIOR, PRIOR_ONLY, NO_GAMES, TRADED]
    )

    assert {
        player_id: (m.average_minutes, m.games_played, m.season) for player_id, m in found.items()
    } == {
        ALWAYS_FIVE: (34.0, 5, NEW),
        FOUR_PLUS_PRIOR: (20.0, 3, LAST),
        PRIOR_ONLY: (20.0, 2, LAST),
        TRADED: (26.0, 5, NEW),
    }


def test_an_unreadable_prior_season_gives_no_minutes_to_the_rows_that_need_it(world):
    world.revoke("player_game_logs", LAST)

    found = _service(world).minutes([ALWAYS_FIVE, FOUR_PLUS_PRIOR, PRIOR_ONLY])

    assert {
        player_id: (m.average_minutes, m.games_played, m.season) for player_id, m in found.items()
    } == {ALWAYS_FIVE: (34.0, 5, NEW)}


def _listing(world, minutes):
    entries = [
        {"source_player_name": name, "canonical_player_id": player_id, "tricode": "LAL",
         "canonical_status": "Out", "reason": "", "report_time": None}
        for name, player_id in [
            ("Always Five", ALWAYS_FIVE), ("Four Plus Prior", FOUR_PLUS_PRIOR),
            ("Prior Only", PRIOR_ONLY), ("No Games", NO_GAMES), ("Traded", TRADED),
            ("No Id", None),
        ]
    ]
    snapshot = SimpleNamespace(
        normalized_entries=entries,
        retrieved_at=datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc),
    )

    class Snapshots:
        def get_many(self, scopes):
            return {scopes[0]: snapshot}

    return StoredInjuryListing(
        event_catalog=Events(),
        snapshot_repository=Snapshots(),
        settings=world.settings,
        enabled=True,
        clock=lambda: AFTERNOON,
        minutes=minutes,
    )


def _minutes_columns(body):
    return {
        row["player"]: (row["average_minutes"], row["games_played"], row["minutes_season"])
        for row in body["injuries"]
    }


def test_every_injury_row_carries_its_minutes_and_keeps_its_order(world):
    body = _listing(world, _service(world)).list_injuries("2026-01-15", None)

    assert _minutes_columns(body) == {
        "Always Five": (34.0, 5, NEW),
        "Four Plus Prior": (20.0, 3, LAST),
        "Prior Only": (20.0, 2, LAST),
        "No Games": (None, 0, None),
        "Traded": (26.0, 5, NEW),
        "No Id": (None, 0, None),
    }
    assert [row["player"] for row in body["injuries"]] == [
        "Always Five", "Four Plus Prior", "No Games", "No Id", "Prior Only", "Traded",
    ]


def test_an_unreadable_prior_season_does_not_fail_the_list(world):
    world.revoke("player_game_logs", LAST)

    body = _listing(world, _service(world)).list_injuries("2026-01-15", None)

    assert _minutes_columns(body) == {
        "Always Five": (34.0, 5, NEW),
        "Four Plus Prior": (None, 0, None),
        "Prior Only": (None, 0, None),
        "No Games": (None, 0, None),
        "Traded": (26.0, 5, NEW),
        "No Id": (None, 0, None),
    }


def test_the_list_reads_each_season_once_however_many_players_it_has(world):
    reads = []

    @event.listens_for(world.engine, "before_cursor_execute")
    def _count(connection, cursor, statement, *_rest):
        if statement.lstrip().upper().startswith("SELECT") and "publication_player_game_logs.row_payload" in statement.split(" FROM ")[0]:
            reads.append(statement)

    _listing(world, _service(world)).list_injuries("2026-01-15", None)

    assert len(reads) == 2


def test_season_unavailable_is_the_only_error_swallowed(world, monkeypatch):
    service = _service(world)
    monkeypatch.setattr(
        world.logs, "list_players_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError):
        service.minutes([ALWAYS_FIVE])
