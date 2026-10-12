"""New-season hold and one-transaction cutover (backend #355).

Every test drives the public service entry points against a migrated
database whose publications rest on real manifests, Event Catalogs, and
accepted ledger evidence, then reads the durable result: pointers, versions,
pointer history, activation evidence, and audit events.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.models.collection_control import (
    AuditEvent,
    OperatorJob,
    PublicationActivation,
    PublicationPointer,
    PublicationPointerHistory,
    PublicationVersion,
)
from app.services.collection_control import (
    CollectionOperationsService,
    ControlPlaneError,
)
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    PublishedSeason,
)
from app.services.traditional_opponent_rebuild import (
    TraditionalOpponentRebuildService,
)
from tests.support.pointer_history import pointer_history
from tests.support.season_cutover import (
    L15_STREAM,
    NEW,
    NEW_CUTOFF,
    NEW_PROVENANCE,
    NOW,
    OLD,
    OLD_CUTOFF,
    OLD_PROVENANCE,
    OTHER_CUTOFF,
    OTHER_PROVENANCE,
    PER36,
    PLAYER_LOGS,
    SEASON_STREAM,
    TEAM_IDS,
    compose_new,
    payload_for,
    pointers,
    seed_world,
)

ACTOR = "operator@example.com"
REASON = "open 2026-27 on the site"


@pytest.fixture
def world(tmp_path):
    engine, publications = seed_world(tmp_path)
    operations = CollectionOperationsService(
        engine, publication_service=publications, clock=lambda: NOW,
    )
    return engine, publications, operations, pointers(engine)


def _audits(engine, action):
    with engine.connect() as connection:
        rows = connection.execute(
            select(AuditEvent.actor, AuditEvent.resource, AuditEvent.reason,
                   AuditEvent.details)
            .where(AuditEvent.action == action)
            .order_by(AuditEvent.resource)
        ).all()
    return [
        (actor, resource, reason, json.loads(details))
        for actor, resource, reason, details in rows
    ]


def _status(engine, publication_id):
    with engine.connect() as connection:
        return connection.scalar(select(PublicationVersion.status).where(
            PublicationVersion.publication_id == publication_id
        ))


def _counts(engine):
    with engine.connect() as connection:
        return {
            model.__tablename__: connection.scalar(
                select(func.count()).select_from(model)
            )
            for model in (
                PublicationVersion, PublicationPointer,
                PublicationPointerHistory, PublicationActivation, AuditEvent,
                OperatorJob,
            )
        }


def _id(before, stream_key):
    return before[stream_key][0]


# --- Rule 1: hold --------------------------------------------------------


def test_a_new_season_composition_is_held_while_the_old_season_is_published(world):
    engine, publications, _operations, before = world

    logs = compose_new(publications, PLAYER_LOGS)
    season = compose_new(publications, SEASON_STREAM)

    assert pointers(engine) == before
    assert (logs.season, logs.status, logs.manifest_id) == (
        NEW, "candidate", "manifest-new",
    )
    assert (season.season, season.status, season.manifest_id) == (
        NEW, "candidate", "manifest-new",
    )
    assert _audits(engine, "publication.held") == [
        ("composition", PLAYER_LOGS, "season_held", {
            "season": NEW, "published_season": OLD,
            "publication_id": logs.publication_id,
            "cutoff": "2026-10-24T04:00:00+00:00",
        }),
        ("composition", SEASON_STREAM, "season_held", {
            "season": NEW, "published_season": OLD,
            "publication_id": season.publication_id,
            "cutoff": "2026-10-24T04:00:00+00:00",
        }),
    ]


def test_recomposing_an_identical_held_candidate_is_idempotent(world):
    engine, publications, _operations, _before = world

    first = compose_new(publications, PLAYER_LOGS)
    again = compose_new(publications, PLAYER_LOGS)

    assert again.publication_id == first.publication_id
    assert len(_audits(engine, "publication.held")) == 1


def test_a_later_held_candidate_supersedes_the_earlier_one(world):
    engine, publications, _operations, _before = world
    earlier = compose_new(publications, SEASON_STREAM)

    later = publications.recompose_ledger(
        SEASON_STREAM, season=NEW, cutoff=OTHER_CUTOFF,
        payload=payload_for(SEASON_STREAM, NEW), provenance=OTHER_PROVENANCE,
        reason="2026-27 ledger composition",
    )

    assert (_status(engine, earlier.publication_id),
            _status(engine, later.publication_id)) == ("superseded", "candidate")


def test_same_season_composition_still_auto_advances(world):
    engine, publications, _operations, before = world
    compose_new(publications, PLAYER_LOGS)

    advanced = publications.recompose_ledger(
        PLAYER_LOGS, season=OLD, cutoff=OLD_CUTOFF,
        payload=payload_for(PLAYER_LOGS, OLD, points=30),
        provenance=OLD_PROVENANCE, reason="2025-26 ledger correction",
    )

    assert pointers(engine)[PLAYER_LOGS] == (
        advanced.publication_id, OLD, "active", 2,
    )
    assert pointers(engine)[SEASON_STREAM] == before[SEASON_STREAM]


def test_a_stream_with_no_active_publication_activates_its_first_candidate(world):
    engine, publications, _operations, _before = world
    publications.register_stream(
        PER36, provider="ledger", owner="railway",
        required_observations=["canonical_game_ledger"],
        publication_strategy="ledger_compose", enabled=True,
    )

    first = compose_new(publications, PER36)

    assert pointers(engine)[PER36] == (first.publication_id, NEW, "active", 1)
    assert _audits(engine, "publication.held") == []


def test_the_observation_compose_path_holds_as_well(world):
    engine, publications, _operations, _before = world
    probe = "season_hold_probe"
    publications.register_stream(
        probe, provider="nba", owner="railway", required_observations=(),
        publication_strategy="snapshot_replace", enabled=True,
    )
    bound = publications.compose(
        probe, season=OLD, cutoff=OLD_CUTOFF, payload={"rows": [1]},
    )

    held = publications.compose(
        probe, season=NEW, cutoff=NEW_CUTOFF, payload={"rows": [2]},
        expected_fence=1,
    )
    again = publications.compose(
        probe, season=NEW, cutoff=NEW_CUTOFF, payload={"rows": [2]},
        expected_fence=1,
    )

    assert pointers(engine)[probe] == (bound.publication_id, OLD, "active", 1)
    assert (held.season, held.status, again.publication_id) == (
        NEW, "candidate", held.publication_id,
    )


# --- Rule 2: cutover ---------------------------------------------------------


def _held_cohort(publications):
    """Hold 2026-27 for logs and the Season window; L15 has no candidate."""

    logs = compose_new(publications, PLAYER_LOGS)
    season = compose_new(publications, SEASON_STREAM)
    # A disabled stream's candidate is not held and never moves.
    per36 = publications.compose_inactive_ledger(
        PER36, season=NEW, cutoff=NEW_CUTOFF,
        payload=payload_for(PER36, NEW), provenance=NEW_PROVENANCE,
    )
    return logs, season, per36


def test_cutover_moves_logs_and_held_streams_atomically(world):
    engine, publications, operations, before = world
    logs, season, per36 = _held_cohort(publications)

    result = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    assert pointers(engine) == {
        PLAYER_LOGS: (logs.publication_id, NEW, "active", 2),
        SEASON_STREAM: (season.publication_id, NEW, "active", 2),
        L15_STREAM: before[L15_STREAM],
    }
    assert _status(engine, per36.publication_id) == "candidate"
    for stream_key, moved in ((PLAYER_LOGS, logs), (SEASON_STREAM, season)):
        assert pointer_history(engine, stream_key) == [
            (_id(before, stream_key), OLD, 1, False),
            (moved.publication_id, NEW, 2, False),
        ]
        # The displaced 2025-26 publication stays that season's authority.
        assert _status(engine, _id(before, stream_key)) == "active"
    with engine.connect() as connection:
        assert connection.execute(
            select(PublicationActivation.stream_key,
                   PublicationActivation.publication_id,
                   PublicationActivation.actor, PublicationActivation.reason,
                   PublicationActivation.fence)
            .order_by(PublicationActivation.stream_key)
        ).all() == [
            (PLAYER_LOGS, logs.publication_id, ACTOR, REASON, 2),
            (SEASON_STREAM, season.publication_id, ACTOR, REASON, 2),
        ]
    moved = [
        {"stream_key": PLAYER_LOGS,
         "from_publication_id": _id(before, PLAYER_LOGS), "from_season": OLD,
         "to_publication_id": logs.publication_id, "fence": 2},
        {"stream_key": SEASON_STREAM,
         "from_publication_id": _id(before, SEASON_STREAM),
         "from_season": OLD,
         "to_publication_id": season.publication_id, "fence": 2},
    ]
    assert _audits(engine, "season.cutover") == [(ACTOR, NEW, REASON, {
        "season": NEW,
        "moved": moved,
        "fences": {PLAYER_LOGS: 2, L15_STREAM: 1, SEASON_STREAM: 2},
        "operator_job_id": result.job_id,
    })]
    assert [dict(item) for item in result.resource.moved] == moved


def test_cutover_refuses_without_a_player_game_logs_candidate(world):
    engine, publications, operations, before = world
    season = compose_new(publications, SEASON_STREAM)

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    assert refused.value.reason == "cutover_player_game_logs_candidate_required"
    assert pointers(engine) == before
    assert _status(engine, season.publication_id) == "candidate"
    assert _audits(engine, "season.cutover") == []


def test_cutover_refuses_a_candidate_on_other_manifest_authority(world):
    engine, publications, operations, before = world
    logs = compose_new(publications, PLAYER_LOGS)
    other = publications.recompose_ledger(
        SEASON_STREAM, season=NEW, cutoff=OTHER_CUTOFF,
        payload=payload_for(SEASON_STREAM, NEW), provenance=OTHER_PROVENANCE,
        reason="2026-27 ledger composition",
    )

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    assert refused.value.reason == "cutover_authority_mismatch"
    assert pointers(engine) == before
    assert (_status(engine, logs.publication_id),
            _status(engine, other.publication_id)) == ("candidate", "candidate")
    assert _audits(engine, "season.cutover") == []


def test_cutover_refuses_when_a_pointer_moved_since_the_dry_run(world):
    engine, publications, operations, before = world
    _held_cohort(publications)

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(
            NEW, actor=ACTOR, reason=REASON,
            expected_fences={PLAYER_LOGS: 1, SEASON_STREAM: 0},
        )

    assert refused.value.reason == "stale_composition"
    assert pointers(engine) == before


def test_dry_run_writes_nothing_and_reports_every_enabled_stream(world):
    engine, publications, operations, before = world
    logs, season, _per36 = _held_cohort(publications)
    counts = _counts(engine)

    plan = operations.cutover_season(
        NEW, actor=ACTOR, reason=REASON, dry_run=True,
    )

    assert _counts(engine) == counts
    assert pointers(engine) == before
    assert plan.to_dict() == {
        "season": NEW,
        "dry_run": True,
        "refusal": None,
        "streams": [
            {"stream_key": PLAYER_LOGS, "season": OLD,
             "publication_id": _id(before, PLAYER_LOGS), "fence": 1,
             "candidate_publication_id": logs.publication_id,
             "candidate_cutoff": "2026-10-24T04:00:00+00:00"},
            {"stream_key": L15_STREAM, "season": OLD,
             "publication_id": _id(before, L15_STREAM), "fence": 1,
             "candidate_publication_id": None, "candidate_cutoff": None},
            {"stream_key": SEASON_STREAM, "season": OLD,
             "publication_id": _id(before, SEASON_STREAM), "fence": 1,
             "candidate_publication_id": season.publication_id,
             "candidate_cutoff": "2026-10-24T04:00:00+00:00"},
        ],
        "team_completed_games": [
            {"team_id": int(TEAM_IDS[0]), "tricode": "T00", "completed_games": 2},
            {"team_id": int(TEAM_IDS[1]), "tricode": "T01", "completed_games": 2},
            {"team_id": int(TEAM_IDS[2]), "tricode": "T02", "completed_games": 1},
            {"team_id": int(TEAM_IDS[3]), "tricode": "T03", "completed_games": 1},
            # The second 2026-27 manifest's game.
            {"team_id": int(TEAM_IDS[4]), "tricode": "T04", "completed_games": 1},
            {"team_id": int(TEAM_IDS[5]), "tricode": "T05", "completed_games": 1},
        ],
        "moved": [],
    }


def test_dry_run_names_the_refusal_the_cutover_would_raise(world):
    _engine, publications, operations, _before = world
    compose_new(publications, SEASON_STREAM)

    plan = operations.cutover_season(
        NEW, actor=ACTOR, reason=REASON, dry_run=True,
    )

    assert plan.refusal == "cutover_player_game_logs_candidate_required"


# --- Rule 3: after cutover -----------------------------------------------


def _l15_read(engine):
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    return reader.snapshot(
        (PLAYER_LOGS, L15_STREAM), season=PublishedSeason(OLD),
    ).reads[L15_STREAM]


def test_after_cutover_l15_reads_window_not_ready_until_its_first_candidate(world):
    engine, publications, operations, before = world
    _held_cohort(publications)
    operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    waiting = _l15_read(engine)
    assert (waiting.status, waiting.unavailable_reason, waiting.season,
            waiting.publication_id) == (
        "missing", "window_not_ready", OLD, _id(before, L15_STREAM),
    )

    first = compose_new(publications, L15_STREAM)

    assert pointers(engine)[L15_STREAM] == (first.publication_id, NEW, "active", 2)
    served = _l15_read(engine)
    assert (served.status, served.unavailable_reason, served.season) == (
        "active", None, NEW,
    )


def test_family_rollback_works_across_the_transitional_split(world):
    engine, publications, operations, before = world
    _held_cohort(publications)
    operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    family = TraditionalOpponentRebuildService(
        engine, publication_service=publications, clock=lambda: NOW,
    )

    restored = family.rollback(actor=ACTOR, reason="restore the 2025-26 window")

    after = pointers(engine)
    assert [version.stream_key for version in restored] == [SEASON_STREAM]
    assert after[SEASON_STREAM][1:] == (OLD, "rollback", 3)
    assert after[L15_STREAM] == before[L15_STREAM]
    with engine.connect() as connection:
        checksums = dict(connection.execute(
            select(PublicationVersion.publication_id, PublicationVersion.checksum)
            .where(PublicationVersion.publication_id.in_((
                after[SEASON_STREAM][0], _id(before, SEASON_STREAM),
            )))
        ).all())
    assert checksums[after[SEASON_STREAM][0]] == checksums[_id(before, SEASON_STREAM)]


def test_family_rollback_of_the_first_new_season_l15_restores_the_split(world):
    engine, publications, operations, before = world
    _held_cohort(publications)
    operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    # The Season window advances within 2026-27, then L15 reunites the family.
    publications.recompose_ledger(
        SEASON_STREAM, season=NEW, cutoff=OTHER_CUTOFF,
        payload=payload_for(SEASON_STREAM, NEW), provenance=OTHER_PROVENANCE,
        reason="2026-27 ledger composition",
    )
    compose_new(publications, L15_STREAM)
    family = TraditionalOpponentRebuildService(
        engine, publication_service=publications, clock=lambda: NOW,
    )

    family.rollback(actor=ACTOR, reason="withdraw the first 2026-27 L15")

    after = pointers(engine)
    assert after[SEASON_STREAM][1:] == (NEW, "rollback", 4)
    assert after[L15_STREAM][1:] == (OLD, "rollback", 3)


# --- Rule 4: revert ----------------------------------------------------------


def test_revert_restores_every_moved_stream_to_its_pre_cutover_publication(world):
    engine, publications, operations, before = world
    _held_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    reverted = operations.revert_season_cutover(
        NEW, cutover.job_id, actor=ACTOR, reason="2026-27 opened too early",
    )

    after = pointers(engine)
    assert after[L15_STREAM] == before[L15_STREAM]
    with engine.connect() as connection:
        for stream_key in (PLAYER_LOGS, SEASON_STREAM):
            assert after[stream_key][1:] == (OLD, "rollback", 3)
            restored, original = (
                connection.execute(select(
                    PublicationVersion.season, PublicationVersion.cutoff,
                    PublicationVersion.checksum,
                ).where(PublicationVersion.publication_id == publication_id)).one()
                for publication_id in (after[stream_key][0], _id(before, stream_key))
            )
            assert restored == original
            assert [row[1:] for row in pointer_history(engine, stream_key)] == [
                (OLD, 1, False), (NEW, 2, False), (OLD, 3, False),
            ]
    assert _audits(engine, "season.cutover_revert") == [(
        ACTOR, NEW, "2026-27 opened too early", {
            "cutover_id": cutover.job_id,
            "restored": [
                {"stream_key": PLAYER_LOGS,
                 "publication_id": after[PLAYER_LOGS][0], "fence": 3},
                {"stream_key": SEASON_STREAM,
                 "publication_id": after[SEASON_STREAM][0], "fence": 3},
            ],
            "operator_job_id": reverted.job_id,
        },
    )]


def test_revert_tolerates_an_enabled_stream_that_never_composed(world):
    engine, publications, operations, before = world
    publications.register_stream(
        "season_hold_probe", provider="nba", owner="railway",
        required_observations=(), publication_strategy="snapshot_replace",
        enabled=True,
    )
    _held_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    operations.revert_season_cutover(
        NEW, cutover.job_id, actor=ACTOR, reason="2026-27 opened too early",
    )

    assert {key: value[1] for key, value in pointers(engine).items()} == {
        PLAYER_LOGS: OLD, SEASON_STREAM: OLD, L15_STREAM: OLD,
    }


def test_revert_refuses_after_a_later_move(world):
    engine, publications, operations, _before = world
    _held_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    compose_new(publications, L15_STREAM)
    moved = pointers(engine)

    with pytest.raises(ControlPlaneError) as refused:
        operations.revert_season_cutover(
            NEW, cutover.job_id, actor=ACTOR, reason="2026-27 opened too early",
        )

    assert refused.value.reason == "cutover_superseded"
    assert pointers(engine) == moved
    assert _audits(engine, "season.cutover_revert") == []


def test_revert_of_an_unknown_cutover_is_not_found(world):
    _engine, _publications, operations, _before = world

    with pytest.raises(ControlPlaneError) as refused:
        operations.revert_season_cutover(
            NEW, "no-such-cutover", actor=ACTOR, reason="nothing to revert",
        )

    assert refused.value.reason == "cutover_not_found"


# --- Rule 1 through the nightly composition job --------------------------


def test_a_held_composition_job_settles_succeeded_and_is_not_retried(tmp_path):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine

    from app.migrations import run_migrations
    from app.models.collection_control import (
        ActiveSeason,
        CatalogPublication,
        CollectionManifest,
        CompositionJob,
    )
    from app.models.event_catalog import EventCatalogEntry
    from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
    from app.services.collection_control import PublicationService
    from app.services.ledger_materialization import (
        LedgerCorrectionQueue,
        LedgerMaterializationService,
    )
    from app.services.ledger_parity import (
        LedgerParityArtifactRepository,
        LegacyParityDiagnosticReader,
    )
    from app.services.ledger_runtime import (
        ActiveManifestLedgerGovernanceReader,
        LedgerRuntime,
    )
    from tests.services.test_correction_propagation import (
        _boundary_observation_values,
        _event_catalog_publication,
    )
    from tests.services.test_ledger_derivations import _league_games

    utc = timezone.utc
    cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=utc)
    runtime_now = cutoff + timedelta(hours=18)
    published = "2024-25"
    engine = create_engine(f"sqlite:///{tmp_path / 'held-job.sqlite3'}")
    run_migrations(engine)
    games = _league_games()
    with engine.begin() as connection:
        catalog = _event_catalog_publication(games, cutoff, "held-event-catalog")
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="held-manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(days=30), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="held-manifest",
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), [{
            "nba_game_id": game.game_id, "season": game.season,
            "home_team_id": game.home_team_id,
            "home_team_name": f"Team {game.home_team_id}",
            "home_team_tricode": game.home_team_tricode,
            "away_team_id": game.away_team_id,
            "away_team_name": f"Team {game.away_team_id}",
            "away_team_tricode": game.away_team_tricode,
            "scheduled_at": datetime.combine(game.game_date, datetime.min.time(), utc),
            "status_text": "Final", "status_code": 3,
            "classification": "Regular Season",
            "first_seen_at": cutoff, "last_seen_at": cutoff,
        } for game in games])

    publications = PublicationService(engine, clock=lambda: runtime_now)
    publications.register_default_streams()
    published_ids = {}
    for stream_key in (PLAYER_LOGS, SEASON_STREAM, L15_STREAM):
        publications.register_stream(
            stream_key, provider="ledger", owner="railway",
            required_observations=(), publication_strategy="ledger_compose",
            enabled=True,
        )
        published_ids[stream_key] = publications.compose(
            stream_key, season=published, cutoff=cutoff - timedelta(days=180),
            payload=payload_for(stream_key, published),
        ).publication_id

    queue = LedgerCorrectionQueue(clock=lambda: runtime_now, require_governance=True)
    ledger = CanonicalGameLedgerRepository(engine, correction_sink=queue)
    ledger.replace_games_atomic(games, accepted_observations={
        game.source_observation_id: _boundary_observation_values(
            game, cutoff=cutoff, manifest_id="held-manifest",
        )
        for game in games
    })
    runtime = LedgerRuntime(
        backfill=None,
        repository=ledger,
        materialization=LedgerMaterializationService(
            ledger,
            parity_repository=LedgerParityArtifactRepository(engine),
            parity_reader=LegacyParityDiagnosticReader(engine),
            publication_service=publications,
            clock=lambda: runtime_now,
        ),
        governance=ActiveManifestLedgerGovernanceReader(
            engine, clock=lambda: runtime_now,
        ),
        publication_service=publications,
        clock=lambda: runtime_now,
    )

    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)

    with engine.connect() as connection:
        jobs = connection.execute(
            select(CompositionJob.stream_key, CompositionJob.status,
                   CompositionJob.last_error)
            .order_by(CompositionJob.stream_key)
        ).all()
        held = dict(connection.execute(
            select(PublicationVersion.stream_key, PublicationVersion.status)
            .where(
                PublicationVersion.season == "2025-26",
                PublicationVersion.stream_key.in_(published_ids),
            )
        ).all())
    assert jobs == [
        (stream_key, "succeeded", None)
        for stream_key in sorted(LedgerCorrectionQueue.STREAMS)
    ]
    assert {key: value[0] for key, value in pointers(engine).items()} == published_ids
    assert held == {
        PLAYER_LOGS: "candidate",
        SEASON_STREAM: "candidate",
        L15_STREAM: "candidate",
    }
    assert [
        (resource, details["season"], details["published_season"])
        for _actor, resource, _reason, details in _audits(engine, "publication.held")
    ] == [
        (PLAYER_LOGS, "2025-26", published),
        (L15_STREAM, "2025-26", published),
        (SEASON_STREAM, "2025-26", published),
    ]
    # Nothing is left queued or failed, so no later pass recomposes it.
    assert runtime.compose_queued("2025-26") == 0


# --- Concurrency on Postgres -------------------------------------------------


def test_a_composition_racing_the_cutover_waits_and_adopts_the_new_season():
    """The cutover holds every stream lock, as composition takes it first.

    A composition that starts while the cutover is moving pointers must wait
    for it and then decide hold-or-activate against the season the cutover
    published, never against the one it replaced.
    """

    import os
    import threading

    from sqlalchemy import create_engine, event, text

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration test")
    engine = create_engine(url)

    def reset_schema():
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))

    reset_schema()
    try:
        engine, publications = seed_world(None, engine=engine)
        _held_cohort(publications)
        operations = CollectionOperationsService(
            engine, publication_service=publications, clock=lambda: NOW,
        )
        racing = {"thread": None, "outcome": []}

        def compose_l15():
            try:
                racing["outcome"].append(compose_new(publications, L15_STREAM))
            except ControlPlaneError as error:
                racing["outcome"].append(error)

        @event.listens_for(engine, "after_cursor_execute")
        def interleave(connection, cursor, statement, *_):
            if racing["thread"] is None and statement.startswith(
                "UPDATE publication_pointers"
            ):
                racing["thread"] = threading.Thread(target=compose_l15)
                racing["thread"].start()
                racing["thread"].join(timeout=3)

        operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
        assert racing["thread"] is not None
        racing["thread"].join(timeout=30)
        assert not racing["thread"].is_alive()
        event.remove(engine, "after_cursor_execute", interleave)

        [l15] = racing["outcome"]
        assert pointers(engine)[L15_STREAM] == (l15.publication_id, NEW, "active", 2)
        assert {key: value[1] for key, value in pointers(engine).items()} == {
            PLAYER_LOGS: NEW, SEASON_STREAM: NEW, L15_STREAM: NEW,
        }
    finally:
        reset_schema()
        engine.dispose()
