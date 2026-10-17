"""Backtest the REB Matchup Score against the rebound-type candidate (#234, #447).

For every qualifying player-game of a season this scores the opponent two ways
from Canonical Game Ledger facts dated strictly before that game, in both the
Season-to-date and the Last 15 window the Matchup shows:

* current: the opponent's ``OPP_REB`` allowed per 48 over the league average,
  minus 1 (``MatchupService._aggregate_offensive_score_window``);
* candidate: the sum over the six miss-type slices of (the player's share of
  his rebounds x the opponent's slice allowed per 48 over the league's slice
  average), minus 1 (``MatchupService._diet_component`` on ``rebound_types``),
  with the player's diet taken from games before the test game too.

Nothing is reimplemented: the team windows come from ``materialize_team_window``
and ``materialize_rebound_type_window``, the diet from
``derive_player_rebound_type_diet_rows``, and the scores from the two Matchup
methods above.  Each test day is scored by handing those functions only games
dated before it, so a fact from the test game or later cannot be read.

Population: at least 20 prior games (games the player played) and at least 15
minutes in the test game.  Target: his REB in the game minus his
season-to-date REB per game.  Statistic: Spearman rho against the target,
ties ranked by their average; a constant score has no association (rho 0).

Win condition, per window and per population (all players, and "bigs" whose
pre-game diet is at least 60% two-point-miss rebounds): rho_new > rho_current
AND the 95% cluster-bootstrap interval of (rho_new - rho_current), resampling
game days, excludes 0.  The verdict is ``win`` only when every window and
population wins.

Read-only: the engine rejects writes (``default_transaction_read_only=on`` on
Postgres), and nothing is stored.

    python scripts/reb_score_backtest.py --season 2025-26 \\
        --database-url "$DATABASE_URL" --output reb-backtest.json

The summary prints to stdout; ``--output`` also writes the JSON (without it
the JSON follows the summary).  Exit status: 0 for a ``win`` or ``lose``
verdict, 2 when the facts could not be read or the miss-type split has not
been derived for every game.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app.config.settings import NBASeasonSettings, RuntimeSettings  # noqa: E402
from app.domain.rebound_types import (  # noqa: E402
    REBOUND_TYPE_WIRE_NAMES,
    REBOUND_TYPES_BASE,
)
from app.services.canonical_game_ledger import (  # noqa: E402
    CanonicalGame,
    CanonicalGameLedgerRepository,
)
from app.services.ledger_derivations import (  # noqa: E402
    NBA_TEAM_IDS,
    LedgerDerivationUnavailable,
    derive_player_rebound_type_diet_rows,
    materialize_rebound_type_window,
    materialize_team_window,
)
from app.services.matchup import (  # noqa: E402
    MatchupService,
    _PlayerDiet,
    _WindowMetricIndex,
    observed_diet_share,
)
from app.services.player_diet import StoredPlayerDietFact  # noqa: E402
from app.services.team_matchup_query import (  # noqa: E402
    LeagueMatchupMetric,
    TeamMatchupMetric,
)
from scripts.season_verify import open_read_only_engine  # noqa: E402

MIN_PRIOR_GAMES = 20
MIN_MINUTES = 15.0
#: A big's pre-game diet is at least this share two-point-miss rebounds.
BIGS_TWO_POINT_SHARE = 0.60
TWO_POINT_SLICES = ("DefTwoPtRebounds", "OffTwoPtRebounds")
WINDOWS = ("season", "last_15")
POPULATIONS = ("overall", "bigs")
LAST_N = 15
MIN_RESAMPLES = 2000
DEFAULT_RESAMPLES = 2000
DEFAULT_SEED = 234447

_OPP_REB = ("traditional", "OPP_REB", "OPP_REB")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_AVAILABLE = {"status": "available", "unavailable_reason": None}


@dataclass(frozen=True, slots=True)
class Sample:
    """One qualifying player-game, with both scores per window."""

    game_id: str
    game_date: date
    player_id: int
    target: float
    two_point_share: float
    #: ``window -> (current, candidate)``; a window with no score is absent.
    scores: Mapping[str, tuple[float, float]]
    #: ``window -> True`` when the candidate fell back to the current score
    #: because the player had no usable rebound diet.
    fell_back: bool = False


def is_big(two_point_share: float) -> bool:
    return two_point_share >= BIGS_TWO_POINT_SHARE


# --- scoring: the Matchup's own methods -----------------------------------------


def _scorer(season: str) -> MatchupService:
    """A Matchup service with no readers: only its pure score methods are used."""

    return MatchupService(
        event_catalog=None,
        player_pool=None,
        player_logs=None,  # type: ignore[arg-type]
        player_diets=None,
        team_matchups=None,
        stats_freshness=None,  # type: ignore[arg-type]
        settings=RuntimeSettings(environment="testing", nba=NBASeasonSettings(current_season=season)),
    )


def _index(traditional: Any, rebound: Any) -> _WindowMetricIndex:
    """The metric index the Matchup would read, built from fresh windows."""

    league: dict[tuple[str, str, str], LeagueMatchupMetric] = {}
    teams: dict[int, dict[tuple[str, str, str], TeamMatchupMetric]] = {}
    first = traditional.teams[0]
    league[_OPP_REB] = LeagueMatchupMetric(
        *_OPP_REB,
        average_allowed_per_48=first.league_average["rebounds"],
        sigma=first.population_sigma["rebounds"],
        team_count=len(traditional.teams),
    )
    for team in traditional.teams:
        teams.setdefault(team.team_id, {})[_OPP_REB] = TeamMatchupMetric(
            *_OPP_REB,
            allowed_per_48=team.per48["rebounds"],
            percent_vs_league_average=None,
            sigma_deviation=0.0,
            rank=team.competition_rank["rebounds"],
        )
    first = rebound.teams[0]
    for wire in REBOUND_TYPE_WIRE_NAMES:
        identity = (REBOUND_TYPES_BASE, wire, wire)
        league[identity] = LeagueMatchupMetric(
            *identity,
            average_allowed_per_48=first.league_average[wire],
            sigma=first.population_sigma[wire],
            team_count=len(rebound.teams),
        )
        for team in rebound.teams:
            teams.setdefault(team.team_id, {})[identity] = TeamMatchupMetric(
                *identity,
                allowed_per_48=team.per48[wire],
                percent_vs_league_average=None,
                sigma_deviation=0.0,
                rank=team.competition_rank[wire],
            )
    return _WindowMetricIndex(league=league, teams=teams)


def _availability(window_name: str) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "traditional": {window_name: dict(_AVAILABLE)},
        REBOUND_TYPES_BASE: {window_name: dict(_AVAILABLE)},
    }


def score_pair(
    scorer: MatchupService,
    *,
    window_name: str,
    index: _WindowMetricIndex,
    opponent_id: int,
    diet: _PlayerDiet,
) -> tuple[float, float, bool] | None:
    """``(current, candidate, fell_back)`` for one window, or ``None``."""

    availability = _availability(window_name)
    current = scorer._aggregate_offensive_score_window(window_name, index, opponent_id, availability)["blend"]
    if current is None:
        return None
    candidate = scorer._diet_component(
        base=REBOUND_TYPES_BASE,
        stat_weights=None,
        slice_keys=None,
        window_name=window_name,
        metric_index=index,
        opponent_id=opponent_id,
        summary=None,
        diet=diet,
        availability=availability,
    )
    if candidate is None:
        # The Matchup's contract: no usable rebound diet falls back to the
        # current ``traditional`` component.
        return current["value"], current["value"], True
    return current["value"], candidate["value"], False


# --- samples ----------------------------------------------------------------------


def _team_universe(games: Sequence[CanonicalGame]) -> frozenset[int]:
    observed = {fact.team_id for game in games for fact in game.team_facts}
    return NBA_TEAM_IDS if observed & NBA_TEAM_IDS else frozenset(range(1, 31))


def _windows_before(
    prior: Sequence[CanonicalGame],
    day: date,
    *,
    season: str,
    team_ids: frozenset[int],
) -> dict[str, _WindowMetricIndex]:
    """Both windows' indexes from games dated before ``day`` only."""

    as_of = day - timedelta(days=1)
    expected = frozenset(game.game_id for game in prior)
    by_team: dict[int, list[CanonicalGame]] = defaultdict(list)
    for game in prior:
        for fact in game.team_facts:
            by_team[fact.team_id].append(game)
    last_n = {
        team_id: sorted(games, key=lambda g: (g.game_date, g.game_id))[-LAST_N:]
        for team_id, games in by_team.items()
    }
    requests: dict[str, dict[str, Any]] = {"season": {}}
    if set(by_team) == set(team_ids) and all(len(games) >= LAST_N for games in last_n.values()):
        requests["last_15"] = {
            "window_games": LAST_N,
            "expected_team_game_ids": {
                team_id: frozenset(g.game_id for g in games) for team_id, games in last_n.items()
            },
        }
    indexes: dict[str, _WindowMetricIndex] = {}
    for name, extra in requests.items():
        arguments = {
            "season": season,
            "as_of": as_of,
            "expected_game_ids": expected,
            "team_ids": team_ids,
            **extra,
        }
        traditional = materialize_team_window(prior, **arguments)
        rebound = materialize_rebound_type_window(prior, **arguments)
        if traditional.complete and rebound.complete:
            indexes[name] = _index(traditional, rebound)
    return indexes


