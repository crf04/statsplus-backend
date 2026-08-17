"""Injectable NBA provider scope adapter for the residential worker."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import math
import os
from typing import Any, Iterator, Protocol

from .contracts import NormalizedObservation, ProviderContractError
from .normalizers import (
    PLAY_TYPES,
    SHOT_TYPES,
    _flatten_frame_columns,
    normalize_grouped_shot_response,
    normalize_opponent_grouped_shot_response,
    normalize_opponent_synergy_response,
    normalize_opponent_zone_response,
    normalize_roster_response,
    normalize_schedule_response,
    normalize_synergy_response,
    normalize_zone_response,
)


class ProviderTransientError(RuntimeError):
    """A provider/network failure eligible for bounded retry."""


class ProviderScopeProvider(Protocol):
    def fetch_whole_season_schedule(self, *, season: str) -> Any: ...

    def get_player_roster(self, *, season: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class ScopeWork:
    scope: str
    observation_type: str
    season: str
    cutoff: str
    instruction_id: str
    manifest_id: str | None
    parameters: Mapping[str, Any]


def _call(provider: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(provider, method, None)
    if not callable(function):
        raise ProviderContractError("provider_scope_unavailable")
    try:
        return function(*args, **kwargs)
    except ProviderContractError:
        raise
    except (TimeoutError, ConnectionError) as error:
        raise ProviderTransientError("provider_timeout") from error
    except Exception as error:
        # requests exceptions are intentionally identified structurally so the
        # package does not require the provider adapter to use requests.
        name = type(error).__name__.casefold()
        if any(marker in name for marker in (
            "timeout", "connection", "request", "http", "unavailable", "temporar", "ratelimit",
        )):
            raise ProviderTransientError("provider_unavailable") from error
        raise


def _scope_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): val for key, val in value.items()}
    if isinstance(value, str) and value.strip():
        return {"window": value.strip()}
    raise ProviderContractError("invalid_scope")


class ResidentialScopeExecutor:
    """Turn an authorized manifest/bootstrap instruction into envelopes."""

    def __init__(self, provider: Any, *, clock: Any) -> None:
        self.provider = provider
        self.clock = clock

    def execute_catalog(
        self, request: Mapping[str, Any], *, catalog_type: str,
        collector_id: str, environment: str, retrieved_at: datetime | str,
        catalog_version: str,
    ) -> tuple[Any, str]:
        request_id = str(request.get("request_id") or "").strip()
        season = str(request.get("season") or "").strip()
        cutoff = str(request.get("cutoff") or "").strip()
        expires_at = str(request.get("expires_at") or "").strip() or None
        if not request_id or not season or not cutoff:
            raise ProviderContractError("malformed_bootstrap")
        if catalog_type not in {"event", "athlete"}:
            raise ProviderContractError("provider_scope_unavailable")
        raw = (
            _call(self.provider, "fetch_whole_season_schedule", season=season)
            if catalog_type == "event"
            else _call(self.provider, "get_player_roster", season=season)
        )
        normalized = (
            normalize_schedule_response(raw, season=season, cutoff=cutoff)
            if catalog_type == "event"
            else normalize_roster_response(raw, season=season, cutoff=cutoff)
        )
        if not normalized.complete:
            raise ProviderContractError("incomplete_observation")
        from .contracts import CatalogEnvelope, ObservationEnvelope
        envelope = ObservationEnvelope.from_observation(
            normalized, manifest_id=None, environment=environment,
            collector_id=collector_id, instruction_id=request_id,
            retrieved_at=retrieved_at,
        )
        return CatalogEnvelope(
            request_id=request_id, envelope=envelope,
            catalog_version=catalog_version, expires_at=expires_at,
        ), normalized.observation_type

    def execute_scope(
        self, work: ScopeWork, *, collector_id: str, environment: str,
        retrieved_at: datetime | str,
    ) -> tuple[NormalizedObservation, ...]:
        return tuple(self.iter_scope(
            work, collector_id=collector_id, environment=environment,
            retrieved_at=retrieved_at,
        ))

    def iter_scope(
        self, work: ScopeWork, *, collector_id: str, environment: str,
        retrieved_at: datetime | str,
    ) -> Iterator[NormalizedObservation]:
        """Yield immediately after each independently validated response."""

        del collector_id, environment, retrieved_at
        scope = work.scope.strip()
        parameters = _scope_mapping(work.parameters)
        window = str(parameters.get("window") or "season").strip().casefold()
        if scope in {"synergy", "synergy_play_types", "synergy_opponent"}:
            if window != "season":
                raise ProviderContractError("provider_window_unsupported")
            requested = parameters.get("play_type")
            categories = (str(requested),) if requested else PLAY_TYPES
            for category in categories:
                raw = _call(
                    self.provider, "fetch_synergy_play_types", category,
                    player_or_team_abbreviation=str(parameters.get("subject_code", "P")),
                    type_grouping=str(parameters.get("type_grouping", "season")),
                    per_mode_simple=str(parameters.get("per_mode", "Totals")),
                    season=work.season,
                    season_type="Regular Season",
                )
                normalized_scope = {
                    "window": "season", "phase": "Regular Season",
                    "play_type": category,
                    "subject": "opponent" if scope == "synergy_opponent" else "player",
                    "value_mode": str(parameters.get("value_mode", "totals")),
                }
                normalizer = (
                    normalize_opponent_synergy_response
                    if scope == "synergy_opponent" else normalize_synergy_response
                )
                yield normalizer(
                    raw, season=work.season, cutoff=work.cutoff,
                    scope=normalized_scope,
                )
            return
        if scope in {"grouped_shot_types", "shot_types", "player_shot_types", "shot_types_opponent"}:
            if window not in {"season", "l15"}:
                raise ProviderContractError("provider_window_unsupported")
            subject = "opponent" if scope == "shot_types_opponent" else str(parameters.get("subject", "player")).casefold()
            requested_value = parameters.get("general_range", parameters.get("category"))
            categories = (str(requested_value),) if requested_value is not None else SHOT_TYPES
            if subject == "opponent":
                team_id = parameters.get("team_id")
                if team_id is None:
                    raise ProviderContractError("scope_team_required")
                for category in categories:
                    raw = _call(
                        self.provider, "fetch_opponent_shot_chart", category,
                        parameters.get("date_from"), date_to=parameters.get("date_to"),
                        season=work.season, season_type="Regular Season",
                        team_id=int(team_id), last_n_games=15 if window == "l15" else None,
                        per_mode_simple=str(parameters.get("per_mode", "Totals")),
                    )
                    yield normalize_opponent_grouped_shot_response(
                        raw, season=work.season, cutoff=work.cutoff,
                        team_id=int(team_id), window=window, category=category,
                        value_mode=str(parameters.get(
                            "value_mode", "totals_with_minutes"
                        )),
                        endpoint_window={
                            "last_n_games": 15 if window == "l15" else 0,
                            "date_from": parameters.get("date_from"),
                            "date_to": parameters.get("date_to"),
                        },
                    )
                return
            for category in categories:
                raw = _call(
                    self.provider, "fetch_player_shot_type", category,
                    season=work.season, season_type="Regular Season",
                )
                yield normalize_grouped_shot_response(
                    raw, season=work.season, cutoff=work.cutoff,
                    scope={"window": window, "subject": "player", "category": category, "phase": "Regular Season"},
                )
            return
        if scope in {"exact_shot_zones", "shot_zones", "player_shot_zones", "shot_zones_opponent"}:
            if window not in {"season", "l15"}:
                raise ProviderContractError("provider_window_unsupported")
            subject = "opponent" if scope == "shot_zones_opponent" else str(parameters.get("subject", "player")).casefold()
            if subject == "opponent":
                team_id = parameters.get("team_id")
                if team_id is None:
                    raise ProviderContractError("scope_team_required")
                raw = _call(
                    self.provider, "fetch_opponent_shooting_zone",
                    parameters.get("date_from"), date_to=parameters.get("date_to"),
                    season=work.season, season_type="Regular Season",
                    team_id=int(team_id), last_n_games=15 if window == "l15" else None,
                    per_mode_detailed=str(parameters.get("per_mode", "Per48")),
                )
                yield normalize_opponent_zone_response(
                    raw, season=work.season, cutoff=work.cutoff,
                    team_id=int(team_id), window=window,
                    value_mode=str(parameters.get("value_mode", "per48")),
                    endpoint_window={
                        "last_n_games": 15 if window == "l15" else 0,
                        "date_from": parameters.get("date_from"),
                        "date_to": parameters.get("date_to"),
                    },
                )
                return
            raw = _call(
                self.provider, "fetch_player_shooting_zone",
                parameters.get("date_from"), season=work.season,
                season_type="Regular Season",
            )
            yield normalize_zone_response(
                raw, season=work.season, cutoff=work.cutoff,
                scope={"window": window, "subject": "player", "phase": "Regular Season"},
            )
            return
        if scope in {"synergy:l15", "synergy_l15"}:
            raise ProviderContractError("provider_window_unsupported")
        raise ProviderContractError("scope_not_registered")


class _StandaloneNBAProvider:
    """Small NBA Stats adapter with no application/web/database imports.

    The residential package intentionally owns this lazy boundary instead of
    importing the Flask application's provider graph.  Each method returns a
    provider frame to the collector normalizer and retains no response object.
    """

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = max(1.0, min(float(timeout), 120.0))

    @staticmethod
    def _frame(endpoint: Any, *, index: int = 0) -> Any:
        frames = endpoint.get_data_frames()
        if not isinstance(frames, (list, tuple)) or index < 0 or index >= len(frames):
            raise ProviderContractError("provider_schema_changed")
        return frames[index]

    def _request(self, factory: Any) -> Any:
        try:
            return self._frame(factory())
        except ProviderContractError:
            raise
        except Exception as error:
            name = type(error).__name__.casefold()
            if any(marker in name for marker in (
                "timeout", "connection", "request", "http", "unavailable", "temporar", "ratelimit",
            )):
                raise ProviderTransientError("provider_unavailable") from error
            raise ProviderContractError("provider_schema_changed") from error

    @staticmethod
    def _bind_window_gp(frame: Any, evidence: Any, *, team_id: int) -> Any:
        try:
            frame = _flatten_frame_columns(frame)
            evidence = _flatten_frame_columns(evidence)
            rows = evidence.loc[
                evidence["TEAM_ID"] == team_id, ["TEAM_ID", "GP", "MIN"]
            ]
            if len(rows.index) != 1:
                raise ProviderContractError("provider_window_unverified")
            expected_gp = int(rows.iloc[0]["GP"])
            expected_minutes = float(rows.iloc[0]["MIN"])
            if not math.isfinite(expected_minutes) or expected_minutes <= 0:
                raise ProviderContractError("provider_window_unverified")
            merged = frame.copy()
            if "TEAM_ID" not in merged or not (merged["TEAM_ID"] == team_id).all():
                raise ProviderContractError("provider_window_unverified")
            if "GP" in merged and not (merged["GP"] == expected_gp).all():
                raise ProviderContractError("provider_window_unverified")
            merged["GP"] = expected_gp
            merged["MIN"] = expected_minutes
            return merged
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ProviderContractError("provider_window_unverified") from error

    def _team_window_evidence(
        self, *, season: str, season_type: str, team_id: int,
        last_n_games: int | None, date_from: str | None, date_to: str | None,
    ) -> Any:
        from nba_api.stats import endpoints
        return self._request(lambda: endpoints.LeagueDashTeamStats(
            season=season,
            season_type_all_star=season_type,
            team_id_nullable=team_id,
            last_n_games=str(last_n_games or 0),
            date_from_nullable=date_from or "",
            date_to_nullable=date_to or "",
            per_mode_detailed="Totals",
            league_id_nullable="00",
            timeout=self.timeout,
        ))

    def fetch_whole_season_schedule(self, *, season: str) -> Any:
        from nba_api.stats import endpoints
        return self._request(lambda: endpoints.ScheduleLeagueV2(season=season, timeout=self.timeout))

    def get_player_roster(self, *, season: str) -> Any:
        from nba_api.stats import endpoints
        return self._request(lambda: endpoints.commonallplayers.CommonAllPlayers(
            is_only_current_season=0, season=season, timeout=self.timeout,
        ))

    def fetch_synergy_play_types(
        self, play_type: str, *, player_or_team_abbreviation: str,
        type_grouping: str, season: str, season_type: str,
        per_mode_simple: str = "Totals",
    ) -> Any:
        from nba_api.stats import endpoints
        frame = self._request(lambda: endpoints.SynergyPlayTypes(
            play_type_nullable=play_type,
            player_or_team_abbreviation=player_or_team_abbreviation,
            type_grouping_nullable=type_grouping,
            season=season,
            season_type_all_star=season_type,
            per_mode_simple=per_mode_simple,
            league_id="00",
            timeout=self.timeout,
        ))
        if player_or_team_abbreviation != "T":
            return frame
        minutes = self._request(lambda: endpoints.LeagueDashTeamStats(
            season=season,
            season_type_all_star=season_type,
            per_mode_detailed="Totals",
            league_id_nullable="00",
            timeout=self.timeout,
        ))
        try:
            minute_rows = minutes[["TEAM_ID", "MIN", "GP"]].rename(
                columns={"GP": "WINDOW_GP"}
            )
            merged = frame.merge(minute_rows, on="TEAM_ID", how="left")
            if merged[["MIN", "WINDOW_GP"]].isna().any().any():
                raise ProviderContractError("provider_window_unverified")
            if "GP" in merged and not (merged["GP"] == merged["WINDOW_GP"]).all():
                raise ProviderContractError("provider_window_unverified")
            merged["GP"] = merged["WINDOW_GP"]
            return merged.drop(columns=["WINDOW_GP"])
        except (KeyError, TypeError, AttributeError) as error:
            raise ProviderContractError("provider_window_unverified") from error

    def fetch_player_shot_type(self, general_range: str, *, season: str, season_type: str) -> Any:
        from nba_api.stats import endpoints
        return self._request(lambda: endpoints.LeagueDashPlayerPtShot(
            general_range_nullable=general_range,
            season=season,
            season_type_all_star=season_type,
            per_mode_simple="Totals",
            league_id="00",
            timeout=self.timeout,
        ))

    def fetch_player_shooting_zone(
        self, date_from: str | None = None, *, season: str,
        season_type: str,
    ) -> Any:
        from nba_api.stats import endpoints
        return self._request(lambda: endpoints.LeagueDashPlayerShotLocations(
            distance_range="By Zone",
            per_mode_detailed="PerGame",
            date_from_nullable=date_from,
            season=season,
            season_type_all_star=season_type,
            timeout=self.timeout,
        ))

    def fetch_opponent_shot_chart(
        self, general_range: str, date_from: str | None, *, date_to: str | None = None,
        season: str, season_type: str, team_id: int, last_n_games: int | None,
        per_mode_simple: str = "Totals",
    ) -> Any:
        from nba_api.stats import endpoints
        frame = self._request(lambda: endpoints.LeagueDashOppPtShot(
            general_range_nullable=general_range,
            date_from_nullable=date_from,
            date_to_nullable=date_to,
            season=season,
            season_type_all_star=season_type,
            team_id_nullable=team_id,
            last_n_games_nullable=last_n_games,
            per_mode_simple=per_mode_simple,
            league_id="00",
            timeout=self.timeout,
        ))
        evidence = self._team_window_evidence(
            season=season, season_type=season_type, team_id=team_id,
            last_n_games=last_n_games, date_from=date_from, date_to=date_to,
        )
        return self._bind_window_gp(frame, evidence, team_id=team_id)

    def fetch_opponent_shooting_zone(
        self, date_from: str | None, *, date_to: str | None = None,
        season: str, season_type: str, team_id: int, last_n_games: int | None,
        per_mode_detailed: str = "Per48",
    ) -> Any:
        from nba_api.stats import endpoints
        frame = self._request(lambda: endpoints.LeagueDashTeamShotLocations(
            distance_range="By Zone",
            measure_type_simple="Opponent",
            per_mode_detailed=per_mode_detailed,
            date_from_nullable=date_from,
            date_to_nullable=date_to,
            season=season,
            season_type_all_star=season_type,
            team_id_nullable=team_id,
            last_n_games=last_n_games,
            league_id_nullable="00",
            timeout=self.timeout,
        ))
        evidence = self._team_window_evidence(
            season=season, season_type=season_type, team_id=team_id,
            last_n_games=last_n_games, date_from=date_from, date_to=date_to,
        )
        return self._bind_window_gp(frame, evidence, team_id=team_id)


class NBAStatsProviderAdapter:
    """Lazy standalone provider; constructing it never starts Flask or opens DB."""

    def __init__(self, provider: Any | None = None, *, settings: Any | None = None) -> None:
        if provider is not None:
            self.provider = provider
            return
        configured = getattr(getattr(settings, "providers", None), "nba_stats_timeout_seconds", None)
        raw_timeout = configured if configured is not None else os.environ.get("NBA_STATS_TIMEOUT_SECONDS", "10")
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = 10.0
        self.provider = _StandaloneNBAProvider(timeout=timeout)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)


__all__ = ["NBAStatsProviderAdapter", "ProviderScopeProvider", "ProviderTransientError", "ResidentialScopeExecutor", "ScopeWork"]
