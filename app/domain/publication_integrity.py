"""Canonical encoding and integrity checks for immutable publications."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def canonical_publication_json(value: Any) -> str:
    """Encode one publication document in its persisted canonical form."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def publication_payload_checksum(payload: str | bytes) -> str:
    """Hash the exact immutable payload bytes stored in a publication row."""

    encoded = payload.encode() if isinstance(payload, str) else payload
    return hashlib.sha256(encoded).hexdigest()


def publication_payload_matches_checksum(
    payload: str | bytes,
    checksum: str,
) -> bool:
    """Compare one stored payload with its persisted digest in constant time."""

    try:
        computed = publication_payload_checksum(payload)
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(computed, str(checksum))


__all__ = [
    "canonical_publication_json",
    "publication_payload_checksum",
    "publication_payload_matches_checksum",
]
