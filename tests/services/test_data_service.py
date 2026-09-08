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


def test_the_nightly_refresh_no_longer_collects_player_play_types(
    service, monkeypatch
):
    """The nightly pays no NBA Stats request for a table nothing reads.

    ``player_play_types`` had one reader, the natural-language player-name
    list, and that now reads the governed athlete catalog.  Dropping it from
    the collector set is what removes the request, so assert on the collector
    rather than on the published set.
    """

    collected = []

    def _record(name, value):
        def build():
            collected.append(name)
            return value

        return build

    for attribute, table_name in (
        ("_collect_player_information", "player_information"),
        ("_fetch_player_per36_stats", "player_per36_stats"),
        ("_collect_opp_shooting_zone", "opp_shooting_zone"),
        ("_collect_playtypes_frame", "player_play_types"),
        ("_collect_player_zone", "player_shooting_zones"),
    ):
        monkeypatch.setattr(
            service,
            attribute,
            _record(table_name, pd.DataFrame([{"value": "new"}])),
        )
    monkeypatch.setattr(
        service,
        "_collect_pbp_frame",
        lambda kind: _record("pbp_opponent_stats", pd.DataFrame([{"v": 1}]))(),
    )
    monkeypatch.setattr(service.publisher, "publish", lambda *a, **k: None)

    assert service.update_all_data() is True

    assert "player_play_types" not in collected
    assert sorted(collected) == sorted(
        [
            "player_information",
            "player_per36_stats",
            "opp_shooting_zone",
            "player_shooting_zones",
            "pbp_opponent_stats",
        ]
    )


def test_the_on_demand_play_type_seam_still_collects_and_publishes(
    service, engine, monkeypatch
):
    """Removing the table from the nightly does not retire its writer."""

    calls = []

    def _collect():
        calls.append("collected")
        return pd.DataFrame([{"PLAYER_NAME": "LeBron James", "TEAM_ABBREVIATION": "LAL"}])

    monkeypatch.setattr(service, "_collect_playtypes_frame", _collect)

    assert service.process_playstyles() is True
    assert calls == ["collected"]
    assert read_table(engine, "player_play_types")["PLAYER_NAME"].tolist() == [
        "LeBron James"
    ]


# --- player shooting zones (#267) ------------------------------------------


def _recorded_zone_frame(kind="per_game"):
    """A recorded wide provider frame, exactly as the endpoint returns it.

    ``per_game`` is the per-mode the nightly reads.  ``totals`` is the same
    endpoint and the same players in season counts, and is used where the
    assertion needs the nonzero ``Backcourt`` attempts that PerGame rounds
    away for every player.
    """

    import json
    from pathlib import Path

    from nba_api.stats.endpoints._base import Endpoint

    result_set = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "player_diets"
         / "player_shot_zones_league.json").read_text(encoding="utf-8")
    )[kind]
    return Endpoint.DataSet(
        {"headers": result_set["headers"], "data": result_set["rowSet"]}
    ).get_data_frame()


def test_the_zone_profile_transform_is_the_historical_43_column_arithmetic(
    service, monkeypatch
):
    """#267 moves this frame's source, not its statistics.

    The fixture carries nonzero ``Backcourt`` attempts, which the profile reads
    into ``Sum`` and into the league ``PTS%`` mean before dropping the category
    at the final projection.  Pinning the values here is what stops the
    cutover quietly changing a published number.
    """

    monkeypatch.setattr(
        service, "_fetch_player_zone_data",
        lambda *a, **k: _recorded_zone_frame("totals"),
    )

    frame = service._collect_player_zone()

    assert len(frame.columns) == 43
    assert not [column for column in frame.columns if "Backcourt" in column]
    assert list(frame.columns[:4]) == [
        "PLAYER_NAME", "Restricted Area_FGM", "Restricted Area_FGA",
        "Restricted Area_FG_PCT",
    ]
    source = _recorded_zone_frame("totals")
    source.columns = ["_".join(filter(None, column)).strip() for column in source.columns]
    assert source["Backcourt_FGA"].sum() > 0

    row = frame[frame["PLAYER_NAME"] == "LeBron James"].to_dict(orient="records")[0]
    assert row["Restricted Area_FGM"] == pytest.approx(257.0)
    assert row["Restricted Area_PTS"] == pytest.approx(514.0)
    assert row["Above the Break 3_PTS"] == pytest.approx(
        source.loc[source["PLAYER_NAME"] == "LeBron James", "Above the Break 3_FGM"].iloc[0] * 3
    )
    # ``Sum`` is the historical sum over every remaining numeric column, which
    # includes the Backcourt metrics that never reach the rendered row.
    identity = ["PLAYER_ID", "PLAYER_NAME", "TEAM_ID", "TEAM_ABBREVIATION", "AGE", "NICKNAME"]
    lebron = source[source["PLAYER_NAME"] == "LeBron James"]
    expected_sum = (
        lebron.drop(identity, axis=1).sum(axis=1).iloc[0]
        + row["Restricted Area_PTS"]
        + sum(
            row[column] for column in row
            if column.endswith("_PTS") and column != "Restricted Area_PTS"
        )
    )
    assert row["Restricted Area_PTS%"] == pytest.approx(
        row["Restricted Area_PTS"] / expected_sum * 100
    )

    # LeBron attempted no backcourt shot, so his ``Sum`` cannot notice the
    # category leaving the denominator and his row alone cannot pin the rule
    # the spec states: "Drop Backcourt only at the original transformation
    # stage after Sum/PTS%/league PTS%+ calculation."  Anthony Black made one
    # of one from the backcourt, so his three Backcourt source cells are worth
    # exactly 3.0 in a historically computed ``Sum`` and his ``PTS%`` moves if
    # they are dropped first.
    assert source.loc[source["PLAYER_NAME"] == "Anthony Black", "Backcourt_FGA"].iloc[0] > 0
    black = frame[frame["PLAYER_NAME"] == "Anthony Black"].to_dict(orient="records")[0]

    # Derived from the fixture alone, never from the transform: the historical
    # ``Sum`` is every source metric cell across all eight provider categories
    # plus the seven published zones' ``PTS``, two points inside the arc and
    # three outside it.
    two_point_zones = ("Restricted Area", "In The Paint (Non-RA)", "Mid-Range")
    three_point_zones = (
        "Left Corner 3", "Right Corner 3", "Above the Break 3", "Corner 3",
    )
    black_source = source[source["PLAYER_NAME"] == "Anthony Black"]
    metric_cells = black_source.drop(identity, axis=1).sum(axis=1).iloc[0]
    zone_points = sum(
        black_source[f"{zone}_FGM"].iloc[0] * points
        for zones, points in ((two_point_zones, 2), (three_point_zones, 3))
        for zone in zones
    )
    backcourt_cells = sum(
        black_source[f"Backcourt_{metric}"].iloc[0]
        for metric in ("FGM", "FGA", "FG_PCT")
    )
    assert backcourt_cells == pytest.approx(3.0)
    assert black["Restricted Area_PTS"] == pytest.approx(340.0)
    assert black["Restricted Area_PTS%"] == pytest.approx(
        black["Restricted Area_PTS"] / (metric_cells + zone_points) * 100
    )
    # The independently computed historical value, pinned so the published
    # number cannot drift with the implementation.
    assert black["Restricted Area_PTS%"] == pytest.approx(15.86696390779054)
    # ...and the same statistic with Backcourt dropped before ``Sum`` is a
    # different number, so the two assertions above are genuinely load-bearing
    # rather than agreeing by coincidence the way LeBron's row does.
    assert black["Restricted Area_PTS%"] != pytest.approx(
        black["Restricted Area_PTS"]
        / (metric_cells + zone_points - backcourt_cells) * 100
    )


