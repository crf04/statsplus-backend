"""
User API routes for account management and profile operations.

Provides endpoints for user profile management, account information,
and basic user statistics.
"""

from flask import Blueprint, request, jsonify
from app.errors import (
    AuthenticationRequiredError,
    InvalidInputError,
    OperationFailedError,
    ResourceNotFoundError,
    route_error_boundary,
)
from app.utils.auth import (
    get_current_user,
    require_admin,
    require_auth,
    require_auth_optional,
)
from ._service_proxy import CurrentAppService

# Initialize blueprint
user_bp = Blueprint('users', __name__)

user_service = CurrentAppService("user")
target_resolution_service = CurrentAppService("target_resolution")
target_backtest_service = CurrentAppService("target_backtest")

@user_bp.route('/profile', methods=['GET'])
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
    db_user = current_user.get('db_user')
    if not db_user:
        # Try to fetch from database if not in context
        db_user = user_service.get_user_by_firebase_uid(current_user['uid'])

    if not db_user:
        raise ResourceNotFoundError("User not found in database.")

    # Return user profile data
    return jsonify({
        'success': True,
        'user': db_user.to_dict()
    })

@user_bp.route('/profile', methods=['PUT'])
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
    db_user = user_service.get_user_by_firebase_uid(current_user['uid'])
    if not db_user:
        raise ResourceNotFoundError("User not found in database.")

    # Update user data with new information
    updated_firebase_data = {
        'uid': current_user['uid'],
        'email': current_user['email'],
        'name': data.get('display_name', current_user.get('name')),
        'picture': data.get('photo_url', current_user.get('picture'))
    }

    # Update user in database
    updated_user = user_service.create_or_update_user(updated_firebase_data)
    if not updated_user:
        raise OperationFailedError("Failed to update user profile.")

    return jsonify({
        'success': True,
        'message': 'Profile updated successfully',
        'user': updated_user.to_dict()
    })

@user_bp.route('/stats', methods=['GET'])
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
    stats = user_service.get_user_stats(current_user['uid'])
    if stats is None:
        raise ResourceNotFoundError("User not found.")

    return jsonify({
        'success': True,
        'stats': stats
    })

@user_bp.route('/activity/ping', methods=['POST'])
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
        return jsonify({
            'success': True,
            'message': 'No authenticated user'
        })

    # Update last login timestamp
    success = user_service.update_last_login(current_user['uid'])

    if not success:
        raise OperationFailedError("Failed to update user activity.")

    return jsonify({
        'success': True,
        'message': 'Activity updated'
    })

@user_bp.route('/deactivate', methods=['POST'])
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
    success = user_service.deactivate_user(current_user['uid'])

    if success:
        return jsonify({
            'success': True,
            'message': 'Account deactivated successfully'
        })
    raise OperationFailedError("Failed to deactivate account.")

def _authenticated_uid():
    """Return the caller's Firebase UID or refuse the request."""
    current_user = get_current_user()
    if not current_user:
        raise AuthenticationRequiredError()
    return current_user['uid']


def _saved_filter_set_body():
    """Return the submitted JSON object for a saved filter set write."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidInputError("No saved filter set data was provided.")
    return data


@user_bp.route('/saved-filter-sets', methods=['GET'])
@require_auth
@route_error_boundary("Failed to retrieve saved filter sets.")
def list_saved_filter_sets():
    """
    List the caller's Saved Filter Sets, newest first.

    Returns:
        JSON response with the caller's saved filter sets
    """
    return jsonify({
        'success': True,
        'saved_filter_sets': user_service.list_saved_filter_sets(
            _authenticated_uid()
        )
    })

@user_bp.route('/saved-filter-sets', methods=['POST'])
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
        firebase_uid,
        name=data.get('name'),
        query_string=data.get('query_string')
    )
    return jsonify({'success': True, 'saved_filter_set': created}), 201

@user_bp.route('/saved-filter-sets/<int:saved_filter_set_id>', methods=['PATCH'])
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

    updated = user_service.rename_saved_filter_set(
        firebase_uid,
        saved_filter_set_id,
        name=data.get('name')
    )
    return jsonify({'success': True, 'saved_filter_set': updated})

@user_bp.route('/saved-filter-sets/<int:saved_filter_set_id>', methods=['DELETE'])
@require_auth
@route_error_boundary("Failed to delete the saved filter set.")
def delete_saved_filter_set(saved_filter_set_id):
    """
    Delete one of the caller's Saved Filter Sets.

    Returns:
        JSON response confirming the deletion
    """
    user_service.delete_saved_filter_set(_authenticated_uid(), saved_filter_set_id)

    return jsonify({
        'success': True,
        'message': 'Saved filter set deleted'
    })

def _target_body():
    """Return the submitted JSON object for a target write."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidInputError("No target data was provided.")
    return data


