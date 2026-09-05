"""
User service for managing user accounts and synchronization with Firebase.

This service handles user account operations using SQLAlchemy ORM,
including creating, updating, and retrieving user information.
"""

from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone
from math import isfinite
import logging
import re
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config.settings import RuntimeSettings, get_runtime_settings
from app.domain.nba_teams import (
    NBA_TEAM_TRICODES,
    canonical_nba_team_abbreviation,
)
from app.errors import ConflictError, InvalidInputError, ResourceNotFoundError
from app.models import get_session, SavedFilterSet, Target, TargetQualifier, User
from app.models.saved_filter_set import (
    SAVED_FILTER_SET_NAME_MAX_LENGTH,
    SAVED_FILTER_SET_QUERY_STRING_MAX_LENGTH,
)
from app.models.target import (
    TARGET_COMPARATOR_SYMBOLS,
    TARGET_NOTE_MAX_LENGTH,
    target_qualifier_signature,
)
from app.services.player_diet import PLAYER_DIET_QUALIFIER_SLICES
from app.utils.db import get_engine

logger = logging.getLogger(__name__)

USER_LOGIN_TOUCH_INTERVAL = timedelta(minutes=15)

SAVED_FILTER_SET_LIMIT = 100

SAVED_FILTER_SET_DUPLICATE_NAME_MESSAGE = (
    "A saved filter set with that name already exists."
)

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


TARGET_LIMIT = 50
TARGET_QUALIFIER_LIMIT = 10

TARGET_DUPLICATE_MESSAGE = (
    "A target for that opponent already has those qualifiers."
)
_TARGET_OPPONENT_MESSAGE = "A target needs one NBA team as its opponent."
_TARGET_QUALIFIER_MESSAGE = (
    "A qualifier needs a known diet base, a slice of that base, a comparator "
    "of at_or_above or at_or_below, and a threshold share between 0 and 1."
)


def _validated_target_opponent(value: Any) -> str:
    """Return the canonical tricode, or refuse an opponent that is not a team."""

    if not isinstance(value, str):
        raise InvalidInputError(_TARGET_OPPONENT_MESSAGE)

    opponent = canonical_nba_team_abbreviation(value)
    if opponent not in NBA_TEAM_TRICODES:
        raise InvalidInputError(_TARGET_OPPONENT_MESSAGE)
    return opponent


def _validated_target_threshold(value: Any) -> float:
    """Return the share, or refuse one that is not a number within 0-1.

    ``bool`` is excluded deliberately: it is a subclass of ``int``, so ``True``
    would otherwise read as the share 1.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInputError(_TARGET_QUALIFIER_MESSAGE)

    threshold = float(value)
    if not isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise InvalidInputError(_TARGET_QUALIFIER_MESSAGE)
    return threshold


def _validated_target_qualifier(value: Any) -> Dict[str, Any]:
    """Return one normalized Qualifier, or refuse one the catalogue rejects."""

    if not isinstance(value, dict):
        raise InvalidInputError(_TARGET_QUALIFIER_MESSAGE)

    base = value.get("base")
    if not isinstance(base, str) or base not in PLAYER_DIET_QUALIFIER_SLICES:
        raise InvalidInputError(_TARGET_QUALIFIER_MESSAGE)

    # The slice has to belong to this base, so a real slice key borrowed from
    # another base is refused rather than stored against a base that never
    # publishes it.
    slice_key = value.get("slice_key")
    if (
        not isinstance(slice_key, str)
        or slice_key not in PLAYER_DIET_QUALIFIER_SLICES[base]
    ):
        raise InvalidInputError(_TARGET_QUALIFIER_MESSAGE)

    comparator = value.get("comparator")
    if (
        not isinstance(comparator, str)
        or comparator not in TARGET_COMPARATOR_SYMBOLS
    ):
        raise InvalidInputError(_TARGET_QUALIFIER_MESSAGE)

    return {
        "base": base,
        "slice_key": slice_key,
        "comparator": comparator,
        "threshold": _validated_target_threshold(value.get("threshold")),
    }


def _validated_target_qualifiers(value: Any) -> List[Dict[str, Any]]:
    """Return the caller's Qualifiers in order, or refuse an unusable set."""

    if not isinstance(value, list) or not value:
        raise InvalidInputError("A target needs at least one qualifier.")

    if len(value) > TARGET_QUALIFIER_LIMIT:
        raise InvalidInputError(
            f"A target may hold at most {TARGET_QUALIFIER_LIMIT} qualifiers."
        )

    qualifiers = [_validated_target_qualifier(item) for item in value]
    identities = [
        (
            qualifier["base"],
            qualifier["slice_key"],
            qualifier["comparator"],
            qualifier["threshold"],
        )
        for qualifier in qualifiers
    ]
    if len(set(identities)) != len(identities):
        raise InvalidInputError("A target cannot repeat the same qualifier.")
    return qualifiers


