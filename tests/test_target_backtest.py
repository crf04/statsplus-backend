"""Season-to-date Target backtest, at the service and HTTP seams (#246).

The backtest asks a different question from resolution (#245): not "who on
today's opposing side fits", but "who in the whole league fits, and how have
they produced against this opponent this season".  It therefore composes the
durable player seams directly -- the league-wide game-log rows against the
opponent, the Diet those players ate, and their Season rates -- so the service
tests drive fake log and Diet seams around a real migrated SQLite database
holding the caller's Targets.  The route tests stay at the HTTP seam with a
stub service, matching ``test_target_resolution``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import json
import zlib

import pytest
import redis
from sqlalchemy import create_engine

from app.config.settings import (
    CacheSettings,
    NBASeasonSettings,
    MatchupScoreSettings,
    RuntimeSettings,
)
from app.domain.player_diet_taxonomy import PLAYER_DIET_QUALIFIER_SLICES
from app.domain.team_matchup_taxonomy import THREE_POINT_SHOT_ZONES
from app.errors import ResourceNotFoundError
from app.migrations import run_migrations
from app.models.user import User
from app.services.database_first_activation import PublicationRead
from app.services.player_diet import (
    PlayerDietBaseline,
    PlayerDietRepository,
    PlayerDietResult,
    StoredPlayerDietFact,
)
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerSeasonLogSummary,
    PlayerSeasonRate,
)
from app.services.statistic_catalog import StatisticCatalog
from app.services.target_backtest import (
    BACKTEST_PUBLICATION_STREAM_KEYS,
    TargetBacktestService,
    backtest_cache_key,
)
from app.services.user_service import UserService


OWNER = "owner-uid"
STRANGER = "stranger-uid"
SEASON = "2025-26"
OKC = 1610612760
LAL = 1610612747
BOS = 1610612738
LEBRON = 2544
TATUM = 1628369
EMBIID = 203954

CORNER_THREE = {
    "base": "shot_zones",
    "slice_key": "Corner 3",
    "comparator": "at_or_above",
    "threshold": 0.4,
}
LOW_RIM = {
    "base": "shot_zones",
    "slice_key": "Restricted Area",
    "comparator": "at_or_below",
    "threshold": 0.25,
}
TRANSITION = {
    "base": "play_types",
    "slice_key": "Transition",
    "comparator": "at_or_above",
    "threshold": 0.2,
}
SHOT_TYPE = {
    "base": "shot_types",
    "slice_key": "Catch and Shoot",
    "comparator": "at_or_above",
    "threshold": 0.01,
}

SHOT_ZONES = (
    "Restricted Area",
    "In The Paint (Non-RA)",
    "Mid-Range",
    "Corner 3",
    "Above the Break 3",
)
#: The approved default display columns for a Corner 3 Target.
CORNER_THREE_COLUMNS = ["PTS", "PTS/36", "3PA", "3PA/36"]

MARKET_PER_GAME = {
    "PTS": 25.0,
    "REB": 8.0,
    "AST": 7.0,
    "3PM": 2.0,
    "STL": 1.0,
    "BLK": 0.5,
    "TOV": 3.0,
    "PRA": 40.0,
    "PA": 32.0,
    "PR": 33.0,
    "RA": 15.0,
    "STKS": 1.5,
    "FGA": 20.0,
    "FG3A": 6.0,
    "FG2A": 14.0,
}


# --- fake seams ------------------------------------------------------------


def _row(
    player_id,
    *,
    name="LeBron James",
    season_type="Regular Season",
    game_id="0022500584",
    game_date=date(2026, 1, 16),
    team_id=LAL,
    team_tricode="LAL",
    opponent_team_id=OKC,
    opponent_team_tricode="OKC",
    is_home=True,
    minutes=34.0,
    points=30,
    rebounds=8,
    assists=9,
    three_pointers_made=4,
    field_goals_attempted=20,
    three_pointers_attempted=8,
):
    return PlayerGameLogRecord(
        season=SEASON,
        season_type=season_type,
        player_id=player_id,
        game_id=game_id,
        player_name=name,
        game_date=game_date,
        team_id=team_id,
        team_tricode=team_tricode,
        opponent_team_id=opponent_team_id,
        opponent_team_tricode=opponent_team_tricode,
        is_home=is_home,
        minutes=minutes,
        points=points,
        rebounds=rebounds,
        assists=assists,
        field_goals_made=12,
        field_goals_attempted=field_goals_attempted,
        three_pointers_made=three_pointers_made,
        three_pointers_attempted=three_pointers_attempted,
    )


class FakeLogs:
    """The league-wide opponent rows and Season rates the backtest composes."""

    def __init__(self, rows=None, *, game_counts=None, scoring=None):
        self.rows = tuple(rows or ())
        self.game_counts = game_counts or {}
        self.scoring = scoring or {}
        self.opponent_calls = []
        self.summary_calls = []
        self.snapshots = []

    def list_opponent_rows(self, season, opponent_team_id, *, publication_snapshot=None):
        self.opponent_calls.append((season, opponent_team_id))
        self.snapshots.append(publication_snapshot)
        return self.rows

    def list_player_rows(self, season, player_id, *, publication_snapshot=None):
        return tuple(row for row in self.rows if row.player_id == player_id)

    def get_player_summaries(self, season, player_ids, *, publication_snapshot=None):
        player_ids = tuple(player_ids)
        self.summary_calls.append((season, player_ids))
        self.snapshots.append(publication_snapshot)
        summaries = {}
        for player_id in player_ids:
            rate_rows = tuple(
                row
                for row in getattr(self, "season_rows", self.rows)
                if row.player_id == player_id and row.season_type == "Regular Season"
            )
            total_minutes = sum(row.minutes for row in rate_rows)
            totals = {
                "PTS": sum(row.points for row in rate_rows),
                "FGA": sum(row.field_goals_attempted for row in rate_rows),
                "FG2A": sum(
                    row.field_goals_attempted - row.three_pointers_attempted
                    for row in rate_rows
                ),
                "FG3A": sum(row.three_pointers_attempted for row in rate_rows),
                "AST": sum(row.assists for row in rate_rows),
            }
            per_minute = (
                {market: value / total_minutes for market, value in totals.items()}
                if total_minutes > 0
                else {}
            )
            summaries[player_id] = PlayerSeasonLogSummary(
                season=season,
                player_id=player_id,
                season_rate=PlayerSeasonRate(
                    season=season,
                    player_id=player_id,
                    game_count=self.game_counts.get(player_id, 20),
                    total_minutes=total_minutes,
                    per_game={
                        **MARKET_PER_GAME,
                        "PTS": self.scoring.get(player_id, 25.0),
                    },
                    per_minute=per_minute,
                ),
                last_ten_minutes=(34.0,),
                rate_rows=rate_rows,
            )
        return summaries


class FakeDiets:
    """Stored Season Diet facts for the players named by the log rows."""

    def __init__(self, zones=None, play_types=None, *, volume=None):
        self.zones = zones or {}
        self.play_types = play_types or {}
        self.volume = volume or {}
        self.calls = []
        self.snapshots = []

    def get_for_players(self, season, player_ids, *, publication_snapshot=None):
        player_ids = tuple(player_ids)
        self.calls.append((season, player_ids))
        self.snapshots.append(publication_snapshot)
        return PlayerDietResult(
            season=season,
            players={
                player_id: (
                    *self._facts(player_id, "shot_zones", self.zones),
                    *self._facts(player_id, "play_types", self.play_types),
                )
                for player_id in player_ids
                if player_id in self.zones or player_id in self.play_types
            },
            observations=(),
            baselines={
                ("shot_zones", slice_key): PlayerDietBaseline(0.2, 0.05)
                for slice_key in SHOT_ZONES
            }
            | {("play_types", "Transition"): PlayerDietBaseline(0.15, 0.04)},
        )

    def _facts(self, player_id, base, source):
        return tuple(
            StoredPlayerDietFact(
                player_id=player_id,
                base=base,
                slice_key=slice_key,
                share=share,
                volume=self.volume.get(player_id, 80.0),
                games_played=20,
                volume_unit="field_goal_attempts",
                provider="nba_stats",
                retrieved_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
            )
            for slice_key, share in source.get(player_id, {}).items()
        )


class PublishedShotTypeReader:
    """A publication reader that leaves player-Diet decoding to the repository."""

    def __init__(self, rows):
        self.rows = tuple(rows)

    def read_many(self, stream_keys, *, season):
        reads = {}
        for stream_key in stream_keys:
            if stream_key == "grouped_shot_types":
                reads[stream_key] = PublicationRead(
                    stream_key=stream_key,
                    publication_id="published-shot-types",
                    season=season,
                    cutoff=None,
                    version=1,
                    status="active",
                    freshness="fresh",
                    age_seconds=0,
                    payload={"base": "shot_types", "rows": list(self.rows)},
                    decoded=None,
                    retrieved_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
                )
            else:
                reads[stream_key] = PublicationRead(
                    stream_key=stream_key,
                    publication_id=None,
                    season=season,
                    cutoff=None,
                    version=None,
                    status="missing",
                    freshness="missing",
                    age_seconds=None,
                    payload=None,
                )
        return reads


def _play_type_diet(transition):
    """A Synergy partition clearing the Base's coverage floor."""

    remainder = round((0.98 - transition) / 2, 6)
    return {
        "Transition": transition,
        "Spotup": remainder,
        "Isolation": remainder,
    }


def _zone_diet(corner_three, restricted_area):
    """A complete five-slice shot-zone diet with the two shares under test."""

    remainder = round((1.0 - corner_three - restricted_area) / 3, 6)
    return {
        "Corner 3": corner_three,
        "Restricted Area": restricted_area,
        "In The Paint (Non-RA)": remainder,
        "Mid-Range": remainder,
        "Above the Break 3": remainder,
    }


# --- service ---------------------------------------------------------------