@user_bp.route('/targets', methods=['GET'])
@require_auth
@route_error_boundary("Failed to retrieve targets.")
def list_targets():
    """
    List the caller's Targets, newest first, each with its derived title.

    Returns:
        JSON response with the caller's targets
    """
    return jsonify({
        'success': True,
        'targets': user_service.list_targets(_authenticated_uid())
    })

@user_bp.route('/targets', methods=['POST'])
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

    Returns:
        JSON response with the created target
    """
    firebase_uid = _authenticated_uid()
    data = _target_body()

    created = user_service.create_target(
        firebase_uid,
        opponent=data.get('opponent'),
        qualifiers=data.get('qualifiers'),
        note=data.get('note')
    )
    return jsonify({'success': True, 'target': created}), 201

@user_bp.route('/targets/resolve', methods=['GET'])
@require_auth
@route_error_boundary("Failed to resolve targets.")
def resolve_targets():
    """
    Resolve every Target for the caller against one ET Slate Date.

    Query parameters:
        date: YYYY-MM-DD; absent means the current ET slate date.

    Live Targets come first, then idle ones.  A malformed date is refused by
    the slate's own rule, as ``400 invalid_input``.

    Returns:
        JSON response with the slate date and the resolved targets
    """
    resolved = target_resolution_service.resolve(
        _authenticated_uid(),
        requested_date=request.args.get('date')
    )
    return jsonify({'success': True, **resolved})

@user_bp.route('/targets/<int:target_id>/backtest', methods=['GET'])
@require_auth
@route_error_boundary("Failed to backtest the target.")
def backtest_target(target_id):
    """
    Report one Target's season to date over the whole league.

    Every player league-wide whose current-season Diet meets every Qualifier
    and is not thin, with their games against the Target's opponent this
    season and their own season per-game averages for the same stat columns.

    Returns:
        JSON response with the target, its stat columns, and the players
    """
    backtested = target_backtest_service.backtest(
        _authenticated_uid(), target_id
    )
    return jsonify({'success': True, **backtested})

@user_bp.route('/targets/<int:target_id>', methods=['PATCH'])
@require_auth
@route_error_boundary("Failed to update the target.")
def update_target(target_id):
    """
    Edit the Qualifiers and/or the note of one of the caller's Targets.

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
    return jsonify({'success': True, 'target': updated})

@user_bp.route('/targets/<int:target_id>', methods=['DELETE'])
@require_auth
@route_error_boundary("Failed to delete the target.")
def delete_target(target_id):
    """
    Delete one of the caller's Targets and its Qualifiers.

    Returns:
        JSON response confirming the deletion
    """
    user_service.delete_target(_authenticated_uid(), target_id)

    return jsonify({
        'success': True,
        'message': 'Target deleted'
    })

@user_bp.route('/admin/stats', methods=['GET'])
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

    return jsonify({
        'success': True,
        'admin_stats': {
            'total_active_users': total_active_users
        }
    })

@user_bp.route('/sync', methods=['POST'])
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
    db_user = current_user.get('db_user')
    if db_user:
        return jsonify({
            'success': True,
            'message': 'User synced successfully',
            'user': db_user.to_dict()
        })
    else:
        raise OperationFailedError("User synchronization failed.")
