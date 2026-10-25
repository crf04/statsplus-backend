"""New-season hold and one-transaction cutover (backend #355).

Every test drives the public service entry points against a migrated
database whose publications rest on real manifests, Event Catalogs, and
accepted ledger evidence, then reads the durable result: pointers, versions,
pointer history, activation evidence, and audit events.
"""

from __future__ import annotations

import json
from datetime import UTC

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
    ReadSeason,
)
from app.services.traditional_opponent_rebuild import (
    TraditionalOpponentRebuildService,
)
from tests.support.pointer_history import pointer_history
from tests.support.season_cutover import (
    L15_STREAM,
    NEW,
    NEW_CUTOFF,
    NEW_GAMES,
    NEW_PROVENANCE,
    NOW,
    OLD,
    OLD_CUTOFF,
    OLD_PROVENANCE,
    OTHER_GAME,
    PER36,
    PLAYER_LOGS,
    SEASON_STREAM,
    TEAM_IDS,
    THIN_CUTOFF,
    TRICODES,
    accept_ledger,
    compose_new,
    compose_other,
    compose_thin,
    open_opening_week,
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
        engine,
        publication_service=publications,
        clock=lambda: NOW,
    )
    return engine, publications, operations, pointers(engine)


def _audits(engine, action):
    with engine.connect() as connection:
        rows = connection.execute(
            select(AuditEvent.actor, AuditEvent.resource, AuditEvent.reason, AuditEvent.details)
            .where(AuditEvent.action == action)
            .order_by(AuditEvent.resource)
        ).all()
    return [(actor, resource, reason, json.loads(details)) for actor, resource, reason, details in rows]


def _status(engine, publication_id):
    with engine.connect() as connection:
        return connection.scalar(
            select(PublicationVersion.status).where(PublicationVersion.publication_id == publication_id)
        )


def _counts(engine):
    with engine.connect() as connection:
        return {
            model.__tablename__: connection.scalar(select(func.count()).select_from(model))
            for model in (
                PublicationVersion,
                PublicationPointer,
                PublicationPointerHistory,
                PublicationActivation,
                AuditEvent,
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
        NEW,
        "candidate",
        "manifest-new",
    )
    assert (season.season, season.status, season.manifest_id) == (
        NEW,
        "candidate",
        "manifest-new",
    )
    assert _audits(engine, "publication.held") == [
        (
            "composition",
            PLAYER_LOGS,
            "season_held",
            {
                "season": NEW,
                "published_season": OLD,
                "publication_id": logs.publication_id,
                "cutoff": "2026-10-24T04:00:00+00:00",
                "composition_job_id": None,
            },
        ),
        (
            "composition",
            SEASON_STREAM,
            "season_held",
            {
                "season": NEW,
                "published_season": OLD,
                "publication_id": season.publication_id,
                "cutoff": "2026-10-24T04:00:00+00:00",
                "composition_job_id": None,
            },
        ),
    ]


def test_recomposing_an_identical_held_candidate_is_idempotent(world):
    engine, publications, _operations, _before = world

    first = compose_new(publications, PLAYER_LOGS)
    again = compose_new(publications, PLAYER_LOGS)

    assert again.publication_id == first.publication_id
    # Every hold is recorded, so each composition job can name its outcome.
    assert [
        details["publication_id"]
        for _actor, _resource, _reason, details in _audits(engine, "publication.held")
    ] == [first.publication_id, first.publication_id]


def test_a_later_held_candidate_supersedes_the_earlier_one(world):
    engine, publications, _operations, _before = world
    earlier = compose_new(publications, SEASON_STREAM)

    later = compose_other(publications, SEASON_STREAM)

    assert (_status(engine, earlier.publication_id), _status(engine, later.publication_id)) == (
        "superseded",
        "candidate",
    )


def test_a_delayed_earlier_cutoff_never_supersedes_a_later_held_candidate(world):
    engine, publications, operations, _before = world
    later = compose_other(publications, SEASON_STREAM)

    delayed = compose_new(publications, SEASON_STREAM)

    assert (_status(engine, later.publication_id), _status(engine, delayed.publication_id)) == (
        "candidate",
        "superseded",
    )
    plan = operations.cutover_season(
        NEW,
        actor=ACTOR,
        reason=REASON,
        dry_run=True,
    )
    assert [
        (row["candidate_publication_id"], row["candidate_cutoff"])
        for row in plan.streams
        if row["stream_key"] == SEASON_STREAM
    ] == [(later.publication_id, "2026-10-25T04:00:00+00:00")]


def test_same_season_composition_still_auto_advances(world):
    engine, publications, _operations, before = world
    compose_new(publications, PLAYER_LOGS)

    advanced = publications.recompose_ledger(
        PLAYER_LOGS,
        season=OLD,
        cutoff=OLD_CUTOFF,
        payload=payload_for(PLAYER_LOGS, OLD, points=30),
        provenance=OLD_PROVENANCE,
        reason="2025-26 ledger correction",
    )

    assert pointers(engine)[PLAYER_LOGS] == (
        advanced.publication_id,
        OLD,
        "active",
        2,
    )
    assert pointers(engine)[SEASON_STREAM] == before[SEASON_STREAM]


def test_a_stream_with_no_active_publication_activates_its_first_candidate(world):
    engine, publications, _operations, _before = world
    publications.register_stream(
        PER36,
        provider="ledger",
        owner="railway",
        required_observations=["canonical_game_ledger"],
        publication_strategy="ledger_compose",
        enabled=True,
    )

    first = compose_new(publications, PER36)

    assert pointers(engine)[PER36] == (first.publication_id, NEW, "active", 1)
    assert _audits(engine, "publication.held") == []


def test_without_a_published_season_a_bound_stream_never_changes_season(tmp_path):
    from sqlalchemy import create_engine

    from app.migrations import run_migrations
    from app.services.collection_control import PublicationService

    engine = create_engine(f"sqlite:///{tmp_path / 'unpublished.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: NOW)
    probe = "season_hold_probe"
    publications.register_stream(
        probe,
        provider="nba",
        owner="railway",
        required_observations=(),
        publication_strategy="snapshot_replace",
        enabled=True,
    )
    first = publications.compose(
        probe,
        season=OLD,
        cutoff=OLD_CUTOFF,
        payload={"rows": [1]},
    )

    held = publications.compose(
        probe,
        season=NEW,
        cutoff=NEW_CUTOFF,
        payload={"rows": [2]},
        expected_fence=1,
    )
    advanced = publications.compose(
        probe,
        season=OLD,
        cutoff=OLD_CUTOFF,
        payload={"rows": [3]},
        expected_fence=1,
    )

    assert (first.status, held.status) == ("active", "candidate")
    assert pointers(engine)[probe] == (advanced.publication_id, OLD, "active", 2)
    assert [
        (details["season"], details["published_season"])
        for _actor, _resource, _reason, details in _audits(engine, "publication.held")
    ] == [(NEW, None)]


def test_the_observation_compose_path_holds_as_well(world):
    engine, publications, _operations, _before = world
    probe = "season_hold_probe"
    publications.register_stream(
        probe,
        provider="nba",
        owner="railway",
        required_observations=(),
        publication_strategy="snapshot_replace",
        enabled=True,
    )
    bound = publications.compose(
        probe,
        season=OLD,
        cutoff=OLD_CUTOFF,
        payload={"rows": [1]},
    )

    held = publications.compose(
        probe,
        season=NEW,
        cutoff=NEW_CUTOFF,
        payload={"rows": [2]},
        expected_fence=1,
    )
    again = publications.compose(
        probe,
        season=NEW,
        cutoff=NEW_CUTOFF,
        payload={"rows": [2]},
        expected_fence=1,
    )

    assert pointers(engine)[probe] == (bound.publication_id, OLD, "active", 1)
    assert (held.season, held.status, again.publication_id) == (
        NEW,
        "candidate",
        held.publication_id,
    )


# --- Rule 2: cutover ---------------------------------------------------------


def _held_cohort(publications):
    """Hold 2026-27 for logs and the Season window; L15 has no candidate."""

    logs = compose_new(publications, PLAYER_LOGS)
    season = compose_new(publications, SEASON_STREAM)
    # A disabled stream's candidate is not held and never moves.
    per36 = publications.compose_inactive_ledger(
        PER36,
        season=NEW,
        cutoff=NEW_CUTOFF,
        payload=payload_for(PER36, NEW),
        provenance=NEW_PROVENANCE,
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
            select(
                PublicationActivation.stream_key,
                PublicationActivation.publication_id,
                PublicationActivation.actor,
                PublicationActivation.reason,
                PublicationActivation.fence,
            ).order_by(PublicationActivation.stream_key)
        ).all() == [
            (PLAYER_LOGS, logs.publication_id, ACTOR, REASON, 2),
            (SEASON_STREAM, season.publication_id, ACTOR, REASON, 2),
        ]
    moved = [
        {
            "stream_key": PLAYER_LOGS,
            "from_publication_id": _id(before, PLAYER_LOGS),
            "from_previous_publication_id": None,
            "from_season": OLD,
            "to_publication_id": logs.publication_id,
            "fence": 2,
        },
        {
            "stream_key": SEASON_STREAM,
            "from_publication_id": _id(before, SEASON_STREAM),
            "from_previous_publication_id": None,
            "from_season": OLD,
            "to_publication_id": season.publication_id,
            "fence": 2,
        },
    ]
    assert _audits(engine, "season.cutover") == [
        (
            ACTOR,
            NEW,
            REASON,
            {
                "season": NEW,
                "moved": moved,
                "left_behind": [],
                "fences": {PLAYER_LOGS: 2, L15_STREAM: 1, SEASON_STREAM: 2},
                "operator_job_id": result.job_id,
            },
        )
    ]
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
    other = compose_other(publications, SEASON_STREAM)

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    assert refused.value.reason == "cutover_leave_behind_mismatch"
    assert pointers(engine) == before
    assert (_status(engine, logs.publication_id), _status(engine, other.publication_id)) == (
        "candidate",
        "candidate",
    )
    assert _audits(engine, "season.cutover") == []


def _statuses(engine):
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(
                    PublicationVersion.publication_id,
                    PublicationVersion.status,
                )
            ).all()
        )


