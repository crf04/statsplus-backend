"""The one-off 2025-26 Synergy play-type backfill (crf04/statsplus#416).

Production state is rebuilt through the real control plane: a finished
2025-26 Regular Season, its last manifest, and Synergy Publications composed
from observations collected before the turnover and free-throw rates were
read.  The active season is then 2026-27, as in production.  The backfill
runs through its command-line entry point against a fake NBA provider that
answers the same calls the residential collector makes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select

from app.collector.normalizers import (
    PLAY_TYPES,
    normalize_opponent_synergy_response,
    normalize_synergy_response,
)
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.migrations import run_migrations
from app.models.collection_control import (
    ActiveSeason,
    CollectionManifest,
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationVersion,
)
from app.services.collection_control import (
    NBA_TEAM_IDS,
    CollectionControlService,
    CollectionOperationsService,
    CollectorTokenService,
    ObservationIngestionService,
    PublicationService,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.player_diet import PlayerDietRepository
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import TeamMatchupRepository
from scripts import synergy_play_types_backfill as backfill
from tests.services.test_matchup_service import GAME_ID, LAL, RETRIEVED_AT, _service
from tests.services.test_play_type_turnover_scores import (
    _BOSTON,
    _FULL_DIET,
    _OTHERS,
    MARKETS,
    _DietPublication,
)
from tests.support import publication_stubs
from tests.test_residential_collector import _compose_queued_slice

SEASON = "2025-26"
#: The last 2025-26 manifest, cut after the season's final game.
CUTOFF = datetime(2026, 4, 14, 8, 0, tzinfo=UTC)
TEAM_STREAM = "synergy_play_types_opponent_season"
PLAYER_STREAM = "synergy_play_types"
TEAMMATE = 201939
_NEUTRAL = {"PTS": 0.5, "TOV": 0.5, "FTT": 0.5}


def _team_rows(category, *, with_events):
    """One game of 48 minutes, so each team's totals are its per-48 values."""

    rows = []
    for team_id in sorted(int(value) for value in NBA_TEAM_IDS):
        values = (_BOSTON if NBA_TEAM_ID_TO_TRICODE[team_id] == "BOS" else _OTHERS).get(category, _NEUTRAL)
        row = {"team_id": team_id, "category": category, "GP": 1, "MIN": 48, "POSS": 10, "PTS": values["PTS"]}
        if with_events:
            row["TOV_POSS_PCT"] = values["TOV"] / 10
            row["FT_POSS_PCT"] = values["FTT"] / 10
        rows.append(row)
    return rows


def _player_rows(category, *, with_events):
    """LeBron in his four rated play types, a teammate in all eleven."""

    rows = [
        {"PLAYER_ID": TEAMMATE, "TEAM_ID": LAL, "GP": 20, "POSS": 40, "POSS_PCT": 0.05, "PLAY_TYPE": category}
    ]
    if category in _FULL_DIET:
        share, possessions, _turnovers, _trips = _FULL_DIET[category]
        rows.append(
            {
                "PLAYER_ID": 2544,
                "TEAM_ID": LAL,
                "GP": 20,
                "POSS": possessions,
                "POSS_PCT": share,
                "PLAY_TYPE": category,
            }
        )
    if with_events:
        for row in rows:
            turnovers, trips = _FULL_DIET[category][2:] if row["PLAYER_ID"] == 2544 else (2, 1)
            row["TOV_POSS_PCT"] = turnovers / row["POSS"]
            row["FT_POSS_PCT"] = trips / row["POSS"]
    return rows


class SynergyProvider:
    """Answers the collector's Synergy calls for the finished season."""

    def __init__(self):
        self.calls = []

    def fetch_synergy_play_types(self, category, **kwargs):
        assert kwargs["season"] == SEASON
        assert kwargs["season_type"] == "Regular Season"
        self.calls.append((category, kwargs["player_or_team_abbreviation"]))
        if kwargs["player_or_team_abbreviation"] == "T":
            assert kwargs["type_grouping"] == "Defensive"
            return _team_rows(category, with_events=True)
        assert kwargs["type_grouping"] == "Offensive"
        return _player_rows(category, with_events=True)


