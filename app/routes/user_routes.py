"""
User API routes for account management and profile operations.

Provides endpoints for user profile management, account information,
and basic user statistics.
"""

from flask import Blueprint, g, jsonify, request

from app.config.settings import get_runtime_settings
from app.errors import (
    AuthenticationRequiredError,
    InvalidInputError,
    OperationFailedError,
    ResourceNotFoundError,
    route_error_boundary,
)
from app.services import feedback_service
from app.services.target_backtest import BACKTEST_FAILED_MESSAGE
from app.utils.auth import (
    _refuse_waitlisted,
    get_current_user,
    require_admin,
    require_auth,
    require_auth_allow_waitlisted,
    require_auth_optional,
)

from ._service_proxy import CurrentAppService

# Initialize blueprint
user_bp = Blueprint("users", __name__)

user_service = CurrentAppService("user")
target_resolution_service = CurrentAppService("target_resolution")
target_backtest_service = CurrentAppService("target_backtest")
target_preview_service = CurrentAppService("target_preview")
report_service = CurrentAppService("report")

#: A report with 38 cards is about 820 KB; the app-wide cap is 1 MB.
REPORT_MAX_REQUEST_BYTES = 2 * 1024 * 1024


@user_bp.route("/profile", methods=["GET"])
@require_auth
@route_error_boundary("Failed to retrieve user profile.")
def get_user_profile():
    """
    Get current user's profile information.

    Returns:
        JSON response with user profile data
    """
    current_user = get_current_user()
    if not current_user:
        raise AuthenticationRequiredError()

    # Get user from database
    db_user = current_user.get("db_user")
    if not db_user:
        # Try to fetch from database if not in context
        db_user = user_service.get_user_by_firebase_uid(current_user["uid"])

    if not db_user:
        raise ResourceNotFoundError("User not found in database.")

    # Return user profile data
    return jsonify({"success": True, "user": db_user.to_dict()})


@user_bp.route("/profile", methods=["PUT"])
@require_auth
@route_error_boundary("Failed to update user profile.")
def update_user_profile():
    """
    Update user profile information.

    Expected JSON body:
        {
            "display_name": "New Display Name",
            "photo_url": "https://example.com/photo.jpg"
        }

    Returns:
        JSON response with updated user data
    """
    current_user = get_current_user()
    if not current_user:
        raise AuthenticationRequiredError()

    # Get request data
    data = request.get_json(silent=True)
    if not data:
        raise InvalidInputError("No profile data was provided.")

    # Get current user from database
    db_user = user_service.get_user_by_firebase_uid(current_user["uid"])
    if not db_user:
        raise ResourceNotFoundError("User not found in database.")

    # Update user data with new information
    updated_firebase_data = {
        "uid": current_user["uid"],
        "email": current_user["email"],
        "name": data.get("display_name", current_user.get("name")),
        "picture": data.get("photo_url", current_user.get("picture")),
    }

    # Update user in database
    updated_user = user_service.create_or_update_user(updated_firebase_data)
    if not updated_user:
        raise OperationFailedError("Failed to update user profile.")

    return jsonify(
        {"success": True, "message": "Profile updated successfully", "user": updated_user.to_dict()}
    )


@user_bp.route("/stats", methods=["GET"])
@require_auth
@route_error_boundary("Failed to retrieve user statistics.")
def get_user_stats():
    """
    Get user account statistics.

    Returns:
        JSON response with user statistics
    """
    current_user = get_current_user()
    if not current_user:
        raise AuthenticationRequiredError()

    # Get user statistics
    stats = user_service.get_user_stats(current_user["uid"])
    if stats is None:
        raise ResourceNotFoundError("User not found.")

    return jsonify({"success": True, "stats": stats})


@user_bp.route("/activity/ping", methods=["POST"])
@require_auth_optional
@route_error_boundary("Failed to update user activity.")
def ping_user_activity():
    """
    Update user's last activity timestamp.
    Can be called periodically to track active usage.

    Returns:
        JSON response confirming activity update
    """
    current_user = get_current_user()
    if not current_user:
        # No user authenticated, but that's okay for optional auth
        return jsonify({"success": True, "message": "No authenticated user"})

    # Update last login timestamp
    success = user_service.update_last_login(current_user["uid"])

    if not success:
        raise OperationFailedError("Failed to update user activity.")

    return jsonify({"success": True, "message": "Activity updated"})


