"""
User service for managing user accounts and synchronization with Firebase.

This service handles user account operations using SQLAlchemy ORM,
including creating, updating, and retrieving user information.
"""

from typing import Optional, Dict, Any
from datetime import datetime, timezone
import logging
from sqlalchemy.exc import SQLAlchemyError

from app.models import get_session, User

logger = logging.getLogger(__name__)


def _days_since(timestamp: Optional[datetime]) -> int:
    """Return whole days elapsed since a stored timestamp.

    The user timestamps are declared ``DateTime(timezone=True)``, which Postgres
    returns as timezone-aware while SQLite returns naive. Comparing the wrong
    kind against "now" raises TypeError, so match the stored value.
    """

    if timestamp is None:
        return 0

    now = datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        now = now.replace(tzinfo=None)

    return (now - timestamp).days

class UserService:
    """
    Service class for user account management.
    
    Handles user CRUD operations and Firebase synchronization
    using SQLAlchemy ORM.
    """
    
    def __init__(self):
        """Initialize the UserService."""
        pass
    
    def create_or_update_user(self, firebase_user_data: Dict[str, Any]) -> Optional[User]:
        """
        Create a new user or update existing user from Firebase data.
        
        Args:
            firebase_user_data (dict): Firebase user information containing:
                - uid: Firebase user ID
                - email: User email
                - name/display_name: User display name
                - picture/photo_url: Profile photo URL
                
        Returns:
            User: The created or updated User instance, or None if error
        """
        session = get_session()
        try:
            firebase_uid = firebase_user_data.get('uid')
            if not firebase_uid:
                logger.error("No Firebase UID provided in user data")
                return None
            
            # Check if user already exists
            existing_user = session.query(User).filter_by(firebase_uid=firebase_uid).first()
            
            if existing_user:
                # Update existing user
                existing_user.email = firebase_user_data.get('email', existing_user.email)
                existing_user.display_name = (
                    firebase_user_data.get('name') or 
                    firebase_user_data.get('display_name') or 
                    existing_user.display_name
                )
                existing_user.photo_url = (
                    firebase_user_data.get('picture') or 
                    firebase_user_data.get('photo_url') or 
                    existing_user.photo_url
                )
                existing_user.last_login = datetime.now(timezone.utc)
                
                session.commit()
                logger.info(f"Updated user: {existing_user.email}")
                return existing_user
            else:
                # Create new user
                new_user = User.from_firebase_user(firebase_user_data)
                session.add(new_user)
                session.commit()
                logger.info(f"Created new user: {new_user.email}")
                return new_user
                
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Database error in create_or_update_user: {e}")
            return None
        except Exception as e:
            session.rollback()
            logger.error(f"Unexpected error in create_or_update_user: {e}")
            return None
        finally:
            session.close()
    
    def get_user_by_firebase_uid(self, firebase_uid: str) -> Optional[User]:
        """
        Retrieve user by Firebase UID.
        
        Args:
            firebase_uid (str): Firebase user ID
            
        Returns:
            User: User instance or None if not found
        """
        session = get_session()
        try:
            user = session.query(User).filter_by(firebase_uid=firebase_uid).first()
            return user
        except SQLAlchemyError as e:
            logger.error(f"Database error in get_user_by_firebase_uid: {e}")
            return None
        finally:
            session.close()
    
    def get_user_by_email(self, email: str) -> Optional[User]:
        """
        Retrieve user by email address.
        
        Args:
            email (str): User email address
            
        Returns:
            User: User instance or None if not found
        """
        session = get_session()
        try:
            user = session.query(User).filter_by(email=email).first()
            return user
        except SQLAlchemyError as e:
            logger.error(f"Database error in get_user_by_email: {e}")
            return None
        finally:
            session.close()
    
    def update_last_login(self, firebase_uid: str) -> bool:
        """
        Update the last login timestamp for a user.
        
        Args:
            firebase_uid (str): Firebase user ID
            
        Returns:
            bool: True if update successful, False otherwise
        """
        session = get_session()
        try:
            user = session.query(User).filter_by(firebase_uid=firebase_uid).first()
            if user:
                user.last_login = datetime.now(timezone.utc)
                session.commit()
                logger.debug(f"Updated last login for user: {user.email}")
                return True
            else:
                logger.warning(f"User not found for UID: {firebase_uid}")
                return False
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Database error in update_last_login: {e}")
            return False
        finally:
            session.close()
    
    def deactivate_user(self, firebase_uid: str) -> bool:
        """
        Deactivate a user account (soft delete).
        
        Args:
            firebase_uid (str): Firebase user ID
            
        Returns:
            bool: True if deactivation successful, False otherwise
        """
        session = get_session()
        try:
            user = session.query(User).filter_by(firebase_uid=firebase_uid).first()
            if user:
                user.is_active = False
                session.commit()
                logger.info(f"Deactivated user: {user.email}")
                return True
            else:
                logger.warning(f"User not found for UID: {firebase_uid}")
                return False
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Database error in deactivate_user: {e}")
            return False
        finally:
            session.close()
    
    def get_user_stats(self, firebase_uid: str) -> Optional[Dict[str, Any]]:
        """
        Get basic statistics for a user account.
        
        Args:
            firebase_uid (str): Firebase user ID
            
        Returns:
            dict: User statistics or None if user not found
        """
        user = self.get_user_by_firebase_uid(firebase_uid)
        if not user:
            return None
        
        # Calculate account age in days
        account_age_days = _days_since(user.created_at)

        # Calculate days since last login
        days_since_login = _days_since(user.last_login)
        
        return {
            'account_age_days': account_age_days,
            'days_since_last_login': days_since_login,
            'is_active': user.is_active,
            'created_at': user.created_at.isoformat() if user.created_at else None,
            'last_login': user.last_login.isoformat() if user.last_login else None
        }
    
    def get_all_active_users_count(self) -> int:
        """
        Get the total count of active users.
        
        Returns:
            int: Number of active users
        """
        session = get_session()
        try:
            count = session.query(User).filter_by(is_active=True).count()
            return count
        except SQLAlchemyError as e:
            logger.error(f"Database error in get_all_active_users_count: {e}")
            return 0
        finally:
            session.close()
