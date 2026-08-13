"""Offline scheduler tests for newest-first resumable ledger backfill."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine

from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    canonical_game_from_pbp,
)
from app.migrations import run_migrations
from app.services.ledger_backfill import (
    AcceptedObservationParticipantCatalog,
    CollectionObservationLedgerRecorder,
    LedgerBackfillService,
)
from app.providers.pbp_game_logs import PBPGameLogAdapter
from app.models.event_catalog import EventCatalogEntry


class _Events:
    def __init__(self, event):
        self.events = [event] if isinstance(event, dict) else list(event)

    def get_events(self, season):
        return self.events

    def get_freshness(self, season, *, now):
        return {"fresh": True, "event_count": len(self.events)}


class _Athletes:
    def get_catalog(self, season, *, active_only=False):
        return [{"player_id": value} for value in (2544, 203507, 201935)]

    def get_freshness(self, season, *, now):
        return {"is_fresh": True, "row_count": 3}


class _Participants:
    def get_participants(self, season, game_id):
        return {1610612747: (2544, 203507), 1610612759: (201935,)}


class _Provider:
    def __init__(self, payload, *, dates=None):
        self.payload = payload
        self.dates = dates or {}
        self.calls = []

    def fetch_game_player_logs(self, game_id, season, *, season_type="Regular Season"):
        self.calls.append((game_id, season, season_type))
        payload = json.loads(json.dumps(self.payload))
        if game_id in self.dates:
            payload["date"] = self.dates[game_id]
        return payload


class _Recorder:
    def accept(self, observation, *, season, game_id, retrieved_at):
        return f"accepted:{game_id}"


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


def _payload():
    payload = json.loads(Path("tests/fixtures/pbp_stats/game_stats.valid.json").read_text())
    payload.pop("team_results", None)
    payload["participant_ids_by_team"] = {
        "1610612747": [2544, 203507],
        "1610612759": [201935],
    }
    return payload


def test_backfill_is_resumable_and_newest_first(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(EventCatalogEntry.__table__.insert().values(
            nba_game_id="0022400001",
            season="2024-25",
            home_team_id=1610612747,
            home_team_name="Lakers",
            home_team_tricode="LAL",
            away_team_id=1610612759,
            away_team_name="Spurs",
            away_team_tricode="SAS",
            scheduled_at=datetime(2024, 11, 15, tzinfo=timezone.utc),
            status_text="Final",
            status_code=3,
            classification="Regular Season",
            first_seen_at=datetime(2024, 11, 15, tzinfo=timezone.utc),
            last_seen_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
        ))
    payload = _payload()
    provider = _Provider(payload)
    repository = CanonicalGameLedgerRepository(engine)
    service = LedgerBackfillService(
        provider=provider,
        event_catalog=_Events(_event()),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
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


def test_rechecks_enforce_daily_and_weekly_cadence_and_allow_historical_repair(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cadence.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    now = datetime(2024, 12, 16, tzinfo=timezone.utc)
    cases = (
        ("daily-not-due", 7, timedelta(hours=23)),
        ("daily-due", 7, timedelta(hours=24)),
        ("weekly-due", 8, timedelta(days=7)),
        ("outside-window", 31, timedelta(days=40)),
    )
    events = []
    for game_id, age_days, retrieved_age in cases:
        event = dict(_event())
        event["nba_game_id"] = game_id
        event["scheduled_at"] = (now - timedelta(days=age_days)).isoformat()
        events.append(event)
        seed_payload = _payload()
        seed_payload["date"] = event["scheduled_at"][:10]
        repository.replace_game(canonical_game_from_pbp(
            seed_payload,
            event=event,
            season="2024-25",
            source_observation_id=f"seed:{game_id}",
            retrieved_at=now - retrieved_age,
            participant_ids_by_team=_Participants().get_participants("2024-25", game_id),
        ))

    provider = _Provider(
        _payload(),
        dates={event["nba_game_id"]: event["scheduled_at"][:10] for event in events},
    )
    service = LedgerBackfillService(
        provider=provider,
        event_catalog=_Events(events),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: now,
    )

    result = service.refresh("2024-25", max_games=1)

    assert result.complete
    assert result.lower_priority_remaining == 1
    assert [call[0] for call in provider.calls] == ["daily-due"]

    provider.calls.clear()
    repaired = service.refresh("2024-25", max_games=4, historical_repair=True)

    assert repaired.complete
    assert repaired.lower_priority_remaining == 0
    assert [call[0] for call in provider.calls][:2] == ["daily-due", "daily-not-due"]
    assert {call[0] for call in provider.calls} == {case[0] for case in cases}


def test_provider_retrieval_is_accepted_before_participants_are_read(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'observations.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(EventCatalogEntry.__table__.insert().values(
            nba_game_id="0022400001", season="2024-25",
            home_team_id=1610612747, home_team_name="Lakers", home_team_tricode="LAL",
            away_team_id=1610612759, away_team_name="Spurs", away_team_tricode="SAS",
            scheduled_at=datetime(2024, 11, 15, tzinfo=timezone.utc),
            status_text="Final", status_code=3, classification="Regular Season",
            first_seen_at=datetime(2024, 11, 15, tzinfo=timezone.utc),
            last_seen_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
        ))
    payload = _payload()
    frame = PBPGameLogAdapter.parse_game_stats(payload, game_id="0022400001")
    recorder = CollectionObservationLedgerRecorder(engine)

    observation_id = recorder.accept(
        frame,
        season="2024-25",
        game_id="0022400001",
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
    )
    participants = AcceptedObservationParticipantCatalog(engine).get_participants(
        "2024-25", "0022400001"
    )

    assert observation_id
    assert participants == {1610612747: [2544, 203507], 1610612759: [201935]}
