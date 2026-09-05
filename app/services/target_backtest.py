"""Backtest one Target against the season it has already had (#246).

Resolution (#245) answers a day's question: who on tonight's opposing side
fits this Target.  The backtest answers the season's: of everyone in the
league whose Diet fits it, who has actually played this opponent, and what did
they do.  It is a separate read for that reason -- the league-wide game-log
scan only runs when a reader expands a Target, so the Slate stays cheap.

Two things make this composition honest rather than a second opinion:

*Thin* is the Matchup's own rule.  ``observed_diet_share`` reads each Base's
coverage and ``diet_evidence_thin`` renders the verdict, so a player the
Matchup marks thin is the player this read excludes.  Unlike resolution, which
flags a thin player and keeps them, the backtest drops them: the longer view
is a claim about production, and a claim resting on an unusable Diet is worse
than no claim.

*Outcomes are proxies.*  No per-game shot-zone or play-type evidence exists,
so a Qualifier's slice is measured through the box-score markets the Defense
Sheet already maps that slice to (``qualifier_slice_outcome_markets``).  Only
the slice's outcome rows are asked -- a shot zone's made shots, not its
attempts -- because the question is what a player produced against this
opponent, and an attempt is not production.  A Corner 3 Qualifier therefore
reads as points and threes, never as corner threes made, and the response says
so in ``proxy``.

The player set is drawn from the whole league rather than one team, and it is
drawn from the opponent's own game-log rows: a qualifying player who has never
faced this opponent has nothing to show and is not listed, so the rows are
both the population and the evidence.  No NBA, PBP, or DFS provider is
reached.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from app.config.settings import RuntimeSettings
from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.models.target import TARGET_COMPARATOR_TESTS
from app.services.matchup import (
    diet_evidence_thin,
    observed_diet_share,
    qualifier_slice_outcome_markets,
)
from app.services.player_diet import PlayerDietResult
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerSeasonLogSummary,
)
from app.services.player_game_log_values import player_game_log_focal_line
from app.services.statistic_catalog import StatisticCatalog


_WIRE_PRECISION = 6

#: The one sentence this response owes its reader.  Every column below is a
#: box-score market the Qualifier's slice maps to, not the slice itself.
PROXY_NOTE = (
    "Outcomes are box-score proxies for the Qualifier slices, not slice-level "
    "results. Each stat column is a market the Matchup's defense sheet already "
    "maps to a Qualifier's slice, so a Corner 3 Qualifier reads as points and "
    "threes rather than as corner threes made."
)


class TargetReader(Protocol):
    def get_target(
        self, firebase_uid: str, target_id: int
    ) -> Mapping[str, Any]: ...


class PlayerLogReader(Protocol):
    def list_opponent_rows(
        self, season: str, opponent_team_id: int
    ) -> Sequence[PlayerGameLogRecord]: ...

    def get_player_summaries(
        self, season: str, player_ids: Iterable[int]
    ) -> Mapping[int, PlayerSeasonLogSummary]: ...


class PlayerDietReader(Protocol):
    def get_for_players(
        self, season: str, player_ids: Sequence[int]
    ) -> PlayerDietResult: ...


class TargetBacktestService:
    """Report one Target's season to date over the whole league."""

    def __init__(
        self,
        *,
        targets: TargetReader,
        player_logs: PlayerLogReader,
        player_diets: PlayerDietReader | None,
        statistic_catalog: StatisticCatalog,
        settings: RuntimeSettings,
    ) -> None:
        self.targets = targets
        self.player_logs = player_logs
        self.player_diets = player_diets
        self.settings = settings
        self._statistics = {
            statistic.market_category: statistic
            for statistic in statistic_catalog.statistics
            if statistic.market_category is not None
        }

    def backtest(self, firebase_uid: str, target_id: int) -> dict[str, Any]:
        """Return one of the caller's Targets with its season to date.

        A Target the caller does not own is missing, never forbidden, so the
        existence of another account's Target is not observable here.
        """

        target = self.targets.get_target(firebase_uid, target_id)
        season = self.settings.nba.current_season
        qualifiers = list(target["qualifiers"])
        markets = self._stat_columns(qualifiers)
        return {
            "target": dict(target),
            "season": season,
            "proxy": PROXY_NOTE,
            "stat_columns": list(markets),
            "players": self._players(target, qualifiers, markets, season),
        }

    @staticmethod
    def _stat_columns(
        qualifiers: Sequence[Mapping[str, Any]],
    ) -> tuple[str, ...]:
        """Union every Qualifier slice's outcome markets, in Qualifier order.

        Two Qualifiers naming overlapping markets contribute one column each,
        the first time each is named, so the column order is the reader's own
        Qualifier order rather than an arbitrary set ordering.
        """

        columns: list[str] = []
        for qualifier in qualifiers:
            for market in qualifier_slice_outcome_markets(
                qualifier["base"], qualifier["slice_key"]
            ):
                if market not in columns:
                    columns.append(market)
        return tuple(columns)

    def _players(
        self,
        target: Mapping[str, Any],
        qualifiers: Sequence[Mapping[str, Any]],
        markets: Sequence[str],
        season: str,
    ) -> list[dict[str, Any]]:
        opponent_team_id = NBA_TEAM_TRICODE_TO_ID[target["opponent"]]
        rows_by_player: dict[int, list[PlayerGameLogRecord]] = {}
        for record in self.player_logs.list_opponent_rows(
            season, opponent_team_id
        ):
            rows_by_player.setdefault(int(record.player_id), []).append(record)
        if not rows_by_player:
            # Nobody has faced this opponent, so there is nobody to judge and
            # no reason to read a Diet at all.
            return []

        player_ids = tuple(sorted(rows_by_player))
        # A deployment with no Diet service stores no Diet, so no player has
        # a share for any slice and nobody fits.  That is the same empty list
        # a Target nobody meets returns, which is honest here: the demo
        # database this arises on carries no Diet schema at all, so there is
        # no evidence being withheld.
        diets = (
            PlayerDietResult(season, {}, ())
            if self.player_diets is None
            else self.player_diets.get_for_players(season, player_ids)
        )
        summaries = self.player_logs.get_player_summaries(season, player_ids)

        players = []
        for player_id in player_ids:
            shares = self._fit(
                qualifiers,
                diets.players.get(player_id, ()),
                summaries.get(player_id),
                diets,
            )
            if shares is None:
                continue
            players.append(
                self._player(
                    rows_by_player[player_id],
                    shares,
                    summaries[player_id],
                    markets,
                )
            )
        # The Matchup's own ordering, so the two Target surfaces read the same
        # way: Season scoring descending, canonical id breaking ties.
        players.sort(
            key=lambda player: (
                player["season_scoring"] is None,
                -(player["season_scoring"] or 0),
                player["canonical_id"],
            )
        )
        return players

    def _fit(
        self,
        qualifiers: Sequence[Mapping[str, Any]],
        facts: Sequence[Any],
        summary: PlayerSeasonLogSummary | None,
        diets: PlayerDietResult,
    ) -> list[dict[str, Any]] | None:
        """Return one player's per-Qualifier shares, or ``None`` if unfit.

        Unfit is any of three things: a missing share for a named slice, a
        share the comparator refuses, or a Base whose evidence is thin.  The
        thin verdict is ``diet_evidence_thin``, asked of each Base a Qualifier
        names, so a Base the Matchup would not score is a Base this read will
        not stake a season claim on either.
        """

        shares = []
        for qualifier in qualifiers:
            base = qualifier["base"]
            base_facts = tuple(fact for fact in facts if fact.base == base)
            fact = next(
                (
                    item
                    for item in base_facts
                    if item.slice_key == qualifier["slice_key"]
                ),
                None,
            )
            if fact is None:
                # No stored share for the slice is not a share of zero, so the
                # player is unjudged rather than judged to fit.
                return None
            if not TARGET_COMPARATOR_TESTS[qualifier["comparator"]](
                fact.share, qualifier["threshold"]
            ):
                return None
            if diet_evidence_thin(
                base=base,
                observed_share=observed_diet_share(base, base_facts),
                selected_facts=base_facts,
                summary=summary,
                settings=self.settings.matchup_scores,
            ):
                return None
            baseline = diets.baselines.get((base, qualifier["slice_key"]))
            shares.append(
                {
                    "base": base,
                    "slice_key": qualifier["slice_key"],
                    "share": self._number(fact.share),
                    "league_average_share": (
                        None
                        if baseline is None
                        or baseline.league_average_share is None
                        else self._number(baseline.league_average_share)
                    ),
                }
            )
        return shares

    def _player(
        self,
        rows: Sequence[PlayerGameLogRecord],
        shares: Sequence[Mapping[str, Any]],
        summary: PlayerSeasonLogSummary,
        markets: Sequence[str],
    ) -> dict[str, Any]:
        """Shape one qualifying player against the games they have played.

        Identity is the newest row's, which is the game-time identity the
        Matchup's historical participants also use: a mid-season trade cannot
        rewrite who a player suited up for, and the newest row is who they are
        now.
        """

        newest = rows[0]
        per_game = summary.season_rate.per_game
        return {
            "canonical_id": int(newest.player_id),
            "name": newest.player_name,
            "team_id": int(newest.team_id),
            "tricode": str(newest.team_tricode),
            "season_scoring": self._number_or_none(per_game.get("PTS")),
            "shares": list(shares),
            "season_averages": {
                market: self._number_or_none(per_game.get(market))
                for market in markets
            },
            "games": [
                player_game_log_focal_line(
                    record, markets, self._statistics, precision=_WIRE_PRECISION
                )
                for record in rows
            ],
        }

    @staticmethod
    def _number(value: float) -> float:
        return round(float(value), _WIRE_PRECISION)

    @classmethod
    def _number_or_none(cls, value: float | None) -> float | None:
        return None if value is None else cls._number(value)


__all__ = ["PROXY_NOTE", "TargetBacktestService"]
