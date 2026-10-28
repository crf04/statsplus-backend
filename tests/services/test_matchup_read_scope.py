"""Every read a Matchup composes rides the request's one connection, the production pool,
injury and book readers included (crf04/statsplus-backend#641)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sqlalchemy_event

from app.migrations import run_migrations
from app.services.matchup import MatchupService
from app.services.matchup_injuries import StoredMatchupInjuryReader
from app.services.player_game_log_repository import PlayerGameLogReadFreshness
from app.services.stats_freshness_repository import StatsFreshness
from tests.routes.test_games_board import LATER, NOW, STARTED, seed_board


class _EmptyPlayerLogs:
    @staticmethod
    def get_player_summaries(_season, _player_ids, **_kwargs):
        return {}

    @staticmethod
    def get_read_freshness(_season, **_kwargs):
        return PlayerGameLogReadFreshness("missing", None)


class _MissingStatsFreshness:
    @staticmethod
    def get():
        return StatsFreshness(None)


def matchup_over_board(board, engine):
    """A Matchup service on the board's schedule, pool, injury report and books."""

    return MatchupService(
        event_catalog=board.event_catalog,
        player_pool=board.player_pool,
        player_logs=_EmptyPlayerLogs(),
        player_diets=None,
        team_matchups=None,
        stats_freshness=_MissingStatsFreshness(),
        settings=board.settings,
        injuries=board.injuries,
        clock=lambda: NOW,
        engine=engine,
        sportsbook_reader=board.sportsbook_reader,
    )


@pytest.fixture
def matchup(tmp_path, dependencies):
    engine = create_engine(f"sqlite:///{tmp_path / 'matchup.sqlite3'}")
    run_migrations(engine)
    service = matchup_over_board(seed_board(engine, dependencies).service, engine)
    checkouts = []
    sqlalchemy_event.listen(engine, "checkout", lambda *_arguments: checkouts.append(1))
    return service, checkouts


@pytest.mark.parametrize("game", (LATER, STARTED), ids=("scheduled", "started"))
def test_a_matchup_checks_out_one_connection(matchup, game):
    service, checkouts = matchup

    document = service.get_matchup(game_id=game)

    assert document["players"]
    assert len(checkouts) == 1


def test_a_snapshot_compose_checks_out_one_connection(matchup):
    service, checkouts = matchup

    document = service.get_matchup_from_snapshot(
        game_id=LATER, publication_snapshot=None, injuries=StoredMatchupInjuryReader(service.injuries)
    )

    assert document["players"]
    assert len(checkouts) == 1
