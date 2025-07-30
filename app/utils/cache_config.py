"""
Cache configuration for NBA Backend API

This module provides Redis client configuration and cache-related utilities
for the NBA statistics caching system.
"""

import os
import redis
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Cache prefixes for different data types
CACHE_PREFIXES = {
    'player_logs_daily': 'nba:player_logs',
    'team_stats_daily': 'nba:team_stats', 
    'season_data': 'nba:season',
    'computed': 'nba:computed',
    'table_data': 'nba:table'
}

# Cache TTL configurations (in seconds)
CACHE_TTLS = {
    'daily_nba_data': 6 * 60 * 60,      # 6 hours - current season data
    'season_historical': 30 * 24 * 60 * 60,  # 30 days - historical data
    'intraday_computed': 2 * 60 * 60,    # 2 hours - computed/derived data  
    'player_info': 24 * 60 * 60,        # 24 hours - player information
}

def get_redis_client() -> Optional[redis.Redis]:
    """
    Get Redis client instance with configuration from environment variables.
    
    Returns:
        redis.Redis: Configured Redis client or None if connection fails
    """
    try:
        # Get Redis configuration from environment variables
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        redis_port = int(os.getenv('REDIS_PORT', 6379))
        redis_db = int(os.getenv('REDIS_DB', 0))
        redis_password = os.getenv('REDIS_PASSWORD', None)
        
        # Create Redis client
        client = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30
        )
        
        # Test connection
        client.ping()
        logger.info(f"Redis connected successfully: {redis_host}:{redis_port}/{redis_db}")
        return client
        
    except redis.ConnectionError as e:
        logger.warning(f"Redis connection failed: {e}. Cache will be disabled.")
        return None
    except Exception as e:
        logger.error(f"Redis setup error: {e}. Cache will be disabled.")
        return None

def get_cache_date_key() -> str:
    """
    Generate a date-based cache key component.
    
    For current season data, this helps ensure cache expires daily.
    
    Returns:
        str: Date string in YYYY-MM-DD format
    """
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

def is_cache_enabled() -> bool:
    """
    Check if caching is enabled via environment variable.
    
    Returns:
        bool: True if caching is enabled, False otherwise
    """
    return os.getenv('ENABLE_CACHE', 'true').lower() in ('true', '1', 'yes', 'on')

def get_ttl_for_cache_type(cache_type: str) -> int:
    """
    Get TTL (time-to-live) for a specific cache type.
    
    Args:
        cache_type: Type of cache data
        
    Returns:
        int: TTL in seconds
    """
    return CACHE_TTLS.get(cache_type, CACHE_TTLS['intraday_computed'])