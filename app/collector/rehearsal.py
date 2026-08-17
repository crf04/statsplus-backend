"""Offline/live-compatible provider probes for the Historical Rehearsal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .normalizers import (
    PLAY_TYPES,
    SHOT_TYPES,
    SHOT_ZONES,
    normalize_grouped_shot_response,
    normalize_opponent_grouped_shot_response,
    normalize_opponent_zone_response,
    normalize_roster_response,
    normalize_schedule_response,
    normalize_synergy_response,
    normalize_zone_response,
)
from .provider import ProviderTransientError, _call

NBA_TEAM_IDS = tuple(range(1610612737, 1610612767))


class SanitizedFixtureProvider:
    """Deterministic recorded-shape provider used by the default offline gate."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def _record(self, method: str, **parameters: Any) -> None:
        self.requests.append({"method": method, **parameters})

    def fetch_whole_season_schedule(self, *, season: str) -> list[dict[str, Any]]:
        self._record("schedule", season=season)
        return [{"game_id": "fixture-game", "home_team_id": NBA_TEAM_IDS[0],
                 "away_team_id": NBA_TEAM_IDS[1], "scheduled_at": "2026-04-10T00:00:00Z",
                 "status": "Final", "classification": "Regular Season"}]

    def get_player_roster(self, *, season: str) -> list[dict[str, Any]]:
        self._record("roster", season=season)
        return [{"player_id": 1, "display_name": "Fixture Player", "team_id": NBA_TEAM_IDS[0],
                 "season": season, "roster_status": "active"}]

    def fetch_synergy_play_types(self, play_type: str, **parameters: Any) -> list[dict[str, Any]]:
        self._record("synergy", play_type=play_type, **parameters)
        return [{"player_id": 1, "category": play_type, "GP": 1, "POSS": 1, "PTS": 1}]

    def fetch_player_shot_type(self, general_range: str, **parameters: Any) -> list[dict[str, Any]]:
        self._record("player_shot_type", general_range=general_range, **parameters)
        return [{"player_id": 1, "category": general_range, "FGA": 1, "FGM": 1}]

    def fetch_player_shooting_zone(self, date_from: str | None = None, **parameters: Any) -> list[dict[str, Any]]:
        self._record("player_zone", date_from=date_from, **parameters)
        return [self._zones(player_id=1)]

    def fetch_opponent_shot_chart(self, general_range: str, date_from: str | None, **parameters: Any) -> list[dict[str, Any]]:
        self._record("opponent_shot_type", general_range=general_range, date_from=date_from, **parameters)
        return [{
            "team_id": parameters["team_id"], "category": general_range,
            "GP": 15 if parameters.get("last_n_games") == 15 else 82,
            "FG2M": 1, "FG2A": 1, "FG3M": 1, "FG3A": 1,
        }]

    def fetch_opponent_shooting_zone(self, date_from: str | None, **parameters: Any) -> list[dict[str, Any]]:
        self._record("opponent_zone", date_from=date_from, **parameters)
        return [{
            "team_id": parameters["team_id"],
            "GP": 15 if parameters.get("last_n_games") == 15 else 82,
            **{
                f"{zone}_{stat}": 1
                for zone in SHOT_ZONES if zone != "Corner 3"
                for stat in ("OPP_FGM", "OPP_FGA")
            },
            "Left Corner 3_OPP_FGM": 0.5,
            "Left Corner 3_OPP_FGA": 0.5,
            "Right Corner 3_OPP_FGM": 0.5,
            "Right Corner 3_OPP_FGA": 0.5,
        }]

    @staticmethod
    def _zones(**identity: Any) -> dict[str, Any]:
        return {**identity, "Restricted Area": 1, "In The Paint (Non-RA)": 1,
                "Mid-Range": 1, "Corner 3": 1, "Above the Break 3": 1}


@dataclass(frozen=True, slots=True)
class ProbeResult:
    scope: str
    passed: bool
    request: dict[str, Any]
    reason: str | None = None


