"""What a process and Redis reuse between requests that compose Matchups.

Target resolution and the slate edges each compose several Matchups from one
Publication snapshot.  Two costs repeat without changing (crf04/statsplus#217):

* capturing that snapshot (~0.3 s) when the Publication generation has not
  moved, which ``PublicationSnapshotReuse`` removes per process; and
* composing a Matchup (~0.13 s) whose inputs have not moved, which
  ``ComposedMatchupCache`` removes in Redis.

Both are optimisations only: a miss, a disabled flag, a reader that cannot
report its generation cheaply, or a Redis failure all compose exactly as
before.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping
from typing import Any

import redis

from app.config.settings import RuntimeSettings
from app.services.research_season import published_capture, season_is_pinned
from app.utils.redis_breaker import RedisCircuitBreaker

#: Bumped whenever the Matchup document's shape changes, so a stored document
#: of the old shape is never served.
MATCHUP_CACHE_SCHEMA = 1
#: A snapshot of the live generation plus, briefly, the one it replaced.
_SNAPSHOT_REUSE_ENTRIES = 2


class PublicationSnapshotReuse:
    """Reuse one captured Publication snapshot until the generation moves.

    The generation check is ``publication_reader.generation(...)``: one
    statement over the stream, pointer and version rows with no payload read.
    The reuse key is that generation (taken *before* the capture, so a pointer
    that advances mid-capture only costs a later recapture), the stream set and
    the season rule.  A capture also stamps each stream's age and freshness
    label, so an entry is never reused past
    ``cache.snapshot_reuse_max_age_seconds``.  At most two entries are held and
    one lock covers lookup and capture, so concurrent requests on a process
    share a single capture.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Any, tuple[float, tuple[str, Any]]] = OrderedDict()

    def capture(
        self,
        settings: RuntimeSettings,
        publication_reader: Any,
        stream_keys: Collection[str],
        capture: Callable[[Any], Any],
    ) -> tuple[str, Any]:
        """``published_capture`` that reuses a snapshot of the same generation."""

        max_age = settings.cache.snapshot_reuse_max_age_seconds
        generation = (
            self._generation(settings, publication_reader, stream_keys)
            if max_age > 0
            else None
        )
        if generation is None:
            return published_capture(settings, publication_reader, capture)
        key = (
            tuple(sorted(str(stream) for stream in stream_keys)),
            season_is_pinned(settings),
            settings.nba.current_season,
            generation,
        )
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and self._clock() - entry[0] < max_age:
                self._entries.move_to_end(key)
                return entry[1]
            captured = published_capture(settings, publication_reader, capture)
            if captured[1] is not None:
                self._entries[key] = (self._clock(), captured)
                self._entries.move_to_end(key)
                while len(self._entries) > _SNAPSHOT_REUSE_ENTRIES:
                    self._entries.popitem(last=False)
            return captured

    @staticmethod
    def _generation(
        settings: RuntimeSettings, publication_reader: Any, stream_keys: Collection[str]
    ) -> tuple[Any, ...] | None:
        """The live generation, or ``None`` when it cannot be read cheaply."""

        generation = getattr(publication_reader, "generation", None)
        if not callable(generation) or not (
            season_is_pinned(settings)
            or getattr(publication_reader, "captures_published_season", False) is True
        ):
            return None
        try:
            return tuple(generation({*map(str, stream_keys), "player_game_logs"}))
        except Exception:
            return None


class ComposedMatchupCache:
    """Composed Matchups in Redis, shared by every snapshot-composed read."""

    def __init__(self, redis_client: Any | None) -> None:
        self._redis = redis_client
        self._breaker = RedisCircuitBreaker(
            message="Matchup cache unavailable (%s); bypassing for %ss"
        )

    def key(
        self,
        settings: RuntimeSettings | None,
        snapshot: Any,
        season: str | None,
        game: Mapping[str, Any],
    ) -> str | None:
        """``matchup:composed:v<schema>:<sha256>``, or ``None`` when uncacheable.

        The body is everything the compose reads that moves with a deploy or a
        publication: the evidence season, the Publication generation (live
        pointers and any retained history rows), the game's id, state and tip
        time, and every setting the scores read.  A stored injury or Player
        Pool change moves none of these, so it can lag by the TTL.  The user is
        not in it: a Matchup is the same for everyone.
        """

        if (
            self._redis is None
            or settings is None
            or not settings.cache.matchup_enabled
            or season is None
        ):
            return None
        generation = getattr(snapshot, "generation", None)
        if not generation:
            return None
        body = {
            "schema": MATCHUP_CACHE_SCHEMA,
            "season": season,
            "generation": [list(entry) for entry in generation],
            "retained_history": [
                list(entry) for entry in getattr(snapshot, "retained_history", ())
            ],
            "game": [game["game_id"], game["status"]["state"], game["scheduled_at"]],
            "settings": {
                "matchup_scores": settings.matchup_scores.model_dump(mode="json"),
                "player_diet_baseline": settings.player_diet_baseline.model_dump(
                    mode="json"
                ),
                "slate_schedule_max_age_hours": (
                    settings.catalog.slate_schedule_max_age_hours
                ),
                "pinned_season": (
                    settings.nba.current_season if season_is_pinned(settings) else None
                ),
            },
        }
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return f"matchup:composed:v{MATCHUP_CACHE_SCHEMA}:{digest}"

    def get_or_compose(
        self,
        key: str | None,
        settings: RuntimeSettings,
        compose: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """The stored Matchup for ``key``, else ``compose()`` stored.

        A composed document is returned as its own JSON round trip, so a hit
        and a miss hand callers equal documents by construction.  Every Redis
        failure is a miss, never an error.
        """

        if key is None:
            return compose()
        cached = self._read(key)
        if cached is not None:
            return cached
        composed = compose()
        try:
            text = json.dumps(composed, separators=(",", ":"))
        except (TypeError, ValueError):
            return composed
        self._write(key, text, settings.cache.matchup_ttl_seconds)
        return json.loads(text)

    def _read(self, key: str) -> Mapping[str, Any] | None:
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

    def _write(self, key: str, text: str, ttl: int) -> None:
        if self._breaker.is_open():
            return
        try:
            self._redis.setex(key, ttl, zlib.compress(text.encode("utf-8"), 6))
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as error:
            self._breaker.record_failure(error)
        except Exception:
            pass


__all__ = ["ComposedMatchupCache", "PublicationSnapshotReuse"]
