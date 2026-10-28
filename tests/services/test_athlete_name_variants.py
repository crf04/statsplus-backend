"""A posted name missing its suffix, or posted by nickname, resolves on its own roster.

Production, 2026-10-25 to 10-28: Dabble, Underdog, Novig and Polymarket posted
``Robert Williams``, ``Bobby Portis`` and ``Alexandre Sarr`` for the catalog's
``Robert Williams III``, ``Bobby Portis Jr.`` and ``Alex Sarr``, and every one
of those lines was archived untargetable.  The markets here have the shape the
real adapters build: Dabble names the athlete's team, Novig names only the two
teams of the event.  They run through the real board, resolvers, mapping
repository and projection archive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from sqlalchemy import create_engine, select

from app.config import RuntimeSettings
from app.migrations import run_migrations
from app.models.projection_archive import ProjectionObservation
from app.providers.dfs import (
    AthleteEvidence,
    CoverageEvidence,
    EventEvidence,
    MarketStatus,
    MarketThreshold,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    Selection,
    SelectionDirection,
    SnapshotStatus,
    StatisticEvidence,
    TeamEvidence,
)
from app.services.athlete_mapping_repository import AthleteMappingRepository
from app.services.athlete_resolver import (
    AthleteResolution,
    AthleteResolver,
    MappingResolutionState,
)
from app.services.dfs_board import DFSBoardService
from app.services.event_mapping_repository import EventMappingRepository
from app.services.event_resolver import EventResolver
from app.services.projection_archive import (
    ProjectionArchive,
    ProjectionArchiveReadScope,
    ProjectionRecordingService,
)
from app.services.projection_collection import (
    ProjectionCollectionCoordinator,
    ProjectionCollectionSettings,
)
from app.services.statistic_catalog import StatisticCatalog

SEASON = "2026-27"
NOW = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)
TIP = NOW + timedelta(hours=1)
CATALOG = StatisticCatalog.load_default()

POR, LAL, MIA, CHI, WAS, DAL = (
    1610612757,
    1610612747,
    1610612748,
    1610612741,
    1610612764,
    1610612742,
)
TEAMS = {
    POR: ("Trail Blazers", "POR", "Portland Trail Blazers"),
    LAL: ("Lakers", "LAL", "Los Angeles Lakers"),
    MIA: ("Heat", "MIA", "Miami Heat"),
    CHI: ("Bulls", "CHI", "Chicago Bulls"),
    WAS: ("Wizards", "WAS", "Washington Wizards"),
    DAL: ("Mavericks", "DAL", "Dallas Mavericks"),
}
GAMES = {
    "0022600101": (POR, LAL),
    "0022600102": (MIA, CHI),
    "0022600103": (WAS, DAL),
}
IND, NOP, NYK, ATL = 1610612754, 1610612740, 1610612752, 1610612737
ROBERT_WILLIAMS, BOBBY_PORTIS, ALEX_SARR = 1629057, 1626171, 1642259


def _athlete_row(player_id, name, team_id):
    nickname, tricode, _ = TEAMS[team_id]
    return {
        "season": SEASON,
        "player_id": player_id,
        "display_name": name,
        "roster_status": "Active",
        "is_active": True,
        "is_active_for_season": True,
        "team_id": team_id,
        "team_name": nickname,
        "team_abbreviation": tricode,
    }


class _AthleteCatalog:
    ROWS: ClassVar = [
        _athlete_row(ROBERT_WILLIAMS, "Robert Williams III", POR),
        _athlete_row(1630166, "Deni Avdija", POR),
        _athlete_row(2544, "LeBron James", LAL),
        _athlete_row(BOBBY_PORTIS, "Bobby Portis Jr.", MIA),
        _athlete_row(1628389, "Bam Adebayo", MIA),
        # Two Bulls whose names differ only by their suffix (synthetic).
        _athlete_row(9000001, "Marcus Hill Jr.", CHI),
        _athlete_row(9000002, "Marcus Hill III", CHI),
        _athlete_row(ALEX_SARR, "Alex Sarr", WAS),
        _athlete_row(1629029, "Luka Doncic", DAL),
    ]

    def get_catalog(self, season, active_only=False):
        return [row for row in self.ROWS if row["season"] == season]


class _EventCatalog:
    def get_events(self, season):
        rows = []
        for game_id, (home, away) in GAMES.items():
            home_name, home_code, _ = TEAMS[home]
            away_name, away_code, _ = TEAMS[away]
            rows.append(
                {
                    "nba_game_id": game_id,
                    "season": SEASON,
                    "scheduled_at": TIP,
                    "home_team_id": home,
                    "home_team_name": home_name,
                    "home_team_tricode": home_code,
                    "away_team_id": away,
                    "away_team_name": away_name,
                    "away_team_tricode": away_code,
                    "status_text": "Scheduled",
                    "status_code": 1,
                    "postponed_status": None,
                    "postponement_evidence": None,
                    "classification": "Regular Season",
                    "is_postponed": False,
                }
            )
        return rows if season == SEASON else []

    def get_freshness(self, season):
        return {"season": season, "fresh": True, "last_success_at": NOW.isoformat()}


def _team(team_id):
    _, tricode, full_name = TEAMS[team_id]
    return TeamEvidence(name=full_name, abbreviation=tricode)


def _event(game_id):
    home, away = GAMES[game_id]
    return EventEvidence(
        provider_id=f"event-{game_id}",
        label=f"{TEAMS[away][2]} @ {TEAMS[home][2]}",
        starts_at=TIP,
        home_team=_team(home),
        away_team=_team(away),
    )


def _market(provider, athlete_id, name, game_id, *, team_id=None, line="10.5", label="points"):
    market_id = f"{provider}-{athlete_id}-{line}"
    return PlayerProjectionMarket(
        provider=provider,
        market_id=market_id,
        athlete=AthleteEvidence(
            provider_id=athlete_id,
            name=name,
            team=None if team_id is None else _team(team_id),
        ),
        event=_event(game_id),
        statistic=StatisticEvidence(label=label),
        threshold=MarketThreshold(line, "count"),
        status=MarketStatus.AVAILABLE,
        scoring_period=None,
        starts_at=TIP,
        selections=(
            Selection(
                selection_id=f"{market_id}-o", direction=SelectionDirection.HIGHER, american_price=-115
            ),
            Selection(selection_id=f"{market_id}-u", direction=SelectionDirection.LOWER, american_price=-105),
        ),
    )


def _dabble(athlete_id, name, team_id, game_id):
    return _market("dabble", athlete_id, name, game_id, team_id=team_id)


def _novig(athlete_id, name, game_id):
    # Novig lists each player on several lines; together they are one observation.
    return (
        _market("novig", athlete_id, name, game_id, line="10.5", label="POINTS"),
        _market("novig", athlete_id, name, game_id, line="12.5", label="POINTS"),
    )


DABBLE_MARKETS = (
    _dabble("1614f491-7bd5-44f5-bd12-5f4c065bf39d", "Robert Williams", POR, "0022600101"),
    _dabble("04616488-8c50-43f1-a03c-f5eef9fa19e4", "Bobby Portis", MIA, "0022600102"),
    _dabble("46416587-bb3f-47aa-9a48-5c7e425498bd", "Alexandre Sarr", WAS, "0022600103"),
    # Posted for the Lakers: no Laker is called Robert Williams anything.
    _dabble("rw-lal", "Robert Williams", LAL, "0022600101"),
    # Fits both suffixed Bulls, so neither is chosen.
    _dabble("hill-chi", "Marcus Hill", CHI, "0022600102"),
)
NOVIG_MARKETS = _novig("816ad2f9-563c-4d7e-b139-c88d87184c87", "Robert Williams", "0022600101")


def _snapshot(provider, markets):
    return ProviderSnapshot(
        provider=provider,
        status=SnapshotStatus.COMPLETE,
        markets=markets,
        coverage=CoverageEvidence(
            fetched_count=len(markets),
            eligible_count=len(markets),
            normalized_count=len(markets),
            pagination_complete=True,
            fanout_complete=True,
        ),
        retrieved_at=NOW,
    )


class _StaticProvider:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_snapshot(self, query, context):
        return self.snapshot


@pytest.fixture
def world(tmp_path):
    url = f"sqlite:///{tmp_path / 'name-variants.sqlite3'}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    run_migrations(engine)
    settings = RuntimeSettings(environment="testing", database={"url": url})
    athletes = AthleteMappingRepository(engine, clock=lambda: NOW)
    events = EventMappingRepository(engine, settings=settings)
    board = DFSBoardService(
        provider_registry={
            "dabble": _StaticProvider(_snapshot("dabble", DABBLE_MARKETS)),
            "novig": _StaticProvider(_snapshot("novig", NOVIG_MARKETS)),
        },
        athlete_resolver=AthleteResolver(_AthleteCatalog(), mapping_repository=athletes),
        athlete_mapping_repository=athletes,
        event_resolver=EventResolver(_EventCatalog(), mapping_repository=events, settings=settings),
        event_mapping_repository=events,
    )
    query = NBAMarketQuery(season=SEASON)
    scopes = [ProjectionArchiveReadScope(provider=name, query=query) for name in ("dabble", "novig")]
    coordinator = ProjectionCollectionCoordinator(
        engine,
        board_service=board,
        recording_service=ProjectionRecordingService(ProjectionArchive(engine, CATALOG), scopes),
        event_reader=lambda _season: tuple(
            {
                "nba_game_id": game_id,
                "scheduled_at": TIP,
                "status_text": "Scheduled",
                "status_code": 1,
                "postponed_status": None,
                "postponement_evidence": None,
            }
            for game_id in GAMES
        ),
        season=SEASON,
        settings=ProjectionCollectionSettings(
            slow_interval=timedelta(minutes=30),
            fast_interval=timedelta(minutes=5),
            pregame_horizon=timedelta(hours=24),
            fast_window=timedelta(hours=2),
            lease_duration=timedelta(minutes=1),
            backoff_base=timedelta(minutes=1),
            backoff_max=timedelta(hours=1),
        ),
        providers=("dabble", "novig"),
        clock=lambda: NOW,
        owner="name-variants",
    )
    return engine, athletes, coordinator


def _archived(engine):
    """``{(provider, provider athlete id): {(targetable, player id, game id), ...}}``."""

    with engine.connect() as connection:
        rows = connection.execute(select(ProjectionObservation)).all()
    archived: dict[tuple[str, str], set[tuple[bool, int | None, str | None]]] = {}
    for row in rows:
        archived.setdefault((row.provider, row.athlete_provider_id), set()).add(
            (bool(row.targetable), row.canonical_player_id, row.canonical_game_id)
        )
    return archived


def test_suffix_and_nickname_spellings_become_targetable_lines(world):
    engine, _, coordinator = world

    assert coordinator.run().status == "complete"

    archived = _archived(engine)
    assert archived[("dabble", "1614f491-7bd5-44f5-bd12-5f4c065bf39d")] == {
        (True, ROBERT_WILLIAMS, "0022600101")
    }
    assert archived[("dabble", "04616488-8c50-43f1-a03c-f5eef9fa19e4")] == {
        (True, BOBBY_PORTIS, "0022600102")
    }
    assert archived[("dabble", "46416587-bb3f-47aa-9a48-5c7e425498bd")] == {(True, ALEX_SARR, "0022600103")}


def test_a_line_with_no_athlete_team_resolves_on_its_events_two_rosters(world):
    engine, _, coordinator = world

    assert coordinator.run().status == "complete"

    assert _archived(engine)[("novig", "816ad2f9-563c-4d7e-b139-c88d87184c87")] == {
        (True, ROBERT_WILLIAMS, "0022600101")
    }


def test_a_name_with_no_fit_on_its_team_stays_unmatched_and_untargetable(world):
    engine, athletes, coordinator = world

    assert coordinator.run().status == "complete"

    assert _archived(engine)[("dabble", "rw-lal")] == {(False, None, "0022600101")}
    (decision,) = athletes.history(provider="dabble", provider_athlete_id="rw-lal")
    assert decision.decision_state == MappingResolutionState.UNMATCHED.value


def test_two_roster_players_differing_only_by_suffix_stay_ambiguous(world):
    engine, athletes, coordinator = world

    assert coordinator.run().status == "complete"

    assert _archived(engine)[("dabble", "hill-chi")] == {(False, None, "0022600102")}
    (decision,) = athletes.history(provider="dabble", provider_athlete_id="hill-chi")
    assert decision.decision_state == MappingResolutionState.AMBIGUOUS.value


def test_an_earlier_unmatched_decision_does_not_hold_back_the_next_poll(world):
    engine, athletes, coordinator = world
    # What every poll before this fix recorded for the Dabble Robert Williams.
    athletes.record_resolution(
        AthleteResolution(
            provider="dabble",
            provider_evidence=DABBLE_MARKETS[0].athlete,
            season=SEASON,
            state=MappingResolutionState.UNMATCHED,
            reason="unmatched",
            observed_at=NOW - timedelta(days=4),
        )
    )

    assert coordinator.run().status == "complete"

    assert _archived(engine)[("dabble", "1614f491-7bd5-44f5-bd12-5f4c065bf39d")] == {
        (True, ROBERT_WILLIAMS, "0022600101")
    }
    mapping = athletes.get_active_mapping("dabble", "1614f491-7bd5-44f5-bd12-5f4c065bf39d")
    assert (mapping.mapping_state, mapping.canonical_player_id) == ("auto", ROBERT_WILLIAMS)


class _UnderdogCatalog:
    ROWS: ClassVar = [
        {**_athlete_row(1626192, "Kelly Oubre Jr.", POR), "team_id": IND, "team_abbreviation": "IND"},
        {**_athlete_row(1630529, "Herbert Jones", POR), "team_id": NOP, "team_abbreviation": "NOP"},
        {**_athlete_row(1630540, "Miles McBride", POR), "team_id": NYK, "team_abbreviation": "NYK"},
        {**_athlete_row(1628981, "Luguentz Dort", POR), "team_id": ATL, "team_abbreviation": "ATL"},
    ]

    def get_catalog(self, season, active_only=False):
        return list(self.ROWS)


@pytest.mark.parametrize(
    ("posted", "tricode", "player_id"),
    [
        ("Kelly Oubre", "IND", 1626192),
        ("Herb Jones", "NOP", 1630529),
        ("Deuce McBride", "NYK", 1630540),
        ("Lu Dort", "ATL", 1628981),
    ],
)
def test_underdog_spellings_resolve_to_the_catalog_player_on_their_team(posted, tricode, player_id):
    resolution = AthleteResolver(_UnderdogCatalog()).resolve(
        "underdog",
        AthleteEvidence(provider_id=f"ud-{player_id}", name=posted, team=TeamEvidence(abbreviation=tricode)),
        SEASON,
    )

    assert (resolution.state, resolution.canonical_player_id) == (MappingResolutionState.AUTO, player_id)


def test_a_nickname_spelling_does_not_resolve_on_another_team():
    resolution = AthleteResolver(_UnderdogCatalog()).resolve(
        "underdog",
        AthleteEvidence(provider_id="ud-x", name="Lu Dort", team=TeamEvidence(abbreviation="NYK")),
        SEASON,
    )

    assert (resolution.state, resolution.canonical_player_id) == (MappingResolutionState.UNMATCHED, None)


def test_a_name_with_no_team_and_no_event_stays_unmatched():
    resolution = AthleteResolver(_AthleteCatalog()).resolve(
        "polymarket",
        AthleteEvidence(provider_id="bobby-portis", name="Bobby Portis"),
        SEASON,
    )

    assert (resolution.state, resolution.canonical_player_id) == (MappingResolutionState.UNMATCHED, None)
