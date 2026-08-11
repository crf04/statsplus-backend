# %% [markdown]
# # 2025–26 NBA scoring archetype matchup analysis
#
# A single player-fixed-effects model separates overall opponent effects from
# subtype-specific interactions. Points per minute is the primary outcome;
# total points and minutes are retained as context. Two-way clustered uncertainty
# by player and actual game accounts for repeat appearances and teammates.
# Results are descriptive research, not betting advice.

# %%
from pathlib import Path
import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from nba_api.stats import endpoints
from scipy import stats
import statsmodels.api as sm
from statsmodels.stats.sandwich_covariance import cov_cluster_2groups

pd.set_option("display.max_columns", 100)
sns.set_theme(style="whitegrid", context="notebook")

SEASON = "2025-26"
SEASON_TYPE = "Regular Season"
MIN_CELL_PLAYERS = 8
MIN_CELL_GAMES = 20
MIN_CELL_OFFENSIVE_TEAMS = 5
FDR_ALPHA = 0.10
MIN_NOTABLE_PCT = 5.0
MIN_NOTABLE_PTS_PER_MIN = 0.03
VOLUME_METRICS = {
    "FGA": "Field-goal attempts",
    "FG2A": "Two-point attempts",
    "FG3A": "Three-point attempts",
    "FTA": "Free-throw attempts",
}
REFRESH_DATA = False
MAX_CACHE_AGE_DAYS = 2

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

archetypes = pd.read_csv(ARCHETYPE_PATH)[
    ["PLAYER_ID", "PLAYER_NAME", "ARCHETYPE", "SUBTYPE_ID", "SUBTYPE_ARCHETYPE"]
].drop_duplicates("PLAYER_ID")
if archetypes.groupby("SUBTYPE_ID")["SUBTYPE_ARCHETYPE"].nunique().max() != 1:
    raise ValueError("Each SUBTYPE_ID must map to exactly one subtype name")


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
# ## Prepare player-game outcomes
#
# `LOG_POINTS` models total scoring, `LOG_MINUTES` isolates playing-time effects,
# and `LOG_SCORING_RATE` isolates points per minute. A shared `+0.5` scoring
# offset permits scoreless games and makes the decomposition exact in log space.

# %%
required_cols = [
    "PLAYER_ID",
    "TEAM_ABBREVIATION",
    "GAME_ID",
    "GAME_DATE",
    "MATCHUP",
    "MIN",
    "PTS",
    "FGA",
    "FG3A",
    "FTA",
]
missing_cols = sorted(set(required_cols) - set(game_logs.columns))
if missing_cols:
    raise ValueError(f"Game-log response is missing columns: {missing_cols}")

logs = game_logs[required_cols].copy()
missing_minutes_before = logs["MIN"].isna().mean()
logs["MIN"] = pd.to_numeric(logs["MIN"], errors="coerce")
logs["PTS"] = pd.to_numeric(logs["PTS"], errors="coerce")
for metric in ["FGA", "FG3A", "FTA"]:
    logs[metric] = pd.to_numeric(logs[metric], errors="coerce")
logs["FG2A"] = (logs["FGA"] - logs["FG3A"]).clip(lower=0)
if logs[["FGA", "FG2A", "FG3A", "FTA"]].isna().any().any():
    raise ValueError("Volume attempt columns contain missing or non-numeric values")
if logs["MIN"].isna().mean() - missing_minutes_before > 0.01:
    raise ValueError("More than 1% of minute values could not be parsed")

league_rate_sample = logs.loc[(logs["MIN"] > 0) & logs["PTS"].notna()]
league_average_ppm = (
    league_rate_sample["PTS"].sum() / league_rate_sample["MIN"].sum()
)
league_average_volume_rates = {
    metric: league_rate_sample[metric].sum() / league_rate_sample["MIN"].sum()
    for metric in VOLUME_METRICS
}
if not np.isfinite(league_average_ppm) or league_average_ppm <= 0:
    raise ValueError("League-average PTS/MIN could not be calculated")

logs["OPP_TEAM"] = logs["MATCHUP"].str.extract(r"(?:vs\.|@)\s+([A-Z]{3})$")
logs = logs.merge(archetypes, on="PLAYER_ID", how="inner", validate="many_to_one")
logs = logs.loc[
    (logs["MIN"] > 0) & logs["PTS"].notna() & logs["OPP_TEAM"].notna()
].copy()
logs["GAME_DATE"] = pd.to_datetime(logs["GAME_DATE"])
logs["PLAYER_KEY"] = logs["PLAYER_ID"].astype(str)
logs["SUBTYPE_KEY"] = logs["SUBTYPE_ID"].astype(str)
logs = logs.sort_values(["PLAYER_ID", "GAME_DATE", "GAME_ID"])
logs["LOG_POINTS"] = np.log(logs["PTS"] + 0.5)
logs["LOG_MINUTES"] = np.log(logs["MIN"])
logs["LOG_SCORING_RATE"] = np.log((logs["PTS"] + 0.5) / logs["MIN"])
logs["PTS_PER_MIN"] = logs["PTS"] / logs["MIN"]
for metric in VOLUME_METRICS:
    logs[f"{metric}_PER_MIN"] = logs[metric] / logs["MIN"]
logs["PLAYER_AVG_MIN"] = logs.groupby("PLAYER_ID")["MIN"].transform("mean")
if not np.allclose(
    logs["LOG_POINTS"], logs["LOG_MINUTES"] + logs["LOG_SCORING_RATE"]
):
    raise AssertionError("Scoring decomposition is not exact")
if logs.empty:
    raise ValueError("No classified player-games remained after validation")

coverage = pd.Series(
    {
        "classified_players": archetypes["PLAYER_ID"].nunique(),
        "players_with_games": logs["PLAYER_ID"].nunique(),
        "player_games": len(logs),
        "distinct_games": logs["GAME_ID"].nunique(),
        "latest_game_date": logs["GAME_DATE"].max().date(),
        "opponents": logs["OPP_TEAM"].nunique(),
        "subtypes": logs["SUBTYPE_ID"].nunique(),
        "offensive_team_clusters": logs["TEAM_ABBREVIATION"].nunique(),
        "league_average_pts_per_min": league_average_ppm,
    },
    name="value",
)
print(coverage.to_string())

# %% [markdown]
# ## Joint fixed-effects models
#
# Player fixed effects provide each player's baseline. Opponent effects and
# subtype×opponent interactions are estimated jointly, so uncertainty in the
# defense-wide effect carries into every interaction. Standard errors are
# clustered by player and actual game after an explicit within-player transform.