@pytest.fixture
def production(tmp_path):
    url = f"sqlite:///{tmp_path / 'production.sqlite3'}"
    engine = create_engine(url)
    run_migrations(engine)
    return build_production(url, engine)


def build_production(url, engine):
    """A finished 2025-26 slate whose Synergy Publications predate the rates."""

    control = CollectionControlService(engine, clock=lambda: CUTOFF)
    control.activate_season(SEASON, actor="operator")
    team_ids = sorted(NBA_TEAM_IDS)
    events = [
        {
            "nba_game_id": f"game-{index}",
            "home_team_id": team_ids[index * 2],
            "away_team_id": team_ids[index * 2 + 1],
            "scheduled_at": (CUTOFF - timedelta(days=2, hours=index)).isoformat(),
            "phase": "Regular Season",
            "status": "Final",
        }
        for index in range(15)
    ]
    event_request = control.create_bootstrap_request(SEASON, "event", cutoff=CUTOFF)
    control.publish_catalog(
        event_request.request_id, {"complete_snapshot": True, "events": events}, version="event-v1"
    )
    athlete_request = control.create_bootstrap_request(SEASON, "athlete", cutoff=CUTOFF)
    control.publish_catalog(
        athlete_request.request_id,
        {
            "complete_snapshot": True,
            "identities": [
                {
                    "player_id": "2544",
                    "team_id": LAL,
                    "status": "active",
                    "event_ids": [event["nba_game_id"] for event in events],
                }
            ],
        },
        version="athlete-v1",
    )
    manifest = control.create_manifest(
        SEASON,
        cutoff=CUTOFF,
        scopes={PLAYER_STREAM, TEAM_STREAM, "canonical_game_ledger"},
        collect_before=CUTOFF + timedelta(hours=1),
    )
    publications = PublicationService(
        engine, clock=lambda: CUTOFF, l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine)
    )
    publications.register_default_streams()
    operations = CollectionOperationsService(engine, publication_service=publications, clock=lambda: CUTOFF)
    for stream_key in (PLAYER_STREAM, TEAM_STREAM):
        operations.activate_stream(stream_key, actor="operator", reason="2025-26 collection")
    tokens = CollectorTokenService(engine, environment="testing", signing_secret="test", clock=lambda: CUTOFF)
    identity = tokens.create_identity(
        "collector",
        scopes=["ingest"],
        owner="residential_collector",
        providers=["nba"],
        surfaces=["synergy_play_types", "synergy_opponent"],
    )
    claims = tokens.validate(tokens.issue_for_secret(identity["identity_id"], identity["secret"]))
    ticks = iter(range(1, 10_000))
    ingestion = ObservationIngestionService(
        engine, publication_service=publications, clock=lambda: CUTOFF + timedelta(seconds=next(ticks))
    )
    for category in PLAY_TYPES:
        for observation in (
            normalize_synergy_response(
                _player_rows(category, with_events=False),
                season=SEASON,
                cutoff=CUTOFF,
                scope={
                    "window": "season",
                    "subject": "player",
                    "phase": "Regular Season",
                    "play_type": category,
                },
            ),
            normalize_opponent_synergy_response(
                _team_rows(category, with_events=False),
                season=SEASON,
                cutoff=CUTOFF,
                scope={
                    "window": "season",
                    "phase": "Regular Season",
                    "play_type": category,
                    "subject": "opponent",
                    "value_mode": "totals_with_minutes",
                },
            ),
        ):
            raw = json.dumps(dict(observation.payload), sort_keys=True, separators=(",", ":")).encode()
            ingestion.ingest(
                claims,
                {
                    "client_observation_id": f"{observation.observation_type}-{category}",
                    "observation_type": observation.observation_type,
                    "provider": "nba",
                    "season": SEASON,
                    "cutoff": CUTOFF.isoformat(),
                    "schema_version": 2,
                    "retrieved_at": CUTOFF.isoformat(),
                    "manifest_id": manifest.manifest_id,
                    "scope": observation.scope,
                    "environment": "testing",
                    "checksum": hashlib.sha256(raw).hexdigest(),
                },
                gzip.compress(raw),
                compressed=True,
            )
    assert _compose_queued_slice(engine, SEASON) == 2
    # Production serves 2025-26 as the Published Season while collection
    # follows 2026-27.
    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.insert().values(
                publication_id="game-logs-2025-26",
                stream_key="player_game_logs",
                season=SEASON,
                cutoff=CUTOFF,
                version=1,
                status="active",
                checksum="0" * 64,
                payload="{}",
                created_at=CUTOFF,
                fence=1,
            )
        )
        connection.execute(
            PublicationPointer.__table__.insert().values(
                stream_key="player_game_logs",
                active_publication_id="game-logs-2025-26",
                fence=1,
                updated_at=CUTOFF,
            )
        )
    control.activate_season("2026-27", actor="operator")
    # Another season's waiting work, which the backfill must leave alone.
    PublicationService(engine).enqueue(
        "player_game_logs", season="2026-27", cutoff=datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
    )
    return url, engine, manifest


