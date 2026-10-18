"""``GET /api/injury_scenarios`` over stored injury snapshots and game logs.

The injury snapshot is a real row in a migrated SQLite database, written by the
scheduled refresh.  Faked at their provider interfaces: the official injury
report (``get_snapshot``), the stored game-log source (``get_player_logs``),
PBPStats WOWY (``get_wowy``) and the Player Pool (``get_pool_for_game``).
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest
from sqlalchemy import create_engine

from app.config.settings import (
    CacheSettings,
    FeatureSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.errors import ProviderUnavailableError
from app.migrations import run_migrations
from app.providers.nba_injury_report import InjuryEntryEvidence, InjuryProviderSnapshot
from app.services.game_log_frame import derive_game_log_frame
from app.services.game_service import GameService
from app.services.injury_listing import StoredInjuryListing
from app.services.injury_refresh import InjuryReportRefreshService
from app.services.injury_scenarios import InjuryScenarioService
from app.services.injury_snapshot_repository import InjurySnapshotRepository
from app.services.matchup_injuries import MatchupInjuryService
from app.services.on_off import OnOffService, build_request, injury_warm_requests
from app.services.pbp_stats_adapter import WowyPlayerRow
from app.services.player_pool import PlayerPool, PoolPlayer
from tests.support.rotation_world import game as rotation_game
from tests.support.rotation_world import line as rotation_line
from tests.support.rotation_world import migrated_repository, store
from tests.support.slate_catalog import NoNeighbourEvents

LAL, BOS = 1610612747, 1610612738
GAME = "0022600100"
REPORT = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-10_05_00PM.pdf"
REPORT_2 = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-10_05_30PM.pdf"
#: 3pm Eastern on 2026-11-10, inside the 11:00-19:30 refresh window.
AFTERNOON = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)

LEBRON, REAVES, HACHIMURA, VANDERBILT, HAYES = 2544, 1630559, 1629060, 1629020, 1629637
TATUM, BROWN, BENCH = 1628369, 1627759, 1630001
EXACT_SIX, JUST_BELOW, THIN_SAMPLE, RETURNER = 1630002, 1630003, 1630004, 1630005
RATE, PRIOR_ONLY, OMITTED = 1630006, 1630007, 1630008
NAMES = {
    LEBRON: ("LeBron James", "LAL"),
    REAVES: ("Austin Reaves", "LAL"),
    HACHIMURA: ("Rui Hachimura", "LAL"),
    VANDERBILT: ("Jarred Vanderbilt", "LAL"),
    HAYES: ("Jaxson Hayes", "LAL"),
    TATUM: ("Jayson Tatum", "BOS"),
    BROWN: ("Jaylen Brown", "BOS"),
    BENCH: ("Bench Reserve", "LAL"),
    EXACT_SIX: ("Exact Six", "LAL"),
    JUST_BELOW: ("Just Below", "LAL"),
    THIN_SAMPLE: ("Thin Sample", "LAL"),
    RETURNER: ("Boomerang Guard", "LAL"),
    RATE: ("Rate Guard", "LAL"),
    PRIOR_ONLY: ("Prior Only", "LAL"),
    OMITTED: ("Omitted Line", "LAL"),
}


class Events(NoNeighbourEvents):
    games: ClassVar = [
        {
            "nba_game_id": GAME,
            "scheduled_at": "2026-11-11T00:30:00+00:00",
            "status_code": 1,
            "status_text": "7:30 pm ET",
            "classification": "Regular Season",
            "away_team": {"id": LAL, "tricode": "LAL", "name": None},
            "home_team": {"id": BOS, "tricode": "BOS", "name": None},
        }
    ]

    def count_events(self, season):
        return 1

    def get_freshness(self, season, *, now):
        return {"last_success_at": "2026-11-10T10:00:00+00:00"}

    def get_events_between(self, season, starts_at, ends_at):
        return [e for e in self.games if starts_at <= datetime.fromisoformat(e["scheduled_at"]) < ends_at]


class AthleteCatalog:
    def __init__(self):
        self.team_ids = {pid: NBA_TEAM_TRICODE_TO_ID[team] for pid, (_, team) in NAMES.items()}
        self.unreadable = False

    def get_catalog(self, season, *, active_only=False):
        if self.unreadable:
            raise ValueError("catalog unreadable")
        return [
            {
                "player_id": pid,
                "display_name": name,
                "team_abbreviation": team,
                "team_id": self.team_ids.get(pid),
                "is_active_for_season": True,
            }
            for pid, (name, team) in NAMES.items()
            if pid in self.team_ids
        ]


def _entry(player, status, url=REPORT, report_time="2026-11-10T17:00:00-05:00"):
    name, team = NAMES[player]
    slug = name.lower().replace(" ", "")
    return InjuryEntryEvidence(
        entry_id=f"nba_official:2026-11-10:LAL@BOS:{slug}",
        source_player_id=f"{team}:{slug}",
        source_player_name=name,
        source_team_tricode=team,
        canonical_status=status,
        raw_status=status,
        reason="Soreness",
        source_url=url,
        game_date="2026-11-10",
        matchup="LAL@BOS",
        report_time=report_time,
    )


class FakeInjuryProvider:
    def __init__(self, entries):
        self.entries = entries
        self.clock = lambda: AFTERNOON

    def get_snapshot(self):
        return InjuryProviderSnapshot(
            raw_payload=[{"report": "recorded"}],
            entries=tuple(self.entries),
            retrieved_at=self.clock(),
        )


class FakeGameLogs:
    """The stored game-log source: ``get_player_logs(player_id, season)``."""

    def __init__(self):
        self.rows = {}
        self.batches = []

    def add(self, player, season, team, games):
        """``games``: (game_id, date, min, pts, reb, ast, fg3m, fga) tuples."""
        for game_id, date, minutes, pts, reb, ast, fg3m, fga in games:
            self.rows.setdefault((player, season), []).append(
                {
                    "PLAYER_ID": player,
                    "PLAYER_NAME": NAMES[player][0],
                    "GAME_ID": game_id,
                    "GAME_DATE": date,
                    "MATCHUP": f"{team} @ XXX",
                    "TEAM_ID": 1,
                    "TEAM_ABBREVIATION": team,
                    "MIN": minutes,
                    "PTS": pts,
                    "REB": reb,
                    "AST": ast,
                    "FGM": 0,
                    "FGA": fga,
                    "FG3M": fg3m,
                    "FG3A": 0,
                    "FTM": 0,
                    "FTA": 0,
                    "OREB": 0,
                    "DREB": 0,
                    "TOV": 0,
                    "STL": 0,
                    "BLK": 0,
                    "PF": 0,
                }
            )

    def list_team_rows(self, season, team_id, publication_snapshot=None):
        """The game-log repository's team roster read."""
        return [
            SimpleNamespace(player_id=player, team_id=team_id)
            for (player, row_season), rows in self.rows.items()
            if row_season == season
            and any(NBA_TEAM_TRICODE_TO_ID[r["TEAM_ABBREVIATION"]] == team_id for r in rows)
        ]

    def _frame(self, player_id, season):
        rows = self.rows.get((player_id, season))
        if not rows:
            return pd.DataFrame(columns=["GAME_ID"])
        return derive_game_log_frame(pd.DataFrame(rows))

    def get_player_logs(self, player_id, season, publication_snapshot=None):
        return self._frame(player_id, season)

    def get_players_logs(self, player_ids, season, publication_snapshot=None):
        """The batched read: one call, each player's frame as ``get_player_logs`` gives it."""
        self.batches.append((tuple(sorted(player_ids)), season))
        return {pid: self._frame(pid, season) for pid in player_ids}


