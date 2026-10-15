"""Kalshi adapter for the shared NBA DFS snapshot contract.

Kalshi is a prediction market, not a DFS board.  Each NBA player prop is a
ladder of binary markets, one per ``N+`` threshold, whose Yes side is the Over
and whose No side is the Under.  The adapter reads the six full-game player
series and the open ``KXNBAGAME`` events (whose milestones carry the exact
start time), then collapses every ladder to one STANDARD line.

Venue facts this module relies on, each checked against live and historical
payloads:

* The event ticker is ``<SERIES>-<YYMONDD><AWAY><HOME>``: ``26JUN13NYKSAS`` is
  New York at San Antonio, San Antonio at home.  Kalshi's own event title reads
  "New York at San Antonio" and ESPN lists that game with San Antonio at home.
* A player market ticker is ``<event>-<TEAM><PLAYERKEY>-<N>``, so its third
  segment starts with the player's team code.
* ``occurrence_datetime`` is an estimate (a fixed point hours after the game),
  not the tip-off.  The game's milestone ``start_date`` is the tip-off, so it is
  used when the game's milestone is open, and the estimate only otherwise.
* The market object states ``yes_bid_dollars``, ``yes_ask_dollars``,
  ``no_bid_dollars`` and ``no_ask_dollars`` as 0-1 dollar strings.  A resting
  price is strictly between 0 and 1; ``0.0000`` for a bid and ``1.0000`` for an
  ask are how an empty book side is stated.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    CoverageCode,
    CoverageRecordExcluded,
    CoverageRecordMalformed,
    EventEvidence,
    MalformedProviderResponseError,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    Selection,
    StatisticEvidence,
    TeamEvidence,
    _NormalizedBatch,
    _RecordCoverageAccumulator,
    _SnapshotMarketCollector,
    _build_snapshot,
    normalize_market_status,
)
from app.providers.dfs_normalization import (
    display_number,
    is_ineligible_event_status,
    optional_text,
    required_number,
    required_text,
    validate_timestamp,
)
from app.providers.dfs_transport import TransportErrorPolicy, request_json
from app.utils.telemetry import PROVIDER_KALSHI, ProviderResponseError

logger = logging.getLogger(__name__)

_KALSHI_TRANSPORT_POLICY = TransportErrorPolicy(
    deadline_message="Kalshi retrieval deadline exceeded.",
    timeout_message="Kalshi timed out while fetching markets.",
    unavailable_message="Kalshi could not be reached.",
    invalid_json_message="Kalshi returned invalid JSON",
)

#: The full-game player series in scope and the statistic label each states.
#: The label is the one Kalshi's own event titles use ("Three Pointers"), so
#: ``statistic_catalog.yaml`` maps the same words.  A series outside this map
#: is excluded, never guessed at.
SERIES_STATISTIC_LABELS: Mapping[str, str] = {
    "KXNBAPTS": "Points",
    "KXNBAREB": "Rebounds",
    "KXNBAAST": "Assists",
    "KXNBA3PT": "Three Pointers",
    "KXNBABLK": "Blocks",
    "KXNBASTL": "Steals",
}
GAME_SERIES = "KXNBAGAME"

_EVENT_TICKER = re.compile(
    r"^(?P<series>[A-Z0-9]+)-(?P<game>\d{2}[A-Z]{3}\d{2}(?P<away>[A-Z]{3})(?P<home>[A-Z]{3}))$"
)
_MAX_PAGES = 50
_ONE = Decimal(1)
_HALF = Decimal("0.5")


class _MalformedPayload(MalformedProviderResponseError):
    """The Kalshi payload cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class _Game:
    """One open game's tip-off and team identities from its milestone."""

    starts_at: str
    title: str | None
    status_label: str | None
    away_team_id: str | None
    home_team_id: str | None


@dataclass(frozen=True, slots=True)
class _Page:
    """One parsed page: its normalized rows and the next cursor, if any."""

    batch: _NormalizedBatch
    #: One Over midpoint (``None`` when the row has none) per batch market, in
    #: the batch's order.
    midpoints: tuple[Decimal | None, ...]
    cursor: str | None


@dataclass(slots=True)
class _Collected:
    markets: _SnapshotMarketCollector = field(default_factory=_SnapshotMarketCollector)
    coverage: _RecordCoverageAccumulator = field(
        default_factory=_RecordCoverageAccumulator
    )
    midpoints: dict[str | None, Decimal | None] = field(default_factory=dict)
    conflicting: set[str | None] = field(default_factory=set)
    failures: list[ProviderUnavailableError] = field(default_factory=list)
    requests_made: int = 0


