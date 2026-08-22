"""Pure derivations and completeness gates over Canonical Game Ledger facts."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from app.services.canonical_game_ledger import (
    CanonicalGame,
    LedgerValidationError,
    PlayerGameFact,
    validate_canonical_season,
)


class LedgerDerivationUnavailable(LedgerValidationError):
    """A requested stream cannot claim completeness from the supplied facts."""


@dataclass(frozen=True, slots=True)
class TraditionalOpponentFact:
    game_id: str
    game_date: date
    team_id: int
    opponent_team_id: int
    opponent_points: int
    opponent_rebounds: int
    opponent_assists: int
    opponent_field_goals_made: int
    opponent_field_goals_attempted: int
    opponent_three_pointers_made: int
    opponent_three_pointers_attempted: int
    opponent_free_throws_made: int
    opponent_free_throws_attempted: int
    opponent_turnovers: int
    opponent_steals: int
    opponent_blocks: int
    opponent_personal_fouls: int


@dataclass(frozen=True, slots=True)
class AssistLocationFact:
    game_id: str
    game_date: date
    player_id: int
    team_id: int
    assists: int
    two_point_assists: int
    three_point_assists: int
    arc3_assists: int
    corner3_assists: int
    at_rim_assists: int
    short_mid_range_assists: int
    long_mid_range_assists: int

    @property
    def location_total(self) -> int:
        return (
            self.arc3_assists
            + self.corner3_assists
            + self.at_rim_assists
            + self.short_mid_range_assists
            + self.long_mid_range_assists
        )


@dataclass(frozen=True, slots=True)
class PlayerPer36Fact:
    season: str
    player_id: int
    minutes: float
    game_count: int
    team_ids_at_game: tuple[int, ...]
    points_per36: float
    rebounds_per36: float
    assists_per36: float
    field_goals_made_per36: float
    field_goals_attempted_per36: float
    three_pointers_made_per36: float
    three_pointers_attempted_per36: float
    free_throws_made_per36: float
    free_throws_attempted_per36: float
    turnovers_per36: float
    steals_per36: float
    blocks_per36: float
    personal_fouls_per36: float


@dataclass(frozen=True, slots=True)
class TeamWindowMetric:
    team_id: int
    team_tricode: str
    game_ids: tuple[str, ...]
    game_count: int
    per48: Mapping[str, float]
    league_average: Mapping[str, float]
    population_sigma: Mapping[str, float]
    competition_rank: Mapping[str, int]
    counts: Mapping[str, float] = ()
    team_minutes: float = 0.0


@dataclass(frozen=True, slots=True)
class TeamWindowMaterialization:
    season: str
    as_of: date
    window_kind: str
    window_games: int
    complete: bool
    reason: str | None
    governed_game_ids: tuple[str, ...]
    teams: tuple[TeamWindowMetric, ...]


@dataclass(frozen=True, slots=True)
class AssistLocationWindowMetric:
    team_id: int
    team_tricode: str
    game_ids: tuple[str, ...]
    game_count: int
    counts: Mapping[str, int]
    per48: Mapping[str, float]
    league_average: Mapping[str, float]
    population_sigma: Mapping[str, float]
    competition_rank: Mapping[str, int]
    team_minutes: float = 0.0


@dataclass(frozen=True, slots=True)
class AssistLocationWindowMaterialization:
    season: str
    as_of: date
    window_kind: str
    window_games: int
    complete: bool
    reason: str | None
    governed_game_ids: tuple[str, ...]
    teams: tuple[AssistLocationWindowMetric, ...]


TEAM_METRICS = (
    "points",
    "rebounds",
    "assists",
    "field_goals_made",
    "field_goals_attempted",
    "three_pointers_made",
    "three_pointers_attempted",
    "free_throws_made",
    "free_throws_attempted",
    "turnovers",
    "steals",
    "blocks",
    "personal_fouls",
)
NBA_TEAM_IDS = frozenset(1610612737 + index for index in range(30))
ASSIST_METRICS = (
    "two_point_assists",
    "three_point_assists",
    "arc3_assists",
    "corner3_assists",
    "at_rim_assists",
    "short_mid_range_assists",
    "long_mid_range_assists",
)
#: The opponent total-assist primitive that feeds the Matchups ``Assists``
#: surface stat on top of the location counters in ``ASSIST_METRICS``.  It is
#: the opponent typed team fact, which includes the team-only residual (an
#: assist credited to no player) that the player rows cannot carry.
ASSIST_TOTAL_METRIC = "assists"
#: The full per-team derived surface: every location counter plus the
#: residual-inclusive opponent total that carries into per48, league averages,
#: sigma, and competition ranks.
ASSIST_DERIVED_METRICS = (*ASSIST_METRICS, ASSIST_TOTAL_METRIC)
#: The four contracted NBA-traditional opponent surfaces and the ledger
#: metric that supplies each count.  ``materialize_team_window`` aggregates the
#: opposing team fact's metric, except that ``OPP_REB`` is player-credited:
#: LeagueDashTeamStats excludes team-only rebounds, so the opposing players'
#: rebounds are summed instead.  The Matchups surface stores the same raw
#: count.
MATCHUP_TRADITIONAL_KEYS = {
    "OPP_REB": "rebounds",
    "OPP_TOV": "turnovers",
    "OPP_STL": "steals",
    "OPP_BLK": "blocks",
}
_PLAYER_CREDITED_MATCHUP_METRICS = frozenset({"rebounds"})


#: A regulation NBA game is 48 minutes and every overtime adds 5.  The legacy
#: ``LeagueDashTeamStats`` denominator is that nominal game length; the
#: ledger's retained ``team_minutes`` is the player-minute sum over five,
#: which drifts from nominal by seconds of PBP clock precision.
REGULATION_MINUTES = 48.0
OVERTIME_MINUTES = 5.0
#: Largest drift from a nominal game length accepted as clock precision:
#: three seconds of team minutes (fifteen player-seconds).  Production 2025-26
#: drift peaks at 0.024 minutes; a missing player row with more than fifteen
#: seconds of play therefore falls outside the band and fails closed.
NOMINAL_MINUTES_TOLERANCE = 0.05


def nominal_team_minutes(team_minutes: float) -> float:
    """Return the nominal game length the retained team minutes establish.

    The retained value is the player-minute sum over five, which is within
    seconds of ``48 + 5k`` when the observation is complete.  Raises
    ``LedgerDerivationUnavailable`` when the value is not within
    ``NOMINAL_MINUTES_TOLERANCE`` of a nominal length, because the
    denominator can then not be derived from evidence.
    """

    if float(team_minutes) <= 0:
        # No minutes retained (hand-built replay facts): callers keep their
        # count-per-game fallback rather than inventing a game length.
        return 0.0
    overtimes = round((float(team_minutes) - REGULATION_MINUTES) / OVERTIME_MINUTES)
    nominal = REGULATION_MINUTES + OVERTIME_MINUTES * max(overtimes, 0)
    if abs(float(team_minutes) - nominal) > NOMINAL_MINUTES_TOLERANCE:
        raise LedgerDerivationUnavailable(
            "team minutes do not prove a nominal game length"
        )
    return nominal


def player_credited_count(game: CanonicalGame, team_id: int, metric: str) -> int:
    """Sum one count over a team's player rows, excluding the team residual."""

    return sum(
        int(getattr(player, metric))
        for player in game.player_facts
        if player.team_id == team_id
    )
