# %% [markdown]
# # 2025–26 NBA scoring archetype matchup analysis
#
# A single player-fixed-effects model separates overall opponent effects from
# subtype-specific interactions. Points per minute is the primary outcome;
# total points and minutes are retained as context. Two-way clustered uncertainty
# by player and actual game accounts for repeat appearances and teammates.
# Results are descriptive research, not betting advice.
#
# The reusable, deterministic Analysis Run builder lives in
# `scripts/matchup_analysis.py`; this script is the data/IO shell that feeds it
# the membership and game-log frames and renders its artifacts.

# ruff: noqa: E402  # the code-revision capture must precede implementation imports

# %%
import io
import json
from pathlib import Path
import shutil
import time

from code_revision import current_code_revision

# Capture the code snapshot before the implementation modules are imported, so
# the run id is bound to the exact code Python is about to load rather than to
# whatever the disk happens to hold after the (potentially long) data fetch or
# after a later edit. The snapshot is re-checked immediately before publishing,
# and publication aborts if it moved.
code_revision = current_code_revision()

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from nba_api.stats import endpoints

from artifact_persistence import (
    artifact_manifest,
    publish_artifact_set,
    stamp_png_identity,
    verify_persisted_manifest,
)
from matchup_analysis import (
    AnalysisRunBuilder,
    DEFAULT_SETTINGS,
    fail_if_code_changed,
    spec_from_clustering_metadata,
)

pd.set_option("display.max_columns", 100)
sns.set_theme(style="whitegrid", context="notebook")

SEASON = "2025-26"
SEASON_TYPE = "Regular Season"
REFRESH_DATA = False
MAX_CACHE_AGE_DAYS = 2

ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() else Path.cwd()
SEASON_KEY = SEASON.replace("-", "_")
ARCHETYPE_PATH = ROOT / "archetypes_outputs" / SEASON_KEY / "player_archetypes.csv"
FEATURES_PATH = ROOT / "archetypes_outputs" / SEASON_KEY / "player_scoring_features.csv"
# The exact standardized feature matrix the clustering was fit on.
MODEL_MATRIX_PATH = ROOT / "archetypes_outputs" / SEASON_KEY / "player_model_matrix.csv"
CLUSTERING_METADATA_PATH = ROOT / "archetypes_outputs" / SEASON_KEY / "clustering_metadata.json"
CACHE_DIR = ROOT / "archetypes_data" / SEASON_KEY
OUTPUT_DIR = ROOT / "archetypes_outputs" / SEASON_KEY / "matchups"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
GAME_LOG_CACHE = CACHE_DIR / "player_game_logs_regular_season.csv"

print(f"Season: {SEASON} {SEASON_TYPE}")


# %% [markdown]
# ## Load classifications and game logs

# %%
if not ARCHETYPE_PATH.exists():
    raise FileNotFoundError(
        f"Run archetypes_fixed.ipynb first; missing {ARCHETYPE_PATH}"
    )
if not FEATURES_PATH.exists():
    raise FileNotFoundError(
        f"Run archetypes_fixed.ipynb first; missing {FEATURES_PATH}"
    )
if not MODEL_MATRIX_PATH.exists():
    raise FileNotFoundError(
        f"Run archetypes_fixed.ipynb first; missing {MODEL_MATRIX_PATH}"
    )
if not CLUSTERING_METADATA_PATH.exists():
    raise FileNotFoundError(
        f"Run archetypes_fixed.ipynb first; missing {CLUSTERING_METADATA_PATH}"
    )

# ``float_precision="round_trip"`` reads back exactly the values the clustering
# wrote, so the input-data identity both sides compute covers the exact fitted
# matrix rather than a precision-truncated serialization.
archetypes = pd.read_csv(ARCHETYPE_PATH, float_precision="round_trip")
clustering_features = pd.read_csv(MODEL_MATRIX_PATH, float_precision="round_trip")
clustering_metadata = json.loads(CLUSTERING_METADATA_PATH.read_text())


