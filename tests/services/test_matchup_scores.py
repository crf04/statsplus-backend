"""Hand-computed Matchup Scores at the stored public service seam."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.services.matchup import DEFENSE_BASES, MatchupService
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


SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738
NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)


class _Events:
    def count_events(self, season):
        return 1

    def get_events(self, season):
        return [
            {
                "nba_game_id": GAME_ID,
                "scheduled_at": "2026-01-15T20:00:00+00:00",
                "status_code": 1,
                "status_text": "Scheduled",
                "classification": "Regular Season",
                "away_team_id": LAL,
                "away_team_name": "Los Angeles Lakers",
                "away_team_tricode": "LAL",
                "home_team_id": BOS,
                "home_team_name": "Boston Celtics",
                "home_team_tricode": "BOS",
                "away_team": {
                    "id": LAL,
                    "name": "Los Angeles Lakers",
                    "tricode": "LAL",
                },
                "home_team": {
                    "id": BOS,
                    "name": "Boston Celtics",
                    "tricode": "BOS",
                },
            }
        ]

    def get_freshness(self, season, *, now):
        return {"last_success_at": RETRIEVED_AT.isoformat()}


class _Logs:
    def __init__(self, per_game):
        self.per_game = per_game

    def get_player_summaries(self, season, player_ids):
        return {
            2544: PlayerSeasonLogSummary(
                season=SEASON,
                player_id=2544,
                season_rate=PlayerSeasonRate(
                    season=SEASON,
                    player_id=2544,
                    game_count=20,
                    total_minutes=700,
                    per_game=self.per_game,
                    per_minute={},
                ),
                last_ten_minutes=(35.0,),
            )
        }

    def get_read_freshness(self, season):
        return PlayerGameLogReadFreshness("fresh", RETRIEVED_AT)


class _Diets:
    def __init__(self, facts):
        self.facts = tuple(facts)

    def get_for_players(self, season, player_ids):
        bases = {fact.base for fact in self.facts}
        return PlayerDietResult(
            season=SEASON,
            players={2544: self.facts},
            observations=tuple(
                StoredPlayerDietObservation(
                    base,
                    "available" if base in bases else "missing",
                    None if base in bases else "provider_no_observation",
                    RETRIEVED_AT,
                )
                for base in DEFENSE_BASES
                if base != "traditional"
            ),
        )


class _Windows:
    def __init__(self, season, last_15):
        self.season = season
        self.last_15 = last_15

    def get_latest_window(self, season, *, window_games=None, as_of=None):
        return self.last_15 if window_games == 15 else self.season


def _fact(base, slice_key, share, volume, games=20):
    units = {
        "play_types": ("possessions", "nba_synergy"),
        "shot_zones": ("field_goal_attempts", "nba_stats"),
        "shot_types": ("field_goal_attempts", "nba_stats"),
        "assist_locations": ("assists", "pbp_stats"),
    }
    unit, provider = units[base]
    return StoredPlayerDietFact(
        2544,
        base,
        slice_key,
        share,
        volume,
        games,
        unit,
        provider,
        RETRIEVED_AT,
    )


def _window(metrics, *, last_15=False, unsupported=()):
    league = tuple(
        LeagueMatchupMetric(base, slice_key, stat, average, 1.0, 30)
        for base, slice_key, stat, average, _opponent in metrics
    )
    teams = {
        LAL: tuple(
            TeamMatchupMetric(base, slice_key, stat, average, 0.0, 0.0, 15)
            for base, slice_key, stat, average, _opponent in metrics
        ),
        BOS: tuple(
            TeamMatchupMetric(
                base,
                slice_key,
                stat,
                opponent,
                (opponent / average - 1) * 100,
                opponent - average,
                16,
            )
            for base, slice_key, stat, average, opponent in metrics
        ),
    }
    available = {base for base, *_rest in metrics}
    observations = tuple(
        StoredTeamMatchupObservation(
            base,
            "unavailable" if base in unsupported or base not in available else "available",
            "provider_unsupported" if base in unsupported or base not in available else None,
            RETRIEVED_AT,
        )
        for base in DEFENSE_BASES
    )
    scope = TeamMatchupSnapshotScope(
        SEASON, date(2026, 1, 15), 15 if last_15 else None
    )
    return TeamMatchupWindow(
        scope=scope,
        fact_scopes={base: scope for base in available},
        fact_retrieved_at={base: RETRIEVED_AT for base in available},
        league_metrics=league,
        team_metrics=teams,
        observations=observations,
    )


def _service(*, markets, facts, season_metrics, last_15_metrics=None, per_game=None):
    pool = PlayerPool(
        (
            PoolPlayer(
                2544,
                "LeBron James",
                LAL,
                tuple(markets),
                {"prizepicks": tuple(markets)},
            ),
        ),
        {LAL: 1},
        {
            "status": "fresh",
            "retrieved_at": RETRIEVED_AT.isoformat(),
            "providers": {},
        },
    )
    season = _window(season_metrics)
    last_15 = _window(
        last_15_metrics if last_15_metrics is not None else season_metrics,
        last_15=True,
        unsupported=("play_types",),
    )
    return MatchupService(
        event_catalog=_Events(),
        player_pool=SimpleNamespace(
            get_pool_for_game=lambda **_kwargs: pool,
        ),
        player_logs=_Logs(per_game or {"PTS": 20.0}),
        player_diets=_Diets(facts),
        team_matchups=_Windows(season, last_15),
        stats_freshness=SimpleNamespace(get=lambda: StatsFreshness(RETRIEVED_AT)),
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
        ),
        clock=lambda: NOW,
    )


def test_play_type_score_is_a_hand_computed_posted_market_row():
    player = _service(
        markets=("PTS",),
        facts=(
            _fact("play_types", "Transition", 0.25, 50),
            _fact("play_types", "Isolation", 0.75, 150),
        ),
        season_metrics=(
            ("play_types", "Transition", "PTS", 10.0, 12.0),
            ("play_types", "Isolation", "PTS", 20.0, 18.0),
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["scores"] == {
        "PTS": {
            "season": {
                "components": {
                    "play_types": {"value": -0.025, "thin": False}
                },
                "blend": {"value": -0.025, "thin": False},
            },
            "last_15": {"components": {}, "blend": None},
        }
    }


def test_shot_zone_score_uses_independent_season_and_last_15_concessions():
    slices = (
        "Restricted Area",
        "In The Paint (Non-RA)",
        "Mid-Range",
        "Corner 3",
        "Above the Break 3",
    )
    player = _service(
        markets=("FGA",),
        facts=tuple(_fact("shot_zones", slice_key, 0.2, 20) for slice_key in slices),
        season_metrics=tuple(
            (
                "shot_zones",
                slice_key,
                "FGA",
                10.0,
                15.0 if slice_key == "Restricted Area" else 10.0,
            )
            for slice_key in slices
        ),
        last_15_metrics=tuple(
            (
                "shot_zones",
                slice_key,
                "FGA",
                10.0,
                5.0 if slice_key == "Restricted Area" else 10.0,
            )
            for slice_key in slices
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["scores"]["FGA"] == {
        "season": {
            "components": {"shot_zones": {"value": 0.1, "thin": False}},
            "blend": {"value": 0.1, "thin": False},
        },
        "last_15": {
            "components": {"shot_zones": {"value": -0.1, "thin": False}},
            "blend": {"value": -0.1, "thin": False},
        },
    }


def test_shot_type_score_derives_fga_from_stored_two_and_three_attempts():
    player = _service(
        markets=("FGA",),
        facts=(
            _fact("shot_types", "Catch and Shoot", 0.5, 50),
            _fact("shot_types", "Pullups", 0.25, 25),
            _fact("shot_types", "Less Than 10 ft", 0.25, 25),
        ),
        season_metrics=(
            ("shot_types", "catch_and_shoot", "FG2A", 6.0, 7.0),
            ("shot_types", "catch_and_shoot", "FG3A", 4.0, 5.0),
            ("shot_types", "pullups", "FG2A", 6.0, 5.0),
            ("shot_types", "pullups", "FG3A", 4.0, 3.0),
            ("shot_types", "less_than_10_ft", "FG2A", 6.0, 6.0),
            ("shot_types", "less_than_10_ft", "FG3A", 4.0, 4.0),
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["scores"]["FGA"]["season"] == {
        "components": {"shot_types": {"value": 0.05, "thin": False}},
        "blend": {"value": 0.05, "thin": False},
    }


def test_assist_location_score_uses_the_player_assist_diet():
    slices = (
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    )
    player = _service(
        markets=("AST",),
        facts=tuple(
            _fact("assist_locations", slice_key, 0.2, 8) for slice_key in slices
        ),
        season_metrics=tuple(
            (
                "assist_locations",
                slice_key,
                slice_key,
                5.0,
                10.0 if slice_key == "AtRimAssists" else 5.0,
            )
            for slice_key in slices
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["scores"]["AST"]["season"] == {
        "components": {"assist_locations": {"value": 0.2, "thin": False}},
        "blend": {"value": 0.2, "thin": False},
    }


def test_combo_score_weights_component_markets_by_season_volumes():
    assist_slices = (
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    )
    metrics = (
        ("play_types", "Transition", "PTS", 10.0, 12.0),
        *(
            (
                "assist_locations",
                slice_key,
                slice_key,
                5.0,
                2.5 if slice_key == "AtRimAssists" else 5.0,
            )
            for slice_key in assist_slices
        ),
    )
    player = _service(
        markets=("PA",),
        facts=(
            _fact("play_types", "Transition", 1.0, 100),
            *(
                _fact("assist_locations", slice_key, 0.2, 8)
                for slice_key in assist_slices
            ),
        ),
        season_metrics=metrics,
        per_game={"PTS": 30.0, "AST": 10.0, "PA": 40.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert set(player["scores"]) == {"PA"}
    assert player["scores"]["PA"] == {
        "season": {
            "components": {
                "play_types": {"value": 0.2, "thin": False},
                "assist_locations": {"value": -0.1, "thin": False},
            },
            "blend": {"value": 0.125, "thin": False},
        },
        "last_15": {
            "components": {
                "assist_locations": {"value": -0.1, "thin": False}
            },
            "blend": {"value": -0.1, "thin": False},
        },
    }


def test_defensive_scores_use_one_traditional_component_and_no_blend():
    player = _service(
        markets=("TOV", "STKS"),
        facts=(),
        season_metrics=(
            ("traditional", "OPP_TOV", "OPP_TOV", 10.0, 12.0),
            ("traditional", "OPP_STL", "OPP_STL", 5.0, 4.0),
            ("traditional", "OPP_BLK", "OPP_BLK", 2.0, 3.0),
        ),
        per_game={"TOV": 4.0, "STL": 2.0, "BLK": 1.0, "STKS": 3.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["scores"]["TOV"]["season"] == {
        "components": {"traditional": {"value": 0.2, "thin": False}}
    }
    assert player["scores"]["STKS"]["season"] == {
        "components": {
            "traditional": {"value": 0.033333, "thin": False}
        }
    }


def test_offensive_blend_is_the_mean_of_computable_base_components():
    zone_slices = (
        "Restricted Area",
        "In The Paint (Non-RA)",
        "Mid-Range",
        "Corner 3",
        "Above the Break 3",
    )
    type_slices = ("catch_and_shoot", "pullups", "less_than_10_ft")
    metrics = [
        ("play_types", "Transition", "PTS", 10.0, 12.0),
        *(
            (
                "shot_zones",
                slice_key,
                "FGM",
                10.0,
                15.0 if slice_key == "Restricted Area" else 10.0,
            )
            for slice_key in zone_slices
        ),
    ]
    for slice_key in type_slices:
        metrics.extend(
            (
                (
                    "shot_types",
                    slice_key,
                    "FG2M",
                    2.0,
                    1.0 if slice_key == "pullups" else 2.0,
                ),
                ("shot_types", slice_key, "FG3M", 2.0, 2.0),
            )
        )
    player = _service(
        markets=("PTS",),
        facts=(
            _fact("play_types", "Transition", 1.0, 100),
            *(_fact("shot_zones", slice_key, 0.2, 20) for slice_key in zone_slices),
            _fact("shot_types", "Catch and Shoot", 0.5, 50),
            _fact("shot_types", "Pullups", 0.25, 25),
            _fact("shot_types", "Less Than 10 ft", 0.25, 25),
        ),
        season_metrics=tuple(metrics),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["scores"]["PTS"]["season"] == {
        "components": {
            "play_types": {"value": 0.2, "thin": False},
            "shot_zones": {"value": 0.1, "thin": False},
            "shot_types": {"value": -0.05, "thin": False},
        },
        "blend": {"value": 0.083333, "thin": False},
    }


def test_thin_flag_marks_low_player_season_volume_without_blanking_the_score():
    metrics = tuple(
        metric
        for slice_key in ("catch_and_shoot", "pullups", "less_than_10_ft")
        for metric in (
            ("shot_types", slice_key, "FG2A", 6.0, 7.0),
            ("shot_types", slice_key, "FG3A", 4.0, 5.0),
        )
    )
    player = _service(
        markets=("FGA",),
        facts=(
            _fact("shot_types", "Catch and Shoot", 0.5, 30),
            _fact("shot_types", "Pullups", 0.25, 15),
            _fact("shot_types", "Less Than 10 ft", 0.25, 15),
        ),
        season_metrics=metrics,
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["scores"]["FGA"]["season"] == {
        "components": {"shot_types": {"value": 0.2, "thin": True}},
        "blend": {"value": 0.2, "thin": True},
    }


def test_thin_flag_marks_a_low_player_game_sample():
    player = _service(
        markets=("PTS",),
        facts=(_fact("play_types", "Transition", 1.0, 100, games=4),),
        season_metrics=(
            ("play_types", "Transition", "PTS", 10.0, 12.0),
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["scores"]["PTS"]["season"] == {
        "components": {"play_types": {"value": 0.2, "thin": True}},
        "blend": {"value": 0.2, "thin": True},
    }
