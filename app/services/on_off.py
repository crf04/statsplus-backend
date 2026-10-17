"""PBPStats on/off-floor (WOWY) splits behind ``GET /api/on_off`` (#424).

One call asks: for this team and season, how did every teammate play while
the ``on`` players were on the floor and the ``off`` players were off it?
Players are StatsPlus player ids; the provider translates them to PBPStats
ids.  Per-36 rates are derived here from the provider's raw totals and
minutes.

Results are cached in Redis, keyed by team, season, on-set and off-set.  An
entry is fresh for ``on_off_ttl_seconds`` (24 hours) and is kept for a week
more as a stale fallback: when a fetch fails the cached copy is served with
``stale: true``, and with no copy the answer is ``unavailable``.  Upstream
calls are spaced at least ``on_off_min_interval_seconds`` apart so a cold
cache or the warm cannot hammer ``api.pbpstats.com`` (whose terms and
rate limits are unconfirmed).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config.settings import RuntimeSettings
from app.domain.nba_teams import (
    NBA_TEAM_TRICODE_TO_ID,
    canonical_nba_team_abbreviation,
)
from app.errors import InvalidInputError, ProviderUnavailableError
from app.services.nba_stats_adapter import validate_canonical_season
from app.services.pbp_stats_adapter import WowyPlayerRow
from app.utils.redis_breaker import RedisCircuitBreaker

logger = logging.getLogger(__name__)

SOURCE = "pbpstats"
CACHE_KEY_PREFIX = "on_off:v1:"
#: How long past its freshness an entry is kept as the stale fallback.
STALE_RETENTION_SECONDS = 7 * 86400
#: ``on[]`` is a combination (PBPStats WOWY returns "all of them together"),
#: so it stays small; ``off[]`` may name a whole injured rotation.
MAX_ON_PLAYERS = 5
MAX_OFF_PLAYERS = 15
#: One Redis key holds the shared upstream slot; see ``_throttle``.
THROTTLE_KEY = "on_off:throttle"
THROTTLE_MAX_WAITS = 50
#: The warm stops after this many failures in a row (an upstream outage).
WARM_MAX_CONSECUTIVE_FAILURES = 3
#: Injury-report statuses that get a warmed split.
WARM_STATUSES = frozenset({"Out", "Doubtful", "Questionable"})


@dataclass(frozen=True, slots=True)
class OnOffRequest:
    """One normalized, validated lineup-state question."""

    team: str
    season: str
    on: tuple[int, ...]
    off: tuple[int, ...]

    @property
    def cache_key(self) -> str:
        return (
            f"{CACHE_KEY_PREFIX}{self.team}:{self.season}"
            f":on={','.join(map(str, self.on))}"
            f":off={','.join(map(str, self.off))}"
        )


def build_request(
    team: Any, season: Any, on: Iterable[Any] = (), off: Iterable[Any] = ()
) -> OnOffRequest:
    """Validate raw inputs; anything unusable is ``InvalidInputError``."""

    abbreviation = canonical_nba_team_abbreviation(str(team or "").strip())
    if abbreviation not in NBA_TEAM_TRICODE_TO_ID:
        raise InvalidInputError("team must be an NBA team abbreviation.")
    try:
        canonical_season = validate_canonical_season(season)
    except ValueError as error:
        raise InvalidInputError("season must be a YYYY-YY NBA season.") from error
    sides: list[tuple[int, ...]] = []
    for label, values, limit in (
        ("on", on, MAX_ON_PLAYERS), ("off", off, MAX_OFF_PLAYERS)
    ):
        ids: set[int] = set()
        for value in values:
            try:
                player_id = int(str(value).strip())
            except ValueError as error:
                raise InvalidInputError(
                    f"{label}[] must be StatsPlus player ids."
                ) from error
            if player_id <= 0:
                raise InvalidInputError(f"{label}[] must be StatsPlus player ids.")
            ids.add(player_id)
        if len(ids) > limit:
            raise InvalidInputError(f"{label}[] accepts at most {limit} players.")
        sides.append(tuple(sorted(ids)))
    on_ids, off_ids = sides
    if not on_ids and not off_ids:
        raise InvalidInputError("Give at least one player in on[] or off[].")
    if set(on_ids) & set(off_ids):
        raise InvalidInputError("A player cannot be both on and off the floor.")
    return OnOffRequest(abbreviation, canonical_season, on_ids, off_ids)


def _per36(total: int, minutes: int) -> float:
    return round(total * 36 / minutes, 2)


def _player_payload(row: WowyPlayerRow) -> dict[str, Any]:
    totals = {
        "pts": row.points,
        "reb": row.rebounds,
        "ast": row.assists,
        "fg3m": row.fg3m,
        "fga": row.fga,
    }
    return {
        "player_id": row.player_id,
        "name": row.name,
        "minutes": row.minutes,
        "possessions": row.possessions,
        "usage": None if row.usage is None else round(row.usage, 2),
        "totals": totals,
        "per36": {key: _per36(value, row.minutes) for key, value in totals.items()},
    }


def normalize_rows(rows: Iterable[WowyPlayerRow]) -> list[dict[str, Any]]:
    """Players with minutes in the lineup state, most minutes first."""

    players = [_player_payload(row) for row in rows if row.minutes > 0]
    players.sort(key=lambda player: (-player["minutes"], player["player_id"]))
    return players


class OnOffCacheWriteError(RuntimeError):
    """A warm could not store what it fetched, so a rerun would refetch it."""


class OnOffService:
    def __init__(
        self,
        provider: Any,
        settings: RuntimeSettings,
        *,
        redis_client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._provider = provider
        self._ttl = settings.cache.on_off_ttl_seconds
        self._min_interval = settings.cache.on_off_min_interval_seconds
        self._redis = (
            redis_client
            if redis_client is not None and settings.cache.on_off_enabled
            else None
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._sleep = sleep
        self._throttle_lock = threading.Lock()
        self._last_upstream: float | None = None
        self._breaker = RedisCircuitBreaker(
            message="On/off cache unavailable (%s); bypassing for %ss"
        )

    # -- reads -----------------------------------------------------------

    def get(
        self, team: Any, season: Any, on: Iterable[Any] = (), off: Iterable[Any] = ()
    ) -> dict[str, Any]:
        request = build_request(team, season, on, off)
        cached = self._cache_read(request)
        if cached is not None and self._is_fresh(cached):
            return self._response(request, cached, stale=False)
        try:
            entry = self._fetch_and_store(request)
        except ProviderUnavailableError as error:
            logger.warning(
                "on_off fetch failed key=%s cached=%s: %s",
                request.cache_key, cached is not None, error,
            )
            if cached is not None:
                return self._response(request, cached, stale=True)
            return self._unavailable(request)
        return self._response(request, entry, stale=False)

    def get_cached(
        self, team: Any, season: Any, on: Iterable[Any] = (), off: Iterable[Any] = ()
    ) -> dict[str, Any]:
        """The cached split only; this never reaches the provider.

        A fresh copy is ``ok``; an expired one still held as the stale
        fallback is ``ok`` with ``stale: true``.  With no copy the status is
        ``not_warmed`` (the warm has not stored it yet), or ``unavailable``
        when there is no cache to read at all.  Injury Scenarios read this way
        so an HTTP request never waits on throttled upstream calls.
        """

        request = build_request(team, season, on, off)
        if self._redis is None:
            return self._unavailable(request)
        cached = self._cache_read(request)
        if cached is None:
            return {**self._unavailable(request), "status": "not_warmed"}
        return self._response(request, cached, stale=not self._is_fresh(cached))

    def warm(self, requests: Iterable[OnOffRequest]) -> dict[str, Any]:
        """Fill missing or expired entries; idempotent within the TTL.

        A request whose entry is still fresh makes no upstream call, so a
        rerun (the warm follows every injury refresh) fetches only players not
        yet cached.  A failed cache write stops the run: its result would not
        be kept, so continuing only spends upstream calls.  With no cache the
        run does nothing at all (it would only spend upstream calls).
        """

        if self._redis is None:
            return {"skipped_reason": "cache_disabled", "fetched": 0,
                    "fresh": 0, "failed": 0, "aborted": False,
                    "cache_write_failed": False}
        fetched = fresh = failed = consecutive = 0
        aborted = cache_write_failed = False
        seen: set[str] = set()
        for request in requests:
            if request.cache_key in seen:
                continue
            seen.add(request.cache_key)
            cached = self._cache_read(request)
            if cached is not None and self._is_fresh(cached):
                fresh += 1
                continue
            try:
                self._fetch_and_store(request, strict=True)
            except OnOffCacheWriteError as error:
                logger.error("on_off warm stopped: %s", error)
                aborted = cache_write_failed = True
                break
            except ProviderUnavailableError as error:
                failed += 1
                consecutive += 1
                logger.warning("on_off warm failed key=%s: %s", request.cache_key, error)
                if consecutive >= WARM_MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    break
                continue
            consecutive = 0
            fetched += 1
        return {"skipped_reason": None, "fetched": fetched, "fresh": fresh,
                "failed": failed, "aborted": aborted,
                "cache_write_failed": cache_write_failed}

    # -- upstream --------------------------------------------------------

    def _throttle(self) -> None:
        """Space upstream calls at least ``min_interval`` apart.

        With Redis the slot is shared by every worker and the warm: an atomic
        ``SET NX PX`` claims it, and a caller that loses sleeps for the key's
        remaining lifetime and tries again.  Without Redis (cache off, or the
        breaker open) only this process's calls are spaced.
        """

        if self._min_interval <= 0:
            return
        if self._redis is not None and not self._breaker.is_open():
            try:
                for _ in range(THROTTLE_MAX_WAITS):
                    if self._redis.set(
                        THROTTLE_KEY, "1", nx=True, px=self._min_interval * 1000
                    ):
                        self._last_upstream = self._monotonic()
                        return
                    remaining_ms = self._redis.pttl(THROTTLE_KEY)
                    self._sleep(max(remaining_ms, 10) / 1000)
            except Exception as error:
                self._breaker.record_failure(error)
            else:
                # The slot never freed up: do not fetch unthrottled.  The
                # caller serves its stale copy or reports unavailable.
                raise ProviderUnavailableError(
                    "PBP Stats on/off throttle slot unavailable.",
                    detail="throttle wait exhausted",
                )
        with self._throttle_lock:
            now = self._monotonic()
            if self._last_upstream is not None:
                wait = self._last_upstream + self._min_interval - now
                if wait > 0:
                    self._sleep(wait)
                    now = self._monotonic()
            self._last_upstream = now

    def _fetch_and_store(
        self, request: OnOffRequest, *, strict: bool = False
    ) -> dict[str, Any]:
        self._throttle()
        rows = self._provider.get_wowy(
            team_id=NBA_TEAM_TRICODE_TO_ID[request.team],
            season=request.season,
            on_player_ids=request.on,
            off_player_ids=request.off,
        )
        entry = {
            "cached_at": self._clock().astimezone(timezone.utc).isoformat(),
            "players": normalize_rows(rows),
        }
        if not self._cache_write(request, entry) and strict:
            raise OnOffCacheWriteError(f"could not store {request.cache_key}")
        return entry

    # -- cache -----------------------------------------------------------

    def _is_fresh(self, entry: Mapping[str, Any]) -> bool:
        age = self._clock() - datetime.fromisoformat(entry["cached_at"])
        return age.total_seconds() < self._ttl

    def _cache_read(self, request: OnOffRequest) -> dict[str, Any] | None:
        if self._redis is None or self._breaker.is_open():
            return None
        try:
            raw = self._redis.get(request.cache_key)
            if raw is None:
                return None
            entry = json.loads(raw)
            datetime.fromisoformat(entry["cached_at"])
            if not isinstance(entry["players"], list):
                raise ValueError("players must be a list")
            return entry
        except Exception as error:  # Redis down or a corrupt entry: a miss
            self._breaker.record_failure(error)
            return None

    def _cache_write(self, request: OnOffRequest, entry: Mapping[str, Any]) -> bool:
        if self._redis is None or self._breaker.is_open():
            return False
        try:
            self._redis.set(
                request.cache_key,
                json.dumps(entry, sort_keys=True),
                ex=self._ttl + STALE_RETENTION_SECONDS,
            )
            return True
        except Exception as error:
            self._breaker.record_failure(error)
            return False

    # -- responses -------------------------------------------------------

    @staticmethod
    def _envelope(request: OnOffRequest) -> dict[str, Any]:
        return {
            "source": SOURCE,
            "team": request.team,
            "season": request.season,
            "on": list(request.on),
            "off": list(request.off),
        }

    def _response(
        self, request: OnOffRequest, entry: Mapping[str, Any], *, stale: bool
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            **self._envelope(request),
            "cached_at": entry["cached_at"],
            "stale": stale,
            "players": entry["players"],
        }

    def _unavailable(self, request: OnOffRequest) -> dict[str, Any]:
        return {
            "status": "unavailable",
            **self._envelope(request),
            "cached_at": None,
            "stale": False,
            "players": [],
        }


def injury_warm_requests(
    injuries: Sequence[Mapping[str, Any]],
    season: str,
    prior_season: str | None = None,
    prior_eligible: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[OnOffRequest]:
    """The splits to pre-warm for a day's injury listing.

    ``injuries`` are the rows ``GET /api/injuries`` returns (``player_id``,
    ``team``, ``status``).  Each listed Out, Doubtful or Questionable player
    with a resolved id gets the two splits an Injury Scenario reads: teammates
    with him off the floor, and with him on it.  When ``prior_eligible`` says
    the player's last season can join his scenario window (he played that
    season for the same team), the same two splits are warmed for
    ``prior_season``.  A player listed for two games appears twice; the warm
    dedupes by cache key.
    """

    requests: list[OnOffRequest] = []
    for row in injuries:
        player_id = row.get("player_id")
        team = row.get("team")
        if row.get("status") not in WARM_STATUSES:
            continue
        if player_id is None or not team:
            continue
        seasons = [season]
        if prior_season and prior_eligible is not None and prior_eligible(row):
            seasons.append(prior_season)
        for warm_season in seasons:
            requests.append(build_request(team, warm_season, off=[player_id]))
            requests.append(build_request(team, warm_season, on=[player_id]))
    return requests


__all__ = [
    "OnOffCacheWriteError",
    "OnOffRequest",
    "OnOffService",
    "build_request",
    "injury_warm_requests",
    "normalize_rows",
]
