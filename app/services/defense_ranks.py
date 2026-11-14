"""The whole league's Defense Sheet for one base (crf04/statsplus#473).

Each row is a Matchup Defense Sheet row: the same identities, row
references, Season columns, ranks and percent versus league, projected for
all thirty teams at once instead of one opponent.  A league that cannot be
served whole serves no rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import (
    DEFENSE_SHEET_BASES,
    DEFENSE_SHEET_FILTER_PREFIX,
    IMPOSSIBLE_DEFENSE_SHEET_IDENTITIES,
    SHOT_TYPE_DERIVED_STATS,
    defense_sheet_display_slice,
    defense_sheet_identities,
    defense_sheet_row_key,
    defense_sheet_row_label,
    defense_sheet_sort_key,
)
from app.domain.utc import parse_utc_iso
from app.errors import InvalidInputError
from app.services.team_filter_rankings import EASTERN, SeasonLeague
from app.services.team_matchup_query import defense_sheet_columns, publication_league_table

#: The Matchup's wire precision for Sheet numbers; ``vs_league`` is its
#: percent as a fraction, so it carries two more places.
_PRECISION = 6


@dataclass(frozen=True, slots=True)
class TeamRank:
    tricode: str
    value: float
    rank: int
    vs_league: float | None


@dataclass(frozen=True, slots=True)
class DefenseRankRow:
    row: str
    slice: str | None
    metric: str
    label: str
    unit: str
    league_average: float
    teams: tuple[TeamRank, ...]


@dataclass(frozen=True, slots=True)
class DefenseRanks:
    season: str
    base: str
    window: str
    status: str
    reason: str | None
    as_of: str | None
    provenance: dict
    rows: tuple[DefenseRankRow, ...]


def row_reference(base: str, slice_key: str, stat_key: str) -> str:
    """The ``sheet:<base>:<row key>`` a Matchup Sheet row is referenced by."""

    return f"{DEFENSE_SHEET_FILTER_PREFIX}{base}:{defense_sheet_row_key(base, slice_key, stat_key)}"


def sheet_selection(base: str, metric: str | None) -> frozenset[tuple[str, str]]:
    """The ``(slice_key, stat_key)`` rows of ``base`` that ``metric`` names.

    ``metric`` is a row's stat (every slice's row of it) or its full row
    reference; ``None`` selects every row.
    """

    if base not in DEFENSE_SHEET_BASES:
        raise InvalidInputError(
            f"Unsupported defense ranks base: {base}.", public_details={"parameters": ["base"]}
        )
    identities = {(slice_key, stat_key) for slice_key, stat_key, _ in defense_sheet_identities(base)}
    if base == "shot_types":
        identities |= {
            (slice_key, stat_key) for slice_key, _ in identities for stat_key in SHOT_TYPE_DERIVED_STATS
        }
    identities = {
        identity
        for identity in identities
        if (base, *identity) not in IMPOSSIBLE_DEFENSE_SHEET_IDENTITIES
        and metric in (None, identity[1], row_reference(base, *identity))
    }
    if not identities:
        raise InvalidInputError(
            f"Unsupported defense ranks metric for {base}: {metric}.",
            public_details={"parameters": ["metric"]},
        )
    return frozenset(identities)


def defense_ranks(base: str, selection: frozenset[tuple[str, str]], league: SeasonLeague) -> DefenseRanks:
    """Project the selected Sheet rows of one base's Season league."""

    if league.rows is None:
        return DefenseRanks(
            league.season,
            base,
            "season",
            "unavailable",
            league.unavailable_reason,
            None,
            league.provenance,
            (),
        )
    columns = sorted(
        (
            (slice_key, stat_key, column)
            for slice_key, stat_key, column in defense_sheet_columns(
                base, publication_league_table(league.rows)
            )
            if (slice_key, stat_key) in selection
        ),
        key=lambda item: defense_sheet_sort_key((base, item[0], item[1])),
    )
    rows = []
    for slice_key, stat_key, column in columns:
        display_slice = defense_sheet_display_slice(base, slice_key)
        teams = sorted(
            (
                TeamRank(
                    tricode=NBA_TEAM_ID_TO_TRICODE[team_id],
                    value=round(value, _PRECISION),
                    rank=column.rank(team_id),
                    vs_league=(
                        None
                        if (percent := column.percent_vs_league_average(team_id)) is None
                        else round(percent / 100, _PRECISION + 2)
                    ),
                )
                for team_id, value in column.values.items()
            ),
            key=lambda team: (-team.rank, team.tricode),
        )
        rows.append(
            DefenseRankRow(
                row=row_reference(base, slice_key, stat_key),
                slice=None if display_slice == stat_key else display_slice,
                metric=stat_key,
                label=defense_sheet_row_label(base, slice_key, stat_key),
                unit="per48",
                league_average=round(column.average, _PRECISION),
                teams=tuple(teams),
            )
        )
    as_of = (
        None if league.cutoff is None else parse_utc_iso(league.cutoff).astimezone(EASTERN).date().isoformat()
    )
    return DefenseRanks(league.season, base, "season", "ok", None, as_of, league.provenance, tuple(rows))
