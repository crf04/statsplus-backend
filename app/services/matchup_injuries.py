"""Matchup injury freshness, reconciliation, and Player Pool overrides."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.domain.utc import assume_utc, parse_utc_iso
from app.domain.nba_events import is_final_event
from app.errors import ProviderUnavailableError
from app.services.athlete_resolver import normalize_athlete_name
from app.services.injury_snapshot_repository import InjurySnapshotScope
from app.utils.telemetry import (
    BoundedInjuryTelemetryRecorder,
    InjuryTelemetryEvent,
    InjuryTelemetryRecorder,
)


INJURY_SOURCE = "rotowire"
INJURY_SOURCE_URL = "https://www.rotowire.com/basketball/injury-report.php"
INJURY_REFRESH_SECONDS = 5 * 60
INJURY_STALE_SERVE_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class MatchupInjuryResult:
    block: Mapping[str, Any]
    out_player_ids: frozenset[int]
    badge_refs: Mapping[int, str]


class MatchupInjuryService:
    """Own the gated injury surface outside routes and DFS collection."""

    def __init__(
        self,
        *,
        provider: Any | None,
        snapshot_repository: Any | None,
        athlete_catalog: Any | None,
        enabled: bool,
        permission_granted: bool,
        telemetry_recorder: InjuryTelemetryRecorder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.snapshot_repository = snapshot_repository
        self.athlete_catalog = athlete_catalog
        self.enabled = enabled
        self.permission_granted = permission_granted
        self.telemetry_recorder = telemetry_recorder or BoundedInjuryTelemetryRecorder()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def get_injuries(
        self,
        *,
        event: Mapping[str, Any],
        season: str,
        pool_players: Sequence[Any],
    ) -> MatchupInjuryResult:
        if not self.enabled:
            return self._unavailable("disabled")
        if not self.permission_granted:
            return self._unavailable("permission_required")
        if (
            self.provider is None
            or self.snapshot_repository is None
            or self.athlete_catalog is None
        ):
            return self._unavailable("fetch_failed")

        scope = InjurySnapshotScope(season, str(event["nba_game_id"]))
        now = assume_utc(self.clock())
        try:
            stored = self.snapshot_repository.get(scope)
        except (SQLAlchemyError, TypeError, ValueError):
            stored = None

        tip = parse_utc_iso(str(event["scheduled_at"]))
        stopped = tip <= now or is_final_event(event)
        if stopped:
            if stored is None:
                return self._unavailable("fetch_failed")
            return self._result(
                stored.normalized_entries,
                event=event,
                status="fresh",
                retrieved_at=stored.retrieved_at,
                pool_players=pool_players,
            )

        if self._within_age(stored, now, INJURY_REFRESH_SECONDS):
            return self._result(
                stored.normalized_entries,
                event=event,
                status="fresh",
                retrieved_at=stored.retrieved_at,
                pool_players=pool_players,
            )

        try:
            snapshot = self.provider.get_snapshot()
            entries = self._reconcile(
                snapshot.entries,
                event=event,
                season=season,
            )
            self.snapshot_repository.replace(
                scope,
                raw_payload=snapshot.raw_payload,
                normalized_entries=entries,
                retrieved_at=snapshot.retrieved_at,
            )
        except (ProviderUnavailableError, SQLAlchemyError, TypeError, ValueError):
            if self._within_age(stored, now, INJURY_STALE_SERVE_SECONDS):
                return self._result(
                    stored.normalized_entries,
                    event=event,
                    status="stale",
                    retrieved_at=stored.retrieved_at,
                    pool_players=pool_players,
                )
            return self._unavailable("fetch_failed")
        return self._result(
            entries,
            event=event,
            status="fresh",
            retrieved_at=assume_utc(snapshot.retrieved_at),
            pool_players=pool_players,
        )

    @staticmethod
    def _within_age(stored: Any | None, now: datetime, maximum_seconds: int) -> bool:
        if stored is None:
            return False
        retrieved_at = assume_utc(stored.retrieved_at)
        age = (assume_utc(now) - retrieved_at).total_seconds()
        return 0 <= age <= maximum_seconds

    def _reconcile(
        self,
        evidence: Sequence[Any],
        *,
        event: Mapping[str, Any],
        season: str,
    ) -> tuple[dict[str, Any], ...]:
        team_by_tricode = self._event_teams(event)
        catalog = self.athlete_catalog.get_catalog(season, active_only=True)
        catalog_index: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for row in catalog:
            abbreviation = self._team_abbreviation(row.get("team_abbreviation"))
            if not abbreviation:
                continue
            key = (normalize_athlete_name(str(row.get("display_name") or "")), abbreviation)
            catalog_index.setdefault(key, []).append(row)

        rows: list[dict[str, Any]] = []
        for entry in evidence:
            tricode = self._team_abbreviation(entry.source_team_tricode)
            team = team_by_tricode.get(tricode)
            if team is None:
                continue
            candidates = catalog_index.get(
                (normalize_athlete_name(entry.source_player_name), tricode), []
            )
            canonical_player_id = (
                int(candidates[0]["player_id"]) if len(candidates) == 1 else None
            )
            rows.append(
                {
                    "entry_id": entry.entry_id,
                    "source_player_id": entry.source_player_id,
                    "source_player_name": entry.source_player_name,
                    "canonical_player_id": canonical_player_id,
                    "team_id": team["team_id"],
                    "tricode": tricode,
                    "canonical_status": entry.canonical_status,
                    "raw_status": entry.raw_status,
                    "reason": entry.reason,
                    "source_url": entry.source_url,
                }
            )
        return tuple(rows)

    def _result(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        event: Mapping[str, Any],
        status: str,
        retrieved_at: datetime,
        pool_players: Sequence[Any],
    ) -> MatchupInjuryResult:
        teams = self._event_teams(event)
        grouped = {team["team_id"]: [] for team in teams.values()}
        out_player_ids: set[int] = set()
        badge_refs: dict[int, str] = {}
        unmatched = 0
        for entry in entries:
            row = dict(entry)
            grouped[int(row["team_id"])].append(row)
            player_id = row.get("canonical_player_id")
            if player_id is None:
                unmatched += 1
                continue
            canonical_id = int(player_id)
            badge_refs.setdefault(canonical_id, str(row["entry_id"]))
            if row.get("canonical_status") == "Out":
                out_player_ids.add(canonical_id)
        pool_ids = {int(player.canonical_player_id) for player in pool_players}
        conflicts = len(out_player_ids.intersection(pool_ids))
        self.telemetry_recorder.record(InjuryTelemetryEvent(unmatched, conflicts))
        ordered_teams = []
        for side in ("away", "home"):
            team = next(value for value in teams.values() if value["side"] == side)
            ordered_teams.append(
                {
                    "team_id": team["team_id"],
                    "tricode": team["tricode"],
                    "submission_state": "unknown",
                    "entries": grouped[team["team_id"]],
                }
            )
        return MatchupInjuryResult(
            block={
                "status": status,
                "unavailable_reason": None,
                "retrieved_at": retrieved_at.isoformat(),
                "source": INJURY_SOURCE,
                "source_url": INJURY_SOURCE_URL,
                "teams": ordered_teams,
            },
            out_player_ids=frozenset(out_player_ids),
            badge_refs=badge_refs,
        )

    @staticmethod
    def _event_teams(event: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        result = {}
        for side in ("away", "home"):
            nested = event.get(f"{side}_team")
            team_id = event.get(f"{side}_team_id")
            tricode = event.get(f"{side}_team_tricode")
            if isinstance(nested, Mapping):
                team_id = nested.get("id", team_id)
                tricode = nested.get("tricode", tricode)
            normalized = MatchupInjuryService._team_abbreviation(tricode)
            if team_id is None or not normalized:
                raise ValueError("matchup injury team identity is incomplete")
            result[normalized] = {
                "team_id": int(team_id),
                "tricode": normalized,
                "side": side,
            }
        return result

    @staticmethod
    def _team_abbreviation(value: Any) -> str:
        text = str(value or "").strip().upper()
        return {"PHO": "PHX", "NO": "NOP"}.get(text, text)

    @staticmethod
    def _unavailable(reason: str) -> MatchupInjuryResult:
        return MatchupInjuryResult(
            block={
                "status": "unavailable",
                "unavailable_reason": reason,
                "retrieved_at": None,
                "source": INJURY_SOURCE,
                "source_url": INJURY_SOURCE_URL,
                "teams": [],
            },
            out_player_ids=frozenset(),
            badge_refs={},
        )


__all__ = [
    "INJURY_SOURCE",
    "INJURY_SOURCE_URL",
    "INJURY_REFRESH_SECONDS",
    "INJURY_STALE_SERVE_SECONDS",
    "MatchupInjuryResult",
    "MatchupInjuryService",
]
