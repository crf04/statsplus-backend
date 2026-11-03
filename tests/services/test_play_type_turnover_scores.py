"""TOV, FTA and FTM Play types components (crf04/statsplus#416).

Every test composes a Matchup from raw evidence: the player's Synergy rows go
through the collector normalizer and the player Synergy aggregation into a
Diet Publication, and the opponent's play-type and Traditional per-48 rows are
raw team-window Publications read by the team matchup query.  Nothing is
handed to the Matchup pre-labelled.
"""

from datetime import UTC, datetime

from sqlalchemy import create_engine

from app.collector.normalizers import normalize_synergy_response
from app.domain.player_synergy import aggregate_player_synergy
from app.domain.team_matchup_taxonomy import PLAY_TYPES
from app.migrations import run_migrations
from app.services.database_first_activation import PublicationRead
from app.services.player_diet import PlayerDietRepository
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import TeamMatchupRepository
from tests.services.test_matchup_service import BOS, GAME_ID, LAL, RETRIEVED_AT, _service
from tests.support import publication_stubs

MARKETS = ("TOV", "FTA", "FTM")

# Per 48 allowed by the 29 other teams, and by BOS, per play type.  Every play
# type not named allows 0.5 of each for every team, so it is neutral.
_OTHERS = {
    "PRBallHandler": {"PTS": 9.0, "TOV": 2.0, "FTT": 1.0},
    "Isolation": {"PTS": 8.0, "TOV": 1.0, "FTT": 1.2},
    "Transition": {"PTS": 7.0, "TOV": 1.5, "FTT": 0.8},
    "Spotup": {"PTS": 6.0, "TOV": 0.5, "FTT": 0.4},
}
_BOSTON = {
    "PRBallHandler": {"PTS": 9.0, "TOV": 3.5, "FTT": 0.4},
    "Isolation": {"PTS": 8.0, "TOV": 0.7, "FTT": 2.4},
    "Transition": {"PTS": 7.0, "TOV": 3.0, "FTT": 0.8},
    "Spotup": {"PTS": 6.0, "TOV": 0.5, "FTT": 0.1},
}

# LeBron's Synergy rows: possession share, possessions, turnovers and
# free-throw trips per play type.  He never turns it over in transition and
# never gets to the line on spot-ups, and Synergy rated four play types only.
_FULL_DIET = {
    "PRBallHandler": (0.40, 200, 6, 3),
    "Isolation": (0.25, 125, 2, 4),
    "Transition": (0.10, 50, 0, 1),
    "Spotup": (0.15, 75, 2, 0),
}


def _play_type_per48(values, *, with_events=True):
    per48 = {}
    for slice_key in PLAY_TYPES:
        stats = values.get(slice_key, {"PTS": 0.5, "TOV": 0.5, "FTT": 0.5})
        per48[f"{slice_key}_PTS"] = stats["PTS"]
        per48[f"{slice_key}_POSS"] = 10.0
        if with_events:
            per48[f"{slice_key}_TOV"] = stats["TOV"]
            per48[f"{slice_key}_FTT"] = stats["FTT"]
    return per48


def _season_window(*, with_events=True):
    play_types = "synergy_play_types_opponent_season"
    traditional = "traditional_opponent_season"
    others = {"turnovers": 13.0, "free_throws_attempted": 20.0, "free_throws_made": 16.0}
    boston = {"turnovers": 15.0, "free_throws_attempted": 17.0, "free_throws_made": 13.0}
    reads = {
        play_types: publication_stubs.read(
            play_types,
            publication_stubs.league(
                lambda tricode: _play_type_per48(
                    _BOSTON if tricode == "BOS" else _OTHERS, with_events=with_events
                )
            ),
        ),
        traditional: publication_stubs.read(
            traditional,
            publication_stubs.league(
                lambda tricode: publication_stubs.traditional_per48(
                    **(boston if tricode == "BOS" else others)
                )
            ),
        ),
    }
    engine = create_engine("sqlite://")
    run_migrations(engine)
    return TeamMatchupQueryService(
        TeamMatchupRepository(engine),
        clock=lambda: publication_stubs.RETRIEVED_AT,
        publication_reader=publication_stubs.StubReader(reads),
        l15_expectation_resolver=publication_stubs.StubGovernance(),
    ).get_latest_window(publication_stubs.SEASON, as_of=publication_stubs.RETRIEVED_AT.date())


