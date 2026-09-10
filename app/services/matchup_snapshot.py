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

from app.services.matchup import MatchupComposeCache
from app.services.publication_snapshot_calls import accepts_keyword


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
        self, composer: SnapshotMatchupComposer, snapshot: Any, injuries: Any
    ) -> None:
        self.composer = composer
        self.snapshot = snapshot
        self.injuries = injuries
        self._compose_cache = MatchupComposeCache()

    def get_matchup(self, *, game_id: str) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {}
        if accepts_keyword(self.composer.get_matchup_from_snapshot, "compose_cache"):
            kwargs["compose_cache"] = self._compose_cache
        return self.composer.get_matchup_from_snapshot(
            game_id=game_id,
            publication_snapshot=self.snapshot,
            injuries=self.injuries,
            **kwargs,
        )


__all__ = [
    "SnapshotMatchupComposer",
    "SnapshotMatchups",
    "capture_publication_snapshot",
]