def test_extra_provider_evidence_cannot_reach_the_zone_profile_denominator(
    service, monkeypatch
):
    """Games played and minutes are collected beside the profile, not into it.

    ``Sum`` sums every remaining numeric column, so an unpinned input column
    would silently move every ``PTS%`` and every ``PTS%+`` on the tab.
    """

    monkeypatch.setattr(
        service, "_fetch_player_zone_data", lambda *a, **k: _recorded_zone_frame()
    )
    clean = service._collect_player_zone()

    contaminated = _recorded_zone_frame()
    contaminated.columns = [
        "_".join(filter(None, column)).strip() for column in contaminated.columns
    ]
    contaminated["GP"] = 41
    contaminated["MIN"] = 1234.5
    contaminated["RETRIEVED_AT_EPOCH"] = 1.7e9
    monkeypatch.setattr(
        service, "_fetch_player_zone_data", lambda *a, **k: contaminated
    )

    pd.testing.assert_frame_equal(clean, service._collect_player_zone())


def test_an_activated_zone_stream_costs_the_nightly_no_nba_request(
    service, monkeypatch
):
    """The last NBA Stats dependency leaves the nightly on activation.

    The fence decides from the table name before the collector runs, so assert
    on the collector invocation rather than on the published set.
    """

    from app.services.collection_control import ControlPlaneError

    collected = []

    class Fence:
        def __init__(self, activated):
            self.activated = activated

        def assert_writable(self, stream_key, connection=None):
            if stream_key in self.activated:
                raise ControlPlaneError("legacy_write_fenced")

    def _record(name, value):
        def build():
            collected.append(name)
            return value
        return build

    for attribute, table_name in (
        ("_collect_player_information", "player_information"),
        ("_fetch_player_per36_stats", "player_per36_stats"),
        ("_collect_opp_shooting_zone", "opp_shooting_zone"),
        ("_collect_player_zone", "player_shooting_zones"),
    ):
        monkeypatch.setattr(
            service, attribute, _record(table_name, pd.DataFrame([{"value": "new"}])),
        )
    monkeypatch.setattr(
        service, "_collect_pbp_frame",
        lambda kind: _record("pbp_opponent_stats", pd.DataFrame([{"v": 1}]))(),
    )

    # Preactivation: the legacy fallback writer is still the only source.
    service.write_fence = Fence(set())
    service._collect_all_frames()
    assert "player_shooting_zones" in collected

    collected.clear()
    service.write_fence = Fence({"exact_shot_zones"})
    frames = service._collect_all_frames()
    assert "player_shooting_zones" not in collected
    assert "player_shooting_zones" not in frames
    # The tables with no database-first replacement still refresh.
    assert "player_information" in collected


def test_a_zone_response_missing_a_pinned_source_column_is_refused(
    service, monkeypatch
):
    """A narrowed provider response must fail, not render a partial profile."""

    frame = _recorded_zone_frame()
    frame.columns = ["_".join(filter(None, column)).strip() for column in frame.columns]
    monkeypatch.setattr(
        service, "_fetch_player_zone_data",
        lambda *a, **k: frame.drop(columns=["Backcourt_FGA"]),
    )

    with pytest.raises(KeyError, match="Backcourt_FGA"):
        service._collect_player_zone()
