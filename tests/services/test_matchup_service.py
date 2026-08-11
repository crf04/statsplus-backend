"""Stored matchup composition at the application-service seam."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import ProviderUnavailableError, ResourceNotFoundError
from app.services.matchup import MatchupService
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

    def count_events(self, season):
        assert season == SEASON
        return self.count

    def get_events(self, season):
        assert season == SEASON
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


def _window(*, last_15=False):
    metrics = (
        ("play_types", "Transition", "PTS", 16.0),
        ("shot_zones", "Restricted Area", "FGA", 20.0),
        ("shot_types", "catch_and_shoot", "FG3A", 7.0),
        ("assist_locations", "AtRimAssists", "AtRimAssists", 8.0),
        ("traditional", "OPP_TOV", "OPP_TOV", 13.0),
        ("traditional", "OPP_STL", "OPP_STL", 7.0),
        ("traditional", "OPP_BLK", "OPP_BLK", 5.0),
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
            base,
            "unavailable" if last_15 and base == "play_types" else "available",
            "provider_unsupported" if last_15 and base == "play_types" else None,
            RETRIEVED_AT,
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
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
        ),
        clock=lambda: NOW,
    )


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
            "unavailable_reason": "provider_unsupported",
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


def test_matchup_player_rows_are_integer_season_only_raw_and_truthfully_unscored():
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
            "status": "unavailable",
            "unavailable_reason": "not_in_scope",
        },
        "injury_badge_ref": None,
    }
    assert all(
        "last_15" not in row for rows in player["diet_shares"].values() for row in rows
    )


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
    assert all(
        availability[window]["status"] == "missing"
        for availability in payload["league"]["surface_availability"].values()
        for window in ("season", "last_15")
    )
    assert all(not rows for rows in payload["league"]["defense_sheet"].values())


def test_unknown_game_is_404_while_an_empty_schedule_surface_is_503():
    with pytest.raises(ResourceNotFoundError):
        _service(events=RecordedEvents(events=[])).get_matchup(game_id="unknown")

    with pytest.raises(ProviderUnavailableError):
        _service(events=RecordedEvents(events=[], count=0)).get_matchup(game_id=GAME_ID)