@pytest.fixture
def backtest_engine(tmp_path):
    """A migrated application database holding both accounts."""

    engine = create_engine(f"sqlite:///{tmp_path / 'backtest.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        for uid in (OWNER, STRANGER):
            connection.execute(
                User.__table__.insert(),
                {
                    "firebase_uid": uid,
                    "email": f"{uid}@example.com",
                    "display_name": uid,
                    "photo_url": None,
                    "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                    "last_login": datetime(2026, 8, 1, tzinfo=timezone.utc),
                    "is_active": True,
                },
            )
    yield engine
    engine.dispose()


@pytest.fixture
def backtest_settings():
    return RuntimeSettings(
        environment="testing",
        nba=NBASeasonSettings(current_season=SEASON),
        matchup_scores=MatchupScoreSettings(),
    )


@pytest.fixture
def targets(backtest_engine, backtest_settings):
    return UserService(backtest_engine, settings=backtest_settings)


@pytest.fixture
def build_backtest(targets, backtest_settings):
    """Build the backtest over the caller's real Targets and fake seams."""

    unset = object()

    def _service(
        *,
        logs=None,
        diets=unset,
        publication_reader=None,
        statistic_catalog=None,
        settings=None,
        redis_client=None,
        cache_clock=None,
    ):
        return TargetBacktestService(
            targets=targets,
            player_logs=logs if logs is not None else FakeLogs(),
            player_diets=FakeDiets() if diets is unset else diets,
            statistic_catalog=(
                StatisticCatalog.load_default()
                if statistic_catalog is None
                else statistic_catalog
            ),
            settings=settings or backtest_settings,
            publication_reader=publication_reader,
            redis_client=redis_client,
            cache_clock=cache_clock,
        )

    return _service


@pytest.fixture
def backtest(build_backtest):
    """Backtest one of the caller's saved Targets by id."""

    def _backtest(target_id, *, uid=OWNER, **seams):
        return build_backtest(**seams).backtest(uid, target_id)

    return _backtest


def _create(
    targets,
    *,
    uid=OWNER,
    opponent="OKC",
    qualifiers=(CORNER_THREE,),
    conditions=None,
):
    return targets.create_target(
        uid,
        opponent=opponent,
        qualifiers=list(qualifiers),
        note=None,
        conditions=conditions,
    )


def test_a_qualifying_player_reports_shares_averages_and_every_game(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(
        rows=(
            _row(LEBRON, game_id="0022500584", game_date=date(2026, 1, 16)),
            _row(
                LEBRON,
                game_id="0022500120",
                game_date=date(2025, 11, 3),
                is_home=False,
                points=22,
                three_pointers_made=2,
                field_goals_attempted=17,
                three_pointers_attempted=5,
            ),
        )
    )
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert payload["target"] == created
    assert payload["season"] == SEASON
    assert "box-score" in payload["proxy"]
    assert payload["stat_columns"] == CORNER_THREE_COLUMNS
    legacy_players = [{key: value for key, value in player.items() if key not in ("season_totals", "season_games")}
                      for player in payload["players"]]
    for player in legacy_players:
        player["games"] = [{key: value for key, value in game.items() if key != "line"}
                           for game in player["games"]]
    assert legacy_players == [
        {
            "canonical_id": LEBRON,
            "name": "LeBron James",
            "team_id": LAL,
            "tricode": "LAL",
            "season_scoring": 25.0,
            "shares": [
                {
                    "base": "shot_zones",
                    "slice_key": "Corner 3",
                    "share": 0.42,
                    "league_average_share": 0.2,
                }
            ],
            "season_averages": {
                "PTS": 25.0,
                "PTS/36": 27.529412,
                "3PA": 6.0,
                "3PA/36": 6.882353,
            },
            "games": [
                {
                    "game_id": "0022500584",
                    "game_date": "2026-01-16",
                    "matchup": "LAL vs. OKC",
                    "minutes": 34.0,
                    "stats": {
                        "PTS": 30.0,
                        "PTS/36": 31.764706,
                        "3PA": 8.0,
                        "3PA/36": 8.470588,
                    },
                },
                {
                    "game_id": "0022500120",
                    "game_date": "2025-11-03",
                    "matchup": "LAL @ OKC",
                    "minutes": 34.0,
                    "stats": {
                        "PTS": 22.0,
                        "PTS/36": 23.294118,
                        "3PA": 5.0,
                        "3PA/36": 5.294118,
                    },
                },
            ],
        }
    ]


def test_a_published_shot_type_diet_fits_targets_and_builds_display_baselines(
    targets, backtest_engine, build_backtest
):
    created = _create(targets, qualifiers=(SHOT_TYPE,))
    logs = FakeLogs(
        rows=(
            _row(LEBRON),
            _row(TATUM, name="Jayson Tatum", team_id=BOS, team_tricode="BOS"),
        ),
        scoring={LEBRON: 25.0, TATUM: 27.0},
    )
    publication_rows = [
        {
            "player_id": player_id,
            "slice_key": slice_key,
            "share": share,
            "volume": 140.0,
            "games_played": 20,
            "volume_unit": "field_goal_attempts",
            "provider": "nba_stats",
        }
        for player_id, shares in (
            (LEBRON, (0.42, 0.33, 0.25)),
            (TATUM, (0.18, 0.47, 0.35)),
        )
        for slice_key, share in zip(
            ("catch_and_shoot", "pullups", "less_than_10_ft"), shares
        )
    ]
    diets = PlayerDietRepository(
        backtest_engine,
        publication_reader=PublishedShotTypeReader(publication_rows),
    )

    payload = build_backtest(logs=logs, diets=diets).backtest(
        OWNER, created["id"]
    )

    assert [player["canonical_id"] for player in payload["players"]] == [
        TATUM,
        LEBRON,
    ]
    assert payload["players"][0]["shares"] == [
        {
            "base": "shot_types",
            "slice_key": "Catch and Shoot",
            "share": 0.18,
            "league_average_share": 0.3,
        }
    ]


def test_the_summary_reads_every_listed_game_against_its_players_season_rate(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(
        rows=(
            _row(LEBRON, game_id="0022500584", points=30, three_pointers_made=4),
            _row(
                LEBRON,
                game_id="0022500120",
                game_date=date(2025, 11, 3),
                points=22,
                three_pointers_made=2,
            ),
            _row(
                TATUM,
                name="Jayson Tatum",
                team_id=BOS,
                team_tricode="BOS",
                game_id="0022500300",
                game_date=date(2025, 12, 1),
                points=27,
                three_pointers_made=1,
            ),
        ),
        scoring={LEBRON: 25.0, TATUM: 27.0},
    )
    diets = FakeDiets(
        zones={LEBRON: _zone_diet(0.42, 0.2), TATUM: _zone_diet(0.5, 0.2)}
    )

    payload = backtest(created["id"], logs=logs, diets=diets)

    # PTS keeps the existing per-game baseline. Per-36 uses each player's
    # aggregate season totals and minutes, not an average of game rates.
    assert payload["summary"] == {
        "players": 2,
        "games": 3,
        "columns": {
            "PTS": {"mean_difference": 0.666667, "over_average_share": 0.666667},
            "PTS/36": {"mean_difference": 0.0, "over_average_share": 0.666667},
            "3PA": {"mean_difference": 2.0, "over_average_share": 1.0},
            "3PA/36": {"mean_difference": 0.0, "over_average_share": 1.0},
        },
    }


def test_an_empty_backtest_summarises_nobody_and_leaves_every_column_blank(
    targets, backtest
):
    created = _create(targets)

    payload = backtest(created["id"], logs=FakeLogs())

    assert payload["players"] == []
    assert payload["summary"] == {
        "players": 0,
        "games": 0,
        "columns": {
            "PTS": {"mean_difference": None, "over_average_share": None},
            "PTS/36": {"mean_difference": None, "over_average_share": None},
            "3PA": {"mean_difference": None, "over_average_share": None},
            "3PA/36": {"mean_difference": None, "over_average_share": None},
        },
    }


#: An unsaved Target as ``UserService.validate_target_draft`` shapes it: the
#: listed item without an id or timestamps.
DRAFT = {
    "opponent": "OKC",
    "title": "OKC vs Corner 3 ≥ 40%",
    "note": None,
    "qualifiers": [CORNER_THREE],
}


def _two_games():
    return dict(
        logs=FakeLogs(
            rows=(
                _row(LEBRON, game_id="0022500584", game_date=date(2026, 1, 16)),
                _row(
                    LEBRON,
                    game_id="0022500120",
                    game_date=date(2025, 11, 3),
                    is_home=False,
                    points=22,
                    three_pointers_made=2,
                ),
            )
        ),
        diets=FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)}),
    )


def test_a_draft_backtests_exactly_as_a_saved_target_with_the_same_qualifiers(
    targets, build_backtest
):
    created = _create(targets)

    saved = build_backtest(**_two_games()).backtest(OWNER, created["id"])
    draft = build_backtest(**_two_games()).backtest_target(DRAFT)

    # The draft is echoed as given: derived title, no id, no timestamps.
    assert draft["target"] == DRAFT
    assert set(draft) == set(saved)
    for key in ("season", "proxy", "stat_columns", "summary", "players"):
        assert draft[key] == saved[key]
    assert draft["summary"]["games"] == 2


def test_a_draft_reads_no_stored_target_and_resolves_one_snapshot(
    backtest_settings,
):
    reader = FakePublicationReader()
    seams = _two_games()
    service = TargetBacktestService(
        # Nothing to read a Target from: a draft has not been stored anywhere.
        targets=SimpleNamespace(),
        player_logs=seams["logs"],
        player_diets=seams["diets"],
        statistic_catalog=StatisticCatalog.load_default(),
        settings=backtest_settings,
        publication_reader=reader,
    )

    payload = service.backtest_target(DRAFT)

    assert [player["canonical_id"] for player in payload["players"]] == [LEBRON]
    assert len(reader.calls) == 1
    assert seams["logs"].snapshots == ["snapshot-1", "snapshot-1"]


def test_a_draft_backtested_over_a_given_snapshot_captures_none_of_its_own(
    build_backtest,
):
    seams = _two_games()
    reader = FakePublicationReader()

    payload = build_backtest(**seams, publication_reader=reader).backtest_target(
        DRAFT, publication_snapshot="preview-snapshot"
    )

    # The caller's generation is the whole read; the reader is never asked.
    assert reader.calls == []
    assert seams["logs"].snapshots == ["preview-snapshot", "preview-snapshot"]
    assert seams["diets"].snapshots == ["preview-snapshot"]
    assert [player["canonical_id"] for player in payload["players"]] == [LEBRON]


