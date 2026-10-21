"""Projection archive retention (#502) through its public entry point."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.domain.utc import assume_utc
from app.migrations import run_migrations
from app.models.projection_archive import (
    ClosingProjectionMembership,
    LatestPlayerProjection,
    ProjectionArchiveScopeLock,
    ProjectionMaterializationGeneration,
    ProjectionObservation,
    ProjectionProviderSnapshot,
    ProjectionRetentionRun,
    ProviderPoll,
)
from app.providers.dfs import (
    AthleteEvidence,
    CoverageEvidence,
    EventEvidence,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    Selection,
    SelectionDirection,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services import projection_retention
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
)
from app.services.projection_retention import ProjectionArchiveRetention
from app.services.statistic_catalog import StatisticCatalog

SEASON = "2026-27"
QUERY = NBAMarketQuery(season=SEASON)
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
LAKERS = 1610612747
#: Tipped 10-14 23:00 and never frozen: only a retained replay reads it.
G0 = "0022600010"
#: Tipped 10-15 23:00 and frozen for underdog and dabble.
G1 = "0022600011"
#: Tips 10-21 00:00.
G2 = "0022600012"
TIPS = {
    G0: datetime(2026, 10, 14, 23, 0, tzinfo=UTC),
    G1: datetime(2026, 10, 15, 23, 0, tzinfo=UTC),
}


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


class Events:
    """The Event Catalog read: G0 and G1 are final, G2 is scheduled."""

    def get_events_by_ids(self, season, game_ids):
        events = []
        for game_id in game_ids:
            if game_id in TIPS:
                tip = TIPS[game_id].isoformat()
                events.append(
                    {
                        "nba_game_id": game_id,
                        "status_text": "Final",
                        "status_code": 3,
                        "scheduled_at": tip,
                        "first_observed_started_at": tip,
                    }
                )
            elif game_id == G2:
                events.append(
                    {
                        "nba_game_id": game_id,
                        "status_text": "Scheduled",
                        "status_code": 1,
                        "scheduled_at": "2026-10-21T00:00:00+00:00",
                        "first_observed_started_at": None,
                    }
                )
        return events


def _sides(over: int, under: int) -> tuple[Selection, ...]:
    return (
        Selection(selection_id="over", direction=SelectionDirection.HIGHER, american_price=over),
        Selection(selection_id="under", direction=SelectionDirection.LOWER, american_price=under),
    )


EVEN = _sides(-110, -110)


def market(
    catalog,
    provider: str,
    market_id: str,
    *,
    game_id: str,
    line: str | None,
    selections: tuple[Selection, ...],
    statistic: str = "points",
    status: MarketStatus = MarketStatus.AVAILABLE,
) -> PlayerProjectionMarket:
    canonical = catalog.by_id[statistic]
    evidence = StatisticEvidence(provider_id=statistic, canonical_id=canonical.id)
    return PlayerProjectionMarket(
        provider=provider,
        market_id=market_id,
        athlete=AthleteEvidence(
            canonical_id=2544,
            name="LeBron James",
            team=TeamEvidence(canonical_id=LAKERS, abbreviation="LAL"),
        ),
        event=EventEvidence(canonical_id=game_id),
        team=TeamEvidence(canonical_id=LAKERS, abbreviation="LAL"),
        statistic=evidence,
        statistic_match=StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=evidence,
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=canonical,
            provider=provider,
        ),
        threshold=None if line is None else MarketThreshold(line, "count"),
        status=status,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
        selections=selections,
    )


def board(provider, markets, retrieved_at, status=SnapshotStatus.COMPLETE) -> ProviderSnapshot:
    count = len(markets)
    complete = status is SnapshotStatus.COMPLETE
    return ProviderSnapshot(
        provider=provider,
        status=status,
        markets=tuple(markets),
        coverage=CoverageEvidence(
            fetched_count=count,
            eligible_count=count,
            normalized_count=count,
            expected_total=count if complete else count + 1,
            warning_codes=() if complete else ("page_fetch_failed",),
        ),
        retrieved_at=retrieved_at,
    )


def ingest(archive, snapshot) -> None:
    archive.ingest_snapshot(snapshot, query=QUERY, accepted_at=snapshot.retrieved_at)


def ud(catalog, game_id, line, over=-110, under=-110, statistic="points"):
    suffix = {"points": "pts", "rebounds": "reb", "assists": "ast"}[statistic]
    return market(
        catalog,
        "underdog",
        f"ud-{game_id[-2:]}-{suffix}",
        game_id=game_id,
        line=line,
        selections=_sides(over, under),
        statistic=statistic,
    )


def seed(engine) -> ProjectionArchive:
    """Underdog boards across six days, a stale dabble board, old prizepicks Partials.

    underdog: B1 10-14 18:00 and B2 20:00 (G0's baseline at its tip), B3
    10-15 10:00, B4 20:00 (G1's baseline, frozen), B5 10-16 10:00, then B6
    10-19 and B7 10-20 inside the window.  Failed polls at 10-14 19:00 and
    10-16 12:00.  dabble: D0 10-13 10:00, then D1 12:00 (still active,
    confirmed unchanged at 13:00, frozen for G1).  prizepicks: two Partials.
    """

    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    ingest(
        archive, board("underdog", (ud(catalog, G0, "20.5"), ud(catalog, G1, "27.5", -115, -105)), at(14, 18))
    )
    archive.record_failed_poll(
        provider="underdog", query=QUERY, completed_at=at(14, 19), failure_reason="http_503"
    )
    ingest(
        archive, board("underdog", (ud(catalog, G0, "21.5"), ud(catalog, G1, "27.5", -115, -105)), at(14, 20))
    )
    ingest(archive, board("underdog", (ud(catalog, G1, "28.5"),), at(15, 10)))
    ingest(archive, board("underdog", (ud(catalog, G1, "29.5"),), at(15, 20)))
    ingest(archive, board("underdog", (ud(catalog, G2, "30.5"),), at(16, 10)))
    archive.record_failed_poll(
        provider="underdog", query=QUERY, completed_at=at(16, 12), failure_reason="http_503"
    )
    ingest(archive, board("underdog", (ud(catalog, G2, "31.5"),), at(19, 10)))
    ingest(
        archive,
        board(
            "underdog", (ud(catalog, G2, "31.5"), ud(catalog, G2, "8.5", statistic="rebounds")), at(20, 11)
        ),
    )

    def db(line):
        return market(
            catalog,
            "dabble",
            "db-11-pts",
            game_id=G1,
            line=line,
            selections=(
                Selection(selection_id="higher", direction=SelectionDirection.HIGHER, american_price=-110),
            ),
        )

    ingest(archive, board("dabble", (db("26.5"),), at(13, 10)))
    ingest(archive, board("dabble", (db("27.5"),), at(13, 12)))
    ingest(archive, board("dabble", (db("27.5"),), at(13, 13)))

    def pp(line):
        return market(
            catalog,
            "prizepicks",
            "pp-11-pts",
            game_id=G1,
            line=line,
            selections=(
                Selection(selection_id="higher", direction=SelectionDirection.HIGHER, american_price=-110),
            ),
        )

    ingest(archive, board("prizepicks", (pp("26.5"),), at(14, 12), SnapshotStatus.PARTIAL))
    ingest(archive, board("prizepicks", (pp("27.5"),), at(14, 14), SnapshotStatus.PARTIAL))

    for provider in ("underdog", "dabble", "prizepicks"):
        archive.freeze_closing_projection_set(
            provider=provider,
            query=QUERY,
            canonical_game_id=G1,
            started_at=TIPS[G1],
            created_at=TIPS[G1] + timedelta(minutes=1),
        )
    return archive


def retention(engine, **kwargs) -> ProjectionArchiveRetention:
    return ProjectionArchiveRetention(engine, event_reader=Events(), clock=lambda: NOW, **kwargs)


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.sqlite3'}")
    run_migrations(engine)
    yield engine
    engine.dispose()


def _utc(value: datetime) -> str:
    return assume_utc(value).strftime("%m-%d %H:%M")


def observations(engine) -> list[tuple]:
    with engine.connect() as connection:
        return sorted(
            (row.provider, row.provider_market_id, row.line_value, _utc(row.observed_at))
            for row in connection.execute(select(ProjectionObservation))
        )


def counts(engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            model.__tablename__: connection.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (
                ProjectionObservation,
                ProjectionMaterializationGeneration,
                ProviderPoll,
                ProjectionProviderSnapshot,
                LatestPlayerProjection,
                ClosingProjectionMembership,
                ProjectionRetentionRun,
            )
        }


SEEDED = [
    ("dabble", "db-11-pts", 26.5, "10-13 10:00"),
    ("dabble", "db-11-pts", 27.5, "10-13 12:00"),
    ("prizepicks", "pp-11-pts", 26.5, "10-14 12:00"),
    ("prizepicks", "pp-11-pts", 27.5, "10-14 14:00"),
    ("underdog", "ud-10-pts", 20.5, "10-14 18:00"),
    ("underdog", "ud-10-pts", 21.5, "10-14 20:00"),
    ("underdog", "ud-11-pts", 27.5, "10-14 18:00"),
    ("underdog", "ud-11-pts", 27.5, "10-14 20:00"),
    ("underdog", "ud-11-pts", 28.5, "10-15 10:00"),
    ("underdog", "ud-11-pts", 29.5, "10-15 20:00"),
    ("underdog", "ud-12-pts", 30.5, "10-16 10:00"),
    ("underdog", "ud-12-pts", 31.5, "10-19 10:00"),
    ("underdog", "ud-12-pts", 31.5, "10-20 11:00"),
    ("underdog", "ud-12-reb", 8.5, "10-20 11:00"),
]

#: What is left: dabble's active board (Latest, at any age), the G0 replay
#: baseline row, the frozen G1 row, the window, and prizepicks untouched.
PRUNED = [
    ("dabble", "db-11-pts", 27.5, "10-13 12:00"),
    ("prizepicks", "pp-11-pts", 26.5, "10-14 12:00"),
    ("prizepicks", "pp-11-pts", 27.5, "10-14 14:00"),
    ("underdog", "ud-10-pts", 21.5, "10-14 20:00"),
    ("underdog", "ud-11-pts", 29.5, "10-15 20:00"),
    ("underdog", "ud-12-pts", 31.5, "10-19 10:00"),
    ("underdog", "ud-12-pts", 31.5, "10-20 11:00"),
    ("underdog", "ud-12-reb", 8.5, "10-20 11:00"),
]

EXPECTED_TABLES = {
    "projection_materialization_generations": {
        "dabble": {"before": 2, "delete": 1, "keep": 1},
        "prizepicks": {"before": 2, "delete": 0, "keep": 2},
        "underdog": {"before": 7, "delete": 3, "keep": 4},
    },
    "projection_observations": {
        "dabble": {"before": 2, "delete": 1, "keep": 1},
        "prizepicks": {"before": 2, "delete": 0, "keep": 2},
        "underdog": {"before": 10, "delete": 5, "keep": 5},
    },
    "projection_provider_polls": {
        "dabble": {"before": 3, "delete": 1, "keep": 2},
        "prizepicks": {"before": 2, "delete": 0, "keep": 2},
        "underdog": {"before": 9, "delete": 4, "keep": 5},
    },
    "projection_provider_snapshots": {
        "dabble": {"before": 2, "delete": 1, "keep": 1},
        "prizepicks": {"before": 2, "delete": 0, "keep": 2},
        "underdog": {"before": 7, "delete": 3, "keep": 4},
    },
}

EXPECTED_PROTECTED = {
    "projection_materialization_generations": {
        "dabble": {"active": 1},
        "underdog": {"closing_membership": 1, "closing_replay": 1},
    },
    "projection_observations": {
        "dabble": {"latest": 1},
        "underdog": {"closing_membership": 1, "closing_replay": 1},
    },
    "projection_provider_polls": {
        "dabble": {"active": 1, "provider_health": 1},
        "underdog": {"closing_membership": 1, "closing_replay": 1, "provider_health": 1},
    },
    "projection_provider_snapshots": {
        "dabble": {"active": 1},
        "underdog": {"closing_membership": 1, "closing_replay": 1},
    },
}


#: Every row the archive holds, whatever its age, and each line a Complete
#: board dropped: G0's line leaves with B3, G1's with B5.
def test_prune_deletes_old_rows_and_keeps_what_readers_need(engine):
    seed(engine)
    assert observations(engine) == SEEDED

    report = retention(engine).run(dry_run=False)

    assert report.outcome == "succeeded"
    assert report.cutoff == datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    assert observations(engine) == PRUNED
    assert report.as_dict()["tables"] == EXPECTED_TABLES
    assert report.as_dict()["protected"] == EXPECTED_PROTECTED
    assert counts(engine) == {
        "projection_observations": 8,
        "projection_materialization_generations": 7,
        "projection_provider_polls": 9,
        "projection_provider_snapshots": 7,
        "latest_player_projections": 4,
        "projection_closing_memberships": 3,
        "projection_retention_runs": 1,
    }
    with engine.connect() as connection:
        run = connection.execute(select(ProjectionRetentionRun)).one()
    assert (run.trigger, run.outcome) == ("manual", "succeeded")


def test_every_pointer_and_its_parents_survive_the_prune(engine):
    seed(engine)

    retention(engine).run(dry_run=False)

    observation = ProjectionObservation.__table__
    generation = ProjectionMaterializationGeneration.__table__
    snapshot = ProjectionProviderSnapshot.__table__
    poll = ProviderPoll.__table__
    with engine.connect() as connection:
        for pointer in (LatestPlayerProjection.__table__, ClosingProjectionMembership.__table__):
            resolved = connection.execute(
                select(func.count()).select_from(
                    pointer.join(observation, observation.c.observation_id == pointer.c.observation_id)
                    .join(generation, generation.c.generation_id == pointer.c.generation_id)
                    .join(snapshot, snapshot.c.snapshot_id == observation.c.snapshot_id)
                    .join(poll, poll.c.poll_id == observation.c.source_poll_id)
                )
            ).scalar_one()
            assert resolved == connection.execute(select(func.count()).select_from(pointer)).scalar_one()
        # dabble's board is six days old and still active, with its document.
        active = connection.execute(
            select(snapshot.c.retrieved_at, func.length(snapshot.c.evidence_document) > 0)
            .select_from(generation.join(snapshot, snapshot.c.snapshot_id == generation.c.snapshot_id))
            .where(generation.c.provider == "dabble")
        ).all()
    assert [(_utc(row[0]), bool(row[1])) for row in active] == [("10-13 12:00", True)]


def test_closing_reads_return_identical_rows_after_the_prune(engine):
    seed(engine)
    reader = LatestProjectionPlayerPoolReader(
        engine,
        tuple(ProjectionArchiveReadScope(provider=p, query=QUERY) for p in ("dabble", "underdog")),
        clock=lambda: NOW,
        event_reader=Events(),
    )

    def reads():
        return {
            "retained G0": reader.get_retained_pool_for_game(season=SEASON, game_id=G0).players,
            "retained G1": reader.get_retained_pool_for_game(season=SEASON, game_id=G1).players,
            "closing G1": reader.get_pool_for_game(season=SEASON, game_id=G1).players,
        }

    before = reads()
    assert {name: [player.lines for player in players] for name, players in before.items()} == {
        "retained G0": [{"underdog": {"PTS": 21.5}}],
        "retained G1": [{"dabble": {"PTS": 27.5}, "underdog": {"PTS": 29.5}}],
        "closing G1": [{"dabble": {"PTS": 27.5}, "underdog": {"PTS": 29.5}}],
    }

    assert retention(engine).run(dry_run=False).outcome == "succeeded"

    assert reads() == before


def test_prizepicks_is_untouched_at_any_age(engine):
    seed(engine)

    retention(engine).run(dry_run=False)

    assert [row for row in observations(engine) if row[0] == "prizepicks"] == [
        ("prizepicks", "pp-11-pts", 26.5, "10-14 12:00"),
        ("prizepicks", "pp-11-pts", 27.5, "10-14 14:00"),
    ]


def test_dry_run_reports_the_real_run_and_writes_nothing(engine):
    seed(engine)
    before = (counts(engine), observations(engine))

    report = retention(engine).run(dry_run=True)

    assert (counts(engine), observations(engine)) == before
    assert report.outcome == "succeeded"
    assert report.as_dict()["mode"] == "dry_run"
    assert report.as_dict()["tables"] == EXPECTED_TABLES
    assert report.as_dict()["protected"] == EXPECTED_PROTECTED


def test_a_second_run_deletes_nothing(engine):
    seed(engine)
    retention(engine).run(dry_run=False)
    after_first = (counts(engine), observations(engine))

    second = retention(engine).run(dry_run=False)

    assert second.outcome == "succeeded"
    assert {
        table: {provider: cell["delete"] for provider, cell in by_provider.items()}
        for table, by_provider in second.as_dict()["tables"].items()
    } == {
        table: {"dabble": 0, "prizepicks": 0, "underdog": 0}
        for table in (
            "projection_materialization_generations",
            "projection_observations",
            "projection_provider_polls",
            "projection_provider_snapshots",
        )
    }
    first_counts, first_rows = after_first
    assert (counts(engine), observations(engine)) == (
        {**first_counts, "projection_retention_runs": 2},
        first_rows,
    )


def test_deletes_run_in_batches_under_the_scope_lock(engine, monkeypatch):
    seed(engine)
    batches = []
    scope_transaction = ProjectionArchive._scope_transaction

    def recording(self, provider, season, query_key):
        batches.append(provider)
        return scope_transaction(self, provider, season, query_key)

    monkeypatch.setattr(ProjectionArchive, "_scope_transaction", recording)

    retention(engine, batch_size=1, snapshot_batch_size=1).run(dry_run=False)

    assert observations(engine) == PRUNED
    # dabble: 2 expired observations one at a time and the empty read, then 1
    # generation, 1 poll, 1 snapshot.  underdog: 7 expired observations and the
    # empty read, then 3 generations, 4 polls, 3 snapshots.
    assert batches == ["dabble"] * 6 + ["underdog"] * 18


def _forget_closing_protection(monkeypatch) -> None:
    """A protection bug: closing memberships and closing replays protect nothing."""

    real_ids_in = projection_retention._ids_in

    def forgetful(connection, column, ids):
        if column.table.name == "projection_closing_memberships":
            return set()
        return real_ids_in(connection, column, ids)

    monkeypatch.setattr(projection_retention, "_ids_in", forgetful)
    monkeypatch.setattr(projection_retention._ReplayRules, "needs", lambda *args: False)


def test_a_violated_safety_check_rolls_the_batch_back(tmp_path, monkeypatch, caplog):
    # A database whose foreign keys are not enforced, so only the safety check
    # stands between the bug and a dangling closing membership.
    engine = create_engine(f"sqlite:///{tmp_path / 'unenforced.sqlite3'}")
    event.listen(engine, "connect", lambda dbapi, _record: dbapi.execute("PRAGMA foreign_keys=OFF"))
    run_migrations(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
    seed(engine)
    _forget_closing_protection(monkeypatch)
    before = [row for row in observations(engine) if row[0] == "underdog"]

    with caplog.at_level(logging.ERROR, logger="app.services.projection_retention"):
        report = retention(engine).run(dry_run=False)

    assert report.outcome == "failed"
    assert report.failure["reason"].startswith("underdog/2026-27/")
    assert report.failure["reason"].endswith(": 1 closing memberships would not resolve")
    assert [row for row in observations(engine) if row[0] == "underdog"] == before
    assert "rolled back a batch" in caplog.text
    with engine.connect() as connection:
        assert connection.execute(select(ProjectionRetentionRun.outcome)).scalar_one() == "failed"
    engine.dispose()


def test_an_enforced_foreign_key_also_rolls_the_batch_back(engine, monkeypatch):
    seed(engine)
    _forget_closing_protection(monkeypatch)
    before = [row for row in observations(engine) if row[0] == "underdog"]

    report = retention(engine).run(dry_run=False)

    assert (report.outcome, report.failure) == ("failed", {"reason": "IntegrityError"})
    assert [row for row in observations(engine) if row[0] == "underdog"] == before


def test_nightly_waits_for_the_first_manual_run(engine):
    seed(engine)

    skipped = retention(engine).run_nightly()

    assert (skipped.outcome, skipped.failure) == (
        "skipped",
        {"reason": "no manual run has succeeded yet"},
    )
    assert observations(engine) == SEEDED

    retention(engine).run(dry_run=True)
    assert retention(engine).run_nightly().outcome == "skipped"

    assert retention(engine).run(dry_run=False).outcome == "succeeded"
    nightly = ProjectionArchiveRetention(
        engine, event_reader=Events(), clock=lambda: NOW + timedelta(days=1)
    ).run_nightly()

    assert (nightly.trigger, nightly.outcome) == ("nightly", "succeeded")
    with engine.connect() as connection:
        assert connection.execute(
            select(ProjectionRetentionRun.trigger, ProjectionRetentionRun.outcome).order_by(
                ProjectionRetentionRun.run_id
            )
        ).all() == [("manual", "succeeded"), ("nightly", "succeeded")]


class NoEvents:
    """An Event Catalog that has no start for any game."""

    def get_events_by_ids(self, season, game_ids):
        return []


def test_closing_memberships_survive_without_a_catalog_start(engine):
    seed(engine)
    reader = LatestProjectionPlayerPoolReader(
        engine,
        tuple(ProjectionArchiveReadScope(provider=p, query=QUERY) for p in ("dabble", "underdog")),
        clock=lambda: NOW,
        event_reader=Events(),
    )
    before = reader.get_pool_for_game(season=SEASON, game_id=G1).players

    report = ProjectionArchiveRetention(engine, event_reader=NoEvents(), clock=lambda: NOW).run(dry_run=False)

    assert report.outcome == "succeeded"
    # Without a start no replay is protected: G0's retained row goes, the
    # frozen G1 row stays because its closing membership names it.
    assert [row for row in observations(engine) if row[0] == "underdog" and row[3] < "10-17"] == [
        ("underdog", "ud-11-pts", 29.5, "10-15 20:00")
    ]
    assert reader.get_pool_for_game(season=SEASON, game_id=G1).players == before


def test_a_kept_poll_keeps_the_generation_it_confirmed(engine):
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    first = board("underdog", (ud(catalog, G2, "30.5"),), at(16, 10))
    ingest(archive, first)
    # Inside the window, an unchanged poll confirms the 10-16 generation.
    confirmed = board("underdog", (ud(catalog, G2, "30.5"),), at(17, 13))
    ingest(archive, confirmed)
    ingest(archive, board("underdog", (ud(catalog, G2, "31.5"),), at(18, 10)))

    report = retention(engine).run(dry_run=False)

    assert report.outcome == "succeeded"
    # The window's unchanged poll names it.
    assert report.protected["projection_materialization_generations"] == {"underdog": {"window": 1}}
    generation = ProjectionMaterializationGeneration.__table__
    poll = ProviderPoll.__table__
    with engine.connect() as connection:
        dangling = connection.execute(
            select(func.count())
            .select_from(poll)
            .where(
                poll.c.generation_id.is_not(None),
                ~select(generation.c.generation_id)
                .where(generation.c.generation_id == poll.c.generation_id)
                .exists(),
            )
        ).scalar_one()
    assert dangling == 0
    # Re-delivering the confirmed snapshot still reads as a replay of that poll.
    replayed = archive.ingest_snapshot(confirmed, query=QUERY, accepted_at=at(17, 13))
    assert (replayed.changed, replayed.observation_count) == (False, 1)


def test_a_stale_active_board_keeps_every_row_a_mapping_replay_reads(engine):
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    targetable = market(catalog, "dabble", "db-pts", game_id=G2, line="27.5", selections=EVEN)
    # No price: archived evidence that never enters Latest or a closing set.
    unpriced = market(
        catalog, "dabble", "db-reb", game_id=G2, line="8.5", selections=(), statistic="rebounds"
    )
    ingest(archive, board("dabble", (targetable, unpriced), at(13, 12)))

    report = retention(engine).run(dry_run=False)

    assert report.outcome == "succeeded"
    assert observations(engine) == [
        ("dabble", "db-pts", 27.5, "10-13 12:00"),
        ("dabble", "db-reb", 8.5, "10-13 12:00"),
    ]
    assert report.protected["projection_observations"] == {"dabble": {"active_snapshot": 1, "latest": 1}}


def test_a_broken_active_pointer_stops_the_prune(engine):
    seed(engine)
    lock = ProjectionArchiveScopeLock.__table__
    with engine.begin() as connection:
        connection.execute(
            lock.update().where(lock.c.provider == "underdog").values(active_generation_id="gen_gone")
        )
    before = [row for row in observations(engine) if row[0] == "underdog"]

    report = retention(engine).run(dry_run=False)

    assert report.outcome == "failed"
    assert report.failure["reason"].startswith("underdog/2026-27/")
    assert report.failure["reason"].endswith(": the active generation pointer does not resolve")
    assert [row for row in observations(engine) if row[0] == "underdog"] == before


def test_latest_rows_outside_the_active_snapshot_keep_the_live_pool(engine):
    catalog = StatisticCatalog.load_default()
    archive = ProjectionArchive(engine, catalog)
    ingest(
        archive,
        board(
            "underdog",
            (
                ud(catalog, G2, "24.5"),
                ud(catalog, G2, "8.5", statistic="rebounds"),
                ud(catalog, G2, "6.5", statistic="assists"),
            ),
            at(12, 10),
        ),
    )
    ingest(
        archive,
        board(
            "underdog", (ud(catalog, G2, "9.5", statistic="rebounds"),), at(13, 10), SnapshotStatus.PARTIAL
        ),
    )
    ingest(archive, board("underdog", (ud(catalog, G2, "25.5"),), at(14, 10), SnapshotStatus.PARTIAL))
    reader = LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(provider="underdog", query=QUERY),
        clock=lambda: at(14, 10) + timedelta(minutes=5),
        event_reader=Events(),
    )

    def reads():
        pool = reader.get_pool_for_game(season=SEASON, game_id=G2)
        state = reader._read_pool_state(SEASON, (G2,))
        offerings = sorted(
            (offering.market_id, offering.line_value)
            for offering in archive.latest_offerings(provider="underdog", query=QUERY, canonical_game_id=G2)
        )
        return (
            offerings,
            [player.lines for player in pool.players],
            pool.freshness,
            sorted((str(row["market_reference"]), row["line_value"]) for row in state.rows),
            [(row["outcome"], row["promoted"], _utc(row["retrieved_at"])) for row in state.polls],
        )

    before = reads()
    offerings, lines, freshness, _rows, polls = before
    # Latest holds the 10-12 assists, the 10-13 rebounds and the 10-14 points.
    assert offerings == [("ud-12-ast", 6.5), ("ud-12-pts", 25.5), ("ud-12-reb", 9.5)]
    assert lines == [{"underdog": {"PTS": 25.5}}]
    assert freshness["providers"] == {
        "underdog": {"status": "fresh", "retrieved_at": "2026-10-14T10:00:00+00:00"}
    }
    assert polls == [
        ("partial", True, "10-14 10:00"),
        ("partial", True, "10-13 10:00"),
        ("changed", True, "10-12 10:00"),
    ]

    assert retention(engine).run(dry_run=False).outcome == "succeeded"

    assert reads() == before
    # The 10-13 rebounds row is Latest from neither the active snapshot nor the newest Complete board.
    assert ("underdog", "ud-12-reb", 9.5, "10-13 10:00") in observations(engine)


def test_the_report_counts_the_evidence_bytes_it_keeps_and_deletes(engine):
    seed(engine)
    snapshot = ProjectionProviderSnapshot.__table__
    with engine.connect() as connection:
        documents = {
            (row.provider, _utc(row.retrieved_at)): len(row.evidence_document)
            for row in connection.execute(select(snapshot))
        }

    report = retention(engine).run(dry_run=True)

    assert report.as_dict()["evidence_bytes"] == {
        "dabble": {
            "before": documents["dabble", "10-13 10:00"] + documents["dabble", "10-13 12:00"],
            "delete": documents["dabble", "10-13 10:00"],
            "keep": documents["dabble", "10-13 12:00"],
            "kept_old": {"active": documents["dabble", "10-13 12:00"]},
        },
        "prizepicks": {
            "before": documents["prizepicks", "10-14 12:00"] + documents["prizepicks", "10-14 14:00"],
            "delete": 0,
            "keep": documents["prizepicks", "10-14 12:00"] + documents["prizepicks", "10-14 14:00"],
            "kept_old": {},
        },
        "underdog": {
            "before": sum(size for (provider, _), size in documents.items() if provider == "underdog"),
            "delete": sum(documents["underdog", at] for at in ("10-14 18:00", "10-15 10:00", "10-16 10:00")),
            "keep": sum(
                documents["underdog", at]
                for at in ("10-14 20:00", "10-15 20:00", "10-19 10:00", "10-20 11:00")
            ),
            "kept_old": {
                "closing_membership": documents["underdog", "10-15 20:00"],
                "closing_replay": documents["underdog", "10-14 20:00"],
            },
        },
    }
    assert (
        retention(engine).run(dry_run=False).as_dict()["evidence_bytes"] == report.as_dict()["evidence_bytes"]
    )
