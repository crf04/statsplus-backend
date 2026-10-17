"""The separately installable StatsPlus residential collector.

This package deliberately has no Flask, SQLAlchemy, or route imports.  The
collector is a pull-only process: it obtains short-lived Railway credentials,
normalizes NBA responses, stores compressed observations in its local outbox,
and uploads them over the narrow collector API.
"""

from .cache import CachedInstructions, InstructionCache
from .client import (
    CollectorHTTPError,
    CollectorToken,
    HTTPResponse,
    RailwayClient,
    RequestsTransport,
)
from .contracts import (
    CURRENT_ENVELOPE_VERSION,
    MAX_COMPRESSED_BYTES,
    MAX_ENVELOPE_BYTES,
    NormalizedObservation,
    ObservationEnvelope,
    ProviderContractError,
    canonical_json,
    payload_checksum,
)
from .normalizers import (
    normalize_grouped_shot_response,
    normalize_opponent_grouped_shot_response,
    normalize_opponent_synergy_response,
    normalize_opponent_zone_response,
    normalize_roster_response,
    normalize_schedule_response,
    normalize_synergy_response,
    normalize_zone_response,
)
from .outbox import (
    OutboxBusy,
    OutboxFull,
    OutboxItem,
    OutboxRepository,
    OutboxRetentionError,
    ResidentialOutbox,
)
from .rehearsal import CompatibilityProbe, ProbeResult, ResidentialCompatibilityProbes
from .runner import (
    EXIT_BUSY,
    EXIT_NO_WORK,
    EXIT_NON_RETRYABLE,
    EXIT_RETRY,
    ResidentialCollector,
    RunDisposition,
    RunResult,
)

__all__ = [
    "CURRENT_ENVELOPE_VERSION",
    "EXIT_BUSY",
    "EXIT_NON_RETRYABLE",
    "EXIT_NO_WORK",
    "EXIT_RETRY",
    "MAX_COMPRESSED_BYTES",
    "MAX_ENVELOPE_BYTES",
    "CachedInstructions",
    "CollectorHTTPError",
    "CollectorToken",
    "CompatibilityProbe",
    "HTTPResponse",
    "InstructionCache",
    "NormalizedObservation",
    "ObservationEnvelope",
    "OutboxBusy",
    "OutboxFull",
    "OutboxItem",
    "OutboxRepository",
    "OutboxRetentionError",
    "ProbeResult",
    "ProviderContractError",
    "RailwayClient",
    "RequestsTransport",
    "ResidentialCollector",
    "ResidentialCompatibilityProbes",
    "ResidentialOutbox",
    "RunDisposition",
    "RunResult",
    "canonical_json",
    "normalize_grouped_shot_response",
    "normalize_opponent_grouped_shot_response",
    "normalize_opponent_synergy_response",
    "normalize_opponent_zone_response",
    "normalize_roster_response",
    "normalize_schedule_response",
    "normalize_synergy_response",
    "normalize_zone_response",
    "payload_checksum",
]
