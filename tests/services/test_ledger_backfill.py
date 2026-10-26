"""Offline scheduler tests for newest-first resumable ledger backfill."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from app.errors import ProviderUnavailableError
from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    LedgerGameRowEvidence,
)
from app.models.collection_control import (
    ActiveSeason,
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
)
from app.models.event_catalog import EventCatalogEntry
from app.providers.nba_live_data import compose_pbp_live_data_observation
from app.providers.pbp_game_logs import PBPGameLogAdapter
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LedgerGameRow,
    ReboundTypeRegressionError,
    canonical_game_from_pbp,
    raw_checksum,
)
from app.services.ledger_backfill import (
    AcceptedObservationParticipantCatalog,
    CollectionObservationLedgerRecorder,
    LedgerBackfillService,
)
from app.services.ledger_derivations import governed_assist_locations, nominal_game_minutes
from app.services.ledger_materialization import LedgerCorrectionQueue


class _Athletes:
    def get_catalog(self, season, *, active_only=False):
        return [{"player_id": value} for value in (2544, 203507, 201935)]

    def get_freshness(self, season, *, now):
        return {"is_fresh": True, "row_count": 3}


class _Participants:
    def get_participants(self, observation_id, *, game_id):
        return {1610612747: (2544, 203507), 1610612759: (201935,)}


class _Provider:
    def __init__(self, payload, *, dates=None):
        self.payload = payload
        self.dates = dates or {}
        self.calls = []

    def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
        self.calls.append((game_id, season, season_type))
        payload = json.loads(json.dumps(self.payload))
        if game_id in self.dates:
            payload["date"] = self.dates[game_id]
        return payload


class _FinalScore:
    """The LiveData final score of the ``game_stats.valid.json`` game: LAL 40, SAS 25."""

    def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
        return {
            "game": {
                "gameId": game_id,
                "gameStatus": 3,
                "homeTeam": {"teamId": 1610612747, "teamTricode": "LAL", "score": 40},
                "awayTeam": {"teamId": 1610612759, "teamTricode": "SAS", "score": 25},
            }
        }

    def fetch_play_by_play(self, game_id):
        return {"game": {"gameId": game_id, "actions": []}}


class _Recorder:
    def __init__(self):
        self.count = 0
        self.staged = set()
        self.peak = 0
        self.lock = threading.Lock()

    def stage(
        self,
        observation,
        *,
        season,
        game_id,
        retrieved_at,
        manifest_id,
        manifest_scope,
        manifest_cutoff,
        schema_version,
    ):
        payload = json.dumps(observation, sort_keys=True, separators=(",", ":"), default=str)
        with self.lock:
            self.count += 1
            observation_id = f"accepted:{game_id}:{self.count}"
            self.staged.add(observation_id)
            self.peak = max(self.peak, len(self.staged))
        return observation_id, {
            "observation_id": observation_id,
            "client_observation_id": observation_id,
            "collector_id": "test",
            "manifest_id": manifest_id,
            "environment": "server",
            "provider": "pbp",
            "observation_type": "canonical_game_ledger",
            "scope": json.dumps({"game_id": game_id, "surface": manifest_scope}),
            "season": season,
            "cutoff": manifest_cutoff,
            "schema_version": schema_version,
            "checksum": hashlib.sha256(payload.encode()).hexdigest(),
            "payload": payload,
            "payload_bytes": len(payload.encode()),
            "retrieved_at": retrieved_at,
            "accepted_at": retrieved_at,
        }

    def get_staged(self, observation_id):
        return []

    def consume(self, observation_id):
        with self.lock:
            self.staged.discard(observation_id)

    def discard(self, observation_id):
        with self.lock:
            self.staged.discard(observation_id)


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
    # Acceptance holds each team to a whole game's 240 player-minutes, which
    # three players can carry only as a stand-in for a full rotation.
    for side, minutes in (("Home", "120:00"), ("Away", "240:00")):
        for row in payload["stats"][side]["FullGame"]:
            if row["EntityId"] != "0":
                row["Minutes"] = minutes
    payload["participant_ids_by_team"] = {
        "1610612747": [2544, 203507],
        "1610612759": [201935],
    }
    return payload


def _authorized(events, cutoff):
    governed = (events,) if isinstance(events, dict) else tuple(events)
    return {
        "cutoff": cutoff,
        "governed_events": governed,
        "manifest_id": "ledger-manifest",
        "manifest_scope": "canonical_game_ledger",
        "collect_before": cutoff + timedelta(days=45),
        "schema_version": 1,
        "accepted_versions": frozenset({1}),
    }


def _install_manifest(engine, cutoff, *, collect_before=None):
    deadline = collect_before or cutoff + timedelta(days=45)
    with engine.begin() as connection:
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="ledger-manifest",
                season="2024-25",
                cutoff=cutoff,
                collect_before=deadline,
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum=f"manifest:{cutoff.isoformat()}",
                status="active",
                created_at=cutoff,
            )
        )


def test_backfill_is_resumable_and_newest_first(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            EventCatalogEntry.__table__.insert().values(
                nba_game_id="0022400001",
                season="2024-25",
                home_team_id=1610612747,
                home_team_name="Lakers",
                home_team_tricode="LAL",
                away_team_id=1610612759,
                away_team_name="Spurs",
                away_team_tricode="SAS",
                scheduled_at=datetime(2024, 11, 15, tzinfo=UTC),
                status_text="Final",
                status_code=3,
                classification="Regular Season",
                first_seen_at=datetime(2024, 11, 15, tzinfo=UTC),
                last_seen_at=datetime(2024, 11, 16, tzinfo=UTC),
            )
        )
        connection.execute(
            ActiveSeason.__table__.insert().values(
                season="2024-25",
                phase="Regular Season",
                status="active",
                cutoff=datetime(2024, 11, 16, tzinfo=UTC),
                activated_at=datetime(2024, 11, 16, tzinfo=UTC),
                activated_by="test",
            )
        )
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="ledger-manifest",
                season="2024-25",
                cutoff=datetime(2024, 11, 16, tzinfo=UTC),
                collect_before=datetime(2024, 12, 31, tzinfo=UTC),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum="ledger-manifest",
                status="active",
                created_at=datetime(2024, 11, 16, tzinfo=UTC),
            )
        )
    payload = _payload()
    provider = _Provider(payload)
    repository = CanonicalGameLedgerRepository(
        engine,
        correction_sink=LedgerCorrectionQueue(
            require_governance=True,
            clock=lambda: datetime(2024, 11, 16, tzinfo=UTC),
        ),
    )
    service = LedgerBackfillService(
        provider=provider,
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=(recorder := _Recorder()),
        repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=UTC),
    )

    result = service.refresh(
        "2024-25",
        **_authorized(
            _event(),
            datetime(2024, 11, 16, tzinfo=UTC),
        ),
    )

    assert result.complete
    assert result.status == "complete"
    assert provider.calls == [("0022400001", "2024-25", "Regular Season")]
    assert recorder.staged == set()
    progress = repository.get_progress("2024-25")
    assert progress is not None
    assert progress.status == "complete"
    assert progress.completed_game_ids == frozenset({"0022400001"})
    with engine.connect() as connection:
        jobs = connection.execute(select(CompositionJob)).mappings().all()
        assert len(jobs) == len(LedgerCorrectionQueue.STREAMS)
        assert {job["manifest_id"] for job in jobs} == {"ledger-manifest"}
        accepted = connection.execute(select(CollectionObservation)).mappings().one()
    assert accepted["manifest_id"] == "ledger-manifest"
    assert accepted["cutoff"] == datetime(2024, 11, 16)
    assert json.loads(accepted["scope"])["surface"] == "canonical_game_ledger"


def test_backfill_uses_explicit_composite_after_primary_validation_fails(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'fallback.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    invalid = _payload()
    del invalid["team_results"]["Home"]["FullGame"]["Blocks"]
    fallback_calls = []

    class Fallback:
        def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
            fallback_calls.append((game_id, season, season_type))
            return _FinalScore().fetch_game_stats(game_id, season)

        def fetch_play_by_play(self, game_id):
            return {"game": {"gameId": game_id, "actions": []}}

    composite = _payload()
    composite["_ledger_provenance"] = {
        "provider": "pbp+nba_live_data",
        "source_documents": {"pbp": invalid, "nba_live_data": {"game": {}}},
    }
    monkeypatch.setattr(
        "app.services.ledger_backfill.compose_pbp_live_data_observation",
        lambda primary, live, *, event, live_play_by_play: composite,
    )
    repository = CanonicalGameLedgerRepository(engine)
    service = LedgerBackfillService(
        provider=_Provider(invalid),
        live_data_provider=Fallback(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: cutoff,
    )

    result = service.refresh("2024-25", **_authorized(_event(), cutoff))

    assert result.complete
    assert fallback_calls == [("0022400001", "2024-25", "Regular Season")]
    with engine.connect() as connection:
        accepted = connection.execute(select(CollectionObservation)).mappings().one()
    assert accepted["provider"] == "pbp+nba_live_data"


def test_backfill_rejects_missing_governance_before_provider_io(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ungoverned.sqlite3'}")
    run_migrations(engine)
    provider = _Provider(_payload())
    service = LedgerBackfillService(
        provider=provider,
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=CanonicalGameLedgerRepository(engine),
    )

    with pytest.raises(ValueError, match="authorized ledger manifest"):
        service.refresh("2024-25")
    with pytest.raises(ValueError, match="active authorized ledger manifest"):
        service.refresh(
            "2024-25",
            **_authorized(
                _event(),
                datetime(2024, 11, 16, tzinfo=UTC),
            ),
        )

    assert provider.calls == []


def test_rechecks_enforce_daily_and_weekly_cadence_and_allow_historical_repair(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cadence.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    now = datetime(2024, 12, 16, tzinfo=UTC)
    _install_manifest(engine, now)
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
        repository.replace_game(
            canonical_game_from_pbp(
                seed_payload,
                event=event,
                season="2024-25",
                source_observation_id=f"seed:{game_id}",
                retrieved_at=now - retrieved_age,
                participant_ids_by_team=_Participants().get_participants(game_id, game_id=game_id),
            )
        )

    provider = _Provider(
        _payload(),
        dates={event["nba_game_id"]: event["scheduled_at"][:10] for event in events},
    )
    service = LedgerBackfillService(
        provider=provider,
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: now,
    )

    authorization = _authorized(events, now)
    result = service.refresh("2024-25", max_games=1, **authorization)

    assert result.complete
    assert result.lower_priority_remaining == 1
    assert [call[0] for call in provider.calls] == ["daily-due"]

    provider.calls.clear()
    repaired = service.refresh(
        "2024-25",
        max_games=4,
        historical_repair=True,
        **authorization,
    )

    assert repaired.complete
    assert repaired.lower_priority_remaining == 0
    assert [call[0] for call in provider.calls][:2] == ["daily-due", "daily-not-due"]
    assert {call[0] for call in provider.calls} == {case[0] for case in cases}


def test_provider_retrieval_is_accepted_before_participants_are_read(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'observations.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            EventCatalogEntry.__table__.insert().values(
                nba_game_id="0022400001",
                season="2024-25",
                home_team_id=1610612747,
                home_team_name="Lakers",
                home_team_tricode="LAL",
                away_team_id=1610612759,
                away_team_name="Spurs",
                away_team_tricode="SAS",
                scheduled_at=datetime(2024, 11, 15, tzinfo=UTC),
                status_text="Final",
                status_code=3,
                classification="Regular Season",
                first_seen_at=datetime(2024, 11, 15, tzinfo=UTC),
                last_seen_at=datetime(2024, 11, 16, tzinfo=UTC),
            )
        )
    payload = _payload()
    frame = PBPGameLogAdapter.parse_game_stats(payload, game_id="0022400001")
    recorder = CollectionObservationLedgerRecorder(engine)

    observation_id, _values = recorder.stage(
        frame,
        season="2024-25",
        game_id="0022400001",
        retrieved_at=datetime(2024, 11, 16, tzinfo=UTC),
        manifest_id="manifest-1",
        manifest_scope="canonical_game_ledger",
        manifest_cutoff=datetime(2024, 11, 16, tzinfo=UTC),
        schema_version=1,
    )
    participants = AcceptedObservationParticipantCatalog(engine, recorder).get_participants(
        observation_id, game_id="0022400001"
    )

    assert observation_id
    assert participants == {1610612747: [2544, 203507], 1610612759: [201935]}

    second = frame.copy()
    second.loc[second.index[0], "EntityId"] = "999999"
    second_id, _second_values = recorder.stage(
        second,
        season="2024-25",
        game_id="0022400001",
        retrieved_at=datetime(2024, 11, 16, 0, 1, tzinfo=UTC),
        manifest_id="manifest-1",
        manifest_scope="canonical_game_ledger",
        manifest_cutoff=datetime(2024, 11, 16, tzinfo=UTC),
        schema_version=1,
    )
    second_participants = AcceptedObservationParticipantCatalog(engine, recorder).get_participants(
        second_id, game_id="0022400001"
    )

    assert participants[1610612747][0] == 2544
    assert second_participants[1610612747][0] == 999999
    recorder.consume(observation_id)
    try:
        recorder.get_staged(observation_id)
    except ValueError:
        pass
    else:
        raise AssertionError("consumed staged observation remained readable")
    assert recorder.get_staged(second_id) is not None
    recorder.discard(second_id)


def test_staged_observation_ids_are_concurrent_and_exactly_discarded(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'staging.sqlite3'}")
    run_migrations(engine)
    recorder = CollectionObservationLedgerRecorder(engine)
    now = datetime(2024, 11, 16, tzinfo=UTC)

    def stage(index):
        return recorder.stage(
            [{"GAME_ID": str(index), "EntityId": index + 1}],
            season="2024-25",
            game_id=str(index),
            retrieved_at=now,
            manifest_id="manifest",
            manifest_scope="canonical_game_ledger",
            manifest_cutoff=now,
            schema_version=1,
        )[0]

    with ThreadPoolExecutor(max_workers=8) as executor:
        observation_ids = tuple(executor.map(stage, range(40)))

    assert len(set(observation_ids)) == 40
    recorder.discard(observation_ids[0])
    assert recorder.get_staged(observation_ids[1]) is not None
    for observation_id in observation_ids[1:]:
        recorder.discard(observation_id)
    assert recorder.pending_count() == 0


def test_invalid_candidate_leaves_no_accepted_observation_ledger_or_jobs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'invalid.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    _install_manifest(engine, datetime(2024, 11, 16, tzinfo=UTC))

    class MissingParticipant:
        def get_participants(self, observation_id, *, game_id):
            return {1610612747: (2544,), 1610612759: (201935,)}

    recorder = _Recorder()
    result = LedgerBackfillService(
        provider=_Provider(_payload()),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=MissingParticipant(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=UTC),
    ).refresh(
        "2024-25",
        **_authorized(
            _event(),
            datetime(2024, 11, 16, tzinfo=UTC),
        ),
    )

    assert not result.complete
    with engine.connect() as connection:
        assert connection.execute(select(CollectionObservation)).all() == []
        assert connection.execute(select(CompositionJob)).all() == []
    assert repository.get_game("0022400001") is None
    assert recorder.staged == set()


def test_transaction_exception_discards_staged_observation(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'write-failure.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    _install_manifest(engine, datetime(2024, 11, 16, tzinfo=UTC))
    monkeypatch.setattr(
        repository,
        "replace_games_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write failed")),
    )
    recorder = _Recorder()

    result = LedgerBackfillService(
        provider=_Provider(_payload()),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=UTC),
    ).refresh(
        "2024-25",
        **_authorized(
            _event(),
            datetime(2024, 11, 16, tzinfo=UTC),
        ),
    )

    assert not result.complete
    assert recorder.staged == set()


def test_deadline_crossed_during_provider_call_cannot_accept_observation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'deadline.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    recorder = _Recorder()
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    deadline = cutoff + timedelta(minutes=1)
    _install_manifest(engine, cutoff, collect_before=deadline)
    clock_values = iter((cutoff, deadline, deadline))

    result = LedgerBackfillService(
        provider=_Provider(_payload()),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: next(clock_values),
    ).refresh(
        "2024-25",
        cutoff=cutoff,
        governed_events=(_event(),),
        manifest_id="ledger-manifest",
        collect_before=deadline,
        accepted_versions=frozenset({1}),
    )

    assert not result.complete
    assert recorder.staged == set()
    with engine.connect() as connection:
        assert connection.execute(select(CollectionObservation)).all() == []


def test_valid_game_commits_when_later_target_fails(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    _install_manifest(engine, datetime(2024, 11, 16, tzinfo=UTC))
    valid_event = _event()
    invalid_event = {**_event(), "nba_game_id": "0022400002"}

    class PartialProvider:
        def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
            return _payload() if game_id == "0022400001" else {"stats": {}}

    result = LedgerBackfillService(
        provider=PartialProvider(),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=UTC),
    ).refresh(
        "2024-25",
        **_authorized(
            (valid_event, invalid_event),
            datetime(2024, 11, 16, tzinfo=UTC),
        ),
    )

    assert not result.complete
    assert result.failed_game_ids == ("0022400002",)
    assert repository.get_game("0022400001") is not None
    assert repository.get_game("0022400002") is None
    with engine.connect() as connection:
        assert len(connection.execute(select(CollectionObservation)).all()) == 1


def test_many_games_never_retain_more_documents_than_worker_concurrency(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bounded-staging.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    now = datetime(2024, 11, 30, tzinfo=UTC)
    _install_manifest(engine, now)
    events = []
    dates = {}
    for index in range(12):
        event = {**_event(), "nba_game_id": f"bounded-{index}"}
        event["scheduled_at"] = (now - timedelta(days=12 - index)).isoformat()
        events.append(event)
        dates[event["nba_game_id"]] = event["scheduled_at"][:10]
    recorder = CollectionObservationLedgerRecorder(engine)

    class ExactParticipants:
        def get_participants(self, observation_id, *, game_id):
            assert recorder.get_staged(observation_id) is not None
            return _Participants().get_participants(observation_id, game_id=game_id)

    LedgerBackfillService(
        provider=_Provider(_payload(), dates=dates),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=ExactParticipants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=3,
        clock=lambda: now,
    ).refresh("2024-25", **_authorized(events, now))

    assert recorder.peak_pending_count() <= 3
    assert recorder.pending_count() == 0


def test_unexpected_future_exception_discards_all_staging_after_prior_success(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'future-exception.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    now = datetime(2024, 11, 20, tzinfo=UTC)
    _install_manifest(engine, now)
    success = {**_event(), "nba_game_id": "success"}
    failure = {
        **_event(),
        "nba_game_id": "failure",
        "scheduled_at": "2024-11-14T00:00:00+00:00",
    }

    class UnexpectedProvider(_Provider):
        def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
            if game_id == "failure":
                raise AssertionError("unexpected worker failure")
            return super().fetch_game_stats(
                game_id,
                season,
                season_type=season_type,
            )

    recorder = _Recorder()
    service = LedgerBackfillService(
        provider=UnexpectedProvider(_payload()),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: now,
    )

    with pytest.raises(AssertionError, match="unexpected worker failure"):
        service.refresh("2024-25", **_authorized((success, failure), now))

    assert repository.get_game("success") is not None
    assert recorder.staged == set()


def test_manifest_superseded_while_provider_in_flight_commits_nothing_for_game(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manifest-race.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        correction_sink=LedgerCorrectionQueue(
            require_governance=True,
            clock=lambda: datetime(2024, 11, 20, tzinfo=UTC),
        ),
    )
    now = datetime(2024, 11, 20, tzinfo=UTC)
    _install_manifest(engine, now)
    success = {**_event(), "nba_game_id": "race-success"}
    superseded = {
        **_event(),
        "nba_game_id": "race-superseded",
        "scheduled_at": "2024-11-14T00:00:00+00:00",
    }

    class SupersedingProvider(_Provider):
        def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
            if game_id == "race-superseded":
                with engine.begin() as connection:
                    connection.execute(
                        CollectionManifest.__table__.update().values(
                            status="superseded",
                            superseded_at=now,
                        )
                    )
            return super().fetch_game_stats(
                game_id,
                season,
                season_type=season_type,
            )

    recorder = _Recorder()
    result = LedgerBackfillService(
        provider=SupersedingProvider(
            _payload(),
            dates={
                "race-success": "2024-11-15",
                "race-superseded": "2024-11-14",
            },
        ),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: now,
    ).refresh("2024-25", **_authorized((success, superseded), now))

    assert not result.complete
    assert repository.get_game("race-success") is not None
    assert repository.get_game("race-superseded") is None
    assert recorder.staged == set()
    with engine.connect() as connection:
        observations = connection.execute(select(CollectionObservation)).mappings().all()
        jobs = connection.execute(select(CompositionJob)).mappings().all()
    assert {row["observation_id"] for row in observations} == {
        repository.get_game("race-success").source_observation_id,
    }
    assert len(jobs) == len(LedgerCorrectionQueue.STREAMS)


def _production_adapter(payload):
    from app.config.settings import ProviderSettings, RuntimeSettings

    class FixtureResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FixtureSession:
        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def get(self, url, params, timeout):
            self.calls.append((url, params, timeout))
            return FixtureResponse(self.payload)

    session = FixtureSession(payload)
    adapter = PBPGameLogAdapter(
        RuntimeSettings(
            environment="testing",
            providers=ProviderSettings(
                pbp_connect_timeout_seconds=1.0,
                pbp_read_timeout_seconds=2.0,
            ),
        ),
        session=session,
    )
    return adapter, session


def _seed_participant_event(engine, cutoff):
    with engine.begin() as connection:
        connection.execute(
            EventCatalogEntry.__table__.insert().values(
                nba_game_id="0022400001",
                season="2024-25",
                home_team_id=1610612747,
                home_team_name="Lakers",
                home_team_tricode="LAL",
                away_team_id=1610612759,
                away_team_name="Spurs",
                away_team_tricode="SAS",
                scheduled_at=datetime(2024, 11, 15, tzinfo=UTC),
                status_text="Final",
                status_code=3,
                classification="Regular Season",
                first_seen_at=datetime(2024, 11, 15, tzinfo=UTC),
                last_seen_at=cutoff,
            )
        )


def test_production_adapter_backfill_archives_complete_raw_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'production.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    _seed_participant_event(engine, cutoff)
    adapter, session = _production_adapter(_payload())
    recorder = CollectionObservationLedgerRecorder(engine)
    repository = CanonicalGameLedgerRepository(
        engine,
        correction_sink=LedgerCorrectionQueue(
            require_governance=True,
            clock=lambda: cutoff,
        ),
    )
    service = LedgerBackfillService(
        provider=adapter,
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=AcceptedObservationParticipantCatalog(engine, recorder),
        reconciliation_sink=lambda game_id, details: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: cutoff,
    )

    result = service.refresh("2024-25", **_authorized(_event(), cutoff))

    assert result.complete
    assert [call[1]["GameId"] for call in session.calls] == ["0022400001"]
    stored = repository.get_game("0022400001")
    assert stored is not None
    assert len(stored.raw_rows) == 5
    team_rows = {row.side: row for row in stored.raw_rows if row.row_type == "team"}
    assert team_rows.keys() == {"Home", "Away"}
    assert team_rows["Home"].team_id == 1610612747
    assert team_rows["Away"].team_id == 1610612759
    player_entities = {row.entity_id for row in stored.raw_rows if row.row_type == "player"}
    assert player_entities == {2544, 203507, 201935}
    wemby = next(row for row in stored.raw_rows if row.entity_id == 201935)
    assert wemby.payload["Name"] == "Victor Wembanyama"
    assert wemby.payload["Points"] == 25
    assert wemby.payload["Assists"] == 3
    with engine.connect() as connection:
        raw_rows = connection.execute(select(LedgerGameRowEvidence)).mappings().all()
    assert len(raw_rows) == 5
    assert sum(row["row_type"] == "team" for row in raw_rows) == 2
    assert {row["side"] for row in raw_rows if row["row_type"] == "team"} == {"Home", "Away"}
    reconstructed = tuple(
        LedgerGameRow(
            game_id=row["game_id"],
            row_type=row["row_type"],
            side=row["side"],
            row_index=row["row_index"],
            entity_id=row["entity_id"],
            entity_name=row["entity_name"],
            team_id=row["team_id"],
            payload=json.loads(row["payload"]),
            checksum=row["checksum"],
            observed_fields=tuple(json.loads(row["observed_fields"] or "[]")),
            schema_version=row["schema_version"],
        )
        for row in raw_rows
    )
    assert raw_checksum(reconstructed) == stored.raw_checksum


def test_null_additive_count_is_a_governed_zero_through_the_production_backfill_seam(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill_null_count.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    _seed_participant_event(engine, cutoff)
    payload = _payload()
    # The provider wire is sparse: a null or omitted additive counter is an
    # observed zero, so the game ingests atomically through the production
    # adapter seam rather than being rejected as missing evidence.  Reaves
    # finished with no blocks and the complete team_results diagnostic
    # (Home Blocks 1) reconciles against the retained player sum plus the zero
    # residual, so the null is a proven governed zero.
    next(row for row in payload["stats"]["Home"]["FullGame"] if row.get("EntityId") == "203507")["Blocks"] = (
        None
    )
    adapter, _session = _production_adapter(payload)
    recorder = CollectionObservationLedgerRecorder(engine)
    repository = CanonicalGameLedgerRepository(engine)

    result = LedgerBackfillService(
        provider=adapter,
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=AcceptedObservationParticipantCatalog(engine, recorder),
        reconciliation_sink=lambda game_id, details: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: cutoff,
    ).refresh("2024-25", **_authorized(_event(), cutoff))

    assert result.complete
    stored = repository.get_game("0022400001")
    assert stored is not None
    reaves = next(fact for fact in stored.player_facts if fact.player_id == 203507)
    assert reaves.blocks == 0


def test_null_provably_nonzero_count_rejects_through_the_production_backfill_seam(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill_null_count_reject.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    _seed_participant_event(engine, cutoff)
    payload = _payload()
    # Wembanyama finished with three assists and the complete Away diagnostic
    # (Assists 3) proves it, so a null Assists value cannot be a governed zero:
    # the diagnostic reconciliation fails and the candidate rejects atomically
    # through the production backfill seam with nothing written.
    next(row for row in payload["stats"]["Away"]["FullGame"] if row.get("EntityId") == "201935")[
        "Assists"
    ] = None
    adapter, _session = _production_adapter(payload)
    recorder = CollectionObservationLedgerRecorder(engine)
    repository = CanonicalGameLedgerRepository(engine)

    result = LedgerBackfillService(
        provider=adapter,
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=AcceptedObservationParticipantCatalog(engine, recorder),
        reconciliation_sink=lambda game_id, details: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: cutoff,
    ).refresh("2024-25", **_authorized(_event(), cutoff))

    assert not result.complete
    assert repository.get_game("0022400001") is None
    assert recorder.pending_count() == 0
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(CollectionObservation)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_missing_minutes_rejects_through_the_production_backfill_seam(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill_missing_minutes.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    _seed_participant_event(engine, cutoff)
    payload = _payload()
    # Minutes is genuinely required identity evidence: it is never zero-filled,
    # so a player row missing it fails the whole candidate through the
    # production backfill seam and nothing is written.
    next(row for row in payload["stats"]["Away"]["FullGame"] if row.get("EntityId") == "201935").pop(
        "Minutes", None
    )
    adapter, _session = _production_adapter(payload)
    recorder = CollectionObservationLedgerRecorder(engine)
    repository = CanonicalGameLedgerRepository(engine)

    result = LedgerBackfillService(
        provider=adapter,
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=AcceptedObservationParticipantCatalog(engine, recorder),
        reconciliation_sink=lambda game_id, details: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: cutoff,
    ).refresh("2024-25", **_authorized(_event(), cutoff))

    assert not result.complete
    assert repository.get_game("0022400001") is None
    assert recorder.pending_count() == 0
    with engine.connect() as connection:
        assert connection.execute(select(CanonicalGameLedgerGame)).all() == []
        assert connection.execute(select(CollectionObservation)).all() == []
        assert connection.execute(select(LedgerGameRowEvidence)).all() == []


def test_pre_migration_ledger_games_are_refetched_until_raw_evidence_exists(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre032.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    now = datetime(2025, 1, 15, tzinfo=UTC)
    _install_manifest(engine, now)
    event = dict(_event())
    event["nba_game_id"] = "0022400001"
    event["scheduled_at"] = (now - timedelta(days=60)).isoformat()
    # A game accepted before migration 032: typed facts only, no raw rows, and
    # a NULL raw_checksum.  It is older than every recheck window, so only the
    # raw-evidence check can make the backfill re-fetch it.
    with engine.begin() as connection:
        connection.execute(
            CanonicalGameLedgerGame.__table__.insert().values(
                game_id="0022400001",
                season="2024-25",
                season_type="Regular Season",
                game_date=date(2024, 11, 15),
                home_team_id=1610612747,
                home_team_tricode="LAL",
                away_team_id=1610612759,
                away_team_tricode="SAS",
                status="final",
                source_observation_id="legacy:0022400001",
                checksum="legacy-typed-checksum",
                raw_checksum=None,
                retrieved_at=datetime(2024, 11, 16, tzinfo=UTC),
                updated_at=datetime(2024, 11, 16, tzinfo=UTC),
            )
        )
    assert repository.game_ids_without_raw_evidence("2024-25") == frozenset({"0022400001"})

    provider = _Provider(_payload(), dates={"0022400001": "2024-11-15"})
    result = LedgerBackfillService(
        provider=provider,
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: now,
    ).refresh("2024-25", **_authorized(event, now))

    assert result.complete
    assert [call[0] for call in provider.calls] == ["0022400001"]
    stored = repository.get_game("0022400001")
    assert stored is not None
    assert stored.raw_checksum is not None
    assert len(stored.raw_rows) == 5
    assert repository.game_ids_without_raw_evidence("2024-25") == frozenset()


def test_historical_repair_resumes_after_current_manifest_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'replay.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    provider = _Provider(_payload())

    def service():
        return LedgerBackfillService(
            provider=provider,
            live_data_provider=_FinalScore(),
            athlete_catalog=_Athletes(),
            participant_catalog=_Participants(),
            reconciliation_sink=lambda game_id, payload: None,
            observation_recorder=_Recorder(),
            repository=repository,
            max_concurrency=1,
            clock=lambda: cutoff,
        )

    first = service().refresh("2024-25", **_authorized(_event(), cutoff))
    assert first.complete
    with engine.connect() as connection:
        assert len(connection.execute(select(CollectionObservation)).all()) == 1

    provider.calls.clear()
    replayed = service().refresh(
        "2024-25",
        historical_repair=True,
        **_authorized(_event(), cutoff),
    )

    assert replayed.complete
    assert replayed.games_replaced == 0
    assert provider.calls == []
    with engine.connect() as connection:
        observations = connection.execute(select(CollectionObservation)).mappings().all()
        stored = repository.get_game("0022400001")
    assert len(observations) == 1
    assert stored is not None
    assert observations[0]["observation_id"] == stored.source_observation_id


def test_historical_repair_replaces_fallback_provenance_with_pbp(tmp_path, monkeypatch):
    """A LiveData-filled game stays a repair target until PBP replaces it."""
    from app.services.collection_control import CollectionControlService, CollectionOperationsService
    from app.services.ledger_backfill import collection_control_reconciliation_sink

    engine = create_engine(f"sqlite:///{tmp_path / 'fallback-repair.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    repository = CanonicalGameLedgerRepository(engine)

    invalid = _payload()
    del invalid["team_results"]["Home"]["FullGame"]["Blocks"]
    composite = _payload()
    # The fallback composite carries no assist-location observation.
    for side in ("Home", "Away"):
        for row in composite["stats"][side]["FullGame"]:
            for key in (
                "TwoPtAssists",
                "ThreePtAssists",
                "Arc3Assists",
                "Corner3Assists",
                "AtRimAssists",
                "ShortMidRangeAssists",
                "LongMidRangeAssists",
            ):
                row.pop(key, None)
    composite["_ledger_provenance"] = {
        "provider": "pbp+nba_live_data",
        "source_documents": {"pbp": invalid, "nba_live_data": {"game": {}}},
    }
    monkeypatch.setattr(
        "app.services.ledger_backfill.compose_pbp_live_data_observation",
        lambda primary, live, *, event, live_play_by_play: composite,
    )

    class Fallback:
        def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
            return _FinalScore().fetch_game_stats(game_id, season)

        def fetch_play_by_play(self, game_id):
            return {"game": {"gameId": game_id, "actions": []}}

    clock = {"now": cutoff}

    def service(provider):
        return LedgerBackfillService(
            provider=provider,
            live_data_provider=Fallback(),
            athlete_catalog=_Athletes(),
            participant_catalog=_Participants(),
            reconciliation_sink=collection_control_reconciliation_sink(
                CollectionControlService(engine), default_season="2024-25"
            ),
            observation_recorder=_Recorder(),
            repository=repository,
            max_concurrency=1,
            clock=lambda: clock["now"],
        )

    def items():
        return [
            (item["kind"], item["details"], item["status"])
            for item in CollectionOperationsService(engine).diagnostics()["reconciliation"]
        ]

    first = service(_Provider(invalid)).refresh("2024-25", **_authorized(_event(), cutoff))
    assert items() == [
        (
            "ledger_fallback_evidence",
            {"game_id": "0022400001", "missing_families": "assist_locations"},
            "open",
        )
    ]
    assert first.complete
    assert repository.game_ids_with_fallback_provenance("2024-25", "ledger-manifest") == {"0022400001"}
    assert repository.game_ids_from_manifest("2024-25", "ledger-manifest") == {"0022400001"}

    # PBP now serves a complete observation: repair must re-fetch and replace.
    clock["now"] = cutoff + timedelta(hours=1)
    healthy = _Provider(_payload())
    replayed = service(healthy).refresh(
        "2024-25",
        historical_repair=True,
        **_authorized(_event(), cutoff),
    )

    assert replayed.complete
    assert healthy.calls == [("0022400001", "2024-25", "Regular Season")]
    assert replayed.games_replaced == 1
    assert repository.game_ids_with_fallback_provenance("2024-25", "ledger-manifest") == frozenset()
    with engine.connect() as connection:
        providers = [row["provider"] for row in connection.execute(select(CollectionObservation)).mappings()]
    assert "pbp" in providers
    stored = repository.get_game("0022400001")
    assert stored is not None
    assert any(player.at_rim_assists is not None for player in stored.player_facts)
    assert items() == [
        (
            "ledger_fallback_evidence",
            {"game_id": "0022400001", "missing_families": "assist_locations"},
            "resolved",
        )
    ]


def test_swapped_wire_sides_are_accepted_through_the_production_seam(tmp_path):
    """The real backfill path: staged original document, participant catalog,
    accepted-observation binding, archived sides in governed orientation."""
    engine = create_engine(f"sqlite:///{tmp_path / 'swapped-seam.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    cutoff = datetime(2024, 11, 16, 0, 0, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    _seed_participant_event(engine, cutoff)
    recorder = CollectionObservationLedgerRecorder(engine)

    straight = _payload()
    swapped = json.loads(json.dumps(straight))
    swapped["home_team_id"], swapped["away_team_id"] = straight["away_team_id"], straight["home_team_id"]
    swapped["home_team_abbreviation"], swapped["away_team_abbreviation"] = (
        straight["away_team_abbreviation"],
        straight["home_team_abbreviation"],
    )
    for envelope in ("stats", "team_results"):
        swapped[envelope] = {"Home": straight[envelope]["Away"], "Away": straight[envelope]["Home"]}

    result = LedgerBackfillService(
        provider=_Provider(swapped),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=AcceptedObservationParticipantCatalog(engine, recorder),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: cutoff,
    ).refresh("2024-25", **_authorized(_event(), cutoff))

    assert result.complete and result.failed_game_ids == ()
    stored = repository.get_game("0022400001")
    assert stored is not None
    assert stored.home_team_id == 1610612747 and stored.away_team_id == 1610612759
    assert {p.player_id for p in stored.player_facts if p.team_id == 1610612747} == {2544, 203507}
    assert {p.player_id for p in stored.player_facts if p.team_id == 1610612759} == {201935}
    with engine.connect() as connection:
        observation = connection.execute(select(CollectionObservation)).mappings().one()
        raw_rows = connection.execute(select(LedgerGameRowEvidence)).mappings().all()
    # The retained observation is the provider's own (swapped) document ...
    assert json.loads(observation["payload"])["home_team_id"] == straight["away_team_id"]
    # ... while the archive carries governed sides.
    assert all(row["side"] == ("Home" if row["team_id"] == 1610612747 else "Away") for row in raw_rows)


def test_each_observation_records_its_own_retrieval_time(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retrieval.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    cutoff = datetime(2024, 11, 16, 0, 0, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    _seed_participant_event(engine, cutoff)
    batch_start = datetime(2024, 11, 16, 0, 0, tzinfo=UTC)
    retrieval = datetime(2024, 11, 16, 0, 5, tzinfo=UTC)
    clock_values = iter((batch_start, retrieval, retrieval))
    recorder = CollectionObservationLedgerRecorder(engine)

    result = LedgerBackfillService(
        provider=_Provider(_payload()),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=AcceptedObservationParticipantCatalog(engine, recorder),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: next(clock_values),
    ).refresh("2024-25", **_authorized(_event(), cutoff))

    assert result.complete
    stored = repository.get_game("0022400001")
    assert stored is not None
    assert stored.retrieved_at == retrieval
    with engine.connect() as connection:
        observation = connection.execute(select(CollectionObservation)).mappings().one()
        raw_rows = connection.execute(select(LedgerGameRowEvidence)).mappings().all()
    assert observation["retrieved_at"].replace(tzinfo=UTC) == retrieval
    assert observation["retrieved_at"] != batch_start
    assert {row["retrieved_at"].replace(tzinfo=UTC) for row in raw_rows} == {retrieval}


def _livedata_only_service(tmp_path, game_id, play_by_play, reconciliation_sink, clock=None):
    from tests.providers.test_nba_live_data import _recorded

    box, _, event = _recorded(game_id)
    cutoff = datetime(2026, 10, 25, tzinfo=UTC)
    clock = clock or (lambda: cutoff)
    engine = create_engine(f"sqlite:///{tmp_path / 'livedata-only.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="ledger-manifest",
                season="2026-27",
                cutoff=cutoff,
                collect_before=cutoff + timedelta(days=45),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum="manifest:2026-27",
                status="active",
                created_at=cutoff,
            )
        )
    players = {
        team["teamId"]: tuple(p["personId"] for p in team["players"] if p["played"] == "1")
        for team in (box["game"]["homeTeam"], box["game"]["awayTeam"])
    }

    class Athletes:
        def get_catalog(self, season, *, active_only=False):
            return [{"player_id": player} for team in players.values() for player in team]

        def get_freshness(self, season, *, now):
            return {"is_fresh": True}

    class Participants:
        def get_participants(self, observation_id, *, game_id):
            return players

    class LiveData:
        def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
            return box

        def fetch_play_by_play(self, game_id):
            return play_by_play

    repository = CanonicalGameLedgerRepository(engine)
    service = LedgerBackfillService(
        provider=_Provider({}),
        live_data_provider=LiveData(),
        athlete_catalog=Athletes(),
        participant_catalog=Participants(),
        reconciliation_sink=reconciliation_sink(engine),
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=clock,
    )
    return engine, repository, service, _authorized(event, cutoff)


def test_a_game_pbpstats_never_serves_is_accepted_complete_from_livedata(tmp_path):
    from app.services.ledger_derivations import derive_assist_location_facts
    from tests.providers.test_nba_live_data import _recorded

    _, play_by_play, _ = _recorded("0022600009")
    reports = []
    _, repository, service, authorized = _livedata_only_service(
        tmp_path, "0022600009", play_by_play, lambda engine: lambda *report: reports.append(report)
    )

    result = service.refresh("2026-27", **authorized)

    stored = repository.get_game("0022600009")
    assert result.complete
    assert len(derive_assist_location_facts((stored,))) == 19
    assert sum(player.def_two_pt_rebounds for player in stored.player_facts) == 34
    assert reports == [
        ("0022600009", {"kind": "fallback_evidence", "season": "2026-27", "missing_families": ()})
    ]


def test_a_fallback_game_keeps_one_item_naming_what_it_still_lacks_until_its_evidence_is_complete(tmp_path):
    import copy

    from app.services.collection_control import CollectionControlService, CollectionOperationsService
    from app.services.ledger_backfill import collection_control_reconciliation_sink
    from tests.providers.test_nba_live_data import _recorded

    _, play_by_play, _ = _recorded("0022600009")
    complete = copy.deepcopy(play_by_play["game"]["actions"])
    rebound = next(a for a in complete if a["actionType"] == "rebound" and a.get("personId"))
    assisted = next(a for a in complete if a.get("assistPersonId"))
    without_rebound = [a for a in complete if a["actionNumber"] != rebound["actionNumber"]]
    without_either = [
        {key: value for key, value in a.items() if key != "assistPersonId"}
        if a["actionNumber"] == assisted["actionNumber"]
        else a
        for a in without_rebound
    ]
    actions = play_by_play["game"]["actions"]
    actions[:] = without_either
    now = [datetime(2026, 10, 25, tzinfo=UTC)]
    engine, repository, service, authorized = _livedata_only_service(
        tmp_path,
        "0022600009",
        play_by_play,
        lambda engine: collection_control_reconciliation_sink(
            CollectionControlService(engine), default_season="2026-27"
        ),
        clock=lambda: now[0],
    )

    def refresh(**options):
        now[0] += timedelta(hours=1)
        return service.refresh("2026-27", **authorized, **options)

    def items():
        return [
            (item["season"], item["kind"], item["details"], item["status"])
            for item in CollectionOperationsService(engine).diagnostics()["reconciliation"]
        ]

    first = refresh()
    after_first = items()
    second = refresh(historical_repair=True)
    after_rerun = items()
    actions[:] = without_rebound
    refresh(historical_repair=True)
    after_assists_repaired = items()
    actions[:] = complete
    service.refresh("2026-27", **authorized, historical_repair=True)
    after_stale_complete = items()
    refresh(historical_repair=True)
    after_complete = items()

    assert (first.complete, second.complete, second.games_processed) == (True, True, 1)
    gap = ("2026-27", "ledger_fallback_evidence")
    assert after_first == [
        (*gap, {"game_id": "0022600009", "missing_families": "assist_locations,rebound_types"}, "open")
    ]
    assert after_rerun == after_first
    assert after_assists_repaired == [
        (*gap, {"game_id": "0022600009", "missing_families": "rebound_types"}, "open")
    ]
    assert after_stale_complete == after_assists_repaired
    assert after_complete == [
        (*gap, {"game_id": "0022600009", "missing_families": "rebound_types"}, "resolved")
    ]
    assert repository.get_game("0022600009").player_facts[0].def_two_pt_rebounds is not None


GSW, MEM = 1610612744, 1610612763
#: NBA.com's final score of 0022600101 (MEM @ GSW, 2026-10-23).
FINAL_0022600101 = {GSW: 108, MEM: 111}
QUINTEN_POST = 1642366


def _truncated_pbp():
    """PBPStats' 0022600101: the last 31 seconds of the first period are lost.

    NBA's scorer logged the first period's end after the game ended, and
    PBPStats clocks that period at 11:29 for both teams.  MEM's Quinten Post
    scored 2 points in the lost time, so the rows sum to 108-109, not 108-111.
    Only the FullGame rows of ``stats`` are kept; ``team_results`` is whole.
    """

    return json.loads(Path("tests/fixtures/pbp_stats/game_stats_0022600101_q1_truncated.json").read_text())


def _box_score_0022600101():
    return json.loads(Path("tests/fixtures/nba_live_data/boxscore_0022600101.json").read_text())


def _event_0022600101():
    return {
        "nba_game_id": "0022600101",
        "season": "2026-27",
        "classification": "Regular Season",
        "scheduled_at": "2026-10-24T02:00:00+00:00",
        "home_team_id": GSW,
        "home_team_tricode": "GSW",
        "away_team_id": MEM,
        "away_team_tricode": "MEM",
        "status_code": 3,
        "status_text": "Final",
    }


def _play_by_play_0022600101():
    return json.loads(Path("tests/fixtures/nba_live_data/playbyplay_heaves_0022600101.json").read_text())


def _heave_free_play_by_play_0022600101():
    # Without its one GSW heave, the truncated PBPStats rows reach the
    # whole-game check instead of failing the heave attempt check first.
    play_by_play = _play_by_play_0022600101()
    actions = play_by_play["game"]["actions"]
    play_by_play["game"]["actions"] = [action for action in actions if action["actionType"] != "heave"]
    return play_by_play


class _Documents:
    def __init__(self, document, *, play_by_play=None):
        self.document = document
        self.play_by_play = _heave_free_play_by_play_0022600101() if play_by_play is None else play_by_play
        self.calls = []
        self.play_by_play_calls = []

    def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
        self.calls.append(game_id)
        if isinstance(self.document, Exception):
            raise self.document
        return json.loads(json.dumps(self.document))

    def fetch_play_by_play(self, game_id):
        self.play_by_play_calls.append(game_id)
        if isinstance(self.play_by_play, Exception):
            raise self.play_by_play
        return json.loads(json.dumps(self.play_by_play))


class _AthletesIn:
    def __init__(self, document):
        game = document["game"]
        self.ids = sorted(
            player["personId"] for team in (game["homeTeam"], game["awayTeam"]) for player in team["players"]
        )

    def get_catalog(self, season, *, active_only=False):
        return [{"player_id": value} for value in self.ids]

    def get_freshness(self, season, *, now):
        return {"is_fresh": True, "row_count": len(self.ids)}


def _ledger_0022600101(tmp_path, cutoff):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger-0022600101.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="ledger-manifest",
                season="2026-27",
                cutoff=cutoff,
                collect_before=cutoff + timedelta(days=45),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum=f"manifest:{cutoff.isoformat()}",
                status="active",
                created_at=cutoff,
            )
        )
        connection.execute(
            EventCatalogEntry.__table__.insert().values(
                nba_game_id="0022600101",
                season="2026-27",
                home_team_id=GSW,
                home_team_name="Warriors",
                home_team_tricode="GSW",
                away_team_id=MEM,
                away_team_name="Grizzlies",
                away_team_tricode="MEM",
                scheduled_at=datetime(2026, 10, 24, 2, tzinfo=UTC),
                status_text="Final",
                status_code=3,
                classification="Regular Season",
                first_seen_at=datetime(2026, 10, 24, tzinfo=UTC),
                last_seen_at=cutoff,
            )
        )
    return engine


def _service_0022600101(engine, *, pbp, live_data, now):
    recorder = CollectionObservationLedgerRecorder(engine)
    return LedgerBackfillService(
        provider=pbp,
        live_data_provider=live_data,
        athlete_catalog=_AthletesIn(_box_score_0022600101()),
        participant_catalog=AcceptedObservationParticipantCatalog(engine, recorder),
        reconciliation_sink=lambda game_id, details: None,
        observation_recorder=recorder,
        repository=CanonicalGameLedgerRepository(engine),
        max_concurrency=1,
        clock=lambda: now,
    )


def _points_by_team(game):
    points = {}
    for player in game.player_facts:
        points[player.team_id] = points.get(player.team_id, 0) + player.points
    return points


def _accepted_provider(engine, game):
    with engine.connect() as connection:
        return connection.scalar(
            select(CollectionObservation.provider).where(
                CollectionObservation.observation_id == game.source_observation_id
            )
        )


def test_a_pbp_game_short_of_its_period_clock_is_accepted_from_the_live_data_box_score(tmp_path):
    cutoff = datetime(2026, 10, 25, 10, tzinfo=UTC)
    engine = _ledger_0022600101(tmp_path, cutoff)
    service = _service_0022600101(
        engine, pbp=_Documents(_truncated_pbp()), live_data=_Documents(_box_score_0022600101()), now=cutoff
    )

    result = service.refresh("2026-27", **_authorized(_event_0022600101(), cutoff))

    assert result.complete
    stored = CanonicalGameLedgerRepository(engine).get_game("0022600101")
    assert stored is not None
    assert _points_by_team(stored) == FINAL_0022600101
    assert next(player.points for player in stored.player_facts if player.player_id == QUINTEN_POST) == 2
    assert nominal_game_minutes(stored) == 48.0
    assert _accepted_provider(engine, stored) == "nba_live_data"


def test_a_truncated_pbp_game_with_a_heave_is_accepted_from_the_live_data_box_score(tmp_path):
    cutoff = datetime(2026, 10, 25, 10, tzinfo=UTC)
    engine = _ledger_0022600101(tmp_path, cutoff)
    live_data = _Documents(_box_score_0022600101(), play_by_play=_play_by_play_0022600101())
    service = _service_0022600101(engine, pbp=_Documents(_truncated_pbp()), live_data=live_data, now=cutoff)

    result = service.refresh("2026-27", **_authorized(_event_0022600101(), cutoff))

    assert result.complete
    stored = CanonicalGameLedgerRepository(engine).get_game("0022600101")
    assert _points_by_team(stored) == FINAL_0022600101
    assert _accepted_provider(engine, stored) == "nba_live_data"
    assert live_data.calls == ["0022600101"]


def test_a_pbp_game_whose_points_miss_the_final_score_is_accepted_from_the_live_data_box_score(tmp_path):
    # The same lost events behind a regulation clock: the points still say so.
    pbp = _truncated_pbp()
    for side in ("Home", "Away"):
        pbp["team_results"][side]["1"]["Minutes"] = "12:00"
    cutoff = datetime(2026, 10, 25, 10, tzinfo=UTC)
    engine = _ledger_0022600101(tmp_path, cutoff)
    service = _service_0022600101(
        engine, pbp=_Documents(pbp), live_data=_Documents(_box_score_0022600101()), now=cutoff
    )

    result = service.refresh("2026-27", **_authorized(_event_0022600101(), cutoff))

    assert result.complete
    stored = CanonicalGameLedgerRepository(engine).get_game("0022600101")
    assert stored is not None
    assert _points_by_team(stored) == FINAL_0022600101
    assert _accepted_provider(engine, stored) == "nba_live_data"


def test_a_live_data_box_score_whose_players_miss_its_score_is_refused(tmp_path):
    box = _box_score_0022600101()
    post = next(player for player in box["game"]["awayTeam"]["players"] if player["personId"] == QUINTEN_POST)
    post["statistics"]["points"] = 0
    post["statistics"]["fieldGoalsMade"] = 0
    post["statistics"]["twoPointersMade"] = 0
    cutoff = datetime(2026, 10, 25, 10, tzinfo=UTC)
    engine = _ledger_0022600101(tmp_path, cutoff)
    service = _service_0022600101(
        engine, pbp=_Documents(_truncated_pbp()), live_data=_Documents(box), now=cutoff
    )

    result = service.refresh("2026-27", **_authorized(_event_0022600101(), cutoff))

    assert not result.complete
    assert result.failed_game_ids == ("0022600101",)
    assert CanonicalGameLedgerRepository(engine).get_game("0022600101") is None


def _refused_0022600101(tmp_path, box):
    cutoff = datetime(2026, 10, 25, 10, tzinfo=UTC)
    engine = _ledger_0022600101(tmp_path, cutoff)
    service = _service_0022600101(
        engine, pbp=_Documents(_truncated_pbp()), live_data=_Documents(box), now=cutoff
    )

    result = service.refresh("2026-27", **_authorized(_event_0022600101(), cutoff))

    assert not result.complete
    assert result.failed_game_ids == ("0022600101",)
    assert CanonicalGameLedgerRepository(engine).get_game("0022600101") is None


def test_a_live_data_box_score_short_of_the_nominal_minutes_is_refused(tmp_path):
    # Five players per team lose 31 seconds and keep their points: about
    # 47.48 team minutes, which composition would refuse after acceptance.
    box = _box_score_0022600101()
    for team in ("homeTeam", "awayTeam"):
        for player in box["game"][team]["players"][:5]:
            minutes, seconds = re.fullmatch(r"PT(\d+)M(\d+)\.00S", player["statistics"]["minutes"]).groups()
            remaining = int(minutes) * 60 + int(seconds) - 31
            player["statistics"]["minutes"] = f"PT{remaining // 60:02d}M{remaining % 60:02d}.00S"

    _refused_0022600101(tmp_path, box)


def test_a_live_data_box_score_with_zero_minutes_is_refused(tmp_path):
    # Every player keeps his participation and counts but records no minutes.
    box = _box_score_0022600101()
    for team in ("homeTeam", "awayTeam"):
        for player in box["game"][team]["players"]:
            player["statistics"]["minutes"] = "PT00M00.00S"

    _refused_0022600101(tmp_path, box)


def test_a_live_data_box_score_whose_team_points_miss_its_score_is_refused(tmp_path):
    # MEM's players and team.score say 111; its team statistics say 113.
    box = _box_score_0022600101()
    box["game"]["awayTeam"]["statistics"]["points"] = 113

    _refused_0022600101(tmp_path, box)


def test_a_game_without_a_final_score_is_refused_even_from_complete_pbp(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'no-final-score.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    repository = CanonicalGameLedgerRepository(engine)
    service = LedgerBackfillService(
        provider=_Provider(_payload()),
        live_data_provider=_Documents(ProviderUnavailableError("NBA LiveData is unavailable")),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: cutoff,
    )

    result = service.refresh("2024-25", **_authorized(_event(), cutoff))

    assert not result.complete
    assert result.failed_game_ids == ("0022400001",)
    assert repository.get_game("0022400001") is None


def _stage_0022600101(engine, document, *, at, cutoff):
    """Stage ``document`` as an accepted observation of 0022600101 and normalize it."""

    recorder = CollectionObservationLedgerRecorder(engine)
    observation_id, values = recorder.stage(
        document,
        season="2026-27",
        game_id="0022600101",
        retrieved_at=at,
        manifest_id="ledger-manifest",
        manifest_scope="canonical_game_ledger",
        manifest_cutoff=cutoff,
        schema_version=1,
    )
    game = canonical_game_from_pbp(
        document,
        event=_event_0022600101(),
        season="2026-27",
        source_observation_id=observation_id,
        retrieved_at=at,
        participant_ids_by_team=AcceptedObservationParticipantCatalog(engine, recorder).get_participants(
            observation_id, game_id="0022600101"
        ),
    )
    recorder.consume(observation_id)
    return game, {observation_id: {**values, "accepted_at": at}}


def test_a_pbp_game_short_of_its_period_clock_is_refused_even_when_its_points_match(tmp_path):
    # A clock gap with no scoring in the lost time: the points cannot tell.
    pbp = _payload()
    for side in ("Home", "Away"):
        pbp["team_results"][side].update(
            {
                "1": {"Minutes": "11:29"},
                "2": {"Minutes": "12:00"},
                "3": {"Minutes": "12:00"},
                "4": {"Minutes": "12:00"},
            }
        )
    engine = create_engine(f"sqlite:///{tmp_path / 'short-clock.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=UTC)
    _install_manifest(engine, cutoff)
    repository = CanonicalGameLedgerRepository(engine)
    service = LedgerBackfillService(
        provider=_Provider(pbp),
        live_data_provider=_FinalScore(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: cutoff,
    )

    result = service.refresh("2024-25", **_authorized(_event(), cutoff))

    assert result.failed_game_ids == ("0022400001",)
    assert repository.get_game("0022400001") is None


def test_a_stored_game_yields_its_rebound_split_only_when_its_points_miss_the_final_score(tmp_path):
    cutoff = datetime(2026, 10, 25, 10, tzinfo=UTC)
    engine = _ledger_0022600101(tmp_path, cutoff)
    repository = CanonicalGameLedgerRepository(engine)
    truncated, accepted = _stage_0022600101(
        engine, _truncated_pbp(), at=datetime(2026, 10, 24, 10, tzinfo=UTC), cutoff=cutoff
    )
    repository.replace_games_atomic((truncated,), accepted_observations=accepted)
    box = compose_pbp_live_data_observation(None, _box_score_0022600101(), event=_event_0022600101())
    # The Eastern date crf04/statsplus-backend#574 gives LiveData evidence.
    box["date"] = "2026-10-23"
    replacement, replacement_accepted = _stage_0022600101(engine, box, at=cutoff, cutoff=cutoff)
    assert all(player.def_two_pt_rebounds is None for player in replacement.player_facts)

    for final_scores in (None, {"0022600101": {GSW: 108, MEM: 109}}):
        with pytest.raises(ReboundTypeRegressionError):
            repository.replace_games_atomic(
                (replacement,), accepted_observations=replacement_accepted, final_scores=final_scores
            )
        assert _points_by_team(repository.get_game("0022600101")) == {GSW: 108, MEM: 109}

    repository.replace_games_atomic(
        (replacement,),
        accepted_observations=replacement_accepted,
        final_scores={"0022600101": FINAL_0022600101},
    )
    assert _points_by_team(repository.get_game("0022600101")) == FINAL_0022600101


@pytest.mark.xfail(
    strict=True,
    reason="LiveData evidence takes the tip's UTC date until crf04/statsplus-backend#574, "
    "so the correction reads as an identity change",
)
def test_a_stored_truncated_game_is_corrected_from_the_live_data_box_score_on_recheck(tmp_path):
    # 0022600101 as production stored it before this rule: the PBPStats rows,
    # rebound split included, accepted with MEM two points short.
    cutoff = datetime(2026, 10, 25, 10, tzinfo=UTC)
    engine = _ledger_0022600101(tmp_path, cutoff)
    repository = CanonicalGameLedgerRepository(engine)
    truncated, accepted = _stage_0022600101(
        engine, _truncated_pbp(), at=datetime(2026, 10, 24, 10, tzinfo=UTC), cutoff=cutoff
    )
    repository.replace_games_atomic((truncated,), accepted_observations=accepted)
    assert _points_by_team(repository.get_game("0022600101")) == {GSW: 108, MEM: 109}

    service = _service_0022600101(
        engine, pbp=_Documents(_truncated_pbp()), live_data=_Documents(_box_score_0022600101()), now=cutoff
    )
    result = service.refresh("2026-27", **_authorized(_event_0022600101(), cutoff))

    assert result.complete
    assert result.games_replaced == 1
    stored = repository.get_game("0022600101")
    assert _points_by_team(stored) == FINAL_0022600101
    assert _accepted_provider(engine, stored) == "nba_live_data"


def _full_play_by_play_0022600101(*, heaves=True):
    """NBA LiveData's whole 0022600101 play-by-play: its shots, free throws and rebounds."""

    play_by_play = json.loads(Path("tests/fixtures/nba_live_data/playbyplay_0022600101.json").read_text())
    if not heaves:
        actions = play_by_play["game"]["actions"]
        play_by_play["game"]["actions"] = [action for action in actions if action["actionType"] != "heave"]
    return play_by_play


