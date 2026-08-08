"""Offline tests for DataService table-building logic.

Provider calls are replaced at the private fetch helpers, so these exercise the
transformation and persistence logic rather than nba_api itself. Every test
writes to a temporary SQLite database.
"""

import pandas as pd
import pytest
import requests

PLAY_TYPES = [
    "Transition", "Isolation", "PRBallHandler", "PRRollMan", "OffRebound",
    "Spotup", "Cut", "Handoff", "OffScreen", "Misc", "Postup",
]

TEAM_NAMES = ["Los Angeles Lakers", "Golden State Warriors"]


@pytest.fixture
def engine(tmp_path):
    from sqlalchemy import create_engine

    return create_engine(f"sqlite:///{tmp_path / 'data.db'}")


@pytest.fixture
def service(engine):
    from app.config.settings import load_settings
    from app.services.data_service import DataService

    return DataService(engine, settings=load_settings())


def read_table(engine, name):
    with engine.connect() as connection:
        return pd.read_sql(f"SELECT * FROM {name}", connection)


# --- opponent scoring ------------------------------------------------------


def test_opponent_scoring_is_stored(service, engine, monkeypatch):
    frame = pd.DataFrame([{"TEAM_NAME": "Los Angeles Lakers", "OPP_PTS": 110.5}])
    monkeypatch.setattr(service, "_fetch_opponent_data", lambda *a, **k: frame)

    service.process_opponent_scoring()

    assert read_table(engine, "general_opponent_stats")["OPP_PTS"].tolist() == [110.5]


# --- opponent shooting -----------------------------------------------------


def _shooting_frame():
    return pd.DataFrame(
        [
            {"TEAM_ABBREVIATION": "LAL", "FG3M": 10, "FG2M": 20, "FG2A": 40, "FG3A": 30},
            {"TEAM_ABBREVIATION": "GSW", "FG3M": 14, "FG2M": 18, "FG2A": 36, "FG3A": 35},
        ]
    )


def test_opponent_shooting_writes_each_normalized_table(service, engine, monkeypatch):
    monkeypatch.setattr(
        service, "_fetch_opp_shooting_data", lambda type, date_filter=None: _shooting_frame()
    )

    service.process_opp_shooting()

    for table in ("catch_and_shoot", "pullups", "less_than_10_ft"):
        df = read_table(engine, table)
        assert len(df) == 2
        # Ranks are ascending, so the lower FG3M ranks first.
        assert df.loc[df["TEAM_ABBREVIATION"] == "LAL", "FG3M_RANK"].item() == 1
        assert df.loc[df["TEAM_ABBREVIATION"] == "GSW", "FG3M_RANK"].item() == 2


def test_opponent_shooting_continues_when_one_type_fails(service, engine, monkeypatch):
    def fetch(type, date_filter=None):
        if type == "Pullups":
            raise RuntimeError("provider exploded")
        return _shooting_frame()

    monkeypatch.setattr(service, "_fetch_opp_shooting_data", fetch)

    service.process_opp_shooting()

    assert len(read_table(engine, "catch_and_shoot")) == 2
    assert len(read_table(engine, "less_than_10_ft")) == 2
    with pytest.raises(Exception):
        read_table(engine, "pullups")


# --- team play types -------------------------------------------------------


def _team_play_type_frame(play_type):
    return pd.DataFrame(
        [
            {"TEAM_NAME": name, "PLAY_TYPE": play_type, "PTS": 100 + index * 20, "GP": 10}
            for index, name in enumerate(TEAM_NAMES)
        ]
    )


def test_team_play_types_are_pivoted_and_normalized(service, engine, monkeypatch):
    monkeypatch.setattr(service, "_fetch_team_play_type_data", _team_play_type_frame)

    service.process_and_store_team_data()

    df = read_table(engine, "team_play_types")

    assert list(df.columns) == [
        "TEAM_NAME", "Cut", "Isolation", "PRRollMan", "PRBallHandler",
        "OffRebound", "Spotup", "Handoff", "OffScreen", "Misc",
        "Postup", "Transition", "Team_ID", "team",
    ]
    assert set(df["team"]) == {"LAL", "GSW"}
    # PTS/G+ is each team's rate over the league mean, so the two teams'
    # normalized values must average to 1.0 for every play type.
    assert df["Transition"].mean() == pytest.approx(1.0)


def test_team_play_types_skip_a_failing_play_type(service, engine, monkeypatch):
    def fetch(play_type):
        if play_type == "Postup":
            raise RuntimeError("provider exploded")
        return _team_play_type_frame(play_type)

    monkeypatch.setattr(service, "_fetch_team_play_type_data", fetch)

    with pytest.raises(KeyError):
        # The fixed column order requires every play type, so a dropped one
        # surfaces rather than silently writing a narrower table.
        service.process_and_store_team_data()


def test_unknown_team_names_map_to_unknown(service):
    assert service._nba_team_to_abbreviation("Los Angeles Lakers") == "LAL"
    assert service._nba_team_to_abbreviation("Springfield Isotopes") == "Unknown"


# --- player play types -----------------------------------------------------


