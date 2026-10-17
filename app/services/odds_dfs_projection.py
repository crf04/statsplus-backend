"""PrizePicks lines from The Odds API's ``us_dfs`` region, recorded as provider
``prizepicks`` in the DFS projection archive (crf04/statsplus#215).

The odds beat owns the credits and the cadence, so it records what it fetched
itself instead of waiting for the DFS collector to ask an adapter: one paid
response for one game becomes one *Partial* snapshot of provider ``prizepicks``.
A Partial snapshot confirms only the markets it names and leaves every other
game's lines alone, which is exactly what a per-game poll knows.  The archive,
the Latest Player Projections, the Player Pool and the ``lines`` /
``provenance`` the Matchup and Selection serve are the ones every direct DFS
provider already feeds, so nothing downstream names the Odds API.

A Partial snapshot cannot retire a line by omitting it, so a line the game had
and the new response no longer carries is named again as a *suspended* market:
the archive then removes it from Latest Player Projections (and from the closing
set), instead of letting it linger until it ages out.

The payload carries a player's name and nothing else about him, so identity is
the roster-scoped resolution the sportsbook feed already uses
(``odds_identity``): a name that is not exactly one athlete on the game's two
rosters produces no market.  The market is then built with the canonical
athlete, team and game already set, as ``apply_mapping_outcomes`` would have
set them for a direct provider.

PrizePicks states no price in the response that means anything ("default odds"
that are "indicative only"), and the price a pick'em leg takes part in is the
entry payout PrizePicks publishes outside any API.  Both sides are therefore
priced from the registry's reviewed payout table, exactly as the direct
adapter does, never from the response.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
import logging
from typing import Any

from app.dfs_catalog import DFS_PRIZEPICKS
from app.domain.nba_events import is_started_event
from app.providers.dfs import (
    AthleteEvidence,
    EventEvidence,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    PriceKind,
    PriceScope,
    ProviderSnapshot,
    Selection,
    SelectionDirection,
    StatisticEvidence,
    TeamEvidence,
    _build_snapshot,
)
from app.providers.odds_api import MARKET_CATEGORIES, BookQuote, team_tricode
from app.providers.registry import EntryPayoutTable, dfs_provider_registration
from app.services.odds_identity import MappedEvent, ResolvedAthlete, athlete_provider_id
from app.services.projection_archive import LatestOffering, ProjectionRecordingService
from app.services.statistic_catalog import StatisticResolver

logger = logging.getLogger(__name__)

PROVIDER = DFS_PRIZEPICKS

#: PrizePicks' own statistic labels, which the Statistic Catalog already maps
#: for provider ``prizepicks`` (``statistic_catalog.yaml``).  The key is the
#: Odds API market; only the five standard markets are ever requested.
STATISTIC_LABELS: Mapping[str, str] = {
    "player_points": "Points",
    "player_rebounds": "Rebounds",
    "player_assists": "Assists",
    "player_threes": "3PM",
    "player_points_rebounds_assists": "PRA",
}

#: The same labels by Matchup category, to name a line already in the archive.
LABEL_BY_CATEGORY: Mapping[str, str] = {
    MARKET_CATEGORIES[key]: label for key, label in STATISTIC_LABELS.items()
}

#: PrizePicks' only standard odds type; demons and goblins are ``_alternate``
#: markets the request never asks for.
STANDARD_VARIANT = "standard"

#: How far back the beat looks for started games whose closing set is not
#: frozen yet.  The DFS collector freezes on every beat; this beat runs hourly
#: for a game, so a day of margin covers a long outage.
CLOSING_LOOKBACK = timedelta(hours=24)


def _team_evidence(team_id: int, name: str | None = None) -> TeamEvidence:
    return TeamEvidence(
        canonical_id=team_id,
        name=name,
        abbreviation=None if name is None else team_tricode(name),
    )


def build_snapshot(
    event: MappedEvent,
    quotes: Iterable[BookQuote],
    athletes: Mapping[str, ResolvedAthlete],
    *,
    retrieved_at: datetime,
    statistics: StatisticResolver,
    payout: EntryPayoutTable | None,
    withdrawn: Iterable[LatestOffering] = (),
) -> ProviderSnapshot | None:
    """One game's PrizePicks quotes as a Partial ``prizepicks`` snapshot.

    ``withdrawn`` are offerings the game still has in the archive that this
    response no longer carries (whatever the reason a carried line's player
    did not resolve, it is not withdrawn); they are named again as suspended
    markets so the archive retires them.  ``None`` when there is nothing to confirm or
    retire: a Partial snapshot names at least one market.  Quotes of any other
    book are never turned into markets.
    """

    home_id, away_id = event.team_ids
    home = _team_evidence(home_id, event.event.home_team)
    away = _team_evidence(away_id, event.event.away_team)
    odds_event = event.event
    event_evidence = EventEvidence(
        provider_id=odds_event.event_id,
        canonical_id=event.game_id,
        label=f"{odds_event.away_team} @ {odds_event.home_team}",
        starts_at=odds_event.commence_time,
        home_team=home,
        away_team=away,
    )
    multiplier = None if payout is None else payout.reference_multiplier
    price_kind = PriceKind.UNPRICED if multiplier is None else PriceKind.MULTIPLIER

    def market(
        *,
        market_id: str,
        provider_id: str,
        athlete: ResolvedAthlete,
        label: str,
        line: float,
        status: MarketStatus,
        updated_at: datetime | None,
    ) -> PlayerProjectionMarket:
        opponent_id = away_id if athlete.team_id == home_id else home_id
        team = TeamEvidence(canonical_id=athlete.team_id)
        built = PlayerProjectionMarket(
            provider=PROVIDER,
            market_id=market_id,
            athlete=AthleteEvidence(
                provider_id=provider_id,
                canonical_id=athlete.player_id,
                name=athlete.display_name,
                team=team,
            ),
            event=event_evidence,
            team=team,
            opponent=TeamEvidence(canonical_id=opponent_id),
            statistic=StatisticEvidence(label=label),
            threshold=MarketThreshold(line, unit="count", original_value=f"{line:g}"),
            status=status,
            variant=MarketVariant.STANDARD,
            variant_label=STANDARD_VARIANT,
            # The response states no scoring period on a standard full-game
            # prop: an absent label, which resolves to FULL_GAME.
            scoring_period=None,
            starts_at=odds_event.commence_time,
            updated_at=updated_at,
            selections=tuple(
                Selection(
                    selection_id=f"{market_id}:{direction.value}",
                    direction=direction,
                    price_kind=price_kind,
                    price_value=multiplier,
                    price_scope=PriceScope.ENTRY,
                )
                for direction in (SelectionDirection.HIGHER, SelectionDirection.LOWER)
            ),
        )
        return replace(built, statistic_match=statistics.resolve_market(built))

    markets: list[PlayerProjectionMarket] = []
    #: Every PrizePicks line the response carries, whether or not its player
    #: resolved this time: an unresolved line is not confirmed, but it is not
    #: withdrawn either, so what the archive already has for it is left alone.
    posted: set[str] = set()
    fetched = 0
    for quote in quotes:
        if quote.book != PROVIDER:
            continue
        fetched += 1
        label = STATISTIC_LABELS.get(quote.market_key)
        if label is None:
            continue
        provider_id = athlete_provider_id(quote.player_name)
        market_id = f"{odds_event.event_id}:{quote.market_key}:{provider_id}"
        posted.add(market_id)
        athlete = athletes.get(quote.player_name)
        if athlete is None:
            continue
        markets.append(
            market(
                market_id=market_id,
                provider_id=provider_id,
                athlete=athlete,
                label=label,
                line=quote.line,
                status=MarketStatus.AVAILABLE,
                updated_at=quote.last_update,
            )
        )
    live_count = len(markets)
    for offering in withdrawn:
        label = LABEL_BY_CATEGORY.get(offering.market_category)
        if (
            offering.market_id is None
            or offering.market_id in posted
            or label is None
            or offering.line_value is None
        ):
            continue
        markets.append(
            market(
                market_id=offering.market_id,
                provider_id=offering.athlete_provider_id or athlete_provider_id(offering.player_name),
                athlete=ResolvedAthlete(offering.canonical_player_id, offering.team_id, offering.player_name),
                label=label,
                line=offering.line_value,
                status=MarketStatus.SUSPENDED,
                updated_at=None,
            )
        )
    if not markets:
        return None
    # Partial by construction: this snapshot knows one game of the slate.
    return _build_snapshot(
        provider=PROVIDER,
        markets=markets,
        retrieved_at=retrieved_at,
        fetched_count=fetched,
        eligible_count=fetched,
        normalized_count=live_count,
        skipped_count=fetched - live_count,
        pagination_complete=True,
        fanout_complete=False,
    )


class PrizePicksProjectionWriter:
    """Record one game's PrizePicks quotes, and close started games."""

    def __init__(
        self,
        recorder: ProjectionRecordingService,
        *,
        query: NBAMarketQuery,
        statistics: StatisticResolver,
        payout: EntryPayoutTable | None = None,
    ) -> None:
        self.recorder = recorder
        self.query = query
        self.statistics = statistics
        self.payout = (
            payout
            if payout is not None
            else dfs_provider_registration(PROVIDER).payout_table(STANDARD_VARIANT)
        )

    def record(
        self,
        event: MappedEvent,
        quotes: Sequence[BookQuote],
        athletes: Mapping[str, ResolvedAthlete],
        *,
        retrieved_at: datetime,
        accepted_at: datetime,
        poll_started_at: datetime,
        fence: Callable[[Any], None] | None = None,
    ) -> int:
        """Archive the game's markets; return how many lines are now posted.

        Lines the game had and this response no longer carries are retired;
        ``fence`` runs first inside the archive's transaction (see
        ``ProjectionArchive.ingest_snapshot``).  Re-recording the same
        response is a replay in the archive, so a retry after a crash is safe.
        """

        snapshot = build_snapshot(
            event,
            quotes,
            athletes,
            retrieved_at=retrieved_at,
            statistics=self.statistics,
            payout=self.payout,
            withdrawn=self.recorder.latest_offerings(
                provider=PROVIDER, query=self.query, canonical_game_id=event.game_id
            ),
        )
        if snapshot is None:
            return 0
        self.recorder.record_snapshot(
            snapshot,
            query=self.query,
            accepted_at=accepted_at,
            poll_started_at=poll_started_at,
            fence=fence,
        )
        return snapshot.coverage.normalized_count

    def freeze_started(self, events: Iterable[Mapping[str, Any]], *, created_at: datetime) -> int:
        """Freeze ``prizepicks`` closing sets for games that have started.

        Idempotent: a game already closed is left alone.  Returns how many
        sets this call created.
        """

        started = tuple(event for event in events if is_started_event(event))
        if not started:
            return 0
        results = self.recorder.freeze_closing_projection_sets(
            events=started, query=self.query, created_at=created_at
        )
        return sum(1 for result in results if result.created)
