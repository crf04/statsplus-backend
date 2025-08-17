"""
User model for storing Firebase user account information.

This model stores basic user account data synchronized from Firebase
authentication to enable local user management and future features.
"""

from sqlalchemy import Column, String, DateTime, Boolean, Index
from sqlalchemy.sql import func
from datetime import datetime
from . import Base

class User(Base):
    """
    User model for storing Firebase user account information.
    
    This table stores basic user data from Firebase authentication
    for faster access and to enable account-based features.
    """
    
    __tablename__ = 'users'
    
    # Primary key - Firebase UID is unique and immutable
    firebase_uid = Column(String(128), primary_key=True, comment="Firebase user unique identifier")
    
    # User profile information from Firebase
    email = Column(String(255), nullable=False, unique=True, comment="User email address")
    display_name = Column(String(255), nullable=True, comment="User display name from Firebase")
    photo_url = Column(String(500), nullable=True, comment="User profile photo URL")
    
    # Timestamps for tracking user activity
    created_at = Column(
        DateTime(timezone=True), 
        nullable=False, 
        server_default=func.now(),
        comment="When the user account was first created"
    )
    last_login = Column(
        DateTime(timezone=True), 
        nullable=False, 
        server_default=func.now(),
        comment="Most recent login timestamp"
    )
    
    # Account status
    is_active = Column(
        Boolean, 
        nullable=False, 
        default=True,
        comment="Whether the user account is active"
    )
    
    # Add indexes for performance
    __table_args__ = (
        Index('idx_users_email', 'email'),
        Index('idx_users_last_login', 'last_login'),
        Index('idx_users_created_at', 'created_at'),
    )
    
    def __repr__(self):
        return f"<User(firebase_uid='{self.firebase_uid}', email='{self.email}', display_name='{self.display_name}')>"
    
    def to_dict(self):
        """
        Convert User model to dictionary for JSON serialization.
        
        Returns:
            dict: User data as dictionary
        """
        return {
            'firebase_uid': self.firebase_uid,
            'email': self.email,
            'display_name': self.display_name,
            'photo_url': self.photo_url,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'is_active': self.is_active
        }
    
    @classmethod
    def from_firebase_user(cls, firebase_user_data):
        """
        Create User instance from Firebase user data.
        
        Args:
            firebase_user_data (dict): Firebase user information
            
        Returns:
            User: New User instance
        """
        return cls(
            firebase_uid=firebase_user_data.get('uid'),
            email=firebase_user_data.get('email'),
            display_name=firebase_user_data.get('name') or firebase_user_data.get('display_name'),
            photo_url=firebase_user_data.get('picture') or firebase_user_data.get('photo_url'),
            last_login=datetime.utcnow()
        )