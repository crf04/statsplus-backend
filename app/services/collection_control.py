"""Control-plane services for collection, ingestion, and publication.

These services intentionally accept an injected SQLAlchemy engine and clock so
they can be exercised against a temporary database without provider calls or
credentials.  The only mutable operation on an observation is the first
durable insert; publication pointers are changed under a per-stream fence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import threading
import secrets
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from cryptography.fernet import Fernet, InvalidToken as FernetInvalidToken

from app.domain.nba_teams import NBA_TEAM_TRICODES, canonical_nba_team_abbreviation
from app.models.catalogs import PLAY_TYPES, SHOOTING_TYPES

from app.models.collection_control import (
    ActiveSeason,
    BootstrapRequest,
    CatalogPublication,
    CollectionManifest,
    CollectorIdentity,
    CollectionObservation,
    CollectorTokenReplay,
    CollectorLease,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
    CompositionJob,
    CollectionCycle,
    AuditEvent,
    ReconciliationItem,
    CollectionAlert,
    CollectorUsage,
    ValidationSummary,
    CredentialDelivery,
    OperatorJob,
    GovernedNotApplicable,
)


UTC = timezone.utc
CURRENT_ENVELOPE_VERSION = 2
MAX_ENVELOPE_BYTES = 10 * 1024 * 1024
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_SCOPE_COUNT = 512
MAX_SCOPE_BYTES = 32 * 1024
OBSERVATION_RETENTION_DAYS = 30
ATHLETE_REUSE_DAYS = 7
MAX_PAYLOAD_VALUES = 100_000
MAX_PAYLOAD_DEPTH = 64
MAX_RECORDS_PER_OBSERVATION = 100_000
COLLECTOR_LEASE_SECONDS = 30
COLLECTOR_LEASE_RETRY_SECONDS = 1
STALE_ALERT_SECONDS = 60 * 60
ATTENTION_ALERT_SECONDS = 6 * 60 * 60
MAX_EVENT_CATALOG_GAMES = 3_000
MAX_ATHLETE_CATALOG_IDENTITIES = 100_000
NBA_TEAM_IDS = frozenset(str(1610612737 + index) for index in range(30))
REGISTERED_BASES = frozenset({"play_types", "shot_zones", "shot_types", "assist_locations"})
STREAM_BASES: dict[str, frozenset[str]] = {
    "synergy_play_types": frozenset({"play_types"}),
    "grouped_shot_types": frozenset({"shot_types"}),
    "exact_shot_zones": frozenset({"shot_zones"}),
    "assist_locations": frozenset({"assist_locations"}),
}
STREAM_REQUIRED_SLICES: dict[str, frozenset[str]] = {
    "synergy_play_types": frozenset(PLAY_TYPES),
    "grouped_shot_types": frozenset(SHOOTING_TYPES),
    "exact_shot_zones": frozenset({
        "Restricted Area", "In The Paint (Non-RA)", "Mid-Range",
        "Corner 3", "Above the Break 3",
    }),
    "assist_locations": frozenset({
        "Arc3Assists", "Corner3Assists", "AtRimAssists",
        "ShortMidRangeAssists", "LongMidRangeAssists",
    }),
}
OBSERVATION_BASES: dict[str, str] = {
    "synergy": "play_types",
    "synergy_play_types": "play_types",
    "shot_types": "shot_types",
    "grouped_shot_types": "shot_types",
    "shot_zones": "shot_zones",
    "exact_shot_zones": "shot_zones",
    "assist_locations": "assist_locations",
}

@dataclass(frozen=True, slots=True)
class SurfaceDefinition:
    """Immutable executable contract for one publication surface."""

    stream_key: str
    provider: str
    owner: str
    scope: str
    required: tuple[str, ...]
    schema: tuple[int, ...]
    complete: str
    strategy: str
    freshness: str
    windows: tuple[str, ...]
    enabled: bool = False
    reason: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


_SURFACE_REGISTRY_RAW: tuple[dict[str, Any], ...] = (
    {"stream_key": "event_catalog", "provider": "nba", "owner": "residential_collector", "scope": "whole_season", "required": ("event_catalog",), "schema": (1, 2), "complete": "catalog_complete", "strategy": "keyed_reconcile", "freshness": "cutoff_current", "windows": ("regular_season",), "enabled": False},
    {"stream_key": "athlete_catalog", "provider": "nba", "owner": "residential_collector", "scope": "whole_season", "required": ("athlete_catalog",), "schema": (1, 2), "complete": "identity_complete", "strategy": "keyed_reconcile", "freshness": "seven_day", "windows": ("regular_season",), "enabled": False},
    {"stream_key": "canonical_game_ledger", "provider": "pbp", "owner": "railway", "scope": "one_completed_game", "required": ("game_stats",), "schema": (1, 2), "complete": "game_complete", "strategy": "atomic_replace", "freshness": "daily_recheck", "windows": ("regular_season",), "enabled": False},
    {"stream_key": "player_game_logs", "provider": "ledger", "owner": "railway", "scope": "season_date_query", "required": ("canonical_game_ledger",), "schema": (1,), "complete": "league_complete", "strategy": "ledger_compose", "freshness": "cutoff_current", "windows": ("season", "since_date"), "enabled": False},
    {"stream_key": "traditional_opponent", "provider": "ledger", "owner": "railway", "scope": "season_l15", "required": ("canonical_game_ledger",), "schema": (1,), "complete": "league_complete", "strategy": "ledger_compose", "freshness": "cutoff_current", "windows": ("season", "l15"), "enabled": False},
    {"stream_key": "assist_locations", "provider": "ledger", "owner": "railway", "scope": "season_l15", "required": ("canonical_game_ledger",), "schema": (1,), "complete": "league_complete", "strategy": "ledger_compose", "freshness": "cutoff_current", "windows": ("season", "l15"), "enabled": False},
    {"stream_key": "player_per36", "provider": "ledger", "owner": "railway", "scope": "full_regular_season", "required": ("canonical_game_ledger",), "schema": (1,), "complete": "league_complete", "strategy": "ledger_compose", "freshness": "cutoff_current", "windows": ("regular_season",), "enabled": False},
    {"stream_key": "synergy_play_types", "provider": "nba", "owner": "residential_collector", "scope": "season", "required": ("synergy",), "schema": (1, 2), "complete": "base_complete", "strategy": "snapshot_replace", "freshness": "cutoff_current", "windows": ("season",), "enabled": False},
    {"stream_key": "grouped_shot_types", "provider": "nba", "owner": "residential_collector", "scope": "season_l15", "required": ("shot_types",), "schema": (1, 2), "complete": "base_complete", "strategy": "snapshot_replace", "freshness": "cutoff_current", "windows": ("season", "l15"), "enabled": False},
    {"stream_key": "exact_shot_zones", "provider": "nba", "owner": "residential_collector", "scope": "season_l15", "required": ("shot_zones",), "schema": (1, 2), "complete": "base_complete", "strategy": "snapshot_replace", "freshness": "cutoff_current", "windows": ("season", "l15"), "enabled": False},
    {"stream_key": "dfs_boards", "provider": "railway", "owner": "request_time", "scope": "pregame", "required": (), "schema": (1,), "complete": "provider_readable", "strategy": "request_time", "freshness": "request_time", "windows": ("pregame",), "enabled": False},
    {"stream_key": "injury_reports", "provider": "rotowire", "owner": "request_time", "scope": "pregame", "required": (), "schema": (1,), "complete": "provider_readable", "strategy": "request_time", "freshness": "request_time", "windows": ("pregame",), "enabled": False},
    {"stream_key": "synergy:l15", "provider": "nba", "owner": "residential_collector", "scope": "l15", "required": ("synergy",), "schema": (1, 2), "complete": "unsupported", "strategy": "never_schedule", "freshness": "unavailable", "windows": ("l15",), "enabled": False, "reason": "provider_window_unsupported"},
)

SURFACE_REGISTRY: tuple[SurfaceDefinition, ...] = tuple(
    SurfaceDefinition(**definition) for definition in _SURFACE_REGISTRY_RAW
)


def _surface_definition(surface: str) -> SurfaceDefinition | None:
    normalized = str(surface or "").strip()
    return next(
        (definition for definition in SURFACE_REGISTRY
         if normalized == definition.stream_key or normalized in definition.required),
        None,
    )


def _stream_definition(stream: PublicationStream) -> SurfaceDefinition:
    definition = _surface_definition(stream.stream_key)
    if definition is not None:
        # The persisted stream is the deploy-time owner/provider authority;
        # the immutable registry contributes the reviewed scope vocabulary.
        return SurfaceDefinition(
            stream_key=definition.stream_key,
            provider=stream.provider,
            owner=stream.owner,
            scope=definition.scope,
            required=tuple(json.loads(stream.required_observations)) or definition.required,
            schema=tuple(json.loads(stream.schema_versions)),
            complete=stream.completeness_rule,
            strategy=stream.publication_strategy,
            freshness=stream.freshness_rule,
            windows=tuple(json.loads(stream.supported_windows)) or definition.windows,
            enabled=stream.enabled,
            reason=definition.reason,
        )
    return SurfaceDefinition(
        stream_key=stream.stream_key,
        provider=stream.provider,
        owner=stream.owner,
        scope=stream.stream_key,
        required=tuple(json.loads(stream.required_observations)),
        schema=tuple(json.loads(stream.schema_versions)),
        complete=stream.completeness_rule,
        strategy=stream.publication_strategy,
        freshness=stream.freshness_rule,
        windows=tuple(json.loads(stream.supported_windows)),
        enabled=stream.enabled,
    )


def _surface_names(definition: SurfaceDefinition, observation_type: str | None = None) -> set[str]:
    names = {definition.stream_key, definition.scope, *definition.required}
    if observation_type:
        names.add(str(observation_type).strip())
    return {name for name in names if name}


def _claims_allow_surface(claims: CollectorClaims, *, definition: SurfaceDefinition,
                          provider: str | None = None,
                          surface: str | None = None) -> bool:
    """Require owner/provider/surface authorization for one governed surface."""

    providers = {str(value).strip() for value in claims.providers if str(value).strip()}
    surfaces = {str(value).strip() for value in claims.surfaces if str(value).strip()}
    if not claims.owner or claims.owner != definition.owner:
        return False
    if provider is not None and provider != definition.provider:
        return False
    if definition.provider not in providers:
        return False
    allowed_names = _surface_names(definition, surface)
    return bool(allowed_names.intersection(surfaces))


def _identity_matches_claims(identity: CollectorIdentity, claims: CollectorClaims) -> bool:
    """Reject forged/manual claims that do not match the persisted identity."""

    try:
        providers = frozenset(json.loads(identity.providers or "[]"))
        surfaces = frozenset(json.loads(identity.surfaces or "[]"))
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        identity.identity_id == claims.collector_id
        and identity.revoked_at is None
        and identity.environment == claims.environment
        and identity.owner == claims.owner
        and claims.providers <= providers
        and claims.surfaces <= surfaces
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _bootstrap_dict(row: BootstrapRequest) -> dict[str, Any]:
    """Serialize bootstrap state without returning catalog payload facts."""

    return {
        "request_id": row.request_id,
        "season": row.season,
        "catalog_type": row.catalog_type,
        "cutoff": _iso(row.cutoff),
        "status": row.status,
        "expires_at": _iso(row.expires_at),
        "catalog_version": row.catalog_version,
        "completed_at": _iso(row.completed_at),
        "failure_reason": row.failure_reason,
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _add_audit(session: Session, *, actor: str, action: str, resource: str,
               reason: str, details: Mapping[str, Any] | None = None,
               created_at: datetime | None = None) -> AuditEvent:
    """Append one bounded, non-secret lifecycle record to the audit log."""

    row = AuditEvent(
        event_id=_uuid(), actor=str(actor)[:128], action=str(action)[:64],
        resource=str(resource)[:128], reason=str(reason)[:255],
        details=_json(dict(details or {}))[:16_384],
        created_at=created_at or utcnow(),
    )
    session.add(row)
    return row


def _checksum(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def decompress_gzip_limited(raw: bytes, *, max_output_bytes: int,
                            max_input_bytes: int = MAX_COMPRESSED_BYTES) -> bytes:
    """Decompress gzip without allocating beyond either wire ceiling."""

    if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_input_bytes:
        raise ControlPlaneError("payload_too_large")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    try:
        for offset in range(0, len(raw), 64 * 1024):
            remaining = max_output_bytes - len(output)
            if remaining < 0:
                raise ControlPlaneError("payload_too_large")
            chunk = decoder.decompress(raw[offset:offset + 64 * 1024], remaining + 1)
            output.extend(chunk)
            if len(output) > max_output_bytes or decoder.unconsumed_tail:
                raise ControlPlaneError("payload_too_large")
        remaining = max_output_bytes - len(output)
        tail = decoder.flush(remaining + 1)
        output.extend(tail)
    except ControlPlaneError:
        raise
    except (zlib.error, ValueError, OverflowError) as error:
        raise ControlPlaneError("invalid_compression") from error
    if len(output) > max_output_bytes or not decoder.eof or decoder.unused_data:
        raise ControlPlaneError("invalid_compression")
    return bytes(output)


def _uuid() -> str:
    return str(uuid.uuid4())


class ControlPlaneError(ValueError):
    """A stable, safe reason for a rejected control-plane operation."""

    def __init__(self, reason: str, message: str | None = None,
                 *, retry_after_seconds: int | None = None) -> None:
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message or reason)


@dataclass(frozen=True, slots=True)
class CollectorClaims:
    collector_id: str
    audience: str
    environment: str
    scopes: frozenset[str]
    token_id: str
    expires_at: datetime
    owner: str = ""
    providers: frozenset[str] = frozenset()
    surfaces: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ObservationReceipt:
    observation_id: str
    client_observation_id: str
    checksum: str
    replay: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "client_observation_id": self.client_observation_id,
            "checksum": self.checksum,
            "replay": self.replay,
        }


@dataclass(frozen=True, slots=True)
class OperatorActionResult:
    """The durable result of one atomic operator mutation."""

    resource: Any
    job: Any
    audit: Any

    @property
    def job_id(self) -> str:
        return str(self.job.job_id)

    def __getattr__(self, name: str) -> Any:
        # Keep the service's historical row-like return ergonomics while the
        # HTTP layer can access the durable job alongside the resource.
        return getattr(self.resource, name)


class _SessionService:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.engine = engine
        self.clock = clock
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def session(self) -> Session:
        return self.session_factory()

    @contextmanager
    def _session_scope(self, session: Session | None = None):
        """Use a caller transaction when supplied, otherwise own one."""

        if session is not None:
            yield session
            return
        with self.session() as owned, owned.begin():
            yield owned


class CollectorTokenService(_SessionService):
    """Issue and validate short-lived, environment-bound collector tokens."""

    def __init__(self, engine: Engine, *, environment: str, signing_secret: str | bytes | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        super().__init__(engine, clock=clock)
        if environment == "production" and not signing_secret:
            raise ControlPlaneError("signing_secret_required")
        self.environment = environment
        self.signing_secret = (signing_secret.encode() if isinstance(signing_secret, str) else signing_secret) or secrets.token_bytes(32)
        self._credential_cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(self.signing_secret).digest()))

    @staticmethod
    def _hash_secret(secret: str, salt: bytes | None = None) -> str:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 200_000)
        return f"{base64.urlsafe_b64encode(salt).decode().rstrip('=')}${digest.hex()}"

    @staticmethod
    def _verify_secret(secret: str, encoded: str) -> bool:
        try:
            salt_text, digest = encoded.split("$", 1)
            salt = base64.urlsafe_b64decode(salt_text + "===")
            actual = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 200_000).hex()
            return hmac.compare_digest(actual, digest)
        except (ValueError, TypeError):
            return False

    def create_identity(self, label: str, *, audience: str = "statsplus-collector",
                        scopes: Iterable[str] = (), identity_id: str | None = None,
                        owner: str = "residential_collector",
                        providers: Iterable[str] = (), surfaces: Iterable[str] = ()) -> dict[str, str]:
        if not label.strip() or not audience.strip():
            raise ControlPlaneError("invalid_identity")
        scope_set = frozenset(str(scope).strip() for scope in scopes if str(scope).strip())
        owner = str(owner).strip()
        provider_set = frozenset(str(provider).strip() for provider in providers if str(provider).strip())
        surface_set = frozenset(str(surface).strip() for surface in surfaces if str(surface).strip())
        if not owner or not provider_set or not surface_set:
            raise ControlPlaneError("surface_scope_required")
        if len(scope_set) > MAX_SCOPE_COUNT or len(provider_set) > MAX_SCOPE_COUNT or len(surface_set) > MAX_SCOPE_COUNT:
            raise ControlPlaneError("scope_limit")
        identity_id = identity_id or secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32)
        now = self.clock()
        with self.session() as session, session.begin():
            session.add(CollectorIdentity(
                identity_id=identity_id, label=label.strip(), environment=self.environment,
                audience=audience.strip(), secret_hash=self._hash_secret(secret),
                scopes=_json(sorted(scope_set)), owner=owner,
                providers=_json(sorted(provider_set)), surfaces=_json(sorted(surface_set)),
                created_at=now,
            ))
            session.add(CollectorUsage(collector_id=identity_id, window_started_at=now))
        return {"identity_id": identity_id, "secret": secret, "audience": audience.strip()}

    def issue(self, identity_id: str, *, scopes: Iterable[str] | None = None,
              providers: Iterable[str] | None = None,
              surfaces: Iterable[str] | None = None,
              ttl_seconds: int = 300) -> str:
        if not 1 <= ttl_seconds <= 900:
            raise ControlPlaneError("invalid_token_ttl")
        now = self.clock()
        with self.session() as session, session.begin():
            identity = session.get(CollectorIdentity, identity_id)
            if identity is None or identity.revoked_at is not None or identity.environment != self.environment:
                raise ControlPlaneError("identity_revoked")
            allowed = frozenset(json.loads(identity.scopes))
            requested = allowed if scopes is None else frozenset(str(s).strip() for s in scopes)
            if not requested <= allowed:
                raise ControlPlaneError("scope_denied")
            allowed_providers = frozenset(json.loads(identity.providers or "[]"))
            allowed_surfaces = frozenset(json.loads(identity.surfaces or "[]"))
            requested_providers = allowed_providers if providers is None else frozenset(
                str(provider).strip() for provider in providers if str(provider).strip()
            )
            requested_surfaces = allowed_surfaces if surfaces is None else frozenset(
                str(surface).strip() for surface in surfaces if str(surface).strip()
            )
            if not requested_providers or not requested_surfaces:
                raise ControlPlaneError("surface_scope_required")
            if not requested_providers <= allowed_providers or not requested_surfaces <= allowed_surfaces:
                raise ControlPlaneError("scope_denied")
            token_id = secrets.token_urlsafe(18)
            claims = {
                "sub": identity_id, "aud": identity.audience, "env": self.environment,
                "scope": sorted(requested), "iat": int(_aware(now).timestamp()),
                "exp": int((_aware(now) + timedelta(seconds=ttl_seconds)).timestamp()),
                "jti": token_id, "owner": identity.owner,
                "providers": sorted(requested_providers), "surfaces": sorted(requested_surfaces),
            }
            body = self._encode(claims)
            _add_audit(
                session, actor=identity_id, action="collector.token_issued",
                resource=identity_id, reason="token_issued",
                details={"token_id": token_id, "scopes": sorted(requested),
                         "providers": sorted(requested_providers),
                         "surfaces": sorted(requested_surfaces)}, created_at=now,
            )
            return body + "." + self._sign(body)

    def issue_for_secret(self, identity_id: str, secret: str, *,
                         scopes: Iterable[str] | None = None,
                         providers: Iterable[str] | None = None,
                         surfaces: Iterable[str] | None = None,
                         ttl_seconds: int = 300) -> str:
        """Issue a token after proving possession of the machine secret.

        The current secret and a non-expired rotated secret are both accepted
        during the configured overlap window.  The secret is never included
        in a token, receipt, log, or database row in plaintext.
        """
        now = _aware(self.clock())
        with self.session() as session:
            identity = session.get(CollectorIdentity, identity_id)
            if identity is None or identity.revoked_at is not None:
                raise ControlPlaneError("identity_revoked")
            current = self._verify_secret(secret, identity.secret_hash)
            previous = (
                identity.previous_secret_hash is not None
                and identity.previous_secret_expires_at is not None
                and _aware(identity.previous_secret_expires_at) > now
                and self._verify_secret(secret, identity.previous_secret_hash)
            )
            if not current and not previous:
                raise ControlPlaneError("invalid_identity_secret")
        return self.issue(identity_id, scopes=scopes, providers=providers,
                          surfaces=surfaces, ttl_seconds=ttl_seconds)

    def _encode(self, claims: Mapping[str, Any]) -> str:
        raw = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def _sign(self, body: str, key: bytes | None = None) -> str:
        return base64.urlsafe_b64encode(hmac.new(key or self.signing_secret, body.encode(), hashlib.sha256).digest()).decode().rstrip("=")

    def validate(self, token: str, *, required_scope: str | None = None,
                 audience: str | None = None, consume: bool = False) -> CollectorClaims:
        try:
            body, signature = token.split(".", 1)
            raw = base64.urlsafe_b64decode(body + "===")
            payload = json.loads(raw)
            identity_id = str(payload["sub"])
            token_id = str(payload["jti"])
            expires = datetime.fromtimestamp(int(payload["exp"]), UTC)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError, OverflowError) as error:
            raise ControlPlaneError("invalid_token") from error
        if not hmac.compare_digest(signature, self._sign(body)):
            raise ControlPlaneError("invalid_token")
        now = _aware(self.clock())
        with self.session() as session, session.begin():
            identity = session.get(CollectorIdentity, identity_id)
            if identity is None or identity.revoked_at is not None:
                raise ControlPlaneError("identity_revoked")
            valid = identity.environment == payload.get("env") == self.environment
            valid = valid and (audience is None or payload.get("aud") == audience)
            valid = valid and hmac.compare_digest(str(payload.get("aud", "")), identity.audience)
            valid = valid and (expires > now)
            if not valid:
                raise ControlPlaneError("invalid_token")
            scopes = frozenset(str(item) for item in payload.get("scope", ()))
            if not scopes <= frozenset(json.loads(identity.scopes)):
                raise ControlPlaneError("scope_denied")
            if required_scope and required_scope not in scopes:
                raise ControlPlaneError("scope_denied")
            if consume:
                if session.get(CollectorTokenReplay, token_id) is not None:
                    raise ControlPlaneError("token_replayed")
                session.add(CollectorTokenReplay(token_id=token_id, collector_id=identity_id, expires_at=expires))
            identity.last_seen_at = now
            try:
                token_providers = frozenset(str(item) for item in payload["providers"])
                token_surfaces = frozenset(str(item) for item in payload["surfaces"])
                token_owner = str(payload["owner"])
            except (KeyError, TypeError, ValueError) as error:
                raise ControlPlaneError("invalid_token") from error
            if (
                not token_owner
                or token_owner != identity.owner
                or not token_providers
                or not token_providers <= frozenset(json.loads(identity.providers or "[]"))
                or not token_surfaces
                or not token_surfaces <= frozenset(json.loads(identity.surfaces or "[]"))
            ):
                raise ControlPlaneError("scope_denied")
            _add_audit(
                session, actor=identity_id, action="collector.token_used",
                resource=identity_id, reason="token_used",
                details={"token_id": token_id, "required_scope": required_scope,
                         "consume": bool(consume)}, created_at=now,
            )
        return CollectorClaims(
            identity_id, str(payload["aud"]), self.environment, scopes, token_id,
            expires, token_owner, token_providers, token_surfaces,
        )

    def revoke(self, identity_id: str, *, session: Session | None = None) -> CollectorIdentity:
        with self._session_scope(session) as session:
            identity = session.get(CollectorIdentity, identity_id)
            if identity is None:
                raise ControlPlaneError("identity_not_found")
            identity.revoked_at = self.clock()
            _add_audit(
                session, actor="operator", action="collector.revoked",
                resource=identity_id, reason="collector_revoked",
                details={"identity_id": identity_id}, created_at=identity.revoked_at,
            )
            return identity

    def rotate(self, identity_id: str, *, overlap_seconds: int = 3600,
               session: Session | None = None) -> dict[str, str]:
        if overlap_seconds < 0:
            raise ControlPlaneError("invalid_rotation_window")
        secret = secrets.token_urlsafe(32)
        now = self.clock()
        with self._session_scope(session) as session:
            identity = session.get(CollectorIdentity, identity_id)
            if identity is None or identity.revoked_at is not None:
                raise ControlPlaneError("identity_revoked")
            identity.previous_secret_hash = identity.secret_hash
            identity.previous_secret_expires_at = _aware(now) + timedelta(seconds=overlap_seconds)
            identity.secret_hash = self._hash_secret(secret)
            delivery_id = _uuid()
            session.add(CredentialDelivery(
                delivery_id=delivery_id, identity_id=identity_id,
                encrypted_secret=self._credential_cipher.encrypt(secret.encode()).decode(),
                created_at=now, expires_at=_aware(now) + timedelta(hours=24),
            ))
            _add_audit(
                session, actor="operator", action="collector.rotated",
                resource=identity_id, reason="collector_rotated",
                details={"identity_id": identity_id, "delivery_id": delivery_id,
                         "overlap_seconds": int(overlap_seconds)}, created_at=now,
            )
        return {"identity_id": identity_id, "secret": secret, "delivery_id": delivery_id}

    def retrieve_delivery(self, delivery_id: str) -> dict[str, str]:
        """Legacy in-process retrieval used by the deployment channel.

        The HTTP admin endpoint deliberately calls :meth:`delivery_metadata`;
        plaintext replacement secrets are only returned by the
        machine-authenticated claim endpoint.
        """

        now = _aware(self.clock())
        with self.session() as session, session.begin():
            delivery = session.get(CredentialDelivery, delivery_id)
            if delivery is None or delivery.retrieved_at is not None or _aware(delivery.expires_at) <= now:
                raise ControlPlaneError("credential_delivery_unavailable")
            try:
                secret = self._credential_cipher.decrypt(delivery.encrypted_secret.encode()).decode()
            except (FernetInvalidToken, UnicodeDecodeError) as error:
                raise ControlPlaneError("credential_delivery_unavailable") from error
            delivery.retrieved_at = now
            return {"delivery_id": delivery_id, "identity_id": delivery.identity_id, "secret": secret}

    def delivery_metadata(self, delivery_id: str) -> dict[str, Any]:
        """Return safe delivery state without decrypting a replacement secret."""

        with self.session() as session:
            delivery = session.get(CredentialDelivery, delivery_id)
            if delivery is None:
                raise ControlPlaneError("credential_delivery_unavailable")
            return {
                "delivery_id": delivery.delivery_id,
                "identity_id": delivery.identity_id,
                "expires_at": _iso(delivery.expires_at),
                "retrieved": delivery.retrieved_at is not None,
            }

    def claim_delivery(self, delivery_id: str, *, collector_id: str,
                       presented_secret: str) -> dict[str, str]:
        """Deliver one replacement only to the rotated machine.

        The old secret remains valid only for the explicit overlap window and
        is the proof that the caller controls the machine identity.  Admin
        reads never decrypt this record.
        """

        now = _aware(self.clock())
        with self.session() as session, session.begin():
            delivery = session.get(CredentialDelivery, delivery_id)
            if delivery is None or delivery.retrieved_at is not None or _aware(delivery.expires_at) <= now:
                raise ControlPlaneError("credential_delivery_unavailable")
            if delivery.identity_id != collector_id:
                raise ControlPlaneError("credential_delivery_unavailable")
            identity = session.get(CollectorIdentity, collector_id)
            if identity is None or identity.revoked_at is not None:
                raise ControlPlaneError("identity_revoked")
            current = self._verify_secret(presented_secret, identity.secret_hash)
            previous = (
                identity.previous_secret_hash is not None
                and identity.previous_secret_expires_at is not None
                and _aware(identity.previous_secret_expires_at) > now
                and self._verify_secret(presented_secret, identity.previous_secret_hash)
            )
            if not current and not previous:
                raise ControlPlaneError("invalid_identity_secret")
            try:
                secret = self._credential_cipher.decrypt(delivery.encrypted_secret.encode()).decode()
            except (FernetInvalidToken, UnicodeDecodeError) as error:
                raise ControlPlaneError("credential_delivery_unavailable") from error
            delivery.retrieved_at = now
            _add_audit(
                session, actor=collector_id, action="collector.credential_claimed",
                resource=delivery_id, reason="credential_claimed",
                details={"delivery_id": delivery_id, "identity_id": collector_id},
                created_at=now,
            )
            return {"delivery_id": delivery_id, "identity_id": collector_id, "secret": secret}


class CollectionControlService(_SessionService):
    """Manage active seasons, bootstrap requests, catalogs, and manifests."""

    def __init__(self, engine: Engine, *, environment: str = "testing",
                 clock: Callable[[], datetime] = utcnow,
                 min_event_catalog_games: int | None = None,
                 min_event_catalog_teams: int | None = None,
                 min_athlete_catalog_identities: int | None = None) -> None:
        super().__init__(engine, clock=clock)
        self.environment = environment.strip() or "testing"
        # Test deployments can use deterministic small fixtures while a
        # production control plane keeps a meaningful whole-season volume
        # floor.  Deployments may override all three values explicitly.
        production = self.environment == "production"
        self.min_event_catalog_games = max(1, self._catalog_bound(
            min_event_catalog_games, "COLLECTOR_MIN_EVENT_CATALOG_GAMES", 100 if production else 1,
        ))
        self.min_event_catalog_teams = max(2, min(30, self._catalog_bound(
            min_event_catalog_teams, "COLLECTOR_MIN_EVENT_CATALOG_TEAMS", 30 if production else 2,
        )))
        self.min_athlete_catalog_identities = max(1, self._catalog_bound(
            min_athlete_catalog_identities, "COLLECTOR_MIN_ATHLETE_CATALOG_IDENTITIES", 1,
        ))
        if (
            self.min_event_catalog_games > MAX_EVENT_CATALOG_GAMES
            or self.min_athlete_catalog_identities > MAX_ATHLETE_CATALOG_IDENTITIES
        ):
            raise ControlPlaneError("invalid_catalog_bounds")

    @staticmethod
    def _catalog_bound(explicit: int | None, environment_name: str, default: int) -> int:
        if explicit is not None:
            return int(explicit)
        raw = os.getenv(environment_name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as error:
            raise ControlPlaneError("invalid_catalog_bounds") from error

    def activate_season(self, season: str, *, actor: str, cutoff: datetime | None = None,
                        session: Session | None = None) -> ActiveSeason:
        if not _valid_season(season):
            raise ControlPlaneError("invalid_season")
        now = self.clock()
        with self._session_scope(session) as session:
            session.query(ActiveSeason).filter(ActiveSeason.status == "active").update({"status": "inactive"})
            row = session.get(ActiveSeason, season)
            if row is None:
                row = ActiveSeason(season=season, phase="Regular Season", status="active", activated_at=now, activated_by=actor)
                session.add(row)
            else:
                row.status, row.activated_at, row.activated_by = "active", now, actor
            row.cutoff = cutoff
        return row

    def create_bootstrap_request(self, season: str, catalog_type: str, *, cutoff: datetime,
                                 ttl_hours: int = 24,
                                 session: Session | None = None) -> BootstrapRequest:
        if catalog_type not in {"event", "athlete"} or not _valid_season(season):
            raise ControlPlaneError("invalid_bootstrap")
        if ttl_hours <= 0 or ttl_hours > 168:
            raise ControlPlaneError("invalid_bootstrap_expiry")
        now = self.clock()
        with self._session_scope(session) as session:
            active = session.get(ActiveSeason, season)
            if active is None or active.status != "active":
                raise ControlPlaneError("season_not_active")
            row = BootstrapRequest(request_id=_uuid(), season=season, catalog_type=catalog_type,
                cutoff=_aware(cutoff), status="pending", expires_at=_aware(now) + timedelta(hours=ttl_hours), created_at=now)
            session.add(row)
        return row

    def publish_catalog(self, request_id: str, payload: Any, *, version: str,
                        checksum: str | None = None, expires_at: datetime | None = None,
                        session: Session | None = None) -> CatalogPublication:
        encoded = _json(payload)
        computed_checksum = _checksum(encoded)
        if checksum is not None and not hmac.compare_digest(str(checksum), computed_checksum):
            raise ControlPlaneError("checksum_mismatch")
        checksum = computed_checksum
        if not str(version).strip() or len(str(version)) > 128:
            raise ControlPlaneError("invalid_catalog_version")
        now = self.clock()
        with self._session_scope(session) as session:
            request = session.get(BootstrapRequest, request_id)
            if request is None:
                raise ControlPlaneError("bootstrap_not_found")
            if request.status != "pending" or _aware(request.expires_at) <= _aware(now):
                request.status = "expired" if request.status == "pending" else request.status
                raise ControlPlaneError("bootstrap_expired")
            try:
                _validate_catalog_payload(
                    payload, request.catalog_type,
                    min_event_games=self.min_event_catalog_games,
                    min_event_teams=self.min_event_catalog_teams,
                    min_athlete_identities=self.min_athlete_catalog_identities,
                )
                if request.catalog_type == "athlete":
                    event = session.scalar(select(CatalogPublication).where(
                        CatalogPublication.season == request.season,
                        CatalogPublication.catalog_type == "event",
                        CatalogPublication.cutoff == request.cutoff,
                        CatalogPublication.complete.is_(True),
                    ).order_by(CatalogPublication.published_at.desc()))
                    if event is None:
                        raise ValueError("event evidence required")
                    required_ids = _required_athlete_ids(event.payload)
                    catalog_ids = _catalog_identity_ids(payload)
                    if not required_ids or not required_ids <= catalog_ids:
                        raise ValueError("athlete catalog incomplete")
            except ValueError as error:
                raise ControlPlaneError("catalog_payload_invalid") from error
            publication = CatalogPublication(publication_id=_uuid(), season=request.season, catalog_type=request.catalog_type,
                cutoff=request.cutoff, version=version, checksum=checksum, payload=encoded,
                complete=True, published_at=now, expires_at=expires_at)
            session.add(publication)
            request.status, request.completed_at, request.catalog_version = "succeeded", now, version
        return publication

    @staticmethod
    def _assert_claims_identity(session: Session, claims: CollectorClaims) -> CollectorIdentity:
        identity = session.get(CollectorIdentity, claims.collector_id)
        if identity is None or not _identity_matches_claims(identity, claims):
            raise ControlPlaneError("invalid_token")
        return identity

    def _claims_allow_catalog(self, claims: CollectorClaims, catalog_type: str) -> bool:
        definition = _surface_definition(f"{catalog_type}_catalog")
        return definition is not None and _claims_allow_surface(
            claims, definition=definition, provider=definition.provider,
            surface=f"{catalog_type}_catalog",
        )

    @staticmethod
    def _authorized_manifest_scopes(session: Session, manifest: CollectionManifest,
                                    claims: CollectorClaims) -> set[str]:
        frozen_scopes = set(json.loads(manifest.scopes))
        authorized: set[str] = set()
        for stream in _manifest_streams(session, manifest):
            definition = _stream_definition(stream)
            if _claims_allow_surface(claims, definition=definition, provider=stream.provider):
                authorized.update(frozen_scopes.intersection(_surface_names(definition)))
        return authorized

    def bootstrap_status(self, request_id: str, *, claims: CollectorClaims | None = None,
                         now: datetime | None = None) -> BootstrapRequest:
        """Read bounded bootstrap state and expire pending work truthfully."""

        current = _aware(now or self.clock())
        with self.session() as session, session.begin():
            if claims is not None:
                self._assert_claims_identity(session, claims)
            request = session.get(BootstrapRequest, request_id)
            if request is None:
                raise ControlPlaneError("bootstrap_not_found")
            if claims is not None and not self._claims_allow_catalog(claims, request.catalog_type):
                raise ControlPlaneError("scope_denied")
            if request.status == "pending" and _aware(request.expires_at) <= current:
                request.status = "expired"
            return request

    def discover(self, *, claims: CollectorClaims | None = None,
                 environment: str | None = None, scopes: Iterable[str] = (),
                 collector_id: str | None = None, owner: str | None = None,
                 providers: Iterable[str] = (), surfaces: Iterable[str] = (),
                 limit: int = 50,
                 now: datetime | None = None) -> dict[str, Any]:
        """Return bounded pending work visible to one collector deployment.

        Bootstrap requests are deployment-local state, so the service rejects
        a token from a different environment before reading any rows.  A
        generic ``poll`` is only an operation capability; a token can discover
        only manifests whose frozen surfaces match its owner/provider binding.
        Results are ordered newest-first with stable IDs for deterministic
        polling and are never allowed to grow beyond the caller's bound.
        """

        caller_environment = str(environment or (claims.environment if claims else "")).strip()
        if caller_environment != self.environment:
            raise ControlPlaneError("environment_mismatch")
        if claims is not None:
            caller_scopes = set(claims.scopes)
            collector_id, owner = claims.collector_id, claims.owner
            providers, surfaces = claims.providers, claims.surfaces
        else:
            caller_scopes = {str(scope).strip() for scope in scopes if str(scope).strip()}
        if not collector_id or not owner:
            raise ControlPlaneError("surface_scope_required")
        if not caller_scopes.intersection({"poll", "bootstrap", "catalog_publish"}):
            raise ControlPlaneError("scope_denied")
        caller_providers = {str(value).strip() for value in providers if str(value).strip()}
        caller_surfaces = {str(value).strip() for value in surfaces if str(value).strip()}
        if not caller_providers or not caller_surfaces:
            raise ControlPlaneError("surface_scope_required")
        bounded = max(1, min(int(limit), 100))
        current = _aware(now or self.clock())
        effective_claims = claims or CollectorClaims(
            collector_id, "", caller_environment, frozenset(caller_scopes), "", current,
            owner, frozenset(caller_providers), frozenset(caller_surfaces),
        )
        visible_requests: list[BootstrapRequest] = []
        visible_manifests: list[dict[str, Any]] = []
        with self.session() as session, session.begin():
            if claims is not None:
                self._assert_claims_identity(session, claims)
            request_stmt = select(BootstrapRequest).where(
                BootstrapRequest.status == "pending",
                BootstrapRequest.expires_at > current,
            ).order_by(
                BootstrapRequest.created_at.desc(),
                BootstrapRequest.request_id.asc(),
            )
            for request in session.scalars(request_stmt):
                definition = _surface_definition(f"{request.catalog_type}_catalog")
                if definition is not None and _claims_allow_surface(
                    effective_claims, definition=definition, provider=definition.provider,
                    surface=f"{request.catalog_type}_catalog",
                ):
                    visible_requests.append(request)
                    if len(visible_requests) >= bounded:
                        break
            manifest_stmt = select(CollectionManifest).where(
                CollectionManifest.status == "active",
                CollectionManifest.collect_before > current,
            ).order_by(
                CollectionManifest.created_at.desc(),
                CollectionManifest.manifest_id.asc(),
            )
            for manifest in session.scalars(manifest_stmt):
                frozen_scopes = set(json.loads(manifest.scopes))
                selected_streams = _manifest_streams(session, manifest)
                authorized_scopes: set[str] = set()
                for stream in selected_streams:
                    definition = _stream_definition(stream)
                    if _claims_allow_surface(
                        effective_claims, definition=definition, provider=stream.provider,
                    ):
                        authorized_scopes.update(frozen_scopes.intersection(_surface_names(definition)))
                if authorized_scopes:
                    visible_manifests.append({
                        "manifest_id": manifest.manifest_id,
                        "season": manifest.season,
                        "cutoff": _iso(manifest.cutoff),
                        "collect_before": _iso(manifest.collect_before),
                        "accepted_versions": json.loads(manifest.accepted_versions),
                        "scopes": sorted(authorized_scopes),
                        "checksum": manifest.checksum,
                        "status": manifest.status,
                    })
                    if len(visible_manifests) >= bounded:
                        break
        return {
            "environment": self.environment,
            "bootstrap_requests": [_bootstrap_dict(row) for row in visible_requests],
            "manifests": visible_manifests,
        }

    def latest_catalog(self, season: str, catalog_type: str, *, cutoff: datetime | None = None,
                       now: datetime | None = None) -> CatalogPublication | None:
        now = _aware(now or self.clock())
        with self.session() as session:
            stmt = select(CatalogPublication).where(CatalogPublication.season == season, CatalogPublication.catalog_type == catalog_type,
                CatalogPublication.complete.is_(True))
            if cutoff is not None:
                stmt = stmt.where(CatalogPublication.cutoff <= _aware(cutoff))
            rows = list(session.scalars(stmt.order_by(CatalogPublication.cutoff.desc(), CatalogPublication.published_at.desc())))
            for row in rows:
                if row.expires_at is None or _aware(row.expires_at) > now:
                    return row
            return None

    def create_manifest(self, season: str, *, cutoff: datetime, scopes: Iterable[str],
                        collect_before: datetime, accepted_versions: Iterable[int] = (1, 2),
                        required_athlete_ids: Iterable[str] = ()) -> CollectionManifest:
        now = _aware(self.clock())
        cutoff, collect_before = _aware(cutoff), _aware(collect_before)
        if cutoff > collect_before or collect_before <= now:
            raise ControlPlaneError("invalid_manifest_window")
        with self.session() as session:
            active = session.get(ActiveSeason, season)
            if active is None or active.status != "active":
                raise ControlPlaneError("season_not_active")
        event = self.latest_catalog(season, "event", cutoff=cutoff, now=now)
        athlete = self.latest_catalog(season, "athlete", cutoff=cutoff, now=now)
        # A newer cutoff can never inherit an old Event Catalog.  Athlete
        # identity may reuse the last-good publication for at most seven days,
        # but only after its required identities are derived from the governed
        # Event Catalog.  ``required_athlete_ids`` is retained as a compatibility
        # keyword and deliberately cannot expand or assert completeness.
        if event is None or _aware(event.cutoff) != cutoff:
            raise ControlPlaneError("event_catalog_required")
        if athlete is None or (_aware(now) - _aware(athlete.published_at)).total_seconds() > ATHLETE_REUSE_DAYS * 86400:
            raise ControlPlaneError("athlete_catalog_required")
        try:
            event_document = json.loads(event.payload)
            athlete_document = json.loads(athlete.payload)
            _validate_catalog_payload(
                event_document, "event",
                min_event_games=self.min_event_catalog_games,
                min_event_teams=self.min_event_catalog_teams,
                min_athlete_identities=self.min_athlete_catalog_identities,
            )
            _validate_catalog_payload(
                athlete_document, "athlete",
                min_event_games=self.min_event_catalog_games,
                min_event_teams=self.min_event_catalog_teams,
                min_athlete_identities=self.min_athlete_catalog_identities,
            )
        except (TypeError, json.JSONDecodeError, ValueError) as error:
            raise ControlPlaneError("catalog_incomplete") from error
        required_ids = _required_athlete_ids(event.payload)
        catalog_ids = _catalog_identity_ids(athlete_document)
        if not required_ids or not required_ids <= catalog_ids:
            raise ControlPlaneError("identity_unresolved")
        scope_list = sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
        try:
            versions = sorted({int(version) for version in accepted_versions})
        except (TypeError, ValueError) as error:
            raise ControlPlaneError("invalid_manifest") from error
        registered_scopes = {
            scope
            for definition in SURFACE_REGISTRY
            for scope in (definition.stream_key, definition.scope, *definition.required)
        }
        if (
            not scope_list
            or not versions
            or not set(scope_list) <= registered_scopes
            or len(_json(scope_list).encode()) > MAX_SCOPE_BYTES
        ):
            raise ControlPlaneError("invalid_manifest")
        material = {"season": season, "cutoff": cutoff.isoformat(), "collect_before": collect_before.isoformat(),
                    "accepted_versions": versions, "scopes": scope_list}
        digest = _checksum(_json(material))
        with self.session() as session, session.begin():
            active = session.get(ActiveSeason, season)
            if active is None or active.status != "active":
                raise ControlPlaneError("season_not_active")
            prior = session.scalars(select(CollectionManifest).where(CollectionManifest.season == season, CollectionManifest.status == "active")).all()
            session.query(CollectionManifest).filter(CollectionManifest.season == season, CollectionManifest.status == "active").update(
                {"status": "superseded", "superseded_at": now})
            row = CollectionManifest(manifest_id=_uuid(), season=season, cutoff=cutoff, collect_before=collect_before,
                accepted_versions=_json(versions), scopes=_json(scope_list), checksum=digest, status="active", created_at=now)
            session.add(row)
            for old in prior:
                old.superseded_by = row.manifest_id
        return row

    def get_manifest(self, manifest_id: str, *, claims: CollectorClaims | None = None,
                     now: datetime | None = None) -> CollectionManifest:
        with self.session() as session:
            if claims is not None:
                self._assert_claims_identity(session, claims)
            row = session.get(CollectionManifest, manifest_id)
            if row is None:
                raise ControlPlaneError("manifest_not_found")
            if row.status != "active" or _aware(row.collect_before) <= _aware(now or self.clock()):
                raise ControlPlaneError("manifest_expired")
            if claims is not None:
                authorized_scopes = self._authorized_manifest_scopes(session, row, claims)
                if not authorized_scopes:
                    raise ControlPlaneError("scope_denied")
                row._authorized_scopes = sorted(authorized_scopes)
            return row

    def open_cycle(self, manifest_id: str, *, completed_game_count: int | None = None,
                   session: Session | None = None) -> CollectionCycle:
        """Open a cycle from the governed Event Catalog.

        ``completed_game_count`` remains a compatibility keyword for older
        callers, but is intentionally ignored.  A caller cannot manufacture a
        no-game or complete-game outcome by submitting an administrator-owned
        count.
        """
        now = self.clock()
        with self._session_scope(session) as session:
            manifest = session.get(CollectionManifest, manifest_id)
            if manifest is None or manifest.status != "active":
                raise ControlPlaneError("manifest_expired")
            active = session.get(ActiveSeason, manifest.season)
            if active is None or active.status != "active":
                raise ControlPlaneError("season_not_active")
            event = session.scalar(select(CatalogPublication).where(
                CatalogPublication.season == manifest.season,
                CatalogPublication.catalog_type == "event",
                CatalogPublication.cutoff == manifest.cutoff,
                CatalogPublication.complete.is_(True),
                (CatalogPublication.expires_at.is_(None)
                 | (CatalogPublication.expires_at > _aware(now))),
            ).order_by(CatalogPublication.published_at.desc()))
            if event is None:
                raise ControlPlaneError("event_catalog_required")
            try:
                event_document = json.loads(event.payload)
                _validate_catalog_payload(
                    event_document, "event",
                    min_event_games=self.min_event_catalog_games,
                    min_event_teams=self.min_event_catalog_teams,
                    min_athlete_identities=self.min_athlete_catalog_identities,
                )
            except (TypeError, json.JSONDecodeError, ValueError) as error:
                raise ControlPlaneError("event_catalog_invalid") from error
            game_count = _completed_game_count(event.payload, cutoff=_aware(manifest.cutoff))
            cycle = CollectionCycle(cycle_id=_uuid(), season=manifest.season, manifest_id=manifest_id,
                cutoff=manifest.cutoff, status="no_game" if game_count == 0 else "collecting",
                completed_game_count=game_count, created_at=now)
            session.add(cycle)
            try:
                session.flush()
            except IntegrityError as error:
                raise ControlPlaneError("cycle_exists") from error
        return cycle

    def finish_cycle(self, cycle_id: str, *, status: str, reason: str | None = None,
                     session: Session | None = None) -> CollectionCycle:
        if status not in {"complete", "attention", "failed"}:
            raise ControlPlaneError("invalid_cycle_status")
        now = self.clock()
        with self._session_scope(session) as session:
            cycle = session.get(CollectionCycle, cycle_id)
            if cycle is None:
                raise ControlPlaneError("cycle_not_found")
            if cycle.status in {"complete", "no_game", "superseded"}:
                raise ControlPlaneError("cycle_immutable")
            if status == "complete":
                exempt = set(session.scalars(select(GovernedNotApplicable.stream_key).where(
                    GovernedNotApplicable.cycle_id == cycle_id
                )))
                manifest = session.get(CollectionManifest, cycle.manifest_id)
                if manifest is None:
                    raise ControlPlaneError("manifest_expired")
                enabled = _manifest_streams(session, manifest)
                missing = []
                for stream in enabled:
                    if stream.publication_strategy in {"request_time", "never_schedule"}:
                        continue
                    if stream.stream_key in exempt:
                        continue
                    pointer = session.get(PublicationPointer, stream.stream_key)
                    publication = session.get(PublicationVersion, pointer.active_publication_id) if pointer and pointer.active_publication_id else None
                    if publication is None or _aware(publication.cutoff) != _aware(cycle.cutoff):
                        missing.append(stream.stream_key)
                if missing:
                    raise ControlPlaneError("cycle_incomplete")
            cycle.status, cycle.completed_at, cycle.attention_reason = status, now, reason
        return cycle

    def govern_not_applicable(self, cycle_id: str, stream_key: str, *, actor: str, reason: str,
                              session: Session | None = None) -> GovernedNotApplicable:
        if not actor.strip() or len(reason.strip()) < 3:
            raise ControlPlaneError("reason_required")
        with self._session_scope(session) as session:
            cycle = session.get(CollectionCycle, cycle_id)
            stream = session.get(PublicationStream, stream_key)
            if cycle is None or stream is None:
                raise ControlPlaneError("not_applicable_invalid")
            manifest = session.get(CollectionManifest, cycle.manifest_id)
            if manifest is None or stream not in _manifest_streams(session, manifest):
                raise ControlPlaneError("not_applicable_invalid")
            row = GovernedNotApplicable(cycle_id=cycle_id, stream_key=stream_key, actor=actor[:128], reason=reason[:255], created_at=self.clock())
            session.merge(row)
        return row

    def supersede_cycle(self, cycle_id: str, *, reason: str, session: Session | None = None) -> CollectionCycle:
        if len(reason.strip()) < 3:
            raise ControlPlaneError("reason_required")
        with self._session_scope(session) as session:
            cycle = session.get(CollectionCycle, cycle_id)
            if cycle is None:
                raise ControlPlaneError("cycle_not_found")
            cycle.status, cycle.superseded_at, cycle.attention_reason = "superseded", self.clock(), reason.strip()[:64]
        return cycle


class ObservationIngestionService(_SessionService):
    """Validate and durably accept one complete observation envelope."""

    def __init__(self, engine: Engine, *, publication_service: "PublicationService | None" = None,
                 collection_control: "CollectionControlService | None" = None,
                 min_event_catalog_games: int = 1,
                 min_event_catalog_teams: int = 2,
                 min_athlete_catalog_identities: int = 1,
                 clock: Callable[[], datetime] = utcnow) -> None:
        super().__init__(engine, clock=clock)
        self.publication_service = publication_service
        self.collection_control = collection_control
        self.min_event_catalog_games = max(1, int(min_event_catalog_games))
        self.min_event_catalog_teams = max(2, min(30, int(min_event_catalog_teams)))
        self.min_athlete_catalog_identities = max(1, int(min_athlete_catalog_identities))
        self._identity_locks: dict[str, threading.BoundedSemaphore] = {}
        self._identity_locks_guard = threading.Lock()

    def _acquire_identity_lease(self, claims: CollectorClaims) -> str:
        """Acquire the cross-worker database lease for one collector identity."""

        now = _aware(self.clock())
        owner = secrets.token_urlsafe(18)
        with self.session() as session, session.begin():
            identity = session.get(CollectorIdentity, claims.collector_id)
            if identity is None or not _identity_matches_claims(identity, claims):
                raise ControlPlaneError("invalid_token")
            lease = session.scalar(select(CollectorLease).where(
                CollectorLease.collector_id == claims.collector_id
            ).with_for_update())
            if lease is None:
                try:
                    with session.begin_nested():
                        lease = CollectorLease(
                            collector_id=claims.collector_id,
                            lease_owner=owner,
                            lease_expires_at=now + timedelta(seconds=COLLECTOR_LEASE_SECONDS),
                            fence=1,
                            updated_at=now,
                        )
                        session.add(lease)
                        session.flush()
                except IntegrityError:
                    lease = session.scalar(select(CollectorLease).where(
                        CollectorLease.collector_id == claims.collector_id
                    ).with_for_update())
            if lease is None:
                raise ControlPlaneError("collector_busy", retry_after_seconds=COLLECTOR_LEASE_RETRY_SECONDS)
            if lease.lease_owner and lease.lease_owner != owner and lease.lease_expires_at is not None and _aware(lease.lease_expires_at) > now:
                retry_after = max(
                    COLLECTOR_LEASE_RETRY_SECONDS,
                    int(math.ceil((_aware(lease.lease_expires_at) - now).total_seconds())),
                )
                raise ControlPlaneError("collector_busy", retry_after_seconds=retry_after)
            lease.lease_owner = owner
            lease.lease_expires_at = now + timedelta(seconds=COLLECTOR_LEASE_SECONDS)
            lease.fence = int(lease.fence or 0) + 1
            lease.updated_at = now
            session.flush()
        return owner

    def _release_identity_lease(self, claims: CollectorClaims, owner: str) -> None:
        now = _aware(self.clock())
        with self.session() as session, session.begin():
            lease = session.scalar(select(CollectorLease).where(
                CollectorLease.collector_id == claims.collector_id
            ).with_for_update())
            if lease is not None and lease.lease_owner == owner:
                lease.lease_owner = None
                lease.lease_expires_at = now
                lease.updated_at = now

    def _preflight_payload(self, envelope: Mapping[str, Any], payload: bytes | str,
                           *, compressed: bool, max_payload_bytes: int,
                           max_compressed_bytes: int) -> None:
        """Reject oversized/malformed input before taking a database lease."""

        if compressed and isinstance(payload, str):
            payload = payload.encode()
        if not isinstance(payload, (bytes, bytearray)):
            raise ControlPlaneError("malformed_payload")
        raw = bytes(payload)
        if len(raw) > (max_compressed_bytes if compressed else max_payload_bytes):
            raise ControlPlaneError("payload_too_large")
        decoded = (
            decompress_gzip_limited(
                raw, max_output_bytes=max_payload_bytes,
                max_input_bytes=max_compressed_bytes,
            ) if compressed else raw
        )
        try:
            value = json.loads(decoded, parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
            _validate_observation_payload(
                value, observation_type=str(envelope.get("observation_type", "")),
                min_event_catalog_games=self.min_event_catalog_games,
                min_event_catalog_teams=self.min_event_catalog_teams,
                min_athlete_catalog_identities=self.min_athlete_catalog_identities,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise ControlPlaneError("malformed_payload") from error

    def ingest(self, claims: CollectorClaims, envelope: Mapping[str, Any], payload: bytes | str,
               *, compressed: bool = False, max_payload_bytes: int = MAX_ENVELOPE_BYTES,
               max_compressed_bytes: int = MAX_COMPRESSED_BYTES) -> ObservationReceipt:
        """Accept an envelope under the database-backed identity lease.

        The semaphore only reduces local contention.  The lease row is the
        authority across Railway workers and expires for crash recovery.
        """
        with self._identity_locks_guard:
            lock = self._identity_locks.setdefault(claims.collector_id, threading.BoundedSemaphore(1))
        if not lock.acquire(blocking=False):
            raise ControlPlaneError("collector_busy", retry_after_seconds=COLLECTOR_LEASE_RETRY_SECONDS)
        lease_owner = None
        try:
            self._preflight_payload(
                envelope, payload, compressed=compressed,
                max_payload_bytes=max_payload_bytes,
                max_compressed_bytes=max_compressed_bytes,
            )
            lease_owner = self._acquire_identity_lease(claims)
            return self._ingest(claims, envelope, payload, compressed=compressed,
                                max_payload_bytes=max_payload_bytes, max_compressed_bytes=max_compressed_bytes)
        finally:
            if lease_owner is not None:
                self._release_identity_lease(claims, lease_owner)
            lock.release()

    def ingest_catalog(self, claims: CollectorClaims, envelope: Mapping[str, Any],
                       payload: bytes | str, *, request_id: str, catalog_version: str,
                       expires_at: datetime | None = None,
                       max_payload_bytes: int = MAX_ENVELOPE_BYTES,
                       max_compressed_bytes: int = MAX_COMPRESSED_BYTES) -> CatalogPublication:
        """Accept and publish one catalog through the normal envelope gate."""

        with self._identity_locks_guard:
            lock = self._identity_locks.setdefault(claims.collector_id, threading.BoundedSemaphore(1))
        if not lock.acquire(blocking=False):
            raise ControlPlaneError("collector_busy", retry_after_seconds=COLLECTOR_LEASE_RETRY_SECONDS)
        lease_owner = None
        try:
            self._preflight_payload(
                envelope, payload, compressed=False,
                max_payload_bytes=max_payload_bytes,
                max_compressed_bytes=max_compressed_bytes,
            )
            lease_owner = self._acquire_identity_lease(claims)
            return self._ingest(
                claims,
                envelope,
                payload,
                compressed=False,
                max_payload_bytes=max_payload_bytes,
                max_compressed_bytes=max_compressed_bytes,
                catalog_request_id=request_id,
                catalog_version=catalog_version,
                catalog_expires_at=expires_at,
            )
        finally:
            if lease_owner is not None:
                self._release_identity_lease(claims, lease_owner)
            lock.release()

    def _ingest(self, claims: CollectorClaims, envelope: Mapping[str, Any], payload: bytes | str,
                *, compressed: bool = False, max_payload_bytes: int = MAX_ENVELOPE_BYTES,
                max_compressed_bytes: int = MAX_COMPRESSED_BYTES,
                catalog_request_id: str | None = None,
                catalog_version: str | None = None,
                catalog_expires_at: datetime | None = None) -> ObservationReceipt | CatalogPublication:
        if not isinstance(envelope, Mapping):
            raise ControlPlaneError("malformed_envelope")
        if compressed and isinstance(payload, str):
            payload = payload.encode()
        if not isinstance(payload, (bytes, bytearray)):
            raise ControlPlaneError("malformed_payload")
        raw = bytes(payload)
        if len(raw) > max_compressed_bytes if compressed else len(raw) > max_payload_bytes:
            raise ControlPlaneError("payload_too_large")
        decoded = (
            decompress_gzip_limited(
                raw,
                max_output_bytes=max_payload_bytes,
                max_input_bytes=max_compressed_bytes,
            )
            if compressed else raw
        )
        try:
            value = json.loads(decoded, parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
            _validate_observation_payload(
                value, observation_type=str(envelope.get("observation_type", "")),
                min_event_catalog_games=self.min_event_catalog_games,
                min_event_catalog_teams=self.min_event_catalog_teams,
                min_athlete_catalog_identities=self.min_athlete_catalog_identities,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise ControlPlaneError("malformed_payload") from error
        if not isinstance(value, (dict, list)):
            raise ControlPlaneError("malformed_payload")
        canonical_payload = _json(value).encode()
        required = {"manifest_id", "client_observation_id", "environment", "provider", "observation_type", "scope", "season", "cutoff", "schema_version", "retrieved_at", "checksum"}
        if set(envelope) != required:
            raise ControlPlaneError("malformed_envelope")
        if catalog_request_id is None and "ingest" not in claims.scopes:
            raise ControlPlaneError("scope_denied")
        if catalog_request_id is not None and not claims.scopes.intersection(
            {"catalog_publish", "bootstrap", "ingest"}
        ):
            raise ControlPlaneError("scope_denied")
        if envelope["environment"] != claims.environment or envelope["season"] is None:
            raise ControlPlaneError("environment_mismatch")
        raw_schema_version = envelope["schema_version"]
        if isinstance(raw_schema_version, bool) or not isinstance(raw_schema_version, int):
            raise ControlPlaneError("schema_unsupported")
        schema_version = raw_schema_version
        if not isinstance(envelope["provider"], str) or not envelope["provider"].strip():
            raise ControlPlaneError("provider_not_registered")
        if not isinstance(envelope["observation_type"], str):
            raise ControlPlaneError("invalid_observation_type")
        observation_type = envelope["observation_type"].strip()
        if not observation_type:
            raise ControlPlaneError("invalid_observation_type")
        if not isinstance(envelope["client_observation_id"], str):
            raise ControlPlaneError("invalid_observation_id")
        client_id = envelope["client_observation_id"].strip()
        if not client_id or len(client_id) > 128:
            raise ControlPlaneError("invalid_observation_id")
        scope_text = _json(envelope["scope"])
        if len(scope_text.encode()) > MAX_SCOPE_BYTES:
            raise ControlPlaneError("scope_limit")
        raw_manifest_id = envelope["manifest_id"]
        if catalog_request_id is None:
            if not isinstance(raw_manifest_id, str) or not raw_manifest_id.strip():
                raise ControlPlaneError("manifest_not_found")
            manifest_id = raw_manifest_id.strip()
        else:
            if raw_manifest_id is not None:
                raise ControlPlaneError("manifest_scope_mismatch")
            manifest_id = ""
        checksum = str(envelope.get("checksum") or "")
        if not checksum or checksum != _checksum(decoded):
            raise ControlPlaneError("checksum_mismatch")
        now = self.clock()
        with self.session() as session, session.begin():
            scope_value = envelope["scope"]
            catalog_request = None
            if catalog_request_id is not None:
                if self.collection_control is None:
                    raise ControlPlaneError("control_plane_unavailable")
                if observation_type not in {"event_catalog", "athlete_catalog"}:
                    raise ControlPlaneError("catalog_observation_invalid")
                if envelope.get("manifest_id") is not None:
                    raise ControlPlaneError("manifest_scope_mismatch")
                catalog_request = session.get(BootstrapRequest, catalog_request_id)
                if catalog_request is None:
                    raise ControlPlaneError("bootstrap_not_found")
                if catalog_request.status != "pending" or _aware(catalog_request.expires_at) <= _aware(now):
                    if catalog_request.status == "succeeded":
                        existing = session.scalar(select(CollectionObservation).where(
                            CollectionObservation.collector_id == claims.collector_id,
                            CollectionObservation.client_observation_id == client_id,
                        ))
                        if existing is not None and existing.checksum == checksum:
                            publication = session.scalar(select(CatalogPublication).where(
                                CatalogPublication.season == catalog_request.season,
                                CatalogPublication.catalog_type == catalog_request.catalog_type,
                                CatalogPublication.cutoff == catalog_request.cutoff,
                            ).order_by(CatalogPublication.published_at.desc()))
                            if publication is not None:
                                return publication
                    raise ControlPlaneError("bootstrap_expired")
                expected_type = f"{catalog_request.catalog_type}_catalog"
                if observation_type != expected_type:
                    raise ControlPlaneError("catalog_observation_invalid")
                catalog_definition = _surface_definition(observation_type)
                if catalog_definition is None or not _claims_allow_surface(
                    claims, definition=catalog_definition,
                    provider=str(envelope["provider"]).strip(), surface=observation_type,
                ):
                    raise ControlPlaneError("scope_denied")
                if catalog_request.season != str(envelope["season"]):
                    raise ControlPlaneError("manifest_scope_mismatch")
                if _aware(catalog_request.cutoff) != _aware(_parse_datetime(envelope["cutoff"])):
                    raise ControlPlaneError("manifest_scope_mismatch")
                if catalog_version is None or not catalog_version.strip():
                    raise ControlPlaneError("invalid_catalog_version")
                if isinstance(scope_value, Mapping):
                    window = scope_value.get("window", scope_value.get("scope"))
                    if window not in {"regular_season", "season", "whole_season"}:
                        raise ControlPlaneError("scope_unsupported")
                elif str(scope_value) not in {"regular_season", "season", "whole_season"}:
                    raise ControlPlaneError("scope_unsupported")
            else:
                manifest = session.get(CollectionManifest, manifest_id)
                if manifest is None or manifest.status != "active":
                    raise ControlPlaneError("manifest_expired")
                if _aware(now) >= _aware(manifest.collect_before):
                    raise ControlPlaneError("manifest_expired")
                if schema_version not in set(json.loads(manifest.accepted_versions)):
                    raise ControlPlaneError("schema_unsupported")
                if manifest.season != str(envelope["season"]) or _aware(manifest.cutoff) != _aware(_parse_datetime(envelope["cutoff"])):
                    raise ControlPlaneError("manifest_scope_mismatch")
                allowed_scopes = set(json.loads(manifest.scopes))
                if observation_type not in allowed_scopes and "*" not in allowed_scopes:
                    raise ControlPlaneError("scope_denied")
            if self.publication_service is not None and catalog_request is None:
                stream = self._registered_stream(session, observation_type, envelope["provider"].strip())
                if stream is None:
                    raise ControlPlaneError("provider_not_registered")
                if not _claims_allow_surface(
                    claims, definition=_stream_definition(stream),
                    provider=envelope["provider"].strip(), surface=observation_type,
                ):
                    raise ControlPlaneError("scope_denied")
                if schema_version not in set(json.loads(stream.schema_versions)):
                    raise ControlPlaneError("schema_unsupported")
                supported_windows = set(json.loads(stream.supported_windows))
                if isinstance(scope_value, Mapping):
                    window = scope_value.get("window", scope_value.get("scope"))
                    if supported_windows and window is None:
                        raise ControlPlaneError("scope_unsupported")
                if window is not None and str(window) not in supported_windows:
                    raise ControlPlaneError("scope_unsupported")
            elif catalog_request is None:
                definition = _surface_definition(observation_type)
                if definition is None or not _claims_allow_surface(
                    claims, definition=definition,
                    provider=envelope["provider"].strip(), surface=observation_type,
                ):
                    raise ControlPlaneError("scope_denied")
            if not isinstance(scope_value, (str, Mapping, list, tuple)):
                raise ControlPlaneError("invalid_scope")
            existing = session.scalar(select(CollectionObservation).where(CollectionObservation.collector_id == claims.collector_id,
                CollectionObservation.client_observation_id == client_id))
            if existing is not None:
                if existing.checksum != checksum:
                    # The rejection itself is append-only evidence.  It is
                    # intentionally written through a short independent
                    # transaction because the enclosing ingestion transaction
                    # must roll back and cannot erase the audit record.
                    with self.session() as audit_session, audit_session.begin():
                        _add_audit(
                            audit_session, actor=claims.collector_id,
                            action="observation.rejected", resource=client_id,
                            reason="observation_id_checksum_conflict",
                            details={"collector_id": claims.collector_id,
                                     "existing_checksum": existing.checksum[:12],
                                     "received_checksum": checksum[:12]},
                            created_at=now,
                        )
                    raise ControlPlaneError("observation_id_conflict")
                if catalog_request is not None:
                    publication = session.scalar(select(CatalogPublication).where(
                        CatalogPublication.season == catalog_request.season,
                        CatalogPublication.catalog_type == catalog_request.catalog_type,
                        CatalogPublication.cutoff == catalog_request.cutoff,
                    ).order_by(CatalogPublication.published_at.desc()))
                    if publication is None:
                        raise ControlPlaneError("catalog_publication_missing")
                    return publication
                return ObservationReceipt(existing.observation_id, client_id, checksum, replay=True)
            row = CollectionObservation(observation_id=_uuid(), client_observation_id=client_id, collector_id=claims.collector_id,
                environment=claims.environment, provider=envelope["provider"].strip(), observation_type=observation_type,
                manifest_id=None if catalog_request is not None else manifest_id, scope=scope_text, season=str(envelope["season"]), cutoff=_parse_datetime(envelope["cutoff"]), schema_version=schema_version,
                checksum=checksum, payload=canonical_payload.decode(), payload_bytes=len(canonical_payload), retrieved_at=_parse_datetime(envelope["retrieved_at"]), accepted_at=now)
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                raise ControlPlaneError("observation_race") from error
            if catalog_request is not None:
                return self.collection_control.publish_catalog(
                    catalog_request_id,
                    value,
                    version=catalog_version or "",
                    checksum=checksum,
                    expires_at=catalog_expires_at,
                    session=session,
                )
            if self.publication_service is not None:
                self.publication_service.enqueue_for_observation(
                    observation_type,
                    season=str(envelope["season"]),
                    cutoff=_parse_datetime(envelope["cutoff"]),
                    manifest_id=manifest_id,
                    session=session,
                )
        return ObservationReceipt(row.observation_id, client_id, checksum)

    @staticmethod
    def _registered_stream(session: Session, observation_type: str, provider: str) -> PublicationStream | None:
        """Resolve ownership from the executable registry persisted at boot."""
        streams = session.scalars(select(PublicationStream).where(PublicationStream.enabled.is_(True))).all()
        for stream in streams:
            if stream.provider == provider and observation_type in set(json.loads(stream.required_observations)):
                return stream
        return None


class PublicationService(_SessionService):
    """Register streams and atomically advance or roll back publications."""

    def register_stream(self, stream_key: str, *, provider: str, owner: str,
                        required_observations: Iterable[str], publication_strategy: str,
                        supported_windows: Iterable[str] = (), enabled: bool | None = None,
                        schema_versions: Iterable[int] = (1, 2), completeness_rule: str = "base_complete",
                        freshness_rule: str = "cutoff_current") -> PublicationStream:
        if not stream_key or not provider or not owner:
            raise ControlPlaneError("invalid_stream")
        definition = next((item for item in SURFACE_REGISTRY if item.stream_key == stream_key), None)
        if definition is not None and definition.strategy == "never_schedule" and enabled:
            raise ControlPlaneError("stream_unavailable")
        now = self.clock()
        with self.session() as session, session.begin():
            row = session.get(PublicationStream, stream_key)
            if row is None:
                row = PublicationStream(stream_key=stream_key, provider=provider, owner=owner,
                    required_observations=_json(sorted(set(required_observations))), publication_strategy=publication_strategy,
                    supported_windows=_json(sorted(set(supported_windows))), schema_versions=_json(sorted(set(schema_versions))),
                    completeness_rule=completeness_rule, freshness_rule=freshness_rule, enabled=bool(enabled), created_at=now)
                session.add(row)
            else:
                row.provider, row.owner = provider, owner
                row.required_observations = _json(sorted(set(required_observations)))
                row.publication_strategy = publication_strategy
                row.supported_windows = _json(sorted(set(supported_windows)))
                row.schema_versions = _json(sorted(set(schema_versions)))
                row.completeness_rule, row.freshness_rule = completeness_rule, freshness_rule
                if enabled is not None:
                    row.enabled = enabled
        return row

    def register_default_streams(self) -> tuple[PublicationStream, ...]:
        rows = []
        for definition in SURFACE_REGISTRY:
            rows.append(self.register_stream(
                definition["stream_key"], provider=definition["provider"], owner=definition["owner"],
                required_observations=definition["required"], publication_strategy=definition["strategy"],
                supported_windows=definition["windows"], enabled=None, schema_versions=definition["schema"],
                completeness_rule=definition["complete"], freshness_rule=definition["freshness"],
            ))
        return tuple(rows)

    def activate_stream(self, stream_key: str, *, reason: str,
                        session: Session | None = None) -> PublicationStream:
        if len(reason.strip()) < 3:
            raise ControlPlaneError("reason_required")
        with self._session_scope(session) as session:
            row = session.get(PublicationStream, stream_key)
            if row is None:
                raise ControlPlaneError("stream_not_found")
            definition = next((item for item in SURFACE_REGISTRY if item.stream_key == stream_key), None)
            if definition is not None and definition.strategy == "never_schedule":
                raise ControlPlaneError("stream_unavailable")
            row.enabled = True
            return row

    def enqueue(self, stream_key: str, *, season: str, cutoff: datetime,
                manifest_id: str | None = None,
                session: Session | None = None) -> CompositionJob:
        """Create one idempotent composition job for a stream/cutoff."""
        now = self.clock()
        with self._session_scope(session) as session:
            existing_query = select(CompositionJob).where(
                CompositionJob.stream_key == stream_key, CompositionJob.season == season,
                CompositionJob.cutoff == _aware(cutoff))
            existing = session.scalar(existing_query)
            if existing is not None:
                return existing
            row = CompositionJob(job_id=_uuid(), stream_key=stream_key, manifest_id=manifest_id,
                                 season=season, cutoff=_aware(cutoff),
                                 status="queued", attempts=0, created_at=now, updated_at=now)
            session.add(row)
        return row

    def enqueue_for_observation(self, observation_type: str, *, season: str, cutoff: datetime,
                                manifest_id: str | None = None,
                                session: Session | None = None) -> int:
        with self._session_scope(session) as session:
            streams = session.scalars(select(PublicationStream).where(PublicationStream.enabled.is_(True))).all()
            matching = [stream.stream_key for stream in streams if observation_type in set(json.loads(stream.required_observations))]
            for stream_key in matching:
                self.enqueue(stream_key, season=season, cutoff=cutoff,
                             manifest_id=manifest_id, session=session)
        return len(matching)

    def retry(self, job_id: str, *, reason: str, session: Session | None = None) -> CompositionJob:
        if len(reason.strip()) < 3:
            raise ControlPlaneError("reason_required")
        now = self.clock()
        with self._session_scope(session) as session:
            row = session.get(CompositionJob, job_id)
            if row is None:
                raise ControlPlaneError("composition_not_found")
            if row.status not in {"failed", "queued"}:
                raise ControlPlaneError("composition_not_retryable")
            row.status, row.attempts, row.updated_at, row.last_error = "queued", row.attempts + 1, now, None
        return row

    def reconcile_pending(self, *, season: str, cutoff: datetime, limit: int = 100) -> int:
        """Scheduled backstop that enqueues accepted observations lacking a job."""
        cutoff = _aware(cutoff)
        with self.session() as session:
            streams = session.scalars(select(PublicationStream).where(PublicationStream.enabled.is_(True))).all()
            candidates: set[tuple[str, str, str]] = set()
            for stream in streams:
                required = set(json.loads(stream.required_observations))
                if not required:
                    continue
                observations = session.scalars(select(CollectionObservation).where(
                    CollectionObservation.season == season,
                    CollectionObservation.cutoff == cutoff,
                    CollectionObservation.provider == stream.provider,
                    CollectionObservation.manifest_id.is_not(None),
                )).all()
                for manifest_id in {row.manifest_id for row in observations
                                    if row.observation_type in required and row.manifest_id}:
                    candidates.add((stream.stream_key, season, manifest_id))
        count = 0
        for stream_key, selected_season, manifest_id in sorted(candidates):
            if count >= min(max(limit, 1), 1000):
                break
            self.enqueue(stream_key, season=selected_season, cutoff=cutoff, manifest_id=manifest_id)
            count += 1
        return count

    def compose(self, stream_key: str, *, season: str, cutoff: datetime, payload: Any,
                expected_fence: int | None = None, reason: str | None = None,
                manifest_id: str | None = None) -> PublicationVersion:
        encoded = _json(payload)
        now = self.clock()
        with self.session() as session, session.begin():
            stream = session.get(PublicationStream, stream_key)
            if stream is None or not stream.enabled:
                raise ControlPlaneError("stream_unavailable")
            self._assert_completeness(session, stream, season=season, cutoff=_aware(cutoff),
                                      manifest_id=manifest_id)
            pointer = session.scalar(select(PublicationPointer).where(
                PublicationPointer.stream_key == stream_key
            ).with_for_update())
            if pointer is None:
                if expected_fence not in (None, 0):
                    raise ControlPlaneError("stale_composition")
                pointer = PublicationPointer(stream_key=stream_key, fence=0, updated_at=now)
                session.add(pointer)
                try:
                    session.flush()
                except IntegrityError as error:
                    raise ControlPlaneError("stale_composition") from error
            elif expected_fence is None:
                raise ControlPlaneError("expected_fence_required")
            if expected_fence is not None and pointer.fence != expected_fence:
                raise ControlPlaneError("stale_composition")
            old = pointer.active_publication_id
            next_version = session.scalar(select(PublicationVersion.version).where(PublicationVersion.stream_key == stream_key,
                PublicationVersion.season == season).order_by(PublicationVersion.version.desc()).limit(1)) or 0
            pointer.fence += 1
            publication = PublicationVersion(publication_id=_uuid(), stream_key=stream_key, season=season,
                cutoff=_aware(cutoff), version=int(next_version) + 1, status="active", checksum=_checksum(encoded), payload=encoded,
                created_at=now, reason=reason, fence=pointer.fence)
            session.add(publication)
            if old:
                previous = session.get(PublicationVersion, old)
                if previous is not None:
                    previous.status = "superseded"
            pointer.previous_publication_id, pointer.active_publication_id, pointer.updated_at = old, publication.publication_id, now
            session.flush()
        return publication

    @staticmethod
    def _assert_completeness(session: Session, stream: PublicationStream, *, season: str,
                             cutoff: datetime, manifest_id: str | None = None) -> None:
        required = set(json.loads(stream.required_observations))
        observations = list(session.scalars(select(CollectionObservation).where(
            CollectionObservation.season == season,
            CollectionObservation.cutoff == cutoff,
            CollectionObservation.manifest_id.is_not(None),
        )))
        manifest_ids = {row.manifest_id for row in observations}
        if manifest_id is not None:
            observations = [row for row in observations if row.manifest_id == manifest_id]
            manifest_ids = {row.manifest_id for row in observations}
        elif len(manifest_ids) > 1:
            raise ControlPlaneError("mixed_manifest")
        observed = {row.observation_type for row in observations
                    if row.provider == stream.provider and _observation_matches_scope(row, stream)}
        missing = sorted(required - observed)
        if missing:
            raise ControlPlaneError("incomplete_publication")
        if stream.completeness_rule == "league_complete":
            team_ids: set[str] = set()
            team_codes: set[str] = set()
            invalid_team_evidence = False
            for observation in observations:
                if observation.provider != stream.provider or not _observation_matches_scope(observation, stream):
                    continue
                try:
                    evidence = json.loads(observation.payload)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(evidence, Mapping):
                    continue
                if any(key in evidence for key in ("team_ids", "team_tricodes")):
                    invalid_team_evidence = True
                for team_evidence in _evidence_team_fields(evidence):
                    if any(key in team_evidence for key in ("team_ids", "team_tricodes")):
                        invalid_team_evidence = True
                    ids, ids_valid = _strict_team_ids(team_evidence.get("team_ids"))
                    codes, codes_valid = _strict_team_codes(
                        team_evidence.get("team_tricodes", team_evidence.get("teams"))
                    )
                    invalid_team_evidence |= not ids_valid or not codes_valid
                    team_ids.update(ids)
                    team_codes.update(codes)
                    if "team_id" in team_evidence:
                        team_id = str(team_evidence.get("team_id") or "")
                        if team_id not in NBA_TEAM_IDS:
                            invalid_team_evidence = True
                        else:
                            team_ids.add(team_id)
                    if "tricode" in team_evidence or "abbreviation" in team_evidence:
                        code = canonical_nba_team_abbreviation(
                            team_evidence.get("tricode", team_evidence.get("abbreviation"))
                        )
                        if code not in NBA_TEAM_TRICODES:
                            invalid_team_evidence = True
                        else:
                            team_codes.add(code)
            if invalid_team_evidence:
                raise ControlPlaneError("league_incomplete")
            if team_ids and team_ids != NBA_TEAM_IDS:
                raise ControlPlaneError("league_incomplete")
            if not team_ids and team_codes != set(NBA_TEAM_TRICODES):
                raise ControlPlaneError("league_incomplete")
        if stream.completeness_rule == "base_complete" and required:
            expected_slices = STREAM_REQUIRED_SLICES.get(stream.stream_key)
            observed_slices: dict[str, set[str]] = {}
            accepted = False
            for observation in observations:
                if (
                    observation.provider != stream.provider
                    or not _observation_matches_scope(observation, stream)
                    or observation.observation_type not in required
                ):
                    continue
                try:
                    evidence = json.loads(observation.payload)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(evidence, Mapping):
                    continue
                matching_bases = {
                    str(base) for base in _evidence_bases(evidence)
                    if _registered_base_for_stream(stream.stream_key, base)
                }
                if not matching_bases:
                    continue
                accepted = True
                if expected_slices is not None:
                    for base, slice_key in _evidence_slice_pairs(evidence):
                        if base in matching_bases and slice_key in expected_slices:
                            observed_slices.setdefault(slice_key, set()).add(observation.observation_id)
            if not accepted or (
                expected_slices is not None
                and (
                    set(observed_slices) != set(expected_slices)
                    or len({next(iter(ids)) for ids in observed_slices.values()})
                    != len(expected_slices)
                )
            ):
                raise ControlPlaneError("base_incomplete")

    def compose_complete(self, stream_key: str, *, season: str, cutoff: datetime, payload: Any,
                         expected_fence: int | None = None,
                         manifest_id: str | None = None) -> PublicationVersion:
        return self.compose(stream_key, season=season, cutoff=cutoff, payload=payload,
                            expected_fence=expected_fence, manifest_id=manifest_id)

    def current(self, stream_key: str) -> PublicationVersion | None:
        with self.session() as session:
            pointer = session.get(PublicationPointer, stream_key)
            return session.get(PublicationVersion, pointer.active_publication_id) if pointer and pointer.active_publication_id else None

    def rollback(self, stream_key: str, *, reason: str, expected_fence: int | None = None,
                 session: Session | None = None) -> PublicationVersion:
        if not reason or len(reason.strip()) < 3:
            raise ControlPlaneError("reason_required")
        now = self.clock()
        with self._session_scope(session) as session:
            pointer = session.scalar(select(PublicationPointer).where(
                PublicationPointer.stream_key == stream_key
            ).with_for_update())
            if pointer is None or not pointer.previous_publication_id:
                raise ControlPlaneError("rollback_unavailable")
            if expected_fence is not None and pointer.fence != expected_fence:
                raise ControlPlaneError("stale_composition")
            prior = session.get(PublicationVersion, pointer.previous_publication_id)
            current = session.get(PublicationVersion, pointer.active_publication_id)
            if prior is None or current is None:
                raise ControlPlaneError("rollback_unavailable")
            pointer.fence += 1
            version = PublicationVersion(publication_id=_uuid(), stream_key=stream_key, season=prior.season,
                cutoff=prior.cutoff, version=current.version + 1, status="rollback", checksum=prior.checksum,
                payload=prior.payload, created_at=now, reason=reason.strip()[:255], fence=pointer.fence)
            session.add(version)
            current.status = "superseded"
            prior.status = "superseded"
            pointer.previous_publication_id, pointer.active_publication_id, pointer.updated_at = current.publication_id, version.publication_id, now
        return version


class CollectionOperationsService(_SessionService):
    """Bounded operational evidence, retention, and completeness gates."""

    def __init__(self, engine: Engine, *, publication_service: "PublicationService | None" = None,
                 collection_control: "CollectionControlService | None" = None,
                 collector_tokens: "CollectorTokenService | None" = None,
                 alert_adapter: "EmailAlertAdapter | None" = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        super().__init__(engine, clock=clock)
        self.publication_service = publication_service
        self.collection_control = collection_control
        self.collector_tokens = collector_tokens
        self.alert_adapter = alert_adapter or EmailAlertAdapter()

    @staticmethod
    def _validate_reason(actor: str, action: str, resource: str, reason: str) -> tuple[str, str, str, str]:
        values = (str(actor).strip(), str(action).strip(), str(resource).strip(), str(reason).strip())
        if not values[0] or not values[1] or not values[2] or len(values[3]) < 3:
            raise ControlPlaneError("reason_required")
        return values[0][:128], values[1][:64], values[2][:128], values[3][:255]

    def _run_operator(self, *, actor: str, action: str, resource: str, reason: str,
                      mutation: Callable[[Session], Any],
                      details: Mapping[str, Any] = ()) -> OperatorActionResult:
        """Run state change, audit, and durable job in one DB transaction."""

        actor, action, resource, reason = self._validate_reason(actor, action, resource, reason)
        now = self.clock()
        with self.session() as session, session.begin():
            job = OperatorJob(
                job_id=_uuid(), actor=actor, action=action, resource=resource,
                reason=reason, status="running", created_at=now,
            )
            session.add(job)
            session.flush()
            changed = mutation(session)
            job.status = "succeeded"
            job.completed_at = self.clock()
            audit = AuditEvent(
                event_id=_uuid(), actor=actor, action=action, resource=resource,
                reason=reason,
                details=_json({**dict(details), "operator_job_id": job.job_id}),
                created_at=now,
            )
            session.add(audit)
        return OperatorActionResult(changed, job, audit)

    def activate_season(self, season: str, *, actor: str, reason: str,
                        cutoff: datetime | None = None) -> OperatorActionResult:
        if self.collection_control is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="season.activate", resource=season, reason=reason,
            mutation=lambda session: self.collection_control.activate_season(
                season, actor=actor, cutoff=cutoff, session=session
            ),
        )

    def rollback_publication(self, stream_key: str, *, actor: str, reason: str,
                             expected_fence: int | None = None) -> OperatorActionResult:
        if self.publication_service is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="publication.rollback", resource=stream_key, reason=reason,
            mutation=lambda session: self.publication_service.rollback(
                stream_key, reason=reason, expected_fence=expected_fence, session=session
            ),
        )

    def activate_stream(self, stream_key: str, *, actor: str, reason: str) -> OperatorActionResult:
        if self.publication_service is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="stream.activate", resource=stream_key, reason=reason,
            mutation=lambda session: self.publication_service.activate_stream(
                stream_key, reason=reason, session=session
            ),
        )

    def retry_composition(self, job_id: str, *, actor: str, reason: str) -> OperatorActionResult:
        if self.publication_service is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="composition.retry", resource=job_id, reason=reason,
            mutation=lambda session: self.publication_service.retry(
                job_id, reason=reason, session=session
            ),
        )

    def start_cycle(self, manifest_id: str, *, actor: str, reason: str) -> OperatorActionResult:
        if self.collection_control is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="cycle.start", resource=manifest_id, reason=reason,
            mutation=lambda session: self.collection_control.open_cycle(
                manifest_id, session=session
            ),
        )

    def scoped_repair(self, stream_key: str, *, season: str, cutoff: datetime,
                      actor: str, reason: str) -> OperatorActionResult:
        if self.publication_service is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="scoped_repair.start", resource=stream_key, reason=reason,
            details={"stream_key": stream_key},
            mutation=lambda session: self.publication_service.enqueue(
                stream_key, season=season, cutoff=cutoff, session=session
            ),
        )

    def finish_cycle(self, cycle_id: str, *, status: str, actor: str,
                     reason: str) -> OperatorActionResult:
        if self.collection_control is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="cycle.finish", resource=cycle_id, reason=reason,
            mutation=lambda session: self.collection_control.finish_cycle(
                cycle_id, status=status, reason=reason, session=session
            ),
        )

    def govern_not_applicable(self, cycle_id: str, stream_key: str, *, actor: str,
                              reason: str) -> OperatorActionResult:
        if self.collection_control is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="cycle.not_applicable", resource=cycle_id, reason=reason,
            details={"stream_key": stream_key},
            mutation=lambda session: self.collection_control.govern_not_applicable(
                cycle_id, stream_key, actor=actor, reason=reason, session=session
            ),
        )

    def bootstrap(self, season: str, catalog_type: str, *, cutoff: datetime,
                  actor: str, reason: str) -> OperatorActionResult:
        if self.collection_control is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="bootstrap.start", resource=f"{season}:{catalog_type}", reason=reason,
            mutation=lambda session: self.collection_control.create_bootstrap_request(
                season, catalog_type, cutoff=cutoff, session=session
            ),
        )

    def revoke_collector(self, identity_id: str, *, actor: str, reason: str) -> OperatorActionResult:
        if self.collector_tokens is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="collector.revoke", resource=identity_id, reason=reason,
            mutation=lambda session: self.collector_tokens.revoke(identity_id, session=session),
        )

    def rotate_collector(self, identity_id: str, *, actor: str, reason: str,
                         overlap_seconds: int = 3600) -> OperatorActionResult:
        if self.collector_tokens is None:
            raise ControlPlaneError("control_plane_unavailable")
        return self._run_operator(
            actor=actor, action="collector.rotate", resource=identity_id, reason=reason,
            mutation=lambda session: self.collector_tokens.rotate(
                identity_id, overlap_seconds=overlap_seconds, session=session
            ),
        )

    def audit(self, *, actor: str, action: str, resource: str, reason: str,
              details: Mapping[str, Any] = ()) -> AuditEvent:
        return self._run_operator(
            actor=actor, action=action, resource=resource, reason=reason,
            mutation=lambda _session: None, details=details,
        ).audit

    def reconciliation(self, *, season: str, kind: str, reason: str,
                       details: Mapping[str, Any] = ()) -> ReconciliationItem:
        if not _valid_season(season) or not kind or not reason:
            raise ControlPlaneError("invalid_reconciliation")
        row = ReconciliationItem(item_id=_uuid(), season=season, kind=kind[:64], reason=reason[:64],
                                 details=_json(dict(details)), created_at=self.clock())
        with self.session() as session, session.begin():
            session.add(row)
        return row

    def resolve_reconciliation(self, item_id: str, *, actor: str, reason: str) -> OperatorActionResult:
        def resolve(session: Session) -> ReconciliationItem:
            item = session.get(ReconciliationItem, item_id)
            if item is None:
                raise ControlPlaneError("reconciliation_not_found")
            if item.status != "open":
                raise ControlPlaneError("reconciliation_already_resolved")
            item.status, item.resolved_at = "resolved", self.clock()
            return item

        return self._run_operator(
            actor=actor, action="reconciliation.resolve", resource=item_id,
            reason=reason, mutation=resolve,
        )

    def alert(self, *, cycle_id: str | None, severity: str, code: str) -> CollectionAlert:
        if severity not in {"warning", "critical"} or not code:
            raise ControlPlaneError("invalid_alert")
        row = CollectionAlert(alert_id=_uuid(), cycle_id=cycle_id, severity=severity, code=code[:64], created_at=self.clock())
        with self.session() as session, session.begin():
            session.add(row)
            _add_audit(
                session, actor="system", action="alert.open", resource=cycle_id or "collection",
                reason=code, details={"cycle_id": cycle_id, "severity": severity, "code": code[:64]},
                created_at=row.created_at,
            )
            # The durable alert and its external notification are one service
            # operation: a failed adapter must roll back the alert row so the
            # scheduler can retry without inventing an acknowledged alert.
            self.alert_adapter.send(code=code, severity=severity, cycle_id=cycle_id)
        return row

    def _open_lifecycle_alert(self, session: Session, *, cycle_id: str,
                              severity: str, code: str, now: datetime) -> bool:
        existing = session.scalar(select(CollectionAlert).where(
            CollectionAlert.cycle_id == cycle_id,
            CollectionAlert.code == code,
            CollectionAlert.status == "open",
        ))
        if existing is not None:
            return False
        row = CollectionAlert(
            alert_id=_uuid(), cycle_id=cycle_id, severity=severity,
            code=code[:64], status="open", created_at=now,
        )
        session.add(row)
        _add_audit(
            session, actor="system", action="alert.open", resource=cycle_id,
            reason=code, details={"cycle_id": cycle_id, "severity": severity, "code": code[:64]},
            created_at=now,
        )
        session.flush()
        self.alert_adapter.send(code=code, severity=severity, cycle_id=cycle_id)
        return True

    def _recover_lifecycle_alerts(self, session: Session, *, cycle_id: str,
                                  now: datetime) -> bool:
        open_alerts = list(session.scalars(select(CollectionAlert).where(
            CollectionAlert.cycle_id == cycle_id,
            CollectionAlert.status == "open",
            CollectionAlert.code.in_(("first_failure", "stale_threshold", "cycle_attention")),
        )))
        if not open_alerts:
            return False
        for alert in open_alerts:
            alert.status, alert.resolved_at = "resolved", now
        prior_recovery = session.scalar(select(CollectionAlert).where(
            CollectionAlert.cycle_id == cycle_id,
            CollectionAlert.code == "recovery",
        ))
        if prior_recovery is not None:
            return True
        recovery = CollectionAlert(
            alert_id=_uuid(), cycle_id=cycle_id, severity="warning",
            code="recovery", status="resolved", created_at=now, resolved_at=now,
        )
        session.add(recovery)
        _add_audit(
            session, actor="system", action="alert.recovered", resource=cycle_id,
            reason="collection_recovered", details={"cycle_id": cycle_id}, created_at=now,
        )
        session.flush()
        self.alert_adapter.send(code="recovery", severity="warning", cycle_id=cycle_id)
        return True

    def run_maintenance(self, *, season: str, cutoff: datetime, now: datetime | None = None) -> dict[str, int]:
        """Run deterministic reconciliation, GC, validation and stale alerts."""
        current = _aware(now or self.clock())
        enqueued = self.publication_service.reconcile_pending(season=season, cutoff=cutoff) if self.publication_service else 0
        deleted = self.gc_observations(now=current)
        attention = 0
        with self.session() as session, session.begin():
            cycles = session.scalars(select(CollectionCycle).where(CollectionCycle.status.in_(
                ("collecting", "attention", "failed", "complete")
            ))).all()
            for cycle in cycles:
                governed = set(session.scalars(select(GovernedNotApplicable.stream_key).where(
                    GovernedNotApplicable.cycle_id == cycle.cycle_id
                )))
                missing = []
                manifest = session.get(CollectionManifest, cycle.manifest_id)
                streams = _manifest_streams(session, manifest) if manifest is not None else []
                for stream in streams:
                    if stream.publication_strategy in {"request_time", "never_schedule"} or stream.stream_key in governed:
                        continue
                    pointer = session.get(PublicationPointer, stream.stream_key)
                    publication = session.get(PublicationVersion, pointer.active_publication_id) if pointer and pointer.active_publication_id else None
                    if publication is None or _aware(publication.cutoff) != _aware(cycle.cutoff):
                        missing.append(stream.stream_key)
                session.add(ValidationSummary(summary_id=_uuid(), cycle_id=cycle.cycle_id,
                    status="attention" if missing else "passed", counts=_json({"missing_streams": sorted(missing)}), created_at=current))
                age = (current - _aware(cycle.created_at)).total_seconds()
                jobs = session.scalars(select(CompositionJob).where(
                    CompositionJob.season == cycle.season,
                    CompositionJob.cutoff == cycle.cutoff,
                    CompositionJob.manifest_id == cycle.manifest_id,
                )).all()
                work_pending = any(job.status in {"queued", "running"} for job in jobs)
                actual_failure = cycle.status == "failed" or any(job.status == "failed" for job in jobs)
                if not missing and not work_pending and not actual_failure:
                    self._recover_lifecycle_alerts(session, cycle_id=cycle.cycle_id, now=current)
                elif not work_pending:
                    if actual_failure:
                        self._open_lifecycle_alert(
                            session, cycle_id=cycle.cycle_id,
                            severity="critical", code="first_failure", now=current,
                        )
                    elif age >= STALE_ALERT_SECONDS:
                        self._open_lifecycle_alert(
                            session, cycle_id=cycle.cycle_id,
                            severity="warning", code="stale_threshold", now=current,
                        )
                if missing and not work_pending and age >= ATTENTION_ALERT_SECONDS:
                    cycle.status, cycle.attention_reason = "attention", "cycle_window_expired"
                    if self._open_lifecycle_alert(
                        session, cycle_id=cycle.cycle_id,
                        severity="critical", code="cycle_attention", now=current,
                    ):
                        attention += 1
        return {"jobs_enqueued": enqueued, "observations_deleted": deleted, "cycles_attention": attention}

    def record_usage(self, collector_id: str, *, envelopes: int = 0, bytes_received: int = 0,
                     polls: int = 0, max_polls: int = 100, max_envelopes: int = 1000, max_bytes: int = 50 * 1024 * 1024) -> CollectorUsage:
        if min(envelopes, bytes_received, polls) < 0 or polls > max_polls or envelopes > max_envelopes or bytes_received > max_bytes:
            raise ControlPlaneError("usage_limit")
        now = self.clock()
        with self.session() as session, session.begin():
            row = session.scalar(select(CollectorUsage).where(
                CollectorUsage.collector_id == collector_id
            ).with_for_update())
            if row is None:
                try:
                    with session.begin_nested():
                        row = CollectorUsage(collector_id=collector_id, window_started_at=now)
                        session.add(row)
                        session.flush()
                except IntegrityError:
                    row = session.scalar(select(CollectorUsage).where(
                        CollectorUsage.collector_id == collector_id
                    ).with_for_update())
            if row is None or (_aware(now) - _aware(row.window_started_at)).total_seconds() >= 86400:
                row = CollectorUsage(collector_id=collector_id, window_started_at=now)
                session.add(row)
            if (row.poll_count + polls > max_polls or row.envelope_count + envelopes > max_envelopes
                    or row.byte_count + bytes_received > max_bytes):
                raise ControlPlaneError("usage_limit")
            row.envelope_count += envelopes
            row.byte_count += bytes_received
            row.poll_count += polls
        return row

    def list_reconciliation(self, *, limit: int = 50) -> list[ReconciliationItem]:
        bounded_limit = max(1, min(int(limit), 100))
        with self.session() as session:
            return list(session.scalars(select(ReconciliationItem).where(
                ReconciliationItem.status == "open"
            ).order_by(ReconciliationItem.created_at.desc()).limit(bounded_limit)))

    def diagnostics(self, *, limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        with self.session() as session:
            def rows(model):
                return list(session.scalars(select(model).order_by(model.created_at.desc()).limit(bounded)))
            cycles = rows(CollectionCycle)
            streams = list(session.scalars(select(PublicationStream).limit(bounded)))
            alerts = rows(CollectionAlert)
            items = rows(ReconciliationItem)
            validations = rows(ValidationSummary)
            jobs = rows(OperatorJob)
            usage = list(session.scalars(select(CollectorUsage).limit(bounded)))
            collectors = list(session.scalars(select(CollectorIdentity).limit(bounded)))
        return {
            "cycles": [{"cycle_id": row.cycle_id, "season": row.season, "status": row.status, "cutoff": row.cutoff.isoformat()} for row in cycles],
            "streams": [{"stream_key": row.stream_key, "provider": row.provider, "owner": row.owner, "enabled": row.enabled, "freshness_rule": row.freshness_rule} for row in streams],
            "collectors": [{"identity_id": row.identity_id, "environment": row.environment, "revoked": row.revoked_at is not None, "last_seen_at": _iso(row.last_seen_at)} for row in collectors],
            "alerts": [{"alert_id": row.alert_id, "severity": row.severity, "code": row.code, "status": row.status} for row in alerts],
            "reconciliation": [{"item_id": row.item_id, "season": row.season, "kind": row.kind, "reason": row.reason, "status": row.status} for row in items],
            "validation": [{"summary_id": row.summary_id, "cycle_id": row.cycle_id, "status": row.status} for row in validations],
            "usage": [{"collector_id": row.collector_id, "poll_count": row.poll_count, "envelope_count": row.envelope_count, "byte_count": row.byte_count} for row in usage],
            "jobs": [{
                "job_id": row.job_id, "action": row.action, "resource": row.resource,
                "status": row.status, "created_at": row.created_at.isoformat(),
                "completed_at": _iso(row.completed_at), "error_code": row.error_code,
            } for row in jobs],
        }

    def validate_completeness(self, *, cycle_id: str) -> dict[str, Any]:
        """Derive a validation summary from accepted evidence and governance.

        Caller-supplied team/base lists and boolean ``complete`` flags are
        deliberately not accepted: they were an unsafe bypass around the
        persisted stream gates.
        """
        with self.session() as session:
            cycle = session.get(CollectionCycle, cycle_id)
            if cycle is None:
                raise ControlPlaneError("cycle_not_found")
            governed = set(session.scalars(select(GovernedNotApplicable.stream_key).where(
                GovernedNotApplicable.cycle_id == cycle_id
            )))
            manifest = session.get(CollectionManifest, cycle.manifest_id)
            streams = _manifest_streams(session, manifest) if manifest is not None else []
            missing: list[str] = []
            for stream in streams:
                if stream.publication_strategy in {"request_time", "never_schedule"}:
                    continue
                if stream.stream_key in governed:
                    continue
                pointer = session.get(PublicationPointer, stream.stream_key)
                publication = session.get(PublicationVersion, pointer.active_publication_id) if pointer and pointer.active_publication_id else None
                if publication is None or _aware(publication.cutoff) != _aware(cycle.cutoff):
                    missing.append(stream.stream_key)
            return {"complete": not missing, "missing_streams": sorted(missing), "governed_not_applicable": sorted(governed)}

    def store_validation(self, cycle_id: str, *, status: str, counts: Mapping[str, Any]) -> ValidationSummary:
        if status not in {"passed", "failed", "attention"}:
            raise ControlPlaneError("invalid_validation_status")
        row = ValidationSummary(summary_id=_uuid(), cycle_id=cycle_id, status=status, counts=_json(dict(counts)), created_at=self.clock())
        with self.session() as session, session.begin():
            session.add(row)
        return row

    def gc_observations(self, *, now: datetime | None = None, retention_days: int = OBSERVATION_RETENTION_DAYS) -> int:
        if retention_days < 1:
            raise ControlPlaneError("invalid_retention")
        cutoff = _aware(now or self.clock()) - timedelta(days=retention_days)
        with self.session() as session, session.begin():
            # Keep observations named by an active or rollback publication;
            # provenance survives ordinary 30-day garbage collection.
            protected: set[str] = set()
            publications = session.scalars(select(PublicationVersion).where(PublicationVersion.status.in_(("active", "rollback")))).all()
            for publication in publications:
                try:
                    document = json.loads(publication.payload)
                except (TypeError, json.JSONDecodeError):
                    continue
                protected.update(_find_observation_ids(document))
            rows = session.scalars(select(CollectionObservation).where(CollectionObservation.accepted_at < cutoff)).all()
            rows = [row for row in rows if row.observation_id not in protected]
            count = len(rows)
            for row in rows:
                session.delete(row)
        return count


class EmailAlertAdapter:
    """Pluggable bounded alert sink; production supplies the email transport."""

    def __init__(self, sender: Callable[[str, str], None] | None = None) -> None:
        self.sender = sender

    def send(self, *, code: str, severity: str, cycle_id: str | None = None) -> None:
        if severity not in {"warning", "critical"} or not code:
            raise ControlPlaneError("invalid_alert")
        if self.sender is not None:
            self.sender(f"StatsPlus collection {severity}", f"code={code[:64]} cycle={cycle_id or 'none'}")


def _canonical_team_ids(value: Any) -> set[str]:
    """Extract only IDs from the immutable NBA team registry."""

    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item) for item in value if str(item) in NBA_TEAM_IDS}


def _canonical_team_codes(value: Any) -> set[str]:
    """Extract only reviewed NBA tricodes from team evidence."""

    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    codes: set[str] = set()
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("tricode", item.get("abbreviation", item.get("team")))
        code = canonical_nba_team_abbreviation(item)
        if code in NBA_TEAM_TRICODES:
            codes.add(code)
    return codes


def _strict_team_ids(value: Any) -> tuple[set[str], bool]:
    """Return canonical team IDs and reject extra or duplicate identities."""

    if value is None:
        return set(), True
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set(), False
    raw = [str(item) for item in value]
    valid = len(raw) == len(set(raw)) and all(item in NBA_TEAM_IDS for item in raw)
    return ({item for item in raw if item in NBA_TEAM_IDS}, valid)


def _strict_team_codes(value: Any) -> tuple[set[str], bool]:
    """Return reviewed NBA tricodes and reject unknown or duplicate codes."""

    if value is None:
        return set(), True
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set(), False
    raw: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("tricode", item.get("abbreviation", item.get("team")))
        raw.append(canonical_nba_team_abbreviation(item))
    valid = len(raw) == len(set(raw)) and all(item in NBA_TEAM_TRICODES for item in raw)
    return ({item for item in raw if item in NBA_TEAM_TRICODES}, valid)


def _evidence_team_fields(evidence: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    fields: list[Mapping[str, Any]] = []
    if any(key in evidence for key in (
        "team_id", "tricode", "abbreviation", "team_ids", "team_tricodes"
    )):
        fields.append(evidence)
    for key in ("rows", "records", "data", "observations", "teams"):
        items = evidence.get(key)
        if isinstance(items, list):
            fields.extend(item for item in items if isinstance(item, Mapping))
    return fields


def _registered_base_for_stream(stream_key: str, value: Any) -> bool:
    base = str(value or "").strip()
    allowed = STREAM_BASES.get(stream_key, REGISTERED_BASES)
    return base in allowed


def _evidence_bases(evidence: Mapping[str, Any]) -> set[Any]:
    bases: set[Any] = {evidence.get("base")}
    for key in ("rows", "records", "data", "observations"):
        items = evidence.get(key)
        if isinstance(items, list):
            bases.update(
                item.get("base") for item in items
                if isinstance(item, Mapping) and "base" in item
            )
    return bases


def _evidence_slice_pairs(evidence: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Extract explicit registered Base/slice identities from accepted rows."""

    fields: list[Mapping[str, Any]] = [evidence]
    for key in ("rows", "records", "data", "observations"):
        items = evidence.get(key)
        if isinstance(items, list):
            fields.extend(item for item in items if isinstance(item, Mapping))
    default_base = str(evidence.get("base") or "").strip()
    pairs: set[tuple[str, str]] = set()
    for field in fields:
        base = str(field.get("base") or default_base).strip()
        slice_key = field.get("slice_key", field.get("slice", field.get("category")))
        if base and slice_key not in (None, ""):
            pairs.add((base, str(slice_key).strip()))
    return pairs