#: The contracted PBP assist surfaces and the ledger player count that supplies
#: each.  ``Assists`` is the opponent total; every location key is one of the
#: governed location counters.
MATCHUP_ASSIST_KEYS = {
    "Assists": ASSIST_TOTAL_METRIC,
    "Arc3Assists": "arc3_assists",
    "Corner3Assists": "corner3_assists",
    "AtRimAssists": "at_rim_assists",
    "ShortMidRangeAssists": "short_mid_range_assists",
    "LongMidRangeAssists": "long_mid_range_assists",
}


def window_ledger_checksum(
    game_ids: Iterable[str],
    game_checksums: Mapping[str, str],
) -> str:
    """Return a deterministic SHA-256 over an exact governed game selection.

    The checksum covers only the selected game IDs with each game's stored
    ledger checksum, so two windows with the same games and facts are stable
    across replays while any selected-game change alters the checksum.
    """

    ordered = tuple(
        (str(game_id), str(game_checksums[game_id]))
        for game_id in sorted(set(game_ids))
    )
    return hashlib.sha256(
        json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _regular_games(games: Iterable[CanonicalGame], *, cutoff: date | None = None) -> tuple[CanonicalGame, ...]:
    selected = [
        game
        for game in games
        if game.season_type == "Regular Season" and (cutoff is None or game.game_date <= cutoff)
    ]
    return tuple(sorted(selected, key=lambda game: (game.game_date, game.game_id)))


def derive_traditional_opponent_facts(games: Iterable[CanonicalGame]) -> tuple[TraditionalOpponentFact, ...]:
    """Derive opponent count facts from the opposing team fact in each game.

    ``opponent_rebounds`` is the player-credited sum, matching the legacy
    ``OPP_REB`` contract; every other count is the opposing team fact's.
    """

    output: list[TraditionalOpponentFact] = []
    for game in _regular_games(games):
        by_team = {fact.team_id: fact for fact in game.team_facts}
        if set(by_team) != {game.home_team_id, game.away_team_id}:
            raise LedgerDerivationUnavailable("traditional opponent requires both team fact sets")
        for team in game.team_facts:
            opponent = by_team.get(team.opponent_team_id)
            if opponent is None:
                raise LedgerDerivationUnavailable("traditional opponent has no opposing team fact")
            output.append(
                TraditionalOpponentFact(
                    game_id=game.game_id,
                    game_date=game.game_date,
                    team_id=team.team_id,
                    opponent_team_id=opponent.team_id,
                    opponent_points=opponent.points,
                    opponent_rebounds=player_credited_count(game, opponent.team_id, "rebounds"),
                    opponent_assists=opponent.assists,
                    opponent_field_goals_made=opponent.field_goals_made,
                    opponent_field_goals_attempted=opponent.field_goals_attempted,
                    opponent_three_pointers_made=opponent.three_pointers_made,
                    opponent_three_pointers_attempted=opponent.three_pointers_attempted,
                    opponent_free_throws_made=opponent.free_throws_made,
                    opponent_free_throws_attempted=opponent.free_throws_attempted,
                    opponent_turnovers=opponent.turnovers,
                    opponent_steals=opponent.steals,
                    opponent_blocks=opponent.blocks,
                    opponent_personal_fouls=opponent.personal_fouls,
                )
            )
    return tuple(output)


def governed_assist_locations(player: PlayerGameFact) -> dict[str, int] | None:
    """Return the player's seven assist-location counts, or ``None``.

    The PBP wire omits observed-zero counters, so a missing location field is
    a governed zero only when the retained fields prove it arithmetically:
    two-point plus three-point assists must equal the player's assists, the
    rim/short/long split must equal the two-point count, and the arc/corner
    split must equal the three-point count.  A row that cannot be reconciled
    (for example a provider fallback that never observed locations) stays an
    incomplete observation.  Rows with every counter observed explicitly are
    returned as observed, bounded only by the leaf total never exceeding the
    player's assists; both the fact derivation and the window materializer
    consume this one seam.
    """

    observed = {metric: getattr(player, metric) for metric in ASSIST_METRICS}
    values = {
        metric: (0 if value is None else int(value)) for metric, value in observed.items()
    }
    if all(value is not None for value in observed.values()):
        # Every counter was observed explicitly; nothing needs proving, but
        # the leaf locations can still never exceed the player's assists.
        leaf_total = (
            values["arc3_assists"]
            + values["corner3_assists"]
            + values["at_rim_assists"]
            + values["short_mid_range_assists"]
            + values["long_mid_range_assists"]
        )
        return values if leaf_total <= player.assists else None
    if (
        values["two_point_assists"] + values["three_point_assists"] != player.assists
        or values["at_rim_assists"]
        + values["short_mid_range_assists"]
        + values["long_mid_range_assists"]
        != values["two_point_assists"]
        or values["arc3_assists"] + values["corner3_assists"]
        != values["three_point_assists"]
    ):
        return None
    return values


def derive_assist_location_facts(games: Iterable[CanonicalGame]) -> tuple[AssistLocationFact, ...]:
    """Derive assist-location counts, refusing a partial location observation."""

    output: list[AssistLocationFact] = []
    for game in _regular_games(games):
        for player in game.player_facts:
            values = governed_assist_locations(player)
            if values is None:
                raise LedgerDerivationUnavailable(
                    "assist-location materialization requires a complete location observation"
                )
            fact = AssistLocationFact(
                game_id=game.game_id,
                game_date=game.game_date,
                player_id=player.player_id,
                team_id=player.team_id,
                assists=player.assists,
                **values,
            )
            if fact.location_total > fact.assists:
                raise LedgerDerivationUnavailable("assist locations exceed the player's assists")
            output.append(fact)
    return tuple(output)


def _per36(total: int, minutes: float) -> float:
    return 0.0 if minutes <= 0 else (float(total) / minutes) * 36.0


def derive_player_per36_facts(
    games: Iterable[CanonicalGame],
    *,
    season: str | None = None,
    cutoff: date | None = None,
) -> tuple[PlayerPer36Fact, ...]:
    """Aggregate count primitives by canonical player identity.

    Team identity remains available in ``team_ids_at_game``; no provider
    percentages are summed, which is the important traded-player invariant.
    """

    canonical_season = validate_canonical_season(season) if season is not None else None
    selected = tuple(
        game for game in _regular_games(games, cutoff=cutoff)
        if canonical_season is None or game.season == canonical_season
    )
    grouped: dict[int, list[PlayerGameFact]] = defaultdict(list)
    for game in selected:
        for player in game.player_facts:
            grouped[player.player_id].append(player)
    output: list[PlayerPer36Fact] = []
    for player_id, rows in sorted(grouped.items()):
        minutes = sum(row.minutes for row in rows)
        if minutes <= 0:
            continue
        totals = {field_name: sum(getattr(row, field_name) for row in rows) for field_name in (
            "points", "rebounds", "assists", "field_goals_made", "field_goals_attempted",
            "three_pointers_made", "three_pointers_attempted", "free_throws_made",
            "free_throws_attempted", "turnovers", "steals", "blocks", "personal_fouls",
        )}
        output.append(
            PlayerPer36Fact(
                season=canonical_season or selected[0].season,
                player_id=player_id,
                minutes=minutes,
                game_count=len(rows),
                team_ids_at_game=tuple(sorted({row.team_id for row in rows})),
                points_per36=_per36(totals["points"], minutes),
                rebounds_per36=_per36(totals["rebounds"], minutes),
                assists_per36=_per36(totals["assists"], minutes),
                field_goals_made_per36=_per36(totals["field_goals_made"], minutes),
                field_goals_attempted_per36=_per36(totals["field_goals_attempted"], minutes),
                three_pointers_made_per36=_per36(totals["three_pointers_made"], minutes),
                three_pointers_attempted_per36=_per36(totals["three_pointers_attempted"], minutes),
                free_throws_made_per36=_per36(totals["free_throws_made"], minutes),
                free_throws_attempted_per36=_per36(totals["free_throws_attempted"], minutes),
                turnovers_per36=_per36(totals["turnovers"], minutes),
                steals_per36=_per36(totals["steals"], minutes),
                blocks_per36=_per36(totals["blocks"], minutes),
                personal_fouls_per36=_per36(totals["personal_fouls"], minutes),
            )
        )
    return tuple(output)


def competition_ranks(values: Mapping[int, float], *, descending: bool = True) -> dict[int, int]:
    """Return deterministic competition ranks (1, 1, 3) for metric values."""

    ordered = sorted(values.items(), key=lambda item: ((-item[1] if descending else item[1]), item[0]))
    ranks: dict[int, int] = {}
    previous: float | None = None
    for index, (team_id, value) in enumerate(ordered, start=1):
        if previous is None or value != previous:
            rank = index
        ranks[team_id] = rank
        previous = value
    return ranks


def _window_game_ids(
    games: Sequence[CanonicalGame],
    *,
    season: str,
    cutoff: date,
    window_games: int | None,
    expected_game_ids: frozenset[str] | None,
    expected_team_game_ids: Mapping[int, frozenset[str]] | None,
) -> tuple[tuple[str, ...], dict[int, tuple[str, ...]], str | None]:
    governed = tuple(game for game in _regular_games(games, cutoff=cutoff) if game.season == season)
    by_id = {game.game_id: game for game in governed}
    if expected_game_ids is None:
        return tuple(sorted(by_id)), {}, "exact governed game IDs are required"
    if set(by_id) != set(expected_game_ids):
        return tuple(sorted(by_id)), {}, "ledger is not complete through the requested cutoff"
    per_team: dict[int, list[CanonicalGame]] = defaultdict(list)
    for game in governed:
        for fact in game.team_facts:
            per_team[fact.team_id].append(game)
    if window_games is not None:
        if window_games < 1:
            raise ValueError("window_games must be positive")
        per_team = {
            team_id: sorted(team_games, key=lambda item: (item.game_date, item.game_id), reverse=True)[:window_games]
            for team_id, team_games in per_team.items()
        }
        if any(len(team_games) < window_games for team_games in per_team.values()):
            return tuple(sorted(by_id)), {}, "L15 is unavailable until every governed team has 15 eligible games"
        actual = {
            team_id: frozenset(game.game_id for game in team_games)
            for team_id, team_games in per_team.items()
        }
        if expected_team_game_ids is None or actual != dict(expected_team_game_ids):
            return tuple(sorted(by_id)), {}, "L15 game IDs do not match the governed exact window"
    return tuple(sorted({game.game_id for team_games in per_team.values() for game in team_games})), {
        team_id: tuple(sorted({game.game_id for game in team_games}))
        for team_id, team_games in per_team.items()
    }, None


def materialize_team_window(
    games: Iterable[CanonicalGame],
    *,
    season: str,
    as_of: date,
    window_games: int | None = None,
    expected_game_ids: frozenset[str] | None = None,
    expected_team_game_ids: Mapping[int, frozenset[str]] | None = None,
    team_ids: frozenset[int] | None = None,
) -> TeamWindowMaterialization:
    """Materialize an exact Season or L15 window with 30-team gates."""

    canonical_season = validate_canonical_season(season)
    supplied = tuple(games)
    governed_game_ids, ids_by_team, reason = _window_game_ids(
        supplied,
        season=canonical_season,
        cutoff=as_of,
        window_games=window_games,
        expected_game_ids=expected_game_ids,
        expected_team_game_ids=expected_team_game_ids,
    )
    if team_ids is not None:
        expected_teams = team_ids
    else:
        observed_team_ids = {fact.team_id for game in supplied for fact in game.team_facts}
        # Offline fixtures commonly use 1..30 synthetic IDs; production
        # Event Catalog rows use the NBA's 16106127xx IDs.  Both are governed
        # 30-team universes, and neither should silently be treated as a
        # two-team complete league.
        expected_teams = NBA_TEAM_IDS if observed_team_ids.intersection(NBA_TEAM_IDS) else frozenset(range(1, 31))
    if len(expected_teams) != 30:
        reason = reason or "governed team roster must contain exactly 30 teams"
    if reason is None and set(ids_by_team) != set(expected_teams):
        reason = "ledger window is not League Complete for all governed teams"
    if reason is not None:
        return TeamWindowMaterialization(
            season=canonical_season,
            as_of=as_of,
            window_kind="rolling_games" if window_games is not None else "season",
            window_games=window_games or 0,
            complete=False,
            reason=reason,
            governed_game_ids=governed_game_ids,
            teams=(),
        )
    game_by_id = {game.game_id: game for game in supplied}
    raw_by_team: dict[int, dict[str, float]] = {}
    counts_by_team: dict[int, dict[str, float]] = {}
    minutes_by_team: dict[int, float] = {}
    team_codes: dict[int, str] = {}
    for team_id, game_ids in ids_by_team.items():
        totals = {metric: 0.0 for metric in TEAM_METRICS}
        team_minutes = 0.0
        for game_id in game_ids:
            game = game_by_id[game_id]
            defense = next(fact for fact in game.team_facts if fact.team_id == team_id)
            opponent = next(
                fact for fact in game.team_facts
                if fact.team_id == defense.opponent_team_id
            )
            team_codes[team_id] = defense.team_tricode
            team_minutes += nominal_team_minutes(defense.team_minutes)
            for metric in TEAM_METRICS:
                if metric in _PLAYER_CREDITED_MATCHUP_METRICS:
                    # LeagueDashTeamStats' OPP_REB surface is player-credited.
                    # Canonical team facts intentionally also retain team-only
                    # rebounds, which must not leak into that legacy contract.
                    totals[metric] += float(
                        player_credited_count(game, opponent.team_id, metric)
                    )
                else:
                    totals[metric] += float(getattr(opponent, metric))
        counts_by_team[team_id] = totals
        minutes_by_team[team_id] = team_minutes
        # ``team_minutes`` here is the nominal game length (48 plus 5 per
        # overtime) that each retained effective denominator (player minutes
        # over five) establishes; see ``nominal_team_minutes``.
        # A manually assembled fact may not carry minutes; its count-per-game
        # values are the conservative equivalent for that test/replay seam.
        if team_minutes > 0:
            raw_by_team[team_id] = {
                metric: totals[metric] * 48.0 / team_minutes
                for metric in TEAM_METRICS
            }
        else:
            denominator = float(len(game_ids))
            raw_by_team[team_id] = {metric: totals[metric] / denominator for metric in TEAM_METRICS}
    averages = {
        metric: sum(values[metric] for values in raw_by_team.values()) / len(raw_by_team)
        for metric in TEAM_METRICS
    }
    sigma = {
        metric: math.sqrt(sum((values[metric] - averages[metric]) ** 2 for values in raw_by_team.values()) / len(raw_by_team))
        for metric in TEAM_METRICS
    }
    ranks = {
        metric: competition_ranks(
            {team_id: values[metric] for team_id, values in raw_by_team.items()},
            descending=False,
        )
        for metric in TEAM_METRICS
    }
    teams = tuple(
        TeamWindowMetric(
            team_id=team_id,
            team_tricode=team_codes[team_id],
            game_ids=ids_by_team[team_id],
            game_count=len(ids_by_team[team_id]),
            per48=raw_by_team[team_id],
            league_average=averages,
            population_sigma=sigma,
            competition_rank={metric: ranks[metric][team_id] for metric in TEAM_METRICS},
            counts=counts_by_team[team_id],
            team_minutes=minutes_by_team[team_id],
        )
        for team_id in sorted(raw_by_team)
    )
    return TeamWindowMaterialization(
        season=canonical_season,
        as_of=as_of,
        window_kind="rolling_games" if window_games is not None else "season",
        window_games=window_games or 0,
        complete=True,
        reason=None,
        governed_game_ids=governed_game_ids,
        teams=teams,
    )


def materialize_assist_location_window(
    games: Iterable[CanonicalGame],
    *,
    season: str,
    as_of: date,
    expected_game_ids: frozenset[str],
    team_ids: frozenset[int],
    window_games: int | None = None,
    expected_team_game_ids: Mapping[int, frozenset[str]] | None = None,
) -> AssistLocationWindowMaterialization:
    """Materialize opponent assist locations for one exact governed window."""

    supplied = tuple(games)
    base = materialize_team_window(
        supplied,
        season=season,
        as_of=as_of,
        window_games=window_games,
        expected_game_ids=expected_game_ids,
        expected_team_game_ids=expected_team_game_ids,
        team_ids=team_ids,
    )
    if not base.complete:
        return AssistLocationWindowMaterialization(
            season=base.season,
            as_of=base.as_of,
            window_kind=base.window_kind,
            window_games=base.window_games,
            complete=False,
            reason=base.reason,
            governed_game_ids=base.governed_game_ids,
            teams=(),
        )
    game_by_id = {game.game_id: game for game in supplied}
    counts_by_team: dict[int, dict[str, int]] = {}
    values_by_team: dict[int, dict[str, float]] = {}
    minutes_by_team: dict[int, float] = {}
    for team in base.teams:
        counts = {metric: 0 for metric in ASSIST_METRICS}
        counts[ASSIST_TOTAL_METRIC] = 0
        denominator = 0.0
        for game_id in team.game_ids:
            game = game_by_id[game_id]
            defense = next(fact for fact in game.team_facts if fact.team_id == team.team_id)
            denominator += nominal_team_minutes(defense.team_minutes)
            opponent_id = defense.opponent_team_id
            opponent = next(
                fact for fact in game.team_facts if fact.team_id == opponent_id
            )
            # The opponent total is the typed team fact: it includes the
            # team-only residual (e.g. a dead-ball assist credited to no
            # player) that the player rows cannot carry.  The location
            # breakdown below stays player-sourced, so a location observation
            # never has to explain the residual.
            counts[ASSIST_TOTAL_METRIC] += opponent.assists
            for player in game.player_facts:
                if player.team_id != opponent_id:
                    continue
                values = governed_assist_locations(player)
                if values is None:
                    raise LedgerDerivationUnavailable(
                        "assist-location materialization requires complete player counts"
                    )
                for metric in ASSIST_METRICS:
                    counts[metric] += values[metric]
        if denominator <= 0:
            raise LedgerDerivationUnavailable(
                "assist-location materialization requires positive team minutes"
            )
        counts_by_team[team.team_id] = counts
        minutes_by_team[team.team_id] = denominator
        values_by_team[team.team_id] = {
            metric: counts[metric] * 48.0 / denominator
            for metric in ASSIST_DERIVED_METRICS
        }
    averages = {
        metric: sum(values[metric] for values in values_by_team.values()) / len(values_by_team)
        for metric in ASSIST_DERIVED_METRICS
    }
    sigma = {
        metric: math.sqrt(
            sum((values[metric] - averages[metric]) ** 2 for values in values_by_team.values())
            / len(values_by_team)
        )
        for metric in ASSIST_DERIVED_METRICS
    }
    ranks = {
        metric: competition_ranks(
            {team_id: values[metric] for team_id, values in values_by_team.items()},
            descending=False,
        )
        for metric in ASSIST_DERIVED_METRICS
    }
    base_by_team = {team.team_id: team for team in base.teams}
    teams = tuple(
        AssistLocationWindowMetric(
            team_id=team_id,
            team_tricode=base_by_team[team_id].team_tricode,
            game_ids=base_by_team[team_id].game_ids,
            game_count=base_by_team[team_id].game_count,
            counts=counts_by_team[team_id],
            per48=values_by_team[team_id],
            league_average=averages,
            population_sigma=sigma,
            competition_rank={metric: ranks[metric][team_id] for metric in ASSIST_DERIVED_METRICS},
            team_minutes=minutes_by_team[team_id],
        )
        for team_id in sorted(values_by_team)
    )
    return AssistLocationWindowMaterialization(
        season=base.season,
        as_of=base.as_of,
        window_kind=base.window_kind,
        window_games=base.window_games,
        complete=True,
        reason=None,
        governed_game_ids=base.governed_game_ids,
        teams=teams,
    )


__all__ = [
    "nominal_team_minutes",
    "ASSIST_DERIVED_METRICS",
    "ASSIST_METRICS",
    "ASSIST_TOTAL_METRIC",
    "MATCHUP_ASSIST_KEYS",
    "MATCHUP_TRADITIONAL_KEYS",
    "TEAM_METRICS",
    "AssistLocationFact",
    "AssistLocationWindowMaterialization",
    "AssistLocationWindowMetric",
    "LedgerDerivationUnavailable",
    "PlayerPer36Fact",
    "TeamWindowMaterialization",
    "TeamWindowMetric",
    "TraditionalOpponentFact",
    "competition_ranks",
    "derive_assist_location_facts",
    "governed_assist_locations",
    "derive_player_per36_facts",
    "derive_traditional_opponent_facts",
    "materialize_assist_location_window",
    "materialize_team_window",
    "window_ledger_checksum",
]
