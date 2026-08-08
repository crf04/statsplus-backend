"""
Firebase Admin SDK initialization and configuration
"""
import os
import json
import logging
import firebase_admin
from firebase_admin import credentials, auth
from typing import Optional

from app.config.settings import RuntimeSettings, get_runtime_settings

logger = logging.getLogger(__name__)

# Global variable to store the initialized app
_firebase_app: Optional[firebase_admin.App] = None

def initialize_firebase_admin(settings: RuntimeSettings | None = None):
    """Initialize Firebase Admin SDK with service account credentials"""
    global _firebase_app

    runtime_settings = settings or get_runtime_settings()
    auth_settings = runtime_settings.auth

    if _firebase_app is not None:
        return _firebase_app

    # Local-dev opt-out (user-approved): when FIREBASE_ADMIN_DISABLED is set,
    # routes fall back to require_auth's dev-user passthrough. Prod unaffected.
    if auth_settings.firebase_admin_disabled:
        logger.warning("Firebase Admin disabled via FIREBASE_ADMIN_DISABLED - local dev only")
        return None

    try:
        # Method 1: Try to load from service account JSON file
        service_account_path = auth_settings.firebase_service_account_path
        if service_account_path and os.path.exists(service_account_path):
            cred = credentials.Certificate(service_account_path)
            _firebase_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin initialized with service account file")
            return _firebase_app
        
        # Method 2: Try to load from environment variables (for deployment)
        service_account_json = auth_settings.firebase_service_account_json
        if service_account_json:
            # Parse JSON string to dict
            service_account_dict = json.loads(service_account_json)
            cred = credentials.Certificate(service_account_dict)
            _firebase_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin initialized with service account JSON")
            return _firebase_app
        
        # Method 3: Try individual environment variables
        private_key = auth_settings.firebase_private_key
        client_email = auth_settings.firebase_client_email
        project_id = auth_settings.firebase_project_id
        
        if private_key and client_email and project_id:
            # Replace escaped newlines in private key
            private_key = private_key.replace('\\n', '\n')
            
            service_account_dict = {
                "type": "service_account",
                "project_id": project_id,
                "private_key": private_key,
                "client_email": client_email,
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs"
            }
            
            cred = credentials.Certificate(service_account_dict)
            _firebase_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin initialized with individual environment variables")
            return _firebase_app
        
        # Method 4: Try default application credentials (for local development)
        if os.path.exists(os.path.expanduser('~/.config/gcloud/application_default_credentials.json')):
            cred = credentials.ApplicationDefault()
            _firebase_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin initialized with application default credentials")
            return _firebase_app
        
        logger.warning(
            "Firebase Admin not initialized; set FIREBASE_SERVICE_ACCOUNT_PATH, "
            "FIREBASE_SERVICE_ACCOUNT_JSON, or individual Firebase env vars"
        )
        return None
        
    except Exception as e:
        logger.error("Firebase Admin initialization failed: %s", e)
        return None

def get_firebase_app(settings: RuntimeSettings | None = None):
    """Get the initialized Firebase app"""
    global _firebase_app
    if _firebase_app is None:
        _firebase_app = initialize_firebase_admin(settings)
    return _firebase_app

def verify_firebase_token(id_token: str):
    """
    Verify Firebase ID token and return decoded claims
    
    Args:
        id_token: Firebase ID token from Authorization header
        
    Returns:
        dict: Decoded token claims containing user info
        
    Raises:
        ValueError: If token is invalid
        Exception: If Firebase is not initialized
    """
    app = get_firebase_app()
    if app is None:
        raise Exception("Firebase Admin not initialized")
    
    try:
        # Verify the ID token and get decoded claims
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token
    except Exception as e:
        raise ValueError(f"Invalid token: {str(e)}")

def get_user_by_uid(uid: str):
    """
    Get user information by UID
    
    Args:
        uid: Firebase user UID
        
    Returns:
        UserRecord: Firebase user record
    """
    app = get_firebase_app()
    if app is None:
        raise Exception("Firebase Admin not initialized")
    
    try:
        user = auth.get_user(uid)
        return user
    except Exception as e:
        raise ValueError(f"User not found: {str(e)}")
