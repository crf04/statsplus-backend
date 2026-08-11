"""Injury freshness, reconciliation, and pool-override service contract."""

from datetime import datetime, timedelta, timezone

from app.providers.rotowire import InjuryEntryEvidence, InjuryProviderSnapshot
from app.services.matchup_injuries import MatchupInjuryService
from app.services.injury_snapshot_repository import StoredInjurySnapshot
from app.services.player_pool import PoolPlayer
from app.utils import telemetry
from app.errors import ProviderUnavailableError


NOW = datetime(2026, 1, 15, 23, 55, tzinfo=timezone.utc)
SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738


def _event(*, scheduled_at="2026-01-16T00:30:00+00:00", status_code=1):
    return {
        "nba_game_id": GAME_ID,
        "scheduled_at": scheduled_at,
        "status_code": status_code,
        "status_text": "Scheduled" if status_code == 1 else "Final",
        "away_team_id": LAL,
        "away_team_tricode": "LAL",
        "home_team_id": BOS,
        "home_team_tricode": "BOS",
    }


class NeverProvider:
    calls = 0

    def get_snapshot(self):
        self.calls += 1
        raise AssertionError("disabled injury service called its provider")


class MemoryRepository:
    def __init__(self, stored=None):
        self.stored = stored
        self.replacements = []

    def get(self, scope):
        self.scope = scope
        return self.stored

    def replace(self, scope, **values):
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
        raw_payload=[{"ID": "6504"}],
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
        {"unmatched_entry_count": 1, "board_conflict_count": 1}
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


def test_tip_or_final_retains_the_last_snapshot_without_refreshing():
    provider = NeverProvider()
    result = MatchupInjuryService(
        provider=provider,
        snapshot_repository=MemoryRepository(_stored(NOW - timedelta(days=10))),
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
    assert result.block["status"] == "fresh"
    assert result.block["retrieved_at"] == (NOW - timedelta(days=10)).isoformat()
    assert result.out_player_ids == frozenset({2544})
