"""Stored matchup composition at the application-service seam."""

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import ProviderUnavailableError, ResourceNotFoundError
from app.services.matchup import MatchupService
from app.services.database_first_activation import PublicationRead
from app.services.matchup_injuries import MatchupInjuryResult
from app.services.player_diet import (
    PlayerDietResult,
    StoredPlayerDietFact,
    StoredPlayerDietObservation,
)
from app.services.player_game_log_repository import (
    PlayerGameLogReadFreshness,
    PlayerSeasonLogSummary,
    PlayerSeasonRate,
)
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.stats_freshness_repository import StatsFreshness
from app.services.team_matchup_query import (
    LeagueMatchupMetric,
    TeamMatchupMetric,
    TeamMatchupWindow,
)
from app.services.team_matchup_repository import (
    StoredTeamMatchupObservation,
    TeamMatchupSnapshotScope,
)


NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)
SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738
BASES = (
    "play_types",
    "shot_zones",
    "shot_types",
    "assist_locations",
    "traditional",
)


class RecordedEvents:
    def __init__(self, events=None, *, count=1, retrieved_at=RETRIEVED_AT):
        self.events = events if events is not None else [_event()]
        self.count = count
        self.retrieved_at = retrieved_at
        self.get_events_calls = 0

    def count_events(self, season):
        assert season == SEASON
        return self.count

    def get_events(self, season):
        assert season == SEASON
        self.get_events_calls += 1
        return self.events

    def get_freshness(self, season, *, now):
        assert season == SEASON
        assert now == NOW
        return {
            "last_success_at": (
                self.retrieved_at.isoformat() if self.retrieved_at else None
            ),
            "fresh": self.retrieved_at is not None,
        }


class RecordedPool:
    def __init__(self, pool):
        self.pool = pool
        self.calls = []

    def get_pool_for_game(self, *, season, game_id):
        self.calls.append((season, game_id))
        return self.pool


class RecordedLogs:
    def get_player_summaries(self, season, player_ids):
        assert season == SEASON
        if tuple(player_ids) == ():
            return {}
        assert tuple(player_ids) == (2544,)
        return {
            2544: PlayerSeasonLogSummary(
                season=SEASON,
                player_id=2544,
                season_rate=PlayerSeasonRate(
                    season=SEASON,
                    player_id=2544,
                    game_count=20,
                    total_minutes=700,
                    per_game={"PTS": 25.4},
                    per_minute={"PTS": 25.4 / 35},
                ),
                last_ten_minutes=(31.0, 32.0, 33.0),
            )
        }

    def get_read_freshness(self, season):
        assert season == SEASON
        return PlayerGameLogReadFreshness("fresh", RETRIEVED_AT)


class RecordedDiets:
    def get_for_players(self, season, player_ids):
        assert season == SEASON
        if tuple(player_ids) == ():
            return PlayerDietResult(season=SEASON, players={}, observations=())
        assert tuple(player_ids) == (2544,)
        return PlayerDietResult(
            season=SEASON,
            players={
                2544: (
                    StoredPlayerDietFact(
                        2544,
                        "play_types",
                        "Transition",
                        0.19,
                        95.0,
                        20,
                        "possessions",
                        "nba_synergy",
                        RETRIEVED_AT,
                    ),
                    StoredPlayerDietFact(
                        2544,
                        "shot_zones",
                        "Restricted Area",
                        0.27,
                        108.0,
                        20,
                        "field_goal_attempts",
                        "nba_stats",
                        RETRIEVED_AT,
                    ),
                )
            },
            observations=tuple(
                StoredPlayerDietObservation(base, "available", None, RETRIEVED_AT)
                for base in BASES
                if base != "traditional"
            ),
        )


class RecordedTeamWindows:
    def __init__(self, season_window, last_15_window):
        self.season_window = season_window
        self.last_15_window = last_15_window
        self.calls = []

    def get_latest_window(self, season, *, window_games=None, as_of=None):
        self.calls.append((season, window_games, as_of))
        return self.season_window if window_games is None else self.last_15_window


