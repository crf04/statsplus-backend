"""Season Rankings for every game-log Team Filter, read from publications."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

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

    def read(self, stream_key, *, season=None):
        self.calls.append((stream_key, season))
        if stream_key in self._reads:
            return self._reads[stream_key]
        return PublicationRead(
            stream_key=stream_key,
            publication_id=None,
            season=season,
            cutoff=None,
            version=None,
            status="missing",
            freshness="missing",
            age_seconds=None,
            payload=None,
        )


def _service(reads):
    return TeamFilterRankingService(_StubReader(reads), season=SEASON)


# --- catalog ---------------------------------------------------------------


def test_every_supported_team_filter_has_one_ranking_definition():
    assert set(TEAM_FILTER_RANKINGS) == set(SUPPORTED_TEAM_FILTERS)


def test_an_unsupported_team_filter_is_rejected():
    with pytest.raises(ValueError, match="Unsupported team filter"):
        _service({}).ranked_teams("NOT_A_FILTER")


# --- ranking per publication base ------------------------------------------


def _traditional_reads():
    def per48(points, blocks, steals):
        values = {metric: 1.0 for metric in TEAM_METRICS}
        values.update(points=points, blocks=blocks, steals=steals)
        return values

    return {
        "traditional_opponent_season": _read(
            "traditional_opponent_season",
            (
                _row(1610612747, "LAL", per48(110.0, 4.0, 6.0)),
                _row(1610612744, "GSW", per48(120.0, 9.0, 3.0)),
                _row(1610612738, "BOS", per48(100.0, 2.0, 2.0)),
            ),
        )
    }


def test_traditional_filter_ranks_the_most_allowed_opponent_first():
    assert _service(_traditional_reads()).ranked_teams("OPP_PTS") == [
        "GSW",
        "LAL",
        "BOS",
    ]


def test_stocks_filter_sums_the_two_published_primitives():
    # GSW 9+3=12, LAL 4+6=10, BOS 2+2=4.
    assert _service(_traditional_reads()).ranked_teams("OPP_STOCKS") == [
        "GSW",
        "LAL",
        "BOS",
    ]


def _shot_type_rows(values):
    """Build shot-type rows from ``{tricode: (fg2m, fg3m)}`` pairs."""

    return tuple(
        _row(
            1610612737 + index,
            tricode,
            {
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
            },
        )
        for index, (tricode, (fg2m, fg3m)) in enumerate(values.items())
    )


def test_catch_and_shoot_points_are_derived_from_the_made_shot_counts():
    reads = {
        "grouped_shot_types_opponent_season": _read(
            "grouped_shot_types_opponent_season",
            # LAL 2*2+3*8=28 beats GSW 2*10+3*2=26 on points while GSW leads
            # on made twos, so the derived column is not a relabelled count.
            _shot_type_rows({"LAL": (2.0, 8.0), "GSW": (10.0, 2.0)}),
        )
    }
    service = _service(reads)

    assert service.ranked_teams("C&S PTS") == ["LAL", "GSW"]
    assert service.ranked_teams("PU 2s") == ["GSW", "LAL"]
    assert service.ranked_teams("Less Than 10 ft") == ["GSW", "LAL"]


def test_play_type_filter_ranks_points_per_possession():
    rows = (
        _row(
            1610612747,
            "LAL",
            {"Transition_PTS": 22.0, "Transition_POSS": 20.0},
        ),
        _row(
            1610612744,
            "GSW",
            {"Transition_PTS": 30.0, "Transition_POSS": 30.0},
        ),
    )
    reads = {
        "synergy_play_types_opponent_season": _read(
            "synergy_play_types_opponent_season", rows
        )
    }

    # GSW allows more transition points, LAL allows more per possession.
    assert _service(reads).ranked_teams("Transition") == ["LAL", "GSW"]


def test_a_play_type_without_possessions_is_not_ranked():
    rows = (
        _row(1610612747, "LAL", {"Transition_PTS": 22.0, "Transition_POSS": 0.0}),
        _row(1610612744, "GSW", {"Transition_PTS": 30.0, "Transition_POSS": 30.0}),
    )
    reads = {
        "synergy_play_types_opponent_season": _read(
            "synergy_play_types_opponent_season", rows
        )
    }

    assert _service(reads).ranked_teams("Transition") == ["GSW"]


def test_assist_location_filter_ranks_the_published_location_counter():
    rows = (
        _row(1610612747, "LAL", {"two_point_assists": 12.0}),
        _row(1610612744, "GSW", {"two_point_assists": 18.0}),
        _row(1610612738, "BOS", {"two_point_assists": 15.0}),
    )
    reads = {
        "assist_locations_season": _read("assist_locations_season", rows)
    }

    assert _service(reads).ranked_teams("TwoPtAssists") == ["GSW", "BOS", "LAL"]


def test_every_filter_reads_only_its_own_season_stream():
    service = _service({})
    reader = service.publication_reader

    for team_filter in SUPPORTED_TEAM_FILTERS:
        service.ranked_teams(team_filter)

    assert {season for _stream, season in reader.calls} == {SEASON}
    assert {stream for stream, _season in reader.calls} == {
        "traditional_opponent_season",
        "assist_locations_season",
        "grouped_shot_types_opponent_season",
        "synergy_play_types_opponent_season",
    }


# --- last-good and unavailable reads ---------------------------------------


def test_a_stale_publication_still_serves_its_last_good_ranking():
    reads = {
        "traditional_opponent_season": _read(
            "traditional_opponent_season",
            _traditional_reads()["traditional_opponent_season"].decoded,
            freshness="stale",
        )
    }

    assert _service(reads).ranked_teams("OPP_PTS") == ["GSW", "LAL", "BOS"]


def test_an_unavailable_publication_ranks_nothing():
    assert _service({}).ranked_teams("OPP_PTS") == []


def test_no_publication_reader_ranks_nothing():
    service = TeamFilterRankingService(None, season=SEASON)

    assert service.ranked_teams("OPP_PTS") == []


# --- one real publication generation ---------------------------------------


def _publish_traditional(tmp_path, *, now):
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

    def payload_row(team_id, tricode, points):
        metrics = {metric: 1.0 for metric in TEAM_METRICS}
        metrics["points"] = points
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

    publications.compose(
        "traditional_opponent_season",
        season=SEASON,
        cutoff=RETRIEVED_AT,
        payload={
            "rows": [
                payload_row(1610612747, "LAL", 110.0),
                payload_row(1610612744, "GSW", 120.0),
                payload_row(1610612738, "BOS", 100.0),
            ]
        },
    )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: now)
    return TeamFilterRankingService(reader, season=SEASON)


def test_a_real_active_publication_ranks_its_teams(tmp_path):
    service = _publish_traditional(tmp_path, now=RETRIEVED_AT)

    assert service.ranked_teams("OPP_PTS") == ["GSW", "LAL", "BOS"]


def test_a_refresh_that_never_landed_serves_the_last_good_ranking(tmp_path):
    """No newer publication arrived for days; the pointer still ranks."""

    service = _publish_traditional(
        tmp_path, now=RETRIEVED_AT + timedelta(days=9)
    )
    read = service.publication_reader.read(
        "traditional_opponent_season", season=SEASON
    )

    assert read.freshness == "stale"
    assert service.ranked_teams("OPP_PTS") == ["GSW", "LAL", "BOS"]
