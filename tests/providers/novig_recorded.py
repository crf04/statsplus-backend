"""Recorded Novig payloads and a replay session, written for offline tests.

``tests/fixtures/novig`` holds real responses from Novig's unauthenticated API
(recorded 2026-10-15), trimmed to a few markets but otherwise verbatim:

* ``markets.page1.json`` -- ``GET /v3/public/catalog/markets?league=NBA&limit=200``
  (first page; its ``next`` is the real cursor).
* ``markets.page2.json`` -- the same listing fetched with ``after=<page 1 next>``.
  Its own ``next`` was removed so that pagination ends after two pages, and its
  last row (a championship market) comes from a later real page.
* ``events.json`` -- ``GET /v3/public/catalog/events/<eventId>``, keyed by id.
* ``batch.json`` -- ``GET https://api.novig.us/nbx/v1/markets/book/batch``
  ``?marketIds=<the 20 player markets above>&currency=CASH``, verbatim.  Most
  books are one-sided because that is what the venue returned.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.providers.novig import NovigAdapter

FIXTURES = Path(__file__).parents[1] / "fixtures" / "novig"
OBSERVED_AT = datetime(2026, 8, 9, 20, tzinfo=timezone.utc)

NYK_EVENT = "01a13a36-f390-7e63-bd56-21d5f3c5613d"
PHI_EVENT = "019ff261-5cb3-7913-8ece-6ab58f99e8f0"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> object:
        return self._payload


class ReplaySession:
    """Serve recorded Novig responses by URL, safely from several threads.

    ``stale`` ids make any batch request containing one answer 404 (what Novig
    does for a settled or removed market); ``unreadable`` ids make it answer
    403.  ``failures`` maps a URL suffix to the exception to raise or the HTTP status
    to answer for any request whose URL ends with it; ``later_page_failure``
    does the same for market-list requests that carry an ``after`` cursor.
    """

    def __init__(
        self,
        *,
        pages: list[Any] | None = None,
        events: dict[str, Any] | None = None,
        batch: list[Any] | None = None,
        stale: set[str] | None = None,
        unreadable: set[str] | None = None,
        failures: dict[str, BaseException | int] | None = None,
        later_page_failure: BaseException | int | None = None,
    ) -> None:
        self.pages = pages if pages is not None else [
            load("markets.page1.json"),
            load("markets.page2.json"),
        ]
        self.events = events if events is not None else load("events.json")
        self.batch = batch if batch is not None else load("batch.json")
        self.stale = stale or set()
        self.unreadable = unreadable or set()
        self.failures = failures or {}
        self.later_page_failure = later_page_failure
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._lock = threading.Lock()

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any):
        del kwargs
        with self._lock:
            self.calls.append((url, None if params is None else dict(params)))
        failure = next(
            (f for suffix, f in self.failures.items() if url.endswith(suffix)), None
        )
        if failure is None and params and "after" in params:
            failure = self.later_page_failure
        if isinstance(failure, BaseException):
            raise failure
        if failure is not None:
            return Response({"code": "ERROR"}, status_code=failure)
        if url == NovigAdapter.BATCH_BOOK_URL:
            ids = params["marketIds"].split(",")
            if self.unreadable.intersection(ids):
                return Response({"code": "FORBIDDEN"}, status_code=403)
            if self.stale.intersection(ids):
                return Response({"code": "NOT_FOUND"}, status_code=404)
            wanted = set(ids)
            return Response([e for e in self.batch if e["market"]["id"] in wanted])
        marker = "/v3/public/catalog/"
        path = url.split(marker, 1)[1]
        if path == "markets":
            return Response(self._page(params or {}))
        if path.startswith("events/"):
            return Response(self.events[path.removeprefix("events/")])
        raise AssertionError(f"unrecorded request {url}")

    def _page(self, params: dict[str, Any]) -> Any:
        cursor = params.get("after")
        if cursor is None:
            return self.pages[0]
        for index, page in enumerate(self.pages[:-1]):
            if page.get("next") == cursor:
                return self.pages[index + 1]
        raise AssertionError(f"unrecorded cursor {cursor}")

    def urls(self, suffix: str) -> list[str]:
        return [url for url, _ in self.calls if url.endswith(suffix)]


def adapter(session: ReplaySession | None = None, **overrides: Any) -> NovigAdapter:
    """A replayed adapter with a fixed clock."""

    options: dict[str, Any] = {
        "session": session or ReplaySession(),
        "now": lambda: OBSERVED_AT,
    }
    options.update(overrides)
    return NovigAdapter(**options)


def market_row(market_id: str) -> dict[str, Any]:
    """A deep copy of one recorded market row."""

    for name in ("markets.page1.json", "markets.page2.json"):
        for row in load(name)["items"]:
            if row["marketId"] == market_id:
                return deepcopy(row)
    raise KeyError(market_id)