class FakeWowy:
    """The PBPStats provider interface: ``get_wowy``."""

    def __init__(self):
        self.responses = {}
        self.failing = set()
        self.asked = []

    def get_wowy(self, *, team_id, season, on_player_ids=(), off_player_ids=(), season_type="Regular Season"):
        key = (team_id, season, tuple(on_player_ids), tuple(off_player_ids))
        self.asked.append((team_id, season))
        if key in self.failing:
            raise ProviderUnavailableError("PBP Stats could not be reached.")
        return self.responses.get(key, [])

    def tweak(self, team, season, *, on=(), off=(), player, **fields):
        key = (team, season, tuple(on), tuple(off))
        self.responses[key] = [
            dataclasses.replace(row, **fields) if row.player_id == player else row
            for row in self.responses[key]
        ]

    def put(self, team, season, *, on=(), off=(), players):
        self.responses[(team, season, tuple(on), tuple(off))] = [
            WowyPlayerRow(
                player_id=pid,
                name=NAMES[pid][0],
                minutes=minutes,
                possessions=minutes * 2,
                usage=usage,
                points=pts,
                rebounds=reb,
                assists=ast,
                fg3m=fg3m,
                fga=fga,
            )
            for pid, minutes, usage, pts, reb, ast, fg3m, fga in players
        ]


class FakeRedis:
    """Dict-backed Redis for the on/off cache."""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False, px=None):
        self.data[key] = value
        return True


class FakePool:
    def __init__(self, lines):
        self.lines = lines

    def get_pool_for_game(self, *, season, game_id):
        players = tuple(
            PoolPlayer(
                pid,
                NAMES[pid][0],
                LAL,
                tuple(lines),
                {"prizepicks": tuple(lines)},
                lines={"prizepicks": lines},
            )
            for pid, lines in self.lines.items()
        )
        return PlayerPool(players, {LAL: len(players)}, {"status": "fresh"})


def _dates(season, count, start):
    base = datetime.fromisoformat(start)
    return [
        (f"{season[2:4]}{i:08d}", (base + timedelta(days=2 * i)).date().isoformat()) for i in range(count)
    ]


CURRENT, PRIOR = "2026-27", "2025-26"
G = _dates(CURRENT, 8, "2026-10-21")  # g1..g8; LeBron plays g1-g5
H = _dates(PRIOR, 4, "2025-10-22")  # h1..h4; LeBron plays h1-h2


def _games(ids, **stats):
    out = []
    for (game_id, date), minutes, pts, reb, ast, fg3m, fga in zip(
        ids,
        stats["minutes"],
        stats["pts"],
        stats["reb"],
        stats["ast"],
        stats["fg3m"],
        stats["fga"],
        strict=False,
    ):
        out.append((game_id, date, minutes, pts, reb, ast, fg3m, fga))
    return out


def _flat(ids, minutes, pts, reb, ast, fg3m, fga):
    n = len(ids)
    return _games(
        ids,
        minutes=[minutes] * n,
        pts=pts if isinstance(pts, list) else [pts] * n,
        reb=[reb] * n,
        ast=[ast] * n,
        fg3m=[fg3m] * n,
        fga=[fga] * n,
    )


class World:
    def __init__(self, tmp_path, dependencies, entries):
        self.now = AFTERNOON
        engine = create_engine(f"sqlite:///{tmp_path / 'scenarios.sqlite3'}")
        run_migrations(engine)
        settings = RuntimeSettings(
            environment="testing",
            features=FeatureSettings(injury_report_enabled=True),
            nba=NBASeasonSettings(current_season=CURRENT),
            cache=CacheSettings(enabled=False, on_off_min_interval_seconds=0),
        )
        self.provider = FakeInjuryProvider(entries)
        repository = InjurySnapshotRepository(engine)
        matchup_injuries = MatchupInjuryService(
            provider=self.provider,
            snapshot_repository=repository,
            athlete_catalog=AthleteCatalog(),
            enabled=True,
            clock=lambda: self.now,
        )
        listing = StoredInjuryListing(
            event_catalog=Events(),
            snapshot_repository=repository,
            settings=settings,
            enabled=True,
            clock=lambda: self.now,
        )
        self.refresh = InjuryReportRefreshService(
            injuries=matchup_injuries,
            event_catalog=Events(),
            settings=settings,
            clock=lambda: self.now,
        )
        self.logs, self.wowy, self.pool = FakeGameLogs(), FakeWowy(), FakePool({})
        self.redis = FakeRedis()
        self.settings = settings
        self.listing = listing
        self.catalog = AthleteCatalog()
        self.on_off = OnOffService(self.wowy, settings, redis_client=self.redis, clock=lambda: self.now)
        self.games = GameService(
            None,
            settings=settings,
            game_logs_source=self.logs,
            game_log_seasons=self.logs,
            athlete_catalog=self.catalog,
        )
        self.scenarios = InjuryScenarioService(
            injuries=listing,
            on_off=self.on_off,
            games=self.games,
            player_pool=self,
            event_catalog=Events(),
            settings=settings,
        )
        dependencies.injury_scenario_service = self.scenarios
        dependencies.game_service = self.games
        self.refresh.run()

    def warm(self):
        """What the scheduled warm does after an injury refresh."""
        report = self.listing.list_injuries("2026-11-10", None)
        return self.on_off.warm(
            injury_warm_requests(
                report["injuries"],
                CURRENT,
                PRIOR,
                lambda row: self.scenarios.prior_season_eligible(row, PRIOR),
            )
        )

    def get_pool_for_game(self, *, season, game_id):
        return self.pool.get_pool_for_game(season=season, game_id=game_id)


