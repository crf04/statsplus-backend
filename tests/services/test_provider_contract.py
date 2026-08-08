"""Contract tests between this codebase and nba_api's declared schemas.

Every provider call in the test suite is mocked, so a renamed or removed
upstream column would otherwise go unnoticed until production. nba_api ships an
``expected_data`` schema on each endpoint class; these tests assert that the
columns this codebase drops, reads, or sorts by still exist there.

They are offline: ``expected_data`` is a static class attribute, not a request.
"""

def declared_columns(endpoint_class, data_set):
    """Return the columns nba_api declares for one of an endpoint's data sets."""
    expected = getattr(endpoint_class, "expected_data", None)
    assert expected, f"{endpoint_class.__name__} no longer declares expected_data"
    assert data_set in expected, (
        f"{endpoint_class.__name__} no longer returns a '{data_set}' data set; "
        f"available: {sorted(expected)}"
    )
    return set(expected[data_set])


# --- player game logs ------------------------------------------------------


def test_the_drop_list_is_not_asserted_to_match_the_declared_schema():
    """The drop list intentionally exceeds nba_api's declared columns.

    nba_api's ``expected_data`` lags the live response: it omits columns the
    API does return (AVAILABLE_FLAG, NICKNAME, the WNBA fantasy pair). Because
    every name here is a column we want gone, the drop is applied with
    ``errors='ignore'`` and a divergence is harmless in either direction.
    """
    from nba_api.stats.endpoints import playergamelogs

    from app.services.game_service import GAME_LOG_DROP_COLUMNS

    available = declared_columns(playergamelogs.PlayerGameLogs, "PlayerGameLogs")
    undeclared = set(GAME_LOG_DROP_COLUMNS) - available

    # Guard the assumption above rather than the exact contents: if the drop
    # list ever diverges wildly, it is no longer just provider metadata lag.
    assert len(undeclared) <= 6, f"Unexpectedly large divergence: {sorted(undeclared)}"


def test_required_game_log_columns_all_exist_upstream():
    """These are read directly when deriving PRA, STKS, FG2M and friends."""
    from nba_api.stats.endpoints import playergamelogs

    from app.services.game_service import GAME_LOG_REQUIRED_COLUMNS

    available = declared_columns(playergamelogs.PlayerGameLogs, "PlayerGameLogs")
    missing = sorted(set(GAME_LOG_REQUIRED_COLUMNS) - available)

    assert not missing, f"Game log processing reads columns nba_api no longer returns: {missing}"


def test_dropped_and_required_game_log_columns_do_not_overlap():
    """A column cannot be both discarded and depended on."""
    from app.services.game_service import (
        GAME_LOG_DROP_COLUMNS,
        GAME_LOG_REQUIRED_COLUMNS,
    )

    overlap = sorted(set(GAME_LOG_DROP_COLUMNS) & set(GAME_LOG_REQUIRED_COLUMNS))

    assert not overlap, f"Columns are dropped then read: {overlap}"


def test_the_columns_dropped_from_game_logs_leave_a_usable_frame():
    """Dropping the discard list must not remove everything callers need."""
    from nba_api.stats.endpoints import playergamelogs

    from app.services.game_service import (
        GAME_LOG_DROP_COLUMNS,
        GAME_LOG_REQUIRED_COLUMNS,
    )

    available = declared_columns(playergamelogs.PlayerGameLogs, "PlayerGameLogs")
    remaining = available - set(GAME_LOG_DROP_COLUMNS)

    assert set(GAME_LOG_REQUIRED_COLUMNS).issubset(remaining)


# --- data service provider endpoints --------------------------------------


def test_opponent_shot_endpoint_still_declares_the_columns_we_read():
    """DataService ranks catch-and-shoot and pullup tables on these."""
    from nba_api.stats.endpoints import leaguedashoppptshot

    available = declared_columns(
        leaguedashoppptshot.LeagueDashOppPtShot, "LeagueDashPTShots"
    )
    missing = sorted({"TEAM_ABBREVIATION", "FG3M", "FG2M", "FG2A", "FG3A"} - available)

    assert not missing, f"LeagueDashOppPtShot no longer returns: {missing}"


def test_team_stats_endpoint_declares_a_base_schema_we_can_extend():
    """The OPP_* columns exist only under measure_type='Opponent'.

    nba_api's static schema describes the default Base measure type, so the
    opponent columns cannot be asserted here. Check the identity column the
    code joins on, which is present under every measure type.
    """
    from nba_api.stats.endpoints import leaguedashteamstats

    available = declared_columns(
        leaguedashteamstats.LeagueDashTeamStats, "LeagueDashTeamStats"
    )

    assert "TEAM_NAME" in available
    assert {"BLK", "STL"}.issubset(available)


# --- static reference data -------------------------------------------------


def test_static_team_records_expose_the_fields_the_code_indexes():
    from nba_api.stats.static import teams

    record = teams.get_teams()[0]

    assert {"id", "full_name", "abbreviation", "city", "nickname"}.issubset(record)


def test_static_active_player_records_expose_the_fields_the_code_indexes():
    from nba_api.stats.static import players

    record = players.get_active_players()[0]

    assert {"id", "full_name"}.issubset(record)
