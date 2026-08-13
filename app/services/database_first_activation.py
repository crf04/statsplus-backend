"""Database-first publication reads and activation validation seams.

The collection control plane owns immutable publication versions and one
fenced pointer per stream.  This module is the deliberately small read-side
authority used by the public Matchups path and by offline validation tools:
it never constructs a provider, never refreshes a legacy table, and serves an
active last-good version even when its age is stale.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models.collection_control import (
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)
from app.services.collection_control import (
    ControlPlaneError,
    PublicationService,
)


UTC = timezone.utc

# These are intentionally independent of the provider refresh windows.  A
# publication's age describes the product fact, while a provider's age
# describes how it was obtained.
PUBLICATION_FRESHNESS_SECONDS: dict[str, int] = {
    "cutoff_current": 60 * 60,
    "daily_recheck": 24 * 60 * 60,
    "seven_day": 7 * 24 * 60 * 60,
    "request_time": 0,
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PublicationRead:
    """One immutable stream read and its bounded provenance."""

    stream_key: str
    publication_id: str | None
    season: str | None
    cutoff: str | None
    version: int | None
    status: str
    freshness: str
    age_seconds: int | None
    payload: Any | None
    source: str = "database"

    @property
    def available(self) -> bool:
        # A rollback pointer still names a known-good immutable publication.
        # Keep its rollback status visible to callers instead of treating the
        # safety action itself as data loss.
        return self.payload is not None and self.status in {"active", "rollback", "stale"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_key": self.stream_key,
            "publication_id": self.publication_id,
            "season": self.season,
            "coverage_cutoff": self.cutoff,
            "version": self.version,
            "status": self.status,
            "freshness": self.freshness,
            "age_seconds": self.age_seconds,
            "source": self.source,
        }


class DatabaseFirstPublicationReader:
    """Read active publication pointers without provider or legacy fallback."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
        freshness_seconds: Mapping[str, int] | None = None,
    ) -> None:
        self.engine = engine
        self._session = sessionmaker(bind=engine, expire_on_commit=False)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.freshness_seconds = dict(
            freshness_seconds or PUBLICATION_FRESHNESS_SECONDS
        )

    def read(
        self,
        stream_key: str,
        *,
        season: str | None = None,
        require_active: bool = True,
    ) -> PublicationRead:
        """Return one active last-good publication, including stale values.

        ``require_active`` is useful for rehearsal reads of an inactive
        candidate.  Public callers leave it at its safe default.
        """

        with self._session() as session:
            stream = session.scalar(
                select(PublicationStream).where(
                    PublicationStream.stream_key == stream_key
                )
            )
            pointer = session.scalar(
                select(PublicationPointer).where(
                    PublicationPointer.stream_key == stream_key
                )
            )
            if stream is None or (require_active and not bool(stream.enabled)):
                return self._missing(stream_key, "unavailable")
            if pointer is None or not pointer.active_publication_id:
                return self._missing(stream_key, "missing")
            publication = session.scalar(
                select(PublicationVersion).where(
                    PublicationVersion.publication_id == pointer.active_publication_id
                )
            )

        if publication is None or publication.status not in {"active", "rollback"}:
            return self._missing(stream_key, "missing")
        if season is not None and publication.season != season:
            return self._missing(stream_key, "missing")
        try:
            payload = json.loads(publication.payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            # A corrupt rendered document must never make the read path fall
            # back to a provider or to a partial prior attempt.
            return self._missing(stream_key, "unavailable")
        now = _utc(self.clock())
        age = max(0, int((now - _utc(publication.created_at)).total_seconds()))
        threshold = self.freshness_seconds.get(str(stream.freshness_rule))
        freshness = "fresh" if threshold is not None and age <= threshold else "stale"
        return PublicationRead(
            stream_key=stream_key,
            publication_id=publication.publication_id,
            season=publication.season,
            cutoff=_utc(publication.cutoff).isoformat(),
            version=int(publication.version),
            status="active" if publication.status == "active" else "rollback",
            freshness=freshness,
            age_seconds=age,
            payload=payload,
        )

    def read_many(
        self,
        stream_keys: Iterable[str],
        *,
        season: str | None = None,
    ) -> dict[str, PublicationRead]:
        """Read streams independently; one missing stream never hides others."""

        return {
            stream_key: self.read(stream_key, season=season)
            for stream_key in sorted(set(stream_keys))
        }

    def metadata(
        self,
        stream_keys: Iterable[str],
        *,
        season: str | None = None,
    ) -> dict[str, Any]:
        reads = self.read_many(stream_keys, season=season)
        available = [read for read in reads.values() if read.available]
        cutoffs = {read.cutoff for read in available if read.cutoff}
        freshness = {read.freshness for read in available}
        return {
            "streams": {key: read.to_dict() for key, read in reads.items()},
            "mixed_cutoff": len(cutoffs) > 1,
            "mixed_freshness": len(freshness) > 1,
            "coverage_cutoffs": sorted(cutoffs),
        }

    @staticmethod
    def _missing(stream_key: str, status: str) -> PublicationRead:
        return PublicationRead(
            stream_key=stream_key,
            publication_id=None,
            season=None,
            cutoff=None,
            version=None,
            status=status,
            freshness="missing" if status == "missing" else "unavailable",
            age_seconds=None,
            payload=None,
        )


class DatabaseOnlyProviderGuard:
    """Test/assembly guard that fails on every provider attribute access."""

    def __init__(self, name: str = "provider") -> None:
        self.name = name

    def __getattr__(self, operation: str) -> Any:
        raise AssertionError(
            f"database-only Matchups read attempted {self.name}.{operation}"
        )


class LegacyWriteFence:
    """Reject legacy writes once the corresponding stream is activated."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def is_activated(self, stream_key: str) -> bool:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(PublicationStream.enabled).where(
                    PublicationStream.stream_key == stream_key
                )
            ).scalar_one_or_none()
        return bool(row)

    def assert_writable(self, stream_key: str) -> None:
        if self.is_activated(stream_key):
            raise ControlPlaneError("legacy_write_fenced")

    def guard(self, stream_key: str) -> Callable[[], None]:
        return lambda: self.assert_writable(stream_key)


class DatabaseFirstActivationService:
    """Convenience facade used by rehearsal and operator tooling."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self.publications = PublicationService(engine, clock=clock)
        self.reader = DatabaseFirstPublicationReader(engine, clock=clock)
        self.fence = LegacyWriteFence(engine)

    def activate(self, stream_key: str, *, actor: str, reason: str, **kwargs: Any) -> Any:
        return self.publications.activate_stream(
            stream_key, actor=actor, reason=reason, **kwargs
        )

    def rollback(self, stream_key: str, *, reason: str, expected_fence: int | None = None) -> Any:
        return self.publications.rollback(
            stream_key, reason=reason, expected_fence=expected_fence
        )


# Friendly names for callers that use the packet vocabulary.
PublicationReadRouter = DatabaseFirstPublicationReader
DatabaseFirstMatchupsReader = DatabaseFirstPublicationReader
ProviderCallGuard = DatabaseOnlyProviderGuard
ActivationService = DatabaseFirstActivationService


__all__ = [
    "ActivationService",
    "DatabaseFirstActivationService",
    "DatabaseFirstMatchupsReader",
    "DatabaseFirstPublicationReader",
    "DatabaseOnlyProviderGuard",
    "LegacyWriteFence",
    "PUBLICATION_FRESHNESS_SECONDS",
    "PublicationRead",
    "PublicationReadRouter",
    "ProviderCallGuard",
]
