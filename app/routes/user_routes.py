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
from app.services.user_service import UserService
from ._service_proxy import CurrentAppService

# Initialize blueprint
user_bp = Blueprint('users', __name__)


def _build_user_service(engine, settings):
    return UserService(engine, settings=settings)


user_service = CurrentAppService("user", _build_user_service)

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
