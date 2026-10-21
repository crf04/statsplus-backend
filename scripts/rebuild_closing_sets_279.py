"""Rebuild the 2026-10-20 closing sets frozen before the statsplus#279 fixes.

One-off, approved in decision 4 of crf04/statsplus#279.  For every stored
closing set of the named games it replays the freeze at the set's own start
fence with the released code and compares the result with the stored
memberships.  A set whose replay differs is deleted and frozen again through
``ProjectionRecordingService.freeze_closing_projection_sets``, in one
transaction that rolls back unless the new set has the same id and exactly
the replayed memberships, and only the closing tables were written.

``closing_set_id`` digests the provider, scope, game and fence, so a rebuilt
set replays to itself: a second run finds no difference and writes nothing.

Without ``--apply`` it only reads, in a read-only transaction.  The database
URL comes from ``STATSPLUS_DATABASE_URL`` so it never appears in argv.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.inspection import _inspects

from app.models.projection_archive import (
    ClosingProjectionMembership,
    ClosingProjectionSet,
    MaterializationOutcome,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProviderPoll,
)
from app.providers.dfs import NBAMarketQuery
from app.services.event_catalog_repository import EventCatalogRepository
from app.services.projection_archive import (
    ProjectionArchive,
    ProjectionArchiveReadScope,
    ProjectionRecordingService,
    _projection_closing_start_fence,
    projection_query_key,
)
from app.services.statistic_catalog import StatisticCatalog

SEASON = "2026-27"
REBUILD_GAMES = ("0022600001", "0022600002")
REPORT_ONLY_GAMES = ("0022600003",)
WRITABLE_TABLES = {"projection_closing_sets", "projection_closing_memberships"}
# The freeze's scope lock re-inserts its existing row inside a savepoint; the
# unique violation rolls that insert back, but Postgres still counts it.
LOCK_TABLE = "projection_archive_scope_locks"


class InTransactionEngine:
    """Engine stand-in whose ``begin()`` and ``connect()`` reuse one open transaction.

    It lets the backend's own freeze run inside the transaction that deleted
    the old set, so the delete and the re-freeze commit or roll back together.
    """

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.dialect = connection.dialect

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        yield self.connection

    connect = begin


@_inspects(InTransactionEngine)
def _inspect_in_transaction(engine: InTransactionEngine) -> Any:
    return inspect(engine.connection)


@dataclass(frozen=True)
class SetShape:
    rows: int
    markets: int
    multi_line: int
    absent_from_last_poll: int

    def __str__(self) -> str:
        return (
            f"rows={self.rows} markets={self.markets} multi_line={self.multi_line} "
            f"absent_from_last_poll={self.absent_from_last_poll}"
        )


def market_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row["canonical_player_id"] or row["market_reference"],
        row["market_category"],
        row["market_variant"],
        row["scoring_period"],
    )


def membership_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row["market_reference"]), str(row["observation_id"]), str(row["generation_id"]))


def stored_memberships(connection: Connection, closing_set_id: str) -> list[Mapping[str, Any]]:
    membership = ClosingProjectionMembership.__table__
    observation = ProjectionObservation.__table__
    return list(
        connection.execute(
            select(
                membership.c.market_reference,
                membership.c.observation_id,
                membership.c.generation_id,
                observation.c.canonical_player_id,
                observation.c.market_category,
                observation.c.market_variant,
                observation.c.scoring_period,
            )
            .select_from(
                membership.join(observation, membership.c.observation_id == observation.c.observation_id)
            )
            .where(membership.c.closing_set_id == closing_set_id)
        ).mappings()
    )


def last_poll(connection: Connection, *, provider: str, query_key: str, game_id: str, fence: datetime) -> Any:
    """The newest advanced, non-replay generation whose poll completed by the fence."""

    generation = ProjectionMaterializationGeneration.__table__
    poll = ProviderPoll.__table__
    observation = ProjectionObservation.__table__
    newest = (
        connection.execute(
            select(generation.c.generation_id, poll.c.completed_at, generation.c.snapshot_id)
            .select_from(generation.join(poll, generation.c.source_poll_id == poll.c.poll_id))
            .where(
                generation.c.provider == provider,
                generation.c.season == SEASON,
                generation.c.query_key == query_key,
                generation.c.outcome == MaterializationOutcome.ADVANCED.value,
                generation.c.is_replay.is_(False),
                poll.c.completed_at <= fence,
            )
            .order_by(
                generation.c.retrieved_at.desc(),
                generation.c.created_at.desc(),
                generation.c.generation_id.desc(),
            )
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if newest is None:
        return None, set()
    offered = {
        market_key(row)
        for row in connection.execute(
            select(observation).where(
                observation.c.generation_id == newest["generation_id"],
                observation.c.canonical_game_id == game_id,
                observation.c.targetable.is_(True),
            )
        ).mappings()
    }
    return newest, offered


def shape(rows: list[Mapping[str, Any]], offered: set[tuple[Any, ...]]) -> SetShape:
    lines = Counter(market_key(row) for row in rows)
    return SetShape(
        rows=len(rows),
        markets=len(lines),
        multi_line=sum(1 for count in lines.values() if count > 1),
        absent_from_last_poll=sum(1 for key in lines if key not in offered),
    )


@dataclass
class SetReport:
    game_id: str
    provider: str
    closing_set_id: str
    started_at: datetime
    event: dict[str, Any]
    before: SetShape
    after: SetShape
    removed: int
    added: int
    last_poll: str

    @property
    def differs(self) -> bool:
        return bool(self.removed or self.added)


def analyse(connection: Connection, game_id: str, now: datetime) -> list[SetReport]:
    set_table = ClosingProjectionSet.__table__
    query_key = projection_query_key(NBAMarketQuery(season=SEASON))
    event = EventCatalogRepository(connection.engine).get_event(SEASON, game_id, connection=connection)
    if event is None:
        raise SystemExit(f"STOP: {game_id} is not in the {SEASON} event catalog")
    fence = _projection_closing_start_fence(event, now=now)
    reports = []
    for stored in connection.execute(
        select(set_table)
        .where(set_table.c.season == SEASON, set_table.c.canonical_game_id == game_id)
        .order_by(set_table.c.provider)
    ).mappings():
        if stored["query_key"] != query_key:
            raise SystemExit(f"STOP: {stored['closing_set_id']} has an unexpected query key")
        started_at = stored["started_at"]
        if fence != started_at:
            raise SystemExit(f"STOP: {game_id} fence {fence} != stored {started_at} ({stored['provider']})")
        provider = stored["provider"]
        replay = ProjectionArchive._closing_materialization_rows(
            connection,
            provider=provider,
            season=SEASON,
            query_key=query_key,
            canonical_game_id=game_id,
            started_at=started_at,
        )
        current = stored_memberships(connection, stored["closing_set_id"])
        newest, offered = last_poll(
            connection, provider=provider, query_key=query_key, game_id=game_id, fence=started_at
        )
        before_keys = {membership_key(row) for row in current}
        after_keys = {membership_key(row) for row in replay}
        reports.append(
            SetReport(
                game_id=game_id,
                provider=provider,
                closing_set_id=stored["closing_set_id"],
                started_at=started_at,
                event=event,
                before=shape(current, offered),
                after=shape(replay, offered),
                removed=len(before_keys - after_keys),
                added=len(after_keys - before_keys),
                last_poll="none"
                if newest is None
                else f"{newest['completed_at'].isoformat()} ({len(offered)} markets)",
            )
        )
    return reports


def rebuild(engine: Any, report: SetReport, now: datetime) -> None:
    membership = ClosingProjectionMembership.__table__
    set_table = ClosingProjectionSet.__table__
    with engine.begin() as connection:
        deleted = connection.execute(
            delete(membership).where(membership.c.closing_set_id == report.closing_set_id)
        ).rowcount
        if deleted != report.before.rows:
            raise RuntimeError(
                f"{report.closing_set_id}: deleted {deleted} memberships, expected {report.before.rows}"
            )
        connection.execute(delete(set_table).where(set_table.c.closing_set_id == report.closing_set_id))

        query = NBAMarketQuery(season=SEASON)
        scope = ProjectionArchiveReadScope(provider=report.provider, query=query)
        archive = ProjectionArchive(InTransactionEngine(connection), StatisticCatalog.load_default())
        results = ProjectionRecordingService(archive, (scope,)).freeze_closing_projection_sets(
            events=[report.event], query=query, created_at=now
        )
        if [(result.closing_set_id, result.created) for result in results] != [(report.closing_set_id, True)]:
            raise RuntimeError(f"{report.closing_set_id}: freeze returned {results}")

        query_key = projection_query_key(query)
        replay = ProjectionArchive._closing_materialization_rows(
            connection,
            provider=report.provider,
            season=SEASON,
            query_key=query_key,
            canonical_game_id=report.game_id,
            started_at=report.started_at,
        )
        frozen = {membership_key(row) for row in stored_memberships(connection, report.closing_set_id)}
        if frozen != {membership_key(row) for row in replay} or len(frozen) != report.after.rows:
            raise RuntimeError(f"{report.closing_set_id}: frozen memberships differ from the replay")

        written = connection.execute(
            text(
                "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_xact_user_tables "
                "WHERE n_tup_ins + n_tup_upd + n_tup_del > 0"
            )
        ).all()
        for table, _inserted, updated, deleted_rows in written:
            if table in WRITABLE_TABLES or (table == LOCK_TABLE and updated == deleted_rows == 0):
                continue
            raise RuntimeError(f"{report.closing_set_id}: unexpected write to {table}")
        print(f"  rebuilt {report.closing_set_id} writes={[tuple(row) for row in written]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete and re-freeze sets whose replay differs")
    args = parser.parse_args()
    url = os.environ["STATSPLUS_DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    now = datetime.now(UTC)
    read_only = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    with read_only.connect() as connection:
        reports = [
            report
            for game_id in (*REBUILD_GAMES, *REPORT_ONLY_GAMES)
            for report in analyse(connection, game_id, now)
        ]
    print(f"mode={'apply' if args.apply else 'dry-run'} now={now.isoformat()}")
    for report in reports:
        action = (
            "report-only"
            if report.game_id in REPORT_ONLY_GAMES
            else ("rebuild" if report.differs else "unchanged")
        )
        print(
            f"{report.game_id} {report.provider:<10} {action:<11} fence={report.started_at.isoformat()} "
            f"last_poll={report.last_poll} removed={report.removed} added={report.added}\n"
            f"  before {report.before}\n  after  {report.after}"
        )
    pending = [report for report in reports if report.differs and report.game_id in REBUILD_GAMES]
    print(f"sets_to_rebuild={len(pending)}")
    if args.apply and pending:
        engine = create_engine(url)
        for report in pending:
            rebuild(engine, report, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
