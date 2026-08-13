"""Bounded safe diagnostics for the residential process."""

from __future__ import annotations

import json
import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


SAFE_CODES = frozenset({
    "no_work", "complete", "retry", "non_retryable", "busy", "work_complete", "work_pending", "railway_timeout", "railway_unavailable",
    "provider_timeout", "provider_unavailable", "provider_schema_changed", "value_invariant_failed",
    "identity_unresolved", "scope_not_registered", "provider_window_unsupported", "invalid_token",
    "token_expired", "environment_mismatch", "outbox_full", "outbox_retention", "collector_busy",
    "operation_conflict", "control_rejected", "malformed_control_response", "malformed_receipt",
    "provider_scope_unavailable", "provider_category_changed", "scope_team_required",
    "scope_failure", "provider_failure", "credential_unavailable", "token_failure",
    "discovery_failure", "outbox_receipt_invalid", "outbox_payload_not_envelope",
    "bootstrap_expired", "bootstrap_not_pending", "manifest_expired", "manifest_not_active",
    "malformed_envelope", "malformed_catalog", "malformed_manifest", "malformed_discovery",
    "malformed_token_response", "manifest_scope_mismatch", "schema_unsupported", "scope_denied",
    "forbidden", "not_found", "control_rejected", "payload_too_large", "compression_required",
    "catalog_observation_invalid", "bootstrap_not_found", "incomplete_observation",
    "cache_rejected",
})


def safe_code(value: Any, *, fallback: str = "collector_failure") -> str:
    text = str(value or fallback).strip().casefold()
    return text[:80] if text in SAFE_CODES else fallback


class SafeStatus:
    """In-memory bounded state; no provider payloads are accepted."""

    def __init__(self, *, max_events: int = 100) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_events)))
        self.counts: dict[str, int] = {}

    def record(self, code: str, *, scope: str | None = None, status: str | None = None) -> None:
        code = safe_code(code)
        self.counts[code] = self.counts.get(code, 0) + 1
        event: dict[str, Any] = {"code": code}
        if scope:
            event["scope"] = str(scope)[:80]
        if status:
            event["status"] = str(status)[:40]
        self.events.append(event)

    def snapshot(self, *, version: str, release_checksum: str | None = None) -> dict[str, Any]:
        return {
            "version": str(version)[:80],
            "release_checksum": str(release_checksum or "")[:128] or None,
            "counts": dict(sorted(self.counts.items())),
            "recent": list(self.events),
        }


def build_safe_logger(path: str | Path, *, name: str = "statsplus.residential", max_bytes: int = 1_000_000, backups: int = 3) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def log_status(logger: logging.Logger, code: str, *, scope: str | None = None, detail: str | None = None) -> None:
    fields = {"code": safe_code(code)}
    if scope:
        fields["scope"] = str(scope)[:80]
    if detail:
        fields["detail"] = str(detail)[:160]
    logger.info(json.dumps(fields, sort_keys=True, separators=(",", ":")))


__all__ = ["SAFE_CODES", "SafeStatus", "build_safe_logger", "log_status", "safe_code"]
