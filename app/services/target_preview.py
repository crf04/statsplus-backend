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

*One season.*  A season other than the published one (requested, or the
2025-26 default) reads the Backtest from that season's retained Generation
alone.  ``today`` is about tonight's players, which such a season says
nothing about, and pairing it with a second capture would break the
one-generation promise, so it is ``null`` (#104); so is a published season
any of whose streams had to be read retained.

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
from app.services.backtest_season import BacktestSeason, BacktestSeasons
from app.services.response_provenance import provenance_block
from app.services.target_conditions import require_defender_fielded
from app.services.target_backtest import (
    BACKTEST_DECODED_ONLY_STREAM_KEYS,
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
#: The Diet decode cache may serve the preview's capture: neither read touches
#: the Diet payloads' rendered form, only their decoded facts.
PREVIEW_DECODED_ONLY_STREAM_KEYS = BACKTEST_DECODED_ONLY_STREAM_KEYS & frozenset(
    PREVIEW_PUBLICATION_STREAM_KEYS
)


class BacktestReader(Protocol):
    seasons: BacktestSeasons
    player_logs: Any

    def backtest_target(
        self,
        target: Mapping[str, Any],
        *,
        publication_snapshot: Any,
        season: BacktestSeason,
    ) -> dict[str, Any]: ...


class TodayReader(Protocol):
    def today_with_sources(
        self, target: Mapping[str, Any], *, matchups: Any
    ) -> tuple[
        dict[str, Any] | None,
        Mapping[str, Mapping[str, Any]],
        list[Mapping[str, Any]],
    ]: ...


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

    def preview(
        self, draft: Mapping[str, Any], *, season: str | None = None
    ) -> dict[str, Any]:
        """Return the draft's Backtest plus ``today``.

        ``draft`` is the validated listed shape ``UserService.validate_target_draft``
        returns; it is echoed as the response's ``target``.  ``season`` is the
        requested season; ``None`` reads the default season.
        """

        seasons = self.backtests.seasons
        # One capture decides the season, and every read uses both.
        choice, snapshot = seasons.capture(
            season,
            self._capture,
            BACKTEST_PUBLICATION_STREAM_KEYS,
            projection_only_keys=BACKTEST_PROJECTION_ONLY_STREAM_KEYS,
            decoded_only_keys=BACKTEST_DECODED_ONLY_STREAM_KEYS,
        )
        defender = (draft.get("conditions") or {}).get("defender")
        if defender:
            # The draft's defender was validated before this capture; a
            # season activating in between must not leave it checked in a
            # different season from the Backtest that reads its minutes.
            require_defender_fielded(
                self.backtests.player_logs,
                defender,
                draft["opponent"],
                choice.season,
                publication_snapshot=snapshot,
            )
        previewed = self.backtests.backtest_target(
            draft, publication_snapshot=snapshot, season=choice
        )
        if choice.past or getattr(snapshot, "retained_history", ()):
            # A season that is not the published one says nothing about
            # tonight's players, and a retained read is not the Generation
            # tonight's Matchup is in.  No Slate is read, so no source is.
            return {
                **previewed,
                "today": None,
                "provenance": provenance_block(
                    snapshot, streams=BACKTEST_PUBLICATION_STREAM_KEYS
                ),
            }
        today, sources, matchup_generation = self.resolutions.today_with_sources(
            draft,
            matchups=SnapshotMatchups(
                self.matchups, snapshot, self.injuries, season=choice.season
            ),
        )
        return {
            **previewed,
            "today": today,
            # The one union capture, narrowed to the Backtest's own streams
            # when the opponent is idle and no Matchup read the rest, plus any
            # retained Publication the composed Matchup's participants read.
            "provenance": provenance_block(
                snapshot,
                sources,
                streams=None if today is not None else BACKTEST_PUBLICATION_STREAM_KEYS,
                also=matchup_generation,
            ),
        }

    def _capture(self, season: Any) -> Any:
        return capture_publication_snapshot(
            self.publication_reader,
            PREVIEW_PUBLICATION_STREAM_KEYS,
            projection_only_keys=PREVIEW_PROJECTION_ONLY_STREAM_KEYS,
            decoded_only_keys=PREVIEW_DECODED_ONLY_STREAM_KEYS,
            season=season,
        )


__all__ = [
    "PREVIEW_PROJECTION_ONLY_STREAM_KEYS",
    "PREVIEW_PUBLICATION_STREAM_KEYS",
    "PREVIEW_DECODED_ONLY_STREAM_KEYS",
    "TargetPreviewService",
]
