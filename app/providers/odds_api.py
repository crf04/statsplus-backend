"""The Odds API v4 client and response parser for sportsbook player props.

This module is the only place that builds an Odds API URL.  The API
authenticates with an ``apiKey`` query parameter, so every error raised here is
composed from the endpoint path and a closed reason, never from a URL or an
upstream exception message, and a log filter strips the parameter from the HTTP
library's own request logging.  Raw payloads are not archived anywhere; the
parser keeps only normalized quotes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

SPORT_KEY = "basketball_nba"
REGIONS = "us"

#: The standard (non-alternate) player markets and their Matchup categories.
MARKET_CATEGORIES: Mapping[str, str] = {
    "player_points": "PTS",
    "player_rebounds": "REB",
    "player_assists": "AST",
    "player_threes": "3PM",
    "player_points_rebounds_assists": "PRA",
}

#: Regulated US books only (owner decision, crf04/statsplus#185 section 12).
#: Offshore books (BetOnline, BetUS, Bovada, LowVig, MyBookie) are never
#: served, and ``us2`` is never requested.  The keys are Odds API bookmaker
#: keys; a book the API does not return is simply absent.
BOOK_ALLOWLIST = frozenset(
    {"draftkings", "fanduel", "betmgm", "williamhill_us", "betrivers", "fanatics"}
)

_API_KEY_QUERY_RE = re.compile(r"(?P<prefix>[?&]apiKey=)[^&#\s'\"]*", re.IGNORECASE)


def redact_url(value: str) -> str:
    """Return text with every ``apiKey`` query value replaced."""

    return _API_KEY_QUERY_RE.sub(r"\g<prefix>[REDACTED]", value)


class OddsApiError(Exception):
    """A failed Odds API call, described without any URL or credential.

    ``reason`` is one of ``unauthorized`` (401), ``invalid_request`` (422),
    ``rate_limited`` (429), ``server`` (5xx), ``event_not_found`` (404),
    ``transport``, or ``invalid_response``.
    """

    def __init__(self, reason: str, *, status: int | None = None, path: str = ""):
        self.reason = reason
        self.status = status
        self.path = path
        detail = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"Odds API {reason}{detail} for {path}".rstrip())

    @property
    def halts(self) -> bool:
        """A bad key or plan: nothing a retry can fix."""

        return self.reason in {"unauthorized", "invalid_request"}


@dataclass(frozen=True, slots=True)
class OddsQuota:
    """The three quota headers of one response; any may be absent."""

    remaining: int | None = None
    used: int | None = None
    last_cost: int | None = None


@dataclass(frozen=True, slots=True)
class OddsEvent:
    event_id: str
    commence_time: datetime
    home_team: str
    away_team: str


@dataclass(frozen=True, slots=True)
class BookQuote:
    """One book's standard line and prices for one player and market."""

    book: str
    market_key: str
    category: str
    player_name: str
    line: float
    over: int | None
    under: int | None
    last_update: datetime | None


@dataclass(frozen=True, slots=True)
class ParsedEventOdds:
    quotes: tuple[BookQuote, ...]
    #: Books the response carried that the allowlist refused.
    dropped_books: frozenset[str]
    #: (book, player, market) groups omitted for posting two distinct lines.
    multi_line_omitted: int


@dataclass(frozen=True, slots=True)
class EventsResult:
    events: tuple[OddsEvent, ...]
    quota: OddsQuota


