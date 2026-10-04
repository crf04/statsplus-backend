"""The provenance block every MCP-read route returns (crf04/statsplus#107).

One shape for the Slate, Matchup, Unscheduled Matchup, game logs, Targets
resolve and the Draft Target preview::

    {"generation": [<PublicationRead.to_dict()>, ...],
     "sources": {<name>: {"status": ..., "retrieved_at": ...}, ...}}

``generation`` is built from the request's own Publication snapshot -- the
capture its facts were read from, never a second one -- plus any retained
prior-season Publication a past game's participants were read from, so a
client can tell which Publications an answer used and how old each was.  One
stream can appear twice, once per season; ``season`` tells the entries apart.
``sources`` names the dependencies that are not Publications (the schedule,
the player pool, injuries) with the existing ``status`` and ``retrieved_at``
of the read the answer used.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from app.domain.utc import parse_utc_iso

#: A stream the registry can never serve (e.g. ``synergy:l15``) is not a
#: dependency of any read, so it is not listed.
_UNSUPPORTED_REASON = "provider_window_unsupported"


#: A source no read reached: nothing was retrieved, so nothing is claimed.
UNAVAILABLE_SOURCE = {"status": "unavailable", "retrieved_at": None}
#: How fresh each source status is; any other status (``missing``,
#: ``unavailable``, a pool's mixed providers) is the least fresh.
_SOURCE_FRESHNESS = {"fresh": 0, "stale": 1, "stale-served": 1}


def provenance_block(
    snapshot: Any | None,
    sources: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    streams: Collection[str] | None = None,
    also: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Describe the generation ``snapshot`` captured and the named sources.

    ``streams`` narrows the generation to the streams the returned facts
    used, when a capture covered more than they read.  ``also`` adds entries,
    already in the read shape, for Publications the facts read outside the
    snapshot (a retained prior-season one); an entry naming the same
    stream, season and Publication as one already listed is not repeated.
    The result is sorted by stream key, then season.

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
        if streams is not None and stream_key not in streams:
            continue
        entry = read.to_dict()
        if not (read.available and read.freshness in {"fresh", "stale"}):
            entry["freshness"] = "unavailable"
        generation.append(entry)
    listed = {_publication_key(entry) for entry in generation}
    for entry in also:
        if _publication_key(entry) not in listed:
            listed.add(_publication_key(entry))
            generation.append(dict(entry))
    generation.sort(key=lambda entry: (entry["stream_key"], entry["season"] or ""))
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


def _publication_key(entry: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return (entry["stream_key"], entry["season"], entry["publication_id"])


def matchup_generation(
    matchups: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Every generation entry the composed Matchups reported.

    Each Matchup composed from a shared snapshot lists that snapshot's
    streams and any retained Publication its own participants came from.
    """

    return [
        entry
        for matchup in matchups
        for entry in (matchup.get("provenance") or {}).get("generation", ())
    ]


def least_fresh(reads: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The least fresh of several reads of one source.

    The worst status wins, then the oldest ``retrieved_at``; a read that
    retrieved nothing is the oldest.  An empty ``reads`` reached no source.
    """

    def staleness(read: Mapping[str, Any]) -> tuple[int, float]:
        retrieved_at = read.get("retrieved_at")
        return (
            _SOURCE_FRESHNESS.get(read.get("status"), 2),
            float("inf")
            if retrieved_at is None
            else -parse_utc_iso(str(retrieved_at)).timestamp(),
        )

    return max(reads, key=staleness, default=UNAVAILABLE_SOURCE)


def target_sources(
    slate: Mapping[str, Any], matchups: Iterable[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    """The sources a Targets answer read: the Slate's, then its Matchups'.

    The Slate's schedule chose the games. Every composed Matchup made its own
    pool and injury reads, and those named the Fits, so they are the ones
    reported -- the least fresh when Matchups differ. With no Matchup
    composed, the Slate's own reads supplied the whole (idle) answer.
    """

    sources = dict(slate["provenance"]["sources"])
    composed = [matchup["freshness"] for matchup in matchups]
    if composed:
        for name in ("pool", "injuries"):
            sources[name] = least_fresh(freshness[name] for freshness in composed)
    return sources


__all__ = [
    "UNAVAILABLE_SOURCE",
    "least_fresh",
    "matchup_generation",
    "provenance_block",
    "target_sources",
]
