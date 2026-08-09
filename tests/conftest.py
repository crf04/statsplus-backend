"""
Pytest configuration and fixtures for NBA backend tests
"""
import pytest
from unittest.mock import Mock
from types import SimpleNamespace

from app.config.settings import AuthenticationSettings, CacheSettings, RuntimeSettings

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
    mock_conn.execute.return_value.scalar.return_value = 1
    mock_engine.dialect.name = "sqlite"
    mock_engine.dialect.driver = "pysqlite"
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
def runtime_settings():
    """Credential-free settings shared by the app and its injected dependencies."""
    return RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
    )


@pytest.fixture
def dependencies(runtime_settings, mock_db_engine):
    """Replaceable application dependency graph for route tests."""
    services = {
        name: Mock(name=f"{name}_service")
        for name in ("game", "player", "team", "data", "nl", "user")
    }
    services["data_refresh_jobs"] = Mock(name="data_refresh_jobs_service")
    services["provider_health"] = Mock(name="provider_health_service")
    services["provider_health"].check_database.return_value = {
        "status": "healthy",
        "dialect": "sqlite",
        "driver": "pysqlite",
    }
    services["provider_health"].check_nba_api.return_value = {
        "status": "healthy",
        "provider": "nba_stats",
    }
    services["provider_health"].check_pbp_api.return_value = {
        "status": "healthy",
        "provider": "pbp_stats",
    }
    services["provider_health"].detailed.return_value = {
        "status": "healthy",
        "checks": {
            "database": {"status": "healthy"},
            "nba_api": {"status": "healthy", "provider": "nba_stats"},
            "pbp_stats": {"status": "healthy", "provider": "pbp_stats"},
        },
    }
    for service in services.values():
        service.settings = runtime_settings
    services["player"].get_all_players.return_value = []
    services["team"].get_all_teams.return_value = []
    services["user"].create_or_update_user.return_value = None
    return SimpleNamespace(
        settings=runtime_settings,
        engine=mock_db_engine,
        redis_client=None,
        nba_stats_provider=Mock(name="nba_stats_provider"),
        pbp_stats_provider=Mock(name="pbp_stats_provider"),
        **{f"{name}_service": service for name, service in services.items()},
    )


@pytest.fixture
def app(monkeypatch, runtime_settings, dependencies):
    """Create a Flask app instance for route tests."""
    from app import create_app

    monkeypatch.setenv("FIREBASE_ADMIN_DISABLED", "true")
    monkeypatch.setenv("FLASK_ENV", "testing")

    return create_app({
        "TESTING": True,
        "RUNTIME_SETTINGS": runtime_settings,
        "DEPENDENCIES": dependencies,
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
        # Auth resolves the user service through the app dependency registry,
        # so replace the lookup rather than the service class.
        monkeypatch.setattr(
            auth,
            "get_dependencies",
            lambda: SimpleNamespace(
                user_service=SimpleNamespace(
                    create_or_update_user=lambda user_data: db_user
                )
            ),
        )
        return {"Authorization": "Bearer test-token"}

    return _authenticate
