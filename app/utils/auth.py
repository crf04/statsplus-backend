"""
Authentication middleware for Firebase token verification
"""
from functools import wraps
from flask import request, jsonify, g
from .firebase_admin import verify_firebase_token, get_firebase_app
from app.services.user_service import UserService
import logging

logger = logging.getLogger(__name__)

def require_auth(f):
    """
    Decorator to require Firebase authentication for Flask routes
    
    Usage:
        @app.route('/protected')
        @require_auth
        def protected_route():
            # Access authenticated user via g.current_user
            return jsonify({'user_id': g.current_user['uid']})
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if Firebase is initialized
        if get_firebase_app() is None:
            logger.warning("Firebase not initialized - skipping authentication")
            # In development, allow requests to pass through
            g.current_user = {
                'uid': 'dev-user',
                'email': 'dev@example.com',
                'name': 'Development User'
            }
            return f(*args, **kwargs)
        
        # Extract token from Authorization header
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({
                'error': 'Missing Authorization header',
                'message': 'Please provide a valid Firebase token'
            }), 401
        
        # Check for Bearer token format
        try:
            scheme, token = auth_header.split(' ', 1)
            if scheme.lower() != 'bearer':
                return jsonify({
                    'error': 'Invalid authorization scheme',
                    'message': 'Authorization header must use Bearer scheme'
                }), 401
        except ValueError:
            return jsonify({
                'error': 'Invalid Authorization header format',
                'message': 'Authorization header must be: Bearer <token>'
            }), 401
        
        # Verify Firebase token
        try:
            decoded_token = verify_firebase_token(token)
            
            # Create user data for synchronization
            firebase_user_data = {
                'uid': decoded_token.get('uid'),
                'email': decoded_token.get('email'),
                'name': decoded_token.get('name'),
                'picture': decoded_token.get('picture'),
                'email_verified': decoded_token.get('email_verified', False)
            }
            
            # Sync user to database
            user_service = UserService()
            db_user = user_service.create_or_update_user(firebase_user_data)
            
            # Store user information in Flask's g object for use in route handlers
            g.current_user = {
                'uid': decoded_token.get('uid'),
                'email': decoded_token.get('email'),
                'name': decoded_token.get('name'),
                'picture': decoded_token.get('picture'),
                'email_verified': decoded_token.get('email_verified', False),
                'firebase_claims': decoded_token,  # Full token for additional claims
                'db_user': db_user  # Local database user record
            }
            
            logger.info(f"Authenticated user: {g.current_user['email']} ({g.current_user['uid']})")
            
            # Call the protected route
            return f(*args, **kwargs)
            
        except ValueError as e:
            logger.warning(f"Invalid token: {str(e)}")
            return jsonify({
                'error': 'Invalid token',
                'message': str(e)
            }), 401
        except Exception as e:
            logger.error(f"Authentication error: {str(e)}")
            return jsonify({
                'error': 'Authentication failed',
                'message': 'Unable to verify token'
            }), 500
    
    return decorated_function

def get_current_user():
    """
    Get the current authenticated user from Flask's g object
    
    Returns:
        dict: User information or None if not authenticated
    """
    return getattr(g, 'current_user', None)

def require_auth_optional(f):
    """
    Decorator for optional authentication - sets user if token is provided
    but doesn't require it
    
    Usage:
        @app.route('/public-or-private')
        @require_auth_optional
        def mixed_route():
            user = get_current_user()
            if user:
                return jsonify({'message': f'Hello {user["email"]}'})
            else:
                return jsonify({'message': 'Hello anonymous user'})
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Try to authenticate, but don't fail if no token
        auth_header = request.headers.get('Authorization')
        if auth_header and get_firebase_app() is not None:
            try:
                scheme, token = auth_header.split(' ', 1)
                if scheme.lower() == 'bearer':
                    decoded_token = verify_firebase_token(token)
                    
                    # Create user data for synchronization
                    firebase_user_data = {
                        'uid': decoded_token.get('uid'),
                        'email': decoded_token.get('email'),
                        'name': decoded_token.get('name'),
                        'picture': decoded_token.get('picture'),
                        'email_verified': decoded_token.get('email_verified', False)
                    }
                    
                    # Sync user to database (optional auth, so don't fail on DB errors)
                    user_service = UserService()
                    db_user = None
                    try:
                        db_user = user_service.create_or_update_user(firebase_user_data)
                    except Exception as db_error:
                        logger.warning(f"Failed to sync user to database: {db_error}")
                    
                    g.current_user = {
                        'uid': decoded_token.get('uid'),
                        'email': decoded_token.get('email'),
                        'name': decoded_token.get('name'),
                        'picture': decoded_token.get('picture'),
                        'email_verified': decoded_token.get('email_verified', False),
                        'firebase_claims': decoded_token,
                        'db_user': db_user
                    }
            except Exception:
                # Ignore authentication errors for optional auth
                pass
        
        return f(*args, **kwargs)
    
    return decorated_function