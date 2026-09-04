"""Season Rankings for every game-log Team Filter, read from publications."""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sqlalchemy_event

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import (
    NBA_PUBLICATION_STREAMS,
    NBA_PUBLICATION_TAXONOMY,
)
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    PublicationPointer,
    PublicationVersion,
)
from app.migrations import run_migrations
from app.models.catalogs import SUPPORTED_TEAM_FILTERS
from app.services.collection_control import PublicationService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    PublicationRead,
    PublicationTeamWindowRow,
)
from app.services.ledger_derivations import (
    ASSIST_DERIVED_METRICS,
    TEAM_METRICS,
)
from app.services.team_filter_rankings import (
    TEAM_FILTER_RANKINGS,
    TeamFilterRankingService,
)

SEASON = "2025-26"
RETRIEVED_AT = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
TRICODE_TO_TEAM_ID = {
    tricode: team_id for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
}


def _row(team_id, tricode, per48):
    return PublicationTeamWindowRow(
        team_id=team_id,
        team_tricode=tricode,
        game_ids=("0022500001",),
        game_count=1,
        per48=per48,
        league_average={},
        population_sigma={},
        competition_rank={},
    )


def _league(per48_for):
    """Build the canonical thirty rows from one per-team metric builder."""

    return tuple(
        _row(team_id, tricode, per48_for(tricode))
        for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
    )


def _read(stream_key, rows, *, freshness="fresh", status="active"):
    return PublicationRead(
        stream_key=stream_key,
        publication_id="publication-1",
        season=SEASON,
        cutoff=RETRIEVED_AT.isoformat(),
        version=1,
        status=status,
        freshness=freshness,
        age_seconds=0,
        payload={"rows": []},
        retrieved_at=RETRIEVED_AT,
        decoded=tuple(rows),
    )


class _StubReader:
    """One publication generation, recorded so the read seam stays visible."""

    def __init__(self, reads):
        self._reads = reads
        self.calls = []
        self.snapshots = 0

    def read_many(self, stream_keys, *, season=None):
        keys = tuple(stream_keys)
        self.snapshots += 1
        self.calls.extend((stream_key, season) for stream_key in keys)
        return {
            stream_key: self._reads.get(
                stream_key,
                PublicationRead(
                    stream_key=stream_key,
                    publication_id=None,
                    season=season,
                    cutoff=None,
                    version=None,
                    status="missing",
                    freshness="missing",
                    age_seconds=None,
                    payload=None,
                ),
            )
            for stream_key in keys
        }


class _StubGovernance:
    """The governed per-team game set an NBA publication must match."""

    def __init__(self, game_ids=("0022500001",)):
        self.game_ids = frozenset(game_ids)

    def resolve_team_game_ids(self, season, cutoff, *, window, **kwargs):
        return {
            team_id: self.game_ids for team_id in NBA_TEAM_ID_TO_TRICODE
        }


def _service(reads, governance=None):
    return TeamFilterRankingService(
        _StubReader(reads),
        governance_resolver=governance or _StubGovernance(),
    )


# --- catalog ---------------------------------------------------------------


def test_every_supported_team_filter_has_one_ranking_definition():
    assert set(TEAM_FILTER_RANKINGS) == set(SUPPORTED_TEAM_FILTERS)


def test_an_unsupported_team_filter_is_rejected():
    with pytest.raises(ValueError, match="Unsupported team filter"):
        _service({}).ranked_teams("NOT_A_FILTER", SEASON)


# --- ranking per publication base ------------------------------------------


def _traditional_reads():
    """LAL/GSW/BOS carry distinct values; every other team is baseline."""

    ranked = {"GSW": (120.0, 9.0, 3.0), "LAL": (110.0, 4.0, 6.0), "BOS": (100.0, 2.0, 2.0)}

    def per48(tricode):
        points, blocks, steals = ranked.get(tricode, (1.0, 0.5, 0.5))
        values = {metric: 1.0 for metric in TEAM_METRICS}
        values.update(points=points, blocks=blocks, steals=steals)
        return values

    return {
        "traditional_opponent_season": _read(
            "traditional_opponent_season", _league(per48)
        )
    }


