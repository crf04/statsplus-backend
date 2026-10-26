"""Polymarket adapter for the shared NBA DFS snapshot contract.

Polymarket is a prediction market, not a DFS board.  Its Gamma API lists one
event per NBA game; a player prop is one Yes/No market inside that event
("Victor Wembanyama: Points O/U 27.5"), where Yes is the Over.  The adapter
reads the unauthenticated ``/events`` listing and normalizes:

* the ``line`` as the threshold, Yes as Over and No as Under;
* the Over price as ``1 / bestAsk`` and the Under price as
  ``1 / (1 - bestBid)`` -- the 0-1 probability price to buy each side -- both
  decimal prices at selection scope.  A market whose Over ask or Under ask is
  missing prices neither side, because the contract requires the priced sides
  of one market to share a price kind and scope;
* one STANDARD line per (player, statistic, game): the line whose Over
  midpoint ``(bestBid + bestAsk) / 2`` is nearest 0.5, ties to the lower line.
  Only a book at most 10 cents wide quotes a midpoint: an empty book
  (0.01 / 0.97) sits near 0.5 by construction and would otherwise beat the
  line people trade.  Only a midpoint within 0.15 of 0.5 can be standard: a
  tight book on a far rung (29.5 priced at 0.15 while the apps post 21.5) says
  where the market is not.  Every other line is ALTERNATE.  When no line has a
  midpoint, a single line missing its bid or ask is STANDARD; a wider book, a
  line priced far from even, or several lines, are all ALTERNATE, never
  guessed.

Only full-game player points, rebounds and assists are in scope.  Game markets
(moneyline, spreads, totals), period-scoped markets, futures events and
unmapped stats are excluded with a typed coverage code.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from app.errors import ProviderUnavailableError
from app.providers.dfs import (
    AthleteEvidence,
    CoverageCode,
    CoverageRecordExcluded,
    CoverageRecordMalformed,
    DeadlineExceededError,
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
    _build_snapshot,
    _NormalizedBatch,
    _RecordCoverageAccumulator,
    _SnapshotMarketCollector,
    normalize_market_status,
)
from app.providers.dfs_normalization import (
    display_number,
    optional_text,
    required_identifier,
    required_number,
    validate_timestamp,
)
from app.providers.dfs_transport import TransportErrorPolicy, request_json
from app.utils.telemetry import PROVIDER_POLYMARKET, ProviderResponseError

logger = logging.getLogger(__name__)

_POLYMARKET_TRANSPORT_POLICY = TransportErrorPolicy(
    deadline_message="Polymarket retrieval deadline exceeded.",
    timeout_message="Polymarket timed out while fetching lines.",
    unavailable_message="Polymarket could not be reached.",
    invalid_json_message="Polymarket returned invalid JSON",
)

# ``sportsMarketType`` values that are full-game player props this adapter
# reads.  The value is the statistic label the catalog maps.
_PROP_TYPES = frozenset({"points", "rebounds", "assists"})
# Team and game markets inside a game event.
_GAME_MARKET_TYPES = frozenset(
    {
        "moneyline",
        "spreads",
        "totals",
        "team_totals",
        "basketball_team_to_score_first",
        "basketball_odd_even",
        "firsts",
    }
)
# first_half_totals, second_half_spreads, q1_moneyline, h2_team_totals, ...
_PERIOD_TYPE = re.compile(r"^(first_half|second_half|1h|2h|h[1-4]|q[1-4])(_|$)")
_SLUG_TEAMS = re.compile(r"^nba-([a-z]{2,4})-([a-z]{2,4})-\d{4}-\d{2}-\d{2}")
_HALF = Decimal("0.5")
# The widest bid-ask spread whose midpoint still says where the market is.
_MAX_QUOTED_SPREAD = Decimal("0.10")
# The furthest a standard line's Over midpoint may sit from an even 0.5.
_MAX_STANDARD_DISTANCE = Decimal("0.15")
_ONE = Decimal(1)
# Decimal prices are published to six places.
_PRICE_STEP = Decimal("0.000001")


class _MalformedPayload(MalformedProviderResponseError):
    """The Polymarket payload cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class _Game:
    event_id: str
    label: str | None
    starts_at: str | None
    home: TeamEvidence
    away: TeamEvidence


