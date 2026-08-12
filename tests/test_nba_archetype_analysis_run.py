"""
Behavioral tests for the archetype matchup Analysis Run builder (issue 70).

The tests drive the complete current matchup-analysis path from a compact
deterministic synthetic fixture: repeated players, unequal archetype sizes,
and teammates sharing games. They assert observable artifacts and arithmetic
instead of source text or dataframe implementations.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1] / "analysis" / "nba-archetypes" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

from matchup_analysis import AnalysisRunBuilder  # noqa: E402

BASE_DATE = date(2026, 1, 5)
TEAMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
GAMES_PER_TEAM = 18
EXPECTED_METRICS = ["FGA", "FG2A", "FG3A", "FTA"]

PLAYERS = [
    ("P001", "Ace One", "AAA", 100, "Rim Pressure", "Finisher"),
    ("P002", "Bee Two", "AAA", 200, "Tertiary Shot Creator", "Creator"),
    ("P003", "Ace Three", "AAA", 100, "Rim Pressure", "Finisher"),
    ("P004", "Four Diamond", "BBB", 200, "Tertiary Shot Creator", "Creator"),
    ("P005", "Five Ember", "BBB", 100, "Rim Pressure", "Finisher"),
    ("P006", "Six Field", "BBB", 300, "Quarterback", "Facilitator"),
    ("P007", "Seven Green", "CCC", 100, "Rim Pressure", "Finisher"),
    ("P008", "Eight Harbor", "CCC", 300, "Quarterback", "Facilitator"),
    ("P009", "Nine Ivory", "CCC", 200, "Tertiary Shot Creator", "Creator"),
    ("P010", "Ten Knot", "DDD", 100, "Rim Pressure", "Finisher"),
    ("P011", "Eleven Lagoon", "DDD", 100, "Rim Pressure", "Finisher"),
    ("P012", "Twelve Mesa", "DDD", 200, "Tertiary Shot Creator", "Creator"),
    ("P013", "Thirteen Night", "EEE", 200, "Tertiary Shot Creator", "Creator"),
    ("P014", "Fourteen Orbit", "EEE", 100, "Rim Pressure", "Finisher"),
    ("P015", "Fifteen Pinnacle", "EEE", 300, "Quarterback", "Facilitator"),
    ("P016", "Sixteen Quarry", "FFF", 200, "Tertiary Shot Creator", "Creator"),
    ("P017", "Seventeen Ridge", "FFF", 300, "Quarterback", "Facilitator"),
    ("P018", "Eighteen Summit", "FFF", 100, "Rim Pressure", "Finisher"),
]


def synthetic_membership():
    return pd.DataFrame(
        [
            {
                "PLAYER_ID": player_id,
                "PLAYER_NAME": name,
                "ARCHETYPE": archetype,
                "SUBTYPE_ID": subtype_id,
                "SUBTYPE_ARCHETYPE": subtype_name,
            }
            for player_id, name, _team, subtype_id, subtype_name, archetype in PLAYERS
        ]
    )


def synthetic_game_logs():
    rng = np.random.default_rng(20250812)
    team_players = {}
    for player_id, _name, team, _s, _sn, _a in PLAYERS:
        team_players.setdefault(team, []).append(player_id)
    other_teams = {team: [t for t in TEAMS if t != team] for team in TEAMS}
    rows = []
    for team_index, team in enumerate(TEAMS):
        for game_index in range(GAMES_PER_TEAM):
            opponent = other_teams[team][(team_index + game_index) % 5]
            location = "vs." if game_index % 2 == 0 else "@"
            game_date = (BASE_DATE + timedelta(days=3 * game_index)).isoformat()
            game_id = f"{team}{game_index:03d}"
            for member in (game_index % 3, (game_index + 1) % 3):
                attempts = int(rng.integers(3, 24))
                rows.append(
                    {
                        "PLAYER_ID": team_players[team][member],
                        "TEAM_ABBREVIATION": team,
                        "GAME_ID": game_id,
                        "GAME_DATE": game_date,
                        "MATCHUP": f"{team} {location} {opponent}",
                        "MIN": int(rng.integers(12, 43)),
                        "PTS": int(rng.integers(5, 35)),
                        "FGA": attempts,
                        "FG3A": int(rng.integers(0, min(12, attempts) + 1)),
                        "FTA": int(rng.integers(0, 13)),
                    }
                )
    return pd.DataFrame(rows)


def synthetic_fixture():
    return synthetic_membership(), synthetic_game_logs()


def build_run():
    archetypes, game_logs = synthetic_fixture()
    return AnalysisRunBuilder(archetypes=archetypes, game_logs=game_logs).build()


def test_fixture_structure_represents_required_shapes():
    archetypes, game_logs = synthetic_fixture()
    sizes = archetypes.groupby("SUBTYPE_ID")["PLAYER_ID"].nunique()
    assert set(sizes.index) == {100, 200, 300}
    assert sizes.loc[100] > sizes.loc[200] > sizes.loc[300]
    assert len(game_logs) == 216
    assert game_logs["GAME_ID"].nunique() == 108
    assert (game_logs.groupby("PLAYER_ID").size() > 1).all()
    shared_players = game_logs.groupby("GAME_ID")["PLAYER_ID"].nunique()
    assert (shared_players == 2).all()


def test_membership_boundary_validates_and_deduplicates():
    archetypes, game_logs = synthetic_fixture()
    builder = AnalysisRunBuilder(archetypes=archetypes, game_logs=game_logs)
    membership = builder.load_membership()
    assert set(membership.columns) == {
        "PLAYER_ID",
        "PLAYER_NAME",
        "ARCHETYPE",
        "SUBTYPE_ID",
        "SUBTYPE_ARCHETYPE",
    }
    assert membership["PLAYER_ID"].nunique() == len(membership)
    assert membership.groupby("SUBTYPE_ID")["SUBTYPE_ARCHETYPE"].nunique().max() == 1


def test_membership_boundary_rejects_ambiguous_subtype_name():
    archetypes, game_logs = synthetic_fixture()
    broken = archetypes.copy()
    broken.loc[broken["PLAYER_ID"] == "P001", "SUBTYPE_ARCHETYPE"] = "Imposter Role"
    builder = AnalysisRunBuilder(archetypes=broken, game_logs=game_logs)
    with pytest.raises(ValueError):
        builder.build()


def test_prepared_logs_derive_exact_outcomes():
    run = build_run()
    logs = run.logs
    assert len(logs) == 216
    assert np.allclose(
        logs["LOG_POINTS"], logs["LOG_MINUTES"] + logs["LOG_SCORING_RATE"]
    )
    assert np.allclose(logs["PTS_PER_MIN"], logs["PTS"] / logs["MIN"])
    assert np.allclose(logs["FG2A"], (logs["FGA"] - logs["FG3A"]).clip(lower=0))
    for metric in EXPECTED_METRICS:
        assert np.allclose(logs[f"{metric}_PER_MIN"], logs[metric] / logs["MIN"])
    assert np.allclose(
        logs["PLAYER_AVG_MIN"], logs.groupby("PLAYER_ID")["MIN"].transform("mean")
    )


def test_coverage_derives_from_fixture():
    archetypes, game_logs = synthetic_fixture()
    run = build_run()
    coverage = run.coverage
    assert coverage.loc["classified_players"] == 18
    assert coverage.loc["players_with_games"] == 18
    assert coverage.loc["player_games"] == 216
    assert coverage.loc["distinct_games"] == 108
    assert coverage.loc["opponents"] == 6
    assert coverage.loc["subtypes"] == 3
    assert coverage.loc["offensive_team_clusters"] == 6
    expected_ppm = game_logs.query("MIN > 0 and PTS == PTS")["PTS"].sum() / game_logs[
        "MIN"
    ].sum()
    assert np.isclose(coverage.loc["league_average_pts_per_min"], expected_ppm)
    assert coverage.loc["latest_game_date"] == pd.to_datetime(
        game_logs["GAME_DATE"]
    ).max().date()


def test_scoring_fit_boundary_produces_consistent_summary():
    run = build_run()
    summary = run.matchup_summary
    assert len(summary) == 18
    assert summary["SUBTYPE_ID"].nunique() == 3
    assert summary["OPP_TEAM"].nunique() == 6
    assert np.allclose(
        summary["POINTS_TOTAL_EFFECT_PCT"],
        np.expm1(summary["POINTS_TOTAL_LOG_EFFECT"]) * 100,
    )
    assert np.allclose(
        summary["PPM_TOTAL_EFFECT_VS_LEAGUE_AVG_PCT"],
        summary["PPM_TOTAL_EFFECT"] / summary["LEAGUE_AVG_PPM"] * 100,
    )
    assert np.allclose(
        summary["MATCHUP_LEAGUE_INDEX"],
        summary["MATCHUP_ADJUSTED_SUBTYPE_PPM"] / summary["LEAGUE_AVG_PPM"] * 100,
    )
    assert summary["ELIGIBLE_FOR_INFERENCE"].dtype == bool
    assert {"INTERACTION_NOTABLE", "TOTAL_RATE_NOTABLE"} <= set(summary)


def test_volume_fit_boundary_produces_consistent_summary():
    run = build_run()
    volume = run.volume_matchup_summary
    assert len(volume) == 72
    assert set(volume["METRIC"]) == set(EXPECTED_METRICS)
    league_rates = {
        metric: run.logs[metric].sum() / run.logs["MIN"].sum()
        for metric in EXPECTED_METRICS
    }
    for metric, rate in league_rates.items():
        assert np.allclose(
            volume.loc[volume["METRIC"] == metric, "LEAGUE_AVG_RATE"], rate
        )
    assert np.allclose(
        volume["TOTAL_EFFECT_VS_LEAGUE_AVG_PCT"],
        volume["VOLUME_TOTAL_EFFECT"] / volume["LEAGUE_AVG_RATE"] * 100,
    )
    assert np.allclose(
        volume["MATCHUP_LEAGUE_INDEX"],
        volume["MATCHUP_ADJUSTED_RATE"] / volume["LEAGUE_AVG_RATE"] * 100,
    )
    reliability = run.volume_reliability
    assert len(reliability) == 4
    assert {"METRIC", "CELLS", "PEARSON", "SPEARMAN"} <= set(reliability)
    assert {"TRUE_INTERACTION_SD", "MEAN_SHRINKAGE_WEIGHT"} <= set(reliability)


def test_artifact_assembly_boundary_exposes_expected_schema():
    run = build_run()
    artifacts = run.artifacts
    assert set(artifacts) == {
        "matchup_summary",
        "notable_matchups",
        "validated_interactions",
        "watchlist",
        "player_relative_matchups",
        "volume_matchup_summary",
        "validated_volume_interactions",
        "volume_reliability",
        "player_relative_volume_matchups",
        "pts_per_min_heatmap",
        "volume_heatmaps",
    }
    summary = artifacts["matchup_summary"]
    assert summary["SUBTYPE_ID"].is_monotonic_increasing or summary["OPP_TEAM"].is_monotonic_increasing
    player_relative = artifacts["player_relative_matchups"]
    assert len(player_relative) == 90
    assert (player_relative["OPP_TEAM"] != player_relative["CURRENT_TEAM"]).all()
    assert {
        "RELATIVE_MATCHUP_PPM",
        "RELATIVE_MATCHUP_LEAGUE_INDEX",
        "MATCHUP_INDEX_CHANGE",
        "ARCHETYPE_INTERACTION_INDEX",
    } <= set(player_relative)
    player_volume = artifacts["player_relative_volume_matchups"]
    assert len(player_volume) == 360
    assert (player_volume["OPP_TEAM"] != player_volume["CURRENT_TEAM"]).all()
    assert len(artifacts["watchlist"]) <= 25
    heatmap = artifacts["pts_per_min_heatmap"]
    assert heatmap.shape == (3, 6)
    assert "100 \u2014 Rim Pressure" in heatmap.index
    assert heatmap.columns.tolist() == sorted(heatmap.columns.tolist())
    assert set(artifacts["volume_heatmaps"]) == set(EXPECTED_METRICS)


def test_dashboard_payload_boundary_exposes_expected_payload():
    run = build_run()
    payload = run.dashboard_payload
    assert payload["coverage"].loc["player_games"] == 216
    diagnostics = payload["diagnostics"]
    assert set(diagnostics) == {
        "eligible_cells",
        "interaction_findings",
        "total_rate_findings",
        "split_half_cells",
    }
    assert payload["subtype_labels"].loc[100] == "100 \u2014 Rim Pressure"
    assert {
        "SUBTYPE_ARCHETYPE",
        "OPP_TEAM",
        "PPM_TOTAL_EFFECT",
        "PPM_INTERACTION_EFFECT",
        "TOTAL_DIRECTION",
        "INTERACTION_DIRECTION",
    } <= set(payload["validated_interactions"])
    assert {
        "METRIC",
        "SUBTYPE_ARCHETYPE",
        "OPP_TEAM",
        "LEAGUE_AVG_RATE",
        "MATCHUP_ADJUSTED_RATE",
        "VOLUME_INTERACTION_EFFECT",
    } <= set(payload["validated_volume_interactions"])
    assert {"CELLS", "PEARSON", "SPEARMAN"} <= set(
        payload["volume_reliability_display"]
    )
    assert {"pearson_reliability", "spearman_reliability"} <= set(payload)


def test_boundary_methods_compose_from_fixture():
    archetypes, game_logs = synthetic_fixture()
    builder = AnalysisRunBuilder(archetypes=archetypes, game_logs=game_logs)
    assert len(builder.load_membership()) == 18
    assert len(builder.prepare_logs()) == 216
    assert len(builder.fit_scoring()) == 18
    assert len(builder.fit_volume()) == 72
    assert "matchup_summary" in builder.assemble_artifacts()
    assert "diagnostics" in builder.assemble_dashboard_payload()
    run = builder.build()
    assert len(run.matchup_summary) == 18
    assert len(run.volume_matchup_summary) == 72


def test_analysis_run_is_deterministic_and_repeatable():
    archetypes, game_logs = synthetic_fixture()
    run_a = AnalysisRunBuilder(archetypes=archetypes, game_logs=game_logs).build()
    run_b = AnalysisRunBuilder(archetypes=archetypes, game_logs=game_logs).build()
    pd.testing.assert_frame_equal(run_a.logs, run_b.logs)
    pd.testing.assert_frame_equal(run_a.matchup_summary, run_b.matchup_summary)
    pd.testing.assert_frame_equal(
        run_a.volume_matchup_summary, run_b.volume_matchup_summary
    )
    pd.testing.assert_frame_equal(
        run_a.artifacts["player_relative_matchups"],
        run_b.artifacts["player_relative_matchups"],
    )