def _history(engine):
    with engine.connect() as connection:
        return connection.execute(
            select(
                PublicationPointerHistory.stream_key,
                PublicationPointerHistory.publication_id,
                PublicationPointerHistory.fence,
                PublicationPointerHistory.revoked_at,
            ).order_by(
                PublicationPointerHistory.stream_key,
                PublicationPointerHistory.fence,
            )
        ).all()


def _durable_state(engine):
    return (
        pointers(engine),
        _statuses(engine),
        _history(engine),
        _counts(engine),
    )


def test_a_failure_after_the_first_pointer_move_leaves_everything_unchanged(
    world,
    monkeypatch,
):
    import app.services.collection_control as control

    engine, publications, operations, _before = world
    _held_cohort(publications)
    unchanged = _durable_state(engine)
    real_move = control._move_pointer_to_candidate
    moves = []

    def move_once_then_fail(*args, **kwargs):
        if moves:
            raise RuntimeError("database lost mid-cutover")
        moves.append(real_move(*args, **kwargs))

    monkeypatch.setattr(control, "_move_pointer_to_candidate", move_once_then_fail)

    # Whatever surfaces (the injected error, or a transaction error raised
    # while unwinding it), the durable state is what decides atomicity.
    with pytest.raises(Exception):  # noqa: B017
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    assert len(moves) == 1
    assert _durable_state(engine) == unchanged
    assert _audits(engine, "season.cutover") == []


