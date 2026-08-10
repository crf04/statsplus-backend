"""Assemble one published DFS Board representation.

The route above this seam does no deciding.  Everything that determines what a
caller receives -- whether the board is published at all, whether it is usable,
the exact JSON of version 1, the representation ETag, the conditional outcome,
and the bounded telemetry describing all of it -- is decided here, once, from
the deterministic :class:`~app.domain.comparisons.ComparisonBoard` the
comparison service returns.

Two rules shape the serialization.  Every exact decimal is written as a string
in the scale the provider published it in, because a JSON number would silently
re-round a threshold the board went to some trouble to keep exact.  Every
timestamp is timezone-aware UTC.  Nothing is added: the payload is the board's
own retained evidence and provenance, and the only facts it invents are the
contract version and the references the board already derived.

The ETag identifies the board's *content*, not the instant it was read.  The
observation time and the ages derived from it are excluded from it deliberately,
so a caller revalidating an unchanged board gets 304 instead of an identical
board that differs only in how old it says it is.  That is why the tag is weak.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

from app.config.settings import RuntimeSettings
from app.domain.comparisons import ComparisonBoard, ComparisonGroup
from app.errors import AppError, InvalidInputError, ProviderUnavailableError
from app.services.comparison_board import ComparisonBoardTooLargeError
from app.services.dfs_board_query import BoardRequest, parse_board_request
from app.utils.telemetry import (
    BOARD_REQUEST_STATUSES,
    BoardRequestEvent,
    ProviderResponseError,
    current_request_id,
    record_board_request_event,
)

#: The public response contract version.  It is independent of the internal
#: reference and cache algorithm versions, so those may change without
#: reinterpreting a published board.
BOARD_CONTRACT_VERSION = "1"

#: Fields derived from the instant the board was read.  They belong in the
#: response and not in the identity of the representation.
_OBSERVATION_FIELDS = frozenset({"generated_at", "observed_at", "age_seconds"})

_USABLE_PROVIDER_STATUSES = frozenset({"complete", "partial"})


class DFSBoardDisabledError(AppError):
    """The DFS Board is not published by this deployment's configuration."""

    status_code = 404
    code = "dfs_board_disabled"
    default_message = "The DFS Board is not enabled on this deployment."


class DFSBoardUnavailableError(ProviderUnavailableError):
    """No enabled provider produced a usable snapshot for this read.

    The public payload is the same bounded Provider Outcome vocabulary the
    board reports on success -- statuses, stable failure reasons, coverage
    warning codes, and cache states -- so a caller learns which provider failed
    and how, without any upstream text, URL, or credential reaching them.
    """

    default_message = (
        "No DFS provider produced a usable snapshot. Please try again later."
    )

    def __init__(
        self,
        *,
        provider_outcomes: list[dict[str, Any]],
        disabled_providers: list[str],
        message: str | None = None,
    ) -> None:
        self.provider_outcomes = provider_outcomes
        self.disabled_providers = disabled_providers
        super().__init__(message)

    @property
    def public_details(self) -> dict[str, Any]:
        return {
            "contract_version": BOARD_CONTRACT_VERSION,
            "provider_outcomes": self.provider_outcomes,
            "disabled_providers": self.disabled_providers,
        }


@dataclass(slots=True)
class _BoardRead:
    """What one request in flight has established, for its single event.

    A read that has not yet reached a decision is an error: nothing else can
    have happened to it, and an outcome is never left unstated.
    """

    board: ComparisonBoard | None = None
    outcome: str = "error"


@dataclass(frozen=True, slots=True)
class BoardRepresentation:
    """One board response: its status, its body, and its entity tag."""

    status_code: int
    etag: str
    payload: dict[str, Any] | None = None

    @property
    def is_not_modified(self) -> bool:
        return self.status_code == 304