@user_bp.route("/attribution", methods=["POST"])
@require_auth_allow_waitlisted
@route_error_boundary("Failed to record the acquisition source.")
def record_attribution():
    """
    Record the account's first-touch acquisition source, once.

    Waitlisted accounts are accepted.  The record is write-once: a repeat
    leaves the stored values unchanged.

    Returns:
        201 ``{"recorded": true}`` for the first record, else 200
        ``{"recorded": false}``
    """
    recorded = user_service.record_first_touch(_authenticated_uid(), request.get_json(silent=True))
    return jsonify({"recorded": recorded}), 201 if recorded else 200


@user_bp.route("/feedback", methods=["POST"])
@require_auth_allow_waitlisted
@route_error_boundary("Failed to send feedback.")
def submit_feedback():
    """
    Email in-app feedback to support through Resend (crf04/statsplus#258).

    Waitlisted accounts are accepted.  Nothing is stored; the email is the
    record.  The account name and email come from the verified token.

    Returns:
        201 ``{"sent": true}``; 400 for invalid input; 503
        ``feedback_unavailable`` when delivery is unconfigured or fails
    """
    # The app-wide 1 MB cap would 413 a body carrying a valid 2 MB screenshot
    # (about 2.8 MB as base64); validation bounds the fields from here.
    request.max_content_length = feedback_service.MAX_REQUEST_BYTES
    feedback = feedback_service.parse_feedback(request.get_json(silent=True))
    feedback_service.deliver(feedback, get_current_user(), get_runtime_settings())
    return jsonify({"sent": True}), 201


@user_bp.route("/deactivate", methods=["POST"])
@require_auth
@route_error_boundary("Failed to deactivate account.")
def deactivate_account():
    """
    Deactivate user account (soft delete).

    Returns:
        JSON response confirming account deactivation
    """
    current_user = get_current_user()
    if not current_user:
        raise AuthenticationRequiredError()

    # Deactivate user account
    success = user_service.deactivate_user(current_user["uid"])

    if success:
        return jsonify({"success": True, "message": "Account deactivated successfully"})
    raise OperationFailedError("Failed to deactivate account.")


def _authenticated_uid():
    """Return the caller's Firebase UID or refuse the request."""
    current_user = get_current_user()
    if not current_user:
        raise AuthenticationRequiredError()
    return current_user["uid"]