class _PlayByPlayOnce(_Documents):
    """Serves the heave check its play-by-play, then the provider goes down."""

    def fetch_play_by_play(self, game_id):
        if self.play_by_play_calls:
            self.play_by_play_calls.append(game_id)
            raise ProviderUnavailableError("NBA LiveData play-by-play is unavailable")
        return super().fetch_play_by_play(game_id)


def _short_clock_0022600101(tmp_path, live_data):
    from app.services.collection_control import CollectionControlService, CollectionOperationsService
    from app.services.ledger_backfill import collection_control_reconciliation_sink

    cutoff = datetime(2026, 10, 25, 10, tzinfo=UTC)
    engine = _ledger_0022600101(tmp_path, cutoff)
    service = _service_0022600101(engine, pbp=_Documents(_truncated_pbp()), live_data=live_data, now=cutoff)
    service.reconciliation_sink = collection_control_reconciliation_sink(
        CollectionControlService(engine), default_season="2026-27"
    )
    result = service.refresh("2026-27", **_authorized(_event_0022600101(), cutoff))
    items = [
        (item["kind"], item["details"], item["status"])
        for item in CollectionOperationsService(engine).diagnostics()["reconciliation"]
    ]
    stored = CanonicalGameLedgerRepository(engine).get_game("0022600101")
    return result, stored, _accepted_provider(engine, stored), items


