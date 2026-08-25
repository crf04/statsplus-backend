"""Cache configuration for NBA Backend API.

This module configures a Redis client using Railway-style environment
variables. It first attempts to read a single `REDIS_URL` (e.g.,
`redis://:password@host:port/0` or `rediss://...`), and falls back to
individual settings: `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, and
`REDIS_PASSWORD`. TLS is automatically enabled when using a `rediss://`
scheme, or when `REDIS_TLS=true`.
"""

import redis
import logging
from datetime import datetime, timezone
from typing import Optional

from app.config.settings import RuntimeSettings, get_runtime_settings

logger = logging.getLogger(__name__)

# Cache TTL configurations (in seconds)
CACHE_TTLS = {
    'intraday_computed': 2 * 60 * 60,    # 2 hours - computed/derived data  
    'player_info': 24 * 60 * 60,        # 24 hours - player information
}

def get_redis_client(settings: RuntimeSettings | None = None) -> Optional[redis.Redis]:
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

    runtime_settings = settings or get_runtime_settings()

    try:
        redis_url = runtime_settings.cache.url

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
            redis_host = runtime_settings.cache.host
            redis_port = runtime_settings.cache.port
            redis_db = runtime_settings.cache.database
            redis_password = runtime_settings.cache.password
            use_tls = runtime_settings.cache.tls

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

def is_cache_enabled(settings: RuntimeSettings | None = None) -> bool:
    """
    Check if caching is enabled via environment variable.
    
    Returns:
        bool: True if caching is enabled, False otherwise
    """
    runtime_settings = settings or get_runtime_settings()
    return runtime_settings.cache.enabled

def get_ttl_for_cache_type(cache_type: str) -> int:
    """
    Get TTL (time-to-live) for a specific cache type.
    
    Args:
        cache_type: Type of cache data
        
    Returns:
        int: TTL in seconds
    """
    return CACHE_TTLS.get(cache_type, CACHE_TTLS['intraday_computed'])