def test_the_opponents_games_are_read_league_wide_for_the_current_season(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(
        rows=(
            _row(LEBRON),
            _row(TATUM, name="Jayson Tatum", team_id=BOS, team_tricode="BOS"),
        ),
        scoring={LEBRON: 25.0, TATUM: 27.0},
    )
    diets = FakeDiets(
        zones={LEBRON: _zone_diet(0.42, 0.2), TATUM: _zone_diet(0.5, 0.2)}
    )

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert logs.opponent_calls == [(SEASON, OKC)]
    assert diets.calls == [(SEASON, (LEBRON, TATUM))]
    # The averages a game is read against are this season's, not any other's.
    assert logs.summary_calls == [(SEASON, (LEBRON, TATUM))]
    # Season scoring descending, as the Matchup and Target resolution order.
    assert [player["tricode"] for player in payload["players"]] == ["BOS", "LAL"]


def test_a_thin_diet_player_is_excluded(targets, backtest):
    created = _create(targets)
    logs = FakeLogs(rows=(_row(LEBRON), _row(TATUM, name="Jayson Tatum")))
    # Tatum's shot-zone attempts fall under the Base's per-game floor.
    diets = FakeDiets(
        zones={LEBRON: _zone_diet(0.42, 0.2), TATUM: _zone_diet(0.5, 0.2)},
        volume={TATUM: 2.0},
    )

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert [player["canonical_id"] for player in payload["players"]] == [LEBRON]


def test_a_thin_season_rate_excludes_a_player_whose_diet_clears_every_floor(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(
        rows=(_row(LEBRON), _row(TATUM, name="Jayson Tatum")),
        game_counts={LEBRON: 20, TATUM: 3},
    )
    diets = FakeDiets(
        zones={LEBRON: _zone_diet(0.42, 0.2), TATUM: _zone_diet(0.5, 0.2)}
    )

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert [player["canonical_id"] for player in payload["players"]] == [LEBRON]


@pytest.mark.parametrize(
    ("comparator", "threshold", "share", "expected"),
    [
        ("at_or_above", 0.4, 0.4, True),
        ("at_or_above", 0.4, 0.39, False),
        ("at_or_below", 0.25, 0.25, True),
        ("at_or_below", 0.25, 0.26, False),
    ],
)
def test_both_comparators_are_inclusive(
    targets, backtest, comparator, threshold, share, expected
):
    created = _create(
        targets,
        qualifiers=[
            {
                "base": "shot_zones",
                "slice_key": "Corner 3",
                "comparator": comparator,
                "threshold": threshold,
            }
        ],
    )
    logs = FakeLogs(rows=(_row(LEBRON),))
    diets = FakeDiets(zones={LEBRON: _zone_diet(share, 0.2)})

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert bool(payload["players"]) is expected


def test_every_qualifier_has_to_be_met(targets, backtest):
    created = _create(targets, qualifiers=(CORNER_THREE, LOW_RIM))
    logs = FakeLogs(
        rows=(_row(LEBRON), _row(EMBIID, name="Joel Embiid")),
        scoring={LEBRON: 25.0, EMBIID: 30.0},
    )
    diets = FakeDiets(
        zones={
            LEBRON: _zone_diet(0.42, 0.2),
            # Corner threes yes, but a rim share above the second Qualifier.
            EMBIID: _zone_diet(0.45, 0.4),
        }
    )

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert [player["canonical_id"] for player in payload["players"]] == [LEBRON]


def test_a_player_with_no_stored_share_for_a_slice_does_not_fit(targets, backtest):
    created = _create(targets)
    logs = FakeLogs(rows=(_row(LEBRON),))

    payload = backtest(created["id"], logs=logs, diets=FakeDiets())

    assert payload["players"] == []


def test_stat_columns_union_every_qualifier_default_in_order(
    targets, backtest
):
    created = _create(targets, qualifiers=(CORNER_THREE, TRANSITION))
    logs = FakeLogs(rows=(_row(LEBRON),))
    diets = FakeDiets(
        zones={LEBRON: _zone_diet(0.42, 0.2)},
        play_types={LEBRON: _play_type_diet(0.25)},
    )

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert payload["stat_columns"] == [
        "PTS",
        "PTS/36",
        "3PA",
        "3PA/36",
        "FGA",
        "FGA/36",
    ]
    player = payload["players"][0]
    assert list(player["season_averages"]) == payload["stat_columns"]
    assert list(player["games"][0]["stats"]) == payload["stat_columns"]


@pytest.mark.parametrize(
    ("base", "slice_key", "expected"),
    [
        ("shot_zones", "Corner 3", ["PTS", "PTS/36", "3PA", "3PA/36"]),
        ("shot_zones", "Restricted Area", ["PTS", "PTS/36", "FG2A", "FG2A/36"]),
        ("play_types", "Transition", ["PTS", "FGA", "PTS/36", "FGA/36"]),
        ("shot_types", "Catch and Shoot", ["PTS", "PTS/36", "FGA", "FGA/36"]),
        ("assist_locations", "Corner3Assists", ["AST", "AST/36"]),
    ],
)
def test_stat_columns_follow_the_approved_defaults(
    targets, backtest, base, slice_key, expected
):
    created = _create(
        targets,
        qualifiers=[
            {
                "base": base,
                "slice_key": slice_key,
                "comparator": "at_or_above",
                "threshold": 0.1,
            }
        ],
    )

    payload = backtest(created["id"])

    assert payload["stat_columns"] == expected


@pytest.mark.parametrize(
    ("base", "slice_key"),
    [
        (base, slice_key)
        for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items()
        for slice_key in slices
    ],
)
def test_every_qualifier_slice_preserves_its_default_order(base, slice_key):
    columns = TargetBacktestService._stat_columns(
        ({"base": base, "slice_key": slice_key},)
    )
    if base == "assist_locations":
        expected = ("AST", "AST/36")
    elif base == "shot_zones" and slice_key in THREE_POINT_SHOT_ZONES:
        expected = ("PTS", "PTS/36", "3PA", "3PA/36")
    elif base == "shot_zones":
        expected = ("PTS", "PTS/36", "FG2A", "FG2A/36")
    elif base == "play_types":
        expected = ("PTS", "FGA", "PTS/36", "FGA/36")
    else:
        expected = ("PTS", "PTS/36", "FGA", "FGA/36")
    assert columns == expected


def test_saved_stat_preferences_are_echoed_without_changing_backend_defaults(
    targets, backtest
):
    preferences = {"columns": ["TS%", "PTS/36"], "graded_by": "TS%"}
    created = targets.create_target(
        OWNER,
        opponent="OKC",
        qualifiers=[CORNER_THREE],
        stat_preferences=preferences,
    )
    payload = backtest(
        created["id"],
        logs=FakeLogs(rows=(_row(LEBRON),)),
        diets=FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)}),
    )

    assert payload["target"]["stat_preferences"] == preferences
    assert payload["stat_columns"] == CORNER_THREE_COLUMNS
    assert list(payload["players"][0]["games"][0]["stats"]) == CORNER_THREE_COLUMNS


def test_two_point_attempts_are_derived_and_season_per36_uses_total_minutes(
    targets, backtest
):
    created = _create(targets, qualifiers=(LOW_RIM,))
    game = _row(
        LEBRON,
        minutes=30,
        points=18,
        field_goals_attempted=12,
        three_pointers_attempted=4,
    )
    other = _row(
        LEBRON,
        game_id="other-season-game",
        opponent_team_id=BOS,
        opponent_team_tricode="BOS",
        minutes=60,
        points=60,
        field_goals_attempted=30,
        three_pointers_attempted=3,
    )
    logs = FakeLogs(rows=(game,))
    logs.season_rows = (game, other)
    payload = backtest(
        created["id"],
        logs=logs,
        diets=FakeDiets(zones={LEBRON: _zone_diet(0.2, 0.2)}),
    )

    assert payload["stat_columns"] == ["PTS", "PTS/36", "FG2A", "FG2A/36"]
    player = payload["players"][0]
    assert player["games"][0]["stats"] == {
        "PTS": 18.0,
        "PTS/36": 21.6,
        "FG2A": 8.0,
        "FG2A/36": 9.6,
    }
    # (18 + 60) / (30 + 60) * 36; averaging 21.6 and 36.0 would be wrong.
    assert player["season_averages"] == {
        "PTS": 25.0,
        "PTS/36": 31.2,
        "FG2A": 14.0,
        "FG2A/36": 14.0,
    }
    assert payload["summary"]["columns"] == {
        "PTS": {"mean_difference": -7.0, "over_average_share": 0.0},
        "PTS/36": {"mean_difference": -9.6, "over_average_share": 0.0},
        "FG2A": {"mean_difference": -6.0, "over_average_share": 0.0},
        "FG2A/36": {"mean_difference": -4.4, "over_average_share": 0.0},
    }


def test_game_default_values_follow_catalogue_components(targets, build_backtest):
    from dataclasses import replace

    # Keep the catalog structurally valid while changing this component in the
    # seam. A Target-specific FGA - 3PA calculation would ignore this definition.
    catalog = StatisticCatalog(
        tuple(
            replace(statistic, components=("field_goals_attempted",))
            if statistic.market_category == "FG2A"
            else statistic
            for statistic in StatisticCatalog.load_default().statistics
        )
    )
    created = _create(targets, qualifiers=(LOW_RIM,))
    payload = build_backtest(
        logs=FakeLogs(
            rows=(
                _row(
                    LEBRON,
                    points=18,
                    field_goals_attempted=12,
                    three_pointers_attempted=4,
                ),
            )
        ),
        diets=FakeDiets(zones={LEBRON: _zone_diet(0.2, 0.2)}),
        statistic_catalog=catalog,
    ).backtest(OWNER, created["id"])

    assert payload["players"][0]["games"][0]["stats"]["FG2A"] == 12.0


def test_zero_minutes_leave_base_stats_available_and_per36_null(targets, backtest):
    created = _create(targets, qualifiers=(LOW_RIM,))
    zero_minute_game = _row(
        LEBRON,
        minutes=0,
        points=18,
        field_goals_attempted=12,
        three_pointers_attempted=4,
    )
    positive_minute_game = _row(
        LEBRON,
        game_id="other-season-game",
        opponent_team_id=BOS,
        opponent_team_tricode="BOS",
        minutes=30,
        points=12,
        field_goals_attempted=10,
        three_pointers_attempted=2,
    )
    logs = FakeLogs(
        rows=(zero_minute_game,)
    )
    logs.season_rows = (zero_minute_game, positive_minute_game)

    payload = backtest(
        created["id"],
        logs=logs,
        diets=FakeDiets(zones={LEBRON: _zone_diet(0.2, 0.2)}),
    )

    player = payload["players"][0]
    assert player["games"][0]["stats"] == {
        "PTS": 18.0,
        "PTS/36": None,
        "FG2A": 8.0,
        "FG2A/36": None,
    }
    assert player["season_averages"] == {
        "PTS": 25.0,
        "PTS/36": 36.0,
        "FG2A": 14.0,
        "FG2A/36": 19.2,
    }
    assert payload["summary"]["columns"]["PTS/36"] == {
        "mean_difference": None,
        "over_average_share": None,
    }
    assert payload["summary"]["columns"]["FG2A/36"] == {
        "mean_difference": None,
        "over_average_share": None,
    }


class FakePublicationReader:
    """A reader whose snapshot is a sentinel the seams can be asked about."""

    def __init__(self, snapshot_value="snapshot-1"):
        self.snapshot_value = snapshot_value
        self.calls = []

    def snapshot(self, stream_keys, *, season, projection_only_keys=None):
        self.calls.append((tuple(stream_keys), season, projection_only_keys))
        return self.snapshot_value


