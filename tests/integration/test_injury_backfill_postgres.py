"""Postgres check for the injury backfill's storage path (crf04/statsplus-backend#443).

The ``(season, game_id) IN (...)`` lookup that tells which games already hold a
snapshot, and a past report stored through the live refresh's path, on the
database production runs.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.providers.nba_injury_report import InjuryEntryEvidence, InjuryProviderSnapshot
from app.services.injury_snapshot_repository import (
    InjurySnapshotRepository,
    InjurySnapshotScope,
)
from app.services.matchup_injuries import MatchupInjuryService

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]

SEASON = "2025-26"
RUN_AT = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
URL = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-12-25_05_00PM.pdf"


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(engine)
    yield engine
    engine.dispose()


class Catalog:
    def get_catalog(self, season, *, active_only=False):
        return [{"player_id": 9001, "display_name": "Harrison Ingram", "team_abbreviation": "SAS"}]


def _event(game_id, away, home):
    return {
        "nba_game_id": game_id,
        "scheduled_at": "2025-12-25T19:30:00+00:00",
        "away_team_id": 1610612759,
        "away_team_tricode": away,
        "home_team_id": 1610612760,
        "home_team_tricode": home,
    }


def test_a_past_report_is_stored_per_game_and_the_game_is_then_seen_as_stored(engine):
    repository = InjurySnapshotRepository(engine)
    injuries = MatchupInjuryService(
        provider=None, snapshot_repository=repository, athlete_catalog=Catalog(), enabled=True
    )
    snapshot = InjuryProviderSnapshot(
        raw_payload=[{"player": "Ingram, Harrison"}],
        entries=(
            InjuryEntryEvidence(
                "SAS:harrison-ingram",
                "SAS:harrison-ingram",
                "Harrison Ingram",
                "SAS",
                "Out",
                "Out",
                "G League - Two-Way",
                URL,
                game_date="2025-12-25",
                matchup="SAS@OKC",
                report_time="2025-12-25T17:00:00-05:00",
            ),
        ),
        retrieved_at=RUN_AT,
    )
    stored = _event("0022500400", "SAS", "OKC")
    free = _event("0022500401", "DAL", "GSW")
    scopes = tuple(InjurySnapshotScope(SEASON, e["nba_game_id"]) for e in (stored, free))
    assert repository.stored_scopes(scopes) == set()

    assert injuries.store_past_report(snapshot, events=[stored], season=SEASON) == 1

    assert repository.stored_scopes(scopes) == {scopes[0]}
    row = repository.get(scopes[0])
    assert row.retrieved_at == RUN_AT
    assert [
        (e["canonical_player_id"], e["canonical_status"], e["report_time"]) for e in row.normalized_entries
    ] == [(9001, "Out", "2025-12-25T17:00:00-05:00")]
    # A second write for the same game replaces nothing and leaves no orphan source.
    again = InjuryProviderSnapshot(snapshot.raw_payload, (), RUN_AT)
    assert injuries.store_past_report(again, events=[stored], season=SEASON) == 0
    assert repository.get(scopes[0]) == row
    with engine.connect() as connection:
        sources = connection.execute(
            text("SELECT source, count(*) FROM injury_source_snapshots GROUP BY source")
        ).all()
    assert sources == [("nba_official_backfill", 1)]
