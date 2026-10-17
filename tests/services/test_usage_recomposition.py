"""Republishing ``player_game_logs`` after the ``usage_pct`` backfill (#450).

The backfill changes no checksum and accepts no observation, so reconciliation
sees every succeeded job as current.  ``requeue_ledger_streams`` is the explicit
operator step; these tests drive the real queue, worker, materialization and
publication service on a one-game season.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select

from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    PublicationObservation,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    raw_rows_from_facts,
)
from app.services.collection_control import PublicationService
from app.services.ledger_materialization import (
    LedgerCorrectionQueue,
    LedgerMaterializationService,
    LedgerMaterializationUnavailable,
    requeue_ledger_streams,
)
from app.services.ledger_parity import LedgerParityArtifactRepository
from app.services.ledger_runtime import LedgerGovernance, LedgerRuntime
from tests.services.test_ledger_derivations import _league_games
from tests.services.test_correction_propagation import (
    _boundary_observation_values,
    _event_catalog_publication,
)
from tests.services.test_usage_ledger import _null_usage, _repository

STREAM = "player_game_logs"


class _NoParity:
    def read(self, stream_key):
        return ()


TEAM_IDS = frozenset(range(1, 31))
#: Every team's one player carries a distinct, unrounded Usage in the archive.
EXPECTED_USAGE = {1000 + team: team + 0.375 for team in sorted(TEAM_IDS)}


def _league_with_usage():
    games = []
    for game in _league_games():
        facts = tuple(
            replace(player, usage_pct=player.team_id + 0.375)
            for player in game.player_facts
        )
        # Extraction stores the participants sorted by team; the shared league
        # fixture does not, which a database round trip would otherwise expose.
        bare = replace(
            game,
            player_facts=facts,
            participant_ids_by_team=tuple(sorted(game.participant_ids_by_team)),
            checksum=None,
        )
        games.append(
            replace(bare, raw_rows=raw_rows_from_facts(bare)).with_checksum()
        )
    return tuple(games)


def build_season(engine):
    """A migrated database holding one published 30-team season, usage NULL."""

    games = _league_with_usage()
    game = games[0]
    cutoff = max(item.retrieved_at for item in games)
    clock = lambda: cutoff  # noqa: E731
    manifest_id = "usage-manifest"
    expected = frozenset(item.game_id for item in games)
    expected_l15 = {
        team: frozenset(
            item.game_id for item in games if team in {item.home_team_id, item.away_team_id}
        )
        for team in TEAM_IDS
    }

    class Governance:
        def read_for_composition(self, season, governed_cutoff, manifest_id=None):
            return LedgerGovernance(
                season, governed_cutoff, expected, TEAM_IDS, expected_l15,
                manifest_id=manifest_id,
            )

    publications = PublicationService(engine, clock=clock)
    publications.register_default_streams()
    with engine.begin() as connection:
        catalog = _event_catalog_publication(games, cutoff, "usage-event-catalog")
        catalog["season"] = game.season
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id, season=game.season, cutoff=cutoff,
            collect_before=cutoff + timedelta(days=30), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum=manifest_id,
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
            status="active", created_at=cutoff,
        ))
        # Only the player log stream is published in this season.
        connection.execute(
            PublicationStream.__table__.update()
            .where(PublicationStream.stream_key != STREAM)
            .values(required_observations="[]", enabled=False)
        )
        connection.execute(
            PublicationStream.__table__.update()
            .where(PublicationStream.stream_key == STREAM)
            .values(enabled=True)
        )
    repository = CanonicalGameLedgerRepository(
        engine, correction_sink=LedgerCorrectionQueue(clock=clock),
    )
    repository.replace_games_atomic(
        games,
        accepted_observations={
            item.source_observation_id: _boundary_observation_values(
                item, cutoff=cutoff, manifest_id=manifest_id,
            )
            for item in games
        },
    )
    _null_usage(engine)  # stored before the usage_pct backfill existed
    runtime = LedgerRuntime(
        backfill=None,
        repository=repository,
        materialization=LedgerMaterializationService(
            repository,
            parity_repository=LedgerParityArtifactRepository(engine),
            parity_reader=_NoParity(),
            publication_service=publications,
        ),
        governance=Governance(),
        publication_service=publications,
        clock=clock,
    )
    assert runtime.compose_queued(game.season) >= 1
    return engine, repository, runtime, game, clock, len(games)


@pytest.fixture
def season(tmp_path):
    engine, _repository_ = _repository(tmp_path)
    yield build_season(engine)
    engine.dispose()


def _publications(engine):
    with engine.connect() as connection:
        versions = connection.execute(
            select(PublicationVersion.publication_id, PublicationVersion.payload)
            .where(PublicationVersion.stream_key == STREAM)
            .order_by(PublicationVersion.created_at, PublicationVersion.publication_id)
        ).all()
        active = connection.execute(
            select(PublicationPointer.active_publication_id)
            .where(PublicationPointer.stream_key == STREAM)
        ).scalar_one()
    return versions, active


def _usage_in(payload):
    return {
        row["player_id"]: row.get("usage_pct", "absent")
        for row in json.loads(payload)
    }


def _job(engine):
    with engine.connect() as connection:
        return connection.execute(
            select(CompositionJob.__table__).where(CompositionJob.stream_key == STREAM)
        ).mappings().one()


def test_the_published_rows_carry_no_usage_before_the_backfill(season):
    engine, *_ = season
    versions, active = _publications(engine)

    assert len(versions) == 1
    assert versions[0].publication_id == active
    assert _usage_in(versions[0].payload) == {1000 + team: None for team in TEAM_IDS}
    assert _job(engine)["status"] == "succeeded"


def test_backfill_alone_composes_nothing_and_the_requeue_publishes_usage(season):
    engine, repository, runtime, game, clock, game_count = season

    assert repository.backfill_usage(season=game.season).updated == game_count
    assert runtime.compose_queued(game.season) == 0
    assert len(_publications(engine)[0]) == 1

    queued = requeue_ledger_streams(repository, game.season, (STREAM,), clock=clock)

    assert queued == game_count
    assert _job(engine)["status"] == "queued"
    assert runtime.compose_queued(game.season) == 1
    versions, active = _publications(engine)
    assert len(versions) == 2
    new = next(version for version in versions if version.publication_id == active)
    assert _usage_in(new.payload) == EXPECTED_USAGE
    assert _job(engine)["status"] == "succeeded"


def test_a_rerun_of_the_requeue_publishes_nothing_new(season):
    engine, repository, runtime, game, clock, game_count = season
    repository.backfill_usage(season=game.season)
    requeue_ledger_streams(repository, game.season, (STREAM,), clock=clock)
    runtime.compose_queued(game.season)
    first = _publications(engine)

    assert requeue_ledger_streams(
        repository, game.season, (STREAM,), clock=clock
    ) == game_count
    assert runtime.compose_queued(game.season) == 1

    assert _publications(engine) == first


def test_the_requeue_touches_only_the_streams_it_is_given(season):
    engine, repository, _runtime, game, clock, _count = season

    def jobs():
        with engine.connect() as connection:
            return {
                row["stream_key"]: (row["status"], row["generation"])
                for row in connection.execute(select(CompositionJob.__table__)).mappings()
            }

    before = jobs()
    requeue_ledger_streams(repository, game.season, (STREAM,), clock=clock)
    after = jobs()

    assert after[STREAM][0] == "queued" and after[STREAM][1] > before[STREAM][1]
    assert {k: v for k, v in after.items() if k != STREAM} == {
        k: v for k, v in before.items() if k != STREAM
    }


def test_the_requeue_refuses_when_the_games_evidence_spans_two_manifests(season):
    engine, repository, _runtime, game, clock, _count = season
    with engine.begin() as connection:
        connection.execute(
            CollectionObservation.__table__.update()
            .where(CollectionObservation.observation_id == game.source_observation_id)
            .values(manifest_id=None)
        )

    with pytest.raises(LedgerMaterializationUnavailable):
        requeue_ledger_streams(repository, game.season, (STREAM,), clock=clock)


# --- the requeue is isolated to the streams it names -------------------------


def check_other_streams_are_untouched(world, *, clock=None):
    """With another stream enabled, only the requeued stream publishes."""

    engine, repository, runtime, game, world_clock, _count = world
    clock = clock or world_clock
    other = "traditional_opponent_season"
    with engine.begin() as connection:
        connection.execute(
            PublicationStream.__table__.update()
            .where(PublicationStream.stream_key == other)
            .values(enabled=True)
        )

    def state():
        with engine.connect() as connection:
            return (
                sorted(connection.execute(
                    select(PublicationPointer.stream_key)
                ).scalars()),
                connection.execute(
                    select(CompositionJob.status, CompositionJob.generation)
                    .where(CompositionJob.stream_key == other)
                ).all(),
                connection.execute(
                    select(func.count()).select_from(PublicationVersion.__table__)
                    .where(PublicationVersion.stream_key == other)
                ).scalar_one(),
            )

    before = state()
    repository.backfill_usage(season=game.season)
    requeue_ledger_streams(repository, game.season, (STREAM,), clock=clock)

    assert runtime.compose_queued(game.season) == 1

    assert before[0] == [STREAM]
    assert state() == before
    versions, active = _publications(engine)
    assert _usage_in(
        next(v for v in versions if v.publication_id == active).payload
    ) == EXPECTED_USAGE


def test_other_enabled_streams_are_not_published_by_a_player_log_requeue(season):
    check_other_streams_are_untouched(season)


# --- a collection deadline does not block recomposing accepted evidence -------


def check_expired_collection_window(world):
    engine, repository, runtime, game, clock, game_count = world
    repository.backfill_usage(season=game.season)

    queued = requeue_ledger_streams(
        repository, game.season, (STREAM,), clock=lambda: clock() + timedelta(days=31),
    )

    assert queued == game_count
    assert runtime.compose_queued(game.season) == 1
    versions, active = _publications(engine)
    assert _usage_in(
        next(v for v in versions if v.publication_id == active).payload
    ) == EXPECTED_USAGE


def test_an_expired_collection_window_still_allows_the_requeue(season):
    check_expired_collection_window(season)


# --- a correction that lands before the requeue's snapshot --------------------


def check_correction_before_the_snapshot(world):
    """The requeue serializes with corrections before it reads its games."""

    engine, repository, runtime, game, clock, game_count = world
    repository.backfill_usage(season=game.season)
    corrected_usage = 88.875
    injected = []

    @event.listens_for(engine, "before_cursor_execute")
    def correct_first(_connection, _cursor, statement, _parameters, _context, _many):
        # The requeue's first locking statement is the game-row lock on
        # PostgreSQL and the job-row UPDATE on SQLite; a correction that commits
        # and composes just before it is already accepted evidence.
        first_lock = (
            "FOR UPDATE" in statement and "canonical_game_ledger_games" in statement
        ) or statement.startswith("UPDATE composition_jobs")
        if first_lock and not injected:
            injected.append(True)
            stored = repository.get_game(game.game_id)
            bare = replace(
                stored,
                player_facts=tuple(
                    replace(p, usage_pct=corrected_usage) for p in stored.player_facts
                ),
                source_observation_id=stored.source_observation_id + "-correction",
                retrieved_at=clock() + timedelta(seconds=1),
                raw_checksum=None,
                checksum=None,
            )
            corrected = replace(bare, raw_rows=raw_rows_from_facts(bare)).with_checksum()
            repository.replace_games_atomic(
                (corrected,),
                accepted_observations={
                    corrected.source_observation_id: _boundary_observation_values(
                        corrected, cutoff=clock(), manifest_id="usage-manifest",
                    )
                },
            )
            runtime.compose_queued(game.season)

    queued = requeue_ledger_streams(repository, game.season, (STREAM,), clock=clock)

    assert injected == [True]
    assert queued == game_count
    assert runtime.compose_queued(game.season) == 1
    assert _job(engine)["status"] == "succeeded"
    assert _job(engine)["last_error"] is None
    versions, active = _publications(engine)
    rows = json.loads(next(v for v in versions if v.publication_id == active).payload)
    corrected_rows = [r for r in rows if r["game_id"] == game.game_id]
    assert {r["usage_pct"] for r in corrected_rows} == {corrected_usage}
    # The publication cites the correction's source observation for that game,
    # not the superseded one a stale snapshot would have re-queued.
    with engine.connect() as connection:
        cited = set(connection.execute(
            select(PublicationObservation.observation_id)
            .where(PublicationObservation.publication_id == active)
        ).scalars())
    assert game.source_observation_id + "-correction" in cited
    assert game.source_observation_id not in cited


def test_a_correction_before_the_requeue_snapshot_is_not_overwritten_with_stale_lineage(
    season,
):
    check_correction_before_the_snapshot(season)


# --- games observed under a superseded manifest -------------------------------


def check_superseded_governing_manifest(world):
    """After a rollover the games' evidence still belongs to the old manifest.

    A later active manifest whose collection window has closed governs nothing
    the stored games were accepted under; recomposition must follow the games'
    own provenance and leave every provenance row alone.
    """

    engine, repository, runtime, game, clock, game_count = world
    old = "usage-manifest"
    with engine.begin() as connection:
        governing = connection.execute(
            select(CollectionManifest.__table__).where(
                CollectionManifest.manifest_id == old
            )
        ).mappings().one()
        connection.execute(
            CollectionManifest.__table__.update()
            .where(CollectionManifest.manifest_id == old)
            .values(status="superseded")
        )
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="usage-manifest-later", season=game.season,
            cutoff=governing["cutoff"] + timedelta(days=1),
            collect_before=governing["cutoff"] + timedelta(days=2),
            accepted_versions="[1]", scopes='["canonical_game_ledger"]',
            checksum="later", status="active",
            created_at=governing["cutoff"] + timedelta(days=1),
            event_catalog_publication_id=governing["event_catalog_publication_id"],
            event_catalog_checksum=governing["event_catalog_checksum"],
        ))

    def provenance():
        with engine.connect() as connection:
            return connection.execute(
                select(
                    CollectionObservation.observation_id,
                    CollectionObservation.manifest_id,
                    CollectionObservation.cutoff,
                ).order_by(CollectionObservation.observation_id)
            ).all()

    before = provenance()
    check_other_streams_are_untouched(world, clock=lambda: clock() + timedelta(days=31))

    assert provenance() == before
    with engine.connect() as connection:
        manifests = set(connection.execute(
            select(CompositionJob.manifest_id)
        ).scalars())
    assert manifests == {old}


def test_games_observed_under_a_superseded_manifest_are_recomposed_untouched_elsewhere(
    season,
):
    check_superseded_governing_manifest(season)


def test_the_requeue_refuses_when_the_governing_manifest_is_not_a_ledger_manifest(season):
    engine, repository, _runtime, game, clock, _count = season
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.update().values(scopes="[]"))

    with pytest.raises(LedgerMaterializationUnavailable):
        requeue_ledger_streams(repository, game.season, (STREAM,), clock=clock)