class RecordedInjuries:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def get_injuries(self, *, event, season, pool_players):
        self.calls.append((event["nba_game_id"], season, tuple(pool_players)))
        return self.result


def _event():
    return {
        "nba_game_id": GAME_ID,
        "season": SEASON,
        "scheduled_at": "2026-01-16T00:30:00+00:00",
        "status_text": "Scheduled",
        "status_code": 1,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
        "home_team_id": BOS,
        "home_team_name": "Boston Celtics",
        "home_team_tricode": "BOS",
        "away_team_id": LAL,
        "away_team_name": "Los Angeles Lakers",
        "away_team_tricode": "LAL",
        "home_team": {"id": BOS, "name": "Boston Celtics", "tricode": "BOS"},
        "away_team": {
            "id": LAL,
            "name": "Los Angeles Lakers",
            "tricode": "LAL",
        },
    }


def _window(
    *,
    last_15=False,
    shot_zone_metrics=None,
    shot_type_metrics=None,
    traditional_metrics=None,
):
    metrics = [
        ("play_types", "Transition", "PTS", 16.0),
        ("assist_locations", "AtRimAssists", "AtRimAssists", 8.0),
    ]
    metrics.extend(
        traditional_metrics
        if traditional_metrics is not None
        else (
            ("traditional", "OPP_TOV", "OPP_TOV", 13.0),
            ("traditional", "OPP_STL", "OPP_STL", 7.0),
            ("traditional", "OPP_BLK", "OPP_BLK", 5.0),
        )
    )
    metrics.extend(
        shot_zone_metrics
        if shot_zone_metrics is not None
        else tuple(
            ("shot_zones", slice_key, "FGA", 20.0)
            for slice_key in (
                "Restricted Area",
                "In The Paint (Non-RA)",
                "Mid-Range",
                "Corner 3",
                "Above the Break 3",
            )
        )
    )
    metrics.extend(
        shot_type_metrics
        if shot_type_metrics is not None
        else (
            ("shot_types", "catch_and_shoot", "FG3A", 7.0),
            ("shot_types", "pullups", "FG3A", 7.0),
            ("shot_types", "less_than_10_ft", "FG3A", 7.0),
        )
    )
    if last_15:
        metrics = tuple(metric for metric in metrics if metric[0] != "play_types")
    league = tuple(
        LeagueMatchupMetric(base, slice_key, stat, average, 2.0, 30)
        for base, slice_key, stat, average in metrics
    )
    teams = {
        team_id: tuple(
            TeamMatchupMetric(
                base,
                slice_key,
                stat,
                average + offset,
                offset / average * 100,
                offset / 2,
                15 + int(offset),
            )
            for base, slice_key, stat, average in metrics
        )
        for team_id, offset in ((LAL, 1.0), (BOS, -1.0))
    }
    observations = tuple(
        StoredTeamMatchupObservation(
            surface=base,
            status="unavailable" if last_15 and base == "play_types" else "available",
            unavailable_reason="provider_window_unsupported" if last_15 and base == "play_types" else None,
            retrieved_at=RETRIEVED_AT,
        )
        for base in BASES
    )
    scope = TeamMatchupSnapshotScope(SEASON, date(2026, 1, 15), 15 if last_15 else None)
    return TeamMatchupWindow(
        scope=scope,
        fact_scopes={
            base: scope for base in BASES if not (last_15 and base == "play_types")
        },
        fact_retrieved_at={
            base: RETRIEVED_AT
            for base in BASES
            if not (last_15 and base == "play_types")
        },
        league_metrics=league,
        team_metrics=teams,
        observations=observations,
    )