def test_traditional_filter_ranks_the_most_allowed_opponent_first():
    ranked = _service(_traditional_reads()).ranked_teams("OPP_PTS", SEASON)

    assert len(ranked) == len(NBA_TEAM_ID_TO_TRICODE)
    assert ranked[:3] == ["GSW", "LAL", "BOS"]


def test_stocks_filter_sums_the_two_published_primitives():
    # GSW 9+3=12, LAL 4+6=10, BOS 2+2=4, every other team 1.0.
    ranked = _service(_traditional_reads()).ranked_teams("OPP_STOCKS", SEASON)

    assert ranked[:3] == ["GSW", "LAL", "BOS"]


def _shot_type_reads(values):
    """Build shot-type rows from ``{tricode: (fg2m, fg3m)}`` pairs."""

    def per48(tricode):
        fg2m, fg3m = values.get(tricode, (0.0, 0.0))
        return {
            "catch_and_shoot_FG2M": fg2m,
            "catch_and_shoot_FG3M": fg3m,
            "catch_and_shoot_FG2A": fg2m,
            "catch_and_shoot_FG3A": fg3m,
            "pullups_FG2M": fg2m,
            "pullups_FG3M": fg3m,
            "pullups_FG2A": fg2m,
            "pullups_FG3A": fg3m,
            "less_than_10_ft_FG2M": fg2m,
            "less_than_10_ft_FG3M": 0.0,
            "less_than_10_ft_FG2A": fg2m,
            "less_than_10_ft_FG3A": 0.0,
        }

    return {
        "grouped_shot_types_opponent_season": _read(
            "grouped_shot_types_opponent_season", _league(per48)
        )
    }


def test_catch_and_shoot_points_are_derived_from_the_made_shot_counts():
    # LAL 2*2+3*8=28 beats GSW 2*10+3*2=26 on points while GSW leads on made
    # twos, so the derived column is not a relabelled count.
    service = _service(_shot_type_reads({"LAL": (2.0, 8.0), "GSW": (10.0, 2.0)}))

    assert service.ranked_teams("C&S PTS", SEASON)[:2] == ["LAL", "GSW"]
    assert service.ranked_teams("PU 2s", SEASON)[:2] == ["GSW", "LAL"]
    assert service.ranked_teams("Less Than 10 ft", SEASON)[:2] == ["GSW", "LAL"]


def _play_type_reads(values):
    def per48(tricode):
        points, possessions = values.get(tricode, (10.0, 20.0))
        metrics = {
            key: 1.0 for key in NBA_PUBLICATION_TAXONOMY["play_types"]
        }
        metrics["Transition_PTS"] = points
        metrics["Transition_POSS"] = possessions
        return metrics

    return {
        "synergy_play_types_opponent_season": _read(
            "synergy_play_types_opponent_season", _league(per48)
        )
    }


def test_play_type_filter_ranks_points_per_possession():
    # GSW allows more transition points, LAL allows more per possession.
    service = _service(
        _play_type_reads({"LAL": (22.0, 20.0), "GSW": (30.0, 30.0)})
    )

    assert service.ranked_teams("Transition", SEASON)[:2] == ["LAL", "GSW"]


def test_a_team_that_never_faced_a_play_type_is_not_ranked_by_it():
    """No points across no possessions is real: that team has no rate."""

    service = _service(
        _play_type_reads({"LAL": (0.0, 0.0), "GSW": (30.0, 30.0)})
    )

    ranked = service.ranked_teams("Transition", SEASON)

    assert "LAL" not in ranked
    assert ranked[0] == "GSW"
    assert len(ranked) == len(NBA_TEAM_ID_TO_TRICODE) - 1


def test_points_across_no_possessions_refuses_to_rank():
    """Contradictory evidence is not an absent rate."""

    service = _service(
        _play_type_reads({"LAL": (22.0, 0.0), "GSW": (30.0, 30.0)})
    )

    assert service.ranked_teams("Transition", SEASON) == []