def _saved_filter_set_body():
    """Return the submitted JSON object for a saved filter set write."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidInputError("No saved filter set data was provided.")
    return data


@user_bp.route("/saved-filter-sets", methods=["GET"])
@require_auth
@route_error_boundary("Failed to retrieve saved filter sets.")
def list_saved_filter_sets():
    """
    List the caller's Saved Filter Sets, newest first.

    Returns:
        JSON response with the caller's saved filter sets
    """
    return jsonify(
        {"success": True, "saved_filter_sets": user_service.list_saved_filter_sets(_authenticated_uid())}
    )


@user_bp.route("/saved-filter-sets", methods=["POST"])
@require_auth
@route_error_boundary("Failed to save the filter set.")
def create_saved_filter_set():
    """
    Save the submitted Log Workspace query string under a name.

    Expected JSON body:
        {
            "name": "Jokic at home",
            "query_string": "player=Nikola+Jokic&location_filter=Home"
        }

    Returns:
        JSON response with the created saved filter set
    """
    firebase_uid = _authenticated_uid()
    data = _saved_filter_set_body()

    created = user_service.create_saved_filter_set(
        firebase_uid, name=data.get("name"), query_string=data.get("query_string")
    )
    return jsonify({"success": True, "saved_filter_set": created}), 201


@user_bp.route("/saved-filter-sets/<int:saved_filter_set_id>", methods=["PATCH"])
@require_auth
@route_error_boundary("Failed to rename the saved filter set.")
def rename_saved_filter_set(saved_filter_set_id):
    """
    Rename one of the caller's Saved Filter Sets.

    Expected JSON body:
        {
            "name": "New name"
        }

    The saved query string is immutable.

    Returns:
        JSON response with the updated saved filter set
    """
    firebase_uid = _authenticated_uid()
    data = _saved_filter_set_body()

    updated = user_service.rename_saved_filter_set(firebase_uid, saved_filter_set_id, name=data.get("name"))
    return jsonify({"success": True, "saved_filter_set": updated})


@user_bp.route("/saved-filter-sets/<int:saved_filter_set_id>", methods=["DELETE"])
@require_auth
@route_error_boundary("Failed to delete the saved filter set.")
def delete_saved_filter_set(saved_filter_set_id):
    """
    Delete one of the caller's Saved Filter Sets.

    Returns:
        JSON response confirming the deletion
    """
    user_service.delete_saved_filter_set(_authenticated_uid(), saved_filter_set_id)

    return jsonify({"success": True, "message": "Saved filter set deleted"})


def _requested_season(data):
    """The preview body's requested season: absent is the default rule.

    Only an omitted ``season`` applies the default; a present one must be a
    season string, so an explicit ``null`` is refused like any other wrong
    value (crf04/statsplus#104).
    """
    if "season" not in data:
        return None
    if not isinstance(data["season"], str):
        raise InvalidInputError("season must be a YYYY-YY season string.")
    return data["season"]


def _target_body():
    """Return the submitted JSON object for a target write."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidInputError("No target data was provided.")
    return data


@user_bp.route("/targets", methods=["GET"])
@require_auth
@route_error_boundary("Failed to retrieve targets.")
def list_targets():
    """
    List the caller's Targets, newest first, each with its derived title.

    Returns:
        JSON response with the caller's targets
    """
    return jsonify({"success": True, "targets": user_service.list_targets(_authenticated_uid())})


@user_bp.route("/targets", methods=["POST"])
@require_auth
@route_error_boundary("Failed to save the target.")
def create_target():
    """
    Create a Target pairing one opponent with one or more Qualifiers.

    Expected JSON body:
        {
            "opponent": "OKC",
            "qualifiers": [
                {
                    "base": "shot_zones",
                    "slice_key": "Corner 3",
                    "comparator": "at_or_above",
                    "threshold": 0.4
                }
            ],
            "note": "Leaks corner threes"
        }

    The title is derived from the opponent and the qualifiers; it is never
    submitted.

    The optional ``season`` takes the preview's values and errors (an explicit
    ``null`` is ``400 invalid_input``).  It only picks the season a defender
    Condition is validated in; it is not stored and not returned.

    Returns:
        JSON response with the created target
    """
    firebase_uid = _authenticated_uid()
    data = _target_body()

    created = user_service.create_target(
        firebase_uid,
        opponent=data.get("opponent"),
        qualifiers=data.get("qualifiers"),
        note=data.get("note"),
        **({key: data[key] for key in ("conditions", "stat_preferences") if key in data}),
        **({"season": _requested_season(data)} if "season" in data else {}),
    )
    return jsonify({"success": True, "target": created}), 201


@user_bp.route("/targets/resolve", methods=["GET"])
@require_auth
@route_error_boundary("Failed to resolve targets.")
def resolve_targets():
    """
    Resolve every Target for the caller against one ET Slate Date.

    Query parameters:
        date: YYYY-MM-DD; absent means the current ET slate date.
        fallback: ``previous_game_day`` or ``next_game_day``; only without
            ``date``.  When the current ET slate date lists no game, resolve
            against the previous (or, for ``next_game_day``, the next game
            date, else the previous one).  Any other value, or ``date`` with it, is
            ``400 invalid_input``.

    Live Targets come first, then idle ones.  A malformed date is refused by
    the slate's own rule, as ``400 invalid_input``.

    Returns:
        JSON response with the slate date and the resolved targets
    """
    resolved = target_resolution_service.resolve(
        _authenticated_uid(),
        requested_date=request.args.get("date"),
        fallback=request.args.get("fallback"),
    )
    return jsonify({"success": True, **resolved})


@user_bp.route("/targets/preview", methods=["POST"])
@require_auth
@route_error_boundary("Failed to preview the target.")
def preview_target():
    """
    Preview a Draft Target: its season to date, and whether it fires today.

    Expected JSON body: the same as creating a Target.  It is validated by the
    same rules, but nothing is stored and neither the per-account cap nor the
    duplicate rule applies, so the Lab can evaluate a Target the caller has
    not saved and may never save.

    The optional ``season`` is ``2025-26`` or ``2026-27``; without it
    ``2025-26`` is read.

    Returns:
        JSON response with the backtest for the draft (its ``target`` carries
        the derived title and no id) and ``today``: ``null`` when the opponent
        is idle on the current slate date or the season is not the published
        one, else
        the game and the fit count
    """
    data = _target_body()
    season = _requested_season(data)

    draft = user_service.validate_target_draft(
        opponent=data.get("opponent"),
        qualifiers=data.get("qualifiers"),
        note=data.get("note"),
        **({key: data[key] for key in ("conditions", "stat_preferences") if key in data}),
        season=season,
    )
    return jsonify(
        {
            "success": True,
            **target_preview_service.preview(draft, season=season),
        }
    )


@user_bp.route("/targets/backtests", methods=["GET"])
@require_auth
@route_error_boundary("Failed to backtest the targets.")
def backtest_targets():
    """
    Report every one of the caller's Targets' cached seasons to date at once.

    One item per Target, in the order the Target list shows them: ``ok``
    with the body the single Target's backtest returns, minus ``success``;
    ``uncached`` when the result cache holds no entry for it, which the
    client then reads through the single route; or ``error`` with the
    standard error object, so one failing Target never blanks the others.
    Nothing is computed here, and every ``ok`` item comes from one
    Publication generation.  ``<int:target_id>`` only matches integers, so
    this path never reaches the per-Target routes.  The optional ``season``
    query parameter is resolved once for every Target.

    Returns:
        JSON response with the season and one Backtest item per Target
    """
    backtested, cache_state = target_backtest_service.backtest_all(
        _authenticated_uid(), season=request.args.get("season")
    )
    # hit when every item is ok, miss when any is uncached, else '-'.
    g.targets_cache = cache_state
    # An answer of only uncached or failed items loaded no Backtest, so it is
    # not a research action (crf04/statsplus#213).
    g.research_action = any(item.get("status") == "ok" for item in backtested.get("backtests", ()))
    return jsonify({"success": True, **backtested})


@user_bp.route("/targets/<int:target_id>/backtest", methods=["GET"])
@require_auth
@route_error_boundary(BACKTEST_FAILED_MESSAGE)
def backtest_target(target_id):
    """
    Report one Target's season to date over the whole league.

    Every player league-wide whose current-season Diet meets every Qualifier
    and is not thin, with their games against the Target's opponent this
    season and their own season per-game averages for the same stat columns.
    The optional ``season`` query parameter is ``2025-26`` or ``2026-27``;
    without it ``2025-26`` is read.

    Returns:
        JSON response with the target, its stat columns, and the players
    """
    backtested, cache_state = target_backtest_service.backtest(
        _authenticated_uid(), target_id, season=request.args.get("season")
    )
    # The service stays HTTP-free; the route stamps the request log's
    # ``targets_cache`` outcome here: hit, miss, bypass, or the unbilled '-'.
    g.targets_cache = cache_state
    return jsonify({"success": True, **backtested})


@user_bp.route("/targets/<int:target_id>", methods=["PATCH"])
@require_auth
@route_error_boundary("Failed to update the target.")
def update_target(target_id):
    """
    Edit a caller's Qualifiers, note, Conditions, or stat preferences.

    Expected JSON body, with either key or both:
        {
            "qualifiers": [...],
            "note": "Rim, not threes"
        }

    The submitted object is forwarded as-is, so an absent key means unchanged
    and a ``null`` note clears it.  The opponent is fixed.

    Returns:
        JSON response with the updated target
    """
    firebase_uid = _authenticated_uid()
    data = _target_body()

    updated = user_service.update_target(firebase_uid, target_id, changes=data)
    return jsonify({"success": True, "target": updated})


@user_bp.route("/targets/<int:target_id>", methods=["DELETE"])
@require_auth
@route_error_boundary("Failed to delete the target.")
def delete_target(target_id):
    """
    Delete one of the caller's Targets and its Qualifiers.

    Returns:
        JSON response confirming the deletion
    """
    user_service.delete_target(_authenticated_uid(), target_id)

    return jsonify({"success": True, "message": "Target deleted"})


@user_bp.route("/reports", methods=["POST"])
@require_auth
@route_error_boundary("Failed to publish the report.")
def create_report():
    """
    Publish a CourtAI report for the caller (crf04/statsplus#342).

    Expected JSON body (at most 2 MB):
        {
            "report": {...},
            "calls": [{"id", "tool", "args", "structured", "meta", "card"}],
            "cards": {"<card>": "<html>"},
            "show_author": false,
            "listed": false
        }

    ``listed: true`` also lists it in Research (crf04/statsplus#372): without a
    handle that is ``409 handle_required`` and past the listing cap ``429
    listed_limit``, and nothing is stored.

    Returns:
        201 with the report's id, public URL and creation time
    """
    firebase_uid = _authenticated_uid()
    request.max_content_length = REPORT_MAX_REQUEST_BYTES
    published = report_service.publish(firebase_uid, request.get_json(silent=True))
    return jsonify({"success": True, "report": published}), 201


@user_bp.route("/reports/<report_id>", methods=["PATCH"])
@require_auth_allow_waitlisted
@route_error_boundary("Failed to update the report.")
def update_report(report_id):
    """
    List or unlist one of the caller's reports in Research (crf04/statsplus#372).

    Expected JSON body: ``{"listed": true | false}``.  Listing needs a public
    handle (``409 handle_required``) and counts toward the listing cap
    (``429 listed_limit``) and an active account (``403 waitlisted``);
    unlisting is always allowed, a waitlisted author included.

    Returns:
        JSON response with the report's id, ``listed`` and ``listed_at``
    """
    firebase_uid = _authenticated_uid()
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"listed"} or not isinstance(body["listed"], bool):
        raise InvalidInputError('Body must be {"listed": true|false}.')
    if body["listed"]:
        _refuse_waitlisted(get_current_user())
    return jsonify(
        {"success": True, "report": report_service.set_listed(firebase_uid, report_id, body["listed"])}
    )


