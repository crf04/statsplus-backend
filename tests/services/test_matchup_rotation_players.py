"""Rotation Players on a scheduled Matchup (crf04/statsplus#459).

A player with no line in CourtAI's books still gets a row when he played 10 or
more minutes for his team in one of its last 5 games, the active Athlete
Catalog places him on that team, and the stored injury report does not list
him Out.  The scenario runs over a real composed ``player_game_logs``
publication and the real Athlete Catalog reader, so the team-rows statement
and the catalog read are the production ones; ``world`` takes any engine, so
the Postgres integration test runs the same scenario.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.services.athlete_catalog_service import AthleteCatalogReader
from app.services.collection_control import PublicationService
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.matchup import (
    MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
    MATCHUP_PUBLICATION_STREAM_KEYS,
    MatchupService,
)
from app.services.matchup_injuries import MatchupInjuryResult
from app.services.player_diet import PlayerDietResult, StoredPlayerDietFact
from app.services.player_game_log_repository import PlayerGameLogRecord, PlayerGameLogRepository
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteReader, SportsbookQuoteStore
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import StatsFreshness
from tests.services.test_matchup_service import (
    BOS,
    GAME_ID,
    LAL,
    NOW,
    RETRIEVED_AT,
    SEASON,
    RecordedEvents,
    RecordedInjuries,
    RecordedPool,
    RecordedTeamWindows,
    _final_event,
    _window,
)

MIA = 1610612748
NYK = 1610612752
LEBRON = 2544
ROTATION = 1001
OUT = 1002
TRADED_AWAY = 1003
DEEP_BENCH = 1004
TRADED_IN = 1005
QUESTIONABLE = 1006
BOS_ROTATION = 1007
TWO_TEAMS = 1008
INACTIVE = 1010
EXACT_TEN = 1011

SCORED_MARKETS = [
    "3PM",
    "AST",
    "BLK",
    "FG2A",
    "FG2M",
    "FG3A",
    "FGA",
    "FGM",
    "FTA",
    "FTM",
    "PA",
    "PR",
    "PRA",
    "PTS",
    "RA",
    "REB",
    "STKS",
    "STL",
    "TOV",
]

#: LAL's six games against MIA before the focal game (tip 2026-01-15 ET),
#: oldest first; the first is the one a five-game window leaves out.
LAL_GAMES = [(f"00225001{n:02d}", f"2026-01-{day:02d}") for n, day in enumerate((3, 5, 7, 9, 11, 13), 1)]
#: BOS's five games against NYK.
BOS_GAMES = [(f"00225002{n:02d}", f"2026-01-{day:02d}") for n, day in enumerate((4, 6, 8, 10, 12), 1)]


def _row(player_id, name, team_id, opponent_id, game, minutes, points):
    game_id, game_date = game
    return {
        "season": SEASON,
        "season_type": "Regular Season",
        "player_id": player_id,
        "game_id": game_id,
        "player_name": name,
        "game_date": game_date,
        "team_id": team_id,
        "team_tricode": NBA_TEAM_ID_TO_TRICODE[team_id],
        "opponent_team_id": opponent_id,
        "opponent_team_tricode": NBA_TEAM_ID_TO_TRICODE[opponent_id],
        "is_home": False,
        "minutes": minutes,
        "points": points,
        "rebounds": 3,
        "assists": 2,
        "field_goals_made": 3,
        "field_goals_attempted": 7,
        "three_pointers_made": 1,
        "three_pointers_attempted": 3,
        "free_throws_made": 1,
        "free_throws_attempted": 2,
        "offensive_rebounds": 1,
        "defensive_rebounds": 2,
        "turnovers": 1,
        "steals": 1,
        "blocks": 0,
        "personal_fouls": 2,
    }


def _season_rows(*, bos_games=True):
    rows = []
    for game in LAL_GAMES:
        # The opponent's row is what names the game as one of LAL's.
        rows.append(_row(9001, "Heat Starter", MIA, LAL, game, 30.0, 14))
        rows.append(_row(LEBRON, "LeBron James", LAL, MIA, game, 34.0, 25))
        rows.append(_row(TRADED_IN, "Traded In", MIA, LAL, game, 30.0, 11))
    last = LAL_GAMES[-1]
    rows.append(_row(ROTATION, "Rotation Guy", LAL, MIA, last, 12.0, 5))
    rows.append(_row(QUESTIONABLE, "Questionable Guy", LAL, MIA, last, 15.0, 30))
    rows.append(_row(EXACT_TEN, "Exactly Ten", LAL, MIA, last, 10.0, 4))
    rows.append(_row(TWO_TEAMS, "Two Teams", LAL, MIA, last, 15.0, 6))
    rows.append(_row(INACTIVE, "Inactive Guy", LAL, MIA, last, 18.0, 7))
    for game in LAL_GAMES[1:]:
        rows.append(_row(OUT, "Out Guy", LAL, MIA, game, 20.0, 9))
        rows.append(_row(TRADED_AWAY, "Traded Away", LAL, MIA, game, 25.0, 10))
        rows.append(_row(DEEP_BENCH, "Deep Bench", LAL, MIA, game, 9.9, 3))
    rows.append(_row(DEEP_BENCH, "Deep Bench", LAL, MIA, LAL_GAMES[0], 30.0, 3))
    if bos_games:
        for game in BOS_GAMES:
            rows.append(_row(9002, "Knicks Starter", NYK, BOS, game, 30.0, 14))
        rows.append(_row(BOS_ROTATION, "Celtics Rotation", BOS, NYK, BOS_GAMES[-1], 22.0, 12))
        # Played for BOS, then was traded to LAL and played for it.
        rows.append(_row(TWO_TEAMS, "Two Teams", BOS, NYK, BOS_GAMES[-1], 20.0, 6))
    return rows


#: (player_id, name, team_id, active for the season)
CATALOG = (
    (LEBRON, "LeBron James", LAL, True),
    (ROTATION, "Rotation Guy", LAL, True),
    (OUT, "Out Guy", LAL, True),
    (TRADED_AWAY, "Traded Away", MIA, True),
    (DEEP_BENCH, "Deep Bench", LAL, True),
    (TRADED_IN, "Traded In", LAL, True),
    (QUESTIONABLE, "Questionable Guy", LAL, True),
    (BOS_ROTATION, "Celtics Rotation", BOS, True),
    (TWO_TEAMS, "Two Teams", LAL, True),
    (INACTIVE, "Inactive Guy", LAL, False),
    (EXACT_TEN, "Exactly Ten", LAL, True),
)


class AnyPlayerDiets:
    """One stored Transition share for whichever players are asked about."""

    def get_for_players(self, season, player_ids):
        return PlayerDietResult(
            season=season,
            players={
                int(player_id): (
                    StoredPlayerDietFact(
                        int(player_id),
                        "play_types",
                        "Transition",
                        0.2,
                        40.0,
                        5,
                        "possessions",
                        "nba_synergy",
                        RETRIEVED_AT,
                    ),
                )
                for player_id in player_ids
            },
            observations=(),
        )


def _pool():
    return PlayerPool(
        players=(
            PoolPlayer(
                LEBRON,
                "LeBron James",
                LAL,
                ("PTS",),
                {"prizepicks": ("PTS",)},
                lines={"prizepicks": {"PTS": 26.5}},
            ),
        ),
        team_counts={LAL: 1},
        freshness={"status": "fresh", "retrieved_at": RETRIEVED_AT.isoformat(), "providers": {}},
    )


def _injuries(out=frozenset({OUT}), badges=None):
    block = {
        "status": "fresh",
        "unavailable_reason": None,
        "retrieved_at": RETRIEVED_AT.isoformat(),
        "source": "nba_official",
        "source_url": "https://official.nba.com/nba-injury-report-2025-26-season/",
        "teams": [],
    }
    return RecordedInjuries(
        MatchupInjuryResult(
            block,
            frozenset(out),
            badges if badges is not None else {OUT: "nba_official:1", QUESTIONABLE: "nba_official:2"},
        )
    )


#: A sportsbook quote for a player no DFS source lists: still a Rotation Player.
BOOK_QUOTE = {"PTS": {"as_of": "2026-01-15T11:00:00Z", "closing": False, "books": []}}


class Books:
    def book_lines(self, game_id, *, now, started, connection=None):
        return {ROTATION: BOOK_QUOTE}


@dataclass
class World:
    engine: object
    logs: PlayerGameLogRepository
    reader: DatabaseFirstPublicationReader

    def service(self, *, catalog=True, events=None, injuries=None, pool=None):
        service = MatchupService(
            event_catalog=events or RecordedEvents(),
            player_pool=RecordedPool(pool or _pool()),
            player_logs=self.logs,
            player_diets=AnyPlayerDiets(),
            team_matchups=RecordedTeamWindows(_window(), _window(last_15=True)),
            stats_freshness=SimpleNamespace(get=lambda: StatsFreshness(RETRIEVED_AT)),
            injuries=injuries if injuries is not None else _injuries(),
            settings=RuntimeSettings(environment="testing", nba=NBASeasonSettings(current_season=SEASON)),
            clock=lambda: NOW,
            publication_reader=self.reader,
            statistic_catalog=StatisticCatalog.load_default(),
            athlete_catalog=AthleteCatalogReader(self.engine) if catalog else None,
            sportsbook_reader=Books(),
        )
        return service


def world(engine, rows=None):
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    publications.register_stream(
        "player_game_logs",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="cutoff_current",
    )
    publications.compose(
        "player_game_logs",
        season=SEASON,
        cutoff=RETRIEVED_AT,
        payload={"rows": _season_rows() if rows is None else rows},
    )
    with engine.begin() as connection:
        connection.execute(
            AthleteCatalog.__table__.insert(),
            [
                {
                    "season": SEASON,
                    "player_id": player_id,
                    "display_name": name,
                    "roster_status": "active" if active else "inactive",
                    "is_active": active,
                    "is_active_for_season": active,
                    "team_id": team_id,
                    "team_abbreviation": NBA_TEAM_ID_TO_TRICODE[team_id],
                    "published_at": RETRIEVED_AT,
                }
                for player_id, name, team_id, active in CATALOG
            ],
        )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    logs = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: NOW,
        stats_surface_max_age=timedelta(hours=30),
        publication_reader=reader,
    )
    return World(engine, logs, reader)


def _sqlite_world(tmp_path, rows=None):
    return world(create_engine(f"sqlite:///{tmp_path / 'rotation.sqlite3'}"), rows)


def _sources(payload):
    return [(row["canonical_id"], row["tricode"], row["player_source"]) for row in payload["players"]]


def assert_rotation_scenario(served):
    payload = served.service().get_matchup(game_id=GAME_ID)

    # Pool rows first, even below a Rotation Player who scores more, then
    # Rotation Players by season scoring; exactly 10 minutes is enough.
    assert _sources(payload) == [
        (LEBRON, "LAL", "player_pool"),
        (QUESTIONABLE, "LAL", "rotation"),
        (BOS_ROTATION, "BOS", "rotation"),
        (TWO_TEAMS, "LAL", "rotation"),
        (ROTATION, "LAL", "rotation"),
        (EXACT_TEN, "LAL", "rotation"),
    ]
    rotation = next(row for row in payload["players"] if row["canonical_id"] == ROTATION)
    assert {key: rotation[key] for key in _LINE_FIELDS} == {
        "player_source": "rotation",
        "name": "Rotation Guy",
        "team_id": LAL,
        "stat_categories": [],
        "posted_markets": [],
        "provenance": {},
        "lines": {},
        "line_prices": {},
        "line_labels": {},
        "book_lines": BOOK_QUOTE,
        "season_averages": {},
        "season_scoring": 5.0,
        "last_10_minutes": [12.0],
        "injury_badge_ref": None,
        "focal_game_line": None,
    }
    assert rotation["last_five_games"] == [
        {
            "game_id": "0022500106",
            "game_date": "2026-01-13",
            "matchup": "LAL @ MIA",
            "minutes": 12.0,
            "stats": {},
        }
    ]
    assert sorted(rotation["scores"]) == SCORED_MARKETS
    assert sorted(rotation["scores"]["PTS"]["season"]["components"]) == ["play_types"]
    assert rotation["diet_shares"]["play_types"][0]["key"] == "Transition"
    questionable = next(row for row in payload["players"] if row["canonical_id"] == QUESTIONABLE)
    assert questionable["injury_badge_ref"] == "nba_official:2"
    # The targetable counts and the declared experience stay the pool's.
    assert payload["game"]["away_team"]["targetable_player_count"] == 1
    assert payload["game"]["home_team"]["targetable_player_count"] == 0
    assert payload["experience"]["player_source"] == "player_pool"
    assert payload["experience"]["sections"]["participants"]["source"] == "player_pool"


_LINE_FIELDS = (
    "player_source",
    "name",
    "team_id",
    "stat_categories",
    "posted_markets",
    "provenance",
    "lines",
    "line_prices",
    "line_labels",
    "book_lines",
    "season_averages",
    "season_scoring",
    "last_10_minutes",
    "injury_badge_ref",
    "focal_game_line",
)


def test_a_scheduled_matchup_lists_each_rotation_player_after_the_pool(tmp_path):
    """Out, traded away, traded in without minutes, inactive, or under 10
    minutes in each of the last 5 games: absent.  The pool player is listed
    once, and the player on both teams' logs once, for his catalog team."""

    assert_rotation_scenario(_sqlite_world(tmp_path))