def _active(engine, stream_key):
    with engine.connect() as connection:
        return connection.execute(
            select(PublicationVersion)
            .join(
                PublicationPointer,
                PublicationPointer.active_publication_id == PublicationVersion.publication_id,
            )
            .where(PublicationPointer.stream_key == stream_key)
        ).one()


def _season_scores(engine):
    """The Matchup LeBron gets against BOS from the active 2025-26 Publications."""

    team = json.loads(_active(engine, TEAM_STREAM).payload)
    diet = json.loads(_active(engine, PLAYER_STREAM).payload)
    others = {"turnovers": 13.0, "free_throws_attempted": 20.0, "free_throws_made": 16.0}
    boston = {"turnovers": 15.0, "free_throws_attempted": 17.0, "free_throws_made": 13.0}
    reads = {
        TEAM_STREAM: publication_stubs.read(
            TEAM_STREAM,
            [
                publication_stubs.row(row["team_id"], row["team_tricode"], row["per48"])
                for row in team["rows"]
            ],
        ),
        "traditional_opponent_season": publication_stubs.read(
            "traditional_opponent_season",
            publication_stubs.league(
                lambda tricode: publication_stubs.traditional_per48(
                    **(boston if tricode == "BOS" else others)
                )
            ),
        ),
    }
    window_engine = create_engine("sqlite://")
    run_migrations(window_engine)
    window = TeamMatchupQueryService(
        TeamMatchupRepository(window_engine),
        clock=lambda: publication_stubs.RETRIEVED_AT,
        publication_reader=publication_stubs.StubReader(reads),
        l15_expectation_resolver=publication_stubs.StubGovernance(),
    ).get_latest_window(publication_stubs.SEASON, as_of=publication_stubs.RETRIEVED_AT.date())
    diets = PlayerDietRepository(window_engine, publication_reader=_DietPublication(diet["rows"]))
    pool = PlayerPool(
        players=(PoolPlayer(2544, "LeBron James", LAL, MARKETS, {"prizepicks": MARKETS}),),
        team_counts={LAL: 1},
        freshness={"status": "fresh", "retrieved_at": RETRIEVED_AT.isoformat(), "providers": {}},
    )
    payload = _service(pool=pool, season_window=window, diets=diets).get_matchup(game_id=GAME_ID)
    return payload["players"][0]["scores"]


def _state(engine):
    with engine.connect() as connection:
        return {
            "seasons": dict(connection.execute(select(ActiveSeason.season, ActiveSeason.status)).all()),
            "manifests": sorted(
                connection.execute(
                    select(
                        CollectionManifest.manifest_id, CollectionManifest.season, CollectionManifest.status
                    )
                ).all()
            ),
            "observations": connection.scalar(select(func.count()).select_from(CollectionObservation)),
            "versions": connection.scalar(select(func.count()).select_from(PublicationVersion)),
            "jobs": sorted(
                connection.execute(
                    select(
                        CompositionJob.stream_key,
                        CompositionJob.season,
                        CompositionJob.status,
                        CompositionJob.generation,
                    )
                ).all()
            ),
            "pointers": sorted(
                connection.execute(
                    select(
                        PublicationPointer.stream_key,
                        PublicationPointer.active_publication_id,
                        PublicationPointer.fence,
                    )
                ).all()
            ),
        }


