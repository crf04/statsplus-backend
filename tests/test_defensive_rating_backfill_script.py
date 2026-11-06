"""The one-off 2025-26 Defensive Rating backfill (crf04/statsplus#447).

Production state is rebuilt through the real control plane: a finished
2025-26 Regular Season, its last manifest, and an active opponent shot-zone
Publication that names it.  The active season is then 2026-27, as in
production.  The backfill runs through its command-line entry point against a
fake NBA provider that answers the collector's own Advanced read.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select

from app.migrations import run_migrations
from app.models.collection_control import (
    CollectionObservation,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.services.collection_control import (
    CollectionControlService,
    CollectionOperationsService,
    PublicationService,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from scripts import defensive_rating_backfill as backfill
from tests.services.test_defensive_rating_publication import L15_STREAM, SEASON_STREAM, _govern
from tests.support.defensive_rating import DEF_RATINGS, advanced_rows

SEASON = "2025-26"
#: The last 2025-26 manifest, cut after the season's final game.
CUTOFF = datetime(2026, 4, 14, 8, 0, tzinfo=UTC)
DAYS = 16
ANCHOR = "exact_shot_zones_opponent_season"


class AdvancedProvider:
    """Answers the collector's Advanced read; LAL rates lower over its Last 15."""

    def __init__(self, *, teams=30):
        self.calls = []
        self.teams = teams

    def fetch_team_advanced_ratings(self, **kwargs):
        self.calls.append(kwargs)
        last_15 = kwargs["last_n_games"] == 15
        ratings = {**DEF_RATINGS, "LAL": 108.2} if last_15 else DEF_RATINGS
        return advanced_rows(ratings, games=15 if last_15 else DAYS)[: self.teams]


def _insert(connection, table, **values):
    connection.execute(table.__table__.insert().values(**values))


@pytest.fixture
def production(tmp_path):
    url = f"sqlite:///{tmp_path / 'production.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    control = CollectionControlService(engine, clock=lambda: CUTOFF)
    manifest = _govern(control, SEASON, CUTOFF, DAYS)
    publications = PublicationService(
        engine, clock=lambda: CUTOFF, l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine)
    )
    publications.register_default_streams()
    operations = CollectionOperationsService(engine, publication_service=publications, clock=lambda: CUTOFF)
    for stream_key in (SEASON_STREAM, L15_STREAM):
        operations.activate_stream(stream_key, actor="operator", reason="enable for first collection")
    with engine.begin() as connection:
        # The Published Season and the opponent shot-zone Publication whose
        # manifest the backfill rides on; the collector's earlier upload under
        # that manifest names its environment.
        for stream_key, publication_id in (
            ("player_game_logs", "game-logs-2025-26"),
            (ANCHOR, "zones-2025-26"),
        ):
            _insert(
                connection,
                PublicationVersion,
                publication_id=publication_id,
                stream_key=stream_key,
                season=SEASON,
                cutoff=CUTOFF,
                version=1,
                status="active",
                checksum="0" * 64,
                payload="{}",
                manifest_id=manifest.manifest_id if stream_key == ANCHOR else None,
                created_at=CUTOFF,
                fence=1,
            )
            _insert(
                connection,
                PublicationPointer,
                stream_key=stream_key,
                active_publication_id=publication_id,
                fence=1,
                updated_at=CUTOFF,
            )
        _insert(
            connection,
            CollectionObservation,
            observation_id=str(uuid.uuid4()),
            client_observation_id="zones-1",
            collector_id="railway-cron",
            manifest_id=manifest.manifest_id,
            environment="production",
            provider="nba",
            observation_type="shot_zones_opponent",
            scope="{}",
            season=SEASON,
            cutoff=CUTOFF,
            schema_version=2,
            checksum="0" * 64,
            payload="{}",
            payload_bytes=2,
            retrieved_at=CUTOFF,
            accepted_at=CUTOFF,
        )
    control.activate_season("2026-27", actor="operator")
    return url, engine, manifest


def _run(url, *arguments, provider):
    return backfill.main(["--database-url", url, *arguments], provider=provider)


