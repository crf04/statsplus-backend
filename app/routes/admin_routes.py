"""Admin routes for the beta sign-up switch, the waitlist and research activity."""

import re
from datetime import date

from flask import Blueprint, jsonify, request

from app.errors import InvalidInputError, route_error_boundary
from app.models.user import ACCESS_ACTIVE, ACCESS_WAITLISTED
from app.services.research_activity import report_id
from app.utils.auth import require_admin

from ._service_proxy import CurrentAppService

admin_bp = Blueprint("admin", __name__)

user_service = CurrentAppService("user")
research_activity_service = CurrentAppService("research_activity")

_LISTABLE_STATUSES = (ACCESS_WAITLISTED, ACCESS_ACTIVE)


@admin_bp.route("/signups", methods=["GET"])
@require_admin
@route_error_boundary("Failed to read the sign-up setting.")
def get_signups():
    return jsonify({"open": user_service.get_signups_open()})


@admin_bp.route("/signups", methods=["PUT"])
@require_admin
@route_error_boundary("Failed to update the sign-up setting.")
def put_signups():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("open"), bool):
        raise InvalidInputError('Body must be {"open": true|false}.')
    return jsonify({"open": user_service.set_signups_open(body["open"])})


@admin_bp.route("/users", methods=["GET"])
@require_admin
@route_error_boundary("Failed to list users.")
def list_users():
    status = request.args.get("status", ACCESS_WAITLISTED)
    if status not in _LISTABLE_STATUSES:
        raise InvalidInputError("status must be 'waitlisted' or 'active'.")
    return jsonify({"users": user_service.list_users_by_access_status(status)})


@admin_bp.route("/users/<uid>/admit", methods=["POST"])
@require_admin
@route_error_boundary("Failed to admit the user.")
def admit_user(uid):
    return jsonify({"uid": uid, "status": user_service.admit_user(uid)})


@admin_bp.route("/users/<uid>/cohort", methods=["PUT"])
@require_admin
@route_error_boundary("Failed to update the cohort.")
def put_cohort(uid):
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "cohort" not in body:
        raise InvalidInputError(
            'Body must be {"cohort": "internal" | "friend" | "friend_of_friend" | null}.'
        )
    return jsonify(
        {
            "uid": uid,
            "cohort": research_activity_service.set_cohort(uid, body["cohort"]),
            "report_id": report_id(uid),
        }
    )


@admin_bp.route("/research-activity", methods=["GET"])
@require_admin
@route_error_boundary("Failed to build the research activity report.")
def get_research_activity():
    unknown = sorted(set(request.args) - {"as_of"})
    if unknown:
        raise InvalidInputError("Only as_of (YYYY-MM-DD) is accepted.")
    as_of = request.args.get("as_of")
    if as_of is not None:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of) is None:
            raise InvalidInputError("as_of must be a YYYY-MM-DD date.")
        try:
            as_of = date.fromisoformat(as_of)
        except ValueError as error:
            raise InvalidInputError("as_of must be a valid date.") from error
    return jsonify(research_activity_service.report(as_of=as_of))