def fetch_game_logs(attempts=3):
    cache_age_days = (
        (time.time() - GAME_LOG_CACHE.stat().st_mtime) / 86_400
        if GAME_LOG_CACHE.exists()
        else np.inf
    )
    if (
        GAME_LOG_CACHE.exists()
        and not REFRESH_DATA
        and cache_age_days <= MAX_CACHE_AGE_DAYS
    ):
        print(f"Using game-log cache ({cache_age_days:.1f} days old)")
        return pd.read_csv(GAME_LOG_CACHE)

    last_error = None
    for attempt in range(attempts):
        try:
            frame = endpoints.PlayerGameLogs(
                season_nullable=SEASON,
                season_type_nullable=SEASON_TYPE,
                timeout=90,
            ).get_data_frames()[0]
            required = {"PLAYER_ID", "TEAM_ABBREVIATION", "GAME_ID", "GAME_DATE", "MATCHUP", "MIN", "PTS"}
            missing = sorted(required - set(frame.columns))
            if missing:
                raise ValueError(f"NBA response is missing columns: {missing}")
            frame.to_csv(GAME_LOG_CACHE, index=False)
            return frame
        except Exception as error:
            last_error = error
            if attempt < attempts - 1:
                time.sleep(2**attempt)
    raise RuntimeError("Could not fetch NBA player game logs") from last_error


game_logs = fetch_game_logs()
print(
    f"Loaded {len(archetypes):,} classified players and "
    f"{len(game_logs):,} player-game rows."
)

# %% [markdown]
# ## Build the analysis run
#
# Membership validation, prepared outcomes, modeling, artifacts, and the
# printed payload are assembled by `AnalysisRunBuilder`, which is exercised by
# `tests/test_nba_archetype_analysis_run.py` against a synthetic fixture. The
# thresholds are the builder's defaults, mirrored nowhere in this script.

# %%
# The model specification is read from the clustering notebook's written
# metadata so the archetype-model identity reflects the clustering that actually
# ran (feature definition, two-stage conditional KMeans, base seed, subtype
# count). The input-data identity is the digest the clustering execution
# recorded over its own membership and exact fitted feature matrix — not a
# digest recomputed from whatever files happen to coexist — so a stale or mixed
# model cannot silently pass the builder's fail-closed guard.
model_spec = spec_from_clustering_metadata(clustering_metadata)
run = AnalysisRunBuilder(
    archetypes=archetypes,
    game_logs=game_logs,
    model_spec=model_spec,
    clustering_features=clustering_features,
    code_revision=code_revision,
).build()

print(f"Analysis Run {run.run_id} of model {run.model_version}")
coverage = run.coverage
print(coverage.to_string())

# %% [markdown]
# ## Printed diagnostics and reliability

# %%
payload = run.dashboard_payload
print(
    pd.Series(payload["diagnostics"]).to_string()
)
print(
    f"Split-half reliability: Pearson {payload['pearson_reliability']:.3f}; "
    f"Spearman {payload['spearman_reliability']:.3f}"
)
print(
    f"Estimated true interaction SD: {payload['tau_ppm']:.4f} PTS/MIN; "
    f"mean shrinkage weight: {payload['mean_shrinkage']:.3f}"
)
print("Validated archetype-specific PTS/MIN interactions:")
print(payload["validated_interactions"].round(3).to_string(index=False))
print("Volume-rate split-half reliability:")
print(payload["volume_reliability_display"].to_string(index=False))
print("Validated archetype-specific volume interactions:")
print(payload["validated_volume_interactions"].round(3).to_string(index=False))

# %% [markdown]
# ## Save auditable outputs

# %%
artifacts = run.artifacts

# The full artifact set is rendered into a private staging directory and only
# swapped into place after the code-race check and a complete set verification,
# so a failure mid-render or a concurrent edit never destroys an
# already-published run's artifact set or leaves a new manifest beside partially
# replaced files. The staging directory is a sibling of the published directory
# so the final swap is an atomic rename.
staging_dir = OUTPUT_DIR.with_name(OUTPUT_DIR.name + ".staging")
if staging_dir.exists():
    shutil.rmtree(staging_dir)
staging_dir.mkdir(parents=True)

