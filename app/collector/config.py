"""Configuration boundary for the residential process.

The only credential accepted by this module is an in-memory secret supplied by
the caller.  Paths and endpoints are ordinary configuration; secrets are
loaded from a credential provider immediately before token exchange.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


class CollectorConfigurationError(ValueError):
    """A missing, unsafe, or contradictory collector setting."""


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    railway_url: str
    environment: str
    identity_id: str
    outbox_path: Path
    log_path: Path
    release_version: str
    token_ttl_seconds: int = 300
    poll_limit: int = 100
    http_timeout_seconds: float = 30.0
    outbox_max_bytes: int = 256 * 1024 * 1024
    outbox_max_item_bytes: int = 2 * 1024 * 1024
    retry_window_seconds: int = 6 * 60 * 60
    retry_interval_seconds: int = 30 * 60
    allow_insecure_localhost: bool = False

    def __post_init__(self) -> None:
        try:
            endpoint = urlsplit(self.railway_url)
            hostname = endpoint.hostname
            _ = endpoint.port
        except ValueError as error:
            raise CollectorConfigurationError("COLLECTOR_RAILWAY_URL is malformed") from error
        if endpoint.scheme not in {"https", "http"} or not hostname or endpoint.username or endpoint.password:
            raise CollectorConfigurationError("COLLECTOR_RAILWAY_URL must be an HTTP(S) URL")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if endpoint.scheme == "http" and not (self.allow_insecure_localhost and hostname.casefold() in local_hosts):
            raise CollectorConfigurationError("collector endpoint must use HTTPS")
        if self.environment not in {"development", "testing", "test", "staging", "production", "historical_rehearsal"}:
            raise CollectorConfigurationError("unsupported collector environment")
        if not self.identity_id.strip():
            raise CollectorConfigurationError("COLLECTOR_IDENTITY_ID is required")
        if not self.release_version.strip() or any(char.isspace() for char in self.release_version):
            raise CollectorConfigurationError("COLLECTOR_RELEASE_VERSION is malformed")
        if not 1 <= self.token_ttl_seconds <= 900:
            raise CollectorConfigurationError("collector token TTL must be 1..900 seconds")
        if not 1 <= self.poll_limit <= 100:
            raise CollectorConfigurationError("collector poll limit must be 1..100")
        if self.outbox_max_bytes < self.outbox_max_item_bytes:
            raise CollectorConfigurationError("outbox hard limit must fit one envelope")
        if self.http_timeout_seconds <= 0 or self.retry_window_seconds <= 0 or self.retry_interval_seconds <= 0:
            raise CollectorConfigurationError("collector timing settings must be positive")


def _value(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name, default)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _value(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise CollectorConfigurationError(f"{name} must be an integer") from error


def load_collector_config(env: Mapping[str, str] | None = None) -> CollectorConfig:
    values = os.environ if env is None else env
    url = _value(values, "COLLECTOR_RAILWAY_URL")
    identity = _value(values, "COLLECTOR_IDENTITY_ID")
    if not url or not identity:
        raise CollectorConfigurationError("COLLECTOR_RAILWAY_URL and COLLECTOR_IDENTITY_ID are required")
    environment = _value(values, "COLLECTOR_ENVIRONMENT", "production") or "production"
    outbox = Path(_value(values, "COLLECTOR_OUTBOX_PATH", str(Path.home() / ".statsplus" / "collector" / "outbox.sqlite3")) or "outbox.sqlite3")
    log = Path(_value(values, "COLLECTOR_LOG_PATH", str(outbox.with_suffix(".log"))) or str(outbox.with_suffix(".log")))
    return CollectorConfig(
        railway_url=url,
        environment=environment,
        identity_id=identity,
        outbox_path=outbox,
        log_path=log,
        release_version=_value(values, "COLLECTOR_RELEASE_VERSION", "0.1.0") or "0.1.0",
        token_ttl_seconds=_integer(values, "COLLECTOR_TOKEN_TTL_SECONDS", 300),
        poll_limit=_integer(values, "COLLECTOR_POLL_LIMIT", 100),
        http_timeout_seconds=float(_value(values, "COLLECTOR_HTTP_TIMEOUT_SECONDS", "30") or "30"),
        outbox_max_bytes=_integer(values, "COLLECTOR_OUTBOX_MAX_BYTES", 256 * 1024 * 1024),
        outbox_max_item_bytes=_integer(values, "COLLECTOR_OUTBOX_MAX_ITEM_BYTES", 2 * 1024 * 1024),
        retry_window_seconds=_integer(values, "COLLECTOR_RETRY_WINDOW_SECONDS", 6 * 60 * 60),
        retry_interval_seconds=_integer(values, "COLLECTOR_RETRY_INTERVAL_SECONDS", 30 * 60),
        allow_insecure_localhost=(str(_value(values, "COLLECTOR_ALLOW_INSECURE_LOCALHOST", "false")).casefold() in {"1", "true", "yes", "on"}),
    )


__all__ = ["CollectorConfig", "CollectorConfigurationError", "load_collector_config"]
