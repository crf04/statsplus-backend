"""Resolve every Target for one account against one Slate Date.

A Target is an opponent plus the Qualifiers a player has to meet (#244).  This
service answers the day-scoped question the Slate and Targets pages ask: for
the viewed date, which Targets have a game, who on the opposing side meets
every Qualifier, and what does the opponent's defense look like on those
slices right now.

Resolution reads no NBA or DFS provider.  It composes two governed reads that
already exist: the Slate for the date, and -- for each game a Target's
opponent plays -- the Matchup document for that game.  Both are database-only
compositions over the durable seams (slate events, Player Pool, Player Diet
facts, team windows, injuries).  Composing from the Matchup rather than
re-deriving from those seams is deliberate: a Target's per-slice shares, thin
evidence, posted markets, injury badges, participant status, defense-sheet
values and player ordering are then the Matchup's own values, so the two
surfaces cannot disagree about the same game -- including which evidence named
that game's players, since a completed game resolves against the same
canonical game-log participants the Matchup page lists.  One Matchup is read at most once per request
however many Targets name the teams in it, and a game no Target names is never
read at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.config.settings import RuntimeSettings
from app.services.player_diet import PLAYER_DIET_SLICE_LABELS


_WINDOW_NAMES = ("season", "last_15")

_COMPARATORS = {
    "at_or_above": lambda share, threshold: share >= threshold,
    "at_or_below": lambda share, threshold: share <= threshold,
}

#: An idle opponent has no game, so no evidence named participants for it and
#: there is no source to report.
_IDLE = {
    "status": "unavailable",
    "source": None,
    "context": None,
    "unavailable_reason": "opponent_idle",
}


class TargetReader(Protocol):
    def list_targets(self, firebase_uid: str) -> Sequence[Mapping[str, Any]]: ...


class SlateReader(Protocol):
    def get_slate(
        self, requested_date: str | None = None
    ) -> Mapping[str, Any]: ...


class MatchupReader(Protocol):
    def get_matchup(self, *, game_id: str) -> Mapping[str, Any]: ...


class TargetResolutionService:
    """Evaluate one account's Targets against one ET Slate Date."""

    def __init__(
        self,
        *,
        targets: TargetReader,
        slates: SlateReader,
        matchups: MatchupReader,
        settings: RuntimeSettings,
    ) -> None:
        self.targets = targets
        self.slates = slates
        self.matchups = matchups
        self.settings = settings

    def resolve(
        self, firebase_uid: str, *, requested_date: str | None = None
    ) -> dict[str, Any]:
        """Return every Target for the caller, live ones first.

        The date is the Slate's own: an absent date is that service's current
        ET Slate Date and a malformed one is its ``invalid_input`` refusal,
        so a Targets page and a Slate page always agree about which day they
        are showing.
        """

        slate = self.slates.get_slate(requested_date)
        games = self._games_by_tricode(slate["games"])
        read_matchups: dict[str, Mapping[str, Any]] = {}

        live: list[dict[str, Any]] = []
        idle: list[dict[str, Any]] = []
        for target in self.targets.list_targets(firebase_uid):
            scheduled = games.get(target["opponent"])
            if scheduled is None:
                idle.append(self._idle(target))
                continue
            game, opponent_side, filtered_side = scheduled
            game_id = game["game_id"]
            if game_id not in read_matchups:
                read_matchups[game_id] = self.matchups.get_matchup(game_id=game_id)
            live.append(
                self._live(
                    target, game, opponent_side, filtered_side, read_matchups[game_id]
                )
            )
        return {"slate_date": slate["slate_date"], "targets": live + idle}

    @staticmethod
    def _games_by_tricode(
        games: Sequence[Mapping[str, Any]],
    ) -> dict[str, tuple[Mapping[str, Any], str, str]]:
        """Index the date's games by each side's tricode.

        The value names the game and which side of it a Target aimed at that
        tricode is the opponent of, so the side being filtered is always the
        other one.
        """

        indexed: dict[str, tuple[Mapping[str, Any], str, str]] = {}
        for game in games:
            for opponent_side, filtered_side in (
                ("away_team", "home_team"),
                ("home_team", "away_team"),
            ):
                tricode = game[opponent_side]["tricode"]
                indexed.setdefault(tricode, (game, opponent_side, filtered_side))
        return indexed

    @staticmethod
    def _idle(target: Mapping[str, Any]) -> dict[str, Any]:
        """Shape a Target whose opponent does not play on the viewed date.

        Its Qualifiers and note still come back so the Targets page can manage
        it, but there is no game, nobody to filter, and no game-scoped window
        to read a defense sheet from.
        """

        return {
            "target": dict(target),
            "game": None,
            "context": [],
            "availability": dict(_IDLE),
            "players": [],
        }

    def _live(
        self,
        target: Mapping[str, Any],
        game: Mapping[str, Any],
        opponent_side: str,
        filtered_side: str,
        matchup: Mapping[str, Any],
    ) -> dict[str, Any]:
        opponent_team_id = int(game[opponent_side]["team_id"])
        opponent_sheet = next(
            team["defense_sheet"]
            for team in matchup["teams"]
            if int(team["team_id"]) == opponent_team_id
        )
        league = matchup["league"]
        availability = self._participant_availability(matchup)
        qualifiers = list(target["qualifiers"])
        return {
            "target": dict(target),
            "game": {
                "game_id": game["game_id"],
                "scheduled_at": game["scheduled_at"],
                "status": dict(game["status"]),
                "opponent": self._team(game[opponent_side]),
                "opposing_team": self._team(game[filtered_side]),
            },
            "context": [
                self._context(qualifier, opponent_sheet, league)
                for qualifier in qualifiers
            ],
            "availability": availability,
            "players": (
                []
                if availability["status"] != "available"
                else self._players(qualifiers, matchup["players"], opponent_team_id)
            ),
        }

    @staticmethod
    def _team(side: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "team_id": int(side["team_id"]),
            "tricode": side["tricode"],
            "name": side["name"],
        }

    @staticmethod
    def _participant_availability(matchup: Mapping[str, Any]) -> dict[str, Any]:
        """Report the status of whatever named the opposing side's players.

        This is the Matchup's own Participants section, so a Target and the
        Matchup detail page always agree about the same game: a scheduled game
        resolves against the stored Player Pool, and a completed one resolves
        against the canonical game-log participants the Matchup page lists.
        ``source`` is restated in the ``experience.player_source`` vocabulary
        (``player_pool`` or ``game_logs``) so one word describes the evidence
        both surfaces are showing.
        """

        experience = matchup["experience"]
        participants = experience["sections"]["participants"]
        return {**participants, "source": experience["player_source"]}

    @classmethod
    def _context(
        cls,
        qualifier: Mapping[str, Any],
        opponent_sheet: Mapping[str, Any],
        league: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Carry every defense-sheet row for one Qualifier's slice.

        The rows, their window values, and the Base/window availability that
        governs whether a window has a value at all are the Matchup's, so the
        number a Target shows for a slice is the number the Matchup shows.
        """

        base = qualifier["base"]
        slice_key = qualifier["slice_key"]
        league_rows = {
            row["key"]: row for row in league["defense_sheet"].get(base, ())
        }
        return {
            "base": base,
            "slice_key": slice_key,
            "label": PLAYER_DIET_SLICE_LABELS[slice_key],
            "availability": {
                window: dict(league["surface_availability"][base][window])
                for window in _WINDOW_NAMES
            },
            "metrics": [
                {
                    "key": row["key"],
                    "label": row["label"],
                    "markets": list(row["markets"]),
                    "opponent": {
                        window: row[window] for window in _WINDOW_NAMES
                    },
                    "league": {
                        window: league_rows.get(row["key"], {}).get(window)
                        for window in _WINDOW_NAMES
                    },
                }
                for row in opponent_sheet.get(base, ())
                if cls._names_slice(row["key"], slice_key)
            ],
        }

    @staticmethod
    def _names_slice(row_key: str, slice_key: str) -> bool:
        """Whether a defense-sheet row belongs to one Diet slice.

        A row key is the slice on its own when the slice and the statistic are
        the same name, and ``slice:stat`` otherwise.
        """

        return row_key == slice_key or row_key.startswith(f"{slice_key}:")

    def _players(
        self,
        qualifiers: Sequence[Mapping[str, Any]],
        players: Sequence[Mapping[str, Any]],
        opponent_team_id: int,
    ) -> list[dict[str, Any]]:
        """Name the opposing participants meeting every Qualifier.

        The Matchup already orders its players by Season scoring descending,
        so preserving its order is that ordering.  A thin diet is flagged, not
        excluded: the Target list never disagrees with the Matchup about who
        is in the game.  A game-log participant carries no posted markets, as
        on the Matchup page.
        """

        fits = []
        for player in players:
            if int(player["team_id"]) == opponent_team_id:
                continue
            fit = self._fit(qualifiers, player)
            if fit is not None:
                fits.append(fit)
        return fits

    def _fit(
        self,
        qualifiers: Sequence[Mapping[str, Any]],
        player: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        shares = []
        thin = False
        for qualifier in qualifiers:
            base_facts = player["diet_shares"].get(qualifier["base"]) or ()
            fact = next(
                (
                    item
                    for item in base_facts
                    if item["key"] == qualifier["slice_key"]
                ),
                None,
            )
            if fact is None:
                # No stored share for the slice is not a share of zero, so the
                # player is unjudged rather than judged to fit.
                return None
            season = fact["season"]
            if not _COMPARATORS[qualifier["comparator"]](
                season["share"], qualifier["threshold"]
            ):
                return None
            shares.append(
                {
                    "base": qualifier["base"],
                    "slice_key": qualifier["slice_key"],
                    "share": season["share"],
                    "league_average_share": season["league_average_share"],
                }
            )
            thin = thin or self._thin(qualifier["base"], base_facts)
        return {
            "canonical_id": int(player["canonical_id"]),
            "name": player["name"],
            "team_id": int(player["team_id"]),
            "tricode": player["tricode"],
            "posted_markets": list(player["posted_markets"]),
            "injury_badge_ref": player["injury_badge_ref"],
            "season_scoring": player["season_scoring"],
            "thin": thin,
            "shares": shares,
        }

    def _thin(self, base: str, base_facts: Sequence[Mapping[str, Any]]) -> bool:
        """Whether one Base's Diet evidence is too slight to lean on.

        The same floors the Matchup Score marks a cell thin with: every stored
        fact in the Base has to clear ``min_games``, and the player's total
        Base volume per game -- the sum of each fact's own volume per game --
        has to clear that Base's floor.
        """

        floors = self.settings.matchup_scores
        if any(
            fact["season"]["games_played"] < floors.min_games
            for fact in base_facts
        ):
            return True
        volume_per_game = sum(
            fact["season"]["volume"] / fact["season"]["games_played"]
            for fact in base_facts
        )
        return volume_per_game < floors.minimum_volume_per_game(base)


__all__ = ["TargetResolutionService"]
