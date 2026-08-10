"""Assemble one published DFS Board representation.

The route above this seam does no deciding.  Everything that determines what a
caller receives -- whether the board is published at all, whether it is usable,
the exact JSON of version 1, the representation ETag, and the conditional
outcome -- is decided here, once, from the deterministic
:class:`~app.domain.comparisons.ComparisonBoard` the comparison service
returns.

The one bounded event describing the request is *not* decided here, and that is
deliberate.  This service knows what it meant to publish; only the response
knows what the caller received.  So a request carries a
:class:`PendingBoardObservation` opened before anything could decide it, this
service contributes the typed facts and typed failures it produces, and the
HTTP layer finalizes it once against the real status.

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
import logging
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
from app.domain.comparisons import (
    BoardReadEvidence,
    ComparisonBoard,
    ComparisonGroup,
    has_readable_provider,
)
from app.errors import AppError, InvalidInputError, ProviderUnavailableError
from app.services.comparison_board import (
    ComparisonBoardTooLargeError,
    UnreadableComparisonBoardError,
)
from app.services.dfs_board_query import BoardRequest, parse_board_request
from app.utils.telemetry import (
    BOARD_REQUEST_STATUSES,
    BoardRequestEvent,
    ProviderResponseError,
    current_request_id,
    record_board_request_event,
)

logger = logging.getLogger(__name__)

#: The public response contract version.  It is independent of the internal
#: reference and cache algorithm versions, so those may change without
#: reinterpreting a published board.
BOARD_CONTRACT_VERSION = "1"

#: Fields derived from the instant the board was read.  They belong in the
#: response and not in the identity of the representation.
_OBSERVATION_FIELDS = frozenset({"generated_at", "observed_at", "age_seconds"})


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


#: The outcome each final HTTP status is counted under.  Two outcomes share the
#: 400, so a refusal that states which one it was decides between them; every
#: other status names exactly one outcome and needs no hint.
_OUTCOME_BY_STATUS = {
    200: "served",
    304: "not_modified",
    400: "invalid",
    404: "disabled",
    500: "error",
    503: "unavailable",
}


class PendingBoardObservation:
    """One authenticated board request, observed until its response exists.

    An observation opens before anything can decide the request -- before the
    dependency graph is read, the publication gate is consulted, or a parameter
    is parsed -- so a request that fails before reaching the board is still one
    request that happened.  It accumulates the typed facts each stage
    establishes, and it is finalized exactly once, from the status of the
    response the caller actually received.

    That last part is the whole point.  A service can only state what it *meant*
    to publish; a serialization that raises afterwards, a dependency that never
    resolved, and an error handler that translated a failure all decide the
    caller's status after the service has stopped speaking.  Recording the real
    status means the event and the response can never describe two different
    requests.
    """

    def __init__(
        self,
        *,
        recorder: Callable[[BoardRequestEvent], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._recorder = recorder or record_board_request_event
        self._monotonic = monotonic or time.monotonic
        started = (clock or (lambda: datetime.now(timezone.utc)))()
        self._started_at = started.astimezone(timezone.utc).isoformat()
        self._started = self._monotonic()
        self._evidence: BoardReadEvidence | None = None
        self._refusal: str | None = None
        self._finalized = False

    # -- accumulation ------------------------------------------------------

    def observe(self, evidence: BoardReadEvidence) -> None:
        """Keep what a completed read established, published or refused."""

        if not isinstance(evidence, BoardReadEvidence):
            raise TypeError("a board observation records typed board evidence")
        self._evidence = evidence

    def note_refusal(self, error: BaseException) -> None:
        """Note the typed failure leaving the board service, and its evidence.

        The label is a hint rather than the outcome: it only distinguishes the
        two refusals that share one status.  A refusal that finished a read
        before refusing it -- an over-ceiling board -- carries that read's
        evidence, which is absorbed here so the event counts what was observed
        instead of the board that was never built.
        """

        self._refusal = _failure_outcome(error)
        evidence = getattr(error, "board_evidence", None)
        if isinstance(evidence, BoardReadEvidence):
            self._evidence = evidence

    # -- the single event --------------------------------------------------

    def finalize(self, status_code: int) -> None:
        """Record this request's one event, from the status it truly ended in.

        Later calls do nothing: one request is one event, however many layers
        see the response on its way out.
        """

        if self._finalized:
            return
        self._finalized = True
        self._recorder(self._event(self._outcome(status_code)))

    def _outcome(self, status_code: int) -> str:
        outcome = _OUTCOME_BY_STATUS.get(status_code)
        if outcome is None:
            # A board request cannot end in any other status, so one that did
            # is a defect rather than an operational signal, and it is counted
            # as the error it is rather than inventing a label for it.
            logger.warning("board request ended in unstatable status %s", status_code)
            return "error"
        if self._refusal is not None and status_code == BOARD_REQUEST_STATUSES.get(
            self._refusal
        ):
            return self._refusal
        return outcome

    def _event(self, outcome: str) -> BoardRequestEvent:
        evidence = self._evidence
        reports = () if evidence is None else evidence.provider_reports
        availability = "unknown"
        if evidence is not None:
            availability = "available"
            if not evidence.availability.available:
                unavailable = evidence.availability.unavailable_catalogs
                if unavailable:
                    availability = unavailable[0].reason.value
        request_id = current_request_id()
        return BoardRequestEvent(
            duration_ms=max(0.0, (self._monotonic() - self._started) * 1000.0),
            outcome=outcome,
            status_code=BOARD_REQUEST_STATUSES[outcome],
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
            group_count=0 if evidence is None else evidence.group_count,
            market_count=0 if evidence is None else evidence.market_count,
            unresolved_count=0 if evidence is None else evidence.unresolved_count,
            disabled_provider_count=(
                0 if evidence is None else len(evidence.disabled_providers)
            ),
            started_at=self._started_at,
            request_id=None if request_id == "-" else request_id,
        )


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
    ) -> None:
        if not callable(getattr(comparison_board_service, "get_comparisons", None)):
            raise TypeError("the board response service requires a comparison board")
        self.comparison_board_service = comparison_board_service
        self.settings = settings

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
        observation: PendingBoardObservation | None = None,
    ) -> BoardRepresentation:
        """Decide one whole authenticated board request, from its query string.

        Publication is settled *first* -- before a single parameter is read --
        because a deployment that publishes no board owes a caller nothing
        about which filters it would have accepted, and must reach no parser,
        provider, database, or cache to say so.

        ``observation`` is the request's own already-open observation.  This
        service contributes the facts it learns and the typed failure it
        raises; it never decides that the request is over, because the status
        the caller receives is not settled here.
        """

        with _contributing(observation):
            self._require_published()
            request = parse_board_request(args, settings=self.settings)
            return self._respond(observation, request, if_none_match)

    def respond(
        self,
        request: BoardRequest,
        *,
        if_none_match: str | None = None,
        observation: PendingBoardObservation | None = None,
    ) -> BoardRepresentation:
        """Assemble one board response for an already-parsed read."""

        with _contributing(observation):
            self._require_published()
            return self._respond(observation, request, if_none_match)

    # -- decisions ---------------------------------------------------------

    def _require_published(self) -> None:
        if not self.is_published:
            raise DFSBoardDisabledError()

    def _respond(
        self,
        observation: PendingBoardObservation | None,
        request: BoardRequest,
        if_none_match: str | None,
    ) -> BoardRepresentation:
        """The board itself: retrieved, judged usable, serialized, and tagged."""

        try:
            board = self.comparison_board_service.get_comparisons(
                request.query, filters=request.filters
            )
        except UnreadableComparisonBoardError as unreadable:
            # A read nothing could be published from built no board, so there
            # is nothing here to observe or serialize -- only the evidence the
            # read finished with, which states the same outage in the same
            # sanitized vocabulary a readable one would have.
            evidence = unreadable.board_evidence
            if observation is not None:
                observation.observe(evidence)
            raise self._unavailable(
                evidence.provider_reports, evidence.disabled_providers
            ) from unreadable

        if observation is not None:
            observation.observe(BoardReadEvidence.of(board))

        # Readability is settled before anything is published, and the board
        # service has already declined to refuse an unreadable read as too
        # large, so an outage is always reported as the outage it is.
        if not has_readable_provider(board.provider_reports):
            raise self._unavailable(board.provider_reports, board.disabled_providers)

        payload = serialize_board(board)
        etag = board_etag(payload)
        if _matches(if_none_match, etag):
            return BoardRepresentation(status_code=304, etag=etag)

        return BoardRepresentation(status_code=200, etag=etag, payload=payload)

    @staticmethod
    def _unavailable(
        reports: Any, disabled_providers: Any
    ) -> DFSBoardUnavailableError:
        """One outage, stated the same way whether or not a board was built."""

        return DFSBoardUnavailableError(
            provider_outcomes=[_provider_outcome(report) for report in reports],
            disabled_providers=list(disabled_providers),
        )


@contextmanager
def _contributing(observation: PendingBoardObservation | None) -> Iterator[None]:
    """Let one request's observation see the typed failure this service raised.

    The classification happens once, here, rather than at each raise, and it is
    only ever a contribution: what the caller finally receives, and therefore
    what the request is counted as, is settled where the response is.
    """

    try:
        yield
    except BaseException as error:
        if observation is not None:
            observation.note_refusal(error)
        raise


# -- serialization ---------------------------------------------------------


def serialize_board(board: ComparisonBoard) -> dict[str, Any]:
    """The complete version 1 JSON body for one comparison board.

    Only an actual board can be published.  A read that stated counts without
    retaining what they counted -- an outage refused after retrieval -- is
    :class:`~app.domain.comparisons.BoardReadEvidence`, which is observed
    rather than published, and is refused here rather than serialized into a
    body whose counts nothing in it supports.
    """

    if not isinstance(board, ComparisonBoard):
        raise TypeError("only a comparison board can be published as a board")
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
    "PendingBoardObservation",
    "board_etag",
    "serialize_board",
]
