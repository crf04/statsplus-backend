"""Matchup injury freshness, reconciliation, and Player Pool overrides."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_teams import (
    NBA_TEAM_TRICODES,
    canonical_nba_team_abbreviation,
)
from app.domain.utc import assume_utc, parse_utc_iso
from app.domain.nba_events import is_final_event
from app.errors import ProviderUnavailableError
from app.providers.nba_injury_report import InjuryEntryEvidence, SOURCE_URL
from app.services.athlete_resolver import normalize_athlete_name
from app.services.injury_snapshot_repository import InjurySnapshotScope
from app.utils.telemetry import (
    BoundedInjuryTelemetryRecorder,
    InjuryTelemetryEvent,
    InjuryTelemetryRecorder,
)


INJURY_SOURCE = "nba_official"
INJURY_SOURCE_URL = SOURCE_URL
_GENERATIONAL_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})
INJURY_REFRESH_SECONDS = 5 * 60
INJURY_STALE_SERVE_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class MatchupInjuryResult:
    block: Mapping[str, Any]
    out_player_ids: frozenset[int]
    badge_refs: Mapping[int, str]


def unavailable_injury_result(reason: str) -> MatchupInjuryResult:
    """Build the one closed present-but-unavailable injury payload."""

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


class AthleteNameIndex:
    """Season Athlete Catalog rows keyed for report-name reconciliation.

    The report prints a player's name and his team that day.  Exact name plus
    team decides first (the existing contract).  Two reviewed fallbacks follow,
    each only when it names exactly one catalog athlete:

    * the name with generational suffixes (Jr., III) removed, still with the
      team -- the report and the catalog disagree on ``Gary Trent`` /
      ``Gary Trent Jr.`` and ``GG Jackson`` / ``G.G. Jackson II``;
    * the exact or suffix-free name alone, because the catalog holds a
      player's team as of its last refresh and a traded player is on his new
      team in the report before then.

    Anything ambiguous or unknown stays unmatched (``None``), never guessed.
    """

    def __init__(self, catalog: Sequence[Mapping[str, Any]]) -> None:
        self._exact_team: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        self._loose_team: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        self._exact: dict[str, list[Mapping[str, Any]]] = {}
        self._loose: dict[str, list[Mapping[str, Any]]] = {}
        for row in catalog:
            name = str(row.get("display_name") or "")
            exact, loose = self._keys(name)
            self._exact.setdefault(exact, []).append(row)
            self._loose.setdefault(loose, []).append(row)
            team = canonical_nba_team_abbreviation(row.get("team_abbreviation"))
            if team:
                self._exact_team.setdefault((exact, team), []).append(row)
                self._loose_team.setdefault((loose, team), []).append(row)

    @staticmethod
    def _keys(name: str) -> tuple[str, str]:
        exact = normalize_athlete_name(name)
        words = [
            word
            for word in name.replace(",", " ").split()
            if normalize_athlete_name(word) not in _GENERATIONAL_SUFFIXES
        ]
        return exact, normalize_athlete_name(" ".join(words))

    def resolve(self, name: str, tricode: str) -> int | None:
        exact, loose = self._keys(name)
        team = canonical_nba_team_abbreviation(tricode)
        for index, key in (
            (self._exact_team, (exact, team)),
            (self._loose_team, (loose, team)),
            (self._exact, exact),
            (self._loose, loose),
        ):
            candidates = index.get(key, [])
            if len(candidates) == 1:
                return int(candidates[0]["player_id"])
            if candidates:
                return None  # namesakes: never pick one
        return None


def reconcile_injury_entries(
    evidence: Sequence[Any],
    *,
    teams: Mapping[str, Mapping[str, Any]],
    catalog: AthleteNameIndex,
) -> tuple[tuple[dict[str, Any], ...], int]:
    """Resolve source rows to canonical athletes for one game's two teams.

    Returns the reconciled rows of ``teams`` and the count of rows whose team
    is not an NBA tricode at all.  A row naming an athlete the catalog cannot
    place keeps ``canonical_player_id = None`` (it is reported, not dropped).
    """

    rows: list[dict[str, Any]] = []
    unresolved_team_entries = 0
    for entry in evidence:
        tricode = canonical_nba_team_abbreviation(entry.source_team_tricode)
        if tricode not in NBA_TEAM_TRICODES:
            unresolved_team_entries += 1
            continue
        team = teams.get(tricode)
        if team is None:
            continue
        rows.append(
            {
                "entry_id": entry.entry_id,
                "source_player_id": entry.source_player_id,
                "source_player_name": entry.source_player_name,
                "canonical_player_id": catalog.resolve(
                    entry.source_player_name, tricode
                ),
                "team_id": team["team_id"],
                "tricode": tricode,
                "canonical_status": entry.canonical_status,
                "raw_status": entry.raw_status,
                "reason": entry.reason,
                "source_url": entry.source_url,
            }
        )
    return tuple(rows), unresolved_team_entries



class MatchupInjuryService:
    """Own the gated injury surface outside routes and DFS collection."""

    def __init__(
        self,
        *,
        provider: Any | None,
        snapshot_repository: Any | None,
        athlete_catalog: Any | None,
        enabled: bool,
        permission_granted: bool = True,
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
        self._refresh_lock = Lock()

    def get_injuries(
        self,
        *,
        event: Mapping[str, Any],
        season: str,
        pool_players: Sequence[Any],
    ) -> MatchupInjuryResult:
        if not self.enabled:
            return unavailable_injury_result("disabled")
        if not self.permission_granted:
            return unavailable_injury_result("permission_required")
        if (
            self.provider is None
            or self.snapshot_repository is None
            or self.athlete_catalog is None
        ):
            return unavailable_injury_result("fetch_failed")

        scope = InjurySnapshotScope(season, str(event["nba_game_id"]))
        now = assume_utc(self.clock())
        try:
            stored = self.snapshot_repository.get(scope)
        except (SQLAlchemyError, TypeError, ValueError):
            stored = None

        tip = parse_utc_iso(str(event["scheduled_at"]))
        stopped = tip <= now or is_final_event(event)
        if stopped:
            return self._stored_result(
                stored,
                now=now,
                event=event,
                pool_players=pool_players,
            )

        if self._within_age(stored, now, INJURY_REFRESH_SECONDS):
            return self._result(
                stored.normalized_entries,
                event=event,
                status="fresh",
                retrieved_at=stored.retrieved_at,
                pool_players=pool_players,
                unresolved_team_entries=stored.unresolved_team_entry_count,
            )

        # This single-flight is intentionally scoped to one service instance
        # (one process worker). Durable source reuse handles later workers.
        with self._refresh_lock:
            now = assume_utc(self.clock())
            try:
                current = self.snapshot_repository.get(scope)
            except (SQLAlchemyError, TypeError, ValueError):
                current = None
            if current is not None:
                stored = current
            if self._within_age(current, now, INJURY_REFRESH_SECONDS):
                return self._result(
                    current.normalized_entries,
                    event=event,
                    status="fresh",
                    retrieved_at=current.retrieved_at,
                    pool_players=pool_players,
                    unresolved_team_entries=current.unresolved_team_entry_count,
                )

            try:
                source_snapshot = self.snapshot_repository.get_latest_source(
                    INJURY_SOURCE
                )
                if self._within_age(source_snapshot, now, INJURY_REFRESH_SECONDS):
                    entries, unresolved_team_entries = self._reconcile(
                        tuple(
                            InjuryEntryEvidence.from_document(entry)
                            for entry in source_snapshot.normalized_entries
                        ),
                        event=event,
                        season=season,
                    )
                    self.snapshot_repository.replace_from_source(
                        scope,
                        source_snapshot=source_snapshot,
                        normalized_entries=entries,
                        unresolved_team_entry_count=unresolved_team_entries,
                    )
                    return self._result(
                        entries,
                        event=event,
                        status="fresh",
                        retrieved_at=source_snapshot.retrieved_at,
                        pool_players=pool_players,
                        unresolved_team_entries=unresolved_team_entries,
                    )
                snapshot = self.provider.get_snapshot()
                entries, unresolved_team_entries = self._reconcile(
                    snapshot.entries,
                    event=event,
                    season=season,
                )
                self.snapshot_repository.publish(
                    scope,
                    source=INJURY_SOURCE,
                    raw_payload=snapshot.raw_payload,
                    source_entries=tuple(
                        entry.to_document() for entry in snapshot.entries
                    ),
                    normalized_entries=entries,
                    retrieved_at=snapshot.retrieved_at,
                    unresolved_team_entry_count=unresolved_team_entries,
                )
            except (ProviderUnavailableError, SQLAlchemyError, TypeError, ValueError):
                if self._within_age(stored, now, INJURY_STALE_SERVE_SECONDS):
                    return self._result(
                        stored.normalized_entries,
                        event=event,
                        status="stale",
                        retrieved_at=stored.retrieved_at,
                        pool_players=pool_players,
                        unresolved_team_entries=stored.unresolved_team_entry_count,
                    )
                return unavailable_injury_result("fetch_failed")
            return self._result(
                entries,
                event=event,
                status="fresh",
                retrieved_at=assume_utc(snapshot.retrieved_at),
                pool_players=pool_players,
                unresolved_team_entries=unresolved_team_entries,
            )

    def get_stored_injuries(
        self,
        *,
        event: Mapping[str, Any],
        season: str,
        pool_players: Sequence[Any],
    ) -> MatchupInjuryResult:
        """Read a usable override without ever starting provider collection."""

        if not self.enabled:
            return unavailable_injury_result("disabled")
        if not self.permission_granted:
            return unavailable_injury_result("permission_required")
        if self.snapshot_repository is None:
            return unavailable_injury_result("fetch_failed")
        try:
            stored = self.snapshot_repository.get(
                InjurySnapshotScope(season, str(event["nba_game_id"]))
            )
        except (SQLAlchemyError, TypeError, ValueError):
            stored = None
        return self._stored_result(
            stored,
            now=assume_utc(self.clock()),
            event=event,
            pool_players=pool_players,
        )

    def get_stored_injuries_many(
        self,
        *,
        events: Sequence[Mapping[str, Any]],
        season: str,
        pool_players_by_game: Mapping[str, Sequence[Any]],
    ) -> dict[str, MatchupInjuryResult]:
        """Read every event's stored override with one batched statement.

        Same gated, stored-only contract as ``get_stored_injuries`` called
        once per event -- never starts provider collection -- except every
        event's snapshot comes from one
        ``InjurySnapshotRepository.get_many`` statement instead of N.  The
        Slate is the caller: it asks this question once per event on the page.
        """

        if not self.enabled:
            return {
                str(event["nba_game_id"]): unavailable_injury_result("disabled")
                for event in events
            }
        if not self.permission_granted:
            return {
                str(event["nba_game_id"]): unavailable_injury_result(
                    "permission_required"
                )
                for event in events
            }
        if self.snapshot_repository is None:
            return {
                str(event["nba_game_id"]): unavailable_injury_result("fetch_failed")
                for event in events
            }
        scopes_by_game = {
            str(event["nba_game_id"]): InjurySnapshotScope(
                season, str(event["nba_game_id"])
            )
            for event in events
        }
        try:
            stored_by_scope = self.snapshot_repository.get_many(
                tuple(scopes_by_game.values())
            )
        except (SQLAlchemyError, TypeError, ValueError):
            stored_by_scope = {}
        now = assume_utc(self.clock())
        results: dict[str, MatchupInjuryResult] = {}
        for event in events:
            game_id = str(event["nba_game_id"])
            stored = stored_by_scope.get(scopes_by_game[game_id])
            results[game_id] = self._stored_result(
                stored,
                now=now,
                event=event,
                pool_players=pool_players_by_game.get(game_id, ()),
            )
        return results

    def _stored_result(
        self,
        stored: Any | None,
        *,
        now: datetime,
        event: Mapping[str, Any],
        pool_players: Sequence[Any],
    ) -> MatchupInjuryResult:
        if self._within_age(stored, now, INJURY_REFRESH_SECONDS):
            status = "fresh"
        elif self._within_age(stored, now, INJURY_STALE_SERVE_SECONDS):
            status = "stale"
        else:
            return unavailable_injury_result("fetch_failed")
        return self._result(
            stored.normalized_entries,
            event=event,
            status=status,
            retrieved_at=stored.retrieved_at,
            pool_players=pool_players,
            unresolved_team_entries=stored.unresolved_team_entry_count,
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
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        catalog = self.athlete_catalog.get_catalog(season, active_only=True)
        return reconcile_injury_entries(
            evidence,
            teams=self._event_teams(event),
            catalog=AthleteNameIndex(catalog),
        )

    def _result(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        event: Mapping[str, Any],
        status: str,
        retrieved_at: datetime,
        pool_players: Sequence[Any],
        unresolved_team_entries: int = 0,
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
        self.telemetry_recorder.record(
            InjuryTelemetryEvent(unmatched, unresolved_team_entries, conflicts)
        )
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
        return canonical_nba_team_abbreviation(value)

class StoredMatchupInjuryReader:
    """Serve the injury surface from what is stored, never refreshing it.

    ``MatchupInjuryService.get_injuries`` may reach the provider and publish a
    snapshot when the stored override is stale or missing; that is the Matchup
    route's contract.  A read that promises no provider call and no write --
    a Target preview -- takes this reader instead, which answers the same
    ``get_injuries`` question through ``get_stored_injuries`` alone.
    """

    def __init__(self, service: MatchupInjuryService) -> None:
        self.service = service

    def get_injuries(
        self,
        *,
        event: Mapping[str, Any],
        season: str,
        pool_players: Sequence[Any],
    ) -> MatchupInjuryResult:
        return self.service.get_stored_injuries(
            event=event, season=season, pool_players=pool_players
        )


__all__ = [
    "INJURY_SOURCE",
    "INJURY_SOURCE_URL",
    "INJURY_REFRESH_SECONDS",
    "INJURY_STALE_SERVE_SECONDS",
    "AthleteNameIndex",
    "MatchupInjuryResult",
    "MatchupInjuryService",
    "StoredMatchupInjuryReader",
    "reconcile_injury_entries",
    "unavailable_injury_result",
]
