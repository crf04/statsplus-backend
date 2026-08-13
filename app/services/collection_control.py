"""Control-plane services for collection, ingestion, and publication.

These services intentionally accept an injected SQLAlchemy engine and clock so
they can be exercised against a temporary database without provider calls or
credentials.  The only mutable operation on an observation is the first
durable insert; publication pointers are changed under a per-stream fence.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models.collection_control import (
    ActiveSeason,
    BootstrapRequest,
    CatalogPublication,
    CollectionManifest,
    CollectorIdentity,
    CollectionObservation,
    CollectorTokenReplay,
    PublicationPointer,
    PublicationStream,
    PublicationVersion,
)


UTC = timezone.utc
CURRENT_ENVELOPE_VERSION = 2
MAX_ENVELOPE_BYTES = 10 * 1024 * 1024
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_SCOPE_COUNT = 512
MAX_SCOPE_BYTES = 32 * 1024


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _checksum(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _uuid() -> str:
    return str(uuid.uuid4())


class ControlPlaneError(ValueError):
    """A stable, safe reason for a rejected control-plane operation."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True, slots=True)
class CollectorClaims:
    collector_id: str
    audience: str
    environment: str
    scopes: frozenset[str]
    token_id: str
    expires_at: datetime


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


class _SessionService:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.engine = engine
        self.clock = clock
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def session(self) -> Session:
        return self.session_factory()


