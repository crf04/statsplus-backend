"""Injury freshness, reconciliation, and pool-override service contract."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.providers.rotowire import InjuryEntryEvidence, InjuryProviderSnapshot
from app.services.matchup_injuries import MatchupInjuryService
from app.services.injury_snapshot_repository import (
    StoredInjurySnapshot,
    StoredInjurySourceSnapshot,
)
from app.services.injury_snapshot_repository import InjurySnapshotRepository
from app.services.player_pool import PoolPlayer
from app.utils import telemetry
from app.errors import ProviderUnavailableError


NOW = datetime(2026, 1, 15, 23, 55, tzinfo=timezone.utc)
SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738


def _event(
    *, scheduled_at="2026-01-16T00:30:00+00:00", status_code=1, **overrides
):
    event = {
        "nba_game_id": GAME_ID,
        "scheduled_at": scheduled_at,
        "status_code": status_code,
        "status_text": "Scheduled" if status_code == 1 else "Final",
        "away_team_id": LAL,
        "away_team_tricode": "LAL",
        "home_team_id": BOS,
        "home_team_tricode": "BOS",
    }
    event.update(overrides)
    return event


class NeverProvider:
    calls = 0

    def get_snapshot(self):
        self.calls += 1
        raise AssertionError("disabled injury service called its provider")


class MemoryRepository:
    def __init__(self, stored=None):
        self.stored = stored
        self.replacements = []
        self.latest_source = None

    def get(self, scope):
        self.scope = scope
        return self.stored

    def get_latest_source(self, source):
        return self.latest_source

    def publish(self, scope, **values):
        self.scope = scope
        self.replacements.append(values)
        self.latest_source = StoredInjurySourceSnapshot(
            1,
            values["source"],
            list(values["raw_payload"]),
            tuple(values["source_entries"]),
            values["retrieved_at"],
        )
        return self.latest_source

    def replace_from_source(self, scope, **values):
        self.scope = scope
        self.replacements.append(values)


class RecordedProvider:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def get_snapshot(self):
        self.calls += 1
        if isinstance(self.snapshot, BaseException):
            raise self.snapshot
        return self.snapshot


class Catalog:
    def get_catalog(self, season, *, active_only=False):
        assert season == SEASON
        assert active_only is True
        return [
            {
                "season": SEASON,
                "player_id": 2544,
                "display_name": "LeBron James",
                "is_active": True,
                "is_active_for_season": True,
                "roster_status": "active",
                "team_id": LAL,
                "team_abbreviation": "LAL",
                "team_name": "Los Angeles Lakers",
            }
        ]


def _provider_snapshot():
    return InjuryProviderSnapshot(
        raw_payload=[{"ID": "6504"}, {"ID": "9999"}],
        entries=(
            InjuryEntryEvidence(
                "rotowire:6504",
                "6504",
                "LeBron James",
                "LAL",
                "Out",
                "Out",
                "Left ankle soreness",
                "https://www.rotowire.com/basketball/player/lebron-james-2344",
            ),
            InjuryEntryEvidence(
                "rotowire:9999",
                "9999",
                "Mystery Reserve",
                "BOS",
                None,
                "Game Time Decision",
                "Illness",
                "https://www.rotowire.com/basketball/player/mystery-reserve-9999",
            ),
        ),
        retrieved_at=NOW,
    )


def _pool_players():
    return (
        PoolPlayer(2544, "LeBron James", LAL, ("PTS",), {"prizepicks": ("PTS",)}),
        PoolPlayer(101, "Lakers Teammate", LAL, ("REB",), {"dabble": ("REB",)}),
    )


def _stored(retrieved_at):
    return StoredInjurySnapshot(
        normalized_entries=(
            {
                "entry_id": "rotowire:6504",
                "source_player_id": "6504",
                "source_player_name": "LeBron James",
                "canonical_player_id": 2544,
                "team_id": LAL,
                "tricode": "LAL",
                "canonical_status": "Out",
                "raw_status": "Out",
                "reason": "Left ankle soreness",
                "source_url": "https://www.rotowire.com/basketball/player/lebron-james-2344",
            },
        ),
        retrieved_at=retrieved_at,
    )


def test_disabled_and_unpermissioned_services_are_unavailable_without_fetching():
    provider = NeverProvider()
    disabled = MatchupInjuryService(
        provider=provider,
        snapshot_repository=None,
        athlete_catalog=None,
        enabled=False,
        permission_granted=False,
        clock=lambda: NOW,
    ).get_injuries(event=_event(), season=SEASON, pool_players=())
    permission_required = MatchupInjuryService(
        provider=provider,
        snapshot_repository=None,
        athlete_catalog=None,
        enabled=True,
        permission_granted=False,
        clock=lambda: NOW,
    ).get_injuries(event=_event(), season=SEASON, pool_players=())

    assert disabled.block == {
        "status": "unavailable",
        "unavailable_reason": "disabled",
        "retrieved_at": None,
        "source": "rotowire",
        "source_url": "https://www.rotowire.com/basketball/injury-report.php",
        "teams": [],
    }
    assert permission_required.block["unavailable_reason"] == "permission_required"
    assert disabled.out_player_ids == frozenset()
    assert permission_required.badge_refs == {}
    assert provider.calls == 0


def test_refresh_reconciles_entries_and_reports_out_board_conflicts():
    telemetry.clear_recorded_provider_events()
    provider = RecordedProvider(_provider_snapshot())
    repository = MemoryRepository()
    service = MatchupInjuryService(
        provider=provider,
        snapshot_repository=repository,
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    )

    result = service.get_injuries(
        event=_event(), season=SEASON, pool_players=_pool_players()
    )

    assert provider.calls == 1
    assert result.block["status"] == "fresh"
    assert result.block["unavailable_reason"] is None
    assert result.block["retrieved_at"] == NOW.isoformat()
    assert [team["submission_state"] for team in result.block["teams"]] == [
        "unknown",
        "unknown",
    ]
    away_entry = result.block["teams"][0]["entries"][0]
    assert away_entry == {
        "entry_id": "rotowire:6504",
        "source_player_id": "6504",
        "source_player_name": "LeBron James",
        "canonical_player_id": 2544,
        "team_id": LAL,
        "tricode": "LAL",
        "canonical_status": "Out",
        "raw_status": "Out",
        "reason": "Left ankle soreness",
        "source_url": "https://www.rotowire.com/basketball/player/lebron-james-2344",
    }
    home_entry = result.block["teams"][1]["entries"][0]
    assert home_entry["canonical_player_id"] is None
    assert home_entry["canonical_status"] is None
    assert home_entry["raw_status"] == "Game Time Decision"
    assert result.out_player_ids == frozenset({2544})
    assert result.badge_refs == {2544: "rotowire:6504"}
    assert repository.replacements[0]["raw_payload"] == _provider_snapshot().raw_payload
    assert repository.replacements[0]["normalized_entries"] == (
        away_entry,
        home_entry,
    )
    assert telemetry.snapshot_recent_injury_events() == [
        {
            "unmatched_entry_count": 1,
            "unresolved_team_entry_count": 0,
            "board_conflict_count": 1,
        }
    ]


@pytest.mark.parametrize(
    ("source_team", "canonical_team"),
    [("GS", "GSW"), ("NY", "NYK"), ("SA", "SAS"), ("PHO", "PHX"), ("NO", "NOP")],
)
def test_rotowire_team_dialects_reconcile_through_the_central_aliases(
    source_team, canonical_team
):
    team_id = 100
    event = _event(
        away_team_id=team_id,
        away_team_tricode=canonical_team,
    )
    snapshot = InjuryProviderSnapshot(
        raw_payload=[{"ID": "1", "team": source_team}],
        entries=(
            InjuryEntryEvidence(
                "rotowire:1",
                "1",
                "Alias Player",
                source_team,
                "Questionable",
                "Questionable",
                "Soreness",
                "https://example.test/player/1",
            ),
        ),
        retrieved_at=NOW,
    )

    result = MatchupInjuryService(
        provider=RecordedProvider(snapshot),
        snapshot_repository=MemoryRepository(),
        athlete_catalog=type(
            "AliasCatalog",
            (),
            {
                "get_catalog": lambda self, season, active_only=False: [
                    {
                        "player_id": 1,
                        "display_name": "Alias Player",
                        "team_abbreviation": canonical_team,
                    }
                ]
            },
        )(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    ).get_injuries(event=event, season=SEASON, pool_players=())

    assert result.block["teams"][0]["entries"][0]["tricode"] == canonical_team
    assert result.block["teams"][0]["entries"][0]["canonical_player_id"] == 1


def test_unresolved_source_team_stays_in_raw_evidence_and_emits_bounded_telemetry():
    telemetry.clear_recorded_provider_events()
    repository = MemoryRepository()
    snapshot = InjuryProviderSnapshot(
        raw_payload=[{"ID": "1", "player": "Unknown Team", "team": "ZZ"}],
        entries=(
            InjuryEntryEvidence(
                "rotowire:1",
                "1",
                "Unknown Team",
                "ZZ",
                "Out",
                "Out",
                "Soreness",
                "https://example.test/player/1",
            ),
        ),
        retrieved_at=NOW,
    )

    result = MatchupInjuryService(
        provider=RecordedProvider(snapshot),
        snapshot_repository=repository,
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    ).get_injuries(event=_event(), season=SEASON, pool_players=())

    assert repository.replacements[0]["raw_payload"] == snapshot.raw_payload
    assert all(not team["entries"] for team in result.block["teams"])
    assert telemetry.snapshot_recent_injury_events() == [
        {
            "unmatched_entry_count": 0,
            "unresolved_team_entry_count": 1,
            "board_conflict_count": 0,
        }
    ]


def test_five_minute_snapshot_is_reused_without_fetch_and_applies_out_override():
    provider = NeverProvider()
    result = MatchupInjuryService(
        provider=provider,
        snapshot_repository=MemoryRepository(_stored(NOW - timedelta(minutes=5))),
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    ).get_injuries(event=_event(), season=SEASON, pool_players=_pool_players())

    assert provider.calls == 0
    assert result.block["status"] == "fresh"
    assert result.out_player_ids == frozenset({2544})


def test_failed_refresh_stale_serves_only_through_thirty_minutes():
    provider = RecordedProvider(ProviderUnavailableError("offline"))
    stale = MatchupInjuryService(
        provider=provider,
        snapshot_repository=MemoryRepository(_stored(NOW - timedelta(minutes=30))),
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    ).get_injuries(event=_event(), season=SEASON, pool_players=_pool_players())
    expired = MatchupInjuryService(
        provider=provider,
        snapshot_repository=MemoryRepository(
            _stored(NOW - timedelta(minutes=30, microseconds=1))
        ),
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    ).get_injuries(event=_event(), season=SEASON, pool_players=_pool_players())

    assert stale.block["status"] == "stale"
    assert stale.out_player_ids == frozenset({2544})
    assert expired.block["status"] == "unavailable"
    assert expired.block["unavailable_reason"] == "fetch_failed"
    assert expired.out_player_ids == frozenset()


def test_tip_or_final_retains_without_refresh_but_keeps_wall_clock_freshness():
    provider = NeverProvider()
    stale = MatchupInjuryService(
        provider=provider,
        snapshot_repository=MemoryRepository(_stored(NOW - timedelta(minutes=10))),
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    ).get_injuries(
        event=_event(scheduled_at="2026-01-01T00:00:00+00:00", status_code=3),
        season=SEASON,
        pool_players=_pool_players(),
    )
    expired = MatchupInjuryService(
        provider=provider,
        snapshot_repository=MemoryRepository(_stored(NOW - timedelta(minutes=31))),
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    ).get_injuries(
        event=_event(scheduled_at="2026-01-01T00:00:00+00:00", status_code=3),
        season=SEASON,
        pool_players=_pool_players(),
    )

    assert provider.calls == 0
    assert stale.block["status"] == "stale"
    assert stale.block["retrieved_at"] == (NOW - timedelta(minutes=10)).isoformat()
    assert stale.out_player_ids == frozenset({2544})
    assert expired.block["status"] == "unavailable"
    assert expired.block["unavailable_reason"] == "fetch_failed"
    assert expired.out_player_ids == frozenset()
    assert expired.badge_refs == {}


def test_stored_override_reader_never_fetches_and_uses_the_same_age_policy():
    provider = NeverProvider()
    service = MatchupInjuryService(
        provider=provider,
        snapshot_repository=MemoryRepository(_stored(NOW - timedelta(minutes=10))),
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    )

    result = service.get_stored_injuries(
        event=_event(), season=SEASON, pool_players=_pool_players()
    )

    assert provider.calls == 0
    assert result.block["status"] == "stale"
    assert result.out_player_ids == frozenset({2544})


def test_two_games_reuse_one_recent_league_feed_without_a_second_fetch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shared-feed.sqlite3'}")
    run_migrations(engine)
    provider = RecordedProvider(_provider_snapshot())
    service = MatchupInjuryService(
        provider=provider,
        snapshot_repository=InjurySnapshotRepository(engine),
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    )

    first = service.get_injuries(
        event=_event(), season=SEASON, pool_players=_pool_players()
    )
    second = service.get_injuries(
        event=_event(nba_game_id="0022500585"),
        season=SEASON,
        pool_players=_pool_players(),
    )

    assert provider.calls == 1
    assert first.block["status"] == "fresh"
    assert second.block["status"] == "fresh"
    assert second.out_player_ids == frozenset({2544})


def test_concurrent_pretip_reads_share_one_refresh_per_service_worker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'single-flight.sqlite3'}")
    run_migrations(engine)
    entered = Event()
    both_initial_reads_finished = Event()
    release = Event()

    class ObservedRepository(InjurySnapshotRepository):
        def __init__(self, database_engine):
            super().__init__(database_engine)
            self.reads = 0
            self.read_lock = Lock()

        def get(self, scope):
            stored = super().get(scope)
            with self.read_lock:
                self.reads += 1
                if self.reads == 2:
                    both_initial_reads_finished.set()
            return stored

    class BlockingProvider:
        def __init__(self):
            self.calls = 0

        def get_snapshot(self):
            self.calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return _provider_snapshot()

    provider = BlockingProvider()
    service = MatchupInjuryService(
        provider=provider,
        snapshot_repository=ObservedRepository(engine),
        athlete_catalog=Catalog(),
        enabled=True,
        permission_granted=True,
        clock=lambda: NOW,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(
            service.get_injuries,
            event=_event(),
            season=SEASON,
            pool_players=_pool_players(),
        )
        assert entered.wait(timeout=5)
        follower = executor.submit(
            service.get_injuries,
            event=_event(),
            season=SEASON,
            pool_players=_pool_players(),
        )
        assert both_initial_reads_finished.wait(timeout=5)
        release.set()
        results = (owner.result(timeout=5), follower.result(timeout=5))

    assert provider.calls == 1
    assert [result.block["status"] for result in results] == ["fresh", "fresh"]
    assert all(result.out_player_ids == frozenset({2544}) for result in results)
