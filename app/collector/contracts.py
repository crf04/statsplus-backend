"""Immutable, JSON-safe collector contracts and numeric validation."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

CURRENT_ENVELOPE_VERSION = 2
ACCEPTED_ENVELOPE_VERSIONS = (1, 2)
MAX_ENVELOPE_BYTES = 10 * 1024 * 1024
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_DEPTH = 64
MAX_PAYLOAD_VALUES = 100_000


class ProviderContractError(ValueError):
    """A safe, stable provider or envelope contract reason."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = str(reason)
        super().__init__(message or self.reason)


class ZoneReconciliationError(ProviderContractError):
    """Opponent zone Totals did not add up to their independent check.

    Carries the team, window, failing equation and residual so a persistent
    mismatch is actionable without echoing the whole provider response.  The
    first occurrence is retried as a pair; the second is reported.
    """

    def __init__(
        self, *, team_id: int, window: str, equation: str,
        expected: float, observed: float,
    ) -> None:
        self.team_id = int(team_id)
        self.window = str(window)
        self.equation = str(equation)
        self.expected = float(expected)
        self.observed = float(observed)
        self.residual = float(observed) - float(expected)
        super().__init__(
            "value_invariant_failed",
            f"team={self.team_id} window={self.window} "
            f"equation={self.equation} expected={self.expected} "
            f"observed={self.observed} residual={self.residual}",
        )

    @property
    def diagnostics(self) -> dict[str, object]:
        return {
            "team_id": self.team_id, "window": self.window,
            "equation": self.equation, "expected": self.expected,
            "observed": self.observed, "residual": self.residual,
        }


def canonical_json(value: Any) -> bytes:
    """Encode JSON with the same canonical representation as Railway."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise ProviderContractError("payload_not_json") from error


def payload_checksum(payload: Any) -> str:
    """Return the checksum of the uncompressed canonical payload bytes."""

    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _validate_payload(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    """Reject non-finite, negative, excessively deep, or huge payloads.

    Provider values are observations, not controls.  A negative number or a
    NaN would make an apparently valid observation impossible to compare on
    Railway, so refusal happens before the value enters the outbox.
    """

    if count is None:
        count = [0]
    if depth > MAX_PAYLOAD_DEPTH:
        raise ProviderContractError("payload_too_deep")
    count[0] += 1
    if count[0] > MAX_PAYLOAD_VALUES:
        raise ProviderContractError("payload_too_large")
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ProviderContractError("non_finite_value")
        if value < 0:
            raise ProviderContractError("negative_value")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ProviderContractError("invalid_payload_key")
            _validate_payload(child, depth=depth + 1, count=count)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_payload(child, depth=depth + 1, count=count)
        return
    raise ProviderContractError("payload_not_json")


def _utc_iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, str):
        try:
            current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProviderContractError("invalid_timestamp") from error
    else:
        raise ProviderContractError("invalid_timestamp")
    if current.tzinfo is None:
        raise ProviderContractError("timestamp_must_be_timezone_aware")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_observation_id(
    *, collector_id: str, instruction_id: str, observation_type: str,
    scope: Any, cutoff: datetime | str, checksum: str,
) -> str:
    """Derive a bounded id that is stable across a crash/replay.

    The checksum is part of the identity so a provider correction is a new
    observation rather than a same-ID checksum conflict.
    """

    material = canonical_json({
        "collector_id": collector_id,
        "instruction_id": instruction_id,
        "observation_type": observation_type,
        "scope": scope,
        "cutoff": _utc_iso(cutoff),
        "checksum": checksum,
    })
    return "rc-" + hashlib.sha256(material).hexdigest()[:48]


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    """One provider response after identity/scope/invariant validation."""

    observation_type: str
    scope: Mapping[str, Any] | str
    season: str
    cutoff: str
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]
    schema_version: int = CURRENT_ENVELOPE_VERSION
    complete: bool = True

    def __post_init__(self) -> None:
        if not self.observation_type.strip() or not self.season.strip():
            raise ProviderContractError("invalid_observation_identity")
        if self.schema_version not in ACCEPTED_ENVELOPE_VERSIONS:
            raise ProviderContractError("schema_unsupported")
        _utc_iso(self.cutoff)
        _validate_payload(self.payload)
        _validate_payload(self.provenance)


@dataclass(frozen=True, slots=True)
class ObservationEnvelope:
    """Wire-ready envelope; only normalized payloads may construct one."""

    manifest_id: str | None
    client_observation_id: str
    environment: str
    collector_id: str
    provider: str
    observation_type: str
    scope: Mapping[str, Any] | str
    season: str
    cutoff: str
    retrieved_at: str
    schema_version: int
    payload: Mapping[str, Any]
    checksum: str = field(init=False)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = {
            "manifest_id": self.manifest_id,
            "client_observation_id": self.client_observation_id,
            "environment": self.environment,
            "collector_id": self.collector_id,
            "provider": self.provider,
            "observation_type": self.observation_type,
            "season": self.season,
        }
        if any(not isinstance(value, str) or not value.strip() for key, value in values.items() if key != "manifest_id"):
            raise ProviderContractError("invalid_envelope_identity")
        if self.manifest_id is not None and not self.manifest_id.strip():
            raise ProviderContractError("invalid_manifest_id")
        if len(self.client_observation_id) > 128:
            raise ProviderContractError("invalid_observation_id")
        if self.schema_version not in ACCEPTED_ENVELOPE_VERSIONS:
            raise ProviderContractError("schema_unsupported")
        _utc_iso(self.cutoff)
        _utc_iso(self.retrieved_at)
        _validate_payload(self.payload)
        _validate_payload(self.provenance)
        computed = payload_checksum(self.payload)
        object.__setattr__(self, "checksum", computed)

    @classmethod
    def from_observation(
        cls,
        observation: NormalizedObservation,
        *,
        manifest_id: str | None,
        environment: str,
        collector_id: str,
        instruction_id: str,
        retrieved_at: datetime | str,
    ) -> "ObservationEnvelope":
        payload = dict(observation.payload)
        # Provenance is part of the normalized evidence and therefore part of
        # the wire payload.  Compute the id from the exact payload that will
        # be checksummed, so a replay remains stable across process restarts.
        payload.setdefault("provenance", dict(observation.provenance))
        checksum = payload_checksum(payload)
        client_id = stable_observation_id(
            collector_id=collector_id,
            instruction_id=instruction_id,
            observation_type=observation.observation_type,
            scope=observation.scope,
            cutoff=observation.cutoff,
            checksum=checksum,
        )
        return cls(
            manifest_id=manifest_id,
            client_observation_id=client_id,
            environment=environment,
            collector_id=collector_id,
            provider="nba",
            observation_type=observation.observation_type,
            scope=observation.scope,
            season=observation.season,
            cutoff=observation.cutoff,
            retrieved_at=_utc_iso(retrieved_at),
            schema_version=observation.schema_version,
            payload=payload,
            provenance=observation.provenance,
        )

    def payload_bytes(self) -> bytes:
        return canonical_json(self.payload)

    def wire_dict(self) -> dict[str, Any]:
        """Return exactly the observation route's accepted envelope fields."""

        return {
            "manifest_id": self.manifest_id,
            "client_observation_id": self.client_observation_id,
            "environment": self.environment,
            "provider": self.provider,
            "observation_type": self.observation_type,
            "scope": self.scope,
            "season": self.season,
            "cutoff": self.cutoff,
            "schema_version": self.schema_version,
            "retrieved_at": self.retrieved_at,
            "checksum": self.checksum,
            "payload": self.payload,
        }

    def compressed_wire(self) -> bytes:
        data = canonical_json(self.wire_dict())
        if len(data) > MAX_ENVELOPE_BYTES:
            raise ProviderContractError("payload_too_large")
        compressed = gzip.compress(data, mtime=0)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise ProviderContractError("payload_too_large")
        return compressed

    def as_outbox_metadata(self) -> dict[str, Any]:
        return {
            "kind": "observation",
            "manifest_id": self.manifest_id,
            "client_observation_id": self.client_observation_id,
            "environment": self.environment,
            "collector_id": self.collector_id,
            "provider": self.provider,
            "observation_type": self.observation_type,
            "scope": self.scope,
            "season": self.season,
            "cutoff": self.cutoff,
            "retrieved_at": self.retrieved_at,
            "schema_version": self.schema_version,
            "checksum": self.checksum,
        }


