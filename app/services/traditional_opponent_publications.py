"""The traditional-opponent publication family and its explicit formats.

The Season and Last 15 traditional-opponent publications are immutable: a
payload composed months ago is still the exact bytes the product serves today.
That makes the set of metrics a publication carries a *format*, not a setting.
Before this module the two ideas were one mutable global tuple, so widening
what new publications produce also retroactively invalidated every publication
already active.

This module is the one place that knows:

* which rendered formats of this family exist (:data:`TRADITIONAL_OPPONENT_V1`
  and :data:`TRADITIONAL_OPPONENT_V2`), each with a stable in-code name and a
  deterministic fingerprint over its exact metric taxonomy and invariants;
* how to recognize a stored payload as exactly one of them, refusing arbitrary
  subsets and supersets rather than guessing;
* what each format is *able* to serve, expressed as capabilities, so a valid
  v1 publication keeps serving all of its own metrics and simply reports the
  rebound split as unavailable instead of fabricating zeros or nulls;
* the format-specific semantic invariants -- in v2, that a team's total
  rebounds are exactly its offensive plus defensive rebounds, in both the
  integer counts and the served per-48 values.

Consumers ask for a normalized window and read capabilities; they never branch
on which stored generation produced the payload.

Deliberately scoped to this one publication family.  A second family with the
same need is the evidence that would justify generalizing the pattern.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from app.services.ledger_derivations import TEAM_METRICS, competition_ranks


class TraditionalOpponentFormatError(ValueError):
    """A traditional-opponent payload is not exactly one supported format.

    Carries a stable ``reason`` so an operator-facing surface can report a
    bounded code without echoing payload contents.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


#: The publication family this module owns.
TRADITIONAL_OPPONENT_FAMILY = "traditional_opponent"

#: The two windows of the family, in their canonical promotion order.  They
#: move together: the product must never observe one window in v1 while the
#: other is in v2.
TRADITIONAL_OPPONENT_STREAM_KEYS = (
    "traditional_opponent_season",
    "traditional_opponent_l15",
)

_WINDOW_KIND_BY_STREAM = MappingProxyType({
    "traditional_opponent_season": "season",
    "traditional_opponent_l15": "l15",
})

#: The player-credited rebound split v2 adds.  Team-only rebounds are excluded
#: from both, exactly as they are from the existing ``rebounds`` total.
REBOUND_SPLIT_METRICS = ("offensive_rebounds", "defensive_rebounds")

#: The capability name a consumer asks about before rendering the split.
REBOUND_SPLIT = "rebound_split"

#: Every mapping a published team row carries one entry per metric in.  A
#: format is only recognized when *all* of them carry the identical taxonomy;
#: a payload that ranks metrics it does not count is not a coherent format.
TRADITIONAL_OPPONENT_METRIC_BLOCKS = (
    "counts",
    "per48",
    "league_average",
    "population_sigma",
    "competition_rank",
)

#: The largest per-48 drift accepted when proving the rebound identity over
#: derived floating values.  Counts are integers and must agree exactly.
_PER48_IDENTITY_TOLERANCE = 1e-6

#: The canonical league size.  A ranked metric computed over fewer teams is a
#: different statistic, not a partial one, so an incomplete population fails
#: the whole publication rather than degrading it.
TRADITIONAL_OPPONENT_LEAGUE_SIZE = 30


