"""Immutable correction lineage shared by queue and materialization boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True, slots=True)
class LedgerLineage:
    source_observation_ids: tuple[str, ...]
    game_ids: tuple[str, ...]
    ledger_checksums: tuple[str, ...]
    cutoff: datetime
    recomposition_reason: str

    @classmethod
    def single(cls, *, game_id: str, source_observation_id: str, ledger_checksum: str,
               cutoff: datetime, reason: str) -> "LedgerLineage":
        return cls((str(source_observation_id),), (str(game_id),), (str(ledger_checksum),), cutoff, reason)

    def merge(self, other: "LedgerLineage") -> "LedgerLineage":
        if self.cutoff != other.cutoff:
            raise ValueError("lineage cutoff mismatch")
        checksums = (
            (other.ledger_checksums[0],)
            if "correction" == other.recomposition_reason
            and set(self.game_ids) & set(other.game_ids)
            else tuple(sorted(set(self.ledger_checksums) | set(other.ledger_checksums)))
        )
        return LedgerLineage(
            tuple(sorted(set(self.source_observation_ids) | set(other.source_observation_ids))),
            tuple(sorted(set(self.game_ids) | set(other.game_ids))),
            checksums,
            self.cutoff,
            "correction" if "correction" in {self.recomposition_reason, other.recomposition_reason}
            else self.recomposition_reason,
        )

    @property
    def game_set_checksum(self) -> str:
        return self.for_game_ids(self.game_ids)

    @staticmethod
    def for_game_ids(game_ids: Iterable[str]) -> str:
        return hashlib.sha256(
            json.dumps(sorted(set(str(game_id) for game_id in game_ids)), separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def ledger_checksum(self) -> str:
        if len(self.ledger_checksums) == 1:
            return self.ledger_checksums[0]
        return hashlib.sha256("\n".join(self.ledger_checksums).encode()).hexdigest()

    def encoded_game_ids(self) -> str:
        return json.dumps(self.game_ids, separators=(",", ":"))
