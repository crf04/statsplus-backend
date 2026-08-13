"""Offline scheduler tests for newest-first resumable ledger backfill."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine

from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_backfill import LedgerBackfillService


class _Events:
    def __init__(self, event):
        self.event = event

    def get_events(self, season):
        return [self.event]


class _Provider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def fetch_game_player_logs(self, game_id, season, *, season_type="Regular Season"):
        self.calls.append((game_id, season, season_type))
        return self.payload


def _event():
    return {
        "nba_game_id": "0022400001",
        "season": "2024-25",
        "classification": "Regular Season",
        "scheduled_at": "2024-11-15T00:00:00+00:00",
        "home_team_id": 1610612747,
        "home_team_tricode": "LAL",
        "away_team_id": 1610612759,
        "away_team_tricode": "SAS",
        "status_code": 3,
        "status_text": "Final",
    }


def test_backfill_is_resumable_and_newest_first(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    payload = json.loads(Path("tests/fixtures/pbp_stats/game_stats.valid.json").read_text())
    provider = _Provider(payload)
    repository = CanonicalGameLedgerRepository(engine, minimum_active_players_per_team=1)
    service = LedgerBackfillService(
        provider=provider,
        event_catalog=_Events(_event()),
        repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=timezone.utc),
    )

    result = service.refresh("2024-25")

    assert result.complete
    assert result.status == "complete"
    assert provider.calls == [("0022400001", "2024-25", "Regular Season")]
    progress = repository.get_progress("2024-25")
    assert progress is not None
    assert progress.status == "complete"
    assert progress.completed_game_ids == frozenset({"0022400001"})
