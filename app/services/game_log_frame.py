"""One canonical derivation for game-log frames.

Both the live PBP path and the durable stored path build their response
frames from the same canonical primitives, so composite and fantasy values are
calculated exactly once here rather than drifting between providers.  The
legacy NBA path keeps its own provider-valued derivation; this module is the
single derivation used by every PBP-based request-time and durable source.
"""

from __future__ import annotations

import pandas as pd

#: Columns that must already be present before derived values are added.
CANONICAL_GAME_LOG_PRIMITIVE_COLUMNS = (
    "PTS",
    "REB",
    "AST",
    "FGM",
    "FGA",
    "FG3M",
    "FG3A",
    "FTM",
    "FTA",
    "STL",
    "BLK",
    "TOV",
    "PLUS_MINUS",
    "MIN",
)

#: Derived columns added by :func:`derive_game_log_frame`.
DERIVED_GAME_LOG_COLUMNS = (
    "NBA_FANTASY_PTS",
    "FG_PCT",
    "FT_PCT",
    "PRA",
    "PA",
    "PR",
    "RA",
    "STKS",
    "FD_PTS",
    "+/-",
    "FG2M",
    "FG2A",
)


def _safe_ratio(numerator: float, denominator: float) -> float:
    """Return ``numerator/denominator`` or zero for an empty denominator."""

    if not denominator:
        return 0.0
    return numerator / denominator


def derive_game_log_frame(
    frame: pd.DataFrame,
    *,
    round_minutes: bool = True,
) -> pd.DataFrame:
    """Add the shared derived game-log columns to one canonical primitive frame.

    ``MIN`` is rounded to a whole minute for the request-time presentation,
    exactly as the legacy NBA path did; durable ingestion passes
    ``round_minutes=False`` so exact minutes are retained before persistence.
    The fantasy total is one reviewed formula over the canonical primitives, so
    a live PBP response and a stored response can never disagree.
    """

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("game log frame must be a pandas DataFrame")
    derived = frame.copy()
    missing = [
        column
        for column in CANONICAL_GAME_LOG_PRIMITIVE_COLUMNS
        if column not in derived.columns
    ]
    if missing:
        raise ValueError(
            "game log frame is missing canonical primitives: "
            + ", ".join(missing)
        )
    if round_minutes:
        derived["MIN"] = derived["MIN"].round().astype(int)
    derived["NBA_FANTASY_PTS"] = (
        derived["PTS"]
        + derived["REB"] * 1.2
        + derived["AST"] * 1.5
        + (derived["STL"] + derived["BLK"]) * 3
        - derived["TOV"]
    )
    derived["FG_PCT"] = _safe_ratio_series(derived["FGM"], derived["FGA"])
    derived["FT_PCT"] = _safe_ratio_series(derived["FTM"], derived["FTA"])
    derived["PRA"] = derived["PTS"] + derived["REB"] + derived["AST"]
    derived["PA"] = derived["PTS"] + derived["AST"]
    derived["PR"] = derived["PTS"] + derived["REB"]
    derived["RA"] = derived["REB"] + derived["AST"]
    derived["STKS"] = derived["STL"] + derived["BLK"]
    derived["FD_PTS"] = derived["NBA_FANTASY_PTS"]
    derived["+/-"] = derived["PLUS_MINUS"]
    derived["FG2M"] = derived["FGM"] - derived["FG3M"]
    derived["FG2A"] = derived["FGA"] - derived["FG3A"]
    return derived


def _safe_ratio_series(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    nonzero = denominator != 0
    return numerator.div(denominator).where(nonzero, 0.0)


__all__ = [
    "CANONICAL_GAME_LOG_PRIMITIVE_COLUMNS",
    "DERIVED_GAME_LOG_COLUMNS",
    "derive_game_log_frame",
]