class CollectorTokenService(_SessionService):
    """Issue and validate short-lived, environment-bound collector tokens."""

    def __init__(self, engine: Engine, *, environment: str, signing_secret: str | bytes | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        super().__init__(engine, clock=clock)
        self.environment = environment
        self.signing_secret = (signing_secret.encode() if isinstance(signing_secret, str) else signing_secret) or secrets.token_bytes(32)

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
                        scopes: Iterable[str] = (), identity_id: str | None = None) -> dict[str, str]:
        if not label.strip() or not audience.strip():
            raise ControlPlaneError("invalid_identity")
        scope_set = frozenset(str(scope).strip() for scope in scopes if str(scope).strip())
        if len(scope_set) > MAX_SCOPE_COUNT:
            raise ControlPlaneError("scope_limit")
        identity_id = identity_id or secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32)
        now = self.clock()
        with self.session() as session, session.begin():
            session.add(CollectorIdentity(
                identity_id=identity_id, label=label.strip(), environment=self.environment,
                audience=audience.strip(), secret_hash=self._hash_secret(secret),
                scopes=_json(sorted(scope_set)), created_at=now,
            ))
        return {"identity_id": identity_id, "secret": secret, "audience": audience.strip()}

    def issue(self, identity_id: str, *, scopes: Iterable[str] | None = None,
              ttl_seconds: int = 300) -> str:
        if not 1 <= ttl_seconds <= 900:
            raise ControlPlaneError("invalid_token_ttl")
        now = self.clock()
        with self.session() as session:
            identity = session.get(CollectorIdentity, identity_id)
            if identity is None or identity.revoked_at is not None or identity.environment != self.environment:
                raise ControlPlaneError("identity_revoked")
            allowed = frozenset(json.loads(identity.scopes))
            requested = allowed if scopes is None else frozenset(str(s).strip() for s in scopes)
            if not requested <= allowed:
                raise ControlPlaneError("scope_denied")
            claims = {
                "sub": identity_id, "aud": identity.audience, "env": self.environment,
                "scope": sorted(requested), "iat": int(_aware(now).timestamp()),
                "exp": int((_aware(now) + timedelta(seconds=ttl_seconds)).timestamp()),
                "jti": secrets.token_urlsafe(18),
            }
            body = self._encode(claims)
            return body + "." + self._sign(body)

    def issue_for_secret(self, identity_id: str, secret: str, *,
                         scopes: Iterable[str] | None = None,
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
        return self.issue(identity_id, scopes=scopes, ttl_seconds=ttl_seconds)

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
        return CollectorClaims(identity_id, str(payload["aud"]), self.environment, scopes, token_id, expires)

    def revoke(self, identity_id: str) -> None:
        with self.session() as session, session.begin():
            identity = session.get(CollectorIdentity, identity_id)
            if identity is None:
                raise ControlPlaneError("identity_not_found")
            identity.revoked_at = self.clock()

    def rotate(self, identity_id: str, *, overlap_seconds: int = 3600) -> dict[str, str]:
        if overlap_seconds < 0:
            raise ControlPlaneError("invalid_rotation_window")
        secret = secrets.token_urlsafe(32)
        now = self.clock()
        with self.session() as session, session.begin():
            identity = session.get(CollectorIdentity, identity_id)
            if identity is None or identity.revoked_at is not None:
                raise ControlPlaneError("identity_revoked")
            identity.previous_secret_hash = identity.secret_hash
            identity.previous_secret_expires_at = _aware(now) + timedelta(seconds=overlap_seconds)
            identity.secret_hash = self._hash_secret(secret)
        return {"identity_id": identity_id, "secret": secret}


class CollectionControlService(_SessionService):
    """Manage active seasons, bootstrap requests, catalogs, and manifests."""

    def activate_season(self, season: str, *, actor: str, cutoff: datetime | None = None) -> ActiveSeason:
        if not _valid_season(season):
            raise ControlPlaneError("invalid_season")
        now = self.clock()
        with self.session() as session, session.begin():
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
                                 ttl_hours: int = 24) -> BootstrapRequest:
        if catalog_type not in {"event", "athlete"} or not _valid_season(season):
            raise ControlPlaneError("invalid_bootstrap")
        if ttl_hours <= 0 or ttl_hours > 168:
            raise ControlPlaneError("invalid_bootstrap_expiry")
        now = self.clock()
        with self.session() as session, session.begin():
            active = session.get(ActiveSeason, season)
            if active is None or active.status != "active":
                raise ControlPlaneError("season_not_active")
            row = BootstrapRequest(request_id=_uuid(), season=season, catalog_type=catalog_type,
                cutoff=_aware(cutoff), status="pending", expires_at=_aware(now) + timedelta(hours=ttl_hours), created_at=now)
            session.add(row)
        return row

    def publish_catalog(self, request_id: str, payload: Any, *, version: str,
                        checksum: str | None = None, expires_at: datetime | None = None) -> CatalogPublication:
        encoded = _json(payload)
        checksum = checksum or _checksum(encoded)
        now = self.clock()
        with self.session() as session, session.begin():
            request = session.get(BootstrapRequest, request_id)
            if request is None:
                raise ControlPlaneError("bootstrap_not_found")
            if request.status != "pending" or _aware(request.expires_at) <= _aware(now):
                request.status = "expired" if request.status == "pending" else request.status
                raise ControlPlaneError("bootstrap_expired")
            publication = CatalogPublication(publication_id=_uuid(), season=request.season, catalog_type=request.catalog_type,
                cutoff=request.cutoff, version=version, checksum=checksum, payload=encoded,
                complete=True, published_at=now, expires_at=expires_at)
            session.add(publication)
            request.status, request.completed_at, request.catalog_version = "succeeded", now, version
        return publication

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
                        collect_before: datetime, accepted_versions: Iterable[int] = (1, 2)) -> CollectionManifest:
        now = _aware(self.clock())
        cutoff, collect_before = _aware(cutoff), _aware(collect_before)
        if cutoff > collect_before or collect_before <= now:
            raise ControlPlaneError("invalid_manifest_window")
        event = self.latest_catalog(season, "event", cutoff=cutoff, now=now)
        athlete = self.latest_catalog(season, "athlete", cutoff=cutoff, now=now)
        if event is None:
            raise ControlPlaneError("event_catalog_required")
        if athlete is None:
            raise ControlPlaneError("athlete_catalog_required")
        scope_list = sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
        versions = sorted({int(version) for version in accepted_versions})
        if not scope_list or not versions or len(_json(scope_list).encode()) > MAX_SCOPE_BYTES:
            raise ControlPlaneError("invalid_manifest")
        material = {"season": season, "cutoff": cutoff.isoformat(), "collect_before": collect_before.isoformat(),
                    "accepted_versions": versions, "scopes": scope_list}
        digest = _checksum(_json(material))
        with self.session() as session, session.begin():
            session.query(CollectionManifest).filter(CollectionManifest.season == season, CollectionManifest.status == "active").update(
                {"status": "superseded", "superseded_at": now})
            row = CollectionManifest(manifest_id=_uuid(), season=season, cutoff=cutoff, collect_before=collect_before,
                accepted_versions=_json(versions), scopes=_json(scope_list), checksum=digest, status="active", created_at=now)
            session.add(row)
        return row

    def get_manifest(self, manifest_id: str, *, now: datetime | None = None) -> CollectionManifest:
        with self.session() as session:
            row = session.get(CollectionManifest, manifest_id)
            if row is None:
                raise ControlPlaneError("manifest_not_found")
            if row.status != "active" or _aware(row.collect_before) <= _aware(now or self.clock()):
                raise ControlPlaneError("manifest_expired")
            return row


