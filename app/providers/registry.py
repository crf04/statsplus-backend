"""The single authority for which DFS providers exist and how they are built.

Configuration, dependency construction, scheduled collection, diagnostics, and
the shared offline compliance suite all read this module, so a provider is
enabled exactly when it is registered here and can be constructed as a
:class:`~app.providers.dfs.ProviderSnapshotProvider`.  Nothing else in the
application may name a provider the registry does not.

Registrations carry the provider-neutral facts the rest of the application
needs about a provider: its name, how to construct its adapter, and the static
entry payout tables the provider publishes outside its API.  Adapter wire
formats stay in the adapter modules, which this module imports lazily and is
never imported by, so a registration can hand an adapter its static facts
without any adapter reaching back into the registry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType

from app.providers.dfs import ProviderSnapshotProvider


class ProviderRegistryError(ValueError):
    """A provider is not registered, or its registration cannot be built."""


@dataclass(frozen=True, slots=True)
class EntryPayoutTable:
    """One provider's static, published slip payouts by entry size.

    An entry-scoped payout is a fact about the provider's product rather than
    about any one market, so it is declared here rather than discovered in a
    payload.  ``reference_entry_size`` names the entry size whose multiplier is
    the canonical entry price of a single selection; the provider's smallest
    legal entry is the only size every selection can be part of, so that is
    what the price of one leg is quoted at.
    """

    reference_entry_size: int
    multipliers: Mapping[int, Decimal]

    def __post_init__(self) -> None:
        multipliers: dict[int, Decimal] = {}
        for raw_size, raw_multiplier in dict(self.multipliers).items():
            if isinstance(raw_size, bool) or not isinstance(raw_size, int):
                raise ProviderRegistryError("entry payout sizes must be integers")
            if raw_size < 1:
                raise ProviderRegistryError("entry payout sizes must be positive")
            multiplier = (
                raw_multiplier
                if isinstance(raw_multiplier, Decimal)
                else Decimal(str(raw_multiplier))
            )
            if multiplier <= 0:
                raise ProviderRegistryError(
                    "entry payout multipliers must be positive"
                )
            multipliers[raw_size] = multiplier
        if not multipliers:
            raise ProviderRegistryError("an entry payout table must not be empty")
        if self.reference_entry_size not in multipliers:
            raise ProviderRegistryError(
                "the reference entry size must appear in the payout table"
            )
        object.__setattr__(
            self,
            "multipliers",
            MappingProxyType(dict(sorted(multipliers.items()))),
        )

    @property
    def reference_multiplier(self) -> Decimal:
        """The entry payout one selection is priced at."""

        return self.multipliers[self.reference_entry_size]


@dataclass(frozen=True, slots=True)
class DFSProviderRuntime:
    """The construction inputs every registered adapter is built from."""

    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 8.0
    detail_concurrency: int = 4

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout_seconds, self.read_timeout_seconds)


@dataclass(frozen=True, slots=True)
class DFSProviderRegistration:
    """One admitted provider: its name, its adapter, and its static payouts."""

    name: str
    build: Callable[[DFSProviderRuntime], ProviderSnapshotProvider]
    entry_payout_tables: Mapping[str, EntryPayoutTable] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        name = self.name.strip().casefold() if isinstance(self.name, str) else ""
        if not name:
            raise ProviderRegistryError("a provider registration requires a name")
        if not callable(self.build):
            raise ProviderRegistryError(
                "a provider registration requires a callable builder"
            )
        tables: dict[str, EntryPayoutTable] = {}
        for raw_label, table in dict(self.entry_payout_tables).items():
            label = raw_label.strip().casefold() if isinstance(raw_label, str) else ""
            if not label:
                raise ProviderRegistryError(
                    "entry payout tables must be keyed by a variant label"
                )
            if not isinstance(table, EntryPayoutTable):
                raise ProviderRegistryError(
                    "entry payout tables must be EntryPayoutTable values"
                )
            tables[label] = table
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "entry_payout_tables", MappingProxyType(tables))

    def payout_table(self, variant_label: str | None) -> EntryPayoutTable | None:
        """Return the static payout table this provider declares for a variant."""

        if not isinstance(variant_label, str):
            return None
        return self.entry_payout_tables.get(variant_label.strip().casefold())

    def construct(self, runtime: DFSProviderRuntime) -> ProviderSnapshotProvider:
        """Build the adapter and refuse anything that is not the shared seam."""

        provider = self.build(runtime)
        if not isinstance(provider, ProviderSnapshotProvider):
            raise ProviderRegistryError(
                f"registered DFS provider {self.name} does not implement the "
                "shared provider snapshot contract"
            )
        return provider


def _build_dabble(runtime: DFSProviderRuntime) -> ProviderSnapshotProvider:
    from app.providers.dabble import DabbleAdapter

    return DabbleAdapter(
        connect_timeout_seconds=runtime.connect_timeout_seconds,
        read_timeout_seconds=runtime.read_timeout_seconds,
        detail_concurrency=runtime.detail_concurrency,
    )


def _build_prizepicks(runtime: DFSProviderRuntime) -> ProviderSnapshotProvider:
    from app.providers.prizepicks import PrizePicksAdapter

    return PrizePicksAdapter(
        timeout=runtime.timeout,
        entry_payout_tables=PRIZEPICKS_ENTRY_PAYOUT_TABLES,
    )


def _build_underdog(runtime: DFSProviderRuntime) -> ProviderSnapshotProvider:
    from app.providers.underdog import UnderdogAdapter

    return UnderdogAdapter(timeout=runtime.timeout)


# PrizePicks publishes its Power Play payouts as a static table on its own site
# rather than in the projections API, so the reviewed table is configuration
# here.  An operator updates it when the provider republishes it; no adapter
# and no archive path may hold a copy.
PRIZEPICKS_ENTRY_PAYOUT_TABLES: Mapping[str, EntryPayoutTable] = MappingProxyType(
    {
        "standard": EntryPayoutTable(
            reference_entry_size=2,
            multipliers={
                2: Decimal("3"),
                3: Decimal("5"),
                4: Decimal("10"),
                5: Decimal("20"),
                6: Decimal("37.5"),
            },
        )
    }
)


_DEFAULT_REGISTRATIONS: tuple[DFSProviderRegistration, ...] = (
    DFSProviderRegistration(name="dabble", build=_build_dabble),
    DFSProviderRegistration(
        name="prizepicks",
        build=_build_prizepicks,
        entry_payout_tables=PRIZEPICKS_ENTRY_PAYOUT_TABLES,
    ),
    DFSProviderRegistration(name="underdog", build=_build_underdog),
)

_registrations: tuple[DFSProviderRegistration, ...] = _DEFAULT_REGISTRATIONS


def dfs_provider_registrations() -> tuple[DFSProviderRegistration, ...]:
    """Every provider currently admitted, in registration order."""

    return _registrations


def dfs_provider_names() -> tuple[str, ...]:
    """The names of every admitted provider, in registration order."""

    return tuple(registration.name for registration in _registrations)


def dfs_provider_name_set() -> frozenset[str]:
    """The names of every admitted provider, as a membership test."""

    return frozenset(dfs_provider_names())


def dfs_provider_registration(name: str) -> DFSProviderRegistration:
    """Return one admitted provider's registration, or refuse the name."""

    wanted = name.strip().casefold() if isinstance(name, str) else ""
    for registration in _registrations:
        if registration.name == wanted:
            return registration
    raise ProviderRegistryError(f"unregistered DFS provider {wanted or name!r}")


