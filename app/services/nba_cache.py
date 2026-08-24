"""
NBA Game Cache

This module provides caching functionality for NBA API calls and data processing.
Uses Redis for distributed caching with intelligent TTL management.
"""

import json
import pickle
import logging
import hashlib
from typing import Any, Optional, Union, Callable
from functools import wraps

import redis
import pandas as pd

from ..utils.cache_config import (
    CACHE_PREFIXES, 
    CACHE_TTLS,
    get_cache_date_key,
    is_cache_enabled
)
from app.config.settings import RuntimeSettings, get_runtime_settings

logger = logging.getLogger(__name__)

class NBAGameCache:
    """
    NBA Game caching system with Redis backend.
    
    Provides intelligent caching for NBA API calls with different TTL strategies
    based on data type (current season vs historical, computed vs raw API data).
    """
    
    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        settings: RuntimeSettings | None = None,
    ):
        """
        Initialize NBA cache with Redis client.
        
        Args:
            redis_client: Redis client instance. If None, caching is disabled.
        """
        self.settings = settings or get_runtime_settings()
        self.redis_client = redis_client
        self.enabled = redis_client is not None and is_cache_enabled(self.settings)
        
        if self.enabled:
            logger.info("NBA cache initialized with Redis backend")
        else:
            logger.info("NBA cache disabled (no Redis client or cache disabled via config)")
    
    def _generate_key(self, prefix: str, include_date: bool = False, 
                     function_name: str = '', *args, **kwargs) -> str:
        """
        Generate a cache key with consistent format.
        
        Args:
            prefix: Cache prefix from CACHE_PREFIXES
            include_date: Whether to include date in key for daily expiration
            function_name: Name of the function being cached
            *args, **kwargs: Function arguments to include in key
            
        Returns:
            str: Generated cache key
        """
        # Create a deterministic key from arguments
        key_parts = [prefix, function_name]
        
        if include_date:
            key_parts.append(get_cache_date_key())
            
        # Add function arguments to key
        if args:
            key_parts.extend([str(arg) for arg in args])
        if kwargs:
            # Sort kwargs for consistent key generation
            for k, v in sorted(kwargs.items()):
                key_parts.extend([k, str(v)])
        
        # Create hash of the key to avoid Redis key length limits
        key_string = ':'.join(key_parts)
        key_hash = hashlib.md5(key_string.encode()).hexdigest()
        
        return f"{prefix}:{key_hash}"
    
    def _serialize_data(self, data: Any) -> bytes:
        """
        Serialize data for Redis storage.
        
        Args:
            data: Data to serialize
            
        Returns:
            bytes: Serialized data
        """
        try:
            # Check if data contains pandas DataFrame (recursively)
            if self._contains_dataframe(data):
                # Use pickle for any data containing DataFrames
                return pickle.dumps(data)
            else:
                # Use JSON for simple data types
                try:
                    return json.dumps(data, default=str).encode('utf-8')
                except (TypeError, ValueError):
                    # Fallback to pickle if JSON fails
                    return pickle.dumps(data)
        except Exception as e:
            logger.error(f"Data serialization failed: {e}")
            raise
    
    def _contains_dataframe(self, data: Any) -> bool:
        """
        Check if data structure contains a pandas DataFrame.
        
        Args:
            data: Data to check
            
        Returns:
            bool: True if contains DataFrame, False otherwise
        """
        if isinstance(data, pd.DataFrame):
            return True
        elif isinstance(data, (list, tuple)):
            return any(self._contains_dataframe(item) for item in data)
        elif isinstance(data, dict):
            return any(self._contains_dataframe(value) for value in data.values())
        return False
    
    def _deserialize_data(self, data: Union[bytes, str]) -> Any:
        """
        Deserialize data from Redis.
        
        Args:
            data: Serialized data from Redis (bytes or str)
            
        Returns:
            Any: Deserialized data
        """
        try:
            # Handle bytes data (expected format after removing decode_responses=True)
            if isinstance(data, bytes):
                # Try pickle first (for DataFrames)
                try:
                    return pickle.loads(data)
                except (pickle.PickleError, TypeError):
                    # Fallback to JSON
                    return json.loads(data.decode('utf-8'))
            
            # Handle string data (legacy compatibility)
            elif isinstance(data, str):
                # Try JSON first for string data
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    # If JSON fails, data might be corrupted - log and raise
                    logger.error(f"Failed to parse cached string data as JSON: {data[:100]}...")
                    raise ValueError("Corrupted cache data - cannot deserialize string data")
            
            else:
                logger.error(f"Unexpected data type from cache: {type(data)}")
                raise TypeError(f"Cannot deserialize data of type {type(data)}")
                
        except Exception as e:
            logger.error(f"Data deserialization failed: {e}")
            raise
    
    def get(self, cache_key: str) -> Optional[Any]:
        """
        Get data from cache.
        
        Args:
            cache_key: Redis cache key
            
        Returns:
            Any: Cached data or None if not found/expired
        """
        if not self.enabled:
            return None
            
        try:
            cached_data = self.redis_client.get(cache_key)
            if cached_data is None:
                return None
                
            return self._deserialize_data(cached_data)
            
        except Exception as e:
            logger.error(f"Cache get failed for key {cache_key}: {e}")
            return None
    
    def set(self, cache_key: str, data: Any, ttl: int) -> bool:
        """
        Set data in cache with TTL.
        
        Args:
            cache_key: Redis cache key
            data: Data to cache
            ttl: Time-to-live in seconds
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self.enabled:
            return False
            
        try:
            serialized_data = self._serialize_data(data)
            result = self.redis_client.setex(cache_key, ttl, serialized_data)
            
            if result:
                logger.debug(f"Cache set successful for key {cache_key} (TTL: {ttl}s)")
            return result
            
        except Exception as e:
            logger.error(f"Cache set failed for key {cache_key}: {e}")
            return False
    
    def delete(self, cache_key: str) -> bool:
        """
        Delete data from cache.
        
        Args:
            cache_key: Redis cache key
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self.enabled:
            return False
            
        try:
            result = self.redis_client.delete(cache_key)
            logger.debug(f"Cache delete for key {cache_key}")
            return bool(result)
            
        except Exception as e:
            logger.error(f"Cache delete failed for key {cache_key}: {e}")
            return False
    
    def _get_ttl(self, cache_type: str) -> int:
        """
        Get TTL for cache type.
        
        Args:
            cache_type: Type of cache from CACHE_TTLS
            
        Returns:
            int: TTL in seconds
        """
        return CACHE_TTLS.get(cache_type, CACHE_TTLS['intraday_computed'])
    
    def _is_current_season(self, season: str) -> bool:
        """
        Check if season is the current NBA season.
        
        Args:
            season: Season string like '2024-25'
            
        Returns:
            bool: True if current season, False otherwise
        """
        # Compare against the one startup-derived season rather than repeating
        # calendar arithmetic in each cache/request path.
        try:
            return season == self.settings.nba.current_season
        except (AttributeError, TypeError):
            logger.warning(f"Could not parse season string: {season}")
            return False
    
    def cache_player_logs(self):
        """
        Decorator factory for caching player log data.
        
        Returns:
            Callable: Decorator function
        """
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs):
                if not self.enabled:
                    return func(*args, **kwargs)
                
                # Generate cache key
                cache_key = self._generate_key(
                    CACHE_PREFIXES['player_logs_daily'],
                    True,  # include_date for daily expiration
                    func.__name__,
                    *args, **kwargs
                )
                
                # Check cache first
                cached_result = self.get(cache_key)
                if cached_result is not None:
                    logger.debug(f"Cache hit for player logs: {func.__name__}")
                    return cached_result
                
                # Cache miss - call function
                logger.info(f"Cache miss for player logs: {func.__name__} - making NBA API call")
                result = func(*args, **kwargs)
                
                # Cache the result
                ttl = self._get_ttl('daily_nba_data')
                self.set(cache_key, result, ttl)
                
                return result
            return wrapper
        return decorator
    
    def clear_cache_pattern(self, pattern: str) -> int:
        """
        Clear cache entries matching a pattern.
        
        Args:
            pattern: Redis key pattern (supports wildcards)
            
        Returns:
            int: Number of keys deleted
        """
        if not self.enabled:
            return 0
            
        try:
            keys = self.redis_client.keys(pattern)
            if keys:
                deleted = self.redis_client.delete(*keys)
                logger.info(f"Cleared {deleted} cache entries matching pattern: {pattern}")
                return deleted
            return 0
            
        except Exception as e:
            logger.error(f"Cache clear failed for pattern {pattern}: {e}")
            return 0
    
    def get_cache_stats(self) -> dict:
        """
        Get cache statistics.
        
        Returns:
            dict: Cache statistics
        """
        if not self.enabled:
            return {"enabled": False}
            
        try:
            info = self.redis_client.info()
            stats = {
                "enabled": True,
                "connected_clients": info.get("connected_clients", 0),
                "used_memory_human": info.get("used_memory_human", "Unknown"),
                "keyspace_hits": info.get("keyspace_hits", 0),
                "keyspace_misses": info.get("keyspace_misses", 0),
                "evicted_keys": info.get("evicted_keys", 0)
            }
            
            # Calculate hit rate
            total_requests = stats["keyspace_hits"] + stats["keyspace_misses"]
            if total_requests > 0:
                stats["hit_rate"] = stats["keyspace_hits"] / total_requests
            else:
                stats["hit_rate"] = 0.0
                
            return stats
            
        except Exception as e:
            logger.error(f"Failed to get cache stats: {e}")
            return {"enabled": True, "error": str(e)}