def _player_synergy_rows(diet, *, with_events=True):
    """Raw Synergy player rows through the collector's normalizer, per category."""

    records = []
    for slice_key, (share, possessions, turnovers, trips) in diet.items():
        row = {
            "PLAYER_ID": 2544,
            "TEAM_ID": LAL,
            "PLAY_TYPE": slice_key,
            "TYPE_GROUPING": "Offensive",
            "GP": 20,
            "POSS_PCT": share,
            "POSS": possessions,
        }
        if with_events:
            row["TOV_POSS_PCT"] = turnovers / possessions
            row["FT_POSS_PCT"] = trips / possessions
        observation = normalize_synergy_response(
            [row],
            season="2025-26",
            cutoff=datetime(2026, 1, 15, tzinfo=UTC),
            scope={"window": "season", "play_type": slice_key},
        )
        records.extend(observation.payload["records"])
    rows, withheld = aggregate_player_synergy(records)
    assert withheld == {}
    return rows


class _DietPublication:
    def __init__(self, rows):
        self.read_value = PublicationRead(
            stream_key="synergy_play_types",
            publication_id="diet-1",
            season=publication_stubs.SEASON,
            cutoff=RETRIEVED_AT.isoformat(),
            version=1,
            status="active",
            freshness="fresh",
            age_seconds=0,
            payload={"base": "play_types", "rows": rows},
            retrieved_at=RETRIEVED_AT,
        )

    def read_many(self, stream_keys, *, season=None):
        return {
            key: (
                self.read_value
                if key == "synergy_play_types"
                else publication_stubs.missing_read(key, season)
            )
            for key in stream_keys
        }


def _scores(diet=_FULL_DIET, *, player_events=True, team_events=True):
    engine = create_engine("sqlite://")
    run_migrations(engine)
    diets = PlayerDietRepository(
        engine,
        publication_reader=_DietPublication(_player_synergy_rows(diet, with_events=player_events)),
    )
    pool = PlayerPool(
        players=(PoolPlayer(2544, "LeBron James", LAL, MARKETS, {"prizepicks": MARKETS}),),
        team_counts={LAL: 1},
        freshness={"status": "fresh", "retrieved_at": RETRIEVED_AT.isoformat(), "providers": {}},
    )
    payload = _service(
        pool=pool, season_window=_season_window(with_events=team_events), diets=diets
    ).get_matchup(game_id=GAME_ID)
    return payload, payload["players"][0]["scores"]


def test_each_market_blends_traditional_with_his_own_play_type_mix():
    """Hand computation against BOS (league average = (29 x others + BOS) / 30).

    TOV: his 10 turnovers are 6 PRBallHandler, 2 Isolation, 2 Spotup, none in
    Transition, so 0.6 x 3.5/2.05 + 0.2 x 0.7/0.99 + 0.2 x 0.5/0.5 - 1, and
    Transition's 3.0/1.55 weighs nothing.  FTA and FTM: his 8 trips are 3
    PRBallHandler, 4 Isolation, 1 Transition, none on spot-ups, so
    0.375 x 0.4/0.98 + 0.5 x 2.4/1.24 + 0.125 x 0.8/0.8 - 1.  Traditional:
    15/13.0667 - 1, 17/19.9 - 1 and 13/15.9 - 1.  Each Blend is the mean of
    the two rounded components.
    """

    _payload, scores = _scores()

    assert {market: scores[market]["season"] for market in MARKETS} == {
        "TOV": {
            "components": {
                "play_types": {"value": 0.365804, "thin": False},
                "traditional": {"value": 0.147959, "thin": False},
            },
            "blend": {"value": 0.256881, "thin": False},
            "missing_inputs": [],
        },
        "FTA": {
            "components": {
                "play_types": {"value": 0.245803, "thin": False},
                "traditional": {"value": -0.145729, "thin": False},
            },
            "blend": {"value": 0.050037, "thin": False},
            "missing_inputs": [],
        },
        "FTM": {
            "components": {
                "play_types": {"value": 0.245803, "thin": False},
                "traditional": {"value": -0.18239, "thin": False},
            },
            "blend": {"value": 0.031706, "thin": False},
            "missing_inputs": [],
        },
    }


