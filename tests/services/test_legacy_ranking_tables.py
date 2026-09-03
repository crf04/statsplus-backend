"""The legacy nightly ranking tables are fenced against every writer (#199).

The Season Rankings cutover (#198) moved every game-log Team Filter onto the
durable Matchup publications, so nothing in the refresh path needs to produce
``general_opponent_stats``, ``catch_and_shoot``, ``pullups``,
``less_than_10_ft``, ``team_play_types``, or ``processed_team_assists`` any
more.  These tests pin the fence rather than the absence of one call site: a
reintroduced collector or a revived compatibility writer fails here.

The tables themselves are not dropped.  ``GET /api/teams/stats`` still reads
all six and has no publication-backed replacement, so
``ALLOWED_RETIRED_TABLE_MENTIONS`` below records what the drop migration must
cut over first, along with the limits of that record.
"""

from __future__ import annotations

from pathlib import Path

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


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Production directories.  Tests are not read paths, so they are excluded.
PRODUCTION_ROOTS = ("app", "scripts")

#: Every production file that may still name a retired ranking table, with the
#: reason it is allowed to.  Three of these issue real SQL or schema
#: expectations against the tables and must be cut over before the drop; the
#: rest are domain vocabulary -- publication slice keys and provider operation
#: names -- that merely share the retired tables' spelling and must not be
#: renamed with them.
ALLOWED_RETIRED_TABLE_MENTIONS: dict[str, str] = {
    # --- real dependencies on the tables, blocking the drop migration ---
    "app/services/team_service.py": (
        "SQL READER: GET /api/teams/stats reads all six for its Traditional, "
        "Playtypes, Assists, and Shooting Type categories"
    ),
    "app/services/ledger_parity.py": (
        "SQL READER: LegacyParityDiagnosticReader selects general_opponent_stats "
        "for the traditional_opponent parity comparison, and raises when the "
        "table is absent"
    ),
    "scripts/validate_demo_db.py": (
        "SCHEMA CONTRACT: requires team_play_types and processed_team_assists in "
        "the tracked demo database, so a drop must update this validator too"
    ),
    # --- vocabulary that shares the spelling ---
    "app/services/table_publisher.py": "the retired-table fence itself",
    "app/domain/team_matchup_taxonomy.py": "shot-type slice keys in publications",
    "app/services/team_filter_rankings.py": "published shot-type metric keys",
    "app/services/nba_stats_adapter.py": "the synergy_team_play_types operation name",
    "app/utils/telemetry.py": "the synergy_team_play_types operation name",
    "app/utils/tables.py": "legacy display-name normalization",
    "app/services/data_service.py": "a docstring naming the removed collector's table",
    "scripts/generate_benchmark_fixture.py": "the catch_and_shoot publication slice key",
}


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
    monkeypatch.setattr(service, "_collect_pbp_frame", lambda *a, **k: frame.copy())
    monkeypatch.setattr(
        service,
        "_collect_assist_frames",
        lambda *a, **k: {"processed_player_assists": frame.copy()},
    )

    collected = set(service._collect_all_frames())

    assert collected & RETIRED_LEGACY_RANKING_TABLES == set()


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


def test_every_remaining_mention_of_a_retired_table_is_accounted_for():
    """The repository-wide search behind #199's first Done-when checkbox.

    A production file that names one of these tables is either the fence, a
    real dependency that must be cut over before the tables can be dropped, or
    vocabulary that merely shares their spelling.  Anything else has to be
    classified in the allow-list before it can land.

    Two limits are deliberate and must not be read as stronger than they are:

    * It is a **per-file** substring allow-list.  A new SQL read added inside a
      file that is already allowed -- another query in ``team_service.py``, say
      -- does not fail this test.  Only a new *file* does.
    * It cannot see a **dynamic** reader.  ``database_utils.fetch_data_from_table``
      and ``PlayerService._fetch_data_from_table`` take a table name as an
      argument, so a caller that passes a retired name through a variable never
      spells it in the source and is invisible here.

    The behavioural fences above are what actually stop a write; this test
    records what is left to cut over before the drop migration can land.
    """

    found = {
        str(path.relative_to(REPOSITORY_ROOT))
        for root in PRODUCTION_ROOTS
        for path in (REPOSITORY_ROOT / root).rglob("*.py")
        if any(name in path.read_text() for name in RETIRED_LEGACY_RANKING_TABLES)
    }

    assert found == set(ALLOWED_RETIRED_TABLE_MENTIONS)
