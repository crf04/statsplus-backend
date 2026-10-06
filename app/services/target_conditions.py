"""Conditions on an opponent's games, shared by saved and draft Targets."""

from datetime import date
from math import isfinite

from app.errors import InvalidInputError


def date_is_kept(conditions, game_date):
    if not conditions:
        return True
    day = game_date.isoformat() if isinstance(game_date, date) else game_date
    return (not conditions.get("from") or day >= conditions["from"]) and (
        not conditions.get("to") or day <= conditions["to"]
    )


def minutes_are_kept(defender, minutes):
    return (
        minutes < defender["minutes"]
        if defender["comparator"] == "under"
        else minutes >= defender["minutes"]
    )


def player_minutes_are_kept(minutes, threshold):
    """Return whether one player's appearance clears a strict threshold.

    Game-log minutes are provider data, so a missing or non-finite value is
    not evidence that the player cleared an enabled condition.
    """

    try:
        value = float(minutes)
    except (TypeError, ValueError):
        return False
    return isfinite(value) and value > threshold


def require_defender_fielded(player_logs, defender, opponent, season, *, publication_snapshot=None):
    """Refuse a defender the opponent never fielded in ``season``'s game logs.

    ``publication_snapshot`` is the game-log evidence the Backtest reads, so a
    defender is checked in exactly the Generation its minutes come from.
    """

    from app.domain.nba_teams import NBA_TEAM_TRICODE_TO_ID
    from app.services.publication_snapshot_calls import call_with_read_scope

    rows = call_with_read_scope(
        player_logs.list_player_rows,
        season,
        defender["player_id"],
        publication_snapshot=publication_snapshot,
    )
    if not any(
        row.team_id == NBA_TEAM_TRICODE_TO_ID[opponent]
        and row.season_type == "Regular Season"
        for row in rows
    ):
        raise InvalidInputError(
            "The defender must appear in the opponent's season game logs."
        )


def validate_conditions(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidInputError("Conditions must be an object or null.")
    result = {
        "defender": value.get("defender"),
        "from": value.get("from"),
        "to": value.get("to"),
        "player_minutes": value.get("player_minutes"),
    }
    for key in ("from", "to"):
        day = result[key]
        if day is not None:
            try:
                if (
                    not isinstance(day, str)
                    or date.fromisoformat(day).isoformat() != day
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise InvalidInputError("Condition dates must be YYYY-MM-DD.") from None
    if result["from"] and result["to"] and result["from"] > result["to"]:
        raise InvalidInputError("Condition start date must not follow its end date.")
    defender = result["defender"]
    if defender is not None:
        if not isinstance(defender, dict):
            raise InvalidInputError("A defender Condition must be an object.")
        player_id, minutes = defender.get("player_id"), defender.get("minutes")
        if type(player_id) is not int or player_id <= 0:
            raise InvalidInputError("A defender needs a canonical player id.")
        if type(minutes) is not int or not 0 <= minutes <= 48:
            raise InvalidInputError("Defender minutes must be an integer from 0 to 48.")
        if defender.get("comparator") not in ("under", "at_least"):
            raise InvalidInputError("Defender comparator must be under or at_least.")
        result["defender"] = {
            key: defender[key] for key in ("player_id", "comparator", "minutes")
        }
    player_minutes = result.get("player_minutes")
    if player_minutes is not None:
        if type(player_minutes) is not int or not 0 <= player_minutes <= 48:
            raise InvalidInputError(
                "Player minutes must be an integer from 0 to 48."
            )
    return result
