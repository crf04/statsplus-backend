"""Persistence, publication, and freshness contract for athlete catalogs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, text

from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    RuntimeSettings,
)
from app.errors import ProviderUnavailableError
from app.migrations import run_migrations
from app.services.athlete_catalog_service import (
    CATALOG_FAILURE_SUMMARY,
    AthleteCatalogService,
)


class FakeRosterProvider:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.seasons: list[str] = []

    def get_player_roster(self, *, season: str) -> pd.DataFrame:
        self.seasons.append(season)
        frame = self.frames[season]
        if isinstance(frame, BaseException):
            raise frame
        return frame.copy()


def _settings(*, freshness_days: int = 7) -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        catalog={"athlete_freshness_days": freshness_days},
    )


def _frame(
    display_name: str = "LeBron James", *, season: str = "2024-25"
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_id": 23,
                "display_name": display_name,
                "roster_status": "active",
                "is_active": True,
                "is_active_for_season": True,
                "season": season,
                "team_id": 1610612747,
                "team_name": "Los Angeles Lakers",
                "team_abbreviation": "LAL",
            }
        ]
    )


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "player_id", "display_name", "roster_status", "is_active",
        "is_active_for_season", "season", "team_id", "team_name",
        "team_abbreviation",
    ])


@pytest.fixture
def catalog_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite3'}")
    run_migrations(engine)
    return engine


def test_migration_004_creates_catalog_and_freshness_tables_idempotently(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite3'}")

    first = run_migrations(engine)
    second = run_migrations(engine)

    assert "004_create_athlete_catalog" in first.applied
    assert second.applied == ()
    assert inspect(engine).has_table("athlete_catalog")
    assert inspect(engine).has_table("athlete_catalog_freshness")
    assert {
        column["name"] for column in inspect(engine).get_columns("athlete_catalog")
    } >= {
        "player_id",
        "display_name",
        "roster_status",
        "is_active",
        "is_active_for_season",
        "season",
        "team_id",
        "team_name",
        "team_abbreviation",
    }


def test_refresh_publishes_idempotently_and_persists_reads_and_freshness(catalog_db):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    provider = FakeRosterProvider({"2024-25": _frame()})
    service = AthleteCatalogService(
        catalog_db,
        settings=_settings(),
        nba_stats_provider=provider,
        clock=lambda: now,
    )

    result = service.refresh(["2024-25"])

    assert result["2024-25"].status == "succeeded"
    assert service.get_catalog("2024-25") == _frame().to_dict(orient="records")
    freshness = service.get_freshness("2024-25", now=now)
    assert freshness["is_fresh"] is True
    assert freshness["last_success_at"] == now.isoformat()
    assert freshness["last_failure_at"] is None
    assert freshness["row_count"] == 1

    # A repeated publication replaces the season's set rather than appending
    # duplicate player identities.
    service.refresh(["2024-25"])
    with catalog_db.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM athlete_catalog WHERE season = '2024-25'")
        ).scalar_one() == 1


def test_refresh_failure_preserves_prior_success_and_records_independent_failure(catalog_db):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    provider = FakeRosterProvider({"2024-25": _frame()})
    service = AthleteCatalogService(
        catalog_db,
        settings=_settings(),
        nba_stats_provider=provider,
        clock=lambda: now,
    )
    service.refresh(["2024-25"])

    provider.frames["2024-25"] = ProviderUnavailableError("offline")
    result = service.refresh(["2024-25"])
    assert result["2024-25"].status == "failed"
    assert result["2024-25"].failure_summary == CATALOG_FAILURE_SUMMARY

    assert service.get_catalog("2024-25") == _frame().to_dict(orient="records")
    freshness = service.get_freshness("2024-25", now=now)
    assert freshness["last_success_at"] == now.isoformat()
    assert freshness["last_failure_at"] == now.isoformat()
    assert freshness["is_fresh"] is True


def test_refresh_returns_each_season_outcome_when_batch_is_mixed(catalog_db):
    provider = FakeRosterProvider({
        "2023-24": _frame(season="2023-24"),
        "2024-25": _empty_frame(),
    })
    service = AthleteCatalogService(
        catalog_db, settings=_settings(), nba_stats_provider=provider
    )

    result = service.refresh(["2023-24", "2024-25"])

    assert result.has_failures is True
    assert result["2023-24"].status == "succeeded"
    assert result["2023-24"].row_count == 1
    assert result["2024-25"].status == "failed"
    assert result["2024-25"].failure_summary == CATALOG_FAILURE_SUMMARY
    assert provider.seasons == ["2023-24", "2024-25"]


def test_empty_initial_roster_is_a_sanitized_failure(catalog_db):
    service = AthleteCatalogService(
        catalog_db,
        settings=_settings(),
        nba_stats_provider=FakeRosterProvider({"2024-25": _empty_frame()}),
    )

    result = service.refresh(["2024-25"])

    assert result["2024-25"].status == "failed"
    assert service.get_catalog("2024-25") == []
    assert service.get_freshness("2024-25")["last_success_at"] is None


def test_empty_roster_preserves_prior_catalog_and_success_freshness(catalog_db):
    provider = FakeRosterProvider({"2024-25": _frame()})
    service = AthleteCatalogService(
        catalog_db, settings=_settings(), nba_stats_provider=provider
    )
    service.refresh(["2024-25"])
    provider.frames["2024-25"] = _empty_frame()

    result = service.refresh(["2024-25"])

    assert result["2024-25"].status == "failed"
    assert service.get_catalog("2024-25") == _frame().to_dict(orient="records")
    freshness = service.get_freshness("2024-25")
    assert freshness["last_success_at"] is not None
    assert freshness["last_failure_at"] is not None


def test_publication_failure_rolls_back_delete_and_insert(catalog_db, monkeypatch):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    provider = FakeRosterProvider({"2024-25": _frame()})
    service = AthleteCatalogService(
        catalog_db,
        settings=_settings(),
        nba_stats_provider=provider,
        clock=lambda: now,
    )
    service.refresh(["2024-25"])
    provider.frames["2024-25"] = _frame("LeBron Raymone James")

    def fail_after_catalog_write(*_args, **_kwargs):
        raise RuntimeError("publication failed")

    monkeypatch.setattr(service, "_upsert_success", fail_after_catalog_write)
    result = service.refresh(["2024-25"])
    assert result["2024-25"].status == "failed"

    assert service.get_catalog("2024-25")[0]["display_name"] == "LeBron James"
    freshness = service.get_freshness("2024-25", now=now)
    assert freshness["last_success_at"] == now.isoformat()
    assert freshness["last_failure_at"] == now.isoformat()


def test_display_name_updates_are_scoped_to_the_requested_season(catalog_db):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    provider = FakeRosterProvider(
        {
            "2023-24": _frame(season="2023-24"),
            "2024-25": _frame(season="2024-25"),
        }
    )
    service = AthleteCatalogService(
        catalog_db,
        settings=_settings(),
        nba_stats_provider=provider,
        clock=lambda: now,
    )
    service.refresh(["2023-24", "2024-25"])
    provider.frames["2024-25"] = _frame(
        "LeBron Raymone James", season="2024-25"
    )
    service.refresh_season("2024-25")

    assert service.get_catalog("2023-24")[0]["display_name"] == "LeBron James"
    assert service.get_catalog("2024-25")[0]["display_name"] == "LeBron Raymone James"


def test_freshness_uses_configured_window_and_explicit_seasons(catalog_db):
    published_at = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    service = AthleteCatalogService(
        catalog_db,
        settings=_settings(freshness_days=7),
        nba_stats_provider=FakeRosterProvider({"2024-25": _frame()}),
        clock=lambda: published_at,
    )
    service.refresh(["2024-25"])

    assert service.is_fresh(
        "2024-25", now=published_at + timedelta(days=7)
    ) is True
    assert service.is_fresh(
        "2024-25", now=published_at + timedelta(days=7, seconds=1)
    ) is False
    with pytest.raises(ValueError, match="explicit|canonical"):
        service.refresh([])
    with pytest.raises(ValueError, match="canonical"):
        service.refresh(["current"])


def test_demo_database_is_rejected_before_catalog_write():
    demo_path = Path("nba_play_types.db")
    before = hashlib.sha256(demo_path.read_bytes()).digest()
    engine = create_engine("sqlite:///nba_play_types.db")

    with pytest.raises(Exception, match="demo database"):
        AthleteCatalogService(engine, settings=_settings(), nba_stats_provider=FakeRosterProvider({}))

    assert hashlib.sha256(demo_path.read_bytes()).digest() == before


def test_athlete_catalog_freshness_states_its_ttl_in_exact_seconds(catalog_db):
    from decimal import Decimal

    service = AthleteCatalogService(
        catalog_db,
        settings=_settings(freshness_days=7),
        nba_stats_provider=FakeRosterProvider({}),
    )
    document = service.get_freshness("2024-25")

    assert document["max_age_seconds"] == Decimal(604800)
    assert isinstance(document["max_age_seconds"], Decimal)
    assert document["freshness_days"] == 7
