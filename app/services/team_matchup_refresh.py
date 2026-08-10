"""Collect exact team-window matchup facts for durable publication."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.domain.nba_events import (
    NBAGameStatus,
    is_all_star_kind,
    is_postponed_event,
    is_preseason_kind,
    resolve_stored_event_classification,
)
from app.domain.utc import assume_utc, parse_utc_iso
from app.models.catalogs import PLAY_TYPES, SHOOTING_TYPES
from app.providers.nba_stats import validate_canonical_season
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class TeamWindowBoundary:
    """The exact governed games and provider date bounds for one team."""

    team_id: int
    from_date: date
    to_date: date
    game_ids: tuple[str, ...]
    season_type: str | None


class TeamWindowBoundaryResolver:
    """Resolve rolling windows from the canonical governed schedule."""

    def __init__(self, event_catalog: Any) -> None:
        self.event_catalog = event_catalog

    def last_n(
        self, season: str, *, as_of: date, window_games: int
    ) -> dict[int, TeamWindowBoundary]:
        if (
            not isinstance(window_games, int)
            or isinstance(window_games, bool)
            or window_games < 1
        ):
            raise ValueError("window_games must be a positive integer")

        games_by_team: dict[int, list[tuple[datetime, str, str]]] = defaultdict(list)
        for event in self.event_catalog.get_events(season):
            if not self._is_governed_completion(event, as_of=as_of):
                continue
            scheduled_at = self._scheduled_at(event)
            game_id = str(event["nba_game_id"])
            season_type = self._governed_season_type(event)
            if season_type is None:  # covered by _is_governed_completion
                continue
            for field in ("home_team_id", "away_team_id"):
                games_by_team[int(event[field])].append(
                    (scheduled_at, game_id, season_type)
                )

        boundaries: dict[int, TeamWindowBoundary] = {}
        for team_id, games in games_by_team.items():
            selected = sorted(games, reverse=True)[:window_games]
            if len(selected) != window_games:
                continue
            boundaries[team_id] = TeamWindowBoundary(
                team_id=team_id,
                from_date=selected[-1][0].astimezone(EASTERN).date(),
                to_date=as_of,
                game_ids=tuple(game_id for _, game_id, _ in selected),
                season_type=(
                    selected[0][2]
                    if len({season_type for _, _, season_type in selected}) == 1
                    else None
                ),
            )
        return boundaries

    @staticmethod
    def _scheduled_at(event: dict[str, Any]) -> datetime:
        value = event["scheduled_at"]
        if isinstance(value, datetime):
            return assume_utc(value)
        return parse_utc_iso(str(value))

    @classmethod
    def _is_governed_completion(cls, event: dict[str, Any], *, as_of: date) -> bool:
        if not cls._is_governed_event(event):
            return False
        if is_postponed_event(event):
            return False
        is_final = event.get("status_code") == NBAGameStatus.FINAL or str(
            event.get("status_text") or ""
        ).casefold().startswith("final")
        if not is_final:
            return False
        return cls._scheduled_at(event).astimezone(EASTERN).date() <= as_of

    @staticmethod
    def _is_governed_event(event: dict[str, Any]) -> bool:
        return TeamWindowBoundaryResolver._governed_season_type(event) is not None

    @staticmethod
    def _governed_season_type(event: dict[str, Any]) -> str | None:
        game_id = str(event.get("nba_game_id") or "")
        classification = resolve_stored_event_classification(
            game_id, str(event.get("classification") or "")
        )
        if (
            is_all_star_kind(classification.kind)
            or is_all_star_kind(classification.display)
            or is_preseason_kind(classification.kind)
            or is_preseason_kind(classification.display)
        ):
            return None
        if classification.kind in {"Regular Season", "Playoffs"}:
            return classification.kind
        return None


_TRADITIONAL_STATS = {
    "OPP_TOV": ("OPP_TOV", "TOV"),
    "OPP_STL": ("OPP_STL", "STL"),
    "OPP_BLK": ("OPP_BLK", "BLK"),
}
_SHOT_STATS = ("FG2M", "FG2A", "FG3M", "FG3A")
_ASSIST_STATS = (
    "Assists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
MATCHUP_SURFACES = (
    "assist_locations",
    "play_types",
    "shot_types",
    "shot_zones",
    "traditional",
)


class _ProviderWindowUnverified(ValueError):
    """An aggregate response cannot prove it represents the governed window."""


class TeamMatchupRefreshService:
    """Collect Season and exact team Last-15 facts, then publish together."""

    def __init__(
        self,
        repository: TeamMatchupRepository,
        event_catalog: Any,
        nba_stats_provider: Any,
        pbp_stats_provider: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.event_catalog = event_catalog
        self.nba_stats = nba_stats_provider
        self.pbp_stats = pbp_stats_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.boundaries = TeamWindowBoundaryResolver(event_catalog)

    def refresh(self, season: str, *, as_of: date | None = None) -> None:
        canonical_season = validate_canonical_season(season)
        retrieved_at = assume_utc(self._clock())
        snapshot_date = as_of or retrieved_at.astimezone(EASTERN).date()
        events = self.event_catalog.get_events(canonical_season)
        team_ids = self._team_ids(events)
        if len(team_ids) != 30:
            raise ValueError(
                "team matchup refresh requires the governed 30-team league"
            )
        boundaries = self.boundaries.last_n(
            canonical_season, as_of=snapshot_date, window_games=15
        )

        season_scope = TeamMatchupSnapshotScope(canonical_season, snapshot_date)
        rolling_scope = TeamMatchupSnapshotScope(
            canonical_season, snapshot_date, window_games=15
        )
        season_play_types_are_bounded = (
            snapshot_date == retrieved_at.astimezone(EASTERN).date()
        )
        season_facts = self._collect_season(
            canonical_season,
            snapshot_date=snapshot_date,
            include_play_types=season_play_types_are_bounded,
        )
        season_observations = self._surface_observations(
            overrides=(
                {}
                if season_play_types_are_bounded
                else {
                    "play_types": (
                        "unavailable",
                        "provider_unbounded_as_of",
                    )
                }
            )
        )
        if set(boundaries) != set(team_ids):
            rolling_facts = []
            rolling_observations = self._surface_observations(
                default_status="missing",
                default_reason="insufficient_governed_games",
            )
        elif any(boundary.season_type is None for boundary in boundaries.values()):
            rolling_facts = []
            rolling_observations = self._surface_observations(
                default_status="unavailable",
                default_reason="provider_window_unrepresentable",
                overrides={"play_types": ("unavailable", "provider_unsupported")},
            )
        else:
            rolling_facts, window_overrides = self._collect_last_15(
                canonical_season,
                snapshot_date=snapshot_date,
                team_ids=team_ids,
                boundaries=boundaries,
            )
            rolling_observations = self._surface_observations(
                overrides={
                    "play_types": ("unavailable", "provider_unsupported"),
                    **window_overrides,
                }
            )
        self.repository.replace_snapshots(
            (
                (season_scope, season_facts, season_observations),
                (rolling_scope, rolling_facts, rolling_observations),
            ),
            retrieved_at=retrieved_at,
        )

    @staticmethod
    def _surface_observations(
        *,
        default_status: str = "available",
        default_reason: str | None = None,
        overrides: Mapping[str, tuple[str, str | None]] | None = None,
    ) -> tuple[TeamMatchupObservation, ...]:
        resolved = overrides or {}
        return tuple(
            TeamMatchupObservation(
                surface=surface,
                status=resolved.get(surface, (default_status, default_reason))[0],
                unavailable_reason=resolved.get(
                    surface, (default_status, default_reason)
                )[1],
            )
            for surface in MATCHUP_SURFACES
        )

    @staticmethod
    def _team_ids(events: list[dict[str, Any]]) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    int(event[field])
                    for event in events
                    if TeamWindowBoundaryResolver._is_governed_event(event)
                    for field in ("home_team_id", "away_team_id")
                }
            )
        )

    def _collect_season(
        self,
        season: str,
        *,
        snapshot_date: date,
        include_play_types: bool,
    ) -> list[TeamMatchupFact]:
        date_to = self._nba_date(snapshot_date)
        common = {
            "season": season,
            "season_type": "Regular Season",
            "team_id": None,
            "last_n_games": 0,
            "date_to": date_to,
        }
        traditional_frame = self.nba_stats.fetch_opponent_team_stats(
            None, per_mode_detailed="Totals", **common
        )
        minutes_by_team = self._minutes_by_team(traditional_frame)
        facts = self._traditional_facts(traditional_frame)
        for shooting_type in SHOOTING_TYPES:
            facts.extend(
                self._shot_type_facts(
                    self.nba_stats.fetch_opponent_shot_chart(
                        shooting_type,
                        None,
                        per_mode_simple="Totals",
                        **common,
                    ),
                    shooting_type,
                    minutes_by_team=minutes_by_team,
                )
            )
        facts.extend(
            self._shot_zone_facts(
                self.nba_stats.fetch_opponent_shooting_zone(
                    None, per_mode_detailed="Totals", **common
                ),
                minutes_by_team=minutes_by_team,
            )
        )
        if include_play_types:
            for play_type in PLAY_TYPES:
                facts.extend(
                    self._play_type_facts(
                        self.nba_stats.fetch_synergy_play_types(
                            play_type,
                            player_or_team_abbreviation="T",
                            type_grouping="Defensive",
                            season=season,
                            per_mode_simple="Totals",
                        ),
                        play_type,
                        minutes_by_team=minutes_by_team,
                    )
                )
        facts.extend(
            self._assist_facts(
                self.pbp_stats.fetch_totals_frame(
                    "opponent",
                    season=season,
                    season_type="Regular Season",
                    team_id=None,
                    from_date=None,
                    to_date=snapshot_date.isoformat(),
                )
            )
        )
        return facts

    def _collect_last_15(
        self,
        season: str,
        *,
        snapshot_date: date,
        team_ids: tuple[int, ...],
        boundaries: Mapping[int, TeamWindowBoundary],
    ) -> tuple[
        list[TeamMatchupFact], dict[str, tuple[str, str | None]]
    ]:
        facts_by_surface: dict[str, list[TeamMatchupFact]] = {
            surface: [] for surface in MATCHUP_SURFACES
        }
        unverified_surfaces: set[str] = set()
        date_to = self._nba_date(snapshot_date)
        for team_id in team_ids:
            boundary = boundaries[team_id]
            if boundary.season_type is None:
                raise ValueError("a mixed-phase rolling window cannot be collected")
            common = {
                "season": season,
                "season_type": boundary.season_type,
                "team_id": team_id,
                "last_n_games": 15,
                "date_to": date_to,
            }
            minutes_by_team = None
            if "traditional" not in unverified_surfaces:
                try:
                    traditional_frame = self.nba_stats.fetch_opponent_team_stats(
                        self._nba_date(boundary.from_date),
                        per_mode_detailed="Totals",
                        **common,
                    )
                    self._verify_team_window(
                        traditional_frame,
                        team_id=team_id,
                        expected_games=len(boundary.game_ids),
                        require_game_count=True,
                    )
                    try:
                        minutes_by_team = self._minutes_by_team(traditional_frame)
                    except ValueError as error:
                        raise _ProviderWindowUnverified(str(error)) from error
                    facts_by_surface["traditional"].extend(
                        self._with_start(
                            self._traditional_facts(traditional_frame),
                            boundary.from_date,
                        )
                    )
                except _ProviderWindowUnverified:
                    unverified_surfaces.update(
                        {"traditional", "shot_types", "shot_zones"}
                    )

            if minutes_by_team is not None and "shot_types" not in unverified_surfaces:
                try:
                    team_shot_type_facts = []
                    for shooting_type in SHOOTING_TYPES:
                        frame = self.nba_stats.fetch_opponent_shot_chart(
                            shooting_type,
                            self._nba_date(boundary.from_date),
                            per_mode_simple="Totals",
                            **common,
                        )
                        self._verify_team_window(
                            frame,
                            team_id=team_id,
                            expected_games=len(boundary.game_ids),
                            require_game_count=True,
                        )
                        team_shot_type_facts.extend(
                            self._shot_type_facts(
                                frame,
                                shooting_type,
                                minutes_by_team=minutes_by_team,
                            )
                        )
                    facts_by_surface["shot_types"].extend(
                        self._with_start(team_shot_type_facts, boundary.from_date)
                    )
                except _ProviderWindowUnverified:
                    unverified_surfaces.add("shot_types")

            if minutes_by_team is not None and "shot_zones" not in unverified_surfaces:
                try:
                    frame = self.nba_stats.fetch_opponent_shooting_zone(
                        self._nba_date(boundary.from_date),
                        per_mode_detailed="Totals",
                        **common,
                    )
                    self._verify_team_window(
                        frame,
                        team_id=team_id,
                        expected_games=len(boundary.game_ids),
                        require_game_count=False,
                    )
                    facts_by_surface["shot_zones"].extend(
                        self._with_start(
                            self._shot_zone_facts(
                                frame,
                                minutes_by_team=minutes_by_team,
                            ),
                            boundary.from_date,
                        )
                    )
                except _ProviderWindowUnverified:
                    unverified_surfaces.add("shot_zones")

            if "assist_locations" not in unverified_surfaces:
                try:
                    frame = self.pbp_stats.fetch_totals_frame(
                        "opponent",
                        season=season,
                        season_type=boundary.season_type,
                        team_id=team_id,
                        from_date=boundary.from_date.isoformat(),
                        to_date=boundary.to_date.isoformat(),
                    )
                    self._verify_team_window(
                        frame,
                        team_id=team_id,
                        expected_games=len(boundary.game_ids),
                        require_game_count=False,
                    )
                    facts_by_surface["assist_locations"].extend(
                        self._with_start(
                            self._assist_facts(frame),
                            boundary.from_date,
                        )
                    )
                except _ProviderWindowUnverified:
                    unverified_surfaces.add("assist_locations")

            facts_by_surface["play_types"].extend(
                TeamMatchupFact(
                    team_id=team_id,
                    base="play_types",
                    slice_key=play_type,
                    stat_key="PTS",
                    raw_value=None,
                    denominator_value=None,
                    denominator_unit=None,
                    provider="nba_synergy",
                    status="unavailable",
                    unavailable_reason="provider_unsupported",
                    window_start_date=boundary.from_date,
                )
                for play_type in PLAY_TYPES
            )
        facts = [
            fact
            for surface in MATCHUP_SURFACES
            if surface not in unverified_surfaces
            for fact in facts_by_surface[surface]
        ]
        return facts, {
            surface: ("unavailable", "provider_window_unverified")
            for surface in unverified_surfaces
        }

    @staticmethod
    def _nba_date(value: date) -> str:
        return value.strftime("%m/%d/%Y")

    @classmethod
    def _verify_team_window(
        cls,
        frame: pd.DataFrame,
        *,
        team_id: int,
        expected_games: int,
        require_game_count: bool,
    ) -> None:
        records = cls._flat_frame(frame).to_dict(orient="records")
        try:
            identified_team = cls._team_id(records[0]) if len(records) == 1 else None
        except ValueError as error:
            raise _ProviderWindowUnverified(str(error)) from error
        if identified_team != team_id:
            raise _ProviderWindowUnverified(
                f"provider response does not identify only team {team_id}"
            )
        row = records[0]
        game_counts = [
            row[column]
            for column in ("GP", "G", "GamesPlayed", "Games")
            if column in row and not pd.isna(row[column])
        ]
        if require_game_count and not game_counts:
            raise _ProviderWindowUnverified(
                "provider response does not expose its aggregate game count"
            )
        if game_counts:
            try:
                matches = all(float(value) == expected_games for value in game_counts)
            except (TypeError, ValueError) as error:
                raise _ProviderWindowUnverified(
                    "provider response has an invalid aggregate game count"
                ) from error
            if not matches:
                raise _ProviderWindowUnverified(
                    f"provider response does not contain exactly {expected_games} games"
                )

    @staticmethod
    def _flat_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            raise ValueError("team matchup providers must return DataFrames")
        result = frame.copy()
        if isinstance(result.columns, pd.MultiIndex):
            result.columns = [
                "_".join(str(part) for part in column if str(part)).strip("_")
                for column in result.columns
            ]
        return result

    @staticmethod
    def _team_id(row: Mapping[str, Any]) -> int:
        value = row.get("TEAM_ID", row.get("TeamId"))
        if value is None or pd.isna(value):
            raise ValueError("team matchup provider row is missing TEAM_ID")
        return int(value)

    @staticmethod
    def _denominator(row: Mapping[str, Any]) -> tuple[float, str]:
        for column, unit in (
            ("MIN", "minutes"),
            ("SecondsPlayed", "seconds"),
        ):
            value = row.get(column)
            if value is not None and not pd.isna(value) and float(value) > 0:
                return float(value), unit
        raise ValueError("team matchup provider row is missing a positive denominator")

    @classmethod
    def _minutes_by_team(cls, frame: pd.DataFrame) -> dict[int, float]:
        minutes_by_team = {}
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            value = row.get("MIN")
            if value is None or pd.isna(value) or float(value) <= 0:
                raise ValueError(
                    "traditional provider row is missing positive team minutes"
                )
            minutes_by_team[cls._team_id(row)] = float(value)
        return minutes_by_team

    @classmethod
    def _denominator_for_team(
        cls,
        row: Mapping[str, Any],
        minutes_by_team: Mapping[int, float] | None,
    ) -> tuple[float, str]:
        if minutes_by_team is None:
            return cls._denominator(row)
        team_id = cls._team_id(row)
        try:
            return minutes_by_team[team_id], "minutes"
        except KeyError as exc:
            raise ValueError(
                f"traditional provider response is missing team {team_id}"
            ) from exc

    @classmethod
    def _traditional_facts(cls, frame: pd.DataFrame) -> list[TeamMatchupFact]:
        facts = []
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator(row)
            for stat_key, candidates in _TRADITIONAL_STATS.items():
                source = next((name for name in candidates if name in row), None)
                if source is None:
                    raise ValueError(f"traditional provider row is missing {stat_key}")
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "traditional",
                        stat_key,
                        stat_key,
                        float(row[source]),
                        denominator,
                        unit,
                        "nba_stats",
                    )
                )
        return facts

    @classmethod
    def _shot_type_facts(
        cls,
        frame: pd.DataFrame,
        shooting_type: str,
        *,
        minutes_by_team: Mapping[int, float] | None = None,
    ) -> list[TeamMatchupFact]:
        facts = []
        slice_key = shooting_type.replace(" ", "_").casefold()
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator_for_team(row, minutes_by_team)
            for stat_key in _SHOT_STATS:
                if stat_key not in row:
                    raise ValueError(f"shot-type provider row is missing {stat_key}")
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "shot_types",
                        slice_key,
                        stat_key,
                        float(row[stat_key]),
                        denominator,
                        unit,
                        "nba_stats",
                    )
                )
        return facts

    @classmethod
    def _shot_zone_facts(
        cls,
        frame: pd.DataFrame,
        *,
        minutes_by_team: Mapping[int, float] | None = None,
    ) -> list[TeamMatchupFact]:
        facts = []
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator_for_team(row, minutes_by_team)
            stat_columns = [
                column
                for column in row
                if str(column).endswith(("_OPP_FGM", "_OPP_FGA"))
            ]
            if not stat_columns:
                raise ValueError(
                    "shot-zone provider row has no raw opponent makes/attempts"
                )
            for column in stat_columns:
                slice_key, stat_key = str(column).rsplit("_OPP_", 1)
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "shot_zones",
                        slice_key,
                        stat_key,
                        float(row[column]),
                        denominator,
                        unit,
                        "nba_stats",
                    )
                )
        return facts

    @classmethod
    def _play_type_facts(
        cls,
        frame: pd.DataFrame,
        play_type: str,
        *,
        minutes_by_team: Mapping[int, float] | None = None,
    ) -> list[TeamMatchupFact]:
        facts = []
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator_for_team(row, minutes_by_team)
            facts.append(
                TeamMatchupFact(
                    cls._team_id(row),
                    "play_types",
                    play_type,
                    "PTS",
                    float(row["PTS"]),
                    denominator,
                    unit,
                    "nba_synergy",
                )
            )
        return facts

    @classmethod
    def _assist_facts(cls, frame: pd.DataFrame) -> list[TeamMatchupFact]:
        facts = []
        for row in cls._flat_frame(frame).to_dict(orient="records"):
            denominator, unit = cls._denominator(row)
            for stat_key in _ASSIST_STATS:
                if stat_key not in row:
                    raise ValueError(f"PBP opponent row is missing {stat_key}")
                facts.append(
                    TeamMatchupFact(
                        cls._team_id(row),
                        "assist_locations",
                        stat_key,
                        stat_key,
                        float(row[stat_key]),
                        denominator,
                        unit,
                        "pbp_stats",
                    )
                )
        return facts

    @staticmethod
    def _with_start(facts: list[TeamMatchupFact], start: date) -> list[TeamMatchupFact]:
        return [replace(fact, window_start_date=start) for fact in facts]


__all__ = [
    "TeamMatchupRefreshService",
    "TeamWindowBoundary",
    "TeamWindowBoundaryResolver",
]
