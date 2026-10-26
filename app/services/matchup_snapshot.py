"""Compose one Matchup from a caller's already-captured Publication snapshot.

Both Target preview (#253) and Target resolution (#245) hold one Publication
generation for a whole request and need a ``MatchupReader`` --
``.get_matchup(game_id=...)`` -- bound to it, so a Matchup composed this way
never captures a second, independently advancing snapshot.  This is the one
place that binding lives; neither caller defines its own copy.

``capture_publication_snapshot`` is the matching capture-side seam: both
callers resolve their own generation from an injected publication reader in
the same shape -- ``snapshot`` or the older ``read_snapshot``, narrowing
offered only where accepted -- so that step lives in one place too.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from app.config.settings import RuntimeSettings
from app.domain.utc import parse_utc_iso
from app.services.matchup import (
    MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
    MATCHUP_PUBLICATION_STREAM_KEYS,
    MatchupComposeCache,
    build_publication_metadata,
)
from app.services.matchup_reuse import ComposedMatchupCache, PublicationSnapshotReuse
from app.services.publication_snapshot_calls import accepts_keyword
from app.services.research_season import event_season, published_capture


def capture_publication_snapshot(
    publication_reader: Any | None,
    stream_keys: Any,
    *,
    projection_only_keys: Any,
    decoded_only_keys: Any = frozenset(),
    season: str,
    session: Any = None,
) -> Any:
    """Capture one Publication generation over a caller's stream set, if any.

    Mirrors the reads' own resolution -- ``snapshot`` or the older
    ``read_snapshot``, narrowing offered only where accepted -- so a reader
    either read can use, this can. Returns ``None`` when ``publication_reader``
    is absent or exposes neither method.
    """

    if publication_reader is None:
        return None
    snapshot = getattr(publication_reader, "snapshot", None)
    if not callable(snapshot):
        snapshot = getattr(publication_reader, "read_snapshot", None)
    if not callable(snapshot):
        return None
    keyword = {}
    if accepts_keyword(snapshot, "projection_only_keys"):
        keyword["projection_only_keys"] = projection_only_keys
    if accepts_keyword(snapshot, "decoded_only_keys"):
        keyword["decoded_only_keys"] = decoded_only_keys
    if session is not None and accepts_keyword(snapshot, "session"):
        keyword["session"] = session
    return snapshot(stream_keys, season=season, **keyword)


#: A game whose retained Publication cannot be named composes uncached.
_BYPASS = object()


def _agrees_with_lookup(document: Mapping[str, Any], found: Any, identity: Any) -> bool:
    """Whether the retained Publication composition listed is the one keyed.

    ``identity`` is empty for a game that reads none; otherwise its second item
    is the retained season, and the document may list at most the keyed
    Publication for that season.
    """

    if not identity:
        return True
    generation = (document.get("provenance") or {}).get("generation") or ()
    listed = {
        entry.get("publication_id")
        for entry in generation
        if isinstance(entry, Mapping)
        and entry.get("stream_key") == "player_game_logs"
        and entry.get("season") == identity[1]
    }
    return listed <= ({found[0]} if found is not None else set())


def _schedule_stamp(schedule: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The Slate's schedule freshness in the shape a Matchup reports it."""

    if not isinstance(schedule, Mapping) or "status" not in schedule:
        return None
    retrieved_at = schedule.get("retrieved_at")
    try:
        retrieved_at = None if retrieved_at is None else parse_utc_iso(str(retrieved_at)).isoformat()
    except ValueError:
        return None
    return {"status": schedule["status"], "retrieved_at": retrieved_at}


def _with_schedule_stamp(document: Mapping[str, Any], stamp: Mapping[str, Any]) -> dict[str, Any]:
    """``document`` with every schedule stamp it carries taken from ``stamp``."""

    document = dict(document)
    if isinstance(document.get("freshness"), Mapping):
        document["freshness"] = {**document["freshness"], "schedule": dict(stamp)}
    sections = (document.get("experience") or {}).get("sections")
    if isinstance(sections, Mapping) and isinstance(sections.get("schedule"), Mapping):
        document["experience"] = {
            **document["experience"],
            "sections": {
                **sections,
                "schedule": {
                    **sections["schedule"],
                    "collected_at": stamp["retrieved_at"],
                },
            },
        }
    return document


def _used_publication(document: Mapping[str, Any], found: Any) -> bool:
    """Whether the composed document lists the retained Publication ``found``."""

    if found is None:
        return False
    generation = (document.get("provenance") or {}).get("generation") or ()
    return any(
        isinstance(entry, Mapping)
        and entry.get("publication_id") == found[0]
        and entry.get("stream_key") == "player_game_logs"
        for entry in generation
    )


class SnapshotMatchupComposer(Protocol):
    def get_matchup_from_snapshot(
        self,
        *,
        game_id: str,
        publication_snapshot: Any,
        injuries: Any,
    ) -> Mapping[str, Any]: ...


