import re

import requests
from flask import Blueprint, jsonify, request

from ..errors import (
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
    route_error_boundary,
    sanitize_public_value,
)
from ..providers.nba_stats import validate_canonical_season
from ..utils.auth import require_admin, require_auth, require_auth_optional
from ._service_proxy import CurrentAppService

# Initialize blueprint and services
player_bp = Blueprint("players", __name__)

player_service = CurrentAppService("player")
player_jobs_service = CurrentAppService("data_refresh_jobs")


@player_bp.route("", methods=["GET"])
@require_auth_optional
@route_error_boundary("Failed to retrieve players.")
def get_players():
    players = player_service.get_all_players()
    return jsonify(players)


@player_bp.route("/profile", methods=["GET"])
@require_auth_optional
@route_error_boundary("Failed to retrieve the player profile.")
def get_player_profile():
    player = request.args.get("player_name")
    category = request.args.get("category")
    opp_team = request.args.get("opp_team")

    if not player or not category:
        raise InvalidInputError("player_name and category are required.")

    try:
        profile_data = player_service.get_player_profile(player, category, opp_team)
        if profile_data is None:
            raise ResourceNotFoundError(
                "The requested player profile was not found.",
                detail=f"player_name={player!r}, category={category!r}",
            )
        return jsonify(profile_data)
    except requests.exceptions.RequestException as error:
        raise ProviderUnavailableError(detail=error) from error


@player_bp.route("/fetch", methods=["PUT"])
@require_admin
@route_error_boundary("Failed to schedule the player data refresh.")
def fetch_players():
    job_state = player_jobs_service.start("fetch_players")
    return jsonify(job_state), 202


@player_bp.route("/next-opponent", methods=["GET"])
@require_auth
@route_error_boundary("Failed to retrieve the next opponent.")
def get_next_opponent():
    names = request.args.getlist("player_name")
    if set(request.args) != {"player_name"} or len(names) != 1 or not names[0].strip():
        raise InvalidInputError("player_name is required.")
    return jsonify(CurrentAppService("next_opponent").get_next_opponent(names[0]))


_SEASON_STATS_PARAMETERS = {"season", "player_id"}
_PLAYER_ID = re.compile(r"[0-9]{1,10}")


def _invalid_season_stats(*parameters: str) -> InvalidInputError:
    return InvalidInputError(
        f"The season stats parameters are invalid: {', '.join(parameters)}.",
        public_details={"parameters": list(parameters)},
    )


@player_bp.route("/season_stats", methods=["GET"])
@require_auth
@route_error_boundary("Failed to retrieve the season stats.")
def get_season_stats():
    """Stored regular-season totals, per-game averages and shooting percentages."""
    unknown = sorted(set(request.args) - _SEASON_STATS_PARAMETERS)
    if unknown:
        raise _invalid_season_stats(*(sanitize_public_value(name) for name in unknown))
    for name in request.args:
        if len(request.args.getlist(name)) != 1:
            raise _invalid_season_stats(name)
    season = request.args.get("season")
    try:
        if season is None or validate_canonical_season(season) != season:
            raise ValueError(season)
    except ValueError:
        raise _invalid_season_stats("season") from None
    player_id = None
    if "player_id" in request.args:
        if _PLAYER_ID.fullmatch(request.args["player_id"]) is None:
            raise _invalid_season_stats("player_id")
        player_id = int(request.args["player_id"])
    return jsonify(CurrentAppService("player_season_stats").get_season_stats(season, player_id))
