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

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.config.settings import (
    NBASeasonSettings,
    MatchupScoreSettings,
    RuntimeSettings,
)
from app.errors import ResourceNotFoundError
from app.migrations import run_migrations
from app.models.user import User
from app.services.player_diet import (
    PlayerDietBaseline,
    PlayerDietResult,
    StoredPlayerDietFact,
)
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerSeasonLogSummary,
    PlayerSeasonRate,
)
from app.services.statistic_catalog import StatisticCatalog
from app.services.target_backtest import TargetBacktestService
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

SHOT_ZONES = (
    "Restricted Area",
    "In The Paint (Non-RA)",
    "Mid-Range",
    "Corner 3",
    "Above the Break 3",
)
#: Every market a Corner 3 Qualifier's Defense Sheet *outcome* row maps to.
#: The FGA row's attempt markets are deliberately not columns.
CORNER_THREE_COLUMNS = ["PTS", "3PM"]

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

    def get_player_summaries(self, season, player_ids, *, publication_snapshot=None):
        player_ids = tuple(player_ids)
        self.summary_calls.append((season, player_ids))
        self.snapshots.append(publication_snapshot)
        return {
            player_id: PlayerSeasonLogSummary(
                season=season,
                player_id=player_id,
                season_rate=PlayerSeasonRate(
                    season=season,
                    player_id=player_id,
                    game_count=self.game_counts.get(player_id, 20),
                    total_minutes=700.0,
                    per_game={
                        **MARKET_PER_GAME,
                        "PTS": self.scoring.get(player_id, 25.0),
                    },
                    per_minute={},
                ),
                last_ten_minutes=(34.0,),
            )
            for player_id in player_ids
        }


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
def backtest(targets, backtest_settings):
    """Build the backtest over the caller's real Targets and fake seams."""

    unset = object()

    def _backtest(
        target_id, *, logs=None, diets=unset, uid=OWNER, publication_reader=None
    ):
        service = TargetBacktestService(
            targets=targets,
            player_logs=logs if logs is not None else FakeLogs(),
            player_diets=FakeDiets() if diets is unset else diets,
            statistic_catalog=StatisticCatalog.load_default(),
            settings=backtest_settings,
            publication_reader=publication_reader,
        )
        return service.backtest(uid, target_id)

    return _backtest


def _create(targets, *, uid=OWNER, opponent="OKC", qualifiers=(CORNER_THREE,)):
    return targets.create_target(
        uid, opponent=opponent, qualifiers=list(qualifiers), note=None
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
    assert payload["players"] == [
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
            "season_averages": {"PTS": 25.0, "3PM": 2.0},
            "games": [
                {
                    "game_id": "0022500584",
                    "game_date": "2026-01-16",
                    "matchup": "LAL vs. OKC",
                    "minutes": 34.0,
                    "stats": {"PTS": 30.0, "3PM": 4.0},
                },
                {
                    "game_id": "0022500120",
                    "game_date": "2025-11-03",
                    "matchup": "LAL @ OKC",
                    "minutes": 34.0,
                    "stats": {"PTS": 22.0, "3PM": 2.0},
                },
            ],
        }
    ]


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


def test_stat_columns_union_every_qualifiers_slice_markets_in_order(
    targets, backtest
):
    created = _create(targets, qualifiers=(CORNER_THREE, TRANSITION))
    logs = FakeLogs(rows=(_row(LEBRON),))
    diets = FakeDiets(
        zones={LEBRON: _zone_diet(0.42, 0.2)},
        play_types={LEBRON: _play_type_diet(0.25)},
    )

    payload = backtest(created["id"], logs=logs, diets=diets)

    assert payload["stat_columns"] == ["PTS", "3PM", "PA", "PR", "PRA"]
    player = payload["players"][0]
    assert list(player["season_averages"]) == payload["stat_columns"]
    assert list(player["games"][0]["stats"]) == payload["stat_columns"]


@pytest.mark.parametrize(
    ("base", "slice_key", "expected"),
    [
        # A shot zone reports made shots and attempts; only the made row is an
        # outcome, and a two-point zone's makes are points alone.
        ("shot_zones", "Corner 3", ["PTS", "3PM"]),
        ("shot_zones", "Restricted Area", ["PTS"]),
        # Synergy reports points and possessions; possessions are not an
        # outcome, and the points row carries its combo markets.
        ("play_types", "Transition", ["PTS", "PA", "PR", "PRA"]),
        # Both of a shot type's made rows are outcomes; neither attempt row is.
        ("shot_types", "Catch and Shoot", ["PTS", "3PM"]),
        ("assist_locations", "Corner3Assists", ["AST", "PA", "RA", "PRA"]),
    ],
)
def test_stat_columns_are_outcomes_never_attempts(
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
    assert not {"FGA", "FG2A", "FG3A", "POSS"} & set(payload["stat_columns"])


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