def test_a_derived_column_that_overflows_refuses_to_rank():
    """Operands can each be finite while their weighted sum is not."""

    service = _service(_shot_type_reads({"LAL": (1e308, 1e308)}))

    assert service.ranked_teams("C&S PTS", SEASON) == []


def test_a_non_numeric_published_metric_refuses_to_rank():
    reads = {
        "traditional_opponent_season": _read(
            "traditional_opponent_season",
            _league(
                lambda tricode: {
                    metric: (True if metric == "points" else 1.0)
                    for metric in TEAM_METRICS
                }
            ),
        )
    }

    assert _service(reads).ranked_teams("OPP_PTS", SEASON) == []


def test_a_publication_missing_a_ranked_metric_refuses_to_rank():
    """A taxonomy that got past decode is invalid evidence for the surface."""

    reads = {
        "synergy_play_types_opponent_season": _read(
            "synergy_play_types_opponent_season",
            _league(lambda tricode: {"Transition_PTS": 10.0}),
        )
    }

    assert _service(reads).ranked_teams("Transition", SEASON) == []


def test_assist_location_filter_ranks_the_published_location_counter():
    values = {"GSW": 18.0, "BOS": 15.0, "LAL": 12.0}
    reads = {
        "assist_locations_season": _read(
            "assist_locations_season",
            _league(
                lambda tricode: {"two_point_assists": values.get(tricode, 1.0)}
            ),
        )
    }

    ranked = _service(reads).ranked_teams("TwoPtAssists", SEASON)

    assert ranked[:3] == ["GSW", "BOS", "LAL"]


def test_every_filter_reads_only_its_own_season_stream():
    service = _service({})
    reader = service.publication_reader

    for team_filter in SUPPORTED_TEAM_FILTERS:
        service.ranked_teams(team_filter, SEASON)

    assert {season for _stream, season in reader.calls} == {SEASON}
    assert {stream for stream, _season in reader.calls} == {
        "traditional_opponent_season",
        "assist_locations_season",
        "grouped_shot_types_opponent_season",
        "synergy_play_types_opponent_season",
    }


def test_the_requested_season_is_the_season_that_is_read():
    service = _service({})

    service.ranked_teams("OPP_PTS", "2024-25")

    assert service.publication_reader.calls == [
        ("traditional_opponent_season", "2024-25")
    ]


# --- last-good, partial, and unavailable reads -----------------------------


def test_a_stale_publication_still_serves_its_last_good_ranking():
    reads = {
        "traditional_opponent_season": _read(
            "traditional_opponent_season",
            _traditional_reads()["traditional_opponent_season"].decoded,
            freshness="stale",
        )
    }

    assert _service(reads).ranked_teams("OPP_PTS", SEASON)[:3] == [
        "GSW",
        "LAL",
        "BOS",
    ]


def test_a_publication_missing_teams_refuses_to_rank():
    """A partial league would rank a plausible but wrong top-N."""

    full = _traditional_reads()["traditional_opponent_season"].decoded
    reads = {
        "traditional_opponent_season": _read(
            "traditional_opponent_season", full[:29]
        )
    }

    assert _service(reads).ranked_teams("OPP_PTS", SEASON) == []


def test_a_publication_carrying_an_unknown_team_refuses_to_rank():
    full = _traditional_reads()["traditional_opponent_season"].decoded
    intruder = _row(1610619999, "INT", dict(full[0].per48))
    reads = {
        "traditional_opponent_season": _read(
            "traditional_opponent_season", (*full[:29], intruder)
        )
    }

    assert _service(reads).ranked_teams("OPP_PTS", SEASON) == []