def test_a_pool_player_listed_out_is_not_listed_again_as_a_rotation_player(tmp_path):
    served = _sqlite_world(tmp_path)

    payload = served.service(injuries=_injuries(out={OUT, LEBRON})).get_matchup(game_id=GAME_ID)

    assert LEBRON not in [row["canonical_id"] for row in payload["players"]]
    assert payload["game"]["away_team"]["targetable_player_count"] == 0


def test_a_team_without_a_game_this_season_adds_no_one(tmp_path):
    served = _sqlite_world(tmp_path, _season_rows(bos_games=False))

    payload = served.service().get_matchup(game_id=GAME_ID)

    assert [row for row in _sources(payload) if row[1] == "BOS"] == []
    assert [row[0] for row in _sources(payload)] == [LEBRON, QUESTIONABLE, TWO_TEAMS, ROTATION, EXACT_TEN]


def test_a_rotation_player_is_scored_exactly_as_he_would_be_from_the_pool(tmp_path):
    served = _sqlite_world(tmp_path)
    pool = _pool()
    posted = PlayerPool(
        players=(
            *pool.players,
            PoolPlayer(
                ROTATION,
                "Rotation Guy",
                LAL,
                ("PTS",),
                {"prizepicks": ("PTS",)},
                lines={"prizepicks": {"PTS": 4.5}},
            ),
        ),
        team_counts={LAL: 2},
        freshness=pool.freshness,
    )

    def row(payload):
        return next(row for row in payload["players"] if row["canonical_id"] == ROTATION)

    rotation = row(served.service().get_matchup(game_id=GAME_ID))
    pooled = row(served.service(pool=posted).get_matchup(game_id=GAME_ID))

    assert (rotation["player_source"], pooled["player_source"]) == ("rotation", "player_pool")
    assert rotation["scores"] == pooled["scores"]
    assert rotation["diet_shares"] == pooled["diet_shares"]


