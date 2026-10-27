"""Rebound chances and potential assists on ``/api/games/game_logs`` rows (crf04/statsplus#349).

Called as the web app and the MCP call it, over the real ``GameService``, the
real ``StoredGameLogsSource`` and a real composed ``player_game_logs``
publication; the tracking rows go in through the real repository.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.models.athlete_catalog import AthleteCatalog
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from app.services.player_tracking_repository import PlayerTrackingRepository
from tests.services import test_backtest_seasons as seasons

NEW = seasons.NEW
URL = "/api/games/game_logs?player_name=LeBron+James&season_filter=" + NEW
FETCHED = datetime(2026, 10, 27, 10, tzinfo=UTC)


def _stored(counts):
    return {
        "reb_chances": counts[0],
        "oreb_chances": counts[1],
        "dreb_chances": counts[2],
        "potential_ast": counts[3],
    }


@pytest.fixture
def world(tmp_path):
    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'tracking.sqlite3'}"))
    built.compose("player_game_logs", NEW, {"rows": seasons._season_logs(NEW, okc_games=0)})
    with built.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert(),
            {
                "season": NEW,
                "player_id": seasons.bt.LEBRON,
                "display_name": "LeBron James",
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "published_at": seasons.NOW,
            },
        )
    tracking = PlayerTrackingRepository(built.engine)
    # Newest game (10-26) has no tracking yet; 10-25 is collected but the day
    # feed lacked him (potential assists null); 10-24 holds a real zero.
    for game_id, day, counts in (
        ("0022600000", date(2026, 10, 22), (9, 4, 5, 12)),
        ("0022600001", date(2026, 10, 23), (3, 1, 2, 8)),
        ("0022600002", date(2026, 10, 24), (0, 0, 0, 0)),
        ("0022600003", date(2026, 10, 25), (6, 2, 4, None)),
    ):
        tracking.replace_game(
            NEW,
            game_id,
            day,
            [
                {"player_id": seasons.bt.LEBRON, **_stored(counts)},
                {"player_id": 999, **_stored((1, 1, 0, 1))},
            ],
            FETCHED,
        )
    yield built, tracking
    built.engine.dispose()


@pytest.fixture
def served(dependencies, world):
    built, tracking = world
    settings = built.settings.model_copy(
        update={"cache": built.settings.cache.model_copy(update={"enabled": False})}
    )
    dependencies.game_service = GameService(
        built.engine,
        Mock(),
        settings=settings,
        game_logs_source=StoredGameLogsSource(built.logs, tracking=tracking),
        publication_reader=built.reader,
        game_log_seasons=built.logs,
        athlete_catalog=AthleteCatalogReader(built.engine),
    )


def _rows(response, *keys):
    assert response.status_code == 200
    return [(row["GAME_DATE"], *(row[key] for key in keys)) for row in response.get_json()["game_logs"]]


ALL = ("REB_CHANCES", "OREB_CHANCES", "DREB_CHANCES", "POTENTIAL_AST")


def test_every_row_carries_the_four_integer_or_null_keys(served, client):
    # Newest first; 10-26 was never collected.
    assert _rows(client.get(URL), *ALL) == [
        ("2026-10-26", None, None, None, None),
        ("2026-10-25", 6, 2, 4, None),
        ("2026-10-24", 0, 0, 0, 0),
        ("2026-10-23", 3, 1, 2, 8),
        ("2026-10-22", 9, 4, 5, 12),
    ]


def test_values_are_json_integers_not_floats(served, client):
    row = client.get(URL).get_json()["game_logs"][-1]

    assert row["REB_CHANCES"] == 9 and isinstance(row["REB_CHANCES"], int)
    assert row["POTENTIAL_AST"] == 12 and isinstance(row["POTENTIAL_AST"], int)


def test_a_potential_assists_range_keeps_only_games_inside_it(served, client):
    assert _rows(client.get(URL + "&self_filters[POTENTIAL_AST]=8,20"), "POTENTIAL_AST") == [
        ("2026-10-23", 8),
        ("2026-10-22", 12),
    ]


def test_a_rebound_chances_range_keeps_only_games_inside_it(served, client):
    assert _rows(client.get(URL + "&self_filters[REB_CHANCES]=3,6"), "REB_CHANCES") == [
        ("2026-10-25", 6),
        ("2026-10-23", 3),
    ]


def test_a_null_value_fails_any_range_and_a_real_zero_does_not(served, client):
    assert _rows(client.get(URL + "&self_filters[POTENTIAL_AST]=0,100"), "POTENTIAL_AST") == [
        ("2026-10-24", 0),
        ("2026-10-23", 8),
        ("2026-10-22", 12),
    ]


def test_without_a_tracking_store_every_row_serves_null_and_a_filter_matches_nothing(
    dependencies, world, client
):
    built, _tracking = world
    dependencies.game_service = GameService(
        built.engine,
        Mock(),
        settings=built.settings.model_copy(
            update={"cache": built.settings.cache.model_copy(update={"enabled": False})}
        ),
        game_logs_source=StoredGameLogsSource(built.logs),
        publication_reader=built.reader,
        game_log_seasons=built.logs,
        athlete_catalog=AthleteCatalogReader(built.engine),
    )

    assert {value for _date, value in _rows(client.get(URL), "POTENTIAL_AST")} == {None}
    assert _rows(client.get(URL + "&self_filters[POTENTIAL_AST]=0,100"), "POTENTIAL_AST") == []


def test_the_batched_read_joins_the_same_values(world):
    built, tracking = world
    source = StoredGameLogsSource(built.logs, tracking=tracking)

    frames = source.get_players_logs([seasons.bt.LEBRON, 12345], NEW)

    lebron = frames[seasons.bt.LEBRON].sort_values("GAME_DATE")
    assert lebron["REB_CHANCES"].tolist()[:4] == [9, 3, 0, 6]
    assert lebron["POTENTIAL_AST"].isna().tolist() == [False, False, False, True, True]
    assert frames[12345].empty


def _serve_custom(dependencies, tmp_path, rows, tracked, player_id, name):
    """A world holding ``rows`` and the ``tracked`` (game_id, date, counts) tuples for ``player_id``."""

    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'custom.sqlite3'}"))
    built.compose("player_game_logs", NEW, {"rows": rows})
    with built.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert(),
            {
                "season": NEW,
                "player_id": player_id,
                "display_name": name,
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "published_at": seasons.NOW,
            },
        )
    tracking = PlayerTrackingRepository(built.engine)
    for game_id, day, counts in tracked:
        tracking.replace_game(NEW, game_id, day, [{"player_id": player_id, **_stored(counts)}], FETCHED)
    dependencies.game_service = GameService(
        built.engine,
        Mock(),
        settings=built.settings.model_copy(
            update={"cache": built.settings.cache.model_copy(update={"enabled": False})}
        ),
        game_logs_source=StoredGameLogsSource(built.logs, tracking=tracking),
        publication_reader=built.reader,
        game_log_seasons=built.logs,
        athlete_catalog=AthleteCatalogReader(built.engine),
    )
    return built


def test_walter_serves_zero_rebounds_and_two_chances_in_the_same_row(dependencies, tmp_path, client):
    # TOR vs BKN, game 0022501192: Ja'Kobe Walter had 0 REB, 2 chances, 1 AST, 2 potential assists.
    walter = 1642266
    row = seasons._log(walter, "Ja'Kobe Walter", NEW, "0022501192", date(2026, 4, 12), seasons.bt.BOS, "BKN")
    row.update(rebounds=0, assists=1)
    built = _serve_custom(
        dependencies,
        tmp_path,
        [row],
        [("0022501192", date(2026, 4, 12), (2, 0, 2, 2))],
        walter,
        "Ja'Kobe Walter",
    )
    try:
        response = client.get("/api/games/game_logs?player_name=Ja%27Kobe+Walter&season_filter=" + NEW)
        [served_row] = response.get_json()["game_logs"]
    finally:
        built.engine.dispose()

    assert (served_row["REB"], served_row["REB_CHANCES"]) == (0, 2)
    assert (served_row["REB_CONV_PCT"], served_row["AST_CONV_PCT"]) == (0.0, 0.5)
    assert served_row["POTENTIAL_AST"] == 2


@pytest.fixture
def conversion_world(dependencies, tmp_path):
    """Chances [10, 20, null] against REB [5, 15, 9] and AST [2, 0, 4] / potential [4, 0, null]."""

    rows = seasons._season_logs(NEW, okc_games=0)[:3]
    for row, rebounds, assists in zip(rows, (5, 15, 9), (2, 0, 4), strict=True):
        row.update(rebounds=rebounds, assists=assists)
    tracked = [
        ("0022600000", date(2026, 10, 22), (10, 0, 10, 4)),
        ("0022600001", date(2026, 10, 23), (20, 0, 20, 0)),
    ]
    built = _serve_custom(dependencies, tmp_path, rows, tracked, seasons.bt.LEBRON, "LeBron James")
    yield
    built.engine.dispose()


def test_conversion_rates_are_per_game_ratios_and_null_without_chances(conversion_world, client):
    # Newest first: 10-24 untracked, 10-23 has 0 potential assists, 10-22 is 5/10 and 2/4.
    assert _rows(client.get(URL), "REB_CONV_PCT", "AST_CONV_PCT") == [
        ("2026-10-24", None, None),
        ("2026-10-23", 0.75, None),
        ("2026-10-22", 0.5, 0.5),
    ]


def test_conversion_ranges_use_the_zero_to_one_scale_and_null_fails_them(conversion_world, client):
    assert _rows(client.get(URL + "&self_filters[REB_CONV_PCT]=0.6,1"), "REB_CONV_PCT") == [
        ("2026-10-23", 0.75)
    ]
    assert _rows(client.get(URL + "&self_filters[AST_CONV_PCT]=0,1"), "AST_CONV_PCT") == [("2026-10-22", 0.5)]


def test_averages_are_a_ratio_of_sums_over_tracked_games(conversion_world, client):
    body = client.get(URL).get_json()

    for block in (body["averages"][0], body["season_averages"][0]):
        # REB [5, 15] over chances [10, 20] is 20/30, not the mean of 0.5 and 0.75.
        assert (block["REB_CHANCES"], block["REB_CONV_PCT"]) == (15.0, 0.667)
        # Potential assists [4, 0]: the zero is a tracked game; AST [2, 0] over 4 is 0.5.
        assert (block["POTENTIAL_AST"], block["AST_CONV_PCT"]) == (2.0, 0.5)


def test_averages_are_null_when_no_game_is_tracked(dependencies, tmp_path, client):
    built = _serve_custom(
        dependencies, tmp_path, seasons._season_logs(NEW, okc_games=0), [], seasons.bt.LEBRON, "LeBron James"
    )
    try:
        body = client.get(URL).get_json()
    finally:
        built.engine.dispose()

    for key in ("REB_CHANCES", "REB_CONV_PCT", "POTENTIAL_AST", "AST_CONV_PCT"):
        assert body["averages"][0][key] is None and body["season_averages"][0][key] is None
