"""Authenticated slate and game-log HTTP adapters.

The slate route delegates one optional ET date to the persisted-schedule
service. The game-log route parses query parameters into one typed
:class:`GameLogQuery` and calls :meth:`GameService.get_filtered_logs`: Flask
serves requests with worker threads, so no event loop is created per request
(#10).  Malformed filters raise a 400 ``invalid_input`` response; provider
timeouts keep the documented 503 ``provider_unavailable`` contract.
"""

import re
from typing import Any

import requests
from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from ..errors import (
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
    redact_public_value,
    sanitize_public_value,
)
from ..models.game_logs import GameLogFilterError, GameLogQuery
from ..utils.auth import require_auth
from ._service_proxy import CurrentAppService


# Initialize blueprint and services
game_bp = Blueprint('games', __name__)
#: Matchups with no Slate game (crf04/statsplus#95), served at
#: ``/api/matchups``; they share this module's Matchup parameter rules.
matchups_bp = Blueprint("matchups", __name__)


game_service = CurrentAppService("game")
slate_service = CurrentAppService("slate")
matchup_service = CurrentAppService("matchup")
matchup_selection_service = CurrentAppService("matchup_selection")

_MATCHUP_PARAMETERS = frozenset({"game_id"})
_SELECTION_PARAMETERS = frozenset({"game_id", "player_id"})
_CANONICAL_PLAYER_ID = re.compile(r"[1-9][0-9]*\Z")
_MAX_CANONICAL_PLAYER_ID = (1 << 63) - 1
_UNSCHEDULED_SELECTORS = ("player_id", "player_name", "team")
_UNSCHEDULED_PARAMETERS = frozenset(
    {*_UNSCHEDULED_SELECTORS, "player_team", "opponent"}
)
_TRICODE = re.compile(r"[A-Za-z]{3}\Z")


def _is_canonical_player_id(value: str) -> bool:
    return (
        _CANONICAL_PLAYER_ID.fullmatch(value) is not None
        and len(value) <= 19
        and int(value) <= _MAX_CANONICAL_PLAYER_ID
    )


@game_bp.route('/slate', methods=['GET'])
@require_auth
def get_slate():
    """Return the persisted current-season slate for one ET calendar date."""
    return jsonify(slate_service.get_slate(request.args.get("date")))


@game_bp.route("/matchup", methods=["GET"])
@require_auth
def get_matchup():
    """Return one stored matchup document for a canonical NBA game."""
    game_ids = request.args.getlist("game_id")
    if (
        set(request.args) != _MATCHUP_PARAMETERS
        or len(game_ids) != 1
        or not game_ids[0]
        or game_ids[0] != game_ids[0].strip()
    ):
        raise InvalidInputError("The matchup parameters are invalid.")
    return jsonify(matchup_service.get_matchup(game_id=game_ids[0]))


@game_bp.route("/matchup/selection", methods=["GET"])
@require_auth
def get_matchup_selection():
    """Return the stored-log tables for one canonical matchup selection."""
    if set(request.args) != _SELECTION_PARAMETERS:
        raise InvalidInputError("The matchup selection parameters are invalid.")
    game_ids = request.args.getlist("game_id")
    player_ids = request.args.getlist("player_id")
    if (
        len(game_ids) != 1
        or not game_ids[0]
        or game_ids[0] != game_ids[0].strip()
        or len(player_ids) != 1
        or not _is_canonical_player_id(player_ids[0])
    ):
        raise InvalidInputError("The matchup selection parameters are invalid.")
    return jsonify(
        matchup_selection_service.get_selection(
            game_id=game_ids[0],
            player_id=int(player_ids[0]),
        )
    )


def _invalid_unscheduled(*parameters: str) -> InvalidInputError:
    names = ", ".join(parameters)
    return InvalidInputError(
        f"The unscheduled matchup parameters are invalid: {names}.",
        public_details={"parameters": list(parameters)},
    )


