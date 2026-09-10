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
so a Qualifier's slice is measured through a small, approved set of box-score
columns.  The defaults follow the slice's family: points and field-goal
attempts for play and shot types, the relevant two- or three-point attempts
for shot zones, and assists for assist locations.  Each default also carries
its per-36 rate where the box score supplies minutes.  The response says this
in ``proxy``; the complete typed line remains available for saved preferences.

The player set is drawn from the whole league rather than one team, and it is
drawn from the opponent's own game-log rows: a qualifying player who has never
faced this opponent has nothing to show and is not listed, so the rows are
both the population and the evidence.  Only Regular Season rows count, because
the season average they are read against is a Regular Season rate; comparing a
playoff line to a regular-season baseline would be comparing two populations.

One immutable Publication snapshot is resolved per request and passed to every
seam, so the whole response is composed from one generation of evidence rather
than a Diet from one and game logs from another, and the game-log reads take
the opponent-indexed projection instead of decoding a season-wide payload.  No
NBA, PBP, or DFS provider is reached.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack
from logging import getLogger
from math import isfinite
from typing import Any, Callable, Protocol
import hashlib
import json
import threading
import time
import zlib

import redis
from flask import g
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.config.settings import RuntimeSettings
from app.domain.nba_events import REGULAR_SEASON_TYPE
from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
from app.domain.target_statistics import target_stat_columns
from app.models.target import TARGET_COMPARATOR_TESTS
from app.services.matchup import (
    diet_evidence_thin,
    observed_diet_share,
)
from app.services.player_diet import (
    PLAYER_DIET_BASES,
    PLAYER_DIET_PUBLICATION_STREAM_KEYS,
    PlayerDietResult,
)
from app.services.publication_snapshot_calls import (
    accepts_keyword,
    call_with_read_scope,
)
from app.services.player_game_log_repository import (
    PlayerGameLogRecord,
    PlayerSeasonLogSummary,
)
from app.services.nba_cache import REDIS_FAILURE_COOLDOWN_SECONDS
from app.services.player_game_log_values import (
    player_game_log_focal_line,
    selected_player_game_log_market_values,
)
from app.services.request_reads import request_read_scope
from app.services.statistic_catalog import StatisticCatalog
from app.services.target_conditions import (
    date_is_kept,
    minutes_are_kept,
    player_minutes_are_kept,
)


_WIRE_PRECISION = 6
_BOX_FIELDS = {
    'points': 'points', 'rebounds': 'rebounds', 'assists': 'assists',
    'field_goals_made': 'field_goals_made', 'field_goals_attempted': 'field_goals_attempted',
    'threes_made': 'three_pointers_made', 'threes_attempted': 'three_pointers_attempted',
    'free_throws_made': 'free_throws_made', 'free_throws_attempted': 'free_throws_attempted',
    'steals': 'steals', 'blocks': 'blocks', 'turnovers': 'turnovers',
    'offensive_rebounds': 'offensive_rebounds', 'defensive_rebounds': 'defensive_rebounds',
    'fouls': 'personal_fouls', 'minutes': 'minutes',
}

# ``3PA`` is the Target display spelling for the governed ``FG3A`` market.
_TARGET_MARKET_ALIASES = {"3PA": "FG3A"}

#: Every stream this read composes.  The Diet and the game logs are resolved
#: from one snapshot so a response cannot mix generations.
_PUBLICATION_STREAM_KEYS = (
    "player_game_logs",
    *sorted(PLAYER_DIET_PUBLICATION_STREAM_KEYS),
)
#: The season-wide game-log payload is never worth shipping: both game-log
#: reads resolve their own rows from the projection's opponent and player
#: indexes.
_PROJECTION_ONLY_STREAM_KEYS = frozenset({"player_game_logs"})
#: The same two facts for a caller capturing one generation to share with
#: another read (#253).
BACKTEST_PUBLICATION_STREAM_KEYS = _PUBLICATION_STREAM_KEYS
BACKTEST_PROJECTION_ONLY_STREAM_KEYS = _PROJECTION_ONLY_STREAM_KEYS
#: The Diet streams are the whole reason this read opens their payloads:
#: their decoded facts are what the qualifiers consume, so a caller that
#: already holds those facts per stream may be served from this reader's
#: decode cache without the ~2 MB rendered payload. The game-log payload is
#: still never loaded (see the projection-only set above).
BACKTEST_DECODED_ONLY_STREAM_KEYS = frozenset(
    PLAYER_DIET_PUBLICATION_STREAM_KEYS
)
#: "Resolve your own": ``backtest_target`` captures a snapshot itself unless
#: the caller hands it one.
_OWN = object()

