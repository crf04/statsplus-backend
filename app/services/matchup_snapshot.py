"""Compose one Matchup from a caller's already-captured Publication snapshot.

Both Target preview (#253) and Target resolution (#245) hold one Publication
generation for a whole request and need a ``MatchupReader`` --
``.get_matchup(game_id=...)`` -- bound to it, so a Matchup composed this way
never captures a second, independently advancing snapshot.  This is the one
place that binding lives; neither caller defines its own copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.services.matchup import MatchupComposeCache
from app.services.publication_snapshot_calls import accepts_keyword


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


__all__ = ["SnapshotMatchupComposer", "SnapshotMatchups"]
