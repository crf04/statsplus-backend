from functools import partial

from flask import Blueprint, jsonify

from ..errors import route_error_boundary
from ..services.data_service import DataService
from ..services.job_service import DataRefreshJobService
from ..utils.auth import require_admin
from ..utils.telemetry import snapshot_metrics, snapshot_recent_events
from ._service_proxy import CurrentAppService

# Initialize blueprint and services
data_bp = Blueprint('data', __name__)


def _build_data_service(engine, settings):
    return DataService(engine, settings=settings)


def _build_job_service(engine, settings):
    return DataRefreshJobService(engine)


data_service = CurrentAppService("data", _build_data_service)
data_jobs_service = CurrentAppService("data_jobs", _build_job_service)


@data_bp.route('/update_database', methods=['POST'])
@require_admin
@route_error_boundary("Failed to schedule the database update.")
def update_database():
    job_state = data_jobs_service.start("update_database", data_service.update_all_data)
    return jsonify(job_state), 202

@data_bp.route('/player_PBP', methods=['PUT'])
@require_admin
@route_error_boundary("Failed to schedule the player PBP data refresh.")
def store_player_PBP():
    job_state = data_jobs_service.start(
        "player_pbp", partial(data_service.fetch_PBP_data, "player")
    )
    return jsonify(job_state), 202

@data_bp.route('/opponent_PBP', methods=['PUT'])
@require_admin
@route_error_boundary("Failed to schedule the opponent PBP data refresh.")
def store_opponent_PBP():
    job_state = data_jobs_service.start(
        "opponent_pbp", partial(data_service.fetch_PBP_data, "opponent")
    )
    return jsonify(job_state), 202

@data_bp.route('/fetch_players_with_teams', methods=['POST'])
@require_admin
@route_error_boundary("Failed to schedule the player team refresh.")
def fetch_players_with_teams():
    job_state = data_jobs_service.start(
        "fetch_players_with_teams", data_service.fetch_players_with_teams
    )
    return jsonify(job_state), 202

@data_bp.route('/jobs/<job_id>', methods=['GET'])
@require_admin
@route_error_boundary("Failed to retrieve the job status.")
def get_job(job_id):
    return jsonify(data_jobs_service.get(job_id))

@data_bp.route('/fetch_playtypes', methods=['GET'])
@require_admin
@route_error_boundary("Failed to fetch play types.")
def fetch_playtypes():
    return jsonify(data_service.get_playtypes())

@data_bp.route('/telemetry', methods=['GET'])
@require_admin
@route_error_boundary("Failed to fetch provider telemetry.")
def provider_telemetry():
    """Return bounded, sanitized provider and application telemetry counters.

    Provider failures are counted at the provider seams and application
    failures by the central error handler; neither list ever carries
    credentials, URLs, bodies, or exception text.
    """
    body = snapshot_metrics()
    body["recent_provider_events"] = snapshot_recent_events(limit=50)
    return jsonify(body)