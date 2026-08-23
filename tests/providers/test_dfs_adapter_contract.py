"""The shared offline compliance suite every registered DFS provider passes.

Admission is decided here and nowhere else.  The suite is parameterized from
:mod:`app.providers.registry`, so a provider that is registered but cannot
prove the shared behavior fails, and a provider that proves it needs no
archive, Player Pool, or closing-set code of its own.

Every case runs against recorded payloads: no test in this module opens a
socket or reads a credential.  The live contract tests stay opt-in in
``tests/live``.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import pytest
import requests
from sqlalchemy import create_engine, select

from app.domain.comparisons import market_reference
from app.domain.market_content import NORMALIZED_DECIMAL_PLACE_LIMIT
from app.errors import ProviderUnavailableError
from app.migrations import run_migrations
from app.models.projection_archive import (
    ClosingProjectionMembership,
    LatestPlayerProjection,
    ProjectionObservation,
)
from app.providers.dabble import DabbleAdapter
from app.providers.dfs import (
    MarketStatus,
    MarketVariant,
    ModifierKind,
    NBAMarketQuery,
    PriceKind,
    PriceScope,
    ProviderSnapshot,
    ProviderSnapshotProvider,
    RetrievalContext,
    ScoringPeriod,
    SnapshotStatus,
)
from app.providers.prizepicks import PrizePicksAdapter
from app.providers.registry import (
    PRIZEPICKS_ENTRY_PAYOUT_TABLES,
    dfs_provider_names,
    dfs_provider_registration,
    registered_dfs_provider,
)
from app.providers.underdog import UnderdogAdapter
from app.services.dfs_snapshot_cache import (
    deserialize_provider_snapshot,
    serialize_provider_snapshot,
)
from app.services.projection_archive import (
    DEFAULT_PROJECTION_ARCHIVE_MAX_DOCUMENT_BYTES,
    DEFAULT_PROJECTION_ARCHIVE_MAX_MARKETS,
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
    ProjectionRecordingService,
)
from app.services.statistic_catalog import StatisticCatalog, StatisticResolver
from tests.providers.fourth_provider import (
    FOURTH_PROVIDER_NAME,
    FourthAdapter,
    fourth_registration,
    recorded_board,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"
SEASON = "2025-26"
QUERY = NBAMarketQuery(season=SEASON)
OBSERVED_AT = datetime(2026, 8, 9, 20, tzinfo=timezone.utc)


class RecordedResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class RecordedSession:
    """Replay recorded payloads, or raise the recorded transport failure."""

    def __init__(self, *payloads: object) -> None:
        self._responses = [
            payload
            if isinstance(payload, BaseException)
            else RecordedResponse(payload)
            for payload in payloads
        ]
        self._lock = Lock()
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> RecordedResponse:
        del url, kwargs
        with self._lock:
            response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _load(provider: str, name: str) -> Any:
    return json.loads((FIXTURES / provider / name).read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class RecordedEvidence:
    """The recorded retrievals one provider is admitted on.

    Every registered provider must be able to demonstrate each of these; the
    suite refuses to run for a provider that cannot, which is what makes the
    registry the admission gate rather than a list of names.
    """

    complete: Callable[[], ProviderSnapshotProvider]
    empty: Callable[[], ProviderSnapshotProvider]
    partial: Callable[[], ProviderSnapshotProvider]
    failing: Callable[[], ProviderSnapshotProvider]


def _dabble_payloads() -> tuple[Any, Any, Any]:
    return (
        _load("dabble", "competitions.valid.json"),
        _load("dabble", "fixtures.valid.json"),
        _load("dabble", "conformance.fixture_details.json"),
    )


def _clock() -> datetime:
    """One recorded retrieval time, so a replay is byte-identical."""

    return OBSERVED_AT


def _dabble(*, mutate: Callable[[Any], None] | None = None) -> DabbleAdapter:
    competitions, fixtures, details = deepcopy(_dabble_payloads())
    if mutate is not None:
        mutate(details)
    return DabbleAdapter(
        session=RecordedSession(competitions, fixtures, details), now=_clock
    )


def _dabble_empty() -> DabbleAdapter:
    def drop_props(details: Any) -> None:
        details["sportFixtureDetail"]["playerProps"] = []

    return _dabble(mutate=drop_props)


def _dabble_partial() -> DabbleAdapter:
    def add_malformed(details: Any) -> None:
        props = details["sportFixtureDetail"]["playerProps"]
        broken = deepcopy(props[0])
        broken["marketId"] = "market-broken"
        broken["selectionId"] = "selection-broken"
        broken["value"] = "not-a-number"
        props.append(broken)

    return _dabble(mutate=add_malformed)


def _dabble_failing() -> DabbleAdapter:
    return DabbleAdapter(
        session=RecordedSession(requests.ConnectionError("recorded failure")),
        now=_clock,
    )


def _prizepicks(payload: Any = None) -> PrizePicksAdapter:
    return PrizePicksAdapter(
        session=RecordedSession(
            payload
            if payload is not None
            else _load("prizepicks", "conformance.projections.json")
        ),
        entry_payout_tables=PRIZEPICKS_ENTRY_PAYOUT_TABLES,
        now=_clock,
    )


def _prizepicks_empty() -> PrizePicksAdapter:
    payload = _load("prizepicks", "conformance.projections.json")
    payload["data"] = []
    return _prizepicks(payload)


def _prizepicks_partial() -> PrizePicksAdapter:
    payload = _load("prizepicks", "conformance.projections.json")
    broken = deepcopy(payload["data"][0])
    broken["id"] = "projection-broken"
    broken["attributes"]["line_score"] = "not-a-number"
    payload["data"].append(broken)
    return _prizepicks(payload)


def _prizepicks_failing() -> PrizePicksAdapter:
    return PrizePicksAdapter(
        session=RecordedSession(requests.ConnectionError("recorded failure")),
        now=_clock,
    )


def _underdog(payload: Any = None) -> UnderdogAdapter:
    return UnderdogAdapter(
        session=RecordedSession(
            payload
            if payload is not None
            else _load("underdog", "conformance.over_under_lines.json")
        ),
        now=_clock,
    )


def _underdog_empty() -> UnderdogAdapter:
    payload = _load("underdog", "conformance.over_under_lines.json")
    payload["over_under_lines"] = []
    return _underdog(payload)


def _underdog_partial() -> UnderdogAdapter:
    payload = _load("underdog", "conformance.over_under_lines.json")
    broken = deepcopy(payload["over_under_lines"][0])
    broken["id"] = "line-broken"
    broken["stat_value"] = "not-a-number"
    payload["over_under_lines"].append(broken)
    return _underdog(payload)


def _underdog_failing() -> UnderdogAdapter:
    return UnderdogAdapter(
        session=RecordedSession(requests.ConnectionError("recorded failure")),
        now=_clock,
    )


def _fourth_empty() -> FourthAdapter:
    return FourthAdapter(payload={"board": []})


def _fourth_partial() -> FourthAdapter:
    payload = recorded_board()
    broken = deepcopy(payload["board"][0])
    broken["id"] = "market-broken"
    broken["line"] = "not-a-number"
    payload["board"].append(broken)
    return FourthAdapter(payload=payload)


def _fourth_failing() -> FourthAdapter:
    return FourthAdapter(
        fail_with=ProviderUnavailableError("Fourth could not be reached.")
    )


RECORDED_EVIDENCE: dict[str, RecordedEvidence] = {
    "dabble": RecordedEvidence(
        complete=_dabble,
        empty=_dabble_empty,
        partial=_dabble_partial,
        failing=_dabble_failing,
    ),
    "prizepicks": RecordedEvidence(
        complete=_prizepicks,
        empty=_prizepicks_empty,
        partial=_prizepicks_partial,
        failing=_prizepicks_failing,
    ),
    "underdog": RecordedEvidence(
        complete=_underdog,
        empty=_underdog_empty,
        partial=_underdog_partial,
        failing=_underdog_failing,
    ),
    FOURTH_PROVIDER_NAME: RecordedEvidence(
        complete=FourthAdapter,
        empty=_fourth_empty,
        partial=_fourth_partial,
        failing=_fourth_failing,
    ),
}


@pytest.fixture(autouse=True)
def _admit_the_recorded_fourth_provider():
    """Admit the recorded fourth provider for the duration of one test."""

    with registered_dfs_provider(fourth_registration()):
        yield


def _conformance_providers() -> tuple[str, ...]:
    """Parameterize from the registry, with the fourth provider admitted."""

    with registered_dfs_provider(fourth_registration()):
        return dfs_provider_names()


PROVIDERS = _conformance_providers()


def _context() -> RetrievalContext:
    return RetrievalContext(deadline=datetime(2030, 1, 1, tzinfo=timezone.utc))


def _snapshot(provider: str, case: str = "complete") -> ProviderSnapshot:
    adapter = getattr(RECORDED_EVIDENCE[provider], case)()
    assert isinstance(adapter, ProviderSnapshotProvider)
    return adapter.get_snapshot(NBAMarketQuery(), _context())


def _canonical_game_id(provider_id: str | None) -> str:
    return f"00225{abs(hash(provider_id)) % 100000:05d}"


def _catalog(tmp_path) -> StatisticCatalog:
    """The reviewed statistic catalog, with the fourth provider's labels.

    A provider's statistic labels are catalog configuration, not adapter code,
    so onboarding one means adding its labels here -- and nothing else.
    """

    import yaml

    data = yaml.safe_load(StatisticCatalog.DEFAULT_PATH.read_text(encoding="utf-8"))
    for statistic in data["statistics"]:
        mappings = statistic["provider_mappings"]
        if "prizepicks" in mappings:
            mappings[FOURTH_PROVIDER_NAME] = list(mappings["prizepicks"])
    path = tmp_path / "statistic_catalog.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return StatisticCatalog.load(path)


def _governed(
    snapshot: ProviderSnapshot, catalog: StatisticCatalog
) -> ProviderSnapshot:
    """Resolve one provider's markets the way every provider's are resolved.

    The board assigns canonical athlete, event, and statistic identities above
    the adapter seam.  Doing it here, identically for every provider, is what
    lets the archive assertions below prove that nothing downstream is
    provider-specific.
    """

    resolver = StatisticResolver(catalog)
    markets = []
    for market in snapshot.markets:
        athlete = market.athlete
        event = market.event
        markets.append(
            replace(
                market,
                athlete=replace(
                    athlete,
                    canonical_id=2544,
                    name=athlete.name or "Recorded Player",
                    team=replace(
                        athlete.team, canonical_id=1610612747
                    )
                    if athlete.team is not None
                    else None,
                ),
                event=replace(
                    event, canonical_id=_canonical_game_id(event.provider_id)
                ),
                team=replace(market.team, canonical_id=1610612747)
                if market.team is not None
                else None,
                statistic_match=resolver.resolve_market(market),
            )
        )
    return replace(snapshot, markets=tuple(markets))


def _archive(tmp_path, provider: str, catalog: StatisticCatalog):
    engine = create_engine(f"sqlite:///{tmp_path / f'{provider}-compliance.sqlite3'}")
    run_migrations(engine)
    archive = ProjectionArchive(engine, catalog)
    recorder = ProjectionRecordingService(
        archive,
        ProjectionArchiveReadScope(provider=provider, query=QUERY),
    )
    return engine, archive, recorder


def test_recorded_evidence_exists_for_every_registered_provider():
    assert set(RECORDED_EVIDENCE) == set(dfs_provider_names())
    assert FOURTH_PROVIDER_NAME in dfs_provider_names()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_complete_recorded_retrieval_satisfies_the_shared_contract(provider: str):
    snapshot = _snapshot(provider)

    assert isinstance(snapshot, ProviderSnapshot)
    assert snapshot.provider == provider
    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.coverage.is_complete
    assert snapshot.coverage.fetched_count == (
        snapshot.coverage.eligible_count + snapshot.coverage.skipped_count
    )
    assert snapshot.coverage.normalized_count >= len(snapshot.markets)
    assert snapshot.retrieved_at.tzinfo is timezone.utc
    assert snapshot.markets
    assert all(market.provider == provider for market in snapshot.markets)
    assert any(
        market.status is MarketStatus.AVAILABLE
        and market.variant is MarketVariant.STANDARD
        and market.scoring_period is ScoringPeriod.FULL_GAME
        and market.is_priced
        for market in snapshot.markets
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_empty_recorded_board_is_complete_and_states_no_markets(provider: str):
    snapshot = _snapshot(provider, "empty")

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.markets == ()
    assert snapshot.coverage.is_complete


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_omitted_record_makes_the_retrieval_partial_with_typed_coverage(
    provider: str,
):
    snapshot = _snapshot(provider, "partial")

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.markets
    assert not snapshot.coverage.is_complete
    assert snapshot.coverage.skipped_count >= 1
    assert snapshot.coverage.skipped_reasons
    assert snapshot.coverage.diagnostic_details
    # The omission is accounted for, never silently dropped.
    assert snapshot.coverage.fetched_count == (
        snapshot.coverage.eligible_count + snapshot.coverage.skipped_count
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_recorded_upstream_failure_is_one_typed_unavailable_error(provider: str):
    adapter = RECORDED_EVIDENCE[provider].failing()

    with pytest.raises(ProviderUnavailableError):
        adapter.get_snapshot(NBAMarketQuery(), _context())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_one_recorded_board_always_normalizes_to_the_same_market_identity(
    provider: str,
):
    first = _snapshot(provider)
    second = _snapshot(provider)

    assert [market_reference(market) for market in first.markets] == [
        market_reference(market) for market in second.markets
    ]
    assert serialize_provider_snapshot(first, QUERY) == serialize_provider_snapshot(
        second, QUERY
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_selection_states_one_comparable_price_in_a_closed_vocabulary(
    provider: str,
):
    snapshot = _snapshot(provider)
    selections = [
        selection for market in snapshot.markets for selection in market.selections
    ]

    assert selections
    for market in snapshot.markets:
        assert isinstance(market.variant, MarketVariant)
        assert isinstance(market.status, MarketStatus)
        assert isinstance(market.scoring_period, ScoringPeriod)
        for selection in market.selections:
            assert isinstance(selection.price_kind, PriceKind)
            assert isinstance(selection.price_scope, PriceScope)
            assert (selection.price_value is None) is (
                selection.price_kind is PriceKind.UNPRICED
            )
            for modifier in selection.modifiers:
                assert modifier.kind in {kind.value for kind in ModifierKind}
        # Every side of one market states one price form.
        priced = {
            (selection.price_kind, selection.price_scope)
            for selection in market.selections
            if selection.is_priced
        }
        assert len(priced) <= 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_canonical_archive_document_round_trips_byte_for_byte(provider: str):
    snapshot = _snapshot(provider)

    document = serialize_provider_snapshot(snapshot, QUERY)
    decoded = deserialize_provider_snapshot(document, expected_query=QUERY)

    assert serialize_provider_snapshot(decoded, QUERY) == document
    assert [
        (selection.price_kind, selection.price_value, selection.price_scope)
        for market in decoded.markets
        for selection in market.selections
    ] == [
        (selection.price_kind, selection.price_value, selection.price_scope)
        for market in snapshot.markets
        for selection in market.selections
    ]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_recorded_board_stays_inside_the_archive_numeric_and_document_bounds(
    provider: str,
):
    snapshot = _snapshot(provider)
    document = serialize_provider_snapshot(snapshot, QUERY)

    assert len(snapshot.markets) <= DEFAULT_PROJECTION_ARCHIVE_MAX_MARKETS
    assert (
        len(document.encode("utf-8")) <= DEFAULT_PROJECTION_ARCHIVE_MAX_DOCUMENT_BYTES
    )
    for market in snapshot.markets:
        numbers = [
            number
            for number in (
                None if market.threshold is None else market.threshold.value,
                *(selection.price_value for selection in market.selections),
                *(
                    modifier.value
                    for selection in market.selections
                    for modifier in selection.modifiers
                ),
            )
            if number is not None
        ]
        assert numbers
        for number in numbers:
            assert isinstance(number, Decimal)
            assert number.is_finite()
            assert -number.adjusted() <= NORMALIZED_DECIMAL_PLACE_LIMIT


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_recorded_board_reaches_latest_projections_pool_and_closing_sets(
    tmp_path, provider: str
):
    catalog = _catalog(tmp_path)
    engine, archive, recorder = _archive(tmp_path, provider, catalog)
    snapshot = _governed(_snapshot(provider), catalog)
    targetable = tuple(
        market
        for market in snapshot.markets
        if market.status is MarketStatus.AVAILABLE
        and market.variant is MarketVariant.STANDARD
        and market.scoring_period is ScoringPeriod.FULL_GAME
        and market.is_priced
    )
    game_id = targetable[0].event.canonical_id

    accepted_at = OBSERVED_AT + timedelta(minutes=1)
    first = recorder.record_snapshot(snapshot, query=QUERY, accepted_at=accepted_at)
    # Recording the same evidence again archives no second copy of it.
    repeat = recorder.record_snapshot(
        snapshot, query=QUERY, accepted_at=accepted_at + timedelta(minutes=1)
    )

    reader = LatestProjectionPlayerPoolReader(
        engine,
        ProjectionArchiveReadScope(provider=provider, query=QUERY),
        clock=lambda: OBSERVED_AT + timedelta(minutes=5),
    )
    pool = reader.get_pool_for_game(season=SEASON, game_id=game_id)

    closing = archive.freeze_closing_projection_set(
        provider=provider,
        query=QUERY,
        canonical_game_id=game_id,
        started_at=OBSERVED_AT + timedelta(hours=1),
        created_at=OBSERVED_AT + timedelta(hours=1),
    )

    with engine.connect() as connection:
        latest = set(
            connection.execute(
                select(LatestPlayerProjection.market_reference)
            ).scalars()
        )
        observed = set(
            connection.execute(
                select(ProjectionObservation.provider_market_id)
            ).scalars()
        )
        memberships = set(
            connection.execute(
                select(ClosingProjectionMembership.market_reference)
            ).scalars()
        )

    assert first.changed is True
    assert repeat.snapshot_id == first.snapshot_id
    assert observed == {market.market_id for market in snapshot.markets}
    assert latest == {market_reference(market) for market in targetable}
    assert pool.freshness["state"] == "live"
    assert pool.players
    assert closing.created is True
    assert memberships == {
        market_reference(market)
        for market in targetable
        if market.event.canonical_id == game_id
    }


def test_a_recorded_period_scoped_market_is_retained_but_excluded_from_targetable(
    tmp_path,
):
    """The Underdog board records an explicit first-half market.

    A period-scoped label resolves to its specific period (never full game), so
    the market is archived as evidence but is excluded from the targetable
    Latest set -- proving the exclusion conjunct end-to-end, not just at the
    unit seam.
    """

    provider = "underdog"
    catalog = _catalog(tmp_path)
    engine, _archive_obj, recorder = _archive(tmp_path, provider, catalog)
    snapshot = _governed(_snapshot(provider), catalog)

    period_scoped = [
        market
        for market in snapshot.markets
        if market.scoring_period is ScoringPeriod.FIRST_HALF
    ]
    assert len(period_scoped) == 1
    period_market = period_scoped[0]
    # The period-scoped label is resolved to its specific period and retained
    # verbatim as evidence -- it is never promoted to full game.
    assert period_market.scoring_period_label == "first_half"

    recorder.record_snapshot(
        snapshot, query=QUERY, accepted_at=OBSERVED_AT + timedelta(minutes=1)
    )

    with engine.connect() as connection:
        observed = set(
            connection.execute(
                select(ProjectionObservation.provider_market_id)
            ).scalars()
        )
        latest = set(
            connection.execute(
                select(LatestPlayerProjection.market_reference)
            ).scalars()
        )

    # Retained as evidence: the first-half market is archived as an observation.
    assert period_market.market_id in observed
    # Excluded from the targetable pool: it never enters Latest.
    assert market_reference(period_market) not in latest
    # The board still reaches Latest through its full-game markets.
    assert latest


def test_the_recorded_fourth_provider_proves_both_price_scopes_and_an_unpriced_side():
    snapshot = _snapshot(FOURTH_PROVIDER_NAME)
    scopes = {
        selection.price_scope
        for market in snapshot.markets
        for selection in market.selections
        if selection.is_priced
    }
    kinds = {
        selection.price_kind
        for market in snapshot.markets
        for selection in market.selections
    }

    assert scopes == {PriceScope.SELECTION, PriceScope.ENTRY}
    assert PriceKind.UNPRICED in kinds
    # Nothing in the application names this provider; one registration does.
    assert dfs_provider_registration(FOURTH_PROVIDER_NAME).name == FOURTH_PROVIDER_NAME


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_default_compliance_run_opens_no_socket_and_reads_no_credential(
    monkeypatch, provider: str
):
    def refuse_session(*args: object, **kwargs: object):
        raise AssertionError("the compliance suite must not create a session")

    monkeypatch.setattr(requests, "Session", refuse_session)
    monkeypatch.delenv("COLLECTOR_SIGNING_SECRET", raising=False)

    for case in ("complete", "empty", "partial"):
        snapshot = _snapshot(provider, case)
        assert snapshot.provider == provider


def test_live_provider_contracts_stay_opt_in():
    config = (Path(__file__).parents[2] / "pytest.ini").read_text(encoding="utf-8")

    assert '-m "not live and not integration"' in config
    assert "live: live provider contract tests" in config
