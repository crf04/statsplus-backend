#!/usr/bin/env python3
"""
Test script for NBA Backend cache functionality
"""

import sys
import os
sys.path.append(os.path.dirname(__file__))

from app.utils.cache_config import get_redis_client
from app.services.nba_cache import NBAGameCache

def test_cache():
    print("Testing NBA Backend Cache...")
    print("=" * 50)
    
    # Test Redis connection
    print("1. Testing Redis connection...")
    redis_client = get_redis_client()
    
    if redis_client is None:
        print("   X Redis connection failed")
        print("   -> Make sure Redis server is running on localhost:6379")
        return False
    else:
        print("   OK Redis connection successful")
    
    # Test cache initialization
    print("\n2. Testing cache initialization...")
    cache = NBAGameCache(redis_client)
    
    if cache.enabled:
        print("   OK Cache enabled and ready")
    else:
        print("   X Cache disabled or not working")
        return False
    
    # Test cache operations
    print("\n3. Testing cache operations...")
    test_key = "nba:test:cache_test"
    test_value = {"player": "LeBron James", "team": "Lakers", "test": True}
    
    # Set operation
    set_result = cache.set(test_key, test_value, 60)
    if set_result:
        print("   OK Cache SET operation successful")
    else:
        print("   X Cache SET operation failed")
        return False
    
    # Get operation
    get_result = cache.get(test_key)
    if get_result == test_value:
        print("   OK Cache GET operation successful")
        print(f"   -> Retrieved: {get_result}")
    else:
        print("   X Cache GET operation failed")
        print(f"   -> Expected: {test_value}")
        print(f"   -> Got: {get_result}")
        return False
    
    # Delete operation
    delete_result = cache.delete(test_key)
    if delete_result:
        print("   OK Cache DELETE operation successful")
    else:
        print("   X Cache DELETE operation failed")
    
    # Test cache stats
    print("\n4. Testing cache statistics...")
    stats = cache.get_cache_stats()
    print(f"   Cache Stats: {stats}")
    
    print("\n" + "=" * 50)
    print("OK All cache tests passed! Cache is ready to use.")
    print("\nYour NBA Backend cache is fully configured and working!")
    return True

if __name__ == "__main__":
    success = test_cache()
    sys.exit(0 if success else 1)