def _run(url, *arguments, provider=None):
    return backfill.main(["--database-url", url, *arguments], provider=provider or SynergyProvider())


class ChangedProvider(SynergyProvider):
    """One response the composition could not publish counts from."""

    def __init__(self, defect):
        super().__init__()
        self.defect = defect

    def fetch_synergy_play_types(self, category, **kwargs):
        rows = super().fetch_synergy_play_types(category, **kwargs)
        team = kwargs["player_or_team_abbreviation"] == "T"
        if category == "Cut" and team == (self.defect != "player_rates"):
            for row in rows:
                if self.defect == "team_games":
                    row["GP"] = 2
                else:
                    row.pop("TOV_POSS_PCT")
                    row.pop("FT_POSS_PCT")
        return rows


def test_the_backfill_gives_2025_26_turnovers_and_trips_a_play_types_component(production, capsys):
    url, engine, manifest = production
    before = _state(engine)
    assert "play_types" not in _season_scores(engine)["TOV"]["season"]["components"]

    assert _run(url) == 0

    # The hand computation of crf04/statsplus-backend#684's score test, now
    # from the composed 2025-26 Publications.
    scores = _season_scores(engine)
    assert scores["TOV"]["season"]["components"]["play_types"] == {"value": 0.365804, "thin": False}
    assert scores["TOV"]["season"]["blend"] == {"value": 0.256881, "thin": False}
    assert scores["FTA"]["season"]["components"]["play_types"] == {"value": 0.245803, "thin": False}
    assert scores["FTM"]["season"]["components"]["play_types"] == {"value": 0.245803, "thin": False}
    for stream_key in (TEAM_STREAM, PLAYER_STREAM):
        active = _active(engine, stream_key)
        assert (active.season, active.manifest_id, active.cutoff.replace(tzinfo=UTC)) == (
            SEASON,
            manifest.manifest_id,
            CUTOFF,
        )
    after = _state(engine)
    # Collection authority is untouched: 2026-27 stays the active season and
    # no manifest is issued, superseded or reopened.
    assert after["seasons"] == before["seasons"] == {SEASON: "inactive", "2026-27": "active"}
    assert after["manifests"] == before["manifests"]
    assert after["observations"] == before["observations"] + 22
    assert ("player_game_logs", "2026-27", "queued", 1) in after["jobs"]
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["streams"][TEAM_STREAM]["teams"] == 30
    assert summary["streams"][TEAM_STREAM]["rows_by_play_type"] == {category: 30 for category in PLAY_TYPES}
    assert summary["streams"][PLAYER_STREAM]["players"] == 2
    assert summary["streams"][PLAYER_STREAM]["rows_by_play_type"] == {
        category: 2 if category in _FULL_DIET else 1 for category in PLAY_TYPES
    }
    assert summary["streams"][TEAM_STREAM]["new_observations"] == 11
    assert summary["streams"][PLAYER_STREAM]["new_observations"] == 11


def test_a_rerun_writes_and_publishes_nothing(production, capsys):
    url, engine, _manifest = production
    assert _run(url) == 0
    settled = _state(engine)

    assert _run(url) == 0

    assert _state(engine) == settled
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {stream["new_observations"] for stream in summary["streams"].values()} == {0}


def test_a_dry_run_fetches_and_reports_without_writing(production, capsys):
    url, engine, _manifest = production
    before = _state(engine)
    provider = SynergyProvider()

    assert _run(url, "--dry-run", provider=provider) == 0

    assert _state(engine) == before
    assert sorted(provider.calls) == sorted(
        (category, subject) for category in PLAY_TYPES for subject in ("P", "T")
    )
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["dry_run"] is True
    assert summary["streams"][TEAM_STREAM]["teams"] == 30
    assert summary["streams"][TEAM_STREAM]["new_observations"] == 11
    assert "play_types" not in _season_scores(engine)["TOV"]["season"]["components"]


