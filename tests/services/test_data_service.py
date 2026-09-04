"""Offline tests for DataService table-building logic.

Provider calls are replaced at the private fetch helpers, so these exercise the
transformation and persistence logic rather than nba_api itself. Every test
writes to a temporary SQLite database.
"""

import pandas as pd
import pytest

PLAY_TYPES = [
    "Transition", "Isolation", "PRBallHandler", "PRRollMan", "OffRebound",
    "Spotup", "Cut", "Handoff", "OffScreen", "Misc", "Postup",
]


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


class _FakeProvider:
    """Stand-in for the PBP Stats adapter."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def get_totals(self, data_type):
        self.calls.append(data_type)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_pbp_data_is_stored_for_players(service, engine):
    service.pbp_provider = _FakeProvider(
        pd.DataFrame([{"Name": "LeBron James", "Points": 30}])
    )

    assert service.fetch_PBP_data() is True
    assert read_table(engine, "pbp_player_stats")["Name"].tolist() == ["LeBron James"]
    assert service.pbp_provider.calls == ["player"]


def test_pbp_data_requests_opponent_totals_for_the_opponent_type(service, engine):
    service.pbp_provider = _FakeProvider(pd.DataFrame([{"Name": "LAL"}]))

    assert service.fetch_PBP_data(data_type="Opponent") is True
    assert service.pbp_provider.calls == ["Opponent"]
    assert read_table(engine, "pbp_Opponent_stats")["Name"].tolist() == ["LAL"]


def test_pbp_storage_failures_are_reported_without_raising(service):
    """A local failure keeps the boolean contract."""
    service.pbp_provider = _FakeProvider(ValueError("malformed payload"))

    assert service.fetch_PBP_data() is False


def test_pbp_provider_outages_propagate_to_the_error_handler(service):
    """Provider unavailability is translated to HTTP by the route boundary,
    so it must not be flattened into a False return."""
    from app.errors import ProviderUnavailableError

    service.pbp_provider = _FakeProvider(
        ProviderUnavailableError("pbpstats is unavailable.")
    )

    with pytest.raises(ProviderUnavailableError):
        service.fetch_PBP_data()


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
    monkeypatch.setattr(
        service,
        "_collect_all_frames",
        lambda: {"player_information": pd.DataFrame([{"id": 1}])},
    )
    monkeypatch.setattr(service.publisher, "publish", lambda *args, **kwargs: None)

    assert service.update_all_data() is True


def test_update_all_data_reports_failure_when_a_step_raises(service, monkeypatch):
    def boom():
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(service, "_collect_all_frames", boom)

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