class SnapshotMatchups:
    """The ``MatchupReader`` a caller holding one generation expects.

    One instance is bound to one snapshot and one injury reader for the
    lifetime of a request. It also owns the per-request memo for per-game
    work that repeats within that one generation -- team Defense Sheet
    windows and Diet baselines -- so a caller composing several games through
    the same instance (Target resolution) shares that memo automatically; a
    caller composing exactly one game (Target preview) pays nothing extra for
    a memo it never gets to reuse.
    """

    def __init__(
        self,
        composer: SnapshotMatchupComposer,
        snapshot: Any,
        injuries: Any,
        *,
        season: str | None = None,
        cache: ComposedMatchupCache | None = None,
        settings: RuntimeSettings | None = None,
        cache_reads: bool = True,
        read_scope: tuple[Any, Any] | None = None,
    ) -> None:
        self.composer = composer
        self.snapshot = snapshot
        self.injuries = injuries
        # The season ``snapshot`` was captured for, so every Matchup composed
        # from it reads its evidence in that season and no other.
        self.season = season
        # Optional: the shared Redis cache of composed Matchups (#422).  It is
        # consulted only for a caller that names the Slate game, whose state
        # and tip time are part of the key.  ``cache_reads=False`` composes
        # fresh and only writes, for a caller that stores its own result for
        # another TTL and so must not stack one on a cached Matchup.
        self._cache = cache
        self._settings = settings
        self._cache_reads = cache_reads
        self._retained: dict[str, Any] = {}
        self._compose_cache = MatchupComposeCache()
        # ``(connection, session)`` every game composes on, the capture's own;
        # ``None`` lets each game open its own.
        self._read_scope = read_scope

    def get_matchup(
        self,
        *,
        game_id: str,
        game: Mapping[str, Any] | None = None,
        pool: Mapping[str, Any] | None = None,
        schedule: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """``pool`` is the Slate's pool freshness block, part of the cache key.

        ``schedule`` is the Slate's schedule freshness: it is not in the key (a
        schedule refresh would churn the hit rate) but a cached document
        reports it, wherever the Matchup reports its schedule stamp, from the
        request's own Slate.
        """

        if self._cache is None or game is None or self._settings is None or self.season is None:
            return self._compose(game_id)
        retained = self._retained_publication(game_id)
        if retained is _BYPASS:
            return self._compose(game_id)
        found, identity = retained
        key = self._cache.key(
            self._settings,
            self.snapshot,
            self.season,
            game,
            identity,
            pool,
            self._started(game),
        )
        if key is None:
            return self._compose(game_id)
        entry = self._cache.get(key) if self._cache_reads else None
        if entry is None:
            document = self._compose(game_id)
            if not _agrees_with_lookup(document, found, identity):
                # Composition read a different retained Publication than the
                # key names (a correction activated in between): its data
                # does not belong under this key.
                return document
            used = _used_publication(document, found)
            entry = self._cache.put(key, {"matchup": document, "retained_used": used}, self._settings) or {
                "matchup": document,
                "retained_used": used,
            }
        return self._with_current_provenance(entry, found, schedule)

    def _started(self, game: Mapping[str, Any]) -> bool:
        """Whether the game's tip time has passed on the composer's clock."""

        clock = getattr(self.composer, "observed_now", None)
        now = clock() if callable(clock) else datetime.now(UTC)
        try:
            return parse_utc_iso(str(game["scheduled_at"])) <= now
        except (KeyError, ValueError):
            return True

    def _stats_freshness(self) -> Any:
        """The ``freshness.stats`` block, read once per request."""

        if "stats" not in self._retained:
            method = getattr(self.composer, "stats_freshness_block", None)
            self._retained["stats"] = None if not callable(method) else method(self.season)
        return self._retained["stats"]

    def _retained_publication(self, game_id: str) -> Any:
        """``(found, key part)`` for the retained Publication this game reads.

        Only a game of an earlier season than the evidence season reads one;
        its identity is what a correction or revocation there changes.
        ``_BYPASS`` when it cannot be named, so the game composes uncached.
        """

        schedule_season = event_season(self._settings, game_id, self.season)
        if schedule_season >= self.season:
            return None, ()
        if schedule_season not in self._retained:
            method = getattr(self.composer, "retained_publication", None)
            try:
                self._retained[schedule_season] = _BYPASS if not callable(method) else method(schedule_season)
            except Exception:
                self._retained[schedule_season] = _BYPASS
        found = self._retained[schedule_season]
        if found is _BYPASS:
            return _BYPASS
        if found is None:
            return None, ["retained", schedule_season, None]
        return found, ["retained", schedule_season, found[0], found[1]]

    def _with_current_provenance(
        self, entry: Mapping[str, Any], found: Any, schedule: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        """The stored document with ``provenance`` and ``coverage`` rebuilt.

        Both report each stream's age and freshness as of the capture, which
        differs between two captures of one generation, so they come from this
        request's snapshot (and the retained read it names) and never from the
        stored copy.  The stored ``sources`` are the composed data's own.
        """

        document = entry["matchup"]
        provenance = document.get("provenance")
        metadata = getattr(self.snapshot, "metadata", None)
        if not isinstance(provenance, Mapping) or not callable(metadata):
            return document
        retained_reads = (
            [found[2]] if entry.get("retained_used") and found is not None and found[2] is not None else []
        )
        sources = provenance.get("sources")
        stamp = _schedule_stamp(schedule)
        if stamp is not None and isinstance(sources, Mapping):
            sources = {**sources, "schedule": stamp}
        rebuilt = build_publication_metadata(metadata(), self.snapshot, sources, retained_reads)
        document = {**document, **rebuilt}
        if stamp is not None:
            document = _with_schedule_stamp(document, stamp)
        stats = self._stats_freshness()
        if stats is not None and isinstance(document.get("freshness"), Mapping):
            document = {
                **document,
                "freshness": {**document["freshness"], "stats": dict(stats)},
            }
        return document

    def _compose(self, game_id: str) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {}
        if accepts_keyword(self.composer.get_matchup_from_snapshot, "compose_cache"):
            kwargs["compose_cache"] = self._compose_cache
        if self.season is not None and accepts_keyword(self.composer.get_matchup_from_snapshot, "season"):
            kwargs["season"] = self.season
        if self._read_scope is not None:
            kwargs["read_scope"] = self._read_scope
        return self.composer.get_matchup_from_snapshot(
            game_id=game_id,
            publication_snapshot=self.snapshot,
            injuries=self.injuries,
            **kwargs,
        )


def read_game_matchup(
    matchups: Any,
    game: Mapping[str, Any],
    pool: Mapping[str, Any] | None = None,
    schedule: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """One Slate game's Matchup from any ``MatchupReader``.

    A snapshot-bound reader is told the game so it can cache by the game's
    state and tip time; any other reader is asked by id alone.
    """

    if isinstance(matchups, SnapshotMatchups):
        return matchups.get_matchup(game_id=game["game_id"], game=game, pool=pool, schedule=schedule)
    return matchups.get_matchup(game_id=game["game_id"])


def request_matchups(
    *,
    composer: Any,
    publication_reader: Any | None,
    injuries: Any | None,
    settings: RuntimeSettings | None,
    snapshot_reuse: PublicationSnapshotReuse | None = None,
    cache: ComposedMatchupCache | None = None,
    cache_reads: bool = True,
    read_scope: tuple[Any, Any] | None = None,
) -> Any:
    """The one reader a request composing several Matchups uses, no provider read.

    Every Matchup shares one Publication snapshot (a reused one when
    ``snapshot_reuse`` holds the live generation) and stored-only injuries.
    Without a publication reader, settings, injuries or a snapshot composer the
    plain ``composer`` reads each game as before, live injuries included.
    ``read_scope`` is a caller's ``(connection, session)``: the capture and
    every game's reads then share its transaction.
    """

    if (
        publication_reader is None
        or settings is None
        or injuries is None
        or not callable(getattr(composer, "get_matchup_from_snapshot", None))
    ):
        return composer

    season, snapshot = capture_matchup_snapshot(
        settings, publication_reader, snapshot_reuse, session=None if read_scope is None else read_scope[1]
    )
    return SnapshotMatchups(
        composer,
        snapshot,
        injuries,
        season=season,
        cache=cache,
        settings=settings,
        cache_reads=cache_reads,
        read_scope=read_scope,
    )


def capture_matchup_snapshot(
    settings: RuntimeSettings,
    publication_reader: Any,
    snapshot_reuse: PublicationSnapshotReuse | None = None,
    *,
    session: Any = None,
) -> tuple[str, Any]:
    """``(season, snapshot)`` of the streams a Matchup reads, as a composed Matchup would capture them.

    A Matchup lists every trailing family in its snapshot whatever the game,
    so a Slate reads its ``trailing`` from the same capture its games' Matchups share.
    """

    def capture(season: Any) -> Any:
        return capture_publication_snapshot(
            publication_reader,
            MATCHUP_PUBLICATION_STREAM_KEYS,
            projection_only_keys=MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
            season=season,
            session=session,
        )

    if snapshot_reuse is None:
        return published_capture(settings, publication_reader, capture)
    return snapshot_reuse.capture(settings, publication_reader, MATCHUP_PUBLICATION_STREAM_KEYS, capture)


__all__ = [
    "SnapshotMatchupComposer",
    "SnapshotMatchups",
    "capture_matchup_snapshot",
    "capture_publication_snapshot",
    "read_game_matchup",
    "request_matchups",
]
