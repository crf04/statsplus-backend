"""Offline tests for the published DFS Board representation seam.

Everything the route hands a caller is decided here, so this is where the HTTP
outcome, the exact serialization, the entity tag, and the bounded telemetry are
asserted.  The board itself is built from the same offline fakes the comparison
board tests use, so no provider, cache, or database is involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests
from werkzeug.datastructures import MultiDict

from app.config.settings import (
    FeatureSettings,
    ProviderSettings,
    RuntimeSettings,
)
from app.domain.statistics import ScoringPeriod
from app.providers.dfs import (
    CoverageEvidence,
    MarketStatus,
    NBAMarketQuery,
    ProviderSnapshot,
    Selection,
    SelectionModifier,
    SnapshotStatus,
)
from app.errors import InvalidInputError
from app.services.comparison_board import (
    ComparisonBoardTooLargeError,
    UnreadableComparisonBoardError,
)
from app.services.dfs_board_query import BoardRequest
from app.services.dfs_board_response import (
    BOARD_CONTRACT_VERSION,
    DFSBoardDisabledError,
    DFSBoardResponseService,
    DFSBoardUnavailableError,
    PendingBoardObservation,
    board_etag,
    serialize_board,
)
from app.utils.telemetry import BoardRequestEvent
from app.domain.comparisons import ComparisonFilters

from tests.services.test_comparison_board import (
    GENERATED_AT,
    RETRIEVED_AT,
    SEASON,
    _market,
    _service,
    _snapshot,
)


def published_settings(**overrides) -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        features=FeatureSettings(dfs_board_enabled=True),
        providers=ProviderSettings(dfs_enabled_providers=("dabble", "prizepicks")),
        **overrides,
    )


def _ticking_clock():
    """A monotonic clock that advances a quarter second per reading."""

    ticks = [0.0]

    def _tick() -> float:
        ticks[0] += 0.25
        return ticks[0]

    return _tick


class RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[BoardRequestEvent] = []

    def __call__(self, event: BoardRequestEvent) -> None:
        self.events.append(event)


def build_service(comparison_service, *, settings=None):
    return DFSBoardResponseService(
        comparison_service,
        settings=settings or published_settings(),
    )


def build_observation(recorder=None):
    """One request's own observation, on deterministic clocks."""

    return PendingBoardObservation(
        recorder=recorder or RecordingRecorder(),
        clock=lambda: GENERATED_AT,
        monotonic=_ticking_clock(),
    )


def observed(service, call, *, status_code, recorder):
    """Run one request through its whole lifecycle, response included.

    The service contributes facts and failures; the observation is finalized
    from the status the caller would have received, exactly as the route's
    ``after_request`` finalizes it.  Tests state that status themselves so a
    service that quietly changed what it publishes cannot also quietly change
    what was recorded about it.
    """

    observation = build_observation(recorder)
    try:
        return call(observation)
    finally:
        observation.finalize(status_code)


def board_request(**filters) -> BoardRequest:
    return BoardRequest(
        query=NBAMarketQuery(season=SEASON),
        filters=ComparisonFilters(**filters),
    )


def complete_board_service(**kwargs):
    markets = (
        _market(
            market_id="m-1",
            threshold="25.5",
            selections=(
                Selection(
                    selection_id="s-1",
                    label="Higher",
                    direction="higher",
                    modifiers=(
                        SelectionModifier(
                            value=Decimal("1.25"), kind="payout", scope="selection"
                        ),
                    ),
                ),
            ),
        ),
        _market(provider="prizepicks", market_id="m-2", threshold="26.0"),
    )
    return _service(
        [
            _snapshot("dabble", markets[:1]),
            _snapshot("prizepicks", markets[1:]),
        ],
        **kwargs,
    )


# -- publication -----------------------------------------------------------


