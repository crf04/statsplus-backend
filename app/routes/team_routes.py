from flask import Blueprint, request, jsonify
from ..errors import AppError, InvalidInputError, OperationFailedError, ResourceNotFoundError
from ..services.team_service import TeamService
from ..utils.auth import require_auth_optional
from ._service_proxy import CurrentAppService

# Initialize blueprint and services
team_bp = Blueprint('teams', __name__)


def _build_team_service(engine, settings):
    return TeamService(engine, settings=settings)


team_service = CurrentAppService("team", _build_team_service)

@team_bp.route('/stats', methods=['GET'])
@require_auth_optional
def get_team_stats():
    try:
        category = request.args.get('category')
        team = request.args.get('team')
        date = request.args.get('date')

        if not category or not team:
            raise InvalidInputError("team and category are required.")
        
        team_stats = team_service.get_team_stats(category, team, date)
        if not team_stats:
            raise ResourceNotFoundError(
                "No data found for the specified team and category."
            )
        return jsonify(team_stats)
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError("Failed to retrieve team stats.", detail=error) from error

@team_bp.route('', methods=['GET'])
@require_auth_optional
def get_teams():
    try:
        teams = team_service.get_all_teams()
        return jsonify(teams)
    except AppError:
        raise
    except Exception as error:
        raise OperationFailedError("Failed to retrieve teams.", detail=error) from error
