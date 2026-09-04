"""The legacy nightly ranking tables are retired and dropped (#199).

The Season Rankings cutover (#198/#225) moved every game-log Team Filter onto
the durable Matchup publications, so nothing reads or produces
``general_opponent_stats``, ``catch_and_shoot``, ``pullups``,
``less_than_10_ft``, ``team_play_types``, or ``processed_team_assists`` any
more.  Migration ``048_drop_legacy_ranking_tables`` drops the storage; these
tests pin the fence rather than the absence of one call site, so a reintroduced
collector or a revived compatibility writer fails here.

The read cutover is complete, so ``ALLOWED_RETIRED_TABLE_MENTIONS`` below no
longer lists a single reader: what remains is the fence itself, the drop
migration, and domain vocabulary that merely shares the retired tables'
spelling.
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
#: reason it is allowed to.  No entry is a reader: the cutover is complete and
#: the storage is dropped.  What is left is the fence, the migration that
#: performs the drop, comments recording the retirement, and domain vocabulary
#: -- publication slice keys, provider operation names -- that merely shares
#: the retired tables' spelling and must not be renamed with them.
ALLOWED_RETIRED_TABLE_MENTIONS: dict[str, str] = {
    # --- the fence and the drop ---
    "app/services/table_publisher.py": "the retired-table fence itself",
    "app/migrations.py": (
        "048_drop_legacy_ranking_tables names the six tables it drops"
    ),
    # --- comments and docstrings recording the retirement ---
    "app/services/data_service.py": (
        "a docstring naming the table the removed opponent collector produced"
    ),
    "app/services/ledger_parity.py": (
        "a docstring recording that the traditional_opponent diagnostic read "
        "is retired; LegacyParityDiagnosticReader.TABLES no longer names it"
    ),
    "app/services/ledger_materialization.py": (
        "a comment explaining why neither traditional_opponent window has a "
        "legacy diagnostic left to compare against"
    ),
    # --- vocabulary that shares the spelling ---
    "app/domain/team_matchup_taxonomy.py": "shot-type slice keys in publications",
    "app/services/team_filter_rankings.py": "published shot-type metric keys",
    "app/services/nba_stats_adapter.py": "the synergy_team_play_types operation name",
    "app/utils/telemetry.py": "the synergy_team_play_types operation name",
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

    The read cutover is complete and migration 048 drops the storage, so a
    production file that names one of these tables is now only ever the fence,
    the drop migration, or vocabulary that shares their spelling.  Anything
    else -- in particular a revived reader -- has to be classified in the
    allow-list before it can land.

    Two limits are deliberate and must not be read as stronger than they are:

    * It is a **per-file** substring allow-list.  A new SQL read added inside a
      file that is already allowed -- another query in ``ledger_parity.py``,
      say -- does not fail this test.  Only a new *file* does.
    * It cannot see a **dynamic** reader.  ``database_utils.fetch_data_from_table``
      and ``PlayerService._fetch_data_from_table`` take a table name as an
      argument, so a caller that passes a retired name through a variable never
      spells it in the source and is invisible here.

    The behavioural fences above are what actually stop a write, and the
    dropped storage is what actually stops a read; this test records the
    vocabulary that legitimately survives both.
    """

    found = {
        str(path.relative_to(REPOSITORY_ROOT))
        for root in PRODUCTION_ROOTS
        for path in (REPOSITORY_ROOT / root).rglob("*.py")
        if any(name in path.read_text() for name in RETIRED_LEGACY_RANKING_TABLES)
    }

    assert found == set(ALLOWED_RETIRED_TABLE_MENTIONS)