def test_a_board_is_unpublished_without_the_feature_flag():
    comparison_service, _ = complete_board_service()
    service = build_service(
        comparison_service,
        settings=RuntimeSettings(
            environment="testing",
            providers=ProviderSettings(dfs_enabled_providers=("dabble",)),
        ),
    )

    assert service.is_published is False
    with pytest.raises(DFSBoardDisabledError) as error:
        service.respond(board_request())

    assert error.value.status_code == 404
    assert error.value.code == "dfs_board_disabled"


def test_a_board_is_unpublished_without_an_enabled_provider():
    comparison_service, _ = complete_board_service()
    service = build_service(
        comparison_service,
        settings=RuntimeSettings(
            environment="testing",
            features=FeatureSettings(dfs_board_enabled=True),
        ),
    )

    assert service.is_published is False
    with pytest.raises(DFSBoardDisabledError):
        service.respond(board_request())


def test_a_disabled_board_never_calls_a_provider():
    comparison_service, providers = complete_board_service()
    service = build_service(
        comparison_service,
        settings=RuntimeSettings(environment="testing"),
    )

    with pytest.raises(DFSBoardDisabledError):
        service.respond(board_request())

    assert [provider.calls for provider in providers.values()] == [0, 0]


def test_a_disabled_board_is_refused_before_its_query_is_read():
    """Publication is decided first, so a disabled deployment parses nothing."""

    comparison_service, providers = complete_board_service()
    service = build_service(
        comparison_service,
        settings=RuntimeSettings(environment="testing"),
    )

    with pytest.raises(DFSBoardDisabledError):
        service.respond_to_query(
            MultiDict([("providers", ""), ("athlete_name", "jokic")])
        )

    assert [provider.calls for provider in providers.values()] == [0, 0]


def test_a_published_board_reads_its_query_string_into_typed_filters():
    comparison_service, providers = complete_board_service()
    service = build_service(comparison_service)

    representation = service.respond_to_query(MultiDict([("providers", "dabble")]))

    assert representation.status_code == 200
    assert representation.payload["filters"]["providers"] == ["dabble"]
    assert providers["prizepicks"].calls == 0


def test_an_invalid_query_is_the_shared_invalid_input_failure():
    comparison_service, providers = complete_board_service()
    service = build_service(comparison_service)

    with pytest.raises(InvalidInputError):
        service.respond_to_query(MultiDict([("providers", "")]))

    assert [provider.calls for provider in providers.values()] == [0, 0]


# -- serialization ---------------------------------------------------------


def test_a_served_board_states_version_one_and_exact_decimal_strings():
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    representation = service.respond(board_request())
    payload = representation.payload

    assert representation.status_code == 200
    assert payload["contract_version"] == BOARD_CONTRACT_VERSION == "1"
    assert payload["generated_at"] == GENERATED_AT.isoformat()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["season"] == SEASON
    group = payload["comparison_groups"][0]
    assert group["summary"]["minimum_threshold"] == "25.5"
    assert group["summary"]["maximum_threshold"] == "26.0"
    assert group["summary"]["threshold_spread"] == "0.5"
    assert group["key"]["scoring_period"] == ScoringPeriod.FULL_GAME.value
    assert [member["threshold"] for member in group["members"]] == ["25.5", "26.0"]
    modifier = payload["markets"][0]["selections"][0]["modifiers"][0]
    assert modifier["value"] == "1.25"


def test_a_served_board_preserves_normalized_evidence_and_provenance():
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    payload = service.respond(board_request()).payload

    market = payload["markets"][0]
    assert market["athlete"]["name"] == "Nikola Jokic"
    assert market["event"]["label"] == "DEN @ LAL"
    assert market["statistic"]["label"] == "points"
    assert market["threshold"] == {
        "value": "25.5",
        "unit": "count",
        "original_value": None,
    }
    assert market["comparison_reference"].startswith("cmp_")
    assert market["observation"]["retrieved_at"] == RETRIEVED_AT.isoformat()
    report = payload["provider_reports"][0]
    assert report["provider"] == "dabble"
    assert report["status"] == "complete"
    assert report["coverage"]["is_complete"] is True
    assert report["freshness"] in {"fresh", "stale", None}


