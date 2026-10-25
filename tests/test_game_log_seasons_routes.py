"""``GET /api/games/seasons`` over real Publications (crf04/statsplus#368)."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.domain.publication_integrity import canonical_publication_json
from app.services.game_service import GameService
from tests.services import test_backtest_seasons as seasons

LAST, NEW = seasons.LAST, seasons.NEW


@pytest.fixture
def world(tmp_path):
    built = seasons.World(create_engine(f"sqlite:///{tmp_path / 'seasons.sqlite3'}"))
    yield built
    built.engine.dispose()


@pytest.fixture
def get_seasons(client, dependencies, authenticate, world):
    headers = authenticate()

    def get():
        # Built per call so a test can pin the world's settings first.
        dependencies.game_service = GameService(
            world.engine,
            Mock(),
            settings=world.settings,
            publication_reader=world.reader,
            game_log_seasons=world.logs,
        )
        return client.get("/api/games/seasons", headers=headers)

    return get


def _listing(response):
    """``seasons`` and ``default_season``, unchanged by ``choices`` (crf04/statsplus#330)."""

    body = response.get_json()
    return {key: body[key] for key in ("seasons", "default_season")}


def _publish_logs(world, season, okc_games=1):
    world.compose("player_game_logs", season, {"rows": seasons._season_logs(season, okc_games=okc_games)})


def test_no_publication_lists_no_season_and_still_names_the_default(get_seasons):
    response = get_seasons()

    assert response.status_code == 200
    assert _listing(response) == {"seasons": [], "default_season": "2026-27"}


def test_the_active_season_alone_is_listed_as_the_default(get_seasons, world):
    _publish_logs(world, LAST)

    assert _listing(get_seasons()) == {"seasons": ["2025-26"], "default_season": "2025-26"}


def test_a_retained_season_is_listed_newest_first_beside_the_active_one(get_seasons, world):
    _publish_logs(world, LAST)
    _publish_logs(world, NEW)

    response = get_seasons()
    assert _listing(response) == {
        "seasons": ["2026-27", "2025-26"],
        "default_season": "2026-27",
    }
    assert [(choice["season"], choice["mode"]) for choice in response.get_json()["choices"]] == [
        ("2026-27", "published"),
        ("2025-26", "retained"),
    ]


def test_a_revoked_retained_season_is_not_listed(get_seasons, world):
    _publish_logs(world, LAST)
    _publish_logs(world, NEW)
    world.revoke("player_game_logs", LAST)

    assert _listing(get_seasons()) == {"seasons": ["2026-27"], "default_season": "2026-27"}


def test_a_retired_retained_season_is_not_listed(get_seasons, world):
    _publish_logs(world, LAST)
    _publish_logs(world, NEW)
    world.revoke("player_game_logs", LAST, column="retired_at")

    assert get_seasons().get_json()["seasons"] == ["2026-27"]


def _latest_publication(world, season):
    with world.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT publication_id FROM publication_pointer_history "
                "WHERE stream_key = 'player_game_logs' AND season = :season "
                "ORDER BY fence DESC LIMIT 1"
            ),
            {"season": season},
        ).scalar_one()


def _history_ids(world, season):
    with world.engine.connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT publication_id FROM publication_pointer_history "
                    "WHERE stream_key = 'player_game_logs' AND season = :season "
                    "ORDER BY fence"
                ),
                {"season": season},
            )
            .scalars()
            .all()
        )


def _projected(world, publication_id):
    with world.engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM publication_player_game_logs WHERE publication_id = :p"),
            {"p": publication_id},
        ).scalar_one()


def _drop_projection(world, publication_id):
    with world.engine.begin() as connection:
        connection.execute(
            text("DELETE FROM publication_player_game_logs WHERE publication_id = :p"),
            {"p": publication_id},
        )


def test_a_revoked_active_season_is_not_listed(get_seasons, world):
    _publish_logs(world, LAST)
    _publish_logs(world, NEW)
    world.revoke("player_game_logs", NEW)
    # A real revocation also clears the pointer it withdrew.
    with world.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE publication_pointers SET active_publication_id = NULL "
                "WHERE stream_key = 'player_game_logs'"
            )
        )

    assert get_seasons().get_json()["seasons"] == ["2025-26"]


def test_a_disabled_stream_lists_no_season(get_seasons, world):
    _publish_logs(world, LAST)
    _publish_logs(world, NEW)
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE publication_streams SET enabled = :off " "WHERE stream_key = 'player_game_logs'"),
            {"off": False},
        )

    assert get_seasons().get_json()["seasons"] == []


def test_an_active_season_without_its_projection_is_not_listed(get_seasons, world):
    _publish_logs(world, LAST)
    _publish_logs(world, NEW)
    _drop_projection(world, _latest_publication(world, NEW))

    assert get_seasons().get_json()["seasons"] == ["2025-26"]


def test_a_retained_season_whose_latest_version_lost_its_projection_is_not_listed(get_seasons, world):
    _publish_logs(world, LAST, okc_games=2)
    _publish_logs(world, LAST, okc_games=1)
    _publish_logs(world, NEW)
    older, newer = _history_ids(world, LAST)
    assert older != newer
    # The retained reader serves the newest 2025-26 version, not the older one.
    _drop_projection(world, newer)
    assert _projected(world, older)

    assert get_seasons().get_json()["seasons"] == ["2026-27"]


def test_a_candidate_that_never_activated_is_not_listed(get_seasons, world):
    _publish_logs(world, LAST)
    payload = {"rows": seasons._season_logs(NEW, okc_games=1)}
    with Session(world.engine) as session, session.begin():
        held = world.publications._compose_active_in_session(
            session,
            stream_key="player_game_logs",
            season=NEW,
            cutoff=seasons.NOW,
            encoded=canonical_publication_json(payload),
            payload=payload,
            expected_fence=None,
            reason=None,
            provenance_ids=set(),
            now=seasons.NOW,
            derive_expected_fence_from_lock=True,
        )
        assert held.status == "candidate"

    assert get_seasons().get_json()["seasons"] == ["2025-26"]


def test_a_pin_keeps_the_listing_and_names_the_pinned_default(get_seasons, world):
    _publish_logs(world, LAST)
    _publish_logs(world, NEW)
    seasons._pin(world, LAST)

    assert _listing(get_seasons()) == {
        "seasons": ["2026-27", "2025-26"],
        "default_season": "2025-26",
    }


def test_an_unreadable_publication_store_is_provider_unavailable(client, dependencies, authenticate, world):
    class Broken:
        def published_seasons(self):
            raise OperationalError("select", {}, Exception("connection lost"))

    dependencies.game_service = GameService(
        world.engine, Mock(), settings=world.settings, game_log_seasons=Broken()
    )

    response = client.get("/api/games/seasons", headers=authenticate())

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_seasons_require_authentication(client, monkeypatch):
    from app.utils import auth

    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "false")
    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    response = client.get("/api/games/seasons")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
