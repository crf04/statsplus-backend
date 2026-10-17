"""PrizePicks lines from The Odds API on GET /api/games/matchup and its
selection (crf04/statsplus#215).

The Matchup serves a provider's posted line as ``lines[provider][category]``
and lists its markets in ``provenance``; the frontend marks the provider key
``prizepicks`` "PP".  The lines here are archived by the same writer the odds
beat uses, over the persisted route world.
"""

from __future__ import annotations

from datetime import timedelta

from app.providers.dfs import NBAMarketQuery
from app.providers.odds_api import BookQuote, OddsEvent
from app.services.odds_dfs_projection import PrizePicksProjectionWriter
from app.services.odds_identity import MappedEvent, ResolvedAthlete
from app.services.projection_archive import (
    ProjectionArchive,
    ProjectionArchiveReadScope,
    ProjectionRecordingService,
)
from app.services.statistic_catalog import StatisticResolver
from tests.routes import test_matchup_fixtures as route_fixtures
from tests.routes.test_matchup_fixtures import (
    BOS,
    GAME_ID,
    LAL,
    NOW,
    SEASON,
    _recorded_projection_snapshot,
)

projection_route_context = route_fixtures.projection_route_context

LEBRON = 2544


def _post_prizepicks_lines(context, quotes):
    query = NBAMarketQuery(season=SEASON)
    scope = ProjectionArchiveReadScope(provider="prizepicks", query=query)
    writer = PrizePicksProjectionWriter(
        ProjectionRecordingService(
            ProjectionArchive(context.engine, context.catalog), (scope,), default_scope=scope
        ),
        query=query,
        statistics=StatisticResolver(context.catalog),
    )
    return writer.record(
        MappedEvent(
            event=OddsEvent(
                "odds-evt-1", NOW + timedelta(hours=12), "Boston Celtics", "Los Angeles Lakers"
            ),
            game_id=GAME_ID,
            team_ids=(BOS, LAL),
            scheduled_at=NOW + timedelta(hours=12),
        ),
        quotes,
        {"LeBron James": ResolvedAthlete(LEBRON, LAL, "LeBron James")},
        retrieved_at=NOW,
        accepted_at=NOW,
        poll_started_at=NOW,
    )


def _quote(market, category, line, book="prizepicks"):
    return BookQuote(book, market, category, "LeBron James", line, -119, -119, None)


def test_the_matchup_row_carries_prizepicks_lines_beside_the_direct_providers(
    projection_route_context,
):
    context = projection_route_context
    context.assembled.projection_recorder.record_complete_snapshot(
        _recorded_projection_snapshot(context.catalog, threshold="26.5"),
        query=NBAMarketQuery(season=SEASON),
        accepted_at=NOW,
    )

    recorded = _post_prizepicks_lines(
        context,
        [
            _quote("player_points", "PTS", 27.5),
            _quote("player_rebounds", "REB", 7.5),
            # Another DFS app's row from the same response is never a line.
            _quote("player_points", "PTS", 25.5, book="underdog"),
        ],
    )

    response = context.client.get(f"/api/games/matchup?game_id={GAME_ID}")

    assert recorded == 2
    assert response.status_code == 200
    payload = response.get_json()
    (player,) = payload["players"]
    assert player["provenance"] == {"dabble": ["PTS"], "prizepicks": ["PTS", "REB"]}
    assert player["lines"] == {"dabble": {"PTS": 26.5}, "prizepicks": {"PTS": 27.5, "REB": 7.5}}
    assert payload["freshness"]["pool"]["providers"]["prizepicks"] == {
        "retrieved_at": "2026-01-15T12:00:00+00:00",
        "status": "fresh",
    }


def test_a_player_only_prizepicks_posts_is_selectable(projection_route_context):
    context = projection_route_context
    _post_prizepicks_lines(context, [_quote("player_points", "PTS", 27.5)])

    response = context.client.get(
        f"/api/games/matchup/selection?game_id={GAME_ID}&player_id={LEBRON}"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["player_id"] == LEBRON
    assert body["freshness"]["player_pool"]["providers"] == {
        "dabble": {"retrieved_at": None, "status": "missing"},
        "prizepicks": {"retrieved_at": "2026-01-15T12:00:00+00:00", "status": "fresh"},
    }
