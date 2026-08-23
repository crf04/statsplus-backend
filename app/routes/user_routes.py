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
