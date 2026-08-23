"""A recorded fourth DFS provider, written only against the shared contract.

Nothing in ``app`` knows this provider exists.  It is admitted by one
registration and is then configurable, constructible, collectable, and subject
to the same compliance suite as every shipped adapter -- which is the whole
claim: onboarding a provider is a registration plus recorded adapter evidence,
never a change to the archive, the Player Pool, or the closing sets.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    CoverageCode,
    CoverageRecordMalformed,
    EventEvidence,
    MarketThreshold,
    NBAMarketQuery,
    PlayerProjectionMarket,
    PriceKind,
    PriceScope,
    ProviderSnapshot,
    RetrievalContext,
    Selection,
    StatisticEvidence,
    TeamEvidence,
    _build_snapshot,
    _RecordCoverageAccumulator,
    _NormalizedBatch,
)
from app.providers.registry import DFSProviderRegistration


FIXTURES = Path(__file__).parents[1] / "fixtures" / "fourth"
FOURTH_PROVIDER_NAME = "fourth"


def recorded_board() -> dict[str, Any]:
    """The recorded board payload this provider is admitted on."""

    return json.loads((FIXTURES / "board.json").read_text(encoding="utf-8"))


class FourthAdapter:
    """Normalize one recorded board into the shared snapshot contract."""

    name = FOURTH_PROVIDER_NAME

    def __init__(
        self,
        *,
        payload: Any = None,
        fail_with: Exception | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.payload = recorded_board() if payload is None else payload
        self.fail_with = fail_with
        self.now = now or (lambda: datetime(2026, 8, 9, 20, tzinfo=timezone.utc))

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        context.ensure_active()
        if self.fail_with is not None:
            raise self.fail_with
        if not isinstance(self.payload, Mapping):
            raise ProviderUnavailableError("Fourth returned an unusable board.")
        rows = self.payload.get("board")
        if not isinstance(rows, list):
            raise ProviderUnavailableError("Fourth returned an unusable board.")
        records = _RecordCoverageAccumulator()
        records.extend(rows, self._normalize)
        batch = _NormalizedBatch.from_accumulator(records)
        return _build_snapshot(
            provider=self.name,
            markets=batch.markets,
            retrieved_at=self.now(),
            fetched_count=batch.fetched_count,
            eligible_count=batch.eligible_count,
            normalized_count=batch.normalized_count,
            skipped_count=batch.skipped_count,
            pagination_complete=True,
            fanout_complete=batch.skipped_count == 0,
            warning_codes=batch.warning_codes,
            skipped_reasons=batch.skipped_reasons,
            diagnostic_details=batch.diagnostic_details,
        )

    def _normalize(self, row: Any) -> PlayerProjectionMarket:
        if not isinstance(row, Mapping):
            raise CoverageRecordMalformed("board row must be an object")
        player = row.get("player")
        game = row.get("game")
        if not isinstance(player, Mapping) or not isinstance(game, Mapping):
            raise CoverageRecordMalformed(
                "board row requires player and game evidence",
                code=CoverageCode.MALFORMED_RECORD,
            )
        sides = row.get("sides")
        if not isinstance(sides, list):
            raise CoverageRecordMalformed("board row sides must be a list")
        team = TeamEvidence(abbreviation=player.get("team"))
        try:
            return PlayerProjectionMarket(
                provider=self.name,
                market_id=row.get("id"),
                athlete=AthleteEvidence(
                    provider_id=player.get("id"),
                    name=player.get("name"),
                    team=team,
                ),
                event=EventEvidence(
                    provider_id=game.get("id"),
                    label=game.get("label"),
                    starts_at=game.get("starts_at"),
                ),
                team=team,
                statistic=StatisticEvidence(label=row.get("stat")),
                threshold=MarketThreshold(row.get("line"), unit="count"),
                status=row.get("status", "open"),
                variant=row.get("variant"),
                variant_label=row.get("variant"),
                scoring_period=row.get("period"),
                starts_at=game.get("starts_at"),
                selections=tuple(self._selection(side) for side in sides),
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error

    @staticmethod
    def _selection(side: Any) -> Selection:
        if not isinstance(side, Mapping):
            raise CoverageRecordMalformed("board side must be an object")
        multiplier = side.get("entry_multiplier")
        if multiplier is not None:
            return Selection(
                selection_id=side.get("id"),
                direction=side.get("direction"),
                price_kind=PriceKind.MULTIPLIER,
                price_value=multiplier,
                price_scope=PriceScope.ENTRY,
            )
        return Selection(
            selection_id=side.get("id"),
            direction=side.get("direction"),
            american_price=side.get("american_price"),
        )


def fourth_registration(
    build: Callable[[Any], Any] | None = None,
) -> DFSProviderRegistration:
    """The one registration that admits this provider to the application."""

    return DFSProviderRegistration(
        name=FOURTH_PROVIDER_NAME,
        build=build or (lambda runtime: FourthAdapter()),
    )