# %%
def matchup_columns(frame):
    opponents = sorted(frame["OPP_TEAM"].unique())
    subtypes = sorted(frame["SUBTYPE_ID"].unique())
    reference_opponent = opponents[-1]
    columns = [
        f"{subtype_id}__{opponent}"
        for subtype_id in subtypes
        for opponent in opponents
        if opponent != reference_opponent
    ]
    return opponents, subtypes, reference_opponent, columns


def fit_model(frame, outcome, weight_column=None):
    opponents, subtypes, reference_opponent, columns = matchup_columns(frame)
    cell_key = frame["SUBTYPE_KEY"] + "__" + frame["OPP_TEAM"]
    design = pd.get_dummies(cell_key, dtype=float).reindex(
        columns=columns, fill_value=0.0
    )
    if weight_column is None:
        model_weights = None
        player_means = design.groupby(frame["PLAYER_KEY"]).transform("mean")
        outcome_means = frame.groupby("PLAYER_KEY")[outcome].transform("mean")
    else:
        model_weights = frame[weight_column].astype(float)
        player_weight_totals = model_weights.groupby(frame["PLAYER_KEY"]).transform(
            "sum"
        )
        player_means = (
            design.mul(model_weights, axis=0)
            .groupby(frame["PLAYER_KEY"])
            .transform("sum")
            .div(player_weight_totals, axis=0)
        )
        outcome_means = (
            (frame[outcome] * model_weights)
            .groupby(frame["PLAYER_KEY"])
            .transform("sum")
            / player_weight_totals
        )
    within_design = design - player_means
    within_outcome = frame[outcome] - outcome_means
    rank = np.linalg.matrix_rank(within_design.to_numpy())
    if rank != len(columns):
        raise ValueError(
            f"Within-player matchup design is rank deficient: {rank}/{len(columns)}"
        )

    if model_weights is None:
        fit = sm.OLS(within_outcome.to_numpy(), within_design.to_numpy()).fit()
    else:
        fit = sm.WLS(
            within_outcome.to_numpy(),
            within_design.to_numpy(),
            weights=model_weights.to_numpy(),
        ).fit()
    covariance, _, _ = cov_cluster_2groups(
        fit,
        frame["PLAYER_ID"].to_numpy(),
        frame["GAME_ID"].to_numpy(),
        use_correction=True,
    )
    # statsmodels sees the within-transformed design but not the absorbed player
    # effects when applying its finite-sample parameter-count correction.
    absorbed_parameters = frame["PLAYER_ID"].nunique() - 1
    visible_residual_df = len(frame) - len(columns)
    full_residual_df = visible_residual_df - absorbed_parameters
    if full_residual_df <= 0:
        raise ValueError("Not enough residual degrees of freedom after player effects")
    covariance *= visible_residual_df / full_residual_df
    return {
        "fit": fit,
        "covariance": covariance,
        "columns": columns,
        "opponents": opponents,
        "subtypes": subtypes,
        "reference_opponent": reference_opponent,
        "degrees_freedom": min(
            frame["PLAYER_ID"].nunique() - 1,
            frame["GAME_ID"].nunique() - 1,
        ),
    }


def contrast_stats(model, contrast):
    beta = model["fit"].params
    covariance = model["covariance"]
    estimate = float(contrast @ beta)
    variance = float(contrast @ covariance @ contrast)
    standard_error = np.sqrt(max(variance, 0.0))
    degrees_freedom = max(model["degrees_freedom"], 1)
    if standard_error > 0:
        statistic = estimate / standard_error
        p_value = 2 * stats.t.sf(abs(statistic), df=degrees_freedom)
        critical = stats.t.ppf(0.95, df=degrees_freedom)
        ci_low = estimate - critical * standard_error
        ci_high = estimate + critical * standard_error
    else:
        p_value = ci_low = ci_high = np.nan
    return estimate, standard_error, ci_low, ci_high, p_value


def extract_effects(model, frame, prefix):
    opponents = model["opponents"]
    subtype_ids = model["subtypes"]
    reference_opponent = model["reference_opponent"]
    column_positions = {
        column: position for position, column in enumerate(model["columns"])
    }
    subtype_names = (
        frame[["SUBTYPE_ID", "SUBTYPE_ARCHETYPE"]]
        .drop_duplicates("SUBTYPE_ID")
        .set_index("SUBTYPE_ID")["SUBTYPE_ARCHETYPE"]
    )
    total_contrasts = {}

    for subtype_id in subtype_ids:
        subtype_positions = [
            column_positions[f"{subtype_id}__{opponent}"]
            for opponent in opponents
            if opponent != reference_opponent
        ]
        for opponent in opponents:
            contrast = np.zeros(len(model["columns"]))
            contrast[subtype_positions] -= 1 / len(opponents)
            if opponent != reference_opponent:
                contrast[column_positions[f"{subtype_id}__{opponent}"]] += 1
            total_contrasts[(subtype_id, opponent)] = contrast

    opponent_main_contrasts = {
        opponent: np.mean(
            [total_contrasts[(subtype_id, opponent)] for subtype_id in subtype_ids],
            axis=0,
        )
        for opponent in opponents
    }

    rows = []
    for subtype_id in subtype_ids:
        for opponent in opponents:
            total_contrast = total_contrasts[(subtype_id, opponent)]
            main_contrast = opponent_main_contrasts[opponent]
            interaction_contrast = total_contrast - main_contrast
            total = contrast_stats(model, total_contrast)
            main = contrast_stats(model, main_contrast)
            interaction = contrast_stats(model, interaction_contrast)
            rows.append(
                {
                    "SUBTYPE_ID": subtype_id,
                    "SUBTYPE_ARCHETYPE": subtype_names[subtype_id],
                    "OPP_TEAM": opponent,
                    f"{prefix}_TOTAL_LOG_EFFECT": total[0],
                    f"{prefix}_TOTAL_EFFECT_PCT": np.expm1(total[0]) * 100,
                    f"{prefix}_TOTAL_SE": total[1],
                    f"{prefix}_TOTAL_CI90_LOW_PCT_UNADJ": np.expm1(total[2]) * 100,
                    f"{prefix}_TOTAL_CI90_HIGH_PCT_UNADJ": np.expm1(total[3]) * 100,
                    f"{prefix}_TOTAL_P_VALUE": total[4],
                    f"{prefix}_OPP_MAIN_EFFECT_PCT": np.expm1(main[0]) * 100,
                    f"{prefix}_INTERACTION_LOG_EFFECT": interaction[0],
                    f"{prefix}_INTERACTION_EFFECT_PCT": np.expm1(interaction[0]) * 100,
                    f"{prefix}_INTERACTION_SE": interaction[1],
                    f"{prefix}_INTERACTION_CI90_LOW_PCT_UNADJ": np.expm1(interaction[2]) * 100,
                    f"{prefix}_INTERACTION_CI90_HIGH_PCT_UNADJ": np.expm1(interaction[3]) * 100,
                    f"{prefix}_INTERACTION_P_VALUE": interaction[4],
                }
            )
    return pd.DataFrame(rows)