@user_bp.route("/reports/<report_id>/edits", methods=["PUT"])
@require_auth_allow_waitlisted
@route_error_boundary("Failed to save the report's edits.")
def put_report_edits(report_id):
    """
    Save the caller's edits of one of their reports (crf04/statsplus#377).

    Expected JSON body, replacing every earlier edit:
        {
            "title": "...",
            "notes": "..." | null,
            "items": [{"tab": 0, "item": 2, "hidden": false, "cards": ["call-id"]}]
        }

    Someone else's report, or a deleted or taken-down one, is ``404``; bad
    notes are ``400 notes_links`` or ``400 notes_too_long`` and items or cards
    the report was not published with ``400 invalid_edits``.  A waitlisted
    author may edit; editing lists nothing.

    Returns:
        The report exactly as ``GET /api/reports/<id>`` returns it to its author
    """
    firebase_uid = _authenticated_uid()
    return jsonify(
        {"report": report_service.save_edits(firebase_uid, report_id, request.get_json(silent=True))}
    )


@user_bp.route("/public-handle", methods=["PUT"])
@require_auth
@route_error_boundary("Failed to set the public handle.")
def put_public_handle():
    """
    Choose the caller's public handle, once (crf04/statsplus#372).

    Expected JSON body: ``{"handle": "bighatprops"}`` (a leading ``@`` and
    capitals are accepted and dropped).  ``400 invalid_handle``, ``409
    handle_taken``, or ``409 handle_already_set`` once one is set.

    Returns:
        JSON response with the stored handle
    """
    firebase_uid = _authenticated_uid()
    body = request.get_json(silent=True)
    handle = body.get("handle") if isinstance(body, dict) else None
    return jsonify({"handle": report_service.claim_public_handle(firebase_uid, handle)})


