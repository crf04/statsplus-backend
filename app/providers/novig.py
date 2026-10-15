"""Novig adapter for the shared NBA DFS snapshot contract.

Novig is a prediction exchange.  Its unauthenticated ``/v3/public`` reads give a
market list (one over/under market per player, statistic, game and strike) and
an event read (teams and start time).  The web app's batch book endpoint
(``api.novig.us/nbx/v1/markets/book/batch``) gives, for up to 150 markets per
call, each market's player identity and its resting orders per outcome.

Prices are derived from the bids as ``docs/ARCHITECTURE.md`` states: the book
lists resting *buys* per outcome, so the ask for a side is one minus the best
bid on the opposite side, and the selection's decimal price is ``1 / ask``.  A
market with no bid on one side is retained unpriced.  A market the batch
endpoint no longer serves (settled or removed) is excluded; a market whose
batch could not be read has no player identity and is a malformed record.

An exchange lists a ladder of strikes per player and statistic.  Exactly one
strike per (player, statistic, game) is the ``standard`` line: the one whose
Over midpoint ``(best Over bid + Over ask) / 2`` is nearest 0.5, ties to the
lower strike.  Every other strike is ``alternate``.  When no strike has a
midpoint a lone strike is standard and several are all alternate, and when a
market of the same game and statistic could not be read the group is
undetermined, so no strike is standard: the adapter does not guess.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    LeagueEvidence,
    MalformedProviderResponseError,
    MarketStatus,
    MarketThreshold,
    MarketVariant,
    NBAMarketQuery,
    PlayerProjectionMarket,
    ProviderSnapshot,
    RetrievalContext,
    Selection,
    SportEvidence,
    StatisticEvidence,
    TeamEvidence,
    _build_snapshot,
    _NormalizedBatch,
    _RecordCoverageAccumulator,
    _SnapshotMarketCollector,
    normalize_market_status,
)
from app.providers.dfs_normalization import (
    optional_text,
    required_identifier,
    required_number,
    required_text,
)
from app.providers.dfs_transport import TransportErrorPolicy, request_json
from app.utils.telemetry import PROVIDER_NOVIG, ProviderResponseError

logger = logging.getLogger(__name__)

_NOVIG_TRANSPORT_POLICY = TransportErrorPolicy(
    deadline_message="Novig retrieval deadline exceeded.",
    timeout_message="Novig timed out while fetching lines.",
    unavailable_message="Novig could not be reached.",
    invalid_json_message="Novig returned invalid JSON",
)

# Novig's market type is the statistic label; ``statistic_catalog.yaml`` maps
# each of these to a canonical statistic.  Anything else is excluded below.
PLAYER_STATISTIC_MARKET_TYPES = frozenset(
    {
        "POINTS",
        "REBOUNDS",
        "ASSISTS",
        "THREE_POINTERS_MADE",
        "STEALS",
        "BLOCKS",
        "TURNOVERS",
        "POINTS_REBOUNDS_ASSISTS",
        "POINTS_ASSISTS",
        "POINTS_REBOUNDS",
        "REBOUNDS_ASSISTS",
        "STEALS_BLOCKS",
    }
)
# Team and game lines carry no player.
_NON_PLAYER_MARKET_TYPES = frozenset(
    {
        "MONEY",
        "SPREAD",
        "TOTAL",
        "TEAM_TOTAL",
        "MONEY_1H",
        "SPREAD_1H",
        "TOTAL_1H",
        "1X2",
    }
)
# Season-long and series markets are not about one game.
_NON_GAME_MARKET_TYPES = frozenset(
    {
        "CHAMPIONSHIP_WINNER",
        "CHAMPIONSHIP_WINNING_DIVISION",
        "SERIES_WINNER",
        "TO_MAKE_THE_PLAYOFFS",
        "REGULAR_SEASON_WINS",
        "MVP_WINNER",
        "FINALS_MVP",
        "EASTERN_CONFERENCE_WINNER",
        "WESTERN_CONFERENCE_WINNER",
        "TO_MAKE_THE_FINALS",
        "TO_ADVANCE",
    }
)

_PRICE_PLACES = Decimal("0.000001")
_HALF = Decimal("0.5")
_ONE = Decimal(1)
_PREGAME_EVENT_STATUS = "OPEN_PREGAME"
_BATCH_BOOK_URL = "https://api.novig.us/nbx/v1/markets/book/batch"
# The batch URL carries every id, and 500 ids answer 414; 200 answer 200.
_BATCH_SIZE = 150


class _MalformedPayload(MalformedProviderResponseError):
    """The Novig payload cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One in-scope Novig statistic market from the catalog.

    The player is not here: only the batch book names it.
    """

    market_id: str
    event_id: str
    statistic_label: str
    strike: Decimal
    strike_text: str
    status: MarketStatus
    status_label: str
    over_id: str
    over_label: str
    under_id: str
    under_label: str


@dataclass(frozen=True, slots=True)
class _Event:
    evidence: EventEvidence
    league: str | None


@dataclass(frozen=True, slots=True)
class _Book:
    """One market's player (None for a non-player market) and best bids."""

    player_id: str | None
    player_name: str | None
    best_bids: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class _BadBook:
    """A batch entry that could not be read as a book; one market's problem."""

    detail: str