def _stored_diet(rows: Iterable[Mapping[str, Any]]) -> _PlayerDiet:
    facts = tuple(
        StoredPlayerDietFact(
            player_id=row["player_id"],
            base=REBOUND_TYPES_BASE,
            slice_key=row["slice_key"],
            share=row["share"],
            volume=row["volume"],
            games_played=row["games_played"],
            volume_unit=row["volume_unit"],
            provider=row["provider"],
            retrieved_at=_EPOCH,
        )
        for row in rows
    )
    return _PlayerDiet.build(facts, lambda base, base_facts: observed_diet_share(base, base_facts))


def build_samples(
    games: Iterable[CanonicalGame],
    *,
    season: str,
    min_prior_games: int = MIN_PRIOR_GAMES,
    min_minutes: float = MIN_MINUTES,
) -> list[Sample]:
    """Score every qualifying player-game from facts dated before it."""

    regular = sorted(
        (game for game in games if game.season == season and game.season_type == "Regular Season"),
        key=lambda game: (game.game_date, game.game_id),
    )
    if not regular:
        return []
    team_ids = _team_universe(regular)
    scorer = _scorer(season)
    by_day: dict[date, list[CanonicalGame]] = defaultdict(list)
    for game in regular:
        by_day[game.game_date].append(game)
    prior: list[CanonicalGame] = []
    prior_games: dict[int, int] = defaultdict(int)
    prior_rebounds: dict[int, int] = defaultdict(int)
    samples: list[Sample] = []
    for day in sorted(by_day):
        # ``prior`` holds exactly the games dated before ``day``: this day's
        # games join it only after every one of them has been scored.
        eligible = [
            (game, player)
            for game in by_day[day]
            for player in game.player_facts
            if prior_games[player.player_id] >= min_prior_games and player.minutes >= min_minutes
        ]
        if eligible:
            indexes = _windows_before(prior, day, season=season, team_ids=team_ids)
            diet_rows = derive_player_rebound_type_diet_rows(prior) if indexes else ()
            rows_by_player: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
            for row in diet_rows:
                rows_by_player[row["player_id"]].append(row)
            for game, player in eligible:
                opponent_id = next(
                    fact.opponent_team_id for fact in game.team_facts if fact.team_id == player.team_id
                )
                rows = rows_by_player.get(player.player_id, ())
                diet = _stored_diet(rows)
                scores: dict[str, tuple[float, float]] = {}
                fell_back = False
                for name, index in indexes.items():
                    pair = score_pair(
                        scorer,
                        window_name=name,
                        index=index,
                        opponent_id=opponent_id,
                        diet=diet,
                    )
                    if pair is not None:
                        scores[name] = pair[:2]
                        fell_back = fell_back or pair[2]
                if not scores:
                    continue
                two_point = sum(row["share"] for row in rows if row["slice_key"] in TWO_POINT_SLICES)
                samples.append(
                    Sample(
                        game_id=game.game_id,
                        game_date=day,
                        player_id=player.player_id,
                        target=player.rebounds
                        - prior_rebounds[player.player_id] / prior_games[player.player_id],
                        two_point_share=two_point,
                        scores=scores,
                        fell_back=fell_back,
                    )
                )
        for game in by_day[day]:
            for player in game.player_facts:
                if player.minutes > 0:
                    prior_games[player.player_id] += 1
                    prior_rebounds[player.player_id] += player.rebounds
        prior.extend(by_day[day])
    return samples


