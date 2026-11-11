"""The moves read stays scoped to the requested cells (crf04/statsplus#491)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.providers.dfs import MarketStatus, MarketThreshold
from app.services.line_moves import provider_histories
from app.services.projection_archive import ProjectionArchive
from app.services.statistic_catalog import StatisticCatalog
from tests.routes.test_games_board import BOS, LATER, QUERY, SEASON, TATUM, _market, _snapshot
from tests.routes.test_games_board_moves import _partial

#: In another game: none of its lines is a requested cell.
OTHER = (999999, "Other Player", BOS, "0022500999")
OPENED = datetime(2025, 12, 18, 20, tzinfo=UTC)
DAY_START = datetime(2026, 1, 15, 5, tzinfo=UTC)


class _CountingConnection:
    """Counts the rows each statement returns."""

    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, statement):
        result = self.connection.execute(statement).all()
        self.rows.append(len(result))
        return _Rows(result)


class _Rows(list):
    def all(self):
        return list(self)


def _read(engine):
    with engine.connect() as connection:
        counting = _CountingConnection(connection)
        histories = provider_histories(
            counting,
            season=SEASON,
            game_ids=[LATER],
            statistic_ids=["points"],
            providers=["prizepicks", "underdog"],
            fences={},
            day_start=DAY_START,
        )
    return histories, sum(counting.rows)


def test_the_read_returns_only_the_generations_the_requested_cells_need(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bound.sqlite3'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    tatum = _market(catalog, "prizepicks", TATUM, "points", "28.5")
    # Before Tatum's line opens: no requested cell is in it.
    archive.ingest_snapshot(
        _partial(
            "prizepicks",
            (_market(catalog, "prizepicks", OTHER, "rebounds", "4.5"),),
            OPENED - timedelta(days=9),
        ),
        query=QUERY,
        accepted_at=OPENED - timedelta(days=9),
    )
    # PrizePicks carries Tatum's line from December 18; Underdog posts it twice, then pulls it.
    archive.ingest_snapshot(_partial("prizepicks", (tatum,), OPENED), query=QUERY, accepted_at=OPENED)
    underdog_tatum = _market(catalog, "underdog", TATUM, "points", "27.5")
    underdog_other = _market(catalog, "underdog", OTHER, "rebounds", "6.5")
    pulled_at = DAY_START + timedelta(hours=10)
    for at, markets in (
        (DAY_START + timedelta(hours=8), (underdog_tatum, underdog_other)),
        (
            DAY_START + timedelta(hours=9),
            (replace(underdog_tatum, threshold=MarketThreshold("28.5", "count")), underdog_other),
        ),
        # The pulling board lists Tatum suspended: a Complete board's untargetable rows are not read.
        (pulled_at, (underdog_other, replace(underdog_tatum, status=MarketStatus.SUSPENDED))),
    ):
        archive.ingest_complete_snapshot(_snapshot("underdog", markets, at), query=QUERY, accepted_at=at)
    histories, rows = _read(engine)

    # Then weeks of generations that hold no requested cell: Partials elsewhere, and Complete
    # Underdog boards after the one that pulled Tatum.
    for step in range(1, 30):
        at = OPENED + timedelta(hours=12 * step)
        other = _market(catalog, "prizepicks", OTHER, "rebounds", str(4.5 + step))
        archive.ingest_snapshot(_partial("prizepicks", (other,), at), query=QUERY, accepted_at=at)
    for step in range(1, 30):
        at = pulled_at + timedelta(minutes=step)
        board = (_market(catalog, "underdog", OTHER, "rebounds", str(6.5 + step)),)
        archive.ingest_complete_snapshot(_snapshot("underdog", board, at), query=QUERY, accepted_at=at)
    after, rows_after = _read(engine)

    assert histories[("prizepicks", LATER, TATUM[0])].posted_now is True
    assert histories[("underdog", LATER, TATUM[0])].posted_now is False
    assert after == histories
    # Lines: PrizePicks' one and Underdog's two.  Generations: PrizePicks' one, Underdog's two
    # and the Complete board that pulled Tatum.  Confirmations: the three that posted him.
    assert (rows, rows_after) == (10, 10)