def test_one_publication_snapshot_is_resolved_and_given_to_every_seam(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(rows=(_row(LEBRON),))
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})
    reader = FakePublicationReader()

    backtest(created["id"], logs=logs, diets=diets, publication_reader=reader)

    # One snapshot for the request, not one per seam, so the Diet and the game
    # logs cannot come from two Publication generations.
    assert len(reader.calls) == 1
    stream_keys, season, projection_only = reader.calls[0]
    assert season == SEASON
    assert "player_game_logs" in stream_keys
    # The season-wide game-log payload is never shipped: both game-log reads
    # resolve their rows from the projection's own indexes.
    assert projection_only == frozenset({"player_game_logs"})
    # Both game-log reads and the Diet read all receive it.
    assert logs.snapshots == ["snapshot-1", "snapshot-1"]
    assert diets.snapshots == ["snapshot-1"]


class LegacySnapshotReader:
    """An older reader: ``read_snapshot``, and no projection narrowing."""

    def __init__(self):
        self.calls = []

    def read_snapshot(self, stream_keys, *, season):
        self.calls.append((tuple(stream_keys), season))
        return "legacy-snapshot"


def test_a_reader_without_projection_narrowing_still_scopes_the_read(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(rows=(_row(LEBRON),))
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})
    reader = LegacySnapshotReader()

    payload = backtest(
        created["id"], logs=logs, diets=diets, publication_reader=reader
    )

    # It cannot be asked to skip the payload, but it can still be asked for
    # one generation, and every seam is given it.
    assert [season for _keys, season in reader.calls] == [SEASON]
    assert logs.snapshots == ["legacy-snapshot", "legacy-snapshot"]
    assert diets.snapshots == ["legacy-snapshot"]
    assert [player["canonical_id"] for player in payload["players"]] == [LEBRON]


def test_a_reader_offering_no_snapshot_at_all_reads_the_durable_tables(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(rows=(_row(LEBRON),))
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})

    payload = backtest(
        created["id"],
        logs=logs,
        diets=diets,
        publication_reader=SimpleNamespace(snapshot=None),
    )

    # No generation to pin, so the seams read their own durable tables. The
    # response is still composed; it is the cost that differs.
    assert logs.snapshots == [None, None]
    assert [player["canonical_id"] for player in payload["players"]] == [LEBRON]


def test_repeated_backtests_reuse_cached_publication_decode_and_keep_response_stable(
    targets, backtest_settings, backtest_engine
):
    """The Targets page backtests one Target at a time, all against the same
    active publication, each naming a different opponent's player pool.

    Before the fix, ``get_player_summaries`` re-selects and re-JSON-decodes a
    player's whole season on every one of those requests, even for a player
    an earlier request in this same generation already decoded.  The fix
    caches a player's decoded season rows per publication -- immutable once
    composed -- so only a player not yet seen this generation costs a fresh
    statement, and the season summary a cached player gets stays the exact
    one a fresh decode would have produced.
    """

    from dataclasses import asdict

    from sqlalchemy import event

    from app.services.collection_control import PublicationService
    from app.services.database_first_activation import (
        DatabaseFirstPublicationReader,
    )
    from app.services.player_game_log_repository import PlayerGameLogRepository

    retrieved_at = datetime(2026, 1, 16, tzinfo=timezone.utc)
    day = date(2026, 1, 16)
    filler_team, filler_tricode = 1610612744, "GSW"  # Named by neither Target.

    def _game(player_id, name, game_id, days_ago, opponent_id, opponent_tricode):
        return _row(
            player_id,
            name=name,
            game_id=game_id,
            game_date=day - timedelta(days=days_ago),
            opponent_team_id=opponent_id,
            opponent_team_tricode=opponent_tricode,
        )

    def _filler_games(player_id, name, prefix, count):
        return [
            _game(player_id, name, f"{prefix}{i}", i, filler_team, filler_tricode)
            for i in range(1, count + 1)
        ]

    # Each player clears the min-games floor across their whole season, not
    # just against the one opponent a Target names.
    records = [
        _game(LEBRON, "LeBron James", "L-OKC", 0, OKC, "OKC"),
        _game(LEBRON, "LeBron James", "L-LAL", 1, LAL, "LAL"),
        *_filler_games(LEBRON, "LeBron James", "L-F", 3),
        _game(TATUM, "Jayson Tatum", "T-OKC", 0, OKC, "OKC"),
        *_filler_games(TATUM, "Jayson Tatum", "T-F", 4),
        _game(EMBIID, "Joel Embiid", "E-LAL", 0, LAL, "LAL"),
        *_filler_games(EMBIID, "Joel Embiid", "E-F", 4),
    ]

    def _payload_rows(records):
        rows = []
        for record in records:
            row = asdict(record)
            row["game_date"] = record.game_date.isoformat()
            rows.append(row)
        return rows

    publications = PublicationService(backtest_engine, clock=lambda: retrieved_at)
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
        cutoff=retrieved_at,
        payload={"rows": _payload_rows(records)},
    )
    reader = DatabaseFirstPublicationReader(backtest_engine, clock=lambda: retrieved_at)
    real_logs = PlayerGameLogRepository(
        backtest_engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: retrieved_at,
        stats_surface_max_age=timedelta(hours=30),
        publication_reader=reader,
    )
    diets = FakeDiets(
        zones={
            LEBRON: _zone_diet(0.42, 0.2),
            TATUM: _zone_diet(0.42, 0.2),
            EMBIID: _zone_diet(0.42, 0.2),
        }
    )
    service = TargetBacktestService(
        targets=targets,
        player_logs=real_logs,
        player_diets=diets,
        statistic_catalog=StatisticCatalog.load_default(),
        settings=backtest_settings,
        publication_reader=reader,
    )
    target_okc = _create(targets, opponent="OKC", qualifiers=(CORNER_THREE,))
    target_lal = _create(targets, opponent="LAL", qualifiers=(CORNER_THREE,))

    # Only record the summary read's own statements against the projection --
    # the opponent-rows read filters on ``opponent_team_id`` and is a
    # separate, already-indexed cost this fix does not touch.  Each entry is
    # how many players one statement decoded, read from its own bind
    # parameters (``publication_id`` plus one per named player), so the
    # count reflects the actual decode work rather than just how many
    # statements ran.
    summary_reads: list[int] = []

    def record_statement(_connection, _cursor, statement, parameters, *_args):
        if statement.strip().startswith(
            "SELECT publication_player_game_logs.row_payload"
        ) and "opponent_team_id" not in statement:
            summary_reads.append(len(parameters) - 1)

    event.listen(backtest_engine, "before_cursor_execute", record_statement)
    try:
        first = service.backtest(OWNER, target_okc["id"])
        second = service.backtest(OWNER, target_lal["id"])
        # A third ask for a player pool this generation has already fully
        # decoded -- OKC's, exactly as the first backtest asked -- costs no
        # further statement against the projection at all.
        third = service.backtest(OWNER, target_okc["id"])
    finally:
        event.remove(backtest_engine, "before_cursor_execute", record_statement)

    # The first backtest decodes both its players (LeBron and Tatum) in one
    # statement; the second decodes only Embiid, since LeBron's season is
    # already cached; the third repeats the first Target's exact player pool
    # and triggers no statement at all.  Before the fix, every one of these
    # three requests re-selects and re-decodes its whole player pool from
    # scratch: three statements naming 2, 2, and 2 players, instead of two
    # statements naming 2 and 1.
    assert summary_reads == [2, 1]

    assert {player["canonical_id"] for player in first["players"]} == {LEBRON, TATUM}
    assert {player["canonical_id"] for player in second["players"]} == {
        LEBRON,
        EMBIID,
    }
    assert third == first

    lebron_first = next(
        player for player in first["players"] if player["canonical_id"] == LEBRON
    )
    lebron_second = next(
        player for player in second["players"] if player["canonical_id"] == LEBRON
    )
    # The cached read for the second backtest reports the exact season
    # summary the first backtest's fresh decode did.
    assert lebron_first["season_games"] == lebron_second["season_games"] == 5
    assert lebron_first["season_averages"] == lebron_second["season_averages"]
    assert lebron_first["season_totals"] == lebron_second["season_totals"]


def test_a_deployment_with_no_diet_service_reports_no_players(targets, backtest):
    created = _create(targets)
    logs = FakeLogs(rows=(_row(LEBRON),))

    payload = backtest(created["id"], logs=logs, diets=None)

    # No stored Diet means no share for any slice, so nobody fits. The columns
    # still describe what the Target asks for.
    assert payload["players"] == []
    assert payload["stat_columns"] == CORNER_THREE_COLUMNS


def test_a_playoff_game_is_not_read_against_a_regular_season_average(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(
        rows=(
            _row(LEBRON, game_id="0042500101", season_type="Playoffs", points=41),
            _row(LEBRON, game_id="0022500584"),
        )
    )
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert [game["game_id"] for game in payload["players"][0]["games"]] == [
        "0022500584"
    ]


def test_a_playoff_only_opponent_history_lists_nobody(targets, backtest):
    created = _create(targets)
    logs = FakeLogs(
        rows=(_row(LEBRON, game_id="0042500101", season_type="Playoffs"),)
    )
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert payload["players"] == []
    assert diets.calls == []


def test_identity_follows_the_most_recent_game_against_this_opponent(
    targets, backtest
):
    created = _create(targets)
    logs = FakeLogs(
        rows=(
            _row(LEBRON, game_id="0022500584", game_date=date(2026, 1, 16)),
            # The same player, earlier, for the team that traded him.
            _row(
                LEBRON,
                game_id="0022500120",
                game_date=date(2025, 11, 3),
                team_id=BOS,
                team_tricode="BOS",
            ),
        )
    )
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})

    player = backtest(created["id"], logs=logs, diets=diets)["players"][0]

    assert (player["team_id"], player["tricode"]) == (LAL, "LAL")
    # Each game still reports the team he suited up for that night.
    assert [game["matchup"] for game in player["games"]] == [
        "LAL vs. OKC",
        "BOS vs. OKC",
    ]


def test_an_incomplete_diet_partition_does_not_fit(targets, backtest):
    created = _create(targets)
    logs = FakeLogs(rows=(_row(LEBRON),))
    # A passing Corner 3 share, but the Base is missing a slice, so the stored
    # Diet is not a usable partition and the Matchup would not score it.
    partial = _zone_diet(0.42, 0.2)
    del partial["Mid-Range"]
    diets = FakeDiets(zones={LEBRON: partial})

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert payload["players"] == []


def test_nobody_having_faced_the_opponent_is_an_empty_player_list(
    targets, backtest
):
    created = _create(targets)
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})

    payload = backtest(created["id"], logs=FakeLogs(), diets=diets)

    assert payload["players"] == []
    # Nobody to judge, so the Diet seam is never read.
    assert diets.calls == []


def test_a_target_owned_by_another_account_is_not_found(targets, backtest):
    created = _create(targets, uid=STRANGER)

    with pytest.raises(ResourceNotFoundError):
        backtest(created["id"], uid=OWNER)


def test_an_unknown_target_is_not_found(backtest):
    with pytest.raises(ResourceNotFoundError):
        backtest(404)


# --- routes ----------------------------------------------------------------


