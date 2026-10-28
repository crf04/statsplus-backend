"""GET /api/games/board: every posted line for one market across a slate (crf04/statsplus#373)."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sqlalchemy_event

from app.domain.statistics import MatchState, ScoringPeriod, StatisticMatch
from app.migrations import run_migrations
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
from app.providers.nba_injury_report import InjuryEntryEvidence, InjuryProviderSnapshot
from app.providers.registry import dfs_provider_names
from app.services.event_catalog_service import EventCatalogService
from app.services.injury_snapshot_repository import InjurySnapshotRepository
from app.services.matchup_injuries import MatchupInjuryService
from app.services.projection_archive import (
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
)
from app.services.slate_board import SlateBoardService
from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteReader, SportsbookQuoteStore
from app.services.statistic_catalog import StatisticCatalog

SEASON = "2025-26"
QUERY = NBAMarketQuery(season=SEASON)
NOW = datetime(2026, 1, 16, 0, 0, tzinfo=UTC)
#: Tipped 30 minutes ago: its pool is the closing set frozen at the tip.
STARTED = "0022500601"
STARTED_TIP = NOW - timedelta(minutes=30)
#: Tips in 30 minutes: its pool is the live one.
LATER = "0022500602"
LATER_TIP = NOW + timedelta(minutes=30)
#: The next ET date's game, never on the 2026-01-15 board.
TOMORROW = "0022500603"

GSW = TeamEvidence(canonical_id=1610612744, abbreviation="GSW")
LAL = TeamEvidence(canonical_id=1610612747, abbreviation="LAL")
BOS = TeamEvidence(canonical_id=1610612738, abbreviation="BOS")
NYK = TeamEvidence(canonical_id=1610612752, abbreviation="NYK")
PHX = TeamEvidence(canonical_id=1610612756, abbreviation="PHX")
CURRY = (201939, "Stephen Curry", GSW, STARTED)
LEBRON = (2544, "LeBron James", LAL, STARTED)
TATUM = (1628369, "Jayson Tatum", BOS, LATER)
BRUNSON = (1628973, "Jalen Brunson", NYK, LATER)
#: Boston's other two posted players; their ids run in a different order from their names.
BROWN = (1627759, "Jaylen Brown", BOS, LATER)
WHITE = (1628401, "Derrick White", BOS, LATER)
BOOKER = (1626164, "Devin Booker", PHX, TOMORROW)


class _NoProvider:
    def __getattr__(self, name):
        raise AssertionError(f"the board reached a provider: {name}")


def _event(game_id, away, home, tip):
    return {
        "nba_game_id": game_id,
        "season": SEASON,
        "home_team_id": home.canonical_id,
        "home_team_name": home.abbreviation,
        "home_team_tricode": home.abbreviation,
        "away_team_id": away.canonical_id,
        "away_team_name": away.abbreviation,
        "away_team_tricode": away.abbreviation,
        "scheduled_at": tip,
        "status_text": "Scheduled",
        "status_code": 1,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
    }


def _market(catalog, provider, player, statistic_id, threshold, prices=(-110, None)):
    player_id, name, team, game_id = player
    statistic = catalog.by_id[statistic_id]
    evidence = StatisticEvidence(provider_id=statistic_id, canonical_id=statistic.id)
    over, under = prices
    return PlayerProjectionMarket(
        provider=provider,
        market_id=f"{provider}-{player_id}-{statistic_id}",
        athlete=AthleteEvidence(canonical_id=player_id, name=name, team=team),
        event=EventEvidence(canonical_id=game_id),
        team=team,
        statistic=evidence,
        statistic_match=StatisticMatch(
            state=MatchState.CANONICAL,
            evidence=evidence,
            scoring_period=ScoringPeriod.FULL_GAME,
            canonical=statistic,
            provider=provider,
        ),
        threshold=MarketThreshold(threshold, "count"),
        status=MarketStatus.AVAILABLE,
        variant=MarketVariant.STANDARD,
        scoring_period=ScoringPeriod.FULL_GAME,
        selections=tuple(
            Selection(selection_id=f"{direction.value}", direction=direction, american_price=price)
            for direction, price in ((SelectionDirection.HIGHER, over), (SelectionDirection.LOWER, under))
            if price is not None
        ),
    )


def _snapshot(provider, markets, retrieved_at):
    return ProviderSnapshot(
        provider=provider,
        status=SnapshotStatus.COMPLETE,
        markets=tuple(markets),
        coverage=CoverageEvidence(
            fetched_count=len(markets),
            eligible_count=len(markets),
            normalized_count=len(markets),
            expected_total=len(markets),
        ),
        retrieved_at=retrieved_at,
    )


def _quote(player, book, line, over, under, *, category="PTS", stamp=NOW - timedelta(minutes=3)):
    return ResolvedQuote(player[0], category, book, line, over, under, stamp)


def _injuries(engine, events):
    class RecordedReport:
        def get_snapshot(self):
            return InjuryProviderSnapshot(
                raw_payload=[],
                entries=tuple(
                    InjuryEntryEvidence(
                        f"nba_official:{player[0]}",
                        str(player[0]),
                        player[1],
                        player[2].abbreviation,
                        status,
                        status,
                        "Left ankle soreness",
                        "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-15_06_30PM.pdf",
                        game_date="2026-01-15",
                        matchup=matchup,
                    )
                    for player, status, matchup in (
                        (BRUNSON, "Out", "BOS@NYK"),
                        (TATUM, "Questionable", "BOS@NYK"),
                        (LEBRON, "Available", "GSW@LAL"),
                    )
                ),
                retrieved_at=NOW - timedelta(minutes=40),
            )

    service = MatchupInjuryService(
        provider=RecordedReport(),
        snapshot_repository=InjurySnapshotRepository(engine),
        athlete_catalog=SimpleNamespace(
            get_catalog=lambda season, active_only=False: [
                {
                    "player_id": player[0],
                    "display_name": player[1],
                    "team_abbreviation": player[2].abbreviation,
                }
                for player in (CURRY, LEBRON, TATUM, BRUNSON)
            ]
        ),
        enabled=True,
        clock=lambda: NOW,
    )
    service.refresh_report(events=events, season=SEASON)
    return service


def seed_board(engine, dependencies):
    """A 2026-01-15 slate on ``engine``: GSW @ LAL tipped 30 minutes ago, BOS @ NYK tips in 30."""

    catalog = StatisticCatalog.load_default()
    settings = dependencies.settings.model_copy(
        update={"nba": dependencies.settings.nba.model_copy(update={"current_season": SEASON})}
    )
    schedule = EventCatalogService(
        engine, nba_stats_provider=_NoProvider(), settings=settings, clock=lambda: NOW
    )
    schedule.repository.publish(
        SEASON,
        pd.DataFrame(
            [
                _event(STARTED, GSW, LAL, STARTED_TIP),
                _event(LATER, BOS, NYK, LATER_TIP),
                _event(TOMORROW, PHX, LAL, NOW + timedelta(days=1)),
            ]
        ),
        NOW - timedelta(hours=1),
    )
    archive = ProjectionArchive(engine, catalog)
    pre_tip = STARTED_TIP - timedelta(minutes=10)
    archive.ingest_complete_snapshot(
        _snapshot(
            "dabble",
            (
                _market(catalog, "dabble", CURRY, "points", "29.5", (-122, -104)),
                _market(catalog, "dabble", CURRY, "three_pointers_attempted", "11.5", (-118, -108)),
                _market(catalog, "dabble", LEBRON, "points", "24.5", (-115, -111)),
            ),
            pre_tip,
        ),
        query=QUERY,
        accepted_at=pre_tip,
    )
    archive.freeze_closing_projection_set(
        provider="dabble",
        query=QUERY,
        canonical_game_id=STARTED,
        started_at=STARTED_TIP,
        created_at=STARTED_TIP,
    )
    # After the tip Dabble moves Curry's line; the started game still reads its closing set.
    archive.ingest_complete_snapshot(
        _snapshot(
            "dabble",
            (
                _market(catalog, "dabble", CURRY, "points", "31.5", (-130, 100)),
                _market(catalog, "dabble", TATUM, "points", "27.5", (-122, -104)),
                _market(catalog, "dabble", TATUM, "three_pointers_attempted", "8.5", (-125, None)),
                _market(catalog, "dabble", BRUNSON, "points", "26.5", (-120, -106)),
                _market(catalog, "dabble", BROWN, "points", "23.5", (-115, -105)),
                _market(catalog, "dabble", WHITE, "points", "15.5", (-110, -110)),
                _market(catalog, "dabble", BOOKER, "points", "25.5", (-120, -106)),
            ),
            NOW - timedelta(minutes=2),
        ),
        query=QUERY,
        accepted_at=NOW - timedelta(minutes=2),
    )
    # PrizePicks last polled 20 minutes ago: past the 15-minute live window, so stale-served.
    archive.ingest_complete_snapshot(
        _snapshot(
            "prizepicks",
            (
                _market(catalog, "prizepicks", TATUM, "points", "28.5"),
                _market(catalog, "prizepicks", BRUNSON, "points", "26.5"),
            ),
            NOW - timedelta(minutes=20),
        ),
        query=QUERY,
        accepted_at=NOW - timedelta(minutes=20),
    )
    store = SportsbookQuoteStore(engine)
    for game_id, quotes, closing in (
        (
            STARTED,
            [_quote(CURRY, "draftkings", 29.5, -115, -105, stamp=STARTED_TIP - timedelta(minutes=1))],
            True,
        ),
        (
            LATER,
            [
                _quote(TATUM, "draftkings", 27.5, -128, 100),
                _quote(TATUM, "fanduel", 28.5, -105, -115, stamp=NOW - timedelta(minutes=6)),
                _quote(TATUM, "draftkings", 3.5, -140, 110, category="3PM"),
            ],
            False,
        ),
        (TOMORROW, [_quote(BOOKER, "betmgm", 25.5, -110, -110)], False),
    ):
        store.record_poll(
            odds_event_id=f"odds-{game_id}",
            game_id=game_id,
            started_at=NOW - timedelta(minutes=3),
            completed_at=NOW - timedelta(minutes=3),
            cost=1,
            remaining=100,
            is_closing=closing,
            quotes=quotes,
        )
    events = schedule.get_events_by_ids(SEASON, (STARTED, LATER))
    pool = LatestProjectionPlayerPoolReader(
        engine,
        tuple(
            ProjectionArchiveReadScope(provider=provider, query=QUERY) for provider in dfs_provider_names()
        ),
        required_providers=("dabble", "prizepicks"),
        clock=lambda: NOW,
        event_reader=schedule,
    )
    dependencies.slate_board_service = SlateBoardService(
        event_catalog=schedule,
        player_pool=pool,
        injuries=_injuries(engine, events),
        sportsbook_reader=SportsbookQuoteReader(engine),
        statistic_catalog=catalog,
        settings=settings,
        engine=engine,
        clock=lambda: NOW,
    )
    return SimpleNamespace(engine=engine, service=dependencies.slate_board_service)


@pytest.fixture
def board(tmp_path, dependencies):
    engine = create_engine(f"sqlite:///{tmp_path / 'board.sqlite3'}")
    run_migrations(engine)
    return seed_board(engine, dependencies)


GAMES = [
    {"game_id": STARTED, "label": "GSW @ LAL", "tip": "2026-01-15T23:30:00Z", "status": "live"},
    {"game_id": LATER, "label": "BOS @ NYK", "tip": "2026-01-16T00:30:00Z", "status": "scheduled"},
]
SOURCES = [
    {"source": "dabble", "kind": "provider", "status": "fresh", "retrieved_at": "2026-01-15T23:58:00Z"},
    {
        "source": "prizepicks",
        "kind": "provider",
        "status": "stale-served",
        "retrieved_at": "2026-01-15T23:40:00Z",
    },
    {"source": "draftkings", "kind": "sportsbook", "status": "fresh", "retrieved_at": "2026-01-15T23:57:00Z"},
    {"source": "fanduel", "kind": "sportsbook", "status": "fresh", "retrieved_at": "2026-01-15T23:54:00Z"},
]


def _get(client, query):
    response = client.get(f"/api/games/board?{query}")
    return response.status_code, response.get_json()


def test_the_index_lists_each_posted_market_with_its_players_and_sources(board, client):
    assert _get(client, "date=2026-01-15") == (
        200,
        {
            "date": "2026-01-15",
            "season": SEASON,
            "games": GAMES,
            "sources": SOURCES,
            "markets": [
                {"market": "PTS", "players": 6, "sources": ["dabble", "prizepicks", "draftkings", "fanduel"]},
                {"market": "3PM", "players": 1, "sources": ["draftkings"]},
                {"market": "FG3A", "players": 2, "sources": ["dabble"]},
            ],
        },
    )


CURRY_PTS = {
    "player_id": 201939,
    "name": "Stephen Curry",
    "team": "GSW",
    "injury_status": None,
    "lines": {"dabble": 29.5},
    "line_prices": {"dabble": {"over": -122, "under": -104}},
    "book_lines": {
        "as_of": "2026-01-15T23:29:00Z",
        "closing": True,
        "books": [
            {
                "book": "draftkings",
                "line": 29.5,
                "over": -115,
                "under": -105,
                "updated_at": "2026-01-15T23:29:00Z",
            }
        ],
    },
}
LEBRON_PTS = {
    "player_id": 2544,
    "name": "LeBron James",
    "team": "LAL",
    "injury_status": None,
    "lines": {"dabble": 24.5},
    "line_prices": {"dabble": {"over": -115, "under": -111}},
    "book_lines": None,
}
TATUM_PTS = {
    "player_id": 1628369,
    "name": "Jayson Tatum",
    "team": "BOS",
    "injury_status": "Questionable",
    "lines": {"dabble": 27.5, "prizepicks": 28.5},
    "line_prices": {"dabble": {"over": -122, "under": -104}},
    "book_lines": {
        "as_of": "2026-01-15T23:57:00Z",
        "closing": False,
        "books": [
            {
                "book": "draftkings",
                "line": 27.5,
                "over": -128,
                "under": 100,
                "updated_at": "2026-01-15T23:57:00Z",
            },
            {
                "book": "fanduel",
                "line": 28.5,
                "over": -105,
                "under": -115,
                "updated_at": "2026-01-15T23:54:00Z",
            },
        ],
    },
}
BROWN_PTS = {
    "player_id": 1627759,
    "name": "Jaylen Brown",
    "team": "BOS",
    "injury_status": None,
    "lines": {"dabble": 23.5},
    "line_prices": {"dabble": {"over": -115, "under": -105}},
    "book_lines": None,
}
WHITE_PTS = {
    "player_id": 1628401,
    "name": "Derrick White",
    "team": "BOS",
    "injury_status": None,
    "lines": {"dabble": 15.5},
    "line_prices": {"dabble": {"over": -110, "under": -110}},
    "book_lines": None,
}
BRUNSON_PTS = {
    "player_id": 1628973,
    "name": "Jalen Brunson",
    "team": "NYK",
    "injury_status": "Out",
    "lines": {"dabble": 26.5, "prizepicks": 26.5},
    "line_prices": {"dabble": {"over": -120, "under": -106}},
    "book_lines": None,
}


def _games(*players_by_game):
    return [{**game, "players": players} for game, players in zip(GAMES, players_by_game, strict=True)]


def test_a_sportsbook_market_carries_every_provider_and_book_per_game(board, client):
    assert _get(client, "date=2026-01-15&market=PTS") == (
        200,
        {
            "date": "2026-01-15",
            "season": SEASON,
            "games": _games([CURRY_PTS, LEBRON_PTS], [WHITE_PTS, BROWN_PTS, TATUM_PTS, BRUNSON_PTS]),
            "sources": SOURCES,
            "players_total": 6,
        },
    )


def test_players_are_ordered_by_team_then_name(board, client):
    status, body = _get(client, f"date=2026-01-15&market=PTS&game={LATER}")

    assert status == 200
    assert [(row["team"], row["name"]) for row in body["games"][0]["players"]] == [
        ("BOS", "Derrick White"),
        ("BOS", "Jaylen Brown"),
        ("BOS", "Jayson Tatum"),
        ("NYK", "Jalen Brunson"),
    ]


def test_a_dfs_only_market_has_no_book_lines(board, client):
    assert _get(client, "date=2026-01-15&market=FG3A") == (
        200,
        {
            "date": "2026-01-15",
            "season": SEASON,
            "games": _games(
                [
                    {
                        "player_id": 201939,
                        "name": "Stephen Curry",
                        "team": "GSW",
                        "injury_status": None,
                        "lines": {"dabble": 11.5},
                        "line_prices": {"dabble": {"over": -118, "under": -108}},
                        "book_lines": None,
                    }
                ],
                [
                    {
                        "player_id": 1628369,
                        "name": "Jayson Tatum",
                        "team": "BOS",
                        "injury_status": "Questionable",
                        "lines": {"dabble": 8.5},
                        "line_prices": {"dabble": {"over": -125, "under": None}},
                        "book_lines": None,
                    }
                ],
            ),
            "sources": SOURCES,
            "players_total": 2,
        },
    )


def test_a_started_game_serves_its_closing_set_not_the_line_posted_after_tip(board, client):
    status, body = _get(client, f"date=2026-01-15&market=PTS&game={STARTED}")

    assert status == 200
    assert body["games"] == [{**GAMES[0], "players": [CURRY_PTS, LEBRON_PTS]}]
    assert body["players_total"] == 2


def test_the_team_filter_keeps_one_teams_rows_and_an_out_player_stays_in(board, client):
    status, body = _get(client, "date=2026-01-15&market=PTS&team=nyk")

    assert status == 200
    assert body["games"] == [{**GAMES[1], "players": [BRUNSON_PTS]}]
    assert body["players_total"] == 1


def test_the_sources_filter_narrows_rows_lines_books_and_the_source_list(board, client):
    status, body = _get(client, "date=2026-01-15&market=PTS&sources=prizepicks,fanduel")

    assert status == 200
    assert body["sources"] == [SOURCES[1], SOURCES[3]]
    assert body["games"] == _games(
        [],
        [
            {
                **TATUM_PTS,
                "lines": {"prizepicks": 28.5},
                "line_prices": {},
                "book_lines": {
                    "as_of": "2026-01-15T23:54:00Z",
                    "closing": False,
                    "books": [TATUM_PTS["book_lines"]["books"][1]],
                },
            },
            {**BRUNSON_PTS, "lines": {"prizepicks": 26.5}, "line_prices": {}},
        ],
    )
    assert body["players_total"] == 2


def test_the_index_counts_only_the_filtered_game_and_sources(board, client):
    status, body = _get(client, f"date=2026-01-15&game={LATER}&sources=draftkings")

    assert status == 200
    assert body["games"] == [GAMES[1]]
    assert body["markets"] == [
        {"market": "PTS", "players": 1, "sources": ["draftkings"]},
        {"market": "3PM", "players": 1, "sources": ["draftkings"]},
    ]


def test_a_date_with_no_games_is_an_empty_board(board, client):
    empty = {"date": "2026-01-14", "season": SEASON, "games": [], "sources": []}

    assert _get(client, "date=2026-01-14") == (200, {**empty, "markets": []})
    assert _get(client, "date=2026-01-14&market=PTS") == (200, {**empty, "players_total": 0})


@pytest.mark.parametrize(
    ("query", "parameters"),
    [
        ("date=2026-01-15&market=XYZ", ["market"]),
        ("date=2026-01-15&market=pts", ["market"]),
        ("market=PTS", ["date"]),
        ("date=2026-1-15", ["date"]),
        ("date=2026-01-15&team=XXX", ["team"]),
        ("date=2026-01-15&game=601", ["game"]),
        ("date=2026-01-15&sources=dabble,offshore", ["sources"]),
        ("date=2026-01-15&market=PTS&market=REB", ["market"]),
        ("date=2026-01-15&player=2544", ["player"]),
    ],
)
def test_invalid_parameters_are_400_invalid_input(board, client, query, parameters):
    status, body = _get(client, query)

    assert status == 400
    assert body["error"]["code"] == "invalid_input"
    assert body["error"]["details"] == {"parameters": parameters}


def test_the_board_requires_authentication_before_reading(client, dependencies, monkeypatch):
    dependencies.slate_board_service = Mock()
    monkeypatch.setattr("app.utils.auth.get_firebase_app", lambda: object())

    status, body = _get(client, "date=2026-01-15&market=PTS")

    assert (status, body["error"]["code"]) == (401, "authentication_required")
    dependencies.slate_board_service.get_board.assert_not_called()


#: Four more 2026-01-15 games, none of them posted.
EXTRA_GAMES = [
    _event(f"00225006{index:02d}", away, home, LATER_TIP + timedelta(minutes=30 * index))
    for index, (away, home) in enumerate(
        (
            (
                TeamEvidence(canonical_id=1610612748, abbreviation="MIA"),
                TeamEvidence(canonical_id=1610612753, abbreviation="ORL"),
            ),
            (
                TeamEvidence(canonical_id=1610612743, abbreviation="DEN"),
                TeamEvidence(canonical_id=1610612762, abbreviation="UTA"),
            ),
            (
                TeamEvidence(canonical_id=1610612742, abbreviation="DAL"),
                TeamEvidence(canonical_id=1610612745, abbreviation="HOU"),
            ),
            (
                TeamEvidence(canonical_id=1610612749, abbreviation="MIL"),
                TeamEvidence(canonical_id=1610612741, abbreviation="CHI"),
            ),
        ),
        start=4,
    )
]


def test_the_slate_is_read_in_the_same_statements_whatever_its_game_count(board, client):
    statements = []
    sqlalchemy_event.listen(
        board.engine, "before_cursor_execute", lambda *arguments: statements.append(arguments[2])
    )

    def reads():
        statements.clear()
        status, body = _get(client, "date=2026-01-15&market=PTS")
        assert status == 200
        return len(body["games"]), Counter(
            table for statement in statements for table in re.findall(r"FROM (\w+)", statement)
        )

    two_games, two_reads = reads()
    board.service.event_catalog.repository.publish(
        SEASON, pd.DataFrame(EXTRA_GAMES), NOW - timedelta(hours=1)
    )
    six_games, six_reads = reads()

    assert (two_games, six_games) == (2, 6)
    assert two_reads == six_reads
    assert two_reads == {
        "event_catalog": 2,
        "latest_player_projections": 1,
        "projection_provider_polls": 1,
        # One closing-set lookup per DFS provider, never per game.
        "projection_closing_sets": len(dfs_provider_names()),
        "projection_closing_memberships": 1,
        "injury_snapshots": 1,
        "sportsbook_prop_latest": 1,
    }
