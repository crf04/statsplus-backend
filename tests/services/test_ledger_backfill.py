"""Offline scheduler tests for newest-first resumable ledger backfill."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    LedgerGameRow,
    canonical_game_from_pbp,
    raw_checksum,
)
from app.migrations import run_migrations
from app.services.ledger_backfill import (
    AcceptedObservationParticipantCatalog,
    CollectionObservationLedgerRecorder,
    LedgerBackfillService,
)
from app.providers.pbp_game_logs import PBPGameLogAdapter
from app.models.event_catalog import EventCatalogEntry
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
from app.services.ledger_materialization import LedgerCorrectionQueue
from sqlalchemy import select


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


class _Recorder:
    def __init__(self):
        self.count = 0
        self.staged = set()
        self.peak = 0
        self.lock = threading.Lock()

    def stage(
        self, observation, *, season, game_id, retrieved_at,
        manifest_id, manifest_scope, manifest_cutoff, schema_version,
    ):
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
            "checksum": observation_id,
            "payload": "{}",
            "payload_bytes": 2,
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
    payload.pop("team_results", None)
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
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="ledger-manifest", season="2024-25", cutoff=cutoff,
            collect_before=deadline, accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum=f"manifest:{cutoff.isoformat()}",
            status="active", created_at=cutoff,
        ))


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
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2024-25", phase="Regular Season", status="active",
            cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
            activated_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
            activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="ledger-manifest", season="2024-25",
            cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
            collect_before=datetime(2024, 12, 31, tzinfo=timezone.utc),
            accepted_versions="[1]", scopes="[\"canonical_game_ledger\"]",
            checksum="ledger-manifest", status="active",
            created_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
        ))
    payload = _payload()
    provider = _Provider(payload)
    repository = CanonicalGameLedgerRepository(
        engine,
        correction_sink=LedgerCorrectionQueue(
            require_governance=True,
            clock=lambda: datetime(2024, 11, 16, tzinfo=timezone.utc),
        ),
    )
    service = LedgerBackfillService(
        provider=provider,
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=(recorder := _Recorder()),
        repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=timezone.utc),
    )

    result = service.refresh("2024-25", **_authorized(
        _event(), datetime(2024, 11, 16, tzinfo=timezone.utc),
    ))

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
        assert len(jobs) == 6
        assert {job["manifest_id"] for job in jobs} == {"ledger-manifest"}
        accepted = connection.execute(select(CollectionObservation)).mappings().one()
    assert accepted["manifest_id"] == "ledger-manifest"
    assert accepted["cutoff"] == datetime(2024, 11, 16)
    assert json.loads(accepted["scope"])["surface"] == "canonical_game_ledger"


def test_backfill_rejects_missing_governance_before_provider_io(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ungoverned.sqlite3'}")
    run_migrations(engine)
    provider = _Provider(_payload())
    service = LedgerBackfillService(
        provider=provider, athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=CanonicalGameLedgerRepository(engine),
    )

    with pytest.raises(ValueError, match="authorized ledger manifest"):
        service.refresh("2024-25")
    with pytest.raises(ValueError, match="active authorized ledger manifest"):
        service.refresh("2024-25", **_authorized(
            _event(), datetime(2024, 11, 16, tzinfo=timezone.utc),
        ))

    assert provider.calls == []


def test_rechecks_enforce_daily_and_weekly_cadence_and_allow_historical_repair(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cadence.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    now = datetime(2024, 12, 16, tzinfo=timezone.utc)
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
        repository.replace_game(canonical_game_from_pbp(
            seed_payload,
            event=event,
            season="2024-25",
            source_observation_id=f"seed:{game_id}",
            retrieved_at=now - retrieved_age,
            participant_ids_by_team=_Participants().get_participants(game_id, game_id=game_id),
        ))

    provider = _Provider(
        _payload(),
        dates={event["nba_game_id"]: event["scheduled_at"][:10] for event in events},
    )
    service = LedgerBackfillService(
        provider=provider,
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
        "2024-25", max_games=4, historical_repair=True, **authorization,
    )

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

    observation_id, _values = recorder.stage(
        frame,
        season="2024-25",
        game_id="0022400001",
        retrieved_at=datetime(2024, 11, 16, tzinfo=timezone.utc),
            manifest_id="manifest-1",
            manifest_scope="canonical_game_ledger",
            manifest_cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
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
        retrieved_at=datetime(2024, 11, 16, 0, 1, tzinfo=timezone.utc),
        manifest_id="manifest-1",
        manifest_scope="canonical_game_ledger",
        manifest_cutoff=datetime(2024, 11, 16, tzinfo=timezone.utc),
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
    now = datetime(2024, 11, 16, tzinfo=timezone.utc)

    def stage(index):
        return recorder.stage(
            [{"GAME_ID": str(index), "EntityId": index + 1}],
            season="2024-25", game_id=str(index), retrieved_at=now,
            manifest_id="manifest", manifest_scope="canonical_game_ledger",
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
    _install_manifest(engine, datetime(2024, 11, 16, tzinfo=timezone.utc))

    class MissingParticipant:
        def get_participants(self, observation_id, *, game_id):
            return {1610612747: (2544,), 1610612759: (201935,)}

    recorder = _Recorder()
    result = LedgerBackfillService(
        provider=_Provider(_payload()),
        athlete_catalog=_Athletes(),
        participant_catalog=MissingParticipant(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder,
        repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=timezone.utc),
    ).refresh("2024-25", **_authorized(
        _event(), datetime(2024, 11, 16, tzinfo=timezone.utc),
    ))

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
    _install_manifest(engine, datetime(2024, 11, 16, tzinfo=timezone.utc))
    monkeypatch.setattr(
        repository,
        "replace_games_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write failed")),
    )
    recorder = _Recorder()

    result = LedgerBackfillService(
        provider=_Provider(_payload()),
        athlete_catalog=_Athletes(), participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder, repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=timezone.utc),
    ).refresh("2024-25", **_authorized(
        _event(), datetime(2024, 11, 16, tzinfo=timezone.utc),
    ))

    assert not result.complete
    assert recorder.staged == set()


def test_deadline_crossed_during_provider_call_cannot_accept_observation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'deadline.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    recorder = _Recorder()
    cutoff = datetime(2024, 11, 16, tzinfo=timezone.utc)
    deadline = cutoff + timedelta(minutes=1)
    _install_manifest(engine, cutoff, collect_before=deadline)
    clock_values = iter((cutoff, deadline))

    result = LedgerBackfillService(
        provider=_Provider(_payload()),
        athlete_catalog=_Athletes(), participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder, repository=repository,
        max_concurrency=1, clock=lambda: next(clock_values),
    ).refresh(
        "2024-25", cutoff=cutoff, governed_events=(_event(),),
        manifest_id="ledger-manifest", collect_before=deadline,
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
    _install_manifest(engine, datetime(2024, 11, 16, tzinfo=timezone.utc))
    valid_event = _event()
    invalid_event = {**_event(), "nba_game_id": "0022400002"}

    class PartialProvider:
        def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
            return _payload() if game_id == "0022400001" else {"stats": {}}

    result = LedgerBackfillService(
        provider=PartialProvider(),
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: datetime(2024, 11, 16, tzinfo=timezone.utc),
    ).refresh("2024-25", **_authorized(
        (valid_event, invalid_event), datetime(2024, 11, 16, tzinfo=timezone.utc),
    ))

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
    now = datetime(2024, 11, 30, tzinfo=timezone.utc)
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
        provider=_Provider(_payload(), dates=dates), athlete_catalog=_Athletes(),
        participant_catalog=ExactParticipants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder, repository=repository,
        max_concurrency=3, clock=lambda: now,
    ).refresh("2024-25", **_authorized(events, now))

    assert recorder.peak_pending_count() <= 3
    assert recorder.pending_count() == 0


def test_unexpected_future_exception_discards_all_staging_after_prior_success(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'future-exception.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    now = datetime(2024, 11, 20, tzinfo=timezone.utc)
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
                game_id, season, season_type=season_type,
            )

    recorder = _Recorder()
    service = LedgerBackfillService(
        provider=UnexpectedProvider(_payload()), athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder, repository=repository,
        max_concurrency=1, clock=lambda: now,
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
            clock=lambda: datetime(2024, 11, 20, tzinfo=timezone.utc),
        ),
    )
    now = datetime(2024, 11, 20, tzinfo=timezone.utc)
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
                    connection.execute(CollectionManifest.__table__.update().values(
                        status="superseded", superseded_at=now,
                    ))
            return super().fetch_game_stats(
                game_id, season, season_type=season_type,
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
        athlete_catalog=_Athletes(), participant_catalog=_Participants(),
        reconciliation_sink=lambda game_id, payload: None,
        observation_recorder=recorder, repository=repository,
        max_concurrency=1, clock=lambda: now,
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
    assert len(jobs) == 6


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
            last_seen_at=cutoff,
        ))


def test_production_adapter_backfill_archives_complete_raw_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'production.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=timezone.utc)
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


def test_missing_required_count_rejects_through_the_production_backfill_seam(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill_missing_count.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2024, 11, 16, tzinfo=timezone.utc)
    _install_manifest(engine, cutoff)
    _seed_participant_event(engine, cutoff)
    payload = _payload()
    next(
        row for row in payload["stats"]["Away"]["FullGame"]
        if row.get("EntityId") == "201935"
    )["Assists"] = None
    adapter, _session = _production_adapter(payload)
    recorder = CollectionObservationLedgerRecorder(engine)
    repository = CanonicalGameLedgerRepository(engine)

    result = LedgerBackfillService(
        provider=adapter,
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
