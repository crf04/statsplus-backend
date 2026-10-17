"""``USG_PCT`` on ``/api/games/game_logs`` rows and as a Self Filter (#450).

The route is called as the web app and the MCP call it, over the real
``GameService``, the real ``StoredGameLogsSource`` and a real composed
``player_game_logs`` publication; only the athlete catalog is minimal.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.models.athlete_catalog import AthleteCatalog
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.database_first_activation import decode_player_game_logs
from app.services.game_logs_source import StoredGameLogsSource
from app.services.game_service import GameService
from tests.services import test_backtest_seasons as seasons

NEW = seasons.NEW
URL = "/api/games/game_logs?player_name=LeBron+James&season_filter=" + NEW

#: One value per game, oldest first; ``MISSING`` stands for a publication row
#: composed before the backfill, which has no ``usage_pct`` key at all.
MISSING = object()
USAGE_BY_GAME = [28.57, 18.0, 41.5, MISSING, 25.0]


@pytest.fixture
def world(tmp_path):
    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'usage.sqlite3'}"))
    rows = seasons._season_logs(NEW, okc_games=0)
    for row, usage in zip(rows, USAGE_BY_GAME, strict=True):
        if usage is MISSING:
            row.pop("usage_pct", None)
        else:
            row["usage_pct"] = usage
    built.compose("player_game_logs", NEW, {"rows": rows})
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
    yield built
    built.engine.dispose()


@pytest.fixture
def served(dependencies, world):
    settings = world.settings.model_copy(
        update={"cache": world.settings.cache.model_copy(update={"enabled": False})}
    )
    dependencies.game_service = GameService(
        world.engine,
        Mock(),
        settings=settings,
        game_logs_source=StoredGameLogsSource(world.logs),
        publication_reader=world.reader,
        game_log_seasons=world.logs,
        athlete_catalog=AthleteCatalogReader(world.engine),
    )


def _usage(response):
    assert response.status_code == 200
    return [(row["GAME_DATE"], row["USG_PCT"]) for row in response.get_json()["game_logs"]]


def test_every_row_carries_usg_pct_and_a_row_without_a_stored_value_is_null(served, client):
    # Newest first; the fourth game's publication row has no usage_pct key.
    assert _usage(client.get(URL)) == [
        ("2026-10-26", 25.0),
        ("2026-10-25", None),
        ("2026-10-24", 41.5),
        ("2026-10-23", 18.0),
        ("2026-10-22", 28.57),
    ]


def test_a_usg_pct_range_keeps_only_the_games_inside_it(served, client):
    assert _usage(client.get(URL + "&self_filters[USG_PCT]=25,40")) == [
        ("2026-10-26", 25.0),
        ("2026-10-22", 28.57),
    ]


def test_a_game_with_no_usage_fails_any_usg_pct_range(served, client):
    assert _usage(client.get(URL + "&self_filters[USG_PCT]=0,100")) == [
        ("2026-10-26", 25.0),
        ("2026-10-24", 41.5),
        ("2026-10-23", 18.0),
        ("2026-10-22", 28.57),
    ]


def test_a_publication_with_no_usage_anywhere_serves_null_and_a_filter_matches_nothing(
    dependencies, tmp_path, client
):
    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}"))
    rows = seasons._season_logs(NEW, okc_games=0)
    for row in rows:
        row.pop("usage_pct", None)
    built.compose("player_game_logs", NEW, {"rows": rows})
    with built.engine.begin() as connection:
        connection.execute(AthleteCatalog.__table__.insert(), {
            "season": NEW, "player_id": seasons.bt.LEBRON,
            "display_name": "LeBron James", "roster_status": "active",
            "is_active": True, "is_active_for_season": True,
            "published_at": seasons.NOW,
        })
    settings = built.settings.model_copy(
        update={"cache": built.settings.cache.model_copy(update={"enabled": False})}
    )
    dependencies.game_service = GameService(
        built.engine, Mock(), settings=settings,
        game_logs_source=StoredGameLogsSource(built.logs),
        publication_reader=built.reader, game_log_seasons=built.logs,
        athlete_catalog=AthleteCatalogReader(built.engine),
    )

    try:
        assert {value for _date, value in _usage(client.get(URL))} == {None}
        assert _usage(client.get(URL + "&self_filters[USG_PCT]=0,100")) == []
    finally:
        built.engine.dispose()


def _publication_row(**extra):
    row = seasons._season_logs(NEW, okc_games=0)[0]
    row.pop("usage_pct", None)
    return {**row, **extra}


def test_decoding_an_older_publication_row_without_usage_pct_gives_none():
    [record] = decode_player_game_logs([_publication_row()], season=NEW)

    assert record.usage_pct is None


def test_decoding_keeps_the_published_usage_unrounded():
    [record] = decode_player_game_logs([_publication_row(usage_pct=28.5714)], season=NEW)

    assert record.usage_pct == 28.5714


@pytest.mark.parametrize("bad", ["28.5", True, -1, 100.5])
def test_decoding_rejects_a_malformed_usage_pct(bad):
    from app.services.database_first_activation import PublicationPayloadError

    with pytest.raises(PublicationPayloadError):
        decode_player_game_logs([_publication_row(usage_pct=bad)], season=NEW)