# Without its heave the PBPStats rows fail the whole-game check (#590); with
# it they fail the heave attempt check first.  Both fall back to the box.
# The heave check's play-by-play is the fallback's evidence, so an outage
# after that one request cannot cost the game its evidence.
@pytest.mark.parametrize(
    ("documents", "heaves"),
    [(_Documents, False), (_Documents, True), (_PlayByPlayOnce, False)],
    ids=["short-clock", "heave-check", "outage-after-first-request"],
)
def test_a_game_refused_from_pbpstats_keeps_assist_and_rebound_evidence_from_live_data(
    tmp_path, documents, heaves
):
    live_data = documents(_box_score_0022600101(), play_by_play=_full_play_by_play_0022600101(heaves=heaves))

    result, stored, provider, items = _short_clock_0022600101(tmp_path, live_data)

    assert (result.complete, provider) == (True, "nba_live_data")
    assisted = {GSW: 0, MEM: 0}
    rebounds = {GSW: 0, MEM: 0}
    for player in stored.player_facts:
        locations = governed_assist_locations(player)
        assisted[player.team_id] += locations["two_point_assists"] + locations["three_point_assists"]
        rebounds[player.team_id] += (
            player.def_two_pt_rebounds
            + player.def_three_pt_rebounds
            + player.ft_def_rebounds
            + player.off_two_pt_rebounds
            + player.off_three_pt_rebounds
            + player.ft_off_rebounds
        )
    # NBA.com's box score: GSW 23 assists and 41 rebounds, MEM 27 and 47.
    assert assisted == {GSW: 23, MEM: 27}
    assert rebounds == {GSW: 41, MEM: 47}
    assert items == []
    assert live_data.play_by_play_calls == ["0022600101"]