@dataclass(frozen=True, slots=True)
class TraditionalOpponentFormat:
    """One exact rendered format of the traditional-opponent family.

    ``metrics`` is the complete taxonomy every metric block must carry.
    ``capabilities`` is what a consumer may ask for.  ``invariants`` names the
    semantic rules a payload in this format must satisfy; it is part of the
    fingerprint, so tightening a rule produces a new fingerprint even when the
    taxonomy is unchanged.
    """

    name: str
    metrics: tuple[str, ...]
    capabilities: frozenset[str]
    invariants: tuple[str, ...] = ()

    @property
    def metric_set(self) -> frozenset[str]:
        return frozenset(self.metrics)

    @property
    def fingerprint(self) -> str:
        """A deterministic digest of this format's taxonomy and invariants.

        Recorded in rebuild audit evidence so operational history names the
        exact contract a publication was rebuilt into, without adding a
        nominal schema field to every stored payload.
        """

        return hashlib.sha256(
            json.dumps(
                {
                    "name": self.name,
                    "metrics": sorted(self.metrics),
                    "capabilities": sorted(self.capabilities),
                    "invariants": sorted(self.invariants),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


#: The format every publication active before the rebound split.  It serves
#: every metric it ever served; it simply has no split to offer.
TRADITIONAL_OPPONENT_V1 = TraditionalOpponentFormat(
    name="traditional_opponent.v1",
    metrics=tuple(TEAM_METRICS),
    capabilities=frozenset(),
)

#: The format that adds the player-credited rebound split.  The split is
#: canonical here: the total is derived from it rather than observed beside it,
#: so the three values can never disagree.
TRADITIONAL_OPPONENT_V2 = TraditionalOpponentFormat(
    name="traditional_opponent.v2",
    metrics=tuple(TEAM_METRICS) + REBOUND_SPLIT_METRICS,
    capabilities=frozenset({REBOUND_SPLIT}),
    invariants=("rebounds == offensive_rebounds + defensive_rebounds",),
)

#: Exactly the formats this deployment accepts.  During the compatibility
#: window both are readable; the contract deployment that removes v1 shortens
#: this tuple and nothing else.
SUPPORTED_TRADITIONAL_OPPONENT_FORMATS = (
    TRADITIONAL_OPPONENT_V1,
    TRADITIONAL_OPPONENT_V2,
)

#: The format this deployment composes.  A rebuild request never names a
#: target: the deployed module owns it, so an operator cannot ask for a format
#: this code cannot produce or validate.
TRADITIONAL_OPPONENT_TARGET_FORMAT = TRADITIONAL_OPPONENT_V2

_FORMAT_BY_NAME = MappingProxyType({
    publication_format.name: publication_format
    for publication_format in SUPPORTED_TRADITIONAL_OPPONENT_FORMATS
})
_FORMAT_BY_METRIC_SET = MappingProxyType({
    publication_format.metric_set: publication_format
    for publication_format in SUPPORTED_TRADITIONAL_OPPONENT_FORMATS
})


def is_traditional_opponent_stream(stream_key: str) -> bool:
    """Whether one stream key belongs to this publication family."""

    return stream_key in _WINDOW_KIND_BY_STREAM


def traditional_opponent_window_kind(stream_key: str) -> str:
    """Return ``"season"`` or ``"l15"`` for one family stream key."""

    try:
        return _WINDOW_KIND_BY_STREAM[stream_key]
    except KeyError:
        raise TraditionalOpponentFormatError(
            "publication_family_mismatch",
            f"{stream_key} is not a traditional-opponent publication",
        ) from None


def traditional_opponent_format_by_name(name: str) -> TraditionalOpponentFormat:
    """Resolve a recorded format name, refusing one this code cannot serve."""

    try:
        return _FORMAT_BY_NAME[name]
    except KeyError:
        raise TraditionalOpponentFormatError(
            "publication_format_unsupported",
            f"{name} is not a supported traditional-opponent format",
        ) from None


def recognize_traditional_opponent_format(
    metrics: Iterable[str],
) -> TraditionalOpponentFormat:
    """Recognize one exact taxonomy, refusing anything approximate.

    A subset would silently drop a metric a consumer is entitled to; a
    superset would serve values this code has never validated.  Both are
    unsupported formats rather than tolerable variations.
    """

    observed = frozenset(str(metric) for metric in metrics)
    try:
        return _FORMAT_BY_METRIC_SET[observed]
    except KeyError:
        raise TraditionalOpponentFormatError(
            "publication_format_unsupported",
            "traditional-opponent metric taxonomy is not a supported format",
        ) from None


def _numeric(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TraditionalOpponentFormatError(
            "publication_payload_invalid",
            "a traditional-opponent metric must be numeric",
        )
    number = float(value)
    if not math.isfinite(number):
        raise TraditionalOpponentFormatError(
            "publication_payload_invalid",
            "a traditional-opponent metric must be finite",
        )
    return number


def _assert_rebound_identity(
    values: Mapping[str, float], *, block: str, exact: bool
) -> None:
    """Prove one team's rebound split sums to its total in one metric block.

    ``counts`` are integer observations and must agree exactly.  ``per48`` is
    derived by dividing each count by the same denominator, so it agrees to
    within floating-point rounding.  ``league_average``, ``population_sigma``,
    and ``competition_rank`` are league statistics of those values, and are
    deliberately not subject to the identity: the mean of a sum is a sum of
    means only by coincidence of rounding, and a rank is not additive at all.
    """

    total = _numeric(values["rebounds"])
    offensive = _numeric(values["offensive_rebounds"])
    defensive = _numeric(values["defensive_rebounds"])
    split = offensive + defensive
    agrees = (
        total == split
        if exact
        else math.isclose(
            total, split, rel_tol=_PER48_IDENTITY_TOLERANCE,
            abs_tol=_PER48_IDENTITY_TOLERANCE,
        )
    )
    if not agrees:
        raise TraditionalOpponentFormatError(
            "publication_rebound_identity_invalid",
            f"traditional-opponent {block} rebounds are not the split's sum",
        )


def validate_traditional_opponent_team(
    blocks: Mapping[str, Mapping[str, float]],
) -> TraditionalOpponentFormat:
    """Validate one published team row's metric blocks as one exact format.

    Returns the recognized format.  Raises when the blocks do not agree on one
    supported taxonomy or when a format invariant is violated.
    """

    missing = [name for name in TRADITIONAL_OPPONENT_METRIC_BLOCKS if name not in blocks]
    if missing:
        raise TraditionalOpponentFormatError(
            "publication_payload_invalid",
            f"traditional-opponent row is missing {', '.join(missing)}",
        )
    recognized = recognize_traditional_opponent_format(blocks["counts"])
    for name in TRADITIONAL_OPPONENT_METRIC_BLOCKS:
        if frozenset(blocks[name]) != recognized.metric_set:
            raise TraditionalOpponentFormatError(
                "publication_format_unsupported",
                f"traditional-opponent {name} taxonomy is not {recognized.name}",
            )
    if recognized.supports(REBOUND_SPLIT):
        _assert_rebound_identity(blocks["counts"], block="counts", exact=True)
        _assert_rebound_identity(blocks["per48"], block="per48", exact=False)
    return recognized


@dataclass(frozen=True, slots=True)
class NormalizedTraditionalOpponentTeam:
    """One opponent's published window, independent of the stored format.

    The attribute names match the decoded publication row so a consumer that
    already reads published rows needs no adaptation.
    """

    team_id: int
    team_tricode: str
    game_ids: tuple[str, ...]
    game_count: int
    per48: Mapping[str, float]
    league_average: Mapping[str, float]
    population_sigma: Mapping[str, float]
    competition_rank: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class NormalizedTraditionalOpponentWindow:
    """One traditional-opponent window, read through one interface.

    Consumers branch on :meth:`supports`, never on which stored generation
    produced the payload.  A metric the format does not publish is absent --
    :meth:`metric_values` returns ``None`` -- and is never presented as zero.
    """

    stream_key: str
    window: str
    format: TraditionalOpponentFormat
    teams: tuple[NormalizedTraditionalOpponentTeam, ...]

    @property
    def capabilities(self) -> frozenset[str]:
        return self.format.capabilities

    @property
    def metrics(self) -> tuple[str, ...]:
        return self.format.metrics

    def supports(self, capability: str) -> bool:
        return self.format.supports(capability)

    def metric_values(self, metric: str) -> Mapping[int, float] | None:
        """One metric's whole-league per-48 values, or ``None`` if unpublished."""

        if metric not in self.format.metric_set:
            return None
        return MappingProxyType({
            team.team_id: team.per48[metric] for team in self.teams
        })


#: Largest relative drift accepted between a stored league statistic and the
#: one recomputed from the published population.  Both are the same arithmetic
#: over the same thirty values, so they agree to floating-point rounding.
_POPULATION_TOLERANCE = 1e-9


def _agrees(stored: float, computed: float) -> bool:
    return math.isclose(
        float(stored), float(computed),
        rel_tol=_POPULATION_TOLERANCE, abs_tol=_POPULATION_TOLERANCE,
    )


def _assert_population_statistics(teams, recognized, *, stream_key: str) -> None:
    """Prove the derived blocks actually describe the published population.

    A league average, a population sigma, and a competition rank are not
    independent facts that happen to travel beside the per-48 values: they are
    functions of exactly those thirty values.  A payload whose rank says 999,
    or whose average is not the mean of the rows it ships with, is internally
    inconsistent evidence, and a consumer that ranked a team by it would
    present a plausible but wrong answer.  Whichever side is wrong, the
    publication as a whole is not serviceable.

    The blocks are optional here on purpose.  A publication carries them and is
    checked; a normalized read model assembled from per-48 values alone has
    nothing to disagree with and stays serviceable.
    """

    values_by_metric = {
        metric: {team.team_id: float(team.per48[metric]) for team in teams}
        for metric in recognized.metrics
    }
    for block in ("league_average", "population_sigma"):
        carried = [team for team in teams if getattr(team, block)]
        if not carried:
            continue
        if len(carried) != len(teams):
            raise TraditionalOpponentFormatError(
                "publication_population_inconsistent",
                f"{stream_key} publication carries {block} for only some teams",
            )
        for metric in recognized.metrics:
            stored = {float(getattr(team, block)[metric]) for team in carried}
            if len(stored) != 1:
                # One league statistic cannot have thirty different values.
                raise TraditionalOpponentFormatError(
                    "publication_population_inconsistent",
                    f"{stream_key} publication rows disagree about {block}",
                )
            population = values_by_metric[metric]
            mean = sum(population.values()) / len(population)
            computed = mean if block == "league_average" else math.sqrt(
                sum((value - mean) ** 2 for value in population.values())
                / len(population)
            )
            if not _agrees(next(iter(stored)), computed):
                raise TraditionalOpponentFormatError(
                    "publication_population_inconsistent",
                    f"{stream_key} publication {block} does not describe its"
                    f" own population for {metric}",
                )
    ranked = [team for team in teams if team.competition_rank]
    if not ranked:
        return
    if len(ranked) != len(teams):
        raise TraditionalOpponentFormatError(
            "publication_population_inconsistent",
            f"{stream_key} publication ranks only some teams",
        )
    for metric in recognized.metrics:
        expected = competition_ranks(values_by_metric[metric], descending=False)
        if any(
            int(team.competition_rank[metric]) != expected[team.team_id]
            for team in ranked
        ):
            raise TraditionalOpponentFormatError(
                "publication_population_inconsistent",
                f"{stream_key} publication {metric} ranks do not follow its"
                " own per-48 ordering",
            )


def normalize_traditional_opponent_window(
    rows: Sequence,
    *,
    stream_key: str,
) -> NormalizedTraditionalOpponentWindow:
    """Normalize one decoded traditional-opponent publication.

    ``rows`` are the publication's decoded team rows.  The whole publication is
    refused when the rows do not agree on one supported format, when the league
    is not the canonical thirty teams, or when a team appears twice: a ranked
    metric over a partial population is wrong rather than incomplete.
    """

    window = traditional_opponent_window_kind(stream_key)
    rows = tuple(rows)
    if not rows:
        raise TraditionalOpponentFormatError(
            "publication_payload_invalid",
            f"{stream_key} publication is empty",
        )
    recognized = recognize_traditional_opponent_format(rows[0].per48)
    teams: list[NormalizedTraditionalOpponentTeam] = []
    for row in rows:
        for block in ("per48", "league_average", "population_sigma", "competition_rank"):
            values = getattr(row, block)
            if values and frozenset(values) != recognized.metric_set:
                raise TraditionalOpponentFormatError(
                    "publication_format_unsupported",
                    f"{stream_key} publication mixes traditional-opponent formats",
                )
        teams.append(NormalizedTraditionalOpponentTeam(
            team_id=row.team_id,
            team_tricode=row.team_tricode,
            game_ids=tuple(row.game_ids),
            game_count=row.game_count,
            per48=row.per48,
            league_average=row.league_average,
            population_sigma=row.population_sigma,
            competition_rank=row.competition_rank,
        ))
    if len({team.team_id for team in teams}) != len(teams):
        raise TraditionalOpponentFormatError(
            "publication_payload_invalid",
            f"{stream_key} publication repeats a team",
        )
    if len(teams) != TRADITIONAL_OPPONENT_LEAGUE_SIZE:
        raise TraditionalOpponentFormatError(
            "publication_league_incomplete",
            f"{stream_key} publication is not a thirty-team population",
        )
    _assert_population_statistics(teams, recognized, stream_key=stream_key)
    return NormalizedTraditionalOpponentWindow(
        stream_key=stream_key,
        window=window,
        format=recognized,
        teams=tuple(teams),
    )


__all__ = [
    "REBOUND_SPLIT",
    "REBOUND_SPLIT_METRICS",
    "SUPPORTED_TRADITIONAL_OPPONENT_FORMATS",
    "TRADITIONAL_OPPONENT_FAMILY",
    "TRADITIONAL_OPPONENT_LEAGUE_SIZE",
    "TRADITIONAL_OPPONENT_METRIC_BLOCKS",
    "TRADITIONAL_OPPONENT_STREAM_KEYS",
    "TRADITIONAL_OPPONENT_TARGET_FORMAT",
    "TRADITIONAL_OPPONENT_V1",
    "TRADITIONAL_OPPONENT_V2",
    "NormalizedTraditionalOpponentTeam",
    "NormalizedTraditionalOpponentWindow",
    "TraditionalOpponentFormat",
    "TraditionalOpponentFormatError",
    "is_traditional_opponent_stream",
    "normalize_traditional_opponent_window",
    "recognize_traditional_opponent_format",
    "traditional_opponent_format_by_name",
    "traditional_opponent_window_kind",
    "validate_traditional_opponent_team",
]
