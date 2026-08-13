"""Compatibility import for the public Residential Outbox seam."""

# Re-export the stable repository names for older rehearsal harnesses.
# ruff: noqa: F401

from .outbox import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ITEM_BYTES,
    LEASE_SECONDS,
    RETENTION_DAYS,
    OutboxBusy,
    OutboxError,
    OutboxFull,
    OutboxItem,
    OutboxRepository,
    OutboxRetentionError,
)
