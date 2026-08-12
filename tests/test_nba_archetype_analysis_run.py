"""
Behavioral tests for the archetype matchup Analysis Run builder (issue 70).

The tests drive the complete current matchup-analysis path from a compact
deterministic synthetic fixture: repeated players, unequal archetype sizes,
and teammates sharing games. They assert observable artifacts and arithmetic
instead of source text or dataframe implementations.
"""

import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1] / "analysis" / "nba-archetypes" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

from artifact_persistence import (  # noqa: E402
    artifact_manifest,
    png_text_entries,
    publish_artifact_set,
    stamp_png_identity,
    verify_persisted_manifest,
)
from matchup_analysis import (  # noqa: E402
    AnalysisRunBuilder,
    AnalysisRunSettings,
    ArchetypeModelSpec,
    FrozenDict,
    RunIdentity,
    _defensive_copy,
    compute_input_data_identity,
    fail_if_code_changed,
    spec_from_clustering_metadata,
)

BASE_DATE = date(2026, 1, 5)
TEAMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
GAMES_PER_TEAM = 18
EXPECTED_METRICS = ["FGA", "FG2A", "FG3A", "FTA"]

GOLDENS_DIR = Path(__file__).resolve().parent / "fixtures" / "archetype_analysis_goldens"
GOLDEN_FRAMES = [
    "matchup_summary",
    "notable_matchups",
    "validated_interactions",
    "watchlist",
    "player_relative_matchups",
    "volume_matchup_summary",
    "validated_volume_interactions",
    "volume_reliability",
    "player_relative_volume_matchups",
]
GOLDEN_RTOL = 1e-4
GOLDEN_ATOL = 1e-7

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


# Pre-refactor golden parity fixture (issue 70 review fix).
#
# The goldens under fixtures/archetype_analysis_goldens/ were generated by the
# PRE-REFACTOR archetype_matchups_2025_26.py at fixed point
# af5574d5d3a8e821bb63d21fc90684d5203aefed, run on this fixture. The default
# behavioral fixture above stays below the builder's cell eligibility thresholds
# (MIN_CELL_PLAYERS=8, MIN_CELL_GAMES=20,
# MIN_CELL_OFFENSIVE_TEAMS=5), where the pre-refactor implementation cannot
# complete (its empty volume-reliability merge crashes). This fixture therefore
# uses numeric PLAYER_ID/GAME_ID values like real provider data and sizes every
# subtype x opponent cell past those thresholds so the full pre-refactor path
# produces non-degenerate outputs for every auditable artifact.

PARITY_BASE_DATE = date(2026, 1, 5)
PARITY_TEAMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
PARITY_GAMES_PER_TEAM = 25
PARITY_SUBTYPE_SIZES = {100: 4, 200: 3, 300: 2}
PARITY_SUBTYPE_NAMES = {
    100: ("Rim Pressure", "Finisher"),
    200: ("Tertiary Shot Creator", "Creator"),
    300: ("Quarterback", "Facilitator"),
}
PARITY_SIGNAL = {
    (100, "FFF"): {"PTS": 6, "FGA": 3},
    (200, "DDD"): {"PTS": 5, "FG3A": 2},
}


def parity_membership():
    return _parity_roster()[
        ["PLAYER_ID", "PLAYER_NAME", "ARCHETYPE", "SUBTYPE_ID", "SUBTYPE_ARCHETYPE"]
    ].copy()


def parity_game_logs():
    rng = np.random.default_rng(20250812)
    roster = _parity_roster()
    player_subtype = dict(zip(roster["PLAYER_ID"], roster["SUBTYPE_ID"]))
    other_teams = {team: [t for t in PARITY_TEAMS if t != team] for team in PARITY_TEAMS}
    team_subtype_players = {
        (team, subtype): list(frame["PLAYER_ID"])
        for (team, subtype), frame in roster.groupby(
            ["TEAM_ABBREVIATION", "SUBTYPE_ID"]
        )
    }
    rows = []
    for team_index, team in enumerate(PARITY_TEAMS):
        for game_index in range(PARITY_GAMES_PER_TEAM):
            opponent = other_teams[team][(team_index + game_index) % 5]
            location = "vs." if game_index % 2 == 0 else "@"
            game_date = (PARITY_BASE_DATE + timedelta(days=3 * game_index)).isoformat()
            game_id = team_index * 1000 + game_index
            for subtype_id, count in PARITY_SUBTYPE_SIZES.items():
                subtype_players = team_subtype_players[(team, subtype_id)]
                member = subtype_players[(game_index + team_index) % count]
                attempts = int(rng.integers(3, 24))
                points = int(rng.integers(5, 35))
                three = int(rng.integers(0, min(12, attempts) + 1))
                signal = PARITY_SIGNAL.get((player_subtype[member], opponent), {})
                attempts = int(attempts + signal.get("FGA", 0))
                points = int(points + signal.get("PTS", 0))
                three = min(attempts, int(three + signal.get("FG3A", 0)))
                rows.append(
                    {
                        "PLAYER_ID": member,
                        "TEAM_ABBREVIATION": team,
                        "GAME_ID": game_id,
                        "GAME_DATE": game_date,
                        "MATCHUP": f"{team} {location} {opponent}",
                        "MIN": int(rng.integers(12, 43)),
                        "PTS": points,
                        "FGA": attempts,
                        "FG3A": three,
                        "FTA": int(rng.integers(0, 13)),
                    }
                )
    return pd.DataFrame(rows)


def _parity_roster():
    rows = []
    player_id = 1
    for team in PARITY_TEAMS:
        for subtype_id, count in PARITY_SUBTYPE_SIZES.items():
            subtype_name, archetype = PARITY_SUBTYPE_NAMES[subtype_id]
            for _slot in range(count):
                rows.append(
                    {
                        "PLAYER_ID": player_id,
                        "PLAYER_NAME": f"Parity Player {player_id}",
                        "ARCHETYPE": archetype,
                        "SUBTYPE_ID": subtype_id,
                        "SUBTYPE_ARCHETYPE": subtype_name,
                        "TEAM_ABBREVIATION": team,
                    }
                )
                player_id += 1
    return pd.DataFrame(rows)


def parity_fixture():
    return parity_membership(), parity_game_logs()


def merged_subtype_fixture():
    archetypes, game_logs = synthetic_fixture()
    merged = archetypes.copy()
    merged_away = merged["SUBTYPE_ID"] == 300
    merged.loc[merged_away, "SUBTYPE_ID"] = 200
    merged.loc[merged_away, "SUBTYPE_ARCHETYPE"] = "Tertiary Shot Creator"
    return merged, game_logs


def build_run():
    archetypes, game_logs = synthetic_fixture()
    return build_builder(archetypes, game_logs).build()


def make_model_spec(archetypes, game_logs, **overrides):
    values = {
        "season": "2025-26",
        "feature_definition": "play-type and shot-zone composition shares (CLR)",
        "clustering_method": "KMeans",
        "cluster_count": int(archetypes["SUBTYPE_ID"].nunique()),
        "random_seed": 42,
        "top_level_clusters": 6,
        "n_bootstraps": 20,
        "min_subtype_size": 12,
        "subtype_min_silhouette": 0.10,
        "subtype_min_stability": 0.65,
        "input_data_identity": compute_input_data_identity(archetypes, game_logs),
    }
    values.update(overrides)
    return ArchetypeModelSpec(**values)


