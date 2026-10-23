"""A trailing NBA-owned team-defense read is governed by its own season (#547).

After a Season Cutover the Published Season is 2026-27 while the Synergy,
shot-type and shot-zone Season windows still serve 2025-26 (``prior_season``).
Their governed game sets exist only for 2025-26, so the query must resolve
governance at the read's own season, not the requested one.
"""

from __future__ import annotations

from dataclasses import replace

from app.services.database_first_activation import PublicationReadSnapshot
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import TeamMatchupRepository, TeamMatchupSnapshotScope
from tests.services.test_nba_publication_matchup_materialization import AS_OF, _engine, _reader

NBA_BASES = ("play_types", "shot_types", "shot_zones")


class _AfterCutover:
    """The 2025-26 reads as a 2026-27 capture serves them: ``prior_season``."""

    def __init__(self, reader):
        self.reader = reader

    def resolve_team_game_ids(self, *args, **kwargs):
        return self.reader.resolve_team_game_ids(*args, **kwargs)

    def snapshot(self, stream_keys, *, season=None, require_active=True):
        reads = {
            key: replace(read, status="prior_season") if key.endswith("_season") and read.available else read
            for key, read in self.reader.snapshot(stream_keys, season=season).reads.items()
        }
        return PublicationReadSnapshot(
            season=season,
            reads=reads,
            generation=tuple(
                (key, read.publication_id, read.fence, read.version) for key, read in sorted(reads.items())
            ),
        )

    def read_many(self, stream_keys, *, season=None, require_active=True):
        return self.snapshot(stream_keys, season=season).reads


def test_a_trailing_nba_season_window_is_available_and_scoped_to_its_own_season(tmp_path):
    query = TeamMatchupQueryService(
        TeamMatchupRepository(_engine(tmp_path)), publication_reader=_AfterCutover(_reader())
    )

    window = query.get_window(TeamMatchupSnapshotScope("2026-27", AS_OF))

    observations = {item.surface: item for item in window.observations if item.surface in NBA_BASES}
    assert {base: (item.status, item.unavailable_reason) for base, item in observations.items()} == {
        base: ("available", None) for base in NBA_BASES
    }
    assert {base: window.fact_scopes[base].season for base in NBA_BASES} == dict.fromkeys(
        NBA_BASES, "2025-26"
    )
    assert {metric.base for metric in window.league_metrics} == set(NBA_BASES)
