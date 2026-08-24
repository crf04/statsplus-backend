"""Provider response normalizers used by the residential collector.

The live NBA adapter returns pandas frames while rehearsal fakes commonly use
lists of mappings.  These functions intentionally accept both forms and
return the same immutable, JSON-safe ``NormalizedObservation`` contract.
They never return the upstream frame or retain provider response objects.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .contracts import NormalizedObservation, ProviderContractError

try:  # pandas is an application dependency, but keep the package seam loose.
    import pandas as pd
except ImportError:  # pragma: no cover - used by a minimal wheel install
    pd = None  # type: ignore[assignment]


PLAY_TYPES = (
    "Transition", "Isolation", "PRBallHandler", "PRRollMan", "OffRebound",
    "Spotup", "Cut", "Handoff", "OffScreen", "Misc", "Postup",
)
SHOT_TYPES = ("Catch and Shoot", "Pullups", "Less Than 10 ft")
SHOT_ZONES = (
    "Restricted Area", "In The Paint (Non-RA)", "Mid-Range", "Corner 3",
    "Above the Break 3",
)
REGULAR_SEASON_TYPE = "Regular Season"
_GAME_TYPE_BY_ID_PREFIX = {
    "001": "Preseason",
    "002": REGULAR_SEASON_TYPE,
    "003": "All-Star",
    "004": "Playoffs",
    "005": "Play-In",
}


def _canonical_event_kind(game_id: str, provider_classification: str) -> str:
    """Classify an NBA game without importing the host application package."""

    if len(game_id) == 10 and game_id.isdigit():
        known = _GAME_TYPE_BY_ID_PREFIX.get(game_id[:3])
        if known is not None:
            return known
    return provider_classification


def _flatten_frame_columns(response: Any) -> Any:
    """Return a frame whose grouped provider columns have stable flat names."""

    if (
        pd is not None
        and isinstance(response, pd.DataFrame)
        and isinstance(response.columns, pd.MultiIndex)
    ):
        response = response.copy()
        response.columns = [
            "_".join(
                str(part).strip() for part in column
                if str(part).strip() and str(part).strip().lower() != "nan"
            )
            for column in response.columns
        ]
    return response


def _records(response: Any) -> list[dict[str, Any]]:
    if pd is not None and isinstance(response, pd.DataFrame):
        response = _flatten_frame_columns(response)
        return [dict(row) for row in response.to_dict(orient="records")]
    if isinstance(response, Mapping):
        result_sets = response.get("resultSets")
        if result_sets is not None:
            result_set = result_sets[0] if isinstance(result_sets, list) and result_sets else result_sets
            if isinstance(result_set, Mapping):
                headers = result_set.get("headers")
                rows = result_set.get("rowSet")
                if isinstance(headers, list) and isinstance(rows, list):
                    if all(isinstance(header, str) for header in headers):
                        response = [dict(zip(headers, row)) for row in rows if isinstance(row, (list, tuple))]
                    elif all(isinstance(header, Mapping) for header in headers):
                        # ``LeagueDashPlayerShotLocations`` uses a grouped
                        # header.  Flatten only the identity and the FGA
                        # values needed by the exact-zone contract.
                        category_header = next((header for header in headers if "columnNames" in header), {})
                        categories = list(category_header.get("columnNames") or [])
                        converted: list[dict[str, Any]] = []
                        for row in rows:
                            if not isinstance(row, (list, tuple)) or len(row) < 6:
                                raise ProviderContractError("provider_schema_changed")
                            item: dict[str, Any] = {
                                "PLAYER_ID": row[0], "PLAYER_NAME": row[1],
                                "TEAM_ID": row[2], "TEAM_ABBREVIATION": row[3],
                            }
                            for index, category in enumerate(categories):
                                position = 6 + index * 3 + 1
                                if position < len(row):
                                    item[category] = row[position]
                            converted.append(item)
                        response = converted
        if isinstance(response, Mapping):
            if isinstance(response.get("records"), list):
                response = response["records"]
            elif isinstance(response.get("data"), list):
                response = response["data"]
            else:
                response = [response]
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes, bytearray)):
        raise ProviderContractError("provider_schema_changed")
    result: list[dict[str, Any]] = []
    for row in response:
        if not isinstance(row, Mapping):
            raise ProviderContractError("provider_schema_changed")
        result.append({str(key): _plain(value) for key, value in row.items()})
    return result


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ProviderContractError("provider_timestamp_unaware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if pd is not None:
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except (AttributeError, ValueError, TypeError):
            pass
    return str(value)


def _value(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    lower = {str(key).strip().casefold(): value for key, value in row.items()}
    for name in names:
        if name.casefold() in lower:
            return lower[name.casefold()]
    return default


def _required(row: Mapping[str, Any], *names: str) -> Any:
    value = _value(row, *names)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ProviderContractError("provider_schema_changed")
    return value


def _text(value: Any, *, reason: str = "provider_schema_changed") -> str:
    if value is None:
        raise ProviderContractError(reason)
    text = str(value).strip()
    if not text:
        raise ProviderContractError(reason)
    return text


def _positive_id(value: Any, *, reason: str = "identity_unresolved") -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        number = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ProviderContractError(reason) from error
    if number <= 0:
        raise ProviderContractError(reason)
    return number


def _number(value: Any, *, nonnegative: bool = True, integer: bool = False) -> float | int:
    if isinstance(value, bool) or value is None:
        raise ProviderContractError("provider_schema_changed")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ProviderContractError("provider_schema_changed") from error
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise ProviderContractError("value_invariant_failed")
    if integer:
        if not number.is_integer():
            raise ProviderContractError("value_invariant_failed")
        return int(number)
    return number


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ProviderContractError("provider_schema_changed") from error
    if parsed.tzinfo is None:
        raise ProviderContractError("provider_timestamp_unaware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_season(season: str) -> str:
    if not isinstance(season, str) or len(season) != 7 or season[4] != "-":
        raise ProviderContractError("invalid_season")
    try:
        start, end = int(season[:4]), int(season[5:])
    except ValueError as error:
        raise ProviderContractError("invalid_season") from error
    if end != (start + 1) % 100:
        raise ProviderContractError("invalid_season")
    return season


def _provenance(*, provider: str = "nba", endpoint: str, scope: Any, records: int) -> dict[str, Any]:
    return {
        "provider": provider,
        "endpoint": endpoint,
        "scope": scope,
        "record_count": records,
        "normalizer_version": "residential-1",
    }


def normalize_schedule_response(
    response: Any,
    *,
    season: str,
    cutoff: datetime | str,
    complete_snapshot: bool = True,
) -> NormalizedObservation:
    """Normalize a whole-season Regular Season schedule.

    A schedule correction is represented by a new observation; duplicate game
    identities within one response are never silently deduplicated.
    """

    season = _canonical_season(season)
    rows = _records(response)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        game_id = _text(_value(row, "nba_game_id", "game_id", "GAME_ID", "gameId"), reason="identity_unresolved")
        classification_value = _value(
            row, "classification", "season_type", "SEASON_TYPE", "game_label", "gameLabel"
        )
        classification = (
            str(classification_value).strip()
            if classification_value is not None and str(classification_value).strip()
            else REGULAR_SEASON_TYPE
        )
        event_kind = _canonical_event_kind(game_id, classification)
        if event_kind.casefold() != REGULAR_SEASON_TYPE.casefold():
            # ScheduleLeagueV2 is a mixed-phase season feed. A canonical NBA
            # game ID safely identifies rows outside this catalog's governed
            # phase; an unknown identity with an explicit foreign phase is a
            # contract violation rather than something we can classify.
            if len(game_id) == 10 and game_id.isdigit():
                continue
            raise ProviderContractError("cross_phase_observation")
        if game_id in seen:
            raise ProviderContractError("duplicate_identity")
        seen.add(game_id)
        home = _positive_id(_value(row, "home_team_id", "HOME_TEAM_ID", "homeTeam_teamId"))
        away = _positive_id(_value(row, "away_team_id", "AWAY_TEAM_ID", "awayTeam_teamId"))
        if home == away:
            raise ProviderContractError("value_invariant_failed")
        scheduled = _timestamp(_required(row, "scheduled_at", "game_date", "GAME_DATE", "GAME_DATE_EST", "gameDateTimeUTC"))
        raw_status = _value(row, "status_code", "gameStatus", "status_text", "status", "GAME_STATUS_TEXT", "gameStatusText", default="scheduled")
        status = _text(raw_status)
        status_key = status.casefold()
        if status_key not in {"1", "2", "3", "scheduled", "final", "finished", "completed", "closed", "live", "in progress", "postponed", "cancelled", "canceled", "game over", "game finished"}:
            raise ProviderContractError("provider_schema_changed")
        status = {
            "1": "Scheduled",
            "2": "Live",
            "3": "Final",
            "finished": "Final",
            "completed": "Final",
            "closed": "Final",
            "game over": "Final",
            "game finished": "Final",
        }.get(status_key, status)
        event = {
            "nba_game_id": game_id,
            "home_team_id": home,
            "away_team_id": away,
            "scheduled_at": scheduled,
            "status": status,
            "phase": "Regular Season",
            "classification": "Regular Season",
        }
        is_postponed = _value(row, "is_postponed")
        postponed_status = _value(row, "postponed_status")
        postponement_evidence = _value(row, "postponement_evidence")
        if status_key == "postponed":
            is_postponed = True
            postponed_status = postponed_status or "postponed"
        if is_postponed is not None:
            if not isinstance(is_postponed, bool):
                raise ProviderContractError("provider_schema_changed")
            event["is_postponed"] = is_postponed
        if postponed_status not in (None, ""):
            event["postponed_status"] = _text(postponed_status)
        if postponement_evidence not in (None, "", {}, []):
            event["postponement_evidence"] = _plain(postponement_evidence)
        normalized.append(event)
    normalized.sort(key=lambda item: (item["scheduled_at"], item["nba_game_id"]))
    payload = {
        "events": normalized,
        "records": normalized,
        "complete_snapshot": bool(complete_snapshot),
        "coverage": {"phase": "Regular Season", "season": season, "game_count": len(normalized)},
    }
    return NormalizedObservation(
        observation_type="event_catalog",
        scope={"window": "whole_season", "phase": "Regular Season"},
        season=season,
        cutoff=_timestamp(cutoff),
        payload=payload,
        provenance=_provenance(endpoint="schedule", scope="whole_season", records=len(normalized)),
        complete=bool(complete_snapshot and normalized),
    )


def normalize_roster_response(
    response: Any,
    *,
    season: str,
    cutoff: datetime | str,
    complete_snapshot: bool = True,
) -> NormalizedObservation:
    """Normalize season-covered roster identities with team evidence."""

    season = _canonical_season(season)
    rows = _records(response)
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    start_year = int(season[:4])
    for row in rows:
        from_year = _value(row, "from_year", "FROM_YEAR")
        to_year = _value(row, "to_year", "TO_YEAR")
        try:
            if from_year is not None and int(from_year) > start_year:
                continue
            if to_year is not None and int(to_year) < start_year:
                continue
        except (TypeError, ValueError, OverflowError) as error:
            raise ProviderContractError("provider_schema_changed") from error
        raw_status = _value(row, "roster_status", "status", "ROSTERSTATUS", default="active")
        if isinstance(raw_status, (int, float)) and not isinstance(raw_status, bool):
            status = "active" if raw_status else "inactive"
        else:
            status = str(raw_status).strip().casefold()
        if status in {"historical", "retired", "inactive"}:
            continue
        if status not in {"active", "current"}:
            raise ProviderContractError("identity_unresolved")
        player_id = _positive_id(_value(row, "player_id", "PERSON_ID", "PLAYER_ID"))
        if player_id in seen:
            raise ProviderContractError("duplicate_identity")
        seen.add(player_id)
        name = _text(_value(row, "display_name", "PLAYER_NAME", "DISPLAY_FIRST_LAST"), reason="identity_unresolved")
        team_id = _positive_id(_value(row, "team_id", "TEAM_ID"))
        covered_season = _text(_value(row, "season", "season_coverage", default=season))
        if covered_season != season:
            raise ProviderContractError("manifest_scope_mismatch")
        coverage_ids = _value(row, "event_ids", "game_ids", "games")
        if isinstance(coverage_ids, (str, bytes, bytearray)) or not isinstance(coverage_ids, Sequence):
            # CommonAllPlayers has season coverage but no player-game ledger.
            # Keep that fact explicit without inventing player statistics; the
            # control-plane catalog validator requires a non-empty marker.
            coverage_ids = [f"season:{season}"]
        normalized.append({
            "player_id": player_id,
            "display_name": name,
            "team_id": team_id,
            "team_abbreviation": _value(row, "team_abbreviation", "TEAM_ABBREVIATION", "TEAM_ABBR"),
            "roster_status": "active",
            "status": "active",
            "season_coverage": season,
            "event_ids": [str(item) for item in coverage_ids if str(item).strip()],
        })
    normalized.sort(key=lambda item: item["player_id"])
    payload = {
        "identities": normalized,
        "records": normalized,
        "complete_snapshot": bool(complete_snapshot),
        "coverage": {"season": season, "identity_count": len(normalized)},
    }
    return NormalizedObservation(
        observation_type="athlete_catalog",
        scope={"window": "whole_season", "phase": "Regular Season"},
        season=season,
        cutoff=_timestamp(cutoff),
        payload=payload,
        provenance=_provenance(endpoint="roster", scope="whole_season", records=len(normalized)),
        complete=bool(complete_snapshot and normalized),
    )


def _stat_rows(
    response: Any,
    *,
    categories: Sequence[str],
    category_names: Sequence[str],
    observation_type: str,
    scope: Mapping[str, Any],
    season: str,
    cutoff: datetime | str,
    endpoint: str,
    required_identity: Sequence[str] = ("player_id",),
    category_default: str | None = None,
    required_categories: Sequence[str] | None = None,
) -> NormalizedObservation:
    rows = _records(response)
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    present: set[str] = set()
    required = tuple(required_categories or categories)
    if not set(required) <= set(categories):
        raise ProviderContractError("provider_category_changed")
    for row in rows:
        identity: dict[str, Any] = {}
        for field in required_identity:
            aliases = {
                "player_id": ("player_id", "PLAYER_ID", "person_id", "PERSON_ID"),
                "team_id": ("team_id", "TEAM_ID"),
                "team_abbreviation": ("team_abbreviation", "TEAM_ABBREVIATION", "TEAM_ABBR"),
            }.get(field, (field, field.upper()))
            identity[field] = _positive_id(_value(row, *aliases)) if field.endswith("_id") else _text(_value(row, *aliases))
        category = _text(_value(row, *category_names, default=category_default), reason="provider_schema_changed")
        if category not in categories:
            raise ProviderContractError("provider_category_changed")
        present.add(category)
        key = tuple(identity.get(field) for field in required_identity) + (category,)
        if key in seen:
            raise ProviderContractError("duplicate_identity")
        seen.add(key)
        stats: dict[str, Any] = {}
        for source, target in (
            (("gp", "GP"), "games_played"),
            (("poss", "POSS", "fga", "FGA"), "attempts"),
            (("pts", "PTS"), "points"),
            (("fga_frequency", "FGA_FREQUENCY"), "share"),
            (("fgm", "FGM"), "makes"),
            (("fga", "FGA"), "attempts"),
            (("fg2m", "FG2M"), "FG2M"),
            (("fg2a", "FG2A"), "FG2A"),
            (("fg3m", "FG3M"), "FG3M"),
            (("fg3a", "FG3A"), "FG3A"),
            (("poss", "POSS"), "POSS"),
            (("pts", "PTS"), "PTS"),
            (("min", "MIN", "minutes"), "minutes"),
        ):
            aliases = source if isinstance(source, tuple) else (source,)
            raw = _value(row, *aliases)
            if raw is not None:
                normalized_number = _number(raw)
                if target == "share" and normalized_number > 1:
                    raise ProviderContractError("value_invariant_failed")
                stats[target] = normalized_number
        if not stats:
            raise ProviderContractError("provider_schema_changed")
        stats.update(identity)
        stats["category"] = category
        # Makes never exceed attempts.  This handles both NBA and rehearsal
        # aliases without deriving or filling provider values.
        if "makes" in stats and "attempts" in stats and stats["makes"] > stats["attempts"]:
            raise ProviderContractError("value_invariant_failed")
        if any(
            made in stats and attempted in stats and stats[made] > stats[attempted]
            for made, attempted in (("FG2M", "FG2A"), ("FG3M", "FG3A"))
        ):
            raise ProviderContractError("value_invariant_failed")
        normalized.append(stats)
    normalized.sort(key=lambda item: tuple(str(item.get(key, "")) for key in (*required_identity, "category")))
    complete = set(required) <= present
    base = {
        "synergy_play_types": "play_types",
        "synergy_opponent": "play_types",
        "grouped_shot_types": "shot_types",
        "shot_types_opponent": "shot_types",
    }.get(observation_type, observation_type)
    payload = {
        "base": base,
        "records": normalized,
        "coverage": {
            "categories": sorted(present),
            "required_categories": list(required),
            "complete": complete,
            "scope": dict(scope),
        },
    }
    return NormalizedObservation(
        observation_type=observation_type,
        scope=scope,
        season=_canonical_season(season),
        cutoff=_timestamp(cutoff),
        payload=payload,
        provenance=_provenance(endpoint=endpoint, scope=scope, records=len(normalized)),
        complete=complete,
    )


def normalize_synergy_response(
    response: Any, *, season: str, cutoff: datetime | str,
    scope: Mapping[str, Any] | None = None,
) -> NormalizedObservation:
    scope = dict(scope or {"window": "season", "phase": "Regular Season"})
    if scope.get("window") != "season":
        raise ProviderContractError("provider_window_unsupported")
    requested = scope.get("play_type")
    required = (str(requested),) if requested is not None else None
    return _stat_rows(
        response, categories=PLAY_TYPES, category_names=("category", "play_type", "PLAY_TYPE"),
        observation_type="synergy_play_types", scope=scope, season=season,
        cutoff=cutoff, endpoint="synergy", required_identity=("player_id",),
        required_categories=required,
    )


def normalize_opponent_synergy_response(
    response: Any, *, season: str, cutoff: datetime | str,
    scope: Mapping[str, Any] | None = None,
) -> NormalizedObservation:
    scope = dict(scope or {
        "window": "season", "subject": "opponent", "phase": "Regular Season",
    })
    if scope.get("window") != "season":
        raise ProviderContractError("provider_window_unsupported")
    requested = scope.get("play_type")
    required = (str(requested),) if requested is not None else None
    observation = _stat_rows(
        response, categories=PLAY_TYPES, category_names=("category", "play_type", "PLAY_TYPE"),
        observation_type="synergy_opponent", scope=scope, season=season,
        cutoff=cutoff, endpoint="synergy_opponent", required_identity=("team_id",),
        required_categories=required,
    )
    _require_opponent_contract(
        observation, require_games_played=True, require_minutes=True,
    )
    return observation


def normalize_grouped_shot_response(
    response: Any, *, season: str, cutoff: datetime | str,
    scope: Mapping[str, Any] | None = None,
) -> NormalizedObservation:
    scope = dict(scope or {"window": "season", "subject": "player", "phase": "Regular Season"})
    if scope.get("window") not in {"season", "l15"}:
        raise ProviderContractError("provider_window_unsupported")
    requested = scope.get("category", scope.get("general_range"))
    required = (str(requested),) if requested is not None else None
    return _stat_rows(
        response, categories=SHOT_TYPES, category_names=("category", "shot_type", "SHOT_TYPE", "general_range"),
        observation_type="grouped_shot_types", scope=scope, season=season,
        cutoff=cutoff, endpoint="player_shot_types", required_identity=("player_id",),
        category_default=str(requested or "Catch and Shoot"),
        required_categories=required,
    )


def normalize_opponent_grouped_shot_response(
    response: Any, *, season: str, cutoff: datetime | str,
    team_id: int, window: str = "season", category: str | None = None,
    value_mode: str = "totals_with_minutes",
    endpoint_window: Mapping[str, Any] | None = None,
) -> NormalizedObservation:
    team_id = _positive_id(team_id)
    scope = {
        "window": window, "subject": "opponent", "team_id": team_id,
        "phase": "Regular Season", "value_mode": value_mode,
        "season": _canonical_season(season), "season_type": "Regular Season",
        "endpoint_window": dict(endpoint_window or {}),
    }
    if category is not None:
        scope["category"] = category
    required = (str(category),) if category is not None else None
    observation = _stat_rows(
        response, categories=SHOT_TYPES, category_names=("category", "shot_type", "SHOT_TYPE", "general_range"),
        observation_type="shot_types_opponent", scope=scope, season=season,
        cutoff=cutoff, endpoint="opponent_shot_types", required_identity=("team_id",),
        category_default=str(category or "Catch and Shoot"),
        required_categories=required,
    )
    _require_opponent_contract(
        observation, scoped_team_id=team_id, require_games_played=True,
        require_minutes=True,
    )
    return observation


def _zone_response(
    response: Any, *, season: str, cutoff: datetime | str,
    scope: Mapping[str, Any], endpoint: str, identity: Sequence[str],
    observation_type: str = "exact_shot_zones",
) -> NormalizedObservation:
    rows = _records(response)
    output: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        identity_values: dict[str, Any] = {}
        for field in identity:
            aliases = (field, field.upper())
            identity_values[field] = _positive_id(_value(row, *aliases)) if field.endswith("_id") else _text(_value(row, *aliases))
        values: dict[str, dict[str, Any]] = {}
        for zone in SHOT_ZONES:
            if observation_type == "exact_shot_zones":
                raw = _value(row, zone, zone.upper(), zone.replace(" ", "_"))
                if raw is None:
                    raise ProviderContractError("provider_schema_changed")
                values[zone] = {"value": _number(raw)}
                continue
            flattened = zone.replace(" ", "_")
            makes = _value(
                row, f"{zone}_OPP_FGM", f"{flattened}_OPP_FGM",
                f"{zone}_FGM", f"{flattened}_FGM",
            )
            attempts = _value(
                row, f"{zone}_OPP_FGA", f"{flattened}_OPP_FGA",
                f"{zone}_FGA", f"{flattened}_FGA",
            )
            if zone == "Corner 3":
                left_makes = _value(row, "Left Corner 3_OPP_FGM")
                left_attempts = _value(row, "Left Corner 3_OPP_FGA")
                right_makes = _value(row, "Right Corner 3_OPP_FGM")
                right_attempts = _value(row, "Right Corner 3_OPP_FGA")
                side_values = (
                    left_makes, left_attempts, right_makes, right_attempts
                )
                # The live endpoint reports the combined corner alongside its
                # left/right split; the two are the same evidence, so a
                # combined value is accepted only when the sides are absent or
                # sum to it exactly, and a lone partial split stays drift.
                if makes is not None or attempts is not None:
                    if makes is None or attempts is None:
                        raise ProviderContractError("provider_schema_changed")
                    if any(value is not None for value in side_values):
                        if not all(value is not None for value in side_values):
                            raise ProviderContractError("provider_schema_changed")
                        # Per-48 values arrive rounded to one decimal, so
                        # each side carries up to half a tenth of error.
                        if (
                            abs(_number(left_makes) + _number(right_makes) - _number(makes)) > 0.11
                            or abs(_number(left_attempts) + _number(right_attempts) - _number(attempts)) > 0.11
                        ):
                            raise ProviderContractError("value_invariant_failed")
                elif all(value is not None for value in side_values):
                    makes = _number(left_makes) + _number(right_makes)
                    attempts = _number(left_attempts) + _number(right_attempts)
                else:
                    raise ProviderContractError("provider_schema_changed")
            if makes is None or attempts is None:
                raise ProviderContractError("provider_schema_changed")
            values[zone] = {"FGM": _number(makes), "FGA": _number(attempts)}
            if values[zone]["FGM"] > values[zone]["FGA"]:
                raise ProviderContractError("value_invariant_failed")
        key = tuple(identity_values.values())
        if key in seen:
            raise ProviderContractError("duplicate_identity")
        seen.add(key)
        # The provider returns one wide row, while the registry consumes one
        # explicit Base/slice record. Keep all five values, but materialize
        # one registry row per zone so category coverage is inspectable.
        for zone in SHOT_ZONES:
            zone_record = dict(identity_values)
            games_played = _value(row, "games_played", "GP")
            if games_played is not None:
                zone_record["games_played"] = _number(
                    games_played, integer=True
                )
            zone_record.update({
                "base": "shot_zones",
                "category": zone,
                "slice_key": zone,
                **values[zone],
            })
            output.append(zone_record)
    output.sort(key=lambda item: tuple(str(item.get(field, "")) for field in (*identity, "category")))
    payload = {
        "base": "shot_zones",
        "records": output,
        "coverage": {"zones": list(SHOT_ZONES), "scope": dict(scope)},
    }
    return NormalizedObservation(
        observation_type=observation_type, scope=scope,
        season=_canonical_season(season), cutoff=_timestamp(cutoff), payload=payload,
        provenance=_provenance(endpoint=endpoint, scope=scope, records=len(output)),
        complete=bool(output),
    )


def normalize_zone_response(
    response: Any, *, season: str, cutoff: datetime | str,
    scope: Mapping[str, Any] | None = None,
) -> NormalizedObservation:
    scope = dict(scope or {"window": "season", "subject": "player", "phase": "Regular Season"})
    if scope.get("window") not in {"season", "l15"}:
        raise ProviderContractError("provider_window_unsupported")
    return _zone_response(response, season=season, cutoff=cutoff, scope=scope, endpoint="player_zones", identity=("player_id",))


def normalize_opponent_zone_response(
    response: Any, *, season: str, cutoff: datetime | str,
    team_id: int, window: str = "season", value_mode: str = "per48",
    endpoint_window: Mapping[str, Any] | None = None,
) -> NormalizedObservation:
    team_id = _positive_id(team_id)
    scope = {
        "window": window, "subject": "opponent", "team_id": team_id,
        "phase": "Regular Season", "value_mode": value_mode,
        "season": _canonical_season(season), "season_type": "Regular Season",
        "endpoint_window": dict(endpoint_window or {}),
    }
    observation = _zone_response(
        response, season=season, cutoff=cutoff, scope=scope,
        endpoint="opponent_zones", identity=("team_id",),
        observation_type="shot_zones_opponent",
    )
    _require_opponent_contract(
        observation, scoped_team_id=team_id, require_games_played=True
    )
    return observation


def _require_opponent_contract(
    observation: NormalizedObservation, *, scoped_team_id: int | None = None,
    require_games_played: bool = False, require_minutes: bool = False,
) -> None:
    """Require real endpoint identity/count facts before envelope creation."""

    records = observation.payload.get("records")
    if not isinstance(records, list) or not records:
        raise ProviderContractError("provider_schema_changed")
    for record in records:
        if not isinstance(record, Mapping):
            raise ProviderContractError("provider_schema_changed")
        if scoped_team_id is not None and record.get("team_id") != scoped_team_id:
            raise ProviderContractError("manifest_scope_mismatch")
        games_played = record.get("games_played")
        if games_played is None:
            if require_games_played:
                raise ProviderContractError("provider_window_unverified")
            continue
        if _number(games_played, integer=True) <= 0:
            raise ProviderContractError("value_invariant_failed")
        if require_minutes:
            minutes = record.get("minutes")
            if minutes is None or _number(minutes) <= 0:
                raise ProviderContractError("provider_window_unverified")


# Friendly aliases used by compatibility probes and release tests.
normalize_schedule = normalize_schedule_response
normalize_roster = normalize_roster_response
normalize_synergy = normalize_synergy_response
normalize_shot_type_response = normalize_grouped_shot_response
normalize_shot_zone_response = normalize_zone_response


__all__ = [
    "PLAY_TYPES", "SHOT_TYPES", "SHOT_ZONES",
    "normalize_grouped_shot_response", "normalize_opponent_grouped_shot_response",
    "normalize_opponent_zone_response", "normalize_roster_response",
    "normalize_schedule_response", "normalize_synergy_response",
    "normalize_opponent_synergy_response",
    "normalize_zone_response", "normalize_schedule", "normalize_roster",
    "normalize_synergy", "normalize_shot_type_response", "normalize_shot_zone_response",
]
