"""Conditions on an opponent's games, shared by saved and draft Targets."""

from datetime import date

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


def validate_conditions(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidInputError("Conditions must be an object or null.")
    result = {
        "defender": value.get("defender"),
        "from": value.get("from"),
        "to": value.get("to"),
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
    return result