def extract_linear_effects(model, frame, prefix):
    opponents = model["opponents"]
    subtype_ids = model["subtypes"]
    reference_opponent = model["reference_opponent"]
    column_positions = {
        column: position for position, column in enumerate(model["columns"])
    }
    subtype_names = (
        frame[["SUBTYPE_ID", "SUBTYPE_ARCHETYPE"]]
        .drop_duplicates("SUBTYPE_ID")
        .set_index("SUBTYPE_ID")["SUBTYPE_ARCHETYPE"]
    )
    total_contrasts = {}

    for subtype_id in subtype_ids:
        subtype_positions = [
            column_positions[f"{subtype_id}__{opponent}"]
            for opponent in opponents
            if opponent != reference_opponent
        ]
        for opponent in opponents:
            contrast = np.zeros(len(model["columns"]))
            contrast[subtype_positions] -= 1 / len(opponents)
            if opponent != reference_opponent:
                contrast[column_positions[f"{subtype_id}__{opponent}"]] += 1
            total_contrasts[(subtype_id, opponent)] = contrast

    opponent_main_contrasts = {
        opponent: np.mean(
            [total_contrasts[(subtype_id, opponent)] for subtype_id in subtype_ids],
            axis=0,
        )
        for opponent in opponents
    }

    rows = []
    for subtype_id in subtype_ids:
        for opponent in opponents:
            total_contrast = total_contrasts[(subtype_id, opponent)]
            main_contrast = opponent_main_contrasts[opponent]
            interaction_contrast = total_contrast - main_contrast
            total = contrast_stats(model, total_contrast)
            main = contrast_stats(model, main_contrast)
            interaction = contrast_stats(model, interaction_contrast)
            rows.append(
                {
                    "SUBTYPE_ID": subtype_id,
                    "SUBTYPE_ARCHETYPE": subtype_names[subtype_id],
                    "OPP_TEAM": opponent,
                    f"{prefix}_TOTAL_EFFECT": total[0],
                    f"{prefix}_TOTAL_SE": total[1],
                    f"{prefix}_TOTAL_CI90_LOW_UNADJ": total[2],
                    f"{prefix}_TOTAL_CI90_HIGH_UNADJ": total[3],
                    f"{prefix}_TOTAL_P_VALUE": total[4],
                    f"{prefix}_OPP_MAIN_EFFECT": main[0],
                    f"{prefix}_INTERACTION_EFFECT": interaction[0],
                    f"{prefix}_INTERACTION_SE": interaction[1],
                    f"{prefix}_INTERACTION_CI90_LOW_UNADJ": interaction[2],
                    f"{prefix}_INTERACTION_CI90_HIGH_UNADJ": interaction[3],
                    f"{prefix}_INTERACTION_P_VALUE": interaction[4],
                }
            )
    return pd.DataFrame(rows)


points_fit = fit_model(logs, "LOG_POINTS")
minutes_fit = fit_model(logs, "LOG_MINUTES")
rate_fit = fit_model(logs, "LOG_SCORING_RATE")
ppm_fit = fit_model(logs, "PTS_PER_MIN", weight_column="MIN")
ppm_constant_weight_fit = fit_model(
    logs, "PTS_PER_MIN", weight_column="PLAYER_AVG_MIN"
)

points_effects = extract_effects(points_fit, logs, "POINTS")
minutes_effects = extract_effects(minutes_fit, logs, "MINUTES")
rate_effects = extract_effects(rate_fit, logs, "RATE")
ppm_effects = extract_linear_effects(ppm_fit, logs, "PPM")
ppm_constant_weight_effects = extract_linear_effects(
    ppm_constant_weight_fit, logs, "PPM_CONSTWT"
)
volume_effect_frames = []
for metric, metric_label in VOLUME_METRICS.items():
    primary_fit = fit_model(logs, f"{metric}_PER_MIN", weight_column="MIN")
    sensitivity_fit = fit_model(
        logs, f"{metric}_PER_MIN", weight_column="PLAYER_AVG_MIN"
    )
    primary_effects = extract_linear_effects(primary_fit, logs, "VOLUME")
    sensitivity_effects = extract_linear_effects(
        sensitivity_fit, logs, "VOLUME_CONSTWT"
    )
    volume_effect_frames.append(
        primary_effects.merge(
            sensitivity_effects,
            on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
            validate="one_to_one",
        ).assign(METRIC=metric, METRIC_LABEL=metric_label)
    )
volume_matchup_summary = pd.concat(volume_effect_frames, ignore_index=True)
matchup_summary = points_effects.merge(
    minutes_effects,
    on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
    validate="one_to_one",
).merge(
    rate_effects,
    on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
    validate="one_to_one",
).merge(
    ppm_effects,
    on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
    validate="one_to_one",
).merge(
    ppm_constant_weight_effects,
    on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
    validate="one_to_one",
)

# %% [markdown]
# ## Sample sizes, multiplicity correction, and reliability

