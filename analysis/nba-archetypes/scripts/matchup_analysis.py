"""Deterministic Analysis Run builder for the 2025-26 archetype matchup analysis.

The builder mirrors the generation stages of archetype_matchups_2025_26.py:
membership loading and validation, prepared player-game outcomes, joint
fixed-effects scoring fits, volume fits, artifact assembly, and the dashboard
payload handed to the notebook-facing prints. It accepts in-memory archetype
and game-log frames so the full path can be exercised deterministically from a
compact synthetic fixture without provider or cache access.

Statistical behavior matches the current script. Sorting, reference-opponent
choice, and effects extraction are deterministic given the input frames.
"""

import copy
import hashlib
import json
import pickle
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from types import MappingProxyType

import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm
from statsmodels.stats.sandwich_covariance import cov_cluster_2groups

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

NOTABLE_DISPLAY_COLUMNS = [
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

VOLUME_DISPLAY_COLUMNS = [
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


class FrozenDict(Mapping):
    """An immutable read-only mapping.

    ``AnalysisRun`` is frozen and ``RunProvenance``/``RunIdentity`` must not
    expose mutable hashes, so the identity-carrying dicts they own are frozen at
    construction time. It is a ``Mapping`` rather than a ``dict`` subclass so the
    base ``dict.__setitem__``/``dict.update``/``dict.__ior__`` invocations cannot
    mutate its storage, and its backing is a read-only mapping proxy so even the
    ``_data`` attribute itself cannot be written through.
    """

    __slots__ = ("_data",)

    def __init__(self, items=(), **kwargs):
        object.__setattr__(self, "_data", MappingProxyType(dict(items, **kwargs)))

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        return f"FrozenDict({self._data!r})"

    def __hash__(self):
        return hash(frozenset(self._data.items()))

    def copy(self):
        return FrozenDict(self._data)

    def __getstate__(self):
        return dict(self._data)

    def __setstate__(self, state):
        # Pickle reconstructs a fresh instance (``__new__`` only, never
        # ``__init__``) whose ``_data`` slot is unset; ``__setstate__`` restores
        # state onto that fresh object. Calling it on a live, already-built
        # value would be a public unrestricted mutator that could rewrite the
        # built identity/settings mappings, so it is a one-shot that only ever
        # initializes a newly unpickled object.
        if hasattr(self, "_data"):
            raise TypeError("This mapping is immutable")
        object.__setattr__(self, "_data", MappingProxyType(state))

    def _immutable(self, *args, **kwargs):
        raise TypeError("This mapping is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable
    __or__ = _immutable


@dataclass(frozen=True)
class AnalysisRunSettings:
    min_cell_players: int = MIN_CELL_PLAYERS
    min_cell_games: int = MIN_CELL_GAMES
    min_cell_offensive_teams: int = MIN_CELL_OFFENSIVE_TEAMS
    fdr_alpha: float = FDR_ALPHA
    min_notable_pct: float = MIN_NOTABLE_PCT
    min_notable_pts_per_min: float = MIN_NOTABLE_PTS_PER_MIN
    volume_metrics: dict = field(default_factory=lambda: dict(VOLUME_METRICS))

    def __post_init__(self):
        # The volume_metrics mapping is deep-frozen at construction: a run id is
        # derived from the settings snapshot and every artifact stage reads the
        # same mapping, so a mutable nested dict must not be able to desynchronize
        # the run id from the artifacts it is attributed to.
        object.__setattr__(self, "volume_metrics", FrozenDict(dict(self.volume_metrics)))


DEFAULT_SETTINGS = AnalysisRunSettings()


def content_hash(*parts) -> str:
    """Deterministic SHA-256 over ``parts`` for content-addressed identities."""
    document = json.dumps(
        parts, sort_keys=True, separators=(",", ":"), default=_json_default
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__} deterministically")


def _frame_canonical_text(frame, *, include_index=False) -> str:
    """Canonical, lossless deterministic text form of a frame for content hashing.

    Numeric columns keep their exact stored values: integers are serialized as
    JSON integers (so values beyond ``2**53`` remain distinct) and floats use
    Python's shortest round-trip representation instead of pandas' truncated
    10-digit JSON precision. Column order and row order are normalized before
    serialization. The index is normalized away unless ``include_index`` is
    set, which artifact digests use for heatmap frames whose index carries the
    semantic display labels.
    """
    canonical = frame.copy()
    canonical = canonical.reindex(sorted(canonical.columns), axis=1)
    if include_index:
        index_name = "_index"
        while index_name in canonical.columns:
            index_name = "_" + index_name
        canonical = canonical.reset_index(names=index_name)
    else:
        canonical = canonical.reset_index(drop=True)
    canonical = canonical.sort_values(list(canonical.columns))
    return json.dumps(
        canonical.to_dict(orient="records"),
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def compute_input_data_identity(archetypes, model_inputs) -> str:
    """Content identity of the archetype membership and the model input snapshot.

    ``model_inputs`` is the data the clustering model was actually fit on (the
    archetype feature snapshot in production), not the downstream matchup game
    logs. The same snapshot must be handed to ``AnalysisRunBuilder`` through its
    ``clustering_features`` argument so artifact assembly can fail closed when a
    stale model is joined to new data.
    """
    return content_hash(
        ("input-data", _frame_canonical_text(archetypes), _frame_canonical_text(model_inputs))
    )


@dataclass(frozen=True)
class ArchetypeModelSpec:
    """Content-addressed identity of one published archetype model.

    The identity covers every parameter that determines the published subtype
    structure: the season, feature definition, clustering method, base random
    seed, top-level cluster count, bootstrap count, minimum subtype size, the
    silhouette and stability thresholds gating a parent split, the final
    subtype count, and the input-data identity. Two clustering executions that
    differ in any of these values are distinct models.
    """

    season: str
    feature_definition: str
    clustering_method: str
    cluster_count: int
    random_seed: int
    top_level_clusters: int
    n_bootstraps: int
    min_subtype_size: int
    subtype_min_silhouette: float
    subtype_min_stability: float
    input_data_identity: str

    def __post_init__(self):
        for name in (
            "season",
            "feature_definition",
            "clustering_method",
            "input_data_identity",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (
            not isinstance(self.cluster_count, int)
            or isinstance(self.cluster_count, bool)
            or self.cluster_count < 1
        ):
            raise ValueError("cluster_count must be a positive integer")
        for name in ("top_level_clusters", "n_bootstraps", "min_subtype_size"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("subtype_min_silhouette", "subtype_min_stability"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{name} must be a fraction between 0 and 1")
        if isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int):
            raise ValueError("random_seed must be an integer")

    @property
    def version(self) -> str:
        return content_hash(
            (
                "archetype-model",
                self.season,
                self.feature_definition,
                self.clustering_method,
                self.cluster_count,
                self.random_seed,
                self.top_level_clusters,
                self.n_bootstraps,
                self.min_subtype_size,
                self.subtype_min_silhouette,
                self.subtype_min_stability,
                self.input_data_identity,
            )
        )


def spec_from_clustering_metadata(metadata: dict) -> ArchetypeModelSpec:
    """Build the archetype-model spec from the clustering notebook's metadata.

    The model input identity must have been recorded by the clustering
    execution that produced the membership and feature snapshots; production
    reads that recorded digest so the stale-model guard is not a self-fulfilling
    comparison against whatever files happen to coexist. ``metadata`` without a
    recorded ``input_data_identity`` is rejected.
    """
    required = (
        "season",
        "feature_definition",
        "clustering_method",
        "cluster_count",
        "random_seed",
        "top_level_clusters",
        "n_bootstraps",
        "min_subtype_size",
        "subtype_min_silhouette",
        "subtype_min_stability",
        "input_data_identity",
    )
    missing = [key for key in required if not str(metadata.get(key, "")).strip()]
    if missing:
        raise ValueError(
            "clustering metadata is missing required keys: " + ", ".join(missing)
        )
    return ArchetypeModelSpec(
        season=str(metadata["season"]),
        feature_definition=str(metadata["feature_definition"]),
        clustering_method=str(metadata["clustering_method"]),
        cluster_count=int(metadata["cluster_count"]),
        random_seed=int(metadata["random_seed"]),
        top_level_clusters=int(metadata["top_level_clusters"]),
        n_bootstraps=int(metadata["n_bootstraps"]),
        min_subtype_size=int(metadata["min_subtype_size"]),
        subtype_min_silhouette=float(metadata["subtype_min_silhouette"]),
        subtype_min_stability=float(metadata["subtype_min_stability"]),
        input_data_identity=str(metadata["input_data_identity"]),
    )


def fail_if_code_changed(initial_revision: str, current_revision: str) -> None:
    """Fail closed when the analysis code snapshot changed during a build.

    Production captures ``code_revision`` before loading any data and re-checks
    it immediately before persisting artifacts. A mismatch means the disk state
    that would be attributed to the run no longer matches the code that
    produced it, so publication must abort rather than bless a mixed snapshot.
    """
    if initial_revision != current_revision:
        raise RuntimeError(
            "The analysis code changed while the run was being built; refusing "
            "to publish a run attributed to a mixed code snapshot"
        )


@dataclass(frozen=True)
class RunProvenance:
    """Immutable provenance recorded for one Analysis Run."""

    information_cutoff: date
    input_hashes: dict
    code_revision: str | None
    generated_at: datetime
    cluster_count: int
    clustering_attempt: int

    def __post_init__(self):
        if isinstance(self.information_cutoff, datetime):
            raise ValueError("information_cutoff must be a date, not a datetime")
        if not isinstance(self.generated_at, datetime) or self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be a timezone-aware datetime")
        if (
            not isinstance(self.clustering_attempt, int)
            or isinstance(self.clustering_attempt, bool)
            or self.clustering_attempt < 1
        ):
            raise ValueError("clustering_attempt must be a positive integer")
        object.__setattr__(self, "input_hashes", FrozenDict(dict(self.input_hashes)))

    def to_dict(self) -> dict:
        return {
            "information_cutoff": self.information_cutoff.isoformat(),
            "input_hashes": dict(self.input_hashes),
            "code_revision": self.code_revision,
            "generated_at": self.generated_at.isoformat(),
            "cluster_count": self.cluster_count,
            "clustering_attempt": self.clustering_attempt,
        }


@dataclass(frozen=True)
class RunIdentity:
    """Immutable identity shared by an Analysis Run, its artifacts, and payload.

    The raw ``run_id``, ``model_version``, and subtype-key map previously
    travelled together as separate mutable fields; one immutable value now owns
    all three so the bundle cannot drift or be rewritten after build.
    """

    run_id: str
    model_version: str
    stable_subtype_keys: dict

    def __post_init__(self):
        for name in ("run_id", "model_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a 64-character lowercase hex digest")
        keys = self.stable_subtype_keys
        if not isinstance(keys, dict) or not keys:
            raise ValueError("stable_subtype_keys must be a non-empty mapping")
        for key in keys.values():
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ValueError("each stable subtype key must be a 64-character hex digest")
        object.__setattr__(self, "stable_subtype_keys", FrozenDict(dict(keys)))


def _freeze_fit_arrays(value, _seen=None):
    """Make every numpy array reachable from a model-fit result read-only.

    statsmodels fit results are opaque objects that cannot be deep-copied
    safely, so after the fit (and its clustered covariance) is complete the
    result's arrays are pinned read-only. ``AnalysisRun`` publishes the fit
    objects by reference, which is safe only because none of their state can
    change afterwards.
    """
    if _seen is None:
        _seen = set()
    if id(value) in _seen:
        return
    _seen.add(id(value))
    if isinstance(value, np.ndarray):
        try:
            value.setflags(write=False)
        except ValueError:
            pass
        return
    if isinstance(value, dict):
        for item in value.values():
            _freeze_fit_arrays(item, _seen)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _freeze_fit_arrays(item, _seen)
        return
    if hasattr(value, "__dict__"):
        for item in vars(value).values():
            _freeze_fit_arrays(item, _seen)


class _ImmutableArray(np.ndarray):
    """Read-only numpy array that cannot be made writable again.

    numpy's ``writeable`` flag alone is not enough for a published snapshot: a
    caller can re-enable writes with ``setflags(write=True)`` and mutate the
    stored data in place. This subclass rejects re-enabling writes and direct
    element writes, so a ``FrozenFitResult`` array stays unchanged however it is
    reached. Read operations (indexing, arithmetic, copies, pandas conversion)
    behave exactly like a plain read-only array.
    """

    __slots__ = ()

    def __array_finalize__(self, obj):
        pass

    def setflags(self, write=None, align=None, uic=None):
        if write is True:
            raise ValueError("This array is read-only")
        return np.ndarray.setflags(self, write=write, align=align, uic=uic)

    def __setitem__(self, key, value):
        raise ValueError("This array is read-only")


def _immutable_ndarray(value) -> np.ndarray:
    """Return ``value`` as an ndarray whose memory can never be re-enabled.

    ``np.frombuffer`` over the ``bytes`` returned by ``tobytes()`` produces an
    array whose backing buffer is immutable Python ``bytes``. numpy refuses to
    re-enable writes on such an array (``setflags(write=True)`` raises), so
    unlike a ``setflags(write=False)`` copy, neither the returned array nor any
    ``.base`` reached from it can be made writable again.
    """
    value = np.asarray(value)
    if value.dtype == object:
        frozen = value.copy()
        frozen.setflags(write=False)
        return frozen
    return np.frombuffer(value.tobytes(order="C"), dtype=value.dtype).reshape(
        value.shape
    )


@dataclass(frozen=True)
class FrozenFitResult:
    """Immutable snapshot of a fitted regression result, safe to publish.

    The live statsmodels result used during a build is replaced by this
    snapshot when an ``AnalysisRun`` is constructed, so no published property
    exposes mutable fit state: neither in-place array writes, nor re-enabling
    array writes, nor attribute reassignment can change a run's recorded fits.
    """

    params: np.ndarray
    bse: np.ndarray
    tvalues: np.ndarray
    pvalues: np.ndarray
    resid: np.ndarray
    fittedvalues: np.ndarray
    df_resid: float
    df_model: float
    nobs: float

    def __post_init__(self):
        for name in ("params", "bse", "tvalues", "pvalues", "resid", "fittedvalues"):
            value = getattr(self, name)
            if not isinstance(value, np.ndarray):
                raise ValueError(f"{name} must be a numpy array")
            # The snapshot is backed by immutable bytes, so even the ``.base``
            # exposed by the published view cannot be made writable again.
            object.__setattr__(
                self, name, _immutable_ndarray(value).view(_ImmutableArray)
            )
        for name in ("df_resid", "df_model", "nobs"):
            object.__setattr__(self, name, float(getattr(self, name)))

    def __setstate__(self, state):
        # Pickle reconstructs ndarray subclasses with owned writable storage and
        # re-enabling writes through the base ``np.ndarray.setflags`` would then
        # succeed, so every deserialized array is rebuilt over immutable bytes:
        # the published snapshot stays intrinsically non-writable even through
        # base-class methods.
        for name in ("params", "bse", "tvalues", "pvalues", "resid", "fittedvalues"):
            value = state.get(name)
            if isinstance(value, np.ndarray):
                object.__setattr__(
                    self, name, _immutable_ndarray(value).view(_ImmutableArray)
                )
            else:
                object.__setattr__(self, name, value)
        for name in ("df_resid", "df_model", "nobs"):
            object.__setattr__(self, name, state.get(name))

    @classmethod
    def from_statsmodels(cls, fit):
        return cls(
            params=np.asarray(fit.params),
            bse=np.asarray(fit.bse),
            tvalues=np.asarray(fit.tvalues),
            pvalues=np.asarray(fit.pvalues),
            resid=np.asarray(fit.resid),
            fittedvalues=np.asarray(fit.fittedvalues),
            df_resid=fit.df_resid,
            df_model=fit.df_model,
            nobs=fit.nobs,
        )


def _repin_immutable_arrays(value, _seen=None):
    """Re-pin published ``_ImmutableArray`` snapshots after a pickle round-trip.

    Pickling reconstructs the immutable fit-snapshot arrays as owned writable
    ``_ImmutableArray`` instances; re-setting their writeable flag restores the
    published read-only contract. Plain arrays (such as the clustered
    covariance) are left writable so a caller mutating a returned copy cannot
    touch the run's serialized backing, exactly as a defensive copy behaves.
    """
    if _seen is None:
        _seen = set()
    if id(value) in _seen:
        return
    _seen.add(id(value))
    if isinstance(value, _ImmutableArray):
        value.setflags(write=False)
        return
    if isinstance(value, dict):
        for item in value.values():
            _repin_immutable_arrays(item, _seen)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _repin_immutable_arrays(item, _seen)
        return
    if hasattr(value, "__dict__"):
        for item in vars(value).values():
            _repin_immutable_arrays(item, _seen)


def _snapshot_fit_results(scoring_fits):
    """Replace live statsmodels fits in a scoring-fits mapping with snapshots.

    Only entries that actually carry a ``fit`` key (the per-outcome fit models)
    are converted; the mapping's other entries (summary, eligible mask,
    diagnostics, scalars) pass through untouched.
    """
    return {
        name: (
            {**entry, "fit": FrozenFitResult.from_statsmodels(entry["fit"])}
            if isinstance(entry, dict) and "fit" in entry
            else entry
        )
        for name, entry in scoring_fits.items()
    }


@dataclass(frozen=True)
class AnalysisRun:
    """Immutable result of one Analysis Run build.

    Every mutable backing collection (frames, series, fit dicts, artifact and
    payload dicts) is serialized to immutable ``bytes`` at construction time,
    so even direct access to a private ``_``-prefixed field cannot mutate the
    run graph: the backing bytes have no mutable surface, and each read-only
    property deserializes a fresh object that no caller can write back through.
    The fit result snapshots remain immutable through the serialization.
    """

    _membership: bytes = field(repr=False)
    _logs: bytes = field(repr=False)
    _coverage: bytes = field(repr=False)
    _scoring_fits: bytes = field(repr=False)
    _matchup_summary: bytes = field(repr=False)
    _volume_fits: bytes = field(repr=False)
    _volume_matchup_summary: bytes = field(repr=False)
    _volume_reliability: bytes = field(repr=False)
    _volume_shrinkage_summary: bytes = field(repr=False)
    _artifacts: bytes = field(repr=False)
    _dashboard_payload: bytes = field(repr=False)
    identity: RunIdentity
    model_spec: ArchetypeModelSpec
    provenance: RunProvenance

    def __post_init__(self):
        # The live statsmodels results are replaced by immutable snapshots so a
        # published fit cannot be rewritten or have its arrays changed, then the
        # entire backing graph is serialized to immutable bytes.
        object.__setattr__(self, "_scoring_fits", _snapshot_fit_results(self._scoring_fits))
        for name in (
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
        ):
            object.__setattr__(self, name, pickle.dumps(getattr(self, name), protocol=pickle.HIGHEST_PROTOCOL))

    @property
    def membership(self) -> pd.DataFrame:
        return pickle.loads(self._membership)

    @property
    def logs(self) -> pd.DataFrame:
        return pickle.loads(self._logs)

    @property
    def coverage(self) -> pd.Series:
        return pickle.loads(self._coverage)

    @property
    def scoring_fits(self) -> dict:
        value = pickle.loads(self._scoring_fits)
        _repin_immutable_arrays(value)
        return value

    @property
    def matchup_summary(self) -> pd.DataFrame:
        return pickle.loads(self._matchup_summary)

    @property
    def volume_fits(self) -> dict:
        value = pickle.loads(self._volume_fits)
        _repin_immutable_arrays(value)
        return value

    @property
    def volume_matchup_summary(self) -> pd.DataFrame:
        return pickle.loads(self._volume_matchup_summary)

    @property
    def volume_reliability(self) -> pd.DataFrame:
        return pickle.loads(self._volume_reliability)

    @property
    def volume_shrinkage_summary(self) -> pd.DataFrame:
        return pickle.loads(self._volume_shrinkage_summary)

    @property
    def artifacts(self) -> dict:
        return pickle.loads(self._artifacts)

    @property
    def dashboard_payload(self) -> dict:
        return pickle.loads(self._dashboard_payload)

    @property
    def run_id(self) -> str:
        return self.identity.run_id

    @property
    def model_version(self) -> str:
        return self.identity.model_version

    @property
    def stable_subtype_keys(self) -> dict:
        return self.identity.stable_subtype_keys


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
        pd.factorize(frame["PLAYER_ID"])[0],
        pd.factorize(frame["GAME_ID"])[0],
        use_correction=True,
    )
    absorbed_parameters = frame["PLAYER_ID"].nunique() - 1
    visible_residual_df = len(frame) - len(columns)
    full_residual_df = visible_residual_df - absorbed_parameters
    if full_residual_df <= 0:
        raise ValueError("Not enough residual degrees of freedom after player effects")
    covariance *= visible_residual_df / full_residual_df
    # The fit result is published on AnalysisRun by reference; pin every array
    # reachable from it read-only so no published property exposes mutable fit
    # state (its clustered covariance is a separate writable array).
    _freeze_fit_arrays(fit)
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


def _extract_effect_rows(model, frame, prefix, linear):
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
            row = {
                "SUBTYPE_ID": subtype_id,
                "SUBTYPE_ARCHETYPE": subtype_names[subtype_id],
                "OPP_TEAM": opponent,
            }
            if linear:
                row.update(
                    {
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
            else:
                row.update(
                    {
                        f"{prefix}_TOTAL_LOG_EFFECT": total[0],
                        f"{prefix}_TOTAL_EFFECT_PCT": np.expm1(total[0]) * 100,
                        f"{prefix}_TOTAL_SE": total[1],
                        f"{prefix}_TOTAL_CI90_LOW_PCT_UNADJ": np.expm1(total[2]) * 100,
                        f"{prefix}_TOTAL_CI90_HIGH_PCT_UNADJ": np.expm1(total[3]) * 100,
                        f"{prefix}_TOTAL_P_VALUE": total[4],
                        f"{prefix}_OPP_MAIN_EFFECT_PCT": np.expm1(main[0]) * 100,
                        f"{prefix}_INTERACTION_LOG_EFFECT": interaction[0],
                        f"{prefix}_INTERACTION_EFFECT_PCT": np.expm1(
                            interaction[0]
                        ) * 100,
                        f"{prefix}_INTERACTION_SE": interaction[1],
                        f"{prefix}_INTERACTION_CI90_LOW_PCT_UNADJ": np.expm1(
                            interaction[2]
                        ) * 100,
                        f"{prefix}_INTERACTION_CI90_HIGH_PCT_UNADJ": np.expm1(
                            interaction[3]
                        ) * 100,
                        f"{prefix}_INTERACTION_P_VALUE": interaction[4],
                    }
                )
            rows.append(row)
    return pd.DataFrame(rows)


def extract_effects(model, frame, prefix):
    return _extract_effect_rows(model, frame, prefix, linear=False)


def extract_linear_effects(model, frame, prefix):
    return _extract_effect_rows(model, frame, prefix, linear=True)


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


class AnalysisRunBuilder:
    """Build a deterministic analysis run from in-memory membership and logs.

    Each public boundary method is independently invocable; consecutive calls
    are idempotent. ``build`` runs the stages in order and returns the run. An
    ``ArchetypeModelSpec`` is required: artifact assembly fails closed when the
    model identity is missing or when the input-data identity of the supplied
    frames disagrees with the specification.
    """

    def __init__(
        self,
        archetypes,
        game_logs,
        model_spec=None,
        *,
        settings=None,
        clustering_attempt=1,
        code_revision=None,
        generated_at=None,
        clustering_features=None,
    ):
        if not isinstance(code_revision, str) or not code_revision.strip():
            raise ValueError("code_revision must be a non-empty string")
        if (
            not isinstance(clustering_attempt, int)
            or isinstance(clustering_attempt, bool)
            or clustering_attempt < 1
        ):
            raise ValueError("clustering_attempt must be a positive integer")
        self.archetypes = archetypes
        self.game_logs = game_logs
        self.model_spec = model_spec
        provided_settings = settings if settings is not None else DEFAULT_SETTINGS
        if not isinstance(provided_settings, AnalysisRunSettings):
            raise ValueError("settings must be an AnalysisRunSettings instance")
        # Deep-freeze the settings snapshot into a private immutable slot at
        # construction: the run id is versioned against this snapshot and every
        # artifact stage reads the same immutable mapping, so a caller mutating
        # (or replacing members of) their own settings object after construction
        # can never desynchronize the run id from the artifacts it is attributed
        # to, and ``builder.settings`` itself cannot be reassigned (it is a
        # read-only property with no setter).
        self._settings = copy.deepcopy(provided_settings)
        self.clustering_attempt = clustering_attempt
        self.code_revision = code_revision
        self.generated_at = generated_at
        self.clustering_features = clustering_features
        self._model_version = (
            model_spec.version if isinstance(model_spec, ArchetypeModelSpec) else None
        )
        self._membership = None
        self._logs = None
        self._coverage = None
        self._league_average_ppm = None
        self._league_average_volume_rates = None
        self._scoring = None
        self._volume = None
        self._artifacts = None
        self._dashboard_payload = None
        self._stable_subtype_keys = None
        self._information_cutoff = None
        self._input_hashes = None
        self._run_id = None
        self._run_identity = None
        self._provenance = None

    @property
    def settings(self) -> AnalysisRunSettings:
        """The immutable settings snapshot this run is versioned and built from.

        Read-only: the snapshot is a private deep copy captured at construction,
        and the attribute cannot be reassigned, so later stages can never read a
        different settings object than the one the run id was derived from.
        """
        return self._settings

    def load_membership(self):
        if self._membership is not None:
            return self._membership
        if self.archetypes["PLAYER_ID"].isna().any() or self.archetypes[
            "SUBTYPE_ID"
        ].isna().any():
            raise ValueError(
                "Each classified player must carry a non-null PLAYER_ID and "
                "SUBTYPE_ID; a null identifier would violate complete "
                "exactly-once membership"
            )
        if self.archetypes["PLAYER_ID"].duplicated().any():
            raise ValueError(
                "Stable subtype membership keys would collide: each classified player "
                "must be assigned to exactly one subtype"
            )
        membership = self.archetypes[
            [
                "PLAYER_ID",
                "PLAYER_NAME",
                "ARCHETYPE",
                "SUBTYPE_ID",
                "SUBTYPE_ARCHETYPE",
            ]
        ].drop_duplicates("PLAYER_ID")
        if membership.groupby("SUBTYPE_ID")["SUBTYPE_ARCHETYPE"].nunique().max() != 1:
            raise ValueError("Each SUBTYPE_ID must map to exactly one subtype name")
        self._stable_subtype_keys = self._derive_stable_subtype_keys(membership)
        self._membership = membership
        return membership

    def _derive_stable_subtype_keys(self, membership):
        members_by_subtype = {
            subtype_id: tuple(sorted(group["PLAYER_ID"].astype(str)))
            for subtype_id, group in membership.groupby("SUBTYPE_ID")
        }
        seen_members = {}
        keys_by_subtype = {}
        for subtype_id, members in members_by_subtype.items():
            key = content_hash(("members", members))
            if key in seen_members and seen_members[key] != members:
                raise ValueError("Stable subtype membership key collision detected")
            seen_members[key] = members
            keys_by_subtype[subtype_id] = key
        if len(set(keys_by_subtype.values())) != len(keys_by_subtype):
            raise ValueError(
                "Distinct subtypes must not share the same stable membership key"
            )
        return keys_by_subtype

    def record_provenance(self):
        if self._provenance is not None:
            return self._provenance
        if not isinstance(self.model_spec, ArchetypeModelSpec):
            raise ValueError("An ArchetypeModelSpec is required to record provenance")
        self.prepare_logs()
        # The run is versioned by its exact archetype model, code revision,
        # Information Cutoff, analysis settings, and the data snapshot it was
        # built from. The per-input hashes are recorded here so changing game
        # points with an unchanged cutoff/model/code still yields a distinct run
        # id, and so do different thresholds on identical data.
        self._run_id = content_hash(
            (
                "analysis-run",
                self._model_version,
                self.code_revision,
                self._information_cutoff.isoformat(),
                content_hash(("settings", asdict(self.settings))),
                self._input_hashes["archetypes"],
                self._input_hashes["game_logs"],
            )
        )
        generated_at = self.generated_at
        if generated_at is None:
            generated_at = datetime.now(timezone.utc)
        elif not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
            raise ValueError("generated_at must be a timezone-aware datetime")
        self._provenance = RunProvenance(
            information_cutoff=self._information_cutoff,
            input_hashes=dict(self._input_hashes),
            code_revision=self.code_revision,
            generated_at=generated_at,
            cluster_count=self.model_spec.cluster_count,
            clustering_attempt=self.clustering_attempt,
        )
        self._run_identity = RunIdentity(
            run_id=self._run_id,
            model_version=self._model_version,
            stable_subtype_keys=dict(self._stable_subtype_keys),
        )
        return self._provenance

    def _require_identity(self):
        if not isinstance(self.model_spec, ArchetypeModelSpec):
            raise ValueError("An ArchetypeModelSpec is required to assemble artifacts")
        membership = self.load_membership()
        self.prepare_logs()
        self.record_provenance()
        actual_cluster_count = int(membership["SUBTYPE_ID"].nunique())
        if actual_cluster_count != self.model_spec.cluster_count:
            raise ValueError(
                f"Model spec cluster_count {self.model_spec.cluster_count} does not "
                f"match the actual membership subtype count {actual_cluster_count}"
            )
        # Production passes the clustering input snapshot explicitly. The
        # synthetic-fixture seam (issue 70) has no clustering stage, so it
        # falls back to the game-log frame it hashes its specs over.
        model_inputs = (
            self.clustering_features
            if self.clustering_features is not None
            else self.game_logs
        )
        observed_identity = compute_input_data_identity(self.archetypes, model_inputs)
        if observed_identity != self.model_spec.input_data_identity:
            raise ValueError(
                "Input data identity does not match the archetype model specification"
            )

    def prepare_logs(self):
        if self._logs is not None:
            return self._logs
        membership = self.load_membership()
        settings = self.settings

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
        missing_cols = sorted(set(required_cols) - set(self.game_logs.columns))
        if missing_cols:
            raise ValueError(
                f"Game-log response is missing columns: {missing_cols}"
            )

        logs = self.game_logs[required_cols].copy()
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
            for metric in settings.volume_metrics
        }
        if not np.isfinite(league_average_ppm) or league_average_ppm <= 0:
            raise ValueError("League-average PTS/MIN could not be calculated")

        logs["OPP_TEAM"] = logs["MATCHUP"].str.extract(r"(?:vs\.|@)\s+([A-Z]{3})$")
        logs = logs.merge(membership, on="PLAYER_ID", how="inner", validate="many_to_one")
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
        for metric in settings.volume_metrics:
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
                "classified_players": membership["PLAYER_ID"].nunique(),
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
        self._logs = logs
        self._coverage = coverage
        self._league_average_ppm = league_average_ppm
        self._league_average_volume_rates = league_average_volume_rates
        self._information_cutoff = logs["GAME_DATE"].max().date()
        self._input_hashes = {
            "archetypes": content_hash(("frame", _frame_canonical_text(self.archetypes))),
            "game_logs": content_hash(("frame", _frame_canonical_text(self.game_logs))),
        }
        return logs

    def fit_scoring(self):
        if self._scoring is not None:
            return self._scoring["summary"]
        logs = self.prepare_logs()
        settings = self.settings
        league_average_ppm = self._league_average_ppm

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
        matchup_summary = (
            points_effects.merge(
                minutes_effects,
                on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
                validate="one_to_one",
            )
            .merge(
                rate_effects,
                on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
                validate="one_to_one",
            )
            .merge(
                ppm_effects,
                on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
                validate="one_to_one",
            )
            .merge(
                ppm_constant_weight_effects,
                on=["SUBTYPE_ID", "SUBTYPE_ARCHETYPE", "OPP_TEAM"],
                validate="one_to_one",
            )
        )

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
            logs.groupby("SUBTYPE_ID")["PTS"].sum() / logs.groupby("SUBTYPE_ID")["MIN"].sum()
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
            matchup_summary["PPM_TOTAL_EFFECT"] / matchup_summary["SUBTYPE_AVG_PPM"] * 100
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
            (matchup_summary["PLAYERS"] >= settings.min_cell_players)
            & (matchup_summary["DISTINCT_GAMES"] >= settings.min_cell_games)
            & (matchup_summary["OFFENSIVE_TEAMS"] >= settings.min_cell_offensive_teams)
            & matchup_summary["PPM_INTERACTION_P_VALUE"].notna()
        )
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
            (matchup_summary["PPM_CONSTWT_INTERACTION_Q_BY"] <= settings.fdr_alpha)
            & (
                np.sign(matchup_summary["PPM_INTERACTION_EFFECT"])
                == np.sign(matchup_summary["PPM_CONSTWT_INTERACTION_EFFECT"])
            )
        )
        matchup_summary["TOTAL_SENSITIVITY_SUPPORT"] = (
            (matchup_summary["PPM_CONSTWT_TOTAL_Q_BY"] <= settings.fdr_alpha)
            & (
                np.sign(matchup_summary["PPM_TOTAL_EFFECT"])
                == np.sign(matchup_summary["PPM_CONSTWT_TOTAL_EFFECT"])
            )
        )
        matchup_summary["INTERACTION_NOTABLE"] = (
            eligible
            & (matchup_summary["PPM_INTERACTION_Q_BY"] <= settings.fdr_alpha)
            & matchup_summary["INTERACTION_SENSITIVITY_SUPPORT"]
            & (
                matchup_summary["PPM_INTERACTION_EFFECT_VS_LEAGUE_AVG_PCT"].abs()
                >= settings.min_notable_pct
            )
            & (
                matchup_summary["PPM_INTERACTION_EFFECT"].abs()
                >= settings.min_notable_pts_per_min
            )
        )
        matchup_summary["TOTAL_RATE_NOTABLE"] = (
            eligible
            & (matchup_summary["PPM_TOTAL_Q_BY"] <= settings.fdr_alpha)
            & matchup_summary["TOTAL_SENSITIVITY_SUPPORT"]
            & (
                matchup_summary["PPM_TOTAL_EFFECT_VS_LEAGUE_AVG_PCT"].abs()
                >= settings.min_notable_pct
            )
            & (matchup_summary["PPM_TOTAL_EFFECT"].abs() >= settings.min_notable_pts_per_min)
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
                ].rename(columns={"PPM_INTERACTION_EFFECT": f"INTERACTION_PPM_{label}"})
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

        self._scoring = {
            "summary": matchup_summary,
            "points_fit": points_fit,
            "minutes_fit": minutes_fit,
            "rate_fit": rate_fit,
            "ppm_fit": ppm_fit,
            "ppm_constant_weight_fit": ppm_constant_weight_fit,
            "eligible": eligible,
            "diagnostics": {
                "eligible_cells": int(eligible.sum()),
                "interaction_findings": int(matchup_summary["INTERACTION_NOTABLE"].sum()),
                "total_rate_findings": int(matchup_summary["TOTAL_RATE_NOTABLE"].sum()),
                "split_half_cells": len(eligible_halves),
            },
            "pearson_reliability": float(pearson_reliability),
            "spearman_reliability": float(spearman_reliability),
            "tau_ppm": float(np.sqrt(tau_squared)),
            "mean_shrinkage": float(
                matchup_summary.loc[eligible, "SHRINKAGE_WEIGHT"].mean()
            ),
        }
        return matchup_summary

    def fit_volume(self):
        if self._volume is not None:
            return self._volume["summary"]
        logs = self.prepare_logs()
        settings = self.settings
        league_average_volume_rates = self._league_average_volume_rates

        volume_effect_frames = []
        for metric, metric_label in settings.volume_metrics.items():
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

        sample_sizes = (
            logs.groupby(["SUBTYPE_ID", "OPP_TEAM"], as_index=False)
            .agg(
                PLAYERS=("PLAYER_ID", "nunique"),
                PLAYER_GAMES=("PLAYER_ID", "size"),
                DISTINCT_GAMES=("GAME_ID", "nunique"),
                OFFENSIVE_TEAMS=("TEAM_ABBREVIATION", "nunique"),
            )
        )
        volume_matchup_summary = volume_matchup_summary.merge(
            sample_sizes,
            on=["SUBTYPE_ID", "OPP_TEAM"],
            how="left",
            validate="many_to_one",
        )
        subtype_volume_rates = {
            metric: (
                logs.groupby("SUBTYPE_ID")[metric].sum()
                / logs.groupby("SUBTYPE_ID")["MIN"].sum()
            )
            for metric in settings.volume_metrics
        }
        volume_matchup_summary["LEAGUE_AVG_RATE"] = volume_matchup_summary[
            "METRIC"
        ].map(league_average_volume_rates)
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
        volume_eligible = volume_matchup_summary["ELIGIBLE_FOR_INFERENCE"] = (
            (volume_matchup_summary["PLAYERS"] >= settings.min_cell_players)
            & (volume_matchup_summary["DISTINCT_GAMES"] >= settings.min_cell_games)
            & (
                volume_matchup_summary["OFFENSIVE_TEAMS"]
                >= settings.min_cell_offensive_teams
            )
            & volume_matchup_summary["VOLUME_INTERACTION_P_VALUE"].notna()
        )

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
            (volume_matchup_summary["CONSTWT_INTERACTION_Q_BY"] <= settings.fdr_alpha)
            & (
                np.sign(volume_matchup_summary["VOLUME_INTERACTION_EFFECT"])
                == np.sign(volume_matchup_summary["VOLUME_CONSTWT_INTERACTION_EFFECT"])
            )
        )
        volume_matchup_summary["TOTAL_SENSITIVITY_SUPPORT"] = (
            (volume_matchup_summary["CONSTWT_TOTAL_Q_BY"] <= settings.fdr_alpha)
            & (
                np.sign(volume_matchup_summary["VOLUME_TOTAL_EFFECT"])
                == np.sign(volume_matchup_summary["VOLUME_CONSTWT_TOTAL_EFFECT"])
            )
        )
        volume_matchup_summary["INTERACTION_NOTABLE"] = (
            volume_eligible
            & (volume_matchup_summary["INTERACTION_Q_BY"] <= settings.fdr_alpha)
            & volume_matchup_summary["INTERACTION_SENSITIVITY_SUPPORT"]
            & (
                volume_matchup_summary["INTERACTION_EFFECT_VS_LEAGUE_AVG_PCT"].abs()
                >= settings.min_notable_pct
            )
        )
        volume_matchup_summary["TOTAL_RATE_NOTABLE"] = (
            volume_eligible
            & (volume_matchup_summary["TOTAL_Q_BY"] <= settings.fdr_alpha)
            & volume_matchup_summary["TOTAL_SENSITIVITY_SUPPORT"]
            & (
                volume_matchup_summary["TOTAL_EFFECT_VS_LEAGUE_AVG_PCT"].abs()
                >= settings.min_notable_pct
            )
        )

        volume_matchup_summary["SHRINKAGE_WEIGHT"] = np.nan
        volume_matchup_summary["SHRUNK_INTERACTION_EFFECT"] = np.nan
        volume_matchup_summary["SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"] = np.nan
        volume_tau_rows = []
        for metric, metric_frame in volume_matchup_summary.loc[
            volume_eligible
        ].groupby("METRIC"):
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

        median_game_date = (
            logs[["GAME_ID", "GAME_DATE"]].drop_duplicates("GAME_ID")["GAME_DATE"].median()
        )
        volume_half_effects = []
        for label, half in [
            ("FIRST_HALF", logs.loc[logs["GAME_DATE"] <= median_game_date]),
            ("SECOND_HALF", logs.loc[logs["GAME_DATE"] > median_game_date]),
        ]:
            metric_half_effects = []
            for metric, metric_label in settings.volume_metrics.items():
                half_fit = fit_model(half, f"{metric}_PER_MIN", weight_column="MIN")
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
                    columns={"VOLUME_INTERACTION_EFFECT": f"INTERACTION_RATE_{label}"}
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

        reliability_rows = {}
        for metric, metric_frame in volume_matchup_summary.loc[
            volume_matchup_summary["ELIGIBLE_FOR_INFERENCE"]
        ].groupby("METRIC"):
            paired = metric_frame[
                ["INTERACTION_RATE_FIRST_HALF", "INTERACTION_RATE_SECOND_HALF"]
            ].dropna()
            reliability_rows[metric] = {
                "METRIC": metric,
                "CELLS": len(paired),
                "PEARSON": paired.corr(method="pearson").iloc[0, 1],
                "SPEARMAN": paired.corr(method="spearman").iloc[0, 1],
            }
        for metric in settings.volume_metrics:
            if metric not in reliability_rows:
                reliability_rows[metric] = {
                    "METRIC": metric,
                    "CELLS": 0,
                    "PEARSON": np.nan,
                    "SPEARMAN": np.nan,
                }
        volume_reliability = pd.DataFrame(
            list(reliability_rows.values()), columns=["METRIC", "CELLS", "PEARSON", "SPEARMAN"]
        )
        if volume_shrinkage_summary.empty:
            volume_reliability["TRUE_INTERACTION_SD"] = np.nan
            volume_reliability["MEAN_SHRINKAGE_WEIGHT"] = np.nan
        else:
            volume_reliability = volume_reliability.merge(
                volume_shrinkage_summary, on="METRIC", validate="one_to_one"
            )

        self._volume = {
            "summary": volume_matchup_summary,
            "volume_reliability": volume_reliability,
            "volume_shrinkage_summary": volume_shrinkage_summary,
        }
        return volume_matchup_summary

    def assemble_artifacts(self):
        if self._artifacts is not None:
            return self._artifacts
        self._require_identity()
        logs = self.prepare_logs()
        self.fit_scoring()
        self.fit_volume()
        settings = self.settings
        league_average_ppm = self._league_average_ppm

        matchup_summary = self._scoring["summary"]
        volume_matchup_summary = self._volume["summary"]
        eligible = self._scoring["eligible"]

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
            player_baselines["PLAYER_TOTAL_PTS"] / player_baselines["PLAYER_TOTAL_MINUTES"]
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
            player_relative_matchups["OPP_TEAM"] != player_relative_matchups["CURRENT_TEAM"]
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
        player_volume_frames = []
        for metric, metric_label in settings.volume_metrics.items():
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
                ABS_SHRUNK=lambda frame: frame["SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"].abs()
            )
            .sort_values(["ABS_SHRUNK", "OFFENSIVE_TEAMS"], ascending=[False, False])
            .head(25)
            .drop(columns="ABS_SHRUNK")
        )
        validated_volume_interactions = volume_matchup_summary.loc[
            volume_matchup_summary["INTERACTION_NOTABLE"]
        ].sort_values(["METRIC", "INTERACTION_Q_BY"])

        # Matrix rows and display labels derive from the complete model
        # membership, not from whichever subtypes happen to have usable game
        # logs: a membership subtype with no eligible cells is still an explicit
        # all-missing row so the matrix always agrees with the run's recorded
        # cluster count and stable subtype keys.
        membership_subtypes = sorted(self._membership["SUBTYPE_ID"].unique())
        subtype_labels = (
            self._membership[["SUBTYPE_ID", "SUBTYPE_ARCHETYPE"]]
            .drop_duplicates("SUBTYPE_ID")
            .assign(
                DISPLAY_LABEL=lambda frame: (
                    frame["SUBTYPE_ID"].astype(str)
                    + " — "
                    + frame["SUBTYPE_ARCHETYPE"]
                )
            )
            .set_index("SUBTYPE_ID")["DISPLAY_LABEL"]
            .reindex(membership_subtypes)
        )
        tau_ppm = self._scoring["tau_ppm"]
        heatmap_data = matchup_summary.assign(
            DISPLAY_VALUE=lambda frame: frame["SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"].where(
                frame["ELIGIBLE_FOR_INFERENCE"]
            )
        ).pivot(index="SUBTYPE_ID", columns="OPP_TEAM", values="DISPLAY_VALUE")
        heatmap_data = heatmap_data.reindex(
            index=membership_subtypes, columns=sorted(heatmap_data.columns)
        )
        heatmap_data.index = heatmap_data.index.map(subtype_labels)
        heatmap_limit = max(2 * tau_ppm / league_average_ppm * 100, 1.0)

        volume_heatmaps = {}
        for metric in settings.volume_metrics:
            metric_frame = volume_matchup_summary.loc[
                volume_matchup_summary["METRIC"] == metric
            ]
            metric_heatmap = metric_frame.assign(
                DISPLAY_VALUE=lambda frame: frame[
                    "SHRUNK_INTERACTION_VS_LEAGUE_AVG_PCT"
                ].where(frame["ELIGIBLE_FOR_INFERENCE"])
            ).pivot(index="SUBTYPE_ID", columns="OPP_TEAM", values="DISPLAY_VALUE")
            metric_heatmap = metric_heatmap.reindex(
                index=membership_subtypes, columns=sorted(metric_heatmap.columns)
            )
            metric_heatmap.index = metric_heatmap.index.map(subtype_labels)
            finite_values = np.abs(metric_heatmap.to_numpy())
            finite_values = finite_values[np.isfinite(finite_values)]
            metric_limit = (
                np.quantile(finite_values, 0.95) if finite_values.size else np.nan
            )
            metric_limit = max(metric_limit, 1.0)
            volume_heatmaps[metric] = {"data": metric_heatmap, "limit": metric_limit}

        self._artifacts = {
            "matchup_summary": matchup_summary.sort_values(["SUBTYPE_ID", "OPP_TEAM"]),
            "notable_matchups": notable_matchups,
            "validated_interactions": validated_interactions,
            "watchlist": descriptive_watchlist,
            "player_relative_matchups": player_relative_matchups.sort_values(
                ["PLAYER_NAME", "OPP_TEAM"]
            ),
            "volume_matchup_summary": volume_matchup_summary.sort_values(
                ["METRIC", "SUBTYPE_ID", "OPP_TEAM"]
            ),
            "validated_volume_interactions": validated_volume_interactions,
            "volume_reliability": self._volume["volume_reliability"],
            "player_relative_volume_matchups": player_relative_volume_matchups.sort_values(
                ["PLAYER_NAME", "METRIC", "OPP_TEAM"]
            ),
            "pts_per_min_heatmap": heatmap_data,
            "volume_heatmaps": volume_heatmaps,
            "identity": self._run_identity,
        }
        self._pts_per_min_heatmap_limit = heatmap_limit
        self._subtype_labels = subtype_labels
        # Every artifact frame is attributed to this exact run and model by a
        # content digest bound to the run/model identity, so artifact assembly
        # and dashboard assembly reject a frame that was replaced with content
        # assembled under a different run identity. Heatmap digests cover the
        # display-label index and the limit that controls the rendered PNG, so
        # relabeling a heatmap or changing its limit after assembly is caught.
        artifact_digests = {
            name: self._expected_artifact_digest(name)
            for name in self._artifacts
            if isinstance(self._artifacts[name], pd.DataFrame)
        }
        for metric in self._artifacts["volume_heatmaps"]:
            artifact_digests[f"volume_heatmap__{metric}"] = self._expected_artifact_digest(
                f"volume_heatmap__{metric}"
            )
        artifact_digests["subtype_labels"] = self._expected_artifact_digest(
            "subtype_labels"
        )
        self._artifact_digests = FrozenDict(artifact_digests)
        self._artifacts["artifact_digests"] = self._artifact_digests
        return self._artifacts

    def _expected_artifact_digest(self, name) -> str:
        """The run-bound content digest expected for the named artifact.

        Heatmap digests also cover the display-label index and the heatmap
        limit, because both are artifact semantics: the index is the rendered
        row labels and the limit is the color scale bounds used when the PNG is
        drawn. ``subtype_labels`` (the payload's label mapping) is bound by its
        own digest so a relabeling cannot silently diverge from the heatmaps.
        """
        if name == "subtype_labels":
            frame = self._subtype_labels.to_frame(name="DISPLAY_LABEL")
            limit = None
            include_index = True
        elif name.startswith("volume_heatmap__"):
            heatmap = self._artifacts["volume_heatmaps"][
                name.removeprefix("volume_heatmap__")
            ]
            frame = heatmap["data"]
            limit = heatmap["limit"]
            include_index = True
        elif name == "pts_per_min_heatmap":
            frame = self._artifacts[name]
            limit = self._pts_per_min_heatmap_limit
            include_index = True
        else:
            frame = self._artifacts[name]
            limit = None
            include_index = False
        parts = [
            "artifact",
            self._run_id,
            self._model_version,
            name,
            _frame_canonical_text(frame, include_index=include_index),
        ]
        if limit is not None:
            parts.append(limit)
        return content_hash(tuple(parts))

    def assemble_dashboard_payload(self):
        if self._dashboard_payload is not None:
            return self._dashboard_payload
        self.fit_scoring()
        self.fit_volume()
        artifacts = self.assemble_artifacts()

        bundle_identity = artifacts.get("identity")
        if bundle_identity is not self._run_identity:
            raise ValueError(
                "Artifact bundle identity is missing or does not match the run identity"
            )
        # The expected digest record is the immutable FrozenDict captured at
        # artifact assembly; replacing it (or removing it) is rejected rather
        # than silently trusting a rewritten expectation.
        if artifacts.get("artifact_digests") is not self._artifact_digests:
            raise ValueError(
                "Artifact digest record is missing or was replaced"
            )
        # Reject any artifact frame that was replaced with content assembled
        # under another run: each artifact is bound to this run and model by its
        # recorded content digest.
        for name, expected in self._artifact_digests.items():
            observed = self._expected_artifact_digest(name)
            if observed != expected:
                raise ValueError(
                    f"Artifact {name} does not match the run's assembled content"
                )

        validated_interactions = artifacts["validated_interactions"]
        validated_volume_interactions = artifacts["validated_volume_interactions"]
        volume_reliability = self._volume["volume_reliability"]

        payload = {
            "coverage": self._coverage,
            "diagnostics": self._scoring["diagnostics"],
            "pearson_reliability": self._scoring["pearson_reliability"],
            "spearman_reliability": self._scoring["spearman_reliability"],
            "tau_ppm": self._scoring["tau_ppm"],
            "mean_shrinkage": self._scoring["mean_shrinkage"],
            "subtype_labels": self._subtype_labels,
            "pts_per_min_heatmap_limit": self._pts_per_min_heatmap_limit,
            "validated_interactions": validated_interactions[
                NOTABLE_DISPLAY_COLUMNS
            ],
            "validated_volume_interactions": validated_volume_interactions[
                VOLUME_DISPLAY_COLUMNS
            ],
            "volume_reliability_display": volume_reliability.round(3),
            "identity": self._run_identity,
            "artifact_digests": self._artifact_digests,
            "provenance": self._provenance.to_dict(),
        }
        self._dashboard_payload = payload
        return payload

    def build(self):
        self.load_membership()
        self.prepare_logs()
        self.fit_scoring()
        self.fit_volume()
        self.assemble_artifacts()
        self.assemble_dashboard_payload()
        scoring = self._scoring
        volume = self._volume
        return AnalysisRun(
            _membership=self._membership,
            _logs=self._logs,
            _coverage=self._coverage,
            _scoring_fits=scoring,
            _matchup_summary=scoring["summary"],
            _volume_fits=volume,
            _volume_matchup_summary=volume["summary"],
            _volume_reliability=volume["volume_reliability"],
            _volume_shrinkage_summary=volume["volume_shrinkage_summary"],
            _artifacts=self._artifacts,
            _dashboard_payload=self._dashboard_payload,
            identity=self._run_identity,
            model_spec=self.model_spec,
            provenance=self._provenance,
        )
