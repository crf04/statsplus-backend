"""Season Rankings for every game-log Team Filter, read from publications."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.migrations import run_migrations
from app.models.catalogs import SUPPORTED_TEAM_FILTERS
from app.services.collection_control import PublicationService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    PublicationRead,
    PublicationTeamWindowRow,
)
from app.services.ledger_derivations import TEAM_METRICS
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


def _service(reads):
    return TeamFilterRankingService(_StubReader(reads))


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
        return {"Transition_PTS": points, "Transition_POSS": possessions}

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
    """Zero possessions is real data: that team has no rate, the rest do."""

    service = _service(
        _play_type_reads({"LAL": (22.0, 0.0), "GSW": (30.0, 30.0)})
    )

    ranked = service.ranked_teams("Transition", SEASON)

    assert "LAL" not in ranked
    assert ranked[0] == "GSW"
    assert len(ranked) == len(NBA_TEAM_ID_TO_TRICODE) - 1


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

    def payload_row(team_id, tricode):
        metrics = {metric: 1.0 for metric in TEAM_METRICS}
        metrics["points"] = points.get(tricode, 50.0)
        return {
            "team_id": team_id,
            "team_tricode": tricode,
            "game_ids": ["0022500001"],
            "game_count": 1,
            "per48": metrics,
            "counts": {metric: 1.0 for metric in TEAM_METRICS},
            "league_average": metrics,
            "population_sigma": {metric: 0.5 for metric in TEAM_METRICS},
            "competition_rank": {metric: 1 for metric in TEAM_METRICS},
            "team_minutes": 240.0,
        }

    published = (
        NBA_TEAM_ID_TO_TRICODE.items()
        if tricodes is None
        else [(TRICODE_TO_TEAM_ID[tricode], tricode) for tricode in tricodes]
    )
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
