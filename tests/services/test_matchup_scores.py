"""Hand-computed Matchup Scores at the stored public service seam."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.models.catalogs import PLAY_TYPES
from app.services.matchup import DEFENSE_BASES, MatchupService
from app.services.player_diet import (
    PlayerDietResult,
    ShotTypeShooting,
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


def _computed_scores(player, *, keep_slices=False):
    """The computed score cells, with the additive input naming checked once.

    Every window carries `missing_inputs` naming the score-contract inputs it
    could not consume. These tests assert the arithmetic, so the additive key
    is verified here and removed from the compared shape; its exact values are
    asserted in `tests/services/test_matchup_service.py`. The shot-type
    `slices` are likewise dropped unless `keep_slices` is set: only the
    player-mix tests assert them.
    """

    windows = [window for market in player["scores"].values() for window in market.values()]
    assert windows
    assert all(isinstance(window["missing_inputs"], list) for window in windows)

    def window_shape(window):
        shape = {key: value for key, value in window.items() if key != "missing_inputs"}
        if not keep_slices:
            shape["components"] = {
                base: {key: value for key, value in cell.items() if key != "slices"}
                for base, cell in window["components"].items()
            }
        return shape

    return {
        market: {name: window_shape(window) for name, window in market_windows.items()}
        for market, market_windows in player["scores"].items()
    }


SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738
NOW = datetime(2026, 1, 15, 12, tzinfo=UTC)
RETRIEVED_AT = datetime(2026, 1, 15, 10, tzinfo=UTC)


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


def _fact(base, slice_key, share, volume, games=20, shooting=None):
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
        shooting=shooting,
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

    assert _computed_scores(player, keep_slices=True)["FGA"]["season"] == {
        "components": {
            "shot_types": {
                "value": 0.05,
                "thin": False,
                # No shooting split to read: each slice keeps the opponent-mix
                # ratio in the value and none is listed in `slices`.
            }
        },
        "blend": {"value": 0.05, "thin": False},
    }


def test_a_shot_type_points_row_in_the_window_changes_no_score():
    """The derived ``:PTS`` row (#315) is read-only context, never a score input.

    The same window is scored twice, once with the points rows the query layer
    derives and once without; the rows carry absurd values so any loop that
    picked them up by market or stat key would move a score.
    """

    base_rows = {
        "catch_and_shoot": (5.0, 5.0, 5.0, 5.0),
        "pullups": (4.0, 4.0, 4.0, 6.0),
        "less_than_10_ft": (10.0, 0.0, 12.0, 0.0),
    }
    without = []
    for slice_key, (league_two, league_three, opponent_two, opponent_three) in base_rows.items():
        without += [
            ("shot_types", slice_key, "FG2M", league_two, opponent_two),
            ("shot_types", slice_key, "FG3M", league_three, opponent_three),
            ("shot_types", slice_key, "FG2A", 10.0, 10.0),
            ("shot_types", slice_key, "FG3A", 8.0, 8.0),
        ]
    with_points = [
        *without,
        *(("shot_types", slice_key, "PTS", 1.0, 9999.0) for slice_key in base_rows),
    ]

    def scores(metrics):
        return _computed_scores(
            _service(
                markets=("PTS", "3PM", "FGA"),
                facts=(
                    _fact("shot_types", "Catch and Shoot", 0.5, 50),
                    _fact("shot_types", "Pullups", 0.25, 25),
                    _fact("shot_types", "Less Than 10 ft", 0.25, 25),
                ),
                season_metrics=tuple(metrics),
            ).get_matchup(game_id=GAME_ID)["players"][0]
        )

    before = scores(without)
    assert scores(with_points) == before
    # 0.5 * (25/25) + 0.25 * (26/20) + 0.25 * (24/20) - 1
    assert before["PTS"]["season"]["components"] == {"shot_types": {"value": 0.125, "thin": False}}


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


@pytest.mark.parametrize(("game_count", "stocks_thin"), ((20, False), (4, True)))
def test_defensive_scores_blend_is_their_one_traditional_component(game_count, stocks_thin):
    player = _service(
        markets=("TOV", "STL", "BLK", "STKS"),
        facts=(),
        season_metrics=(
            ("traditional", "OPP_REB", "OPP_REB", 10.0, 10.0),
            ("traditional", "OPP_TOV", "OPP_TOV", 10.0, 12.0),
            ("traditional", "OPP_STL", "OPP_STL", 5.0, 4.0),
            ("traditional", "OPP_BLK", "OPP_BLK", 2.0, 3.0),
        ),
        per_game={"TOV": 4.0, "STL": 2.0, "BLK": 1.0, "STKS": 3.0},
        game_count=game_count,
    ).get_matchup(game_id=GAME_ID)["players"][0]

    # STKS: (2 * -0.2 + 1 * 0.5) / 3, thin below the Season-rate game floor.
    expected = {
        "TOV": {"value": 0.2, "thin": False},
        "STL": {"value": -0.2, "thin": False},
        "BLK": {"value": 0.5, "thin": False},
        "STKS": {"value": 0.033333, "thin": stocks_thin},
    }
    assert {market: windows["season"] for market, windows in _computed_scores(player).items()} == {
        market: {"components": {"traditional": cell}, "blend": cell} for market, cell in expected.items()
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
        *(
            ("traditional", key, key, 10.0, 11.0)
            for key in (
                "OPP_REB",
                "OPP_TOV",
                "OPP_STL",
                "OPP_BLK",
                "OPP_PTS",
                "OPP_AST",
                "OPP_FGM",
                "OPP_FGA",
                "OPP_FG3M",
                "OPP_FG3A",
                "OPP_FTM",
                "OPP_FTA",
            )
        ),
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
        "FGM",
        "FG2M",
        "FGA",
        "FG2A",
        "FG3A",
        "FTM",
        "FTA",
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
        for slice_key, league, boston in zip(REBOUND_SLICES, REBOUND_LEAGUE, REBOUND_BOSTON, strict=False)
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
        for slice_key, volume in zip(REBOUND_SLICES, volumes, strict=False)
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
        "season": "2025-26",
        "window_games": None,
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


def _rebound_player(volumes, *, markets=("REB",), metrics=None, **kwargs):
    facts = _rebound_diet(volumes) if volumes else ()
    return _service(
        markets=markets,
        facts=facts,
        season_metrics=(*_TRADITIONAL_REB, *_rebound_metrics()) if metrics is None else metrics,
        per_game={"PTS": 20.0, "REB": 8.0, "AST": 5.0},
        **kwargs,
    ).get_matchup(game_id=GAME_ID)["players"][0]


# Boston allows 5.0 OPP_REB against a league 4.0: traditional is 5/4 - 1 = 0.25.
# Its slice ratios (Boston / league) are DefTwoPt 1.6, DefThreePt 1.125,
# FTDef 1.25, OffTwoPt 0.8, OffThreePt 0.75, and FTOff 0/0, which is skipped
# along with its share.  A diet of 16/8/4/6/4/2 rebounds (shares .4 .2 .1 .15
# .1 .05) scores 1.185 - 0.95 = 0.235; a three-point-heavy 4/24/4/2/4/2 (shares
# .1 .6 .1 .05 .1 .05) scores 1.075 - 0.95 = 0.125.
_BALANCED = (16, 8, 4, 6, 4, 2)
_THREE_POINT_HEAVY = (4, 24, 4, 2, 4, 2)


def test_two_diets_against_one_opponent_get_the_mean_of_their_two_components():
    balanced = _computed_scores(_rebound_player(_BALANCED))["REB"]["season"]
    heavy = _computed_scores(_rebound_player(_THREE_POINT_HEAVY))["REB"]["season"]

    assert balanced == {
        "components": {
            "traditional": {"value": 0.25, "thin": False},
            "rebound_types": {"value": 0.235, "thin": False},
        },
        "blend": {"value": 0.2425, "thin": False},
    }
    assert heavy == {
        "components": {
            "traditional": {"value": 0.25, "thin": False},
            "rebound_types": {"value": 0.125, "thin": False},
        },
        "blend": {"value": 0.1875, "thin": False},
    }


def test_last_15_blends_its_own_rebound_window_not_the_seasons():
    # Last 15: OPP_REB 6/4 - 1 = 0.5, and Boston's DefTwoPt slice is 3.0 against
    # the league 1.5 (ratio 2.0), so the balanced diet scores
    # 0.8 + 0.225 + 0.125 + 0.12 + 0.075 - 0.95 = 0.395.
    last_15 = (
        ("traditional", "OPP_REB", "OPP_REB", 4.0, 6.0),
        *_TRADITIONAL_REB[1:],
        *(
            ("rebound_types", slice_key, slice_key, league, boston)
            for slice_key, league, boston in zip(
                REBOUND_SLICES, REBOUND_LEAGUE, (3.0, 0.9, 1.0, 0.4, 0.3, 0.0), strict=False
            )
        ),
    )
    player = _rebound_player(_BALANCED, last_15_metrics=last_15)
    scores = _computed_scores(player)["REB"]

    assert scores["season"]["blend"] == {"value": 0.2425, "thin": False}
    assert scores["last_15"] == {
        "components": {
            "traditional": {"value": 0.5, "thin": False},
            "rebound_types": {"value": 0.395, "thin": False},
        },
        "blend": {"value": 0.4475, "thin": False},
    }


def test_a_thin_rebound_diet_makes_the_rebound_blend_thin():
    thin = _computed_scores(_rebound_player((16, 8, 4, 6, 4, 1)))["REB"]["season"]

    assert thin["components"]["traditional"]["thin"] is False
    assert thin["components"]["rebound_types"]["thin"] is True
    assert thin["blend"]["thin"] is True


def test_a_player_without_a_rebound_diet_scores_traditional_alone():
    player = _rebound_player(None)

    assert _computed_scores(player)["REB"]["season"] == {
        "components": {"traditional": {"value": 0.25, "thin": False}},
        "blend": {"value": 0.25, "thin": False},
    }
    # The Base is optional: nothing is named missing, as before it existed.
    assert player["scores"]["REB"]["season"]["missing_inputs"] == []


def test_an_unavailable_rebound_types_base_names_itself_and_leaves_traditional_alone():
    player = _rebound_player(_BALANCED, metrics=_TRADITIONAL_REB)

    assert _computed_scores(player)["REB"]["season"] == {
        "components": {"traditional": {"value": 0.25, "thin": False}},
        "blend": {"value": 0.25, "thin": False},
    }
    assert player["scores"]["REB"]["season"]["missing_inputs"] == []


def test_the_rebound_blend_reaches_pr_ra_and_pra_through_their_rebound_part():
    player = _rebound_player(_BALANCED, markets=("REB", "PR", "RA", "PRA"))
    scores = _computed_scores(player)

    assert scores["REB"]["season"]["blend"] == {"value": 0.2425, "thin": False}
    # Each combo weighs the REB part by 8 rebounds a game over all the positive
    # per-game volumes (PR 28, RA 13, PRA 33); the PTS and AST parts have no
    # stored defense here, OPP_PTS and OPP_AST included, so every combo cell
    # is a thin partial that names the Traditional gap.
    assert scores["PR"]["season"] == {
        "components": {
            "traditional": {"value": 0.071429, "thin": True},
            "rebound_types": {"value": 0.067143, "thin": True},
        },
        "blend": {"value": 0.069286, "thin": True},
    }
    assert scores["RA"]["season"] == {
        "components": {
            "traditional": {"value": 0.153846, "thin": True},
            "rebound_types": {"value": 0.144615, "thin": True},
        },
        "blend": {"value": 0.149231, "thin": True},
    }
    assert scores["PRA"]["season"] == {
        "components": {
            "traditional": {"value": 0.060606, "thin": True},
            "rebound_types": {"value": 0.05697, "thin": True},
        },
        "blend": {"value": 0.058788, "thin": True},
    }
    assert {market: windows["season"]["missing_inputs"] for market, windows in player["scores"].items()} == {
        "REB": [],
        "PR": [
            "team_defense:play_types",
            "team_defense:shot_zones",
            "team_defense:shot_types",
            "team_defense:traditional",
        ],
        "RA": ["team_defense:assist_locations", "team_defense:traditional"],
        "PRA": [
            "team_defense:play_types",
            "team_defense:shot_zones",
            "team_defense:shot_types",
            "team_defense:assist_locations",
            "team_defense:traditional",
        ],
    }


def test_combos_fall_back_to_traditional_for_a_player_without_a_rebound_diet():
    scores = _computed_scores(_rebound_player(None, markets=("PR",)))

    assert scores["PR"]["season"] == {
        "components": {"traditional": {"value": 0.071429, "thin": True}},
        "blend": {"value": 0.071429, "thin": True},
    }


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


# --- Shot-type scores weigh twos and threes by the player's own mix (#578) ----
#
# League / opponent allowed per 48 for each slice; Less Than 10 ft has no threes.
_MIX_METRICS = (
    ("shot_types", "catch_and_shoot", "FG2M", 5.0, 5.0),
    ("shot_types", "catch_and_shoot", "FG3M", 4.0, 5.0),
    ("shot_types", "catch_and_shoot", "FG2A", 10.0, 11.0),
    ("shot_types", "catch_and_shoot", "FG3A", 8.0, 10.0),
    ("shot_types", "pullups", "FG2M", 4.0, 4.4),
    ("shot_types", "pullups", "FG3M", 2.0, 3.0),
    ("shot_types", "pullups", "FG2A", 9.0, 9.0),
    ("shot_types", "pullups", "FG3A", 5.0, 6.0),
    ("shot_types", "less_than_10_ft", "FG2M", 6.0, 6.6),
    ("shot_types", "less_than_10_ft", "FG3M", 0.0, 0.0),
    ("shot_types", "less_than_10_ft", "FG2A", 12.0, 12.0),
    ("shot_types", "less_than_10_ft", "FG3A", 0.0, 0.0),
)


def _split(two_makes, three_makes, two_attempts, three_attempts):
    return ShotTypeShooting(
        makes=two_makes + three_makes,
        two_point_makes=two_makes,
        two_point_attempts=two_attempts,
        two_point_share=0.0,
        three_point_makes=three_makes,
        three_point_attempts=three_attempts,
        three_point_share=0.0,
    )


# Catch and Shoot: 30 vs 30 points made -> half twos.  Pull-ups: 4 vs 24 points
# made -> 1/7 twos, 6/7 threes.  Less Than 10 ft: all twos.
_MIX_SHOOTING = {
    "Catch and Shoot": _split(15, 10, 30, 10),
    "Pullups": _split(2, 8, 6, 24),
    "Less Than 10 ft": _split(20, 0, 40, 0),
}


def _mix_facts(shooting=None):
    shooting = _MIX_SHOOTING if shooting is None else shooting
    return (
        _fact("shot_types", "Catch and Shoot", 0.5, 50, shooting=shooting.get("Catch and Shoot")),
        _fact("shot_types", "Pullups", 0.3, 30, shooting=shooting.get("Pullups")),
        _fact("shot_types", "Less Than 10 ft", 0.2, 20, shooting=shooting.get("Less Than 10 ft")),
    )


def _mix_scores(markets, *, shooting=None, extra_facts=(), extra_metrics=(), **kwargs):
    player = _service(
        markets=markets,
        facts=(*extra_facts, *_mix_facts(shooting)),
        season_metrics=(*extra_metrics, *_MIX_METRICS),
        **kwargs,
    ).get_matchup(game_id=GAME_ID)["players"][0]
    return _computed_scores(player, keep_slices=True)


def test_points_shot_type_score_weighs_twos_and_threes_by_his_own_made_mix():
    zone_slices = ("Restricted Area", "In The Paint (Non-RA)", "Mid-Range", "Corner 3", "Above the Break 3")
    scores = _mix_scores(
        ("PTS",),
        extra_facts=(
            *_complete_play_facts(_fact("play_types", "Transition", 1.0, 100)),
            *(_fact("shot_zones", slice_key, 0.2, 20) for slice_key in zone_slices),
        ),
        extra_metrics=(
            ("play_types", "Transition", "PTS", 10.0, 12.0),
            *(
                ("shot_zones", slice_key, "FGM", 10.0, 15.0 if slice_key == "Restricted Area" else 10.0)
                for slice_key in zone_slices
            ),
        ),
    )

    # Catch and Shoot: 1/2 * (5/5) + 1/2 * (5/4) = 1.125
    # Pullups:         1/7 * (4.4/4) + 6/7 * (3/2) = 10.1/7 (the opponent-mix ratio was 17.8/14)
    # Less Than 10 ft: league has no threes -> the twos ratio, 6.6/6 = 1.1
    # 0.5 * 0.125 + 0.3 * (10.1/7 - 1) + 0.2 * 0.1 = 0.215357
    assert scores["PTS"]["season"] == {
        "components": {
            "play_types": {"value": 0.2, "thin": False},
            "shot_zones": {"value": 0.1, "thin": False},
            "shot_types": {
                "value": 0.215357,
                "thin": False,
                "slices": [
                    {"key": "Catch and Shoot", "percent_vs_league_average": 12.5},
                    {"key": "Pullups", "percent_vs_league_average": 44.285714},
                    {"key": "Less Than 10 ft", "percent_vs_league_average": 10.0},
                ],
            },
        },
        # (0.2 + 0.1 + 0.215357) / 3
        "blend": {"value": 0.171786, "thin": False},
    }
    assert (
        scores["PTS"]["last_15"]["components"]["shot_types"]
        == scores["PTS"]["season"]["components"]["shot_types"]
    )


def test_a_slice_with_no_makes_falls_back_to_his_attempt_mix_and_no_split_to_the_opponent_mix():
    no_makes = {**_MIX_SHOOTING, "Pullups": _split(0, 0, 10, 10)}
    # 2 * 10 vs 3 * 10 attempts -> 0.4 twos: 0.4 * 1.1 + 0.6 * 1.5 = 1.34
    # 0.5 * 0.125 + 0.3 * 0.34 + 0.2 * 0.1 = 0.1845
    assert _mix_scores(("PTS",), shooting=no_makes)["PTS"]["season"]["components"]["shot_types"] == {
        "value": 0.1845,
        "thin": False,
        "slices": [
            {"key": "Catch and Shoot", "percent_vs_league_average": 12.5},
            {"key": "Pullups", "percent_vs_league_average": 34.0},
            {"key": "Less Than 10 ft", "percent_vs_league_average": 10.0},
        ],
    }

    no_split = {key: value for key, value in _MIX_SHOOTING.items() if key != "Pullups"}
    # Pull-ups keep (2 * 4.4 + 3 * 3) / (2 * 4 + 3 * 2) = 17.8/14
    assert _mix_scores(("PTS",), shooting=no_split)["PTS"]["season"]["components"]["shot_types"] == {
        "value": 0.163929,
        "thin": False,
        "slices": [
            # Pull-ups were scored without his split, so they are not "for his mix".
            {"key": "Catch and Shoot", "percent_vs_league_average": 12.5},
            {"key": "Less Than 10 ft", "percent_vs_league_average": 10.0},
        ],
    }


def test_a_slice_with_neither_makes_nor_attempts_is_scored_by_the_opponent_mix_and_not_listed():
    empty = {**_MIX_SHOOTING, "Pullups": _split(0, 0, 0, 0)}
    assert _mix_scores(("PTS",), shooting=empty)["PTS"]["season"]["components"]["shot_types"] == {
        "value": 0.163929,
        "thin": False,
        "slices": [
            {"key": "Catch and Shoot", "percent_vs_league_average": 12.5},
            {"key": "Less Than 10 ft", "percent_vs_league_average": 10.0},
        ],
    }


def test_less_than_10_ft_without_league_threes_scores_the_twos_ratio_whatever_his_mix():
    # A (mis)reported three-point make in the slice must not move it: the league allows none.
    odd = {**_MIX_SHOOTING, "Less Than 10 ft": _split(10, 10, 20, 20)}
    cell = _mix_scores(("PTS",), shooting=odd)["PTS"]["season"]["components"]["shot_types"]
    assert cell["slices"][2] == {"key": "Less Than 10 ft", "percent_vs_league_average": 10.0}
    assert cell["value"] == 0.215357


def test_fga_shot_type_score_weighs_twos_and_threes_by_his_attempt_shares():
    # Catch and Shoot: 30 of 40 attempts are twos -> 0.75 * (11/10) + 0.25 * (10/8) = 1.1375
    # Pullups:         6 of 30 -> 0.2 * (9/9) + 0.8 * (6/5) = 1.16
    # Less Than 10 ft: no league threes -> 12/12 = 1.0
    # 0.5 * 0.1375 + 0.3 * 0.16 + 0.2 * 0 = 0.11675
    assert _mix_scores(("FGA",))["FGA"] == {
        window: {
            "components": {
                "shot_types": {
                    "value": 0.11675,
                    "thin": False,
                    "slices": [
                        {"key": "Catch and Shoot", "percent_vs_league_average": 13.75},
                        {"key": "Pullups", "percent_vs_league_average": 16.0},
                        {"key": "Less Than 10 ft", "percent_vs_league_average": 0.0},
                    ],
                }
            },
            "blend": {"value": 0.11675, "thin": False},
        }
        for window in ("season", "last_15")
    }


def test_single_stat_shot_type_scores_weigh_each_slice_by_his_own_count_of_that_stat():
    scores = _mix_scores(("3PM", "FG2M", "FG2A", "FG3A"))
    # 3PM:  his makes 10 / 8 / 0 -> (10 * (5/4 - 1) + 8 * (3/2 - 1)) / 18 = 0.361111
    # FG2M: his makes 15 / 2 / 20 -> (15 * 0 + 2 * 0.1 + 20 * 0.1) / 37 = 0.059459
    # FG2A: his attempts 30 / 6 / 40 -> (30 * 0.1 + 6 * 0 + 40 * 0) / 76 = 0.039474
    # FG3A: his attempts 10 / 24 / 0 -> (10 * 0.25 + 24 * 0.2) / 34 = 0.214706
    # (Weighed by his share of all attempts, 0.5 / 0.3 / 0.2, these were 0.275,
    # FG2M 0.05, 0.05 and 0.185.)  No single-stat cell lists slices.
    assert scores == {
        market: {
            window: {
                "components": {"shot_types": {"value": value, "thin": False}},
                "blend": {"value": value, "thin": False},
            }
            for window in ("season", "last_15")
        }
        for market, value in {"3PM": 0.361111, "FG2M": 0.059459, "FG2A": 0.039474, "FG3A": 0.214706}.items()
    }


def test_a_three_point_row_where_he_takes_no_threes_does_not_move_his_three_point_scores():
    # The league allows a sliver of threes inside 10 ft and this opponent none:
    # -100% on that row.  He takes 40% of all his shots there but no threes, so
    # neither three-point score reads it (statsplus#436, Bane vs PHX 2026-11-05).
    sliver = {("less_than_10_ft", "FG3M"): (0.1, 0.0), ("less_than_10_ft", "FG3A"): (0.2, 0.0)}
    metrics = tuple(
        (base, slice_key, stat, *sliver.get((slice_key, stat), (league, team)))
        for base, slice_key, stat, league, team in _MIX_METRICS
    )
    shooting = {
        "Catch and Shoot": _split(0, 6, 0, 15),
        "Pullups": _split(1, 4, 5, 10),
        "Less Than 10 ft": _split(20, 0, 40, 0),
    }
    player = _service(
        markets=("3PM", "FG3A"),
        facts=(
            _fact("shot_types", "Catch and Shoot", 0.3, 30, shooting=shooting["Catch and Shoot"]),
            _fact("shot_types", "Pullups", 0.3, 30, shooting=shooting["Pullups"]),
            _fact("shot_types", "Less Than 10 ft", 0.4, 40, shooting=shooting["Less Than 10 ft"]),
        ),
        season_metrics=metrics,
    ).get_matchup(game_id=GAME_ID)["players"][0]
    scores = _computed_scores(player)

    # FG3A: (15 * 0.25 + 10 * 0.2) / 25 = 0.23 (by all attempts: 0.3 * 0.25 + 0.3 * 0.2 - 0.4 = -0.265)
    # 3PM:  (6 * 0.25 + 4 * 0.5) / 10 = 0.35
    assert scores["FG3A"]["season"]["components"]["shot_types"] == {"value": 0.23, "thin": False}
    assert scores["3PM"]["season"]["components"]["shot_types"] == {"value": 0.35, "thin": False}


def test_single_stat_shot_type_weights_fall_back_to_attempts_then_to_his_overall_shares():
    no_threes_made = {
        "Catch and Shoot": _split(15, 0, 30, 10),
        "Pullups": _split(2, 0, 6, 30),
        "Less Than 10 ft": _split(20, 0, 40, 0),
    }
    # No three-point makes anywhere -> his three-point attempts 10 / 30 / 0:
    # (10 * 0.25 + 30 * 0.5) / 40 = 0.4375
    assert _mix_scores(("3PM",), shooting=no_threes_made)["3PM"]["season"]["components"]["shot_types"] == {
        "value": 0.4375,
        "thin": False,
    }

    # One shot type without a split -> his share of all attempts for every slice:
    # 0.5 * 0.25 + 0.3 * 0.2 = 0.185
    no_split = {key: value for key, value in _MIX_SHOOTING.items() if key != "Pullups"}
    assert _mix_scores(("FG3A",), shooting=no_split)["FG3A"]["season"]["components"]["shot_types"] == {
        "value": 0.185,
        "thin": False,
    }

    # No threes at all -> his share of all attempts: 0.5 * 0.25 + 0.3 * 0.2 = 0.185
    no_threes = {
        key: _split(split.two_point_makes, 0, split.two_point_attempts, 0)
        for key, split in _MIX_SHOOTING.items()
    }
    assert _mix_scores(("FG3A",), shooting=no_threes)["FG3A"]["season"]["components"]["shot_types"] == {
        "value": 0.185,
        "thin": False,
    }


def test_slices_ride_only_the_shot_types_cell_of_points_and_fga_in_both_windows():
    scores = _mix_scores(("PTS", "FGA", "3PM", "FG2A", "FG3A", "PA"), per_game={"PTS": 20.0, "AST": 5.0})

    carrying = {
        (market, window, base)
        for market, windows in scores.items()
        for window, score in windows.items()
        for base, cell in score["components"].items()
        if "slices" in cell
    }
    assert carrying == {
        (market, window, "shot_types") for market in ("PTS", "FGA") for window in ("season", "last_15")
    }
    assert "slices" not in scores["PA"]["season"]["components"]["shot_types"]
    assert all("slices" not in score["blend"] for windows in scores.values() for score in windows.values())


# --- Traditional rows join every Blend; FGM, FG2M, FTA, FTM score (#407) -----
#
# One opponent, BOS. League / BOS allowed per 48, and the Traditional score
# each row gives: BOS / league - 1.
_TRADITIONAL_METRICS = (
    ("traditional", "OPP_REB", "OPP_REB", 40.0, 44.0),  # 0.1
    ("traditional", "OPP_TOV", "OPP_TOV", 10.0, 12.0),  # 0.2
    ("traditional", "OPP_STL", "OPP_STL", 5.0, 4.0),  # -0.2
    ("traditional", "OPP_BLK", "OPP_BLK", 2.0, 3.0),  # 0.5
    ("traditional", "OPP_PTS", "OPP_PTS", 110.0, 121.0),  # 0.1
    ("traditional", "OPP_AST", "OPP_AST", 25.0, 20.0),  # -0.2
    ("traditional", "OPP_FGM", "OPP_FGM", 40.0, 42.0),  # 0.05
    ("traditional", "OPP_FGA", "OPP_FGA", 80.0, 88.0),  # 0.1
    ("traditional", "OPP_FG3M", "OPP_FG3M", 12.0, 15.0),  # 0.25
    ("traditional", "OPP_FG3A", "OPP_FG3A", 30.0, 33.0),  # 0.1
    ("traditional", "OPP_FTM", "OPP_FTM", 16.0, 20.0),  # 0.25
    ("traditional", "OPP_FTA", "OPP_FTA", 20.0, 18.0),  # -0.1
)
_ZONE_SLICES = ("Restricted Area", "In The Paint (Non-RA)", "Mid-Range", "Corner 3", "Above the Break 3")
_ASSIST_SLICES = (
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)


def _coverage_player(markets, *, per_game=None):
    """Every Base for one player against BOS, with the shot-type mix of #578."""

    return _service(
        markets=markets,
        facts=(
            *_complete_play_facts(_fact("play_types", "Transition", 1.0, 100)),
            *(_fact("shot_zones", slice_key, 0.2, 20) for slice_key in _ZONE_SLICES),
            *_mix_facts(),
            *(_fact("assist_locations", slice_key, 0.2, 8) for slice_key in _ASSIST_SLICES),
        ),
        season_metrics=(
            ("play_types", "Transition", "PTS", 10.0, 12.0),
            # Restricted Area makes 1.3x the league; every other zone 1x.
            *(
                (
                    "shot_zones",
                    slice_key,
                    stat,
                    10.0,
                    13.0 if (slice_key, stat) == ("Restricted Area", "FGM") else 10.0,
                )
                for slice_key in _ZONE_SLICES
                for stat in ("FGM", "FGA")
            ),
            *_MIX_METRICS,
            # At-rim assists 1.5x the league; every other location 1x.
            *(
                ("assist_locations", slice_key, slice_key, 5.0, 7.5 if slice_key == "AtRimAssists" else 5.0)
                for slice_key in _ASSIST_SLICES
            ),
            *_TRADITIONAL_METRICS,
        ),
        per_game=per_game,
    ).get_matchup(game_id=GAME_ID)["players"][0]


def test_each_market_blends_its_traditional_row_with_its_diet_components_as_a_plain_mean():
    player = _coverage_player(("PTS", "AST", "3PM", "FGA", "FG3A", "FGM", "FG2M", "FTA", "FTM"))

    def cells(**values):
        return {base: {"value": value, "thin": False} for base, value in values.items()}

    # Shot zones: makes over every zone 0.2 * 1.3 + 0.8 - 1 = 0.06; over the
    # three 2PT zones (equal volume) (1.3 + 1 + 1) / 3 - 1 = 0.1; threes and
    # attempts read the league.  Shot types: points, FGA, 3PM, FG2M and FG3A as
    # pinned by the #578 and #436 tests above; FGM
    # weighs each slice's twos and threes by his made counts (not points):
    # Catch and Shoot 15:10 -> 0.6 * 1 + 0.4 * 1.25 = 1.1, Pullups 2:8 ->
    # 0.2 * 1.1 + 0.8 * 1.5 = 1.42, Less Than 10 ft the twos ratio 1.1, so
    # 0.5 * 0.1 + 0.3 * 0.42 + 0.2 * 0.1 = 0.196.
    assert {market: windows["season"] for market, windows in _computed_scores(player).items()} == {
        "PTS": {
            "components": cells(play_types=0.2, shot_zones=0.06, shot_types=0.215357, traditional=0.1),
            # (0.2 + 0.06 + 0.215357 + 0.1) / 4
            "blend": {"value": 0.143839, "thin": False},
        },
        "AST": {
            "components": cells(assist_locations=0.1, traditional=-0.2),
            "blend": {"value": -0.05, "thin": False},
        },
        "3PM": {
            "components": cells(shot_zones=0.0, shot_types=0.361111, traditional=0.25),
            "blend": {"value": 0.203704, "thin": False},
        },
        "FGA": {
            "components": cells(shot_zones=0.0, shot_types=0.11675, traditional=0.1),
            "blend": {"value": 0.07225, "thin": False},
        },
        "FG3A": {
            "components": cells(shot_zones=0.0, shot_types=0.214706, traditional=0.1),
            "blend": {"value": 0.104902, "thin": False},
        },
        "FGM": {
            "components": cells(shot_zones=0.06, shot_types=0.196, traditional=0.05),
            "blend": {"value": 0.102, "thin": False},
        },
        "FG2M": {
            "components": cells(shot_zones=0.1, shot_types=0.059459),
            "blend": {"value": 0.07973, "thin": False},
        },
        "FTA": {"components": cells(traditional=-0.1), "blend": {"value": -0.1, "thin": False}},
        "FTM": {"components": cells(traditional=0.25), "blend": {"value": 0.25, "thin": False}},
    }
    assert all(windows["season"]["missing_inputs"] == [] for windows in player["scores"].values())


def test_fgm_shot_types_list_each_slice_read_for_his_made_count_mix():
    player = _coverage_player(("FGM",))

    assert _computed_scores(player, keep_slices=True)["FGM"]["season"]["components"]["shot_types"] == {
        "value": 0.196,
        "thin": False,
        "slices": [
            {"key": "Catch and Shoot", "percent_vs_league_average": 10.0},
            {"key": "Pullups", "percent_vs_league_average": 42.0},
            {"key": "Less Than 10 ft", "percent_vs_league_average": 10.0},
        ],
    }


def test_combos_build_their_traditional_cell_from_their_parts_rows():
    player = _coverage_player(("PA", "PRA"), per_game={"PTS": 30.0, "REB": 10.0, "AST": 10.0})

    scores = _computed_scores(player)
    # PA: (30 * OPP_PTS 0.1 + 10 * OPP_AST -0.2) / 40; its Blend weighs the
    # PTS Blend 0.143839 and the AST Blend -0.05 the same way.
    assert scores["PA"]["season"]["components"]["traditional"] == {"value": 0.025, "thin": False}
    assert scores["PA"]["season"]["blend"] == {"value": 0.095379, "thin": False}
    # PRA adds REB's OPP_REB 0.1: (3 + 1 - 2) / 50, and Blend
    # (30 * 0.143839 + 10 * 0.1 + 10 * -0.05) / 50.
    assert scores["PRA"]["season"]["components"]["traditional"] == {"value": 0.04, "thin": False}
    assert scores["PRA"]["season"]["blend"] == {"value": 0.096303, "thin": False}