def test_a_publication_mislabelling_a_canonical_team_refuses_to_rank():
    """Canonical IDs with a wrong tricode would rank a name that is not real."""

    full = _traditional_reads()["traditional_opponent_season"].decoded
    mislabelled = _row(full[0].team_id, "XXX", dict(full[0].per48))
    reads = {
        "traditional_opponent_season": _read(
            "traditional_opponent_season", (mislabelled, *full[1:])
        )
    }

    assert _service(reads).ranked_teams("OPP_PTS", SEASON) == []


def test_one_request_reads_each_needed_stream_once():
    """Several filters over one base cost one read, from one generation."""

    service = _service({**_traditional_reads(), **_shot_type_reads({})})
    reader = service.publication_reader

    rankings = service.rank_all(
        ("OPP_PTS", "OPP_REB", "C&S PTS", "OPP_PTS"), SEASON
    )

    assert reader.snapshots == 1
    assert sorted(stream for stream, _season in reader.calls) == [
        "grouped_shot_types_opponent_season",
        "traditional_opponent_season",
    ]
    assert set(rankings) == {"OPP_PTS", "OPP_REB", "C&S PTS"}


def test_an_unavailable_publication_ranks_nothing():
    assert _service({}).ranked_teams("OPP_PTS", SEASON) == []


def test_no_publication_reader_ranks_nothing():
    service = TeamFilterRankingService(None)

    assert service.ranked_teams("OPP_PTS", SEASON) == []


# --- one real publication generation ---------------------------------------


def _publish_traditional(tmp_path, *, now, tricodes=None):
    """Compose one real active ``traditional_opponent_season`` publication."""

    engine = create_engine(f"sqlite:///{tmp_path / 'rankings.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    publications.register_stream(
        "traditional_opponent_season",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
    )
    points = {"GSW": 120.0, "LAL": 110.0, "BOS": 100.0}

    published = (
        list(NBA_TEAM_ID_TO_TRICODE.items())
        if tricodes is None
        else [(TRICODE_TO_TEAM_ID[tricode], tricode) for tricode in tricodes]
    )

    def per48_for(tricode):
        metrics = {metric: 1.0 for metric in TEAM_METRICS}
        metrics["points"] = points.get(tricode, 50.0)
        return metrics

    # The derived blocks are functions of the published per-48 population, and
    # the family module proves it, so the fixture computes them rather than
    # asserting arbitrary values a real composer would never emit.
    per48_by_team = {
        team_id: per48_for(tricode) for team_id, tricode in published
    }
    average = {
        metric: sum(v[metric] for v in per48_by_team.values()) / len(per48_by_team)
        for metric in TEAM_METRICS
    }
    sigma = {
        metric: math.sqrt(
            sum((v[metric] - average[metric]) ** 2 for v in per48_by_team.values())
            / len(per48_by_team)
        )
        for metric in TEAM_METRICS
    }

    def ranks_for(metric):
        ordered = sorted(
            per48_by_team.items(), key=lambda item: (item[1][metric], item[0])
        )
        assigned, previous, rank = {}, None, 1
        for position, (team_id, values) in enumerate(ordered, start=1):
            if previous is None or values[metric] != previous:
                rank = position
            assigned[team_id] = rank
            previous = values[metric]
        return assigned

    rank_by_metric = {metric: ranks_for(metric) for metric in TEAM_METRICS}

    def payload_row(team_id, tricode):
        return {
            "team_id": team_id,
            "team_tricode": tricode,
            "game_ids": ["0022500001"],
            "game_count": 1,
            "per48": per48_by_team[team_id],
            "counts": {metric: 1.0 for metric in TEAM_METRICS},
            "league_average": dict(average),
            "population_sigma": dict(sigma),
            "competition_rank": {
                metric: rank_by_metric[metric][team_id] for metric in TEAM_METRICS
            },
            "team_minutes": 240.0,
        }
    publications.compose(
        "traditional_opponent_season",
        season=SEASON,
        cutoff=RETRIEVED_AT,
        payload={
            "rows": [
                payload_row(team_id, tricode) for team_id, tricode in published
            ]
        },
    )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: now)
    return TeamFilterRankingService(reader)


