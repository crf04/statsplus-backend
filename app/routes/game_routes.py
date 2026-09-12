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
    sanitize_public_value,
)
from ..models.game_logs import GameLogFilterError, GameLogQuery
from ..utils.auth import require_auth
from ._service_proxy import CurrentAppService


# Initialize blueprint and services
game_bp = Blueprint('games', __name__)


game_service = CurrentAppService("game")
slate_service = CurrentAppService("slate")
matchup_service = CurrentAppService("matchup")
matchup_selection_service = CurrentAppService("matchup_selection")

_MATCHUP_PARAMETERS = frozenset({"game_id"})
_SELECTION_PARAMETERS = frozenset({"game_id", "player_id"})
_CANONICAL_PLAYER_ID = re.compile(r"[1-9][0-9]*\Z")
_MAX_CANONICAL_PLAYER_ID = (1 << 63) - 1


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
        or _CANONICAL_PLAYER_ID.fullmatch(player_ids[0]) is None
        or len(player_ids[0]) > 19
        or int(player_ids[0]) > _MAX_CANONICAL_PLAYER_ID
    ):
        raise InvalidInputError("The matchup selection parameters are invalid.")
    return jsonify(
        matchup_selection_service.get_selection(
            game_id=game_ids[0],
            player_id=int(player_ids[0]),
        )
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
            public_details=_game_log_validation_details(error, filters),
        ) from error


#: Parsers the typed models do not own, translated from one pydantic-native
#: rejection at the HTTP seam, the original submitted value in hand. Their
#: accepted grammar is pydantic's, so acceptance is untouched.
_UNTYPED_PARAMETER_NAMES = frozenset(
    {"date_filter", "location_filter", "game_filter"}
)


def _game_log_rejected_filter(
    cause: GameLogFilterError | None,
    validation_error: dict[str, Any],
    filters: dict[str, Any],
) -> dict[str, Any] | None:
    """One rejected filter as published facts, or none if not safely known."""

    if isinstance(cause, GameLogFilterError):
        facts = {
            "parameter": cause.parameter,
            "values": [sanitize_public_value(value) for value in cause.values],
        }
        if cause.supported_values is not None:
            facts["supported_values"] = list(cause.supported_values)
        if cause.supported_aliases is not None:
            facts["supported_aliases"] = list(cause.supported_aliases)
        return facts

    # A parser pydantic owns (``date_filter``, ``location_filter``) has no
    # typed cause, but the route holds exactly the value the caller
    # submitted and it is a scalar string, so publish that. Anything still
    # unknown is skipped, keeping the other rejected filters' facts.
    field = validation_error.get("loc", ())
    field = field[0] if field and isinstance(field[0], str) else None
    submitted = filters.get(field) if field in _UNTYPED_PARAMETER_NAMES else None
    if isinstance(submitted, str):
        return {
            "parameter": field,
            "values": [sanitize_public_value(submitted)],
        }
    return None


def _game_log_validation_details(
    error: ValidationError,
    filters: dict[str, Any],
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
        facts = _game_log_rejected_filter(cause, validation_error, filters)
        if facts is not None:
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
