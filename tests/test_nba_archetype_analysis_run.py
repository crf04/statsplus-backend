"""
Behavioral tests for the archetype matchup Analysis Run builder (issue 70).

The tests drive the complete current matchup-analysis path from a compact
deterministic synthetic fixture: repeated players, unequal archetype sizes,
and teammates sharing games. They assert observable artifacts and arithmetic
instead of source text or dataframe implementations.
"""

import hashlib
import importlib.util
import json
import os
import pickle
import py_compile
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
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

import artifact_persistence as artifact_persistence_module  # noqa: E402
from artifact_persistence import (  # noqa: E402
    PERSISTED_ARTIFACT_FILENAMES,
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
    FrozenFitResult,
    RunIdentity,
    VOLUME_METRICS,
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
    """Write every required persisted artifact into ``directory`` and return paths.

    Each artifact is written under its canonical persisted filename, so a
    manifest built from the returned paths always binds the logical artifact to
    its canonical name.
    """
    paths = {}
    for name in PERSISTED_CSV_ARTIFACTS:
        filename = PERSISTED_ARTIFACT_FILENAMES[name]
        if name in empty_csv_names:
            paths[name] = write_empty_attributed_csv(directory / filename, run)
        else:
            paths[name] = write_attributed_csv(directory / filename, run)
    for name in PERSISTED_PNG_ARTIFACTS:
        paths[name] = write_stamped_png(
            directory / PERSISTED_ARTIFACT_FILENAMES[name], run
        )
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
            "file": "descriptive_pts_per_min_interaction_heatmap.png",
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


def test_png_rejects_duplicate_and_conflicting_identity_chunks():
    # Reproduction of the round-7 finding: a double-stamped PNG embedding a
    # foreign identity first and the expected identity later silently overwrote
    # the earlier value, so both identities were present but verification
    # passed. Duplicate tEXt keywords now fail closed, so a conflicting
    # double-stamped image can never be blessed.
    run = build_run()
    foreign = "ab12" * 16
    double = stamp_png_identity(
        stamp_png_identity(minimal_png_bytes(), run.run_id, run.model_version),
        foreign,
        "cd34" * 16,
    )
    with pytest.raises(ValueError, match="duplicate tEXt"):
        png_text_entries(double)
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_full_artifact_set(Path(tmp), run)
        paths["descriptive_pts_per_min_heatmap"].write_bytes(double)
        with pytest.raises(ValueError, match="duplicate tEXt"):
            artifact_manifest(run, paths)


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


def test_verify_rejects_duplicate_and_mangled_csv_identity_columns():
    # Reproduction of the round-8 finding: pandas silently renames a repeated
    # header field to ``RUN_ID.1``/``MODEL_VERSION.1`` while the verifier
    # checked only the first identity column, so a conflicting second identity
    # slipped past the value check. The raw header is now parsed before any
    # values are trusted, and a duplicate identity field (in any case) or any
    # pandas-mangled ``.N`` variant of an identity field fails closed.
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = write_full_artifact_set(directory, run)
        manifest = artifact_manifest(run, paths)

        for header, message in (
            ("RUN_ID,RUN_ID", "repeats the RUN_ID identity column"),
            ("RUN_ID,MODEL_VERSION,RUN_ID,MODEL_VERSION",
             "repeats the RUN_ID identity column"),
            ("run_id,run_id", "repeats the RUN_ID identity column"),
            ("RUN_ID,RUN_ID.1", "mangled identity column"),
            ("MODEL_VERSION.1,RUN_ID,MODEL_VERSION", "mangled identity column"),
        ):
            width = len(header.split(","))
            csv_path = directory / "matchup_summary.csv"
            csv_path.write_text(
                header + "\n" + ",".join(str(i) for i in range(1, width + 1)) + "\n"
            )
            crafted = dict(manifest)
            crafted["artifacts"] = dict(manifest["artifacts"])
            crafted["artifacts"]["matchup_summary"] = {
                "file": "matchup_summary.csv",
                "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            }
            with pytest.raises(ValueError, match=message):
                verify_persisted_manifest(crafted, directory)

        # The manifest-building seam rejects the same ambiguous raw headers
        # before any values are read.
        for header in ("RUN_ID,RUN_ID", "RUN_ID,RUN_ID.1"):
            width = len(header.split(","))
            csv_path = directory / "matchup_summary.csv"
            csv_path.write_text(
                header + "\n" + ",".join(str(i) for i in range(1, width + 1)) + "\n"
            )
            paths["matchup_summary"] = csv_path
            with pytest.raises(ValueError, match="identity column"):
                artifact_manifest(run, paths)


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


def test_nested_settings_cannot_desync_run_id_and_artifacts():
    # Reproduction of the round-7 finding: a frozen settings value owning a
    # mutable nested volume_metrics dict could be mutated after provenance was
    # recorded, so the run id kept the original metrics while the artifacts were
    # assembled from the mutated subset. The nested mapping is deep-frozen at
    # construction and every identity/artifact stage reads the same snapshot.
    archetypes, game_logs = synthetic_fixture()
    settings = AnalysisRunSettings()
    builder = build_builder(archetypes, game_logs, code_revision="rev-1", settings=settings)
    builder.record_provenance()
    for mutator in (
        lambda: settings.volume_metrics.pop("FTA"),
        lambda: settings.volume_metrics.__setitem__("FTA", "tampered"),
        lambda: settings.volume_metrics.update({"FTA": "tampered"}),
    ):
        with pytest.raises(TypeError):
            mutator()
    run = builder.build()
    assert set(run.artifacts["volume_matchup_summary"]["METRIC"].unique()) == set(
        VOLUME_METRICS
    )
    assert set(
        run.dashboard_payload["volume_reliability_display"]["METRIC"].unique()
    ) == set(VOLUME_METRICS)
    # A caller constructing their own settings object cannot mutate it either.
    with pytest.raises(TypeError):
        AnalysisRunSettings(volume_metrics=dict(VOLUME_METRICS)).volume_metrics.pop("FTA")


def test_builder_settings_cannot_be_reassigned_and_frozen_setstate_is_one_shot():
    # Reproduction of the round-8 finding: ``builder.settings`` was a plain
    # writable attribute, so a later stage could read a different settings
    # object than the one the run id was versioned against, and ``FrozenDict``'s
    # public ``__setstate__`` was an unrestricted mutator that could rewrite a
    # built identity/settings mapping. The snapshot is now a private read-only
    # property with no setter, and ``__setstate__`` only ever initializes a
    # freshly unpickled object.
    archetypes, game_logs = synthetic_fixture()
    settings = AnalysisRunSettings()
    builder = build_builder(
        archetypes, game_logs, code_revision="rev-1", settings=settings
    )
    builder.record_provenance()
    with pytest.raises(AttributeError, match="no setter"):
        builder.settings = AnalysisRunSettings()
    with pytest.raises(TypeError, match="immutable"):
        builder.settings.volume_metrics.__setstate__(
            dict(builder.settings.volume_metrics)
        )
    with pytest.raises(TypeError, match="immutable"):
        settings.volume_metrics.__setstate__(dict(settings.volume_metrics))

    # A live FrozenDict anywhere cannot be rewritten through __setstate__.
    frozen = FrozenDict({"a": 1})
    with pytest.raises(TypeError, match="immutable"):
        frozen.__setstate__({"a": 2})
    assert frozen == {"a": 1}

    # Pickle reconstruction still works: __setstate__ initializes the fresh
    # object exactly once, and the restored object is immutable again.
    restored = pickle.loads(pickle.dumps(frozen))
    assert restored == {"a": 1}
    with pytest.raises(TypeError, match="immutable"):
        restored.__setstate__({"a": 2})
    assert restored == {"a": 1}

    # The run identity stays consistent: a control builder built from the same
    # settings snapshot produces the same run id despite the tampering attempts.
    control = build_builder(
        archetypes, game_logs, code_revision="rev-1", settings=settings
    ).build()
    assert builder.build().run_id == control.run_id


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


def test_artifact_manifest_binds_each_logical_artifact_to_its_canonical_file():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_full_artifact_set(Path(tmp), run)
        paths["watchlist"] = paths["matchup_summary"]
        with pytest.raises(ValueError, match="must be saved as"):
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
        with pytest.raises(ValueError, match="must be saved as"):
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
        with pytest.raises(ValueError, match="must be persisted as"):
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


# --- Round-6 adversarial tests (issue 71 review) -----------------------------


def _assert_base_chain_cannot_be_made_writable(array, label):
    """Walk the ``.base`` chain; every ndarray link must refuse writes and the
    chain must terminate in an immutable bytes buffer."""
    seen = 0
    cursor = array
    while isinstance(cursor, np.ndarray) and cursor.base is not None:
        if isinstance(cursor.base, np.ndarray):
            with pytest.raises(ValueError):
                cursor.base.setflags(write=True)
            with pytest.raises(ValueError):
                cursor.base[0] = 999
        cursor = cursor.base
        seen += 1
        assert seen < 16, label
    assert isinstance(cursor, bytes), label


def test_frozen_fit_snapshot_exposes_no_mutable_base_backing():
    # The directly constructed snapshot (the in-memory publication path) must
    # back its arrays with immutable bytes: no link in the ``.base`` chain can
    # ever be re-enabled for writing.
    arr = np.array([1.0, 2.0, 3.0])
    frozen = FrozenFitResult(
        params=arr,
        bse=arr,
        tvalues=arr,
        pvalues=arr,
        resid=arr,
        fittedvalues=arr,
        df_resid=1.0,
        df_model=1.0,
        nobs=3.0,
    )
    for name in ("params", "bse", "tvalues", "pvalues", "resid", "fittedvalues"):
        _assert_base_chain_cannot_be_made_writable(getattr(frozen, name), name)
    run = build_run()
    fit = run.scoring_fits["points_fit"]["fit"]
    for name in ("params", "bse", "tvalues", "pvalues", "resid", "fittedvalues"):
        array = getattr(fit, name)
        if isinstance(array.base, np.ndarray):
            with pytest.raises(ValueError):
                array.base.setflags(write=True)
            with pytest.raises(ValueError):
                array.base[0] = 999
        with pytest.raises(ValueError):
            array.setflags(write=True)


def test_deserialized_fit_arrays_cannot_be_mutated_via_base_methods():
    # Reproduction of the round-7 finding: pickling the immutable snapshot
    # recreated owning writable storage, and re-pinning only set the flag on the
    # view, so the base ``np.ndarray.setflags``/``np.ndarray.__setitem__``
    # invocations could mutate the returned snapshot. Deserialized arrays are
    # now rebuilt over immutable bytes, so even base-class method calls fail.
    run = build_run()
    for fit in [
        entry["fit"] for entry in run.scoring_fits.values()
        if isinstance(entry, dict) and "fit" in entry
    ]:
        for name in ("params", "bse", "tvalues", "pvalues", "resid", "fittedvalues"):
            array = getattr(fit, name)
            original = array.copy()
            with pytest.raises(ValueError):
                np.ndarray.setflags(array, write=True)
            with pytest.raises(ValueError):
                np.ndarray.__setitem__(array, 0, 999)
            with pytest.raises(ValueError):
                array += 1
            np.testing.assert_array_equal(getattr(fit, name), original)
            _assert_base_chain_cannot_be_made_writable(array, name)


def test_run_private_backing_state_is_immutable_serialized_bytes():
    run = build_run()
    backing_fields = (
        "_membership",
        "_logs",
        "_coverage",
        "_scoring_fits",
        "_matchup_summary",
        "_volume_fits",
        "_volume_matchup_summary",
        "_volume_reliability",
        "_volume_shrinkage_summary",
        "_artifacts",
        "_dashboard_payload",
    )
    for name in backing_fields:
        assert isinstance(getattr(run, name), bytes), name

    before_membership = run.membership.copy()
    before_logs = run.logs.copy()
    with pytest.raises(TypeError):
        run._membership[0] = 1
    with pytest.raises(FrozenInstanceError):
        run._membership = b"tampered"

    # Even reconstructing the backing from its serialized form and mutating it
    # must not change what later property reads return.
    leaked = pickle.loads(run._membership)
    leaked.iloc[0, 0] = "TAMPERED"
    pd.testing.assert_frame_equal(run.membership, before_membership)

    leaked_logs = pickle.loads(run._logs)
    leaked_logs.loc[leaked_logs.index[0], "PTS"] = 999
    pd.testing.assert_frame_equal(run.logs, before_logs)

    leaked_artifacts = pickle.loads(run._artifacts)
    leaked_artifacts.pop("watchlist")
    leaked_artifacts["identity"] = None
    assert run.artifacts["identity"] == run.identity
    assert "watchlist" in run.artifacts

    leaked_payload = pickle.loads(run._dashboard_payload)
    leaked_payload.pop("provenance")
    leaked_payload["identity"] = None
    assert run.dashboard_payload["identity"] == run.identity
    assert "provenance" in run.dashboard_payload


def test_publish_artifact_set_uses_versioned_dirs_and_atomic_pointer():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "matchups"
        staging = root / "stage"
        staging.mkdir()
        manifest = write_staged_set(staging, run)
        publish_artifact_set(staging, output)
        assert output.is_symlink()
        versioned_root = root / "matchups-runs"
        entries = list(versioned_root.iterdir())
        assert len(entries) == 1
        assert output.resolve() == entries[0].resolve()
        verify_persisted_manifest(manifest, output)
        assert (output / "run_identity_manifest.json").exists()


def test_publish_artifact_set_replaces_pointer_and_garbage_collects_old_set():
    run_a = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    run_b = build_builder(*synthetic_fixture(), code_revision="rev-b").build()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "matchups"
        versioned_root = root / "matchups-runs"

        staging_a = root / "stage_a"
        staging_a.mkdir()
        manifest_a = write_staged_set(staging_a, run_a)
        publish_artifact_set(staging_a, output)
        verify_persisted_manifest(manifest_a, output)

        staging_b = root / "stage_b"
        staging_b.mkdir()
        manifest_b = write_staged_set(staging_b, run_b)
        publish_artifact_set(staging_b, output)

        entries = list(versioned_root.iterdir())
        assert len(entries) == 1
        assert output.resolve() == entries[0].resolve()
        persisted = json.loads((output / "run_identity_manifest.json").read_text())
        assert persisted["run_id"] == run_b.run_id
        verify_persisted_manifest(manifest_b, output)


def test_publish_artifact_set_sigkill_never_leaves_pointer_absent():
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

        # A child process that kills itself with SIGKILL at the exact pointer
        # swap: no Python exception handler can run, so a crash here must not
        # leave the published path absent or pointing at a partial set.
        child = root / "crash_child.py"
        child.write_text(
            "import os, sys\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import artifact_persistence as ap\n"
            "real_replace = os.replace\n"
            "def killer(src, dst):\n"
            "    if '.current-' in str(src):\n"
            "        os.kill(os.getpid(), 9)\n"
            "    return real_replace(src, dst)\n"
            "os.replace = killer\n"
            f"ap.publish_artifact_set({str(staging_b)!r}, {str(output)!r})\n"
        )
        subprocess.run([sys.executable, str(child)], check=False)

        assert output.exists()
        assert output.is_symlink()
        persisted = json.loads((output / "run_identity_manifest.json").read_text())
        assert persisted["run_id"] == run_a.run_id
        verify_persisted_manifest(manifest_a, output)


def test_publish_artifact_set_rejects_real_directory_target():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "matchups"
        output.mkdir()
        staging = root / "stage"
        staging.mkdir()
        write_staged_set(staging, run)
        with pytest.raises(ValueError, match="real directory"):
            publish_artifact_set(staging, output)


def test_publish_rejects_mutation_after_version_install_and_preserves_previous(tmp_path):
    # Reproduction of the round-8 TOCTOU finding: the old publication verified
    # the shared staging directory before locking and then moved it without
    # revalidation, so another process could replace the staged bytes after
    # verification and get unverified content installed. The staged set is now
    # moved into the private immutable versioned namespace first and verified
    # there under the publication lock, so a mutation of the moved set (or of
    # the now-consumed staging path) fails closed and leaves the published path
    # on the previous set.
    run_a = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    run_b = build_builder(*synthetic_fixture(), code_revision="rev-b").build()
    root = Path(tmp_path)
    output = root / "matchups"
    staging_a = root / "stage_a"
    staging_a.mkdir()
    manifest_a = write_staged_set(staging_a, run_a)
    publish_artifact_set(staging_a, output)

    staging_b = root / "stage_b"
    staging_b.mkdir()
    write_staged_set(staging_b, run_b)

    def tamper_moved_set():
        versioned_root = root / "matchups-runs"
        moved = next(
            entry
            for entry in versioned_root.iterdir()
            if entry.is_dir() and entry.name.startswith(run_b.run_id + "~")
        )
        (moved / "matchup_summary.csv").write_text("tampered\n")

    artifact_persistence_module._PUBLISH_HOOKS["after_version_install"] = tamper_moved_set
    try:
        with pytest.raises(ValueError, match="does not match its recorded digest"):
            publish_artifact_set(staging_b, output)
    finally:
        artifact_persistence_module._PUBLISH_HOOKS.clear()

    verify_persisted_manifest(manifest_a, output)
    versioned_root = root / "matchups-runs"
    sets = [entry for entry in versioned_root.iterdir() if entry.is_dir()]
    assert len(sets) == 1 and output.resolve() == sets[0].resolve()


def test_publish_installs_the_verified_moved_set_not_a_recreated_staging_path(tmp_path):
    # The bytes that are verified are exactly the bytes installed: after the
    # staged set is moved into the private versioned namespace and verified
    # under the lock, a concurrent process recreating the (now consumed)
    # staging path with junk cannot change what gets published. The published
    # set still matches the verified moved bytes and the recreated staging
    # path is never consulted.
    run = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    root = Path(tmp_path)
    output = root / "matchups"
    staging = root / "stage"
    staging.mkdir()
    manifest = write_staged_set(staging, run)

    def recreate_staging_with_junk():
        if not staging.exists():
            staging.mkdir()
        (staging / "matchup_summary.csv").write_text("UNVERIFIED_JUNK\n")

    artifact_persistence_module._PUBLISH_HOOKS["after_verify"] = recreate_staging_with_junk
    try:
        publish_artifact_set(staging, output)
    finally:
        artifact_persistence_module._PUBLISH_HOOKS.clear()

    verify_persisted_manifest(manifest, output)
    assert (staging / "matchup_summary.csv").read_text() == "UNVERIFIED_JUNK\n"
    versioned_root = root / "matchups-runs"
    installed = next(entry for entry in versioned_root.iterdir() if entry.is_dir())
    assert (installed / "run_identity_manifest.json").exists()


def test_concurrent_publishers_cannot_delete_a_pending_version(tmp_path):
    # Reproduction of the round-7 finding: publisher B installs its version and
    # pauses before flipping the pointer, then publisher A's garbage collection
    # deletes B's pending version, so B flips the live pointer onto a deleted
    # directory — both calls return successfully and the live pointer is broken.
    # Publication is now serialized by a cross-process lock held from version
    # install through pointer flip and garbage collection, so B's pending
    # version always survives A's concurrent publication.
    run_a = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    run_b = build_builder(*synthetic_fixture(), code_revision="rev-b").build()
    run_c = build_builder(*synthetic_fixture(), code_revision="rev-c").build()
    root = Path(tmp_path)
    output = root / "matchups"

    staging_a = root / "stage_a"
    staging_a.mkdir()
    write_staged_set(staging_a, run_a)
    publish_artifact_set(staging_a, output)

    sync = root / "sync"
    sync.mkdir()
    staging_b = root / "stage_b"
    staging_b.mkdir()
    write_staged_set(staging_b, run_b)
    child = root / "publisher_b.py"
    child.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "import artifact_persistence as ap\n"
        "SYNC = os.environ['SYNC']\n"
        "def pause():\n"
        "    open(os.path.join(SYNC, 'installed.marker'), 'w').write('x')\n"
        "    deadline = time.time() + 60\n"
        "    while not os.path.exists(os.path.join(SYNC, 'proceed')):\n"
        "        if time.time() > deadline: raise RuntimeError('timeout')\n"
        "        time.sleep(0.02)\n"
        "ap._PUBLISH_HOOKS['before_pointer_flip'] = pause\n"
        f"ap.publish_artifact_set({str(staging_b)!r}, {str(output)!r})\n"
        "print('B_DONE')\n"
    )
    env = dict(os.environ, SYNC=str(sync))
    proc = subprocess.Popen(
        [sys.executable, str(child)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _wait_for(sync / "installed.marker")

    # Publisher C (in this process, on a worker thread) publishes while B holds
    # the lock and its pending version exists.
    staging_c = root / "stage_c"
    staging_c.mkdir()
    manifest_c = write_staged_set(staging_c, run_c)
    results = {}

    def publish_c():
        try:
            publish_artifact_set(staging_c, output)
            results["ok"] = True
        except Exception as error:  # pragma: no cover - failure path
            results["ok"] = False
            results["error"] = repr(error)

    thread = threading.Thread(target=publish_c)
    thread.start()
    # Let C reach the lock, then confirm B's pending version directory survived.
    time.sleep(0.5)
    versioned_root = root / "matchups-runs"
    b_pending = versioned_root / f"{run_b.run_id}~1"
    assert b_pending.is_dir(), "a concurrent publisher deleted B's pending version"

    (sync / "proceed").write_text("go")
    thread.join(timeout=60)
    out, _ = proc.communicate(timeout=90)
    assert "B_DONE" in out, out
    assert results["ok"], results.get("error")

    sets = [entry for entry in versioned_root.iterdir() if entry.is_dir()]
    assert len(sets) == 1, [entry.name for entry in versioned_root.iterdir()]
    assert output.resolve() == sets[0].resolve()
    persisted = json.loads((output / "run_identity_manifest.json").read_text())
    assert persisted["run_id"] == run_c.run_id
    verify_persisted_manifest(manifest_c, output)


def test_publish_durability_barriers_precede_garbage_collection(monkeypatch):
    # Reproduction of the round-7 finding: the new set and pointer were never
    # fsynced before the prior immutable set was deleted, so power loss could
    # lose the new contents or pointer after the old set was removed. Every
    # artifact file is now flushed, then the versioned directory and the
    # pointer's parent directory, before any garbage collection runs.
    run_a = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    run_b = build_builder(*synthetic_fixture(), code_revision="rev-b").build()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "matchups"
        staging_a = root / "stage_a"
        staging_a.mkdir()
        write_staged_set(staging_a, run_a)
        publish_artifact_set(staging_a, output)

        order = []

        def fsync_path(path):
            order.append(("file", Path(path).name))

        def fsync_directory(path):
            order.append(("dir", Path(path).name))

        def gc(versioned_root, target):
            order.append(("gc", Path(target).name))

        monkeypatch.setattr(artifact_persistence_module, "_fsync_path", fsync_path)
        monkeypatch.setattr(
            artifact_persistence_module, "_fsync_directory", fsync_directory
        )
        monkeypatch.setattr(
            artifact_persistence_module, "_gc_published_runs", gc
        )
        staging_b = root / "stage_b"
        staging_b.mkdir()
        write_staged_set(staging_b, run_b)
        publish_artifact_set(staging_b, output)

        file_indices = [i for i, (kind, _) in enumerate(order) if kind == "file"]
        dir_indices = [i for i, (kind, _) in enumerate(order) if kind == "dir"]
        gc_index = order.index(("gc", "matchups"))
        assert file_indices, "no artifact file was fsynced before garbage collection"
        assert dir_indices, "no directory was fsynced before garbage collection"
        assert max(file_indices) < min(dir_indices)
        assert max(dir_indices) < gc_index


def test_publish_retains_prior_set_when_durability_cannot_be_confirmed(monkeypatch):
    # If a durability barrier cannot be completed (for example a filesystem that
    # rejects directory fsync), the prior immutable set must be retained rather
    # than risked: garbage collection only runs after the new set and pointer
    # are durably committed.
    run_a = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    run_b = build_builder(*synthetic_fixture(), code_revision="rev-b").build()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "matchups"
        staging_a = root / "stage_a"
        staging_a.mkdir()
        write_staged_set(staging_a, run_a)
        publish_artifact_set(staging_a, output)

        def broken_fsync(path):
            raise OSError("directory fsync is not supported")

        monkeypatch.setattr(artifact_persistence_module, "_fsync_directory", broken_fsync)
        staging_b = root / "stage_b"
        staging_b.mkdir()
        manifest_b = write_staged_set(staging_b, run_b)
        publish_artifact_set(staging_b, output)

        versioned_root = root / "matchups-runs"
        sets = [entry for entry in versioned_root.iterdir() if entry.is_dir()]
        assert len(sets) == 2, [entry.name for entry in versioned_root.iterdir()]
        verify_persisted_manifest(manifest_b, output)


def test_durability_barriers_fsync_versioned_directory_before_pointer_parent(tmp_path, monkeypatch):
    # Reproduction of the round-8 finding: artifact files and their parent
    # directories were fsynced, but not the installed version directory itself,
    # so the directory entries naming the artifacts could be lost on power loss
    # after the pointer flip. The installed version directory is now fsynced
    # after every artifact file and before the versioned root and the pointer's
    # parent directory.
    run_a = build_builder(*synthetic_fixture(), code_revision="rev-a").build()
    run_b = build_builder(*synthetic_fixture(), code_revision="rev-b").build()
    root = Path(tmp_path)
    output = root / "matchups"
    versioned_root = root / "matchups-runs"
    staging_a = root / "stage_a"
    staging_a.mkdir()
    write_staged_set(staging_a, run_a)
    publish_artifact_set(staging_a, output)

    order = []

    def fsync_path(path):
        order.append(("file", Path(path)))

    def fsync_directory(path):
        order.append(("dir", Path(path)))

    def gc(versioned_root_arg, target):
        order.append(("gc", Path(target)))

    monkeypatch.setattr(artifact_persistence_module, "_fsync_path", fsync_path)
    monkeypatch.setattr(
        artifact_persistence_module, "_fsync_directory", fsync_directory
    )
    monkeypatch.setattr(artifact_persistence_module, "_gc_published_runs", gc)

    staging_b = root / "stage_b"
    staging_b.mkdir()
    write_staged_set(staging_b, run_b)
    publish_artifact_set(staging_b, output)

    versioned_dir = versioned_root / f"{run_b.run_id}~1"
    file_count = sum(1 for kind, _ in order if kind == "file")
    kinds = [kind for kind, _ in order]
    assert kinds == ["file"] * file_count + ["dir", "dir", "dir", "gc"]
    dir_targets = [path for kind, path in order if kind == "dir"]
    assert dir_targets[0] == versioned_dir, "the installed version directory must be fsynced"
    assert dir_targets[1] == versioned_root
    assert dir_targets[2] == root  # the pointer's parent directory
    assert order[-1] == ("gc", output)


def _crafted_manifest(run_id, model_version, file_map):
    return {
        "run_id": run_id,
        "model_version": model_version,
        "stable_subtype_keys": {"100": "0" * 64},
        "provenance": {},
        "artifacts": {
            name: {"file": filename, "sha256": "0" * 64}
            for name, filename in file_map.items()
        },
    }


def _all_canonical_files():
    return dict(PERSISTED_ARTIFACT_FILENAMES)


def test_verify_persisted_manifest_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        crafted = _crafted_manifest(
            "ab12" * 16,
            "cd34" * 16,
            {
                name: f"../outside/{PERSISTED_ARTIFACT_FILENAMES[name]}"
                for name in PERSISTED_ARTIFACT_FILENAMES
            },
        )
        with pytest.raises(ValueError, match="must be persisted as"):
            verify_persisted_manifest(crafted, directory)


def test_verify_persisted_manifest_rejects_absolute_and_non_basename_paths():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for bad_file in ("/etc/passwd", "sub/dir/matchup_summary.csv", "matchup_summary.csv/../../x"):
            crafted = _crafted_manifest(
                "ab12" * 16,
                "cd34" * 16,
                {
                    name: (
                        bad_file if name == "matchup_summary" else PERSISTED_ARTIFACT_FILENAMES[name]
                    )
                    for name in PERSISTED_ARTIFACT_FILENAMES
                },
            )
            with pytest.raises(ValueError, match="must be persisted as"):
                verify_persisted_manifest(crafted, directory)


def test_verify_persisted_manifest_rejects_logical_type_substitution():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = write_full_artifact_set(directory, run)
        base = artifact_manifest(run, paths)
        # PNG logical artifacts substituted with CSV filenames.
        png_as_csv = dict(base)
        png_as_csv["artifacts"] = dict(base["artifacts"])
        png_as_csv["artifacts"]["descriptive_pts_per_min_heatmap"] = {
            "file": "descriptive_pts_per_min_interaction_heatmap.csv",
            "sha256": base["artifacts"]["descriptive_pts_per_min_heatmap"]["sha256"],
        }
        with pytest.raises(ValueError, match="must be persisted as"):
            verify_persisted_manifest(png_as_csv, directory)
        # CSV logical artifacts substituted with PNG filenames.
        csv_as_png = dict(base)
        csv_as_png["artifacts"] = dict(base["artifacts"])
        csv_as_png["artifacts"]["matchup_summary"] = {
            "file": "matchup_summary.png",
            "sha256": base["artifacts"]["matchup_summary"]["sha256"],
        }
        with pytest.raises(ValueError, match="must be persisted as"):
            verify_persisted_manifest(csv_as_png, directory)


def test_verify_persisted_manifest_rejects_wrong_identity_file_for_canonical_name():
    # A canonical-name PNG file whose bytes are really a CSV must be rejected by
    # the file-type binding, not blessed as a substituted artifact.
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        paths = write_full_artifact_set(directory, run)
        manifest = artifact_manifest(run, paths)
        # Rewrite the heatmap PNG with CSV identity bytes under its canonical name.
        csv_path = paths["matchup_summary"]
        (directory / PERSISTED_ARTIFACT_FILENAMES["descriptive_pts_per_min_heatmap"]).write_bytes(
            csv_path.read_bytes()
        )
        crafted = dict(manifest)
        crafted["artifacts"] = dict(manifest["artifacts"])
        crafted["artifacts"]["descriptive_pts_per_min_heatmap"] = {
            "file": PERSISTED_ARTIFACT_FILENAMES["descriptive_pts_per_min_heatmap"],
            "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        }
        with pytest.raises(ValueError, match="Not a PNG"):
            verify_persisted_manifest(crafted, directory)


def test_verify_persisted_manifest_rejects_symlink_escape():
    run = build_run()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory = root / "matchups"
        directory.mkdir()
        outside = root / "outside_secret.csv"
        pd.DataFrame({"a": [1]}).assign(
            RUN_ID=run.run_id, MODEL_VERSION=run.model_version
        ).to_csv(outside, index=False)
        os.symlink(outside, directory / "matchup_summary.csv")
        paths = {name: directory / PERSISTED_ARTIFACT_FILENAMES[name] for name in PERSISTED_ARTIFACT_FILENAMES}
        for name in PERSISTED_ARTIFACT_FILENAMES:
            if name != "matchup_summary":
                if PERSISTED_ARTIFACT_FILENAMES[name].endswith(".csv"):
                    write_attributed_csv(paths[name], run)
                else:
                    write_stamped_png(paths[name], run)
        crafted = _crafted_manifest(
            run.run_id,
            run.model_version,
            {name: PERSISTED_ARTIFACT_FILENAMES[name] for name in PERSISTED_ARTIFACT_FILENAMES},
        )
        crafted["artifacts"]["matchup_summary"]["sha256"] = hashlib.sha256(
            outside.read_bytes()
        ).hexdigest()
        with pytest.raises(ValueError, match="resolves outside"):
            verify_persisted_manifest(crafted, directory)


def test_verify_persisted_manifest_rejects_non_hex_identity_fields():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for field in ("run_id", "model_version"):
            crafted = _crafted_manifest(
                "ab12" * 16, "cd34" * 16, _all_canonical_files()
            )
            crafted[field] = "../etc/passwd"
            with pytest.raises(ValueError, match="hex digest"):
                verify_persisted_manifest(crafted, directory)
        crafted = _crafted_manifest("ab12" * 16, "cd34" * 16, _all_canonical_files())
        crafted["run_id"] = "AB12" * 16
        with pytest.raises(ValueError, match="hex digest"):
            verify_persisted_manifest(crafted, directory)


def _write_temp_analysis_root(tmp_path):
    """A git-backed temp analysis root containing the real ``code_revision``."""
    root = Path(tmp_path) / "analysis"
    root.mkdir()
    sync = Path(tmp_path) / "sync"
    sync.mkdir()
    shutil.copy(SCRIPTS_DIR / "code_revision.py", root / "code_revision.py")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root, sync


def _commit_all(root, message="seed"):
    subprocess.run(["git", "add", "."], cwd=root, check=True)
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
            message,
        ],
        cwd=root,
        check=True,
    )


def _wait_for(path, timeout=45):
    deadline = time.time() + timeout
    while not path.exists():
        if time.time() > deadline:
            raise RuntimeError(f"timed out waiting for {path}")
        time.sleep(0.02)


ENTRY_TEMPLATE = """
import os, pathlib, sys, time
sys.path.insert(0, {root!r})
from code_revision import current_code_revision, verify_loaded_code_matches_disk
ROOT = pathlib.Path(__file__).resolve().parent
SYNC = pathlib.Path({sync!r})
code_revision = os.environ.get("STATSPLUS_ANALYSIS_CODE_REVISION") or current_code_revision(ROOT)
(SYNC / "loaded.marker").write_text("loaded\\n")
deadline = time.time() + 45
while not (SYNC / "proceed").exists():
    if time.time() > deadline: raise RuntimeError("timeout")
    time.sleep(0.02)
verify_loaded_code_matches_disk(ROOT)
print("VERIFY_PASSED")
"""


def _format_root_sync(root, sync, template):
    return template.format(root=str(root), sync=str(sync))

LAUNCHER_TEMPLATE = """
import os
import sys
from pathlib import Path

import code_revision as _code_revision

_LAUNCHER_FILE = Path(__file__).resolve()

if globals().get("__launcher_bootstrapped__") is None:
    globals()["__launcher_bootstrapped__"] = True
    if getattr(globals().get("__spec__"), "name", None) != "run_matchup_analysis":
        raise RuntimeError(
            "The analysis launcher must run as ``python -m run_matchup_analysis`` "
            "so its trusted bootstrap records and executes one code object; "
            "running it as a plain script cannot prove which code ran"
        )
    _bootstrap_source = _LAUNCHER_FILE.read_bytes()
    _bootstrap_code = compile(
        _bootstrap_source,
        str(_LAUNCHER_FILE),
        "exec",
        dont_inherit=True,
        optimize=sys.flags.optimize,
    )
    _code_revision.record_bootstrap_code("run_matchup_analysis", _bootstrap_code)
    exec(_bootstrap_code, globals())
else:
    from code_revision import begin_load_proof, current_code_revision

    ANALYSIS_ROOT = _LAUNCHER_FILE.parent
    begin_load_proof(ANALYSIS_ROOT, launcher_file=_LAUNCHER_FILE)
    os.environ["STATSPLUS_ANALYSIS_CODE_REVISION"] = current_code_revision(ANALYSIS_ROOT)
    import archetype_matchups_2025_26
"""


def _run_launcher(root, env=None):
    """Run the launcher via ``python -m`` from ``root`` so its own loaded code
    is provable; returns the ``Popen`` child."""
    return subprocess.Popen(
        [sys.executable, "-m", "run_matchup_analysis"],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_loaded_code_verification_passes_when_entry_runs_via_launcher(tmp_path):
    root, sync = _write_temp_analysis_root(tmp_path)
    (root / "archetype_matchups_2025_26.py").write_text(
        _format_root_sync(root, sync, ENTRY_TEMPLATE)
    )
    (root / "run_matchup_analysis.py").write_text(LAUNCHER_TEMPLATE)
    _commit_all(root)
    proc = _run_launcher(root)
    _wait_for(sync / "loaded.marker")
    (sync / "proceed").write_text("go")
    out, _ = proc.communicate(timeout=90)
    assert "VERIFY_PASSED" in out, out


def test_loaded_code_verification_fails_when_entry_runs_directly(tmp_path):
    root, sync = _write_temp_analysis_root(tmp_path)
    entry = (
        "import os, pathlib, sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from code_revision import verify_loaded_code_matches_disk\n"
        "ROOT = pathlib.Path(__file__).resolve().parent\n"
        "verify_loaded_code_matches_disk(ROOT)\n"
        "print('VERIFY_PASSED')\n"
    )
    (root / "archetype_matchups_2025_26.py").write_text(entry)
    _commit_all(root)
    proc = subprocess.run(
        [sys.executable, str(root / "archetype_matchups_2025_26.py")],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert "VERIFY_PASSED" not in proc.stdout
    assert "must run through its launcher" in proc.stdout


def test_loaded_code_provenance_race_fails_closed(tmp_path):
    # Reproduction of the bootstrap race: the entry script is already loaded
    # (v1) and its disk source is then edited to v2 before verification. Both
    # a disk-only snapshot pair would agree on v2; the import-time loaded-code
    # proof must still fail closed instead of attributing the run to code it
    # did not load.
    root, sync = _write_temp_analysis_root(tmp_path)
    entry_source = _format_root_sync(root, sync, ENTRY_TEMPLATE)
    (root / "archetype_matchups_2025_26.py").write_text(entry_source)
    (root / "run_matchup_analysis.py").write_text(LAUNCHER_TEMPLATE)
    _commit_all(root)

    proc = _run_launcher(root)
    _wait_for(sync / "loaded.marker")
    (root / "archetype_matchups_2025_26.py").write_text(
        entry_source + "\nMARKER = 'v2'\n"
    )
    (sync / "proceed").write_text("go")
    out, _ = proc.communicate(timeout=90)
    assert "VERIFY_PASSED" not in out
    assert "does not match its current disk source" in out


def test_loaded_code_verification_does_not_need_a_later_cache_read(tmp_path):
    # Import-time in-memory evidence proves the loaded code, so verification
    # never re-reads a shared __pycache__ file after the fact: deleting the
    # entry's bytecode cache after load still verifies.
    root, sync = _write_temp_analysis_root(tmp_path)
    (root / "archetype_matchups_2025_26.py").write_text(
        _format_root_sync(root, sync, ENTRY_TEMPLATE)
    )
    (root / "run_matchup_analysis.py").write_text(LAUNCHER_TEMPLATE)
    _commit_all(root)
    proc = _run_launcher(root)
    _wait_for(sync / "loaded.marker")
    shutil.rmtree(root / "__pycache__", ignore_errors=True)
    (sync / "proceed").write_text("go")
    out, _ = proc.communicate(timeout=90)
    assert "VERIFY_PASSED" in out, out


def test_launcher_loaded_code_proof_fails_closed_on_post_load_edit(tmp_path):
    # Reproduction of the round-7 finding: the launcher was skipped by the
    # verifier, so editing the launcher after load but before revision capture
    # executed v1 while the disk and revision said v2 and verification passed.
    # The launcher's own loaded code is captured at bootstrap and proven against
    # its disk source like every analysis module, so the mixed snapshot fails
    # closed.
    root, sync = _write_temp_analysis_root(tmp_path)
    # The launcher exercises the same trusted bootstrap as production: it reads
    # its own source once, compiles it once, records the exact code object, and
    # then executes that same object, so the attributable body below provably
    # runs from the recorded code and a post-load disk edit fails closed.
    launcher_source = (
        "import os, sys, time, pathlib\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
        "import code_revision as _code_revision\n"
        "_LAUNCHER_FILE = pathlib.Path(__file__).resolve()\n"
        "if globals().get('__launcher_bootstrapped__') is None:\n"
        "    globals()['__launcher_bootstrapped__'] = True\n"
        "    if getattr(globals().get('__spec__'), 'name', None) != 'run_matchup_analysis':\n"
        "        raise RuntimeError(\n"
        "            'The analysis launcher must run as ``python -m run_matchup_analysis`` '\n"
        "            'so its trusted bootstrap records and executes one code object'\n"
        "        )\n"
        "    _bootstrap_source = _LAUNCHER_FILE.read_bytes()\n"
        "    _bootstrap_code = compile(\n"
        "        _bootstrap_source, str(_LAUNCHER_FILE), 'exec',\n"
        "        dont_inherit=True, optimize=sys.flags.optimize,\n"
        "    )\n"
        "    _code_revision.record_bootstrap_code('run_matchup_analysis', _bootstrap_code)\n"
        "    exec(_bootstrap_code, globals())\n"
        "else:\n"
        "    from code_revision import begin_load_proof, current_code_revision\n"
        "    SYNC = pathlib.Path(" + repr(str(sync)) + ")\n"
        "    ROOT = pathlib.Path(__file__).resolve().parent\n"
        "    begin_load_proof(ROOT, launcher_file=os.path.abspath(__file__))\n"
        "    (SYNC / 'launcher_loaded.marker').write_text('x')\n"
        "    deadline = time.time() + 60\n"
        "    while not (SYNC / 'launcher_proceed').exists():\n"
        "        if time.time() > deadline: raise RuntimeError('timeout')\n"
        "        time.sleep(0.02)\n"
        "    os.environ['STATSPLUS_ANALYSIS_CODE_REVISION'] = current_code_revision(ROOT)\n"
        "    import archetype_matchups_2025_26\n"
    )
    (root / "archetype_matchups_2025_26.py").write_text(
        _format_root_sync(root, sync, ENTRY_TEMPLATE)
    )
    (root / "run_matchup_analysis.py").write_text(launcher_source)
    _commit_all(root)

    proc = _run_launcher(root)
    _wait_for(sync / "launcher_loaded.marker")
    (root / "run_matchup_analysis.py").write_text(launcher_source + "\nMARKER = 'v2'\n")
    (sync / "launcher_proceed").write_text("go")
    _wait_for(sync / "loaded.marker")
    (sync / "proceed").write_text("go")
    out, _ = proc.communicate(timeout=90)
    assert "VERIFY_PASSED" not in out
    assert "does not match its current disk source" in out


def test_loaded_code_proof_rejects_replaced_shared_cache(tmp_path):
    # Reproduction of the round-7 finding: the entry is loaded from a crafted
    # bytecode cache whose code differs from disk (v2), then disk and cache are
    # restored to v1 before verification. A later read of the restored shared
    # cache would agree with disk and pass; the import-time, in-process evidence
    # still proves v2 executed, so verification fails closed.
    root, sync = _write_temp_analysis_root(tmp_path)
    entry_v1 = _format_root_sync(root, sync, ENTRY_TEMPLATE)
    entry_v2 = entry_v1.replace(
        '(SYNC / "loaded.marker").write_text("loaded\\n")',
        '(SYNC / "loaded.marker").write_text("v2\\n")',
    )
    assert "v2" in entry_v2 and "loaded" in entry_v2
    (root / "archetype_matchups_2025_26.py").write_text(entry_v1)
    (root / "run_matchup_analysis.py").write_text(LAUNCHER_TEMPLATE)
    _commit_all(root)

    cache_dir = root / "__pycache__"
    cache_dir.mkdir()
    pyc_path = Path(
        importlib.util.cache_from_source(str(root / "archetype_matchups_2025_26.py"))
    )
    tmp_v2 = root / "v2_src.py"
    tmp_v2.write_text(entry_v2)
    py_compile.compile(
        str(tmp_v2),
        cfile=str(pyc_path),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    data = bytearray(pyc_path.read_bytes())
    data[8:16] = importlib.util.source_hash(entry_v1.encode("utf-8"))
    pyc_path.write_bytes(bytes(data))
    tmp_v2.unlink()

    env = dict(os.environ, SOURCE_DATE_EPOCH="0")
    proc = _run_launcher(root, env=env)
    _wait_for(sync / "loaded.marker")
    assert (sync / "loaded.marker").read_text().strip() == "v2"
    py_compile.compile(
        str(root / "archetype_matchups_2025_26.py"),
        cfile=str(pyc_path),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    (sync / "proceed").write_text("go")
    out, _ = proc.communicate(timeout=90)
    assert "VERIFY_PASSED" not in out
    assert "does not match its current disk source" in out


def test_launcher_run_as_plain_script_is_rejected_before_analysis(tmp_path):
    root, sync = _write_temp_analysis_root(tmp_path)
    (root / "archetype_matchups_2025_26.py").write_text(
        _format_root_sync(root, sync, ENTRY_TEMPLATE)
    )
    (root / "run_matchup_analysis.py").write_text(LAUNCHER_TEMPLATE)
    _commit_all(root)
    proc = subprocess.run(
        [sys.executable, str(root / "run_matchup_analysis.py")],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert "VERIFY_PASSED" not in proc.stdout
    assert "python -m run_matchup_analysis" in proc.stdout


def test_code_objects_equal_is_semantic_not_byte_serialization():
    from code_revision import _code_objects_equal, _code_from_source_file

    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.py"
        path.write_text("VALUE = 1\n\ndef f(x):\n    return x + VALUE\n")
        first = _code_from_source_file(path)
        second = _code_from_source_file(path)
        assert _code_objects_equal(first, second)
        path.write_text("VALUE = 1\n\ndef f(x):\n    return x + VALUE\n\n# comment only\n")
        assert _code_objects_equal(first, _code_from_source_file(path))
        path.write_text("VALUE = 2\n\ndef f(x):\n    return x + VALUE\n")
        assert not _code_objects_equal(first, _code_from_source_file(path))


def test_recording_loader_records_and_executes_the_exact_code_object():
    # Reproduction of the round-8 finding: the old loader wrapper called
    # ``get_code`` once to record evidence and then delegated to the wrapped
    # ``exec_module``, which fetched ``get_code`` again — so a cache swap
    # between the two calls could record v1, execute v2, and pass verification.
    # The proof loader now calls ``get_code`` exactly once, records that object,
    # and executes that same object, never invoking the wrapped ``exec_module``.
    import importlib.abc
    import types as _types

    from code_revision import _RecordingLoader

    get_code_calls = []
    versions = iter(
        [
            compile("EXECUTED = 'recorded_v1'\n", "<probe>", "exec"),
            compile("EXECUTED = 'swapped_v2'\n", "<probe>", "exec"),
        ]
    )

    class FakeLoader(importlib.abc.Loader):
        def get_code(self, fullname):
            get_code_calls.append(fullname)
            return next(versions)

        def exec_module(self, module):
            # A second loader fetch with swapped content; must never run.
            exec(compile("EXECUTED = 'wrapped_v2'\n", "<probe>", "exec"), module.__dict__)

    recorded = {}
    module = _types.ModuleType("probe_module")
    _RecordingLoader(
        FakeLoader(), lambda name, code: recorded.__setitem__(name, code)
    ).exec_module(module)

    # Exactly one loader fetch, and the module ran from the recorded object,
    # not from any second (swapped) fetch or from the wrapped loader.
    assert get_code_calls == ["probe_module"]
    assert list(recorded) == ["probe_module"]
    assert recorded["probe_module"].co_code == compile(
        "EXECUTED = 'recorded_v1'\n", "<probe>", "exec"
    ).co_code
    assert module.EXECUTED == "recorded_v1"


def test_begin_load_proof_fails_closed_without_bootstrap_records(tmp_path):
    # Reproduction of the round-8 finding: launcher/code_revision evidence was
    # inferred from the shared bytecode cache after the code was already
    # executing, so a concurrent cache replacement could present v2 cache/disk
    # as what v1 ran. Evidence is now captured by a trusted bootstrap as the
    # immutable process-local code object each module executes, and
    # ``begin_load_proof`` fails closed when that evidence was not recorded
    # before attributable logic began.
    import importlib.util
    import types as _types

    import code_revision as cr

    root = Path(tmp_path) / "analysis"
    root.mkdir()
    saved = dict(cr._LOADED_CODE)
    try:
        cr._LOADED_CODE.pop("code_revision", None)
        with pytest.raises(RuntimeError, match="trusted bootstrap did not record"):
            cr.begin_load_proof(str(root))
    finally:
        cr._LOADED_CODE.clear()
        cr._LOADED_CODE.update(saved)
        cr._PROOF_BEGUN = False

    launcher = root / "run_matchup_analysis.py"
    launcher.write_text("X = 1\n")
    launcher_module = _types.ModuleType("run_matchup_analysis")
    launcher_module.__file__ = str(launcher)
    launcher_module.__spec__ = importlib.util.spec_from_loader(
        "run_matchup_analysis", loader=None
    )
    sys.modules["run_matchup_analysis"] = launcher_module
    try:
        with pytest.raises(RuntimeError, match="trusted bootstrap records"):
            cr.begin_load_proof(str(root), launcher_file=str(launcher))
    finally:
        sys.modules.pop("run_matchup_analysis", None)
        cr._LOADED_CODE.clear()
        cr._LOADED_CODE.update(saved)
        cr._PROOF_BEGUN = False