# %%
sample_sizes = (
    logs.groupby(["SUBTYPE_ID", "OPP_TEAM"], as_index=False)
    .agg(
        PLAYERS=("PLAYER_ID", "nunique"),
        PLAYER_GAMES=("PLAYER_ID", "size"),
        DISTINCT_GAMES=("GAME_ID", "nunique"),
        OFFENSIVE_TEAMS=("TEAM_ABBREVIATION", "nunique"),
        MEAN_PTS=("PTS", "mean"),
        TOTAL_PTS=("PTS", "sum"),
        TOTAL_MINUTES=("MIN", "sum"),
    )
)
sample_sizes["MEAN_PTS_PER_MIN"] = (
    sample_sizes["TOTAL_PTS"] / sample_sizes["TOTAL_MINUTES"]
)
subtype_mean_points = logs.groupby("SUBTYPE_ID")["PTS"].mean()
subtype_mean_ppm = (
    logs.groupby("SUBTYPE_ID")["PTS"].sum()
    / logs.groupby("SUBTYPE_ID")["MIN"].sum()
)
subtype_geometric_shifted_points = np.exp(
    logs.groupby("SUBTYPE_ID")["LOG_POINTS"].mean()
)
matchup_summary = matchup_summary.merge(
    sample_sizes,
    on=["SUBTYPE_ID", "OPP_TEAM"],
    how="left",
    validate="one_to_one",
)
matchup_summary["TYPICAL_TOTAL_POINTS_LIFT"] = (
    matchup_summary["SUBTYPE_ID"].map(subtype_geometric_shifted_points)
    * np.expm1(matchup_summary["POINTS_TOTAL_LOG_EFFECT"])
)
matchup_summary["TYPICAL_INTERACTION_POINTS"] = (
    matchup_summary["SUBTYPE_ID"].map(subtype_geometric_shifted_points)
    * np.expm1(matchup_summary["POINTS_INTERACTION_LOG_EFFECT"])
)
matchup_summary["POINTS_TOTAL_POINTS_SCALE_PCT"] = (
    matchup_summary["TYPICAL_TOTAL_POINTS_LIFT"]
    / matchup_summary["SUBTYPE_ID"].map(subtype_mean_points)
    * 100
)
matchup_summary["POINTS_INTERACTION_POINTS_SCALE_PCT"] = (
    matchup_summary["TYPICAL_INTERACTION_POINTS"]
    / matchup_summary["SUBTYPE_ID"].map(subtype_mean_points)
    * 100
)
matchup_summary["LEAGUE_AVG_PPM"] = league_average_ppm
matchup_summary["SUBTYPE_AVG_PPM"] = matchup_summary["SUBTYPE_ID"].map(
    subtype_mean_ppm
)
matchup_summary["PPM_TOTAL_EFFECT_VS_SUBTYPE_AVG_PCT"] = (
    matchup_summary["PPM_TOTAL_EFFECT"]
    / matchup_summary["SUBTYPE_AVG_PPM"]
    * 100
)
matchup_summary["PPM_INTERACTION_EFFECT_VS_SUBTYPE_AVG_PCT"] = (
    matchup_summary["PPM_INTERACTION_EFFECT"]
    / matchup_summary["SUBTYPE_AVG_PPM"]
    * 100
)
matchup_summary["PPM_TOTAL_EFFECT_VS_LEAGUE_AVG_PCT"] = (
    matchup_summary["PPM_TOTAL_EFFECT"] / league_average_ppm * 100
)
matchup_summary["PPM_INTERACTION_EFFECT_VS_LEAGUE_AVG_PCT"] = (
    matchup_summary["PPM_INTERACTION_EFFECT"] / league_average_ppm * 100
)
matchup_summary["MATCHUP_ADJUSTED_SUBTYPE_PPM"] = (
    matchup_summary["SUBTYPE_AVG_PPM"] + matchup_summary["PPM_TOTAL_EFFECT"]
)
matchup_summary["MATCHUP_VS_LEAGUE_PPM"] = (
    matchup_summary["MATCHUP_ADJUSTED_SUBTYPE_PPM"] - league_average_ppm
)
matchup_summary["MATCHUP_VS_LEAGUE_PCT"] = (
    matchup_summary["MATCHUP_VS_LEAGUE_PPM"] / league_average_ppm * 100
)
matchup_summary["MATCHUP_LEAGUE_INDEX"] = (
    matchup_summary["MATCHUP_ADJUSTED_SUBTYPE_PPM"] / league_average_ppm * 100
)
matchup_summary["ELIGIBLE_FOR_INFERENCE"] = (
    (matchup_summary["PLAYERS"] >= MIN_CELL_PLAYERS)
    & (matchup_summary["DISTINCT_GAMES"] >= MIN_CELL_GAMES)
    & (matchup_summary["OFFENSIVE_TEAMS"] >= MIN_CELL_OFFENSIVE_TEAMS)
    & matchup_summary["PPM_INTERACTION_P_VALUE"].notna()
)


