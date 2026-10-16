"""Injury freshness, reconciliation, and pool-override service contract."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.providers.nba_injury_report import InjuryEntryEvidence, InjuryProviderSnapshot
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
                "nba_official:6504",
                "6504",
                "LeBron James",
                "LAL",
                "Out",
                "Out",
                "Left ankle soreness",
                "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-15_05_00PM.pdf",
                game_date="2026-01-15",
                matchup="LAL@BOS",
            ),
            InjuryEntryEvidence(
                "nba_official:9999",
                "9999",
                "Mystery Reserve",
                "BOS",
                None,
                "Game Time Decision",
                "Illness",
                "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-15_05_00PM.pdf",
                game_date="2026-01-15",
                matchup="LAL@BOS",
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
                "entry_id": "nba_official:6504",
                "source_player_id": "6504",
                "source_player_name": "LeBron James",
                "canonical_player_id": 2544,
                "team_id": LAL,
                "tricode": "LAL",
                "canonical_status": "Out",
                "raw_status": "Out",
                "reason": "Left ankle soreness",
                "source_url": "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-15_05_00PM.pdf",
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
        "source": "nba_official",
        "source_url": "https://official.nba.com/nba-injury-report-2025-26-season/",
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
        "entry_id": "nba_official:6504",
        "source_player_id": "6504",
        "source_player_name": "LeBron James",
        "canonical_player_id": 2544,
        "team_id": LAL,
        "tricode": "LAL",
        "canonical_status": "Out",
        "raw_status": "Out",
        "reason": "Left ankle soreness",
        "source_url": "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-15_05_00PM.pdf",
    }
    home_entry = result.block["teams"][1]["entries"][0]
    assert home_entry["canonical_player_id"] is None
    assert home_entry["canonical_status"] is None
    assert home_entry["raw_status"] == "Game Time Decision"
    assert result.out_player_ids == frozenset({2544})
    assert result.badge_refs == {2544: "nba_official:6504"}
    assert repository.replacements[0]["raw_payload"] == _provider_snapshot().raw_payload
    stored = repository.replacements[0]["normalized_entries"]
    # Stored rows carry the game identity on top of the wire fields.
    assert [
        {k: v for k, v in row.items() if k not in {"game_id", "game_date", "matchup"}}
        for row in stored
    ] == [away_entry, home_entry]
    assert [(row["game_id"], row["game_date"], row["matchup"]) for row in stored] == [
        (GAME_ID, "2026-01-15", "LAL@BOS")
    ] * 2
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
def test_injury_team_dialects_reconcile_through_the_central_aliases(
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
                "nba_official:1",
                "1",
                "Alias Player",
                source_team,
                "Questionable",
                "Questionable",
                "Soreness",
                "https://example.test/player/1",
                game_date="2026-01-15",
                matchup=f"{canonical_team}@BOS",
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
                "nba_official:1",
                "1",
                "Unknown Team",
                "ZZ",
                "Out",
                "Out",
                "Soreness",
                "https://example.test/player/1",
                game_date="2026-01-15",
                matchup="LAL@BOS",
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


# -- reconciling official-report names against the Athlete Catalog ------------


def _official_report_entries():
    from pathlib import Path

    from app.providers.nba_injury_report import entries_from_report, parse_injury_report

    path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "injury_report"
        / "Injury-Report_2025-12-25_05_00PM.pdf"
    )
    return entries_from_report(parse_injury_report(path.read_bytes()), source_url="u")


def _reconcile_official(catalog_rows):
    from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
    from app.services.matchup_injuries import AthleteNameIndex, reconcile_injury_entries

    rows, unresolved = reconcile_injury_entries(
        _official_report_entries(),
        teams={code: {"team_id": team_id} for code, team_id in NBA_TEAM_TRICODE_TO_ID.items()},
        catalog=AthleteNameIndex(catalog_rows),
    )
    assert unresolved == 0
    return {row["source_player_name"]: row["canonical_player_id"] for row in rows}


def _catalog_row(player_id, name, team):
    return {"player_id": player_id, "display_name": name, "team_abbreviation": team}


def test_official_report_names_reconcile_through_accents_suffixes_and_hyphens():
    resolved = _reconcile_official(
        [
            _catalog_row(1, "Luka Dončić", "LAL"),
            _catalog_row(2, "Larry Nance Jr.", "CLE"),
            _catalog_row(3, "Dorian Finney-Smith", "HOU"),
            _catalog_row(4, "Dereck Lively II", "DAL"),
            _catalog_row(5, "Jae'Sean Tate", "HOU"),
            _catalog_row(6, "P.J. Washington", "DAL"),
            _catalog_row(7, "Jayson Tatum", "BOS"),
        ]
    )

    assert resolved["Luka Doncic"] == 1
    assert resolved["Larry Nance Jr."] == 2
    assert resolved["Dorian Finney-Smith"] == 3
    assert resolved["Dereck Lively II"] == 4
    assert resolved["Jae'Sean Tate"] == 5
    assert resolved["P.J. Washington"] == 6
    assert resolved["Jayson Tatum"] == 7


def test_official_report_suffix_the_catalog_omits_still_resolves_with_the_team():
    resolved = _reconcile_official(
        [
            _catalog_row(10, "Gary Trent", "MIL"),
            _catalog_row(11, "Jaime Jaquez", "MIA"),
        ]
    )

    assert resolved["Gary Trent Jr."] == 10
    assert resolved["Jaime Jaquez Jr."] == 11


def test_official_report_suffix_fallback_keeps_the_team_when_namesakes_exist():
    # "Kelly Oubre" is on two catalog teams; the report's PHI row picks PHI.
    resolved = _reconcile_official(
        [
            _catalog_row(21, "Kelly Oubre", "PHI"),
            _catalog_row(22, "Kelly Oubre", "CHA"),
        ]
    )

    assert resolved["Kelly Oubre Jr."] == 21


def test_official_report_traded_player_resolves_on_a_unique_name_only():
    # The report prints Kelly Oubre Jr. on PHI; the catalog still has him on CHA.
    resolved = _reconcile_official([_catalog_row(20, "Kelly Oubre Jr.", "CHA")])

    assert resolved["Kelly Oubre Jr."] == 20


def test_official_report_namesakes_and_unknown_players_stay_unmatched_but_listed():
    resolved = _reconcile_official(
        [
            # Two athletes share a name and neither is on the report's team.
            _catalog_row(30, "Brandon Williams", "UTA"),
            _catalog_row(31, "Brandon Williams", "SAC"),
            _catalog_row(32, "Jaylin Williams", "OKC"),
        ]
    )

    assert resolved["Brandon Williams"] is None  # ambiguous, never guessed
    assert resolved["Jaylin Williams"] == 32
    assert resolved["Harrison Ingram"] is None  # absent from the catalog
    assert len(resolved) == 115


# -- one report, several games of the same team --------------------------------


NYK = 1610612752
PDF = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-15_05_00PM.pdf"


def _two_game_snapshot():
    def entry(entry_id, name, team, status, day, matchup):
        return InjuryEntryEvidence(
            entry_id, entry_id, name, team, status, status, "r", PDF,
            game_date=day, matchup=matchup,
        )

    return InjuryProviderSnapshot(
        raw_payload=[{"report": "two dates"}],
        entries=(
            entry("a", "LeBron James", "LAL", "Probable", "2026-01-15", "LAL@BOS"),
            entry("b", "Jayson Tatum", "BOS", "Questionable", "2026-01-15", "LAL@BOS"),
            entry("c", "Austin Reaves", "LAL", "Out", "2026-01-17", "NYK@LAL"),
            entry("d", "Jalen Brunson", "NYK", "Out", "2026-01-17", "NYK@LAL"),
            # Same date, same team, a different game: not the requested one.
            entry("e", "Rui Hachimura", "LAL", "Out", "2026-01-15", "LAL@NYK"),
        ),
        retrieved_at=NOW,
    )


class TwoGameCatalog:
    def get_catalog(self, season, *, active_only=False):
        return [
            {"player_id": 2544, "display_name": "LeBron James", "team_abbreviation": "LAL"},
            {"player_id": 1630559, "display_name": "Austin Reaves", "team_abbreviation": "LAL"},
            {"player_id": 1628369, "display_name": "Jayson Tatum", "team_abbreviation": "BOS"},
            {"player_id": 1628973, "display_name": "Jalen Brunson", "team_abbreviation": "NYK"},
            {"player_id": 1629060, "display_name": "Rui Hachimura", "team_abbreviation": "LAL"},
        ]


def _read(event, repository=None):
    repository = repository or MemoryRepository()
    result = MatchupInjuryService(
        provider=RecordedProvider(_two_game_snapshot()),
        snapshot_repository=repository,
        athlete_catalog=TwoGameCatalog(),
        enabled=True,
        clock=lambda: NOW,
    ).get_injuries(event=event, season=SEASON, pool_players=())
    return result, repository


def test_only_the_requested_games_report_rows_describe_it():
    first, repository = _read(_event())  # LAL@BOS, 2026-01-15 ET

    assert [
        [entry["source_player_name"] for entry in team["entries"]]
        for team in first.block["teams"]
    ] == [["LeBron James"], ["Jayson Tatum"]]
    assert first.out_player_ids == frozenset()  # Reaves is Out only on the 17th
    assert [row["game_id"] for row in repository.replacements[0]["normalized_entries"]] == [
        GAME_ID,
        GAME_ID,
    ]

    later = _event(
        nba_game_id="0022500611",
        scheduled_at="2026-01-18T00:30:00+00:00",  # 7:30 PM ET on the 17th
        away_team_id=NYK,
        away_team_tricode="NYK",
        home_team_id=LAL,
        home_team_tricode="LAL",
    )
    second, _ = _read(later)

    assert [
        [entry["source_player_name"] for entry in team["entries"]]
        for team in second.block["teams"]
    ] == [["Jalen Brunson"], ["Austin Reaves"]]
    assert second.out_player_ids == frozenset({1630559, 1628973})


def test_a_team_game_the_report_does_not_list_gets_no_rows_from_other_dates():
    third = _event(
        nba_game_id="0022500700",
        scheduled_at="2026-01-20T00:30:00+00:00",  # LAL@BOS again, the 19th
    )

    result, _ = _read(third)

    assert result.block["status"] == "fresh"
    assert [team["entries"] for team in result.block["teams"]] == [[], []]
    assert result.out_player_ids == frozenset()


def test_evidence_without_a_game_identity_describes_no_game():
    snapshot = InjuryProviderSnapshot(
        raw_payload=[{}],
        entries=(InjuryEntryEvidence("x", "x", "LeBron James", "LAL", "Out", "Out", "", PDF),),
        retrieved_at=NOW,
    )
    result = MatchupInjuryService(
        provider=RecordedProvider(snapshot),
        snapshot_repository=MemoryRepository(),
        athlete_catalog=TwoGameCatalog(),
        enabled=True,
        clock=lambda: NOW,
    ).get_injuries(event=_event(), season=SEASON, pool_players=())

    assert [team["entries"] for team in result.block["teams"]] == [[], []]


def test_report_matchup_dialects_resolve_to_the_event_teams():
    # A report that printed legacy tricodes (GS/NO/PHO) still names this game.
    entry = InjuryEntryEvidence(
        "e", "s", "Some Player", "NO", "Out", "Out", "", PDF,
        game_date="2025-12-25", matchup="NO@PHO",
    )
    from app.services.matchup_injuries import (
        AthleteNameIndex,
        GameIdentity,
        reconcile_injury_entries,
    )

    rows, unresolved = reconcile_injury_entries(
        [entry],
        teams={"NOP": {"team_id": 1}, "PHX": {"team_id": 2}},
        catalog=AthleteNameIndex(
            [{"player_id": 7, "display_name": "Some Player", "team_abbreviation": "NOP"}]
        ),
        game=GameIdentity("2025-12-25", "NOP@PHX", "g"),
    )

    assert [(r["canonical_player_id"], r["canonical_status"], r["game_id"]) for r in rows] == [
        (7, "Out", "g")
    ]
    assert unresolved == 0