def _summary(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _active_rows(engine, stream_key):
    with engine.connect() as connection:
        version = connection.execute(
            select(PublicationVersion)
            .join(
                PublicationPointer,
                PublicationPointer.active_publication_id == PublicationVersion.publication_id,
            )
            .where(PublicationPointer.stream_key == stream_key)
        ).one_or_none()
    if version is None:
        return None, None
    return version, {row["team_tricode"]: row for row in json.loads(version.payload)["rows"]}


def _drtg_observations(engine):
    with engine.connect() as connection:
        return connection.scalar(
            select(func.count())
            .select_from(CollectionObservation)
            .where(CollectionObservation.observation_type == "defensive_rating")
        )


def test_the_backfill_publishes_2025_26_season_and_last_15_def_rating(production, capsys):
    url, engine, manifest = production
    provider = AdvancedProvider()

    assert _run(url, provider=provider) == 0

    assert provider.calls == [
        {"date_to": "04/14/2026", "season": SEASON, "season_type": "Regular Season", "last_n_games": 0},
        {"date_to": "04/14/2026", "season": SEASON, "season_type": "Regular Season", "last_n_games": 15},
    ]
    season, season_rows = _active_rows(engine, SEASON_STREAM)
    l15, l15_rows = _active_rows(engine, L15_STREAM)
    assert (season.season, season.manifest_id, len(season_rows)) == (SEASON, manifest.manifest_id, 30)
    assert (season_rows["LAL"]["per48"], season_rows["LAL"]["game_count"]) == ({"DEF_RATING": 114.8}, 16)
    assert (l15_rows["LAL"]["per48"], l15_rows["LAL"]["game_count"]) == ({"DEF_RATING": 108.2}, 15)
    assert l15.season == SEASON
    summary = _summary(capsys)
    assert summary["streams"][SEASON_STREAM]["published"]["serving"] is True
    assert summary["streams"][L15_STREAM]["published"]["serving"] is True


def test_a_rerun_writes_and_publishes_nothing(production, capsys):
    url, engine, _manifest = production
    assert _run(url, provider=AdvancedProvider()) == 0
    first, _ = _active_rows(engine, SEASON_STREAM)
    capsys.readouterr()

    assert _run(url, provider=AdvancedProvider()) == 0

    summary = _summary(capsys)
    assert summary["streams"][SEASON_STREAM]["new_observations"] == 0
    assert summary["streams"][L15_STREAM]["new_observations"] == 0
    assert _active_rows(engine, SEASON_STREAM)[0].publication_id == first.publication_id
    assert _drtg_observations(engine) == 2


def test_a_dry_run_fetches_and_reports_without_writing(production, capsys):
    url, engine, _manifest = production

    assert _run(url, "--dry-run", provider=AdvancedProvider()) == 0

    summary = _summary(capsys)
    assert summary["streams"][SEASON_STREAM] == {"teams": 30, "games_played": [16], "new_observations": 1}
    assert _drtg_observations(engine) == 0
    assert _active_rows(engine, SEASON_STREAM) == (None, None)


def test_a_partial_nba_response_writes_and_publishes_nothing(production, capsys):
    url, engine, _manifest = production

    assert _run(url, provider=AdvancedProvider(teams=29)) == 2

    assert _summary(capsys)["refused"] == "incomplete_observation:season"
    assert _drtg_observations(engine) == 0
    assert _active_rows(engine, SEASON_STREAM) == (None, None)


def test_disabled_streams_are_refused_before_any_fetch(production, capsys):
    url, engine, _manifest = production
    with engine.begin() as connection:
        connection.execute(
            PublicationStream.__table__.update()
            .where(PublicationStream.__table__.c.stream_key == L15_STREAM)
            .values(enabled=False)
        )
    provider = AdvancedProvider()

    assert _run(url, provider=provider) == 2

    assert _summary(capsys)["refused"] == f"stream_disabled:{L15_STREAM}"
    assert provider.calls == []


def test_without_a_2025_26_anchor_publication_nothing_is_fetched(production, capsys):
    url, engine, _manifest = production
    with engine.begin() as connection:
        connection.execute(
            PublicationPointer.__table__.delete().where(PublicationPointer.__table__.c.stream_key == ANCHOR)
        )
    provider = AdvancedProvider()

    assert _run(url, provider=provider) == 2

    assert _summary(capsys)["refused"] == f"anchor_not_serving_2025_26:{ANCHOR}"
    assert provider.calls == []