@pytest.fixture
def world(tmp_path, dependencies):
    w = World(
        tmp_path,
        dependencies,
        [
            _entry(LEBRON, "Questionable"),
            _entry(TATUM, "Out"),
            _entry(BROWN, "Probable"),
            _entry(HAYES, "Doubtful"),
        ],
    )
    logs, wowy = w.logs, w.wowy
    # LeBron: plays g1-g5 and h1-h2, always for LAL.
    logs.add(LEBRON, CURRENT, "LAL", _flat(G[:5], 34, 25, 7, 8, 2, 20))
    logs.add(LEBRON, PRIOR, "LAL", _flat(H[:2], 34, 25, 7, 8, 2, 20))
    # Reaves: with LeBron g1-g5, h1-h2; without g6-g8, h3-h4.
    logs.add(
        REAVES,
        CURRENT,
        "LAL",
        _flat(G[:5], 30, 20, 4, 5, 2, 15)
        + _games(
            G[5:], minutes=[34] * 3, pts=[26, 24, 25], reb=[3] * 3, ast=[4] * 3, fg3m=[2, 2, 3], fga=[18] * 3
        ),
    )
    logs.add(REAVES, PRIOR, "LAL", _flat(H[:2], 30, 20, 4, 5, 2, 15) + _flat(H[2:], 34, 26, 3, 4, 2, 18))
    # Hachimura: only two games without LeBron (g6, g7), absent g8.
    logs.add(HACHIMURA, CURRENT, "LAL", _flat(G[:5], 25, 12, 5, 1, 1, 9) + _flat(G[5:7], 29, 16, 6, 1, 2, 11))
    # Vanderbilt: three games without LeBron, but little off-floor time.
    logs.add(VANDERBILT, CURRENT, "LAL", _flat(G[:5], 22, 8, 7, 2, 0, 6) + _flat(G[5:], 26, 9, 8, 2, 0, 7))
    # Hayes: three games without LeBron this season; last season he left LAL for BOS.
    logs.add(HAYES, CURRENT, "LAL", _flat(G[:5], 20, 10, 6, 1, 0, 7) + _flat(G[5:], 24, 14, 7, 1, 0, 9))
    # Traded mid-season: two games with LAL (LeBron played both), two with BOS.
    logs.add(HAYES, PRIOR, "LAL", _flat(H[:2], 20, 30, 6, 1, 0, 7))
    logs.add(HAYES, PRIOR, "BOS", _flat(H[2:], 20, 30, 6, 1, 0, 7))
    # A bench player under the 15 minute rotation line, with big numbers.
    logs.add(BENCH, CURRENT, "LAL", _flat(G, 10, 18, 9, 4, 3, 12))
    # Tatum is Out; Brown starts every game, Tatum plays g1-g5.
    logs.add(TATUM, CURRENT, "BOS", _flat(G[:5], 35, 27, 8, 5, 3, 20))
    logs.add(BROWN, CURRENT, "BOS", _flat(G[:5], 33, 22, 6, 3, 2, 17) + _flat(G[5:], 36, 28, 7, 4, 3, 20))

    # (id, minutes, usage, pts, reb, ast, fg3m, fga) per lineup state.
    wowy.put(
        LAL,
        CURRENT,
        off=[LEBRON],
        players=[
            (REAVES, 100, 31.0, 70, 40, 15, 12, 80),
            (HACHIMURA, 200, 20.0, 80, 30, 10, 10, 60),
            (VANDERBILT, 100, 15.0, 30, 25, 6, 0, 25),
            (HAYES, 150, 18.0, 66, 30, 5, 0, 55),
        ],
    )
    wowy.put(
        LAL,
        CURRENT,
        on=[LEBRON],
        players=[
            (REAVES, 400, 26.0, 220, 80, 80, 40, 190),
            (HACHIMURA, 400, 18.0, 150, 60, 20, 22, 130),
            (VANDERBILT, 300, 14.0, 80, 70, 20, 0, 60),
            (HAYES, 300, 17.0, 110, 60, 10, 0, 90),
        ],
    )
    wowy.put(
        LAL,
        PRIOR,
        off=[LEBRON],
        players=[
            (REAVES, 80, 30.0, 55, 0, 15, 8, 60),
        ],
    )
    wowy.put(
        LAL,
        PRIOR,
        on=[LEBRON],
        players=[
            (REAVES, 300, 25.0, 165, 60, 60, 30, 140),
        ],
    )
    wowy.failing.add((BOS, CURRENT, (), (TATUM,)))
    wowy.put(BOS, CURRENT, on=[TATUM], players=[(BROWN, 300, 27.0, 200, 50, 30, 30, 170)])
    w.pool = FakePool({REAVES: {"PTS": 24.5}, BROWN: {"PTS": 26.5}})
    w.warm()
    return w


def _scenarios(client, authenticate, query="date=2026-11-10"):
    response = client.get(f"/api/injury_scenarios?{query}", headers=authenticate())
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _row(scenario, branch, player_id, market):
    (found,) = [b for b in scenario["branches"] if b["branch"] == branch]
    (row,) = [r for r in found["rows"] if r["player_id"] == player_id and r["market"] == market]
    return row


def _shifts(row):
    return {k: v for k, v in row["shift"].items() if k != "season_span"}


def _scenario(body, player_id):
    (found,) = [s for s in body["scenarios"] if s["injured"]["player_id"] == player_id]
    return found


def test_questionable_has_both_branches_and_out_only_sits_and_probable_none(client, authenticate, world):
    body = _scenarios(client, authenticate)

    assert [
        (s["injured"]["player"], s["injured"]["status"], [b["branch"] for b in s["branches"]])
        for s in body["scenarios"]
    ] == [
        ("Jayson Tatum", "Out", ["sits"]),
        ("Jaxson Hayes", "Doubtful", ["plays", "sits"]),
        ("LeBron James", "Questionable", ["plays", "sits"]),
    ]
    assert _scenario(body, LEBRON)["injured"] == {
        "player": "LeBron James",
        "player_id": LEBRON,
        "team": "LAL",
        "game_id": GAME,
        "status": "Questionable",
        "report_time": "2026-11-10T17:00:00-05:00",
    }
    plays = _row(_scenario(body, LEBRON), "plays", REAVES, "PTS")
    assert plays["baseline"] == {
        "avg": 20.0,
        "games": 7,
        "per36": 19.8,
        "minutes": 700,
        "season_span": "2025-26 to 2026-27",
        "on_floor_season_span": "2025-26 to 2026-27",
    }
    assert "without" not in plays and plays["flagged"] is False