def _validated_target_note(value: Any) -> Optional[str]:
    """Return the trimmed note, or refuse one the contract cannot store.

    A note is optional, and one that is only whitespace is stored as absent
    rather than as an empty string.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInputError("A target note must be text.")

    note = value.strip()
    if not note:
        return None
    if len(note) > TARGET_NOTE_MAX_LENGTH:
        raise InvalidInputError(
            f"A target note may be at most {TARGET_NOTE_MAX_LENGTH} characters."
        )
    return note


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

    def _commit_unique_name(
        self,
        session,
        firebase_uid: str,
        name: str,
        *,
        exclude_id: Optional[int] = None,
    ) -> None:
        """Commit a write whose only conflict can be a duplicate name.

        The pre-insert check cannot see a row a competing transaction has not
        committed yet, so ``uq_saved_filter_sets_owner_name`` is the real
        arbiter.  Re-reading after the rollback tells a lost uniqueness race
        (the caller's 409) apart from any other integrity failure, such as an
        owner that no longer exists, which stays a server error.
        """

        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            if self._saved_filter_set_name_taken(
                session, firebase_uid, name, exclude_id=exclude_id
            ):
                raise ConflictError(
                    SAVED_FILTER_SET_DUPLICATE_NAME_MESSAGE, detail=error
                ) from error
            raise

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
            # Advisory under concurrency: the cap has no database backstop, so
            # two simultaneous writes can both observe room and leave the
            # account one over. Deliberate -- a bookmark limit does not justify
            # locking or a counter table.
            if held >= SAVED_FILTER_SET_LIMIT:
                raise ConflictError(
                    "An account may hold at most "
                    f"{SAVED_FILTER_SET_LIMIT} saved filter sets."
                )
            if self._saved_filter_set_name_taken(
                session, firebase_uid, validated_name
            ):
                raise ConflictError(SAVED_FILTER_SET_DUPLICATE_NAME_MESSAGE)

            now = datetime.now(timezone.utc)
            saved_filter_set = SavedFilterSet(
                firebase_uid=firebase_uid,
                name=validated_name,
                query_string=validated_query_string,
                created_at=now,
                updated_at=now,
            )
            session.add(saved_filter_set)
            self._commit_unique_name(session, firebase_uid, validated_name)
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
                raise ConflictError(SAVED_FILTER_SET_DUPLICATE_NAME_MESSAGE)

            renamed_id = saved_filter_set.id
            saved_filter_set.name = validated_name
            saved_filter_set.updated_at = datetime.now(timezone.utc)
            self._commit_unique_name(
                session, firebase_uid, validated_name, exclude_id=renamed_id
            )
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

    # --- Targets -----------------------------------------------------------
    #
    # Every operation is scoped to the caller's ``firebase_uid``.  A row that
    # belongs to another account is reported as missing rather than forbidden,
    # so one account cannot probe another's identifiers.

    @staticmethod
    def _target_signature_taken(
        session,
        firebase_uid: str,
        opponent: str,
        signature: str,
        *,
        exclude_id: Optional[int] = None,
    ) -> bool:
        """Whether the account already aims this Qualifier set at this team."""

        query = session.query(Target.id).filter(
            Target.firebase_uid == firebase_uid,
            Target.opponent == opponent,
            Target.qualifier_signature == signature,
        )
        if exclude_id is not None:
            query = query.filter(Target.id != exclude_id)
        return session.query(query.exists()).scalar()

    def _commit_unique_target(
        self,
        session,
        firebase_uid: str,
        opponent: str,
        signature: str,
        *,
        exclude_id: Optional[int] = None,
    ) -> None:
        """Commit a write whose only conflict can be a duplicate Target.

        The pre-insert check cannot see a row a competing transaction has not
        committed yet, so ``uq_targets_owner_opponent_signature`` is the real
        arbiter.  Re-reading after the rollback tells a lost uniqueness race
        (the caller's 409) apart from any other integrity failure, such as an
        owner that does not exist, which stays a server error.
        """

        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            if self._target_signature_taken(
                session, firebase_uid, opponent, signature, exclude_id=exclude_id
            ):
                raise ConflictError(
                    TARGET_DUPLICATE_MESSAGE, detail=error
                ) from error
            raise

    @staticmethod
    def _owned_target(session, firebase_uid: str, target_id: int) -> Target:
        """Load one of the caller's targets or report it missing."""

        target = (
            session.query(Target)
            .filter(Target.id == target_id, Target.firebase_uid == firebase_uid)
            .first()
        )
        if target is None:
            raise ResourceNotFoundError("The requested target was not found.")
        return target

    def list_targets(self, firebase_uid: str) -> List[Dict[str, Any]]:
        """Return the caller's targets, newest first."""

        session = self._get_session()
        try:
            rows = (
                session.query(Target)
                .filter(Target.firebase_uid == firebase_uid)
                .order_by(Target.created_at.desc(), Target.id.desc())
                .all()
            )
            return [row.to_dict() for row in rows]
        finally:
            session.close()

    def create_target(
        self,
        firebase_uid: str,
        *,
        opponent: Any,
        qualifiers: Any,
        note: Any = None,
    ) -> Dict[str, Any]:
        """Create a target for the caller and return the new item."""

        validated_opponent = _validated_target_opponent(opponent)
        validated_qualifiers = _validated_target_qualifiers(qualifiers)
        validated_note = _validated_target_note(note)
        signature = target_qualifier_signature(validated_qualifiers)

        session = self._get_session()
        try:
            held = (
                session.query(Target)
                .filter(Target.firebase_uid == firebase_uid)
                .count()
            )
            # Advisory under concurrency, like the saved filter set cap: two
            # simultaneous writes can both observe room and leave the account
            # one over.  Deliberate -- a per-account limit does not justify
            # locking or a counter table.
            if held >= TARGET_LIMIT:
                raise ConflictError(
                    f"An account may hold at most {TARGET_LIMIT} targets."
                )
            if self._target_signature_taken(
                session, firebase_uid, validated_opponent, signature
            ):
                raise ConflictError(TARGET_DUPLICATE_MESSAGE)

            now = datetime.now(timezone.utc)
            target = Target(
                firebase_uid=firebase_uid,
                opponent=validated_opponent,
                note=validated_note,
                qualifier_signature=signature,
                created_at=now,
                updated_at=now,
                qualifiers=[
                    TargetQualifier(position=position, **qualifier)
                    for position, qualifier in enumerate(validated_qualifiers)
                ],
            )
            session.add(target)
            self._commit_unique_target(
                session, firebase_uid, validated_opponent, signature
            )
            return target.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_target(
        self,
        firebase_uid: str,
        target_id: int,
        *,
        changes: Any,
    ) -> Dict[str, Any]:
        """Edit one of the caller's targets and return the updated item.

        Only the Qualifiers and the note are editable.  A target's opponent is
        fixed: aiming the same criteria at another team is a different target,
        so any ``opponent`` key in ``changes`` is ignored.  Absent keys mean
        unchanged, which is why ``note`` is read by presence rather than by
        value -- ``None`` clears it.
        """

        if not isinstance(changes, dict):
            raise InvalidInputError("No target changes were provided.")

        editable = {
            key: changes[key] for key in ("qualifiers", "note") if key in changes
        }
        if not editable:
            raise InvalidInputError(
                "A target update must change its qualifiers or its note."
            )

        validated_qualifiers = (
            _validated_target_qualifiers(editable["qualifiers"])
            if "qualifiers" in editable
            else None
        )
        validated_note = (
            _validated_target_note(editable["note"]) if "note" in editable else None
        )

        session = self._get_session()
        try:
            target = self._owned_target(session, firebase_uid, target_id)
            edited_id = target.id
            opponent = target.opponent
            signature = target.qualifier_signature

            if validated_qualifiers is not None:
                signature = target_qualifier_signature(validated_qualifiers)
                if self._target_signature_taken(
                    session,
                    firebase_uid,
                    opponent,
                    signature,
                    exclude_id=edited_id,
                ):
                    raise ConflictError(TARGET_DUPLICATE_MESSAGE)
                target.qualifier_signature = signature
                # Replacing the collection deletes the previous rows through
                # ``delete-orphan`` rather than leaving them behind.
                target.qualifiers = [
                    TargetQualifier(position=position, **qualifier)
                    for position, qualifier in enumerate(validated_qualifiers)
                ]
            if "note" in editable:
                target.note = validated_note
            target.updated_at = datetime.now(timezone.utc)

            self._commit_unique_target(
                session,
                firebase_uid,
                opponent,
                signature,
                exclude_id=edited_id,
            )
            return target.to_dict()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_target(self, firebase_uid: str, target_id: int) -> None:
        """Delete one of the caller's targets and its qualifiers."""

        session = self._get_session()
        try:
            target = self._owned_target(session, firebase_uid, target_id)
            session.delete(target)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
