"""Scheduled injury-report refresh, ``/api/injuries`` and Slate injury counts.

Stored snapshots are real rows in a migrated SQLite database; only the NBA
injury-report provider (at its ``get_snapshot`` interface), the Event Catalog
and the Athlete Catalog are replaced.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import FeatureSettings, NBASeasonSettings, RuntimeSettings
from app.errors import ProviderUnavailableError
from app.migrations import run_migrations
from app.providers.nba_injury_report import InjuryEntryEvidence, InjuryProviderSnapshot
from app.services.injury_listing import StoredInjuryListing
from app.services.injury_refresh import InjuryReportRefreshService
from app.services.injury_snapshot_repository import InjurySnapshotRepository
from app.services.matchup_injuries import MatchupInjuryService
from app.services.slate_service import SlateService
from tests.support.slate_catalog import NoNeighbourEvents

LAL, BOS, NYK, MIA = 1610612747, 1610612738, 1610612752, 1610612748
REPORT_5PM = (
    "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-15_05_00PM.pdf"
)
REPORT_530PM = (
    "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-15_05_30PM.pdf"
)
#: 3pm Eastern on 2026-01-15, inside the 11:00-19:30 refresh window.
AFTERNOON = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)


def _event(game_id, away, home, tip):
    return {
        "nba_game_id": game_id,
        "scheduled_at": tip,
        "status_code": 1,
        "status_text": "7:30 pm ET",
        "classification": "Regular Season",
        "away_team": {"id": away[0], "tricode": away[1], "name": None},
        "home_team": {"id": home[0], "tricode": home[1], "name": None},
    }


GAMES = [
    _event("0022500584", (LAL, "LAL"), (BOS, "BOS"), "2026-01-16T00:30:00+00:00"),
    _event("0022500585", (NYK, "NYK"), (MIA, "MIA"), "2026-01-16T02:00:00+00:00"),
]


class Events(NoNeighbourEvents):
    def count_events(self, season):
        return len(GAMES)

    def get_freshness(self, season, *, now):
        return {"last_success_at": "2026-01-15T10:00:00+00:00"}

    def get_events_between(self, season, starts_at, ends_at):
        return [
            event
            for event in GAMES
            if starts_at <= datetime.fromisoformat(event["scheduled_at"]) < ends_at
        ]


class AthleteCatalog:
    def get_catalog(self, season, *, active_only=False):
        return [
            {"player_id": 2544, "display_name": "LeBron James", "team_abbreviation": "LAL"},
            {"player_id": 1628369, "display_name": "Jayson Tatum", "team_abbreviation": "BOS"},
        ]


def _entry(name, team, status, reason, matchup, url=REPORT_5PM):
    slug = name.lower().replace(" ", "")
    return InjuryEntryEvidence(
        entry_id=f"nba_official:2026-01-15:{matchup}:{slug}",
        source_player_id=f"{team}:{slug}",
        source_player_name=name,
        source_team_tricode=team,
        canonical_status=None if status == "Available" else status,
        raw_status=status,
        reason=reason,
        source_url=url,
        game_date="2026-01-15",
        matchup=matchup,
    )


class FakeInjuryProvider:
    """The official-report provider interface: ``get_snapshot``."""

    def __init__(self, entries):
        self.entries = entries
        self.failure = None
        self.clock = lambda: AFTERNOON

    def get_snapshot(self):
        if self.failure is not None:
            raise self.failure
        return InjuryProviderSnapshot(
            raw_payload=[{"report": "recorded"}],
            entries=tuple(self.entries),
            retrieved_at=self.clock(),
        )


REPORT_ONE = [
    _entry("LeBron James", "LAL", "Questionable", "Left ankle soreness", "LAL@BOS"),
    _entry("Jayson Tatum", "BOS", "Out", "Achilles recovery", "LAL@BOS"),
    _entry("Mystery Reserve", "BOS", "Available", "", "LAL@BOS"),
    _entry("Jalen Brunson", "NYK", "Probable", "Hamstring", "NYK@MIA"),
]


class World:
    """One migrated database plus the services that read and write it."""

    def __init__(self, tmp_path, dependencies, enabled=True):
        self.now = AFTERNOON
        self.engine = create_engine(f"sqlite:///{tmp_path / 'injuries.sqlite3'}")
        run_migrations(self.engine)
        self.provider = FakeInjuryProvider(REPORT_ONE)
        repository = InjurySnapshotRepository(self.engine)
        settings = RuntimeSettings(
            environment="testing",
            features=FeatureSettings(injury_report_enabled=enabled),
            nba=NBASeasonSettings(current_season="2025-26"),
        )
        injuries = MatchupInjuryService(
            provider=self.provider,
            snapshot_repository=repository,
            athlete_catalog=AthleteCatalog(),
            enabled=enabled,
            clock=lambda: self.now,
        )
        listing = StoredInjuryListing(
            event_catalog=Events(),
            snapshot_repository=repository,
            settings=settings,
            enabled=enabled,
            clock=lambda: self.now,
        )
        self.refresh = InjuryReportRefreshService(
            injuries=injuries,
            event_catalog=Events(),
            settings=settings,
            clock=lambda: self.now,
        )
        dependencies.injury_listing_service = listing
        dependencies.slate_service = SlateService(
            Events(),
            settings=settings,
            clock=lambda: self.now,
            injury_counts=listing,
        )

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)
        self.provider.clock = lambda: self.now

    def rows(self, table):
        with self.engine.connect() as connection:
            return connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


@pytest.fixture
def world(tmp_path, dependencies):
    return World(tmp_path, dependencies)


def _get(client, authenticate, path):
    return client.get(path, headers=authenticate())


def test_scheduled_refresh_stores_the_report_that_injuries_then_lists(
    client, authenticate, world
):
    assert world.refresh.run() == {
        "state": "new_report",
        "games": 2,
        "report_url": REPORT_5PM,
    }

    response = _get(client, authenticate, "/api/injuries?date=2026-01-15")

    assert response.status_code == 200
    stored_at = "2026-01-15T20:00:00+00:00"
    report_time = "2026-01-15T17:00:00-05:00"
    assert response.get_json() == {
        "date": "2026-01-15",
        "team": None,
        "injuries": [
            {
                "player": "Jayson Tatum", "player_id": 1628369, "team": "BOS",
                "game_id": "0022500584", "status": "Out", "reason": "Achilles recovery",
                "report_time": report_time, "retrieved_at": stored_at, "stale": False,
            },
            {
                "player": "Mystery Reserve", "player_id": None, "team": "BOS",
                "game_id": "0022500584", "status": "Available", "reason": "",
                "report_time": report_time, "retrieved_at": stored_at, "stale": False,
            },
            {
                "player": "LeBron James", "player_id": 2544, "team": "LAL",
                "game_id": "0022500584", "status": "Questionable",
                "reason": "Left ankle soreness",
                "report_time": report_time, "retrieved_at": stored_at, "stale": False,
            },
            {
                "player": "Jalen Brunson", "player_id": None, "team": "NYK",
                "game_id": "0022500585", "status": "Probable", "reason": "Hamstring",
                "report_time": report_time, "retrieved_at": stored_at, "stale": False,
            },
        ],
    }


def test_injuries_filters_to_one_team(client, authenticate, world):
    world.refresh.run()

    body = _get(client, authenticate, "/api/injuries?date=2026-01-15&team=lal").get_json()

    assert body["team"] == "LAL"
    assert [(row["player"], row["status"]) for row in body["injuries"]] == [
        ("LeBron James", "Questionable")
    ]


def test_a_day_with_no_report_lists_nothing(client, authenticate, world):
    world.refresh.run()

    body = _get(client, authenticate, "/api/injuries?date=2026-01-20").get_json()

    assert body == {"date": "2026-01-20", "team": None, "injuries": []}


def test_rerunning_within_one_report_adds_no_snapshot_but_moves_retrieved_at(
    client, authenticate, world
):
    world.refresh.run()
    world.advance(30)
    assert world.refresh.run()["state"] == "unchanged"

    assert world.rows("injury_source_snapshots") == 1
    assert world.rows("injury_snapshots") == 2
    body = _get(client, authenticate, "/api/injuries?date=2026-01-15&team=LAL").get_json()
    assert body["injuries"][0]["retrieved_at"] == "2026-01-15T20:30:00+00:00"
    assert body["injuries"][0]["stale"] is False


def test_a_new_report_replaces_game_rows_and_keeps_one_row_per_game(
    client, authenticate, world
):
    world.refresh.run()
    world.advance(30)
    world.provider.entries = [
        _entry("LeBron James", "LAL", "Out", "Left ankle soreness", "LAL@BOS", REPORT_530PM)
    ]

    assert world.refresh.run()["state"] == "new_report"

    assert world.rows("injury_source_snapshots") == 2
    assert world.rows("injury_snapshots") == 2
    body = _get(client, authenticate, "/api/injuries?date=2026-01-15").get_json()
    assert [(row["player"], row["status"], row["report_time"]) for row in body["injuries"]] == [
        ("LeBron James", "Out", "2026-01-15T17:30:00-05:00")
    ]


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc),  # 10:30 ET
        datetime(2026, 1, 16, 0, 45, tzinfo=timezone.utc),  # 19:45 ET
        datetime(2026, 1, 16, 1, 0, tzinfo=timezone.utc),  # 20:00 ET, after tip
    ],
)
def test_refresh_does_nothing_outside_the_game_day_window(
    client, authenticate, world, now
):
    world.now = now

    assert world.refresh.run()["state"] in {"outside_window", "no_games"}

    assert world.rows("injury_source_snapshots") == 0


def test_refresh_does_nothing_on_a_date_without_games(world):
    world.now = datetime(2026, 1, 20, 20, 0, tzinfo=timezone.utc)

    assert world.refresh.run() == {"state": "no_games", "games": 0}
    assert world.rows("injury_source_snapshots") == 0


def test_a_provider_outage_leaves_the_stored_report_untouched(
    client, authenticate, world
):
    world.refresh.run()
    world.advance(30)
    world.provider.failure = ProviderUnavailableError("down")

    with pytest.raises(ProviderUnavailableError):
        world.refresh.run()

    body = _get(client, authenticate, "/api/injuries?date=2026-01-15&team=LAL").get_json()
    assert body["injuries"][0]["retrieved_at"] == "2026-01-15T20:00:00+00:00"


def test_a_report_older_than_one_missed_run_is_marked_stale_before_tip(
    client, authenticate, world
):
    world.refresh.run()
    world.advance(50)

    body = _get(client, authenticate, "/api/injuries?date=2026-01-15&team=LAL").get_json()

    assert body["injuries"][0]["stale"] is True


def test_slate_games_carry_status_counts(client, world):
    world.refresh.run()

    games = client.get("/api/games/slate?date=2026-01-15").get_json()["games"]

    assert {game["game_id"]: game["injury_counts"] for game in games} == {
        "0022500584": {
            "Out": 1, "Doubtful": 0, "Questionable": 1, "Probable": 0, "Available": 1,
        },
        "0022500585": {
            "Out": 0, "Doubtful": 0, "Questionable": 0, "Probable": 1, "Available": 0,
        },
    }


def test_slate_counts_are_null_before_any_report_is_stored(client, world):
    games = client.get("/api/games/slate?date=2026-01-15").get_json()["games"]

    assert [game["injury_counts"] for game in games] == [None, None]


def test_injuries_requires_a_valid_date_and_known_parameters(client, authenticate, world):
    headers = authenticate()

    assert client.get("/api/injuries", headers=headers).status_code == 400
    assert client.get("/api/injuries?date=15-01-2026", headers=headers).status_code == 400
    assert client.get("/api/injuries?date=2026-01-15&team=XYZ", headers=headers).status_code == 400
    assert client.get("/api/injuries?date=2026-01-15&x=1", headers=headers).status_code == 400


def test_injuries_is_unavailable_while_the_report_is_disabled(
    client, authenticate, tmp_path, dependencies
):
    World(tmp_path, dependencies, enabled=False)

    response = _get(client, authenticate, "/api/injuries?date=2026-01-15")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "provider_unavailable"


def test_a_disabled_report_refreshes_nothing(tmp_path, dependencies):
    world = World(tmp_path, dependencies, enabled=False)

    assert world.refresh.run() == {"state": "disabled", "games": 0, "report_url": None}
    assert world.rows("injury_source_snapshots") == 0
