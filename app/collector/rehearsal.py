"""Offline/live-compatible provider probes for the Historical Rehearsal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .normalizers import (
    PLAY_TYPES,
    SHOT_TYPES,
    normalize_grouped_shot_response,
    normalize_opponent_grouped_shot_response,
    normalize_opponent_zone_response,
    normalize_roster_response,
    normalize_schedule_response,
    normalize_synergy_response,
    normalize_zone_response,
)
from .provider import ProviderTransientError, _call


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
                    },
                    lambda category=category, window=window, last_n_games=last_n_games: normalize_opponent_grouped_shot_response(
                        _call(self.provider, "fetch_opponent_shot_chart", category, None,
                              date_to=None, season=season, season_type="Regular Season",
                              team_id=int(opponent_team_id), last_n_games=last_n_games),
                        season=season, cutoff=cutoff, team_id=int(opponent_team_id), window=window,
                        category=category,
                    ),
                ))
            results.append(self._probe(
                f"opponent_shot_zones_{window}", {
                    "season": season, "season_type": "Regular Season", "team_id": int(opponent_team_id),
                    "last_n_games": last_n_games, "window": window,
                },
                lambda window=window, last_n_games=last_n_games: normalize_opponent_zone_response(
                    _call(self.provider, "fetch_opponent_shooting_zone", None,
                          date_to=None, season=season, season_type="Regular Season",
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


__all__ = ["CompatibilityProbe", "ProbeResult", "ResidentialCompatibilityProbes"]
