"""Offline contract tests for the canonical event catalog."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import OperationalError

from app.errors import ProviderUnavailableError
from app.config.settings import CatalogSettings, RuntimeSettings
from app.migrations import run_migrations
from app.services.event_catalog_service import EventCatalogService
from app.services.nba_stats_adapter import normalize_whole_season_schedule


FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/nba_stats/schedule.valid.json").read_text()
)


def _frame() -> pd.DataFrame:
    result_set = FIXTURE["resultSets"][0]
    return pd.DataFrame(result_set["rowSet"], columns=result_set["headers"])


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'events.sqlite3'}")
    run_migrations(engine)
    return engine


class FakeScheduleProvider:
    def __init__(self, frame: pd.DataFrame | None = None):
        self.frame = frame if frame is not None else _frame()
        self.calls: list[str] = []
        self.error: Exception | None = None

    def fetch_whole_season_schedule(self, *, season: str) -> pd.DataFrame:
        self.calls.append(season)
        if self.error is not None:
            raise self.error
        return normalize_whole_season_schedule(self.frame.copy(), season=season)


def test_refresh_persists_future_events_and_postponement_evidence(tmp_path):
    now = datetime(2025, 10, 20, tzinfo=timezone.utc)
    provider = FakeScheduleProvider()
    service = EventCatalogService(_engine(tmp_path), provider, clock=lambda: now)

    result = service.refresh("2025-26")
    rows = service.get_events("2025-26")

    assert result.event_count == 2
    assert provider.calls == ["2025-26"]
    assert rows[0]["nba_game_id"] == "0022500001"
    assert rows[0]["home_team"]["id"] == 1610612747
    assert rows[0]["away_team"]["tricode"] == "SAS"
    assert rows[0]["scheduled_at"] == "2025-10-23T00:00:00+00:00"
    assert rows[0]["postponement_evidence"] is None
    assert rows[0]["is_postponed"] is False
    postponed = next(row for row in rows if row["nba_game_id"] == "0022500002")
    assert postponed["status_text"] == "Postponed"
    assert postponed["postponed_status"] == "Postponed"
    assert postponed["postponement_evidence"]
    assert postponed["is_postponed"] is True
    assert postponed["classification"] == "Regular Season"


def test_service_uses_catalog_event_max_age_setting(tmp_path):
    now = datetime(2025, 10, 20, tzinfo=timezone.utc)
    settings = RuntimeSettings(
        environment="testing",
        catalog=CatalogSettings(event_max_age_hours=24),
    )
    service = EventCatalogService(
        _engine(tmp_path), FakeScheduleProvider(), settings=settings, clock=lambda: now
    )
    service.refresh("2025-26")
    assert service.get_freshness("2025-26", now=now + timedelta(hours=24))["fresh"]
    assert not service.get_freshness("2025-26", now=now + timedelta(hours=25))["fresh"]


def test_service_consumes_canonical_provider_frame_and_preserves_structured_evidence(tmp_path):
    canonical = normalize_whole_season_schedule(_frame(), season="2025-26")
    expected = {"source": "schedule", "reason": "weather", "nested": {"code": 7}}
    canonical.loc[canonical["nba_game_id"] == "0022500002", "postponement_evidence"] = json.dumps(expected)

    class CanonicalProvider:
        def fetch_whole_season_schedule(self, *, season):
            assert season == "2025-26"
            return canonical.copy()

    service = EventCatalogService(_engine(tmp_path), CanonicalProvider())
    service.refresh("2025-26")
    postponed = next(row for row in service.get_events("2025-26") if row["nba_game_id"] == "0022500002")
    assert postponed["postponement_evidence"] == expected


def test_refresh_upserts_by_game_id_and_retains_omitted_rows(tmp_path):
    engine = _engine(tmp_path)
    now = datetime(2025, 10, 20, tzinfo=timezone.utc)
    provider = FakeScheduleProvider()
    service = EventCatalogService(engine, provider, clock=lambda: now)
    service.refresh("2025-26")

    updated = _frame()
    updated.loc[updated["gameId"] == "0022500001", "gameDateTimeUTC"] = (
        "2025-10-23T01:00:00Z"
    )
    updated.loc[updated["gameId"] == "0022500001", "gameStatusText"] = "8:00 pm ET"
    updated.loc[updated["gameId"] == "0022500001", "gameLabel"] = "Playoffs"
    updated = updated[updated["gameId"] != "0022500002"]
    provider.frame = updated
    service.refresh("2025-26")

    rows = service.get_events("2025-26")
    assert {row["nba_game_id"] for row in rows} == {"0022500001", "0022500002"}
    changed = next(row for row in rows if row["nba_game_id"] == "0022500001")
    assert changed["scheduled_at"] == "2025-10-23T01:00:00+00:00"
    retained = next(row for row in rows if row["nba_game_id"] == "0022500002")
    assert retained["status_text"] == "Postponed"
    assert changed["classification"] == "Playoffs"


def test_replacement_game_id_is_a_new_event_without_heuristic_transfer(tmp_path):
    engine = _engine(tmp_path)
    provider = FakeScheduleProvider()
    service = EventCatalogService(engine, provider)
    service.refresh("2025-26")

    replacement = _frame().iloc[[0]].copy()
    replacement.loc[:, "gameId"] = "0022500999"
    provider.frame = replacement
    service.refresh("2025-26")

    rows = service.get_events("2025-26")
    assert {row["nba_game_id"] for row in rows} == {
        "0022500001",
        "0022500002",
        "0022500999",
    }


def test_invalid_batch_is_atomic_and_previous_events_survive(tmp_path):
    engine = _engine(tmp_path)
    provider = FakeScheduleProvider()
    service = EventCatalogService(engine, provider)
    service.refresh("2025-26")
    before = service.get_events("2025-26")

    invalid = _frame().copy()
    invalid.loc[0, "homeTeam_teamId"] = None
    provider.frame = invalid
    with pytest.raises(ProviderUnavailableError):
        service.refresh("2025-26")

    assert service.get_events("2025-26") == before
    status = service.get_freshness("2025-26")
    assert status["last_success_at"]
    assert status["last_failure_at"]
    assert status["fresh"] is True


def test_freshness_is_configurable_and_success_failure_are_independent(tmp_path):
    now = datetime(2025, 10, 20, tzinfo=timezone.utc)
    provider = FakeScheduleProvider()
    service = EventCatalogService(
        _engine(tmp_path), provider, clock=lambda: now, max_age=timedelta(hours=72)
    )
    service.refresh("2025-26")
    assert service.get_freshness("2025-26")["fresh"] is True

    provider.error = ProviderUnavailableError("provider unavailable")
    service._clock = lambda: now + timedelta(hours=73)
    with pytest.raises(ProviderUnavailableError):
        service.refresh("2025-26")

    status = service.get_freshness("2025-26")
    assert status["fresh"] is False
    assert status["last_success_at"] == "2025-10-20T00:00:00+00:00"
    assert status["last_failure_at"] == "2025-10-23T01:00:00+00:00"


def test_persistence_failure_rolls_back_rows_and_does_not_become_provider_error(tmp_path):
    engine = _engine(tmp_path)
    provider = FakeScheduleProvider()
    service = EventCatalogService(engine, provider)
    service.refresh("2025-26")
    before = service.get_events("2025-26")
    before_success = service.get_freshness("2025-26")["last_success_at"]
    state = {"updates": 0}

    def fail_second_insert(conn, cursor, statement, parameters, context, executemany):
        if "UPDATE event_catalog " in statement:
            state["updates"] += 1
            if state["updates"] == 2:
                raise OperationalError("forced catalog failure", {}, RuntimeError("test"))

    event.listen(engine, "before_cursor_execute", fail_second_insert)
    try:
        with pytest.raises(OperationalError):
            service.refresh("2025-26")
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_insert)

    assert state["updates"] == 2
    assert service.get_events("2025-26") == before
    status = service.get_freshness("2025-26")
    assert status["last_success_at"] == before_success
    assert status["fresh"] is True


@pytest.mark.parametrize("season", ["2025", "2025-2026", "current", "2025-27"])
def test_service_requires_explicit_canonical_season(tmp_path, season):
    service = EventCatalogService(_engine(tmp_path), FakeScheduleProvider())
    with pytest.raises(ValueError, match="canonical NBA season"):
        service.get_events(season)


def test_migration_creates_event_tables_in_writable_database_only(tmp_path):
    engine = _engine(tmp_path)
    names = set(inspect(engine).get_table_names())
    assert {"event_catalog", "event_catalog_refreshes"}.issubset(names)
    columns = {column["name"] for column in inspect(engine).get_columns("event_catalog")}
    assert not {"mapping_needed", "audit_status", "audit_note"} & columns
    assert engine.url.database != "nba_play_types.db"


def test_refresh_multiple_seasons_dedupes_calls_and_is_deterministic(tmp_path):
    now = datetime(2025, 10, 20, tzinfo=timezone.utc)
    provider = FakeScheduleProvider()
    provider.frame = provider.frame.drop(columns=["season", "seasonYear"], errors="ignore")
    service = EventCatalogService(_engine(tmp_path), provider, clock=lambda: now)
    result = service.refresh(["2025-26", "2024-25", "2025-26"])
    assert [item.season for item in result.results] == ["2024-25", "2025-26"]
    assert result.failures == {}
    assert provider.calls == ["2024-25", "2025-26"]
    assert service.get_freshness("2024-25")["fresh"] is True
    assert service.get_freshness("2025-26")["fresh"] is True


def test_refresh_multiple_seasons_keeps_success_when_one_fails(tmp_path):
    class MixedProvider(FakeScheduleProvider):
        def fetch_whole_season_schedule(self, *, season):
            self.calls.append(season)
            if season == "2024-25":
                raise ProviderUnavailableError("recorded outage")
            return normalize_whole_season_schedule(self.frame.copy(), season=season)

    now = datetime(2025, 10, 20, tzinfo=timezone.utc)
    service = EventCatalogService(_engine(tmp_path), MixedProvider(), clock=lambda: now)
    result = service.refresh(["2025-26", "2024-25"])
    assert [item.season for item in result.results] == ["2025-26"]
    assert result.failures == {"2024-25": "recorded outage"}
    assert service.get_freshness("2025-26")["fresh"] is True
    assert service.get_freshness("2024-25")["last_success_at"] is None


def test_refresh_rejects_empty_season_batch(tmp_path):
    service = EventCatalogService(_engine(tmp_path), FakeScheduleProvider())
    with pytest.raises(ValueError, match="at least one"):
        service.refresh([])


def test_the_reported_maximum_age_is_the_exact_ttl_the_catalog_gates_on(tmp_path):
    """One duration decides freshness and is reported, to the microsecond.

    A TTL rewritten as floating-point hours and multiplied back to seconds is
    not the duration the catalog compared against: a third of an hour gates at
    exactly 1200 seconds but was reported as 1199.99999999999988.
    """

    now = datetime(2025, 10, 20, tzinfo=timezone.utc)
    provider = FakeScheduleProvider()
    service = EventCatalogService(
        _engine(tmp_path), provider, clock=lambda: now, max_age=1 / 3
    )
    service.refresh("2025-26")

    at_boundary = service.get_freshness("2025-26", now=now + timedelta(seconds=1200))
    past_boundary = service.get_freshness(
        "2025-26", now=now + timedelta(seconds=1200, microseconds=1)
    )

    assert at_boundary["max_age_seconds"] == Decimal(1200)
    assert isinstance(at_boundary["max_age_seconds"], Decimal)
    assert at_boundary["fresh"] is True
    assert past_boundary["fresh"] is False


def test_a_catalog_freshness_document_states_no_lossy_window(tmp_path):
    now = datetime(2025, 10, 20, tzinfo=timezone.utc)
    service = EventCatalogService(
        _engine(tmp_path), FakeScheduleProvider(), clock=lambda: now, max_age=1 / 3
    )
    service.refresh("2025-26")

    assert "max_age_hours" not in service.get_freshness("2025-26")