def test_a_real_active_publication_ranks_the_whole_league(tmp_path):
    service = _publish_traditional(tmp_path, now=RETRIEVED_AT)

    ranked = service.ranked_teams("OPP_PTS", SEASON)

    assert len(ranked) == len(NBA_TEAM_ID_TO_TRICODE)
    assert ranked[:3] == ["GSW", "LAL", "BOS"]


def test_a_real_partial_publication_refuses_to_rank(tmp_path):
    service = _publish_traditional(
        tmp_path, now=RETRIEVED_AT, tricodes=("GSW", "LAL", "BOS")
    )

    assert service.ranked_teams("OPP_PTS", SEASON) == []


def test_a_real_publication_for_another_season_ranks_nothing(tmp_path):
    """A stream has one pointer, so only the published season can rank.

    This is the specified outcome, not an oversight: a request for a season
    with no Season publication ranks nothing rather than borrowing the
    published season's rankings and attributing them to the wrong year.
    """

    service = _publish_traditional(tmp_path, now=RETRIEVED_AT)
    read = service.publication_reader.read(
        "traditional_opponent_season", season="2024-25"
    )

    assert read.unavailable_reason == "publication_season_mismatch"
    assert service.ranked_teams("OPP_PTS", "2024-25") == []


def test_two_bases_rank_from_one_real_database_snapshot(tmp_path):
    """Two filters over two streams issue exactly one publication query."""

    engine, service = _publish_two_streams(tmp_path)
    statements = []

    @sqlalchemy_event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, *args):  # noqa: ARG001
        if "publication_streams" in statement:
            statements.append(statement)

    try:
        rankings = service.rank_all(("OPP_PTS", "TwoPtAssists"), SEASON)
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", record)

    assert len(statements) == 1
    assert rankings["OPP_PTS"][:2] == ["GSW", "LAL"]
    assert rankings["TwoPtAssists"][:2] == ["LAL", "GSW"]


def test_a_team_without_a_rate_is_excluded_from_both_ends(tmp_path):
    """An unrankable team is neither a strongest nor a weakest opponent."""

    service = _service(
        _play_type_reads({"LAL": (0.0, 0.0), "GSW": (30.0, 30.0)})
    )

    ranked = service.ranked_teams("Transition", SEASON)

    assert "LAL" not in ranked[-5:]
    assert "LAL" not in ranked[:5]


def test_a_refresh_that_never_landed_serves_the_last_good_ranking(tmp_path):
    """No newer publication arrived for days; the pointer still ranks."""

    service = _publish_traditional(
        tmp_path, now=RETRIEVED_AT + timedelta(days=9)
    )
    read = service.publication_reader.read(
        "traditional_opponent_season", season=SEASON
    )

    assert read.freshness == "stale"
    assert service.ranked_teams("OPP_PTS", SEASON)[:3] == ["GSW", "LAL", "BOS"]


