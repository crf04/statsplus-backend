"""Crash-safe SQLite Residential Outbox.

Only compressed, normalized wire documents enter this database.  Provider
responses and credentials are never accepted as a separate field.  SQLite's
``BEGIN IMMEDIATE`` transaction is the local durability boundary; the server
receipt, not a local attempt counter, is the deletion boundary.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .contracts import (
    CatalogEnvelope,
    MAX_COMPRESSED_BYTES,
    ObservationEnvelope,
    ProviderContractError,
    payload_checksum,
)
from .diagnostics import safe_code

RETENTION_DAYS = 30
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_ITEM_BYTES = MAX_COMPRESSED_BYTES
LEASE_SECONDS = 60 * 60
MAX_METADATA_BYTES = 64 * 1024


class OutboxError(RuntimeError):
    """Base for safe outbox control outcomes."""


class OutboxBusy(OutboxError):
    """Another collector process owns the local lease."""


class OutboxFull(OutboxError):
    """The configured hard limit would be exceeded."""


class OutboxRetentionError(OutboxError):
    """Unsent work has aged out and needs operator action."""


def _utc(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("outbox timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | str | None = None) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _safe_metadata(value: Any, *, path: str = "metadata") -> Any:
    """Keep operational metadata bounded and reject secret-shaped fields."""

    forbidden = (
        "secret", "password", "credential", "authorization", "bearer", "token", "cookie", "private_key", "api_key",
        "raw_response", "provider_response", "player_fact", "player_facts",
    )
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if len(key) > 256:
                raise ProviderContractError("metadata_too_large")
            if any(part in key.casefold() for part in forbidden):
                raise ProviderContractError("credential_in_outbox")
            result[key] = _safe_metadata(child, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(child, path=f"{path}[]") for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise ProviderContractError("non_finite_value")
        return value
    raise ProviderContractError("metadata_not_json")


@dataclass(frozen=True, slots=True)
class OutboxItem:
    item_id: int
    kind: str
    client_observation_id: str
    checksum: str
    cutoff: datetime
    created_at: datetime
    payload: bytes
    metadata: Mapping[str, Any]
    attempts: int
    last_error: str | None
    request_id: str | None = None

    @property
    def compressed_bytes(self) -> int:
        return len(self.payload)


class OutboxRepository:
    """A bounded, single-file durable queue with an expiring process lease."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        retention_days: int = RETENTION_DAYS,
        clock: Any = _utc,
    ) -> None:
        if max_bytes < 1 or max_item_bytes < 1 or retention_days < 1:
            raise ValueError("outbox limits must be positive")
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.max_item_bytes = int(max_item_bytes)
        self.retention = timedelta(days=int(retention_days))
        self.clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        journal_mode = str(self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA wal_autocheckpoint=64")
        if journal_mode != "wal" or int(self._connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
            self._connection.close()
            raise OutboxError("required SQLite durability mode is unavailable")
        self._initialize()
        self._baseline_footprint = self._initialize_footprint_baseline()
        self._operational_headroom = min(max(4 * 1024, self.max_bytes // 8), 1024 * 1024)
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        maximum_pages = page_count + max(1, self.max_bytes // page_size)
        applied = int(self._connection.execute(f"PRAGMA max_page_count={maximum_pages}").fetchone()[0])
        if applied < maximum_pages:
            self._connection.close()
            raise OutboxError("SQLite hard page limit could not be applied")

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collector_outbox (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('observation', 'catalog')),
                    client_observation_id TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    cutoff TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    metadata TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    request_id TEXT,
                    UNIQUE(kind, client_observation_id, checksum)
                );
                CREATE INDEX IF NOT EXISTS ix_collector_outbox_ready
                    ON collector_outbox(cutoff DESC, created_at ASC, item_id ASC);
                CREATE TABLE IF NOT EXISTS collector_process_lease (
                    lease_id INTEGER PRIMARY KEY CHECK(lease_id = 1),
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collector_outbox_meta (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )

    def _initialize_footprint_baseline(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM collector_outbox_meta WHERE key = 'baseline_footprint'"
        ).fetchone()
        if row is not None:
            return int(row["value"])
        has_work = int(self._connection.execute("SELECT COUNT(*) FROM collector_outbox").fetchone()[0]) > 0
        if not has_work and self.path != ":memory:":
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        baseline = 0 if has_work else self.storage_footprint_bytes()
        self._connection.execute(
            "INSERT INTO collector_outbox_meta(key, value) VALUES('baseline_footprint', ?)",
            (baseline,),
        )
        if not has_work and self.path != ":memory:":
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            baseline = self.storage_footprint_bytes()
            self._connection.execute(
                "UPDATE collector_outbox_meta SET value = ? WHERE key = 'baseline_footprint'", (baseline,)
            )
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return baseline

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self.path != ":memory:":
                # Bound accumulated WAL growth before reserving the next write.
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                if self.storage_footprint_bytes() - self._baseline_footprint > self.max_bytes:
                    self._connection.execute("ROLLBACK")
                    raise OutboxFull("SQLite operational headroom is exhausted")
                self._connection.execute("COMMIT")

    def _total_bytes(self, connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT COALESCE(SUM(payload_bytes), 0) AS total FROM collector_outbox").fetchone()
        return int(row["total"])

    def storage_footprint_bytes(self) -> int:
        """Return allocated SQLite database plus WAL bytes (SHM is transient)."""

        with self._lock:
            page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        logical = page_size * page_count
        if self.path == ":memory:":
            return logical
        database = Path(self.path)
        return max(logical, database.stat().st_size if database.exists() else 0) + (
            Path(self.path + "-wal").stat().st_size if Path(self.path + "-wal").exists() else 0
        ) + (Path(self.path + "-shm").stat().st_size if Path(self.path + "-shm").exists() else 0)

    def durability_pragmas(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "journal_mode": str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold(),
                "synchronous": int(self._connection.execute("PRAGMA synchronous").fetchone()[0]),
                "foreign_keys": int(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            }

    @staticmethod
    def _row(row: sqlite3.Row) -> OutboxItem:
        return OutboxItem(
            item_id=int(row["item_id"]),
            kind=str(row["kind"]),
            client_observation_id=str(row["client_observation_id"]),
            checksum=str(row["checksum"]),
            cutoff=_utc(str(row["cutoff"])),
            created_at=_utc(str(row["created_at"])),
            payload=bytes(row["payload"]),
            metadata=json.loads(str(row["metadata"])),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            request_id=row["request_id"],
        )

    def enqueue(
        self,
        *,
        kind: str,
        client_observation_id: str,
        checksum: str,
        cutoff: datetime | str,
        payload: bytes,
        metadata: Mapping[str, Any],
        request_id: str | None = None,
    ) -> OutboxItem:
        if kind not in {"observation", "catalog"}:
            raise ValueError("unsupported outbox item kind")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise ValueError("outbox payload must be non-empty bytes")
        payload_bytes = bytes(payload)
        if len(payload_bytes) > self.max_item_bytes:
            raise OutboxFull("one normalized envelope exceeds the outbox item limit")
        self._validate_wire_payload(payload_bytes, kind=kind, expected_checksum=str(checksum))
        safe = _safe_metadata(dict(metadata))
        encoded_metadata = json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if len(encoded_metadata.encode("utf-8")) > MAX_METADATA_BYTES:
            raise OutboxFull("outbox routing metadata exceeds its hard limit")
        current = _utc(self.clock())
        cutoff_value = _utc(cutoff)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM collector_outbox WHERE kind = ? AND client_observation_id = ? AND checksum = ?",
                (kind, client_observation_id, checksum),
            ).fetchone()
            if existing is not None:
                return self._row(existing)
            conflict = connection.execute(
                "SELECT 1 FROM collector_outbox WHERE kind = ? AND client_observation_id = ? AND checksum <> ? LIMIT 1",
                (kind, client_observation_id, checksum),
            ).fetchone()
            if conflict is not None:
                raise OutboxError("client observation ID is already bound to a different checksum")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            reserved = len(payload_bytes) + len(encoded_metadata.encode("utf-8")) + (3 * page_size)
            committed_payload = self._total_bytes(connection)
            if (
                self.storage_footprint_bytes() - self._baseline_footprint
                + committed_payload + reserved
                > self.max_bytes - self._operational_headroom
            ):
                raise OutboxFull("the outbox hard limit would discard unsent work")
            cursor = connection.execute(
                """INSERT INTO collector_outbox
                   (kind, client_observation_id, checksum, cutoff, created_at,
                    payload, payload_bytes, metadata, request_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (kind, str(client_observation_id), str(checksum), _iso(cutoff_value), _iso(current),
                 sqlite3.Binary(payload_bytes), len(payload_bytes), encoded_metadata, request_id),
            )
            row = connection.execute("SELECT * FROM collector_outbox WHERE item_id = ?", (cursor.lastrowid,)).fetchone()
            assert row is not None
            if self.storage_footprint_bytes() - self._baseline_footprint > self.max_bytes:
                raise OutboxFull("the outbox hard limit would discard unsent work")
            return self._row(row)

    def _validate_wire_payload(self, payload: bytes, *, kind: str, expected_checksum: str) -> None:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        output = bytearray()
        try:
            for offset in range(0, len(payload), 64 * 1024):
                output.extend(decoder.decompress(payload[offset:offset + 64 * 1024], 10 * 1024 * 1024 - len(output) + 1))
                if len(output) > 10 * 1024 * 1024 or decoder.unconsumed_tail:
                    raise ValueError
            output.extend(decoder.flush(10 * 1024 * 1024 - len(output) + 1))
            if not decoder.eof or decoder.unused_data or len(output) > 10 * 1024 * 1024:
                raise ValueError
            document = json.loads(bytes(output))
        except (ValueError, TypeError, UnicodeDecodeError, OverflowError, zlib.error, json.JSONDecodeError) as error:
            raise ProviderContractError("outbox_payload_not_envelope") from error
        if not isinstance(document, Mapping) or not isinstance(document.get("payload"), Mapping):
            raise ProviderContractError("outbox_payload_not_envelope")
        required = {
            "client_observation_id", "environment", "provider", "observation_type",
            "scope", "season", "cutoff", "schema_version", "retrieved_at", "checksum", "payload",
        }
        if kind == "observation":
            required.add("manifest_id")
            allowed = required
        else:
            required.update({"manifest_id", "catalog_version"})
            allowed = required | {"expires_at"}
        if not required <= set(document) or not set(document) <= allowed:
            raise ProviderContractError("outbox_payload_not_envelope")
        if not isinstance(document.get("client_observation_id"), str) or not document["client_observation_id"].strip():
            raise ProviderContractError("outbox_payload_not_envelope")
        if not isinstance(document.get("checksum"), str) or document.get("checksum") != payload_checksum(document["payload"]) or document.get("checksum") != expected_checksum:
            raise ProviderContractError("checksum_mismatch")
        if kind == "observation":
            if not isinstance(document.get("manifest_id"), str) or not document["manifest_id"].strip():
                raise ProviderContractError("outbox_payload_not_envelope")
        elif document.get("manifest_id") is not None:
            raise ProviderContractError("outbox_payload_not_envelope")
        if kind == "catalog" and "expires_at" in document:
            try:
                _utc(str(document["expires_at"]))
            except (TypeError, ValueError):
                raise ProviderContractError("outbox_payload_not_envelope")

    def enqueue_observation(self, envelope: ObservationEnvelope) -> OutboxItem:
        return self.enqueue(
            kind="observation",
            client_observation_id=envelope.client_observation_id,
            checksum=envelope.checksum,
            cutoff=envelope.cutoff,
            payload=envelope.compressed_wire(),
            metadata=envelope.as_outbox_metadata(),
        )

    def enqueue_catalog(self, envelope: CatalogEnvelope) -> OutboxItem:
        return self.enqueue(
            kind="catalog",
            client_observation_id=envelope.envelope.client_observation_id,
            checksum=envelope.envelope.checksum,
            cutoff=envelope.envelope.cutoff,
            payload=envelope.compressed_wire(),
            metadata=envelope.as_outbox_metadata(),
            request_id=envelope.request_id,
        )

    def get(self, item_id: int) -> OutboxItem | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM collector_outbox WHERE item_id = ?", (int(item_id),)).fetchone()
            return self._row(row) if row is not None else None

    def pending(self, *, limit: int = 100) -> tuple[OutboxItem, ...]:
        bounded = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM collector_outbox ORDER BY cutoff DESC, created_at ASC, item_id ASC LIMIT ?",
                (bounded,),
            ).fetchall()
            return tuple(self._row(row) for row in rows)

    def aged_pending(self, *, now: datetime | str | None = None, limit: int = 1000) -> tuple[OutboxItem, ...]:
        threshold = _utc(now or self.clock()) - self.retention
        bounded = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM collector_outbox WHERE created_at < ? ORDER BY created_at ASC, item_id ASC LIMIT ?",
                (_iso(threshold), bounded),
            ).fetchall()
            return tuple(self._row(row) for row in rows)

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM collector_outbox").fetchone()
            return int(row["count"])

    def bytes_pending(self) -> int:
        with self._lock:
            return self._total_bytes(self._connection)

    def mark_attempt(self, item_id: int, *, reason: str | None = None) -> None:
        safe_reason = safe_code(reason, fallback="collector_failure") if reason else None
        with self._transaction() as connection:
            connection.execute(
                "UPDATE collector_outbox SET attempts = attempts + 1, last_error = ? WHERE item_id = ?",
                (safe_reason, int(item_id)),
            )

    def acknowledge(self, item_id: int, *, checksum: str) -> bool:
        """Delete only after the durable Railway receipt matches exactly."""

        with self._transaction() as connection:
            row = connection.execute("SELECT checksum FROM collector_outbox WHERE item_id = ?", (int(item_id),)).fetchone()
            if row is None:
                return False
            if str(row["checksum"]) != str(checksum):
                raise OutboxError("durable receipt checksum did not match the outbox item")
            connection.execute("DELETE FROM collector_outbox WHERE item_id = ?", (int(item_id),))
            return True

    def enforce_retention(self, *, now: datetime | str | None = None) -> None:
        current = _utc(now or self.clock())
        threshold = current - self.retention
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM collector_outbox WHERE created_at < ?", (_iso(threshold),)
            ).fetchone()
        if row is not None and int(row["count"]) > 0:
            raise OutboxRetentionError("unsent outbox work is older than the 30-day retention bound")

    def prune_obsolete(self, *, governed_before_cutoff: datetime | str) -> int:
        """Remove only work made obsolete by an explicit governed cutoff."""

        threshold = _utc(governed_before_cutoff)
        with self._transaction() as connection:
            cursor = connection.execute("DELETE FROM collector_outbox WHERE cutoff < ?", (_iso(threshold),))
            return int(cursor.rowcount)

    def acquire_lease(self, *, owner: str | None = None, ttl_seconds: int = LEASE_SECONDS) -> str:
        owner = owner or secrets.token_urlsafe(18)
        if not owner or ttl_seconds < 1:
            raise ValueError("a lease owner and positive TTL are required")
        now = _utc(self.clock())
        expires = now + timedelta(seconds=min(int(ttl_seconds), 24 * 60 * 60))
        with self._transaction() as connection:
            row = connection.execute("SELECT owner, expires_at FROM collector_process_lease WHERE lease_id = 1").fetchone()
            if row is not None and _utc(str(row["expires_at"])) > now:
                raise OutboxBusy("another collector invocation owns the process lease")
            connection.execute(
                "INSERT INTO collector_process_lease(lease_id, owner, expires_at) VALUES(1, ?, ?) "
                "ON CONFLICT(lease_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
                (owner, _iso(expires)),
            )
        return owner

    def renew_lease(self, owner: str, *, ttl_seconds: int = LEASE_SECONDS) -> bool:
        now = _utc(self.clock())
        expires = now + timedelta(seconds=min(int(ttl_seconds), 24 * 60 * 60))
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE collector_process_lease SET expires_at = ? WHERE lease_id = 1 AND owner = ? AND expires_at > ?",
                (_iso(expires), owner, _iso(now)),
            )
            return cursor.rowcount == 1

    def release_lease(self, owner: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute("DELETE FROM collector_process_lease WHERE lease_id = 1 AND owner = ?", (owner,))
            return cursor.rowcount == 1

    @contextmanager
    def process_lease(self, *, owner: str | None = None, ttl_seconds: int = LEASE_SECONDS) -> Iterator[str]:
        lease_owner = self.acquire_lease(owner=owner, ttl_seconds=ttl_seconds)
        try:
            yield lease_owner
        finally:
            self.release_lease(lease_owner)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "OutboxRepository":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_MAX_BYTES", "DEFAULT_MAX_ITEM_BYTES", "LEASE_SECONDS", "RETENTION_DAYS",
    "OutboxBusy", "OutboxError", "OutboxFull", "OutboxItem", "OutboxRepository",
    "OutboxRetentionError",
]
