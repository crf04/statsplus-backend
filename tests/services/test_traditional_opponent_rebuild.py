"""Durable traditional-opponent family rebuilds.

Every assertion crosses the rebuild service's interface and observes durable
state: rows, pointers, fences, and the bounded status projection.  Nothing
asserts that a particular private helper was called, so the tests survive a
restructuring of composition or persistence.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.collection_control import (
    PublicationActivation,
    PublicationPointer,
    PublicationRebuild,
    PublicationVersion,
)
from app.services.collection_control import ControlPlaneError, PublicationService
from app.services.database_first_activation import decode_team_window
from app.services.traditional_opponent_publications import (
    normalize_traditional_opponent_window,
    TRADITIONAL_OPPONENT_FAMILY,
    TRADITIONAL_OPPONENT_TARGET_FORMAT,
    TRADITIONAL_OPPONENT_V1,
    TRADITIONAL_OPPONENT_V2,
)
from app.services.traditional_opponent_rebuild import (
    ACTIVE_REBUILD_STATES,
    TraditionalOpponentRebuildService,
)
from tests.support.traditional_opponent_family import (
    CUTOFF,
    L15_STREAM,
    NOW,
    SEASON,
    SEASON_STREAM,
    active_expectation,
    payload,
    seed_family,
    v2_payload,
)

UTC = timezone.utc
ACTOR = "operator@example.com"
REASON = "publish the opponent rebound split"


def _v2_composer(*, mutate=None):
    """Compose both windows in the deployed target format."""

    def compose(sources):
        return {
            stream_key: v2_payload(mutate=mutate)
            for stream_key in sources.stream_keys
        }

    return compose


@pytest.fixture
def family(tmp_path):
    engine = seed_family(tmp_path)
    publications = PublicationService(engine, clock=lambda: NOW)
    return engine, publications


def _service(engine, *, compose=None, clock=None, lease_seconds=300):
    return TraditionalOpponentRebuildService(
        engine,
        publication_service=PublicationService(
            engine, clock=clock or (lambda: NOW)
        ),
        compose=compose or _v2_composer(),
        clock=clock or (lambda: NOW),
        lease_seconds=lease_seconds,
    )


def _pointers(engine):
    publications = PublicationService(engine, clock=lambda: NOW)
    with publications.session() as session:
        return {
            stream_key: (
                session.get(PublicationPointer, stream_key).active_publication_id,
                int(session.get(PublicationPointer, stream_key).fence),
            )
            for stream_key in (SEASON_STREAM, L15_STREAM)
        }


# --- Starting a rebuild -----------------------------------------------------


def test_a_start_records_a_durable_queued_rebuild_bound_to_the_active_pair(family):
    engine, publications = family
    expected = active_expectation(publications)

    rebuild = _service(engine).start(
        actor=ACTOR, reason=REASON, expected=expected
    )

    assert rebuild.state == "queued"
    assert rebuild.family == TRADITIONAL_OPPONENT_FAMILY
    assert rebuild.target_format == TRADITIONAL_OPPONENT_TARGET_FORMAT.name
    assert (
        rebuild.target_fingerprint
        == TRADITIONAL_OPPONENT_TARGET_FORMAT.fingerprint
    )
    assert rebuild.expected_season_publication_id == expected.season_publication_id
    assert rebuild.expected_l15_publication_id == expected.l15_publication_id
    assert rebuild.season == SEASON
    # The active pair's own authority is reused, never re-supplied.
    assert rebuild.manifest_id == "ledger-manifest"
    assert rebuild.event_catalog_publication_id == "event-catalog"
    assert rebuild.source_checksum


def test_the_deployed_module_owns_the_target_format(family):
    """A request names a family and a state, never a rendered format."""

    engine, publications = family
    rebuild = _service(engine).start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )

    assert rebuild.target_format == TRADITIONAL_OPPONENT_V2.name
    assert TRADITIONAL_OPPONENT_TARGET_FORMAT is TRADITIONAL_OPPONENT_V2


def test_a_start_refuses_a_stale_expected_pair(family):
    engine, publications = family
    expected = active_expectation(publications)
    stale = expected.__class__(
        season_publication_id=expected.season_publication_id,
        season_fence=expected.season_fence + 5,
        l15_publication_id=expected.l15_publication_id,
        l15_fence=expected.l15_fence,
    )

    with pytest.raises(ControlPlaneError, match="stale_publication_family"):
        _service(engine).start(actor=ACTOR, reason=REASON, expected=stale)


def test_supplied_season_and_cutoff_are_assertions_not_replacement_authority(family):
    engine, publications = family
    expected = active_expectation(publications)
    service = _service(engine)

    # An assertion that contradicts the active pair is refused outright; it is
    # never accepted as the authority the rebuild should use instead.
    with pytest.raises(ControlPlaneError, match="stale_publication_family"):
        service.start(
            actor=ACTOR, reason=REASON, expected=expected,
            season="2024-25", cutoff=CUTOFF,
        )

    accepted = service.start(
        actor=ACTOR, reason=REASON, expected=expected,
        season=SEASON, cutoff=CUTOFF,
    )
    assert accepted.season == SEASON
    assert accepted.cutoff is not None


def test_a_short_reason_and_a_missing_actor_are_refused(family):
    engine, publications = family
    expected = active_expectation(publications)

    with pytest.raises(ControlPlaneError, match="reason_required"):
        _service(engine).start(actor=ACTOR, reason="x", expected=expected)
    with pytest.raises(ControlPlaneError, match="actor_required"):
        _service(engine).start(actor="  ", reason=REASON, expected=expected)


def test_a_mixed_starting_family_is_terminal_rather_than_rebuildable(tmp_path):
    """One window already in v2 while its sibling is in v1 is not a start state."""

    engine = seed_family(tmp_path)
    publications = PublicationService(engine, clock=lambda: NOW)
    publications.recompose_ledger(
        L15_STREAM, season=SEASON, cutoff=CUTOFF, payload=v2_payload(),
        provenance={f"pbp:game-{index}": f"game-{index}" for index in range(15)},
        reason="drifted window",
    )

    with pytest.raises(ControlPlaneError, match="publication_family_mixed_format"):
        _service(engine).start(
            actor=ACTOR, reason=REASON,
            expected=active_expectation(publications),
        )


# --- Idempotency and conflict ----------------------------------------------


def test_an_identical_active_request_is_the_same_rebuild(family):
    engine, publications = family
    expected = active_expectation(publications)
    service = _service(engine)

    first = service.start(actor=ACTOR, reason=REASON, expected=expected)
    again = service.start(actor=ACTOR, reason=REASON, expected=expected)

    assert again.rebuild_id == first.rebuild_id
    with publications.session() as session:
        assert len(session.scalars(select(PublicationRebuild)).all()) == 1


def test_a_conflicting_active_request_for_the_family_is_a_duplicate(family):
    engine, publications = family
    expected = active_expectation(publications)
    service = _service(engine)
    service.start(actor=ACTOR, reason=REASON, expected=expected)

    with pytest.raises(ControlPlaneError, match="duplicate_active_operation"):
        service.start(
            actor=ACTOR, reason="a different operator reason", expected=expected
        )


def test_a_repeated_completed_request_returns_the_completed_receipt(family):
    engine, publications = family
    expected = active_expectation(publications)
    service = _service(engine)
    rebuild = service.start(actor=ACTOR, reason=REASON, expected=expected)
    service.run(rebuild.rebuild_id, owner="worker-1")

    receipt = service.start(actor=ACTOR, reason=REASON, expected=expected)

    assert receipt.rebuild_id == rebuild.rebuild_id
    assert receipt.state == "succeeded"


def test_new_expected_generations_create_a_new_rebuild(family):
    engine, publications = family
    service = _service(engine)
    first = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(first.rebuild_id, owner="worker-1")

    second = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )

    assert second.rebuild_id != first.rebuild_id
    assert second.state == "queued"


# --- Successful paired promotion -------------------------------------------


def test_a_successful_rebuild_promotes_both_windows_atomically(family):
    engine, publications = family
    expected = active_expectation(publications)
    before = _pointers(engine)
    service = _service(engine)
    rebuild = service.start(actor=ACTOR, reason=REASON, expected=expected)

    done = service.run(rebuild.rebuild_id, owner="worker-1")

    assert done.state == "succeeded"
    assert done.error_code is None
    after = _pointers(engine)
    for stream_key in (SEASON_STREAM, L15_STREAM):
        assert after[stream_key][0] != before[stream_key][0]
        assert after[stream_key][1] == before[stream_key][1] + 1
    assert after[SEASON_STREAM][0] == done.promoted_season_publication_id
    assert after[L15_STREAM][0] == done.promoted_l15_publication_id
    # Both promoted payloads are the deployed target format.
    assert publications.current(SEASON_STREAM).status == "active"
    assert publications.current(L15_STREAM).status == "active"


def test_promotion_records_activation_evidence_for_both_windows(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")

    with publications.session() as session:
        activations = session.scalars(select(PublicationActivation)).all()
    assert {row.stream_key for row in activations} == {SEASON_STREAM, L15_STREAM}


def test_existing_reads_keep_the_old_pair_until_the_rebuild_commits(family):
    """Staging and validation happen without moving either pointer."""

    engine, publications = family
    before = _pointers(engine)
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    staged = service.stage(rebuild.rebuild_id, owner="worker-1")

    assert staged.state == "validating"
    assert staged.staged_season_publication_id
    assert staged.staged_l15_publication_id
    assert _pointers(engine) == before


def test_a_composed_candidate_that_is_not_the_target_format_is_terminal(family):
    engine, publications = family

    def compose_v1(sources):
        return {
            stream_key: payload(TRADITIONAL_OPPONENT_V1)
            for stream_key in sources.stream_keys
        }

    service = _service(engine, compose=compose_v1)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    before = _pointers(engine)

    failed = service.run(rebuild.rebuild_id, owner="worker-1")

    assert failed.state == "failed"
    assert failed.error_code == "publication_format_unsupported"
    assert _pointers(engine) == before


def test_a_semantically_invalid_candidate_fails_the_whole_rebuild(family):
    engine, publications = family

    def break_the_identity(rows):
        rows[4]["counts"] = {**rows[4]["counts"], "offensive_rebounds": 12.0}

    service = _service(engine, compose=_v2_composer(mutate=break_the_identity))
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    before = _pointers(engine)

    failed = service.run(rebuild.rebuild_id, owner="worker-1")

    assert failed.state == "failed"
    assert failed.error_code == "publication_candidate_invalid"
    assert _pointers(engine) == before


# --- Stale preconditions ----------------------------------------------------


def test_a_pointer_that_moved_during_the_rebuild_prevents_both_promotions(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.stage(rebuild.rebuild_id, owner="worker-1")
    # An accepted ledger correction wins the race and moves one pointer.
    publications.recompose_ledger(
        SEASON_STREAM, season=SEASON, cutoff=CUTOFF,
        payload=payload(TRADITIONAL_OPPONENT_V1, mutate=_bump_points),
        provenance={f"pbp:game-{index}": f"game-{index}" for index in range(15)},
        reason="ledger correction",
    )
    before = _pointers(engine)

    failed = service.promote(rebuild.rebuild_id, owner="worker-1")

    assert failed.state == "failed"
    assert failed.error_code == "stale_publication_family"
    assert _pointers(engine) == before


def _bump_points(rows):
    rows[0]["counts"] = {**rows[0]["counts"], "points": 111.0}
    rows[0]["per48"] = {**rows[0]["per48"], "points": 111.0}


def test_a_changed_ledger_source_prevents_promotion(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.stage(rebuild.rebuild_id, owner="worker-1")
    _correct_one_game_checksum(engine)
    before = _pointers(engine)

    failed = service.promote(rebuild.rebuild_id, owner="worker-1")

    assert failed.state == "failed"
    assert failed.error_code == "stale_publication_family"
    assert _pointers(engine) == before


def _correct_one_game_checksum(engine):
    from app.models.canonical_game_ledger import CanonicalGameLedgerGame

    with engine.begin() as connection:
        connection.execute(
            CanonicalGameLedgerGame.__table__.update()
            .where(CanonicalGameLedgerGame.game_id == "game-3")
            .values(checksum="c" * 64)
        )


def test_a_stale_valid_candidate_is_retained_as_superseded_evidence(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    staged = service.stage(rebuild.rebuild_id, owner="worker-1")
    _correct_one_game_checksum(engine)

    service.promote(rebuild.rebuild_id, owner="worker-1")

    with publications.session() as session:
        candidate = session.get(
            PublicationVersion, staged.staged_season_publication_id
        )
    # The evidence survives, and can never be activated by accident.
    assert candidate is not None
    assert candidate.status == "superseded"


# --- Leases, restarts, and retries -----------------------------------------


def test_a_second_worker_cannot_claim_a_live_lease(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.stage(rebuild.rebuild_id, owner="worker-1")

    with pytest.raises(ControlPlaneError, match="rebuild_lease_held"):
        service.promote(rebuild.rebuild_id, owner="worker-2")


def test_an_expired_lease_is_recoverable_by_the_next_worker(tmp_path):
    clock = [NOW]
    engine = seed_family(tmp_path, clock=lambda: clock[0])
    publications = PublicationService(engine, clock=lambda: clock[0])
    service = _service(engine, clock=lambda: clock[0], lease_seconds=60)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.stage(rebuild.rebuild_id, owner="worker-1")

    clock[0] = NOW + timedelta(minutes=5)
    resumed = service.promote(rebuild.rebuild_id, owner="worker-2")

    assert resumed.state == "succeeded"
    assert resumed.lease_owner is None


def test_a_checksum_identical_retry_does_not_mint_a_second_candidate(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    first = service.stage(rebuild.rebuild_id, owner="worker-1")
    again = service.stage(rebuild.rebuild_id, owner="worker-1")

    assert again.staged_season_publication_id == first.staged_season_publication_id
    assert again.staged_l15_publication_id == first.staged_l15_publication_id
    assert again.attempts >= first.attempts


def test_a_transient_worker_failure_leaves_the_rebuild_retryable(family):
    engine, publications = family
    calls = []

    def flaky(sources):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("database connection dropped")
        return {stream_key: v2_payload() for stream_key in sources.stream_keys}

    service = _service(engine, compose=flaky)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )

    with pytest.raises(TimeoutError):
        service.run(rebuild.rebuild_id, owner="worker-1")
    assert service.status(rebuild.rebuild_id)["state"] in ACTIVE_REBUILD_STATES

    assert service.run(rebuild.rebuild_id, owner="worker-1").state == "succeeded"


# --- Bounded status ---------------------------------------------------------


def test_status_reports_bounded_phases_ids_checksums_and_fingerprints(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )

    queued = service.status(rebuild.rebuild_id)
    assert queued["state"] == "queued"
    assert queued["target_format"] == TRADITIONAL_OPPONENT_TARGET_FORMAT.name
    assert (
        queued["target_fingerprint"]
        == TRADITIONAL_OPPONENT_TARGET_FORMAT.fingerprint
    )
    assert queued["governed_game_count"] == 15
    assert queued["team_count"] == 30

    service.run(rebuild.rebuild_id, owner="worker-1")
    done = service.status(rebuild.rebuild_id)
    assert done["state"] == "succeeded"
    assert done["promoted"]["season"]["publication_id"]
    assert done["promoted"]["l15"]["checksum"]
    assert done["error_code"] is None


def test_status_never_exposes_game_ids_payloads_actors_or_stack_traces(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")

    rendered = repr(service.status(rebuild.rebuild_id))

    assert "game-" not in rendered
    assert "per48" not in rendered
    assert "counts" not in rendered
    assert ACTOR not in rendered
    assert "Traceback" not in rendered


def test_status_of_an_unknown_rebuild_is_a_bounded_refusal(family):
    engine, _publications = family

    with pytest.raises(ControlPlaneError, match="rebuild_not_found"):
        _service(engine).status("no-such-rebuild")


def test_every_observable_phase_belongs_to_the_bounded_vocabulary(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    observed = {service.status(rebuild.rebuild_id)["state"]}
    service.stage(rebuild.rebuild_id, owner="worker-1")
    observed.add(service.status(rebuild.rebuild_id)["state"])
    service.promote(rebuild.rebuild_id, owner="worker-1")
    observed.add(service.status(rebuild.rebuild_id)["state"])

    assert observed <= {
        "queued", "composing", "validating", "promoting", "succeeded", "failed"
    }
    assert {"queued", "validating", "succeeded"} <= observed


# --- Atomic family rollback -------------------------------------------------


def test_family_rollback_moves_both_windows_together(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")
    promoted = _pointers(engine)

    service.rollback(actor=ACTOR, reason="restore the previous format")

    after = _pointers(engine)
    for stream_key in (SEASON_STREAM, L15_STREAM):
        assert after[stream_key][0] != promoted[stream_key][0]
        assert after[stream_key][1] == promoted[stream_key][1] + 1


def test_rollback_refuses_a_target_this_deployment_cannot_read(family, monkeypatch):
    """Strict code must not knowingly activate an unreadable publication."""

    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")
    before = _pointers(engine)
    # Simulate the later strict-v2 deployment: v1 is no longer supported.
    monkeypatch.setattr(
        "app.services.traditional_opponent_rebuild"
        ".SUPPORTED_TRADITIONAL_OPPONENT_FORMATS",
        (TRADITIONAL_OPPONENT_V2,),
    )

    with pytest.raises(ControlPlaneError, match="publication_format_unsupported"):
        service.rollback(actor=ACTOR, reason="restore the previous format")
    assert _pointers(engine) == before


def test_rollback_of_only_one_window_cannot_split_the_family(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")
    before = _pointers(engine)

    with pytest.raises(ControlPlaneError, match="publication_family_coupled"):
        service.rollback(
            actor=ACTOR, reason="restore one window", stream_keys=(SEASON_STREAM,)
        )
    assert _pointers(engine) == before


def test_the_promotion_reuses_the_active_pairs_authority(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")

    for stream_key in (SEASON_STREAM, L15_STREAM):
        version = publications.current(stream_key)
        assert version.manifest_id == "ledger-manifest"
        assert version.event_catalog_publication_id == "event-catalog"


def test_a_rebuild_is_not_a_composition_job_or_a_repair(family):
    """The operation has its own durable identity and lifecycle."""

    engine, publications = family
    from app.models.collection_control import CompositionJob

    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")

    with publications.session() as session:
        assert session.scalars(select(CompositionJob)).all() == []
        rows = session.scalars(select(PublicationRebuild)).all()
    assert len(rows) == 1
    assert rows[0].reason == REASON
    assert rows[0].actor == ACTOR


def test_a_rebuild_never_advances_a_pointer_outside_its_own_family(family):
    engine, publications = family
    with publications.session() as session:
        other = session.scalars(
            select(PublicationPointer).where(
                PublicationPointer.stream_key.notin_(
                    (SEASON_STREAM, L15_STREAM)
                )
            )
        ).all()
        before = {row.stream_key: int(row.fence) for row in other}

    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")

    with publications.session() as session:
        after = {
            row.stream_key: int(row.fence)
            for row in session.scalars(
                select(PublicationPointer).where(
                    PublicationPointer.stream_key.notin_(
                        (SEASON_STREAM, L15_STREAM)
                    )
                )
            ).all()
        }
    assert after == before


def test_a_start_requires_both_windows_to_be_active(tmp_path):
    engine = seed_family(tmp_path)
    publications = PublicationService(engine, clock=lambda: NOW)
    expected = active_expectation(publications)
    with publications.session() as session, session.begin():
        session.get(PublicationPointer, L15_STREAM).active_publication_id = None

    with pytest.raises(ControlPlaneError, match="stale_publication_family"):
        _service(engine).start(actor=ACTOR, reason=REASON, expected=expected)


def test_the_rebuilt_pair_is_read_back_through_the_family_interface(family):
    engine, publications = family
    from app.services.database_first_activation import decode_team_window
    from app.services.traditional_opponent_publications import (
        REBOUND_SPLIT,
        normalize_traditional_opponent_window,
    )
    import json as _json

    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")

    for stream_key in (SEASON_STREAM, L15_STREAM):
        version = publications.current(stream_key)
        window = normalize_traditional_opponent_window(
            decode_team_window(
                _json.loads(version.payload), stream_key=stream_key
            ),
            stream_key=stream_key,
        )
        assert window.format is TRADITIONAL_OPPONENT_V2
        assert window.supports(REBOUND_SPLIT)


def test_a_datetime_is_reported_as_an_isoformat_string(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )

    status = service.status(rebuild.rebuild_id)

    assert isinstance(status["created_at"], str)
    assert datetime.fromisoformat(status["created_at"]).tzinfo is not None


# --- The production composer (#236) ----------------------------------------


def _ledger_sources(games):
    """Everything the composer is allowed to use, taken from the active pair."""

    from app.services.traditional_opponent_rebuild import RebuildSources

    game_ids = tuple(sorted(game.game_id for game in games))
    per_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    return RebuildSources(
        season="2025-26",
        cutoff=CUTOFF,
        as_of=date(2025, 10, 20),
        target_format=TRADITIONAL_OPPONENT_TARGET_FORMAT,
        stream_keys=(SEASON_STREAM, L15_STREAM),
        governed_game_ids=game_ids,
        team_game_ids={SEASON_STREAM: per_team, L15_STREAM: per_team},
        team_ids=frozenset(range(1, 31)),
        source_checksum="0" * 64,
    )


def test_the_production_composer_emits_the_target_format_for_both_windows():
    from tests.services.test_ledger_derivations import _league_games
    from app.services.traditional_opponent_rebuild import compose_from_ledger

    games = _league_games()

    payloads = compose_from_ledger(_ledger_sources(games), games=games)

    assert set(payloads) == {SEASON_STREAM, L15_STREAM}
    for stream_key, payload_rows in payloads.items():
        window = normalize_traditional_opponent_window(
            decode_team_window(payload_rows, stream_key=stream_key),
            stream_key=stream_key,
        )
        assert window.format is TRADITIONAL_OPPONENT_V2
        assert len(window.teams) == 30
        for team in window.teams:
            assert team.per48["rebounds"] == (
                team.per48["offensive_rebounds"]
                + team.per48["defensive_rebounds"]
            )


def test_the_composer_refuses_to_widen_its_own_governed_game_set():
    """Dropping a governed game makes the window incomplete, not smaller."""

    from tests.services.test_ledger_derivations import _league_games
    from app.services.traditional_opponent_rebuild import compose_from_ledger

    games = _league_games()

    with pytest.raises(ControlPlaneError, match="stale_publication_family"):
        compose_from_ledger(_ledger_sources(games), games=games[:-1])


def test_the_composer_without_facts_refuses_rather_than_staging_nothing():
    from tests.services.test_ledger_derivations import _league_games
    from app.services.traditional_opponent_rebuild import compose_from_ledger

    with pytest.raises(ControlPlaneError, match="rebuild_composer_unavailable"):
        compose_from_ledger(_ledger_sources(_league_games()))


# --- Generation fencing on every state write (review finding 2) ------------


def test_an_expired_worker_cannot_overwrite_its_successors_state(family):
    """A revived worker must not revert a rebuild its successor completed."""

    engine, publications = family
    clock = [NOW]
    service = _service(engine, clock=lambda: clock[0], lease_seconds=60)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    successor = _service(engine, clock=lambda: clock[0], lease_seconds=60)

    def slow_compose(sources):
        # While worker-1 composes, its lease expires and worker-2 takes the
        # rebuild all the way to succeeded.
        clock[0] = NOW + timedelta(minutes=5)
        successor.run(rebuild.rebuild_id, owner="worker-2")
        return {stream_key: v2_payload() for stream_key in sources.stream_keys}

    slow = _service(engine, compose=slow_compose, clock=lambda: clock[0],
                    lease_seconds=60)

    # Worker-1 is refused loudly rather than silently reverting the rebuild.
    with pytest.raises(ControlPlaneError, match="rebuild_lease_held"):
        slow.stage(rebuild.rebuild_id, owner="worker-1")

    status = service.status(rebuild.rebuild_id)
    assert status["state"] == "succeeded"
    assert status["promoted"]["season"]["publication_id"]


def test_a_write_from_a_worker_that_lost_the_lease_is_refused(family):
    engine, publications = family
    clock = [NOW]
    service = _service(engine, clock=lambda: clock[0], lease_seconds=60)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.stage(rebuild.rebuild_id, owner="worker-1")
    # The lease expires and a successor claims the still-active rebuild.
    clock[0] = NOW + timedelta(minutes=5)
    service._claim(rebuild.rebuild_id, owner="worker-2", state="promoting")

    with pytest.raises(ControlPlaneError, match="rebuild_lease_held"):
        service._transition(
            rebuild.rebuild_id, owner="worker-1", state="validating"
        )


# --- Coupled-family activation (review finding 3) --------------------------


def test_activating_one_bound_window_cannot_split_the_family(family):
    engine, publications = family

    with pytest.raises(ControlPlaneError, match="publication_family_coupled"):
        publications.activate_stream(
            SEASON_STREAM, reason="promote one window only"
        )


def test_the_sibling_window_is_equally_protected(family):
    engine, publications = family

    with pytest.raises(ControlPlaneError, match="publication_family_coupled"):
        publications.activate_stream(
            L15_STREAM, reason="promote one window only"
        )


def test_an_unbound_family_stream_is_not_refused_as_coupled(tmp_path):
    """Initial binding is the ledger cutover, not a split.

    It still faces every ordinary activation gate; it simply is not refused
    for coupling, because there is no sibling generation to disagree with.
    """

    from sqlalchemy import create_engine
    from app.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'initial.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: NOW)
    publications.register_stream(
        SEASON_STREAM, provider="ledger", owner="railway",
        required_observations=(), publication_strategy="replace", enabled=False,
    )

    with pytest.raises(ControlPlaneError) as refusal:
        publications.activate_stream(
            SEASON_STREAM, reason="initial ledger cutover"
        )

    assert refusal.value.reason != "publication_family_coupled"


# --- Family rollback pair coherence (review finding 4) ---------------------


def test_family_rollback_refuses_a_target_pair_that_is_not_one_generation(
    family,
):
    """Both windows must fall back to one coherent format and authority."""

    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run(rebuild.rebuild_id, owner="worker-1")
    # A correction advances only the Last 15 window, so its rollback target
    # becomes v2 while the Season rollback target is still v1: a mixed pair
    # that never existed together.
    publications.recompose_ledger(
        L15_STREAM, season=SEASON, cutoff=CUTOFF,
        payload=v2_payload(mutate=_bump_points),
        provenance={f"pbp:game-{index}": f"game-{index}" for index in range(15)},
        reason="ledger correction",
    )
    before = _pointers(engine)

    with pytest.raises(ControlPlaneError, match="publication_family_mixed_format"):
        service.rollback(actor=ACTOR, reason="restore the previous format")
    assert _pointers(engine) == before


# --- Promotion revalidation depth (review finding 5) -----------------------


def test_a_staged_candidate_whose_bytes_changed_cannot_promote(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    staged = service.stage(rebuild.rebuild_id, owner="worker-1")
    with publications.session() as session, session.begin():
        candidate = session.get(
            PublicationVersion, staged.staged_season_publication_id
        )
        candidate.checksum = "f" * 64
    before = _pointers(engine)

    failed = service.promote(rebuild.rebuild_id, owner="worker-1")

    assert failed.state == "failed"
    assert _pointers(engine) == before


def test_a_correction_that_rebinds_a_games_source_prevents_promotion(family):
    """The candidate must still rest on the exact accepted evidence."""

    from app.models.canonical_game_ledger import CanonicalGameLedgerGame

    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.stage(rebuild.rebuild_id, owner="worker-1")
    with engine.begin() as connection:
        connection.execute(
            CanonicalGameLedgerGame.__table__.update()
            .where(CanonicalGameLedgerGame.game_id == "game-5")
            .values(source_observation_id="pbp:corrected-game-5")
        )
    before = _pointers(engine)

    failed = service.promote(rebuild.rebuild_id, owner="worker-1")

    assert failed.state == "failed"
    assert failed.error_code == "stale_publication_family"
    assert _pointers(engine) == before


# --- Execution driver (review finding 1) -----------------------------------


def test_a_started_rebuild_is_driven_to_completion_by_the_worker_pass(family):
    """Starting records intent; the driver is what makes it progress."""

    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    before = _pointers(engine)

    driven = service.run_pending(owner="worker-1")

    assert [row.rebuild_id for row in driven] == [rebuild.rebuild_id]
    assert service.status(rebuild.rebuild_id)["state"] == "succeeded"
    after = _pointers(engine)
    for stream_key in (SEASON_STREAM, L15_STREAM):
        assert after[stream_key][1] == before[stream_key][1] + 1


def test_a_worker_pass_with_nothing_queued_does_nothing(family):
    engine, _publications = family

    assert _service(engine).run_pending(owner="worker-1") == ()


def test_a_completed_rebuild_is_not_picked_up_again(family):
    engine, publications = family
    service = _service(engine)
    service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.run_pending(owner="worker-1")

    assert service.claimable() == ()
    assert service.run_pending(owner="worker-1") == ()


def test_a_rebuild_abandoned_mid_phase_resumes_on_the_next_pass(tmp_path):
    """A worker that died leaves a row whose lease simply runs out."""

    clock = [NOW]
    engine = seed_family(tmp_path, clock=lambda: clock[0])
    publications = PublicationService(engine, clock=lambda: clock[0])
    crashed = _service(
        engine,
        compose=lambda sources: (_ for _ in ()).throw(TimeoutError("worker died")),
        clock=lambda: clock[0],
        lease_seconds=60,
    )
    rebuild = crashed.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    with pytest.raises(TimeoutError):
        crashed.run_pending(owner="worker-1")
    assert crashed.status(rebuild.rebuild_id)["state"] in ACTIVE_REBUILD_STATES

    # The lease expires; a fresh worker resumes the same durable rebuild.
    clock[0] = NOW + timedelta(minutes=5)
    healthy = _service(engine, clock=lambda: clock[0], lease_seconds=60)

    driven = healthy.run_pending(owner="worker-2")

    assert [row.rebuild_id for row in driven] == [rebuild.rebuild_id]
    assert healthy.status(rebuild.rebuild_id)["state"] == "succeeded"


def test_a_rebuild_another_worker_holds_is_not_contended_for(family):
    engine, publications = family
    service = _service(engine)
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )
    service.stage(rebuild.rebuild_id, owner="worker-1")

    assert service.claimable() == ()
    assert service.run_pending(owner="worker-2") == ()


def test_the_executable_adapter_drives_and_reports_one_rebuild(tmp_path, capsys):
    """The operator command is a thin adapter over the same service."""

    import json as _json

    from scripts.publication_rebuild import main

    engine = seed_family(tmp_path)
    publications = PublicationService(engine, clock=lambda: NOW)
    database_url = str(engine.url)
    service = TraditionalOpponentRebuildService(
        engine,
        publication_service=publications,
        compose=_v2_composer(),
        clock=lambda: NOW,
    )
    rebuild = service.start(
        actor=ACTOR, reason=REASON, expected=active_expectation(publications)
    )

    status_code = main(["--database-url", database_url, "--status", rebuild.rebuild_id])
    reported = _json.loads(capsys.readouterr().out)

    assert status_code == 0
    assert reported["rebuild_id"] == rebuild.rebuild_id
    assert reported["state"] == "queued"

    missing = main(["--database-url", database_url, "--status", "no-such-id"])
    assert missing == 3
    assert _json.loads(capsys.readouterr().out)["error_code"] == "rebuild_not_found"