def test_catalog_and_provider_freshness_and_disabled_providers_are_explicit():
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    payload = service.respond(board_request()).payload

    availability = payload["comparison_availability"]
    assert availability["available"] is True
    assert [catalog["catalog"] for catalog in availability["catalogs"]] == [
        "athlete_catalog",
        "event_catalog",
    ]
    assert all(
        catalog["last_success_at"].endswith("+00:00")
        for catalog in availability["catalogs"]
    )
    assert payload["disabled_providers"] == ["underdog"]


def test_collections_are_deterministically_ordered():
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    payload = service.respond(board_request()).payload

    assert [market["provider"] for market in payload["markets"]] == [
        "dabble",
        "prizepicks",
    ]
    assert [report["provider"] for report in payload["provider_reports"]] == [
        "dabble",
        "prizepicks",
    ]


def test_the_serializer_refuses_a_value_it_cannot_state_exactly():
    class Opaque:
        pass

    from app.services import dfs_board_response

    with pytest.raises(TypeError):
        dfs_board_response._encode(Opaque())


# -- entity tags -----------------------------------------------------------


def test_an_equivalent_board_revalidates_as_not_modified():
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)
    first = service.respond(board_request())

    second = service.respond(board_request(), if_none_match=f'W/"{first.etag}"')

    assert second.status_code == 304
    assert second.payload is None
    assert second.etag == first.etag


def test_a_conditional_request_accepts_a_bare_or_wildcard_tag():
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)
    first = service.respond(board_request())

    assert service.respond(board_request(), if_none_match=first.etag).status_code == 304
    assert service.respond(board_request(), if_none_match="*").status_code == 304


def test_an_unrelated_tag_serves_the_board_again():
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)
    service.respond(board_request())

    representation = service.respond(board_request(), if_none_match='W/"stale-tag"')

    assert representation.status_code == 200


def test_the_tag_ignores_the_instant_of_observation():
    payload = {
        "generated_at": "2026-08-09T20:00:30+00:00",
        "markets": [{"observation": {"observed_at": "x", "age_seconds": "30"}}],
    }
    later = {
        "generated_at": "2026-08-09T21:00:30+00:00",
        "markets": [{"observation": {"observed_at": "y", "age_seconds": "3630"}}],
    }

    assert board_etag(payload) == board_etag(later)


def test_the_tag_changes_when_a_stated_fact_changes():
    comparison_service, _ = complete_board_service()
    other_service, _ = complete_board_service()
    served = build_service(comparison_service).respond(board_request())

    narrowed = build_service(other_service).respond(board_request(providers=("dabble",)))

    assert narrowed.etag != served.etag


# -- HTTP outcomes ---------------------------------------------------------


def test_an_empty_complete_snapshot_is_a_usable_board():
    comparison_service, _ = _service(
        [
            ProviderSnapshot(
                provider="dabble",
                status=SnapshotStatus.COMPLETE,
                markets=(),
                coverage=CoverageEvidence(
                    pagination_complete=True, fanout_complete=True
                ),
                retrieved_at=RETRIEVED_AT,
            )
        ]
    )
    service = build_service(comparison_service)

    representation = service.respond(board_request())

    assert representation.status_code == 200
    assert representation.payload["comparison_groups"] == []
    assert representation.payload["markets"] == []
    assert representation.payload["market_count"] == 0


