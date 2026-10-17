"""The provider registry is the only authority on which providers exist."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config.settings import ConfigurationError, load_settings
from app.providers.dfs import (
    CoverageEvidence,
    NBAMarketQuery,
    ProviderSnapshot,
    ProviderSnapshotProvider,
    RetrievalContext,
    SnapshotStatus,
)
from app.providers.registry import (
    DFSProviderRegistration,
    DFSProviderRuntime,
    EntryPayoutTable,
    ProviderRegistryError,
    build_dfs_provider,
    dfs_provider_live_max_ages,
    dfs_provider_name_set,
    dfs_provider_names,
    dfs_provider_registration,
    dfs_provider_registrations,
    registered_dfs_provider,
)


class _FourthAdapter:
    """A recorded adapter with no wire format and no provider-specific path."""

    name = "fourth"

    def get_snapshot(
        self,
        query: NBAMarketQuery,
        context: RetrievalContext,
    ) -> ProviderSnapshot:
        del query, context
        return ProviderSnapshot(
            provider=self.name,
            status=SnapshotStatus.COMPLETE,
            markets=(),
            coverage=CoverageEvidence(pagination_complete=True, fanout_complete=True),
            retrieved_at=datetime(2026, 1, 2, tzinfo=UTC),
        )


def _fourth_registration(**overrides: object) -> DFSProviderRegistration:
    fields: dict[str, object] = {
        "name": "fourth",
        "build": lambda runtime: _FourthAdapter(),
    }
    fields.update(overrides)
    return DFSProviderRegistration(**fields)  # type: ignore[arg-type]


def _environ(providers: str) -> dict[str, str]:
    return {
        "FLASK_ENV": "testing",
        "FIREBASE_ADMIN_DISABLED": "true",
        "DFS_ENABLED_PROVIDERS": providers,
    }


def test_the_registry_names_every_provider_the_application_may_enable():
    assert dfs_provider_names() == (
        "dabble",
        "prizepicks",
        "underdog",
        "kalshi",
        "polymarket",
        "novig",
    )
    assert dfs_provider_name_set() == set(dfs_provider_names())
    assert tuple(registration.name for registration in dfs_provider_registrations()) == dfs_provider_names()
    assert dfs_provider_registration("PrizePicks").name == "prizepicks"

    with pytest.raises(ProviderRegistryError, match="unregistered DFS provider"):
        dfs_provider_registration("fourth")


def test_an_unregistered_provider_cannot_be_configured():
    with pytest.raises(ConfigurationError, match="unregistered provider: fourth"):
        load_settings(environ=_environ("fourth"))

    with pytest.raises(ConfigurationError, match="registered DFS provider"):
        load_settings(
            environ={
                **_environ("dabble"),
                "PROJECTION_ARCHIVE_READ_PROVIDER": "fourth",
            }
        )


def test_registering_a_provider_is_all_onboarding_takes(monkeypatch):
    from unittest.mock import Mock

    from sqlalchemy import create_engine

    from app.dependencies import build_dependencies
    from app.migrations import run_migrations

    with registered_dfs_provider(_fourth_registration()):
        assert "fourth" in dfs_provider_name_set()
        settings = load_settings(
            environ={
                **_environ("fourth"),
                "DATABASE_URL": "postgresql://statsplus.example/db",
                "PROJECTION_ARCHIVE_READ_PROVIDER": "fourth",
                "PROJECTION_ARCHIVE_READ_ENABLED": "true",
            }
        )
        engine = create_engine("sqlite:///:memory:")
        run_migrations(engine)
        monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
        monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))

        dependencies = build_dependencies(settings)

        assert set(dependencies.dfs_providers) == {"fourth"}
        assert isinstance(dependencies.dfs_providers["fourth"], _FourthAdapter)
        assert dependencies.dfs_board_service.enabled_providers == ("fourth",)
        assert set(dependencies.dfs_board_service.disabled_providers) == {
            "dabble",
            "polymarket",
            "prizepicks",
            "underdog",
            "kalshi",
            "novig",
        }

    # The registration is gone, and so is the configuration that named it.
    assert "fourth" not in dfs_provider_name_set()
    with pytest.raises(ConfigurationError, match="unregistered provider: fourth"):
        load_settings(environ=_environ("fourth"))


def test_a_nonconforming_registration_cannot_be_enabled(monkeypatch):
    from unittest.mock import Mock

    from sqlalchemy import create_engine

    from app.dependencies import build_dependencies
    from app.migrations import run_migrations

    registration = _fourth_registration(build=lambda runtime: object())
    with registered_dfs_provider(registration):
        settings = load_settings(environ=_environ("fourth"))
        engine = create_engine("sqlite:///:memory:")
        run_migrations(engine)
        monkeypatch.setattr("app.utils.db.get_engine", Mock(return_value=engine))
        monkeypatch.setattr("app.utils.cache_config.get_redis_client", Mock(return_value=None))

        with pytest.raises(ConfigurationError, match="does not implement the shared provider"):
            build_dependencies(settings)


def test_a_registration_must_name_a_provider_and_a_way_to_build_it():
    with pytest.raises(ProviderRegistryError, match="requires a name"):
        DFSProviderRegistration(name="  ", build=lambda runtime: _FourthAdapter())
    with pytest.raises(ProviderRegistryError, match="callable builder"):
        DFSProviderRegistration(name="fourth", build=None)  # type: ignore[arg-type]
    with pytest.raises(ProviderRegistryError, match="already registered"):
        with registered_dfs_provider(_fourth_registration(name="dabble")):
            pass
    with pytest.raises(ProviderRegistryError, match="DFSProviderRegistration"):
        with registered_dfs_provider("dabble"):  # type: ignore[arg-type]
            pass


def test_entry_payout_tables_are_declared_facts_about_a_product():
    table = EntryPayoutTable(
        reference_entry_size=2,
        multipliers={2: Decimal("3"), 3: 5, 6: "37.5"},
    )
    registration = _fourth_registration(entry_payout_tables={"Standard": table})

    assert table.reference_multiplier == Decimal("3")
    assert list(table.multipliers) == [2, 3, 6]
    assert registration.payout_table("standard") is table
    assert registration.payout_table("demon") is None
    assert registration.payout_table(None) is None

    with pytest.raises(ProviderRegistryError, match="must appear in the payout table"):
        EntryPayoutTable(reference_entry_size=4, multipliers={2: Decimal("3")})
    with pytest.raises(ProviderRegistryError, match="must be positive"):
        EntryPayoutTable(reference_entry_size=2, multipliers={2: Decimal("0")})
    with pytest.raises(ProviderRegistryError, match="sizes must be positive"):
        EntryPayoutTable(reference_entry_size=2, multipliers={0: Decimal("3")})
    with pytest.raises(ProviderRegistryError, match="sizes must be integers"):
        EntryPayoutTable(reference_entry_size=2, multipliers={"2": Decimal("3")})
    with pytest.raises(ProviderRegistryError, match="must not be empty"):
        EntryPayoutTable(reference_entry_size=2, multipliers={})
    with pytest.raises(ProviderRegistryError, match="keyed by a variant label"):
        _fourth_registration(entry_payout_tables={" ": table})
    with pytest.raises(ProviderRegistryError, match="EntryPayoutTable values"):
        _fourth_registration(entry_payout_tables={"standard": object()})


def test_every_registered_provider_builds_the_shared_snapshot_seam():
    runtime = DFSProviderRuntime()

    assert runtime.timeout == (3.0, 8.0)
    for name in dfs_provider_names():
        provider = build_dfs_provider(name, runtime)
        assert isinstance(provider, ProviderSnapshotProvider)
        assert type(provider).__name__.casefold().startswith(name)


def test_registered_dabble_adapter_prices_a_recorded_standard_line_at_three_times():
    import json
    from pathlib import Path

    folder = Path(__file__).parent / "fixtures" / "dabble"
    payloads = [
        json.loads((folder / f"recorded_2026-10-15.{name}.json").read_text("utf-8"))
        for name in ("competitions", "fixtures", "fixture_details")
    ]

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def get(self, url, **kwargs):
            return Response(payloads.pop(0))

    adapter = build_dfs_provider("dabble", DFSProviderRuntime())
    adapter._request_session = Session()

    snapshot = adapter.get_snapshot(
        NBAMarketQuery(),
        RetrievalContext(deadline="2099-01-01T00:00:00Z", request_id="registry"),
    )

    standard = next(m for m in snapshot.markets if m.variant.value == "standard")
    assert [(s.price_kind.value, s.price_value, s.price_scope.value) for s in standard.selections] == [
        ("multiplier", Decimal("3"), "entry")
    ] * 2


def test_dabble_registration_carries_its_entry_payout_table():
    from app.providers.registry import DABBLE_ENTRY_PAYOUT_TABLES

    registration = dfs_provider_registration("dabble")

    assert dict(registration.entry_payout_tables) == dict(DABBLE_ENTRY_PAYOUT_TABLES)
    assert registration.payout_table("standard").reference_multiplier == Decimal("3")


def test_only_prizepicks_declares_a_live_window_longer_than_the_default():
    # PrizePicks lines come from the hourly Odds API beat (crf04/statsplus#215).
    assert dict(dfs_provider_live_max_ages()) == {"prizepicks": timedelta(minutes=90)}
    assert dfs_provider_registration("underdog").live_max_age is None

    registration = _fourth_registration(live_max_age=timedelta(minutes=45))
    with registered_dfs_provider(registration):
        assert dfs_provider_live_max_ages()["fourth"] == timedelta(minutes=45)
    assert "fourth" not in dfs_provider_live_max_ages()


@pytest.mark.parametrize("window", [timedelta(0), timedelta(minutes=-5), 90, "90m"])
def test_a_live_window_must_be_a_positive_duration(window):
    with pytest.raises(ProviderRegistryError, match="positive duration"):
        _fourth_registration(live_max_age=window)