def _service(
    *,
    events=None,
    pool=None,
    season_window=None,
    last_15_window=None,
    stats_at=RETRIEVED_AT,
    injuries=None,
):
    if pool is None:
        pool = PlayerPool(
            players=(
                PoolPlayer(
                    2544,
                    "LeBron James",
                    LAL,
                    ("PTS", "FGA"),
                    {"prizepicks": ("PTS", "FGA"), "underdog": ("PTS",)},
                ),
            ),
            team_counts={LAL: 1},
            freshness={
                "status": "fresh",
                "retrieved_at": RETRIEVED_AT.isoformat(),
                "providers": {
                    "prizepicks": {
                        "status": "fresh",
                        "retrieved_at": RETRIEVED_AT.isoformat(),
                    }
                },
            },
        )
    return MatchupService(
        event_catalog=events if events is not None else RecordedEvents(),
        player_pool=RecordedPool(pool) if pool is not False else None,
        player_logs=RecordedLogs(),
        player_diets=RecordedDiets(),
        team_matchups=RecordedTeamWindows(
            season_window if season_window is not None else _window(),
            last_15_window if last_15_window is not None else _window(last_15=True),
        ),
        stats_freshness=SimpleNamespace(get=lambda: StatsFreshness(stats_at)),
        injuries=injuries,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
        ),
        clock=lambda: NOW,
    )


def test_usable_out_injury_removes_pool_player_and_updates_matchup_counts():
    injury_block = {
        "status": "fresh",
        "unavailable_reason": None,
        "retrieved_at": RETRIEVED_AT.isoformat(),
        "source": "rotowire",
        "source_url": "https://www.rotowire.com/basketball/injury-report.php",
        "teams": [],
    }
    injuries = RecordedInjuries(
        MatchupInjuryResult(injury_block, frozenset({2544}), {2544: "rotowire:6504"})
    )

    payload = _service(injuries=injuries).get_matchup(game_id=GAME_ID)

    assert payload["players"] == []
    assert payload["game"]["away_team"]["targetable_player_count"] == 0
    assert payload["injuries"] == injury_block
    assert payload["freshness"]["injuries"] == {
        "status": "fresh",
        "retrieved_at": RETRIEVED_AT.isoformat(),
    }
    assert injuries.calls[0][0:2] == (GAME_ID, SEASON)


def test_non_out_badge_does_not_change_scores_diets_or_pool_membership():
    baseline = _service().get_matchup(game_id=GAME_ID)
    injury_block = {
        "status": "fresh",
        "unavailable_reason": None,
        "retrieved_at": RETRIEVED_AT.isoformat(),
        "source": "rotowire",
        "source_url": "https://www.rotowire.com/basketball/injury-report.php",
        "teams": [],
    }
    injuries = RecordedInjuries(
        MatchupInjuryResult(injury_block, frozenset(), {2544: "rotowire:6504"})
    )

    payload = _service(injuries=injuries).get_matchup(game_id=GAME_ID)

    assert payload["game"]["away_team"]["targetable_player_count"] == 1
    assert payload["players"][0]["injury_badge_ref"] == "rotowire:6504"
    assert payload["players"][0]["scores"] == baseline["players"][0]["scores"]
    assert payload["players"][0]["diet_shares"] == baseline["players"][0]["diet_shares"]
    assert payload["players"][0]["season_scoring"] == baseline["players"][0]["season_scoring"]


def test_matchup_composes_only_stored_facts_with_nullable_unavailable_window():
    payload = _service().get_matchup(game_id=GAME_ID)

    assert payload["game"]["game_id"] == GAME_ID
    assert payload["game"]["away_team"]["targetable_player_count"] == 1
    assert payload["game"]["home_team"]["targetable_player_count"] == 0
    assert set(payload["league"]["defense_sheet"]) == set(BASES)
    assert set(payload["league"]["defensive_columns"]) == {
        "OPP_TOV",
        "OPP_STL",
        "OPP_BLK",
    }
    assert payload["league"]["surface_availability"]["play_types"] == {
        "season": {"status": "available", "unavailable_reason": None},
        "last_15": {
            "status": "unavailable",
            "unavailable_reason": "provider_window_unsupported",
        },
    }
    league_play = payload["league"]["defense_sheet"]["play_types"][0]
    assert league_play == {
        "key": "Transition:PTS",
        "season": {"average_allowed_per_48": 16.0, "sigma": 2.0},
        "last_15": None,
    }
    away_play = payload["teams"][0]["defense_sheet"]["play_types"][0]
    assert away_play["key"] == league_play["key"]
    assert away_play["last_15"] is None
    assert away_play["season"] == {
        "allowed_per_48": 17.0,
        "percent_vs_league_average": 6.25,
        "sigma_deviation": 0.5,
        "rank": 16,
    }
    for base in BASES:
        league_keys = {row["key"] for row in payload["league"]["defense_sheet"][base]}
        for team in payload["teams"]:
            assert {row["key"] for row in team["defense_sheet"][base]} <= league_keys


