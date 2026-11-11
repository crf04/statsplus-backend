"""Stored official injury report, listed by date, by player, and counted per Slate game.

Reads what the scheduled refresh already
stored in ``injury_snapshots``; it never reaches the provider.  Unlike the
Matchup block, a snapshot is never hidden for being old: it is listed with
``stale`` set, so a reader can still see what the last report said.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import current_nba_season
from app.domain.nba_events import is_all_star_kind, is_final_event, resolve_stored_event_classification
from app.domain.nba_teams import NBA_TEAM_TRICODES, canonical_nba_team_abbreviation
from app.domain.slate_time import slate_date_for_instant, slate_day_bounds_utc
from app.domain.utc import assume_utc, parse_utc_iso
from app.errors import InvalidInputError, ProviderUnavailableError, raise_if_query_timeout
from app.providers.nba_injury_report import (
    REPORT_STATUSES,
    report_time_from_name,
)
from app.services.athlete_resolver import CanonicalAthlete, catalog_athlete_matches
from app.services.injury_snapshot_repository import InjurySnapshotScope
from app.services.publication_snapshot_calls import call_with_read_scope
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


def report_status(entry: Mapping[str, Any]) -> str:
    return str(entry.get("canonical_status") or entry.get("raw_status") or "")


def _report_moment(entry: Mapping[str, Any]) -> datetime:
    try:
        return assume_utc(datetime.fromisoformat(str(_report_time(entry))))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def listed_entry(snapshot: Any, player_id: int, team: str, game_date: str) -> Mapping[str, Any] | None:
    """His row for ``team`` on one game's stored report, or ``None`` when it does not list him.

    A row the report printed for another day (the same teams' next game) does
    not describe this game; a row stored before rows carried a date does.  Of
    two rows for him, the later report time decides.  A row whose name matched
    no catalog player (``canonical_player_id`` null) is never taken for him.
    """

    rows = [
        entry
        for entry in snapshot.normalized_entries
        if _player_id(entry.get("canonical_player_id")) == player_id
        and entry.get("tricode") == team
        and str(entry.get("game_date") or game_date) == game_date
    ]
    return max(rows, key=_report_moment, default=None)


def _player_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _minutes_fields(value: Any, minutes: Any | None) -> dict[str, Any]:
    player_id = _player_id(value)
    current = None if minutes is None else minutes.current.get(player_id)
    prior = None if minutes is None else minutes.prior.get(player_id)
    return {**_season_fields("", current), **_season_fields("prior_", prior)}


def _season_fields(prefix: str, found: Any | None) -> dict[str, Any]:
    if found is None:
        return {f"{prefix}average_minutes": None, f"{prefix}games_played": 0, f"{prefix}minutes_season": None}
    return {
        f"{prefix}average_minutes": found.average_minutes,
        f"{prefix}games_played": found.games_played,
        f"{prefix}minutes_season": found.season,
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
        athlete_catalog: Any | None = None,
    ) -> None:
        self.event_catalog = event_catalog
        self.snapshot_repository = snapshot_repository
        #: Resolves the ``player`` of a status history.
        self.athlete_catalog = athlete_catalog
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
                status = report_status(entry)
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
        minutes = self._minutes(stored, tricode, season)
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
                    "status": report_status(entry),
                    "reason": str(entry.get("reason") or ""),
                    "report_time": _report_time(entry),
                    "retrieved_at": retrieved_at.isoformat(),
                    "stale": stale,
                    **_minutes_fields(entry.get("canonical_player_id"), minutes),
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
            "minutes_season": None if minutes is None else minutes.season,
            "minutes_published_season": None if minutes is None else minutes.published_season,
            "injuries": injuries,
        }

    # -- /api/injuries?player= ----------------------------------------------

    def player_history(self, player: str, from_date: str | None, to_date: str | None) -> dict[str, Any]:
        """One player's stored statuses by game, oldest first, over one season's date range.

        A game whose stored report does not list him is not in ``statuses``;
        ``report_coverage`` says, per team he is listed for in the range or the
        Athlete Catalog places him on, how many of its games had a report.
        """

        first = self._parse_range_date(from_date, "from_date")
        if to_date is None:
            today = slate_date_for_instant(assume_utc(self._clock()))
            last = min(today, date(int(current_nba_season(first)[:4]) + 1, 9, 30))
            if last < first:
                raise InvalidInputError(
                    "The injuries from_date is after today; pass a to_date.",
                    public_details={"parameter": "from_date", "parameters": ["from_date"]},
                )
        else:
            last = self._parse_range_date(to_date, "to_date")
            if last < first or current_nba_season(last) != current_nba_season(first):
                raise InvalidInputError(
                    "The injuries to_date must be on or after from_date and in the same season.",
                    public_details={"parameter": "to_date", "parameters": ["to_date"]},
                )
        if not self.enabled:
            raise ProviderUnavailableError("The injury report is not enabled.")
        if self.event_catalog is None or self.snapshot_repository is None or self.athlete_catalog is None:
            raise ProviderUnavailableError("The injury report is not available. Please try again later.")
        season = schedule_season(self.settings, self.event_catalog, first)
        athlete = self._athlete(player, season)
        events = range_events(self.event_catalog, season, first, last)
        by_game = {str(event["nba_game_id"]): event for event in events}
        mentions = self._read(self.snapshot_repository.get_season_mentions, season, [athlete.player_id])
        statuses = []
        for scope, snapshot in mentions.items():
            event = by_game.get(scope.game_id)
            if event is None:
                continue
            game_date = _eastern_date(event)
            for team in _event_tricodes(event):
                entry = listed_entry(snapshot, athlete.player_id, team, game_date)
                if entry is not None:
                    statuses.append(
                        {
                            "game_date": game_date,
                            "game_id": scope.game_id,
                            "matchup": _matchup(event, team),
                            "team": team,
                            "status": report_status(entry),
                            "reason": str(entry.get("reason") or ""),
                            "report_time": _report_time(entry),
                        }
                    )
        statuses.sort(key=lambda row: (row["game_date"], str(by_game[row["game_id"]]["scheduled_at"])))
        catalog_team = canonical_nba_team_abbreviation(athlete.team_abbreviation)
        teams = sorted({row["team"] for row in statuses} | ({catalog_team} & NBA_TEAM_TRICODES))
        team_games = {
            team: [
                InjurySnapshotScope(season, game_id)
                for game_id, event in by_game.items()
                if team in _event_tricodes(event)
            ]
            for team in teams
        }
        reported = self._read(
            self.snapshot_repository.stored_scopes,
            [scope for scopes in team_games.values() for scope in scopes],
        )
        return {
            "player": {
                "player_id": athlete.player_id,
                "name": athlete.display_name,
                "team": athlete.team_abbreviation,
            },
            "season": season,
            "from_date": first.isoformat(),
            "to_date": last.isoformat(),
            "report_coverage": [
                {
                    "team": team,
                    "team_games": len(scopes),
                    "games_with_report": sum(scope in reported for scope in scopes),
                }
                for team, scopes in team_games.items()
            ],
            "statuses": statuses,
        }

    def _athlete(self, player: str, season: str) -> CanonicalAthlete:
        """The season's catalog athlete ``player`` names, by player id or full name."""

        wanted = str(player).strip()
        catalog = self.athlete_catalog.get_catalog(season, active_only=False)
        if wanted.isdigit():
            matches = [CanonicalAthlete.from_row(row) for row in catalog if str(row["player_id"]) == wanted]
        else:
            matches = list(catalog_athlete_matches(catalog, wanted))
        if not matches:
            raise InvalidInputError(
                "The injuries player is not in the season's Athlete Catalog.",
                public_details={"parameter": "player", "parameters": ["player"]},
            )
        if len(matches) > 1:
            raise InvalidInputError(
                "The injuries player matches more than one player; name him by player_id.",
                public_details={
                    "parameter": "player",
                    "parameters": ["player"],
                    "candidates": [
                        {
                            "player_id": athlete.player_id,
                            "name": athlete.display_name,
                            "team": athlete.team_abbreviation,
                        }
                        for athlete in sorted(matches, key=lambda athlete: athlete.player_id)
                    ],
                },
            )
        return matches[0]

    @staticmethod
    def _read(method: Callable[..., Any], *args: Any) -> Any:
        try:
            return method(*args)
        except (SQLAlchemyError, TypeError, ValueError) as error:
            raise_if_query_timeout(error)
            raise ProviderUnavailableError(
                "The injury report is not available. Please try again later.",
                detail=error,
            ) from error

    # -- shared ------------------------------------------------------------

    def _minutes(self, stored: Mapping[str, Any], tricode: str | None, season: str) -> Any | None:
        """Minutes of every listed player in ``season`` and the one before, read once for the list."""

        if self.minutes is None:
            return None
        player_ids = {
            player_id
            for snapshot in stored.values()
            for entry in snapshot.normalized_entries
            if (tricode is None or entry.get("tricode") == tricode)
            and (player_id := _player_id(entry.get("canonical_player_id"))) is not None
        }
        return self.minutes.minutes(player_ids, season)

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
    def _parse_range_date(value: str | None, parameter: str) -> date:
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            parsed = None
        if parsed is None or parsed.isoformat() != value:
            raise InvalidInputError(
                f"The injuries {parameter} must use YYYY-MM-DD.",
                public_details={"parameter": parameter, "parameters": [parameter]},
            )
        return parsed

    @staticmethod
    def _parse_team(value: str | None) -> str | None:
        if value is None:
            return None
        tricode = canonical_nba_team_abbreviation(value)
        if tricode not in NBA_TEAM_TRICODES:
            raise InvalidInputError("The injuries team must be an NBA team abbreviation.")
        return tricode


def slate_events(
    event_catalog: Any,
    settings: Any,
    slate_date: date,
    season: str | None = None,
    *,
    connection: Connection | None = None,
) -> list[Mapping[str, Any]]:
    """The stored non-All-Star events of one ET calendar date, in tip order.

    ``season`` is the date's schedule season when the caller already has it,
    and ``connection`` the caller's read scope.
    """

    if event_catalog is None:
        return []
    season = season or schedule_season(settings, event_catalog, slate_date)
    return range_events(event_catalog, season, slate_date, slate_date, connection=connection)


def range_events(
    event_catalog: Any,
    season: str,
    first: date,
    last: date,
    *,
    connection: Connection | None = None,
) -> list[Mapping[str, Any]]:
    """The season's stored non-All-Star events from ET date ``first`` through ``last``, in tip order."""

    events = call_with_read_scope(
        event_catalog.get_events_between,
        season,
        slate_day_bounds_utc(first)[0],
        slate_day_bounds_utc(last)[1],
        connection=connection,
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


def _eastern_date(event: Mapping[str, Any]) -> str:
    return slate_date_for_instant(parse_utc_iso(str(event["scheduled_at"]))).isoformat()


def _event_tricodes(event: Mapping[str, Any]) -> tuple[str, str]:
    return (
        canonical_nba_team_abbreviation(event["away_team"]["tricode"]),
        canonical_nba_team_abbreviation(event["home_team"]["tricode"]),
    )


def _matchup(event: Mapping[str, Any], team: str) -> str:
    """``LAC @ DEN`` for an away game, ``LAC vs. DEN`` for a home game, as the rotation prints it."""

    away, home = _event_tricodes(event)
    return f"{team} vs. {away}" if team == home else f"{team} @ {home}"


__all__ = [
    "INJURY_LIST_STALE_SECONDS",
    "INJURY_STATUS_ORDER",
    "StoredInjuryListing",
    "listed_entry",
    "range_events",
    "report_status",
    "slate_events",
]