def build_builder(archetypes, game_logs, **builder_kwargs):
    model_spec = make_model_spec(archetypes, game_logs)
    builder_kwargs.setdefault("code_revision", "test-revision")
    return AnalysisRunBuilder(
        archetypes=archetypes,
        game_logs=game_logs,
        model_spec=model_spec,
        **builder_kwargs,
    )


def minimal_png_bytes():
    """A structurally valid minimal 1x1 RGBA PNG, built offline."""

    def chunk(chunk_type, data):
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def write_attributed_csv(path, run, rows=None):
    frame = pd.DataFrame(
        rows if rows is not None else [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    )
    frame = frame.assign(RUN_ID=run.run_id, MODEL_VERSION=run.model_version)
    frame.to_csv(path, index=False)
    return path


def write_empty_attributed_csv(path, run):
    empty = pd.DataFrame(columns=["a", "b"]).assign(
        RUN_ID=run.run_id, MODEL_VERSION=run.model_version
    )
    row = {column: "" for column in empty.columns}
    row.update({"RUN_ID": run.run_id, "MODEL_VERSION": run.model_version})
    empty = pd.DataFrame([row], columns=empty.columns)
    empty.to_csv(path, index=False)
    return path


def write_stamped_png(path, run):
    path.write_bytes(
        stamp_png_identity(minimal_png_bytes(), run.run_id, run.model_version)
    )
    return path


PERSISTED_CSV_ARTIFACTS = [
    "matchup_summary",
    "notable_matchups",
    "validated_interactions",
    "watchlist",
    "player_relative_matchups",
    "volume_matchup_summary",
    "validated_volume_interactions",
    "volume_reliability",
    "player_relative_volume_matchups",
]
PERSISTED_PNG_ARTIFACTS = [
    "descriptive_pts_per_min_heatmap",
    "descriptive_volume_interaction_heatmaps",
]
PERSISTED_ARTIFACTS = [*PERSISTED_CSV_ARTIFACTS, *PERSISTED_PNG_ARTIFACTS]


def write_full_artifact_set(directory, run, empty_csv_names=()):
    """Write every required persisted artifact into ``directory`` and return paths."""
    paths = {}
    for name in PERSISTED_CSV_ARTIFACTS:
        if name in empty_csv_names:
            paths[name] = write_empty_attributed_csv(directory / f"{name}.csv", run)
        else:
            paths[name] = write_attributed_csv(directory / f"{name}.csv", run)
    for name in PERSISTED_PNG_ARTIFACTS:
        paths[name] = write_stamped_png(directory / f"{name}.png", run)
    return paths


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
    builder = build_builder(archetypes, game_logs)
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
    builder = build_builder(broken, game_logs)
    with pytest.raises(ValueError):
        builder.build()


def test_membership_rejects_null_player_identifier():
    archetypes, game_logs = synthetic_fixture()
    incomplete = pd.concat(
        [
            archetypes,
            pd.DataFrame(
                [
                    {
                        "PLAYER_ID": None,
                        "PLAYER_NAME": "Ghost Player",
                        "ARCHETYPE": "Finisher",
                        "SUBTYPE_ID": 100,
                        "SUBTYPE_ARCHETYPE": "Rim Pressure",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(ValueError, match="PLAYER_ID"):
        build_builder(incomplete, game_logs).build()


def test_membership_rejects_null_subtype_assignment():
    archetypes, game_logs = synthetic_fixture()
    incomplete = archetypes.copy()
    incomplete.loc[incomplete["PLAYER_ID"] == "P001", "SUBTYPE_ID"] = None
    with pytest.raises(ValueError, match="SUBTYPE_ID"):
        build_builder(incomplete, game_logs).build()


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
        "identity",
        "artifact_digests",
    }
    assert set(artifacts["artifact_digests"]) == {
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
        "volume_heatmap__FGA",
        "volume_heatmap__FG2A",
        "volume_heatmap__FG3A",
        "volume_heatmap__FTA",
        "subtype_labels",
    }
    for digest in artifacts["artifact_digests"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
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
    assert payload["identity"] == run.identity
    assert payload["identity"].run_id == run.run_id
    assert set(payload["identity"].stable_subtype_keys) == {100, 200, 300}
    assert payload["artifact_digests"] == run.artifacts["artifact_digests"]
    assert re.fullmatch(r"[0-9a-f]{64}", payload["artifact_digests"]["matchup_summary"])
    assert set(payload["provenance"]) == {
        "information_cutoff",
        "input_hashes",
        "code_revision",
        "generated_at",
        "cluster_count",
        "clustering_attempt",
    }


def test_boundary_methods_compose_from_fixture():
    archetypes, game_logs = synthetic_fixture()
    builder = build_builder(archetypes, game_logs)
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
    run_a = build_builder(archetypes, game_logs).build()
    run_b = build_builder(archetypes, game_logs).build()
    pd.testing.assert_frame_equal(run_a.logs, run_b.logs)
    pd.testing.assert_frame_equal(run_a.matchup_summary, run_b.matchup_summary)
    pd.testing.assert_frame_equal(
        run_a.volume_matchup_summary, run_b.volume_matchup_summary
    )
    pd.testing.assert_frame_equal(
        run_a.artifacts["player_relative_matchups"],
        run_b.artifacts["player_relative_matchups"],
    )


def test_model_spec_version_is_content_addressed():
    archetypes, game_logs = synthetic_fixture()
    base = make_model_spec(archetypes, game_logs)
    assert re.fullmatch(r"[0-9a-f]{64}", base.version)
    variations = {
        "season": "2024-25",
        "feature_definition": "a different feature definition",
        "clustering_method": "GaussianMixture",
        "cluster_count": base.cluster_count + 1,
        "random_seed": base.random_seed + 1,
        "top_level_clusters": base.top_level_clusters + 1,
        "n_bootstraps": base.n_bootstraps + 1,
        "min_subtype_size": base.min_subtype_size + 1,
        "subtype_min_silhouette": 0.20,
        "subtype_min_stability": 0.70,
        "input_data_identity": "0" * 64,
    }
    for field, value in variations.items():
        changed = make_model_spec(archetypes, game_logs, **{field: value})
        assert changed.version != base.version, field
    other_archetypes, other_logs = parity_fixture()
    other_data = make_model_spec(other_archetypes, other_logs)
    assert other_data.version != base.version


def test_run_records_provenance_and_carries_matching_identity():
    archetypes, game_logs = synthetic_fixture()
    model_spec = make_model_spec(archetypes, game_logs)
    builder = AnalysisRunBuilder(
        archetypes=archetypes,
        game_logs=game_logs,
        model_spec=model_spec,
        clustering_attempt=3,
        code_revision="abc123",
    )
    run = builder.build()
    assert run.model_spec is model_spec
    assert run.model_version == model_spec.version
    assert re.fullmatch(r"[0-9a-f]{64}", run.run_id)
    provenance = run.provenance
    assert provenance.information_cutoff == date(2026, 2, 25)
    assert provenance.cluster_count == model_spec.cluster_count
    assert provenance.clustering_attempt == 3
    assert provenance.code_revision == "abc123"
    assert provenance.generated_at.tzinfo is not None
    assert isinstance(provenance.generated_at, datetime)
    assert set(provenance.input_hashes) == {"archetypes", "game_logs"}
    for digest in provenance.input_hashes.values():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    payload = run.dashboard_payload
    assert payload["identity"] == run.identity
    assert payload["identity"].run_id == run.run_id
    assert payload["identity"].stable_subtype_keys == run.stable_subtype_keys
    assert payload["provenance"] == provenance.to_dict()
    assert payload["provenance"]["clustering_attempt"] == 3


def test_artifacts_carry_matching_run_and_model_identifiers():
    run = build_run()
    assert run.artifacts["identity"] == run.identity
    assert run.artifacts["identity"].run_id == run.run_id
    assert run.artifacts["identity"].model_version == run.model_version
    assert run.artifacts["identity"].stable_subtype_keys == run.stable_subtype_keys


def test_stable_subtype_keys_invariant_under_label_permutation():
    archetypes, game_logs = synthetic_fixture()
    baseline = build_run()
    permuted = archetypes.copy()
    permuted["SUBTYPE_ID"] = permuted["SUBTYPE_ID"].map({100: 200, 200: 300, 300: 100})
    permuted_run = build_builder(permuted, game_logs).build()

    def keys_by_members(run):
        keys = {}
        for subtype_id, group in run.membership.groupby("SUBTYPE_ID"):
            members = frozenset(group["PLAYER_ID"].astype(str))
            keys[members] = run.stable_subtype_keys[subtype_id]
        return keys

    assert keys_by_members(baseline) == keys_by_members(permuted_run)
    assert set(baseline.stable_subtype_keys) == {100, 200, 300}
    assert set(permuted_run.stable_subtype_keys) == {200, 300, 100}


def test_overlapping_membership_rejected_as_key_collision():
    archetypes, game_logs = synthetic_fixture()
    overlapping = pd.concat(
        [
            archetypes,
            archetypes.loc[archetypes["PLAYER_ID"] == "P001"].assign(
                SUBTYPE_ID=200, SUBTYPE_ARCHETYPE="Tertiary Shot Creator"
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(ValueError):
        build_builder(overlapping, game_logs).build()


def test_missing_model_spec_is_rejected_at_artifact_assembly():
    archetypes, game_logs = synthetic_fixture()
    builder = AnalysisRunBuilder(
        archetypes=archetypes, game_logs=game_logs, code_revision="test-revision"
    )
    with pytest.raises(ValueError, match="ArchetypeModelSpec is required"):
        builder.assemble_artifacts()
    with pytest.raises(ValueError, match="ArchetypeModelSpec is required"):
        builder.build()


def test_mismatched_input_data_identity_is_rejected():
    archetypes, game_logs = synthetic_fixture()
    other_archetypes, other_logs = parity_fixture()
    model_spec = make_model_spec(other_archetypes, other_logs)
    with pytest.raises(ValueError, match="Input data identity does not match"):
        AnalysisRunBuilder(
            archetypes=archetypes,
            game_logs=game_logs,
            model_spec=model_spec,
            code_revision="test-revision",
        ).build()


def test_run_identity_is_deterministic_across_builds():
    archetypes, game_logs = synthetic_fixture()
    run_a = build_builder(
        archetypes, game_logs, code_revision="rev-1", clustering_attempt=1
    ).build()
    run_b = build_builder(
        archetypes, game_logs, code_revision="rev-1", clustering_attempt=1
    ).build()
    assert run_a.run_id == run_b.run_id
    assert run_a.model_version == run_b.model_version
    assert run_a.provenance.input_hashes == run_b.provenance.input_hashes
    assert run_a.provenance.information_cutoff == run_b.provenance.information_cutoff


def test_code_revision_is_required():
    archetypes, game_logs = synthetic_fixture()
    spec = make_model_spec(archetypes, game_logs)
    for missing in (None, "", "   "):
        with pytest.raises(ValueError, match="code_revision"):
            AnalysisRunBuilder(
                archetypes=archetypes,
                game_logs=game_logs,
                model_spec=spec,
                code_revision=missing,
            )
    run_a = AnalysisRunBuilder(
        archetypes=archetypes,
        game_logs=game_logs,
        model_spec=spec,
        code_revision="rev-a",
    ).build()
    run_b = AnalysisRunBuilder(
        archetypes=archetypes,
        game_logs=game_logs,
        model_spec=spec,
        code_revision="rev-b",
    ).build()
    assert run_a.run_id != run_b.run_id


def test_run_id_is_versioned_by_data_snapshot():
    archetypes, game_logs = synthetic_fixture()
    clustering_snapshot = game_logs[["PLAYER_ID", "MIN"]].copy()
    spec = make_model_spec(
        archetypes,
        game_logs,
        input_data_identity=compute_input_data_identity(archetypes, clustering_snapshot),
    )
    run_a = AnalysisRunBuilder(
        archetypes=archetypes,
        game_logs=game_logs,
        model_spec=spec,
        clustering_features=clustering_snapshot,
        code_revision="rev-1",
    ).build()
    tweaked_logs = game_logs.copy()
    tweaked_logs.loc[tweaked_logs.index[0], "PTS"] += 1
    run_b = AnalysisRunBuilder(
        archetypes=archetypes,
        game_logs=tweaked_logs,
        model_spec=spec,
        clustering_features=clustering_snapshot,
        code_revision="rev-1",
    ).build()
    assert run_a.model_version == run_b.model_version
    assert run_a.provenance.information_cutoff == run_b.provenance.information_cutoff
    assert run_a.provenance.input_hashes != run_b.provenance.input_hashes
    assert run_a.run_id != run_b.run_id


def test_input_data_identity_preserves_full_numeric_precision():
    archetypes, game_logs = synthetic_fixture()
    precise = game_logs.assign(MODEL_VALUE=0.12345678901)
    tweaked = precise.copy()
    tweaked.loc[tweaked.index[0], "MODEL_VALUE"] = 0.12345678902
    assert compute_input_data_identity(archetypes, precise) != compute_input_data_identity(
        archetypes, tweaked
    )


def test_input_data_identity_preserves_integers_beyond_2_53():
    archetypes, game_logs = synthetic_fixture()
    large = game_logs.assign(BIG_ID=2**53)
    tweaked = large.copy()
    tweaked.loc[tweaked.index[0], "BIG_ID"] = 2**53 + 1
    assert compute_input_data_identity(archetypes, large) != compute_input_data_identity(
        archetypes, tweaked
    )


def test_spec_cluster_count_must_match_actual_membership():
    archetypes, game_logs = synthetic_fixture()
    spec = make_model_spec(archetypes, game_logs, cluster_count=999)
    with pytest.raises(ValueError, match="cluster_count"):
        AnalysisRunBuilder(
            archetypes=archetypes,
            game_logs=game_logs,
            model_spec=spec,
            code_revision="test-revision",
        ).build()


def test_analysis_run_and_identity_are_immutable():
    run = build_run()
    with pytest.raises(FrozenInstanceError):
        run.run_id = "tampered"
    with pytest.raises(TypeError, match="immutable"):
        run.provenance.input_hashes["archetypes"] = "0" * 64
    with pytest.raises(TypeError, match="immutable"):
        run.stable_subtype_keys[100] = "0" * 64
    with pytest.raises(TypeError, match="immutable"):
        run.stable_subtype_keys |= {100: "0" * 64}
    with pytest.raises(TypeError, match="immutable"):
        run.provenance.input_hashes |= {"archetypes": "0" * 64}


def test_analysis_run_exposes_only_defensive_copies():
    run = build_run()
    original_summary = run.matchup_summary.copy()
    summary = run.matchup_summary
    summary.loc[summary.index[0], "PPM_INTERACTION_EFFECT"] = 999
    pd.testing.assert_frame_equal(run.matchup_summary, original_summary)

    original_logs = run.logs.copy()
    logs = run.logs
    logs.loc[logs.index[0], "PTS"] = 999
    pd.testing.assert_frame_equal(run.logs, original_logs)

    original_artifact = run.artifacts["matchup_summary"].copy()
    artifact = run.artifacts["matchup_summary"]
    artifact.iloc[0, 0] = 999
    pd.testing.assert_frame_equal(run.artifacts["matchup_summary"], original_artifact)

    run.artifacts.pop("matchup_summary")
    run.artifacts["identity"] = None
    assert "matchup_summary" in run.artifacts
    assert run.artifacts["identity"] == run.identity

    run.dashboard_payload["identity"] = None
    run.dashboard_payload.pop("provenance")
    assert run.dashboard_payload["identity"] == run.identity
    assert "provenance" in run.dashboard_payload


def test_run_identity_value_object_validates_and_equals():
    run = build_run()
    assert isinstance(run.identity, RunIdentity)
    assert run.identity == run.identity
    with pytest.raises(ValueError):
        RunIdentity(run_id="not-hex", model_version=run.model_version, stable_subtype_keys={100: "0" * 64})
    with pytest.raises(ValueError):
        RunIdentity(run_id=run.run_id, model_version="not-hex", stable_subtype_keys={100: "0" * 64})
    with pytest.raises(ValueError):
        RunIdentity(run_id=run.run_id, model_version=run.model_version, stable_subtype_keys={100: "short"})


def test_generated_at_is_injectable_and_pinned():
    archetypes, game_logs = synthetic_fixture()
    pinned = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    run_a = build_builder(
        archetypes, game_logs, code_revision="rev-1", generated_at=pinned
    ).build()
    run_b = build_builder(
        archetypes, game_logs, code_revision="rev-1", generated_at=pinned
    ).build()
    assert run_a.provenance.generated_at == pinned
    assert run_b.provenance == run_a.provenance


def test_model_identity_uses_clustering_inputs_not_game_logs():
    archetypes, game_logs = synthetic_fixture()
    clustering_snapshot = game_logs[["PLAYER_ID", "MIN"]].copy()
    model_input_identity = compute_input_data_identity(archetypes, clustering_snapshot)
    assert model_input_identity != compute_input_data_identity(archetypes, game_logs)
    spec = make_model_spec(
        archetypes, game_logs, input_data_identity=model_input_identity
    )
    run = AnalysisRunBuilder(
        archetypes=archetypes,
        game_logs=game_logs,
        model_spec=spec,
        clustering_features=clustering_snapshot,
        code_revision="test-revision",
    ).build()
    assert run.model_version == spec.version
    with pytest.raises(ValueError, match="Input data identity does not match"):
        AnalysisRunBuilder(
            archetypes=archetypes,
            game_logs=game_logs,
            model_spec=spec,
            clustering_features=game_logs,
            code_revision="test-revision",
        ).build()


def test_dashboard_assembly_rejects_missing_bundle_identity():
    # Drive the public assembly seam into the guarded state: an artifact bundle
    # whose identity was removed must be rejected, not silently accepted.
    archetypes, game_logs = synthetic_fixture()
    builder = build_builder(archetypes, game_logs)
    builder.assemble_artifacts()
    builder._artifacts["identity"] = None
    with pytest.raises(ValueError, match="Artifact bundle identity"):
        builder.assemble_dashboard_payload()


def test_dashboard_assembly_rejects_cross_run_artifact():
    # An artifact frame replaced with one from a different run must be rejected
    # at dashboard assembly, not silently mixed into the payload.
    archetypes, game_logs = synthetic_fixture()
    builder_a = build_builder(archetypes, game_logs)
    builder_a.assemble_artifacts()

    other_archetypes, other_logs = parity_fixture()
    builder_b = build_builder(other_archetypes, other_logs)
    builder_b.assemble_artifacts()

    builder_a._artifacts["matchup_summary"] = builder_b._artifacts["matchup_summary"]
    with pytest.raises(ValueError, match="does not match the run's assembled content"):
        builder_a.assemble_dashboard_payload()


def test_artifact_manifest_records_content_digests():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = write_full_artifact_set(directory, run)
        manifest = artifact_manifest(run, paths)
        assert manifest["run_id"] == run.run_id
        assert manifest["model_version"] == run.model_version
        assert manifest["stable_subtype_keys"] == {
            str(key): value for key, value in run.stable_subtype_keys.items()
        }
        assert manifest["provenance"] == run.provenance.to_dict()
        assert manifest["artifacts"]["matchup_summary"] == {
            "file": "matchup_summary.csv",
            "sha256": hashlib.sha256(paths["matchup_summary"].read_bytes()).hexdigest(),
        }
        assert manifest["artifacts"]["descriptive_pts_per_min_heatmap"] == {
            "file": "descriptive_pts_per_min_heatmap.png",
            "sha256": hashlib.sha256(
                paths["descriptive_pts_per_min_heatmap"].read_bytes()
            ).hexdigest(),
        }
        tampered = pd.DataFrame({"a": [9], "b": [9]}).assign(
            RUN_ID=run.run_id, MODEL_VERSION=run.model_version
        )
        tampered.to_csv(paths["matchup_summary"], index=False)
        replaced = artifact_manifest(run, paths)
        assert (
            replaced["artifacts"]["matchup_summary"]["sha256"]
            != manifest["artifacts"]["matchup_summary"]["sha256"]
        )


def test_artifact_manifest_rejects_identity_less_csv():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = write_full_artifact_set(directory, run)
        bad = directory / "matchup_summary.csv"
        bad.write_text("a,b\n1,2\n")
        paths["matchup_summary"] = bad
        with pytest.raises(ValueError, match="not self-attributing"):
            artifact_manifest(run, paths)


def test_artifact_manifest_rejects_foreign_csv_identity():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = write_full_artifact_set(directory, run)
        foreign = pd.DataFrame({"a": [1]}).assign(
            RUN_ID="0" * 64, MODEL_VERSION="0" * 64
        )
        foreign.to_csv(paths["matchup_summary"], index=False)
        with pytest.raises(ValueError, match="does not match this run"):
            artifact_manifest(run, paths)


def test_artifact_manifest_accepts_empty_attributed_csv():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_full_artifact_set(Path(tmp), run, empty_csv_names={"watchlist"})
        manifest = artifact_manifest(run, paths)
        frame = pd.read_csv(paths["watchlist"])
        assert frame["RUN_ID"].tolist() == [run.run_id]
        assert manifest["artifacts"]["watchlist"]["sha256"] == hashlib.sha256(
            paths["watchlist"].read_bytes()
        ).hexdigest()
        verify_persisted_manifest(manifest, Path(tmp))


def test_png_identity_stamping_roundtrips():
    run = build_run()
    stamped = stamp_png_identity(minimal_png_bytes(), run.run_id, run.model_version)
    entries = png_text_entries(stamped)
    assert entries["run_id"] == run.run_id
    assert entries["model_version"] == run.model_version


def test_verify_persisted_manifest_rejects_tampered_and_missing():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = write_full_artifact_set(directory, run)
        manifest = artifact_manifest(run, paths)
        verify_persisted_manifest(manifest, directory)

        paths["matchup_summary"].write_text("tampered\n1\n")
        with pytest.raises(ValueError, match="does not match its recorded digest"):
            verify_persisted_manifest(manifest, directory)
        write_attributed_csv(paths["matchup_summary"], run)

        paths["matchup_summary"].unlink()
        with pytest.raises(FileNotFoundError, match="is missing"):
            verify_persisted_manifest(manifest, directory)
        write_attributed_csv(paths["matchup_summary"], run)

        foreign = pd.DataFrame({"a": [1]}).assign(
            RUN_ID="1" * 64, MODEL_VERSION="1" * 64
        )
        foreign.to_csv(paths["matchup_summary"], index=False)
        crafted = dict(manifest)
        crafted["artifacts"] = dict(manifest["artifacts"])
        crafted["artifacts"]["matchup_summary"] = {
            "file": "matchup_summary.csv",
            "sha256": hashlib.sha256(paths["matchup_summary"].read_bytes()).hexdigest(),
        }
        with pytest.raises(ValueError, match="embeds identity that does not match"):
            verify_persisted_manifest(crafted, directory)


def test_run_id_is_versioned_by_analysis_settings():
    archetypes, game_logs = synthetic_fixture()
    defaults = build_builder(archetypes, game_logs, code_revision="rev-1").build()
    tight = build_builder(
        archetypes,
        game_logs,
        code_revision="rev-1",
        settings=AnalysisRunSettings(
            min_cell_players=1,
            min_cell_games=1,
            min_cell_offensive_teams=1,
        ),
    ).build()
    assert defaults.run_id != tight.run_id
    assert defaults.model_version == tight.model_version


def test_artifact_digests_are_bound_to_run_identity():
    archetypes, game_logs = synthetic_fixture()
    run_a = build_builder(archetypes, game_logs, code_revision="rev-a").build()
    run_b = build_builder(archetypes, game_logs, code_revision="rev-b").build()
    assert run_a.run_id != run_b.run_id
    pd.testing.assert_frame_equal(
        run_a.artifacts["matchup_summary"], run_b.artifacts["matchup_summary"]
    )
    assert (
        run_a.artifacts["artifact_digests"]["matchup_summary"]
        != run_b.artifacts["artifact_digests"]["matchup_summary"]
    )


def test_dashboard_assembly_rejects_replaced_digest_record():
    archetypes, game_logs = synthetic_fixture()
    builder = build_builder(archetypes, game_logs)
    builder.assemble_artifacts()
    builder._artifacts["artifact_digests"] = FrozenDict()
    with pytest.raises(ValueError, match="digest record"):
        builder.assemble_dashboard_payload()


def test_fit_objects_published_by_analysis_run_are_immutable():
    run = build_run()
    fit = run.scoring_fits["points_fit"]["fit"]
    original = fit.params.copy()
    with pytest.raises(ValueError):
        fit.params[0] = 999
    np.testing.assert_array_equal(fit.params, original)
    with pytest.raises(ValueError):
        fit.params += 1
    np.testing.assert_array_equal(fit.params, original)
    original_cov = run.scoring_fits["points_fit"]["covariance"].copy()
    covariance = run.scoring_fits["points_fit"]["covariance"]
    covariance[0, 0] = 999
    np.testing.assert_array_equal(
        run.scoring_fits["points_fit"]["covariance"], original_cov
    )


def test_published_fit_arrays_cannot_be_made_writable_again():
    run = build_run()
    fit = run.scoring_fits["points_fit"]["fit"]
    for name in ("params", "bse", "tvalues", "pvalues", "resid", "fittedvalues"):
        array = getattr(fit, name)
        original = array.copy()
        with pytest.raises(ValueError):
            array.setflags(write=True)
        with pytest.raises(ValueError):
            array[0] = 999
        with pytest.raises(ValueError):
            array += 1
        np.testing.assert_array_equal(getattr(fit, name), original)


def test_defensive_copy_closes_object_valued_cells():
    source = {
        "frame": pd.DataFrame({"notes": [["a"], {"b": 1}], "value": [1, 2]})
    }
    copied = _defensive_copy(source)
    copied["frame"].at[0, "notes"].append("leaked")
    copied["frame"].at[1, "notes"]["b"] = 99
    assert source["frame"].at[0, "notes"] == ["a"]
    assert source["frame"].at[1, "notes"] == {"b": 1}


def test_frozen_dict_rejects_base_dict_invocation():
    frozen = FrozenDict({"a": 1})
    with pytest.raises(TypeError):
        dict.__setitem__(frozen, "a", 2)
    with pytest.raises(TypeError):
        dict.update(frozen, {"a": 2})
    with pytest.raises(TypeError):
        dict.__ior__(frozen, {"a": 2})
    assert frozen["a"] == 1
    assert dict(frozen) == {"a": 1}
    assert frozen == {"a": 1}


def test_clustering_attempt_requires_positive_integer():
    archetypes, game_logs = synthetic_fixture()
    spec = make_model_spec(archetypes, game_logs)
    for bad in (None, "3", 0, -2, 2.5, True, False):
        with pytest.raises(ValueError, match="clustering_attempt"):
            AnalysisRunBuilder(
                archetypes=archetypes,
                game_logs=game_logs,
                model_spec=spec,
                code_revision="rev-1",
                clustering_attempt=bad,
            )


def test_input_data_identity_survives_lossless_csv_round_trip():
    archetypes, game_logs = synthetic_fixture()
    snapshot = game_logs[["PLAYER_ID", "MIN"]].assign(RATE=0.12345678901234567)
    expected = compute_input_data_identity(archetypes, snapshot)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.csv"
        snapshot.to_csv(path, index=False)
        loaded = pd.read_csv(path, float_precision="round_trip")
        assert compute_input_data_identity(archetypes, loaded) == expected


def test_spec_from_clustering_metadata_requires_recorded_identity():
    metadata = {
        "season": "2025-26",
        "feature_definition": "some feature definition",
        "clustering_method": "KMeans",
        "cluster_count": 3,
        "random_seed": 42,
        "top_level_clusters": 6,
        "n_bootstraps": 20,
        "min_subtype_size": 12,
        "subtype_min_silhouette": 0.10,
        "subtype_min_stability": 0.65,
    }
    with pytest.raises(ValueError, match="input_data_identity"):
        spec_from_clustering_metadata(metadata)
    metadata["input_data_identity"] = "0" * 64
    spec = spec_from_clustering_metadata(metadata)
    assert spec.season == "2025-26"
    assert spec.cluster_count == 3
    assert spec.random_seed == 42
    assert spec.top_level_clusters == 6
    assert spec.n_bootstraps == 20
    assert spec.min_subtype_size == 12
    assert spec.subtype_min_silhouette == 0.10
    assert spec.subtype_min_stability == 0.65
    assert spec.input_data_identity == "0" * 64
    assert re.fullmatch(r"[0-9a-f]{64}", spec.version)
    incomplete = {key: value for key, value in metadata.items() if key != "cluster_count"}
    with pytest.raises(ValueError, match="cluster_count"):
        spec_from_clustering_metadata(incomplete)


def test_matrix_dimensions_and_labels_derive_from_run_data():
    archetypes, game_logs = merged_subtype_fixture()
    assert archetypes["SUBTYPE_ID"].nunique() == 2
    run = build_builder(archetypes, game_logs).build()
    assert run.matchup_summary["SUBTYPE_ID"].nunique() == 2
    assert len(run.matchup_summary) == 12
    assert run.artifacts["pts_per_min_heatmap"].shape == (2, 6)
    assert set(run.stable_subtype_keys) == {100, 200}
    assert run.dashboard_payload["subtype_labels"].index.tolist() == [100, 200]


def build_parity_run():
    archetypes, game_logs = parity_fixture()
    return build_builder(archetypes, game_logs).build()


def _canonical_frame(frame):
    """Deterministic row order shared with the stored goldens."""
    return frame.sort_values(list(frame.columns)).reset_index(drop=True)


def _assert_frame_equivalent(actual, golden, label):
    assert set(actual.columns) == set(golden.columns), f"{label}: column sets differ"
    columns = sorted(actual.columns)
    actual = _canonical_frame(actual[columns])
    golden = _canonical_frame(golden[columns])
    assert len(actual) == len(golden), f"{label}: row count differs"
    for column in columns:
        got = actual[column]
        exp = golden[column]
        if pd.api.types.is_numeric_dtype(got.dtype):
            got_arr = got.to_numpy(dtype=float)
            exp_arr = exp.to_numpy(dtype=float)
            np.testing.assert_allclose(
                got_arr,
                exp_arr,
                rtol=GOLDEN_RTOL,
                atol=GOLDEN_ATOL,
                equal_nan=True,
                err_msg=f"{label}.{column}: values differ",
            )
        else:
            got_missing = got.isna()
            exp_missing = exp.isna()
            np.testing.assert_array_equal(
                got_missing, exp_missing, err_msg=f"{label}.{column}: missing mask differs"
            )
            np.testing.assert_array_equal(
                got[~got_missing], exp[~exp_missing], err_msg=f"{label}.{column}: values differ"
            )


def _assert_heatmap_equivalent(actual, golden, label):
    assert list(actual.index) == list(golden.index), f"{label}: row labels differ"
    assert list(actual.columns) == list(golden.columns), f"{label}: column labels differ"
    got = actual.to_numpy(dtype=float)
    exp = golden.to_numpy(dtype=float)
    np.testing.assert_allclose(
        got,
        exp,
        rtol=GOLDEN_RTOL,
        atol=GOLDEN_ATOL,
        equal_nan=True,
        err_msg=f"{label}: values differ",
    )


def test_parity_fixture_cells_exceed_eligibility_thresholds():
    archetypes, game_logs = parity_fixture()
    sizes = archetypes.groupby("SUBTYPE_ID")["PLAYER_ID"].nunique()
    assert set(sizes.index) == {100, 200, 300}
    assert sizes.loc[100] > sizes.loc[200] > sizes.loc[300]
    assert (game_logs.groupby("PLAYER_ID").size() > 1).all()
    assert (game_logs.groupby("GAME_ID")["PLAYER_ID"].nunique() >= 3).all()
    assert set(game_logs["PLAYER_ID"]) == set(archetypes["PLAYER_ID"])
    merged = game_logs.merge(
        archetypes, on="PLAYER_ID", how="inner", validate="many_to_one"
    )
    merged["OPP_TEAM"] = merged["MATCHUP"].str.extract(r"(?:vs\.|@)\s+([A-Z]{3})$")
    cells = merged.groupby(["SUBTYPE_ID", "OPP_TEAM"]).agg(
        PLAYERS=("PLAYER_ID", "nunique"),
        DISTINCT_GAMES=("GAME_ID", "nunique"),
        OFFENSIVE_TEAMS=("TEAM_ABBREVIATION", "nunique"),
    )
    assert (cells["PLAYERS"] >= 8).all()
    assert (cells["DISTINCT_GAMES"] >= 20).all()
    assert (cells["OFFENSIVE_TEAMS"] >= 5).all()


def test_builder_matches_pre_refactor_golden_frames():
    run = build_parity_run()
    for label in GOLDEN_FRAMES:
        golden = pd.read_csv(GOLDENS_DIR / f"{label}.csv")
        _assert_frame_equivalent(run.artifacts[label], golden, label)


def test_dashboard_payload_matches_pre_refactor_goldens():
    run = build_parity_run()
    payload = run.dashboard_payload
    scalars = json.loads((GOLDENS_DIR / "dashboard_scalars.json").read_text())

    assert payload["diagnostics"] == scalars["diagnostics"]
    for key in ("pearson_reliability", "spearman_reliability", "tau_ppm", "mean_shrinkage"):
        assert np.isclose(
            payload[key], scalars[key], rtol=GOLDEN_RTOL, atol=GOLDEN_ATOL
        ), key
    assert {
        str(key): str(value) for key, value in payload["subtype_labels"].items()
    } == scalars["subtype_labels"]

    coverage = json.loads((GOLDENS_DIR / "coverage.json").read_text())
    for key, expected in coverage.items():
        actual = run.coverage[key]
        if key == "latest_game_date":
            assert actual.isoformat() == expected
        elif isinstance(expected, float):
            assert np.isclose(
                float(actual), expected, rtol=GOLDEN_RTOL, atol=GOLDEN_ATOL
            )
        else:
            assert int(actual) == expected


def test_heatmaps_match_pre_refactor_goldens():
    run = build_parity_run()
    payload = run.dashboard_payload
    limits = json.loads((GOLDENS_DIR / "heatmap_limits.json").read_text())

    golden = pd.read_csv(GOLDENS_DIR / "pts_per_min_heatmap.csv", index_col=0)
    _assert_heatmap_equivalent(
        run.artifacts["pts_per_min_heatmap"], golden, "pts_per_min_heatmap"
    )
    assert np.isclose(
        payload["pts_per_min_heatmap_limit"],
        limits["pts_per_min_heatmap_limit"],
        rtol=GOLDEN_RTOL,
        atol=GOLDEN_ATOL,
    )
    for metric in EXPECTED_METRICS:
        golden = pd.read_csv(GOLDENS_DIR / f"volume_heatmap_{metric}.csv", index_col=0)
        heatmap = run.artifacts["volume_heatmaps"][metric]
        _assert_heatmap_equivalent(heatmap["data"], golden, f"volume_heatmap_{metric}")
        assert np.isclose(
            heatmap["limit"], limits[metric], rtol=GOLDEN_RTOL, atol=GOLDEN_ATOL
        )


def test_golden_comparators_reject_infinity_mismatches():
    frame = pd.DataFrame(
        {"ID": [1, 2, 3, 4], "VALUE": [1.0, np.inf, -np.inf, np.nan]}
    )
    sign_flip = frame.assign(VALUE=[1.0, -np.inf, np.inf, np.nan])
    finite_to_inf = frame.assign(VALUE=[1.0, 5.0, -np.inf, np.nan])
    for mismatched in (sign_flip, finite_to_inf):
        with pytest.raises(AssertionError):
            _assert_frame_equivalent(frame, mismatched, "probe")
    _assert_frame_equivalent(frame, frame.copy(), "probe")

    heatmap = pd.DataFrame({"col": [1.0, np.inf, np.nan]})
    with pytest.raises(AssertionError):
        _assert_heatmap_equivalent(
            heatmap, heatmap.assign(col=[1.0, -np.inf, np.nan]), "probe"
        )
    _assert_heatmap_equivalent(heatmap, heatmap.copy(), "probe")


def test_artifact_digests_cover_heatmap_labels_and_limits():
    archetypes, game_logs = parity_fixture()
    builder = build_builder(archetypes, game_logs)
    builder.assemble_artifacts()
    heatmap = builder._artifacts["pts_per_min_heatmap"]
    relabeled = heatmap.copy()
    relabeled.index = heatmap.index[::-1]
    builder._artifacts["pts_per_min_heatmap"] = relabeled
    with pytest.raises(ValueError, match="does not match the run's assembled content"):
        builder.assemble_dashboard_payload()

    builder_volume = build_builder(archetypes, game_logs)
    builder_volume.assemble_artifacts()
    builder_volume._artifacts["volume_heatmaps"]["FGA"]["limit"] = 999
    with pytest.raises(ValueError, match="does not match the run's assembled content"):
        builder_volume.assemble_dashboard_payload()

    builder_labels = build_builder(archetypes, game_logs)
    builder_labels.assemble_artifacts()
    data = builder_labels._artifacts["volume_heatmaps"]["FGA"]["data"]
    relabeled = data.copy()
    relabeled.index = data.index[::-1]
    builder_labels._artifacts["volume_heatmaps"]["FGA"]["data"] = relabeled
    with pytest.raises(ValueError, match="does not match the run's assembled content"):
        builder_labels.assemble_dashboard_payload()


def test_artifact_digests_cover_subtype_labels():
    archetypes, game_logs = synthetic_fixture()
    builder = build_builder(archetypes, game_logs)
    builder.assemble_artifacts()
    builder._subtype_labels = builder._subtype_labels.rename(index={100: "999"})
    with pytest.raises(ValueError, match="does not match the run's assembled content"):
        builder.assemble_dashboard_payload()


def test_published_fit_objects_are_immutable_snapshots():
    run = build_run()
    fit = run.scoring_fits["points_fit"]["fit"]
    with pytest.raises(FrozenInstanceError):
        fit.params = np.array([9.0, 9.0])
    with pytest.raises(ValueError):
        fit.bse[0] = 999
    run.scoring_fits["points_fit"]["fit"] = None
    assert run.scoring_fits["points_fit"]["fit"] is not None


def test_frozen_dict_backing_mapping_is_not_writable():
    run = build_run()
    with pytest.raises(TypeError):
        run.identity.stable_subtype_keys._data[100] = "0" * 64
    with pytest.raises(TypeError):
        run.provenance.input_hashes._data["archetypes"] = "0" * 64
    assert run.stable_subtype_keys[100] != "0" * 64
    assert run.provenance.input_hashes["archetypes"] != "0" * 64


class _MutableMappingView(Mapping):
    def __init__(self, items):
        self._items = dict(items)

    def __getitem__(self, key):
        return self._items[key]

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __setitem__(self, key, value):
        self._items[key] = value


def test_defensive_copy_closes_set_and_custom_mapping_cells():
    cell_map = _MutableMappingView({"b": 1})
    source = {
        "frame": pd.DataFrame(
            {
                "tags": [{"a"}, {1, 2}],
                "meta": [cell_map, {"c": 3}],
                "value": [1, 2],
            }
        )
    }
    copied = _defensive_copy(source)
    copied["frame"].at[0, "tags"].add("leaked")
    copied["frame"].at[1, "meta"]["c"] = 99
    assert source["frame"].at[0, "tags"] == {"a"}
    assert source["frame"].at[1, "meta"] == {"c": 3}
    copied["frame"].at[0, "meta"]["b"] = 99
    assert cell_map._items == {"b": 1}


def test_fail_if_code_changed_guards_mixed_snapshots():
    fail_if_code_changed("rev", "rev")
    with pytest.raises(RuntimeError, match="code"):
        fail_if_code_changed("rev-a", "rev-b")


def test_code_revision_is_a_stable_digest_of_analysis_disk_state(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    from code_revision import current_code_revision

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "analysis_code.txt"
    tracked.write_text("v1\n")
    subprocess.run(["git", "add", "analysis_code.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        cwd=tmp_path,
        check=True,
    )
    baseline = current_code_revision(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{40}", baseline)
    assert current_code_revision(tmp_path) == baseline

    tracked.write_text("v1\nv2\n")
    dirty = current_code_revision(tmp_path)
    assert dirty != baseline
    assert re.fullmatch(r"[0-9a-f]{64}", dirty)
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=tmp_path, check=True)
    assert current_code_revision(tmp_path) == baseline

    untracked = tmp_path / "untracked_inputs.csv"
    untracked.write_text("a,b\n1,2\n")
    assert current_code_revision(tmp_path) != baseline
    untracked.unlink()
    assert current_code_revision(tmp_path) == baseline


def test_artifact_manifest_rejects_empty_and_incomplete_graphs():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="required"):
            artifact_manifest(run, {})
        paths = write_full_artifact_set(Path(tmp), run)
        del paths["watchlist"]
        with pytest.raises(ValueError, match="missing"):
            artifact_manifest(run, paths)
        paths = write_full_artifact_set(Path(tmp), run)
        paths["sneaky_extra"] = paths["matchup_summary"]
        with pytest.raises(ValueError, match="unexpected"):
            artifact_manifest(run, paths)


def test_artifact_manifest_rejects_duplicate_files():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_full_artifact_set(Path(tmp), run)
        paths["watchlist"] = paths["matchup_summary"]
        with pytest.raises(ValueError, match="duplicate"):
            artifact_manifest(run, paths)


def test_verify_persisted_manifest_rejects_partial_and_duplicate_records():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = write_full_artifact_set(directory, run)
        manifest = artifact_manifest(run, paths)
        verify_persisted_manifest(manifest, directory)

        crafted = dict(manifest)
        crafted["artifacts"] = dict(manifest["artifacts"])
        crafted["artifacts"]["watchlist"] = crafted["artifacts"]["matchup_summary"]
        with pytest.raises(ValueError, match="duplicate"):
            verify_persisted_manifest(crafted, directory)

        crafted = dict(manifest)
        crafted["artifacts"] = {
            name: record
            for name, record in manifest["artifacts"].items()
            if name != "watchlist"
        }
        with pytest.raises(ValueError, match="missing"):
            verify_persisted_manifest(crafted, directory)

        crafted = dict(manifest)
        crafted["artifacts"] = dict(manifest["artifacts"])
        crafted["artifacts"]["sneaky"] = crafted["artifacts"]["matchup_summary"]
        with pytest.raises(ValueError, match="unexpected"):
            verify_persisted_manifest(crafted, directory)


def test_artifact_manifest_rejects_header_only_csv():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_full_artifact_set(Path(tmp), run)
        header_only = paths["watchlist"]
        header_only.write_text("RUN_ID,MODEL_VERSION\n")
        with pytest.raises(ValueError, match="no rows"):
            artifact_manifest(run, paths)


def write_staged_set(staging_dir, run):
    """Stage a complete verified artifact set plus its identity manifest."""
    paths = write_full_artifact_set(staging_dir, run)
    manifest = artifact_manifest(run, paths)
    (staging_dir / "run_identity_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True)
    )
    return manifest


def test_publish_artifact_set_swaps_verified_staged_set():
    run_a = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    run_b = build_builder(*synthetic_fixture(), code_revision="rev-b").build()
    assert run_a.run_id != run_b.run_id
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "matchups"
        staging_a = root / "stage_a"
        staging_a.mkdir()
        manifest_a = write_staged_set(staging_a, run_a)
        publish_artifact_set(staging_a, output)
        verify_persisted_manifest(manifest_a, output)
        assert (output / "run_identity_manifest.json").exists()

        staging_b = root / "stage_b"
        staging_b.mkdir()
        manifest_b = write_staged_set(staging_b, run_b)
        publish_artifact_set(staging_b, output)
        verify_persisted_manifest(manifest_b, output)
        persisted = json.loads((output / "run_identity_manifest.json").read_text())
        assert persisted["run_id"] == run_b.run_id


def test_publish_artifact_set_preserves_previous_set_when_staging_fails():
    run_a = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    run_b = build_builder(*synthetic_fixture(), code_revision="rev-b").build()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "matchups"
        staging_a = root / "stage_a"
        staging_a.mkdir()
        manifest_a = write_staged_set(staging_a, run_a)
        publish_artifact_set(staging_a, output)

        staging_b = root / "stage_b"
        staging_b.mkdir()
        write_staged_set(staging_b, run_b)
        (staging_b / "matchup_summary.csv").write_text("tampered\n1\n")
        with pytest.raises(ValueError, match="does not match its recorded digest"):
            publish_artifact_set(staging_b, output)
        verify_persisted_manifest(manifest_a, output)
        assert (output / "run_identity_manifest.json").read_text() == json.dumps(
            manifest_a, indent=2, sort_keys=True
        )


def test_publish_artifact_set_rejects_missing_manifest():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        staging = root / "stage"
        staging.mkdir()
        write_full_artifact_set(staging, run)
        with pytest.raises(ValueError, match="manifest"):
            publish_artifact_set(staging, root / "matchups")


def test_artifact_manifest_rejects_unsupported_file_types():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = {}
        for name in PERSISTED_ARTIFACTS:
            path = directory / f"{name}.bin"
            path.write_bytes(b"not a csv or png")
            paths[name] = path
        with pytest.raises(ValueError, match="supported"):
            artifact_manifest(run, paths)


def test_verify_persisted_manifest_rejects_unsupported_file_types():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        crafted = {
            "run_id": run.run_id,
            "model_version": run.model_version,
            "stable_subtype_keys": {
                str(key): value for key, value in run.stable_subtype_keys.items()
            },
            "provenance": run.provenance.to_dict(),
            "artifacts": {},
        }
        for name in PERSISTED_ARTIFACTS:
            path = directory / f"{name}.bin"
            path.write_bytes(b"not a csv or png")
            crafted["artifacts"][name] = {
                "file": f"{name}.bin",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        with pytest.raises(ValueError, match="supported"):
            verify_persisted_manifest(crafted, directory)


def test_png_text_entries_rejects_missing_iend():
    run = build_run()
    stamped = stamp_png_identity(minimal_png_bytes(), run.run_id, run.model_version)
    iend_index = stamped.index(b"IEND")
    mutilated = stamped[: iend_index - 4]
    with pytest.raises(ValueError, match="IEND"):
        png_text_entries(mutilated)


def test_png_text_entries_rejects_corrupt_crc_and_truncation():
    run = build_run()
    stamped = stamp_png_identity(minimal_png_bytes(), run.run_id, run.model_version)
    idat_index = stamped.index(b"IDAT")
    corrupt = bytearray(stamped)
    corrupt[idat_index + 4 + 2] ^= 0xFF
    with pytest.raises(ValueError, match="CRC"):
        png_text_entries(bytes(corrupt))
    with pytest.raises(ValueError, match="end of the file"):
        png_text_entries(stamped[:-4])


def test_stamp_png_identity_rejects_malformed_input():
    run = build_run()
    with pytest.raises(ValueError, match="Not a PNG"):
        stamp_png_identity(b"not a png", run.run_id, run.model_version)
    no_iend = minimal_png_bytes()[:-12]
    with pytest.raises(ValueError, match="IEND"):
        stamp_png_identity(no_iend, run.run_id, run.model_version)


def test_model_spec_version_covers_clustering_method_parameters():
    archetypes, game_logs = synthetic_fixture()
    base = make_model_spec(archetypes, game_logs)
    variations = {
        "top_level_clusters": base.top_level_clusters + 1,
        "n_bootstraps": base.n_bootstraps + 1,
        "min_subtype_size": base.min_subtype_size + 1,
        "subtype_min_silhouette": 0.20,
        "subtype_min_stability": 0.70,
    }
    for field, value in variations.items():
        changed = make_model_spec(archetypes, game_logs, **{field: value})
        assert changed.version != base.version, field


def test_model_spec_rejects_invalid_clustering_parameters():
    archetypes, game_logs = synthetic_fixture()
    for bad in (None, "3", 0, -1, 2.5, True):
        with pytest.raises(ValueError):
            make_model_spec(archetypes, game_logs, top_level_clusters=bad)
        with pytest.raises(ValueError):
            make_model_spec(archetypes, game_logs, n_bootstraps=bad)
        with pytest.raises(ValueError):
            make_model_spec(archetypes, game_logs, min_subtype_size=bad)
    for bad in (-0.1, 1.5, True, "high"):
        with pytest.raises(ValueError):
            make_model_spec(archetypes, game_logs, subtype_min_silhouette=bad)
        with pytest.raises(ValueError):
            make_model_spec(archetypes, game_logs, subtype_min_stability=bad)


def test_matrix_and_labels_cover_membership_subtypes_without_usable_logs():
    archetypes, game_logs = synthetic_fixture()
    subtype_300_players = set(
        archetypes.loc[archetypes["SUBTYPE_ID"] == 300, "PLAYER_ID"]
    )
    filtered = game_logs.loc[~game_logs["PLAYER_ID"].isin(subtype_300_players)].copy()
    run = build_builder(archetypes, filtered).build()
    assert run.provenance.cluster_count == 3
    assert set(run.stable_subtype_keys) == {100, 200, 300}
    heatmap = run.artifacts["pts_per_min_heatmap"]
    assert heatmap.shape == (3, 6)
    assert heatmap.loc["300 \u2014 Quarterback"].isna().all()
    labels = run.dashboard_payload["subtype_labels"]
    assert labels.index.tolist() == [100, 200, 300]
    assert labels.loc[300] == "300 \u2014 Quarterback"
    for metric in EXPECTED_METRICS:
        data = run.artifacts["volume_heatmaps"][metric]["data"]
        assert data.shape == (3, 6)
        assert data.loc["300 \u2014 Quarterback"].isna().all()
