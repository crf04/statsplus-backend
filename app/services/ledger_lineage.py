"""Immutable correction lineage shared by queue and materialization boundaries.

Lineage is keyed by game identity.  Keeping a checksum beside the game that
produced it is important: two independently coalesced corrections must never
re-associate a checksum with a different trigger merely because two tuples
happened to have the same length.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True, order=True)
class LedgerEvidence:
    """One immutable keyed ledger observation."""

    game_id: str
    ledger_checksum: str
    source_observation_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "game_id", str(self.game_id))
        object.__setattr__(self, "ledger_checksum", str(self.ledger_checksum))
        object.__setattr__(self, "source_observation_id", str(self.source_observation_id))


@dataclass(frozen=True, slots=True, init=False)
class LedgerLineage:
    """Immutable, commutatively mergeable keyed correction lineage.

    ``evidence`` is the canonical representation.  The compatibility
    properties below retain the old read shape for callers that only need the
    sorted IDs/checksums; they are derived from the keyed evidence and are not
    used for merging.

    The constructor accepts the old five-positional shape as a migration aid.
    New code should use :meth:`single` or pass an iterable/mapping of
    :class:`LedgerEvidence` values.
    """

    evidence: tuple[LedgerEvidence, ...]
    cutoff: datetime
    recomposition_reason: str
    _source_ids: tuple[str, ...]

    def __init__(
        self,
        source_observation_ids=None,
        game_ids=None,
        ledger_checksums=None,
        cutoff: datetime | None = None,
        recomposition_reason: str | None = None,
        *,
        evidence=None,
    ) -> None:
        # Compatibility with the former parallel-array constructor.  It is
        # intentionally converted immediately into keyed evidence so no
        # instance can retain the unsafe representation.
        if cutoff is None or recomposition_reason is None:
            raise TypeError("cutoff and recomposition_reason are required")
        if evidence is not None:
            if source_observation_ids is not None or game_ids is not None or ledger_checksums is not None:
                raise TypeError("evidence cannot be combined with legacy lineage fields")
            source_observation_ids = evidence
        evidence = _coerce_evidence(
            source_observation_ids,
            game_ids=game_ids,
            ledger_checksums=ledger_checksums,
        )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "cutoff", cutoff)
        object.__setattr__(self, "recomposition_reason", str(recomposition_reason))
        object.__setattr__(
            self,
            "_source_ids",
            tuple(sorted({item.source_observation_id for item in evidence if item.source_observation_id})),
        )

    @classmethod
    def _from_parts(
        cls,
        evidence: tuple[LedgerEvidence, ...],
        *,
        cutoff: datetime,
        reason: str,
        source_ids: Iterable[str],
    ) -> "LedgerLineage":
        instance = object.__new__(cls)
        object.__setattr__(instance, "evidence", evidence)
        object.__setattr__(instance, "cutoff", cutoff)
        object.__setattr__(instance, "recomposition_reason", str(reason))
        object.__setattr__(instance, "_source_ids", tuple(sorted({str(item) for item in source_ids if str(item)})))
        return instance

    @classmethod
    def single(
        cls,
        *,
        game_id: str,
        source_observation_id: str,
        ledger_checksum: str,
        cutoff: datetime,
        reason: str,
    ) -> "LedgerLineage":
        return cls(
            (LedgerEvidence(game_id, ledger_checksum, source_observation_id),),
            cutoff=cutoff,
            recomposition_reason=reason,
        )

    @property
    def source_observation_ids(self) -> tuple[str, ...]:
        return self._source_ids

    @property
    def game_ids(self) -> tuple[str, ...]:
        return tuple(item.game_id for item in self.evidence)

    @property
    def ledger_checksums(self) -> tuple[str, ...]:
        return tuple(item.ledger_checksum for item in self.evidence)

    def merge(self, other: "LedgerLineage") -> "LedgerLineage":
        if self.cutoff != other.cutoff:
            raise ValueError("lineage cutoff mismatch")
        by_game: dict[str, LedgerEvidence] = {item.game_id: item for item in self.evidence}
        for candidate in other.evidence:
            current = by_game.get(candidate.game_id)
            if current is None:
                by_game[candidate.game_id] = candidate
            else:
                by_game[candidate.game_id] = _prefer_evidence(
                    current,
                    candidate,
                    current_reason=self.recomposition_reason,
                    candidate_reason=other.recomposition_reason,
                )
        # If both lineages contain the same game with the same checksum but
        # different source observations, preserve all source evidence while
        # retaining one deterministic keyed checksum entry.
        merged = tuple(sorted(by_game.values(), key=lambda item: item.game_id))
        return LedgerLineage._from_parts(
            merged,
            cutoff=self.cutoff,
            reason=_prefer_reason(self.recomposition_reason, other.recomposition_reason),
            source_ids=(*self.source_observation_ids, *other.source_observation_ids),
        )

    @property
    def game_set_checksum(self) -> str:
        return self.for_game_ids(self.game_ids)

    @staticmethod
    def for_game_ids(game_ids: Iterable[str]) -> str:
        return hashlib.sha256(
            json.dumps(
                sorted(set(str(game_id) for game_id in game_ids)),
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    @property
    def ledger_checksum(self) -> str:
        if len(self.evidence) == 1:
            return self.evidence[0].ledger_checksum
        return self.evidence_checksum(
            {item.game_id: item.ledger_checksum for item in self.evidence}
        )

    @staticmethod
    def evidence_checksum(evidence: Mapping[str, str]) -> str:
        keyed = {str(game_id): str(checksum) for game_id, checksum in evidence.items()}
        return hashlib.sha256(
            json.dumps(keyed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def encoded_game_ids(self) -> str:
        return json.dumps(self.game_ids, separators=(",", ":"))


def _coerce_evidence(
    value,
    *,
    game_ids: Iterable[str] | None,
    ledger_checksums: Iterable[str] | None,
) -> tuple[LedgerEvidence, ...]:
    if isinstance(value, Mapping):
        entries = []
        for game_id, item in value.items():
            if isinstance(item, Mapping):
                checksum = item.get("ledger_checksum", item.get("checksum", ""))
                source = item.get("source_observation_id", "")
            else:
                checksum, source = item, ""
            entries.append(LedgerEvidence(game_id, checksum, source))
        return tuple(sorted(_dedupe_evidence(entries), key=lambda item: item.game_id))
    values = tuple(value or ())
    if values and all(isinstance(item, LedgerEvidence) for item in values):
        return tuple(sorted(_dedupe_evidence(values), key=lambda item: item.game_id))
    selected_games = tuple(str(item) for item in (game_ids or ()))
    selected_checksums = tuple(str(item) for item in (ledger_checksums or ()))
    if len(selected_games) != len(selected_checksums):
        raise ValueError("lineage game IDs and checksums must have equal lengths")
    # The former constructor carried source IDs first and game IDs second.
    # Pairing happens only at this compatibility boundary; all later merges
    # use the immutable game key.
    source_ids = tuple(str(item) for item in values)
    entries = tuple(
        LedgerEvidence(
            game_id,
            checksum,
            source_ids[index] if index < len(source_ids) else "",
        )
        for index, (game_id, checksum) in enumerate(zip(selected_games, selected_checksums))
    )
    return tuple(sorted(_dedupe_evidence(entries), key=lambda item: item.game_id))


def _dedupe_evidence(entries: Iterable[LedgerEvidence]) -> tuple[LedgerEvidence, ...]:
    by_game: dict[str, LedgerEvidence] = {}
    for item in entries:
        current = by_game.get(item.game_id)
        if current is None or (item.ledger_checksum, item.source_observation_id) > (
            current.ledger_checksum,
            current.source_observation_id,
        ):
            by_game[item.game_id] = item
    return tuple(by_game.values())


def _reason_rank(reason: str) -> tuple[int, str]:
    normalized = str(reason).strip().lower()
    if normalized == "correction":
        return (3, normalized)
    if normalized == "initial_acceptance":
        return (2, normalized)
    if normalized:
        return (1, normalized)
    return (0, normalized)


def _prefer_reason(left: str, right: str) -> str:
    left_rank = _reason_rank(left)
    right_rank = _reason_rank(right)
    if left_rank[0] != right_rank[0]:
        return left if left_rank[0] > right_rank[0] else right
    # Equal-priority reasons are canonicalized lexicographically, which keeps
    # merge order from changing a persisted job's reason.
    return max(str(left), str(right))


def _prefer_evidence(
    left: LedgerEvidence,
    right: LedgerEvidence,
    *,
    current_reason: str,
    candidate_reason: str,
) -> LedgerEvidence:
    left_rank = _reason_rank(current_reason)[0]
    right_rank = _reason_rank(candidate_reason)[0]
    if left_rank != right_rank:
        return left if left_rank > right_rank else right
    # A correction has no sequence number in the durable contract.  Selecting
    # the greatest keyed tuple gives a stable commutative result for two
    # concurrent corrections; replaying the same evidence is a no-op.
    return max((left, right), key=lambda item: (item.ledger_checksum, item.source_observation_id))


__all__ = ["LedgerEvidence", "LedgerLineage"]