@dataclass(frozen=True, slots=True)
class CatalogEnvelope:
    """Catalog publication metadata stored in the same outbox table."""

    request_id: str
    envelope: ObservationEnvelope
    catalog_version: str
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.catalog_version.strip():
            raise ProviderContractError("invalid_catalog_version")
        if self.envelope.manifest_id is not None:
            raise ProviderContractError("manifest_scope_mismatch")
        if self.expires_at is not None:
            _utc_iso(self.expires_at)

    def wire_dict(self) -> dict[str, Any]:
        wire = self.envelope.wire_dict()
        wire["catalog_version"] = self.catalog_version
        if self.expires_at is not None:
            wire["expires_at"] = self.expires_at
        return wire

    def compressed_wire(self) -> bytes:
        data = canonical_json(self.wire_dict())
        if len(data) > MAX_ENVELOPE_BYTES:
            raise ProviderContractError("payload_too_large")
        compressed = gzip.compress(data, mtime=0)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise ProviderContractError("payload_too_large")
        return compressed

    def as_outbox_metadata(self) -> dict[str, Any]:
        result = self.envelope.as_outbox_metadata()
        result.update({
            "kind": "catalog",
            "request_id": self.request_id,
            "catalog_version": self.catalog_version,
            "expires_at": self.expires_at,
        })
        return result


def parse_datetime(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ProviderContractError("timestamp_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "ACCEPTED_ENVELOPE_VERSIONS",
    "CURRENT_ENVELOPE_VERSION",
    "MAX_COMPRESSED_BYTES",
    "MAX_ENVELOPE_BYTES",
    "ObservationEnvelope",
    "CatalogEnvelope",
    "NormalizedObservation",
    "ProviderContractError",
    "canonical_json",
    "parse_datetime",
    "payload_checksum",
    "stable_observation_id",
]
