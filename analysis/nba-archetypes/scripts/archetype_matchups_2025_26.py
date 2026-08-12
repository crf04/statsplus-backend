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

# %%
from pathlib import Path
import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from nba_api.stats import endpoints

from matchup_analysis import (
    AnalysisRunBuilder,
    DEFAULT_SETTINGS,
    ArchetypeModelSpec,
    compute_input_data_identity,
)

pd.set_option("display.max_columns", 100)
sns.set_theme(style="whitegrid", context="notebook")

SEASON = "2025-26"
SEASON_TYPE = "Regular Season"
REFRESH_DATA = False
MAX_CACHE_AGE_DAYS = 2
CLUSTERING_METHOD = "KMeans"
RANDOM_STATE = 42
FEATURE_DEFINITION = (
    "play-type and shot-zone composition shares with centered log-ratio, weighted"
)

ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() else Path.cwd()
SEASON_KEY = SEASON.replace("-", "_")
ARCHETYPE_PATH = ROOT / "archetypes_outputs" / SEASON_KEY / "player_archetypes.csv"
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

archetypes = pd.read_csv(ARCHETYPE_PATH)


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
model_spec = ArchetypeModelSpec(
    season=SEASON,
    feature_definition=FEATURE_DEFINITION,
    clustering_method=CLUSTERING_METHOD,
    cluster_count=int(archetypes["SUBTYPE_ID"].nunique()),
    random_seed=RANDOM_STATE,
    input_data_identity=compute_input_data_identity(archetypes, game_logs),
)
run = AnalysisRunBuilder(
    archetypes=archetypes, game_logs=game_logs, model_spec=model_spec
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

summary_path = OUTPUT_DIR / "matchup_summary.csv"
notable_path = OUTPUT_DIR / "notable_pts_per_min_matchups.csv"
validated_path = OUTPUT_DIR / "validated_pts_per_min_interactions.csv"
watchlist_path = OUTPUT_DIR / "descriptive_pts_per_min_watchlist.csv"
player_relative_path = OUTPUT_DIR / "player_relative_matchups.csv"
volume_summary_path = OUTPUT_DIR / "volume_matchup_summary.csv"
validated_volume_path = OUTPUT_DIR / "validated_volume_interactions.csv"
volume_reliability_path = OUTPUT_DIR / "volume_split_half_reliability.csv"
player_volume_path = OUTPUT_DIR / "player_relative_volume_matchups.csv"
artifacts["matchup_summary"].to_csv(summary_path, index=False)
artifacts["notable_matchups"].to_csv(notable_path, index=False)
artifacts["validated_interactions"].to_csv(validated_path, index=False)
artifacts["watchlist"].to_csv(watchlist_path, index=False)
artifacts["player_relative_matchups"].to_csv(player_relative_path, index=False)
artifacts["volume_matchup_summary"].to_csv(volume_summary_path, index=False)
artifacts["validated_volume_interactions"].to_csv(validated_volume_path, index=False)
artifacts["volume_reliability"].to_csv(volume_reliability_path, index=False)
artifacts["player_relative_volume_matchups"].to_csv(player_volume_path, index=False)

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
descriptive_heatmap_path = OUTPUT_DIR / "descriptive_pts_per_min_interaction_heatmap.png"
plt.savefig(descriptive_heatmap_path, dpi=180, bbox_inches="tight")
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
volume_heatmap_path = OUTPUT_DIR / "descriptive_volume_interaction_heatmaps.png"
fig.savefig(volume_heatmap_path, dpi=180, bbox_inches="tight")
plt.show()
plt.close(fig)

for path in [
    summary_path,
    notable_path,
    validated_path,
    watchlist_path,
    player_relative_path,
    volume_summary_path,
    validated_volume_path,
    volume_reliability_path,
    player_volume_path,
    descriptive_heatmap_path,
    volume_heatmap_path,
]:
    print(f"Saved {path}")

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