"""Seeded Season publications and the read seams that serve them.

One canonical league of thirty decoded rows, one publication generation, and
the governed game set an NBA-owned stream must match.  Service and route tests
for the Team Profile share these so both prove the same evidence.
"""

from datetime import datetime, timezone

from app.config.settings import RuntimeSettings
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.services.database_first_activation import (
    PublicationRead,
    PublicationTeamWindowRow,
)
from app.services.team_filter_rankings import TeamFilterRankingService
from app.services.team_service import TeamService
from app.services.traditional_opponent_publications import (
    TRADITIONAL_OPPONENT_V2,
)

#: The traditional-opponent taxonomy this deployment reads.  Consumer fixtures
#: build from it rather than from the ledger's historical tuple, so they seed
#: the format production actually serves.
TRADITIONAL_METRICS = TRADITIONAL_OPPONENT_V2.metrics

SEASON = "2025-26"
RETRIEVED_AT = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
GAME_ID = "0022500001"


def traditional_per48(*, offensive_rebounds=1.0, defensive_rebounds=3.0, **overrides):
    """One coherent v2 traditional block.

    The rebound total is derived from the split exactly as composition derives
    it, so the block satisfies the identity the decoder proves.
    """

    values = {metric: 2.0 for metric in TRADITIONAL_METRICS}
    values.update(overrides)
    values["offensive_rebounds"] = offensive_rebounds
    values["defensive_rebounds"] = defensive_rebounds
    values["rebounds"] = offensive_rebounds + defensive_rebounds
    return values


def row(team_id, tricode, per48):
    return PublicationTeamWindowRow(
        team_id=team_id,
        team_tricode=tricode,
        game_ids=(GAME_ID,),
        game_count=1,
        per48=per48,
        league_average={},
        population_sigma={},
        competition_rank={},
    )


def league(per48_for):
    """Build the canonical thirty rows from one per-team metric builder."""

    return tuple(
        row(team_id, tricode, per48_for(tricode))
        for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
    )


def read(stream_key, rows, *, freshness="fresh", status="active"):
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


def missing_read(stream_key, season):
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


class StubReader:
    """One publication generation, recorded so the read seam stays visible."""

    def __init__(self, reads):
        self._reads = reads
        self.calls = []

    def read_many(self, stream_keys, *, season=None):
        keys = tuple(stream_keys)
        self.calls.extend((stream_key, season) for stream_key in keys)
        return {
            stream_key: self._reads.get(
                stream_key, missing_read(stream_key, season)
            )
            for stream_key in keys
        }


class StubGovernance:
    """The governed per-team game set an NBA publication must match."""

    def resolve_team_game_ids(self, season, cutoff, *, window, **kwargs):
        return {
            team_id: frozenset({GAME_ID}) for team_id in NBA_TEAM_ID_TO_TRICODE
        }


def team_service(reads):
    """A Team Profile service that can reach nothing but the publications."""

    return TeamService(
        settings=RuntimeSettings(
            environment="testing", nba={"current_season": SEASON}
        ),
        season_publications=TeamFilterRankingService(
            StubReader(reads), governance_resolver=StubGovernance()
        ),
    )
