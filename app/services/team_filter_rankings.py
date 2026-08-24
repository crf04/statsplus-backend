"""Season Rankings for the game-log Team Filters, read from publications.

Every ``teams_against`` filter ranks the 30 opponents by a whole-Regular-Season
aggregate taken from the durable window-aware team matchup publications.  There
is no governed-window parameter and no request-time provider call: the Season
stream is the only source, and a stale newest publication still serves its
last-good values rather than degrading the ranking.

A ranking is all thirty opponents or nothing.  NBA-owned streams already prove
the canonical league at their decode boundary; the ledger-owned traditional and
assist-location streams do not, so this module proves it for every base rather
than presenting a partial league as a complete ranking.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import (
    NBA_PUBLICATION_STREAMS,
    PLAY_TYPES,
    matchup_stream_key,
)
from app.models.catalogs import LESS_THAN_TEN_FEET_FILTER

logger = logging.getLogger(__name__)

#: Every Team Filter ranks the Season window.
_WINDOW = "season"

_TRADITIONAL = "traditional"
_ASSIST_LOCATIONS = "assist_locations"
_SHOT_TYPES = "shot_types"
_PLAY_TYPES_BASE = "play_types"

_STREAM_KEY_BY_BASE: dict[str, str] = {
    _TRADITIONAL: matchup_stream_key(_TRADITIONAL, _WINDOW),
    _ASSIST_LOCATIONS: matchup_stream_key(_ASSIST_LOCATIONS, _WINDOW),
    **{
        base: template.format(window=_WINDOW)
        for base, template in NBA_PUBLICATION_STREAMS.items()
    },
}


@dataclass(frozen=True, slots=True)
class TeamFilterRanking:
    """How one Team Filter is computed from a publication's per-48 metrics.

    ``numerator`` is a linear combination of metric keys; ``denominator``
    turns it into a rate (play types are points per possession).
    """

    base: str
    numerator: tuple[tuple[str, float], ...]
    denominator: str | None = None

    def value(self, per48: Mapping[str, float]) -> float | None:
        """Score one team, or ``None`` when the publication cannot evidence it."""

        try:
            total = sum(
                coefficient * float(per48[key])
                for key, coefficient in self.numerator
            )
        except (KeyError, TypeError, ValueError):
            return None
        if self.denominator is None:
            return total
        try:
            divisor = float(per48[self.denominator])
        except (KeyError, TypeError, ValueError):
            return None
        return total / divisor if divisor > 0 else None


def _shot_type(slice_key: str, stat_key: str) -> TeamFilterRanking:
    return TeamFilterRanking(
        _SHOT_TYPES, ((f"{slice_key}_{stat_key}", 1.0),)
    )


def _shot_type_points(slice_key: str) -> TeamFilterRanking:
    return TeamFilterRanking(
        _SHOT_TYPES,
        ((f"{slice_key}_FG3M", 3.0), (f"{slice_key}_FG2M", 2.0)),
    )


def _traditional(metric_key: str) -> TeamFilterRanking:
    return TeamFilterRanking(_TRADITIONAL, ((metric_key, 1.0),))


#: The assist-location filter names and the ledger metric each one ranks.
_ASSIST_LOCATION_METRICS: dict[str, str] = {
    "TwoPtAssists": "two_point_assists",
    "ThreePtAssists": "three_point_assists",
    "Arc3Assists": "arc3_assists",
    "Corner3Assists": "corner3_assists",
    "AtRimAssists": "at_rim_assists",
    "ShortMidRangeAssists": "short_mid_range_assists",
    "LongMidRangeAssists": "long_mid_range_assists",
}

#: One canonical definition per Team Filter.  ``SUPPORTED_TEAM_FILTERS`` and
#: this map are proven equal by the catalog test, so a new filter cannot be
#: accepted by the request model without a ranking definition.
TEAM_FILTER_RANKINGS: dict[str, TeamFilterRanking] = {
    "OPP_PTS": _traditional("points"),
    "OPP_REB": _traditional("rebounds"),
    "OPP_AST": _traditional("assists"),
    "OPP_STOCKS": TeamFilterRanking(
        _TRADITIONAL, (("blocks", 1.0), ("steals", 1.0))
    ),
    "OPP_FTA": _traditional("free_throws_attempted"),
    "OPP_TOV": _traditional("turnovers"),
    "OPP_BLK": _traditional("blocks"),
    "OPP_STL": _traditional("steals"),
    "OPP_FG3M": _traditional("three_pointers_made"),
    "OPP_FG3A": _traditional("three_pointers_attempted"),
    "C&S 3s": _shot_type("catch_and_shoot", "FG3M"),
    "C&S PTS": _shot_type_points("catch_and_shoot"),
    "C&S 3A": _shot_type("catch_and_shoot", "FG3A"),
    "PU 2s": _shot_type("pullups", "FG2M"),
    "PU 3s": _shot_type("pullups", "FG3M"),
    "PU PTS": _shot_type_points("pullups"),
    LESS_THAN_TEN_FEET_FILTER: _shot_type("less_than_10_ft", "FG2M"),
    **{
        play_type: TeamFilterRanking(
            _PLAY_TYPES_BASE,
            ((f"{play_type}_PTS", 1.0),),
            denominator=f"{play_type}_POSS",
        )
        for play_type in PLAY_TYPES
    },
    **{
        team_filter: TeamFilterRanking(_ASSIST_LOCATIONS, ((metric_key, 1.0),))
        for team_filter, metric_key in _ASSIST_LOCATION_METRICS.items()
    },
}


class TeamFilterRankingService:
    """Rank opponents for one Team Filter from its Season publication."""

    def __init__(self, publication_reader) -> None:
        self.publication_reader = publication_reader

    def ranked_teams(self, team_filter: str, season: str) -> list[str]:
        """Return the thirty team tricodes for one season, most-allowed first.

        A publication that is unavailable, does not carry the canonical league,
        or cannot score every team ranks nothing.  The caller resolves that
        into an empty opponent set rather than a new error or a provider call.
        """

        ranking = TEAM_FILTER_RANKINGS.get(team_filter)
        if ranking is None:
            raise ValueError(f"Unsupported team filter: {team_filter!r}")
        rows = self._rows(ranking.base, season)
        if rows is None:
            return []
        scored = []
        for row in rows:
            value = ranking.value(row.per48)
            if value is None:
                logger.warning(
                    "Team Filter %s cannot score team %s from its Season "
                    "publication; refusing a partial ranking",
                    team_filter,
                    row.team_id,
                )
                return []
            scored.append((value, row.team_tricode))
        # Descending by value; the tricode breaks ties so one publication
        # always produces one ranking.
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tricode for _value, tricode in scored]

    def _rows(self, base: str, season: str):
        """Read one Season publication, including a stale last-good one."""

        if self.publication_reader is None:
            return None
        stream_key = _STREAM_KEY_BY_BASE[base]
        read = self.publication_reader.read(stream_key, season=season)
        if not read.available or read.decoded is None:
            logger.info(
                "Team Filter rankings unavailable for %s: status=%s reason=%s",
                stream_key,
                read.status,
                read.unavailable_reason,
            )
            return None
        rows = read.decoded
        if {row.team_id for row in rows} != set(NBA_TEAM_ID_TO_TRICODE):
            # NBA-owned streams already proved this at decode; the ledger-owned
            # traditional and assist-location streams do not, and a partial
            # league would rank a plausible but wrong top-N.
            logger.warning(
                "%s publication does not carry the canonical league; refusing "
                "a partial Team Filter ranking",
                stream_key,
            )
            return None
        return rows


__all__ = [
    "TEAM_FILTER_RANKINGS",
    "TeamFilterRanking",
    "TeamFilterRankingService",
]
