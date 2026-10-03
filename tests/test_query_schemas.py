"""The game_logs schema advertises the filters the NL path can now emit."""

from app.config.query_schemas import ENDPOINT_SCHEMAS


def test_game_logs_schema_accepts_an_inclusive_end_date():
    params = ENDPOINT_SCHEMAS["game_logs"]["optional_params"]

    assert params["date_to"]["description"] == "Inclusive end date filter in YYYY-MM-DD format"