def _observation_matches_scope(observation: CollectionObservation,
                               stream: PublicationStream) -> bool:
    """Ensure completeness counts only the stream's registered scope."""

    try:
        scope = json.loads(observation.scope)
    except (TypeError, json.JSONDecodeError):
        return False
    windows = set(json.loads(stream.supported_windows))
    if not windows:
        return True
    if isinstance(scope, Mapping):
        window = scope.get("window")
        if window is None:
            window = scope.get("scope")
        if window is None:
            return False
        return str(window) in windows
    return str(scope) in windows


def _completed_game_count(payload: str, *, cutoff: datetime) -> int:
    """Count completed canonical events from an Event Catalog publication."""

    try:
        document = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return 0
    if isinstance(document, list):
        events = document
    elif isinstance(document, Mapping):
        events = document.get("events", document.get("games"))
    else:
        return 0
    if isinstance(events, list):
        count = 0
        identities: set[str] = set()
        for event in events:
            if not isinstance(event, Mapping):
                continue
            game_id = event.get("nba_game_id", event.get("game_id", event.get("id")))
            status = str(event.get("status", event.get("status_text", ""))).lower()
            status_code = event.get("status_code")
            completed = bool(event.get("completed")) or status in {
                "final", "finished", "completed", "closed", "game over", "3",
            } or status.startswith("final") or status_code in {3, "3"}
            if not completed or not game_id:
                continue
            scheduled = event.get("scheduled_at", event.get("date"))
            if scheduled:
                try:
                    if _parse_datetime(str(scheduled)) > cutoff:
                        continue
                except ControlPlaneError:
                    continue
            identity = str(game_id)
            if identity not in identities:
                identities.add(identity)
                count += 1
        return count
    return 0


