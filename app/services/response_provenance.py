"""The provenance block every MCP-read route returns (crf04/statsplus#107).

One shape for the Slate, Matchup, Unscheduled Matchup, game logs, Targets
resolve and the Draft Target preview::

    {"generation": [<PublicationRead.to_dict()>, ...],
     "sources": {<name>: {"status": ..., "retrieved_at": ...}, ...}}

``generation`` is built from the request's own Publication snapshot -- the
capture its facts were read from, never a second one -- so a client can tell
which Publications an answer used and how old each was.  ``sources`` names the
dependencies that are not Publications (the schedule, the player pool,
injuries) with the status the response already reports for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: A stream the registry can never serve (e.g. ``synergy:l15``) is not a
#: dependency of any read, so it is not listed.
_UNSUPPORTED_REASON = "provider_window_unsupported"


def provenance_block(
    snapshot: Any | None,
    sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe the generation ``snapshot`` captured and the named sources.

    A stream's ``freshness`` is the reader's age label (``fresh``/``stale``,
    from its per-stream freshness rule) only when the read could serve it;
    every refused, missing or legacy-fallback read is ``unavailable``, even
    when the reader computed an age for it.  A missing snapshot -- no reader,
    or a read that captured none -- reads no Publication: ``[]``.
    """

    # Older readers and request doubles may hand back a snapshot without
    # per-stream reads; such a capture names no stream.
    reads = getattr(snapshot, "reads", None) or {}
    generation = []
    for stream_key in sorted(reads):
        read = reads[stream_key]
        if read.unavailable_reason == _UNSUPPORTED_REASON:
            continue
        entry = read.to_dict()
        if not (read.available and read.freshness in {"fresh", "stale"}):
            entry["freshness"] = "unavailable"
        generation.append(entry)
    return {
        "generation": generation,
        "sources": {
            name: {
                "status": freshness.get("status"),
                "retrieved_at": freshness.get("retrieved_at"),
            }
            for name, freshness in (sources or {}).items()
        },
    }


def slate_sources(slate: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The non-Publication sources a read of ``slate`` depends on."""

    freshness = slate["freshness"]
    return {"schedule": freshness["schedule"], "pool": freshness["pool"]}


__all__ = ["provenance_block", "slate_sources"]