@pytest.mark.parametrize(
    "defect, refusal",
    [
        ("unrelated_log_game", "publication_game_set_mismatch"),
        ("log_game_missing", "publication_game_set_mismatch"),
        ("log_game_one_team_only", "publication_game_set_mismatch"),
        ("partial_lineage", "publication_game_set_mismatch"),
        ("malformed_payload", "publication_candidate_invalid"),
        ("checksum_mismatch", "publication_checksum_mismatch"),
        ("unbound_candidate", "publication_governance_unavailable"),
    ],
)
def test_cutover_refuses_a_candidate_that_is_not_complete_and_bound(
    world,
    defect,
    refusal,
):
    from sqlalchemy import update

    from tests.support.season_cutover import NEW_GAMES, provenance

    engine, publications, operations, before = world
    if defect == "unrelated_log_game":
        stray = ("0022699999",) + NEW_GAMES[0][1:]
        compose_new(publications, PLAYER_LOGS, payload=payload_for(PLAYER_LOGS, NEW, games=(stray,)))
    elif defect == "log_game_missing":
        # The night's last game never made it into the logs payload.
        compose_new(publications, PLAYER_LOGS, payload=payload_for(PLAYER_LOGS, NEW, games=NEW_GAMES[:14]))
    elif defect == "log_game_one_team_only":
        rows = payload_for(PLAYER_LOGS, NEW)["rows"]
        compose_new(publications, PLAYER_LOGS, payload={"rows": rows[:-1]})
    elif defect == "partial_lineage":
        # Composed before the ledger held the last game of the night.
        compose_new(publications, PLAYER_LOGS, provenance=provenance(NEW_GAMES[:14]))
    else:
        compose_new(publications, PLAYER_LOGS)
    season = compose_new(publications, SEASON_STREAM)
    if defect == "malformed_payload":
        season = compose_new(publications, SEASON_STREAM, payload={"rows": "not a team window"})
    elif defect == "checksum_mismatch":
        with engine.begin() as connection:
            connection.execute(
                update(PublicationVersion)
                .where(
                    PublicationVersion.publication_id == season.publication_id,
                )
                .values(payload=json.dumps({"rows": []}))
            )
    elif defect == "unbound_candidate":
        probe = "season_hold_probe"
        publications.register_stream(
            probe,
            provider="nba",
            owner="railway",
            required_observations=(),
            publication_strategy="snapshot_replace",
            enabled=True,
        )
        publications.compose(probe, season=OLD, cutoff=OLD_CUTOFF, payload={"rows": [1]})
        publications.compose(probe, season=NEW, cutoff=NEW_CUTOFF, payload={"rows": [2]}, expected_fence=1)
    unchanged = _durable_state(engine)

    plan = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, dry_run=True)
    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    failing = {
        "malformed_payload": SEASON_STREAM,
        "checksum_mismatch": SEASON_STREAM,
        "unbound_candidate": "season_hold_probe",
    }.get(defect)
    if failing is None:
        # A player_game_logs defect refuses the whole cutover.
        assert (plan.refusal, refused.value.reason) == (refusal, refusal)
    else:
        # Any other stream's defect is left behind, and a cutover that does
        # not name it refuses.
        assert plan.refusal is None
        assert plan.to_dict()["leave_behind"] == [{"stream_key": failing, "reason": refusal}]
        assert refused.value.reason == "cutover_leave_behind_mismatch"
    assert _durable_state(engine) == unchanged
    assert pointers(engine)[PLAYER_LOGS] == before[PLAYER_LOGS]


def test_cutover_refuses_an_authority_that_does_not_govern_every_team(world):
    from tests.support.season_cutover import (
        SHORT,
        SHORT_CUTOFF,
        SHORT_GAME,
        SHORT_PROVENANCE,
    )

    engine, publications, operations, before = world
    for stream_key in (PLAYER_LOGS, SEASON_STREAM):
        publications.recompose_ledger(
            stream_key,
            season=SHORT,
            cutoff=SHORT_CUTOFF,
            payload=payload_for(stream_key, SHORT, games=(SHORT_GAME,)),
            provenance=SHORT_PROVENANCE,
            reason="2027-28 ledger composition",
        )

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(SHORT, actor=ACTOR, reason=REASON)

    # The shared governance resolver refuses a roster short of thirty teams.
    assert refused.value.reason == "publication_governance_unavailable"
    assert pointers(engine) == before


def test_cutover_refuses_when_a_pointer_moved_since_the_dry_run(world):
    engine, publications, operations, before = world
    _held_cohort(publications)

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(
            NEW,
            actor=ACTOR,
            reason=REASON,
            expected_fences={PLAYER_LOGS: 1, SEASON_STREAM: 0},
        )

    assert refused.value.reason == "stale_composition"
    assert pointers(engine) == before


def test_dry_run_writes_nothing_and_reports_every_enabled_stream(world):
    engine, publications, operations, before = world
    logs, season, _per36 = _held_cohort(publications)
    counts = _counts(engine)

    plan = operations.cutover_season(
        NEW,
        actor=ACTOR,
        reason=REASON,
        dry_run=True,
    )

    assert _counts(engine) == counts
    assert pointers(engine) == before
    assert plan.to_dict() == {
        "season": NEW,
        "dry_run": True,
        "refusal": None,
        "leave_behind": [],
        "streams": [
            {
                "stream_key": PLAYER_LOGS,
                "season": OLD,
                "publication_id": _id(before, PLAYER_LOGS),
                "fence": 1,
                "candidate_publication_id": logs.publication_id,
                "candidate_cutoff": "2026-10-24T04:00:00+00:00",
                "action": "move",
            },
            {
                "stream_key": L15_STREAM,
                "season": OLD,
                "publication_id": _id(before, L15_STREAM),
                "fence": 1,
                "candidate_publication_id": None,
                "candidate_cutoff": None,
                "action": None,
            },
            {
                "stream_key": SEASON_STREAM,
                "season": OLD,
                "publication_id": _id(before, SEASON_STREAM),
                "fence": 1,
                "candidate_publication_id": season.publication_id,
                "candidate_cutoff": "2026-10-24T04:00:00+00:00",
                "action": "move",
            },
        ],
        # Ten rounds give every team ten; the later manifest's extra game
        # is the first two teams' eleventh.
        "team_completed_games": [
            {
                "team_id": int(team_id),
                "tricode": TRICODES[team_id],
                "completed_games": 11 if team_id in OTHER_GAME[1:] else 10,
            }
            for team_id in TEAM_IDS
        ],
        "moved": [],
    }


def test_dry_run_names_the_refusal_the_cutover_would_raise(world):
    _engine, publications, operations, _before = world
    compose_new(publications, SEASON_STREAM)

    plan = operations.cutover_season(
        NEW,
        actor=ACTOR,
        reason=REASON,
        dry_run=True,
    )

    assert plan.refusal == "cutover_player_game_logs_candidate_required"


# --- Rule 3: after cutover -----------------------------------------------


def _l15_read(engine):
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    return reader.snapshot(
        (PLAYER_LOGS, L15_STREAM),
        season=ReadSeason(OLD),
    ).reads[L15_STREAM]


def test_after_cutover_l15_reads_window_not_ready_until_its_first_candidate(world):
    engine, publications, operations, before = world
    _held_cohort(publications)
    operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    waiting = _l15_read(engine)
    assert (waiting.status, waiting.unavailable_reason, waiting.season, waiting.publication_id) == (
        "missing",
        "window_not_ready",
        OLD,
        _id(before, L15_STREAM),
    )

    first = compose_new(publications, L15_STREAM)

    assert pointers(engine)[L15_STREAM] == (first.publication_id, NEW, "active", 2)
    served = _l15_read(engine)
    assert (served.status, served.unavailable_reason, served.season) == (
        "active",
        None,
        NEW,
    )


def test_family_rollback_works_across_the_transitional_split(world):
    engine, publications, operations, before = world
    _held_cohort(publications)
    operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    family = TraditionalOpponentRebuildService(
        engine,
        publication_service=publications,
        clock=lambda: NOW,
    )

    restored = family.rollback(actor=ACTOR, reason="restore the 2025-26 window")

    after = pointers(engine)
    assert [version.stream_key for version in restored] == [SEASON_STREAM]
    assert after[SEASON_STREAM][1:] == (OLD, "rollback", 3)
    assert after[L15_STREAM] == before[L15_STREAM]
    with engine.connect() as connection:
        checksums = dict(
            connection.execute(
                select(PublicationVersion.publication_id, PublicationVersion.checksum).where(
                    PublicationVersion.publication_id.in_(
                        (
                            after[SEASON_STREAM][0],
                            _id(before, SEASON_STREAM),
                        )
                    )
                )
            ).all()
        )
    assert checksums[after[SEASON_STREAM][0]] == checksums[_id(before, SEASON_STREAM)]