@matchups_bp.route("/unscheduled", methods=["GET"])
@require_auth
def get_unscheduled_matchup():
    """Score a player, or a team's players, against one opponent, no game."""
    unknown = sorted(set(request.args) - _UNSCHEDULED_PARAMETERS)
    if unknown:
        # Only names from the documented vocabulary are echoed back.
        raise _invalid_unscheduled(*(sanitize_public_value(name) for name in unknown))
    selectors = [name for name in _UNSCHEDULED_SELECTORS if name in request.args]
    if len(selectors) != 1:
        raise _invalid_unscheduled(*(selectors or _UNSCHEDULED_SELECTORS))
    (selector,) = selectors
    if "player_team" in request.args and selector != "player_name":
        # player_team only narrows a name; an id or a team needs no narrowing.
        raise _invalid_unscheduled("player_team")
    values = {
        name: request.args.getlist(name)
        for name in (selector, "opponent", "player_team")
        if name in request.args or name != "player_team"
    }
    for name, submitted in values.items():
        if len(submitted) != 1:
            raise _invalid_unscheduled(name)
    value = values[selector][0]
    opponent = values["opponent"][0]
    if _TRICODE.fullmatch(opponent) is None:
        raise _invalid_unscheduled("opponent")
    opponent = opponent.upper()
    selected: dict[str, Any] = {
        "player_id": None,
        "player_name": None,
        "player_team": None,
        "team": None,
    }
    if selector == "player_id":
        if not _is_canonical_player_id(value):
            raise _invalid_unscheduled("player_id")
        selected["player_id"] = int(value)
    elif selector == "player_name":
        if not value.strip():
            raise _invalid_unscheduled("player_name")
        selected["player_name"] = value
        if "player_team" in values:
            player_team = values["player_team"][0]
            if _TRICODE.fullmatch(player_team) is None:
                raise _invalid_unscheduled("player_team")
            selected["player_team"] = player_team.upper()
    else:
        if _TRICODE.fullmatch(value) is None:
            raise _invalid_unscheduled("team")
        selected["team"] = value.upper()
        if selected["team"] == opponent:
            raise _invalid_unscheduled("opponent")
    return jsonify(
        matchup_service.get_unscheduled_matchup(opponent=opponent, **selected)
    )


def _default_season() -> str:
    """The season used when a request omits ``season_filter``."""
    return game_service.settings.nba.current_season


def _parse_game_log_filters() -> tuple[str, GameLogQuery]:
    """Parse game-log query parameters into one typed :class:`GameLogQuery`."""

    player_name = request.args.get("player_name")
    if not player_name:
        raise InvalidInputError("player_name is required.")

    filters = {
        "season_filter": request.args.get("season_filter", _default_season()),
        "minutes_filter": request.args.get("minutes_filter", "0,48"),
        "players_on": request.args.getlist("players_on[]"),
        "players_off": request.args.getlist("players_off[]"),
        "date_filter": request.args.get("date_filter"),
        "date_to": request.args.get("date_to"),
        "teams_against": request.args.getlist("teams_against[]"),
        "rank_filter": request.args.getlist("rank_filter[]"),
        "opponent_tricode": request.args.get("opponent_tricode"),
        "location_filter": request.args.get("location_filter", "Both"),
        "game_filter": request.args.get("game_filter"),
        "playstyle_range": [
            request.args.get("playstyle_RTG_min", "0"),
            request.args.get("playstyle_RTG_max", "200"),
        ],
        # Keep repeated ``self_filters[STAT]`` parameters as ordered pairs;
        # collapsing them into a dict would silently drop same-stat bounds.
        "self_filters": [
            (key[len("self_filters[") : -1], value)
            for key, value in request.args.items(multi=True)
            if key.startswith("self_filters[") and key.endswith("]")
        ],
    }

    try:
        return player_name, GameLogQuery(**filters)
    except ValidationError as error:
        raise InvalidInputError(
            "One or more game log filters are invalid.",
            detail=error,
            public_details=_game_log_validation_details(error, filters, request.args),
        ) from error


#: Scalar parameters whose grammar is pydantic's own, which normalizes the
#: value before any check sees it. The name decides two things at the HTTP
#: seam, both publishing the original submitted string rather than the
#: normalized one:
#:
#: - a pydantic-native rejection of one of them, which no typed cause owns,
#:   is translated into facts naming the parameter and that string; and
#: - a typed ``GameLogFilterError`` naming one of them (``date_to`` earlier
#:   than ``date_filter``) reports that string instead of the model's
#:   normalized value.
#:
#: Acceptance is untouched either way.
_PYDANTIC_SCALAR_PARAMETER_NAMES = frozenset(
    {"date_filter", "date_to", "location_filter", "game_filter"}
)


def _playstyle_range_failures(
    filters: dict[str, Any],
    args: Any,
) -> list[dict[str, str]]:
    """Facts for an inverted playstyle range, per submitted bound.

    The range spans two query parameters. Only the ones the caller actually
    submitted are named with the bound as it arrived, so a default the
    caller never sent is never blamed for a rejection.
    """

    low, high = filters["playstyle_range"]
    bounds = (
        ("playstyle_RTG_min", low),
        ("playstyle_RTG_max", high),
    )
    return [
        {
            "parameter": parameter,
            # The filters hold exactly the queried bound (the omitted one
            # replaced by its documented default), as submitted strings.
            "values": [sanitize_public_value(str(bound))],
        }
        for parameter, bound in bounds
        if args.getlist(parameter)
    ]