def _publish_two_streams(tmp_path):
    """Compose one real traditional and one real assist-location publication."""

    engine = create_engine(f"sqlite:///{tmp_path / 'two-streams.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    points = {"GSW": 120.0, "LAL": 110.0}
    assists = {"LAL": 30.0, "GSW": 25.0}

    def rows(metrics, overrides, key):
        # The derived blocks describe the published population, because the
        # traditional-opponent family proves that they do.
        per48 = {
            team_id: {
                **{metric: 1.0 for metric in metrics},
                key: overrides.get(tricode, 5.0),
            }
            for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
        }
        average = {
            metric: sum(v[metric] for v in per48.values()) / len(per48)
            for metric in metrics
        }
        sigma = {
            metric: math.sqrt(
                sum((v[metric] - average[metric]) ** 2 for v in per48.values())
                / len(per48)
            )
            for metric in metrics
        }
        rank_by_metric = {}
        for metric in metrics:
            ordered = sorted(
                per48.items(), key=lambda item: (item[1][metric], item[0])
            )
            assigned, previous, rank = {}, None, 1
            for position, (team_id, values) in enumerate(ordered, start=1):
                if previous is None or values[metric] != previous:
                    rank = position
                assigned[team_id] = rank
                previous = values[metric]
            rank_by_metric[metric] = assigned
        return [
            {
                "team_id": team_id,
                "team_tricode": tricode,
                "game_ids": ["0022500001"],
                "game_count": 1,
                "per48": per48[team_id],
                "counts": {metric: 1.0 for metric in metrics},
                "league_average": dict(average),
                "population_sigma": dict(sigma),
                "competition_rank": {
                    metric: rank_by_metric[metric][team_id] for metric in metrics
                },
                "team_minutes": 240.0,
            }
            for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
        ]

    for stream_key, metrics, overrides, key in (
        ("traditional_opponent_season", TEAM_METRICS, points, "points"),
        (
            "assist_locations_season",
            ASSIST_DERIVED_METRICS,
            assists,
            "two_point_assists",
        ),
    ):
        publications.register_stream(
            stream_key,
            provider="ledger",
            owner="railway",
            required_observations=(),
            publication_strategy="replace",
            enabled=True,
        )
        publications.compose(
            stream_key,
            season=SEASON,
            cutoff=RETRIEVED_AT,
            payload={"rows": rows(metrics, overrides, key)},
        )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: RETRIEVED_AT)
    return engine, TeamFilterRankingService(reader)


# --- one real NBA-owned publication generation -----------------------------

MANIFEST_ID = "team-filter-manifest"
EVENT_CATALOG_ID = "team-filter-event-catalog"


def _publish_nba_streams(tmp_path, values):
    """Seed real active grouped-shot and Synergy Season publications.

    NBA-owned streams carry publication authority, so this exercises the
    registration, taxonomy, decoder, and authority path that a stubbed
    pre-decoded read cannot reach.
    """

    engine = create_engine(f"sqlite:///{tmp_path / 'nba-filters.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    catalog_payload = json.dumps({"events": []}, separators=(",", ":"), sort_keys=True)
    catalog_checksum = hashlib.sha256(catalog_payload.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id=EVENT_CATALOG_ID,
            season=SEASON,
            catalog_type="event",
            cutoff=RETRIEVED_AT,
            version="event-v1",
            checksum=catalog_checksum,
            payload=catalog_payload,
            complete=True,
            published_at=RETRIEVED_AT - timedelta(minutes=1),
            expires_at=None,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=MANIFEST_ID,
            season=SEASON,
            cutoff=RETRIEVED_AT,
            collect_before=RETRIEVED_AT + timedelta(days=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="team-filter-manifest-checksum",
            event_catalog_publication_id=EVENT_CATALOG_ID,
            event_catalog_checksum=catalog_checksum,
            status="active",
            created_at=RETRIEVED_AT,
        ))
    for base, template in NBA_PUBLICATION_STREAMS.items():
        stream_key = template.format(window="season")
        metric_keys = tuple(sorted(NBA_PUBLICATION_TAXONOMY[base]))
        publications.register_stream(
            stream_key,
            provider="nba",
            owner="residential_collector",
            required_observations=(),
            publication_strategy="replace",
            enabled=True,
        )
        payload = {"rows": [
            {
                "team_id": team_id,
                "team_tricode": tricode,
                "game_ids": ["0022500001"],
                "game_count": 1,
                "per48": {
                    key: values.get(base, {}).get(tricode, {}).get(key, 1.0)
                    for key in metric_keys
                },
                "league_average": {key: 1.0 for key in metric_keys},
                "population_sigma": {key: 0.5 for key in metric_keys},
                "competition_rank": {key: 1 for key in metric_keys},
            }
            for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
        ]}
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        publication_id = f"publication-{stream_key}"
        with engine.begin() as connection:
            connection.execute(PublicationVersion.__table__.insert().values(
                publication_id=publication_id,
                stream_key=stream_key,
                season=SEASON,
                cutoff=RETRIEVED_AT,
                version=1,
                status="active",
                checksum=hashlib.sha256(encoded.encode()).hexdigest(),
                payload=encoded,
                manifest_id=MANIFEST_ID,
                event_catalog_publication_id=EVENT_CATALOG_ID,
                event_catalog_checksum=catalog_checksum,
                created_at=RETRIEVED_AT,
                fence=1,
            ))
            connection.execute(PublicationPointer.__table__.insert().values(
                stream_key=stream_key,
                active_publication_id=publication_id,
                previous_publication_id=None,
                fence=1,
                updated_at=RETRIEVED_AT,
            ))
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: RETRIEVED_AT)
    return TeamFilterRankingService(reader)