def test_family_rollback_of_the_first_new_season_l15_restores_the_split(world):
    engine, publications, operations, before = world
    _held_cohort(publications)
    operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    # The Season window advances within 2026-27, then L15 reunites the family.
    compose_other(publications, SEASON_STREAM)
    compose_new(publications, L15_STREAM)
    family = TraditionalOpponentRebuildService(
        engine,
        publication_service=publications,
        clock=lambda: NOW,
    )

    family.rollback(actor=ACTOR, reason="withdraw the first 2026-27 L15")

    after = pointers(engine)
    assert after[SEASON_STREAM][1:] == (NEW, "rollback", 4)
    assert after[L15_STREAM][1:] == (OLD, "rollback", 3)


# --- Rule 4: revert ----------------------------------------------------------


def test_revert_restores_every_moved_stream_to_its_pre_cutover_publication(world):
    engine, publications, operations, before = world
    logs, season, _per36 = _held_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    reverted = operations.revert_season_cutover(
        NEW,
        cutover.job_id,
        actor=ACTOR,
        reason="2026-27 opened too early",
    )

    assert pointers(engine) == {
        PLAYER_LOGS: (_id(before, PLAYER_LOGS), OLD, "active", 3),
        SEASON_STREAM: (_id(before, SEASON_STREAM), OLD, "active", 3),
        L15_STREAM: before[L15_STREAM],
    }
    with engine.connect() as connection:
        previous = dict(
            connection.execute(
                select(
                    PublicationPointer.stream_key,
                    PublicationPointer.previous_publication_id,
                )
            ).all()
        )
    assert (previous[PLAYER_LOGS], previous[SEASON_STREAM]) == (None, None)
    for stream_key, promoted in ((PLAYER_LOGS, logs), (SEASON_STREAM, season)):
        # The withdrawn 2026-27 row is revoked; the 2025-26 one serves again.
        assert pointer_history(engine, stream_key) == [
            (_id(before, stream_key), OLD, 1, False),
            (promoted.publication_id, NEW, 2, True),
            (_id(before, stream_key), OLD, 3, False),
        ]
        assert _status(engine, promoted.publication_id) == "superseded"
    assert _audits(engine, "season.cutover_revert") == [
        (
            ACTOR,
            NEW,
            "2026-27 opened too early",
            {
                "cutover_id": cutover.job_id,
                "restored": [
                    {
                        "stream_key": PLAYER_LOGS,
                        "publication_id": _id(before, PLAYER_LOGS),
                        "season": OLD,
                        "fence": 3,
                    },
                    {
                        "stream_key": SEASON_STREAM,
                        "publication_id": _id(before, SEASON_STREAM),
                        "season": OLD,
                        "fence": 3,
                    },
                ],
                "operator_job_id": reverted.job_id,
            },
        )
    ]


def test_a_reverted_season_has_no_retained_authority_even_after_pruning(world):
    engine, publications, operations, before = world
    logs, _season, _per36 = _held_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    assert (
        reader.retained_snapshot((PLAYER_LOGS,), season=NEW).reads[PLAYER_LOGS].publication_id
        == logs.publication_id
    )

    operations.revert_season_cutover(
        NEW,
        cutover.job_id,
        actor=ACTOR,
        reason="2026-27 opened too early",
    )

    withdrawn = reader.retained_snapshot((PLAYER_LOGS,), season=NEW).reads[PLAYER_LOGS]
    assert (withdrawn.available, withdrawn.publication_id) == (False, None)
    publications.prune_history(stream_key=PLAYER_LOGS)
    assert _status(engine, logs.publication_id) is None
    assert reader.retained_snapshot((PLAYER_LOGS,), season=NEW).reads[PLAYER_LOGS].available is False
    assert reader.retained_snapshot((PLAYER_LOGS,), season=OLD).reads[PLAYER_LOGS].publication_id == _id(
        before, PLAYER_LOGS
    )


def test_revert_tolerates_an_enabled_stream_that_never_composed(world):
    engine, publications, operations, before = world
    publications.register_stream(
        "season_hold_probe",
        provider="nba",
        owner="railway",
        required_observations=(),
        publication_strategy="snapshot_replace",
        enabled=True,
    )
    _held_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    operations.revert_season_cutover(
        NEW,
        cutover.job_id,
        actor=ACTOR,
        reason="2026-27 opened too early",
    )

    assert {key: value[1] for key, value in pointers(engine).items()} == {
        PLAYER_LOGS: OLD,
        SEASON_STREAM: OLD,
        L15_STREAM: OLD,
    }


def _advanced_logs_cohort(publications):
    """Advance 2025-26 logs once, so the pre-cutover pointer has a previous."""

    advanced = publications.recompose_ledger(
        PLAYER_LOGS,
        season=OLD,
        cutoff=OLD_CUTOFF,
        payload=payload_for(PLAYER_LOGS, OLD, points=30),
        provenance=OLD_PROVENANCE,
        reason="2025-26 ledger correction",
    )
    _held_cohort(publications)
    return advanced


def test_pruning_before_revert_keeps_every_restoration_target(world):
    engine, publications, operations, before = world
    advanced = _advanced_logs_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

    pruned = publications.prune_history()
    operations.revert_season_cutover(
        NEW,
        cutover.job_id,
        actor=ACTOR,
        reason="2026-27 opened too early",
    )

    # The original 2025-26 logs are only the pre-cutover previous: kept for
    # the revert, which then restores active and previous exactly.
    assert pruned.kept["cutover_restore"] == 1
    assert pointers(engine)[PLAYER_LOGS] == (advanced.publication_id, OLD, "active", 4)
    with engine.connect() as connection:
        previous = connection.scalar(
            select(
                PublicationPointer.previous_publication_id,
            ).where(PublicationPointer.stream_key == PLAYER_LOGS)
        )
    assert previous == _id(before, PLAYER_LOGS)
    restored = publications.rollback(PLAYER_LOGS, reason="undo the correction")
    assert (restored.season, restored.checksum) == (
        OLD,
        publications_checksum(engine, _id(before, PLAYER_LOGS)),
    )


def publications_checksum(engine, publication_id):
    with engine.connect() as connection:
        return connection.scalar(
            select(PublicationVersion.checksum).where(PublicationVersion.publication_id == publication_id)
        )


