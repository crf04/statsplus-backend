"""Every traditional-opponent read seam refuses a retired v1 publication.

The contraction's whole safety claim is that a v1 pair fails closed with one
stable code rather than degrading into partial data.  These prove it at each
consumer boundary, and that the code the owning module chose survives to the
seam's own logs and telemetry instead of being flattened into "malformed
bytes" -- which is a different operational fact and would send an operator
looking for corruption instead of for a code rollback.
"""

import json
import logging

import pytest
from sqlalchemy import create_engine

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.migrations import run_migrations
from app.services.collection_control import PublicationService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    PublicationTeamWindowRow,
)
from app.services.ledger_derivations import TEAM_METRICS
from app.services.matchup_parity import MatchupParityError
from app.services.team_filter_rankings import TeamFilterRankingService
from tests.support.publication_stubs import (
    RETRIEVED_AT,
    SEASON,
    read as _read,
    team_service as _team_service,
)

UNSUPPORTED = "publication_format_unsupported"
SEASON_STREAM = "traditional_opponent_season"


def _v1_per48():
    return {metric: 2.0 for metric in TEAM_METRICS}


def _v1_rows():
    """Thirty structurally valid rows in the retired format."""

    return tuple(
        PublicationTeamWindowRow(
            team_id=team_id,
            team_tricode=tricode,
            game_ids=("0022500001",),
            game_count=1,
            per48=_v1_per48(),
            league_average={},
            population_sigma={},
            competition_rank={},
        )
        for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
    )


def _v1_publication_payload():
    """The stored envelope a retained v1 publication actually holds."""

    return [
        {
            "team_id": int(team_id),
            "team_tricode": tricode,
            "game_ids": ["0022500001"],
            "game_count": 1,
            "counts": {metric: 1.0 for metric in TEAM_METRICS},
            "team_minutes": 240.0,
            "per48": _v1_per48(),
            "league_average": _v1_per48(),
            "population_sigma": {metric: 0.0 for metric in TEAM_METRICS},
            "competition_rank": {metric: 1 for metric in TEAM_METRICS},
        }
        for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
    ]


# --- The production read boundary ------------------------------------------


def _publish_v1(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'v1.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    publications.register_stream(
        SEASON_STREAM, provider="ledger", owner="railway",
        required_observations=(), publication_strategy="replace", enabled=True,
    )
    publications.compose(
        SEASON_STREAM, season=SEASON, cutoff=RETRIEVED_AT,
        payload={"rows": _v1_publication_payload()},
    )
    return engine


def test_the_read_boundary_reports_the_stable_reason_not_malformed_bytes(tmp_path):
    """A retained v1 pair is unreadable, not corrupt, and says so."""

    engine = _publish_v1(tmp_path)
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: RETRIEVED_AT)

    read = reader.read(SEASON_STREAM, season=SEASON)

    assert not read.available
    assert read.unavailable_reason == UNSUPPORTED
    assert read.decoded is None


# --- Team Profile ----------------------------------------------------------


def test_the_team_profile_serves_nothing_for_a_v1_publication():
    reads = {SEASON_STREAM: _read(SEASON_STREAM, _v1_rows())}

    stats = _team_service(reads).get_team_stats(
        "Traditional", "Los Angeles Lakers"
    )

    assert stats == {}


# --- Team Filters ----------------------------------------------------------


def test_team_filters_refuse_to_rank_a_v1_publication(caplog):
    from tests.support.publication_stubs import StubGovernance, StubReader

    service = TeamFilterRankingService(
        StubReader({SEASON_STREAM: _read(SEASON_STREAM, _v1_rows())}),
        governance_resolver=StubGovernance(),
    )

    with caplog.at_level(logging.WARNING):
        ranked = service.ranked_teams("OPP_PTS", SEASON)

    assert ranked == []
    assert UNSUPPORTED in caplog.text


# --- Matchups --------------------------------------------------------------


def test_the_matchups_window_records_the_stable_reason_for_a_v1_publication(
    tmp_path,
):
    from app.services.team_matchup_query import TeamMatchupQueryService
    from app.services.team_matchup_repository import TeamMatchupRepository

    engine = _publish_v1(tmp_path)
    service = TeamMatchupQueryService(
        TeamMatchupRepository(engine),
        clock=lambda: RETRIEVED_AT,
        publication_reader=DatabaseFirstPublicationReader(
            engine, clock=lambda: RETRIEVED_AT
        ),
    )

    window = service.get_latest_window(SEASON, as_of=RETRIEVED_AT.date())

    traditional = next(
        observation for observation in window.observations
        if observation.surface == "traditional"
    )
    assert traditional.status == "unavailable"
    assert traditional.unavailable_reason == UNSUPPORTED
    # No part of the unreadable publication reaches the Defense Sheet.
    assert "traditional" not in {
        metric.base for metrics in window.team_metrics.values()
        for metric in metrics
    }


# --- Matchup parity --------------------------------------------------------


def test_parity_refuses_a_v1_publication_with_the_stable_reason():
    from app.services.matchup_parity import _decode_ledger_rows

    with pytest.raises(MatchupParityError) as refusal:
        _decode_ledger_rows(
            json.dumps(_v1_publication_payload()), stream_key=SEASON_STREAM
        )

    assert str(refusal.value) == UNSUPPORTED
