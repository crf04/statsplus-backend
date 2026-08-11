"""Permission-gated RotoWire NBA injury-report adapter.

The adapter is deliberately only an injected provider boundary. Runtime
dependency assembly decides whether it exists; importing this module never
starts collection and the application keeps it absent by default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin

import requests

from app.errors import ProviderUnavailableError
from app.utils.telemetry import (
    PROVIDER_ROTOWIRE,
    ProviderResponseError,
    provider_call,
)


CANONICAL_INJURY_STATUSES = {
    "probable": "Probable",
    "questionable": "Questionable",
    "doubtful": "Doubtful",
    "out": "Out",
}


@dataclass(frozen=True, slots=True)
class InjuryEntryEvidence:
    """One normalized source row, before canonical athlete reconciliation."""

    entry_id: str
    source_player_id: str
    source_player_name: str
    source_team_tricode: str
    canonical_status: str | None
    raw_status: str
    reason: str
    source_url: str


@dataclass(frozen=True, slots=True)
class InjuryProviderSnapshot:
    """Raw evidence and its deterministic source normalization."""

    raw_payload: list[Mapping[str, Any]]
    entries: tuple[InjuryEntryEvidence, ...]
    retrieved_at: datetime


class RotoWireInjuryProvider:
    """Retrieve the current league-wide RotoWire injury table once."""

    ENDPOINT_URL = (
        "https://www.rotowire.com/basketball/tables/injury-report.php"
    )
    SOURCE_URL = "https://www.rotowire.com/basketball/injury-report.php"
    DEFAULT_TIMEOUT = (3.0, 8.0)

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "StatsPlus/1.0",
            }
        )

    def get_snapshot(self) -> InjuryProviderSnapshot:
        """Fetch and normalize one immutable observation."""

        try:
            with provider_call(PROVIDER_ROTOWIRE, "get_injuries") as tracker:
                response = self.session.get(
                    self.ENDPOINT_URL,
                    params={"team": "ALL", "pos": "ALL"},
                    timeout=self.timeout,
                )
                tracker.status_code = getattr(response, "status_code", None)
                response.raise_for_status()
                try:
                    payload = response.json()
                except (TypeError, ValueError) as error:
                    raise ProviderResponseError(
                        "RotoWire returned invalid JSON"
                    ) from error
                snapshot = self._parse(payload, retrieved_at=self._now_utc())
        except ProviderResponseError as error:
            raise ProviderUnavailableError(
                "The injury provider returned an invalid response.", detail=error
            ) from error
        except requests.RequestException as error:
            raise ProviderUnavailableError(
                "The injury provider could not be reached.", detail=error
            ) from error
        return snapshot

    def _now_utc(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("RotoWire clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @classmethod
    def _parse(
        cls, payload: Any, *, retrieved_at: datetime
    ) -> InjuryProviderSnapshot:
        if not isinstance(payload, list):
            raise ProviderResponseError("RotoWire payload must be a list")
        raw_rows: list[Mapping[str, Any]] = []
        entries: list[InjuryEntryEvidence] = []
        seen: set[str] = set()
        for row in payload:
            if not isinstance(row, Mapping):
                raise ProviderResponseError("RotoWire injury row must be an object")
            raw_rows.append(dict(row))
            source_player_id = cls._required_text(row, "ID", "id")
            source_player_name = cls._required_text(row, "player", "name")
            team = cls._required_text(row, "team").upper()
            raw_status = cls._required_text(row, "status")
            if raw_status.casefold() == "available":
                continue
            entry_id = f"rotowire:{source_player_id}"
            if entry_id in seen:
                raise ProviderResponseError("RotoWire entry IDs must be unique")
            seen.add(entry_id)
            player_url = cls._optional_text(
                row, "playerURL", "playerUrl", "player_url", "url"
            )
            entries.append(
                InjuryEntryEvidence(
                    entry_id=entry_id,
                    source_player_id=source_player_id,
                    source_player_name=source_player_name,
                    source_team_tricode=team,
                    canonical_status=CANONICAL_INJURY_STATUSES.get(
                        raw_status.casefold()
                    ),
                    raw_status=raw_status,
                    reason=cls._optional_text(row, "injury", "reason") or "",
                    source_url=(
                        urljoin("https://www.rotowire.com", player_url)
                        if player_url
                        else cls.SOURCE_URL
                    ),
                )
            )
        return InjuryProviderSnapshot(raw_rows, tuple(entries), retrieved_at)

    @staticmethod
    def _required_text(row: Mapping[str, Any], *keys: str) -> str:
        value = RotoWireInjuryProvider._optional_text(row, *keys)
        if value is None:
            raise ProviderResponseError(
                f"RotoWire injury row is missing {keys[0]}"
            )
        return value

    @staticmethod
    def _optional_text(row: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = row.get(key)
            if value is not None:
                text = str(value).strip()
                if text:
                    return text
        return None


__all__ = [
    "CANONICAL_INJURY_STATUSES",
    "InjuryEntryEvidence",
    "InjuryProviderSnapshot",
    "RotoWireInjuryProvider",
]