def test_revert_refuses_when_a_restoration_target_is_gone(world):
    from sqlalchemy import delete

    engine, publications, operations, before = world
    _advanced_logs_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    with engine.begin() as connection:
        connection.execute(
            delete(PublicationVersion).where(PublicationVersion.publication_id == _id(before, PLAYER_LOGS))
        )
    unchanged = _durable_state(engine)

    with pytest.raises(ControlPlaneError) as refused:
        operations.revert_season_cutover(
            NEW,
            cutover.job_id,
            actor=ACTOR,
            reason="2026-27 opened too early",
        )

    assert refused.value.reason == "cutover_restore_unavailable"
    assert _durable_state(engine) == unchanged


def test_a_reversed_cutover_no_longer_protects_its_targets(world):
    engine, publications, operations, before = world
    _advanced_logs_cohort(publications)
    operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    compose_new(publications, L15_STREAM)  # a later move: no longer reversible

    pruned = publications.prune_history(stream_key=PLAYER_LOGS)

    assert pruned.kept["cutover_restore"] == 0
    assert publications_checksum(engine, _id(before, PLAYER_LOGS)) is None


def test_revert_refuses_after_a_later_move(world):
    engine, publications, operations, _before = world
    _held_cohort(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    compose_new(publications, L15_STREAM)
    moved = pointers(engine)

    with pytest.raises(ControlPlaneError) as refused:
        operations.revert_season_cutover(
            NEW,
            cutover.job_id,
            actor=ACTOR,
            reason="2026-27 opened too early",
        )

    assert refused.value.reason == "cutover_superseded"
    assert pointers(engine) == moved
    assert _audits(engine, "season.cutover_revert") == []


def test_revert_of_an_unknown_cutover_is_not_found(world):
    _engine, _publications, operations, _before = world

    with pytest.raises(ControlPlaneError) as refused:
        operations.revert_season_cutover(
            NEW,
            "no-such-cutover",
            actor=ACTOR,
            reason="nothing to revert",
        )

    assert refused.value.reason == "cutover_not_found"


# --- Rule 1 through the nightly composition job --------------------------


def _held_runtime(tmp_path, *, games=None, streams=(PLAYER_LOGS, SEASON_STREAM, L15_STREAM)):
    """A 2025-26 runtime composing while 2024-25 is published on ``streams``."""

    from datetime import datetime, timedelta

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

    utc = UTC
    cutoff = datetime(2025, 10, 15, 5, 22, tzinfo=utc)
    runtime_now = cutoff + timedelta(hours=18)
    published = "2024-25"
    engine = create_engine(f"sqlite:///{tmp_path / 'held-job.sqlite3'}")
    run_migrations(engine)
    games = games or _league_games()
    with engine.begin() as connection:
        catalog = _event_catalog_publication(games, cutoff, "held-event-catalog")
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(
            ActiveSeason.__table__.insert().values(
                season="2025-26",
                phase="Regular Season",
                status="active",
                cutoff=cutoff,
                activated_at=cutoff,
                activated_by="test",
            )
        )
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="held-manifest",
                season="2025-26",
                cutoff=cutoff,
                collect_before=cutoff + timedelta(days=30),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum="held-manifest",
                event_catalog_publication_id=catalog["publication_id"],
                event_catalog_checksum=catalog["checksum"],
                status="active",
                created_at=cutoff,
            )
        )
        connection.execute(
            EventCatalogEntry.__table__.insert(),
            [
                {
                    "nba_game_id": game.game_id,
                    "season": game.season,
                    "home_team_id": game.home_team_id,
                    "home_team_name": f"Team {game.home_team_id}",
                    "home_team_tricode": game.home_team_tricode,
                    "away_team_id": game.away_team_id,
                    "away_team_name": f"Team {game.away_team_id}",
                    "away_team_tricode": game.away_team_tricode,
                    "scheduled_at": datetime.combine(game.game_date, datetime.min.time(), utc),
                    "status_text": "Final",
                    "status_code": 3,
                    "classification": "Regular Season",
                    "first_seen_at": cutoff,
                    "last_seen_at": cutoff,
                }
                for game in games
            ],
        )

    publications = PublicationService(engine, clock=lambda: runtime_now)
    publications.register_default_streams()
    published_ids = {}
    for stream_key in streams:
        publications.register_stream(
            stream_key,
            provider="ledger",
            owner="railway",
            required_observations=(),
            publication_strategy="ledger_compose",
            enabled=True,
        )
        published_ids[stream_key] = publications.compose(
            stream_key,
            season=published,
            cutoff=cutoff - timedelta(days=180),
            payload=payload_for(stream_key, published),
        ).publication_id

    queue = LedgerCorrectionQueue(clock=lambda: runtime_now, require_governance=True)
    ledger = CanonicalGameLedgerRepository(engine, correction_sink=queue)
    ledger.replace_games_atomic(
        games,
        accepted_observations={
            game.source_observation_id: _boundary_observation_values(
                game,
                cutoff=cutoff,
                manifest_id="held-manifest",
            )
            for game in games
        },
    )
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
            engine,
            clock=lambda: runtime_now,
        ),
        publication_service=publications,
        clock=lambda: runtime_now,
    )

    return engine, runtime, published_ids


def test_a_held_composition_job_settles_succeeded_and_is_not_retried(tmp_path):
    from app.models.collection_control import CompositionJob
    from app.services.ledger_materialization import LedgerCorrectionQueue

    engine, runtime, published_ids = _held_runtime(tmp_path)
    published = "2024-25"

    assert runtime.compose_queued("2025-26") == len(LedgerCorrectionQueue.STREAMS)

    with engine.connect() as connection:
        jobs = connection.execute(
            select(CompositionJob.stream_key, CompositionJob.status, CompositionJob.last_error).order_by(
                CompositionJob.stream_key
            )
        ).all()
        held = dict(
            connection.execute(
                select(PublicationVersion.stream_key, PublicationVersion.status).where(
                    PublicationVersion.season == "2025-26",
                    PublicationVersion.stream_key.in_(published_ids),
                )
            ).all()
        )
    assert jobs == [(stream_key, "succeeded", None) for stream_key in sorted(LedgerCorrectionQueue.STREAMS)]
    assert {key: value[0] for key, value in pointers(engine).items()} == published_ids
    assert held == {
        PLAYER_LOGS: "candidate",
        SEASON_STREAM: "candidate",
        L15_STREAM: "candidate",
    }
    with engine.connect() as connection:
        job_ids = dict(connection.execute(select(CompositionJob.stream_key, CompositionJob.job_id)).all())
    assert [
        (resource, details["season"], details["published_season"], details["composition_job_id"])
        for _actor, resource, _reason, details in _audits(engine, "publication.held")
    ] == [
        (PLAYER_LOGS, "2025-26", published, job_ids[PLAYER_LOGS]),
        (L15_STREAM, "2025-26", published, job_ids[L15_STREAM]),
        (SEASON_STREAM, "2025-26", published, job_ids[SEASON_STREAM]),
    ]
    # Nothing is left queued or failed, so no later pass recomposes it.
    assert runtime.compose_queued("2025-26") == 0