def test_matchup_player_rows_are_integer_season_only_raw_and_truthfully_degraded():
    player = _service().get_matchup(game_id=GAME_ID)["players"][0]

    assert player == {
        "canonical_id": 2544,
        "name": "LeBron James",
        "team_id": LAL,
        "tricode": "LAL",
        "posted_markets": ["PTS", "FGA"],
        "provenance": {
            "prizepicks": ["PTS", "FGA"],
            "underdog": ["PTS"],
        },
        "season_scoring": 25.4,
        "last_10_minutes": [31.0, 32.0, 33.0],
        "diet_shares": {
            "play_types": [
                {
                    "key": "Transition",
                    "season": {
                        "share": 0.19,
                        "volume": 95.0,
                        "games_played": 20,
                        "volume_unit": "possessions",
                    },
                }
            ],
            "shot_zones": [
                {
                    "key": "Restricted Area",
                    "season": {
                        "share": 0.27,
                        "volume": 108.0,
                        "games_played": 20,
                        "volume_unit": "field_goal_attempts",
                    },
                }
            ],
            "shot_types": [],
            "assist_locations": [],
        },
        "scores": {
            "PTS": {
                "season": {"components": {}, "blend": None},
                "last_15": {"components": {}, "blend": None},
            },
            "FGA": {
                "season": {"components": {}, "blend": None},
                "last_15": {"components": {}, "blend": None},
            },
        },
        "injury_badge_ref": None,
    }
    assert all(
        "last_15" not in row for rows in player["diet_shares"].values() for row in rows
    )


def test_shot_zone_rows_expose_only_governed_compatible_markets():
    shot_zone_metrics = tuple(
        ("shot_zones", slice_key, stat_key, 20.0)
        for slice_key in (
            "Restricted Area",
            "In The Paint (Non-RA)",
            "Mid-Range",
            "Corner 3",
            "Above the Break 3",
        )
        for stat_key in ("FGA", "FGM")
    )
    payload = _service(
        season_window=_window(shot_zone_metrics=shot_zone_metrics),
        last_15_window=_window(
            last_15=True,
            shot_zone_metrics=shot_zone_metrics,
        ),
    ).get_matchup(game_id=GAME_ID)

    rows = {
        row["key"]: row["markets"]
        for row in payload["teams"][0]["defense_sheet"]["shot_zones"]
    }
    assert rows == {
        "Above the Break 3:FGA": ["FGA", "FG3A"],
        "Above the Break 3:FGM": ["PTS", "3PM"],
        "Corner 3:FGA": ["FGA", "FG3A"],
        "Corner 3:FGM": ["PTS", "3PM"],
        "In The Paint (Non-RA):FGA": ["FGA", "FG2A"],
        "In The Paint (Non-RA):FGM": ["PTS"],
        "Mid-Range:FGA": ["FGA", "FG2A"],
        "Mid-Range:FGM": ["PTS"],
        "Restricted Area:FGA": ["FGA", "FG2A"],
        "Restricted Area:FGM": ["PTS"],
    }


