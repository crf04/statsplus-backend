import asyncio
import requests
from flask import Blueprint, request

from ..errors import (
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
)
from ..services.game_service import GameService
from ..utils.auth import require_auth
from ._service_proxy import CurrentAppService


# Initialize blueprint and services
game_bp = Blueprint('games', __name__)


def _build_game_service(engine, settings):
    return GameService(engine, settings=settings)


game_service = CurrentAppService("game", _build_game_service)


def _parse_game_log_filters() -> tuple[str, dict]:
    """Parse game-log query parameters into the service contract."""

    player_name = request.args.get("player_name")
    if not player_name:
        raise InvalidInputError("player_name is required.")

    try:
        minutes_values = request.args.get("minutes_filter", "0,48").split(",")
        if len(minutes_values) != 2:
            raise ValueError("minutes_filter must contain two values")

        filter_params = {
            "minutes_filter": tuple(map(int, minutes_values)),
            "players_on": request.args.getlist("players_on[]"),
            "players_off": request.args.getlist("players_off[]"),
            "date_filter": request.args.get("date_filter"),
            "teams_against": request.args.getlist("teams_against[]"),
            "rank_filter": request.args.getlist("rank_filter[]"),
            "location_filter": request.args.get("location_filter", "Both"),
            "game_filter": request.args.get("game_filter"),
            "season_filter": request.args.get(
                "season_filter", game_service.settings.nba.current_season
            ),
            "playstyle_range": [
                float(request.args.get("playstyle_RTG_min", "0")),
                float(request.args.get("playstyle_RTG_max", "200")),
            ],
            "self_filters": {
                key[13:-1]: list(map(float, value.split(",")))
                for key, value in request.args.items()
                if key.startswith("self_filters[") and key.endswith("]")
            },
        }
    except (TypeError, ValueError) as error:
        raise InvalidInputError(
            "One or more game log filters are invalid.", detail=error
        ) from error

    return player_name, filter_params


@game_bp.route('/game_logs', methods=['GET'])
@require_auth
def get_game_logs():
    try:
        player_name, filter_params = _parse_game_log_filters()
        return asyncio.run(game_service.get_filtered_logs(player_name, filter_params))
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