def test_a_real_nba_publication_without_governed_games_refuses_to_rank(tmp_path):
    """Rows claiming a game the governed authority never held are not evidence."""

    service = _publish_nba_streams(tmp_path, {})

    assert service.ranked_teams("C&S 3s", SEASON) == []
    assert service.ranked_teams("Transition", SEASON) == []


def test_real_grouped_shot_and_synergy_publications_rank(tmp_path):
    service = _publish_nba_streams(tmp_path, {
        "shot_types": {
            "LAL": {"catch_and_shoot_FG3M": 9.0, "catch_and_shoot_FG2M": 2.0},
            "GSW": {"catch_and_shoot_FG3M": 4.0, "catch_and_shoot_FG2M": 3.0},
        },
        # LAL allows fewer transition points than GSW but more per
        # possession, so the real decoder path still ranks a rate.
        "play_types": {
            "LAL": {"Transition_PTS": 22.0, "Transition_POSS": 10.0},
            "GSW": {"Transition_PTS": 30.0, "Transition_POSS": 20.0},
        },
    })

    service.governance_resolver = _StubGovernance()
    shot_ranking = service.ranked_teams("C&S 3s", SEASON)
    play_ranking = service.ranked_teams("Transition", SEASON)

    assert len(shot_ranking) == len(NBA_TEAM_ID_TO_TRICODE)
    assert shot_ranking[:2] == ["LAL", "GSW"]
    assert len(play_ranking) == len(NBA_TEAM_ID_TO_TRICODE)
    assert play_ranking[:2] == ["LAL", "GSW"]


def test_a_failed_refresh_leaves_the_prior_publication_ranking(tmp_path):
    """A refresh that never composes cannot disturb the active pointer."""

    from app.services.collection_control import ControlPlaneError

    engine, service = _publish_two_streams(tmp_path)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)

    with pytest.raises(ControlPlaneError):
        publications.compose(
            "traditional_opponent_season",
            season=SEASON,
            cutoff=RETRIEVED_AT + timedelta(days=1),
            payload={"rows": [{"team_id": 1610612747}]},
        )

    assert service.ranked_teams("OPP_PTS", SEASON)[:2] == ["GSW", "LAL"]


def _governed_league_events():
    """Fifteen Regular Season games pairing the whole canonical league once."""

    teams = list(NBA_TEAM_ID_TO_TRICODE)
    events, by_team = [], {}
    for index in range(0, len(teams), 2):
        home, away = teams[index], teams[index + 1]
        game_id = f"00225000{index // 2:03d}"
        events.append({
            "nba_game_id": game_id,
            "home_team_id": home,
            "away_team_id": away,
            "phase": "Regular Season",
            "status": "Final",
            "status_code": 3,
            "scheduled_at": (RETRIEVED_AT - timedelta(days=1)).isoformat(),
        })
        by_team[home] = frozenset({game_id})
        by_team[away] = frozenset({game_id})
    return events, by_team