#: The one sentence this response owes its reader.  The columns are approved
#: box-score proxies for the named Diet slices, not slice-level outcomes.
PROXY_NOTE = (
    "Outcomes are box-score proxies for the Qualifier slices, not slice-level "
    "results. Base columns are whole-game box-score stats, and /36 columns are "
    "derived from minutes. A Corner 3 Qualifier therefore reads as points and "
    "three-point attempts rather than as corner threes."
)

#: Bump whenever the backtest's computation or its stat catalogue changes
#: (#279): a different schema must never be served from a cached value the
#: older code shape wrote, and the constant is part of every key.
TARGET_BACKTEST_CACHE_SCHEMA = 1

#: The result cache lives in the shared Redis namespace under one schema.
_TARGET_CACHE_KEY_PREFIX = "targets:backtest:v1:"

#: Raw threshold precision in the cache key: the six-decimal
#: ``qualifier_signature`` folds 0.4000001 and 0.4000004 onto one line, but
#: those two Targets are different questions, so the key keeps ``repr`` of
#: the full float.


def backtest_cache_key(
    target: Mapping[str, Any],
    generation: Sequence[tuple[str, str | None, int | None, int | None]],
    *,
    season: str,
    settings: RuntimeSettings,
) -> str:
    """Build ``targets:backtest:v1:<sha256>`` over the canonical key body.

    Canonical JSON: sorted object keys, arrays in stored ``position`` order,
    thresholds as ``repr(float(...))`` so raw precision separates two
    qualifiers the wire signature would fold together.  ``note``, ``title``,
    ``stat_preferences``, timestamps, and ``firebase_uid`` are excluded on
    purpose: two users with identical definitions share one entry, and
    ownership is enforced by the Target load before any lookup.
    """

    body = {
        "schema": TARGET_BACKTEST_CACHE_SCHEMA,
        "season": season,
        "generation": [list(entry) for entry in generation],
        "qualifiers": [
            {
                "base": qualifier["base"],
                "slice_key": qualifier["slice_key"],
                "comparator": qualifier["comparator"],
                "threshold": repr(float(qualifier["threshold"])),
            }
            for qualifier in target["qualifiers"]
        ],
        "conditions": target.get("conditions"),
        "thresholds": {
            "matchup": {
                "min_games": settings.matchup_scores.min_games,
                "volume_per_game": {
                    base: settings.matchup_scores.minimum_volume_per_game(base)
                    for base in PLAYER_DIET_BASES
                },
            },
            "diet_baseline": {
                "min_games": settings.player_diet_baseline.min_games,
                "volume_per_game": {
                    base: settings.player_diet_baseline.minimum_volume_per_game(
                        base
                    )
                    for base in PLAYER_DIET_BASES
                },
            },
        },
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{_TARGET_CACHE_KEY_PREFIX}{digest}"


class TargetReader(Protocol):
    def get_target(
        self, firebase_uid: str, target_id: int
    ) -> Mapping[str, Any]: ...

    # A composed read supplies the request-scope session, so the Target load
    # joins the one connection every other seam in the request uses.
    def get_target_in_session(
        self, session: Session | None, firebase_uid: str, target_id: int
    ) -> Mapping[str, Any]: ...


class PlayerLogReader(Protocol):
    def list_opponent_rows(
        self,
        season: str,
        opponent_team_id: int,
        *,
        publication_snapshot: Any | None = None,
    ) -> Sequence[PlayerGameLogRecord]: ...

    def list_player_rows(self, season: str, player_id: int, *, publication_snapshot: Any | None = None) -> Sequence[PlayerGameLogRecord]: ...

    def get_player_summaries(
        self,
        season: str,
        player_ids: Iterable[int],
        *,
        publication_snapshot: Any | None = None,
    ) -> Mapping[int, PlayerSeasonLogSummary]: ...


class PlayerDietReader(Protocol):
    def get_for_players(
        self,
        season: str,
        player_ids: Sequence[int],
        *,
        publication_snapshot: Any | None = None,
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
        publication_reader: Any | None = None,
        engine: Engine | None = None,
        redis_client: Any | None = None,
        cache_clock: Callable[[], float] | None = None,
    ) -> None:
        self.targets = targets
        self.player_logs = player_logs
        self.player_diets = player_diets
        self.publication_reader = publication_reader
        self.settings = settings
        # One request checks out one connection for every read it composes;
        # without an engine each seam keeps opening its own, exactly as the
        # Matchup and Selection reads default without one.
        self._engine = engine
        # The saved-Target result cache (#279): shared Redis under the
        # generation-keyed namespace.  No client, or the flag off, is a
        # feature that is off -- never an error.
        self._redis = redis_client
        self._cache_enabled = (
            redis_client is not None and settings.cache.target_backtest_enabled
        )
        self._cache_ttl_seconds = settings.cache.target_backtest_ttl_seconds
        self._clock = cache_clock or time.monotonic
        self._breaker_lock = threading.Lock()
        self._open_until = 0.0
        self._statistics = {
            statistic.market_category: statistic
            for statistic in statistic_catalog.statistics
            if statistic.market_category is not None
        }

    def backtest(self, firebase_uid: str, target_id: int) -> dict[str, Any]:
        """Return one of the caller's Targets with its season to date.

        The owned Target load runs inside the same ``request_read_scope`` the
        whole response composes on, so one request checks out one connection
        rather than a second for the Target row.  A Target the caller does
        not own is missing, never forbidden, so the existence of another
        account's Target is not observable here.
        """

        with request_read_scope(self._engine) as (connection, session):
            # Without an engine the scope yields no session, so the Target
            # load keeps the per-call default exactly as every other seam.
            if session is not None:
                target = self.targets.get_target_in_session(
                    session, firebase_uid, target_id
                )
            else:
                target = self.targets.get_target(firebase_uid, target_id)
            return self.backtest_target(
                target, connection=connection, session=session
            )

    def backtest_target(
        self,
        target: Mapping[str, Any],
        *,
        publication_snapshot: Any = _OWN,
        connection: Connection | None = None,
        session: Session | None = None,
    ) -> dict[str, Any]:
        """Return one Target mapping with its season to date.

        The mapping is the item ``list_targets`` returns, or a Draft Target
        validated into that shape without an id; it is echoed as given, so a
        draft comes back as a draft.  Nothing here reads the caller's stored
        Targets, which is what lets the Lab evaluate a Target that does not
        exist yet exactly as the detail evaluates one that does.

        A caller composing this read alongside another passes the generation
        it already holds as ``publication_snapshot``; none is captured then.
        """

        season = self.settings.nba.current_season
        qualifiers = list(target["qualifiers"])
        markets = self._stat_columns(qualifiers)
        # A saved Target is the only caller whose response may be served from
        # the shared Redis result cache (#279): ``backtest`` loads it through
        # the request scope, so a cache hit still echoes that fresh row,
        # whatever ``note`` or ``updated_at`` has since become.  The Lab
        # hands its own snapshot in for a Draft Target the cache must never
        # see.
        cache_key = (
            self._saved_backtest_cache_key(target, season, session)
            if publication_snapshot is _OWN and target.get("id") is not None
            else None
        )
        if cache_key is not None:
            evidence = self._cache_read(cache_key)
            if evidence is not None:
                self._stamp_targets_cache("hit")
                return {
                    "target": dict(target),
                    "season": evidence["season"],
                    "proxy": PROXY_NOTE,
                    "stat_columns": evidence["stat_columns"],
                    "summary": evidence["summary"],
                    "players": evidence["players"],
                    "games_considered": evidence["games_considered"],
                }
        with ExitStack() as scope:
            # One connection for every read this request composes, exactly as
            # the Matchup and Selection reads share theirs; a caller already
            # holding one (``backtest``) hands its connection and session in,
            # so nothing here checks out a second.  Without an engine
            # each seam keeps opening its own.
            if connection is None and session is None:
                connection, session = scope.enter_context(
                    request_read_scope(self._engine)
                )
            # One snapshot for the whole response: the Diet a player ate and
            # the games they played have to come from the same generation of
            # evidence.
            snapshot = (
                self._publication_snapshot(season, session=session)
                if publication_snapshot is _OWN
                else publication_snapshot
            )
            opponent_team_id = NBA_TEAM_TRICODE_TO_ID[target["opponent"]]
            rows = tuple(record for record in call_with_read_scope(
                self.player_logs.list_opponent_rows, season, opponent_team_id,
                publication_snapshot=snapshot, connection=connection,
            ) if record.season_type == REGULAR_SEASON_TYPE)
            conditions = target.get("conditions")
            defender = conditions.get("defender") if conditions else None
            player_minutes = conditions.get("player_minutes") if conditions else None
            defender_minutes = {}
            if defender:
                defender_minutes = {
                    row.game_id: row.minutes for row in call_with_read_scope(
                        self.player_logs.list_player_rows, season, defender["player_id"],
                        publication_snapshot=snapshot, connection=connection,
                    ) if row.team_id == opponent_team_id and row.season_type == REGULAR_SEASON_TYPE
                }
            kept = tuple(row for row in rows if date_is_kept(conditions, row.game_date)
                         and (not defender or minutes_are_kept(defender, defender_minutes.get(row.game_id, 0))))
            players = self._players(
                target,
                qualifiers,
                markets,
                season,
                snapshot,
                kept,
                player_minutes=player_minutes,
                connection=connection,
            )
            readings = getattr(snapshot, "reads", None)
            eligible = (
                snapshot is not None
                and isinstance(readings, Mapping)
                and all(
                    read.available and read.unavailable_reason is None
                    for read in readings.values()
                )
            )
            if cache_key is not None:
                if eligible:
                    # A pointer advance between the pre-check generation and
                    # this capture files the result under the captured
                    # generation, so the next request's pre-check key finds
                    # it or recomputes it -- never a torn hybrid.
                    self._cache_write(
                        self._cache_key(
                            target,
                            season,
                            getattr(snapshot, "generation", ()),
                        ),
                        {
                            "players": players,
                            "summary": self._summary(players, markets),
                            "stat_columns": list(markets),
                            "games_considered": {
                                "played": len({row.game_id for row in rows}),
                                "kept": len({row.game_id for row in kept}),
                            },
                            "season": season,
                        },
                    )
                    self._stamp_targets_cache("miss")
                else:
                    # An unavailable stream means the read ran on refusal
                    # labels, not evidence; a result computed from those is
                    # not one any future generation may reuse.
                    self._stamp_targets_cache("bypass")
        return {
            "target": dict(target),
            "season": season,
            "proxy": PROXY_NOTE,
            "stat_columns": list(markets),
            "summary": self._summary(players, markets),
            "players": players,
            "games_considered": {"played": len({row.game_id for row in rows}), "kept": len({row.game_id for row in kept})},
        }

    @classmethod
    def _summary(
        cls,
        players: Sequence[Mapping[str, Any]],
        markets: Sequence[str],
    ) -> dict[str, Any]:
        """Reduce every listed game to one line per stat column.

        ``mean_difference`` is the mean of (game stat - that player's season
        average) over every listed game with both values available, and
        ``over_average_share`` is the share of those same pairs at or above
        the average -- an exact average counts, as both comparators are
        inclusive.  Both are ``None`` when no such pair exists, including
        when no game is listed or every listed value is unavailable.  Computed
        here rather than by each reader so the Lab and the saved detail show
        the same numbers.
        """

        lines = [
            (player["season_averages"], game["stats"])
            for player in players
            for game in player["games"]
        ]
        columns = {}
        for market in markets:
            differences = [
                stats[market] - averages[market]
                for averages, stats in lines
                if stats.get(market) is not None
                and averages.get(market) is not None
            ]
            columns[market] = {
                "mean_difference": (
                    cls._number(sum(differences) / len(differences))
                    if differences
                    else None
                ),
                "over_average_share": (
                    cls._number(
                        sum(1 for difference in differences if difference >= 0)
                        / len(differences)
                    )
                    if differences
                    else None
                ),
            }
        return {"players": len(players), "games": len(lines), "columns": columns}

    def _saved_backtest_cache_key(
        self,
        target: Mapping[str, Any],
        season: str,
        session: Session | None,
    ) -> str | None:
        """Build the generation-keyed cache key, or ``None`` when off.

        A ``None`` key is the cache deciding it has nothing to say: the
        feature flag off, no reader able to answer a generation, or no
        Request scope session to read the pointers on.  Nothing Redis is
        contacted in that case and ``targets_cache`` stays ``-``.
        """

        if not self._cache_enabled:
            return None
        reader = self.publication_reader
        generation_reader = getattr(reader, "generation", None)
        if not callable(generation_reader):
            return None
        try:
            generation = generation_reader(
                _PUBLICATION_STREAM_KEYS,
                season=season,
                session=session,
            )
        except Exception:
            # The pre-check must not turn a Redis-or-reader outage into a
            # failed request: answer as the uncached read answers.
            return None
        if generation is None:
            return None
        return self._cache_key(target, season, generation)

    def _cache_key(
        self,
        target: Mapping[str, Any],
        season: str,
        generation: Sequence[tuple[str, str | None, int | None, int | None]],
    ) -> str:
        return backtest_cache_key(
            target, generation, season=season, settings=self.settings
        )

    def _cache_read(self, key: str) -> dict[str, Any] | None:
        """Return one cached result's evidence, or ``None`` to compute.

        Every failure is a miss, never a 5xx: an open circuit skips the
        read entirely, a Redis-level error opens the circuit and falls
        through, and an unreadable stored value just misses this once.
        """

        if self._circuit_open():
            return None
        try:
            payload = self._redis.get(key)
        except (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ) as error:
            self._open_circuit(error)
            return None
        except Exception:
            return None
        if not payload:
            return None
        try:
            return json.loads(zlib.decompress(payload).decode("utf-8"))
        except (TypeError, ValueError, zlib.error, json.JSONDecodeError):
            return None

    def _cache_write(self, key: str, evidence: Mapping[str, Any]) -> None:
        """Store one result's evidence under its generation key.

        The stored document is the derived evidence only, zlib level-6
        over canonical JSON, so a hit decompresses to the identical body a
        miss would have serialized.  A write that fails is dropped: the
        response has already been computed, and a full cache must not
        block a read on its own room.
        """

        if self._circuit_open():
            return
        try:
            payload = zlib.compress(
                json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                ),
                6,
            )
            self._redis.setex(key, self._cache_ttl_seconds, payload)
        except (
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ) as error:
            self._open_circuit(error)
        except Exception:
            pass

    def _circuit_open(self) -> bool:
        with self._breaker_lock:
            return self._clock() < self._open_until

    def _open_circuit(self, error: Exception) -> None:
        with self._breaker_lock:
            now = self._clock()
            already_open = now < self._open_until
            self._open_until = now + REDIS_FAILURE_COOLDOWN_SECONDS
        if already_open:
            return
        logger.warning(
            "Targets result cache unavailable (%s); bypassing for %ss",
            error,
            REDIS_FAILURE_COOLDOWN_SECONDS,
        )

    @staticmethod
    def _stamp_targets_cache(value: str) -> None:
        """Stamp ``flask.g`` so the request log can report the cache seam.

        Service callers outside a Request context (the tests) simply stay
        unstamped; the log line's default already says ``-``.
        """

        try:
            g.targets_cache = value
        except RuntimeError:
            pass

    def _publication_snapshot(self, season: str, *, session: Session | None = None):
        """Resolve this request's immutable Publication generation, if any.

        Mirrors the Matchup and Selection reads, including their
        ``projection_only_keys`` narrowing: this service resolves its rows
        from the projection's own opponent and player indexes, so shipping the
        season-wide game-log payload alongside it would be decoding the whole
        league to answer a question about one opponent.
        """

        if self.publication_reader is None:
            return None
        snapshot = getattr(self.publication_reader, "snapshot", None)
        if not callable(snapshot):
            snapshot = getattr(self.publication_reader, "read_snapshot", None)
        if not callable(snapshot):
            return None
        keyword = {}
        if accepts_keyword(snapshot, "projection_only_keys"):
            keyword["projection_only_keys"] = _PROJECTION_ONLY_STREAM_KEYS
        if accepts_keyword(snapshot, "decoded_only_keys"):
            keyword["decoded_only_keys"] = BACKTEST_DECODED_ONLY_STREAM_KEYS
        if session is not None and accepts_keyword(snapshot, "session"):
            keyword["session"] = session
        return snapshot(_PUBLICATION_STREAM_KEYS, season=season, **keyword)

    @staticmethod
    def _stat_columns(
        qualifiers: Sequence[Mapping[str, Any]],
    ) -> tuple[str, ...]:
        """Resolve the approved default union order."""

        return target_stat_columns(qualifiers)

    def _players(
        self,
        target: Mapping[str, Any],
        qualifiers: Sequence[Mapping[str, Any]],
        markets: Sequence[str],
        season: str,
        snapshot: Any | None,
        records: Sequence[PlayerGameLogRecord],
        *,
        player_minutes: int | None = None,
        connection: Connection | None = None,
    ) -> list[dict[str, Any]]:
        rows_by_player: dict[int, list[PlayerGameLogRecord]] = {}
        for record in records:
            # A playoff line is not evidence against a Regular Season average,
            # which is the baseline every column below is read against.
            if record.season_type != REGULAR_SEASON_TYPE:
                continue
            if (
                player_minutes is not None
                and not player_minutes_are_kept(record.minutes, player_minutes)
            ):
                continue
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
            else call_with_read_scope(
                self.player_diets.get_for_players,
                season,
                player_ids,
                publication_snapshot=snapshot,
                connection=connection,
            )
        )
        summaries = call_with_read_scope(
            self.player_logs.get_player_summaries,
            season,
            player_ids,
            publication_snapshot=snapshot,
            connection=connection,
        )

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
                    summaries[player_id].rate_rows,
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
        season_rows: Sequence[PlayerGameLogRecord],
    ) -> dict[str, Any]:
        """Shape one qualifying player against the games they have played.

        Identity is the game-time identity the Matchup's historical
        participants also use, taken from the most recent game against this
        opponent.  That is deliberately not a claim about the player's current
        team: a trade after their last meeting with this opponent is not
        visible here, and the row still says who they suited up for that
        night, which is what the games below are evidence about.
        """

        newest = rows[0]
        season_values = self._season_averages(summary, markets)
        per_game_points = (
            None
            if summary.season_rate is None
            else self._number_or_none(summary.season_rate.per_game.get("PTS"))
        )
        games = []
        for record in rows:
            line = player_game_log_focal_line(
                record, (), self._statistics, precision=_WIRE_PRECISION
            )
            line["stats"] = self._game_stats(record, markets)
            line["line"] = {
                field: self._number(getattr(record, attr))
                for field, attr in _BOX_FIELDS.items()
            }
            games.append(line)
        return {
            "canonical_id": int(newest.player_id),
            "name": newest.player_name,
            "team_id": int(newest.team_id),
            "tricode": str(newest.team_tricode),
            "season_scoring": per_game_points,
            "shares": list(shares),
            "season_games": len(season_rows),
            "season_totals": {
                field: self._number(sum(getattr(row, attr) for row in season_rows))
                for field, attr in _BOX_FIELDS.items()
            },
            "season_averages": season_values,
            "games": games,
        }

    def _game_stats(
        self, record: PlayerGameLogRecord, markets: Sequence[str]
    ) -> dict[str, float | None]:
        """Read governed game values and add Target's minute-based rates."""

        display_bases = {
            market: market[:-3] if market.endswith("/36") else market
            for market in markets
        }
        governed_markets = tuple(
            dict.fromkeys(
                _TARGET_MARKET_ALIASES.get(base, base)
                for base in display_bases.values()
            )
        )
        values = selected_player_game_log_market_values(
            record, governed_markets, self._statistics
        )
        stats: dict[str, float | None] = {}
        for market, base in display_bases.items():
            value = values[_TARGET_MARKET_ALIASES.get(base, base)]
            if market.endswith("/36"):
                minutes = getattr(record, "minutes", None)
                if minutes is None:
                    value = None
                else:
                    minutes = float(minutes)
                    value = (
                        None
                        if not isfinite(minutes) or minutes <= 0
                        else value / minutes * 36.0
                    )
            stats[market] = self._number_or_none(value)
        return stats

    @classmethod
    def _season_averages(
        cls,
        summary: PlayerSeasonLogSummary,
        markets: Sequence[str],
    ) -> dict[str, float | None]:
        """Read all-season rates, with per-36 weighted by total minutes."""

        rate = summary.season_rate
        per_game = {} if rate is None else rate.per_game
        per_minute = {} if rate is None else rate.per_minute
        result: dict[str, float | None] = {}
        for market in markets:
            if market.endswith("/36"):
                base = market[:-3]
                rate_base = "FG3A" if base == "3PA" else base
                value = (
                    None
                    if rate is None or rate.total_minutes <= 0
                    else per_minute.get(rate_base)
                )
                if value is not None:
                    value *= 36.0
                result[market] = cls._number_or_none(value)
                continue

            rate_market = "FG3A" if market == "3PA" else market
            value = per_game.get(rate_market)
            result[market] = cls._number_or_none(value)
        return result

    @staticmethod
    def _number(value: float) -> float:
        return round(float(value), _WIRE_PRECISION)

    @classmethod
    def _number_or_none(cls, value: float | None) -> float | None:
        return None if value is None else cls._number(value)


__all__ = ["TargetBacktestService"]

logger = getLogger(__name__)
