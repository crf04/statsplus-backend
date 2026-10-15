"""The home page's slate edges: each game's biggest points effects and picks.

One request answers what the home page would otherwise read as one Matchup per
game (crf04/statsplus#204).  Like Target resolution, this reads no provider and
re-derives nothing: it composes the Slate for the date and, for each of its
games, the Matchup document the matchup route serves, so a ticker cell and a
Matchup page cannot disagree about one game.  Every Matchup is composed from
one Publication snapshot with the caller's stored-only injury reader.

What this adds of its own is selection only: the season PTS score a player
shows (the same Blend-or-single-component rule the Matchup page uses), the
8 and 12 PPG floors, the ordering, and the one-slice reason for a pick.

Each game also lists every known season PTS score, floor-free, for the Targets
cards.  A game whose Matchup cannot be read contributes no effects, no scores
and no pick; the
response still succeeds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import RuntimeSettings
from app.errors import AppError
from app.services.matchup import (
    MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
    MATCHUP_PUBLICATION_STREAM_KEYS,
)
from app.services.matchup_snapshot import (
    SnapshotMatchups,
    capture_publication_snapshot,
)
from app.services.research_season import published_capture
from app.services.target_resolution import MatchupReader, SlateReader

#: Players below this many Season points per game are not worth a ticker cell.
EFFECT_MIN_SCORING = 8
#: A spotlight pick has to be a player the reader has heard of.
SPOTLIGHT_MIN_SCORING = 12
EFFECTS_PER_GAME = 3
_REASON_BASE = "play_types"
_REASON_SLICES = 2

#: A Matchup that cannot be read: a refused or incomplete stored read.
_UNREADABLE = (AppError, SQLAlchemyError)


class SlateEdgesService:
    """Compose one Slate Date's per-game effects and Best/Toughest picks."""

    def __init__(
        self,
        *,
        slates: SlateReader,
        matchups: MatchupReader,
        publication_reader: Any | None = None,
        injuries: Any | None = None,
        settings: RuntimeSettings | None = None,
    ) -> None:
        self.slates = slates
        self.matchups = matchups
        # All three together let every game compose from one Publication
        # snapshot with stored-only injuries (as Target resolution does);
        # without them each game reads through ``matchups.get_matchup``.
        self.publication_reader = publication_reader
        self.injuries = injuries
        self.settings = settings

    def get_edges(self, requested_date: str | None = None) -> dict[str, Any]:
        """The date is the Slate's own: absent is the current ET Slate Date and
        a malformed one is its ``invalid_input`` refusal."""

        slate = self.slates.get_slate(requested_date)
        slate_games = list(slate["games"])
        matchups = self._request_matchups() if slate_games else self.matchups

        games: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for game in slate_games:
            try:
                matchup = matchups.get_matchup(game_id=game["game_id"])
                players = self._scored_players(matchup)
            except _UNREADABLE:
                players = []
            games.append(
                {
                    "game_id": game["game_id"],
                    "effects": self._effects(players),
                    "scores": [
                        {
                            "canonical_id": int(entry["player"]["canonical_id"]),
                            "value": entry["cell"]["value"],
                        }
                        for entry in players
                    ],
                }
            )
            candidates.extend(
                self._spotlight_candidate(game, matchup, player)
                for player in players
                if self._is_spotlight_eligible(player)
            )
        picks = [pick for pick in candidates if pick is not None]
        return {
            "slate_date": slate["slate_date"],
            "games": games,
            "spotlight": {
                "best": self._extreme(picks, sign=1),
                "toughest": self._extreme(picks, sign=-1),
            },
        }

    def _request_matchups(self) -> MatchupReader:
        composer = self.matchups
        if (
            self.publication_reader is None
            or self.settings is None
            or self.injuries is None
            or not callable(getattr(composer, "get_matchup_from_snapshot", None))
        ):
            return self.matchups
        season, snapshot = published_capture(
            self.settings,
            self.publication_reader,
            lambda season: capture_publication_snapshot(
                self.publication_reader,
                MATCHUP_PUBLICATION_STREAM_KEYS,
                projection_only_keys=MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
                season=season,
            ),
        )
        return SnapshotMatchups(composer, snapshot, self.injuries, season=season)

    # -- selection -----------------------------------------------------------

    @staticmethod
    def _season_pts_cell(player: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The season PTS score a Matchup row shows, as ``{value, thin, blend}``.

        The Blend; else, when the market has no Blend and names no missing
        input, its single component (a Matchup row never shows a lone
        component standing in for a withheld Blend).
        """

        window = (player.get("scores") or {}).get("PTS", {}).get("season")
        if not window:
            return None
        blend = window.get("blend")
        if blend is not None:
            return {"value": blend["value"], "thin": blend["thin"], "blend": True}
        components = window.get("components") or {}
        if window.get("missing_inputs") or len(components) != 1:
            return None
        (component,) = components.values()
        return {"value": component["value"], "thin": component["thin"], "blend": False}

    def _scored_players(self, matchup: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Players whose season PTS score is known, with the cell they show,
        in the Matchup's own order."""

        scored = []
        for player in matchup["players"]:
            cell = self._season_pts_cell(player)
            if cell is None or cell["value"] is None:
                continue
            scored.append({"player": player, "cell": cell})
        return scored

    @staticmethod
    def _order(entry: Mapping[str, Any]) -> tuple[Any, ...]:
        player = entry["player"]
        return (
            -abs(entry["cell"]["value"]),
            -(player["season_scoring"] or 0),
            int(player["canonical_id"]),
        )

    def _effects(self, scored: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        eligible = [
            entry
            for entry in scored
            if entry["player"]["season_scoring"] is not None
            and entry["player"]["season_scoring"] >= EFFECT_MIN_SCORING
        ]
        eligible.sort(key=self._order)
        return [
            {
                "canonical_id": int(entry["player"]["canonical_id"]),
                "name": entry["player"]["name"],
                "tricode": entry["player"]["tricode"],
                "season_scoring": entry["player"]["season_scoring"],
                "value": entry["cell"]["value"],
                "thin": entry["cell"]["thin"],
            }
            for entry in eligible[:EFFECTS_PER_GAME]
        ]

    @staticmethod
    def _is_spotlight_eligible(entry: Mapping[str, Any]) -> bool:
        scoring = entry["player"]["season_scoring"]
        return (
            entry["cell"]["blend"]
            and not entry["cell"]["thin"]
            and entry["cell"]["value"] != 0
            and scoring is not None
            and scoring >= SPOTLIGHT_MIN_SCORING
        )

    def _spotlight_candidate(
        self,
        game: Mapping[str, Any],
        matchup: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Shape one eligible player as a pick, or ``None`` with no reason.

        A pick without a readable reason cannot fill the contract's shape, so
        the next player stands in rather than the reason being invented.
        """

        player = entry["player"]
        side, opponent_side = (
            ("away_team", "home_team")
            if int(player["team_id"]) == int(game["away_team"]["team_id"])
            else ("home_team", "away_team")
        )
        value = entry["cell"]["value"]
        reason = self._reason(
            player, matchup, int(game[opponent_side]["team_id"]), 1 if value > 0 else -1
        )
        if reason is None:
            return None
        return {
            "game_id": game["game_id"],
            "canonical_id": int(player["canonical_id"]),
            "name": player["name"],
            "tricode": player["tricode"],
            "opponent": game[opponent_side]["tricode"],
            "season_scoring": player["season_scoring"],
            "value": value,
            "reason": reason,
            "_order": self._order(entry),
        }

    @staticmethod
    def _reason(
        player: Mapping[str, Any],
        matchup: Mapping[str, Any],
        opponent_team_id: int,
        direction: int,
    ) -> dict[str, Any] | None:
        facts = sorted(
            (
                fact
                for fact in player["diet_shares"].get(_REASON_BASE) or ()
                if fact.get("season") and fact["season"].get("share") is not None
            ),
            key=lambda fact: (-fact["season"]["share"], fact["key"]),
        )[:_REASON_SLICES]
        sheet = next(
            (
                team["defense_sheet"].get(_REASON_BASE) or ()
                for team in matchup["teams"]
                if int(team["team_id"]) == opponent_team_id
            ),
            (),
        )
        rows = {row["key"]: row.get("season") for row in sheet}
        best: tuple[float, dict[str, Any]] | None = None
        for fact in facts:
            reading = rows.get(f"{fact['key']}:PTS")
            if not reading or reading.get("percent_vs_league_average") is None:
                continue
            push = direction * reading["percent_vs_league_average"]
            if best is None or push > best[0]:
                best = (
                    push,
                    {
                        "base": _REASON_BASE,
                        "slice_key": fact["key"],
                        "share": fact["season"]["share"],
                        "league_average_share": fact["season"]["league_average_share"],
                        "opponent_rank": reading["rank"],
                        "opponent_percent_vs_league": reading[
                            "percent_vs_league_average"
                        ],
                    },
                )
        return None if best is None else best[1]

    @staticmethod
    def _extreme(picks: Sequence[Mapping[str, Any]], *, sign: int) -> dict[str, Any] | None:
        pool = [pick for pick in picks if sign * pick["value"] > 0]
        if not pool:
            return None
        chosen = min(pool, key=lambda pick: (-sign * pick["value"], pick["_order"][1:]))
        return {key: item for key, item in chosen.items() if key != "_order"}


__all__ = ["SlateEdgesService"]