BACKTESTED = {
    "target": {
        "id": 7,
        "opponent": "OKC",
        "title": "OKC vs Corner 3 ≥ 40%",
        "note": None,
        "qualifiers": [CORNER_THREE],
        "created_at": "2026-09-05T12:00:00+00:00",
        "updated_at": "2026-09-05T12:00:00+00:00",
    },
    "season": SEASON,
    "proxy": "Outcomes are box-score proxies.",
    "stat_columns": CORNER_THREE_COLUMNS,
    "summary": {
        "players": 0,
        "games": 0,
        "columns": {
            "PTS": {"mean_difference": None, "over_average_share": None},
            "PTS/36": {"mean_difference": None, "over_average_share": None},
            "3PA": {"mean_difference": None, "over_average_share": None},
            "3PA/36": {"mean_difference": None, "over_average_share": None},
        },
    },
    "players": [],
}


@pytest.fixture
def backtest_service(dependencies):
    """Replace the backtest in the application graph, as ARCHITECTURE.md asks."""

    dependencies.target_backtest_service = Mock(name="target_backtest_service")
    return dependencies.target_backtest_service


def test_the_backtest_route_returns_the_backtest_for_the_targets_id(
    client, authenticate, backtest_service
):
    headers = authenticate()
    backtest_service.backtest.return_value = BACKTESTED

    response = client.get("/api/user/targets/7/backtest", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"success": True, **BACKTESTED}
    backtest_service.backtest.assert_called_once_with("test-uid", 7)


def test_the_backtest_route_reports_a_target_of_another_account_as_not_found(
    client, authenticate, backtest_service
):
    headers = authenticate()
    backtest_service.backtest.side_effect = ResourceNotFoundError(
        "The requested target was not found."
    )

    response = client.get("/api/user/targets/7/backtest", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == {
        "code": "resource_not_found",
        "message": "The requested target was not found.",
    }


def test_the_backtest_route_reports_an_unexpected_failure_safely(
    client, authenticate, backtest_service
):
    headers = authenticate()
    backtest_service.backtest.side_effect = RuntimeError("stored rows are wrong")

    response = client.get("/api/user/targets/7/backtest", headers=headers)

    assert response.status_code == 500
    assert response.get_json()["error"] == {
        "code": "operation_failed",
        "message": "Failed to backtest the target.",
    }


def test_the_backtest_route_refuses_an_unauthenticated_caller(
    client, authenticate, backtest_service
):
    authenticate()

    response = client.get("/api/user/targets/7/backtest")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
    backtest_service.backtest.assert_not_called()


def test_date_conditions_are_saved_cleared_and_preserve_unedited_fields(targets):
    conditions = {'defender': None, 'from': '2026-01-01', 'to': '2026-02-01'}
    expected_conditions = {**conditions, 'player_minutes': None}
    created = targets.create_target(OWNER, opponent='OKC', qualifiers=[CORNER_THREE], conditions=conditions)
    assert created['conditions'] == expected_conditions
    edited = targets.update_target(OWNER, created['id'], changes={'note': 'Keep window'})
    assert edited['conditions'] == expected_conditions
    assert targets.get_target(OWNER, created['id'])['conditions'] == expected_conditions
    assert targets.update_target(OWNER, created['id'], changes={'conditions': None})['conditions'] is None


def test_player_minutes_filters_appearances_and_preserves_season_baseline(
    targets, backtest
):
    conditions = {'player_minutes': 10}
    created = _create(targets, conditions=conditions)
    lebron_rows = (
        _row(LEBRON, game_id='below', minutes=9.9, points=11),
        _row(LEBRON, game_id='equal', minutes=10, points=22),
        _row(LEBRON, game_id='above', minutes=10.1, points=33),
    )
    tatum_rows = (
        _row(
            TATUM,
            name='Jayson Tatum',
            team_id=BOS,
            team_tricode='BOS',
            game_id='tatum-equal',
            minutes=10,
        ),
    )
    logs = FakeLogs(rows=lebron_rows + tatum_rows)
    logs.season_rows = lebron_rows + tatum_rows + (
        _row(
            LEBRON,
            game_id='other-season-game',
            opponent_team_id=BOS,
            opponent_team_tricode='BOS',
            minutes=30,
            points=7,
        ),
    )
    diets = FakeDiets(
        zones={
            LEBRON: _zone_diet(0.42, 0.2),
            TATUM: _zone_diet(0.45, 0.2),
        }
    )

    payload = backtest(created['id'], logs=logs, diets=diets)

    assert created['conditions'] == {
        'defender': None,
        'from': None,
        'to': None,
        'player_minutes': 10,
    }
    assert payload['games_considered'] == {'played': 4, 'kept': 4}
    assert [player['canonical_id'] for player in payload['players']] == [LEBRON]
    player = payload['players'][0]
    assert [game['game_id'] for game in player['games']] == ['above']
    assert player['games'][0]['minutes'] == 10.1
    assert player['season_games'] == 4
    assert player['season_totals']['points'] == 73
    assert player['season_averages']['PTS'] == 25.0
    assert payload['summary'] == {
        'players': 1,
        'games': 1,
        'columns': {
            'PTS': {'mean_difference': 8.0, 'over_average_share': 1.0},
            'PTS/36': {'mean_difference': 73.823762, 'over_average_share': 1.0},
            '3PA': {'mean_difference': 2.0, 'over_average_share': 1.0},
            '3PA/36': {'mean_difference': 9.314851, 'over_average_share': 1.0},
        },
    }


@pytest.mark.parametrize('minutes', [None, float('nan'), float('inf'), float('-inf')])
def test_player_minutes_excludes_missing_and_nonfinite_appearances(
    targets, backtest, minutes
):
    created = _create(targets, conditions={'player_minutes': 0})
    logs = FakeLogs(rows=(_row(LEBRON, minutes=minutes),))
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})

    payload = backtest(created['id'], logs=logs, diets=diets)

    assert payload['players'] == []
    assert payload['summary']['players'] == 0
    assert payload['summary']['games'] == 0
    assert payload['games_considered'] == {'played': 1, 'kept': 1}


def test_player_minutes_conditions_round_trip_replace_and_clear(targets):
    created = _create(targets, conditions={'player_minutes': 10})
    canonical = {
        'defender': None,
        'from': None,
        'to': None,
        'player_minutes': 10,
    }
    assert created['conditions'] == canonical
    assert targets.get_target(OWNER, created['id'])['conditions'] == canonical
    draft = targets.validate_target_draft(
        opponent='OKC', qualifiers=[CORNER_THREE], conditions={'player_minutes': 10}
    )
    assert draft['conditions'] == canonical

    preserved = targets.update_target(
        OWNER, created['id'], changes={'note': 'Keep this threshold'}
    )
    assert preserved['conditions'] == canonical

    replaced = targets.update_target(
        OWNER,
        created['id'],
        changes={'conditions': {'from': '2026-01-01'}},
    )
    assert replaced['conditions'] == {
        'defender': None,
        'from': '2026-01-01',
        'to': None,
        'player_minutes': None,
    }
    restored = targets.update_target(
        OWNER, created['id'], changes={'conditions': {'player_minutes': 10}}
    )
    assert restored['conditions'] == canonical
    cleared = targets.update_target(
        OWNER, created['id'], changes={'conditions': {'player_minutes': None}}
    )
    assert cleared['conditions'] == {
        'defender': None,
        'from': None,
        'to': None,
        'player_minutes': None,
    }


@pytest.mark.parametrize(('comparator', 'minutes', 'expected'), [('under', 20, ['sat']), ('at_least', 20, ['played']), ('under', 0, []), ('at_least', 0, ['played', 'sat'])])
def test_backtest_conditions_include_absence_as_zero_and_bound_dates(targets, build_backtest, comparator, minutes, expected):
    conditions = {'defender': {'player_id': 99, 'comparator': comparator, 'minutes': minutes}, 'from': '2026-01-01', 'to': '2026-01-31'}
    logs = FakeLogs(rows=[
        _row(LEBRON, game_id='played', game_date=date(2026, 1, 31)),
        _row(LEBRON, game_id='sat', game_date=date(2026, 1, 1)),
        _row(LEBRON, game_id='outside', game_date=date(2025, 12, 31)),
    ])
    logs.list_player_rows = lambda season, player_id, **kwargs: (
        _row(99, game_id='played', game_date=date(2026, 1, 31), team_id=OKC, opponent_team_id=LAL, minutes=20),
    )
    service = build_backtest(logs=logs, diets=FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)}))
    target = {'opponent': 'OKC', 'qualifiers': [CORNER_THREE], 'conditions': conditions}
    payload = service.backtest_target(target)
    assert payload['games_considered'] == {'played': 3, 'kept': len(expected)}
    assert [game['game_id'] for player in payload['players'] for game in player['games']] == expected


@pytest.mark.parametrize('conditions', [
    {'defender': {'player_id': 99, 'comparator': 'under', 'minutes': 49}},
    {'defender': {'player_id': 99, 'comparator': 'under', 'minutes': 1.5}},
    {'defender': {'player_id': 99, 'comparator': 'over', 'minutes': 20}},
    {'defender': {'player_id': 100, 'comparator': 'under', 'minutes': 20}},
    {'player_minutes': -1},
    {'player_minutes': 49},
    {'player_minutes': 10.5},
    {'player_minutes': True},
    {'player_minutes': '10'},
    {'from': '2026-02-01', 'to': '2026-01-01'},
    {'from': '20260101'},
])
def test_invalid_conditions_are_refused_for_create_update_and_draft(targets, conditions):
    from app.errors import InvalidInputError
    targets.player_logs = SimpleNamespace(list_player_rows=lambda season, player_id: (
        _row(99, team_id=OKC), _row(99, team_id=BOS),
    ) if player_id == 99 else ())
    created = _create(targets)
    for action in (
        lambda: targets.create_target(OWNER, opponent='BOS', qualifiers=[CORNER_THREE], conditions=conditions),
        lambda: targets.update_target(OWNER, created['id'], changes={'conditions': conditions}),
        lambda: targets.validate_target_draft(opponent='BOS', qualifiers=[CORNER_THREE], conditions=conditions),
    ):
        with pytest.raises(InvalidInputError):
            action()


def test_identical_saved_and_draft_conditions_have_identical_evidence(targets, build_backtest):
    conditions = {'defender': {'player_id': 99, 'comparator': 'at_least', 'minutes': 20}, 'from': None, 'to': None}
    logs = FakeLogs(rows=[_row(LEBRON)])
    logs.list_player_rows = lambda *args, **kwargs: (_row(99, game_id='other', team_id=OKC, opponent_team_id=LAL),)
    targets.player_logs = logs
    created = targets.create_target(OWNER, opponent='OKC', qualifiers=[CORNER_THREE], conditions=conditions)
    draft = targets.validate_target_draft(opponent='OKC', qualifiers=[CORNER_THREE], conditions=conditions)
    service = build_backtest(logs=logs, diets=FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)}))
    saved, preview = service.backtest(OWNER, created['id']), service.backtest_target(draft)
    assert saved['players'] == preview['players']
    assert saved['summary'] == preview['summary']
    assert saved['games_considered'] == {'kept': 0, 'played': 1}
    assert targets.list_targets(OWNER)[0]['conditions'] == {
        **conditions,
        'player_minutes': None,
    }


