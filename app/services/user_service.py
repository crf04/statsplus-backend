"""
User service for managing user accounts and synchronization with Firebase.

This service handles user account operations using SQLAlchemy ORM,
including creating, updating, and retrieving user information.
"""

from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone
import logging
import re
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.errors import ConflictError, InvalidInputError, ResourceNotFoundError
from app.models import get_session, SavedFilterSet, User
from app.utils.db import get_engine

logger = logging.getLogger(__name__)

USER_LOGIN_TOUCH_INTERVAL = timedelta(minutes=15)

SAVED_FILTER_SET_NAME_MAX_LENGTH = 100
SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH = 2048
SAVED_FILTER_SET_LIMIT = 100

# A bare query string never carries a scheme, so anything shaped like
# ``scheme://`` is a whole URL rather than the value this feature stores.
_URL_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

_BARE_QUERY_STRING_MESSAGE = (
    "A saved filter set query string must be a bare URL query string, without "
    "a scheme, host, path, leading '?', or '#' fragment."
)


def _validated_saved_filter_set_name(value: Any) -> str:
    """Return the trimmed name, or refuse one the contract cannot store."""

    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError("A saved filter set name is required.")

    name = value.strip()
    if len(name) > SAVED_FILTER_SET_NAME_MAX_LENGTH:
        raise InvalidInputError(
            "A saved filter set name may be at most "
            f"{SAVED_FILTER_SET_NAME_MAX_LENGTH} characters."
        )
    return name


def _validated_saved_filter_set_query_string(value: Any) -> str:
    """Return the query string, or refuse one that is not bare.

    Parameter names inside the query string are deliberately not judged here:
    a saved set that no longer parses is reported by the client's existing
    URL-entry error path when it is opened.
    """

    if not isinstance(value, str) or not value:
        raise InvalidInputError("A saved filter set query string is required.")

    if len(value) > SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH:
        raise InvalidInputError(
            "A saved filter set query string may be at most "
            f"{SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH} characters."
        )

    unusable = (
        value.startswith("?")
        or value.startswith("/")
        or "#" in value
        or any(character.isspace() for character in value)
        or _URL_SCHEME_PREFIX.match(value) is not None
    )
    if unusable:
        raise InvalidInputError(_BARE_QUERY_STRING_MESSAGE)

    return value


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


def _login_touch_due(timestamp: Optional[datetime], now: datetime) -> bool:
    """Return whether durable activity is old enough to write again."""

    if timestamp is None:
        return True
    if timestamp.tzinfo is None:
        now = now.replace(tzinfo=None)
    return now - timestamp >= USER_LOGIN_TOUCH_INTERVAL


