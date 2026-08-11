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
from urllib.parse import urljoin, urlsplit

import requests

from app.config.settings import RuntimeSettings, get_runtime_settings
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

    def to_document(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "source_player_id": self.source_player_id,
            "source_player_name": self.source_player_name,
            "source_team_tricode": self.source_team_tricode,
            "canonical_status": self.canonical_status,
            "raw_status": self.raw_status,
            "reason": self.reason,
            "source_url": self.source_url,
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "InjuryEntryEvidence":
        canonical_status = (
            None
            if value.get("canonical_status") is None
            else str(value["canonical_status"])
        )
        if canonical_status not in {None, *CANONICAL_INJURY_STATUSES.values()}:
            raise ValueError("stored RotoWire injury status is not canonical")
        return cls(
            entry_id=str(value["entry_id"]),
            source_player_id=str(value["source_player_id"]),
            source_player_name=str(value["source_player_name"]),
            source_team_tricode=str(value["source_team_tricode"]),
            canonical_status=canonical_status,
            raw_status=str(value["raw_status"]),
            reason=str(value.get("reason") or ""),
            source_url=str(value["source_url"]),
        )


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

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        settings: RuntimeSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_runtime_settings()
        self.session = session or requests.Session()
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
                    timeout=(
                        self.settings.providers.rotowire_connect_timeout_seconds,
                        self.settings.providers.rotowire_read_timeout_seconds,
                    ),
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
                    source_url=cls._safe_source_url(player_url),
                )
            )
        return InjuryProviderSnapshot(raw_rows, tuple(entries), retrieved_at)

    @classmethod
    def _safe_source_url(cls, player_url: str | None) -> str:
        if player_url is None:
            return cls.SOURCE_URL
        candidate = urljoin("https://www.rotowire.com", player_url)
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError:
            return cls.SOURCE_URL
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme.casefold() != "https"
            or not (
                hostname == "rotowire.com" or hostname.endswith(".rotowire.com")
            )
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
        ):
            return cls.SOURCE_URL
        return candidate

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
