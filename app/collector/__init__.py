"""The separately installable StatsPlus residential collector.

This package deliberately has no Flask, SQLAlchemy, or route imports.  The
collector is a pull-only process: it obtains short-lived Railway credentials,
normalizes NBA responses, stores compressed observations in its local outbox,
and uploads them over the narrow collector API.
"""

from .contracts import (
    CURRENT_ENVELOPE_VERSION,
    MAX_ENVELOPE_BYTES,
    MAX_COMPRESSED_BYTES,
    ObservationEnvelope,
    NormalizedObservation,
    ProviderContractError,
    canonical_json,
    payload_checksum,
)
from .normalizers import (
    normalize_grouped_shot_response,
    normalize_opponent_grouped_shot_response,
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
)
from .client import (
    CollectorHTTPError,
    CollectorToken,
    HTTPResponse,
    RailwayClient,
    RequestsTransport,
)
from .cache import CachedInstructions, InstructionCache
from .runner import (
    EXIT_BUSY,
    EXIT_NO_WORK,
    EXIT_NON_RETRYABLE,
    EXIT_RETRY,
    RunDisposition,
    RunResult,
    ResidentialCollector,
)
from .rehearsal import CompatibilityProbe, ProbeResult, ResidentialCompatibilityProbes

__all__ = [
    "CURRENT_ENVELOPE_VERSION",
    "MAX_ENVELOPE_BYTES",
    "MAX_COMPRESSED_BYTES",
    "ObservationEnvelope",
    "NormalizedObservation",
    "ProviderContractError",
    "canonical_json",
    "payload_checksum",
    "normalize_grouped_shot_response",
    "normalize_opponent_grouped_shot_response",
    "normalize_opponent_zone_response",
    "normalize_roster_response",
    "normalize_schedule_response",
    "normalize_synergy_response",
    "normalize_zone_response",
    "OutboxBusy",
    "OutboxFull",
    "OutboxItem",
    "OutboxRepository",
    "OutboxRetentionError",
    "CollectorHTTPError",
    "CollectorToken",
    "HTTPResponse",
    "RailwayClient",
    "RequestsTransport",
    "CachedInstructions",
    "InstructionCache",
    "EXIT_BUSY",
    "EXIT_NO_WORK",
    "EXIT_NON_RETRYABLE",
    "EXIT_RETRY",
    "RunDisposition",
    "RunResult",
    "ResidentialCollector",
    "CompatibilityProbe",
    "ProbeResult",
    "ResidentialCompatibilityProbes",
]