def test_missing_governed_shot_zone_degrades_only_that_surface():
    incomplete_zones = tuple(
        ("shot_zones", slice_key, "FGA", 20.0)
        for slice_key in (
            "Restricted Area",
            "In The Paint (Non-RA)",
            "Mid-Range",
            "Above the Break 3",
        )
    )

    payload = _service(
        season_window=_window(shot_zone_metrics=incomplete_zones),
        last_15_window=_window(
            last_15=True,
            shot_zone_metrics=incomplete_zones,
        ),
    ).get_matchup(game_id=GAME_ID)

    assert payload["league"]["surface_availability"]["shot_zones"] == {
        "season": {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
        "last_15": {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
    }
    assert all(
        row[window_name] is None
        for row in payload["league"]["defense_sheet"]["shot_zones"]
        for window_name in ("season", "last_15")
    )
    assert payload["league"]["defense_sheet"]["assist_locations"][0][
        "season"
    ] is not None


def test_unknown_shot_type_degrades_without_leaking_a_divergent_key():
    shot_types = (
        ("shot_types", "catch_and_shoot", "FG3A", 7.0),
        ("shot_types", "pullups", "FG3A", 7.0),
        ("shot_types", "less_than_10_ft", "FG3A", 7.0),
        ("shot_types", "running_jumpers", "FG3A", 7.0),
    )

    payload = _service(
        season_window=_window(shot_type_metrics=shot_types),
        last_15_window=_window(last_15=True, shot_type_metrics=shot_types),
    ).get_matchup(game_id=GAME_ID)

    assert payload["league"]["surface_availability"]["shot_types"] == {
        "season": {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
        "last_15": {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
    }
    assert {
        row["key"].rsplit(":", 1)[0]
        for row in payload["league"]["defense_sheet"]["shot_types"]
    } == {"Catch and Shoot", "Pullups", "Less Than 10 ft"}
    assert all(
        row[window_name] is None
        for row in payload["league"]["defense_sheet"]["shot_types"]
        for window_name in ("season", "last_15")
    )


def test_missing_required_defensive_column_degrades_traditional_without_503():
    incomplete_traditional = (
        ("traditional", "OPP_TOV", "OPP_TOV", 13.0),
        ("traditional", "OPP_STL", "OPP_STL", 7.0),
    )

    payload = _service(
        season_window=_window(traditional_metrics=incomplete_traditional),
        last_15_window=_window(
            last_15=True,
            traditional_metrics=incomplete_traditional,
        ),
    ).get_matchup(game_id=GAME_ID)

    assert payload["league"]["surface_availability"]["traditional"] == {
        "season": {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
        "last_15": {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
    }
    assert all(
        row[window_name] is None
        for row in payload["league"]["defense_sheet"]["traditional"]
        for window_name in ("season", "last_15")
    )
    assert all(
        row[window_name] is None
        for team in payload["teams"]
        for row in team["defense_sheet"]["traditional"]
        for window_name in ("season", "last_15")
    )
    assert all(
        column[window_name] is None
        for column in payload["league"]["defensive_columns"].values()
        for window_name in ("season", "last_15")
    )
    assert all(
        column[window_name] is None
        for team in payload["teams"]
        for column in team["defensive_columns"].values()
        for window_name in ("season", "last_15")
    )
    assert payload["league"]["defense_sheet"]["shot_zones"][0]["season"] is not None


@pytest.mark.parametrize("extra_metric_window", ("season", "last_15"))
def test_non_rebound_traditional_identity_divergence_degrades_only_missing_window(
    extra_metric_window,
):
    complete_traditional = (
        ("traditional", "OPP_REB", "OPP_REB", 10.0),
        ("traditional", "OPP_TOV", "OPP_TOV", 13.0),
        ("traditional", "OPP_STL", "OPP_STL", 7.0),
        ("traditional", "OPP_BLK", "OPP_BLK", 5.0),
    )
    traditional_with_pf = (
        *complete_traditional,
        ("traditional", "OPP_PF", "OPP_PF", 18.0),
    )
    missing_window = "last_15" if extra_metric_window == "season" else "season"

    payload = _service(
        season_window=_window(
            traditional_metrics=(
                traditional_with_pf
                if extra_metric_window == "season"
                else complete_traditional
            )
        ),
        last_15_window=_window(
            last_15=True,
            traditional_metrics=(
                traditional_with_pf
                if extra_metric_window == "last_15"
                else complete_traditional
            ),
        ),
    ).get_matchup(game_id=GAME_ID)

    assert payload["league"]["surface_availability"]["traditional"] == {
        extra_metric_window: {
            "status": "available",
            "unavailable_reason": None,
        },
        missing_window: {
            "status": "unavailable",
            "unavailable_reason": "legacy_surface_incomplete",
        },
    }
    league_rows = payload["league"]["defense_sheet"]["traditional"]
    assert {row["key"] for row in league_rows} == {
        "OPP_BLK",
        "OPP_PF",
        "OPP_REB",
        "OPP_STL",
        "OPP_TOV",
    }
    assert all(row[extra_metric_window] is not None for row in league_rows)
    assert all(row[missing_window] is None for row in league_rows)
    assert all(
        row[extra_metric_window] is not None
        for team in payload["teams"]
        for row in team["defense_sheet"]["traditional"]
    )
    assert all(
        row[missing_window] is None
        for team in payload["teams"]
        for row in team["defense_sheet"]["traditional"]
    )
    assert all(
        row["season"] is not None and row["last_15"] is not None
        for row in payload["league"]["defense_sheet"]["shot_zones"]
    )


@pytest.mark.parametrize("missing_rebound_window", ("season", "last_15"))
def test_legacy_traditional_without_rebounds_keeps_defensive_scores_available(
    missing_rebound_window,
):
    markets = ("TOV", "STL", "BLK", "REB")
    pool = PlayerPool(
        players=(
            PoolPlayer(
                2544,
                "LeBron James",
                LAL,
                markets,
                {"prizepicks": markets},
            ),
        ),
        team_counts={LAL: 1},
        freshness={
            "status": "fresh",
            "retrieved_at": RETRIEVED_AT.isoformat(),
            "providers": {},
        },
    )

    complete_traditional = (
        ("traditional", "OPP_TOV", "OPP_TOV", 13.0),
        ("traditional", "OPP_STL", "OPP_STL", 7.0),
        ("traditional", "OPP_BLK", "OPP_BLK", 5.0),
        ("traditional", "OPP_REB", "OPP_REB", 10.0),
    )
    season_window = _window(
        traditional_metrics=(
            None if missing_rebound_window == "season" else complete_traditional
        )
    )
    last_15_window = _window(
        last_15=True,
        traditional_metrics=(
            None if missing_rebound_window == "last_15" else complete_traditional
        ),
    )

    payload = _service(
        pool=pool,
        season_window=season_window,
        last_15_window=last_15_window,
    ).get_matchup(game_id=GAME_ID)

    assert payload["league"]["surface_availability"]["traditional"] == {
        "season": {"status": "available", "unavailable_reason": None},
        "last_15": {"status": "available", "unavailable_reason": None},
    }
    for column in ("OPP_TOV", "OPP_STL", "OPP_BLK"):
        assert payload["league"]["defensive_columns"][column]["season"] is not None
        assert payload["league"]["defensive_columns"][column]["last_15"] is not None

    league_rows = {
        row["key"]: row
        for row in payload["league"]["defense_sheet"]["traditional"]
    }
    assert league_rows["OPP_REB"][missing_rebound_window] is None
    present_rebound_window = (
        "last_15" if missing_rebound_window == "season" else "season"
    )
    assert league_rows["OPP_REB"][present_rebound_window] is not None
    for key in ("OPP_TOV", "OPP_STL", "OPP_BLK"):
        assert league_rows[key]["season"] is not None
        assert league_rows[key]["last_15"] is not None
    for team in payload["teams"]:
        team_rows = {row["key"]: row for row in team["defense_sheet"]["traditional"]}
        assert team_rows["OPP_REB"][missing_rebound_window] is None
        assert team_rows["OPP_REB"][present_rebound_window] is not None
        for key in ("OPP_TOV", "OPP_STL", "OPP_BLK"):
            assert team_rows[key]["season"] is not None
            assert team_rows[key]["last_15"] is not None

    scores = payload["players"][0]["scores"]
    expected = {
        "TOV": -0.076923,
        "STL": -0.142857,
        "BLK": -0.2,
    }
    for market, value in expected.items():
        for window_name in ("season", "last_15"):
            assert scores[market][window_name] == {
                "components": {
                    "traditional": {"value": value, "thin": False}
                }
            }
    assert scores["REB"][missing_rebound_window] == {
        "components": {},
        "blend": None,
    }
    assert scores["REB"][present_rebound_window] == {
        "components": {
            "traditional": {"value": -0.1, "thin": False}
        },
        "blend": {"value": -0.1, "thin": False},
    }


def test_matchup_carries_strict_source_freshness_and_unavailable_injuries():
    payload = _service().get_matchup(game_id=GAME_ID)

    assert payload["injuries"] == {
        "status": "unavailable",
        "unavailable_reason": "disabled",
        "retrieved_at": None,
        "source": "rotowire",
        "source_url": "https://www.rotowire.com/basketball/injury-report.php",
        "teams": [],
    }
    assert payload["freshness"]["schedule"] == {
        "status": "fresh",
        "retrieved_at": RETRIEVED_AT.isoformat(),
    }
    assert payload["freshness"]["pool"]["status"] == "fresh"
    assert payload["freshness"]["stats"] == {
        "status": "fresh",
        "retrieved_at": RETRIEVED_AT.isoformat(),
    }
    assert payload["freshness"]["player_game_logs"] == {
        "status": "fresh",
        "retrieved_at": RETRIEVED_AT.isoformat(),
    }
    assert set(payload["freshness"]["player_diets"]["surfaces"]) == set(BASES) - {
        "traditional"
    }
    assert set(payload["freshness"]["team_matchups"]) == {"season", "last_15"}
    assert payload["freshness"]["injuries"] == {
        "status": "unavailable",
        "retrieved_at": None,
    }


def test_stats_freshness_is_stale_when_it_predates_the_latest_completed_game():
    completed = {
        **_event(),
        "scheduled_at": "2026-01-15T00:30:00+00:00",
        "status_text": "Final",
        "status_code": 3,
    }

    payload = _service(
        events=RecordedEvents(events=[completed]),
        stats_at=datetime(2026, 1, 14, 10, tzinfo=timezone.utc),
    ).get_matchup(game_id=GAME_ID)

    assert payload["freshness"]["stats"] == {
        "status": "stale",
        "retrieved_at": "2026-01-14T10:00:00+00:00",
    }


def test_matchup_reuses_the_resolved_event_catalog_read_for_stats_freshness():
    events = RecordedEvents()

    _service(events=events).get_matchup(game_id=GAME_ID)

    assert events.get_events_calls == 1


def test_past_matchup_queries_both_team_windows_at_the_slate_date():
    past_event = {**_event(), "scheduled_at": "2026-01-14T00:30:00+00:00"}
    service = _service(events=RecordedEvents(events=[past_event]))

    payload = service.get_matchup(game_id=GAME_ID)

    assert payload["game"]["game_id"] == GAME_ID
    assert service.team_matchups.calls == [
        (SEASON, None, date(2026, 1, 13)),
        (SEASON, 15, date(2026, 1, 13)),
    ]


def test_future_matchup_queries_both_team_windows_without_a_future_cutoff():
    service = _service()

    payload = service.get_matchup(game_id=GAME_ID)

    assert payload["game"]["game_id"] == GAME_ID
    assert service.team_matchups.calls == [
        (SEASON, None, None),
        (SEASON, 15, None),
    ]


def test_missing_pool_and_stats_are_degraded_without_provider_fallback():
    payload = _service(
        pool=False,
        season_window=False,
        last_15_window=False,
    ).get_matchup(game_id=GAME_ID)

    assert payload["players"] == []
    assert payload["freshness"]["pool"] == {
        "status": "unavailable",
        "retrieved_at": None,
        "providers": {},
    }
    for base, availability in payload["league"]["surface_availability"].items():
        assert availability["season"]["status"] == "missing"
        if base == "play_types":
            assert availability["last_15"] == {
                "status": "unavailable",
                "unavailable_reason": "provider_window_unsupported",
            }
        else:
            assert availability["last_15"]["status"] == "missing"
    assert all(not rows for rows in payload["league"]["defense_sheet"].values())


def test_stored_unavailable_team_surface_retains_observation_timestamp():
    window = _window()
    unavailable = replace(
        window,
        observations=tuple(
            replace(
                observation,
                status="unavailable" if observation.surface == "traditional" else observation.status,
                unavailable_reason=(
                    "provider_invalid_numeric"
                    if observation.surface == "traditional"
                    else observation.unavailable_reason
                ),
            )
            for observation in window.observations
        ),
    )

    payload = _service(season_window=unavailable).get_matchup(game_id=GAME_ID)

    assert payload["freshness"]["team_matchups"]["season"]["surfaces"]["traditional"] == {
        "status": "unavailable",
        "unavailable_reason": "provider_invalid_numeric",
        "retrieved_at": RETRIEVED_AT.isoformat(),
    }


def test_invalid_database_publication_has_no_retrieved_timestamp(tmp_path):
    from sqlalchemy import create_engine

    from app.migrations import run_migrations
    from app.services.team_matchup_query import TeamMatchupQueryService
    from app.services.team_matchup_repository import TeamMatchupRepository

    engine = create_engine(f"sqlite:///{tmp_path / 'invalid-publication.sqlite3'}")
    run_migrations(engine)
    invalid = PublicationRead(
        stream_key="traditional_opponent_season",
        publication_id="invalid-publication",
        season=SEASON,
        cutoff=RETRIEVED_AT.isoformat(),
        version=1,
        status="active",
        freshness="fresh",
        age_seconds=0,
        payload={"rows": [{"team_id": LAL}]},
        retrieved_at=RETRIEVED_AT,
        checksum="a" * 64,
    )

    class Reader:
        def read_many(self, stream_keys, *, season):
            assert season == SEASON
            return {
                stream_key: invalid
                for stream_key in stream_keys
                if stream_key == "traditional_opponent_season"
            }

    window = TeamMatchupQueryService(
        TeamMatchupRepository(engine),
        clock=lambda: NOW,
        publication_reader=Reader(),
    ).get_window(TeamMatchupSnapshotScope(SEASON, NOW.date()))

    observation = next(
        item for item in window.observations if item.surface == "traditional"
    )
    assert observation.status == "unavailable"
    assert observation.retrieved_at is None


def test_non_governed_event_team_degrades_team_surfaces_without_losing_game():
    exhibition_team_id = 1610619999
    exhibition = {
        **_event(),
        "home_team_id": exhibition_team_id,
        "home_team_name": "International Select",
        "home_team_tricode": "INT",
        "home_team": {
            "id": exhibition_team_id,
            "name": "International Select",
            "tricode": "INT",
        },
    }

    payload = _service(events=RecordedEvents(events=[exhibition])).get_matchup(
        game_id=GAME_ID
    )

    assert payload["game"]["home_team"] == {
        "team_id": exhibition_team_id,
        "name": "International Select",
        "tricode": "INT",
        "targetable_player_count": 0,
    }
    for base, windows in payload["league"]["surface_availability"].items():
        for window_name, state in windows.items():
            if base == "play_types" and window_name == "last_15":
                assert state == {
                    "status": "unavailable",
                    "unavailable_reason": "provider_window_unsupported",
                }
            else:
                assert state == {
                    "status": "missing",
                    "unavailable_reason": "team_not_in_governed_roster",
                }
    assert payload["players"][0]["canonical_id"] == 2544
    assert all(
        row[window_name] is None
        for team in payload["teams"]
        for rows in team["defense_sheet"].values()
        for row in rows
        for window_name in ("season", "last_15")
    )
    assert all(
        row[window_name] is None
        for rows in payload["league"]["defense_sheet"].values()
        for row in rows
        for window_name in ("season", "last_15")
    )
    assert all(
        column[window_name] is None
        for team in payload["teams"]
        for column in team["defensive_columns"].values()
        for window_name in ("season", "last_15")
    )


def test_unknown_game_is_404_while_an_empty_schedule_surface_is_503():
    with pytest.raises(ResourceNotFoundError):
        _service(events=RecordedEvents(events=[])).get_matchup(game_id="unknown")

    with pytest.raises(ProviderUnavailableError):
        _service(events=RecordedEvents(events=[], count=0)).get_matchup(game_id=GAME_ID)