@dataclass(frozen=True, slots=True)
class EventOddsResult:
    parsed: ParsedEventOdds
    quota: OddsQuota
    #: Credits this call cost (``x-requests-last``); 0 for an empty response.
    cost: int


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _american(value: Any) -> int | None:
    """An integer American price, or ``None`` for anything else."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value != int(value):
        return None
    price = int(value)
    return price if abs(price) >= 100 else None


def _line(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _header_int(headers: Mapping[str, Any], name: str) -> int | None:
    raw = headers.get(name)
    try:
        return None if raw is None else int(float(str(raw).strip()))
    except ValueError:
        return None


def parse_quota(headers: Mapping[str, Any]) -> OddsQuota:
    return OddsQuota(
        remaining=_header_int(headers, "x-requests-remaining"),
        used=_header_int(headers, "x-requests-used"),
        last_cost=_header_int(headers, "x-requests-last"),
    )


def parse_events(payload: Any) -> tuple[OddsEvent, ...]:
    """Decode the free ``/events`` list; malformed entries are skipped."""

    if not isinstance(payload, list):
        raise OddsApiError("invalid_response", path="/events")
    events = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        commence = _parse_time(item.get("commence_time"))
        event_id = item.get("id")
        home, away = item.get("home_team"), item.get("away_team")
        if (
            commence is None
            or not isinstance(event_id, str)
            or not event_id.strip()
            or not isinstance(home, str)
            or not isinstance(away, str)
        ):
            continue
        events.append(OddsEvent(event_id.strip(), commence, home.strip(), away.strip()))
    return tuple(events)


def parse_event_odds(payload: Any) -> ParsedEventOdds:
    """Normalize one ``/events/{id}/odds`` body into allowlisted book quotes.

    A (book, player, market) is one quote.  Its Over and Under outcomes must
    share one ``point``; a book that posts two distinct points for the same
    player and market in this response is omitted for that market rather than
    guessed at.  Unknown markets and non-American or malformed prices are
    dropped, and a side a book does not post stays ``None``.
    """

    if not isinstance(payload, Mapping):
        raise OddsApiError("invalid_response", path="/odds")
    bookmakers = payload.get("bookmakers") or []
    if not isinstance(bookmakers, list):
        raise OddsApiError("invalid_response", path="/odds")
    quotes: list[BookQuote] = []
    dropped: set[str] = set()
    omitted = 0
    for bookmaker in bookmakers:
        if not isinstance(bookmaker, Mapping):
            continue
        book = str(bookmaker.get("key") or "").strip().casefold()
        if not book:
            continue
        if book not in BOOK_ALLOWLIST:
            dropped.add(book)
            continue
        for market in bookmaker.get("markets") or []:
            if not isinstance(market, Mapping):
                continue
            market_key = str(market.get("key") or "")
            category = MARKET_CATEGORIES.get(market_key)
            if category is None:
                continue
            last_update = _parse_time(market.get("last_update")) or _parse_time(
                bookmaker.get("last_update")
            )
            sides: dict[str, dict[float, dict[str, int | None]]] = {}
            for outcome in market.get("outcomes") or []:
                if not isinstance(outcome, Mapping):
                    continue
                player = str(outcome.get("description") or "").strip()
                side = str(outcome.get("name") or "").strip().casefold()
                point = _line(outcome.get("point"))
                if not player or side not in {"over", "under"} or point is None:
                    continue
                by_point = sides.setdefault(player, {}).setdefault(point, {})
                by_point[side] = _american(outcome.get("price"))
            for player, by_point in sides.items():
                if len(by_point) != 1:
                    omitted += 1
                    continue
                (point, prices), = by_point.items()
                quotes.append(
                    BookQuote(
                        book=book,
                        market_key=market_key,
                        category=category,
                        player_name=player,
                        line=point,
                        over=prices.get("over"),
                        under=prices.get("under"),
                        last_update=last_update,
                    )
                )
    return ParsedEventOdds(tuple(quotes), frozenset(dropped), omitted)


class _ApiKeyLogFilter(logging.Filter):
    """Strip the ``apiKey`` query value from the HTTP library's own logging."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "apikey=" in message.casefold():
            record.msg = redact_url(message)
            record.args = ()
        return True


def install_log_redaction() -> None:
    """Attach the key filter to the HTTP stack's loggers (idempotent).

    urllib3 logs ``"GET /v4/...?apiKey=..."`` at DEBUG; the filter rewrites
    such records before any handler sees them.
    """

    for name in ("urllib3", "urllib3.connectionpool", "requests"):
        target = logging.getLogger(name)
        if not any(isinstance(item, _ApiKeyLogFilter) for item in target.filters):
            target.addFilter(_ApiKeyLogFilter())


