"""
Pytest configuration and fixtures for NBA backend tests
"""
import pytest
from unittest.mock import Mock

@pytest.fixture
def mock_redis_client():
    """Mock Redis client for testing cache functionality"""
    mock_client = Mock()
    mock_client.ping.return_value = True
    mock_client.get.return_value = None
    mock_client.setex.return_value = True
    mock_client.delete.return_value = 1
    mock_client.scan_iter.return_value = iter(['test:key1', 'test:key2'])
    mock_client.info.return_value = {
        'db0': {'keys': 10},
        'used_memory_human': '1.2MB',
        'keyspace_hits': 80,
        'keyspace_misses': 20,
        'uptime_in_seconds': 3600
    }
    return mock_client

@pytest.fixture
def mock_db_engine():
    """Mock database engine for testing"""
    mock_engine = Mock()
    mock_conn = Mock()
    mock_engine.connect.return_value.__enter__ = Mock(return_value=mock_conn)
    mock_engine.connect.return_value.__exit__ = Mock(return_value=None)
    return mock_engine

@pytest.fixture
def mock_game_service(mock_db_engine, mock_redis_client):
    """Mock GameService for testing"""
    from app.services.game_service import GameService
    service = GameService(mock_db_engine, mock_redis_client)
    return service

@pytest.fixture
def sample_player_data():
    """Sample player data for testing"""
    return {
        'player_name': 'LeBron James',
        'season': '2024-25',
        'game_logs': [
            {'GAME_DATE': '2024-01-15', 'PTS': 25, 'REB': 8, 'AST': 7},
            {'GAME_DATE': '2024-01-17', 'PTS': 30, 'REB': 6, 'AST': 9}
        ]
    }

@pytest.fixture
def sample_filter_params():
    """Sample filter parameters for testing"""
    return {
        'season_filter': '2024-25',
        'teams_against': ['LAL'],
        'rank_filter': [5],
        'date_filter': '2024-01-01',
        'location_filter': 'Both',
        'minutes_filter': [20, 48],
        'players_on': [],
        'players_off': [],
        'self_filters': []
    }

@pytest.fixture(autouse=True)
def setup_logging():
    """Setup logging for tests"""
    import logging
    logging.basicConfig(level=logging.DEBUG)
    
@pytest.fixture
def app(monkeypatch):
    """Create a Flask app instance for route tests."""
    from app import create_app

    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setenv("FLASK_ENV", "testing")

    return create_app({
        "TESTING": True,
        "SKIP_FIREBASE_INIT": True,
        "SKIP_TABLE_CREATE": True,
    })


@pytest.fixture
def client(app):
    """Create a Flask test client."""
    return app.test_client()


SEEDED_PLAYER_NAMES = ["LeBron James", "Stephen Curry", "Nikola Jokic"]


@pytest.fixture
def seeded_db_url(tmp_path):
    """Create a temporary SQLite database holding known player rows.

    Route tests assert against these exact names so that a query returning an
    empty result is a failure rather than a pass.
    """
    from sqlalchemy import create_engine, text

    path = tmp_path / "seeded.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE player_play_types ("PLAYER_NAME" TEXT)'))
        for name in SEEDED_PLAYER_NAMES:
            connection.execute(
                text('INSERT INTO player_play_types ("PLAYER_NAME") VALUES (:name)'),
                {"name": name},
            )
    engine.dispose()
    return f"sqlite:///{path}"


@pytest.fixture
def empty_db_url(tmp_path):
    """Point at a temporary SQLite database with no tables at all."""
    return f"sqlite:///{tmp_path / 'empty.db'}"


@pytest.fixture
def make_client(monkeypatch):
    """Build a test client whose services are bound to a given database URL.

    Services resolve their engine from the app's runtime settings, so the
    database is chosen at app-creation time rather than by patching a service.
    """

    def _make_client(database_url):
        from app import create_app

        monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
        monkeypatch.setenv("FLASK_ENV", "testing")

        app = create_app({
            "TESTING": True,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
            "DATABASE_URL": database_url,
        })
        return app.test_client()

    return _make_client


@pytest.fixture
def make_db_user():
    """Build an unpersisted User row for routes that serialize the DB user."""

    def _make_db_user(**overrides):
        from app.models.user import User

        fields = {
            "firebase_uid": "test-uid",
            "email": "user@example.com",
            "display_name": "Test User",
            "photo_url": None,
            "is_active": True,
        }
        fields.update(overrides)
        return User(**fields)

    return _make_db_user


@pytest.fixture
def authenticate(monkeypatch):
    """Return a callable that installs a verified Firebase identity.

    Avoids Firebase credentials entirely: the token verifier and the user-sync
    call are both replaced, so routes see a fully authenticated request.
    """

    def _authenticate(claims=None, db_user=None):
        import app.utils.auth as auth

        token_claims = {
            "uid": "test-uid",
            "email": "user@example.com",
            "name": "Test User",
            "picture": None,
        }
        token_claims.update(claims or {})

        monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
        monkeypatch.setattr(auth, "verify_firebase_token", lambda token: token_claims)
        monkeypatch.setattr(
            auth.UserService,
            "create_or_update_user",
            lambda self, user_data: db_user,
        )
        return {"Authorization": "Bearer test-token"}

    return _authenticate
