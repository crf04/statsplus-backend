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

import hashlib
import json
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

import redis
from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import RuntimeSettings
from app.errors import AppError
from app.services.matchup_reuse import ComposedMatchupCache, PublicationSnapshotReuse
from app.services.matchup_snapshot import (
    SnapshotMatchups,
    read_game_matchup,
    request_matchups,
)
from app.services.slate_fallback import read_slate
from app.services.target_resolution import MatchupReader, SlateReader
from app.utils.redis_breaker import RedisCircuitBreaker

#: Players below this many Season points per game are not worth a ticker cell.
EFFECT_MIN_SCORING = 8
#: A spotlight pick has to be a player the reader has heard of.
SPOTLIGHT_MIN_SCORING = 12
EFFECTS_PER_GAME = 3
#: Bumped whenever the selection rules or the response shape change, so a
#: stored response of the old rules is never served.
SLATE_EDGES_CACHE_SCHEMA = 2
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
        redis_client: Any | None = None,
        snapshot_reuse: PublicationSnapshotReuse | None = None,
        matchup_cache: ComposedMatchupCache | None = None,
    ) -> None:
        self.slates = slates
        self.matchups = matchups
        # All three together let every game compose from one Publication
        # snapshot with stored-only injuries (as Target resolution does);
        # without them each game reads through ``matchups.get_matchup``.
        self.publication_reader = publication_reader
        self.injuries = injuries
        self.settings = settings
        # Optional reuse (#422), shared with Target resolution.
        self.snapshot_reuse = snapshot_reuse
        self.matchup_cache = matchup_cache
        # The response cache is off, never an error, without a client, without
        # settings or with its flag off.
        self._redis = redis_client
        self._cache_enabled = (
            redis_client is not None and settings is not None and settings.cache.slate_edges_enabled
        )
        self._breaker = RedisCircuitBreaker(message="Slate edges cache unavailable (%s); bypassing for %ss")

    def get_edges(self, requested_date: str | None = None, *, fallback: str | None = None) -> dict[str, Any]:
        """The date is the Slate's own: absent is the current ET Slate Date and
        a malformed one is its ``invalid_input`` refusal; ``fallback`` is the
        Slate's ``previous_game_day`` rule (``slate_fallback.read_slate``)."""

        slate = read_slate(self.slates.get_slate, requested_date, fallback)
        slate_games = list(slate["games"])
        if not slate_games:
            return self._response(slate["slate_date"], [], [])
        matchups = self._request_matchups()
        key = self._cache_key(slate, matchups)
        cached = None if key is None else self._cache_read(key)
        if cached is not None:
            return cached

        reads = slate.get("freshness") or {}
        composed = [
            self._compose_game(matchups, game, reads.get("pool"), reads.get("schedule"))
            for game in slate_games
        ]
        response = self._response(
            slate["slate_date"],
            [entry for entry, _ in composed],
            [pick for _, picks in composed for pick in picks],
        )
        if key is not None:
            self._cache_write(key, response)
        return response

    def _compose_game(
        self,
        matchups: MatchupReader,
        game: Mapping[str, Any],
        pool: Mapping[str, Any] | None = None,
        schedule: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """One game's entry and its spotlight candidates, from its Matchup.

        The Matchup is reduced here, so only one document is held at a time.
        """

        try:
            matchup = read_game_matchup(matchups, game, pool, schedule)
            players = self._scored_players(matchup)
            picks = [
                self._spotlight_candidate(game, matchup, entry)
                for entry in players
                if self._is_spotlight_eligible(entry)
            ]
        except _UNREADABLE:
            players, picks = [], []
        return (
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
            },
            picks,
        )

    def _response(
        self,
        slate_date: str,
        games: list[dict[str, Any]],
        picks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "slate_date": slate_date,
            "games": games,
            "spotlight": {
                "best": self._extreme(picks, sign=1),
                "toughest": self._extreme(picks, sign=-1),
            },
        }

    # -- result cache (modelled on the Targets backtest cache, #279) ---------

    def _cache_key(self, slate: Mapping[str, Any], matchups: MatchupReader) -> str | None:
        """``slate:edges:v<schema>:<sha256>``, or ``None`` when nothing is cached.

        The body is the slate date, the evidence season, the Publication
        generation the Matchups will be composed from (live pointers and any
        retained history rows), each game's
        id and status, and the score thresholds.  A new publication, or a game
        starting or finishing, changes the key.  The user is not in it: the
        response is the same for everyone.
        """

        if not self._cache_enabled or not isinstance(matchups, SnapshotMatchups):
            return None
        snapshot = matchups.snapshot
        generation = getattr(snapshot, "generation", None)
        if not generation:
            return None
        settings = self.settings
        body = {
            "schema": SLATE_EDGES_CACHE_SCHEMA,
            "slate_date": slate["slate_date"],
            "season": matchups.season,
            # Both: a capture can mix live pointers with retained history rows.
            "generation": [list(entry) for entry in generation],
            "retained_history": [list(entry) for entry in getattr(snapshot, "retained_history", ())],
            "games": [
                [game["game_id"], game["status"]["state"], game["scheduled_at"]] for game in slate["games"]
            ],
            # Every setting the thin verdict and the floors read, whole, so a
            # configuration rollout never serves a response of the old one.
            "thresholds": {
                "matchup_scores": settings.matchup_scores.model_dump(mode="json"),
                "player_diet_baseline": settings.player_diet_baseline.model_dump(mode="json"),
                "effect_min_scoring": EFFECT_MIN_SCORING,
                "spotlight_min_scoring": SPOTLIGHT_MIN_SCORING,
                "effects_per_game": EFFECTS_PER_GAME,
            },
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return f"slate:edges:v{SLATE_EDGES_CACHE_SCHEMA}:{digest}"

    def _cache_read(self, key: str) -> dict[str, Any] | None:
        """A stored response, or ``None``: every failure is a miss, never a 5xx."""

        if self._breaker.is_open():
            return None
        try:
            payload = self._redis.get(key)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as error:
            self._breaker.record_failure(error)
            return None
        except Exception:
            return None
        if not payload:
            return None
        try:
            return json.loads(zlib.decompress(payload).decode("utf-8"))
        except (TypeError, ValueError, zlib.error):
            return None

    def _cache_write(self, key: str, response: Mapping[str, Any]) -> None:
        if self._breaker.is_open():
            return
        try:
            payload = zlib.compress(json.dumps(response, separators=(",", ":")).encode("utf-8"), 6)
            self._redis.setex(key, self.settings.cache.slate_edges_ttl_seconds, payload)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as error:
            self._breaker.record_failure(error)
        except Exception:
            pass

    def _request_matchups(self) -> MatchupReader:
        return request_matchups(
            composer=self.matchups,
            publication_reader=self.publication_reader,
            injuries=self.injuries,
            settings=self.settings,
            snapshot_reuse=self.snapshot_reuse,
            cache=self.matchup_cache,
            # The edges response is cached for a TTL of its own: composing
            # from a cached Matchup would stack the two (#422).
            cache_reads=False,
        )

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
    ) -> dict[str, Any]:
        """Shape one eligible player as a pick.

        ``reason`` is ``None`` when neither of his two largest play-type slices
        has an opponent points reading: the pick is still the extreme.
        """

        player = entry["player"]
        side, opponent_side = (
            ("away_team", "home_team")
            if int(player["team_id"]) == int(game["away_team"]["team_id"])
            else ("home_team", "away_team")
        )
        value = entry["cell"]["value"]
        reason = self._reason(player, matchup, int(game[opponent_side]["team_id"]), 1 if value > 0 else -1)
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
                        "opponent_percent_vs_league": reading["percent_vs_league_average"],
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