def _publish_governed_nba_stream(tmp_path):
    """Seed an NBA publication whose rows match a real Event Catalog."""

    from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader

    engine = create_engine(f"sqlite:///{tmp_path / 'governed.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    events, games_by_team = _governed_league_events()
    catalog_payload = json.dumps(
        {"events": events}, separators=(",", ":"), sort_keys=True
    )
    catalog_checksum = hashlib.sha256(catalog_payload.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id=EVENT_CATALOG_ID,
            season=SEASON,
            catalog_type="event",
            cutoff=RETRIEVED_AT,
            version="event-v1",
            checksum=catalog_checksum,
            payload=catalog_payload,
            complete=True,
            published_at=RETRIEVED_AT - timedelta(minutes=1),
            expires_at=None,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=MANIFEST_ID,
            season=SEASON,
            cutoff=RETRIEVED_AT,
            collect_before=RETRIEVED_AT + timedelta(days=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="team-filter-manifest-checksum",
            event_catalog_publication_id=EVENT_CATALOG_ID,
            event_catalog_checksum=catalog_checksum,
            status="active",
            created_at=RETRIEVED_AT,
        ))
    stream_key = NBA_PUBLICATION_STREAMS["play_types"].format(window="season")
    metric_keys = tuple(sorted(NBA_PUBLICATION_TAXONOMY["play_types"]))
    publications.register_stream(
        stream_key,
        provider="nba",
        owner="residential_collector",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
    )
    allowed = {"LAL": (22.0, 10.0), "GSW": (30.0, 20.0)}
    rows = []
    for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items():
        points, possessions = allowed.get(tricode, (10.0, 20.0))
        per48 = {key: 1.0 for key in metric_keys}
        per48["Transition_PTS"] = points
        per48["Transition_POSS"] = possessions
        rows.append({
            "team_id": team_id,
            "team_tricode": tricode,
            "game_ids": sorted(games_by_team[team_id]),
            "game_count": len(games_by_team[team_id]),
            "per48": per48,
            "league_average": {key: 1.0 for key in metric_keys},
            "population_sigma": {key: 0.5 for key in metric_keys},
            "competition_rank": {key: 1 for key in metric_keys},
        })
    encoded = json.dumps({"rows": rows}, separators=(",", ":"), sort_keys=True)
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id=f"publication-{stream_key}",
            stream_key=stream_key,
            season=SEASON,
            cutoff=RETRIEVED_AT,
            version=1,
            status="active",
            checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            payload=encoded,
            manifest_id=MANIFEST_ID,
            event_catalog_publication_id=EVENT_CATALOG_ID,
            event_catalog_checksum=catalog_checksum,
            created_at=RETRIEVED_AT,
            fence=1,
        ))
        connection.execute(PublicationPointer.__table__.insert().values(
            stream_key=stream_key,
            active_publication_id=f"publication-{stream_key}",
            previous_publication_id=None,
            fence=1,
            updated_at=RETRIEVED_AT,
        ))
    return engine, TeamFilterRankingService(
        DatabaseFirstPublicationReader(engine, clock=lambda: RETRIEVED_AT),
        governance_resolver=ActiveManifestLedgerGovernanceReader(engine),
    )


def test_a_publication_matching_the_real_event_catalog_ranks(tmp_path):
    """The production resolver, not a stub, admits a governed publication."""

    _engine, service = _publish_governed_nba_stream(tmp_path)

    ranked = service.ranked_teams("Transition", SEASON)

    assert len(ranked) == len(NBA_TEAM_ID_TO_TRICODE)
    assert ranked[:2] == ["LAL", "GSW"]


def test_a_publication_claiming_an_ungoverned_game_is_refused(tmp_path):
    """The production resolver rejects rows the Event Catalog never held."""

    engine, service = _publish_governed_nba_stream(tmp_path)
    stream_key = NBA_PUBLICATION_STREAMS["play_types"].format(window="season")
    with engine.begin() as connection:
        row = connection.execute(
            PublicationVersion.__table__.select().where(
                PublicationVersion.stream_key == stream_key
            )
        ).mappings().one()
        payload = json.loads(row["payload"])
        payload["rows"][0]["game_ids"] = ["0022599999"]
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == row["publication_id"]
            ).values(
                payload=encoded,
                checksum=hashlib.sha256(encoded.encode()).hexdigest(),
            )
        )

    assert service.ranked_teams("Transition", SEASON) == []