def _manifest_streams(session: Session, manifest: CollectionManifest) -> list[PublicationStream]:
    """Resolve only streams explicitly frozen into this manifest."""

    scopes = set(json.loads(manifest.scopes))
    # The manifest freezes its required scopes.  Do not re-read the current
    # activation flags here: a later operator toggle must not widen or shrink
    # an already-open cycle.
    streams = session.scalars(select(PublicationStream)).all()
    selected: list[PublicationStream] = []
    for stream in streams:
        definition = next((item for item in SURFACE_REGISTRY if item.stream_key == stream.stream_key), None)
        if definition is not None and definition.strategy == "never_schedule":
            continue
        registry_scopes = {stream.stream_key, *json.loads(stream.required_observations)}
        if definition is not None:
            registry_scopes.update({definition.scope, *definition.required})
        if scopes.intersection(registry_scopes):
            selected.append(stream)
    return selected


def _find_observation_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"observation_id", "observation_ids"}:
                if isinstance(item, str):
                    found.add(item)
                elif isinstance(item, list):
                    found.update(str(candidate) for candidate in item)
            found.update(_find_observation_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_observation_ids(item))
    return found


def _catalog_rows(value: Any, *, catalog_type: str) -> list[Any] | None:
    if isinstance(value, list):
        return value if catalog_type == "event" else None
    if not isinstance(value, Mapping):
        return None
    key = "events" if catalog_type == "event" else "identities"
    alternate = "games" if catalog_type == "event" else "players"
    rows = value.get(key, value.get(alternate))
    return rows if isinstance(rows, list) else None