def test_identical_saved_and_draft_player_minutes_have_identical_evidence(
    targets, build_backtest
):
    conditions = {'player_minutes': 10}
    logs = FakeLogs(
        rows=(
            _row(LEBRON, game_id='above', minutes=10.1),
            _row(LEBRON, game_id='equal', minutes=10),
        )
    )
    diets = FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2)})
    targets.player_logs = logs
    created = _create(targets, conditions=conditions)
    draft = targets.validate_target_draft(
        opponent='OKC', qualifiers=[CORNER_THREE], conditions=conditions
    )
    service = build_backtest(logs=logs, diets=diets)

    saved = service.backtest(OWNER, created['id'])
    preview = service.backtest_target(draft)

    expected_conditions = {
        'defender': None,
        'from': None,
        'to': None,
        'player_minutes': 10,
    }
    assert saved['target']['conditions'] == expected_conditions
    assert preview['target']['conditions'] == expected_conditions
    assert saved['players'] == preview['players']
    assert saved['summary'] == preview['summary']
    assert saved['games_considered'] == {'kept': 2, 'played': 2}


def test_season_minutes_roster_groups_players_and_orders_by_average(backtest_engine, backtest_settings):
    from app.services.target_season_minutes import TargetSeasonMinutesService
    logs = SimpleNamespace(list_team_rows=lambda *args, **kwargs: (
        _row(1, name='Starter', minutes=30), _row(1, name='Starter', game_id='two', minutes=20),
        _row(2, name='Reserve', minutes=10),
        _row(3, name='Playoffs only', season_type='Playoffs', minutes=40),
    ))
    payload = TargetSeasonMinutesService(player_logs=logs, settings=backtest_settings).get('okc')
    assert payload == {'season': SEASON, 'players': [
        {'player_id': 1, 'name': 'Starter', 'games_played': 2, 'average_minutes': 25.0},
        {'player_id': 2, 'name': 'Reserve', 'games_played': 1, 'average_minutes': 10.0},
    ]}


def test_season_minutes_route(client, authenticate, dependencies):
    authenticate()
    assert client.get('/api/teams/OKC/season-minutes').status_code == 401
    dependencies.target_season_minutes_service = Mock()
    dependencies.target_season_minutes_service.get.return_value = {'season': SEASON, 'players': []}
    response = client.get('/api/teams/OKC/season-minutes', headers=authenticate())
    assert response.status_code == 200
    assert response.json == {'season': SEASON, 'players': []}


@pytest.mark.parametrize('path', ['/api/user/targets', '/api/user/targets/preview'])
def test_conditions_cross_create_and_preview_http_seams(client, authenticate, dependencies, path):
    conditions = {'defender': None, 'from': '2026-01-01', 'to': None, 'player_minutes': 10}
    draft = {'opponent': 'OKC', 'qualifiers': [CORNER_THREE], 'conditions': conditions}
    dependencies.user_service.create_target = Mock(return_value=draft)
    dependencies.user_service.validate_target_draft = Mock(return_value=draft)
    dependencies.target_preview_service = Mock()
    dependencies.target_preview_service.preview.return_value = {'target': draft, 'players': [], 'summary': {}, 'games_considered': {'kept': 0, 'played': 1}}
    response = client.post(path, json=draft, headers=authenticate())
    assert response.status_code in (200, 201)
    assert response.json['target']['conditions'] == conditions
    method = dependencies.user_service.validate_target_draft if path.endswith('preview') else dependencies.user_service.create_target
    assert method.call_args.kwargs['conditions'] == conditions


@pytest.mark.parametrize(('method', 'path', 'service_method'), [('post', '/api/user/targets', 'create_target'), ('patch', '/api/user/targets/7', 'update_target'), ('post', '/api/user/targets/preview', 'validate_target_draft')])
def test_invalid_conditions_return_standard_http_errors(client, authenticate, dependencies, method, path, service_method):
    from app.errors import InvalidInputError
    setattr(dependencies.user_service, service_method, Mock(side_effect=InvalidInputError('Invalid Conditions.')))
    response = getattr(client, method)(path, json={'conditions': {'from': 'wrong'}}, headers=authenticate())
    assert response.status_code == 400
    assert response.json['error'] == {'code': 'invalid_input', 'message': 'Invalid Conditions.'}


def test_season_minutes_reads_only_the_requested_teams_stored_rows(backtest_engine, backtest_settings):
    from datetime import timedelta
    from app.services.player_game_log_repository import PlayerGameLogRepository
    from app.services.target_season_minutes import TargetSeasonMinutesService
    logs = PlayerGameLogRepository(backtest_engine, statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON, stats_surface_max_age=timedelta(hours=30), serve_stale=True)
    logs.publish(SEASON, [_row(99, name='Defender', team_id=OKC, team_tricode='OKC', opponent_team_id=LAL, opponent_team_tricode='LAL', minutes=21), _row(LEBRON)],
        retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc), source_provider='nba_stats', source_row_count=2)
    result = TargetSeasonMinutesService(player_logs=logs, settings=backtest_settings).get('OKC')
    assert result['players'] == [{'player_id': 99, 'name': 'Defender', 'games_played': 1, 'average_minutes': 21.0}]
    assert TargetSeasonMinutesService(player_logs=logs, settings=backtest_settings).get('BOS')['players'] == []


def test_box_lines_and_totals_include_the_whole_regular_season(targets, build_backtest):
    from dataclasses import replace
    opponent = replace(_row(LEBRON), free_throws_made=2, free_throws_attempted=3, steals=1, blocks=2, turnovers=3, offensive_rebounds=2, defensive_rebounds=6, personal_fouls=4)
    other = replace(opponent, game_id='other', opponent_team_id=BOS, points=20, minutes=26)
    playoff = replace(opponent, game_id='playoff', season_type='Playoffs', points=99)
    second = replace(opponent, player_id=TATUM, points=40)
    second_other = replace(other, player_id=TATUM, points=5)
    logs = FakeLogs(rows=[opponent, second])
    logs.season_rows = (opponent, other, playoff, second, second_other)
    logs.list_player_rows = Mock(side_effect=AssertionError("Season rows must be read in one batch."))
    service = build_backtest(logs=logs, diets=FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2), TATUM: _zone_diet(0.45, 0.2)}))
    snapshot = object()
    players = service.backtest_target({'opponent': 'OKC', 'qualifiers': [CORNER_THREE]}, publication_snapshot=snapshot)['players']
    player = players[0]
    assert players[1]['canonical_id'] == TATUM
    assert players[1]['season_totals']['points'] == 45
    assert players[1]['season_games'] == 2
    assert logs.snapshots == [snapshot, snapshot]
    assert player['games'][0]['line'] == {
        'points': 30, 'rebounds': 8, 'assists': 9, 'field_goals_made': 12, 'field_goals_attempted': 20,
        'threes_made': 4, 'threes_attempted': 8, 'free_throws_made': 2, 'free_throws_attempted': 3,
        'steals': 1, 'blocks': 2, 'turnovers': 3, 'offensive_rebounds': 2, 'defensive_rebounds': 6, 'fouls': 4, 'minutes': 34,
    }
    assert logs.summary_calls == [(SEASON, (LEBRON, TATUM))]
    logs.list_player_rows.assert_not_called()
    assert player['season_games'] == 2
    assert player['season_totals'] == {
        'points': 50, 'rebounds': 16, 'assists': 18, 'field_goals_made': 24, 'field_goals_attempted': 40,
        'threes_made': 8, 'threes_attempted': 16, 'free_throws_made': 4, 'free_throws_attempted': 6,
        'steals': 2, 'blocks': 4, 'turnovers': 6, 'offensive_rebounds': 4, 'defensive_rebounds': 12, 'fouls': 8, 'minutes': 60,
    }
    assert player['games'][0]['stats'] == {
        'PTS': 30,
        'PTS/36': 31.764706,
        '3PA': 8,
        '3PA/36': 8.470588,
    }
    assert player['season_averages'] == {
        'PTS': 25,
        'PTS/36': 30.0,
        '3PA': 6,
        '3PA/36': 9.6,
    }


def test_stat_preferences_save_independently_from_conditions_and_criteria(targets):
    preferences = {'columns': ['PTS/36', 'TS%'], 'graded_by': 'TS%'}
    created = targets.create_target(OWNER, opponent='OKC', qualifiers=[CORNER_THREE], stat_preferences=preferences, conditions={'from': '2026-01-01'})
    assert created['stat_preferences'] == preferences
    updated = targets.update_target(OWNER, created['id'], changes={'stat_preferences': {'columns': ['SB'], 'graded_by': 'SB'}})
    assert updated['conditions'] == created['conditions']
    assert updated['qualifiers'] == created['qualifiers']
    assert updated['stat_preferences'] == {'columns': ['SB'], 'graded_by': 'SB'}
    assert targets.update_target(OWNER, created['id'], changes={'note': 'New note'})['stat_preferences'] == updated['stat_preferences']
    assert targets.list_targets(OWNER)[0]['stat_preferences'] == updated['stat_preferences']
    assert targets.validate_target_draft(opponent='OKC', qualifiers=[CORNER_THREE], stat_preferences=preferences)['stat_preferences'] == preferences
    assert targets.update_target(OWNER, created['id'], changes={'stat_preferences': None})['stat_preferences'] is None


@pytest.mark.parametrize('preferences', [
    {}, {'columns': [], 'graded_by': 'PTS'}, {'columns': ['NOPE'], 'graded_by': 'NOPE'},
    {'columns': ['PTS'], 'graded_by': 'REB'}, {'columns': ['pts/36'], 'graded_by': 'pts/36'},
    {'columns': 'PTS', 'graded_by': 'PTS'}, {'columns': [False], 'graded_by': False},
])
def test_invalid_stat_preferences_are_refused_everywhere(targets, preferences):
    from app.errors import InvalidInputError
    created = _create(targets)
    for action in (
        lambda: targets.create_target(OWNER, opponent='BOS', qualifiers=[CORNER_THREE], stat_preferences=preferences),
        lambda: targets.update_target(OWNER, created['id'], changes={'stat_preferences': preferences}),
        lambda: targets.validate_target_draft(opponent='BOS', qualifiers=[CORNER_THREE], stat_preferences=preferences),
    ):
        with pytest.raises(InvalidInputError):
            action()


