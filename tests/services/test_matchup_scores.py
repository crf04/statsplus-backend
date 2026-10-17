"""Hand-computed Matchup Scores at the stored public service seam."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.models.catalogs import PLAY_TYPES
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


def _computed_scores(player):
    """The computed score cells, with the additive input naming checked once.

    Every window carries `missing_inputs` naming the score-contract inputs it
    could not consume. These tests assert the arithmetic, so the additive key
    is verified here and removed from the compared shape; its exact values are
    asserted in `tests/services/test_matchup_service.py`.
    """

    windows = [window for market in player["scores"].values() for window in market.values()]
    assert windows
    assert all(isinstance(window["missing_inputs"], list) for window in windows)
    return {
        market: {
            name: {key: value for key, value in window.items() if key != "missing_inputs"}
            for name, window in market_windows.items()
        }
        for market, market_windows in player["scores"].items()
    }


SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738
NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)


class _CountingSequence:
    def __init__(self, values):
        self.values = tuple(values)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def __getitem__(self, index):
        return self.values[index]


class _CountingDietFact:
    def __init__(self, fact):
        self.base = fact.base
        self.slice_key = fact.slice_key
        self.volume = fact.volume
        self.games_played = fact.games_played
        self.volume_unit = fact.volume_unit
        self.provider = fact.provider
        self.retrieved_at = fact.retrieved_at
        self._share = fact.share
        self.share_reads = 0

    @property
    def share(self):
        self.share_reads += 1
        return self._share


class _Events:
    def count_events(self, season):
        return 1

    def get_event(self, season, game_id):
        if game_id != GAME_ID:
            return None
        return {
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

    def latest_final_scheduled_at(self, season):
        return None

    def get_freshness(self, season, *, now):
        return {"last_success_at": RETRIEVED_AT.isoformat()}


class _Logs:
    def __init__(self, per_game, *, game_count=20):
        self.per_game = per_game
        self.game_count = game_count

    def get_player_summaries(self, season, player_ids):
        return {
            2544: PlayerSeasonLogSummary(
                season=SEASON,
                player_id=2544,
                season_rate=PlayerSeasonRate(
                    season=SEASON,
                    player_id=2544,
                    game_count=self.game_count,
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
        "rebound_types": ("rebounds", "pbp_stats"),
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


def _complete_play_facts(*facts):
    observed = {fact.slice_key for fact in facts}
    games = facts[0].games_played
    return (
        *facts,
        *(
            _fact("play_types", slice_key, 0.0, 0.0, games=games)
            for slice_key in PLAY_TYPES
            if slice_key not in observed
        ),
    )


def _window(metrics, *, last_15=False, unsupported=(), metric_trackers=None):
    league_values = tuple(
        LeagueMatchupMetric(base, slice_key, stat, average, 1.0, 30)
        for base, slice_key, stat, average, _opponent in metrics
    )
    team_values = {
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
                (opponent / average - 1) * 100 if average else 0.0,
                opponent - average,
                16,
            )
            for base, slice_key, stat, average, opponent in metrics
        ),
    }
    if metric_trackers is None:
        league = league_values
        teams = team_values
    else:
        league = _CountingSequence(league_values)
        teams = {team_id: _CountingSequence(values) for team_id, values in team_values.items()}
        metric_trackers.extend((league, *teams.values()))
    available = {base for base, *_rest in metrics}
    observations = tuple(
        StoredTeamMatchupObservation(
            surface=base,
            status="unavailable" if base in unsupported or base not in available else "available",
            unavailable_reason="provider_window_unsupported"
            if base in unsupported or base not in available
            else None,
            retrieved_at=RETRIEVED_AT,
        )
        for base in DEFENSE_BASES
    )
    scope = TeamMatchupSnapshotScope(SEASON, date(2026, 1, 15), 15 if last_15 else None)
    return TeamMatchupWindow(
        scope=scope,
        fact_scopes={base: scope for base in available},
        fact_retrieved_at={base: RETRIEVED_AT for base in available},
        league_metrics=league,
        team_metrics=teams,
        observations=observations,
    )


def _service(
    *,
    markets,
    facts,
    season_metrics,
    last_15_metrics=None,
    per_game=None,
    game_count=20,
    metric_trackers=None,
):
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
    season = _window(season_metrics, metric_trackers=metric_trackers)
    last_15 = _window(
        last_15_metrics if last_15_metrics is not None else season_metrics,
        last_15=True,
        unsupported=("play_types",),
        metric_trackers=metric_trackers,
    )
    return MatchupService(
        event_catalog=_Events(),
        player_pool=SimpleNamespace(
            get_pool_for_game=lambda **_kwargs: pool,
        ),
        player_logs=_Logs(per_game or {"PTS": 20.0}, game_count=game_count),
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
        facts=_complete_play_facts(
            _fact("play_types", "Transition", 0.25, 50),
            _fact("play_types", "Isolation", 0.75, 150),
        ),
        season_metrics=(
            ("play_types", "Transition", "PTS", 10.0, 12.0),
            ("play_types", "Isolation", "PTS", 20.0, 18.0),
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player) == {
        "PTS": {
            "season": {
                "components": {"play_types": {"value": -0.025, "thin": False}},
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

    assert _computed_scores(player)["FGA"] == {
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

    assert _computed_scores(player)["FGA"]["season"] == {
        "components": {"shot_types": {"value": 0.05, "thin": False}},
        "blend": {"value": 0.05, "thin": False},
    }


def test_structural_zero_shot_types_do_not_poison_supported_3pm_and_fg3a_slices():
    assist_slices = (
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    )
    player = _service(
        markets=("3PM", "FG3A", "PA"),
        facts=(
            _fact("shot_types", "Catch and Shoot", 0.4, 40),
            _fact("shot_types", "Pullups", 0.3, 30),
            _fact("shot_types", "Less Than 10 ft", 0.3, 30),
            *(_fact("assist_locations", slice_key, 0.2, 8) for slice_key in assist_slices),
        ),
        season_metrics=(
            ("shot_types", "catch_and_shoot", "FG2M", 2.0, 2.0),
            ("shot_types", "catch_and_shoot", "FG3M", 4.0, 5.0),
            ("shot_types", "catch_and_shoot", "FG3A", 8.0, 10.0),
            ("shot_types", "pullups", "FG2M", 2.0, 2.0),
            ("shot_types", "pullups", "FG3M", 2.0, 1.0),
            ("shot_types", "pullups", "FG3A", 4.0, 2.0),
            ("shot_types", "less_than_10_ft", "FG2M", 6.0, 6.0),
            ("shot_types", "less_than_10_ft", "FG3M", 0.0, 0.0),
            ("shot_types", "less_than_10_ft", "FG3A", 0.0, 0.0),
            *(("assist_locations", slice_key, slice_key, 5.0, 5.0) for slice_key in assist_slices),
        ),
        per_game={"PTS": 30.0, "AST": 10.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["3PM"]["season"] == {
        "components": {"shot_types": {"value": -0.05, "thin": False}},
        "blend": {"value": -0.05, "thin": False},
    }
    assert _computed_scores(player)["FG3A"]["season"] == {
        "components": {"shot_types": {"value": -0.05, "thin": False}},
        "blend": {"value": -0.05, "thin": False},
    }
    assert _computed_scores(player)["PA"]["season"] == {
        "components": {
            "shot_types": {"value": -0.01125, "thin": False},
            "assist_locations": {"value": 0.0, "thin": False},
        },
        "blend": {"value": -0.01125, "thin": False},
    }


def test_all_structural_zero_slices_emit_no_component_or_blend():
    player = _service(
        markets=("3PM",),
        facts=(
            _fact("shot_types", "Catch and Shoot", 0.4, 40),
            _fact("shot_types", "Pullups", 0.3, 30),
            _fact("shot_types", "Less Than 10 ft", 0.3, 30),
        ),
        season_metrics=tuple(
            ("shot_types", slice_key, "FG3M", 0.0, 0.0)
            for slice_key in (
                "catch_and_shoot",
                "pullups",
                "less_than_10_ft",
            )
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["3PM"]["season"] == {
        "components": {},
        "blend": None,
    }


def test_zero_league_with_positive_team_evidence_fails_closed():
    player = _service(
        markets=("FG3A",),
        facts=(
            _fact("shot_types", "Catch and Shoot", 0.4, 40),
            _fact("shot_types", "Pullups", 0.3, 30),
            _fact("shot_types", "Less Than 10 ft", 0.3, 30),
        ),
        season_metrics=(
            ("shot_types", "catch_and_shoot", "FG3A", 8.0, 10.0),
            ("shot_types", "pullups", "FG3A", 4.0, 2.0),
            ("shot_types", "less_than_10_ft", "FG3A", 0.0, 1.0),
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["FG3A"]["season"] == {
        "components": {},
        "blend": None,
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
        facts=tuple(_fact("assist_locations", slice_key, 0.2, 8) for slice_key in slices),
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

    assert _computed_scores(player)["AST"]["season"] == {
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
        facts=_complete_play_facts(
            _fact("play_types", "Transition", 1.0, 100),
            *(_fact("assist_locations", slice_key, 0.2, 8) for slice_key in assist_slices),
        ),
        season_metrics=metrics,
        per_game={"PTS": 30.0, "AST": 10.0, "PA": 40.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert set(_computed_scores(player)) == {"PA"}
    assert _computed_scores(player)["PA"] == {
        "season": {
            "components": {
                "play_types": {"value": 0.15, "thin": False},
                "assist_locations": {"value": -0.025, "thin": False},
            },
            "blend": {"value": 0.125, "thin": False},
        },
        "last_15": {
            "components": {"assist_locations": {"value": -0.025, "thin": True}},
            "blend": {"value": -0.025, "thin": True},
        },
    }


def test_defensive_scores_use_one_traditional_component_and_no_blend():
    player = _service(
        markets=("TOV", "STKS"),
        facts=(),
        season_metrics=(
            ("traditional", "OPP_REB", "OPP_REB", 10.0, 10.0),
            ("traditional", "OPP_TOV", "OPP_TOV", 10.0, 12.0),
            ("traditional", "OPP_STL", "OPP_STL", 5.0, 4.0),
            ("traditional", "OPP_BLK", "OPP_BLK", 2.0, 3.0),
        ),
        per_game={"TOV": 4.0, "STL": 2.0, "BLK": 1.0, "STKS": 3.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["TOV"]["season"] == {
        "components": {"traditional": {"value": 0.2, "thin": False}}
    }
    assert _computed_scores(player)["STKS"]["season"] == {
        "components": {"traditional": {"value": 0.033333, "thin": False}}
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
        facts=_complete_play_facts(
            _fact("play_types", "Transition", 1.0, 100),
            *(_fact("shot_zones", slice_key, 0.2, 20) for slice_key in zone_slices),
            _fact("shot_types", "Catch and Shoot", 0.5, 50),
            _fact("shot_types", "Pullups", 0.25, 25),
            _fact("shot_types", "Less Than 10 ft", 0.25, 25),
        ),
        season_metrics=tuple(metrics),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["PTS"]["season"] == {
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

    assert _computed_scores(player)["FGA"]["season"] == {
        "components": {"shot_types": {"value": 0.2, "thin": True}},
        "blend": {"value": 0.2, "thin": True},
    }


def test_thin_flag_marks_a_low_player_game_sample():
    player = _service(
        markets=("PTS",),
        facts=_complete_play_facts(_fact("play_types", "Transition", 1.0, 100, games=4)),
        season_metrics=(("play_types", "Transition", "PTS", 10.0, 12.0),),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["PTS"]["season"] == {
        "components": {"play_types": {"value": 0.2, "thin": True}},
        "blend": {"value": 0.2, "thin": True},
    }


def test_rebound_score_does_not_use_the_player_season_game_floor():
    traditional_metrics = (
        ("traditional", "OPP_REB", "OPP_REB", 10.0, 11.0),
        ("traditional", "OPP_TOV", "OPP_TOV", 10.0, 10.0),
        ("traditional", "OPP_STL", "OPP_STL", 10.0, 10.0),
        ("traditional", "OPP_BLK", "OPP_BLK", 10.0, 10.0),
    )
    player = _service(
        markets=("REB",),
        facts=(),
        season_metrics=traditional_metrics,
        per_game={"REB": 8.0},
        game_count=1,
    ).get_matchup(game_id=GAME_ID)["players"][0]

    expected = {
        "components": {"traditional": {"value": 0.1, "thin": False}},
        "blend": {"value": 0.1, "thin": False},
    }
    assert _computed_scores(player)["REB"]["season"] == expected
    assert _computed_scores(player)["REB"]["last_15"] == expected


def test_combo_components_and_blend_share_the_season_rate_game_floor():
    assist_slices = (
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    )
    player = _service(
        markets=("PA",),
        facts=_complete_play_facts(
            _fact("play_types", "Transition", 1.0, 100),
            *(_fact("assist_locations", slice_key, 0.2, 8) for slice_key in assist_slices),
        ),
        season_metrics=(
            ("play_types", "Transition", "PTS", 10.0, 12.0),
            *(("assist_locations", slice_key, slice_key, 5.0, 2.5) for slice_key in assist_slices),
        ),
        per_game={"PTS": 30.0, "AST": 10.0, "PA": 40.0},
        game_count=4,
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["PA"]["season"] == {
        "components": {
            "play_types": {"value": 0.15, "thin": True},
            "assist_locations": {"value": -0.125, "thin": True},
        },
        "blend": {"value": 0.025, "thin": True},
    }


def test_complete_provider_rounded_diet_partitions_score_without_normalization():
    play_shares = (0.089,) * 10 + (0.108,)  # observed provider range sums to 0.998
    shot_facts = (
        _fact("shot_types", "Catch and Shoot", 0.475, 515, games=81),
        _fact("shot_types", "Pullups", 0.246, 267, games=81),
        _fact("shot_types", "Less Than 10 ft", 0.278, 301, games=81),
    )
    metrics = tuple(("play_types", slice_key, "PTS", 10.0, 11.0) for slice_key in PLAY_TYPES) + tuple(
        metric
        for slice_key in ("catch_and_shoot", "pullups", "less_than_10_ft")
        for metric in (
            ("shot_types", slice_key, "FG2A", 6.0, 6.6),
            ("shot_types", slice_key, "FG3A", 4.0, 4.4),
        )
    )
    player = _service(
        markets=("PTS", "FGA"),
        facts=(
            *(
                _fact("play_types", slice_key, share, 20)
                for slice_key, share in zip(PLAY_TYPES, play_shares, strict=True)
            ),
            *shot_facts,
        ),
        season_metrics=metrics,
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["PTS"]["season"] == {
        "components": {"play_types": {"value": 0.0998, "thin": False}},
        "blend": {"value": 0.0998, "thin": False},
    }
    assert _computed_scores(player)["FGA"]["season"] == {
        "components": {"shot_types": {"value": 0.0999, "thin": False}},
        "blend": {"value": 0.0999, "thin": False},
    }


def test_unobserved_rounded_diet_residual_is_neutral_for_primitives_and_combos():
    assist_slices = (
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    )
    shot_type_slices = (
        ("Catch and Shoot", "catch_and_shoot", 0.4, 40),
        ("Pullups", "pullups", 0.3, 30),
        ("Less Than 10 ft", "less_than_10_ft", 0.2, 20),
    )
    player = _service(
        markets=("PTS", "PA"),
        facts=(
            *(
                _fact("shot_types", stored_key, share, volume)
                for stored_key, _metric_key, share, volume in shot_type_slices
            ),
            *(_fact("assist_locations", slice_key, 0.2, 8) for slice_key in assist_slices),
        ),
        season_metrics=(
            *(
                metric
                for _stored_key, metric_key, _share, _volume in shot_type_slices
                for metric in (
                    ("shot_types", metric_key, "FG2M", 2.0, 2.0),
                    ("shot_types", metric_key, "FG3M", 2.0, 2.0),
                )
            ),
            *(("assist_locations", slice_key, slice_key, 5.0, 6.0) for slice_key in assist_slices),
        ),
        per_game={"PTS": 30.0, "AST": 10.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["PTS"]["season"] == {
        "components": {"shot_types": {"value": 0.0, "thin": False}},
        "blend": {"value": 0.0, "thin": False},
    }
    assert _computed_scores(player)["PA"]["season"] == {
        "components": {
            "shot_types": {"value": 0.0, "thin": False},
            "assist_locations": {"value": 0.05, "thin": False},
        },
        "blend": {"value": 0.05, "thin": False},
    }


def test_missing_diet_slices_fail_closed_except_for_the_partial_play_type_partition():
    # Synergy omits a play type a player rarely runs, so ten observed slices
    # are a complete play-type Diet; a shot-type Diet missing a slice is not.
    player = _service(
        markets=("PTS", "FGA"),
        facts=(
            *(_fact("play_types", slice_key, 0.1, 20) for slice_key in PLAY_TYPES[:-1]),
            _fact("shot_types", "Catch and Shoot", 0.5, 50),
            _fact("shot_types", "Pullups", 0.5, 50),
        ),
        season_metrics=tuple(("play_types", slice_key, "PTS", 10.0, 11.0) for slice_key in PLAY_TYPES)
        + tuple(
            metric
            for slice_key in ("catch_and_shoot", "pullups", "less_than_10_ft")
            for metric in (
                ("shot_types", slice_key, "FG2A", 6.0, 6.6),
                ("shot_types", slice_key, "FG3A", 4.0, 4.4),
            )
        ),
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["PTS"]["season"] == {
        "components": {"play_types": {"value": 0.1, "thin": False}},
        "blend": {"value": 0.1, "thin": False},
    }
    assert _computed_scores(player)["FGA"]["season"] == {
        "components": {},
        "blend": None,
    }


def test_play_type_coverage_below_the_floor_scores_but_is_thin():
    def score(shares):
        return _service(
            markets=("PTS",),
            facts=tuple(_fact("play_types", slice_key, share, 100) for slice_key, share in shares),
            season_metrics=(
                ("play_types", "Transition", "PTS", 10.0, 12.0),
                ("play_types", "Isolation", "PTS", 20.0, 18.0),
            ),
        ).get_matchup(game_id=GAME_ID)["players"][0]["scores"]["PTS"]["season"]

    # 0.85 observed is the floor, not thin; the unobserved residual is neutral.
    at_floor = score((("Transition", 0.25), ("Isolation", 0.6)))
    assert at_floor["components"]["play_types"]["thin"] is False
    assert at_floor["components"]["play_types"]["value"] == -0.01

    below = score((("Transition", 0.25), ("Isolation", 0.5)))
    assert below["components"]["play_types"] == {"value": 0.0, "thin": True}
    assert below["blend"] == {"value": 0.0, "thin": True}


def test_common_posted_market_union_has_decoder_safe_blends_for_every_offensive_row():
    zone_slices = (
        "Restricted Area",
        "In The Paint (Non-RA)",
        "Mid-Range",
        "Corner 3",
        "Above the Break 3",
    )
    type_slices = ("catch_and_shoot", "pullups", "less_than_10_ft")
    assist_slices = (
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    )
    metrics = [
        ("play_types", "Transition", "PTS", 10.0, 11.0),
        ("traditional", "OPP_REB", "OPP_REB", 10.0, 11.0),
        ("traditional", "OPP_TOV", "OPP_TOV", 10.0, 11.0),
        ("traditional", "OPP_STL", "OPP_STL", 10.0, 11.0),
        ("traditional", "OPP_BLK", "OPP_BLK", 10.0, 11.0),
    ]
    metrics.extend(
        ("shot_zones", slice_key, stat, 10.0, 11.0) for slice_key in zone_slices for stat in ("FGM", "FGA")
    )
    metrics.extend(
        ("shot_types", slice_key, stat, 10.0, 11.0)
        for slice_key in type_slices
        for stat in ("FG2M", "FG2A", "FG3M", "FG3A")
    )
    metrics.extend(("assist_locations", slice_key, slice_key, 10.0, 11.0) for slice_key in assist_slices)
    markets = (
        "PTS",
        "REB",
        "AST",
        "3PM",
        "FGA",
        "FG2A",
        "FG3A",
        "PRA",
        "PA",
        "PR",
        "RA",
        "TOV",
        "STL",
        "BLK",
        "STKS",
    )
    player = _service(
        markets=markets,
        facts=_complete_play_facts(
            _fact("play_types", "Transition", 1.0, 100),
            *(_fact("shot_zones", key, 0.2, 20) for key in zone_slices),
            _fact("shot_types", "Catch and Shoot", 0.4, 40),
            _fact("shot_types", "Pullups", 0.3, 30),
            _fact("shot_types", "Less Than 10 ft", 0.3, 30),
            *(_fact("assist_locations", key, 0.2, 8) for key in assist_slices),
        ),
        season_metrics=tuple(metrics),
        last_15_metrics=tuple(metrics),
        per_game={
            "PTS": 20.0,
            "REB": 10.0,
            "AST": 5.0,
            "TOV": 3.0,
            "STL": 2.0,
            "BLK": 1.0,
        },
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert tuple(_computed_scores(player)) == markets
    for market in markets:
        for window_name in ("season", "last_15"):
            window = _computed_scores(player)[market][window_name]
            if market in {"TOV", "STL", "BLK", "STKS"}:
                assert "blend" not in window
            else:
                assert window["components"]
                assert window["blend"] == {"value": 0.1, "thin": False}


@pytest.mark.parametrize(
    ("market", "missing_part", "expected"),
    (
        (
            "PRA",
            "AST",
            {
                "play_types": 0.114286,
                "traditional": 0.057143,
                "blend": 0.171429,
            },
        ),
        ("PA", "AST", {"play_types": 0.16, "blend": 0.16}),
        ("PR", "REB", {"play_types": 0.133333, "blend": 0.133333}),
        ("RA", "AST", {"traditional": 0.133333, "blend": 0.133333}),
    ),
)
def test_combo_uses_full_season_volume_when_a_positive_part_is_unavailable(market, missing_part, expected):
    assist_slices = (
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    )
    metrics = [
        ("play_types", "Transition", "PTS", 10.0, 12.0),
        ("traditional", "OPP_TOV", "OPP_TOV", 10.0, 10.0),
        ("traditional", "OPP_STL", "OPP_STL", 10.0, 10.0),
        ("traditional", "OPP_BLK", "OPP_BLK", 10.0, 10.0),
    ]
    if missing_part != "REB":
        metrics.append(("traditional", "OPP_REB", "OPP_REB", 10.0, 12.0))
    if missing_part != "AST":
        metrics.extend(("assist_locations", slice_key, slice_key, 5.0, 6.0) for slice_key in assist_slices)
    player = _service(
        markets=(market,),
        facts=_complete_play_facts(
            _fact("play_types", "Transition", 1.0, 100),
            *(_fact("assist_locations", slice_key, 0.2, 8) for slice_key in assist_slices),
        ),
        season_metrics=tuple(metrics),
        per_game={"PTS": 20.0, "REB": 10.0, "AST": 5.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)[market]["season"] == {
        "components": {
            base: {"value": value, "thin": True} for base, value in expected.items() if base != "blend"
        },
        "blend": {"value": expected["blend"], "thin": True},
    }


def test_public_matchup_indexes_each_window_once_and_memoizes_shared_primitives():
    trackers = []
    fact = _CountingDietFact(_fact("play_types", "Transition", 1.0, 100))
    player = _service(
        markets=("PTS", "PA", "PR", "PRA"),
        facts=_complete_play_facts(fact),
        season_metrics=(("play_types", "Transition", "PTS", 10.0, 12.0),),
        per_game={"PTS": 30.0, "REB": 10.0, "AST": 10.0},
        metric_trackers=trackers,
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert _computed_scores(player)["PTS"]["season"]["blend"]["value"] == 0.2
    assert all(tracker.iterations == 1 for tracker in trackers)
    # One player-row serialization read plus one computation for the available
    # Season scope; that computation reads once for completeness and weighting.
    # Provider-unsupported play-type Last-15 does no score work.
    assert fact.share_reads == 3


# --- Rebound Types (crf04/statsplus#234) ------------------------------------

REBOUND_SLICES = (
    "DefTwoPtRebounds",
    "DefThreePtRebounds",
    "FTDefRebounds",
    "OffTwoPtRebounds",
    "OffThreePtRebounds",
    "FTOffRebounds",
)
#: League average and what Boston's opponents collect per 48, by slice.  The
#: six add up to Boston's OPP_REB of 5.0 and to the league's 4.0.
REBOUND_LEAGUE = (1.5, 0.8, 0.8, 0.5, 0.4, 0.0)
REBOUND_BOSTON = (2.4, 0.9, 1.0, 0.4, 0.3, 0.0)


def _rebound_metrics():
    return tuple(
        ("rebound_types", slice_key, slice_key, league, boston)
        for slice_key, league, boston in zip(REBOUND_SLICES, REBOUND_LEAGUE, REBOUND_BOSTON)
    )


_TRADITIONAL_REB = (
    ("traditional", "OPP_REB", "OPP_REB", 4.0, 5.0),
    ("traditional", "OPP_TOV", "OPP_TOV", 10.0, 10.0),
    ("traditional", "OPP_STL", "OPP_STL", 10.0, 10.0),
    ("traditional", "OPP_BLK", "OPP_BLK", 10.0, 10.0),
)


def _rebound_diet(volumes, *, games=20):
    total = sum(volumes)
    return tuple(
        _fact("rebound_types", slice_key, volume / total, volume, games=games)
        for slice_key, volume in zip(REBOUND_SLICES, volumes)
    )


def _boston(payload):
    return next(team for team in payload["teams"] if team["tricode"] == "BOS")


def test_the_six_rebound_types_rows_are_listed_and_add_up_to_opp_reb():
    payload = _service(
        markets=("REB",),
        facts=(),
        season_metrics=(*_TRADITIONAL_REB, *_rebound_metrics()),
        per_game={"REB": 8.0},
    ).get_matchup(game_id=GAME_ID)

    rows = _boston(payload)["defense_sheet"]["rebound_types"]
    # Sorted by key, like every other base's rows.
    assert [row["key"] for row in rows] == [
        "DefThreePtRebounds",
        "DefTwoPtRebounds",
        "FTDefRebounds",
        "FTOffRebounds",
        "OffThreePtRebounds",
        "OffTwoPtRebounds",
    ]
    by_key = {row["key"]: row["season"] for row in rows}
    assert {key: season["allowed_per_48"] for key, season in by_key.items()} == {
        "DefTwoPtRebounds": 2.4,
        "DefThreePtRebounds": 0.9,
        "FTDefRebounds": 1.0,
        "OffTwoPtRebounds": 0.4,
        "OffThreePtRebounds": 0.3,
        "FTOffRebounds": 0.0,
    }
    assert {season["rank"] for season in by_key.values()} == {16}
    # Every row feeds the four markets with a rebound part, and only those.
    assert {tuple(row["markets"]) for row in rows} == {("REB", "PR", "RA", "PRA")}
    opp_reb = next(row for row in _boston(payload)["defense_sheet"]["traditional"] if row["key"] == "OPP_REB")
    assert opp_reb["season"]["allowed_per_48"] == 5.0
    assert sum(row["season"]["allowed_per_48"] for row in rows) == pytest.approx(5.0)
    assert {key: season["percent_vs_league_average"] for key, season in by_key.items()} == {
        "DefTwoPtRebounds": 60.0,
        "DefThreePtRebounds": 12.5,
        "FTDefRebounds": 25.0,
        "OffTwoPtRebounds": -20.0,
        "OffThreePtRebounds": -25.0,
        "FTOffRebounds": 0.0,
    }
    league = payload["league"]["defense_sheet"]["rebound_types"]
    assert {row["key"]: row["season"]["average_allowed_per_48"] for row in league} == {
        "DefTwoPtRebounds": 1.5,
        "DefThreePtRebounds": 0.8,
        "FTDefRebounds": 0.8,
        "OffTwoPtRebounds": 0.5,
        "OffThreePtRebounds": 0.4,
        "FTOffRebounds": 0.0,
    }
    assert payload["league"]["surface_availability"]["rebound_types"]["season"] == {
        "status": "available",
        "unavailable_reason": None,
    }


def test_the_rebound_diet_is_stated_in_rebounds_and_thin_below_two_a_game():
    # 40 rebounds over 20 games is exactly the 2.0 floor: not thin.
    at_floor = _service(
        markets=("REB",),
        facts=_rebound_diet((16, 8, 4, 6, 4, 2)),
        season_metrics=(*_TRADITIONAL_REB, *_rebound_metrics()),
        per_game={"REB": 8.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]
    # 39 over 20 games is 1.95 a game: thin, though every share is complete.
    below_floor = _service(
        markets=("REB",),
        facts=_rebound_diet((16, 8, 4, 6, 4, 1)),
        season_metrics=(*_TRADITIONAL_REB, *_rebound_metrics()),
        per_game={"REB": 8.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert at_floor["diet_thin"]["rebound_types"] is False
    assert below_floor["diet_thin"]["rebound_types"] is True
    rows = at_floor["diet_shares"]["rebound_types"]
    assert [row["key"] for row in rows] == list(REBOUND_SLICES)
    assert rows[0]["season"] == {
        "share": 0.4,
        "volume": 16.0,
        "games_played": 20,
        "volume_unit": "rebounds",
        "league_average_share": None,
        "sigma_deviation": None,
    }
    assert sum(row["season"]["share"] for row in rows) == pytest.approx(1.0)


def test_a_thin_rebound_diet_is_not_thin_for_a_player_with_too_few_games():
    player = _service(
        markets=("REB",),
        facts=_rebound_diet((16, 8, 4, 6, 4, 2), games=4),
        season_metrics=(*_TRADITIONAL_REB, *_rebound_metrics()),
        per_game={"REB": 8.0},
    ).get_matchup(game_id=GAME_ID)["players"][0]

    assert player["diet_thin"]["rebound_types"] is True


def test_the_rebound_score_and_its_combos_ignore_the_rebound_types_base():
    kwargs = dict(
        markets=("REB", "PR", "RA", "PRA"),
        per_game={"PTS": 20.0, "REB": 8.0, "AST": 5.0},
    )
    without = _service(facts=(), season_metrics=_TRADITIONAL_REB, **kwargs).get_matchup(game_id=GAME_ID)[
        "players"
    ][0]
    with_base = _service(
        facts=_rebound_diet((16, 8, 4, 6, 4, 2)),
        season_metrics=(*_TRADITIONAL_REB, *_rebound_metrics()),
        **kwargs,
    ).get_matchup(game_id=GAME_ID)["players"][0]

    # The full score JSON, `missing_inputs` included: changing the bases REB or
    # a combo requires would change what it names as missing.
    assert with_base["scores"] == without["scores"]
    assert _computed_scores(with_base) == _computed_scores(without)
    assert {
        market: {name: window["missing_inputs"] for name, window in windows.items()}
        for market, windows in with_base["scores"].items()
    } == {
        "REB": {"season": [], "last_15": []},
        "PR": {
            "season": ["team_defense:play_types", "team_defense:shot_zones", "team_defense:shot_types"],
            "last_15": ["team_defense:play_types", "team_defense:shot_zones", "team_defense:shot_types"],
        },
        "RA": {
            "season": ["team_defense:assist_locations"],
            "last_15": ["team_defense:assist_locations"],
        },
        "PRA": {
            "season": [
                "team_defense:play_types",
                "team_defense:shot_zones",
                "team_defense:shot_types",
                "team_defense:assist_locations",
            ],
            "last_15": [
                "team_defense:play_types",
                "team_defense:shot_zones",
                "team_defense:shot_types",
                "team_defense:assist_locations",
            ],
        },
    }
    assert _computed_scores(with_base)["REB"]["season"] == {
        "components": {"traditional": {"value": 0.25, "thin": False}},
        "blend": {"value": 0.25, "thin": False},
    }
    assert set(_computed_scores(with_base)["REB"]["season"]["components"]) == {"traditional"}


def test_a_missing_rebound_types_base_is_listed_and_leaves_traditional_alone():
    payload = _service(
        markets=("REB",),
        facts=(),
        season_metrics=_TRADITIONAL_REB,
        per_game={"REB": 8.0},
    ).get_matchup(game_id=GAME_ID)
    player = payload["players"][0]

    assert payload["league"]["surface_availability"]["rebound_types"]["season"]["status"] != "available"
    assert payload["league"]["defense_sheet"]["rebound_types"] == []
    assert _boston(payload)["defense_sheet"]["rebound_types"] == []
    assert player["diet_shares"]["rebound_types"] == []
    assert player["diet_thin"]["rebound_types"] is True
    assert _computed_scores(player)["REB"]["season"] == {
        "components": {"traditional": {"value": 0.25, "thin": False}},
        "blend": {"value": 0.25, "thin": False},
    }