def test_agreeing_evidence_at_ten_percent_is_flagged_and_disagreement_is_not(client, authenticate, world):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    pts = _row(scenario, "sits", REAVES, "PTS")
    assert pts["without"] == {"avg": 25.4, "games": 5, "season_span": "2025-26 to 2026-27"}
    assert pts["off_floor"] == {"per36": 25.0, "minutes": 180, "season_span": "2025-26 to 2026-27"}
    assert _shifts(pts) == {"full_game": 0.27, "on_off_per36": 0.2626}
    assert pts["flagged"] is True
    assert pts["limits"] == []

    ast = _row(scenario, "sits", REAVES, "AST")  # both down: 5 -> 4, 7.2 -> 6.0
    assert _shifts(ast) == {"full_game": -0.2, "on_off_per36": -0.1667}
    assert ast["flagged"] is True

    reb = _row(scenario, "sits", REAVES, "REB")  # 4 -> 3 in games, 7.2 -> 8.0 per 36
    assert _shifts(reb) == {"full_game": -0.25, "on_off_per36": 0.1111}
    assert reb["flagged"] is False

    three = _row(scenario, "sits", REAVES, "3PM")  # exactly +10% in games: 2.0 -> 2.2
    assert _shifts(three) == {"full_game": 0.1, "on_off_per36": 0.1111}
    assert three["flagged"] is True

    assert {r["market"]: r["kind"] for r in scenario["branches"][1]["rows"] if r["player_id"] == REAVES} == {
        "PTS": "scored",
        "REB": "scored",
        "AST": "scored",
        "PRA": "scored",
        "3PM": "scored",
        "MIN": "context",
        "FGA": "context",
        "USG": "context",
    }


def test_context_markets_are_never_flagged_and_carry_the_supporting_numbers(client, authenticate, world):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    minutes = _row(scenario, "sits", REAVES, "MIN")
    assert minutes["without"]["avg"] == 34.0 and minutes["baseline"]["avg"] == 30.0
    assert minutes["flagged"] is False and "line" not in minutes
    usage = _row(scenario, "sits", REAVES, "USG")
    assert usage["baseline"]["usage"] == 25.57 and usage["off_floor"]["usage"] == 30.56
    assert usage["flagged"] is False


def test_thin_samples_are_insufficient_and_never_flagged(client, authenticate, world):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    few_games = _row(scenario, "sits", HACHIMURA, "PTS")  # 2 games without, 200 minutes
    assert few_games["without"]["games"] == 2 and few_games["off_floor"]["minutes"] == 200
    assert "insufficient" in few_games["limits"] and few_games["flagged"] is False
    assert "shift" not in few_games

    few_minutes = _row(scenario, "sits", VANDERBILT, "REB")  # 3 games, 100 minutes
    assert few_minutes["without"]["games"] == 3 and few_minutes["off_floor"]["minutes"] == 100
    assert "insufficient" in few_minutes["limits"] and few_minutes["flagged"] is False


def test_a_teammate_who_changed_teams_loses_last_season(client, authenticate, world):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    hayes = _row(scenario, "sits", HAYES, "PTS")
    assert hayes["without"] == {"avg": 14.0, "games": 3, "season_span": "2026-27"}
    assert hayes["baseline"]["games"] == 5 and hayes["baseline"]["season_span"] == "2026-27"
    assert hayes["off_floor"]["season_span"] == "2026-27"
    assert hayes["flagged"] is True  # 14 vs 10 in games; 15.84 vs 13.2 per 36, on exactly 150 minutes


def test_beat_is_counted_against_todays_line_in_the_games_without(client, authenticate, world):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    pts = _row(scenario, "sits", REAVES, "PTS")
    assert pts["line"] == {"value": 24.5, "source": "prizepicks", "beat": 4, "of": 5}
    reb = _row(scenario, "sits", REAVES, "REB")
    assert reb["line"] is None and "no_line" in reb["limits"]


def test_an_unwarmed_split_is_a_limit_and_never_fetches_upstream(client, authenticate, world):
    asked_before = len(world.wowy.asked)

    scenario = _scenario(_scenarios(client, authenticate), TATUM)

    pts = _row(scenario, "sits", BROWN, "PTS")  # Tatum's off-floor split failed to warm
    assert pts["without"] == {"avg": 28.0, "games": 3, "season_span": "2026-27"}
    assert pts["off_floor"] == {"per36": None, "minutes": 0, "season_span": None}
    assert pts["limits"] == ["on_off_not_warmed", "on_off_seasons_differ"]
    assert pts["line"] == {"value": 26.5, "source": "prizepicks", "beat": 3, "of": 3}
    assert pts["flagged"] is False
    assert len(world.wowy.asked) == asked_before  # the request made no upstream call


def test_the_warm_covers_each_eligible_prior_season_once(world):
    assert sorted(set(world.wowy.asked)) == sorted(
        {
            (BOS, "2026-27"),
            (LAL, "2026-27"),
            (LAL, "2025-26"),
        }
    )
    before = len(world.wowy.asked)

    world.warm()  # a rerun inside the TTL fetches only what failed before

    assert world.wowy.asked[before:] == [(BOS, "2026-27")]


def test_no_cache_keeps_full_game_evidence_and_reports_on_off_unavailable(client, authenticate, world):
    world.scenarios.on_off = OnOffService(world.wowy, world.settings)

    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    pts = _row(scenario, "sits", REAVES, "PTS")
    assert pts["without"] == {"avg": 25.4, "games": 5, "season_span": "2025-26 to 2026-27"}
    assert pts["limits"] == ["on_off_unavailable"]
    assert pts["flagged"] is False


def test_an_expired_split_is_served_stale_with_a_limit(client, authenticate, world):
    world.now += timedelta(hours=25)

    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    pts = _row(scenario, "sits", REAVES, "PTS")
    assert pts["off_floor"]["minutes"] == 180
    assert "on_off_stale" in pts["limits"] and pts["flagged"] is True