class ResidentialCompatibilityProbes:
    """Exercise each supported NBA Stats request with explicit scope facts."""

    def __init__(self, provider: Any, *, clock: Any | None = None) -> None:
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, *, season: str, cutoff: datetime | str, opponent_team_id: int) -> tuple[ProbeResult, ...]:
        results: list[ProbeResult] = []
        cutoff_value = datetime.fromisoformat(str(cutoff).replace("Z", "+00:00")) if isinstance(cutoff, str) else cutoff
        date_to = cutoff_value.date().isoformat()
        results.append(self._probe(
            "event_catalog", {"season": season, "season_type": "Regular Season"},
            lambda: normalize_schedule_response(
                _call(self.provider, "fetch_whole_season_schedule", season=season),
                season=season, cutoff=cutoff,
            ),
        ))
        results.append(self._probe(
            "athlete_catalog", {"season": season, "season_type": "Regular Season"},
            lambda: normalize_roster_response(
                _call(self.provider, "get_player_roster", season=season),
                season=season, cutoff=cutoff,
            ),
        ))
        # Each Synergy category is an independently verified response.  This
        # deliberately exercises the provider's authored taxonomy and never
        # asks it for the unsupported public L15 window.
        for category in PLAY_TYPES:
            results.append(self._probe(
                f"synergy:{category}", {
                    "season": season, "season_type": "Regular Season",
                    "play_type": category, "window": "season",
                },
                lambda category=category: normalize_synergy_response(
                    _call(self.provider, "fetch_synergy_play_types", category,
                          player_or_team_abbreviation="P", type_grouping="season",
                          season=season, season_type="Regular Season"),
                    season=season, cutoff=cutoff,
                    scope={"window": "season", "phase": "Regular Season", "play_type": category},
                ),
            ))
        for category in SHOT_TYPES:
            results.append(self._probe(
                f"player_shot_types:{category}", {
                    "season": season, "season_type": "Regular Season",
                    "general_range": category, "window": "season",
                },
                lambda category=category: normalize_grouped_shot_response(
                    _call(self.provider, "fetch_player_shot_type", category,
                          season=season, season_type="Regular Season"),
                    season=season, cutoff=cutoff,
                    scope={"window": "season", "subject": "player", "category": category, "phase": "Regular Season"},
                ),
            ))
        results.append(self._probe(
            "player_shot_zones", {"season": season, "season_type": "Regular Season", "window": "season"},
            lambda: normalize_zone_response(
                _call(self.provider, "fetch_player_shooting_zone", None,
                      season=season, season_type="Regular Season"),
                season=season, cutoff=cutoff,
                scope={"window": "season", "subject": "player", "phase": "Regular Season"},
            ),
        ))
        for window in ("season", "l15"):
            last_n_games = 15 if window == "l15" else None
            for category in SHOT_TYPES:
                results.append(self._probe(
                    f"opponent_shot_types_{window}:{category}", {
                        "season": season, "season_type": "Regular Season", "team_id": int(opponent_team_id),
                        "last_n_games": last_n_games, "window": window, "general_range": category,
                        "date_from": None, "date_to": date_to,
                    },
                    lambda category=category, window=window, last_n_games=last_n_games: normalize_opponent_grouped_shot_response(
                        _call(self.provider, "fetch_opponent_shot_chart", category, None,
                              date_to=date_to, season=season, season_type="Regular Season",
                              team_id=int(opponent_team_id), last_n_games=last_n_games),
                        season=season, cutoff=cutoff, team_id=int(opponent_team_id), window=window,
                        category=category,
                    ),
                ))
            results.append(self._probe(
                f"opponent_shot_zones_{window}", {
                    "season": season, "season_type": "Regular Season", "team_id": int(opponent_team_id),
                    "last_n_games": last_n_games, "window": window, "date_from": None, "date_to": date_to,
                },
                lambda window=window, last_n_games=last_n_games: normalize_opponent_zone_response(
                    _call(self.provider, "fetch_opponent_shooting_zone", None,
                          date_to=date_to, season=season, season_type="Regular Season",
                          team_id=int(opponent_team_id), last_n_games=last_n_games),
                    season=season, cutoff=cutoff, team_id=int(opponent_team_id), window=window,
                ),
            ))
        return tuple(results)

    @staticmethod
    def _probe(scope: str, request: dict[str, Any], operation: Any) -> ProbeResult:
        safe_request = {str(key): value for key, value in request.items() if key not in {"payload", "response"}}
        try:
            operation()
        except ProviderTransientError as error:
            return ProbeResult(scope, False, safe_request, str(error))
        except Exception as error:
            return ProbeResult(scope, False, safe_request, getattr(error, "reason", type(error).__name__))
        return ProbeResult(scope, True, safe_request)


CompatibilityProbe = ResidentialCompatibilityProbes


__all__ = ["CompatibilityProbe", "NBA_TEAM_IDS", "ProbeResult", "ResidentialCompatibilityProbes", "SanitizedFixtureProvider"]