def test_play_type_coverage_below_the_floor_scores_but_marks_the_component_and_blend_thin():
    # Without Spotup his rated play types cover 0.75 of his possessions.
    diet = {slice_key: values for slice_key, values in _FULL_DIET.items() if slice_key != "Spotup"}

    _payload, scores = _scores(diet)

    # 8 turnovers: 0.75 x 3.5/2.05 + 0.25 x 0.7/0.99 - 1.
    assert scores["TOV"]["season"]["components"]["play_types"] == {"value": 0.457255, "thin": True}
    assert scores["TOV"]["season"]["blend"] == {"value": 0.302607, "thin": True}
    # The trips never touched spot-ups, so FTA is unchanged but thin.
    assert scores["FTA"]["season"]["components"]["play_types"] == {"value": 0.245803, "thin": True}
    assert scores["FTA"]["season"]["blend"] == {"value": 0.050037, "thin": True}


def test_a_team_publication_from_before_the_new_stats_leaves_the_blend_traditional_and_names_no_gap():
    payload, scores = _scores(team_events=False)

    for market, traditional in (("TOV", 0.147959), ("FTA", -0.145729), ("FTM", -0.18239)):
        assert scores[market]["season"] == {
            "components": {"traditional": {"value": traditional, "thin": False}},
            "blend": {"value": traditional, "thin": False},
            "missing_inputs": [],
        }
    assert payload["league"]["surface_availability"]["play_types"]["season"]["status"] == "available"
    boston = next(team for team in payload["teams"] if team["team_id"] == BOS)
    assert {row["key"] for row in boston["defense_sheet"]["play_types"]} == {
        f"{slice_key}:{stat_key}" for slice_key in PLAY_TYPES for stat_key in ("POSS", "PTS")
    }


def test_a_diet_publication_from_before_the_new_stats_leaves_the_blend_traditional_and_names_no_gap():
    _payload, scores = _scores(player_events=False)

    assert scores["TOV"]["season"] == {
        "components": {"traditional": {"value": 0.147959, "thin": False}},
        "blend": {"value": 0.147959, "thin": False},
        "missing_inputs": [],
    }
    assert scores["FTM"]["season"]["blend"] == {"value": -0.18239, "thin": False}


def test_the_new_sheet_rows_rank_and_average_like_the_points_rows_and_list_their_markets():
    payload, _scores_by_market = _scores()

    boston = next(team for team in payload["teams"] if team["team_id"] == BOS)
    rows = {row["key"]: row for row in boston["defense_sheet"]["play_types"]}
    league = {row["key"]: row for row in payload["league"]["defense_sheet"]["play_types"]}
    assert {key: rows[key]["markets"] for key in ("Isolation:PTS", "Isolation:TOV", "Isolation:FTT")} == {
        "Isolation:PTS": ["PTS", "PA", "PR", "PRA"],
        "Isolation:TOV": ["TOV"],
        "Isolation:FTT": ["FTA", "FTM"],
    }
    # BOS forces the most PRBallHandler turnovers and allows the fewest
    # PRBallHandler trips; rank 30 allows the most.
    assert rows["PRBallHandler:TOV"]["season"] == {
        "allowed_per_48": 3.5,
        "percent_vs_league_average": 70.731707,
        "sigma_deviation": 5.385165,
        "rank": 30,
    }
    assert rows["PRBallHandler:FTT"]["season"]["rank"] == 1
    assert rows["PRBallHandler:TOV"]["last_15"] is None
    assert league["PRBallHandler:TOV"]["season"] == {"average_allowed_per_48": 2.05, "sigma": 0.269258}
    assert league["Isolation:FTT"]["season"] == {"average_allowed_per_48": 1.24, "sigma": 0.215407}
