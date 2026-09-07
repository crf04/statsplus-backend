"""Where the natural-language parser gets its recognisable player names.

Entity matching used to read ``player_play_types``, so only players who
accumulated a play-type statistic were resolvable.  The governed athlete
catalog names the whole season's roster, and is the surface the nightly
refresh keeps current.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import load_settings
from app.services.nl_query.parser import BaseQueryParser


CATALOG_ONLY = "Catalog Only Rookie"
PLAY_TYPES_ONLY = "Play Types Only Veteran"
IN_BOTH = "LeBron James"


@pytest.fixture
def engine_with_both_sources():
    """Seed the two candidate name sources so they cannot be confused."""

    settings = load_settings()
    season = settings.nba.current_season
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE athlete_catalog (season TEXT, display_name TEXT)")
        )
        connection.execute(
            text('CREATE TABLE player_play_types ("PLAYER_NAME" TEXT)')
        )
        for display_name in (CATALOG_ONLY, IN_BOTH):
            connection.execute(
                text(
                    "INSERT INTO athlete_catalog (season, display_name) "
                    "VALUES (:season, :display_name)"
                ),
                {"season": season, "display_name": display_name},
            )
        # A name only the retired source knows, and one from another season the
        # current-season parser must not pick up.
        connection.execute(
            text(
                "INSERT INTO athlete_catalog (season, display_name) "
                "VALUES ('1999-00', 'Wrong Season Player')"
            )
        )
        for player_name in (PLAY_TYPES_ONLY, IN_BOTH):
            connection.execute(
                text('INSERT INTO player_play_types ("PLAYER_NAME") VALUES (:name)'),
                {"name": player_name},
            )
    try:
        yield engine, settings
    finally:
        engine.dispose()


def test_player_names_come_from_the_athlete_catalog(engine_with_both_sources):
    engine, settings = engine_with_both_sources

    parser = BaseQueryParser(engine, settings)

    assert CATALOG_ONLY in parser.players
    assert IN_BOTH in parser.players
    # The retired source is not consulted, so a name only it knows is absent.
    assert PLAY_TYPES_ONLY not in parser.players


def test_player_names_are_scoped_to_the_current_season(engine_with_both_sources):
    engine, settings = engine_with_both_sources

    parser = BaseQueryParser(engine, settings)

    assert "Wrong Season Player" not in parser.players


def test_a_missing_catalog_degrades_to_no_names_rather_than_failing(tmp_path):
    """A parser still constructs when the catalog is unreadable."""

    engine = create_engine("sqlite://")
    try:
        parser = BaseQueryParser(engine, load_settings())
        assert parser.players == []
    finally:
        engine.dispose()