# --- statistics -------------------------------------------------------------------


def _average_ranks(values: np.ndarray) -> np.ndarray:
    unique, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    upper = np.cumsum(counts)
    return (upper - (counts - 1) / 2.0)[inverse]


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rho with average ranks; 0.0 when either side is constant."""

    a = _average_ranks(np.asarray(x, dtype=float))
    b = _average_ranks(np.asarray(y, dtype=float))
    a = a - a.mean()
    b = b - b.mean()
    denominator = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return 0.0 if denominator == 0.0 else float((a * b).sum() / denominator)


def cluster_bootstrap_difference(
    current: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
    days: np.ndarray,
    *,
    resamples: int,
    seed: int,
) -> tuple[float, float]:
    """95% interval of rho(candidate) - rho(current), resampling game days."""

    unique_days, inverse = np.unique(days, return_inverse=True)
    members = [np.flatnonzero(inverse == k) for k in range(len(unique_days))]
    rng = np.random.default_rng(seed)
    differences = np.empty(resamples)
    for i in range(resamples):
        chosen = rng.integers(0, len(members), size=len(members))
        index = np.concatenate([members[k] for k in chosen])
        differences[i] = spearman(candidate[index], target[index]) - spearman(current[index], target[index])
    low, high = np.percentile(differences, [2.5, 97.5])
    return float(low), float(high)


def evaluate_cell(
    rows: Sequence[tuple[date, float, float, float]],
    *,
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    """One window x population: N, both rhos, the difference and its interval."""

    if len(rows) < 2:
        return {"n": len(rows), "win": False, "reason": "too few player-games"}
    days = np.array([row[0].toordinal() for row in rows])
    current = np.array([row[1] for row in rows])
    candidate = np.array([row[2] for row in rows])
    target = np.array([row[3] for row in rows])
    rho_current = spearman(current, target)
    rho_new = spearman(candidate, target)
    low, high = cluster_bootstrap_difference(current, candidate, target, days, resamples=resamples, seed=seed)
    difference = rho_new - rho_current
    return {
        "n": len(rows),
        "game_days": int(len(np.unique(days))),
        "rho_current": rho_current,
        "rho_new": rho_new,
        "difference": difference,
        "ci95": [low, high],
        "win": bool(difference > 0 and low > 0),
    }


def evaluate(samples: Sequence[Sample], *, resamples: int, seed: int) -> dict[str, Any]:
    if resamples < 1:
        raise ValueError("resamples must be positive")
    cells: dict[str, dict[str, Any]] = {}
    for window in WINDOWS:
        windowed = [s for s in samples if window in s.scores]
        cells[window] = {
            "overall": evaluate_cell(
                [(s.game_date, *s.scores[window], s.target) for s in windowed],
                resamples=resamples,
                seed=seed,
            ),
            "bigs": evaluate_cell(
                [(s.game_date, *s.scores[window], s.target) for s in windowed if is_big(s.two_point_share)],
                resamples=resamples,
                seed=seed,
            ),
        }
    verdict = "win" if all(cells[w][p]["win"] for w in WINDOWS for p in POPULATIONS) else "lose"
    return {
        "verdict": verdict,
        "windows": cells,
        "population": {
            "min_prior_games": MIN_PRIOR_GAMES,
            "min_minutes": MIN_MINUTES,
            "bigs_two_point_share": BIGS_TWO_POINT_SHARE,
            "samples": len(samples),
            "candidate_fell_back_to_current": sum(s.fell_back for s in samples),
        },
        "bootstrap": {"resamples": resamples, "seed": seed, "unit": "game day"},
    }


def summary_text(report: Mapping[str, Any]) -> str:
    lines = [
        f"REB Matchup Score backtest {report['season']} "
        f"(bootstrap {report['bootstrap']['resamples']} resamples by game day, "
        f"seed {report['bootstrap']['seed']})"
    ]
    for window in WINDOWS:
        for population in POPULATIONS:
            cell = report["windows"][window][population]
            if "rho_new" not in cell:
                lines.append(f"  {window:8} {population:8} n={cell['n']} {cell['reason']}")
                continue
            low, high = cell["ci95"]
            lines.append(
                f"  {window:8} {population:8} n={cell['n']:>6} "
                f"rho_current={cell['rho_current']:+.4f} rho_new={cell['rho_new']:+.4f} "
                f"diff={cell['difference']:+.4f} CI95=[{low:+.4f}, {high:+.4f}] "
                f"{'win' if cell['win'] else 'lose'}"
            )
    lines.append(f"verdict: {report['verdict']}")
    return "\n".join(lines)


def run(
    games: Iterable[CanonicalGame],
    *,
    season: str,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    samples = build_samples(games, season=season)
    return {"season": season, **evaluate(samples, resamples=resamples, seed=seed)}


def underived_games(games: Iterable[CanonicalGame]) -> list[str]:
    """Games whose player rows never derived the miss-type split."""

    return sorted(
        game.game_id
        for game in games
        if any(player.def_two_pt_rebounds is None for player in game.player_facts)
    )


def load_games(database_url: str, season: str) -> tuple[CanonicalGame, ...]:
    engine = open_read_only_engine(database_url)
    try:
        repository = CanonicalGameLedgerRepository(engine)
        ids = [summary.game_id for summary in repository.list_games(season)]
        return tuple(game for game in repository.get_games(ids) if game is not None)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--database-url", help="or set DATABASE_URL")
    parser.add_argument("--output", help="write the JSON report here")
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    if args.resamples < MIN_RESAMPLES:
        parser.error(f"--resamples must be at least {MIN_RESAMPLES}")
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("--database-url or DATABASE_URL is required")
    games = load_games(database_url, args.season)
    if not games:
        print(json.dumps({"error": "no_games", "season": args.season}), file=sys.stderr)
        return 2
    missing = underived_games(games)
    if missing:
        print(
            json.dumps(
                {
                    "error": "rebound_types_not_derived",
                    "games": len(missing),
                    "first": missing[:5],
                }
            ),
            file=sys.stderr,
        )
        return 2
    try:
        report = run(games, season=args.season, resamples=args.resamples, seed=args.seed)
    except LedgerDerivationUnavailable as error:
        print(json.dumps({"error": "derivation_unavailable", "detail": str(error)}), file=sys.stderr)
        return 2
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(summary_text(report))
    if args.output:
        Path(args.output).write_text(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
