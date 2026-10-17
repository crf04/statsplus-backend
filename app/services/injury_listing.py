"""Stored official injury report, listed by date and counted per Slate game.

Reads what the scheduled refresh (and Matchup's on-demand fetch) already
stored in ``injury_snapshots``; it never reaches the provider.  Unlike the
Matchup block, a snapshot is never hidden for being old: it is listed with
``stale`` set, so a reader can still see what the last report said.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.domain.nba_events import is_all_star_kind, is_final_event, resolve_stored_event_classification
from app.domain.nba_teams import NBA_TEAM_TRICODES, canonical_nba_team_abbreviation
from app.domain.utc import assume_utc, parse_utc_iso
from app.errors import InvalidInputError, ProviderUnavailableError, raise_if_query_timeout
from app.providers.nba_injury_report import (
    EASTERN,
    REPORT_STATUSES,
    report_time_from_name,
)
from app.services.injury_snapshot_repository import InjurySnapshotScope
from app.services.research_season import schedule_season

#: Report statuses in severity order; also the keys of a Slate game's counts.
INJURY_STATUS_ORDER = ("Out", "Doubtful", "Questionable", "Probable", "Available")
assert set(INJURY_STATUS_ORDER) == REPORT_STATUSES
#: Scheduled refreshes run every 30 minutes, so one missed run is tolerated
#: before a not-yet-started game's report is called stale.
INJURY_LIST_STALE_SECONDS = 45 * 60


def _report_time(entry: Mapping[str, Any]) -> str | None:
    """The report's header time; the file name's only for evidence stored without it."""

    header = str(entry.get("report_time") or "")
    if header:
        return header
    name = os.path.basename(str(entry.get("source_url") or ""))
    moment = report_time_from_name(name)
    return None if moment is None else moment.isoformat()


def _status(entry: Mapping[str, Any]) -> str:
    return str(entry.get("canonical_status") or entry.get("raw_status") or "")


def _player_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _minutes_fields(value: Any, minutes: Mapping[int, Any]) -> dict[str, Any]:
    found = minutes.get(_player_id(value))
    if found is None:
        return {"average_minutes": None, "games_played": 0, "minutes_season": None}
    return {
        "average_minutes": found.average_minutes,
        "games_played": found.games_played,
        "minutes_season": found.season,
    }


class StoredInjuryListing:
    """List or count the stored injury report of the games on one date."""

    def __init__(
        self,
        *,
        event_catalog: Any | None,
        snapshot_repository: Any | None,
        settings: Any,
        enabled: bool,
        clock: Callable[[], datetime] | None = None,
        minutes: Any | None = None,
    ) -> None:
        self.event_catalog = event_catalog
        self.snapshot_repository = snapshot_repository
        self.settings = settings
        self.enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self.minutes = minutes

    # -- Slate -------------------------------------------------------------

    def status_counts(
        self, events: Sequence[Mapping[str, Any]], season: str
    ) -> dict[str, dict[str, int] | None]:
        """Listed players per status for each game; ``None`` with no stored report."""

        stored = self._stored(events, season, strict=False)
        counts: dict[str, dict[str, int] | None] = {}
        for event in events:
            game_id = str(event["nba_game_id"])
            snapshot = stored.get(game_id)
            if snapshot is None:
                counts[game_id] = None
                continue
            per_status = dict.fromkeys(INJURY_STATUS_ORDER, 0)
            for entry in snapshot.normalized_entries:
                status = _status(entry)
                if status in per_status:
                    per_status[status] += 1
            counts[game_id] = per_status
        return counts

    # -- /api/injuries -----------------------------------------------------

    def list_injuries(self, requested_date: str | None, team: str | None) -> dict[str, Any]:
        slate_date = self._parse_date(requested_date)
        tricode = self._parse_team(team)
        if not self.enabled:
            raise ProviderUnavailableError("The injury report is not enabled.")
        if self.event_catalog is None or self.snapshot_repository is None:
            raise ProviderUnavailableError("The injury report is not available. Please try again later.")
        events = self._events(slate_date)
        season = schedule_season(self.settings, self.event_catalog, slate_date)
        stored = self._stored(events, season, strict=True)
        now = assume_utc(self._clock())
        minutes = self._minutes(stored, tricode)
        minutes_by_player = {} if minutes is None else minutes.by_player
        injuries: list[dict[str, Any]] = []
        for event in events:
            game_id = str(event["nba_game_id"])
            snapshot = stored.get(game_id)
            if snapshot is None:
                continue
            retrieved_at = assume_utc(snapshot.retrieved_at)
            started = parse_utc_iso(str(event["scheduled_at"])) <= now or is_final_event(event)
            stale = not started and (now - retrieved_at) > timedelta(seconds=INJURY_LIST_STALE_SECONDS)
            rows = [
                {
                    "player": str(entry["source_player_name"]),
                    "player_id": entry.get("canonical_player_id"),
                    "team": str(entry["tricode"]),
                    "game_id": game_id,
                    "status": _status(entry),
                    "reason": str(entry.get("reason") or ""),
                    "report_time": _report_time(entry),
                    "retrieved_at": retrieved_at.isoformat(),
                    "stale": stale,
                    **_minutes_fields(entry.get("canonical_player_id"), minutes_by_player),
                }
                for entry in snapshot.normalized_entries
                if tricode is None or entry.get("tricode") == tricode
            ]
            rows.sort(
                key=lambda row: (
                    row["team"],
                    INJURY_STATUS_ORDER.index(row["status"])
                    if row["status"] in INJURY_STATUS_ORDER
                    else len(INJURY_STATUS_ORDER),
                    row["player"],
                )
            )
            injuries.extend(rows)
        return {
            "date": slate_date.isoformat(),
            "team": tricode,
            "minutes_published_season": None if minutes is None else minutes.published_season,
            "injuries": injuries,
        }

    # -- shared ------------------------------------------------------------

    def _minutes(self, stored: Mapping[str, Any], tricode: str | None) -> Any | None:
        """Minutes of every listed player, read once for the whole list."""

        if self.minutes is None:
            return None
        player_ids = {
            player_id
            for snapshot in stored.values()
            for entry in snapshot.normalized_entries
            if (tricode is None or entry.get("tricode") == tricode)
            and (player_id := _player_id(entry.get("canonical_player_id"))) is not None
        }
        return self.minutes.minutes(player_ids)

    def _events(self, slate_date: date) -> list[Mapping[str, Any]]:
        return slate_events(self.event_catalog, self.settings, slate_date)

    def _stored(self, events: Sequence[Mapping[str, Any]], season: str, *, strict: bool) -> dict[str, Any]:
        """Stored snapshots by game; a failed read is never an absent report.

        A statement timeout is always the public 503.  Any other database
        failure is a 503 for the injuries list (``strict``) and degrades to "no
        report" for the Slate, which must still answer.
        """

        if not (self.enabled and events and self.snapshot_repository is not None):
            return {}
        scopes = {
            str(event["nba_game_id"]): InjurySnapshotScope(season, str(event["nba_game_id"]))
            for event in events
        }
        try:
            by_scope = self.snapshot_repository.get_many(tuple(scopes.values()))
        except (SQLAlchemyError, TypeError, ValueError) as error:
            raise_if_query_timeout(error)
            if strict:
                raise ProviderUnavailableError(
                    "The injury report is not available. Please try again later.",
                    detail=error,
                ) from error
            return {}
        return {game_id: by_scope[scope] for game_id, scope in scopes.items() if scope in by_scope}

    @staticmethod
    def _parse_date(value: str | None) -> date:
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError as error:
            raise InvalidInputError("The injuries date must use YYYY-MM-DD.", detail=error) from error
        if parsed.isoformat() != value:
            raise InvalidInputError("The injuries date must use YYYY-MM-DD.")
        return parsed

    @staticmethod
    def _parse_team(value: str | None) -> str | None:
        if value is None:
            return None
        tricode = canonical_nba_team_abbreviation(value)
        if tricode not in NBA_TEAM_TRICODES:
            raise InvalidInputError("The injuries team must be an NBA team abbreviation.")
        return tricode


def slate_events(event_catalog: Any, settings: Any, slate_date: date) -> list[Mapping[str, Any]]:
    """The stored non-All-Star events of one ET calendar date, in tip order."""

    if event_catalog is None:
        return []
    season = schedule_season(settings, event_catalog, slate_date)
    local_start = datetime.combine(slate_date, datetime.min.time(), EASTERN)
    events = event_catalog.get_events_between(
        season,
        local_start.astimezone(UTC),
        (local_start + timedelta(days=1)).astimezone(UTC),
    )
    kept = [
        event
        for event in events
        if not is_all_star_kind(
            resolve_stored_event_classification(
                str(event.get("nba_game_id", "")),
                str(event.get("classification") or ""),
            ).kind
        )
    ]
    kept.sort(key=lambda event: (str(event["scheduled_at"]), str(event["nba_game_id"])))
    return kept


__all__ = [
    "INJURY_LIST_STALE_SECONDS",
    "INJURY_STATUS_ORDER",
    "StoredInjuryListing",
    "slate_events",
]
