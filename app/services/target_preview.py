"""Preview a Draft Target: its season to date, and whether it fires today (#253).

The Lab evaluates a Target nobody has saved, and owes it exactly the numbers a
saved Target would get.  Both halves of that already exist as reads --
``TargetBacktestService.backtest_target`` for the season and
``TargetResolutionService.today`` for tonight -- so this service adds no
evaluator.  What it adds is the two promises a preview makes that neither read
makes on its own.

*One generation.*  Each read resolves its own Publication snapshot when run
alone, and a publication advancing between the two would pair season evidence
from one generation with tonight's fit count from another.  The preview
captures one snapshot over the union of both reads' streams and hands it to
both: the backtest takes it directly, and the Matchup that ``today`` filters
is composed from it through ``MatchupService.get_matchup_from_snapshot``.  The
projection-only narrowing is the intersection of the two reads' own, so a
stream is read payload-less only where both reads would.

*No provider, no write.*  The Matchup route may refresh injuries from the
provider and publish a snapshot when the stored override is stale; that is its
contract, and ``resolve`` (#245) inherits it.  A preview composes its Matchup
with ``StoredMatchupInjuryReader`` instead, which answers from what is stored
and never refreshes, so previewing at any rate adds no collection and writes
nothing.  The draft itself is stored nowhere: the caller validates it into the
listed shape before this service sees it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.config.settings import RuntimeSettings
from app.services.matchup import (
    MATCHUP_PROJECTION_ONLY_STREAM_KEYS,
    MATCHUP_PUBLICATION_STREAM_KEYS,
)
from app.services.matchup_snapshot import (
    SnapshotMatchupComposer,
    SnapshotMatchups,
    capture_publication_snapshot,
)
from app.services.target_backtest import (
    BACKTEST_PROJECTION_ONLY_STREAM_KEYS,
    BACKTEST_PUBLICATION_STREAM_KEYS,
)


#: Every stream either read composes, each once, in the backtest's order then
#: the Matchup's, so one capture serves both.
PREVIEW_PUBLICATION_STREAM_KEYS = tuple(
    dict.fromkeys(
        (*BACKTEST_PUBLICATION_STREAM_KEYS, *MATCHUP_PUBLICATION_STREAM_KEYS)
    )
)
#: Narrowed only where both reads resolve through the projection.
PREVIEW_PROJECTION_ONLY_STREAM_KEYS = (
    BACKTEST_PROJECTION_ONLY_STREAM_KEYS & MATCHUP_PROJECTION_ONLY_STREAM_KEYS
)


class BacktestReader(Protocol):
    def backtest_target(
        self, target: Mapping[str, Any], *, publication_snapshot: Any
    ) -> dict[str, Any]: ...


class TodayReader(Protocol):
    def today(
        self, target: Mapping[str, Any], *, matchups: Any
    ) -> dict[str, Any] | None: ...


class TargetPreviewService:
    """Evaluate one Draft Target from one generation, touching nothing."""

    def __init__(
        self,
        *,
        backtests: BacktestReader,
        resolutions: TodayReader,
        matchups: SnapshotMatchupComposer,
        injuries: Any | None,
        settings: RuntimeSettings,
        publication_reader: Any | None = None,
    ) -> None:
        self.backtests = backtests
        self.resolutions = resolutions
        self.matchups = matchups
        self.injuries = injuries
        self.settings = settings
        self.publication_reader = publication_reader

    def preview(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        """Return the draft's Backtest plus ``today``.

        ``draft`` is the validated listed shape ``UserService.validate_target_draft``
        returns; it is echoed as the response's ``target``.
        """

        snapshot = capture_publication_snapshot(
            self.publication_reader,
            PREVIEW_PUBLICATION_STREAM_KEYS,
            projection_only_keys=PREVIEW_PROJECTION_ONLY_STREAM_KEYS,
            season=self.settings.nba.current_season,
        )
        previewed = self.backtests.backtest_target(
            draft, publication_snapshot=snapshot
        )
        today = self.resolutions.today(
            draft,
            matchups=SnapshotMatchups(self.matchups, snapshot, self.injuries),
        )
        return {**previewed, "today": today}


__all__ = [
    "PREVIEW_PROJECTION_ONLY_STREAM_KEYS",
    "PREVIEW_PUBLICATION_STREAM_KEYS",
    "TargetPreviewService",
]