summary_path = staging_dir / "matchup_summary.csv"
notable_path = staging_dir / "notable_pts_per_min_matchups.csv"
validated_path = staging_dir / "validated_pts_per_min_interactions.csv"
watchlist_path = staging_dir / "descriptive_pts_per_min_watchlist.csv"
player_relative_path = staging_dir / "player_relative_matchups.csv"
volume_summary_path = staging_dir / "volume_matchup_summary.csv"
validated_volume_path = staging_dir / "validated_volume_interactions.csv"
volume_reliability_path = staging_dir / "volume_split_half_reliability.csv"
player_volume_path = staging_dir / "player_relative_volume_matchups.csv"
# Every persisted CSV carries the run and model identifiers that produced it on
# every row, including an identity row for otherwise-empty outputs, so a later
# reader or the persisted verifier can attribute the file without trusting a
# sibling manifest.
identity_columns = {"RUN_ID": run.run_id, "MODEL_VERSION": run.model_version}


def write_artifact_csv(frame, path):
    attributed = frame.assign(**identity_columns)
    if attributed.empty:
        row = {column: "" for column in attributed.columns}
        row.update(identity_columns)
        attributed = pd.DataFrame([row], columns=attributed.columns)
    attributed.to_csv(path, index=False)


write_artifact_csv(artifacts["matchup_summary"], summary_path)
write_artifact_csv(artifacts["notable_matchups"], notable_path)
write_artifact_csv(artifacts["validated_interactions"], validated_path)
write_artifact_csv(artifacts["watchlist"], watchlist_path)
write_artifact_csv(artifacts["player_relative_matchups"], player_relative_path)
write_artifact_csv(artifacts["volume_matchup_summary"], volume_summary_path)
write_artifact_csv(artifacts["validated_volume_interactions"], validated_volume_path)
write_artifact_csv(artifacts["volume_reliability"], volume_reliability_path)
write_artifact_csv(artifacts["player_relative_volume_matchups"], player_volume_path)


def save_stamped_png(figure, path, **kwargs):
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", **kwargs)
    path.write_bytes(
        stamp_png_identity(buffer.getvalue(), run.run_id, run.model_version)
    )


heatmap_data = artifacts["pts_per_min_heatmap"]
heatmap_limit = payload["pts_per_min_heatmap_limit"]

plt.figure(figsize=(22, 9))
sns.heatmap(
    heatmap_data,
    cmap="vlag",
    center=0,
    vmin=-heatmap_limit,
    vmax=heatmap_limit,
    linewidths=0.25,
    mask=heatmap_data.isna(),
    cbar_kws={"label": "Shrunken interaction (% of league-average PTS/MIN)"},
)
plt.title(f"{SEASON} shrunken PTS/MIN subtype interactions by opponent")
plt.xlabel("Opponent")
plt.ylabel("Scoring subtype")
plt.tight_layout()
descriptive_heatmap_path = (
    staging_dir / "descriptive_pts_per_min_interaction_heatmap.png"
)
save_stamped_png(plt.gcf(), descriptive_heatmap_path, dpi=180, bbox_inches="tight")
plt.show()
plt.close()

fig, axes = plt.subplots(2, 2, figsize=(22, 17))
for axis, (metric, metric_spec) in zip(
    axes.flat, artifacts["volume_heatmaps"].items()
):
    metric_heatmap = metric_spec["data"]
    metric_limit = metric_spec["limit"]
    metric_label = DEFAULT_SETTINGS.volume_metrics[metric]
    sns.heatmap(
        metric_heatmap,
        ax=axis,
        cmap="vlag",
        center=0,
        vmin=-metric_limit,
        vmax=metric_limit,
        linewidths=0.2,
        mask=metric_heatmap.isna(),
        cbar_kws={"label": "% of league-average rate"},
    )
    axis.set_title(f"{metric_label} per minute")
    axis.set_xlabel("Opponent")
    axis.set_ylabel("Scoring subtype")
fig.suptitle(f"{SEASON} shrunken archetype volume interactions", y=1.01)
fig.tight_layout()
volume_heatmap_path = staging_dir / "descriptive_volume_interaction_heatmaps.png"
save_stamped_png(fig, volume_heatmap_path, dpi=180, bbox_inches="tight")
plt.show()
plt.close(fig)

