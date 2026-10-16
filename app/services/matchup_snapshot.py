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
from typing import Any, Protocol

from app.config.settings import RuntimeSettings
from app.services.matchup import (
    MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
    MATCHUP_PUBLICATION_STREAM_KEYS,
    MatchupComposeCache,
)
from app.services.matchup_reuse import ComposedMatchupCache, PublicationSnapshotReuse
from app.services.publication_snapshot_calls import accepts_keyword
from app.services.research_season import published_capture


def capture_publication_snapshot(
    publication_reader: Any | None,
    stream_keys: Any,
    *,
    projection_only_keys: Any,
    decoded_only_keys: Any = frozenset(),
    season: str,
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
    return snapshot(stream_keys, season=season, **keyword)


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
    ) -> None:
        self.composer = composer
        self.snapshot = snapshot
        self.injuries = injuries
        # The season ``snapshot`` was captured for, so every Matchup composed
        # from it reads its evidence in that season and no other.
        self.season = season
        # Optional: the shared Redis cache of composed Matchups (#422).  It is
        # consulted only for a caller that names the Slate game, whose state
        # and tip time are part of the key.
        self._cache = cache
        self._settings = settings
        self._compose_cache = MatchupComposeCache()

    def get_matchup(
        self, *, game_id: str, game: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        if self._cache is None or game is None or self._settings is None:
            return self._compose(game_id)
        return self._cache.get_or_compose(
            self._cache.key(self._settings, self.snapshot, self.season, game),
            self._settings,
            lambda: self._compose(game_id),
        )

    def _compose(self, game_id: str) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {}
        if accepts_keyword(self.composer.get_matchup_from_snapshot, "compose_cache"):
            kwargs["compose_cache"] = self._compose_cache
        if self.season is not None and accepts_keyword(
            self.composer.get_matchup_from_snapshot, "season"
        ):
            kwargs["season"] = self.season
        return self.composer.get_matchup_from_snapshot(
            game_id=game_id,
            publication_snapshot=self.snapshot,
            injuries=self.injuries,
            **kwargs,
        )


def read_game_matchup(matchups: Any, game: Mapping[str, Any]) -> Mapping[str, Any]:
    """One Slate game's Matchup from any ``MatchupReader``.

    A snapshot-bound reader is told the game so it can cache by the game's
    state and tip time; any other reader is asked by id alone.
    """

    if isinstance(matchups, SnapshotMatchups):
        return matchups.get_matchup(game_id=game["game_id"], game=game)
    return matchups.get_matchup(game_id=game["game_id"])


def request_matchups(
    *,
    composer: Any,
    publication_reader: Any | None,
    injuries: Any | None,
    settings: RuntimeSettings | None,
    snapshot_reuse: PublicationSnapshotReuse | None = None,
    cache: ComposedMatchupCache | None = None,
) -> Any:
    """The one reader a request composing several Matchups uses, no provider read.

    Every Matchup shares one Publication snapshot (a reused one when
    ``snapshot_reuse`` holds the live generation) and stored-only injuries.
    Without a publication reader, settings, injuries or a snapshot composer the
    plain ``composer`` reads each game as before, live injuries included.
    """

    if (
        publication_reader is None
        or settings is None
        or injuries is None
        or not callable(getattr(composer, "get_matchup_from_snapshot", None))
    ):
        return composer

    def capture(season: Any) -> Any:
        return capture_publication_snapshot(
            publication_reader,
            MATCHUP_PUBLICATION_STREAM_KEYS,
            projection_only_keys=MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
            season=season,
        )

    if snapshot_reuse is None:
        season, snapshot = published_capture(settings, publication_reader, capture)
    else:
        season, snapshot = snapshot_reuse.capture(
            settings, publication_reader, MATCHUP_PUBLICATION_STREAM_KEYS, capture
        )
    return SnapshotMatchups(
        composer, snapshot, injuries, season=season, cache=cache, settings=settings
    )


__all__ = [
    "SnapshotMatchupComposer",
    "SnapshotMatchups",
    "read_game_matchup",
    "request_matchups",
    "capture_publication_snapshot",
]
