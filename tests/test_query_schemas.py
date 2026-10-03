"""The game_logs schema advertises filters the game-log query actually accepts."""

from datetime import date

import pytest

from app.config.query_schemas import ENDPOINT_SCHEMAS
from app.models.game_logs import GameLogQuery


@pytest.mark.parametrize("name", ["date_filter", "date_to"])
def test_game_logs_schema_date_examples_parse_as_query_dates(name):
    example = ENDPOINT_SCHEMAS["game_logs"]["optional_params"][name]["example"]

    query = GameLogQuery(season_filter="2025-26", **{name: example})

    assert getattr(query, name) == date.fromisoformat(example)