def test_player_playstyles_become_percentages(service, engine, monkeypatch):
    def fetch(play_type):
        return pd.DataFrame(
            [
                {
                    "PLAYER_NAME": "LeBron James",
                    "TEAM_ABBREVIATION": "LAL",
                    "PLAY_TYPE": play_type,
                    "PTS": 10,
                }
            ]
        )

    monkeypatch.setattr(service, "_fetch_play_type_data", fetch)

    service.process_playstyles()

    df = read_table(engine, "player_play_types")
    percentage_columns = [f"{play_type}%" for play_type in PLAY_TYPES]

    assert set(percentage_columns).issubset(df.columns)
    # Raw play-type columns are replaced by their percentage equivalents.
    assert not set(PLAY_TYPES).intersection(df.columns)
    assert df[percentage_columns].sum(axis=1).item() == pytest.approx(100.0)


# --- play-by-play ----------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _install_session(monkeypatch, session):
    from app.services import data_service as data_service_module

    monkeypatch.setattr(
        data_service_module, "get_shared_nba_session", lambda *a, **k: session
    )


def test_pbp_data_is_stored_for_players(service, engine, monkeypatch):
    session = _FakeSession(
        _FakeResponse({"multi_row_table_data": [{"Name": "LeBron James", "Points": 30}]})
    )
    _install_session(monkeypatch, session)

    assert service.fetch_PBP_data() is True
    assert read_table(engine, "pbp_player_stats")["Name"].tolist() == ["LeBron James"]
    assert session.calls[0]["params"]["Type"] == "Player"


def test_pbp_data_requests_opponent_totals_for_the_opponent_type(
    service, engine, monkeypatch
):
    session = _FakeSession(_FakeResponse({"multi_row_table_data": [{"Name": "LAL"}]}))
    _install_session(monkeypatch, session)

    assert service.fetch_PBP_data(data_type="Opponent") is True
    assert session.calls[0]["params"]["Type"] == "Opponent"
    assert read_table(engine, "pbp_Opponent_stats")["Name"].tolist() == ["LAL"]


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.Timeout("pbpstats timed out"),
        requests.exceptions.ConnectionError("pbpstats unreachable"),
        ValueError("malformed payload"),
    ],
)
def test_pbp_failures_are_reported_without_raising(service, monkeypatch, error):
    _install_session(monkeypatch, _FakeSession(error))

    assert service.fetch_PBP_data() is False


# --- player information ----------------------------------------------------


def test_player_information_is_stored_and_returned(service, engine, monkeypatch):
    from app.services import data_service as data_service_module

    monkeypatch.setattr(
        data_service_module.players,
        "get_active_players",
        lambda: [{"id": 2544, "full_name": "LeBron James"}],
    )

    result = service.store_player_information()

    assert result == [{"id": 2544, "full_name": "LeBron James"}]
    assert read_table(engine, "player_information")["full_name"].tolist() == [
        "LeBron James"
    ]


def test_player_information_failure_is_reported(service, monkeypatch):
    from app.services import data_service as data_service_module

    def boom():
        raise RuntimeError("nba_api is down")

    monkeypatch.setattr(data_service_module.players, "get_active_players", boom)

    assert service.store_player_information() is False


def test_per36_stats_are_stored(service, engine, monkeypatch):
    frame = pd.DataFrame([{"PLAYER_NAME": "LeBron James", "PTS": 27.1}])
    monkeypatch.setattr(service, "_fetch_player_per36_stats", lambda: frame)

    assert service.store_player_per36_stats() is True
    assert read_table(engine, "player_per36_stats")["PTS"].tolist() == [27.1]


def test_per36_failure_is_reported(service, monkeypatch):
    def boom():
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(service, "_fetch_player_per36_stats", boom)

    assert service.store_player_per36_stats() is False


# --- orchestration ---------------------------------------------------------


def test_update_all_data_reports_success_when_every_step_passes(service, monkeypatch):
    steps = [
        "store_player_information", "process_opponent_scoring",
        "store_player_per36_stats", "process_and_store_team_data",
        "process_opp_shooting", "process_opp_shooting_zone", "process_playstyles",
        "process_player_zone", "fetch_PBP_data", "process_assist_data",
    ]
    for step in steps:
        monkeypatch.setattr(service, step, lambda *a, **k: True)

    assert service.update_all_data() is True


def test_update_all_data_reports_failure_when_a_step_raises(service, monkeypatch):
    monkeypatch.setattr(service, "store_player_information", lambda: True)

    def boom():
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(service, "process_opponent_scoring", boom)

    assert service.update_all_data() is False


# --- helpers ---------------------------------------------------------------


def test_table_reads_normalize_legacy_names(service, engine):
    pd.DataFrame([{"Name": "LAL"}]).to_sql(
        "pbp_opponent_stats", engine, index=False, if_exists="replace"
    )

    # The legacy mixed-case name must resolve to the snake_case table.
    df = service._fetch_data_from_table("pbp_Opponent_stats")

    assert df["Name"].tolist() == ["LAL"]


def test_team_info_is_saved(service, engine):
    service.save_team()

    df = read_table(engine, "team_info")
    assert "abbreviation" in df.columns
    assert "LAL" in df["abbreviation"].tolist()


def test_player_id_lookup_raises_for_an_unknown_player(service):
    with pytest.raises(ValueError, match="Player not found"):
        service._get_player_id("Nonexistent Person")


def test_player_id_lookup_resolves_a_known_player(service):
    assert service._get_player_id("LeBron James") == 2544