class ObservationIngestionService(_SessionService):
    """Validate and durably accept one complete observation envelope."""

    def ingest(self, claims: CollectorClaims, envelope: Mapping[str, Any], payload: bytes | str,
               *, compressed: bool = False, max_payload_bytes: int = MAX_ENVELOPE_BYTES,
               max_compressed_bytes: int = MAX_COMPRESSED_BYTES) -> ObservationReceipt:
        if compressed and isinstance(payload, str):
            payload = payload.encode()
        raw = payload if isinstance(payload, bytes) else payload.encode()
        if len(raw) > max_compressed_bytes if compressed else len(raw) > max_payload_bytes:
            raise ControlPlaneError("payload_too_large")
        try:
            decoded = gzip.decompress(raw) if compressed else raw
        except (OSError, EOFError) as error:
            raise ControlPlaneError("invalid_compression") from error
        if len(decoded) > max_payload_bytes:
            raise ControlPlaneError("payload_too_large")
        try:
            value = json.loads(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ControlPlaneError("malformed_payload") from error
        if not isinstance(value, (dict, list)):
            raise ControlPlaneError("malformed_payload")
        required = {"manifest_id", "client_observation_id", "environment", "provider", "observation_type", "scope", "season", "cutoff", "schema_version", "retrieved_at"}
        if not required <= envelope.keys():
            raise ControlPlaneError("malformed_envelope")
        if envelope["environment"] != claims.environment or envelope["season"] is None:
            raise ControlPlaneError("environment_mismatch")
        schema_version = int(envelope["schema_version"])
        if schema_version not in {CURRENT_ENVELOPE_VERSION, CURRENT_ENVELOPE_VERSION - 1}:
            raise ControlPlaneError("schema_unsupported")
        client_id = str(envelope["client_observation_id"]).strip()
        if not client_id or len(client_id) > 128:
            raise ControlPlaneError("invalid_observation_id")
        scope_text = _json(envelope["scope"])
        if len(scope_text.encode()) > MAX_SCOPE_BYTES:
            raise ControlPlaneError("scope_limit")
        manifest_id = str(envelope["manifest_id"])
        checksum = str(envelope.get("checksum") or _checksum(decoded))
        if checksum != _checksum(decoded):
            raise ControlPlaneError("checksum_mismatch")
        now = self.clock()
        with self.session() as session, session.begin():
            manifest = session.get(CollectionManifest, manifest_id)
            if manifest is None or manifest.status != "active":
                raise ControlPlaneError("manifest_expired")
            if manifest.season != str(envelope["season"]) or _aware(manifest.cutoff) != _aware(_parse_datetime(envelope["cutoff"])):
                raise ControlPlaneError("manifest_scope_mismatch")
            allowed_scopes = set(json.loads(manifest.scopes))
            if str(envelope["observation_type"]) not in allowed_scopes and "*" not in allowed_scopes:
                raise ControlPlaneError("scope_denied")
            existing = session.scalar(select(CollectionObservation).where(CollectionObservation.collector_id == claims.collector_id,
                CollectionObservation.client_observation_id == client_id))
            if existing is not None:
                if existing.checksum != checksum:
                    raise ControlPlaneError("observation_id_conflict")
                return ObservationReceipt(existing.observation_id, client_id, checksum, replay=True)
            row = CollectionObservation(observation_id=_uuid(), client_observation_id=client_id, collector_id=claims.collector_id,
                environment=claims.environment, provider=str(envelope["provider"]), observation_type=str(envelope["observation_type"]),
                scope=scope_text, season=str(envelope["season"]), cutoff=_parse_datetime(envelope["cutoff"]), schema_version=schema_version,
                checksum=checksum, payload=decoded.decode(), payload_bytes=len(decoded), retrieved_at=_parse_datetime(envelope["retrieved_at"]), accepted_at=now)
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                session.rollback()
                raise ControlPlaneError("observation_race") from error
        return ObservationReceipt(row.observation_id, client_id, checksum)


class PublicationService(_SessionService):
    """Register streams and atomically advance or roll back publications."""

    def register_stream(self, stream_key: str, *, provider: str, owner: str,
                        required_observations: Iterable[str], publication_strategy: str,
                        supported_windows: Iterable[str] = (), enabled: bool = False) -> PublicationStream:
        if not stream_key or not provider or not owner:
            raise ControlPlaneError("invalid_stream")
        now = self.clock()
        with self.session() as session, session.begin():
            row = session.get(PublicationStream, stream_key)
            if row is None:
                row = PublicationStream(stream_key=stream_key, provider=provider, owner=owner,
                    required_observations=_json(sorted(set(required_observations))), publication_strategy=publication_strategy,
                    supported_windows=_json(sorted(set(supported_windows))), enabled=enabled, created_at=now)
                session.add(row)
            else:
                row.enabled = enabled
        return row

    def compose(self, stream_key: str, *, season: str, cutoff: datetime, payload: Any,
                expected_fence: int | None = None, reason: str | None = None) -> PublicationVersion:
        encoded = _json(payload)
        now = self.clock()
        with self.session() as session, session.begin():
            stream = session.get(PublicationStream, stream_key)
            if stream is None or not stream.enabled:
                raise ControlPlaneError("stream_unavailable")
            pointer = session.get(PublicationPointer, stream_key)
            if pointer is None:
                pointer = PublicationPointer(stream_key=stream_key, fence=0, updated_at=now)
                session.add(pointer)
                session.flush()
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
        return publication

    def current(self, stream_key: str) -> PublicationVersion | None:
        with self.session() as session:
            pointer = session.get(PublicationPointer, stream_key)
            return session.get(PublicationVersion, pointer.active_publication_id) if pointer and pointer.active_publication_id else None

    def rollback(self, stream_key: str, *, reason: str) -> PublicationVersion:
        if not reason or len(reason.strip()) < 3:
            raise ControlPlaneError("reason_required")
        now = self.clock()
        with self.session() as session, session.begin():
            pointer = session.get(PublicationPointer, stream_key)
            if pointer is None or not pointer.previous_publication_id:
                raise ControlPlaneError("rollback_unavailable")
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
    "CURRENT_ENVELOPE_VERSION", "ControlPlaneError", "CollectorClaims", "CollectorTokenService",
    "CollectionControlService", "ObservationIngestionService", "ObservationReceipt", "PublicationService",
]