def benjamini_yekutieli(p_values):
    p_values = np.asarray(p_values, dtype=float)
    result = np.full_like(p_values, np.nan)
    finite = np.isfinite(p_values)
    finite_values = p_values[finite]
    if len(finite_values) == 0:
        return result
    order = np.argsort(finite_values)
    ranked = finite_values[order]
    harmonic = np.sum(1 / np.arange(1, len(ranked) + 1))
    adjusted = ranked * len(ranked) * harmonic / np.arange(1, len(ranked) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    finite_result = np.empty_like(adjusted)
    finite_result[order] = np.clip(adjusted, 0, 1)
    result[finite] = finite_result
    return result


# Volume-rate comparisons use one joint testing family across all four metrics.
volume_matchup_summary = volume_matchup_summary.merge(
    sample_sizes[
        [
            "SUBTYPE_ID",
            "OPP_TEAM",
            "PLAYERS",
            "PLAYER_GAMES",
            "DISTINCT_GAMES",
            "OFFENSIVE_TEAMS",
        ]
    ],
    on=["SUBTYPE_ID", "OPP_TEAM"],
    how="left",
    validate="many_to_one",
)
subtype_volume_rates = {
    metric: (
        logs.groupby("SUBTYPE_ID")[metric].sum()
        / logs.groupby("SUBTYPE_ID")["MIN"].sum()
    )
    for metric in VOLUME_METRICS
}
volume_matchup_summary["LEAGUE_AVG_RATE"] = volume_matchup_summary["METRIC"].map(
    league_average_volume_rates
)
volume_matchup_summary["SUBTYPE_AVG_RATE"] = [
    subtype_volume_rates[metric].loc[subtype_id]
    for metric, subtype_id in zip(
        volume_matchup_summary["METRIC"], volume_matchup_summary["SUBTYPE_ID"]
    )
]
volume_matchup_summary["TOTAL_EFFECT_VS_LEAGUE_AVG_PCT"] = (
    volume_matchup_summary["VOLUME_TOTAL_EFFECT"]
    / volume_matchup_summary["LEAGUE_AVG_RATE"]
    * 100
)
volume_matchup_summary["INTERACTION_EFFECT_VS_LEAGUE_AVG_PCT"] = (
    volume_matchup_summary["VOLUME_INTERACTION_EFFECT"]
    / volume_matchup_summary["LEAGUE_AVG_RATE"]
    * 100
)
volume_matchup_summary["MATCHUP_ADJUSTED_RATE"] = (
    volume_matchup_summary["SUBTYPE_AVG_RATE"]
    + volume_matchup_summary["VOLUME_TOTAL_EFFECT"]
)
volume_matchup_summary["MATCHUP_LEAGUE_INDEX"] = (
    volume_matchup_summary["MATCHUP_ADJUSTED_RATE"]
    / volume_matchup_summary["LEAGUE_AVG_RATE"]
    * 100
)
volume_matchup_summary["ELIGIBLE_FOR_INFERENCE"] = (
    (volume_matchup_summary["PLAYERS"] >= MIN_CELL_PLAYERS)
    & (volume_matchup_summary["DISTINCT_GAMES"] >= MIN_CELL_GAMES)
    & (volume_matchup_summary["OFFENSIVE_TEAMS"] >= MIN_CELL_OFFENSIVE_TEAMS)
    & volume_matchup_summary["VOLUME_INTERACTION_P_VALUE"].notna()
)
volume_eligible = volume_matchup_summary["ELIGIBLE_FOR_INFERENCE"]
for q_column, p_column in [
    ("INTERACTION_Q_BY", "VOLUME_INTERACTION_P_VALUE"),
    ("TOTAL_Q_BY", "VOLUME_TOTAL_P_VALUE"),
    ("CONSTWT_INTERACTION_Q_BY", "VOLUME_CONSTWT_INTERACTION_P_VALUE"),
    ("CONSTWT_TOTAL_Q_BY", "VOLUME_CONSTWT_TOTAL_P_VALUE"),
]:
    volume_matchup_summary[q_column] = np.nan
    volume_matchup_summary.loc[volume_eligible, q_column] = benjamini_yekutieli(
        volume_matchup_summary.loc[volume_eligible, p_column]
    )
volume_matchup_summary["INTERACTION_SENSITIVITY_SUPPORT"] = (
    (volume_matchup_summary["CONSTWT_INTERACTION_Q_BY"] <= FDR_ALPHA)
    & (
        np.sign(volume_matchup_summary["VOLUME_INTERACTION_EFFECT"])
        == np.sign(volume_matchup_summary["VOLUME_CONSTWT_INTERACTION_EFFECT"])
    )
)
volume_matchup_summary["TOTAL_SENSITIVITY_SUPPORT"] = (
    (volume_matchup_summary["CONSTWT_TOTAL_Q_BY"] <= FDR_ALPHA)
    & (
        np.sign(volume_matchup_summary["VOLUME_TOTAL_EFFECT"])
        == np.sign(volume_matchup_summary["VOLUME_CONSTWT_TOTAL_EFFECT"])
    )
)
volume_matchup_summary["INTERACTION_NOTABLE"] = (
    volume_eligible
    & (volume_matchup_summary["INTERACTION_Q_BY"] <= FDR_ALPHA)
    & volume_matchup_summary["INTERACTION_SENSITIVITY_SUPPORT"]
    & (
        volume_matchup_summary["INTERACTION_EFFECT_VS_LEAGUE_AVG_PCT"].abs()
        >= MIN_NOTABLE_PCT
    )
)
volume_matchup_summary["TOTAL_RATE_NOTABLE"] = (
    volume_eligible
    & (volume_matchup_summary["TOTAL_Q_BY"] <= FDR_ALPHA)
    & volume_matchup_summary["TOTAL_SENSITIVITY_SUPPORT"]
    & (
        volume_matchup_summary["TOTAL_EFFECT_VS_LEAGUE_AVG_PCT"].abs()
        >= MIN_NOTABLE_PCT
    )
)
volume_matchup_summary["SHRINKAGE_WEIGHT"] = np.nan
volume_matchup_summary["SHRUNK_INTERACTION_EFFECT"] = np.nan
volume_matchup_summary["SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"] = np.nan
volume_tau_rows = []
for metric, metric_frame in volume_matchup_summary.loc[volume_eligible].groupby(
    "METRIC"
):
    metric_index = metric_frame.index
    metric_variance = metric_frame["VOLUME_INTERACTION_SE"] ** 2
    metric_mean = metric_frame["VOLUME_INTERACTION_EFFECT"].mean()
    metric_tau_squared = max(
        metric_frame["VOLUME_INTERACTION_EFFECT"].var(ddof=1)
        - metric_variance.mean(),
        0.0,
    )
    metric_weight = metric_tau_squared / (metric_tau_squared + metric_variance)
    metric_shrunk = metric_mean + metric_weight * (
        metric_frame["VOLUME_INTERACTION_EFFECT"] - metric_mean
    )
    volume_matchup_summary.loc[metric_index, "SHRINKAGE_WEIGHT"] = metric_weight
    volume_matchup_summary.loc[
        metric_index, "SHRUNK_INTERACTION_EFFECT"
    ] = metric_shrunk
    volume_matchup_summary.loc[
        metric_index, "SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"
    ] = (metric_shrunk / league_average_volume_rates[metric] * 100)
    volume_tau_rows.append(
        {
            "METRIC": metric,
            "TRUE_INTERACTION_SD": np.sqrt(metric_tau_squared),
            "MEAN_SHRINKAGE_WEIGHT": metric_weight.mean(),
        }
    )
volume_shrinkage_summary = pd.DataFrame(volume_tau_rows)

eligible = matchup_summary["ELIGIBLE_FOR_INFERENCE"]
matchup_summary["PPM_INTERACTION_Q_BY"] = np.nan
matchup_summary["PPM_TOTAL_Q_BY"] = np.nan
matchup_summary["PPM_CONSTWT_INTERACTION_Q_BY"] = np.nan
matchup_summary["PPM_CONSTWT_TOTAL_Q_BY"] = np.nan
matchup_summary.loc[eligible, "PPM_INTERACTION_Q_BY"] = benjamini_yekutieli(
    matchup_summary.loc[eligible, "PPM_INTERACTION_P_VALUE"]
)
matchup_summary.loc[eligible, "PPM_TOTAL_Q_BY"] = benjamini_yekutieli(
    matchup_summary.loc[eligible, "PPM_TOTAL_P_VALUE"]
)
matchup_summary.loc[eligible, "PPM_CONSTWT_INTERACTION_Q_BY"] = (
    benjamini_yekutieli(
        matchup_summary.loc[eligible, "PPM_CONSTWT_INTERACTION_P_VALUE"]
    )
)
matchup_summary.loc[eligible, "PPM_CONSTWT_TOTAL_Q_BY"] = benjamini_yekutieli(
    matchup_summary.loc[eligible, "PPM_CONSTWT_TOTAL_P_VALUE"]
)
matchup_summary["INTERACTION_SENSITIVITY_SUPPORT"] = (
    (matchup_summary["PPM_CONSTWT_INTERACTION_Q_BY"] <= FDR_ALPHA)
    & (
        np.sign(matchup_summary["PPM_INTERACTION_EFFECT"])
        == np.sign(matchup_summary["PPM_CONSTWT_INTERACTION_EFFECT"])
    )
)
matchup_summary["TOTAL_SENSITIVITY_SUPPORT"] = (
    (matchup_summary["PPM_CONSTWT_TOTAL_Q_BY"] <= FDR_ALPHA)
    & (
        np.sign(matchup_summary["PPM_TOTAL_EFFECT"])
        == np.sign(matchup_summary["PPM_CONSTWT_TOTAL_EFFECT"])
    )
)
matchup_summary["INTERACTION_NOTABLE"] = (
    eligible
    & (matchup_summary["PPM_INTERACTION_Q_BY"] <= FDR_ALPHA)
    & matchup_summary["INTERACTION_SENSITIVITY_SUPPORT"]
    & (
        matchup_summary["PPM_INTERACTION_EFFECT_VS_LEAGUE_AVG_PCT"].abs()
        >= MIN_NOTABLE_PCT
    )
    & (matchup_summary["PPM_INTERACTION_EFFECT"].abs() >= MIN_NOTABLE_PTS_PER_MIN)
)
matchup_summary["TOTAL_RATE_NOTABLE"] = (
    eligible
    & (matchup_summary["PPM_TOTAL_Q_BY"] <= FDR_ALPHA)
    & matchup_summary["TOTAL_SENSITIVITY_SUPPORT"]
    & (
        matchup_summary["PPM_TOTAL_EFFECT_VS_LEAGUE_AVG_PCT"].abs()
        >= MIN_NOTABLE_PCT
    )
    & (matchup_summary["PPM_TOTAL_EFFECT"].abs() >= MIN_NOTABLE_PTS_PER_MIN)
)
matchup_summary["INTERACTION_DIRECTION"] = "Neutral"
matchup_summary.loc[
    matchup_summary["INTERACTION_NOTABLE"]
    & (matchup_summary["PPM_INTERACTION_EFFECT"] > 0),
    "INTERACTION_DIRECTION",
] = "Interaction boost"
matchup_summary.loc[
    matchup_summary["INTERACTION_NOTABLE"]
    & (matchup_summary["PPM_INTERACTION_EFFECT"] < 0),
    "INTERACTION_DIRECTION",
] = "Interaction suppression"
matchup_summary["TOTAL_DIRECTION"] = "Neutral"
matchup_summary.loc[
    matchup_summary["TOTAL_RATE_NOTABLE"]
    & (matchup_summary["PPM_TOTAL_EFFECT"] > 0),
    "TOTAL_DIRECTION",
] = "PTS/MIN boost"
matchup_summary.loc[
    matchup_summary["TOTAL_RATE_NOTABLE"]
    & (matchup_summary["PPM_TOTAL_EFFECT"] < 0),
    "TOTAL_DIRECTION",
] = "PTS/MIN suppression"

# Normal-means shrinkage of PTS/MIN interaction effects for descriptive ranking.
interaction_variance = matchup_summary.loc[eligible, "PPM_INTERACTION_SE"] ** 2
interaction_mean = matchup_summary.loc[eligible, "PPM_INTERACTION_EFFECT"].mean()
tau_squared = max(
    matchup_summary.loc[eligible, "PPM_INTERACTION_EFFECT"].var(ddof=1)
    - interaction_variance.mean(),
    0.0,
)
matchup_summary["SHRINKAGE_WEIGHT"] = np.nan
matchup_summary.loc[eligible, "SHRINKAGE_WEIGHT"] = (
    tau_squared / (tau_squared + interaction_variance)
)
matchup_summary["SHRUNK_INTERACTION_PPM"] = (
    interaction_mean
    + matchup_summary["SHRINKAGE_WEIGHT"]
    * (matchup_summary["PPM_INTERACTION_EFFECT"] - interaction_mean)
)
matchup_summary["SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"] = (
    matchup_summary["SHRUNK_INTERACTION_PPM"] / league_average_ppm * 100
)

# Independent half-season fits provide a descriptive reliability check.
median_game_date = (
    logs[["GAME_ID", "GAME_DATE"]].drop_duplicates("GAME_ID")["GAME_DATE"].median()
)
half_effects = []
for label, half in [
    ("FIRST_HALF", logs.loc[logs["GAME_DATE"] <= median_game_date]),
    ("SECOND_HALF", logs.loc[logs["GAME_DATE"] > median_game_date]),
]:
    half_fit = fit_model(half, "PTS_PER_MIN", weight_column="MIN")
    half_result = extract_linear_effects(half_fit, half, "PPM")
    half_effects.append(
        half_result[
            [
                "SUBTYPE_ID",
                "SUBTYPE_ARCHETYPE",
                "OPP_TEAM",
                "PPM_INTERACTION_EFFECT",
            ]
        ].rename(
            columns={
                "PPM_INTERACTION_EFFECT": f"INTERACTION_PPM_{label}"
            }
        )
    )
matchup_summary = matchup_summary.merge(
    half_effects[0],
    on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
    how="left",
    validate="one_to_one",
).merge(
    half_effects[1],
    on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
    how="left",
    validate="one_to_one",
)
eligible_halves = matchup_summary.loc[
    eligible,
    ["INTERACTION_PPM_FIRST_HALF", "INTERACTION_PPM_SECOND_HALF"],
].dropna()
pearson_reliability = (
    eligible_halves.corr(method="pearson").iloc[0, 1]
    if len(eligible_halves) >= 2
    else np.nan
)
spearman_reliability = (
    eligible_halves.corr(method="spearman").iloc[0, 1]
    if len(eligible_halves) >= 2
    else np.nan
)

volume_half_effects = []
for label, half in [
    ("FIRST_HALF", logs.loc[logs["GAME_DATE"] <= median_game_date]),
    ("SECOND_HALF", logs.loc[logs["GAME_DATE"] > median_game_date]),
]:
    metric_half_effects = []
    for metric, metric_label in VOLUME_METRICS.items():
        half_fit = fit_model(
            half, f"{metric}_PER_MIN", weight_column="MIN"
        )
        half_result = extract_linear_effects(half_fit, half, "VOLUME")
        metric_half_effects.append(
            half_result[
                [
                    "SUBTYPE_ID",
                    "SUBTYPE_ARCHETYPE",
                    "OPP_TEAM",
                    "VOLUME_INTERACTION_EFFECT",
                ]
            ].assign(METRIC=metric, METRIC_LABEL=metric_label)
        )
    volume_half_effects.append(
        pd.concat(metric_half_effects, ignore_index=True).rename(
            columns={
                "VOLUME_INTERACTION_EFFECT": f"INTERACTION_RATE_{label}"
            }
        )
    )
volume_matchup_summary = volume_matchup_summary.merge(
    volume_half_effects[0],
    on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM", "METRIC", "METRIC_LABEL"],
    how="left",
    validate="one_to_one",
).merge(
    volume_half_effects[1],
    on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM", "METRIC", "METRIC_LABEL"],
    how="left",
    validate="one_to_one",
)
volume_reliability_rows = []
for metric, metric_frame in volume_matchup_summary.loc[
    volume_matchup_summary["ELIGIBLE_FOR_INFERENCE"]
].groupby("METRIC"):
    paired = metric_frame[
        ["INTERACTION_RATE_FIRST_HALF", "INTERACTION_RATE_SECOND_HALF"]
    ].dropna()
    volume_reliability_rows.append(
        {
            "METRIC": metric,
            "CELLS": len(paired),
            "PEARSON": paired.corr(method="pearson").iloc[0, 1],
            "SPEARMAN": paired.corr(method="spearman").iloc[0, 1],
        }
    )
volume_reliability = pd.DataFrame(volume_reliability_rows)
volume_reliability = volume_reliability.merge(
    volume_shrinkage_summary, on="METRIC", validate="one_to_one"
)
validated_volume_interactions = volume_matchup_summary.loc[
    volume_matchup_summary["INTERACTION_NOTABLE"]
].sort_values(["METRIC", "INTERACTION_Q_BY"])

notable_matchups = matchup_summary.loc[
    matchup_summary["INTERACTION_NOTABLE"] | matchup_summary["TOTAL_RATE_NOTABLE"]
].sort_values(
    ["INTERACTION_NOTABLE", "PPM_INTERACTION_Q_BY"],
    ascending=[False, True],
)
validated_interactions = matchup_summary.loc[
    matchup_summary["INTERACTION_NOTABLE"]
].sort_values("PPM_INTERACTION_Q_BY")
descriptive_watchlist = (
    matchup_summary.loc[eligible]
    .assign(
        ABS_SHRUNK=lambda frame: frame[
            "SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"
        ].abs()
    )
    .sort_values(["ABS_SHRUNK", "OFFENSIVE_TEAMS"], ascending=[False, False])
    .head(25)
    .drop(columns="ABS_SHRUNK")
)

tau_ppm = np.sqrt(tau_squared)
mean_shrinkage = matchup_summary.loc[eligible, "SHRINKAGE_WEIGHT"].mean()
print(
    pd.Series(
        {
            "eligible_cells": int(eligible.sum()),
            "interaction_findings": int(matchup_summary["INTERACTION_NOTABLE"].sum()),
            "total_rate_findings": int(matchup_summary["TOTAL_RATE_NOTABLE"].sum()),
            "split_half_cells": len(eligible_halves),
        }
    ).to_string()
)
print(
    f"Split-half reliability: Pearson {pearson_reliability:.3f}; "
    f"Spearman {spearman_reliability:.3f}"
)
print(
    f"Estimated true interaction SD: {tau_ppm:.4f} PTS/MIN; "
    f"mean shrinkage weight: {mean_shrinkage:.3f}"
)
notable_display_columns = [
    "SUBTYPE_ARCHETYPE",
    "OPP_TEAM",
    "PPM_TOTAL_EFFECT",
    "PPM_TOTAL_EFFECT_VS_LEAGUE_AVG_PCT",
    "MATCHUP_ADJUSTED_SUBTYPE_PPM",
    "MATCHUP_LEAGUE_INDEX",
    "PPM_TOTAL_Q_BY",
    "PPM_INTERACTION_EFFECT",
    "PPM_INTERACTION_EFFECT_VS_LEAGUE_AVG_PCT",
    "PPM_INTERACTION_Q_BY",
    "PPM_CONSTWT_INTERACTION_EFFECT",
    "PPM_CONSTWT_INTERACTION_Q_BY",
    "MINUTES_TOTAL_EFFECT_PCT",
    "TOTAL_DIRECTION",
    "INTERACTION_DIRECTION",
]
print("Validated archetype-specific PTS/MIN interactions:")
print(validated_interactions[notable_display_columns].round(3).to_string(index=False))
print("Volume-rate split-half reliability:")
print(volume_reliability.round(3).to_string(index=False))
volume_display_columns = [
    "METRIC",
    "SUBTYPE_ARCHETYPE",
    "OPP_TEAM",
    "LEAGUE_AVG_RATE",
    "MATCHUP_ADJUSTED_RATE",
    "MATCHUP_LEAGUE_INDEX",
    "VOLUME_INTERACTION_EFFECT",
    "INTERACTION_EFFECT_VS_LEAGUE_AVG_PCT",
    "INTERACTION_Q_BY",
    "CONSTWT_INTERACTION_Q_BY",
    "INTERACTION_RATE_FIRST_HALF",
    "INTERACTION_RATE_SECOND_HALF",
]
print("Validated archetype-specific volume interactions:")
print(
    validated_volume_interactions[volume_display_columns]
    .round(3)
    .to_string(index=False)
)

# Player-level comparison tool: 100 equals the league-average scoring rate.
player_baselines = (
    logs.groupby(
        [
            "PLAYER_ID",
            "PLAYER_NAME",
            "ARCHETYPE",
            "SUBTYPE_ID",
            "SUBTYPE_ARCHETYPE",
        ],
        as_index=False,
    )
    .agg(
        CURRENT_TEAM=("TEAM_ABBREVIATION", "last"),
        PLAYER_GAMES=("GAME_ID", "nunique"),
        PLAYER_TOTAL_PTS=("PTS", "sum"),
        PLAYER_TOTAL_MINUTES=("MIN", "sum"),
    )
)
player_baselines["PLAYER_BASELINE_PPM"] = (
    player_baselines["PLAYER_TOTAL_PTS"]
    / player_baselines["PLAYER_TOTAL_MINUTES"]
)
player_baselines["PLAYER_BASELINE_LEAGUE_INDEX"] = (
    player_baselines["PLAYER_BASELINE_PPM"] / league_average_ppm * 100
)
player_relative_matchups = player_baselines.merge(
    matchup_summary[
        [
            "SUBTYPE_ID",
            "OPP_TEAM",
            "PPM_TOTAL_EFFECT",
            "PPM_TOTAL_Q_BY",
            "PPM_INTERACTION_EFFECT",
            "PPM_INTERACTION_Q_BY",
            "INTERACTION_NOTABLE",
            "TOTAL_RATE_NOTABLE",
        ]
    ],
    on="SUBTYPE_ID",
    how="inner",
    validate="many_to_many",
)
player_relative_matchups = player_relative_matchups.loc[
    player_relative_matchups["OPP_TEAM"]
    != player_relative_matchups["CURRENT_TEAM"]
].copy()
player_relative_matchups["LEAGUE_AVG_PPM"] = league_average_ppm
player_relative_matchups["RELATIVE_MATCHUP_PPM"] = (
    player_relative_matchups["PLAYER_BASELINE_PPM"]
    + player_relative_matchups["PPM_TOTAL_EFFECT"]
)
player_relative_matchups["RELATIVE_MATCHUP_LEAGUE_INDEX"] = (
    player_relative_matchups["RELATIVE_MATCHUP_PPM"] / league_average_ppm * 100
)
player_relative_matchups["MATCHUP_INDEX_CHANGE"] = (
    player_relative_matchups["PPM_TOTAL_EFFECT"] / league_average_ppm * 100
)
player_relative_matchups["ARCHETYPE_INTERACTION_INDEX"] = (
    player_relative_matchups["PPM_INTERACTION_EFFECT"] / league_average_ppm * 100
)

player_volume_frames = []
player_identity_columns = [
    "PLAYER_ID",
    "PLAYER_NAME",
    "ARCHETYPE",
    "SUBTYPE_ID",
    "SUBTYPE_ARCHETYPE",
    "CURRENT_TEAM",
    "PLAYER_GAMES",
    "PLAYER_TOTAL_MINUTES",
]
for metric, metric_label in VOLUME_METRICS.items():
    metric_totals = (
        logs.groupby("PLAYER_ID", as_index=False)[metric]
        .sum()
        .rename(columns={metric: "PLAYER_TOTAL_VOLUME"})
    )
    metric_players = player_baselines[player_identity_columns].merge(
        metric_totals, on="PLAYER_ID", how="left", validate="one_to_one"
    )
    metric_players["PLAYER_BASELINE_RATE"] = (
        metric_players["PLAYER_TOTAL_VOLUME"]
        / metric_players["PLAYER_TOTAL_MINUTES"]
    )
    metric_players["METRIC"] = metric
    metric_players["METRIC_LABEL"] = metric_label
    player_volume_frames.append(metric_players)
player_volume_baselines = pd.concat(player_volume_frames, ignore_index=True)
player_relative_volume_matchups = player_volume_baselines.merge(
    volume_matchup_summary[
        [
            "SUBTYPE_ID",
            "METRIC",
            "OPP_TEAM",
            "LEAGUE_AVG_RATE",
            "VOLUME_TOTAL_EFFECT",
            "TOTAL_Q_BY",
            "VOLUME_INTERACTION_EFFECT",
            "INTERACTION_Q_BY",
            "INTERACTION_NOTABLE",
            "TOTAL_RATE_NOTABLE",
        ]
    ],
    on=["SUBTYPE_ID", "METRIC"],
    how="inner",
    validate="many_to_many",
)
player_relative_volume_matchups = player_relative_volume_matchups.loc[
    player_relative_volume_matchups["OPP_TEAM"]
    != player_relative_volume_matchups["CURRENT_TEAM"]
].copy()
player_relative_volume_matchups["PLAYER_BASELINE_LEAGUE_INDEX"] = (
    player_relative_volume_matchups["PLAYER_BASELINE_RATE"]
    / player_relative_volume_matchups["LEAGUE_AVG_RATE"]
    * 100
)
player_relative_volume_matchups["RELATIVE_MATCHUP_RATE"] = (
    player_relative_volume_matchups["PLAYER_BASELINE_RATE"]
    + player_relative_volume_matchups["VOLUME_TOTAL_EFFECT"]
)
player_relative_volume_matchups["RELATIVE_MATCHUP_LEAGUE_INDEX"] = (
    player_relative_volume_matchups["RELATIVE_MATCHUP_RATE"]
    / player_relative_volume_matchups["LEAGUE_AVG_RATE"]
    * 100
)
player_relative_volume_matchups["MATCHUP_INDEX_CHANGE"] = (
    player_relative_volume_matchups["VOLUME_TOTAL_EFFECT"]
    / player_relative_volume_matchups["LEAGUE_AVG_RATE"]
    * 100
)
player_relative_volume_matchups["ARCHETYPE_INTERACTION_INDEX"] = (
    player_relative_volume_matchups["VOLUME_INTERACTION_EFFECT"]
    / player_relative_volume_matchups["LEAGUE_AVG_RATE"]
    * 100
)

# %% [markdown]
# ## Save auditable outputs

# %%
subtype_labels = (
    matchup_summary[["SUBTYPE_ID", "SUBTYPE_ARCHETYPE"]]
    .drop_duplicates()
    .assign(
        DISPLAY_LABEL=lambda frame: (
            frame["SUBTYPE_ID"].astype(str) + " — " + frame["SUBTYPE_ARCHETYPE"]
        )
    )
    .set_index("SUBTYPE_ID")["DISPLAY_LABEL"]
)
heatmap_data = matchup_summary.assign(
    DISPLAY_VALUE=lambda frame: frame[
        "SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"
    ].where(frame["ELIGIBLE_FOR_INFERENCE"])
).pivot(index="SUBTYPE_ID", columns="OPP_TEAM", values="DISPLAY_VALUE")
heatmap_data = heatmap_data.loc[sorted(heatmap_data.index), sorted(heatmap_data.columns)]
heatmap_data.index = heatmap_data.index.map(subtype_labels)
heatmap_limit = max(
    2 * tau_ppm / league_average_ppm * 100,
    1.0,
)

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
for axis, (metric, metric_label) in zip(axes.flat, VOLUME_METRICS.items()):
    metric_frame = volume_matchup_summary.loc[
        volume_matchup_summary["METRIC"] == metric
    ]
    metric_heatmap = metric_frame.assign(
        DISPLAY_VALUE=lambda frame: frame[
            "SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"
        ].where(frame["ELIGIBLE_FOR_INFERENCE"])
    ).pivot(index="SUBTYPE_ID", columns="OPP_TEAM", values="DISPLAY_VALUE")
    metric_heatmap = metric_heatmap.loc[
        sorted(metric_heatmap.index), sorted(metric_heatmap.columns)
    ]
    metric_heatmap.index = metric_heatmap.index.map(subtype_labels)
    metric_limit = max(
        np.nanquantile(np.abs(metric_heatmap.to_numpy()), 0.95), 1.0
    )
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

summary_path = OUTPUT_DIR / "matchup_summary.csv"
notable_path = OUTPUT_DIR / "notable_pts_per_min_matchups.csv"
validated_path = OUTPUT_DIR / "validated_pts_per_min_interactions.csv"
watchlist_path = OUTPUT_DIR / "descriptive_pts_per_min_watchlist.csv"
player_relative_path = OUTPUT_DIR / "player_relative_matchups.csv"
volume_summary_path = OUTPUT_DIR / "volume_matchup_summary.csv"
validated_volume_path = OUTPUT_DIR / "validated_volume_interactions.csv"
volume_reliability_path = OUTPUT_DIR / "volume_split_half_reliability.csv"
player_volume_path = OUTPUT_DIR / "player_relative_volume_matchups.csv"
matchup_summary.sort_values(["SUBTYPE_ID", "OPP_TEAM"]).to_csv(summary_path, index=False)
notable_matchups.to_csv(notable_path, index=False)
validated_interactions.to_csv(validated_path, index=False)
descriptive_watchlist.to_csv(watchlist_path, index=False)
player_relative_matchups.sort_values(["PLAYER_NAME", "OPP_TEAM"]).to_csv(
    player_relative_path, index=False
)
volume_matchup_summary.sort_values(["METRIC", "SUBTYPE_ID", "OPP_TEAM"]).to_csv(
    volume_summary_path, index=False
)
validated_volume_interactions.to_csv(validated_volume_path, index=False)
volume_reliability.to_csv(volume_reliability_path, index=False)
player_relative_volume_matchups.sort_values(
    ["PLAYER_NAME", "METRIC", "OPP_TEAM"]
).to_csv(player_volume_path, index=False)
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