def test_a_missing_prior_season_state_cannot_compare_unlike_seasons(client, authenticate, world):
    del world.redis.data[build_request("LAL", PRIOR, on=[LEBRON]).cache_key]

    pts = _row(_scenario(_scenarios(client, authenticate), LEBRON), "sits", REAVES, "PTS")

    assert pts["limits"] == ["on_off_not_warmed", "on_off_seasons_differ"]
    assert pts["flagged"] is False and "shift" not in pts


def test_a_mid_season_team_change_leaves_that_season_full_game_only(client, authenticate, world):
    reaves = world.logs.rows[(REAVES, CURRENT)]
    reaves[0]["TEAM_ABBREVIATION"] = "BOS"  # he joined LAL after the opener

    pts = _row(_scenario(_scenarios(client, authenticate), LEBRON), "sits", REAVES, "PTS")

    assert pts["without"] == {"avg": 25.0, "games": 3, "season_span": "2026-27"}
    assert pts["baseline"]["games"] == 4  # g2-g5; the BOS opener and last season are out
    assert pts["off_floor"] == {"per36": None, "minutes": 0, "season_span": None}
    assert pts["limits"] == ["on_off_unsliceable"]
    assert pts["flagged"] is False


def test_players_outside_the_rotation_or_gone_from_the_team_are_left_out(client, authenticate, world):
    world.logs.add(HACHIMURA, CURRENT, "BOS", _flat(G[7:8], 25, 12, 5, 1, 1, 9))
    # Hachimura's newest game is for Boston now, so he is no longer a teammate.

    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    mates = {r["player_id"] for r in scenario["branches"][1]["rows"]}
    assert mates == {REAVES, VANDERBILT, HAYES}  # not Hachimura, not the 10-minute BENCH


def test_pra_on_off_sums_points_rebounds_and_assists(client, authenticate, world):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    pra = _row(scenario, "sits", REAVES, "PRA")
    assert pra["baseline"]["per36"] == 34.2  # (385 + 140 + 140) * 36 / 700
    assert pra["off_floor"]["per36"] == 39.0  # (125 + 40 + 30) * 36 / 180
    assert _shifts(pra) == {"full_game": 0.1172, "on_off_per36": 0.1404}
    assert pra["flagged"] is True


def test_a_shift_just_under_ten_percent_is_not_flagged(client, authenticate, world):
    # Games: 3PM 2.0 -> 2.2, exactly +10%.  On/off: 449 on and 127 off 3PM
    # over 700 / 180 minutes is +9.9975%, which rounds to 0.1 but is not 10%.
    wowy = world.wowy
    wowy.tweak(LAL, CURRENT, on=[LEBRON], player=REAVES, fg3m=249)
    wowy.tweak(LAL, PRIOR, on=[LEBRON], player=REAVES, fg3m=200)
    wowy.tweak(LAL, CURRENT, off=[LEBRON], player=REAVES, fg3m=70)
    wowy.tweak(LAL, PRIOR, off=[LEBRON], player=REAVES, fg3m=57)
    world.redis.data.clear()
    world.warm()

    three = _row(_scenario(_scenarios(client, authenticate), LEBRON), "sits", REAVES, "3PM")

    assert _shifts(three) == {"full_game": 0.1, "on_off_per36": 0.1}
    assert three["flagged"] is False


def test_without_games_equal_the_game_log_players_off_filter(client, authenticate, world):
    # LeBron missed the opener and the second game; nothing says he changed teams.
    world.logs.rows[(LEBRON, CURRENT)] = world.logs.rows[(LEBRON, CURRENT)][2:]
    headers = authenticate()

    expected_games, expected_points = 0, 0
    for season in (CURRENT, PRIOR):
        body = client.get(
            "/api/games/game_logs?player_name=Austin Reaves"
            f"&season_filter={season}&players_off[]=LeBron James",
            headers=headers,
        ).get_json()
        expected_games += len(body["game_logs"])
        expected_points += sum(row["PTS"] for row in body["game_logs"])
    assert (expected_games, expected_points) == (7, 167)  # g1,g2,g6-g8 and h3,h4

    pts = _row(_scenario(_scenarios(client, authenticate), LEBRON), "sits", REAVES, "PTS")
    assert pts["without"]["games"] == expected_games
    assert pts["without"]["avg"] == round(expected_points / expected_games, 2) == 23.86


class RetainedReader:
    """A publication reader whose active season has moved on from ``PRIOR``."""

    def snapshot(self, keys, season=None, **keywords):
        read = SimpleNamespace(season=CURRENT, unavailable_reason="publication_season_mismatch")
        return SimpleNamespace(read=lambda key: read, name="live")

    def retained_snapshot(self, keys, season=None, **keywords):
        return SimpleNamespace(name="retained")


def test_last_seasons_logs_are_read_from_the_retained_publication(world):
    class RetainedOnlyLogs(FakeGameLogs):
        def get_player_logs(self, player_id, season, publication_snapshot=None):
            if getattr(publication_snapshot, "name", None) != "retained":
                return pd.DataFrame(columns=["GAME_ID"])
            return super().get_player_logs(player_id, season)

    logs = RetainedOnlyLogs()
    logs.rows = world.logs.rows
    games = GameService(
        None,
        settings=world.settings,
        game_logs_source=logs,
        publication_reader=RetainedReader(),
    )

    assert len(games.get_player_logs_by_id(REAVES, PRIOR)) == 4


def test_a_stale_report_is_a_limit_on_the_scenario_and_its_rows(client, authenticate, world):
    world.now += timedelta(minutes=50)

    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    assert scenario["limits"] == ["stale_report"]
    assert "stale_report" in _row(scenario, "sits", REAVES, "PTS")["limits"]


def test_scenarios_follow_the_stored_snapshot_when_a_status_changes(client, authenticate, world):
    world.now += timedelta(minutes=30)
    world.provider.entries = [_entry(LEBRON, "Out", REPORT_2, "2026-11-10T17:30:00-05:00")]
    world.provider.clock = lambda: world.now
    assert world.refresh.run()["state"] == "new_report"

    body = _scenarios(client, authenticate)

    assert [
        (s["injured"]["player"], s["injured"]["status"], [b["branch"] for b in s["branches"]])
        for s in body["scenarios"]
    ] == [
        ("LeBron James", "Out", ["sits"]),
    ]