# The run id is bound to the code snapshot captured before the implementation
# modules were imported. The snapshot is re-captured now that every artifact has
# been computed, and publication aborts if it moved: an edit made while the run
# was building must not let newer disk code claim an older run's identity.
fail_if_code_changed(code_revision, current_code_revision())

# Persist one content-addressed identity manifest so every saved artifact is
# attributable to the exact Analysis Run and archetype model that produced it.
# Each file is bound to a SHA-256 digest of its persisted bytes and to its own
# embedded identity, and the staged set is verified before it is published: a
# file that does not carry this run's identity is rejected rather than blessed.
# The manifest must cover exactly the required persisted artifact set, and the
# verified staging directory is swapped in atomically, so an interrupted or
# failed run never leaves the old manifest beside mixed/new files.
saved_paths = {
    "matchup_summary": summary_path,
    "notable_matchups": notable_path,
    "validated_interactions": validated_path,
    "watchlist": watchlist_path,
    "player_relative_matchups": player_relative_path,
    "volume_matchup_summary": volume_summary_path,
    "validated_volume_interactions": validated_volume_path,
    "volume_reliability": volume_reliability_path,
    "player_relative_volume_matchups": player_volume_path,
    "descriptive_pts_per_min_heatmap": descriptive_heatmap_path,
    "descriptive_volume_interaction_heatmaps": volume_heatmap_path,
}
run_manifest = artifact_manifest(run, saved_paths)
verify_persisted_manifest(run_manifest, staging_dir)
(staging_dir / "run_identity_manifest.json").write_text(
    json.dumps(run_manifest, indent=2, sort_keys=True)
)
publish_artifact_set(staging_dir, OUTPUT_DIR)

for name, path in saved_paths.items():
    print(f"Saved {OUTPUT_DIR / Path(path).name}")
print(f"Saved {OUTPUT_DIR / 'run_identity_manifest.json'}")

# %% [markdown]
# ## Interpretation guardrails
#
# - `PPM_TOTAL_EFFECT` is the minute-weighted change in points per minute for a
#   subtype against an opponent relative to that subtype's own 30-opponent
#   average. `PPM_INTERACTION_EFFECT` is the portion beyond that opponent's
#   equally weighted average effect across subtypes; only an interaction
#   supports an archetype-specific matchup claim.
# - `MATCHUP_LEAGUE_INDEX` and `RELATIVE_MATCHUP_LEAGUE_INDEX` use 100 as the
#   full regular-season player-game population's aggregate rate, including
#   players without an archetype assignment. Effect percentages explicitly say
#   whether their denominator is the subtype average or full league average.
# - `RELATIVE_MATCHUP_PPM` adds the estimated subtype-opponent adjustment to a
#   player's own season PTS/MIN. It is a comparison score, not a projection.
#   PTS/MIN is the primary multiplicity-tested outcome.
# - Volume outputs repeat that comparison for FGA/MIN, 2PA/MIN, 3PA/MIN, and
#   FTA/MIN. Their interaction q-values use one joint BY family across all four
#   metrics. Findings must also pass the opponent-invariant fit, exceed 5% of
#   that metric's full-league average, and the heatmaps use shrunken effects.
# - A result is labeled notable only when an opponent-invariant robustness fit,
#   weighted by each player's season-average minutes, also survives BY correction
#   with the same direction. It reuses the same games and is not independent
#   confirmation.
# - Player comparison files omit the player's current team as an opponent. Team
#   is the player's last recorded team, so traded-player rows are a season-end
#   comparison rather than a transaction-date reconstruction.
# - `POINTS_*` and `MINUTES_*` are retained as secondary context only.
# - Full-season subtype assignments and full-season fixed effects contain
#   look-ahead. Prospective use requires rolling/as-of-date model fits.
# - Venue, rest, injuries, role, projected minutes, pace, and the offered line
#   are not controlled and require separate modeling.
# - If split-half reliability and shrinkage weights are near zero, do not price
#   bets from individual descriptive cells even when a raw estimate looks large.