def test_without_an_athlete_catalog_the_matchup_is_todays(tmp_path):
    served = _sqlite_world(tmp_path)

    payload = served.service(catalog=False).get_matchup(game_id=GAME_ID)

    assert _sources(payload) == [(LEBRON, "LAL", "player_pool")]


def test_a_historical_matchup_is_the_same_with_the_catalog_wired(tmp_path):
    served = _sqlite_world(tmp_path)
    final = _final_event()
    empty_pool = PlayerPool((), {}, PlayerPool.unavailable_freshness())

    def compose(catalog):
        return served.service(
            catalog=catalog,
            events=RecordedEvents(events=[final]),
            pool=empty_pool,
        ).get_matchup(game_id=GAME_ID)

    with_catalog = compose(True)

    assert with_catalog["experience"]["mode"] == "historical"
    assert {row["player_source"] for row in with_catalog["players"]} <= {"game_logs"}
    assert with_catalog == compose(False)


def test_the_team_rows_read_keeps_each_teams_last_five_games_before_a_date(tmp_path):
    """The indexed projection and the rendered payload agree on the window."""

    served = _sqlite_world(tmp_path)
    snapshot = served.reader.snapshot(
        MATCHUP_PUBLICATION_STREAM_KEYS,
        season=SEASON,
        projection_only_keys=MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
    )
    before = LAL_GAMES[-1][1]

    def read(publication_snapshot):
        rows = served.logs.list_team_recent_rows(
            SEASON,
            LAL,
            before_date=date.fromisoformat(before),
            games=3,
            publication_snapshot=publication_snapshot,
        )
        return [(row.game_id, row.player_id) for row in rows]

    expected = sorted(
        (game_id, player_id)
        for game_id in ("0022500103", "0022500104", "0022500105")
        for player_id in (LEBRON, OUT, TRADED_AWAY, DEEP_BENCH)
    )
    projected = read(snapshot)
    assert sorted(projected) == expected
    assert [game_id for game_id, _ in projected] == sorted(
        (game_id for game_id, _ in projected), reverse=True
    )
    assert sorted(read(None)) == expected


