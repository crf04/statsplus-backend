"""An explicit past ``season_filter`` reads the season's retained Publication.

crf04/statsplus#368: ``/api/games/seasons`` lists every season a game-log read
can serve, so Search must be able to read a listed past season after a newer
season activates -- logs, averages and provenance from the retained generation.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text

from app.models.athlete_catalog import AthleteCatalog
from app.models.game_logs import GameLogQuery
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from tests.services import test_backtest_seasons as seasons

LAST, NEW = seasons.LAST, seasons.NEW


@pytest.fixture
def world(tmp_path):
    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'logs.sqlite3'}"))
    for season in (LAST, NEW):
        built.compose("player_game_logs", season, {"rows": seasons._season_logs(season, okc_games=1)})
        with built.engine.begin() as connection:
            connection.execute(
                AthleteCatalog.__table__.insert(),
                {
                    "season": season,
                    "player_id": seasons.bt.LEBRON,
                    "display_name": "LeBron James",
                    "roster_status": "active",
                    "is_active": True,
                    "is_active_for_season": True,
                    "published_at": seasons.NOW,
                },
            )
    yield built
    built.engine.dispose()


@pytest.fixture
def service(world):
    settings = world.settings.model_copy(
        update={"cache": world.settings.cache.model_copy(update={"enabled": False})}
    )
    return GameService(
        world.engine,
        Mock(),
        settings=settings,
        game_logs_source=StoredGameLogsSource(world.logs),
        publication_reader=world.reader,
        game_log_seasons=world.logs,
        athlete_catalog=AthleteCatalogReader(world.engine),
    )


def _history(world, season):
    with world.engine.connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT publication_id FROM publication_pointer_history "
                    "WHERE stream_key = 'player_game_logs' AND season = :s ORDER BY fence"
                ),
                {"s": season},
            )
            .scalars()
            .all()
        )


def _read(service, season):
    body = service.get_filtered_logs("LeBron James", GameLogQuery(season_filter=season))
    generation = {g["stream_key"]: g for g in body["provenance"]["generation"]}
    return body, generation["player_game_logs"]


def test_a_retained_season_reads_its_own_rows_averages_and_publication(service, world):
    last, last_pub = _read(service, LAST)
    new, new_pub = _read(service, NEW)

    assert (len(last["game_logs"]), last["season_game_count"]) == (5, 5)
    assert last["season_averages"][0]["PTS"] == 30.0
    assert (last_pub["season"], last_pub["unavailable_reason"]) == (LAST, None)
    assert last_pub["publication_id"] == _history(world, LAST)[-1]
    assert (len(new["game_logs"]), new["season_averages"][0]["PTS"]) == (5, 12.0)
    assert (new_pub["season"], new_pub["status"]) == (NEW, "active")


@pytest.mark.parametrize("column", ["revoked_at", "retired_at"])
def test_a_revoked_or_retired_retained_season_reads_nothing(service, world, column):
    world.revoke("player_game_logs", LAST, column=column)

    body, publication = _read(service, LAST)

    assert body["game_logs"] == []
    assert body["season_game_count"] == 0
    assert publication["unavailable_reason"] == "retained_publication_missing"
    assert LAST not in service.list_seasons()["seasons"]


def test_a_disabled_stream_reads_nothing_and_lists_nothing(service, world):
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE publication_streams SET enabled = :off " "WHERE stream_key = 'player_game_logs'"),
            {"off": False},
        )

    assert _read(service, LAST)[0]["game_logs"] == []
    assert service.list_seasons()["seasons"] == []


def test_a_season_is_listed_if_and_only_if_its_read_succeeds(service, world):
    world.revoke("player_game_logs", LAST)
    listed = service.list_seasons()["seasons"]

    for season in (LAST, NEW):
        assert (season in listed) == bool(_read(service, season)[0]["game_logs"])


def test_a_season_later_than_the_active_one_stays_unavailable(service, world):
    with world.engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert(),
            {
                "season": "2027-28",
                "player_id": seasons.bt.LEBRON,
                "display_name": "LeBron James",
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "published_at": seasons.NOW,
            },
        )

    body, publication = _read(service, "2027-28")

    assert body["game_logs"] == []
    assert publication["unavailable_reason"] == "window_not_ready"