def test_a_composition_failed_for_incomplete_assist_evidence_holds_no_candidate(tmp_path):
    from dataclasses import replace

    from app.models.collection_control import CompositionJob
    from app.services.canonical_game_ledger import raw_rows_from_facts
    from tests.services.test_ledger_derivations import _league_games

    games = list(_league_games())
    first = games[0]
    without_locations = replace(
        first,
        player_facts=tuple(
            replace(
                player,
                two_point_assists=None,
                three_point_assists=None,
                arc3_assists=None,
                corner3_assists=None,
                at_rim_assists=None,
                short_mid_range_assists=None,
                long_mid_range_assists=None,
            )
            for player in first.player_facts
        ),
        checksum=None,
    )
    games[0] = replace(without_locations, raw_rows=raw_rows_from_facts(without_locations)).with_checksum()
    engine, runtime, published_ids = _held_runtime(
        tmp_path,
        games=tuple(games),
        streams=(PLAYER_LOGS, SEASON_STREAM, "assist_locations_season"),
    )

    runtime.compose_queued("2025-26")

    with engine.connect() as connection:
        job = connection.execute(
            select(CompositionJob.status, CompositionJob.last_error).where(
                CompositionJob.stream_key == "assist_locations_season"
            )
        ).one()
        versions = connection.execute(
            select(PublicationVersion.season, PublicationVersion.status).where(
                PublicationVersion.stream_key == "assist_locations_season"
            )
        ).all()
    assert tuple(job) == ("failed", "assist_location_evidence_incomplete")
    assert versions == [("2024-25", "active")]
    assert pointers(engine)["assist_locations_season"][0] == published_ids["assist_locations_season"]


# --- Concurrency on Postgres -------------------------------------------------


# --- Leave behind (#471) -------------------------------------------------

PROBE = "pace_probe"


def _probe_on_other_manifest(publications):
    """A collector-style stream whose newest 2026-27 candidate is another night's."""

    publications.register_stream(
        PROBE,
        provider="ledger",
        owner="railway",
        required_observations=["canonical_game_ledger"],
        publication_strategy="ledger_compose",
        enabled=True,
    )
    publications.recompose_ledger(
        PROBE,
        season=OLD,
        cutoff=OLD_CUTOFF,
        payload=payload_for(PROBE, OLD),
        provenance=OLD_PROVENANCE,
        reason="2025-26 ledger composition",
    )
    return compose_other(publications, PROBE)


def _probe_world(world):
    engine, publications, operations, _before = world
    logs, season, _per36 = _held_cohort(publications)
    probe = _probe_on_other_manifest(publications)
    return engine, publications, operations, pointers(engine), logs, season, probe


def test_dry_run_plans_a_stream_on_another_manifest_as_leave_behind(world):
    _engine, _publications, operations, _before, logs, season, probe = _probe_world(world)

    plan = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, dry_run=True).to_dict()

    assert plan["refusal"] is None
    assert plan["leave_behind"] == [{"stream_key": PROBE, "reason": "cutover_authority_mismatch"}]
    assert [
        (row["stream_key"], row["action"], row["candidate_publication_id"]) for row in plan["streams"]
    ] == [
        (PROBE, "leave_behind", probe.publication_id),
        (PLAYER_LOGS, "move", logs.publication_id),
        (L15_STREAM, None, None),
        (SEASON_STREAM, "move", season.publication_id),
    ]


def test_cutover_with_the_leave_behind_list_moves_every_other_stream(world):
    engine, _publications, operations, before, logs, season, probe = _probe_world(world)

    result = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[PROBE])

    assert pointers(engine) == {
        PLAYER_LOGS: (logs.publication_id, NEW, "active", 2),
        SEASON_STREAM: (season.publication_id, NEW, "active", 2),
        L15_STREAM: before[L15_STREAM],
        PROBE: before[PROBE],
    }
    assert _status(engine, probe.publication_id) == "candidate"
    assert [item["stream_key"] for item in result.resource.moved] == [PLAYER_LOGS, SEASON_STREAM]
    [(_actor, _resource, _reason, audit)] = _audits(engine, "season.cutover")
    assert audit["left_behind"] == [{"stream_key": PROBE, "reason": "cutover_authority_mismatch"}]
    assert [item["stream_key"] for item in audit["moved"]] == [PLAYER_LOGS, SEASON_STREAM]


@pytest.mark.parametrize("listed", [None, [], ["other_stream"], [PROBE, L15_STREAM], [PROBE, PLAYER_LOGS]])
def test_cutover_without_the_exact_leave_behind_list_refuses_and_moves_nothing(world, listed):
    engine, _publications, operations, before, *_ = _probe_world(world)
    unchanged = _durable_state(engine)

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=listed)

    assert refused.value.reason == "cutover_leave_behind_mismatch"
    assert pointers(engine) == before
    assert _durable_state(engine) == unchanged
    assert _audits(engine, "season.cutover") == []


def test_a_leave_behind_list_for_a_cutover_that_leaves_nothing_behind_refuses(world):
    engine, publications, operations, before = world
    _held_cohort(publications)

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[SEASON_STREAM])

    assert refused.value.reason == "cutover_leave_behind_mismatch"
    assert pointers(engine) == before


def test_a_player_game_logs_failure_still_refuses_even_when_the_stream_is_listed(world):
    from tests.support.season_cutover import NEW_GAMES, provenance

    engine, publications, operations, before = world
    compose_new(publications, PLAYER_LOGS, provenance=provenance(NEW_GAMES[:14]))
    compose_new(publications, SEASON_STREAM)
    _probe_on_other_manifest(publications)
    before = pointers(engine)

    plan = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, dry_run=True)
    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[PROBE, PLAYER_LOGS])

    assert (plan.refusal, plan.to_dict()["leave_behind"]) == ("publication_game_set_mismatch", [])
    assert refused.value.reason == "publication_game_set_mismatch"
    assert pointers(engine) == before