def test_a_playoffs_log_on_the_legacy_table_never_admits_a_rotation_player(tmp_path):
    """The legacy fallback, unlike a publication, can hold Playoffs rows."""

    served = _sqlite_world(tmp_path)
    rows = _season_rows()
    for row in rows:
        row["game_date"] = date.fromisoformat(row["game_date"])
        if row["player_id"] == ROTATION:
            row.update(season_type="Playoffs", game_id="0042500106")
    served.logs.publish(
        SEASON,
        [PlayerGameLogRecord(**row) for row in rows],
        retrieved_at=RETRIEVED_AT,
        source_provider="nba_stats",
        source_row_count=len(rows),
        publication_status="complete",
    )
    with served.engine.begin() as connection:
        connection.execute(
            text("UPDATE publication_streams SET enabled = false WHERE stream_key = 'player_game_logs'")
        )

    payload = served.service().get_matchup(game_id=GAME_ID)

    assert ROTATION not in [row["canonical_id"] for row in payload["players"]]
    assert QUESTIONABLE in [row["canonical_id"] for row in payload["players"]]


@pytest.mark.parametrize("on_engine", [True, False])
def test_a_rotation_players_books_are_judged_stale_and_he_gets_no_line_label(tmp_path, on_engine):
    """No line, so no label is invented; his book quotes are judged like anyone's (crf04/statsplus#495, #496).

    With an engine, as in production, the books are read on the request's connection."""

    served = _sqlite_world(tmp_path)
    SportsbookQuoteStore(served.engine).record_poll(
        odds_event_id="odds-rotation",
        game_id=GAME_ID,
        started_at=NOW,
        completed_at=NOW,
        cost=1,
        remaining=100,
        is_closing=False,
        quotes=[
            ResolvedQuote(ROTATION, "PTS", "draftkings", 6.5, -115, -110, NOW),
            ResolvedQuote(ROTATION, "PTS", "betmgm", 6.5, -120, -105, NOW),
            ResolvedQuote(ROTATION, "PTS", "fanduel", 6.5, 110, 105, NOW - timedelta(minutes=61)),
        ],
    )
    service = served.service()
    if on_engine:
        service._engine = served.engine
    service.sportsbook_reader = SportsbookQuoteReader(served.engine)

    rotation = next(
        row for row in service.get_matchup(game_id=GAME_ID)["players"] if row["canonical_id"] == ROTATION
    )

    assert (rotation["player_source"], rotation["lines"], rotation["line_labels"]) == ("rotation", {}, {})
    assert [
        (book["book"], book["stale"], book["best_over"], book["best_under"])
        for book in rotation["book_lines"]["PTS"]["books"]
    ] == [("betmgm", False, False, True), ("draftkings", False, True, False), ("fanduel", True, False, False)]