class DFSBoardResponseService:
    """Decide and assemble exactly what one board request receives."""

    def __init__(
        self,
        comparison_board_service: Any,
        *,
        settings: RuntimeSettings,
        monotonic: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
        recorder: Callable[[BoardRequestEvent], None] | None = None,
    ) -> None:
        if not callable(getattr(comparison_board_service, "get_comparisons", None)):
            raise TypeError("the board response service requires a comparison board")
        self.comparison_board_service = comparison_board_service
        self.settings = settings
        self.monotonic = monotonic or time.monotonic
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.recorder = recorder or record_board_request_event

    # -- publication -------------------------------------------------------

    @property
    def is_published(self) -> bool:
        """Whether this deployment publishes the board at all.

        Both halves are required everywhere: the feature flag says the board is
        meant to be exposed, and the registry says which providers may be
        called.  Either one alone publishes nothing.
        """

        return bool(
            self.settings.features.dfs_board_enabled
            and self.settings.providers.dfs_enabled_providers
        )

    # -- public seam -------------------------------------------------------

    def respond_to_query(
        self,
        args: Any,
        *,
        if_none_match: str | None = None,
    ) -> BoardRepresentation:
        """Decide one whole authenticated board request, from its query string.

        This is the seam an authenticated request enters and leaves exactly
        once, so everything that decides its outcome is inside one observation:
        the publication gate, the parser, the board itself, and the
        serialization.  Publication is settled *first* -- before a single
        parameter is read -- because a deployment that publishes no board owes
        a caller nothing about which filters it would have accepted, and must
        reach no parser, provider, database, or cache to say so.
        """

        read = _BoardRead()
        with self._recorded(read):
            self._require_published()
            request = parse_board_request(args, settings=self.settings)
            return self._respond(read, request, if_none_match)

    def respond(
        self,
        request: BoardRequest,
        *,
        if_none_match: str | None = None,
    ) -> BoardRepresentation:
        """Assemble one board response for an already-parsed read.

        The same single observation applies, so a caller that parsed the query
        itself still produces exactly one event and never two.
        """

        read = _BoardRead()
        with self._recorded(read):
            self._require_published()
            return self._respond(read, request, if_none_match)

    # -- decisions ---------------------------------------------------------

    def _require_published(self) -> None:
        if not self.is_published:
            raise DFSBoardDisabledError()

    def _respond(
        self,
        read: "_BoardRead",
        request: BoardRequest,
        if_none_match: str | None,
    ) -> BoardRepresentation:
        """The board itself: retrieved, judged usable, serialized, and tagged."""

        board = self.comparison_board_service.get_comparisons(
            request.query, filters=request.filters
        )
        read.board = board

        if not _has_usable_provider(board):
            raise DFSBoardUnavailableError(
                provider_outcomes=[
                    _provider_outcome(report) for report in board.provider_reports
                ],
                disabled_providers=list(board.disabled_providers),
            )

        payload = serialize_board(board)
        etag = board_etag(payload)
        if _matches(if_none_match, etag):
            read.outcome = "not_modified"
            return BoardRepresentation(status_code=304, etag=etag)

        read.outcome = "served"
        return BoardRepresentation(status_code=200, etag=etag, payload=payload)

    # -- telemetry ---------------------------------------------------------

    @contextmanager
    def _recorded(self, read: "_BoardRead") -> Iterator[None]:
        """Observe one board request exactly once, however it ends.

        The failure classification happens here rather than at each raise, so
        every path -- the gate, the parser, a refused size, an outage, and an
        unexpected error the route boundary will translate -- is counted once
        under the status its caller actually receives.
        """

        started_at = self.clock().astimezone(timezone.utc).isoformat()
        started = self.monotonic()
        try:
            yield
        except BaseException as error:
            read.outcome = _failure_outcome(error)
            raise
        finally:
            self._record(read, started=started, started_at=started_at)

    def _record(self, read: "_BoardRead", *, started: float, started_at: str) -> None:
        """Emit one bounded aggregate for this read, whatever it produced."""

        board = read.board
        reports = () if board is None else board.provider_reports
        availability = "unknown" if board is None else "available"
        if board is not None and not board.availability.available:
            unavailable = board.availability.unavailable_catalogs
            availability = unavailable[0].reason.value if unavailable else "available"
        request_id = current_request_id()
        self.recorder(
            BoardRequestEvent(
                duration_ms=max(0.0, (self.monotonic() - started) * 1000.0),
                outcome=read.outcome,
                status_code=BOARD_REQUEST_STATUSES[read.outcome],
                comparison_availability=availability,
                provider_status_counts=_counts(report.status for report in reports),
                failure_reason_counts=_counts(
                    report.reason for report in reports if report.reason
                ),
                freshness_counts=_counts(
                    "unknown" if report.freshness is None else report.freshness.value
                    for report in reports
                ),
                cache_counts=_counts(
                    "unset"
                    if report.cache is None or report.cache.status is None
                    else report.cache.status
                    for report in reports
                ),
                group_count=0 if board is None else len(board.groups),
                market_count=0 if board is None else board.market_count,
                unresolved_count=0 if board is None else len(board.unresolved),
                disabled_provider_count=(
                    0 if board is None else len(board.disabled_providers)
                ),
                started_at=started_at,
                request_id=None if request_id == "-" else request_id,
            )
        )


# -- serialization ---------------------------------------------------------


def serialize_board(board: ComparisonBoard) -> dict[str, Any]:
    """The complete version 1 JSON body for one comparison board."""

    return {
        "contract_version": BOARD_CONTRACT_VERSION,
        "generated_at": _encode(board.generated_at),
        "season": board.season,
        "filters": _encode(board.filters),
        "market_count": board.market_count,
        "comparison_availability": _encode(board.availability),
        "comparison_groups": [_group(group) for group in board.groups],
        "unresolved_markets": [_encode(entry) for entry in board.unresolved],
        "markets": [_encode(market) for market in board.markets],
        "provider_reports": [_encode(report) for report in board.provider_reports],
        "disabled_providers": list(board.disabled_providers),
    }