def test_game_log_reads_are_one_batch_per_season_however_many_scenarios_and_teammates(world):
    """crf04/statsplus#240: scenarios share each season's logs instead of re-reading teammates."""

    singles = []
    read_one = world.logs.get_player_logs
    world.logs.get_player_logs = lambda *args, **kwargs: singles.append(args) or read_one(*args, **kwargs)

    def reads(player=None):
        world.logs.batches.clear()
        body = world.scenarios.scenarios("2026-11-10", player=player)
        return len(body["scenarios"]), len(world.logs.batches)

    # One listed Laker, then two Lakers plus Tatum on the other team.
    assert reads(player="LeBron James") == (1, 2)
    assert reads() == (3, 2)  # LeBron, Hayes, Tatum: LAL and BOS share one batch per season

    # Another listed Laker and another teammate with logs add no reads.
    world.provider.entries.append(_entry(REAVES, "Questionable"))
    world.now += timedelta(minutes=30)
    world.provider.clock = lambda: world.now
    world.refresh.run()
    world.logs.add(RATE, CURRENT, "LAL", _flat(G, 20, 10, 5, 3, 1, 8))
    assert reads() == (4, 2)
    assert singles == []
    assert sorted(season for _, season in world.logs.batches) == sorted([PRIOR, CURRENT])


def test_filters_by_game_and_player_and_validates_parameters(client, authenticate, world):
    assert [
        s["injured"]["player"]
        for s in _scenarios(client, authenticate, f"date=2026-11-10&game_id={GAME}&player=tatum")["scenarios"]
    ] == []
    assert [
        s["injured"]["player"]
        for s in _scenarios(client, authenticate, f"date=2026-11-10&game_id={GAME}&player={TATUM}")[
            "scenarios"
        ]
    ] == ["Jayson Tatum"]
    assert [
        s["injured"]["player"]
        for s in _scenarios(client, authenticate, "date=2026-11-10&player=lebron james")["scenarios"]
    ] == ["LeBron James"]
    assert _scenarios(client, authenticate, "date=2026-11-10&game_id=0022600999") == {
        "date": "2026-11-10",
        "scenarios": [],
    }
    headers = authenticate()
    assert client.get("/api/injury_scenarios", headers=headers).status_code == 400
    assert client.get("/api/injury_scenarios?date=2026-11-10&x=1", headers=headers).status_code == 400


def test_an_injured_player_who_changed_teams_resets_the_pair(client, authenticate, world):
    for row in world.logs.rows[(LEBRON, PRIOR)]:
        row["TEAM_ABBREVIATION"] = "BOS"  # LeBron was with Boston last season

    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    pts = _row(scenario, "sits", REAVES, "PTS")
    assert pts["without"] == {"avg": 25.0, "games": 3, "season_span": "2026-27"}
    assert pts["baseline"]["games"] == 5 and pts["baseline"]["season_span"] == "2026-27"
    assert pts["off_floor"]["season_span"] == "2026-27"
    assert pts["off_floor"]["minutes"] == 100


def _mates(client, authenticate):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)
    return {r["player_id"] for r in scenario["branches"][1]["rows"]}


def test_opening_night_a_teammate_the_catalog_places_elsewhere_is_dropped(client, authenticate, world):
    # Reaves's only games are for LAL, but the catalog says he is a Celtic now.
    world.catalog.team_ids[REAVES] = BOS

    assert _mates(client, authenticate) == {HACHIMURA, VANDERBILT, HAYES}


def test_a_player_missing_from_or_unreadable_in_the_catalog_falls_back_to_game_logs(
    client, authenticate, world
):
    del world.catalog.team_ids[REAVES]
    assert REAVES in _mates(client, authenticate)

    world.catalog.unreadable = True
    assert REAVES in _mates(client, authenticate)


def test_an_unsliceable_prior_season_blocks_the_flag(client, authenticate, world):
    world.logs.rows[(REAVES, PRIOR)][0]["TEAM_ABBREVIATION"] = "BOS"  # joined mid-season
    # This season alone would clear 150 minutes and agree with the games (+13.6%).
    world.wowy.tweak(LAL, CURRENT, off=[LEBRON], player=REAVES, minutes=160, points=100)
    world.redis.data.clear()
    world.warm()

    pts = _row(_scenario(_scenarios(client, authenticate), LEBRON), "sits", REAVES, "PTS")

    assert pts["without"]["season_span"] == "2025-26 to 2026-27"
    assert pts["off_floor"] == {"per36": 22.5, "minutes": 160, "season_span": "2026-27"}
    assert pts["limits"] == ["on_off_unsliceable"]
    assert pts["flagged"] is False


def test_on_off_spans_name_the_seasons_that_supplied_minutes(client, authenticate, world):
    wowy = world.wowy
    wowy.put(LAL, PRIOR, on=[LEBRON], players=[])  # no minutes together last season
    wowy.put(LAL, PRIOR, off=[LEBRON], players=[])
    wowy.tweak(LAL, CURRENT, off=[LEBRON], player=REAVES, minutes=160)
    world.redis.data.clear()
    world.warm()

    pts = _row(_scenario(_scenarios(client, authenticate), LEBRON), "sits", REAVES, "PTS")

    assert pts["baseline"]["season_span"] == "2025-26 to 2026-27"
    assert pts["baseline"]["on_floor_season_span"] == "2026-27"
    assert pts["baseline"]["minutes"] == 400
    assert pts["off_floor"]["season_span"] == "2026-27"
    assert pts["shift"]["season_span"] == {
        "full_game": "2025-26 to 2026-27",
        "on_off": "2026-27",
    }


# -- rotation context (crf04/statsplus#239) ---------------------------------
#
# Stored rotations exist for 2026-27 only (g1..g8 below); 2025-26 has none.
# LeBron played g1-g5 and has DNP lines in g6-g8, so g6-g8 are the without
# games.  Each tuple is (minutes, started, closed); ``None`` means no line.
T, F = True, False


