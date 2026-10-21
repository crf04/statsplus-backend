"""A game whose provider freezes only partly committed (crf04/statsplus#238)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sqlalchemy_event

from app.migrations import run_migrations
from app.providers.dfs import NBAMarketQuery
from app.services.posted_lines import PostedLinesReader
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
)
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_projection_archive import (
    GAME_ID,
    OBSERVED_AT,
    SEASON,
    _line_market,
    _line_snapshot,
)

PROVIDERS = ("dabble", "prizepicks")


def _posted_line(tmp_path, frozen):
    """His posted line once ``frozen`` froze, and the providers replayed for it."""

    engine = create_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    query = NBAMarketQuery(season=SEASON)
    for provider, offers in (
        ("dabble", (("points", "26.5"),)),
        ("prizepicks", (("points", "27.5"), ("rebounds", "7.5"))),
    ):
        archive.ingest_complete_snapshot(
            _line_snapshot(
                provider,
                tuple(
                    _line_market(
                        catalog, provider=provider, market_id=stat, statistic_id=stat, threshold=value
                    )
                    for stat, value in offers
                ),
            ),
            query=query,
            accepted_at=OBSERVED_AT,
        )
    tip = OBSERVED_AT + timedelta(minutes=5)
    for provider in frozen:
        archive.freeze_closing_projection_set(
            provider=provider, query=query, canonical_game_id=GAME_ID, started_at=tip, created_at=tip
        )
    event = dict(
        nba_game_id=GAME_ID,
        scheduled_at=tip.isoformat(),
        classification="",
        status_code=3,
        status_text="Final",
        first_observed_started_at=tip.isoformat(),
        home_team_id=1610612744,
        home_team_tricode="GSW",
        away_team_id=1610612747,
        away_team_tricode="LAL",
    )

    class Schedule:
        def count_events(self, season):
            return 1

        def get_events_between(self, *args):
            return [event]

        def get_events_by_ids(self, *args):
            return [event]

    schedule = Schedule()
    pool = LatestProjectionPlayerPoolReader(
        engine,
        tuple(ProjectionArchiveReadScope(provider=p, query=query) for p in PROVIDERS),
        event_reader=schedule,
        clock=lambda: OBSERVED_AT + timedelta(days=90),
    )
    settings = SimpleNamespace(
        nba=SimpleNamespace(model_fields_set={"current_season"}, current_season=SEASON)
    )
    replayed = set()

    @sqlalchemy_event.listens_for(engine, "before_cursor_execute")
    def record_replay(_conn, _cursor, statement, parameters, _context, _many):
        if "projection_materialization_generations" in statement:
            replayed.update(value for value in parameters if value in PROVIDERS)

    result = PostedLinesReader(event_catalog=schedule, player_pool=pool, settings=settings).for_player(
        2544, OBSERVED_AT.date()
    )
    return result, replayed


@pytest.mark.parametrize(
    ("frozen", "replayed"),
    (
        # freeze_closing_projection_sets commits each alphabetically sorted provider
        # separately. A crash after dabble commits leaves this legitimate state.
        (("dabble",), {"prizepicks"}),
        # Both froze: their sets are the whole answer and no archive is replayed.
        (PROVIDERS, set()),
    ),
)
def test_a_frozen_game_keeps_prizepicks_preference_and_replays_only_the_unfrozen(tmp_path, frozen, replayed):
    result, read_from_archive = _posted_line(tmp_path, frozen)

    # Dabble's PTS 26.5 comes from its set; PrizePicks' lines, frozen or read
    # from its archive, win the preference for both markets.
    assert result["lines"] == {
        "PTS": {"value": 27.5, "provider": "prizepicks"},
        "REB": {"value": 7.5, "provider": "prizepicks"},
    }
    assert result["provider_lines"] == {
        "dabble": {"PTS": 26.5},
        "prizepicks": {"PTS": 27.5, "REB": 7.5},
    }
    assert read_from_archive == replayed