@dataclass(frozen=True, slots=True)
class _Prop:
    """One normalized prop before its standard-line decision."""

    market: PlayerProjectionMarket
    group: tuple[str, str, str]
    line: Decimal
    midpoint: Decimal | None
    # Both sides quoted, but too far apart to say where the market is.
    wide: bool


class PolymarketAdapter:
    """Retrieve and normalize Polymarket's public NBA player-prop markets."""

    name = "polymarket"
    BASE_URL = "https://gamma-api.polymarket.com/events"
    PAGE_SIZE = 100
    # The open NBA listing is a few hundred events at most; the cap only bounds
    # a listing that never ends.
    MAX_PAGES = 10
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
        self.now = now or (lambda: datetime.now(UTC))
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
        """Fetch one complete or partial Polymarket observation."""

        retrieved_at = self._now_utc()
        events: list[Any] = []
        pagination_complete = True
        warning_codes: list[CoverageCode] = []
        diagnostic_details: list[str] = []
        offset = 0
        for page in range(self.MAX_PAGES):
            try:
                rows = self._request_page(context, offset=offset)
            except ProviderUnavailableError as error:
                if not events or self._is_deadline_error(error):
                    raise
                pagination_complete = False
                warning_codes.append(CoverageCode.PAGE_FETCH_FAILED)
                logger.warning("Polymarket page %s failed after usable data: %s", page, error)
                break
            except _MalformedPayload as error:
                if not events:
                    raise self._invalid_response(error) from error
                pagination_complete = False
                warning_codes.extend((CoverageCode.PAGE_MALFORMED, CoverageCode.PAGE_FETCH_FAILED))
                logger.warning("Polymarket page %s was malformed after usable data", page)
                break
            events.extend(rows)
            if len(rows) < self.PAGE_SIZE:
                break
            offset += self.PAGE_SIZE
        else:
            pagination_complete = False
            warning_codes.append(CoverageCode.PAGE_FETCH_FAILED)
            diagnostic_details.append("event listing exceeded the page cap")

        batch = self._normalize_events(events, allowed_statuses=query.market_statuses)
        if batch.malformed_count and not batch.markets:
            raise self._invalid_response("no usable Polymarket player markets")
        return _build_snapshot(
            provider=self.name,
            markets=batch.markets,
            retrieved_at=retrieved_at,
            fetched_count=batch.fetched_count,
            eligible_count=batch.eligible_count,
            normalized_count=batch.normalized_count,
            skipped_count=batch.skipped_count,
            pagination_complete=pagination_complete,
            fanout_complete=not batch.malformed_count,
            expected_total=None,
            warning_codes=[*warning_codes, *batch.warning_codes],
            skipped_reasons=batch.skipped_reasons,
            diagnostic_details=[*diagnostic_details, *batch.diagnostic_details],
        )

    def _request_page(self, context: RetrievalContext, *, offset: int) -> list[Any]:
        try:
            return request_json(
                context=context,
                session=self.session,
                url=self.BASE_URL,
                params={
                    "tag_slug": "nba",
                    "closed": "false",
                    "limit": self.PAGE_SIZE,
                    "offset": offset,
                },
                timeout=self.timeout,
                now=self._now_utc,
                provider=PROVIDER_POLYMARKET,
                operation="get_snapshot",
                parse=self._parse_page,
                error_policy=_POLYMARKET_TRANSPORT_POLICY,
            )
        except ProviderResponseError as error:
            raise _MalformedPayload(str(error)) from error

    @staticmethod
    def _parse_page(payload: Any) -> list[Any]:
        if not isinstance(payload, list):
            raise _MalformedPayload("events payload must be a list")
        return payload

    def _now_utc(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Polymarket clock must return an aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _is_deadline_error(error: ProviderUnavailableError) -> bool:
        cause = error.__cause__
        while cause is not None:
            if isinstance(cause, DeadlineExceededError):
                return True
            cause = cause.__cause__
        return False

    # -- normalization -------------------------------------------------------

    @classmethod
    def _normalize_events(
        cls,
        events: list[Any],
        *,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _NormalizedBatch:
        # One record per market of a game event; a non-game event (futures,
        # awards) is one record, so a 110-outcome award does not inflate the
        # skipped count.  A listing that shifts between pages can repeat an
        # event, which is read once.
        items: list[tuple[Any, Any]] = []
        seen_events: set[str] = set()
        for event in events:
            event_id = event.get("id") if isinstance(event, Mapping) else None
            if event_id is not None:
                if str(event_id) in seen_events:
                    continue
                seen_events.add(str(event_id))
            markets = event.get("markets") if isinstance(event, Mapping) else None
            if isinstance(markets, list) and cls._is_game_event(event):
                items.extend((event, market) for market in markets)
            else:
                items.append((event, None))

        games: dict[int, _Game] = {}
        parsed: dict[int, _Prop] = {}

        def game_of(event: Any) -> _Game:
            game = games.get(id(event))
            if game is None:
                game = cls._parse_game(event)
                games[id(event)] = game
            return game

        def prop_of(event: Any, market: Any) -> _Prop:
            prop = parsed.get(id(market))
            if prop is None:
                prop = cls._parse_prop(game_of(event), market, allowed_statuses)
                parsed[id(market)] = prop
            return prop

        # First pass: the usable props decide each group's standard line.
        groups: dict[tuple[str, str, str], list[_Prop]] = {}
        for event, market in items:
            if market is None:
                continue
            try:
                prop = prop_of(event, market)
            except (CoverageRecordExcluded, CoverageRecordMalformed):
                continue
            groups.setdefault(prop.group, []).append(prop)
        standard_ids = {
            chosen.market.market_id
            for props in groups.values()
            if (chosen := cls._standard_line(props)) is not None
        }

        def normalize(item: tuple[Any, Any]) -> PlayerProjectionMarket:
            event, market = item
            game_of(event)
            if market is None:
                raise CoverageRecordMalformed("event markets must be a list")
            prop = prop_of(event, market)
            variant = (
                MarketVariant.STANDARD if prop.market.market_id in standard_ids else MarketVariant.ALTERNATE
            )
            return replace(
                prop.market,
                variant=variant.value,
                variant_label=variant.value,
            )

        collection = _SnapshotMarketCollector()
        records = _RecordCoverageAccumulator()
        records.extend(items, normalize, on_success=collection.add)
        return _NormalizedBatch.from_accumulator(
            records,
            markets=collection.markets,
            warning_codes=collection.warning_codes,
        )

    @staticmethod
    def _standard_line(props: list[_Prop]) -> _Prop | None:
        """Pick the group's STANDARD line (D3), or None when none is chosen."""

        quoted = [prop for prop in props if prop.midpoint is not None]
        even = [prop for prop in quoted if abs(prop.midpoint - _HALF) <= _MAX_STANDARD_DISTANCE]
        if even:
            return min(
                even,
                key=lambda prop: (
                    abs(prop.midpoint - _HALF),
                    prop.line,
                    prop.market.market_id,
                ),
            )
        if not quoted and len(props) == 1 and not props[0].wide:
            return props[0]
        return None

    @staticmethod
    def _is_game_event(event: Any) -> bool:
        if not isinstance(event, Mapping):
            return False
        return event.get("gameId") is not None or (
            isinstance(event.get("teams"), list) and len(event["teams"]) == 2
        )

    @classmethod
    def _parse_game(cls, event: Any) -> _Game:
        if not isinstance(event, Mapping):
            raise CoverageRecordMalformed("event must be an object")
        if not cls._is_game_event(event):
            raise CoverageRecordExcluded(CoverageCode.NON_GAME_MARKET)
        event_id = required_identifier(event, "id")
        if event.get("closed") is True or event.get("ended") is True or event.get("live") is True:
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_EVENT_STATUS)
        home, away = cls._event_teams(event)
        starts_at = optional_text(event.get("startTime"))
        validate_timestamp(starts_at, "startTime")
        return _Game(
            event_id=event_id,
            label=optional_text(event.get("title")),
            starts_at=starts_at,
            home=home,
            away=away,
        )

    @staticmethod
    def _event_teams(event: Mapping[str, Any]) -> tuple[TeamEvidence, TeamEvidence]:
        """The (home, away) teams: the event's ordered teams, else its slug."""

        teams = event.get("teams")
        if isinstance(teams, list) and len(teams) == 2:
            by_order: dict[str, TeamEvidence] = {}
            for team in teams:
                if not isinstance(team, Mapping):
                    raise CoverageRecordMalformed("event team must be an object")
                league = optional_text(team.get("league"))
                if league is not None and league.casefold() != "nba":
                    raise CoverageRecordExcluded(CoverageCode.NON_NBA_MARKET)
                ordering = optional_text(team.get("ordering"))
                if ordering is None or ordering.casefold() not in {"home", "away"}:
                    break
                try:
                    by_order[ordering.casefold()] = TeamEvidence(
                        provider_id=(None if team.get("id") is None else str(team["id"])),
                        name=optional_text(team.get("name")),
                        abbreviation=optional_text(team.get("abbreviation")),
                    )
                except ValueError as error:
                    raise CoverageRecordMalformed(str(error)) from error
            if set(by_order) == {"home", "away"}:
                return by_order["home"], by_order["away"]
        slug = optional_text(event.get("slug")) or ""
        match = _SLUG_TEAMS.match(slug)
        if match is None:
            raise CoverageRecordMalformed(
                "missing_event_relationship",
                code=CoverageCode.MISSING_EVENT_RELATIONSHIP,
            )
        return (
            TeamEvidence(abbreviation=match.group(2)),
            TeamEvidence(abbreviation=match.group(1)),
        )

    @classmethod
    def _parse_prop(
        cls,
        game: _Game,
        market: Any,
        allowed_statuses: tuple[MarketStatus | str, ...],
    ) -> _Prop:
        if not isinstance(market, Mapping):
            raise CoverageRecordMalformed("market must be an object")
        market_type = optional_text(market.get("sportsMarketType"))
        if market_type is None:
            raise CoverageRecordExcluded(CoverageCode.NON_PROJECTION_MARKET)
        market_type = market_type.casefold()
        if market_type not in _PROP_TYPES:
            if _PERIOD_TYPE.match(market_type):
                raise CoverageRecordExcluded(CoverageCode.NON_GAME_MARKET)
            if market_type in _GAME_MARKET_TYPES:
                raise CoverageRecordExcluded(CoverageCode.NON_PLAYER_MARKET)
            raise CoverageRecordExcluded(CoverageCode.NON_PROJECTION_MARKET)

        market_id = required_identifier(market, "id")
        if market.get("closed") is True or market.get("active") is False:
            raise CoverageRecordExcluded(CoverageCode.INELIGIBLE_STATUS)
        raw_status = "suspended" if market.get("acceptingOrders") is False else "available"
        status = normalize_market_status(raw_status).value
        if status not in allowed_statuses:
            raise CoverageRecordExcluded(CoverageCode.STATUS_FILTER)

        player_name = cls._player_name(market)
        line_raw = required_number(market, "line")
        outcomes = cls._outcomes(market.get("outcomes"))
        if outcomes != ("yes", "no"):
            raise CoverageRecordMalformed("market outcomes must be Yes and No")
        bid = cls._probability(market, "bestBid")
        ask = cls._probability(market, "bestAsk")
        over_ask = ask if ask is not None and ask > 0 else None
        under_ask = _ONE - bid if bid is not None and bid < 1 else None
        priced = over_ask is not None and under_ask is not None
        starts_at = optional_text(market.get("gameStartTime")) or game.starts_at
        validate_timestamp(starts_at, "gameStartTime")

        try:
            over_price = (_ONE / over_ask).quantize(_PRICE_STEP) if priced else None
            under_price = (_ONE / under_ask).quantize(_PRICE_STEP) if priced else None
        except ArithmeticError as error:
            raise CoverageRecordMalformed("price is outside the supported numeric range") from error

        athlete_id = cls._athlete_key(player_name)
        if not athlete_id:
            raise CoverageRecordMalformed("player name must contain letters or digits")
        try:
            threshold = MarketThreshold(
                line_raw,
                unit="count",
                original_value=display_number(line_raw, field="line"),
            )
            built = PlayerProjectionMarket(
                provider=cls.name,
                market_id=market_id,
                athlete=AthleteEvidence(provider_id=athlete_id, name=player_name),
                event=EventEvidence(
                    provider_id=game.event_id,
                    label=game.label,
                    starts_at=starts_at,
                    home_team=game.home,
                    away_team=game.away,
                ),
                statistic=StatisticEvidence(label=market_type),
                threshold=threshold,
                status=status,
                status_label=raw_status,
                variant=MarketVariant.STANDARD,
                variant_label=MarketVariant.STANDARD.value,
                scoring_period=None,
                starts_at=starts_at,
                selections=(
                    Selection(
                        selection_id=f"{market_id}:over",
                        label="Over",
                        direction="over",
                        direction_label="Over",
                        decimal_price=over_price,
                    ),
                    Selection(
                        selection_id=f"{market_id}:under",
                        label="Under",
                        direction="under",
                        direction_label="Under",
                        decimal_price=under_price,
                    ),
                ),
            )
        except ValueError as error:
            raise CoverageRecordMalformed(str(error)) from error
        wide = bid is not None and ask is not None and ask - bid > _MAX_QUOTED_SPREAD
        midpoint = (bid + ask) / 2 if bid is not None and ask is not None and not wide else None
        return _Prop(
            market=built,
            group=(athlete_id, market_type, game.event_id),
            line=threshold.value,
            midpoint=midpoint,
            wide=wide,
        )

    @staticmethod
    def _player_name(market: Mapping[str, Any]) -> str:
        """The player before the ':' of ``groupItemTitle``, else of ``question``."""

        for key in ("groupItemTitle", "question"):
            text = optional_text(market.get(key))
            if text is not None and ":" in text:
                name = text.split(":", 1)[0].strip()
                if name:
                    return name
        raise CoverageRecordMalformed("player name must precede ':' in the market title")

    @staticmethod
    def _athlete_key(name: str) -> str:
        decomposed = unicodedata.normalize("NFKD", name).casefold()
        ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
        return "-".join(re.findall(r"[a-z0-9]+", ascii_name))

    @staticmethod
    def _outcomes(value: Any) -> tuple[str, ...]:
        try:
            decoded = json.loads(value) if isinstance(value, str) else value
        except ValueError as error:
            raise CoverageRecordMalformed("market outcomes must be a JSON list") from error
        if not isinstance(decoded, list) or not all(isinstance(o, str) for o in decoded):
            raise CoverageRecordMalformed("market outcomes must be a list of labels")
        return tuple(outcome.strip().casefold() for outcome in decoded)

    @staticmethod
    def _probability(market: Mapping[str, Any], key: str) -> Decimal | None:
        value = market.get(key)
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise CoverageRecordMalformed(f"{key} must be numeric")
        try:
            probability = Decimal(str(value))
        except InvalidOperation as error:
            raise CoverageRecordMalformed(f"{key} must be numeric") from error
        if not probability.is_finite() or not 0 <= probability <= 1:
            raise CoverageRecordMalformed(f"{key} must be a probability between 0 and 1")
        return probability

    @staticmethod
    def _invalid_response(detail: Any) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            "Polymarket returned an invalid response.",
            detail=detail,
            provider_reason="malformed_response",
        )