def test_a_failing_family_member_leaves_the_whole_family_on_the_old_season(world):
    engine, publications, operations, before = world
    logs = compose_new(publications, PLAYER_LOGS)
    season = compose_new(publications, SEASON_STREAM)
    l15 = compose_other(publications, L15_STREAM)

    plan = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, dry_run=True).to_dict()
    assert plan["refusal"] is None
    assert plan["leave_behind"] == [
        {"stream_key": L15_STREAM, "reason": "cutover_authority_mismatch"},
        {"stream_key": SEASON_STREAM, "reason": "publication_family_coupled"},
    ]
    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[SEASON_STREAM])
    assert refused.value.reason == "cutover_leave_behind_mismatch"
    assert pointers(engine) == before

    operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[L15_STREAM, SEASON_STREAM])

    assert pointers(engine) == {
        PLAYER_LOGS: (logs.publication_id, NEW, "active", 2),
        SEASON_STREAM: before[SEASON_STREAM],
        L15_STREAM: before[L15_STREAM],
    }
    assert (_status(engine, season.publication_id), _status(engine, l15.publication_id)) == (
        "candidate",
        "candidate",
    )


def test_a_trailing_stream_left_behind_serves_its_prior_season_then_activates_on_its_next_composition(world):
    engine, publications, operations, before = world
    compose_new(publications, PLAYER_LOGS)
    compose_new(publications, SEASON_STREAM)
    compose_other(publications, L15_STREAM)
    operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[L15_STREAM, SEASON_STREAM])

    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    waiting = reader.snapshot((PLAYER_LOGS, SEASON_STREAM), season=ReadSeason(NEW)).reads[SEASON_STREAM]
    assert (waiting.status, waiting.unavailable_reason, waiting.season, waiting.publication_id) == (
        "prior_season",
        None,
        OLD,
        _id(before, SEASON_STREAM),
    )

    first = compose_new(publications, SEASON_STREAM, points=12)

    assert pointers(engine)[SEASON_STREAM] == (first.publication_id, NEW, "active", 2)
    served = reader.snapshot((PLAYER_LOGS, SEASON_STREAM), season=ReadSeason(NEW)).reads[SEASON_STREAM]
    assert (served.status, served.unavailable_reason, served.season) == ("active", None, NEW)


def test_a_leave_behind_list_that_goes_stale_after_the_plan_refuses(world):
    engine, publications, operations, before, *_ = _probe_world(world)
    planned = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, dry_run=True).to_dict()
    assert [item["stream_key"] for item in planned["leave_behind"]] == [PROBE]
    # Another stream's newest candidate now rests on the wrong manifest.
    compose_other(publications, SEASON_STREAM)
    unchanged = _durable_state(engine)

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[PROBE])

    assert refused.value.reason == "cutover_leave_behind_mismatch"
    assert pointers(engine) == before
    assert _durable_state(engine) == unchanged
    assert _audits(engine, "season.cutover") == []


def test_expected_fences_are_checked_for_a_stream_being_left_behind(world):
    engine, _publications, operations, before, *_ = _probe_world(world)
    unchanged = _durable_state(engine)

    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(
            NEW,
            actor=ACTOR,
            reason=REASON,
            leave_behind=[PROBE],
            expected_fences={PLAYER_LOGS: 1, SEASON_STREAM: 1, PROBE: 0},
        )

    assert refused.value.reason == "stale_composition"
    assert pointers(engine) == before
    assert _durable_state(engine) == unchanged


def test_revert_refuses_once_a_stream_left_behind_has_advanced(world):
    engine, publications, operations, before = world
    _advanced_logs_cohort(publications)
    _probe_on_other_manifest(publications)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[PROBE])
    first = compose_new(publications, PROBE, points=12)
    assert pointers(engine)[PROBE] == (first.publication_id, NEW, "active", 2)

    with pytest.raises(ControlPlaneError) as refused:
        operations.revert_season_cutover(NEW, cutover.job_id, actor=ACTOR, reason="too early")
    pruned = publications.prune_history(stream_key=PLAYER_LOGS)

    assert refused.value.reason == "cutover_superseded"
    # The cutover is no longer reversible, so it stops protecting its targets.
    assert pruned.kept["cutover_restore"] == 0
    assert publications_checksum(engine, _id(before, PLAYER_LOGS)) is None


def test_revert_restores_only_the_streams_that_moved(world):
    engine, _publications, operations, before, *_ = _probe_world(world)
    cutover = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, leave_behind=[PROBE])

    operations.revert_season_cutover(NEW, cutover.job_id, actor=ACTOR, reason="opened too early")

    assert pointers(engine) == {
        PLAYER_LOGS: (_id(before, PLAYER_LOGS), OLD, "active", 3),
        SEASON_STREAM: (_id(before, SEASON_STREAM), OLD, "active", 3),
        L15_STREAM: before[L15_STREAM],
        PROBE: before[PROBE],
    }
    [(_a, _r, _reason, audit)] = _audits(engine, "season.cutover_revert")
    assert [item["stream_key"] for item in audit["restored"]] == [PLAYER_LOGS, SEASON_STREAM]


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
            engine,
            publication_service=publications,
            clock=lambda: NOW,
        )
        racing = {"thread": None, "outcome": []}

        def compose_l15():
            try:
                racing["outcome"].append(compose_new(publications, L15_STREAM))
            except ControlPlaneError as error:
                racing["outcome"].append(error)

        @event.listens_for(engine, "after_cursor_execute")
        def interleave(connection, cursor, statement, *_):
            if racing["thread"] is None and statement.startswith("UPDATE publication_pointers"):
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
            PLAYER_LOGS: NEW,
            SEASON_STREAM: NEW,
            L15_STREAM: NEW,
        }
    finally:
        reset_schema()
        engine.dispose()


# --- Rule 4: trailing families (crf04/statsplus#304) -------------------------


def _opening_week_cutover(operations, publications):
    """Cut over in the opening week: every team has played one game."""

    open_opening_week(publications.engine)
    logs = compose_thin(publications, PLAYER_LOGS)
    season = compose_thin(publications, SEASON_STREAM)
    plan = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, dry_run=True).to_dict()
    cutover = operations.cutover_season(
        NEW,
        actor=ACTOR,
        reason=REASON,
        leave_behind=[item["stream_key"] for item in plan["leave_behind"]],
    )
    return logs, season, plan, cutover


def _snapshot_reads(engine):
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    snapshot = reader.snapshot((PLAYER_LOGS, SEASON_STREAM, L15_STREAM), season=ReadSeason(NEW))
    return snapshot.season, {
        key: (read.status, read.unavailable_reason, read.season, read.publication_id, read.available)
        for key, read in snapshot.reads.items()
    }


