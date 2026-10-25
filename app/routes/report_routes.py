"""Public reads of CourtAI reports (crf04/statsplus#342).

Anyone with a report's link can read it, signed in or not, waitlisted
included; a token only adds ``is_author``.  Publishing and deleting live under
``/api/user/reports``, the takedown under ``/api/admin``.
"""

from flask import Blueprint, Response, jsonify, request

from app.errors import route_error_boundary
from app.services.report_preview import render_report_preview
from app.utils.auth import get_current_user, require_auth_optional_allow_waitlisted

from ._service_proxy import CurrentAppService

report_bp = Blueprint("reports", __name__)

report_service = CurrentAppService("report")


@report_bp.route("/<report_id>", methods=["GET"])
@require_auth_optional_allow_waitlisted
@route_error_boundary("Failed to read the report.")
def get_report(report_id):
    viewer = get_current_user()
    return jsonify({"report": report_service.read(report_id, viewer_uid=viewer["uid"] if viewer else None)})


@report_bp.route("/<report_id>/meta", methods=["GET"])
@route_error_boundary("Failed to read the report.")
def get_report_meta(report_id):
    return jsonify(report_service.meta(report_id))


@report_bp.route("/<report_id>/preview.png", methods=["GET"])
@route_error_boundary("Failed to draw the report preview.")
def get_report_preview(report_id):
    image = render_report_preview(report_service.document(report_id))
    return Response(image.png, mimetype="image/png", headers={"Cache-Control": image.cache_control})


@report_bp.after_request
def _refusals_of_the_preview_are_not_cached(response):
    if request.endpoint == "reports.get_report_preview" and response.status_code != 200:
        response.headers["Cache-Control"] = "no-store"
    return response
