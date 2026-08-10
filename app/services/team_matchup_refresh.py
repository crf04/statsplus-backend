"""Collect exact team-window matchup facts for durable publication."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from collections.abc import Callable, Mapping
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

        games_by_team: dict[int, list[tuple[datetime, str]]] = defaultdict(list)
        for event in self.event_catalog.get_events(season):
            if not self._is_governed_completion(event, as_of=as_of):
                continue
            scheduled_at = self._scheduled_at(event)
            game_id = str(event["nba_game_id"])
            for field in ("home_team_id", "away_team_id"):
                games_by_team[int(event[field])].append((scheduled_at, game_id))

        boundaries: dict[int, TeamWindowBoundary] = {}
        for team_id, games in games_by_team.items():
            selected = sorted(games, reverse=True)[:window_games]
            if len(selected) != window_games:
                continue
            boundaries[team_id] = TeamWindowBoundary(
                team_id=team_id,
                from_date=selected[-1][0].astimezone(EASTERN).date(),
                to_date=as_of,
                game_ids=tuple(game_id for _, game_id in selected),
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
            return False
        if classification.kind not in {"Regular Season", "Playoffs"}:
            return False
        if is_postponed_event(event):
            return False
        is_final = event.get("status_code") == NBAGameStatus.FINAL or str(
            event.get("status_text") or ""
        ).casefold().startswith("final")
        if not is_final:
            return False
        return cls._scheduled_at(event).astimezone(EASTERN).date() <= as_of


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
        if set(boundaries) != set(team_ids):
            raise ValueError(
                "exact Last-15 is unavailable until every team has 15 governed games"
            )

        season_scope = TeamMatchupSnapshotScope(canonical_season, snapshot_date)
        rolling_scope = TeamMatchupSnapshotScope(
            canonical_season, snapshot_date, window_games=15
        )
        season_facts = self._collect_season(
            canonical_season, snapshot_date=snapshot_date
        )
        rolling_facts = self._collect_last_15(
            canonical_season,
            snapshot_date=snapshot_date,
            team_ids=team_ids,
            boundaries=boundaries,
        )
        available = tuple(
            TeamMatchupObservation(surface=surface, status="available")
            for surface in (
                "assist_locations",
                "play_types",
                "shot_types",
                "shot_zones",
                "traditional",
            )
        )
        rolling_observations = tuple(
            TeamMatchupObservation(
                surface=surface,
                status="unavailable" if surface == "play_types" else "available",
                unavailable_reason=(
                    "provider_unsupported" if surface == "play_types" else None
                ),
            )
            for surface in (
                "assist_locations",
                "play_types",
                "shot_types",
                "shot_zones",
                "traditional",
            )
        )
        self.repository.replace_snapshots(
            (
                (season_scope, season_facts, available),
                (rolling_scope, rolling_facts, rolling_observations),
            ),
            retrieved_at=retrieved_at,
        )

    @staticmethod
    def _team_ids(events: list[dict[str, Any]]) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    int(event[field])
                    for event in events
                    for field in ("home_team_id", "away_team_id")
                }
            )
        )

    def _collect_season(
        self, season: str, *, snapshot_date: date
    ) -> list[TeamMatchupFact]:
        date_to = snapshot_date.isoformat()
        common = {
            "season": season,
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
                    team_id=None,
                    from_date=None,
                    to_date=date_to,
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
    ) -> list[TeamMatchupFact]:
        facts: list[TeamMatchupFact] = []
        date_to = snapshot_date.isoformat()
        for team_id in team_ids:
            boundary = boundaries[team_id]
            common = {
                "season": season,
                "team_id": team_id,
                "last_n_games": 15,
                "date_to": date_to,
            }
            traditional_frame = self.nba_stats.fetch_opponent_team_stats(
                None, per_mode_detailed="Totals", **common
            )
            minutes_by_team = self._minutes_by_team(traditional_frame)
            facts.extend(
                self._with_start(
                    self._traditional_facts(traditional_frame),
                    boundary.from_date,
                )
            )
            for shooting_type in SHOOTING_TYPES:
                facts.extend(
                    self._with_start(
                        self._shot_type_facts(
                            self.nba_stats.fetch_opponent_shot_chart(
                                shooting_type,
                                None,
                                per_mode_simple="Totals",
                                **common,
                            ),
                            shooting_type,
                            minutes_by_team=minutes_by_team,
                        ),
                        boundary.from_date,
                    )
                )
            facts.extend(
                self._with_start(
                    self._shot_zone_facts(
                        self.nba_stats.fetch_opponent_shooting_zone(
                            None, per_mode_detailed="Totals", **common
                        ),
                        minutes_by_team=minutes_by_team,
                    ),
                    boundary.from_date,
                )
            )
            facts.extend(
                self._with_start(
                    self._assist_facts(
                        self.pbp_stats.fetch_totals_frame(
                            "opponent",
                            season=season,
                            team_id=team_id,
                            from_date=boundary.from_date.isoformat(),
                            to_date=boundary.to_date.isoformat(),
                        )
                    ),
                    boundary.from_date,
                )
            )
            facts.extend(
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
        return facts

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
            ("GP", "games"),
            ("G", "games"),
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
        return [
            TeamMatchupFact(
                team_id=fact.team_id,
                base=fact.base,
                slice_key=fact.slice_key,
                stat_key=fact.stat_key,
                raw_value=fact.raw_value,
                denominator_value=fact.denominator_value,
                denominator_unit=fact.denominator_unit,
                provider=fact.provider,
                status=fact.status,
                unavailable_reason=fact.unavailable_reason,
                window_start_date=start,
            )
            for fact in facts
        ]


__all__ = [
    "TeamMatchupRefreshService",
    "TeamWindowBoundary",
    "TeamWindowBoundaryResolver",
]
