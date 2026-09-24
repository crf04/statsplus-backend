"""The legacy nightly ranking tables are retired and dropped (#199).

The Season Rankings cutover (#198/#225) moved every game-log Team Filter onto
the durable Matchup publications, so nothing reads or produces
``general_opponent_stats``, ``catch_and_shoot``, ``pullups``,
``less_than_10_ft``, ``team_play_types``, or ``processed_team_assists`` any
more.  Migration ``048_drop_legacy_ranking_tables`` drops the storage; these
tests pin the fence rather than the absence of one call site, so a reintroduced
collector or a revived compatibility writer fails here.
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect

from app.config.settings import RuntimeSettings
from app.services.data_service import DataService
from app.services.table_publisher import (
    RETIRED_LEGACY_RANKING_TABLES,
    AtomicTablePublisher,
    TablePublicationError,
)


@pytest.fixture
def engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'retired.db'}")


@pytest.fixture
def service(engine):
    return DataService(engine, settings=RuntimeSettings(environment="testing"))


def test_the_retired_set_is_exactly_the_six_legacy_ranking_tables():
    assert RETIRED_LEGACY_RANKING_TABLES == frozenset({
        "general_opponent_stats",
        "catch_and_shoot",
        "pullups",
        "less_than_10_ft",
        "team_play_types",
        "processed_team_assists",
    })


def test_the_nightly_refresh_collects_no_retired_ranking_table(service, monkeypatch):
    """``update_database`` is the only production writer; it produces none."""

    frame = pd.DataFrame([{"value": 1}])
    for helper in (
        "_collect_player_information",
        "_fetch_player_per36_stats",
        "_collect_opp_shooting_zone",
        "_collect_playtypes_frame",
        "_collect_player_zone",
    ):
        monkeypatch.setattr(service, helper, lambda *a, **k: frame.copy())
    pbp_calls = []
    monkeypatch.setattr(
        service,
        "_collect_pbp_frame",
        lambda data_type: pbp_calls.append(data_type) or frame.copy(),
    )
    monkeypatch.setattr(
        service,
        "_collect_assist_frames",
        lambda *a, **k: pytest.fail("retired player assist frame was collected"),
    )

    collected = set(service._collect_all_frames())

    assert collected & RETIRED_LEGACY_RANKING_TABLES == set()
    assert "processed_player_assists" not in collected
    assert "pbp_player_stats" not in collected
    assert pbp_calls == ["opponent"]


@pytest.mark.parametrize("table_name", sorted(RETIRED_LEGACY_RANKING_TABLES))
def test_the_atomic_publisher_refuses_a_retired_ranking_table(
    engine, table_name
):
    publisher = AtomicTablePublisher(engine)

    with pytest.raises(TablePublicationError, match="retired"):
        publisher.publish({table_name: pd.DataFrame([{"value": 1}])})

    assert table_name not in inspect(engine).get_table_names()


@pytest.mark.parametrize("table_name", sorted(RETIRED_LEGACY_RANKING_TABLES))
def test_the_compatibility_writer_refuses_a_retired_ranking_table(
    service, engine, table_name
):
    with pytest.raises(TablePublicationError, match="retired"):
        service._publish_compat_frame(table_name, pd.DataFrame([{"value": 1}]))

    assert table_name not in inspect(engine).get_table_names()

