"""Season Rankings for the game-log Team Filters, read from publications.

Every ``teams_against`` filter ranks the 30 opponents by a whole-Regular-Season
aggregate taken from the durable window-aware team matchup publications.  There
is no governed-window parameter and no request-time provider call: the Season
stream is the only source, and a stale newest publication still serves its
last-good values rather than degrading the ranking.

The publication is all thirty opponents or nothing.  NBA-owned streams already
prove the canonical league at their decode boundary; the ledger-owned
traditional and assist-location streams do not, so this module proves it for
every base rather than presenting a partial league as a complete ranking.  One
team may still be absent from one filter, and only where it has no rate to
rank at all -- a play type it faced zero possessions of -- which excludes it
from both the strongest and the weakest end.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

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
        """Score one team, or ``None`` when the team has no rate to rank.

        A metric this ranking needs must be present and finite: its absence or
        corruption is invalid evidence that got past the decode boundary, and
        it raises rather than quietly scoring the team.  ``None`` is the one
        narrow legitimate case -- a team that never faced this play type has no
        points-per-possession at all, which is not the same as allowing none.
        A zero denominator carrying a non-zero numerator is not that case: it
        is contradictory evidence (points scored across no possessions) and
        raises like any other corrupt cell.
        """

        total = _finite(sum(
            coefficient * _finite(per48[key])
            for key, coefficient in self.numerator
        ))
        if self.denominator is None:
            return total
        divisor = _finite(per48[self.denominator])
        if divisor > 0:
            return _finite(total / divisor)
        if total:
            raise ValueError(
                "a per-48 rate cannot carry a numerator across no denominator"
            )
        return None


def _finite(value) -> float:
    """Coerce one published metric, refusing a non-numeric or unbounded cell.

    The decode boundary already proves every published cell is a finite,
    non-negative, non-boolean number.  This repeats the proof because the
    decoded rows arrive through an injected reader, and because a derived sum
    or quotient can overflow to infinity from operands that were each finite.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("a published per-48 metric must be numeric")
    number = float(value)
    if not isfinite(number):
        raise ValueError("a published per-48 metric must be finite")
    return number


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
    """Rank opponents for Team Filters from their Season publications."""

    def __init__(self, publication_reader) -> None:
        self.publication_reader = publication_reader

    def ranked_teams(self, team_filter: str, season: str) -> list[str]:
        """Return the thirty team tricodes for one season, most-allowed first."""

        return self.rank_all((team_filter,), season)[team_filter]

    def rank_all(self, team_filters, season: str) -> dict[str, list[str]]:
        """Rank several Team Filters from one publication generation.

        Every filter in a request is answered from a single snapshot, so a
        multi-filter Filter Set can never intersect two generations that never
        coexisted, and two filters sharing a base cost one read rather than two.
        """

        requested = tuple(team_filters)
        rankings = {}
        for team_filter in requested:
            if team_filter not in TEAM_FILTER_RANKINGS:
                raise ValueError(f"Unsupported team filter: {team_filter!r}")
        bases = {TEAM_FILTER_RANKINGS[name].base for name in requested}
        rows_by_base = self._rows_by_base(bases, season)
        for team_filter in requested:
            ranking = TEAM_FILTER_RANKINGS[team_filter]
            rankings[team_filter] = self._rank(
                team_filter, ranking, rows_by_base[ranking.base]
            )
        return rankings

    @staticmethod
    def _rank(team_filter: str, ranking: TeamFilterRanking, rows) -> list[str]:
        if rows is None:
            return []
        scored = []
        for row in rows:
            try:
                value = ranking.value(row.per48)
            except (KeyError, TypeError, ValueError):
                # The publication proved its own taxonomy at decode, so a
                # metric that cannot be read here is invalid evidence for the
                # whole surface rather than for one team.
                logger.warning(
                    "Team Filter %s cannot read its metrics from the Season "
                    "publication; refusing to rank",
                    team_filter,
                )
                return []
            if value is None:
                # A team with no rate for this filter is simply not ranked by
                # it; the other twenty-nine remain a complete answer.
                logger.debug(
                    "Team %s has no %s rate in the Season publication",
                    row.team_tricode,
                    team_filter,
                )
                continue
            scored.append((value, row.team_tricode))
        # Descending by value; the tricode breaks ties so one publication
        # always produces one ranking.
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tricode for _value, tricode in scored]

    def _rows_by_base(self, bases, season: str) -> dict:
        """Read every needed Season publication in one generation."""

        if self.publication_reader is None:
            return {base: None for base in bases}
        stream_by_base = {base: _STREAM_KEY_BY_BASE[base] for base in bases}
        reads = self.publication_reader.read_many(
            tuple(stream_by_base.values()), season=season
        )
        return {
            base: self._league_rows(reads[stream_key], stream_key)
            for base, stream_key in stream_by_base.items()
        }

    @staticmethod
    def _league_rows(read, stream_key: str):
        """Return the canonical thirty rows, or ``None`` to refuse ranking."""

        if not read.available or read.decoded is None:
            logger.info(
                "Team Filter rankings unavailable for %s: status=%s reason=%s",
                stream_key,
                read.status,
                read.unavailable_reason,
            )
            return None
        rows = read.decoded
        # NBA-owned streams already proved the canonical league and its
        # identities at decode; the ledger-owned traditional and
        # assist-location streams did not, and a partial or mislabelled league
        # would rank a plausible but wrong top-N.
        if {row.team_id for row in rows} != set(NBA_TEAM_ID_TO_TRICODE) or any(
            row.team_tricode != NBA_TEAM_ID_TO_TRICODE[row.team_id]
            for row in rows
        ):
            logger.warning(
                "%s publication does not carry the canonical league identities;"
                " refusing a partial Team Filter ranking",
                stream_key,
            )
            return None
        return rows


__all__ = [
    "TEAM_FILTER_RANKINGS",
    "TeamFilterRanking",
    "TeamFilterRankingService",
]