def test_a_partial_and_a_permitted_stale_snapshot_still_serve_the_board():
    partial = ProviderSnapshot(
        provider="prizepicks",
        status=SnapshotStatus.PARTIAL,
        markets=(_market(provider="prizepicks", market_id="m-2", threshold="26.0"),),
        coverage=CoverageEvidence(
            fetched_count=1,
            eligible_count=1,
            normalized_count=1,
            pagination_complete=False,
        ),
        retrieved_at=RETRIEVED_AT,
    )
    stale = _snapshot(
        "dabble",
        (_market(market_id="m-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=900),
    )
    comparison_service, _ = _service([stale, partial])
    service = build_service(comparison_service)

    payload = service.respond(board_request()).payload

    statuses = {report["provider"]: report["status"] for report in payload["provider_reports"]}
    assert statuses == {"dabble": "complete", "prizepicks": "partial"}
    freshness = {
        report["provider"]: report["freshness"] for report in payload["provider_reports"]
    }
    assert freshness == {"dabble": "stale", "prizepicks": "fresh"}
    assert payload["comparison_groups"][0]["summary"]["freshness"] == "mixed"


def test_no_usable_snapshot_is_a_sanitized_service_unavailable():
    comparison_service, providers = complete_board_service()
    providers["dabble"].failure = requests.exceptions.Timeout("host=secret.example")
    providers["prizepicks"].failure = requests.exceptions.HTTPError("token=secret")
    service = build_service(comparison_service)

    with pytest.raises(DFSBoardUnavailableError) as error:
        service.respond(board_request())

    details = error.value.public_details
    assert error.value.status_code == 503
    assert error.value.code == "provider_unavailable"
    assert details["contract_version"] == "1"
    assert [outcome["provider"] for outcome in details["provider_outcomes"]] == [
        "dabble",
        "prizepicks",
    ]
    assert {outcome["status"] for outcome in details["provider_outcomes"]} == {"failed"}
    assert {outcome["reason"] for outcome in details["provider_outcomes"]} <= {
        "timeout",
        "upstream_error",
    }
    assert "secret" not in str(details)
    assert details["disabled_providers"] == ["underdog"]


def test_a_snapshot_past_the_stale_ceiling_is_not_a_usable_board():
    """A retrieval nothing on the board may read is an outage, not a 200.

    The provider succeeded, so its status is ``complete``; but every market it
    carried is excluded as ``stale_snapshot``, so the caller would receive a
    board stating nothing at all.
    """

    beyond = _snapshot(
        "dabble",
        (_market(market_id="m-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    comparison_service, _ = _service([beyond])
    service = build_service(comparison_service)

    with pytest.raises(DFSBoardUnavailableError) as error:
        service.respond(board_request())

    outcome = error.value.public_details["provider_outcomes"][0]
    assert error.value.status_code == 503
    assert outcome["status"] == "complete"
    assert outcome["freshness"] is None
    assert outcome["future_observation"] is False


def test_a_snapshot_at_the_exact_stale_ceiling_is_still_usable():
    """The maximum permitted age is inclusive here, as everywhere else."""

    ceiling = _snapshot(
        "dabble",
        (_market(market_id="m-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=1800),
    )
    comparison_service, _ = _service([ceiling])
    service = build_service(comparison_service)

    representation = service.respond(board_request())

    assert representation.status_code == 200
    assert representation.payload["provider_reports"][0]["freshness"] == "stale"


def test_a_future_dated_snapshot_alone_is_not_a_usable_board():
    future = _snapshot(
        "dabble",
        (_market(market_id="m-1"),),
        retrieved_at=GENERATED_AT + timedelta(seconds=60),
    )
    comparison_service, _ = _service([future])
    service = build_service(comparison_service)

    with pytest.raises(DFSBoardUnavailableError) as error:
        service.respond(board_request())

    outcome = error.value.public_details["provider_outcomes"][0]
    assert outcome["future_observation"] is True
    assert outcome["freshness"] is None


def test_one_readable_provider_publishes_the_board_beside_an_unreadable_one():
    beyond = _snapshot(
        "dabble",
        (_market(market_id="m-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    fresh = _snapshot(
        "prizepicks",
        (_market(provider="prizepicks", market_id="m-2", threshold="26.0"),),
    )
    comparison_service, _ = _service([beyond, fresh])
    service = build_service(comparison_service)

    representation = service.respond(board_request())

    assert representation.status_code == 200
    assert [
        entry["reason"] for entry in representation.payload["unresolved_markets"]
    ] == ["stale_snapshot"]


def test_an_oversized_board_is_refused_with_its_observed_count():
    comparison_service, _ = complete_board_service(max_markets=1)
    service = build_service(comparison_service)

    with pytest.raises(ComparisonBoardTooLargeError) as error:
        service.respond(board_request())

    assert error.value.status_code == 400
    assert error.value.code == "board_too_large"
    assert error.value.public_details == {
        "observed_market_count": 2,
        "market_limit": 1,
        "supported_filters": [
            "canonical_athlete_ids",
            "canonical_event_ids",
            "canonical_statistic_ids",
            "market_statuses",
            "providers",
        ],
    }


def test_an_oversized_read_no_provider_could_be_read_from_is_a_sanitized_503():
    """Readability outranks size: an outage is never reported as a too-large board."""

    beyond = _snapshot(
        "dabble",
        (
            _market(market_id="m-1", threshold="25.5"),
            _market(market_id="m-2", threshold="26.5"),
        ),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    comparison_service, _ = _service([beyond], max_markets=1)
    service = build_service(comparison_service)

    with pytest.raises(DFSBoardUnavailableError) as error:
        service.respond(board_request())

    outcome = error.value.public_details["provider_outcomes"][0]
    assert error.value.status_code == 503
    assert outcome["status"] == "complete"
    assert outcome["freshness"] is None
    assert outcome["future_observation"] is False


def test_an_oversized_future_dated_read_is_a_sanitized_503():
    future = _snapshot(
        "dabble",
        (
            _market(market_id="m-1", threshold="25.5"),
            _market(market_id="m-2", threshold="26.5"),
        ),
        retrieved_at=GENERATED_AT + timedelta(seconds=60),
    )
    comparison_service, _ = _service([future], max_markets=1)
    service = build_service(comparison_service)

    with pytest.raises(DFSBoardUnavailableError) as error:
        service.respond(board_request())

    assert error.value.public_details["provider_outcomes"][0][
        "future_observation"
    ] is True


def test_an_unreadable_read_never_reaches_the_serializer():
    """The refusal carries evidence, not a board, so nothing can serialize it."""

    beyond = _snapshot(
        "dabble",
        (
            _market(market_id="m-1", threshold="25.5"),
            _market(market_id="m-2", threshold="26.5"),
        ),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    comparison_service, _ = _service([beyond], max_markets=1)

    with pytest.raises(UnreadableComparisonBoardError) as error:
        comparison_service.get_comparisons(NBAMarketQuery(season=SEASON))

    with pytest.raises(TypeError, match="comparison board"):
        serialize_board(error.value.board_evidence)


def test_an_unreadable_oversized_read_is_observed_from_what_it_observed():
    """The outage is counted with the read that happened, not an empty one."""

    recorder = RecordingRecorder()
    beyond = _snapshot(
        "dabble",
        (
            _market(market_id="m-1", threshold="25.5"),
            _market(market_id="m-2", threshold="26.5"),
        ),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    comparison_service, _ = _service([beyond], max_markets=1)
    service = build_service(comparison_service)

    with pytest.raises(DFSBoardUnavailableError):
        observed(
            service,
            lambda observation: service.respond(
                board_request(), observation=observation
            ),
            status_code=503,
            recorder=recorder,
        )

    event = recorder.events[0]
    assert (event.outcome, event.status_code) == ("unavailable", 503)
    assert event.market_count == 2
    assert event.unresolved_count == 2
    assert event.group_count == 0
    assert event.comparison_availability == "available"
    assert event.provider_status_counts == (("complete", 1),)
    assert event.freshness_counts == (("unknown", 1),)
    assert event.disabled_provider_count == 2


def test_an_oversized_read_with_one_readable_provider_is_still_too_large():
    beyond = _snapshot(
        "dabble",
        (_market(market_id="m-1"),),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    fresh = _snapshot(
        "prizepicks",
        (_market(provider="prizepicks", market_id="m-2", threshold="26.0"),),
    )
    comparison_service, _ = _service([beyond, fresh], max_markets=1)
    service = build_service(comparison_service)

    with pytest.raises(ComparisonBoardTooLargeError) as error:
        service.respond(board_request())

    assert error.value.status_code == 400
    assert error.value.public_details["observed_market_count"] == 2


def test_an_oversized_outage_is_observed_as_one_unavailable_request():
    """Telemetry counts the outage the caller received, with what was observed."""

    recorder = RecordingRecorder()
    beyond = _snapshot(
        "dabble",
        (
            _market(market_id="m-1", threshold="25.5"),
            _market(market_id="m-2", threshold="26.5"),
        ),
        retrieved_at=GENERATED_AT - timedelta(seconds=1801),
    )
    comparison_service, _ = _service([beyond], max_markets=1)
    service = build_service(comparison_service)

    with pytest.raises(DFSBoardUnavailableError):
        observed(
            service,
            lambda observation: service.respond(
                board_request(), observation=observation
            ),
            status_code=503,
            recorder=recorder,
        )

    event = recorder.events[0]
    assert (event.outcome, event.status_code) == ("unavailable", 503)
    assert event.market_count == 2
    assert event.unresolved_count == 2
    assert event.group_count == 0
    assert event.provider_status_counts == (("complete", 1),)
    assert event.freshness_counts == (("unknown", 1),)


def test_an_empty_complete_read_under_a_tight_ceiling_is_still_published():
    empty = ProviderSnapshot(
        provider="dabble",
        status=SnapshotStatus.COMPLETE,
        markets=(),
        coverage=CoverageEvidence(pagination_complete=True, fanout_complete=True),
        retrieved_at=RETRIEVED_AT,
    )
    comparison_service, _ = _service([empty], max_markets=1)
    service = build_service(comparison_service)

    representation = service.respond(board_request())

    assert representation.status_code == 200
    assert representation.payload["market_count"] == 0


# -- telemetry -------------------------------------------------------------


def test_a_served_board_records_bounded_labels_only():
    recorder = RecordingRecorder()
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    observed(
        service,
        lambda observation: service.respond(board_request(), observation=observation),
        status_code=200,
        recorder=recorder,
    )

    event = recorder.events[0]
    assert event.outcome == "served"
    assert event.status_code == 200
    assert event.comparison_availability == "available"
    assert event.provider_status_counts == (("complete", 2),)
    assert event.failure_reason_counts == ()
    assert event.freshness_counts == (("fresh", 2),)
    assert event.cache_counts == (("unset", 2),)
    assert event.group_count == 1
    assert event.market_count == 2
    assert event.unresolved_count == 0
    assert event.disabled_provider_count == 1
    assert event.duration_ms >= 0


def test_every_outcome_is_observable():
    recorder = RecordingRecorder()
    comparison_service, providers = complete_board_service()
    service = build_service(comparison_service)
    first = observed(
        service,
        lambda observation: service.respond(board_request(), observation=observation),
        status_code=200,
        recorder=recorder,
    )
    observed(
        service,
        lambda observation: service.respond(
            board_request(), if_none_match=first.etag, observation=observation
        ),
        status_code=304,
        recorder=recorder,
    )

    unavailable_service, unavailable_providers = complete_board_service()
    for provider in unavailable_providers.values():
        provider.failure = requests.exceptions.Timeout()
    unavailable = build_service(unavailable_service)
    with pytest.raises(DFSBoardUnavailableError):
        observed(
            unavailable,
            lambda observation: unavailable.respond(
                board_request(), observation=observation
            ),
            status_code=503,
            recorder=recorder,
        )

    oversized_service, _ = complete_board_service(max_markets=1)
    oversized = build_service(oversized_service)
    with pytest.raises(ComparisonBoardTooLargeError):
        observed(
            oversized,
            lambda observation: oversized.respond(
                board_request(), observation=observation
            ),
            status_code=400,
            recorder=recorder,
        )

    assert [event.outcome for event in recorder.events] == [
        "served",
        "not_modified",
        "unavailable",
        "too_large",
    ]
    assert [event.status_code for event in recorder.events] == [200, 304, 503, 400]
    assert recorder.events[2].failure_reason_counts == (("timeout", 2),)
    assert recorder.events[3].comparison_availability == "available"


def test_a_refused_board_is_observed_with_the_read_it_completed():
    """The ceiling stops a board, not the retrieval that had already happened."""

    recorder = RecordingRecorder()
    oversized_service, _ = complete_board_service(max_markets=1)
    service = build_service(oversized_service)

    with pytest.raises(ComparisonBoardTooLargeError):
        observed(
            service,
            lambda observation: service.respond(
                board_request(), observation=observation
            ),
            status_code=400,
            recorder=recorder,
        )

    event = recorder.events[0]
    assert (event.outcome, event.status_code) == ("too_large", 400)
    assert event.market_count == 2
    assert event.comparison_availability == "available"
    assert event.provider_status_counts == (("complete", 2),)
    assert event.freshness_counts == (("fresh", 2),)
    assert event.cache_counts == (("unset", 2),)
    assert event.disabled_provider_count == 1


def test_a_disabled_request_is_observed_as_one_disabled_event():
    recorder = RecordingRecorder()
    comparison_service, _ = complete_board_service()
    service = build_service(
        comparison_service,
        settings=RuntimeSettings(environment="testing"),
    )

    with pytest.raises(DFSBoardDisabledError):
        observed(
            service,
            lambda observation: service.respond_to_query(
                MultiDict(), observation=observation
            ),
            status_code=404,
            recorder=recorder,
        )

    assert [(event.outcome, event.status_code) for event in recorder.events] == [
        ("disabled", 404)
    ]
    assert recorder.events[0].comparison_availability == "unknown"


def test_an_invalid_request_is_observed_as_one_invalid_event():
    recorder = RecordingRecorder()
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    with pytest.raises(InvalidInputError):
        observed(
            service,
            lambda observation: service.respond_to_query(
                MultiDict([("season", "")]), observation=observation
            ),
            status_code=400,
            recorder=recorder,
        )

    assert [(event.outcome, event.status_code) for event in recorder.events] == [
        ("invalid", 400)
    ]


def test_an_unexpected_failure_is_observed_as_one_error_event():
    class ExplodingBoard:
        def get_comparisons(self, query, *, filters=None):
            raise RuntimeError("host=secret.example")

    recorder = RecordingRecorder()
    service = build_service(ExplodingBoard())

    with pytest.raises(RuntimeError):
        observed(
            service,
            lambda observation: service.respond_to_query(
                MultiDict(), observation=observation
            ),
            status_code=500,
            recorder=recorder,
        )

    assert [(event.outcome, event.status_code) for event in recorder.events] == [
        ("error", 500)
    ]


def test_the_response_decides_the_outcome_a_service_only_intended():
    """A board that was assembled but never published is not a served board."""

    recorder = RecordingRecorder()
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    observation = build_observation(recorder)
    service.respond(board_request(), observation=observation)
    # The representation never became a response: the rendering after it raised.
    observation.finalize(500)

    event = recorder.events[0]
    assert (event.outcome, event.status_code) == ("error", 500)
    assert event.market_count == 2
    assert event.provider_status_counts == (("complete", 2),)


def test_one_request_is_exactly_one_event_however_often_it_is_finalized():
    recorder = RecordingRecorder()
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    observation = build_observation(recorder)
    service.respond(board_request(), observation=observation)
    observation.finalize(200)
    observation.finalize(500)

    assert [event.outcome for event in recorder.events] == ["served"]


@pytest.mark.parametrize(
    ("args", "status_code"),
    [
        (MultiDict(), 200),
        (MultiDict([("providers", "")]), 400),
        (MultiDict([("providers", "dabble")]), 200),
    ],
)
def test_one_request_is_exactly_one_event(args, status_code):
    recorder = RecordingRecorder()
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    try:
        observed(
            service,
            lambda observation: service.respond_to_query(
                args, observation=observation
            ),
            status_code=status_code,
            recorder=recorder,
        )
    except InvalidInputError:
        pass

    assert len(recorder.events) == 1


def test_a_status_the_board_cannot_state_is_recorded_as_the_error_it_is():
    """No response invents a label; an unstatable status is a defect, not a signal."""

    recorder = RecordingRecorder()
    observation = build_observation(recorder)

    observation.finalize(409)

    assert [(event.outcome, event.status_code) for event in recorder.events] == [
        ("error", 500)
    ]


def test_a_request_without_an_observation_records_nothing():
    """Telemetry is the request's, not the service's: no observation, no event."""

    recorder = RecordingRecorder()
    comparison_service, _ = complete_board_service()

    build_service(comparison_service).respond(board_request())

    assert recorder.events == []


def test_telemetry_labels_never_carry_identities():
    recorder = RecordingRecorder()
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    observed(
        service,
        lambda observation: service.respond(board_request(), observation=observation),
        status_code=200,
        recorder=recorder,
    )

    labels = {
        label
        for event in recorder.events
        for counts in (
            event.provider_status_counts,
            event.failure_reason_counts,
            event.freshness_counts,
            event.cache_counts,
        )
        for label, _count in counts
    }
    assert labels <= {
        "complete",
        "partial",
        "failed",
        "timeout",
        "deadline_exceeded",
        "rate_limited",
        "access_denied",
        "upstream_error",
        "malformed_response",
        "fresh",
        "stale",
        "unknown",
        "hit",
        "miss",
        "disabled",
        "error",
        "unset",
    }


# -- filters ---------------------------------------------------------------


def test_a_provider_filter_narrows_the_board_centrally():
    comparison_service, providers = complete_board_service()
    service = build_service(comparison_service)

    payload = service.respond(board_request(providers=("dabble",))).payload

    assert [market["provider"] for market in payload["markets"]] == ["dabble"]
    assert payload["filters"]["providers"] == ["dabble"]
    assert providers["prizepicks"].calls == 0


def test_a_market_status_filter_is_applied_before_serialization():
    comparison_service, _ = complete_board_service()
    service = build_service(comparison_service)

    payload = service.respond(
        board_request(market_statuses=(MarketStatus.SUSPENDED,))
    ).payload

    assert payload["markets"] == []
    assert payload["filters"]["market_statuses"] == ["suspended"]


def test_serialize_board_is_reusable_outside_a_request():
    comparison_service, _ = complete_board_service()
    board = comparison_service.get_comparisons(NBAMarketQuery(season=SEASON))

    payload = serialize_board(board)

    assert payload["contract_version"] == "1"
    assert isinstance(payload["generated_at"], str)
    assert datetime.fromisoformat(payload["generated_at"]).tzinfo is timezone.utc


def test_a_published_count_is_an_exact_integer_never_a_boolean():
    """A serialized count is a JSON number, never ``true`` or ``false``."""

    comparison_service, _ = complete_board_service()
    board = comparison_service.get_comparisons(NBAMarketQuery(season=SEASON))

    payload = serialize_board(board)

    assert type(payload["market_count"]) is int
    for group in payload["comparison_groups"]:
        assert type(group["summary"]["market_count"]) is int
        assert type(group["summary"]["provider_count"]) is int
