"""Build independent 2026 WNBA scoring archetypes from attempt volume and context.

The clustering intentionally excludes shooting efficiency, height, impact metrics,
home/away status, rest, injuries, and betting markets. Height can be attached later
as a post-cluster size label without changing the scoring archetype.

Run with:
    uv run --with numpy --with pandas --with scikit-learn \
        python wnba_archetypes_2026.py
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import adjusted_rand_score, silhouette_score
from sklearn.preprocessing import StandardScaler


SEASON = "2026"
SEASON_TYPE = "Regular Season"
MIN_GAMES = 8
MIN_MPG = 10.0
MIN_FGA = 50
SELECTED_K = 7
K_VALUES = range(4, 13)
N_BOOTSTRAPS = 100
RANDOM_STATE = 42
REQUEST_DELAY_SECONDS = 1.5
REFRESH_DATA = False
INCLUDE_RAW_SHOT_CONTEXT = True

API_ROOT = "https://api.pbpstats.com"
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "wnba_archetypes_data" / SEASON
OUTPUT_DIR = ROOT / "wnba_archetypes_outputs" / SEASON
TOTALS_CACHE = DATA_DIR / "player_totals.json"
SHOT_CACHE_DIR = DATA_DIR / "player_shots"

VOLUME_FEATURES = ["FGA_PER_MIN", "FG3A_PER_MIN", "FTA_PER_MIN"]
LOCATION_FEATURES = [
    "RIM_FGA_SHARE",
    "SHORT_MID_FGA_SHARE",
    "LONG_MID_FGA_SHARE",
    "CORNER3_FGA_SHARE",
    "ARC3_FGA_SHARE",
]
CONTEXT_FEATURES = [
    "SECOND_CHANCE_FGA_SHARE",
]
if INCLUDE_RAW_SHOT_CONTEXT:
    CONTEXT_FEATURES += ["PUTBACK_FGA_SHARE", "EARLY_OFFENSE_FGA_SHARE"]
FEATURES = VOLUME_FEATURES + LOCATION_FEATURES + CONTEXT_FEATURES

COUNT_COLUMNS = [
    "FG2A",
    "FG3A",
    "FTA",
    "AtRimFGA",
    "ShortMidRangeFGA",
    "LongMidRangeFGA",
    "Corner3FGA",
    "Arc3FGA",
    "SecondChanceFG2A",
    "SecondChanceFG3A",
]


def fetch_json(endpoint: str, params: dict[str, object], attempts: int = 4) -> object:
    """Fetch JSON with bounded retries and a descriptive user agent."""
    url = f"{API_ROOT}{endpoint}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "wnba-archetype-research/1.0"})
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=90) as response:
                return json.load(response)
        except Exception as error:  # network/API failures are retried below
            last_error = error
            if attempt < attempts - 1:
                # The public API returns transient 503s under burst traffic.
                # Jitter prevents retries from repeatedly landing together.
                time.sleep(min(30, 2 ** (attempt + 1)) + random.uniform(0.25, 1.0))
    raise RuntimeError(f"Could not fetch {endpoint} after {attempts} attempts") from last_error


def load_player_totals() -> pd.DataFrame:
    if TOTALS_CACHE.exists() and not REFRESH_DATA:
        payload = json.loads(TOTALS_CACHE.read_text())
    else:
        payload = fetch_json(
            "/get-totals/wnba",
            {"Season": SEASON, "SeasonType": SEASON_TYPE, "Type": "Player"},
        )
        TOTALS_CACHE.write_text(json.dumps(payload))
    rows = payload.get("multi_row_table_data", [])
    if not rows:
        raise ValueError("PBP Stats returned no WNBA player totals")
    return pd.DataFrame(rows)


def load_player_shots(player_id: str) -> tuple[list[dict[str, object]], bool]:
    cache_path = SHOT_CACHE_DIR / f"{player_id}.json"
    if cache_path.exists() and not REFRESH_DATA:
        payload = json.loads(cache_path.read_text())
        results = payload.get("results", [])
        if payload.get("complete") or len(results) < 500:
            return results, True
    else:
        payload = fetch_json(
            "/get-shots/wnba",
            {
                "Season": SEASON,
                "SeasonType": SEASON_TYPE,
                "EntityType": "Player",
                "EntityId": player_id,
            },
        )
        results = payload.get("results", [])

    # The API silently caps a response at 500 records. Reconstruct capped
    # players from non-overlapping regulation-period and overtime queries.
    if len(results) == 500:
        partitioned: list[dict[str, object]] = []
        period_filters = [{"PeriodEquals": period} for period in range(1, 5)]
        period_filters.append({"PeriodGte": 5})
        for period_filter in period_filters:
            segment = fetch_json(
                "/get-shots/wnba",
                {
                    "Season": SEASON,
                    "SeasonType": SEASON_TYPE,
                    "EntityType": "Player",
                    "EntityId": player_id,
                    **period_filter,
                },
            ).get("results", [])
            partitioned.extend(segment)
            time.sleep(REQUEST_DELAY_SECONDS)
        deduplicated = {
            (str(shot.get("gid")), str(shot.get("event_num"))): shot
            for shot in partitioned
        }
        results = list(deduplicated.values())

    cache_path.write_text(json.dumps({"complete": True, "results": results}))
    return results, False


def fetch_shot_context(players: pd.DataFrame) -> pd.DataFrame:
    """Count putbacks and early-offense attempts from raw shot records.

    Early offense is a non-Synergy proxy: a non-putback shot within seven seconds
    of possessions beginning after a live-ball turnover or a missed field goal.
    The raw start-type distribution is exported so this definition remains auditable.
    """
    player_ids = players["PLAYER_ID"].astype(str).tolist()
    shot_rows: list[dict[str, object]] = []
    for completed, player_id in enumerate(player_ids, start=1):
        player_shots, was_cached = load_player_shots(player_id)
        for shot in player_shots:
            shot_rows.append({"PLAYER_ID": player_id, **shot})
        if completed % 10 == 0 or completed == len(player_ids):
            print(f"Loaded shots for {completed}/{len(player_ids)} players", flush=True)
        if not was_cached and completed < len(player_ids):
            time.sleep(REQUEST_DELAY_SECONDS)

    shots = pd.DataFrame(shot_rows)
    if shots.empty:
        raise ValueError("No raw WNBA shots were returned")
    shots.to_csv(DATA_DIR / "eligible_player_shots.csv", index=False)

    shots["putback"] = shots["putback"].fillna(False).astype(bool)
    shots["start_time"] = pd.to_numeric(shots["start_time"], errors="coerce")
    shots["shot_time"] = pd.to_numeric(shots["shot_time"], errors="coerce")
    shots["SECONDS_SINCE_POSSESSION_START"] = shots["start_time"] - shots["shot_time"]
    missed_field_goal_start = shots["start_type"].fillna("").str.match(
        r"^Off(?:AtRim|ShortMidRange|LongMidRange|Corner3|Arc3)(?:Miss|Block)$"
    )
    shots["EARLY_OFFENSE"] = (
        ~shots["putback"]
        & ((shots["start_type"] == "OffLiveBallTurnover") | missed_field_goal_start)
        & shots["SECONDS_SINCE_POSSESSION_START"].between(0, 7, inclusive="both")
    )

    start_types = (
        shots.groupby("start_type", dropna=False)
        .size()
        .rename("FGA")
        .sort_values(ascending=False)
        .reset_index()
    )
    start_types.to_csv(OUTPUT_DIR / "possession_start_type_audit.csv", index=False)

    return (
        shots.groupby("PLAYER_ID")
        .agg(
            RAW_SHOT_FGA=("gid", "size"),
            PUTBACK_FGA=("putback", "sum"),
            EARLY_OFFENSE_FGA=("EARLY_OFFENSE", "sum"),
        )
        .reset_index()
    )


def prepare_features(totals: pd.DataFrame) -> pd.DataFrame:
    required = {
        "EntityId",
        "Name",
        "TeamAbbreviation",
        "GamesPlayed",
        "Minutes",
        *COUNT_COLUMNS,
    }
    missing = sorted(required - set(totals.columns))
    if missing:
        raise ValueError(f"PBP Stats totals are missing fields: {missing}")

    players = totals.copy()
    numeric_columns = ["GamesPlayed", "Minutes", *COUNT_COLUMNS]
    players[numeric_columns] = players[numeric_columns].apply(pd.to_numeric, errors="coerce")
    players[COUNT_COLUMNS] = players[COUNT_COLUMNS].fillna(0)
    players["FGA"] = players["FG2A"] + players["FG3A"]
    players["MPG"] = players["Minutes"] / players["GamesPlayed"].replace(0, np.nan)
    players = players.loc[
        (players["GamesPlayed"] >= MIN_GAMES)
        & (players["MPG"] >= MIN_MPG)
        & (players["FGA"] >= MIN_FGA)
    ].copy()
    players = players.rename(
        columns={
            "EntityId": "PLAYER_ID",
            "Name": "PLAYER_NAME",
            "TeamAbbreviation": "TEAM",
            "GamesPlayed": "GP",
            "Minutes": "MIN",
        }
    )
    players["PLAYER_ID"] = players["PLAYER_ID"].astype(str)

    if INCLUDE_RAW_SHOT_CONTEXT:
        shot_context = fetch_shot_context(players)
        players = players.merge(shot_context, on="PLAYER_ID", how="left", validate="one_to_one")
        players[["RAW_SHOT_FGA", "PUTBACK_FGA", "EARLY_OFFENSE_FGA"]] = players[
            ["RAW_SHOT_FGA", "PUTBACK_FGA", "EARLY_OFFENSE_FGA"]
        ].fillna(0)

        # Raw shot records and total FGA should agree. A tiny tolerance permits
        # feed corrections that occur while a snapshot is being fetched.
        players["SHOT_FGA_DIFFERENCE"] = players["RAW_SHOT_FGA"] - players["FGA"]
        mismatches = players["SHOT_FGA_DIFFERENCE"].abs() > 2
        if mismatches.any():
            examples = players.loc[mismatches, ["PLAYER_NAME", "FGA", "RAW_SHOT_FGA"]]
            raise ValueError(f"Raw-shot and totals FGA disagree:\n{examples.to_string(index=False)}")

    players["FGA_PER_MIN"] = players["FGA"] / players["MIN"]
    players["FG3A_PER_MIN"] = players["FG3A"] / players["MIN"]
    players["FTA_PER_MIN"] = players["FTA"] / players["MIN"]
    for output, source in {
        "RIM_FGA_SHARE": "AtRimFGA",
        "SHORT_MID_FGA_SHARE": "ShortMidRangeFGA",
        "LONG_MID_FGA_SHARE": "LongMidRangeFGA",
        "CORNER3_FGA_SHARE": "Corner3FGA",
        "ARC3_FGA_SHARE": "Arc3FGA",
    }.items():
        players[output] = players[source] / players["FGA"]
    players["SECOND_CHANCE_FGA_SHARE"] = (
        players["SecondChanceFG2A"] + players["SecondChanceFG3A"]
    ) / players["FGA"]
    if INCLUDE_RAW_SHOT_CONTEXT:
        players["PUTBACK_FGA_SHARE"] = players["PUTBACK_FGA"] / players["FGA"]
        players["EARLY_OFFENSE_FGA_SHARE"] = players["EARLY_OFFENSE_FGA"] / players["FGA"]

    if not np.isfinite(players[FEATURES].to_numpy(dtype=float)).all():
        raise ValueError("The clustering feature matrix contains non-finite values")
    return players.reset_index(drop=True)


def scaled_feature_matrix(players: pd.DataFrame) -> np.ndarray:
    """Give volume, location, and possession context equal family weight."""
    blocks = []
    for columns in (VOLUME_FEATURES, LOCATION_FEATURES, CONTEXT_FEATURES):
        standardized = StandardScaler().fit_transform(players[columns])
        blocks.append(standardized / np.sqrt(len(columns)))
    return np.column_stack(blocks)


def bootstrap_stability(X: np.ndarray, reference_labels: np.ndarray, k: int) -> tuple[float, float]:
    rng = np.random.default_rng(RANDOM_STATE + k)
    scores = []
    for bootstrap in range(N_BOOTSTRAPS):
        sample = rng.integers(0, len(X), size=len(X))
        model = KMeans(
            n_clusters=k,
            n_init=25,
            random_state=RANDOM_STATE + bootstrap + 1,
        ).fit(X[sample])
        scores.append(adjusted_rand_score(reference_labels, model.predict(X)))
    return float(np.mean(scores)), float(np.quantile(scores, 0.10))


def name_seven_clusters(profiles: pd.DataFrame) -> dict[int, str]:
    """Assign basketball names from profile signatures, not numeric labels."""
    if len(profiles) != 7:
        raise ValueError("The domain naming rules currently require seven clusters")

    remaining = set(profiles.index.astype(int))

    def take_max(score: pd.Series) -> int:
        cluster_id = int(score.loc[list(remaining)].idxmax())
        remaining.remove(cluster_id)
        return cluster_id

    interior_big = take_max(
        profiles["RIM_FGA_SHARE"]
        + profiles["PUTBACK_FGA_SHARE"]
        + profiles["SECOND_CHANCE_FGA_SHARE"]
    )
    floor_spacer = take_max(profiles["CORNER3_FGA_SHARE"] + profiles["ARC3_FGA_SHARE"])
    high_volume_perimeter = take_max(
        profiles["FGA_PER_MIN"]
        + profiles["FG3A_PER_MIN"]
        + profiles["FTA_PER_MIN"]
    )
    paint_midrange_focal = take_max(profiles["FGA_PER_MIN"] + profiles["FTA_PER_MIN"])
    transition_attacker = take_max(profiles["EARLY_OFFENSE_FGA_SHARE"])
    midrange_perimeter = take_max(
        profiles["SHORT_MID_FGA_SHARE"] + profiles["LONG_MID_FGA_SHARE"]
    )
    balanced_finisher = remaining.pop()

    return {
        interior_big: "Interior & Putback Big",
        floor_spacer: "Low-Volume Perimeter Spacer",
        high_volume_perimeter: "High-Volume Multi-Level Creator",
        paint_midrange_focal: "Paint & Midrange Focal Scorer",
        transition_attacker: "Transition Slasher & Connector",
        midrange_perimeter: "Midrange-Oriented Perimeter Creator",
        balanced_finisher: "Balanced Inside-Out Finisher",
    }


def cluster(players: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    X = scaled_feature_matrix(players)
    diagnostic_rows = []
    fitted_models: dict[int, KMeans] = {}
    for k in K_VALUES:
        model = KMeans(n_clusters=k, n_init=100, random_state=RANDOM_STATE).fit(X)
        fitted_models[k] = model
        counts = np.bincount(model.labels_, minlength=k)
        stability_mean, stability_p10 = bootstrap_stability(X, model.labels_, k)
        diagnostic_rows.append(
            {
                "K": k,
                "SILHOUETTE": silhouette_score(X, model.labels_),
                "STABILITY_ARI_MEAN": stability_mean,
                "STABILITY_ARI_P10": stability_p10,
                "MIN_CLUSTER_PLAYERS": counts.min(),
                "MAX_CLUSTER_PLAYERS": counts.max(),
            }
        )
    diagnostics = pd.DataFrame(diagnostic_rows)

    selected = fitted_models[SELECTED_K]
    players = players.copy()
    players["CLUSTER"] = selected.labels_
    distances = selected.transform(X)
    sorted_distances = np.sort(distances, axis=1)
    players["DISTANCE_TO_CENTROID"] = distances[np.arange(len(players)), selected.labels_]
    players["CONFIDENCE_MARGIN"] = sorted_distances[:, 1] - sorted_distances[:, 0]

    profiles = players.groupby("CLUSTER")[FEATURES].mean()
    archetype_names = name_seven_clusters(profiles)
    players["ARCHETYPE"] = players["CLUSTER"].map(archetype_names)
    profiles.insert(0, "PLAYERS", players.groupby("CLUSTER").size())
    profiles.insert(0, "ARCHETYPE", profiles.index.map(archetype_names))

    pca = PCA(n_components=2, random_state=RANDOM_STATE).fit(X)
    coordinates = pca.transform(X)
    players["PCA1"] = coordinates[:, 0]
    players["PCA2"] = coordinates[:, 1]
    return players, profiles.reset_index(), diagnostics


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SHOT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    totals = load_player_totals()
    totals.to_csv(DATA_DIR / "player_totals.csv", index=False)
    players = prepare_features(totals)
    assignments, profiles, diagnostics = cluster(players)

    assignments = assignments.sort_values(["CLUSTER", "DISTANCE_TO_CENTROID"])
    assignments.to_csv(OUTPUT_DIR / "player_archetypes_working.csv", index=False)
    profiles.to_csv(OUTPUT_DIR / "cluster_profiles_working.csv", index=False)
    diagnostics.to_csv(OUTPUT_DIR / "cluster_diagnostics.csv", index=False)

    representatives = (
        assignments.groupby("CLUSTER", group_keys=False)
        .head(6)
        .groupby("CLUSTER")["PLAYER_NAME"]
        .agg(", ".join)
        .rename("REPRESENTATIVE_PLAYERS")
    )
    summary = profiles[["CLUSTER", "ARCHETYPE", "PLAYERS"]].merge(
        representatives, on="CLUSTER"
    )

    print(f"Eligible players: {len(assignments)}")
    print("\nCluster-count diagnostics:")
    print(diagnostics.round(3).to_string(index=False))
    print(f"\nWorking {SELECTED_K}-cluster taxonomy:")
    print(summary.to_string(index=False))
    print(f"\nSaved outputs to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