def test_a_season_whose_catalog_still_has_games_to_play_is_refused_before_any_fetch(production, capsys):
    url, engine, manifest = production
    with engine.begin() as connection:
        catalog_id = connection.scalar(
            select(CollectionManifest.event_catalog_publication_id).where(
                CollectionManifest.manifest_id == manifest.manifest_id
            )
        )
    _add_unplayed_game(engine, catalog_id)
    before = _state(engine)
    provider = SynergyProvider()

    assert _run(url, provider=provider) == 2

    assert provider.calls == []
    assert _state(engine) == before
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["refused"] == "season_not_complete"


def _add_unplayed_game(engine, catalog_id):
    """Rewrite the bound Event Catalog as a mid-season one, checksum included."""

    from app.domain.publication_integrity import publication_payload_checksum
    from app.models.collection_control import CatalogPublication

    with engine.begin() as connection:
        payload = json.loads(
            connection.scalar(
                select(CatalogPublication.payload).where(CatalogPublication.publication_id == catalog_id)
            )
        )
        team_ids = sorted(NBA_TEAM_IDS)
        payload["events"].append(
            {
                "nba_game_id": "game-unplayed",
                "home_team_id": team_ids[0],
                "away_team_id": team_ids[1],
                "scheduled_at": (CUTOFF + timedelta(days=1)).isoformat(),
                "phase": "Regular Season",
                "status": "Scheduled",
            }
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        checksum = publication_payload_checksum(encoded)
        connection.execute(
            CatalogPublication.__table__.update()
            .where(CatalogPublication.publication_id == catalog_id)
            .values(payload=encoded, checksum=checksum)
        )
        connection.execute(
            CollectionManifest.__table__.update()
            .where(CollectionManifest.event_catalog_publication_id == catalog_id)
            .values(event_catalog_checksum=checksum)
        )


def test_a_backfill_after_the_2026_27_cutover_is_refused_before_any_fetch(production, capsys):
    # Once 2026-27 is the Published Season a 2025-26 composition would only be
    # held, never served.
    url, engine, _manifest = production
    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.update()
            .where(PublicationVersion.publication_id == "game-logs-2025-26")
            .values(season="2026-27")
        )
    before = _state(engine)
    provider = SynergyProvider()

    assert _run(url, provider=provider) == 2

    assert provider.calls == []
    assert _state(engine) == before
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["refused"] == "published_season_not_2025_26"


@pytest.mark.parametrize(
    ("defect", "refused"),
    (
        ("team_rates", "team_rates_missing"),
        ("player_rates", "player_rates_missing"),
        ("team_games", "games_played_mismatch"),
    ),
)
def test_a_response_that_cannot_carry_the_counts_is_refused_before_any_write(
    production, capsys, defect, refused
):
    url, engine, _manifest = production
    before = _state(engine)

    assert _run(url, provider=ChangedProvider(defect)) == 2

    assert _state(engine) == before
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["refused"] == refused


def test_a_rerun_recovers_a_composition_that_failed_after_acceptance(production):
    url, engine, _manifest = production
    from sqlalchemy.orm import Session

    def failing_worker(season):
        with Session(engine) as session, session.begin():
            for job in session.scalars(select(CompositionJob).where(CompositionJob.season == season)):
                job.status, job.last_error = "failed", "stale_composition"
        return 0

    status, _summary = backfill.run_backfill(
        engine,
        provider=SynergyProvider(),
        compose_queued=lambda: failing_worker,
        now=datetime.now(UTC),
        dry_run=False,
    )
    assert status == 2
    assert "play_types" not in _season_scores(engine)["TOV"]["season"]["components"]

    assert _run(url) == 0

    assert _season_scores(engine)["TOV"]["season"]["components"]["play_types"] == {
        "value": 0.365804,
        "thin": False,
    }