def _rotation_lines():
    def run(with_games, without_games):
        return [*with_games, *without_games]

    return {
        LEBRON: run([(34, T, F)] * 4 + [(34, F, F)], [(0, F, F)] * 3),
        REAVES: run(
            [(30, T, T)] + [(30, T, F)] * 4,
            [(34, T, T), (34, T, F), (34, T, T)],
        ),
        VANDERBILT: run([(22, T, F)] * 2 + [(22, F, F)] * 3, [(26, T, F)] * 3),
        # g5 is a DNP-CD that the game logs do not show.
        HAYES: run([(20, F, F)] * 4 + [(0, F, F)], [(24, T, F), (24, T, F), (24, F, F)]),
        # No line in g8: not on that box score, so that game is not his.
        HACHIMURA: run([(25, T, F)] + [(25, F, F)] * 4, [(29, T, F), (29, T, F), None]),
        BENCH: run([(10, F, F)] * 5, [(18, T, F), (20, T, F), (16, F, F)]),
        EXACT_SIX: run([(10, F, F)] * 5, [(16, F, F)] * 3),
        JUST_BELOW: run([(10, F, F)] * 5, [(15.9, F, F)] * 3),
        THIN_SAMPLE: run([(10, F, F)] * 5, [(19, F, F), (19, F, F), None]),
        # Only on the roster in the two-season fixtures (no game logs otherwise).
        RATE: run([(20, T, F)] * 3 + [(20, F, F)] * 2, [(20, T, F), (20, T, F), (20, F, F)]),
        OMITTED: run([(20, F, F)] * 5, [(20, F, F), (20, F, F), None]),
    }


@pytest.fixture
def rotation_world(world, tmp_path):
    for player in (EXACT_SIX, JUST_BELOW, THIN_SAMPLE):
        world.logs.add(player, CURRENT, "LAL", _flat(G, 10, 5, 2, 1, 0, 4))
    repository = migrated_repository(tmp_path)
    lines = _rotation_lines()
    store(
        repository,
        *(
            rotation_game(
                game_id,
                date,
                "LAL",
                "BOS",
                [
                    rotation_line(
                        player,
                        NAMES[player][0],
                        "LAL",
                        minutes=per_game[index][0],
                        started=per_game[index][1],
                        closed=per_game[index][2],
                    )
                    for player, per_game in lines.items()
                    if per_game[index] is not None
                ],
            )
            for index, (game_id, date) in enumerate(G)
        ),
        season=CURRENT,
    )
    world.scenarios.rotations = repository
    return world


def _split(without, played, change, *, insufficient=False, seasons="2026-27", missing=0):
    keys = ("games", "played", "starts", "minutes", "closed")
    return {
        "without": dict(zip(keys, without, strict=True)),
        "with": dict(zip(keys, played, strict=True)),
        "minutes_change": change,
        "insufficient": insufficient,
        "seasons": seasons,
        "missing_games": missing,
    }


def test_teammate_rows_carry_the_rotation_split_in_both_branches(client, authenticate, rotation_world):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    # Reaves: 3 without games at 34.0, 5 with games at 30.0.  His 2025-26
    # without games h3 and h4 have no stored rotation, so they are missing.
    expected = _split((3, 3, 3, 34.0, 2), (5, 5, 5, 30.0, 1), 4.0, missing=2)
    assert _row(scenario, "sits", REAVES, "PTS")["rotation"] == expected
    assert _row(scenario, "plays", REAVES, "MIN")["rotation"] == expected


def test_a_dnp_teammate_counts_zero_minutes_and_a_missing_line_excludes_the_game(
    client, authenticate, rotation_world
):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    # Hayes: g5 is a DNP-CD, so 4 played of 5 and (4 * 20) / 5 = 16.0 minutes.
    assert _row(scenario, "sits", HAYES, "PTS")["rotation"] == _split(
        (3, 3, 2, 24.0, 0), (5, 4, 0, 16.0, 0), 8.0
    )
    # Hachimura has no g8 line: only g6 and g7 are his without games.
    assert _row(scenario, "sits", HACHIMURA, "PTS")["rotation"] == _split(
        (2, 2, 2, 29.0, 0), (5, 5, 1, 25.0, 0), 4.0, insufficient=True, missing=0
    )


def test_a_teammate_with_no_stored_rotation_has_a_null_rotation(client, authenticate, rotation_world):
    tatum = _scenario(_scenarios(client, authenticate), TATUM)

    assert _row(tatum, "sits", BROWN, "PTS")["rotation"] is None
    assert tatum["rotation_starts"] == {"starts": 0, "games": 0}
    assert tatum["replacement_starters"] == [] and tatum["minutes_jumps"] == []


def test_rotation_starts_counts_the_injured_players_own_with_games(client, authenticate, rotation_world):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    assert scenario["rotation_starts"] == {"starts": 4, "games": 5}


def test_replacement_starters_need_a_sufficient_higher_start_rate_ordered_by_starts(
    client, authenticate, rotation_world
):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    # Reaves starts every game (3/3 vs 5/5: not higher) and Hachimura has only
    # 2 without games, so neither is listed.  Bench and Hayes tie on 2 starts.
    assert scenario["replacement_starters"] == [
        {
            "name": "Jarred Vanderbilt",
            "starts_without": 3,
            "games_without": 3,
            "starts_with": 2,
            "games_with": 5,
        },
        {
            "name": "Bench Reserve",
            "starts_without": 2,
            "games_without": 3,
            "starts_with": 0,
            "games_with": 5,
        },
        {
            "name": "Jaxson Hayes",
            "starts_without": 2,
            "games_without": 3,
            "starts_with": 0,
            "games_with": 5,
        },
    ]


def test_minutes_jumps_list_only_sub_rotation_teammates_at_six_minutes_or_more(
    client, authenticate, rotation_world
):
    body = _scenarios(client, authenticate)
    scenario = _scenario(body, LEBRON)

    # Bench +8.0, Exact Six exactly +6.0 (listed), Just Below +5.9 (not),
    # Thin Sample +9.0 on only 2 without games (insufficient, not listed).
    assert scenario["minutes_jumps"] == [
        {"name": "Bench Reserve", "rotation": _split((3, 3, 2, 18.0, 0), (5, 5, 0, 10.0, 0), 8.0)},
        {"name": "Exact Six", "rotation": _split((3, 3, 0, 16.0, 0), (5, 5, 0, 10.0, 0), 6.0)},
    ]
    # They carry no scored rows and are never flagged.
    rows = [r for b in scenario["branches"] for r in b["rows"]]
    assert BENCH not in {r["player_id"] for r in rows} and EXACT_SIX not in {r["player_id"] for r in rows}


def test_rotation_never_changes_the_existing_rows_or_flags(client, authenticate, world, rotation_world):
    without_rotation = _scenarios(client, authenticate)
    strip = lambda body: [  # noqa: E731
        [{k: v for k, v in r.items() if k != "rotation"} for b in s["branches"] for r in b["rows"]]
        for s in body["scenarios"]
    ]

    rotation_world.scenarios.rotations = None
    plain = _scenarios(client, authenticate)

    assert strip(without_rotation) == strip(plain)
    assert [s["limits"] for s in without_rotation["scenarios"]] == [s["limits"] for s in plain["scenarios"]]


