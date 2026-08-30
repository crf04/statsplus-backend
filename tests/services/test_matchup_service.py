"""Stored matchup composition at the application-service seam."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import ProviderUnavailableError, ResourceNotFoundError
from app.migrations import run_migrations
from app.services.collection_control import PublicationService
from app.services.matchup import MatchupService
from app.services.database_first_activation import (
    DatabaseFirstPublicationReader,
    PublicationRead,
)
from app.services.matchup_injuries import MatchupInjuryResult
from app.services.player_diet import (
    PlayerDietResult,
    StoredPlayerDietFact,
    StoredPlayerDietObservation,
)
from app.services.player_game_log_repository import (
    PlayerGameLogReadFreshness,
    PlayerGameLogRecord,
    PlayerGameLogRepository,
    PlayerGameLogSyncStatus,
    PlayerSeasonLogSummary,
    PlayerSeasonRate,
)
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import StatsFreshness
from app.services.team_matchup_query import (
    LeagueMatchupMetric,
    TeamMatchupMetric,
    TeamMatchupWindow,
)
from app.services.team_matchup_publications import PublicationLineage
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
    def __init__(
        self,
        season_window,
        last_15_window,
        *,
        pre_focal_season_window=None,
        pre_focal_cutoff=None,
    ):
        self.season_window = season_window
        self.last_15_window = last_15_window
        # A snapshot stored strictly before the focal game's date cannot
        # contain that game; scopes carry a date, so nothing dated on the game
        # date can be proven pre-tip.
        self.pre_focal_season_window = pre_focal_season_window
        self.pre_focal_cutoff = pre_focal_cutoff
        self.calls = []

    def get_latest_window(
        self, season, *, window_games=None, as_of=None, strict_as_of=False
    ):
        self.calls.append((season, window_games, as_of, strict_as_of))
        if window_games is not None:
            return self.last_15_window
        if (
            self.pre_focal_cutoff is not None
            and as_of is not None
            and as_of <= self.pre_focal_cutoff
        ):
            # Issue 41 serves the completed-season snapshot for any earlier
            # date in that season. Only a strict read refuses it.
            return (
                self.pre_focal_season_window
                if strict_as_of
                else self.season_window
            )
        return self.season_window


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


def test_season_complete_snapshot_names_its_reason_in_surface_availability():
    season = _window()
    season = replace(season, observations=tuple(
        replace(item, publication=PublicationLineage(
            publication_id="publication-exact_shot_zones_opponent_season",
            cutoff="2026-08-01T00:00:00+00:00",
            freshness="stale",
            version=3,
            reason="season_complete_snapshot",
        ))
        if item.surface == "shot_zones"
        else item
        for item in season.observations
    ))

    payload = _service(season_window=season).get_matchup(game_id=GAME_ID)

    availability = payload["league"]["surface_availability"]
    assert availability["shot_zones"]["season"] == {
        "status": "available",
        "unavailable_reason": None,
        "reason": "season_complete_snapshot",
    }
    assert availability["shot_types"]["season"] == {
        "status": "available",
        "unavailable_reason": None,
    }


def test_matchup_player_rows_are_integer_season_only_raw_and_thin_where_evidence_is_partial():
    player = _service().get_matchup(game_id=GAME_ID)["players"][0]

    assert player == {
        "canonical_id": 2544,
        "name": "LeBron James",
        "team_id": LAL,
        "tricode": "LAL",
        "player_source": "player_pool",
        "stat_categories": ["PTS", "FGA"],
        "focal_game_line": None,
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
                # One observed play type is a complete Synergy Diet, so the
                # crossing scores; at 0.19 coverage the cell is thin.
                "season": {
                    "components": {
                        "play_types": {"value": -0.011875, "thin": True}
                    },
                    "blend": {"value": -0.011875, "thin": True},
                    "missing_inputs": [
                        "player_diet:shot_zones",
                        "player_diet:shot_types",
                    ],
                },
                "last_15": {
                    "components": {},
                    "blend": None,
                    "missing_inputs": [
                        "team_defense:play_types",
                        "player_diet:shot_zones",
                        "player_diet:shot_types",
                    ],
                },
            },
            "FGA": {
                "season": {
                    "components": {},
                    "blend": None,
                    "missing_inputs": [
                        "player_diet:shot_zones",
                        "player_diet:shot_types",
                    ],
                },
                "last_15": {
                    "components": {},
                    "blend": None,
                    "missing_inputs": [
                        "player_diet:shot_zones",
                        "player_diet:shot_types",
                    ],
                },
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
                },
                "missing_inputs": [],
            }
    assert scores["REB"][missing_rebound_window] == {
        "components": {},
        "blend": None,
        # The legacy scope has no stored OPP_REB identity to consume.
        "missing_inputs": ["team_defense:traditional"],
    }
    assert scores["REB"][present_rebound_window] == {
        "components": {
            "traditional": {"value": -0.1, "thin": False}
        },
        "blend": {"value": -0.1, "thin": False},
        "missing_inputs": [],
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
    # A current-slate read is never strict: it asks the same question it
    # always asked.
    assert service.team_matchups.calls == [
        (SEASON, None, date(2026, 1, 13), False),
        (SEASON, 15, date(2026, 1, 13), False),
    ]


def test_future_matchup_queries_both_team_windows_without_a_future_cutoff():
    service = _service()

    payload = service.get_matchup(game_id=GAME_ID)

    assert payload["game"]["game_id"] == GAME_ID
    assert service.team_matchups.calls == [
        (SEASON, None, None, False),
        (SEASON, 15, None, False),
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


def _log_row(*, game_id: str, game_date: str, points: int, minutes: float):
    return {
        "season": SEASON,
        "season_type": "Regular Season",
        "player_id": 2544,
        "game_id": game_id,
        "player_name": "LeBron James",
        "game_date": game_date,
        "team_id": LAL,
        "team_tricode": "LAL",
        "opponent_team_id": BOS,
        "opponent_team_tricode": "BOS",
        "is_home": False,
        "minutes": minutes,
        "points": points,
        "rebounds": 8,
        "assists": 7,
        "field_goals_made": 9,
        "field_goals_attempted": 18,
        "three_pointers_made": 3,
        "three_pointers_attempted": 7,
        "free_throws_made": 4,
        "free_throws_attempted": 5,
        "offensive_rebounds": 2,
        "defensive_rebounds": 6,
        "turnovers": 4,
        "steals": 2,
        "blocks": 1,
        "personal_fouls": 2,
    }


def _published_matchup_service(tmp_path, rows):
    """Build a matchup service over one real activated player-log publication."""

    engine = create_engine(f"sqlite:///{tmp_path / 'matchup-logs.sqlite3'}")
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
    publication = publications.compose(
        "player_game_logs",
        season=SEASON,
        cutoff=RETRIEVED_AT,
        payload={"rows": list(rows)},
    )
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: NOW,
        stats_surface_max_age=timedelta(hours=30),
        publication_reader=reader,
    )
    service = MatchupService(
        event_catalog=RecordedEvents(),
        player_pool=RecordedPool(
            PlayerPool(
                players=(
                    PoolPlayer(
                        2544,
                        "LeBron James",
                        LAL,
                        ("PTS", "FGA"),
                        {"prizepicks": ("PTS", "FGA")},
                    ),
                ),
                team_counts={LAL: 1},
                freshness={
                    "status": "fresh",
                    "retrieved_at": RETRIEVED_AT.isoformat(),
                    "providers": {},
                },
            )
        ),
        player_logs=repository,
        player_diets=RecordedDiets(),
        team_matchups=RecordedTeamWindows(_window(), _window(last_15=True)),
        stats_freshness=SimpleNamespace(get=lambda: StatsFreshness(RETRIEVED_AT)),
        injuries=None,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
        ),
        clock=lambda: NOW,
        publication_reader=reader,
    )
    return engine, service, publication


def test_matchup_reads_player_logs_through_the_indexed_projection(tmp_path):
    """The matchup generation never ships the season-wide game-log payload."""

    engine, service, _ = _published_matchup_service(
        tmp_path,
        (
            _log_row(
                game_id="0022500001",
                game_date="2026-01-02",
                points=25,
                minutes=35.0,
            ),
            _log_row(
                game_id="0022500002",
                game_date="2026-01-05",
                points=31,
                minutes=37.0,
            ),
        ),
    )
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
        ),
    )

    payload = service.get_matchup(game_id=GAME_ID)

    assert payload["players"][0]["canonical_id"] == 2544
    assert payload["players"][0]["season_scoring"] == 28.0
    assert payload["players"][0]["last_10_minutes"] == [35.0, 37.0]
    assert payload["provenance"]["player_game_logs"]["status"] == "active"
    # Freshness reads only the payload-less metadata this generation carries.
    assert payload["freshness"]["player_game_logs"]["retrieved_at"] == (
        RETRIEVED_AT.isoformat()
    )
    assert any(
        "publication_player_game_logs" in statement for statement in statements
    )
    # The one generation query still hydrates its other streams, so the payload
    # column may only appear behind the guard that excludes this stream.
    for statement in statements:
        if "publication_versions.payload" not in statement:
            continue
        assert re.search(
            r"CASE WHEN \(publication_versions\.stream_key IN [^)]*\)\)? "
            r"THEN publication_versions\.payload",
            statement,
        ), statement


def test_matchup_serves_the_projection_rather_than_the_publication_payload(
    tmp_path,
):
    """Only the indexed rows can explain the document this request returns."""

    engine, service, publication = _published_matchup_service(
        tmp_path,
        (
            _log_row(
                game_id="0022500001",
                game_date="2026-01-02",
                points=25,
                minutes=35.0,
            ),
        ),
    )
    # Re-render the payload with a different scoring fact and keep it
    # self-consistent, so a payload-reading path would return 99 points.
    divergent = json.dumps(
        {
            "rows": [
                _log_row(
                    game_id="0022500001",
                    game_date="2026-01-02",
                    points=99,
                    minutes=35.0,
                )
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE publication_versions SET payload = :payload, "
                "checksum = :checksum WHERE publication_id = :publication_id"
            ),
            {
                "payload": divergent,
                "checksum": hashlib.sha256(divergent.encode()).hexdigest(),
                "publication_id": publication.publication_id,
            },
        )

    payload = service.get_matchup(game_id=GAME_ID)

    assert payload["players"][0]["season_scoring"] == 25.0


def test_matchup_without_the_projection_degrades_without_a_legacy_fallback(
    tmp_path,
):
    engine, service, publication = _published_matchup_service(
        tmp_path,
        (
            _log_row(
                game_id="0022500001",
                game_date="2026-01-02",
                points=25,
                minutes=35.0,
            ),
        ),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM publication_player_game_logs "
                "WHERE publication_id = :publication_id"
            ),
            {"publication_id": publication.publication_id},
        )

    payload = service.get_matchup(game_id=GAME_ID)

    assert payload["provenance"]["player_game_logs"] == {
        **payload["provenance"]["player_game_logs"],
        "status": "unavailable",
        "unavailable_reason": "publication_projection_missing",
    }
    assert payload["players"][0]["season_scoring"] is None
    assert payload["players"][0]["last_10_minutes"] == []


HISTORICAL_GAME_DATE = date(2026, 1, 10)
AWAY_PARTICIPANT = 1629661
HOME_PARTICIPANT = 203507


def _final_event(**overrides):
    """A completed Regular Season event whose tip is already in the past."""

    return {
        **_event(),
        "scheduled_at": "2026-01-11T00:30:00+00:00",
        "status_text": "Final",
        "status_code": 3,
        **overrides,
    }


def _pool_with_freshness(freshness):
    return PlayerPool(
        players=(
            PoolPlayer(
                2544,
                "LeBron James",
                LAL,
                ("PTS", "FGA"),
                {"prizepicks": ("PTS", "FGA")},
            ),
        ),
        team_counts={LAL: 1},
        freshness=freshness,
    )


def _empty_projection_pool():
    """The production pattern: a final game whose closing sets are empty."""

    return PlayerPool(
        players=(),
        team_counts={},
        freshness=PlayerPool.missing_projection_freshness(),
    )


def _closing_projection_freshness():
    freshness = PlayerPool.unavailable_freshness()
    freshness.update(
        {
            "state": "closing",
            "observed_at": RETRIEVED_AT.isoformat(),
            "retrieved_at": RETRIEVED_AT.isoformat(),
        }
    )
    return freshness


def _game_log_record(
    *,
    player_id,
    player_name,
    team_id,
    team_tricode,
    opponent_team_id,
    opponent_team_tricode,
    game_id=GAME_ID,
    game_date=HISTORICAL_GAME_DATE,
    minutes=34.0,
    points=24,
):
    return PlayerGameLogRecord(
        season=SEASON,
        season_type="Regular Season",
        player_id=player_id,
        game_id=game_id,
        player_name=player_name,
        game_date=game_date,
        team_id=team_id,
        team_tricode=team_tricode,
        opponent_team_id=opponent_team_id,
        opponent_team_tricode=opponent_team_tricode,
        is_home=False,
        minutes=minutes,
        points=points,
        rebounds=5,
        assists=7,
        field_goals_made=9,
        field_goals_attempted=18,
        three_pointers_made=3,
        three_pointers_attempted=7,
        free_throws_made=3,
        free_throws_attempted=4,
        offensive_rebounds=1,
        defensive_rebounds=4,
        turnovers=2,
        steals=1,
        blocks=1,
        personal_fouls=2,
    )


def _historical_rows():
    return (
        _game_log_record(
            player_id=AWAY_PARTICIPANT,
            player_name="Away Participant",
            # The identity recorded for this game. A later trade must not
            # rewrite which side the participant played for.
            team_id=LAL,
            team_tricode="LAL",
            opponent_team_id=BOS,
            opponent_team_tricode="BOS",
        ),
        _game_log_record(
            player_id=HOME_PARTICIPANT,
            player_name="Home Participant",
            team_id=BOS,
            team_tricode="BOS",
            opponent_team_id=LAL,
            opponent_team_tricode="LAL",
            points=31,
        ),
    )


class RecordedGameLogs:
    """A player-log seam that also serves one game's canonical rows."""

    def __init__(
        self,
        rows=None,
        *,
        sync_status="complete",
        season_rate=True,
        focal_points=0.0,
    ):
        self.rows = _historical_rows() if rows is None else rows
        self.sync_status = sync_status
        self.season_rate = season_rate
        # What the focal game itself contributes to the completed-season rate.
        # A read that excludes the focal game must not see it.
        self.focal_points = focal_points
        self.summary_calls = []
        self.game_row_calls = []

    def list_game_rows(self, season, game_id):
        assert season == SEASON
        self.game_row_calls.append(game_id)
        return tuple(row for row in self.rows if row.game_id == game_id)

    def get_sync_status(self, season, game_id):
        assert season == SEASON
        if self.sync_status is None:
            return None
        return PlayerGameLogSyncStatus(
            season=SEASON,
            game_id=game_id,
            season_type="Regular Season",
            status=self.sync_status,
            checksum="abc",
            row_count=len(self.rows),
            source_provider="pbp_stats",
            retrieved_at=RETRIEVED_AT,
        )

    def get_player_summaries(self, season, player_ids, *, exclude_game_id=None):
        assert season == SEASON
        self.summary_calls.append((tuple(player_ids), exclude_game_id))
        focal = 0.0 if exclude_game_id is not None else self.focal_points
        return {
            player_id: PlayerSeasonLogSummary(
                season=SEASON,
                player_id=player_id,
                season_rate=(
                    PlayerSeasonRate(
                        season=SEASON,
                        player_id=player_id,
                        game_count=20,
                        total_minutes=700,
                        per_game={
                            "PTS": 21.0 + focal,
                            "REB": 5.0,
                            "AST": 4.0,
                        },
                        per_minute={"PTS": 0.03, "REB": 0.007, "AST": 0.006},
                    )
                    if self.season_rate
                    else None
                ),
                last_ten_minutes=(31.0 + focal, 32.0),
            )
            for player_id in player_ids
        }

    def get_read_freshness(self, season):
        assert season == SEASON
        return PlayerGameLogReadFreshness("fresh", RETRIEVED_AT)


