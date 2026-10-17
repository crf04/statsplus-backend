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
from sqlalchemy import select

from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CompositionJob,
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


@pytest.fixture
def season(tmp_path):
    engine, _repository_ = _repository(tmp_path)
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
    yield engine, repository, runtime, game, clock, len(games)
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


def test_the_requeue_refuses_without_an_active_manifest(season):
    engine, repository, _runtime, game, clock, _count = season
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.update().values(status="superseded"))

    with pytest.raises(LedgerMaterializationUnavailable):
        requeue_ledger_streams(repository, game.season, (STREAM,), clock=clock)