class _Dropped:
    """The batch endpoint no longer serves this market."""


_DROPPED = _Dropped()


@dataclass(frozen=True, slots=True)
class _Quote:
    """Over and Under asks, present only when both sides have a bid."""

    over_bid: Decimal
    over_ask: Decimal
    under_ask: Decimal

    @property
    def over_midpoint(self) -> Decimal:
        return (self.over_bid + self.over_ask) / 2


def _build_session(pool_size: int) -> requests.Session:
    session = requests.Session()
    session.mount(
        "https://",
        requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=pool_size),
    )
    return session


def _http_status(error: BaseException) -> int | None:
    cause = error.__cause__
    response = getattr(cause, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


class NovigAdapter:
    """Retrieve and normalize Novig's public NBA player over/under markets.

    An injected ``session`` serves bounded concurrent GETs and must tolerate
    that; a ``requests.Session`` does.
    """

    name = "novig"
    BASE_URL = "https://api.novig.com/v3/public"
    BATCH_BOOK_URL = _BATCH_BOOK_URL
    # Novig answers non-browser clients with 403 where these succeed.
    DEFAULT_HEADERS = {
        "Accept": "application/json",
        "Origin": "https://novig.com",
        "Referer": "https://novig.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
    }
    DEFAULT_TIMEOUT = (3.0, 8.0)
    PAGE_LIMIT = 200
    MAX_PAGES = 25
    BATCH_SIZE = _BATCH_SIZE
    MAX_DETAIL_CONCURRENCY = 4

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        detail_concurrency: int = MAX_DETAIL_CONCURRENCY,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if detail_concurrency < 1:
            raise ValueError("detail_concurrency must be at least 1")
        self.detail_concurrency = min(detail_concurrency, self.MAX_DETAIL_CONCURRENCY)
        self.session = session or _build_session(self.detail_concurrency + 1)
        self.timeout = timeout
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.session.headers.update(self.DEFAULT_HEADERS)

    # ------------------------------------------------------------------ flow

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        """Fetch one complete or partial Novig observation."""

        retrieved_at = self._now_utc()
        rows, pagination_complete, page_codes, page_details = self._fetch_catalog(
            context
        )

        records = _RecordCoverageAccumulator()
        outcomes: list[tuple[Any, _Candidate | Exception]] = []
        for row in rows:
            try:
                outcomes.append(
                    (row, self._classify(row, query.market_statuses))
                )
            except (CoverageRecordExcluded, CoverageRecordMalformed) as error:
                outcomes.append((row, error))
        candidates = [
            result for _, result in outcomes if isinstance(result, _Candidate)
        ]

        events, event_errors = self._fetch_events(context, candidates)
        usable = [
            candidate
            for candidate in candidates
            if self._event_problem(events.get(candidate.event_id)) is None
        ]
        books, unread = self._fetch_books(context, usable)
        variants = self._choose_variants(usable, books, unread)

        collection = _SnapshotMarketCollector()
        for row, result in outcomes:
            records.add(
                (row, result),
                lambda item: self._market(
                    item[1],
                    events=events,
                    books=books,
                    unread=unread,
                    variants=variants,
                ),
                on_success=collection.add,
            )

        fanout_codes: list[CoverageCode] = []
        fanout_details: list[str] = []
        if unread:
            fanout_codes.append(CoverageCode.FIXTURE_FAILED)
            fanout_details.append(
                f"novig book unavailable for {len(unread)} of {len(usable)} markets"
            )
        batch = _NormalizedBatch.from_accumulator(
            records,
            markets=collection.markets,
            warning_codes=(*page_codes, *fanout_codes, *collection.warning_codes),
        )
        if batch.malformed_count and not batch.markets:
            if event_errors and len(event_errors) == len(
                {candidate.event_id for candidate in candidates}
            ):
                raise next(iter(event_errors.values()))
            if unread and len(unread) == len(usable):
                raise next(iter(unread.values()))
            raise self._invalid_response("no usable Novig player markets")
        return _build_snapshot(
            provider=self.name,
            markets=batch.markets,
            retrieved_at=retrieved_at,
            fetched_count=batch.fetched_count,
            eligible_count=batch.eligible_count,
            normalized_count=batch.normalized_count,
            skipped_count=batch.skipped_count,
            pagination_complete=pagination_complete,
            fanout_complete=not batch.malformed_count and not unread,
            expected_total=None,
            warning_codes=batch.warning_codes,
            skipped_reasons=batch.skipped_reasons,
            diagnostic_details=(
                *batch.diagnostic_details,
                *page_details,
                *fanout_details,
            ),
        )

    # ------------------------------------------------------------- transport

    def _get(
        self,
        context: RetrievalContext,
        url: str,
        *,
        operation: str,
        params: Mapping[str, Any] | None,
        parse: Callable[[Any], Any],
    ) -> Any:
        try:
            return request_json(
                context=context,
                session=self.session,
                url=url,
                params=params,
                timeout=self.timeout,
                now=self._now_utc,
                provider=PROVIDER_NOVIG,
                operation=operation,
                parse=parse,
                error_policy=_NOVIG_TRANSPORT_POLICY,
            )
        except ProviderResponseError as error:
            raise _MalformedPayload(str(error)) from error

    def _fetch_catalog(
        self, context: RetrievalContext
    ) -> tuple[list[Any], bool, list[CoverageCode], list[str]]:
        rows: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        for page_number in range(self.MAX_PAGES):
            params: dict[str, Any] = {"league": "NBA", "limit": self.PAGE_LIMIT}
            if cursor is not None:
                params["after"] = cursor
            try:
                items, cursor = self._get(
                    context,
                    f"{self.BASE_URL}/catalog/markets",
                    operation="market_page",
                    params=params,
                    parse=self._parse_page,
                )
            except ProviderUnavailableError as error:
                if page_number == 0:
                    raise
                return (
                    rows,
                    False,
                    [CoverageCode.PAGE_FETCH_FAILED],
                    [f"novig market page {page_number + 1} unavailable: {error}"],
                )
            except _MalformedPayload as error:
                if page_number == 0:
                    raise self._invalid_response(error) from error
                return (
                    rows,
                    False,
                    [CoverageCode.PAGE_MALFORMED],
                    [f"novig market page {page_number + 1} malformed: {error}"],
                )
            rows.extend(items)
            if cursor is None:
                return rows, True, [], []
            if cursor in seen:
                return (
                    rows,
                    False,
                    [CoverageCode.PAGINATION_PAGE_MISMATCH],
                    ["novig market pagination repeated a cursor"],
                )
            seen.add(cursor)
        return (
            rows,
            False,
            [CoverageCode.PAGINATION_PAGE_MISMATCH],
            [f"novig market pagination exceeded {self.MAX_PAGES} pages"],
        )

    @staticmethod
    def _parse_page(payload: Any) -> tuple[list[Any], str | None]:
        if not isinstance(payload, Mapping):
            raise _MalformedPayload("market page must be an object")
        items = payload.get("items")
        if not isinstance(items, list):
            raise _MalformedPayload("market page items must be a list")
        cursor = payload.get("next")
        if cursor is None or cursor == "":
            return items, None
        if not isinstance(cursor, str):
            raise _MalformedPayload("market page next must be a string")
        return items, cursor

    def _fetch_events(
        self, context: RetrievalContext, candidates: list[_Candidate]
    ) -> tuple[dict[str, _Event], dict[str, ProviderUnavailableError]]:
        event_ids = list(dict.fromkeys(c.event_id for c in candidates))
        events: dict[str, _Event] = {}
        errors: dict[str, ProviderUnavailableError] = {}

        def fetch(event_id: str) -> _Event:
            return self._get(
                context,
                f"{self.BASE_URL}/catalog/events/{event_id}",
                operation="event_detail",
                params=None,
                parse=lambda payload: self._parse_event(payload, event_id),
            )

        for event_id, result in self._fan_out(event_ids, fetch):
            if isinstance(result, _Event):
                events[event_id] = result
            else:
                errors[event_id] = result
        return events, errors

    def _fetch_books(
        self, context: RetrievalContext, candidates: list[_Candidate]
    ) -> tuple[
        dict[str, _Book | _Dropped | _BadBook], dict[str, ProviderUnavailableError]
    ]:
        """Read every candidate's book in batches.

        A market the endpoint does not return, or answers 404 for alone, is
        ``_DROPPED``; a batch entry that cannot be read is a ``_BadBook`` for
        that market alone.  A market whose request failed any other way is
        ``unread``: it has no player identity, and its siblings' standard line
        is therefore unknowable.

        A batch that answers 404 holds a settled or removed market, so it is
        split in halves until the stale market stands alone.  The halves run in
        the same bounded pool as the first batches.
        """

        by_id = {candidate.market_id: candidate for candidate in candidates}
        ordered = sorted(
            candidates, key=lambda c: (c.event_id, c.statistic_label, c.strike)
        )
        batches = [
            [c.market_id for c in ordered[start : start + self.BATCH_SIZE]]
            for start in range(0, len(ordered), self.BATCH_SIZE)
        ]
        books: dict[str, _Book | _Dropped | _BadBook] = {}
        unread: dict[str, ProviderUnavailableError] = {}

        def attempt(ids: list[str]) -> tuple[bool, list[str], dict[str, Any]]:
            """Return (split?, ids, results by id)."""

            try:
                served = self._get(
                    context,
                    self.BATCH_BOOK_URL,
                    operation="market_book_batch",
                    params={"marketIds": ",".join(ids), "currency": "CASH"},
                    parse=self._parse_batch,
                )
                return False, ids, {i: served.get(i, _DROPPED) for i in ids}
            except ProviderUnavailableError as error:
                if _http_status(error) != 404:
                    return False, ids, {i: error for i in ids}
                if len(ids) == 1:
                    return False, ids, {ids[0]: _DROPPED}
                return True, ids, {}
            except _MalformedPayload as error:
                return False, ids, {i: self._invalid_response(error) for i in ids}

        results: dict[str, Any] = {}
        if batches:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.detail_concurrency
            ) as executor:
                pending = {executor.submit(attempt, ids) for ids in batches}
                while pending:
                    done, pending = concurrent.futures.wait(
                        pending, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    for future in done:
                        split, ids, result = future.result()
                        if split:
                            middle = len(ids) // 2
                            pending.add(executor.submit(attempt, ids[:middle]))
                            pending.add(executor.submit(attempt, ids[middle:]))
                        else:
                            results.update(result)

        for market_id, value in results.items():
            candidate = by_id[market_id]
            if isinstance(value, Exception):
                unread[market_id] = value
            elif isinstance(value, _Book) and not set(value.best_bids) <= {
                candidate.over_id,
                candidate.under_id,
            }:
                books[market_id] = _BadBook(
                    "book names an outcome the market does not have"
                )
            else:
                books[market_id] = value
        return books, unread

    def _fan_out(self, items: list[Any], fetch: Callable[[Any], Any]):
        """Run ``fetch`` over ``items`` with bounded workers, in item order.

        A failed fetch yields its :class:`ProviderUnavailableError` (a malformed
        payload is converted to one) instead of aborting its siblings.
        """

        if not items:
            return []

        def run(item: Any) -> Any:
            try:
                return fetch(item)
            except ProviderUnavailableError as error:
                return error
            except _MalformedPayload as error:
                return self._invalid_response(error)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.detail_concurrency, len(items))
        ) as executor:
            return list(zip(items, executor.map(run, items)))

    def _now_utc(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Novig clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    # --------------------------------------------------------------- parsing

    @staticmethod
    def _parse_event(payload: Any, event_id: str) -> _Event:
        if not isinstance(payload, Mapping):
            raise _MalformedPayload("event must be an object")
        if str(payload.get("eventId")) != event_id:
            raise _MalformedPayload("event id conflicts with its market")
        description = optional_text(payload.get("description"))
        if description is None or description.count(" @ ") != 1:
            raise _MalformedPayload("event description must be 'Away @ Home'")
        away, home = (part.strip() for part in description.split(" @ "))
        if not away or not home:
            raise _MalformedPayload("event description must name both teams")
        starts = payload.get("startsTs")
        if isinstance(starts, bool) or not isinstance(starts, int):
            raise _MalformedPayload("event startsTs must be epoch milliseconds")
        try:
            starts_at = datetime.fromtimestamp(starts / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise _MalformedPayload("event startsTs is out of range") from error
        status = optional_text(payload.get("status"))
        if status is None:
            raise _MalformedPayload("event status must be present")
        return _Event(
            evidence=EventEvidence(
                provider_id=event_id,
                label=description,
                status_label=status,
                starts_at=starts_at,
                home_team=TeamEvidence(name=home),
                away_team=TeamEvidence(name=away),
            ),
            league=optional_text(payload.get("league")),
        )

    @classmethod
    def _parse_batch(cls, payload: Any) -> dict[str, _Book | _BadBook]:
        """Read each entry on its own; only a non-list envelope is unusable.

        An entry that names no market cannot be attributed, so it is skipped
        and its market surfaces as not served.
        """

        if not isinstance(payload, list):
            raise _MalformedPayload("batch book must be a list")
        served: dict[str, _Book | _BadBook] = {}
        for entry in payload:
            market = entry.get("market") if isinstance(entry, Mapping) else None
            market_id = market.get("id") if isinstance(market, Mapping) else None
            if not isinstance(market_id, str) or not market_id:
                continue
            try:
                served[market_id] = cls._parse_entry(entry)
            except _MalformedPayload as error:
                served[market_id] = _BadBook(str(error))
        return served

    @staticmethod
    def _parse_entry(entry: Mapping[str, Any]) -> _Book:
        market = entry["market"]
        ladders = entry.get("ladders")
        if not isinstance(ladders, Mapping):
            raise _MalformedPayload("batch book entry needs ladders")
        player = market.get("player")
        player_id = player_name = None
        if player is not None:
            if not isinstance(player, Mapping):
                raise _MalformedPayload("batch book player must be an object")
            player_id = optional_text(player.get("id"))
            player_name = optional_text(player.get("fullName"))
            if player_id is None or player_name is None:
                raise _MalformedPayload("batch book player needs id and fullName")
        best: dict[str, Decimal] = {}
        for outcome_id, ladder in ladders.items():
            if not isinstance(ladder, Mapping) or not isinstance(
                ladder.get("bids"), list
            ):
                raise _MalformedPayload("batch book ladder needs a bids list")
            for bid in ladder["bids"]:
                if not isinstance(bid, Mapping):
                    raise _MalformedPayload("batch book bid must be an object")
                price, quantity = bid.get("price"), bid.get("qty")
                if isinstance(price, bool) or not isinstance(price, (int, float, str)):
                    raise _MalformedPayload("batch book price must be numeric")
                if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
                    raise _MalformedPayload("batch book qty must be numeric")
                try:
                    value = Decimal(str(price))
                except InvalidOperation as error:
                    raise _MalformedPayload("batch book price must be numeric") from error
                if not value.is_finite() or not 0 < value < 1:
                    raise _MalformedPayload("batch book price must be between 0 and 1")
                if quantity <= 0 or bid.get("status", "OPEN") != "OPEN":
                    continue
                if value > best.get(str(outcome_id), Decimal(0)):
                    best[str(outcome_id)] = value
        return _Book(player_id, player_name, best)

    # --------------------------------------------------------- normalization

    @classmethod
    def _classify(
        cls,
        row: Any,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _Candidate:
        if not isinstance(row, Mapping):
            raise CoverageRecordMalformed("market must be an object")
        market_id = required_identifier(row, "marketId")
        market_type = required_text(row, "marketType")
        if market_type not in PLAYER_STATISTIC_MARKET_TYPES:
            if market_type in _NON_PLAYER_MARKET_TYPES:
                raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)
            if market_type in _NON_GAME_MARKET_TYPES:
                raise CoverageRecordExcluded(CoverageCode.NON_GAME_MARKET)
            raise CoverageRecordExcluded(CoverageCode.NON_PROJECTION_MARKET)
        raw_status = required_text(row, "status")
        try:
            status = normalize_market_status(raw_status).value
        except ValueError as error:
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_STATUS) from error
        if status not in allowed_statuses:
            raise CoverageRecordExcluded(CoverageCode.STATUS_FILTER)

        required_number(row, "strike")
        strike_text = str(row["strike"])
        outcomes = row.get("outcomes")
        if not isinstance(outcomes, list):
            raise CoverageRecordMalformed("market outcomes must be a list")
        sides: dict[str, tuple[str, str]] = {}
        for outcome in outcomes:
            if not isinstance(outcome, Mapping):
                raise CoverageRecordMalformed("market outcome must be an object")
            outcome_id = required_identifier(outcome, "outcomeId")
            label = required_text(outcome, "name")
            direction = label.split(" ", 1)[0].casefold()
            if direction not in {"over", "under"} or direction in sides:
                raise CoverageRecordMalformed(
                    "market outcomes must be one Over and one Under"
                )
            sides[direction] = (outcome_id, label)
        if set(sides) != {"over", "under"}:
            raise CoverageRecordMalformed(
                "market outcomes must be one Over and one Under"
            )
        return _Candidate(
            market_id=market_id,
            event_id=required_identifier(row, "eventId"),
            statistic_label=market_type,
            strike=Decimal(strike_text),
            strike_text=strike_text,
            status=MarketStatus(status),
            status_label=raw_status,
            over_id=sides["over"][0],
            over_label=sides["over"][1],
            under_id=sides["under"][0],
            under_label=sides["under"][1],
        )

    @staticmethod
    def _event_problem(event: _Event | None) -> CoverageCode | None:
        """Why a market's event cannot carry it, or None when it can."""

        if event is None:
            return CoverageCode.FIXTURE_FAILED
        if event.league is not None and event.league.upper() != "NBA":
            return CoverageCode.NON_NBA_MARKET
        if event.evidence.status_label != _PREGAME_EVENT_STATUS:
            return CoverageCode.INELIGIBLE_EVENT_STATUS
        return None

    @staticmethod
    def _quote(candidate: _Candidate, book: _Book) -> _Quote | None:
        """Both asks, or None when either side has no bid to price against."""

        over_bid = book.best_bids.get(candidate.over_id)
        under_bid = book.best_bids.get(candidate.under_id)
        if over_bid is None or under_bid is None:
            return None
        return _Quote(
            over_bid=over_bid,
            over_ask=_ONE - under_bid,
            under_ask=_ONE - over_bid,
        )

    def _choose_variants(
        self,
        candidates: list[_Candidate],
        books: Mapping[str, _Book | _Dropped | _BadBook],
        unread: Mapping[str, ProviderUnavailableError],
    ) -> dict[str, MarketVariant]:
        """Pick the one standard strike per (player, statistic, game).

        Only markets whose player is known can be grouped.  A market that could
        not be read might belong to any group of its game and statistic, so
        those groups are undetermined.
        """

        undetermined = {
            (c.event_id, c.statistic_label)
            for c in candidates
            if c.market_id in unread or isinstance(books.get(c.market_id), _BadBook)
        }
        groups: dict[tuple[str, str, str], list[_Candidate]] = defaultdict(list)
        for candidate in candidates:
            book = books.get(candidate.market_id)
            if isinstance(book, _Book) and book.player_id is not None:
                groups[
                    (candidate.event_id, book.player_id, candidate.statistic_label)
                ].append(candidate)
        variants: dict[str, MarketVariant] = {}
        for (event_id, _player, label), members in groups.items():
            standard = (
                None
                if (event_id, label) in undetermined
                else self._standard_member(members, books)
            )
            for member in members:
                variants[member.market_id] = (
                    MarketVariant.STANDARD
                    if member is standard
                    else MarketVariant.ALTERNATE
                )
        return variants

    def _standard_member(
        self,
        members: list[_Candidate],
        books: Mapping[str, _Book | _Dropped | _BadBook],
    ) -> _Candidate | None:
        if len(members) == 1:
            return members[0]
        midpoints: dict[str, Decimal] = {}
        for member in members:
            quote = self._quote(member, books[member.market_id])  # type: ignore[arg-type]
            if quote is not None:
                midpoints[member.market_id] = quote.over_midpoint
        if not midpoints:
            return None
        return min(
            (member for member in members if member.market_id in midpoints),
            key=lambda m: (abs(midpoints[m.market_id] - _HALF), m.strike, m.market_id),
        )

    def _market(
        self,
        result: _Candidate | Exception,
        *,
        events: Mapping[str, _Event],
        books: Mapping[str, _Book | _Dropped | _BadBook],
        unread: Mapping[str, ProviderUnavailableError],
        variants: Mapping[str, MarketVariant],
    ) -> PlayerProjectionMarket:
        if isinstance(result, Exception):
            raise result
        candidate = result
        problem = self._event_problem(events.get(candidate.event_id))
        if problem is CoverageCode.FIXTURE_FAILED:
            raise CoverageRecordMalformed(
                "event detail unavailable", code=CoverageCode.FIXTURE_FAILED
            )
        if problem is not None:
            raise CoverageRecordExcluded(problem)
        event = events[candidate.event_id].evidence

        if candidate.market_id in unread:
            raise CoverageRecordMalformed(
                "market book unavailable", code=CoverageCode.FIXTURE_FAILED
            )
        book = books[candidate.market_id]
        if isinstance(book, _BadBook):
            raise CoverageRecordMalformed(book.detail)
        if isinstance(book, _Dropped):
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_STATUS)
        if book.player_id is None or book.player_name is None:
            raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)
        quote = self._quote(candidate, book)
        variant = variants[candidate.market_id]
        try:
            return PlayerProjectionMarket(
                provider=self.name,
                market_id=candidate.market_id,
                athlete=AthleteEvidence(
                    provider_id=book.player_id,
                    name=book.player_name,
                ),
                event=event,
                league=LeagueEvidence(label="NBA"),
                sport=SportEvidence(label="BASKETBALL"),
                statistic=StatisticEvidence(label=candidate.statistic_label),
                threshold=MarketThreshold(
                    candidate.strike,
                    unit="count",
                    original_value=candidate.strike_text,
                ),
                status=candidate.status,
                status_label=candidate.status_label,
                variant=variant,
                variant_label=variant.value,
                # Every Novig player market is a full-game market, which this
                # contract spells as an explicitly absent period label.
                scoring_period=None,
                starts_at=event.starts_at,
                selections=(
                    Selection(
                        selection_id=candidate.over_id,
                        label=candidate.over_label,
                        direction="over",
                        direction_label="Over",
                        decimal_price=None if quote is None else self._decimal(quote.over_ask),
                    ),
                    Selection(
                        selection_id=candidate.under_id,
                        label=candidate.under_label,
                        direction="under",
                        direction_label="Under",
                        decimal_price=None if quote is None else self._decimal(quote.under_ask),
                    ),
                ),
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error

    @staticmethod
    def _decimal(ask: Decimal) -> Decimal:
        return (_ONE / ask).quantize(_PRICE_PLACES)

    @staticmethod
    def _invalid_response(detail: Any) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            "Novig returned an invalid response.",
            detail=detail,
            provider_reason="malformed_response",
        )


__all__ = ["NovigAdapter", "PLAYER_STATISTIC_MARKET_TYPES"]