def build_dfs_provider(
    name: str,
    runtime: DFSProviderRuntime,
) -> ProviderSnapshotProvider:
    """Construct one admitted provider through its own registration."""

    return dfs_provider_registration(name).construct(runtime)


@contextmanager
def registered_dfs_provider(
    registration: DFSProviderRegistration,
) -> Iterator[DFSProviderRegistration]:
    """Admit one further provider for the duration of the block.

    Onboarding a provider is a registration plus adapter evidence and nothing
    else, so this seam exists to demonstrate exactly that: a caller that
    registers an adapter gets configuration, construction, collection,
    diagnostics, and the compliance suite without touching any of them.

    This mutates the module-global registration tuple without a lock and is a
    single-threaded pytest-only seam: production admits providers through the
    static ``_DEFAULT_REGISTRATIONS`` and never registers one at runtime.
    """

    global _registrations

    if not isinstance(registration, DFSProviderRegistration):
        raise ProviderRegistryError(
            "a provider registration must be a DFSProviderRegistration"
        )
    if registration.name in dfs_provider_name_set():
        raise ProviderRegistryError(
            f"DFS provider {registration.name} is already registered"
        )
    previous = _registrations
    _registrations = (*previous, registration)
    try:
        yield registration
    finally:
        _registrations = previous


__all__ = [
    "DFSProviderRegistration",
    "DFSProviderRuntime",
    "EntryPayoutTable",
    "PRIZEPICKS_ENTRY_PAYOUT_TABLES",
    "ProviderRegistryError",
    "build_dfs_provider",
    "dfs_provider_name_set",
    "dfs_provider_names",
    "dfs_provider_registration",
    "dfs_provider_registrations",
    "registered_dfs_provider",
]