def _catalog_identity_ids(value: Any) -> set[str]:
    rows = _catalog_rows(value, catalog_type="athlete") or []
    identities: set[str] = set()
    for row in rows:
        if isinstance(row, Mapping):
            identity = row.get("player_id", row.get("id", row.get("identity_id")))
        else:
            identity = row
        if identity not in (None, ""):
            identities.add(str(identity).strip())
    return identities


def _required_athlete_ids(payload: str | Mapping[str, Any]) -> set[str]:
    """Derive athlete coverage from persisted Event Catalog evidence only."""

    try:
        document = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, json.JSONDecodeError):
        return set()
    rows = _catalog_rows(document, catalog_type="event") or []
    required: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in ("athlete_ids", "player_ids", "participants", "identities"):
            values = row.get(key)
            if isinstance(values, Mapping):
                values = values.keys()
            if isinstance(values, (list, tuple, set, frozenset)):
                required.update(str(value).strip() for value in values if str(value).strip())
    return required


def _canonical_catalog_team(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("team_id", value.get("id", value.get(
            "tricode", value.get("abbreviation", value.get("team"))
        )))
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text in NBA_TEAM_IDS:
        return text
    code = canonical_nba_team_abbreviation(text)
    return code if code in NBA_TEAM_TRICODES else None


def _event_team_evidence(row: Mapping[str, Any]) -> set[str]:
    values: list[Any] = []
    for home_key, away_key in (
        ("home_team_id", "away_team_id"),
        ("home_team", "away_team"),
        ("home_team_tricode", "away_team_tricode"),
        ("home_tricode", "away_tricode"),
    ):
        if home_key in row or away_key in row:
            values.extend((row.get(home_key), row.get(away_key)))
            break
    if not values and isinstance(row.get("teams"), (list, tuple)):
        values.extend(row["teams"])
    return {team for value in values if (team := _canonical_catalog_team(value))}


def _validate_catalog_payload(value: Any, catalog_type: str, *,
                              min_event_games: int = 1,
                              min_event_teams: int = 2,
                              min_athlete_identities: int = 1) -> None:
    """Validate a whole-season catalog before it can become complete."""

    if catalog_type not in {"event", "athlete"}:
        raise ValueError("invalid catalog type")
    rows = _catalog_rows(value, catalog_type=catalog_type)
    max_catalog_records = MAX_EVENT_CATALOG_GAMES if catalog_type == "event" else MAX_ATHLETE_CATALOG_IDENTITIES
    if rows is None or not rows or len(rows) > min(MAX_RECORDS_PER_OBSERVATION, max_catalog_records):
        raise ValueError("catalog_incomplete: rows required")
    identities: set[str] = set()
    if catalog_type == "event":
        teams: set[str] = set()
        phases: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("event row required")
            identity = row.get("nba_game_id", row.get("game_id", row.get("id")))
            if identity in (None, ""):
                raise ValueError("event identity required")
            identity_text = str(identity).strip()
            if identity_text in identities:
                raise ValueError("duplicate canonical game identity")
            identities.add(identity_text)
            row_teams = _event_team_evidence(row)
            if len(row_teams) != 2:
                raise ValueError("canonical event teams required")
            teams.update(row_teams)
            phase = row.get("phase", row.get("season_phase", row.get("season_type")))
            normalized_phase = str(phase or "").strip().lower().replace("_", " ")
            if normalized_phase not in {
                "regular season", "regular", "playoffs", "playoff", "play in", "play-in",
                "play in tournament",
            }:
                raise ValueError("event phase required")
            phases.add(normalized_phase)
            status = row.get("status", row.get("status_text", row.get("status_code")))
            if status in (None, "") and "completed" not in row:
                raise ValueError("event status required")
            if status not in (None, ""):
                status_text = str(status).strip().lower()
                if status_text not in {
                    "scheduled", "final", "finished", "completed", "closed", "in progress",
                    "live", "postponed", "canceled", "cancelled", "game over", "1", "2", "3",
                } and not status_text.startswith("final"):
                    raise ValueError("event status invalid")
            scheduled = row.get("scheduled_at", row.get("date", row.get("game_date")))
            if scheduled in (None, ""):
                raise ValueError("event date required")
            try:
                _parse_datetime(str(scheduled))
            except ControlPlaneError as error:
                raise ValueError("event date invalid") from error
        if len(identities) < max(1, int(min_event_games)):
            raise ValueError("catalog_incomplete: event volume below bound")
        if len(teams) < max(2, int(min_event_teams)):
            raise ValueError("catalog_incomplete: team coverage below bound")
        if not phases:
            raise ValueError("catalog_incomplete: governed evidence missing")
        return

    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("athlete roster row required")
        identity = row.get("player_id", row.get("id", row.get("identity_id")))
        if identity in (None, ""):
            raise ValueError("athlete identity required")
        identity_text = str(identity).strip()
        if identity_text in identities:
            raise ValueError("duplicate athlete identity")
        identities.add(identity_text)
        if _canonical_catalog_team(row.get("team_id", row.get("team", row.get("tricode")))) is None:
            raise ValueError("athlete team evidence required")
        if not str(row.get("status", row.get("phase", "active"))).strip():
            raise ValueError("athlete roster status required")
        coverage = row.get("event_ids", row.get("game_ids", row.get("games")))
        if not isinstance(coverage, (list, tuple, set, frozenset)) or not coverage:
            raise ValueError("athlete season coverage required")
    if len(identities) < max(1, int(min_athlete_identities)):
        raise ValueError("catalog_incomplete: athlete volume below bound")


def _validate_observation_payload(value: Any, *, observation_type: str,
                                  min_event_catalog_games: int = 1,
                                  min_event_catalog_teams: int = 2,
                                  min_athlete_catalog_identities: int = 1) -> None:
    """Run the closed registry validator before durable observation insert."""

    if observation_type in {"event_catalog", "athlete_catalog"}:
        _validate_catalog_payload(
            value,
            "event" if observation_type == "event_catalog" else "athlete",
            min_event_games=min_event_catalog_games,
            min_event_teams=min_event_catalog_teams,
            min_athlete_identities=min_athlete_catalog_identities,
        )
        _validate_payload_shape(value)
        return
    expected_base = OBSERVATION_BASES.get(observation_type)
    if expected_base is not None:
        if not isinstance(value, Mapping):
            raise ValueError("registry payload must be an object")
        root_base = value.get("base")
        if root_base not in (None, expected_base):
            raise ValueError("observation base mismatch")
        rows = None
        for key in ("rows", "records", "data", "observations"):
            if key in value:
                rows = value[key]
                break
        if not isinstance(rows, list) or not rows or not all(isinstance(row, Mapping) for row in rows):
            raise ValueError("registered observation rows required")
        for row in rows:
            row_base = row.get("base", expected_base)
            if row_base != expected_base:
                raise ValueError("observation base mismatch")
            if not any(key in row for key in ("slice_key", "slice", "category")):
                raise ValueError("observation slice required")
    elif isinstance(value, Mapping):
        for key in ("rows", "records", "data", "observations"):
            if key in value and (
                not isinstance(value[key], list)
                or not value[key]
                or not all(isinstance(row, Mapping) for row in value[key])
            ):
                raise ValueError("observation rows malformed")
    if value is None or value == {} or value == []:
        raise ValueError("empty payload")
    if isinstance(value, list):
        if not value or not all(isinstance(item, Mapping) for item in value):
            raise ValueError("generic payload")
        for item in value:
            _validate_record_invariants(item)
    elif isinstance(value, Mapping):
        collection_keys = {
            "rows", "records", "data", "events", "games", "identities", "players",
            "teams", "observations", "team_ids", "team_tricodes", "base",
        }
        if not collection_keys.intersection(value):
            raise ValueError("generic payload")
        for key in ("rows", "records", "data", "events", "games", "identities", "players", "teams", "observations"):
            if key in value and isinstance(value[key], (list, tuple)) and not value[key]:
                raise ValueError("empty collection")
        for key in ("participants", "participant_ids"):
            if key in value:
                participants = value[key]
                if not isinstance(participants, (list, tuple, set)) or not participants:
                    raise ValueError("participants required")
        _validate_record_invariants(value)
    _validate_payload_shape(value)


def _validate_record_invariants(value: Mapping[str, Any]) -> None:
    """Validate explicit identity/category/percentage/count evidence."""

    for key, child in value.items():
        lower = str(key).lower()
        if lower in {"percentage", "percent", "pct"}:
            if not isinstance(child, (int, float)) or isinstance(child, bool) or not math.isfinite(float(child)) or not 0 <= float(child) <= 100:
                raise ValueError("invalid percentage")
        if lower in {"share", "rate", "ratio"}:
            if not isinstance(child, (int, float)) or isinstance(child, bool) or not math.isfinite(float(child)) or not 0 <= float(child) <= 1:
                raise ValueError("invalid percentage")
        if "category" in lower and child in (None, ""):
            raise ValueError("category required")
        if lower in {"player_id", "team_id", "game_id", "nba_game_id", "identity_id"} and child in (None, ""):
            raise ValueError("identity required")
        if lower in {"games_played", "game_count", "volume", "possessions", "field_goal_attempts"}:
            if isinstance(child, bool) or not isinstance(child, (int, float)) or child < 0 or child > MAX_RECORDS_PER_OBSERVATION:
                raise ValueError("invalid historical volume")
    for key in ("rows", "records", "events", "games", "identities", "players", "teams", "observations"):
        records = value.get(key)
        if isinstance(records, list):
            if len(records) > MAX_RECORDS_PER_OBSERVATION:
                raise ValueError("payload volume exceeds limit")
            for record in records:
                if isinstance(record, Mapping):
                    _validate_record_invariants(record)


def _validate_payload_shape(value: Any, *, _count: list[int] | None = None, _depth: int = 0) -> None:
    """Apply generic envelope safety plus schema-neutral count invariants.

    Stream-specific fields remain owned by the persisted surface registry; the
    generic checks here only cover properties that are unsafe for every stats
    payload (finite numbers, non-negative counts, and made <= attempted).
    """
    counter = _count or [0]
    counter[0] += 1
    if counter[0] > MAX_PAYLOAD_VALUES or _depth > MAX_PAYLOAD_DEPTH:
        raise ValueError("payload volume exceeds limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite value")
    if isinstance(value, dict):
        unresolved = value.get("unresolved_identities")
        if unresolved:
            raise ValueError("unresolved identity")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 512:
                raise ValueError("invalid payload key")
            lower = key.lower()
            if isinstance(child, (int, float)) and not isinstance(child, bool):
                if not math.isfinite(float(child)) or (any(marker in lower for marker in ("count", "made", "attempted", "points", "rebounds", "assists", "minutes")) and child < 0):
                    raise ValueError("invalid numeric value")
            if key.endswith("_made"):
                attempted_value = value.get(key[:-5] + "_attempted")
                if isinstance(attempted_value, (int, float)) and child > attempted_value:
                    raise ValueError("made exceeds attempted")
            _validate_payload_shape(child, _count=counter, _depth=_depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_payload_shape(child, _count=counter, _depth=_depth + 1)


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ControlPlaneError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ControlPlaneError("invalid_timestamp") from error
    if parsed.tzinfo is None:
        raise ControlPlaneError("invalid_timestamp")
    return parsed.astimezone(UTC)


def _valid_season(season: str) -> bool:
    if not isinstance(season, str) or len(season) != 7 or season[4] != "-":
        return False
    try:
        return int(season[:4]) + 1 == 2000 + int(season[5:])
    except ValueError:
        return False


__all__ = [
    "CURRENT_ENVELOPE_VERSION", "SURFACE_REGISTRY", "SurfaceDefinition", "ControlPlaneError", "CollectorClaims", "CollectorTokenService",
    "CollectionControlService", "ObservationIngestionService", "ObservationReceipt", "PublicationService", "CollectionOperationsService", "EmailAlertAdapter",
    "OperatorActionResult", "decompress_gzip_limited",
]
