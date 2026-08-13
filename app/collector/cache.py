"""Bounded, non-secret cache for Railway collection instructions."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import parse_datetime


@dataclass(frozen=True, slots=True)
class CachedInstructions:
    bootstrap_requests: tuple[Mapping[str, Any], ...]
    manifests: tuple[Mapping[str, Any], ...]
    cached_at: datetime
    environment: str | None = None


class InstructionCache:
    """Crash-safe JSON cache containing routing metadata only.

    Cached instructions are useful during a Railway outage but cannot extend
    the server's expiry window.  Payload facts, credentials, and response
    bodies are deliberately rejected from this cache.
    """

    _allowed_bootstrap = frozenset({
        "request_id", "catalog_type", "season", "cutoff", "expires_at", "catalog_version",
        "status", "completed_at", "failure_reason",
    })
    _allowed_manifest = frozenset({"manifest_id", "season", "cutoff", "collect_before", "accepted_versions", "scopes", "checksum", "scope_parameters", "parameters", "status"})
    _max_bytes = 256 * 1024

    def __init__(self, path: str | os.PathLike[str], *, clock: Any | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _safe(value: Any, allowed: frozenset[str]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("cached instruction must be an object")
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("cached instruction contains unsupported fields")
        forbidden = ("secret", "password", "credential", "authorization", "bearer", "token", "cookie", "private_key", "api_key")

        def scrub(child: Any) -> Any:
            if isinstance(child, Mapping):
                result: dict[str, Any] = {}
                for raw_key, raw_value in child.items():
                    key = str(raw_key)
                    if any(marker in key.casefold() for marker in forbidden):
                        raise ValueError("cached instruction contains credential-shaped metadata")
                    result[key] = scrub(raw_value)
                return result
            if isinstance(child, (list, tuple)):
                return [scrub(item) for item in child]
            return child

        return json.loads(json.dumps(scrub(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    def store(self, discovery: Mapping[str, Any]) -> None:
        environment = discovery.get("environment")
        if not isinstance(environment, str) or not environment.strip():
            raise ValueError("discovery cache environment is required")
        requests = discovery.get("bootstrap_requests", [])
        manifests = discovery.get("manifests", [])
        if not isinstance(requests, list) or not isinstance(manifests, list):
            raise ValueError("discovery cache shape is malformed")
        safe_requests = [self._safe(value, self._allowed_bootstrap) for value in requests]
        safe_manifests = [self._safe(value, self._allowed_manifest) for value in manifests]
        value = {
            "cached_at": self.clock().astimezone(timezone.utc).isoformat(),
            "environment": environment.strip(),
            "bootstrap_requests": safe_requests,
            "manifests": safe_manifests,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise ValueError("cached instructions exceed the bounded cache")
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def load(self, *, now: datetime | None = None, environment: str | None = None) -> CachedInstructions:
        current = (now or self.clock()).astimezone(timezone.utc)
        if not self.path.exists():
            return CachedInstructions((), (), current, environment)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                return CachedInstructions((), (), current, environment)
            cached_environment = value.get("environment")
            if not isinstance(cached_environment, str) or not cached_environment.strip():
                return CachedInstructions((), (), current, environment)
            if environment is not None and cached_environment.strip() != environment.strip():
                return CachedInstructions((), (), current, environment)
            requests = tuple(self._safe(item, self._allowed_bootstrap) for item in value.get("bootstrap_requests", []))
            manifests = tuple(self._safe(item, self._allowed_manifest) for item in value.get("manifests", []))
        except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return CachedInstructions((), (), current, environment)
        requests = tuple(item for item in requests if not self._expired(item, current, "expires_at"))
        manifests = tuple(item for item in manifests if not self._expired(item, current, "collect_before"))
        return CachedInstructions(requests, manifests, current, cached_environment.strip())

    @staticmethod
    def _expired(item: Mapping[str, Any], now: datetime, field: str) -> bool:
        raw = item.get(field)
        if not raw:
            return True
        try:
            return parse_datetime(str(raw)) <= now
        except (TypeError, ValueError):
            return True


__all__ = ["CachedInstructions", "InstructionCache"]