def _redacted_split_values(
    fragments: list[str],
    complete_values: list[str],
) -> list[str]:
    """Publish fragments that keep the sanitization context of the whole value.

    Validators report the bounds or parts of one submitted value
    individually, and splitting on ``,`` can break the context a credential
    pattern needs to match (a quoted value spanning the split). When a
    fragment came from a complete value the sanitizer redacts, the
    sanitized complete value is published instead; ordinary invalid
    fragments -- including long ones the sanitizer only truncates -- are
    untouched, because redaction and length bounding are compared apart.
    """

    published = []
    for fragment in fragments:
        complete = next(
            (
                candidate
                for candidate in complete_values
                if fragment in candidate
                and redact_public_value(candidate) != candidate
            ),
            None,
        )
        published.append(
            sanitize_public_value(complete)
            if complete is not None
            else sanitize_public_value(fragment)
        )
    return list(dict.fromkeys(published))


def _game_log_rejected_filter(
    cause: GameLogFilterError | None,
    validation_error: dict[str, Any],
    filters: dict[str, Any],
    args: Any,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """One rejected filter as published facts, or none if not safely known."""

    if isinstance(cause, GameLogFilterError):
        if cause.parameter == "playstyle_RTG_range":
            # Internal two-parameter marker, never published as a name:
            # report the bounds the caller actually submitted.
            return _playstyle_range_failures(filters, args)
        submitted = (
            filters.get(cause.parameter)
            if cause.parameter in _PYDANTIC_SCALAR_PARAMETER_NAMES
            else None
        )
        if isinstance(submitted, str):
            # A pydantic-parsed scalar (``date_to`` earlier than
            # ``date_filter``) reaches the model normalized: an epoch or
            # other lax spelling would be reported as a date the caller
            # never sent, so publish the value exactly as submitted.
            values = [sanitize_public_value(submitted)]
        else:
            values = _redacted_split_values(
                list(cause.values),
                [cause.context] if isinstance(cause.context, str) else [],
            )
        facts = {
            # Caller-supplied content can appear inside a parameter name
            # (a self_filter's stat), so it is redacted like the values.
            "parameter": sanitize_public_value(cause.parameter),
            "values": values,
        }
        if cause.supported_values is not None:
            facts["supported_values"] = list(cause.supported_values)
        if cause.supported_aliases is not None:
            facts["supported_aliases"] = list(cause.supported_aliases)
        return facts

    # A parser pydantic owns (``date_filter``, ``date_to``,
    # ``location_filter``) has no
    # typed cause, but the route holds exactly the value the caller
    # submitted and it is a scalar string, so publish that. Anything still
    # unknown is skipped, keeping the other rejected filters' facts.
    field = validation_error.get("loc", ())
    field = field[0] if field and isinstance(field[0], str) else None
    submitted = (
        filters.get(field)
        if field in _PYDANTIC_SCALAR_PARAMETER_NAMES
        else None
    )
    if isinstance(submitted, str):
        return {
            "parameter": field,
            "values": [sanitize_public_value(submitted)],
        }
    return None


def _game_log_validation_details(
    error: ValidationError,
    filters: dict[str, Any],
    args: Any,
) -> dict[str, Any] | None:
    """The failed game-log parameters a caller can act on, or none.

    Each rejection is translated separately: typed
    :class:`GameLogFilterError` causes name the parameter, the unusable
    submitted values, and where the service owns the vocabulary, the
    canonical accepted values. A failure with no safely actionable detail
    -- an unknown internal one -- is skipped rather than blanking the
    whole payload, and none of them publishes Pydantic context or input:
    only redacted, bounded scalars reach ``details``.
    """

    failed_filters = []
    for validation_error in error.errors():
        cause = (validation_error.get("ctx") or {}).get("error")
        facts = _game_log_rejected_filter(cause, validation_error, filters, args)
        if isinstance(facts, list):
            failed_filters.extend(facts)
        elif facts is not None:
            failed_filters.append(facts)
    if not failed_filters:
        return None
    return {"filters": failed_filters}


@game_bp.route('/game_logs', methods=['GET'])
@require_auth
def get_game_logs():
    player_name, query = _parse_game_log_filters()
    try:
        result = game_service.get_filtered_logs(player_name, query)
    except requests.exceptions.Timeout as error:
        raise ProviderUnavailableError(
            "The upstream stats provider timed out. Please try again shortly.",
            detail=error,
        ) from error
    except requests.exceptions.RequestException as error:
        raise ProviderUnavailableError(detail=error) from error
    except ResourceNotFoundError:
        raise
    except ValueError as error:
        if "No matching player found" in str(error):
            raise ResourceNotFoundError(
                "The requested player was not found.", detail=error
            ) from error
        raise InvalidInputError("The game log request is invalid.", detail=error) from error
    return jsonify(result)