class RecordedEmptyDiets:
    def get_for_players(self, season, player_ids):
        assert season == SEASON
        return PlayerDietResult(season=SEASON, players={}, observations=())


class RecordedPartialDiets:
    """Stored Diet for two of PTS's three Bases, so a Blend would be partial."""

    def get_for_players(self, season, player_ids):
        assert season == SEASON
        return PlayerDietResult(
            season=SEASON,
            players={
                player_id: (
                    StoredPlayerDietFact(
                        player_id,
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
                        player_id,
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
                for player_id in player_ids
            },
            observations=tuple(
                StoredPlayerDietObservation(base, "available", None, RETRIEVED_AT)
                for base in BASES
                if base != "traditional"
            ),
        )


def _historical_service(
    *,
    player_logs=None,
    pool=None,
    season_window=None,
    last_15_window=None,
    injuries=None,
    events=None,
    diets=None,
    stats_at=None,
    team_windows=None,
):
    return MatchupService(
        event_catalog=(
            events if events is not None else RecordedEvents(events=[_final_event()])
        ),
        player_pool=RecordedPool(
            _empty_projection_pool() if pool is None else pool
        ),
        player_logs=player_logs if player_logs is not None else RecordedGameLogs(),
        player_diets=diets if diets is not None else RecordedEmptyDiets(),
        team_matchups=team_windows or RecordedTeamWindows(
            season_window if season_window is not None else _window(),
            last_15_window,
        ),
        stats_freshness=SimpleNamespace(get=lambda: StatsFreshness(stats_at)),
        statistic_catalog=StatisticCatalog.load_default(),
        injuries=injuries,
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
        ),
        clock=lambda: NOW,
    )


def test_final_regular_season_game_without_archived_projections_is_historical():
    payload = _historical_service().get_matchup(game_id=GAME_ID)

    assert payload["experience"]["mode"] == "historical"
    assert payload["experience"]["player_source"] == "game_logs"
    assert payload["experience"]["sections"]["schedule"] == {
        "status": "available",
        "source": "event_catalog",
        "context": "completed_season_catalog",
        "unavailable_reason": None,
        "collected_at": RETRIEVED_AT.isoformat(),
    }
    # The declaration is explicit: a client never has to read an empty array
    # or a freshness marker to learn the mode.
    assert payload["freshness"]["pool"]["state"] == "missing"


def test_completed_season_schedule_keeps_provenance_without_a_stale_warning():
    aged = NOW - timedelta(days=30)

    payload = _historical_service(
        events=RecordedEvents(events=[_final_event()], retrieved_at=aged)
    ).get_matchup(game_id=GAME_ID)

    assert payload["freshness"]["schedule"] == {
        "status": "stale",
        "retrieved_at": aged.isoformat(),
    }
    assert payload["experience"]["sections"]["schedule"] == {
        "status": "available",
        "source": "event_catalog",
        "context": "completed_season_catalog",
        "unavailable_reason": None,
        "collected_at": aged.isoformat(),
    }


def test_final_game_with_archived_closing_projections_stays_current():
    logs = RecordedGameLogs()

    payload = _historical_service(
        pool=_pool_with_freshness(_closing_projection_freshness()),
        player_logs=logs,
    ).get_matchup(game_id=GAME_ID)

    assert payload["experience"]["mode"] == "current"
    assert payload["experience"]["player_source"] == "player_pool"
    assert payload["experience"]["sections"]["schedule"]["context"] == (
        "current_season_catalog"
    )
    assert [row["canonical_id"] for row in payload["players"]] == [2544]
    assert payload["players"][0]["player_source"] == "player_pool"
    assert payload["players"][0]["posted_markets"] == ["PTS", "FGA"]
    assert payload["players"][0]["focal_game_line"] is None
    assert payload["game"]["away_team"]["targetable_player_count"] == 1
    # Governed archived projection evidence keeps the existing experience, so
    # the canonical game-log rail is never read.
    assert logs.game_row_calls == []


def test_a_final_game_with_a_servable_stored_pool_stays_current():
    """A landed stored pool is still governed evidence for a past game."""

    logs = RecordedGameLogs()

    payload = _historical_service(
        pool=_pool_with_freshness(
            {
                "status": "stale-served",
                "retrieved_at": RETRIEVED_AT.isoformat(),
                "providers": {},
            }
        ),
        player_logs=logs,
    ).get_matchup(game_id=GAME_ID)

    assert payload["experience"]["mode"] == "current"
    assert [row["canonical_id"] for row in payload["players"]] == [2544]
    assert logs.game_row_calls == []


def test_a_past_tip_that_is_not_final_is_not_a_historical_matchup():
    past = {**_event(), "scheduled_at": "2026-01-11T00:30:00+00:00"}

    payload = _historical_service(
        events=RecordedEvents(events=[past])
    ).get_matchup(game_id=GAME_ID)

    assert payload["experience"]["mode"] == "current"
    assert payload["players"] == []


def test_a_postponed_event_is_not_a_historical_matchup():
    payload = _historical_service(
        events=RecordedEvents(events=[_final_event(postponed_status="postponed")])
    ).get_matchup(game_id=GAME_ID)

    assert payload["experience"]["mode"] == "current"


def test_historical_matchup_reports_section_owned_evidence():
    payload = _historical_service().get_matchup(game_id=GAME_ID)

    sections = payload["experience"]["sections"]
    assert sections["participants"] == {
        "status": "available",
        "source": "player_game_logs",
        "context": "completed_season",
        "unavailable_reason": None,
    }
    assert sections["season_defense"] == {
        "status": "available",
        "source": "team_matchup_publication",
        "context": "completed_season",
        "unavailable_reason": None,
    }
    assert sections["last_15_defense"] == {
        "status": "unavailable",
        "source": None,
        "context": None,
        "unavailable_reason": "no_point_in_time_snapshot",
    }
    assert sections["injuries"] == {
        "status": "unavailable",
        "source": None,
        "context": None,
        "unavailable_reason": "no_pregame_snapshot",
    }
    # Section provenance never replaces the per-Base authority.
    assert payload["league"]["surface_availability"]["shot_zones"]["season"] == {
        "status": "available",
        "unavailable_reason": None,
    }


def test_a_retained_pregame_snapshot_is_labeled_pregame_and_removes_nobody():
    # A stopped game serves only its retained pre-tip snapshot, so available
    # historical injury evidence is pregame rather than current.
    injury_block = {
        "status": "fresh",
        "unavailable_reason": None,
        "retrieved_at": RETRIEVED_AT.isoformat(),
        "source": "rotowire",
        "source_url": "https://www.rotowire.com/basketball/injury-report.php",
        "teams": [],
    }
    injuries = RecordedInjuries(
        MatchupInjuryResult(
            injury_block,
            frozenset({AWAY_PARTICIPANT}),
            {AWAY_PARTICIPANT: "rotowire:6504"},
        )
    )

    payload = _historical_service(injuries=injuries).get_matchup(game_id=GAME_ID)

    assert payload["experience"]["sections"]["injuries"] == {
        "status": "available",
        "source": "rotowire",
        "context": "pregame",
        "unavailable_reason": None,
    }
    # A canonical row means the player appeared: injury evidence never removes
    # or badges a participant.
    players = {row["canonical_id"]: row for row in payload["players"]}
    assert set(players) == {AWAY_PARTICIPANT, HOME_PARTICIPANT}
    assert players[AWAY_PARTICIPANT]["injury_badge_ref"] is None


def test_historical_participants_come_from_game_logs_on_both_sides():
    payload = _historical_service().get_matchup(game_id=GAME_ID)

    players = {row["canonical_id"]: row for row in payload["players"]}
    assert set(players) == {AWAY_PARTICIPANT, HOME_PARTICIPANT}
    assert players[AWAY_PARTICIPANT]["team_id"] == LAL
    assert players[AWAY_PARTICIPANT]["tricode"] == "LAL"
    assert players[HOME_PARTICIPANT]["team_id"] == BOS
    assert players[HOME_PARTICIPANT]["tricode"] == "BOS"
    for player in players.values():
        assert player["player_source"] == "game_logs"
        # Box-score participants carry no posted-market claim.
        assert player["posted_markets"] == []
        assert player["provenance"] == {}
        assert player["injury_badge_ref"] is None
        assert set(player["scores"]) == set(player["stat_categories"])
        assert "PTS" in player["stat_categories"]
    assert payload["game"]["away_team"]["targetable_player_count"] == 0
    assert payload["game"]["home_team"]["targetable_player_count"] == 0


def test_historical_participants_use_the_game_time_team_not_a_current_roster():
    traded = _game_log_record(
        player_id=AWAY_PARTICIPANT,
        player_name="Away Participant",
        team_id=LAL,
        team_tricode="LAL",
        opponent_team_id=BOS,
        opponent_team_tricode="BOS",
    )

    # The catalog now carries a newer tricode for the same franchise; the
    # identity recorded for that game still governs the rail.
    renamed = _final_event(
        away_team_tricode="LAK",
        away_team={"id": LAL, "name": "Los Angeles Lakers", "tricode": "LAK"},
    )

    payload = _historical_service(
        player_logs=RecordedGameLogs(rows=(traded,)),
        events=RecordedEvents(events=[renamed]),
    ).get_matchup(game_id=GAME_ID)

    (player,) = payload["players"]
    assert (player["team_id"], player["tricode"]) == (LAL, "LAL")
    # The opposing rail is rendered from this identity alone.
    assert [
        row["canonical_id"]
        for row in payload["players"]
        if row["team_id"] != BOS
    ] == [AWAY_PARTICIPANT]


def test_historical_participants_carry_the_focal_line_excluded_from_inputs():
    logs = RecordedGameLogs()

    payload = _historical_service(player_logs=logs).get_matchup(game_id=GAME_ID)

    player = next(
        row for row in payload["players"] if row["canonical_id"] == AWAY_PARTICIPANT
    )
    assert player["focal_game_line"]["game_id"] == GAME_ID
    assert player["focal_game_line"]["game_date"] == "2026-01-10"
    assert player["focal_game_line"]["matchup"] == "LAL @ BOS"
    assert player["focal_game_line"]["minutes"] == 34.0
    assert player["focal_game_line"]["stats"]["PTS"] == 24.0
    # 24 points in the focal game against 21.0 completed-season context. The
    # displayed context is the completed season under its hindsight label; the
    # analytical read that feeds scores and samples drops the focal row.
    assert player["season_scoring"] == 21.0
    assert (
        (AWAY_PARTICIPANT, HOME_PARTICIPANT),
        GAME_ID,
    ) in logs.summary_calls


def test_historical_scores_name_missing_inputs_without_a_partial_blend():
    payload = _historical_service().get_matchup(game_id=GAME_ID)

    scores = payload["players"][0]["scores"]
    # No stored Diet evidence, so every Diet-backed window is unavailable
    # rather than silently partial.
    assert scores["PTS"]["season"] == {
        "components": {},
        "blend": None,
        "missing_inputs": [
            "player_diet:play_types",
            "player_diet:shot_zones",
            "player_diet:shot_types",
        ],
    }
    assert scores["PTS"]["last_15"]["missing_inputs"][0] == (
        "team_defense:play_types"
    )
    # The traditional Base consumes no Diet, so it still scores.
    assert scores["TOV"]["season"]["components"]["traditional"]["value"] is not None
    assert scores["TOV"]["season"]["missing_inputs"] == []


def test_a_historical_blend_is_withheld_while_its_components_still_show():
    # A window carrying OPP_REB lets the REB part score from team defense
    # alone, so a combo has one usable component and several unusable ones.
    rebounding = _window(
        traditional_metrics=(
            ("traditional", "OPP_REB", "OPP_REB", 44.0),
            ("traditional", "OPP_TOV", "OPP_TOV", 13.0),
            ("traditional", "OPP_STL", "OPP_STL", 7.0),
            ("traditional", "OPP_BLK", "OPP_BLK", 5.0),
        )
    )
    windows = RecordedTeamWindows(
        _window(),
        None,
        pre_focal_season_window=rebounding,
        pre_focal_cutoff=HISTORICAL_GAME_DATE - timedelta(days=1),
    )

    payload = _historical_service(
        diets=RecordedPartialDiets(), team_windows=windows
    ).get_matchup(game_id=GAME_ID)

    season = payload["players"][0]["scores"]["PRA"]["season"]
    # The REB part's evidence is still shown, but a mean of the survivors is
    # not a Blend of the contract's required inputs, so none is returned.
    assert season["components"]["traditional"]["value"] is not None
    assert season["blend"] is None
    assert season["missing_inputs"] == [
        "player_diet:play_types",
        "player_diet:shot_zones",
        "player_diet:shot_types",
        "player_diet:assist_locations",
    ]


def test_the_focal_row_moves_historical_display_but_never_the_score():
    # Display carries completed-season hindsight under its declared label;
    # analytical score and sample inputs exclude the focal row. Changing only
    # what the focal game contributes must move the first and not the second.
    small = _historical_service(
        player_logs=RecordedGameLogs(focal_points=3.0)
    ).get_matchup(game_id=GAME_ID)
    large = _historical_service(
        player_logs=RecordedGameLogs(focal_points=9.0)
    ).get_matchup(game_id=GAME_ID)

    assert small["players"][0]["season_scoring"] == 24.0
    assert large["players"][0]["season_scoring"] == 30.0
    assert small["players"][0]["last_10_minutes"] == [34.0, 32.0]
    assert large["players"][0]["last_10_minutes"] == [40.0, 32.0]
    assert [row["scores"] for row in small["players"]] == [
        row["scores"] for row in large["players"]
    ]


def test_historical_display_and_analysis_read_the_summary_separately():
    logs = RecordedGameLogs(focal_points=3.0)

    _historical_service(player_logs=logs).get_matchup(game_id=GAME_ID)

    # One read keeps the focal game for the labeled hindsight display, one
    # drops it for the analysis. Neither borrows the other's summary.
    assert {
        exclude_game_id for _ids, exclude_game_id in logs.summary_calls
    } == {GAME_ID, None}


def test_changing_the_focal_contribution_to_diet_cannot_move_a_historical_score():
    # The stored Player Diet is one completed-season aggregate per slice with
    # no game dimension, so its focal contribution cannot be subtracted. The
    # property the issue requires is therefore proven negatively: whatever the
    # aggregate says, the presented historical score is identical.
    without_diet = _historical_service().get_matchup(game_id=GAME_ID)
    with_diet = _historical_service(diets=RecordedPartialDiets()).get_matchup(
        game_id=GAME_ID
    )

    assert [row["scores"] for row in with_diet["players"]] == [
        row["scores"] for row in without_diet["players"]
    ]
    # The raw Diet evidence is independent of the score and still displayed.
    assert with_diet["players"][0]["diet_shares"]["play_types"]
    assert without_diet["players"][0]["diet_shares"]["play_types"] == []


def test_a_historical_score_names_player_diet_as_unusable_evidence():
    payload = _historical_service(diets=RecordedPartialDiets()).get_matchup(
        game_id=GAME_ID
    )

    season = payload["players"][0]["scores"]["PTS"]["season"]
    # No Diet-backed component survives, and the response says which inputs it
    # could not use rather than consuming a focal-contaminated aggregate.
    assert season["components"] == {}
    assert season["blend"] is None
    assert season["missing_inputs"] == [
        "player_diet:play_types",
        "player_diet:shot_zones",
        "player_diet:shot_types",
    ]


def test_a_historical_score_reads_team_defense_from_before_the_focal_game():
    contaminated = _window(
        traditional_metrics=(
            ("traditional", "OPP_TOV", "OPP_TOV", 99.0),
            ("traditional", "OPP_STL", "OPP_STL", 99.0),
            ("traditional", "OPP_BLK", "OPP_BLK", 99.0),
        )
    )
    windows = RecordedTeamWindows(
        contaminated,
        None,
        pre_focal_season_window=_window(),
        pre_focal_cutoff=HISTORICAL_GAME_DATE - timedelta(days=1),
    )

    payload = _historical_service(team_windows=windows).get_matchup(game_id=GAME_ID)

    # Scores consume the snapshot stored strictly before the focal date; the
    # completed-season sheet the focal game is inside is display-only.
    baseline = _historical_service().get_matchup(game_id=GAME_ID)
    assert [row["scores"] for row in payload["players"]] == [
        row["scores"] for row in baseline["players"]
    ]
    assert payload["league"]["defense_sheet"]["traditional"]
    assert any(
        as_of == HISTORICAL_GAME_DATE - timedelta(days=1) and strict
        for _season, _games, as_of, strict in windows.calls
    )


def test_only_a_later_completed_season_snapshot_scores_nothing_but_still_shows():
    # The issue 41 exemption hands a non-strict read the completed-season
    # snapshot for any earlier date in that season. That snapshot contains the
    # focal game, so scoring must refuse it while the sheet still renders.
    windows = RecordedTeamWindows(
        _window(),
        None,
        pre_focal_season_window=None,
        pre_focal_cutoff=HISTORICAL_GAME_DATE - timedelta(days=1),
    )

    payload = _historical_service(team_windows=windows).get_matchup(game_id=GAME_ID)

    # Display: the completed-season Defense Sheet is available and labeled.
    assert payload["experience"]["sections"]["season_defense"] == {
        "status": "available",
        "source": "team_matchup_publication",
        "context": "completed_season",
        "unavailable_reason": None,
    }
    assert payload["league"]["defense_sheet"]["shot_zones"]
    assert payload["teams"][0]["defense_sheet"]["shot_zones"][0]["season"]
    # Scoring: the same evidence is refused and named, not silently consumed.
    for player in payload["players"]:
        for windows_by_name in player["scores"].values():
            season = windows_by_name["season"]
            assert season["components"] == {}
            assert season.get("blend") is None
            assert season["missing_inputs"]
    assert payload["players"][0]["scores"]["TOV"]["season"]["missing_inputs"] == [
        "team_defense:traditional"
    ]
    # The scoring read asked strictly; the display read did not.
    assert (SEASON, None, HISTORICAL_GAME_DATE - timedelta(days=1), True) in (
        windows.calls
    )
    assert (SEASON, None, HISTORICAL_GAME_DATE, False) in windows.calls


def test_a_historical_score_is_withheld_without_a_pre_focal_defense_window():
    windows = RecordedTeamWindows(
        _window(),
        None,
        pre_focal_season_window=None,
        pre_focal_cutoff=HISTORICAL_GAME_DATE - timedelta(days=1),
    )

    payload = _historical_service(team_windows=windows).get_matchup(game_id=GAME_ID)

    season = payload["players"][0]["scores"]["TOV"]["season"]
    assert season["components"] == {}
    assert season["missing_inputs"] == ["team_defense:traditional"]
    # The completed-season Defense Sheet still renders for the reader.
    assert payload["experience"]["sections"]["season_defense"]["status"] == (
        "available"
    )
    assert payload["league"]["defense_sheet"]["shot_zones"]


def test_a_historical_window_with_every_required_input_keeps_its_cells():
    payload = _historical_service(diets=RecordedPartialDiets()).get_matchup(
        game_id=GAME_ID
    )

    # The traditional Base consumes no Diet, so TOV needs nothing it lacks and
    # its cells are exactly what they were. Defensive windows omit `blend`.
    season = payload["players"][0]["scores"]["TOV"]["season"]
    assert season["missing_inputs"] == []
    assert season["components"]["traditional"]["value"] is not None


def test_a_current_matchup_keeps_its_partial_blend_unchanged():
    payload = _service().get_matchup(game_id=GAME_ID)

    # Current-slate scoring is untouched: a mean of the present Bases is still
    # returned beside the named gaps.
    assert payload["players"][0]["scores"]["PTS"]["season"] == {
        "components": {"play_types": {"value": -0.011875, "thin": True}},
        "blend": {"value": -0.011875, "thin": True},
        "missing_inputs": ["player_diet:shot_zones", "player_diet:shot_types"],
    }


def test_participants_without_a_season_rate_stay_visible_and_name_the_gap():
    payload = _historical_service(
        player_logs=RecordedGameLogs(season_rate=False)
    ).get_matchup(game_id=GAME_ID)

    assert len(payload["players"]) == 2
    for player in payload["players"]:
        assert player["season_scoring"] is None
        assert player["scores"]["PRA"]["season"] == {
            "components": {},
            "blend": None,
            "missing_inputs": ["player_season_rate"],
        }
    # Participants without a score sort last but are never dropped.
    assert {row["canonical_id"] for row in payload["players"]} == {
        AWAY_PARTICIPANT,
        HOME_PARTICIPANT,
    }


def test_incomplete_game_logs_remove_only_participants():
    payload = _historical_service(
        player_logs=RecordedGameLogs(sync_status="in_progress")
    ).get_matchup(game_id=GAME_ID)

    assert payload["players"] == []
    assert payload["experience"]["sections"]["participants"] == {
        "status": "unavailable",
        "source": "player_game_logs",
        "context": None,
        "unavailable_reason": "game_logs_incomplete",
    }
    # The available completed-season Defense Sheet still renders.
    assert payload["experience"]["sections"]["season_defense"]["status"] == (
        "available"
    )
    assert payload["league"]["defense_sheet"]["shot_zones"]
    assert payload["teams"][0]["defense_sheet"]["shot_zones"][0]["season"]


def test_a_game_without_canonical_rows_names_its_own_reason():
    payload = _historical_service(
        player_logs=RecordedGameLogs(rows=())
    ).get_matchup(game_id=GAME_ID)

    assert payload["players"] == []
    assert payload["experience"]["sections"]["participants"][
        "unavailable_reason"
    ] == "no_game_log_rows"


def test_available_season_defense_survives_every_other_missing_surface():
    payload = _historical_service(
        player_logs=RecordedGameLogs(sync_status=None)
    ).get_matchup(game_id=GAME_ID)

    # No pool, no legacy stats_tables marker, no Last 15, no injuries, no
    # participants: none of them governs the Season Defense Sheet.
    assert payload["freshness"]["pool"]["state"] == "missing"
    assert payload["freshness"]["stats"] == {
        "status": "missing",
        "retrieved_at": None,
    }
    assert payload["freshness"]["injuries"]["status"] == "unavailable"
    assert payload["freshness"]["team_matchups"]["last_15"]["status"] == (
        "unavailable"
    )
    assert payload["players"] == []
    assert payload["experience"]["sections"]["season_defense"]["status"] == (
        "available"
    )
    assert payload["league"]["surface_availability"]["traditional"]["season"][
        "status"
    ] == "available"
    assert payload["teams"][1]["defense_sheet"]["traditional"][0]["season"]


def test_current_matchup_declares_its_mode_and_keeps_its_existing_fields():
    payload = _service().get_matchup(game_id=GAME_ID)

    assert payload["experience"]["mode"] == "current"
    assert payload["experience"]["player_source"] == "player_pool"
    assert payload["experience"]["sections"]["schedule"] == {
        "status": "available",
        "source": "event_catalog",
        "context": "current_season_catalog",
        "unavailable_reason": None,
        "collected_at": RETRIEVED_AT.isoformat(),
    }
    assert payload["experience"]["sections"]["participants"] == {
        "status": "available",
        "source": "player_pool",
        "context": "posted_markets",
        "unavailable_reason": None,
    }
    assert payload["experience"]["sections"]["injuries"] == {
        "status": "unavailable",
        "source": "rotowire",
        "context": "current",
        "unavailable_reason": "disabled",
    }
    assert payload["players"][0]["posted_markets"] == ["PTS", "FGA"]
    assert set(payload) == {
        "game",
        "league",
        "teams",
        "players",
        "injuries",
        "freshness",
        "provenance",
        "coverage",
        "experience",
    }
