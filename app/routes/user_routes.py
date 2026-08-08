"""
User API routes for account management and profile operations.

Provides endpoints for user profile management, account information,
and basic user statistics.
"""

from flask import Blueprint, request, jsonify
from app.utils.auth import (
    get_current_user,
    require_admin,
    require_auth,
    require_auth_optional,
)
from app.services.user_service import UserService
import logging

logger = logging.getLogger(__name__)

# Initialize blueprint
user_bp = Blueprint('users', __name__)
user_service = UserService()

@user_bp.route('/profile', methods=['GET'])
@require_auth
def get_user_profile():
    """
    Get current user's profile information.
    
    Returns:
        JSON response with user profile data
    """
    try:
        current_user = get_current_user()
        if not current_user:
            return jsonify({'error': 'User not authenticated'}), 401
        
        # Get user from database
        db_user = current_user.get('db_user')
        if not db_user:
            # Try to fetch from database if not in context
            db_user = user_service.get_user_by_firebase_uid(current_user['uid'])
        
        if not db_user:
            return jsonify({'error': 'User not found in database'}), 404
        
        # Return user profile data
        return jsonify({
            'success': True,
            'user': db_user.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Error in get_user_profile: {e}")
        return jsonify({'error': 'Failed to retrieve user profile'}), 500

@user_bp.route('/profile', methods=['PUT'])
@require_auth
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
    try:
        current_user = get_current_user()
        if not current_user:
            return jsonify({'error': 'User not authenticated'}), 401
        
        # Get request data
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # Get current user from database
        db_user = user_service.get_user_by_firebase_uid(current_user['uid'])
        if not db_user:
            return jsonify({'error': 'User not found in database'}), 404
        
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
            return jsonify({'error': 'Failed to update user profile'}), 500
        
        return jsonify({
            'success': True,
            'message': 'Profile updated successfully',
            'user': updated_user.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Error in update_user_profile: {e}")
        return jsonify({'error': 'Failed to update user profile'}), 500

@user_bp.route('/stats', methods=['GET'])
@require_auth
def get_user_stats():
    """
    Get user account statistics.
    
    Returns:
        JSON response with user statistics
    """
    try:
        current_user = get_current_user()
        if not current_user:
            return jsonify({'error': 'User not authenticated'}), 401
        
        # Get user statistics
        stats = user_service.get_user_stats(current_user['uid'])
        if stats is None:
            return jsonify({'error': 'User not found'}), 404
        
        return jsonify({
            'success': True,
            'stats': stats
        })
        
    except Exception as e:
        logger.error(f"Error in get_user_stats: {e}")
        return jsonify({'error': 'Failed to retrieve user statistics'}), 500

@user_bp.route('/activity/ping', methods=['POST'])
@require_auth_optional
def ping_user_activity():
    """
    Update user's last activity timestamp.
    Can be called periodically to track active usage.
    
    Returns:
        JSON response confirming activity update
    """
    try:
        current_user = get_current_user()
        if not current_user:
            # No user authenticated, but that's okay for optional auth
            return jsonify({
                'success': True,
                'message': 'No authenticated user'
            })
        
        # Update last login timestamp
        success = user_service.update_last_login(current_user['uid'])
        
        return jsonify({
            'success': success,
            'message': 'Activity updated' if success else 'Failed to update activity'
        })
        
    except Exception as e:
        logger.error(f"Error in ping_user_activity: {e}")
        return jsonify({'error': 'Failed to update user activity'}), 500

@user_bp.route('/deactivate', methods=['POST'])
@require_auth
def deactivate_account():
    """
    Deactivate user account (soft delete).
    
    Returns:
        JSON response confirming account deactivation
    """
    try:
        current_user = get_current_user()
        if not current_user:
            return jsonify({'error': 'User not authenticated'}), 401
        
        # Deactivate user account
        success = user_service.deactivate_user(current_user['uid'])
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Account deactivated successfully'
            })
        else:
            return jsonify({'error': 'Failed to deactivate account'}), 500
        
    except Exception as e:
        logger.error(f"Error in deactivate_account: {e}")
        return jsonify({'error': 'Failed to deactivate account'}), 500

@user_bp.route('/admin/stats', methods=['GET'])
@require_admin
def get_admin_stats():
    """
    Get administrative statistics (total users, etc.).

    Returns:
        JSON response with admin statistics
    """
    try:
        # Get total active users count
        total_active_users = user_service.get_all_active_users_count()
        
        return jsonify({
            'success': True,
            'admin_stats': {
                'total_active_users': total_active_users
            }
        })
        
    except Exception as e:
        logger.error(f"Error in get_admin_stats: {e}")
        return jsonify({'error': 'Failed to retrieve admin statistics'}), 500

@user_bp.route('/sync', methods=['POST'])
@require_auth
def sync_user():
    """
    Force sync current user to database.
    Call this after login to ensure user is saved.
    """
    try:
        current_user = get_current_user()
        if not current_user:
            return jsonify({'error': 'User not authenticated'}), 401
        
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
            return jsonify({
                'success': False,
                'message': 'User sync failed',
                'firebase_user': {
                    'uid': current_user.get('uid'),
                    'email': current_user.get('email'),
                    'name': current_user.get('name')
                }
            })
        
    except Exception as e:
        logger.error(f"Error in sync_user: {e}")
        return jsonify({'error': f'Sync failed: {str(e)}'}), 500