class KalshiAdapter:
    """Retrieve and normalize Kalshi's open NBA player-prop ladders."""

    name = "kalshi"
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    DEFAULT_TIMEOUT = (3.0, 8.0)

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "StatsPlus/1.0",
            }
        )

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        """Fetch one complete or partial Kalshi observation."""

        retrieved_at = self._now_utc()
        collected = _Collected()
        games: Mapping[str, _Game] = {}
        games_failed = False
        try:
            games = self._open_games(context)
        except ProviderUnavailableError as error:
            if self._is_deadline_error(error):
                raise
            games_failed = True
            logger.warning("Kalshi open-game lookup failed: %s", error)

        pages_failed = False
        for series in SERIES_STATISTIC_LABELS:
            if not self._collect_series(
                collected,
                context,
                series=series,
                games=games,
                allowed_statuses=query.market_statuses,
            ):
                pages_failed = True

        if not collected.requests_made:
            raise collected.failures[-1]
        markets = self._with_standard_lines(
            tuple(
                market
                for market in collected.markets.markets
                if market.market_id not in collected.conflicting
            ),
            {k: v for k, v in collected.midpoints.items() if v is not None},
        )
        coverage = collected.coverage
        if not markets:
            if coverage.malformed_count:
                raise self._invalid_response("no usable Kalshi player markets")
            if pages_failed:
                raise collected.failures[-1]

        warning_codes: list[CoverageCode] = [
            *coverage.warning_values(),
            *collected.markets.warning_codes,
        ]
        skipped_reasons = list(coverage.skipped_values())
        fanout_complete = not coverage.malformed_count
        pagination_complete = not pages_failed
        if pages_failed:
            warning_codes.append(CoverageCode.PAGE_FETCH_FAILED)
        if games_failed and markets:
            # Without the milestones the start times are Kalshi's estimates.
            warning_codes.append(CoverageCode.FIXTURE_LIST_FAILED)
            fanout_complete = False
        return _build_snapshot(
            provider=self.name,
            markets=markets,
            retrieved_at=retrieved_at,
            fetched_count=coverage.fetched_count,
            eligible_count=coverage.eligible_count,
            normalized_count=coverage.normalized_count,
            skipped_count=coverage.skipped_count,
            pagination_complete=pagination_complete,
            fanout_complete=fanout_complete,
            expected_total=None,
            warning_codes=warning_codes,
            skipped_reasons=skipped_reasons,
            diagnostic_details=coverage.diagnostic_values(),
        )

    def _collect_series(
        self,
        collected: _Collected,
        context: RetrievalContext,
        *,
        series: str,
        games: Mapping[str, _Game],
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> bool:
        """Read every open page of one series; ``False`` when a page failed."""

        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "series_ticker": series,
                "status": "open",
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                page = self._request(
                    context,
                    "/markets",
                    params,
                    operation="series_markets",
                    parse=lambda payload: self._parse_markets_page(
                        payload,
                        games=games,
                        allowed_statuses=allowed_statuses,
                    ),
                )
            except ProviderUnavailableError as error:
                if self._is_deadline_error(error):
                    raise
                collected.failures.append(error)
                logger.warning("Kalshi %s page failed: %s", series, error)
                return False
            except _MalformedPayload as error:
                collected.failures.append(self._invalid_response(error))
                logger.warning("Kalshi %s page was malformed: %s", series, error)
                return False
            collected.requests_made += 1
            page_midpoints = iter(page.midpoints)
            collected.coverage.absorb(
                page.batch,
                on_success=lambda market: self._accept(
                    collected, market, next(page_midpoints)
                ),
            )
            cursor = page.cursor
            if not cursor:
                return True
            if cursor in seen_cursors:
                collected.failures.append(
                    self._invalid_response("pagination cursor repeated")
                )
                return False
            seen_cursors.add(cursor)
        collected.failures.append(self._invalid_response("pagination did not end"))
        return False

    @staticmethod
    def _accept(
        collected: _Collected, market: PlayerProjectionMarket, midpoint: Decimal | None
    ) -> None:
        """Add one market, refusing a repeat whose midpoint contradicts the first.

        The Over midpoint chooses the standard line but is not part of the
        market content the collector compares, so a repeated ticker with a
        different bid would otherwise be a silent duplicate and the chosen line
        would depend on row order.  A contradiction is a conflicting source
        identity, excluded wherever it appears, exactly as the collector does.
        """

        key = market.market_id
        if key in collected.midpoints and collected.midpoints[key] != midpoint:
            collected.conflicting.add(key)
            raise CoverageRecordMalformed(
                "repeated provider market identity has conflicting book evidence",
                code=CoverageCode.CONFLICTING_SOURCE_IDENTITY,
            )
        collected.midpoints.setdefault(key, midpoint)
        collected.markets.add(market)

    def _open_games(self, context: RetrievalContext) -> Mapping[str, _Game]:
        """Index every open NBA game's milestone by game key."""

        games: dict[str, _Game] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "series_ticker": GAME_SERIES,
                "status": "open",
                "with_milestones": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                page_games, cursor = self._request(
                    context,
                    "/events",
                    params,
                    operation="open_games",
                    parse=self._parse_games_page,
                )
            except _MalformedPayload as error:
                raise self._invalid_response(error) from error
            games.update(page_games)
            if not cursor:
                return games
            if cursor in seen_cursors:
                raise self._invalid_response("pagination cursor repeated")
            seen_cursors.add(cursor)
        raise self._invalid_response("pagination did not end")

    def _request(
        self,
        context: RetrievalContext,
        path: str,
        params: Mapping[str, Any],
        *,
        operation: str,
        parse: Callable[[Any], Any],
    ) -> Any:
        try:
            return request_json(
                context=context,
                session=self.session,
                url=f"{self.BASE_URL}{path}",
                params=params,
                timeout=self.timeout,
                now=self._now_utc,
                provider=PROVIDER_KALSHI,
                operation=operation,
                parse=parse,
                error_policy=_KALSHI_TRANSPORT_POLICY,
            )
        except ProviderResponseError as error:
            raise _MalformedPayload(str(error)) from error

    def _now_utc(self) -> datetime:
        value = self.now()
        if not isinstance(value, datetime):
            raise ValueError("Kalshi clock must return an aware datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Kalshi clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _is_deadline_error(error: ProviderUnavailableError) -> bool:
        return "deadline" in str(error).casefold()

    @staticmethod
    def _invalid_response(detail: Any) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            "Kalshi returned an invalid response.",
            detail=detail,
            provider_reason="malformed_response",
        )

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _cursor(payload: Mapping[str, Any]) -> str | None:
        cursor = payload.get("cursor")
        if cursor is None or cursor == "":
            return None
        if not isinstance(cursor, str):
            raise _MalformedPayload("cursor must be a string")
        return cursor

    @classmethod
    def _parse_games_page(cls, payload: Any) -> tuple[dict[str, _Game], str | None]:
        if not isinstance(payload, Mapping):
            raise _MalformedPayload("payload must be an object")
        milestones = payload.get("milestones")
        if not isinstance(milestones, list):
            raise _MalformedPayload("milestones must be a list")
        games: dict[str, _Game] = {}
        for milestone in milestones:
            parsed = cls._game_from_milestone(milestone)
            if parsed is not None:
                games[parsed[0]] = parsed[1]
        return games, cls._cursor(payload)

    @staticmethod
    def _game_from_milestone(milestone: Any) -> tuple[str, _Game] | None:
        """One game from its milestone, or ``None`` when it cannot be trusted.

        A milestone that cannot be read only costs its game the exact start
        time, so it is skipped rather than failing the whole lookup.
        """

        if not isinstance(milestone, Mapping):
            return None
        primary = milestone.get("primary_event_tickers")
        if not isinstance(primary, list):
            return None
        game_ticker = next(
            (
                ticker
                for ticker in primary
                if isinstance(ticker, str) and ticker.startswith(f"{GAME_SERIES}-")
            ),
            None,
        )
        match = _EVENT_TICKER.match(game_ticker) if game_ticker else None
        if match is None:
            return None
        starts_at = milestone.get("start_date")
        try:
            validate_timestamp(starts_at, "start_date")
        except CoverageRecordMalformed:
            return None
        if not isinstance(starts_at, str):
            return None
        details = milestone.get("details")
        details = details if isinstance(details, Mapping) else {}
        return match.group("game"), _Game(
            starts_at=starts_at,
            title=optional_text(milestone.get("title")),
            status_label=optional_text(details.get("status")),
            away_team_id=optional_text(details.get("away_team_id")),
            home_team_id=optional_text(details.get("home_team_id")),
        )

    @classmethod
    def _parse_markets_page(
        cls,
        payload: Any,
        *,
        games: Mapping[str, _Game],
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _Page:
        if not isinstance(payload, Mapping):
            raise _MalformedPayload("payload must be an object")
        rows = payload.get("markets")
        if not isinstance(rows, list):
            raise _MalformedPayload("markets must be a list")
        midpoints: list[Decimal | None] = []
        records = _RecordCoverageAccumulator()
        # Repeats are judged once, across pages, in get_snapshot's collector.
        records.extend(
            rows,
            lambda row: cls._normalize_market(
                row,
                games=games,
                allowed_statuses=allowed_statuses,
                midpoints=midpoints,
            ),
        )
        return _Page(
            batch=_NormalizedBatch.from_accumulator(records),
            midpoints=tuple(midpoints),
            cursor=cls._cursor(payload),
        )

    @classmethod
    def _normalize_market(
        cls,
        row: Any,
        *,
        games: Mapping[str, _Game],
        allowed_statuses: tuple[MarketStatus | str, ...],
        midpoints: list[Decimal | None],
    ) -> PlayerProjectionMarket:
        if not isinstance(row, Mapping):
            raise CoverageRecordMalformed("market must be an object")
        ticker = required_text(row, "ticker")
        event_ticker = required_text(row, "event_ticker")
        # The series decides scope before the ticker's shape is read: head to
        # head and season-long series have other shapes and are out of scope,
        # not malformed.
        statistic_label = SERIES_STATISTIC_LABELS.get(event_ticker.partition("-")[0])
        if statistic_label is None:
            raise CoverageRecordExcluded(CoverageCode.NON_PROJECTION_MARKET)
        event_match = _EVENT_TICKER.match(event_ticker)
        if event_match is None:
            raise CoverageRecordMalformed("event_ticker is not a Kalshi game event")
        if row.get("market_type") != "binary" or row.get("strike_type") != "structured":
            raise CoverageRecordExcluded(CoverageCode.NON_PROJECTION_MARKET)
        custom_strike = row.get("custom_strike")
        player_id = (
            optional_text(custom_strike.get("basketball_player"))
            if isinstance(custom_strike, Mapping)
            else None
        )
        if player_id is None:
            raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)

        raw_status = required_text(row, "status")
        status = cls._market_status(raw_status)
        if status.value not in allowed_statuses:
            raise CoverageRecordExcluded(CoverageCode.STATUS_FILTER)

        game_key = event_match.group("game")
        game = games.get(game_key)
        if (
            game is not None
            and game.status_label is not None
            and (
                is_ineligible_event_status(game.status_label)
                or game.status_label.casefold() == "complete"
            )
        ):
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_EVENT_STATUS)

        line = required_number(row, "floor_strike")
        title = required_text(row, "title")
        player_name = optional_text(title.partition(":")[0])
        if player_name is None or ":" not in title:
            raise CoverageRecordMalformed("title must name the player before ':'")
        updated_at = row.get("updated_time")
        validate_timestamp(updated_at, "updated_time")

        away = event_match.group("away")
        home = event_match.group("home")
        team_code = cls._player_team_code(ticker, event_ticker)
        team = opponent = None
        if team_code == away:
            team, opponent = cls._team(away, game, "away"), cls._team(home, game, "home")
        elif team_code == home:
            team, opponent = cls._team(home, game, "home"), cls._team(away, game, "away")

        starts_at = game.starts_at if game is not None else row.get("occurrence_datetime")
        validate_timestamp(starts_at, "occurrence_datetime")
        event = EventEvidence(
            provider_id=f"{GAME_SERIES}-{game_key}",
            label=(game.title if game is not None else None) or f"{away} at {home}",
            status_label=game.status_label if game is not None else None,
            starts_at=starts_at,
            home_team=cls._team(home, game, "home"),
            away_team=cls._team(away, game, "away"),
        )
        selections, midpoint = cls._selections(row, ticker)
        try:
            market = PlayerProjectionMarket(
                provider=cls.name,
                market_id=ticker,
                athlete=AthleteEvidence(
                    provider_id=player_id,
                    name=player_name,
                    team=team,
                ),
                event=event,
                team=team,
                opponent=opponent,
                statistic=StatisticEvidence(label=statistic_label),
                threshold=MarketThreshold(
                    line,
                    unit="count",
                    original_value=display_number(row["floor_strike"], field="floor_strike"),
                ),
                status=status.value,
                status_label=raw_status,
                # Every rung starts as ALTERNATE; the ladder's standard line is
                # chosen once all of its rungs are known.
                variant=MarketVariant.ALTERNATE,
                variant_label="alternate",
                # Kalshi's player series are full-game props and state no
                # scoring period, which is an absent label, not an unknown one.
                scoring_period=None,
                starts_at=starts_at,
                updated_at=updated_at,
                selections=selections,
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error
        midpoints.append(midpoint)
        return market

    @staticmethod
    def _market_status(raw_status: str) -> MarketStatus:
        """Map Kalshi's lifecycle states onto the eligible vocabulary.

        ``active`` is open for trading and ``inactive`` is trading paused by
        the exchange.  ``initialized`` has not opened, and ``closed``,
        ``determined``, ``disputed``, ``amended`` and ``finalized`` are over.
        """

        word = {"inactive": "paused"}.get(raw_status.strip().casefold(), raw_status)
        try:
            return normalize_market_status(word).value
        except ValueError as error:
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_STATUS) from error

    @staticmethod
    def _player_team_code(ticker: str, event_ticker: str) -> str | None:
        prefix = f"{event_ticker}-"
        if not ticker.startswith(prefix):
            return None
        player_key = ticker[len(prefix):].partition("-")[0]
        code = player_key[:3]
        return code if len(code) == 3 and code.isalpha() else None

    @staticmethod
    def _team(code: str, game: _Game | None, side: str) -> TeamEvidence:
        provider_id = None
        if game is not None:
            provider_id = game.away_team_id if side == "away" else game.home_team_id
        return TeamEvidence(provider_id=provider_id, abbreviation=code)

    @classmethod
    def _selections(
        cls, row: Mapping[str, Any], ticker: str
    ) -> tuple[tuple[Selection, ...], Decimal | None]:
        """The Over (Yes) and Under (No) sides, and the Over's midpoint.

        Each side is priced at ``1 / ask``.  When either ask is absent both
        sides are unpriced, because the priced sides of one market must share a
        price kind and scope.  The midpoint needs a bid and an ask on the Over.
        """

        yes_bid = cls._dollars(row, "yes_bid_dollars")
        yes_ask = cls._dollars(row, "yes_ask_dollars")
        no_ask = cls._dollars(row, "no_ask_dollars")
        yes_price = no_price = None
        if yes_ask is not None and no_ask is not None:
            yes_price, no_price = _ONE / yes_ask, _ONE / no_ask
        sides = (
            Selection(
                selection_id=f"{ticker}:yes",
                label="Over",
                direction="Over",
                direction_label="Over",
                decimal_price=yes_price,
            ),
            Selection(
                selection_id=f"{ticker}:no",
                label="Under",
                direction="Under",
                direction_label="Under",
                decimal_price=no_price,
            ),
        )
        midpoint = None
        if yes_bid is not None and yes_ask is not None:
            midpoint = (yes_bid + yes_ask) / 2
        return sides, midpoint

    @staticmethod
    def _dollars(row: Mapping[str, Any], key: str) -> Decimal | None:
        """A resting price, or ``None`` when that side of the book is empty."""

        raw = row.get(key)
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise CoverageRecordMalformed(f"{key} must be a dollar price")
        try:
            value = Decimal(str(raw))
        except InvalidOperation as error:
            raise CoverageRecordMalformed(f"{key} must be a dollar price") from error
        if not value.is_finite() or value < 0 or value > 1:
            raise CoverageRecordMalformed(f"{key} must be between 0 and 1")
        return value if 0 < value < 1 else None

    # -- standard line -----------------------------------------------------

    @staticmethod
    def _with_standard_lines(
        markets: tuple[PlayerProjectionMarket, ...],
        midpoints: Mapping[str, Decimal],
    ) -> tuple[PlayerProjectionMarket, ...]:
        """Mark the one line per (game, statistic, player) that is STANDARD.

        The standard line is the one whose Over midpoint is nearest 0.5, the
        lower line on a tie.  When no line has a midpoint, a lone line is
        standard and several are all alternates, because choosing among them
        would be a guess.
        """

        ladders: dict[tuple[Any, ...], list[PlayerProjectionMarket]] = defaultdict(list)
        for market in markets:
            ladders[
                (
                    market.event.provider_id,
                    market.statistic.label,
                    market.athlete.provider_id,
                )
            ].append(market)
        standard: set[str | None] = set()
        for ladder in ladders.values():
            priced = [m for m in ladder if m.market_id in midpoints]
            if priced:
                chosen = min(
                    priced,
                    key=lambda m: (abs(midpoints[m.market_id] - _HALF), m.threshold.value),
                )
            elif len(ladder) == 1:
                chosen = ladder[0]
            else:
                continue
            standard.add(chosen.market_id)
        return tuple(
            replace(market, variant=MarketVariant.STANDARD, variant_label="standard")
            if market.market_id in standard
            else market
            for market in markets
        )


__all__ = ["KalshiAdapter", "SERIES_STATISTIC_LABELS"]