def board_etag(payload: dict[str, Any]) -> str:
    """A weak entity tag over everything but the instant of observation."""

    identity = json.dumps(
        _without_observation(payload), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _group(group: ComparisonGroup) -> dict[str, Any]:
    """One comparison group, led by the reference its members cite."""

    return {
        "comparison_reference": group.reference,
        "key": _encode(group.key),
        "summary": _encode(group.summary),
        "members": [_encode(member) for member in group.members],
    }


def _encode(value: Any) -> Any:
    """One immutable board value as its JSON-ready, exact equivalent."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        # Written in the scale the provider published, never through a float
        # and never in exponent notation.
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if is_dataclass(value):
        return {
            field.name: _encode(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    raise TypeError(f"a board response cannot serialize {type(value).__name__}")


def _without_observation(value: Any) -> Any:
    """The same payload with every observation-time field removed."""

    if isinstance(value, dict):
        return {
            key: _without_observation(item)
            for key, item in value.items()
            if key not in _OBSERVATION_FIELDS
        }
    if isinstance(value, list):
        return [_without_observation(item) for item in value]
    return value


# -- outcomes --------------------------------------------------------------


def _has_usable_provider(board: ComparisonBoard) -> bool:
    """Whether any provider contributed a snapshot this board could read.

    A complete, partial, permitted-stale, or empty-complete snapshot all count.
    Emptiness is a fact about the providers' current offerings, not an outage.

    A retrieval that succeeded is not by itself a usable board.  An observation
    past its provider's stale-if-error ceiling, or timestamped ahead of the
    board's own clock, enters no comparison and leaves every one of its markets
    unresolved, so a board carrying only those states nothing and is reported as
    the outage it is.  Both are read from the report's own typed evidence --
    :class:`~app.domain.comparisons.MarketFreshness` and the future-observation
    flag the board already derived -- never from exclusion text.
    """

    return any(_is_usable(report) for report in board.provider_reports)


def _is_usable(report: Any) -> bool:
    """Whether one provider report is a snapshot the board could read.

    A complete or partial outcome always carries a snapshot, so it always
    carries an observation: freshness is absent exactly when the observation is
    beyond the permitted maximum age or ahead of the board's clock.
    """

    return (
        report.status in _USABLE_PROVIDER_STATUSES
        and not report.future_observation
        and report.freshness is not None
    )


def _failure_outcome(error: BaseException) -> str:
    """The bounded outcome one failed board request is counted under.

    Each name is the one the caller's own status will state, so the recorded
    outcome and the response can never describe two different things.  Anything
    unrecognized is an ``error``: the route boundary turns it into the same safe
    500, and telemetry says exactly that rather than guessing at a cause.
    """

    if isinstance(error, DFSBoardDisabledError):
        return "disabled"
    if isinstance(error, ComparisonBoardTooLargeError):
        return "too_large"
    if isinstance(error, InvalidInputError):
        return "invalid"
    if isinstance(error, (ProviderUnavailableError, ProviderResponseError)):
        return "unavailable"
    return "error"


def _provider_outcome(report: Any) -> dict[str, Any]:
    """One provider's bounded, sanitized outcome for a 503 body."""

    return {
        "provider": report.provider,
        "status": report.status,
        "reason": report.reason,
        # Why a successful retrieval still published nothing: both are closed
        # facts the board derived, never an upstream label.
        "freshness": None if report.freshness is None else report.freshness.value,
        "future_observation": report.future_observation,
        "warning_codes": list(report.warning_codes),
        "cache_status": None if report.cache is None else report.cache.status,
        "cache_failure_reason": (
            None if report.cache is None else report.cache.failure_reason
        ),
    }


def _counts(values: Any) -> tuple[tuple[str, int], ...]:
    """Bounded label counts in a deterministic order."""

    return tuple(sorted(Counter(values).items()))


def _matches(if_none_match: str | None, etag: str) -> bool:
    """Whether a conditional request already holds this representation.

    The tag is weak, so the comparison is weak too: a ``W/`` prefix and the
    quoting around a tag are syntax, not identity.
    """

    if not if_none_match:
        return False
    candidates = [candidate.strip() for candidate in if_none_match.split(",")]
    for candidate in candidates:
        if candidate == "*":
            return True
        if candidate.startswith(("W/", "w/")):
            candidate = candidate[2:]
        if candidate.strip().strip('"') == etag:
            return True
    return False


__all__ = [
    "BOARD_CONTRACT_VERSION",
    "BoardRepresentation",
    "DFSBoardDisabledError",
    "DFSBoardResponseService",
    "DFSBoardUnavailableError",
    "board_etag",
    "serialize_board",
]