def test_a_teammate_who_left_and_returned_counts_rotation_games_only_after_the_reset(
    client, authenticate, rotation_world, tmp_path
):
    # Boomerang played for LAL in g1-g2, for BOS in g3-g4, and for LAL again in
    # g5-g8: the pair window starts after g4, so his g1-g2 games do not count.
    logs = rotation_world.logs
    logs.add(RETURNER, CURRENT, "LAL", _flat(G[:2], 20, 9, 3, 2, 1, 8) + _flat(G[4:], 20, 9, 3, 2, 1, 8))
    logs.add(RETURNER, CURRENT, "BOS", _flat(G[2:4], 20, 9, 3, 2, 1, 8))
    repository = rotation_world.scenarios.rotations
    store(
        repository,
        *(
            rotation_game(
                G[index][0],
                G[index][1],
                "LAL",
                "BOS",
                [
                    rotation_line(LEBRON, "LeBron James", "LAL", minutes=34 if index < 5 else 0),
                    rotation_line(RETURNER, "Boomerang Guard", "LAL", minutes=20, started=index != 4),
                ],
            )
            for index in (0, 1, 4, 5, 6, 7)
        ),
        season=CURRENT,
    )

    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    assert _row(scenario, "sits", RETURNER, "MIN")["rotation"] == _split(
        (3, 3, 3, 20.0, 0), (1, 1, 0, 20.0, 0), 0.0
    )


# -- two seasons of stored rotations ------------------------------------------
#
# The fixture below also stores 2025-26 rotations: LAL games h1-h6 (h1-h2 are
# the real game-log games LeBron played, h3-h4 the game-log games he missed,
# h5-h6 stored games the game logs do not have), so the window spans both seasons.
P_EXTRA = [("2500000004", "2025-11-01"), ("2500000005", "2025-11-03")]
PRIOR_GAMES = [*H, *P_EXTRA]


def _store_prior(world, lines):
    store(
        world.scenarios.rotations,
        *(
            rotation_game(
                game_id,
                date,
                "LAL",
                "BOS",
                [
                    rotation_line(
                        player,
                        NAMES[player][0],
                        "LAL",
                        minutes=per_game[index][0],
                        started=per_game[index][1],
                        closed=per_game[index][2],
                    )
                    for player, per_game in lines.items()
                ],
            )
            for index, (game_id, date) in enumerate(PRIOR_GAMES)
        ),
        season=PRIOR,
    )


@pytest.fixture
def two_seasons(rotation_world):
    logs = rotation_world.logs
    logs.add(RATE, CURRENT, "LAL", _flat(G, 20, 5, 2, 1, 0, 4))
    logs.add(OMITTED, CURRENT, "LAL", _flat(G, 20, 5, 2, 1, 0, 4))
    logs.add(PRIOR_ONLY, PRIOR, "LAL", _flat(H, 20, 5, 2, 1, 0, 4))
    _store_prior(
        rotation_world,
        {
            LEBRON: [(34, T, F)] * 2 + [(0, F, F)] * 4,
            REAVES: [(30, T, F)] * 2 + [(36, T, F)] * 2 + [(36, F, F)] * 2,
            PRIOR_ONLY: [(20, F, F)] * 2 + [(24, T, F)] * 3 + [(24, F, F)],
        },
    )
    return rotation_world


def test_rotation_splits_combine_both_seasons_with_a_span(client, authenticate, two_seasons):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    # Reaves without: 2026-27 g6-g8 (3 x 34.0, 3 starts, 2 closed) + 2025-26
    # h3-h6 (4 x 36.0, 2 starts) = 7 games, 246 / 7 = 35.1 minutes.  With: g1-g5
    # (5 x 30.0, 1 closed) + h1-h2 (2 x 30.0) = 7 games, 30.0 minutes.
    assert _row(scenario, "sits", REAVES, "PTS")["rotation"] == _split(
        (7, 7, 5, 35.1, 2), (7, 7, 7, 30.0, 1), 5.1, seasons="2025-26 to 2026-27", missing=0
    )


def test_a_teammate_found_only_in_last_seasons_roster_gets_a_rotation(client, authenticate, two_seasons):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    # Prior Only has game logs and rotations in 2025-26 only.
    assert _row(scenario, "sits", PRIOR_ONLY, "MIN")["rotation"] == _split(
        (4, 4, 3, 24.0, 0), (2, 2, 0, 20.0, 0), 4.0, seasons="2025-26", missing=0
    )
    assert {
        "name": "Prior Only",
        "starts_without": 3,
        "games_without": 4,
        "starts_with": 0,
        "games_with": 2,
    } in scenario["replacement_starters"]


def test_replacement_starters_compare_start_rates_not_counts(client, authenticate, two_seasons):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    # Rate Guard starts 2 of 3 without him (0.67) but 3 of 5 with him (0.60):
    # more with him in count terms (3 > 2), yet a higher rate without.
    assert {
        "name": "Rate Guard",
        "starts_without": 2,
        "games_without": 3,
        "starts_with": 3,
        "games_with": 5,
    } in scenario["replacement_starters"]


def test_a_stored_game_without_the_teammates_line_is_not_a_missing_game(client, authenticate, two_seasons):
    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    # g8 is a game-log without game for him and is stored, but its box score
    # has no line for him: it is not his game, and it is not missing either.
    assert _row(scenario, "sits", OMITTED, "MIN")["rotation"] == _split(
        (2, 2, 0, 20.0, 0), (5, 5, 0, 20.0, 0), 0.0, insufficient=True, missing=0
    )


def test_the_injured_players_starts_count_only_after_his_own_team_change(
    client, authenticate, rotation_world
):
    # LeBron played for BOS on 2025-10-25, after his stored LAL games h1-h2
    # (both starts) and before h3: those two games are before the reset.
    rotation_world.logs.add(LEBRON, PRIOR, "BOS", _flat([("2500090001", "2025-10-25")], 34, 25, 7, 8, 2, 20))
    _store_prior(rotation_world, {LEBRON: [(34, T, F)] * 2 + [(0, F, F)] * 4})

    scenario = _scenario(_scenarios(client, authenticate), LEBRON)

    assert scenario["rotation_starts"] == {"starts": 4, "games": 5}
