"""Game-log HTTP adapter.

The route parses query parameters into one typed :class:`GameLogQuery` and
calls the synchronous :meth:`GameService.get_filtered_logs` directly: Flask
serves requests with worker threads, so no event loop is created per request
(#10).  Malformed filters raise a 400 ``invalid_input`` response; provider
timeouts keep the documented 503 ``provider_unavailable`` contract.
"""

import requests
from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from ..errors import (
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from ..models.game_logs import GameLogQuery
from ..utils.auth import require_auth
from ._service_proxy import CurrentAppService


# Initialize blueprint and services
game_bp = Blueprint('games', __name__)


game_service = CurrentAppService("game")
slate_service = CurrentAppService("slate")


@game_bp.route('/slate', methods=['GET'])
@require_auth
def get_slate():
    """Return the persisted current-season slate for one ET calendar date."""
    return jsonify(slate_service.get_slate(request.args.get("date")))


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
            "One or more game log filters are invalid.", detail=error
        ) from error


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