@pytest.mark.parametrize('key', 'PTS REB AST 3PM FG2A 3PA FGM FGA FTM FTA STL BLK TOV OREB DREB PF MIN PA PR RA PRA SB PTS/36 REB/36 AST/36 3PM/36 FG2A/36 3PA/36 FGA/36 FTA/36 STL/36 BLK/36 TOV/36 PRA/36 PR/36 PA/36 FG% 3P% TS% PTS/FGA'.split())
def test_every_spec_stat_key_is_accepted_by_draft_validation(targets, key):
    preferences = {'columns': [key], 'graded_by': key}
    assert targets.validate_target_draft(opponent='OKC', qualifiers=[CORNER_THREE], stat_preferences=preferences)['stat_preferences'] == preferences


@pytest.mark.parametrize('path', ['/api/user/targets', '/api/user/targets/preview', '/api/user/targets/7'])
def test_stat_preferences_cross_the_http_seam(client, authenticate, dependencies, path):
    preferences = {'columns': ['PTS/36', 'FG%'], 'graded_by': 'PTS/36'}
    body = {'opponent': 'OKC', 'qualifiers': [CORNER_THREE], 'stat_preferences': preferences}
    dependencies.user_service.create_target = Mock(return_value=body)
    dependencies.user_service.update_target = Mock(return_value=body)
    dependencies.user_service.validate_target_draft = Mock(return_value=body)
    dependencies.target_preview_service = Mock()
    dependencies.target_preview_service.preview.return_value = {'target': body}
    method = client.patch if path.endswith('/7') else client.post
    response = method(path, json=body, headers=authenticate())
    assert response.status_code in (200, 201)
    assert response.json['target']['stat_preferences'] == preferences
    if path.endswith('/7'):
        assert dependencies.user_service.update_target.call_args.kwargs['changes']['stat_preferences'] == preferences
    else:
        seam = dependencies.user_service.validate_target_draft if path.endswith('preview') else dependencies.user_service.create_target
        assert seam.call_args.kwargs['stat_preferences'] == preferences


def test_roster_reads_the_immutable_publication_instead_of_legacy_rows(backtest_engine, backtest_settings):
    from dataclasses import asdict
    from datetime import timedelta
    from app.services.collection_control import PublicationService
    from app.services.database_first_activation import DatabaseFirstPublicationReader
    from app.services.player_game_log_repository import PlayerGameLogRepository
    from app.services.target_season_minutes import TargetSeasonMinutesService

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    publications = PublicationService(backtest_engine, clock=lambda: now)
    publications.register_stream('player_game_logs', provider='ledger', owner='railway',
        required_observations=(), publication_strategy='replace', enabled=True, freshness_rule='cutoff_current')
    rows = [_row(99, name='Published defender', team_id=OKC, team_tricode='OKC', opponent_team_id=LAL, opponent_team_tricode='LAL', minutes=27),
            _row(LEBRON), _row(123, game_id='unrelated', team_id=BOS, opponent_team_id=LAL)]
    publications.compose('player_game_logs', season=SEASON, cutoff=now,
        payload={'rows': [{**asdict(row), 'game_date': row.game_date.isoformat()} for row in rows]})
    reader = DatabaseFirstPublicationReader(backtest_engine, clock=lambda: now)
    logs = PlayerGameLogRepository(backtest_engine, statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON, stats_surface_max_age=timedelta(hours=30), publication_reader=reader)
    payload = TargetSeasonMinutesService(player_logs=logs, settings=backtest_settings, publication_reader=reader).get('OKC')
    assert payload['players'] == [{'player_id': 99, 'name': 'Published defender', 'games_played': 1, 'average_minutes': 27.0}]


@pytest.mark.parametrize(('method', 'path', 'seam'), [('post', '/api/user/targets', 'create_target'), ('patch', '/api/user/targets/7', 'update_target'), ('post', '/api/user/targets/preview', 'validate_target_draft')])
def test_invalid_stat_preferences_retain_the_http_error_contract(client, authenticate, dependencies, method, path, seam):
    from app.errors import InvalidInputError
    setattr(dependencies.user_service, seam, Mock(side_effect=InvalidInputError('Unknown stat key.')))
    response = getattr(client, method)(path, json={'stat_preferences': {'columns': ['BAD'], 'graded_by': 'BAD'}}, headers=authenticate())
    assert response.status_code == 400
    assert response.json['error'] == {'code': 'invalid_input', 'message': 'Unknown stat key.'}


@pytest.mark.parametrize("use_publication", [False, True])
def test_box_totals_use_real_batch_publication_rows_for_each_players_regular_season(backtest_engine, targets, backtest_settings, use_publication):
    from dataclasses import asdict, replace
    from datetime import timedelta
    from app.services.collection_control import PublicationService
    from app.services.database_first_activation import DatabaseFirstPublicationReader
    from app.services.player_game_log_repository import PlayerGameLogRepository

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    publications = PublicationService(backtest_engine, clock=lambda: now)
    publications.register_stream('player_game_logs', provider='ledger', owner='railway',
        required_observations=(), publication_strategy='replace', enabled=True, freshness_rule='cutoff_current')
    rows = [
        _row(LEBRON, points=30, minutes=30),
        _row(LEBRON, game_id='0022500002', opponent_team_id=BOS, points=10, minutes=20),
        _row(TATUM, points=40, minutes=35),
        _row(TATUM, game_id='0022500003', opponent_team_id=BOS, points=5, minutes=15),
        _row(LEBRON, game_id='0042500001', season_type='Playoffs', points=99),
        _row(EMBIID, opponent_team_id=BOS, points=88),
        replace(_row(LEBRON, points=77), season='2024-25', game_id='0022400001'),
    ]
    publications.compose('player_game_logs', season=SEASON, cutoff=now,
        payload={'rows': [{**asdict(row), 'game_date': row.game_date.isoformat()} for row in rows if row.season == SEASON and row.season_type == 'Regular Season']})
    reader = DatabaseFirstPublicationReader(backtest_engine, clock=lambda: now) if use_publication else None
    logs = PlayerGameLogRepository(backtest_engine, statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON, stats_surface_max_age=timedelta(hours=30), publication_reader=reader, serve_stale=True)
    if not use_publication:
        logs.publish(SEASON, rows[:-1], retrieved_at=now, source_provider="nba_stats", source_row_count=len(rows)-1)
        logs.publish("2024-25", rows[-1:], retrieved_at=now, source_provider="nba_stats", source_row_count=1)
    service = TargetBacktestService(targets=targets, player_logs=logs,
        player_diets=FakeDiets(zones={LEBRON: _zone_diet(0.42, 0.2), TATUM: _zone_diet(0.45, 0.2)}),
        statistic_catalog=StatisticCatalog.load_default(), publication_reader=reader,
        settings=backtest_settings.model_copy(update={'matchup_scores': MatchupScoreSettings(min_games=1)}))
    players = service.backtest_target({'opponent': 'OKC', 'qualifiers': [CORNER_THREE]})['players']
    by_id = {player['canonical_id']: player for player in players}
    assert set(by_id) == {LEBRON, TATUM}
    assert by_id[LEBRON]['season_games'] == 2
    assert by_id[LEBRON]['season_totals']['points'] == 40
    assert by_id[LEBRON]['season_totals']['minutes'] == 50
    assert by_id[LEBRON]['season_averages']['PTS'] == 20
    assert by_id[TATUM]['season_games'] == 2
    assert by_id[TATUM]['season_totals']['points'] == 45
    assert by_id[TATUM]['season_averages']['PTS'] == 22.5


def test_a_saved_backtest_loads_its_target_on_the_request_scope_connection(
    backtest_engine, backtest_settings, targets
):
    """One saved backtest checks out one connection, count them.

    The Target load joins the one ``request_read_scope`` checkout the whole
    response composes on; it no longer opens the second session
    ``UserService.get_target`` used to check out before the composition
    began.
    """

    created = _create(targets)
    seams = _two_games()
    service = TargetBacktestService(
        targets=targets,
        player_logs=seams["logs"],
        player_diets=seams["diets"],
        statistic_catalog=StatisticCatalog.load_default(),
        settings=backtest_settings,
        engine=backtest_engine,
    )

    checkouts: list[int] = []
    real_connect = backtest_engine.connect

    def counted():
        checkouts.append(1)
        return real_connect()

    from unittest.mock import patch as mock_patch

    loads_through_scope: list = []
    real_loader = targets.get_target_in_session

    def recording_loader(session, firebase_uid, target_id):
        loads_through_scope.append(
            (session is not None, firebase_uid, target_id)
        )
        return real_loader(session, firebase_uid, target_id)

    with mock_patch.object(
        backtest_engine, "connect", side_effect=lambda *a, **kw: counted()
    ), mock_patch.object(
        type(targets), "get_target", autospec=True, side_effect=AssertionError(
            "the second per-call session still opened"
        )
    ), mock_patch.object(
        targets, "get_target_in_session", side_effect=recording_loader
    ):
        payload = service.backtest(OWNER, created["id"])

    assert len(checkouts) == 1
    assert loads_through_scope == [(True, OWNER, created["id"])]
    assert [player["canonical_id"] for player in payload["players"]] == [LEBRON]


def test_the_scope_loaded_target_still_hides_an_unowned_one(
    backtest_engine, backtest_settings, targets
):
    """Ownership is unchanged on the scope-loaded path: another account's
    Target is missing, never forbidden."""

    created = _create(targets)
    seams = _two_games()
    service = TargetBacktestService(
        targets=targets,
        player_logs=seams["logs"],
        player_diets=seams["diets"],
        statistic_catalog=StatisticCatalog.load_default(),
        settings=backtest_settings,
        engine=backtest_engine,
    )

    with pytest.raises(ResourceNotFoundError):
        service.backtest(STRANGER, created["id"])


# --- shared Redis result cache for saved Targets (#279) ----------------------


class GenerationSnapshotReader:
    """A reader answering generations pointer-only and capturing one frozen
    snapshot: the split the real reader gives a request."""

    def __init__(self, reads, generation):
        self.reads = reads
        self.frozen_generation = generation
        self.snapshot_calls = []
        self.generation_calls = []

    def generation(self, stream_keys, *, season, session=None):
        self.generation_calls.append((tuple(stream_keys), season, session))
        return self.frozen_generation

    def snapshot(
        self,
        stream_keys,
        *,
        season,
        projection_only_keys=None,
        decoded_only_keys=None,
        session=None,
    ):
        self.snapshot_calls.append((tuple(stream_keys), season))
        return SimpleNamespace(reads=self.reads, generation=self.frozen_generation)


class FakeRedis:
    """A Redis client recording GET and SETEX calls and storing bytes."""

    def __init__(self):
        self.store = {}
        self.gets = []
        self.sets = []

    def get(self, key):
        self.gets.append(key)
        return self.store.get(key)

    def setex(self, key, seconds, payload):
        self.sets.append((key, seconds))
        self.store[key] = payload