class UserService:
    """
    Service class for user account management.
    
    Handles user CRUD operations and Firebase synchronization
    using SQLAlchemy ORM.
    """
    
    def __init__(self, db_engine=None, settings: RuntimeSettings | None = None):
        """Initialize the service with the active app's engine and settings."""
        self.settings = settings or get_runtime_settings()
        self.engine = db_engine or get_engine(self.settings)

    def _get_session(self):
        """Create a session bound to this service's app-scoped engine."""
        return get_session(self.engine)
    
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
        session = self._get_session()
        try:
            firebase_uid = firebase_user_data.get('uid')
            if not firebase_uid:
                logger.error("No Firebase UID provided in user data")
                return None
            
            # Check if user already exists
            existing_user = session.query(User).filter_by(firebase_uid=firebase_uid).first()
            
            if existing_user:
                email = firebase_user_data.get('email', existing_user.email)
                display_name = (
                    firebase_user_data.get('name') or 
                    firebase_user_data.get('display_name') or 
                    existing_user.display_name
                )
                photo_url = (
                    firebase_user_data.get('picture') or 
                    firebase_user_data.get('photo_url') or 
                    existing_user.photo_url
                )
                profile_changed = (
                    existing_user.email != email
                    or existing_user.display_name != display_name
                    or existing_user.photo_url != photo_url
                )
                if profile_changed:
                    existing_user.email = email
                    existing_user.display_name = display_name
                    existing_user.photo_url = photo_url

                now = datetime.now(timezone.utc)
                login_touched = _login_touch_due(existing_user.last_login, now)
                if login_touched:
                    existing_user.last_login = now

                if profile_changed or login_touched:
                    session.commit()
                    logger.info("Updated user: %s", existing_user.email)
                else:
                    logger.debug("User sync already current: %s", existing_user.email)
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
        session = self._get_session()
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
        session = self._get_session()
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
        session = self._get_session()
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
        session = self._get_session()
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
        session = self._get_session()
        try:
            count = session.query(User).filter_by(is_active=True).count()
            return count
        except SQLAlchemyError as e:
            logger.error(f"Database error in get_all_active_users_count: {e}")
            return 0
        finally:
            session.close()

    # --- Saved Filter Sets -------------------------------------------------
    #
    # Every operation is scoped to the caller's ``firebase_uid``.  A row that
    # belongs to another account is reported as missing rather than forbidden,
    # so one account cannot probe another's identifiers.

    @staticmethod
    def _saved_filter_set_name_taken(
        session,
        firebase_uid: str,
        name: str,
        *,
        exclude_id: Optional[int] = None,
    ) -> bool:
        """Whether the account already holds this name, ignoring case."""

        query = session.query(SavedFilterSet.id).filter(
            SavedFilterSet.firebase_uid == firebase_uid,
            func.lower(SavedFilterSet.name) == name.lower(),
        )
        if exclude_id is not None:
            query = query.filter(SavedFilterSet.id != exclude_id)
        return session.query(query.exists()).scalar()

    @staticmethod
    def _owned_saved_filter_set(
        session,
        firebase_uid: str,
        saved_filter_set_id: int,
    ) -> SavedFilterSet:
        """Load one of the caller's saved filter sets or report it missing."""

        saved_filter_set = (
            session.query(SavedFilterSet)
            .filter(
                SavedFilterSet.id == saved_filter_set_id,
                SavedFilterSet.firebase_uid == firebase_uid,
            )
            .first()
        )
        if saved_filter_set is None:
            raise ResourceNotFoundError(
                "The requested saved filter set was not found."
            )
        return saved_filter_set

    def list_saved_filter_sets(self, firebase_uid: str) -> List[Dict[str, Any]]:
        """Return the caller's saved filter sets, newest first."""

        session = self._get_session()
        try:
            rows = (
                session.query(SavedFilterSet)
                .filter(SavedFilterSet.firebase_uid == firebase_uid)
                .order_by(
                    SavedFilterSet.created_at.desc(),
                    SavedFilterSet.id.desc(),
                )
                .all()
            )
            return [row.to_dict() for row in rows]
        finally:
            session.close()

    def create_saved_filter_set(
        self,
        firebase_uid: str,
        *,
        name: Any,
        query_string: Any,
    ) -> Dict[str, Any]:
        """Save a named query string for the caller and return the new item."""

        validated_name = _validated_saved_filter_set_name(name)
        validated_query_string = _validated_saved_filter_set_query_string(query_string)

        session = self._get_session()
        try:
            held = (
                session.query(SavedFilterSet)
                .filter(SavedFilterSet.firebase_uid == firebase_uid)
                .count()
            )
            if held >= SAVED_FILTER_SET_LIMIT:
                raise ConflictError(
                    "An account may hold at most "
                    f"{SAVED_FILTER_SET_LIMIT} saved filter sets."
                )
            if self._saved_filter_set_name_taken(
                session, firebase_uid, validated_name
            ):
                raise ConflictError(
                    "A saved filter set with that name already exists."
                )

            now = datetime.now(timezone.utc)
            saved_filter_set = SavedFilterSet(
                firebase_uid=firebase_uid,
                name=validated_name,
                query_string=validated_query_string,
                created_at=now,
                updated_at=now,
            )
            session.add(saved_filter_set)
            session.commit()
            return saved_filter_set.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def rename_saved_filter_set(
        self,
        firebase_uid: str,
        saved_filter_set_id: int,
        *,
        name: Any,
    ) -> Dict[str, Any]:
        """Rename one of the caller's saved filter sets.

        The query string is immutable; only the label changes.
        """

        validated_name = _validated_saved_filter_set_name(name)

        session = self._get_session()
        try:
            saved_filter_set = self._owned_saved_filter_set(
                session, firebase_uid, saved_filter_set_id
            )
            if self._saved_filter_set_name_taken(
                session,
                firebase_uid,
                validated_name,
                exclude_id=saved_filter_set.id,
            ):
                raise ConflictError(
                    "A saved filter set with that name already exists."
                )

            saved_filter_set.name = validated_name
            saved_filter_set.updated_at = datetime.now(timezone.utc)
            session.commit()
            return saved_filter_set.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_saved_filter_set(
        self,
        firebase_uid: str,
        saved_filter_set_id: int,
    ) -> None:
        """Delete one of the caller's saved filter sets."""

        session = self._get_session()
        try:
            saved_filter_set = self._owned_saved_filter_set(
                session, firebase_uid, saved_filter_set_id
            )
            session.delete(saved_filter_set)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
