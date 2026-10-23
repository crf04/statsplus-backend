"""Trailing team-defense reads keep their own season (#547).

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
from tests.services import test_matchup_service as m
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


def test_a_season_window_with_trailing_synergy_beside_switched_team_defense_names_each_bases_season():
    window = m._window()
    switched = TeamMatchupSnapshotScope("2026-27", window.scope.as_of)
    mixed = replace(
        window,
        scope=switched,
        fact_scopes={
            base: scope if base == "play_types" else switched for base, scope in window.fact_scopes.items()
        },
    )

    payload = m._service(season_window=mixed).get_matchup(game_id=m.GAME_ID)

    assert {
        base: windows["season"]["season"]
        for base, windows in payload["league"]["surface_availability"].items()
    } == {
        "assist_locations": "2026-27",
        "play_types": "2025-26",
        "rebound_types": "2026-27",
        "shot_types": "2026-27",
        "shot_zones": "2026-27",
        "traditional": "2026-27",
    }
    # The section reads as trailing while any of its Bases trails.
    assert payload["experience"]["sections"]["season_defense"]["season"] == "2025-26"