class DeadRedis(FakeRedis):
    """A Redis whose reads fail the way an outage does."""

    def get(self, key):
        raise redis.exceptions.ConnectionError("connection refused")


def _cached_service(
    build_backtest, *, reads=None, generation=None, redis_client=None
):
    reader = GenerationSnapshotReader(
        reads if reads is not None else _available_reads(),
        generation if generation is not None else _frozen_generation(),
    )
    ticks = iter(float(i) for i in range(10_000))
    client = redis_client if redis_client is not None else FakeRedis()
    service = build_backtest(
        **_two_games(),
        publication_reader=reader,
        redis_client=client,
        cache_clock=lambda: next(ticks),
    )
    return service, reader, client


def _available_reads():
    """All five streams read available, with no refusal labels."""

    return {
        key: PublicationRead(
            stream_key=key,
            publication_id=f"pub-1-{key}",
            season=SEASON,
            cutoff=None,
            version=1,
            status="active",
            freshness="fresh",
            age_seconds=0,
            payload={"rows": []},
        )
        for key in BACKTEST_PUBLICATION_STREAM_KEYS
    }


def _frozen_generation():
    return tuple(
        (key, f"pub-1-{key}", 1, 1)
        for key in BACKTEST_PUBLICATION_STREAM_KEYS
    )


def _key(
    generation,
    *,
    qualifiers=(CORNER_THREE,),
    settings=None,
):
    """Build one cache key directly, the way the flow does."""

    target = {"id": 1, "qualifiers": list(qualifiers), "conditions": None}
    return backtest_cache_key(
        target,
        generation,
        season=SEASON,
        settings=settings
        or RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
            matchup_scores=MatchupScoreSettings(),
        ),
    )


def test_a_miss_files_its_field_under_the_generation_key(
    targets, build_backtest
):
    created = _create(targets)
    service, reader, client = _cached_service(build_backtest)

    payload = service.backtest(OWNER, created["id"])

    # The generation pre-check ran once with the request's season, and the
    # one miss captured its snapshot and stored the evidence under it.
    assert [season for _, season, _ in reader.generation_calls] == [SEASON]
    assert len(reader.snapshot_calls) == 1
    assert [player["canonical_id"] for player in payload["players"]] == [
        LEBRON
    ]
    assert len(client.sets) == 1
    cache_key, ttl = client.sets[0]
    assert cache_key.startswith("targets:backtest:v1:")
    assert cache_key in client.gets
    assert ttl == 86400
    evidence = json.loads(
        zlib.decompress(client.store[cache_key]).decode("utf-8")
    )
    assert set(evidence) == {
        "players",
        "summary",
        "stat_columns",
        "games_considered",
        "season",
    }
    assert evidence["season"] == SEASON


def test_a_hit_serves_the_stored_result_without_recomputing(
    targets, build_backtest
):
    created = _create(targets)
    service, _, _ = _cached_service(build_backtest)
    service.backtest(OWNER, created["id"])

    logs = service.player_logs
    compute_calls_before = len(logs.opponent_calls)
    payload = service.backtest(OWNER, created["id"])

    assert len(logs.opponent_calls) == compute_calls_before
    assert [player["canonical_id"] for player in payload["players"]] == [
        LEBRON
    ]


def test_a_hit_returns_a_body_byte_identical_to_a_miss(
    targets, build_backtest
):
    created = _create(targets)
    service, _, _ = _cached_service(build_backtest)

    miss = service.backtest(OWNER, created["id"])
    hit = service.backtest(OWNER, created["id"])

    assert json.dumps(hit, sort_keys=True) == json.dumps(miss, sort_keys=True)


def test_each_of_the_five_streams_advancing_alone_changes_the_key():
    base = _frozen_generation()
    base_key = _key(base)
    for stream_key in BACKTEST_PUBLICATION_STREAM_KEYS:
        bumped = tuple(
            (
                entry[0],
                "pub-2-entry",
                entry[2],
                entry[3],
            )
            if entry[0] == stream_key
            else entry
            for entry in base
        )
        assert _key(bumped) != base_key


def test_a_qualifier_reorder_changes_the_key():
    assert _key(_frozen_generation(), qualifiers=(LOW_RIM, CORNER_THREE)) != (
        _key(_frozen_generation(), qualifiers=(CORNER_THREE, LOW_RIM))
    )


def test_raw_threshold_precision_sifies_two_qualifiers():
    lower = dict(CORNER_THREE, threshold=0.4000001)
    higher = dict(CORNER_THREE, threshold=0.4000004)
    generation = _frozen_generation()
    assert _key(generation, qualifiers=(lower,)) != _key(
        generation, qualifiers=(higher,)
    )


def test_a_schema_bump_changes_the_key(monkeypatch):
    original = backtest_cache_key(
        {"id": 1, "qualifiers": [CORNER_THREE], "conditions": None},
        _frozen_generation(),
        season=SEASON,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
            matchup_scores=MatchupScoreSettings(),
        ),
    )
    monkeypatch.setattr(
        "app.services.target_backtest.TARGET_BACKTEST_CACHE_SCHEMA", 2
    )
    bumped = backtest_cache_key(
        {"id": 1, "qualifiers": [CORNER_THREE], "conditions": None},
        _frozen_generation(),
        season=SEASON,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
            matchup_scores=MatchupScoreSettings(),
        ),
    )
    assert bumped != original


def test_a_note_only_edit_still_hits_with_the_new_note(
    targets, build_backtest
):
    created = _create(targets)
    service, _, _ = _cached_service(build_backtest)
    service.backtest(OWNER, created["id"])

    updated = targets.update_target(
        OWNER,
        created["id"],
        changes={"note": "season bet"},
    )
    reads_before = len(service.player_logs.opponent_calls)
    payload = service.backtest(OWNER, created["id"])

    # The stored row is reloaded every request, so note and updated_at are
    # the fresh ones; the evidence came from the cache (no recompute).
    assert payload["target"]["note"] == "season bet"
    assert payload["target"]["updated_at"] == updated["updated_at"]
    assert len(service.player_logs.opponent_calls) == reads_before


def test_a_read_with_unavailable_reason_bypasses_the_write(
    targets, build_backtest
):
    refused = _available_reads()
    refused["player_game_logs"] = PublicationRead(
        stream_key="player_game_logs",
        publication_id=None,
        season=None,
        cutoff=None,
        version=None,
        status="unavailable",
        freshness="unavailable",
        age_seconds=None,
        payload=None,
        unavailable_reason="publication_checksum_mismatch",
    )
    created = _create(targets)
    service, reader, client = _cached_service(
        build_backtest, reads=refused
    )

    payload = service.backtest(OWNER, created["id"])

    # A generation containing a refusal is still looked up, but a result
    # computed from refusal labels is written nowhere.
    assert len(client.gets) == 1
    assert client.sets == []
    assert [player["canonical_id"] for player in payload["players"]] == [
        LEBRON
    ]


def test_a_redis_outage_is_computed_around_with_a_open_breaker(
    targets, build_backtest
):
    created = _create(targets)
    service, reader, client = _cached_service(
        build_backtest, redis_client=DeadRedis()
    )

    payload = service.backtest(OWNER, created["id"])

    assert [player["canonical_id"] for player in payload["players"]] == [
        LEBRON
    ]
    assert client.sets == []
    # The circuit is now open, so the next read never contacts Redis again
    # during the cooldown.
    gets_after_first = len(client.gets)
    second = service.backtest(OWNER, created["id"])
    assert len(client.gets) == gets_after_first
    assert second["players"] == payload["players"]


def test_the_lab_preview_never_touches_the_result_cache(
    targets, build_backtest
):
    from flask import Flask, g
    from app.services.target_preview import TargetPreviewService

    reader = GenerationSnapshotReader(_available_reads(), _frozen_generation())
    client = FakeRedis()
    draft = targets.validate_target_draft(
        opponent="OKC", qualifiers=[dict(CORNER_THREE)]
    )
    diagnostics = _two_games()
    preview = TargetPreviewService(
        backtests=TargetBacktestService(
            targets=targets,
            player_logs=diagnostics["logs"],
            player_diets=diagnostics["diets"],
            statistic_catalog=StatisticCatalog.load_default(),
            settings=RuntimeSettings(
                environment="testing",
                nba=NBASeasonSettings(current_season=SEASON),
                matchup_scores=MatchupScoreSettings(),
            ),
            publication_reader=reader,
            redis_client=client,
            cache_clock=lambda: 0.0,
        ),
        resolutions=SimpleNamespace(today=lambda _target, *, matchups: None),
        matchups=object(),
        injuries=object(),
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
            matchup_scores=MatchupScoreSettings(),
        ),
        publication_reader=reader,
    )
    with Flask(__name__).test_request_context():
        payload = preview.preview(draft)
        assert "targets_cache" not in g

    assert [player["canonical_id"] for player in payload["players"]] == [
        LEBRON
    ]
    assert client.gets == []
    assert client.sets == []


def test_the_flag_off_leaves_redis_entirely_alone(targets, build_backtest):
    created = _create(targets)
    client = FakeRedis()
    service = build_backtest(
        **_two_games(),
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
            matchup_scores=MatchupScoreSettings(),
            cache=CacheSettings(target_backtest_enabled=False),
        ),
        redis_client=client,
        publication_reader=GenerationSnapshotReader(
            _available_reads(), _frozen_generation()
        ),
    )

    payload = service.backtest(OWNER, created["id"])

    assert client.gets == []
    assert client.sets == []
    assert [player["canonical_id"] for player in payload["players"]] == [
        LEBRON
    ]


def test_the_request_log_sees_hit_miss_and_bypass(targets, build_backtest):
    from flask import Flask, g

    created = _create(targets)
    refused = _available_reads()
    refused["grouped_shot_types"] = PublicationRead(
        stream_key="grouped_shot_types",
        publication_id=None,
        season=None,
        cutoff=None,
        version=None,
        status="unavailable",
        freshness="unavailable",
        age_seconds=None,
        payload=None,
        unavailable_reason="publication_payload_invalid",
    )
    client = FakeRedis()
    service, reader, _ = _cached_service(build_backtest, redis_client=client)
    app = Flask(__name__)

    # One healthy read: the first request is a miss and writes; the second
    # is served and stamped as a hit.
    with app.test_request_context():
        service.backtest(OWNER, created["id"])
        assert g.targets_cache == "miss"
    with app.test_request_context():
        service.backtest(OWNER, created["id"])
        assert g.targets_cache == "hit"
    # Then a stream refuses and the generation moves, so the same read is
    # a bypass: computed, never written, stamped as such.
    reader.reads = refused
    reader.frozen_generation = tuple(
        (stream_key, "pub-2-refused", fence, version)
        for stream_key, _, fence, version in reader.frozen_generation
    )
    with app.test_request_context():
        service.backtest(OWNER, created["id"])
        assert g.targets_cache == "bypass"
