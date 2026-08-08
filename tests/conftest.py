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