def test_an_opening_week_cutover_plans_a_not_ready_family_as_left_behind(world):
    engine, publications, operations, _before = world
    open_opening_week(engine)
    logs = compose_thin(publications, PLAYER_LOGS)
    season = compose_thin(publications, SEASON_STREAM)

    plan = operations.cutover_season(NEW, actor=ACTOR, reason=REASON, dry_run=True).to_dict()

    assert plan["refusal"] is None
    assert plan["leave_behind"] == [{"stream_key": SEASON_STREAM, "reason": "serving_season_not_ready"}]
    assert [
        (row["stream_key"], row["action"], row["candidate_publication_id"]) for row in plan["streams"]
    ] == [
        (PLAYER_LOGS, "move", logs.publication_id),
        (L15_STREAM, None, None),
        (SEASON_STREAM, "leave_behind", season.publication_id),
    ]
    with pytest.raises(ControlPlaneError) as refused:
        operations.cutover_season(NEW, actor=ACTOR, reason=REASON)
    assert refused.value.reason == "cutover_leave_behind_mismatch"


def test_an_opening_week_cutover_moves_the_game_logs_and_leaves_team_defense_trailing(world):
    engine, publications, operations, before = world

    logs, season, _plan, cutover = _opening_week_cutover(operations, publications)

    assert pointers(engine) == {
        PLAYER_LOGS: (logs.publication_id, NEW, "active", 2),
        SEASON_STREAM: before[SEASON_STREAM],
        L15_STREAM: before[L15_STREAM],
    }
    assert _status(engine, season.publication_id) == "candidate"
    assert [dict(item) for item in cutover.resource.left_behind] == [
        {"stream_key": SEASON_STREAM, "reason": "serving_season_not_ready"}
    ]
    # Team defense serves 2025-26, labelled; Last 15 still waits, as before.
    assert _snapshot_reads(engine) == (
        NEW,
        {
            PLAYER_LOGS: ("active", None, NEW, logs.publication_id, True),
            SEASON_STREAM: ("prior_season", None, OLD, _id(before, SEASON_STREAM), True),
            L15_STREAM: ("missing", "window_not_ready", OLD, _id(before, L15_STREAM), False),
        },
    )


def test_a_not_ready_composition_is_held_and_the_first_ready_one_activates(world):
    engine, publications, operations, before = world
    _opening_week_cutover(operations, publications)

    held = compose_thin(publications, SEASON_STREAM)

    assert pointers(engine)[SEASON_STREAM] == before[SEASON_STREAM]
    assert (held.season, held.status, _aware_cutoff(held)) == (NEW, "candidate", THIN_CUTOFF)
    assert [(resource, reason) for _actor, resource, reason, _details in _audits(engine, "publication.held")][
        -1:
    ] == [(SEASON_STREAM, "serving_season_not_ready")]

    accept_ledger(engine, NEW_GAMES[:15], "pbp")
    ready = compose_new(publications, SEASON_STREAM)

    assert pointers(engine)[SEASON_STREAM] == (ready.publication_id, NEW, "active", 2)
    assert _snapshot_reads(engine)[1][SEASON_STREAM] == ("active", None, NEW, ready.publication_id, True)


def test_before_the_cutover_a_read_of_the_next_season_serves_no_trailing_family(world):
    engine, _publications, _operations, _before = world

    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    reads = reader.snapshot((PLAYER_LOGS, SEASON_STREAM), season=NEW).reads

    # 2025-26 is the Published Season, so 2026-27 has nothing to trail.
    assert {key: (read.status, read.unavailable_reason, read.season) for key, read in reads.items()} == {
        PLAYER_LOGS: ("missing", "window_not_ready", OLD),
        SEASON_STREAM: ("missing", "window_not_ready", OLD),
    }


def test_reverting_an_opening_week_cutover_restores_every_read_to_the_old_season(world):
    engine, publications, operations, before = world
    _logs, _season, _plan, cutover = _opening_week_cutover(operations, publications)

    operations.revert_season_cutover(NEW, cutover.job_id, actor=ACTOR, reason="2026-27 opened too early")

    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    snapshot = reader.snapshot((PLAYER_LOGS, SEASON_STREAM, L15_STREAM), season=ReadSeason(NEW))
    assert snapshot.season == OLD
    assert {key: (read.status, read.season, read.publication_id) for key, read in snapshot.reads.items()} == {
        key: ("active", OLD, _id(before, key)) for key in (PLAYER_LOGS, SEASON_STREAM, L15_STREAM)
    }


def _aware_cutoff(publication):
    return publication.cutoff.replace(tzinfo=UTC) if publication.cutoff.tzinfo is None else publication.cutoff


def test_an_opening_week_cutover_and_the_first_ready_composition_on_postgres():
    import os

    from sqlalchemy import create_engine, text

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
        before = pointers(engine)
        operations = CollectionOperationsService(engine, publication_service=publications, clock=lambda: NOW)
        logs, _season, plan, _cutover = _opening_week_cutover(operations, publications)

        assert plan["leave_behind"] == [{"stream_key": SEASON_STREAM, "reason": "serving_season_not_ready"}]
        assert _snapshot_reads(engine)[1][SEASON_STREAM] == (
            "prior_season",
            None,
            OLD,
            _id(before, SEASON_STREAM),
            True,
        )
        accept_ledger(engine, NEW_GAMES[:15], "pbp")
        ready = compose_new(publications, SEASON_STREAM)
        assert pointers(engine) == {
            PLAYER_LOGS: (logs.publication_id, NEW, "active", 2),
            SEASON_STREAM: (ready.publication_id, NEW, "active", 2),
            L15_STREAM: before[L15_STREAM],
        }
    finally:
        reset_schema()
        engine.dispose()


def test_a_read_racing_the_cutover_judges_trailing_by_its_own_generation_on_postgres():
    """The Published Season a trailing read is judged by is the one it captured.

    A 2026-27 read that captures the old team-defense pointer just before a
    cutover commits must not then see the new game-log pointer and serve the
    replaced defense as ``prior_season``: neither generation allows that.
    """

    import os

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
        _logs, defense, _per36 = _held_cohort(publications)
        operations = CollectionOperationsService(engine, publication_service=publications, clock=lambda: NOW)
        raced = []

        def cut_over_after_the_capture(_connection, _cursor, statement, *_):
            if not raced and statement.startswith("SELECT publication_streams."):
                raced.append(True)
                operations.cutover_season(NEW, actor=ACTOR, reason=REASON)

        event.listen(engine, "after_cursor_execute", cut_over_after_the_capture)
        try:
            read = (
                DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
                .snapshot((SEASON_STREAM,), season=NEW)
                .reads[SEASON_STREAM]
            )
        finally:
            event.remove(engine, "after_cursor_execute", cut_over_after_the_capture)

        assert raced == [True]
        assert pointers(engine)[SEASON_STREAM][0] == defense.publication_id
        assert (read.status, read.unavailable_reason, read.season) == ("missing", "window_not_ready", OLD)
    finally:
        reset_schema()
        engine.dispose()