@user_bp.route("/reports/<report_id>", methods=["DELETE"])
@require_auth
@route_error_boundary("Failed to delete the report.")
def delete_report(report_id):
    """
    Delete one of the caller's reports; it then reads as not found for everyone.

    Returns:
        JSON response confirming the deletion
    """
    report_service.delete(_authenticated_uid(), report_id)
    return jsonify({"success": True, "message": "Report deleted"})


@user_bp.route("/admin/stats", methods=["GET"])
@require_admin
@route_error_boundary("Failed to retrieve admin statistics.")
def get_admin_stats():
    """
    Get administrative statistics (total users, etc.).

    Returns:
        JSON response with admin statistics
    """
    # Get total active users count
    total_active_users = user_service.get_all_active_users_count()

    return jsonify({"success": True, "admin_stats": {"total_active_users": total_active_users}})


@user_bp.route("/sync", methods=["POST"])
@require_auth
@route_error_boundary("User synchronization failed.")
def sync_user():
    """
    Force sync current user to database.
    Call this after login to ensure user is saved.
    """
    current_user = get_current_user()
    if not current_user:
        raise AuthenticationRequiredError()

    # The user should already be synced by the auth middleware,
    # but let's return the user info to confirm
    db_user = current_user.get("db_user")
    if db_user:
        return jsonify({"success": True, "message": "User synced successfully", "user": db_user.to_dict()})
    else:
        raise OperationFailedError("User synchronization failed.")
