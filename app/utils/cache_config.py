"""Cache configuration for NBA Backend API.

This module configures a Redis client using Railway-style environment
variables. It first attempts to read a single `REDIS_URL` (e.g.,
`redis://:password@host:port/0` or `rediss://...`), and falls back to
individual settings: `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, and
`REDIS_PASSWORD`. TLS is automatically enabled when using a `rediss://`
scheme, or when `REDIS_TLS=true`.
"""

import os
import redis
import logging
import pytz
from datetime import datetime, timezone, timedelta, time
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
    """Create a Redis client from Railway environment variables.

    Precedence:
    1) `REDIS_URL` (e.g., redis://:password@host:port/db or rediss://...)
    2) Individual vars: `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_PASSWORD`
       Optional TLS via `REDIS_TLS=true`.

    Returns
    -------
    Optional[redis.Redis]
        A configured Redis client, or None if connection fails.
    """

    try:
        redis_url = os.getenv("REDIS_URL")

        if redis_url:
            # Use URL-based configuration; respects rediss:// for TLS
            client = redis.from_url(
                redis_url,
                socket_timeout=5,
                socket_connect_timeout=5,
                health_check_interval=30,
            )
        else:
            # Fallback to individual env vars
            redis_host = os.getenv("REDISHOST", "localhost")
            redis_port = int(os.getenv("REDISPORT", 6379))
            redis_db = int(os.getenv("REDISDB", 0))
            redis_password = os.getenv("REDISPASSWORD")
            use_tls = os.getenv("REDISTLS", "false").lower() in {"1", "true", "yes", "on"}

            client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                password=redis_password,
                ssl=use_tls,
                socket_timeout=5,
                socket_connect_timeout=5,
                health_check_interval=30,
            )

        # Test connection
        client.ping()

        # Log where we connected (mask password if using URL)
        if redis_url:
            logger.info("Redis connected successfully via REDIS_URL")
        else:
            logger.info("Redis connected successfully via host/port/db env vars")

        return client

    except redis.ConnectionError as exc:
        logger.warning(f"Redis connection failed: {exc}. Cache will be disabled.")
        return None
    except Exception as exc:
        logger.error(f"Redis setup error: {exc}. Cache will be disabled.")
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

def get_next_1am_cst_timestamp() -> int:
    """
    Get the Unix timestamp for 1 AM CST the following day.
    
    This is used for setting cache expiration to occur at a specific time
    rather than using a fixed TTL duration.
    
    Returns:
        int: Unix timestamp for next 1 AM CST
    """
    cst = pytz.timezone('US/Central')
    now = datetime.now(cst)
    
    # Get tomorrow's date
    tomorrow = now.date() + timedelta(days=1)
    
    # Create 1 AM CST tomorrow
    next_1am = cst.localize(datetime.combine(tomorrow, time(1, 0)))
    
    return int(next_1am.timestamp())

def set_cache_with_1am_expiry(redis_client: redis.Redis, key: str, value: str) -> bool:
    """
    Set a cache value that expires at 1 AM CST the following day.
    
    Args:
        redis_client: Redis client instance
        key: Cache key
        value: Cache value
        
    Returns:
        bool: True if successful, False otherwise
    """
    if not redis_client:
        return False
        
    try:
        # Set the value
        redis_client.set(key, value)
        
        # Set expiration to 1 AM CST tomorrow
        expire_timestamp = get_next_1am_cst_timestamp()
        redis_client.expireat(key, expire_timestamp)
        
        logger.debug(f"Cache key '{key}' set to expire at 1 AM CST tomorrow (timestamp: {expire_timestamp})")
        return True
        
    except Exception as e:
        logger.error(f"Failed to set cache with 1 AM expiry: {e}")
        return False