class OddsApiClient:
    """Thin, injectable-transport client for the two endpoints we use."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.the-odds-api.com",
        timeout: float = 20.0,
        session: Any | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ValueError("an Odds API key is required")
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()
        install_log_redaction()

    def __repr__(self) -> str:
        return f"OddsApiClient(base_url={self._base_url!r})"

    def list_events(
        self, commence_from: datetime, commence_to: datetime
    ) -> EventsResult:
        """The free event list for a window (does not count against quota)."""

        path = f"/v4/sports/{SPORT_KEY}/events"
        body, headers = self._get(
            path,
            {
                "commenceTimeFrom": _iso(commence_from),
                "commenceTimeTo": _iso(commence_to),
            },
        )
        return EventsResult(parse_events(body), parse_quota(headers))

    def event_odds(self, event_id: str) -> EventOddsResult:
        """One event's five standard prop markets for the ``us`` region."""

        path = f"/v4/sports/{SPORT_KEY}/events/{event_id}/odds"
        body, headers = self._get(
            path,
            {
                "regions": REGIONS,
                "markets": ",".join(MARKET_CATEGORIES),
                "oddsFormat": "american",
                "dateFormat": "iso",
            },
        )
        quota = parse_quota(headers)
        parsed = parse_event_odds(body)
        # An empty response is documented as free; trust the header when it
        # is present and otherwise assume nothing was spent.
        cost = quota.last_cost if quota.last_cost is not None else 0
        return EventOddsResult(parsed, quota, cost)

    def bulk_props_probe(self) -> tuple[int | None, bool, OddsQuota]:
        """Spike: ask the bulk ``/odds`` endpoint for a player-prop market.

        Returns ``(http_status, any_prop_market_returned, quota)``.  Documented
        expectation: the bulk endpoint does not serve player props.
        """

        try:
            body, headers = self._get(
                f"/v4/sports/{SPORT_KEY}/odds",
                {"regions": REGIONS, "markets": "player_points", "oddsFormat": "american"},
            )
        except OddsApiError as error:
            return error.status, False, OddsQuota()
        served = isinstance(body, list) and any(
            market.get("key") == "player_points"
            for event in body
            if isinstance(event, Mapping)
            for bookmaker in event.get("bookmakers") or []
            for market in bookmaker.get("markets") or []
        )
        return 200, served, parse_quota(headers)

    def _get(self, path: str, params: Mapping[str, str]) -> tuple[Any, Mapping[str, Any]]:
        try:
            response = self._session.get(
                self._base_url + path,
                params={**params, "apiKey": self._api_key},
                timeout=self._timeout,
            )
        except requests.RequestException:
            # The exception text embeds the full URL, key included.
            raise OddsApiError("transport", path=path) from None
        status = int(response.status_code)
        if status == 401:
            raise OddsApiError("unauthorized", status=status, path=path)
        if status == 422:
            raise OddsApiError("invalid_request", status=status, path=path)
        if status == 429:
            raise OddsApiError("rate_limited", status=status, path=path)
        if status == 404:
            raise OddsApiError("event_not_found", status=status, path=path)
        if status >= 500:
            raise OddsApiError("server", status=status, path=path)
        if status != 200:
            raise OddsApiError("invalid_response", status=status, path=path)
        try:
            body = response.json()
        except ValueError:
            raise OddsApiError("invalid_response", status=status, path=path) from None
        return body, response.headers


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Odds API team name -> NBA tricode (the Event Catalog stores short names such
#: as "Lakers", so a full name only resolves through the tricode).
TEAM_NAME_TRICODES: Mapping[str, str] = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "Los Angeles Clippers": "LAC", "LA Clippers": "LAC", "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM", "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}


def team_tricode(name: str) -> str | None:
    return TEAM_NAME_TRICODES.get(name.strip())


__all__ = [
    "BOOK_ALLOWLIST",
    "BookQuote",
    "EventOddsResult",
    "EventsResult",
    "MARKET_CATEGORIES",
    "OddsApiClient",
    "OddsApiError",
    "OddsEvent",
    "OddsQuota",
    "ParsedEventOdds",
    "TEAM_NAME_TRICODES",
    "install_log_redaction",
    "parse_event_odds",
    "parse_events",
    "parse_quota",
    "redact_url",
    "team_tricode",
]
