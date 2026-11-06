"""NBA.com Defensive Rating, from the collector's read to every DRTG reader.

crf04/statsplus#447: DRTG is NBA.com's ``DEF_RATING``.  These tests drive the
real control plane: a governed Event Catalog and manifest, the collector's own
normalizer, ingestion, composition, and the database-first reader that the
Team Profile, the Team Filter and Next Opponent read through.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, select

from app.collector.normalizers import normalize_defensive_rating_response
from app.collector.provider import ResidentialScopeExecutor, ScopeWork
from app.config.settings import RuntimeSettings
from app.migrations import run_migrations
from app.models.collection_control import AuditEvent, PublicationPointer, PublicationVersion
from app.models.game_logs import GameLogQuery
from app.services import collection_control as module
from app.services.collection_control import (
    NBA_TEAM_IDS,
    CollectionControlService,
    CollectionOperationsService,
    CollectorTokenService,
    ControlPlaneError,
    ObservationIngestionService,
    PublicationService,
)
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.team_filter_rankings import TeamFilterRankingService
from app.services.team_service import TeamService
from tests.support import publication_stubs
from tests.support.defensive_rating import DEF_RATINGS, TEN_HIGHEST, advanced_rows
from tests.test_residential_collector import _compose_queued_slice

SEASON = "2025-26"
NEXT = "2026-27"
#: Sixteen game days before the cutoff: every team plays once a day, so the
#: Season window is 16 games and the Last 15 its last fifteen.
CUTOFF = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
DAYS = 16
SEASON_STREAM = "defensive_rating_season"
L15_STREAM = "defensive_rating_l15"
DATE_TO = "01/20/2026"


def _round_robin(season, cutoff, days):
    """``days`` slates of fifteen games in which each team plays once."""

    teams = sorted(int(team_id) for team_id in NBA_TEAM_IDS)
    events = []
    for day in range(days):
        rest = teams[1:]
        rotation = [teams[0], *rest[day % 29 :], *rest[: day % 29]]
        for index in range(15):
            events.append(
                {
                    "nba_game_id": f"{season}-{day:02d}-{index:02d}",
                    "home_team_id": rotation[index],
                    "away_team_id": rotation[29 - index],
                    "scheduled_at": (cutoff - timedelta(days=days - day, hours=2)).isoformat(),
                    "phase": "Regular Season",
                    "status": "Final",
                }
            )
    return events


def _govern(control, season, cutoff, days):
    control.activate_season(season, actor="operator")
    events = _round_robin(season, cutoff, days)
    event_request = control.create_bootstrap_request(season, "event", cutoff=cutoff)
    control.publish_catalog(
        event_request.request_id, {"complete_snapshot": True, "events": events}, version=f"event-{season}"
    )
    athlete_request = control.create_bootstrap_request(season, "athlete", cutoff=cutoff)
    control.publish_catalog(
        athlete_request.request_id,
        {
            "complete_snapshot": True,
            "identities": [
                {
                    "player_id": "2544",
                    "team_id": 1610612747,
                    "status": "active",
                    "event_ids": [event["nba_game_id"] for event in events],
                }
            ],
        },
        version=f"athlete-{season}",
    )
    return control.create_manifest(
        season,
        cutoff=cutoff,
        scopes={SEASON_STREAM, L15_STREAM, "canonical_game_ledger"},
        collect_before=cutoff + timedelta(hours=1),
    )


class _WithTraditional:
    """The real reader, plus a ledger traditional Season publication.

    The Traditional category serves only beside its own publication, which
    the ledger composes; that path is not under test here, so its decoded
    rows are seeded and every other stream is read from the database.
    """

    def __init__(self, reader):
        self.reader = reader

    def read_many(self, stream_keys, *, season=None):
        keys = tuple(stream_keys)
        reads = self.reader.read_many(
            tuple(key for key in keys if key != "traditional_opponent_season"), season=season
        )
        if "traditional_opponent_season" in keys:
            reads["traditional_opponent_season"] = publication_stubs.read(
                "traditional_opponent_season",
                publication_stubs.league(lambda tricode: publication_stubs.traditional_per48()),
            )
        return reads


class Plane:
    """One governed control plane and the collector that uploads to it."""

    def __init__(self, tmp_path):
        self.engine = create_engine(f"sqlite:///{tmp_path / 'drtg.sqlite3'}")
        run_migrations(self.engine)
        self.control = CollectionControlService(self.engine, clock=lambda: CUTOFF)
        self.manifest = _govern(self.control, SEASON, CUTOFF, DAYS)
        self.governance = ActiveManifestLedgerGovernanceReader(self.engine)
        self.publications = PublicationService(
            self.engine, clock=lambda: CUTOFF, l15_expectation_resolver=self.governance
        )
        self.publications.register_default_streams()
        operations = CollectionOperationsService(
            self.engine, publication_service=self.publications, clock=lambda: CUTOFF
        )
        for stream_key in (SEASON_STREAM, L15_STREAM):
            operations.activate_stream(stream_key, actor="operator", reason="first DRTG collection")
        tokens = CollectorTokenService(
            self.engine, environment="testing", signing_secret="test", clock=lambda: CUTOFF
        )
        identity = tokens.create_identity(
            "collector",
            scopes=["ingest"],
            owner="residential_collector",
            providers=["nba"],
            surfaces=["defensive_rating"],
        )
        self.claims = tokens.validate(tokens.issue_for_secret(identity["identity_id"], identity["secret"]))
        ticks = iter(range(1, 10_000))
        self.ingestion = ObservationIngestionService(
            self.engine,
            publication_service=self.publications,
            clock=lambda: CUTOFF + timedelta(seconds=next(ticks)),
        )

    def upload(self, rows, *, window="season", manifest=None, season=SEASON, cutoff=CUTOFF, name="1"):
        manifest = manifest or self.manifest
        observation = normalize_defensive_rating_response(
            rows,
            season=season,
            cutoff=cutoff,
            window=window,
            endpoint_window={
                "last_n_games": 15 if window == "l15" else 0,
                "date_from": None,
                "date_to": cutoff.strftime("%m/%d/%Y"),
            },
        )
        raw = json.dumps(dict(observation.payload), sort_keys=True, separators=(",", ":")).encode()
        return self.ingestion.ingest(
            self.claims,
            {
                "client_observation_id": f"drtg-{season}-{window}-{name}",
                "observation_type": observation.observation_type,
                "provider": "nba",
                "season": season,
                "cutoff": cutoff.isoformat(),
                "schema_version": 2,
                "retrieved_at": cutoff.isoformat(),
                "manifest_id": manifest.manifest_id,
                "scope": observation.scope,
                "environment": "testing",
                "checksum": hashlib.sha256(raw).hexdigest(),
            },
            gzip.compress(raw),
            compressed=True,
        )

    def compose(self, stream_key, *, season=SEASON, cutoff=CUTOFF, manifest=None):
        return self.publications.compose_from_observations(
            stream_key,
            season=season,
            cutoff=cutoff,
            manifest_id=(manifest or self.manifest).manifest_id,
        )

    def active(self, stream_key):
        with self.engine.connect() as connection:
            return connection.execute(
                select(PublicationVersion)
                .join(
                    PublicationPointer,
                    PublicationPointer.active_publication_id == PublicationVersion.publication_id,
                )
                .where(PublicationPointer.stream_key == stream_key)
            ).one_or_none()

    def rankings(self):
        return TeamFilterRankingService(
            _WithTraditional(DatabaseFirstPublicationReader(self.engine)), governance_resolver=self.governance
        )

    def team_service(self):
        return TeamService(
            settings=RuntimeSettings(environment="testing", nba={"current_season": SEASON}),
            season_publications=self.rankings(),
        )


@pytest.fixture
def plane(tmp_path):
    return Plane(tmp_path)


def _ratings(**overrides):
    return {**DEF_RATINGS, **overrides}


def test_a_collected_def_rating_serves_the_profile_value_and_rank(plane):
    plane.upload(advanced_rows(games=DAYS))

    assert _compose_queued_slice(plane.engine, SEASON) >= 1

    payload = json.loads(plane.active(SEASON_STREAM).payload)
    assert len(payload["rows"]) == 30
    lakers = next(row for row in payload["rows"] if row["team_tricode"] == "LAL")
    assert lakers["per48"] == {"DEF_RATING": 114.8}
    assert lakers["game_count"] == 16
    stats = plane.team_service().get_team_stats("Traditional", "Los Angeles Lakers")
    assert (stats["DRTG"], stats["DRTG_RANK"]) == (114.8, 18)
    assert stats["DRTG_vs_avg_pct"] == pytest.approx((114.8 / (3412.6 / 30) - 1) * 100)
    jazz = plane.team_service().get_team_stats("Traditional", "Utah Jazz")
    assert (jazz["DRTG"], jazz["DRTG_RANK"]) == (120.4, 30)


def test_the_drtg_team_filter_keeps_only_games_against_the_ten_highest(plane, monkeypatch):
    from app.services import game_service as game_service_module

    plane.upload(advanced_rows(games=DAYS))
    plane.compose(SEASON_STREAM)
    monkeypatch.setattr(game_service_module, "get_redis_client", lambda *args, **kwargs: None)
    games = game_service_module.GameService(plane.engine, team_filter_rankings=plane.rankings())

    top_ten = games.filter_teams("DRTG", 10, SEASON)
    logs = pd.DataFrame(
        [
            {"GAME_ID": "1", "MATCHUP": "LAL vs. UTA", "MIN": 30},
            {"GAME_ID": "2", "MATCHUP": "LAL @ PHI", "MIN": 30},
            {"GAME_ID": "3", "MATCHUP": "LAL vs. ATL", "MIN": 30},
            {"GAME_ID": "4", "MATCHUP": "LAL @ OKC", "MIN": 30},
        ]
    )
    kept = games.apply_filters(
        logs,
        GameLogQuery(season_filter=SEASON, teams_against=["DRTG"], rank_filter=["10"]),
        teams_against=set(top_ten),
    )

    assert top_ten == TEN_HIGHEST
    assert list(kept["GAME_ID"]) == ["1", "2"]


def test_the_l15_window_binds_last_n_games_15_to_the_governed_last_fifteen(plane):
    plane.upload(advanced_rows(_ratings(LAL=101.0), games=15), window="l15")

    plane.compose(L15_STREAM)

    rows = json.loads(plane.active(L15_STREAM).payload)["rows"]
    lakers = next(row for row in rows if row["team_tricode"] == "LAL")
    assert (lakers["per48"], lakers["game_count"]) == ({"DEF_RATING": 101.0}, 15)


def test_an_l15_read_whose_games_are_not_the_last_fifteen_publishes_nothing(plane):
    plane.upload(advanced_rows(games=DAYS), window="l15")

    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        plane.compose(L15_STREAM)
    assert plane.active(L15_STREAM) is None


@pytest.mark.parametrize("response", ["partial", "empty"])
def test_a_refused_collection_publishes_nothing_and_the_previous_drtg_serves(plane, response):
    plane.upload(advanced_rows(games=DAYS))
    good = plane.compose(SEASON_STREAM)
    partial = advanced_rows(_ratings(LAL=130.0), games=DAYS)[:29] if response == "partial" else []

    # The collector uploads nothing for an incomplete league.  A hand-built
    # upload of one is refused at ingestion (empty) or composition (partial).
    observation = normalize_defensive_rating_response(
        partial,
        season=SEASON,
        cutoff=CUTOFF,
        window="season",
        endpoint_window={"last_n_games": 0, "date_from": None, "date_to": DATE_TO},
    )
    assert observation.complete is False
    if response == "empty":
        with pytest.raises(ControlPlaneError, match="malformed_payload"):
            plane.upload(partial, name="retry")
    else:
        plane.upload(partial, name="retry")
        with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
            plane.compose(SEASON_STREAM)

    assert plane.active(SEASON_STREAM).publication_id == good.publication_id
    stats = plane.team_service().get_team_stats("Traditional", "Los Angeles Lakers")
    assert (stats["DRTG"], stats["DRTG_RANK"]) == (114.8, 18)


def test_a_retried_league_rate_with_wrong_games_played_keeps_the_last_good(plane):
    plane.upload(advanced_rows(games=DAYS))
    good = plane.compose(SEASON_STREAM)
    plane.upload(advanced_rows(_ratings(LAL=130.0), games=DAYS - 1), name="lagging")

    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        plane.compose(SEASON_STREAM)

    assert plane.active(SEASON_STREAM).publication_id == good.publication_id


def test_an_unpublished_seasons_candidate_is_held_with_thirty_teams(plane, tmp_path):
    plane.upload(advanced_rows(games=DAYS))
    serving = plane.compose(SEASON_STREAM)
    # 2025-26 is the Published Season: the season of the game-log pointer.
    with plane.engine.begin() as connection:
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
    next_cutoff = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    plane.control = CollectionControlService(plane.engine, clock=lambda: next_cutoff)
    manifest = _govern(plane.control, NEXT, next_cutoff, 2)
    plane.publications = PublicationService(
        plane.engine, clock=lambda: next_cutoff, l15_expectation_resolver=plane.governance
    )
    ticks = iter(range(1, 10_000))
    plane.ingestion = ObservationIngestionService(
        plane.engine,
        publication_service=plane.publications,
        clock=lambda: next_cutoff + timedelta(seconds=next(ticks)),
    )
    plane.upload(
        advanced_rows(_ratings(LAL=104.0), games=2),
        manifest=manifest,
        season=NEXT,
        cutoff=next_cutoff,
    )

    held = plane.compose(SEASON_STREAM, season=NEXT, cutoff=next_cutoff, manifest=manifest)

    assert plane.active(SEASON_STREAM).publication_id == serving.publication_id
    assert (held.season, held.status, held.manifest_id) == (NEXT, "candidate", manifest.manifest_id)
    with plane.engine.connect() as connection:
        audits = connection.execute(
            select(AuditEvent.resource, AuditEvent.reason, AuditEvent.details).where(
                AuditEvent.action == "publication.held"
            )
        ).all()
    assert [
        (resource, reason, json.loads(details)["publication_id"]) for resource, reason, details in audits
    ] == [(SEASON_STREAM, "season_held", held.publication_id)]
    rows = json.loads(held.payload)["rows"]
    assert len(rows) == 30
    assert next(row for row in rows if row["team_tricode"] == "LAL")["per48"] == {"DEF_RATING": 104.0}
    stats = plane.team_service().get_team_stats("Traditional", "Los Angeles Lakers")
    assert stats["DRTG"] == 114.8


def test_the_manifest_issues_one_league_wide_read_per_window():
    descriptors = module._collector_scope_descriptors({SEASON_STREAM, L15_STREAM}, CUTOFF)

    assert descriptors == [
        {
            "scope": "defensive_rating",
            "parameters": {"window": "season", "subject": "team", "date_to": DATE_TO},
        },
        {"scope": "defensive_rating", "parameters": {"window": "l15", "subject": "team", "date_to": DATE_TO}},
    ]
    # Last 15 opens league-wide: no read before every team has fifteen games.
    assert module._collector_scope_descriptors({L15_STREAM}, CUTOFF, l15_date_from_by_team={}) == []


def test_the_collector_reads_last_n_games_through_the_advanced_measure():
    calls = []

    class Provider:
        def fetch_team_advanced_ratings(self, **kwargs):
            calls.append(kwargs)
            return advanced_rows(games=DAYS)

    executor = ResidentialScopeExecutor(Provider(), clock=lambda: CUTOFF)
    observations = [
        observation
        for window in ("season", "l15")
        for observation in executor.execute_scope(
            ScopeWork(
                scope="defensive_rating",
                observation_type="defensive_rating",
                season=SEASON,
                cutoff=CUTOFF.isoformat(),
                instruction_id="instruction",
                manifest_id="manifest",
                parameters={"window": window, "subject": "team", "date_to": DATE_TO},
            ),
            collector_id="collector",
            environment="testing",
            retrieved_at=CUTOFF,
        )
    ]

    assert calls == [
        {"date_to": DATE_TO, "season": SEASON, "season_type": "Regular Season", "last_n_games": 0},
        {"date_to": DATE_TO, "season": SEASON, "season_type": "Regular Season", "last_n_games": 15},
    ]
    assert [observation.complete for observation in observations] == [True, True]
    assert observations[1].scope["endpoint_window"] == {
        "last_n_games": 15,
        "date_from": None,
        "date_to": DATE_TO,
    }


def test_the_provider_requests_advanced_per_game_def_rating(monkeypatch):
    from nba_api.stats import endpoints

    from app.collector.provider import _StandaloneNBAProvider

    requested = {}

    class TeamStats:
        def __init__(self, **parameters):
            requested.update(parameters)

        def get_data_frames(self):
            return [pd.DataFrame(advanced_rows())]

    monkeypatch.setattr(endpoints, "LeagueDashTeamStats", TeamStats)

    frame = _StandaloneNBAProvider().fetch_team_advanced_ratings(
        date_to=DATE_TO, season=SEASON, season_type="Regular Season", last_n_games=15
    )

    assert {key: requested[key] for key in ("measure_type_detailed_defense", "per_mode_detailed")} == {
        "measure_type_detailed_defense": "Advanced",
        "per_mode_detailed": "PerGame",
    }
    assert (requested["last_n_games"], requested["date_to_nullable"], requested["season"]) == (
        "15",
        DATE_TO,
        SEASON,
    )
    assert len(frame) == 30


def test_a_non_numeric_def_rating_is_a_provider_contract_failure():
    from app.collector.contracts import ProviderContractError

    rows = advanced_rows()
    rows[3]["DEF_RATING"] = "n/a"

    with pytest.raises(ProviderContractError):
        normalize_defensive_rating_response(
            rows,
            season=SEASON,
            cutoff=CUTOFF,
            window="season",
            endpoint_window={"last_n_games": 0, "date_from": None, "date_to": DATE_TO},
        )


def test_ingestion_refuses_a_rating_read_outside_its_issued_window(plane):
    observation = normalize_defensive_rating_response(
        advanced_rows(games=DAYS),
        season=SEASON,
        cutoff=CUTOFF,
        window="season",
        endpoint_window={"last_n_games": 0, "date_from": None, "date_to": "01/19/2026"},
    )
    raw = json.dumps(dict(observation.payload), sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(ControlPlaneError, match="manifest_scope_mismatch"):
        plane.ingestion.ingest(
            plane.claims,
            {
                "client_observation_id": "stale-window",
                "observation_type": "defensive_rating",
                "provider": "nba",
                "season": SEASON,
                "cutoff": CUTOFF.isoformat(),
                "schema_version": 2,
                "retrieved_at": CUTOFF.isoformat(),
                "manifest_id": plane.manifest.manifest_id,
                "scope": observation.scope,
                "environment": "testing",
                "checksum": hashlib.sha256(raw).hexdigest(),
            },
            gzip.compress(raw),
            compressed=True,
        )