def _ended_play_by_play_without_events():
    play_by_play = _full_play_by_play_0022600101(heaves=False)
    actions = play_by_play["game"]["actions"]
    play_by_play["game"]["actions"] = [
        action for action in actions if (action["actionType"], action.get("subType")) == ("game", "end")
    ]
    return play_by_play


# An unavailable play-by-play fails the heave check and falls back from there;
# one that passes it but cannot reconcile to the box falls back as short-clock.
@pytest.mark.parametrize(
    "play_by_play",
    [
        lambda: ProviderUnavailableError("NBA LiveData play-by-play is unavailable"),
        _ended_play_by_play_without_events,
    ],
    ids=["unavailable", "no-events"],
)
def test_a_short_clock_game_whose_play_by_play_fails_is_accepted_and_named(tmp_path, play_by_play):
    live_data = _Documents(_box_score_0022600101(), play_by_play=play_by_play())

    result, stored, provider, items = _short_clock_0022600101(tmp_path, live_data)

    assert (result.complete, provider) == (True, "nba_live_data")
    assert _points_by_team(stored) == FINAL_0022600101
    assert items == [
        (
            "ledger_fallback_evidence",
            {"game_id": "0022600101", "missing_families": "assist_locations,rebound_types"},
            "open",
        )
    ]
    assert live_data.play_by_play_calls == ["0022600101"]
